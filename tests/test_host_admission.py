"""Isolated host-admission regressions; all HTTP/DNS are synthetic fixtures."""
import json
import socket
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from peps.collector import SafeFetcher
from peps.engine import Engine
from peps.news import NewsAcquirer


PUBLIC_DNS = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('93.184.216.34', 443))]


class EffectiveAdmissionHostTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.fetcher = SafeFetcher({'trust_env_proxy': False, 'cache_ttl_seconds': 0}, Path(folder.name))
        self.addCleanup(self.fetcher.close)
        self.current = time.time()
        self.fetcher._clock = lambda: self.current
        self.fetcher._dns = Mock(return_value=PUBLIC_DNS)
        self.fetcher._transport = Mock(return_value=(200, {'content-type': 'text/plain'}, b'fixture body'))
        self.source = dict(id='fixture', enabled=True, terms_approved=True, robots_required=False,
                           allowed_hosts=['news.example', 'target.example'], min_interval_seconds=5)

    def block_interval(self, host):
        with self.fetcher._db() as db:
            db.execute('INSERT INTO hosts(host,next_at) VALUES(?,?)', (host, self.current + 600))

    def test_direct_wait_identifies_normalized_effective_host_without_network(self):
        self.block_interval('news.example')
        result = self.fetcher.fetch('https://NEWS.example./q', self.source)
        self.assertEqual((result['error_code'], result.get('admission_host')), ('host_interval', 'news.example'))
        self.assertIs(result['request_attempted'], False)
        self.assertEqual(result['network_requests'], 0)
        self.fetcher._dns.assert_not_called()
        self.fetcher._transport.assert_not_called()

    def test_robots_refresh_preserves_effective_wait_host(self):
        self.block_interval('news.example')
        result = self.fetcher.fetch('https://news.example/q', dict(self.source, robots_required=True))
        self.assertEqual((result['error_code'], result.get('admission_host')), ('host_interval', 'news.example'))
        self.assertIs(result['request_attempted'], False)
        self.assertEqual(result['network_requests'], 0)
        self.fetcher._dns.assert_not_called()
        self.fetcher._transport.assert_not_called()

    def test_redirect_wait_identifies_target_not_original_host(self):
        self.block_interval('target.example')
        self.fetcher._transport.return_value = (302, {'location': 'https://target.example/nota'}, b'')
        result = self.fetcher.fetch('https://news.example/redirect', self.source)
        self.assertEqual((result['error_code'], result.get('admission_host')), ('host_interval', 'target.example'))
        self.assertEqual(result['network_requests'], 1)
        self.assertEqual(self.fetcher._transport.call_count, 1)


class HostAdmissionQueueTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.engine = Engine(folder.name, online=True)
        self.news = NewsAcquirer(self.engine)
        self.store = self.engine.store
        self.fetcher = SafeFetcher({'trust_env_proxy': False}, self.engine.data_dir)
        self.addCleanup(self.fetcher.close)
        self.fetcher._dns = Mock(side_effect=AssertionError('DNS is forbidden in admission tests'))
        self.fetcher._transport = Mock(side_effect=AssertionError('HTTP is forbidden in admission tests'))
        self.current = time.time()
        self.target = self.current + 600
        self.sources = [dict(id=name, kind='rss', enabled=True, terms_approved=True,
                             url='https://news.example/feed', robots_required=False,
                             allowed_hosts=['news.example', 'other.example', 'sub.news.example',
                                            'news.example.evil', 'niño.example', 'xn--nio-8ma.example'],
                             download_articles=True, article_terms_approved=True)
                        for name in ('one', 'two')]
        self.news.config_path.write_text(json.dumps({'sources': self.sources, 'search': {'strategy': 'feeds'}}), encoding='utf-8')

    def result(self, **extra):
        return dict(error_code='host_interval', request_attempted=False, admission_host='news.example',
                    retry_at=self.target, **extra)

    def enqueue(self, suffix, host='news.example', kind='news_feed', source_id='one', **values):
        payload = dict(source_id=source_id, url='https://' + host + '/' + suffix)
        if kind == 'news_article':
            payload['article_id'] = suffix
        identifier = self.store.enqueue(kind, payload, key='host-test:' + suffix, due=values.pop('due', 0))
        if values:
            with self.store.connection() as con:
                assignments = ','.join(key + '=?' for key in values)
                con.execute('UPDATE jobs SET ' + assignments + ' WHERE id=?', (*values.values(), identifier))
        return identifier

    def row(self, identifier):
        with self.store.connection() as con:
            return dict(con.execute('SELECT * FROM jobs WHERE id=?', (identifier,)).fetchone())

    def events(self):
        with self.store.connection() as con:
            return [dict(row) for row in con.execute("SELECT * FROM audit WHERE action='news_host_admission_deferred'")]

    def cached(self, url, expires_at):
        with self.fetcher._db() as db:
            db.execute('INSERT INTO http_cache(url,body,content_type,sha256,retrieved_at,fetched_at,expires_at) '
                       'VALUES(?,?,?,?,?,?,?)', (url, 'body', 'text/plain', 'fixture', 'fixture', self.current, expires_at))

    def assert_no_network(self):
        self.fetcher._dns.assert_not_called()
        self.fetcher._transport.assert_not_called()

    def test_300_waiting_host_jobs_do_not_hide_other_host_or_consume_attempts(self):
        identifiers = []
        with self.store.connection() as con:
            for index in range(300):
                identifiers.append(self.store.enqueue('news_feed',
                    {'source_id': 'one', 'url': 'https://news.example/' + str(index)},
                    key='bulk-host:' + str(index), due=index / 1000, connection=con))
        other = self.enqueue('other-host', host='other.example', due=1)
        self.assertEqual(self.news._defer_host_pending(self.fetcher, self.result()), 300)
        claimed = self.store.claim(kind_prefix='news_', preferred_kind='news_article')
        self.assertEqual(claimed['id'], other)
        with self.store.connection() as con:
            rows = con.execute("SELECT status,attempts,due FROM jobs WHERE id!=?", (other,)).fetchall()
        self.assertEqual(len(rows), 300)
        self.assertTrue(all(row['status'] == 'queued' and row['attempts'] == 0 and row['due'] == self.target for row in rows))
        self.assertEqual(len(self.events()), 1)
        self.assertTrue(self.store.verify_audit()['ok'])
        self.assert_no_network()

    def test_session_fetcher_bulk_wait_hook_reaches_other_host_with_four_task_budget(self):
        with self.store.connection() as con:
            for index in range(300):
                self.store.enqueue('news_feed', {'source_id': 'one', 'url': 'https://news.example/session-' + str(index)},
                                   key='session-host:' + str(index), due=index / 1000, connection=con)
        other = self.enqueue('session-other', host='other.example', due=1)
        with self.fetcher._db() as db:
            db.execute('INSERT INTO hosts(host,next_at) VALUES(?,?)', ('news.example', self.target))
        self.fetcher._dns = Mock(return_value=PUBLIC_DNS)
        self.fetcher._transport = Mock(return_value=(200, {'content-type': 'application/rss+xml'},
                                                      b'<rss version="2.0"><channel><title>Empty fixture</title></channel></rss>'))
        with patch('peps.collector.SafeFetcher', return_value=self.fetcher):
            result = self.news.acquire(queue=False, workers=1, max_articles=4, seconds=30)
        self.assertEqual(result['processed'], 2)
        self.assertEqual(self.row(other)['status'], 'done')
        with self.store.connection() as con:
            rows = con.execute('SELECT status,attempts,due FROM jobs WHERE id!=?', (other,)).fetchall()
        self.assertEqual(len(rows), 300)
        self.assertEqual(sum(row['status'] == 'queued' for row in rows), 299)
        self.assertEqual(sum(row['status'] == 'deferred' for row in rows), 1)
        self.assertTrue(all(row['attempts'] == 0 and abs(row['due'] - self.target) < 1e-5 for row in rows))
        events = self.events()
        self.assertEqual(len(events), 1)
        self.assertEqual(json.loads(events[0]['payload'])['count'], 299)
        self.fetcher._transport.assert_called_once()
        self.assertEqual(self.fetcher._transport.call_args.args[0], 'https://other.example/session-other')
        self.fetcher._dns.assert_called_once()
        self.assertTrue(self.store.verify_audit()['ok'])

    def test_exact_normalized_host_matches_sources_not_suffixes(self):
        expected = [self.enqueue('same-one'), self.enqueue('same-two', source_id='two'),
                    self.enqueue('case-dot', host='NEWS.example.')]
        untouched = [self.enqueue('subdomain', host='sub.news.example'),
                     self.enqueue('evil-suffix', host='news.example.evil'),
                     self.enqueue('other', host='other.example')]
        self.assertEqual(self.news._defer_host_pending(self.fetcher, self.result()), len(expected))
        self.assertTrue(all(self.row(identifier)['due'] == self.target for identifier in expected))
        self.assertTrue(all(self.row(identifier)['due'] == 0 for identifier in untouched))
        self.assert_no_network()

    def test_idn_ascii_and_unicode_use_same_exact_host(self):
        first = self.enqueue('unicode', host='niño.example')
        second = self.enqueue('punycode', host='xn--nio-8ma.example')
        other = self.enqueue('unrelated')
        result = self.result()
        result['admission_host'] = 'xn--nio-8ma.example'
        self.assertEqual(self.news._defer_host_pending(self.fetcher, result), 2)
        self.assertEqual((self.row(first)['due'], self.row(second)['due']), (self.target, self.target))
        self.assertEqual(self.row(other)['due'], 0)

    def test_any_http_cache_row_is_excluded_including_expired_and_revalidate(self):
        for mode in ('reuse', 'revalidate'):
            with self.subTest(mode=mode):
                fresh = self.enqueue(mode + '-fresh')
                stale = self.enqueue(mode + '-stale')
                network = self.enqueue(mode + '-network')
                self.cached('https://news.example/' + mode + '-fresh', self.target + 600)
                self.cached('https://news.example/' + mode + '-stale', self.current - 600)
                self.assertEqual(self.news._defer_host_pending(self.fetcher, self.result(), cache_mode=mode), 1)
                self.assertEqual(self.row(fresh)['due'], 0)
                self.assertEqual(self.row(stale)['due'], 0)
                self.assertEqual(self.row(network)['due'], self.target)
        self.assert_no_network()

    def test_local_mode_and_non_admission_results_do_not_write(self):
        identifier = self.enqueue('untouched')
        before = self.row(identifier)
        self.assertEqual(self.news._defer_host_pending(self.fetcher, self.result(), cache_mode='local'), 0)
        invalid = [dict(self.result(), error_code='host_circuit'),
                   dict(self.result(), request_attempted=True),
                   dict(self.result(), request_attempted=0),
                   dict(self.result(), admission_host=''),
                   dict(self.result(), retry_at=float('nan')),
                   dict(self.result(), retry_at=float('inf')),
                   dict(self.result(), retry_at=self.current - 1)]
        for result in invalid:
            with self.subTest(result=result):
                self.assertEqual(self.news._defer_host_pending(self.fetcher, result), 0)
        self.assertEqual(self.row(identifier), before)
        self.assertEqual(self.events(), [])
        self.assert_no_network()

    def test_future_due_error_attempts_and_owners_preserved_with_one_batch_audit(self):
        first = self.enqueue('deferred', status='deferred', attempts=3, due=self.current + 5,
                             error='HTTP 429: Retry-After retained', owner='previous-owner')
        later = self.enqueue('later', status='deferred', attempts=2, due=self.target + 500,
                             error='longer server backoff', owner='other-owner')
        before_first, before_later = self.row(first), self.row(later)
        self.assertEqual(self.news._defer_host_pending(self.fetcher, self.result()), 1)
        after = self.row(first)
        for key in ('status', 'attempts', 'error', 'owner', 'lease_until', 'payload', 'created_at'):
            self.assertEqual(after[key], before_first[key])
        self.assertEqual(after['due'], self.target)
        self.assertEqual(self.row(later), before_later)
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(self.news._defer_host_pending(self.fetcher, self.result()), 0)
        self.assertEqual(len(self.events()), 1)

    def test_running_jobs_and_same_url_pending_duplicates_are_preserved(self):
        active = self.enqueue('active', status='running', attempts=2, owner='worker-owner', lease_until=self.target)
        expired = self.enqueue('expired', status='running', attempts=1, owner='old-worker', lease_until=self.current - 1)
        unknown = self.enqueue('unknown-lease', status='running', attempts=1, owner='unknown-worker', lease_until=None)
        duplicate = self.store.enqueue('news_feed', {'source_id': 'two', 'url': 'https://news.example/active'}, key='duplicate-active', due=0)
        duplicate_unknown = self.store.enqueue('news_feed', {'source_id': 'two', 'url': 'https://news.example/unknown-lease'}, key='duplicate-unknown', due=0)
        waiting = self.enqueue('ordinary-waiting')
        preserved = {identifier: self.row(identifier) for identifier in (active, expired, unknown, duplicate, duplicate_unknown)}
        self.assertEqual(self.news._defer_host_pending(self.fetcher, self.result()), 1)
        self.assertEqual(self.row(waiting)['due'], self.target)
        for identifier, original in preserved.items():
            self.assertEqual(self.row(identifier), original)
        active_job = dict(preserved[active], payload=json.loads(preserved[active]['payload']))
        self.assertFalse(self.store.finish(dict(active_job, owner='wrong-owner')))
        self.assertTrue(self.store.finish(active_job))
        self.assertEqual(self.row(active)['status'], 'done')

    def test_other_host_ready_article_stays_preferred_over_older_discovery(self):
        self.enqueue('blocked-discovery', due=0)
        discovery = self.enqueue('other-discovery', host='other.example', due=0)
        body = self.enqueue('ready-body', host='other.example', kind='news_article', due=1)
        self.assertEqual(self.news._defer_host_pending(self.fetcher, self.result()), 1)
        self.assertEqual(self.store.claim(kind_prefix='news_', preferred_kind='news_article')['id'], body)
        self.assertEqual(self.store.claim(kind_prefix='news_', preferred_kind='news_article')['id'], discovery)

    def test_cached_redirect_and_unknown_source_are_excluded(self):
        redirected = self.enqueue('cached-redirect')
        stale_redirect = self.enqueue('stale-redirect')
        unknown = self.enqueue('missing-source', source_id='not-configured')
        network = self.enqueue('ordinary-network')
        with self.fetcher._db() as db:
            db.execute('INSERT INTO redirects(url,target,expires_at) VALUES(?,?,?)',
                       ('https://news.example/cached-redirect', 'https://other.example/final', self.target))
            db.execute('INSERT INTO redirects(url,target,expires_at) VALUES(?,?,?)',
                       ('https://news.example/stale-redirect', 'https://other.example/stale', self.current - 600))
        self.assertEqual(self.news._defer_host_pending(self.fetcher, self.result()), 1)
        self.assertEqual(self.row(redirected)['due'], 0)
        self.assertEqual(self.row(stale_redirect)['due'], 0)
        self.assertEqual(self.row(unknown)['due'], 0)
        self.assertEqual(self.row(network)['due'], self.target)

    def test_query_with_mismatched_current_execution_url_is_not_guessed_at(self):
        query = self.enqueue('query-payload', kind='news_query', host='other.example')
        with patch.object(self.news, '_query_url', return_value='https://news.example/computed-query'):
            self.assertEqual(self.news._defer_host_pending(self.fetcher, self.result()), 0)
        self.assertEqual(self.row(query)['due'], 0)

    def test_query_matching_current_execution_url_accepts_iso_deadline(self):
        query = self.enqueue('query-exact', kind='news_query')
        result = self.result()
        result['retry_at'] = datetime.fromtimestamp(self.target, timezone.utc).isoformat()
        with patch.object(self.news, '_query_url', return_value='https://news.example/query-exact'):
            self.assertEqual(self.news._defer_host_pending(self.fetcher, result), 1)
        self.assertAlmostEqual(self.row(query)['due'], self.target, places=5)

    def test_even_pending_row_with_live_lease_is_preserved(self):
        identifier = self.enqueue('pending-live-lease', lease_until=self.target, owner='worker')
        original = self.row(identifier)
        self.assertEqual(self.news._defer_host_pending(self.fetcher, self.result()), 0)
        self.assertEqual(self.row(identifier), original)

    def test_payload_changed_during_cache_snapshot_cannot_receive_stale_host_due(self):
        identifier = self.enqueue('payload-changes')
        def change_payload(urls):
            urls = set(urls)
            with self.store.connection() as con:
                con.execute('UPDATE jobs SET payload=? WHERE id=?',
                            (json.dumps({'source_id': 'one', 'url': 'https://other.example/changed'}), identifier))
            return urls
        with patch.object(self.fetcher, 'admission_uncached_urls', side_effect=change_payload):
            self.assertEqual(self.news._defer_host_pending(self.fetcher, self.result()), 0)
        self.assertEqual(self.row(identifier)['due'], 0)
        self.assertEqual(self.events(), [])

    def test_duplicate_producer_completing_after_snapshot_protects_local_read(self):
        identifier = self.enqueue('producer-race')
        def complete_new_producer(urls):
            urls = set(urls)
            producer = self.store.enqueue('news_feed', {'source_id': 'two', 'url': 'https://news.example/producer-race'},
                                          key='concurrent-producer', due=-100)
            job = self.store.claim(owner='producer', kind_prefix='news_')
            self.assertIsNotNone(job)
            self.assertEqual(job['id'], producer)
            self.assertNotEqual(job['id'], identifier)
            self.assertTrue(self.store.finish(job))
            return urls
        with patch.object(self.fetcher, 'admission_uncached_urls', side_effect=complete_new_producer):
            self.assertEqual(self.news._defer_host_pending(self.fetcher, self.result()), 0)
        self.assertEqual(self.row(identifier)['due'], 0)
        self.assertEqual(self.events(), [])

    def test_concurrent_claim_preserves_new_owner_and_lease(self):
        identifier = self.enqueue('claimed-during-snapshot')
        claimed = []
        def claim(urls):
            urls = set(urls)
            claimed.append(self.store.claim(owner='concurrent-owner', kind_prefix='news_'))
            return urls
        with patch.object(self.fetcher, 'admission_uncached_urls', side_effect=claim):
            self.assertEqual(self.news._defer_host_pending(self.fetcher, self.result()), 0)
        row = self.row(identifier)
        self.assertEqual((row['status'], row['owner'], row['attempts'], row['due']),
                         ('running', 'concurrent-owner', 1, 0))
        self.assertEqual(row['lease_until'], claimed[0]['lease_until'])
        self.assertTrue(self.store.finish(claimed[0]))
        self.assertEqual(self.events(), [])

    def test_malformed_and_out_of_scope_rows_do_not_poison_valid_batch(self):
        invalid = []
        for index, url in enumerate(('http://news.example/plain', 'https://u:p@news.example/credential',
                                     'https://unapproved.example/n', 'https://news.example/bad\r\nheader')):
            identifier = self.store.enqueue('news_feed', {'source_id': 'one', 'url': url},
                                            key='invalid-url-' + str(index), due=0)
            invalid.append(identifier)
        malformed = self.enqueue('malformed-json')
        with self.store.connection() as con:
            con.execute('UPDATE jobs SET payload=? WHERE id=?', ('{broken json', malformed))
        valid = self.enqueue('valid-alongside-invalid')
        self.assertEqual(self.news._defer_host_pending(self.fetcher, self.result()), 1)
        self.assertEqual(self.row(valid)['due'], self.target)
        self.assertTrue(all(self.row(identifier)['due'] == 0 for identifier in invalid + [malformed]))
        self.assert_no_network()

    def test_pause_between_cache_read_and_queue_write_does_not_change_due(self):
        identifier = self.enqueue('pause-during-snapshot')
        def pause(urls):
            urls = set(urls)
            self.engine.stop_event.set()
            return urls
        with patch.object(self.fetcher, 'admission_uncached_urls', side_effect=pause):
            self.assertEqual(self.news._defer_host_pending(self.fetcher, self.result()), 0)
        self.assertEqual(self.row(identifier)['due'], 0)
        self.assertEqual(self.events(), [])


if __name__ == '__main__':
    unittest.main()
