"""Cache controls use real SQLite and injected HTTP; no public network."""
import json
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing, contextmanager
from itertools import count
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from peps.collector import SafeFetcher
from peps.engine import Engine
from peps.news import NewsAcquirer
from peps.news_session import AcquisitionSession, last_session
from peps.store import digest, now


PUBLIC = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('93.184.216.34', 443))]


class CollectorCacheControls(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fetcher = SafeFetcher({'trust_env_proxy': False}, Path(self.temp.name))
        self.addCleanup(self.fetcher.close)
        self.clock = [1_800_000_000.]
        self.fetcher._clock = lambda: self.clock[0]
        self.fetcher._dns = lambda *a, **kw: PUBLIC
        self.source = dict(id='fixture', kind='wordpress_api', enabled=True, terms_approved=True,
                           robots_required=False, allowed_hosts=['news.example'], min_interval_seconds=0,
                           max_bytes=100_000, cache_ttl_seconds=600)
        self.url = 'https://news.example/wp-json/wp/v2/posts?page=3'

    def test_local_and_warm_reuse_have_no_dns_or_http_and_keep_pagination(self):
        transport = self.fetcher._transport = Mock(return_value=(200,
            {'content-type': 'application/json', 'x-wp-total': '300', 'x-wp-totalpages': '3'}, b'[]'))
        original = self.fetcher.fetch(self.url, self.source)
        self.fetcher._dns = Mock(side_effect=AssertionError('DNS on cached request'))
        for mode in ('reuse', 'local'):
            result = self.fetcher.fetch(self.url, dict(self.source, _cache_mode=mode))
            self.assertEqual(result['network_requests'], 0)
            self.assertEqual(result['pagination_headers'], original['pagination_headers'])
            self.assertEqual(result['retrieved_at'], original['retrieved_at'])
            self.assertEqual(result['validated_at'], original['validated_at'])
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(self.fetcher.metrics_snapshot()['cache_hits'], 2)

    def test_revalidate_uses_conditional_http_and_separates_validation_from_retrieval(self):
        transport = self.fetcher._transport = Mock(side_effect=[
            (200, {'etag': '"v1"', 'x-wp-totalpages': '3'}, b'[]'), (304, {}, b'')])
        original = self.fetcher.fetch(self.url, self.source)
        self.clock[0] += 10
        checked = self.fetcher.fetch(self.url, dict(self.source, _cache_mode='revalidate'))
        self.assertEqual(checked['network_requests'], 1)
        self.assertEqual(transport.call_args.args[1]['If-None-Match'], '"v1"')
        self.assertEqual(checked['retrieved_at'], original['retrieved_at'])
        self.assertNotEqual(checked['validated_at'], original['validated_at'])
        self.assertEqual(checked['pagination_headers'], {'x-wp-totalpages': '3'})

    def test_cached_redirect_obeys_current_robots_for_both_original_and_final_urls(self):
        final = 'https://news.example/editorial/article'
        self.fetcher._transport = Mock(side_effect=[(302, {'location': final}, b''), (200, {}, b'Editorial fixture')])
        self.fetcher.fetch(self.url, self.source)
        self.fetcher._dns = Mock(side_effect=AssertionError('DNS while rejecting cache'))
        with self.fetcher._db() as con:
            con.execute('INSERT OR REPLACE INTO robots VALUES(?,?,?,?)',
                        ('https://news.example', 'User-agent: *\nDisallow: /editorial/\n', self.clock[0] + 100, self.clock[0]))
        for mode in ('reuse', 'local'):
            result = self.fetcher.fetch(self.url, dict(self.source, robots_required=True, _cache_mode=mode))
            self.assertEqual(result['status'], 'robots_denied')
            self.assertEqual(result['network_requests'], 0)

    def test_local_miss_is_explicit_and_cannot_start_robots_or_dns(self):
        self.fetcher.offline = True
        self.fetcher._dns = Mock(side_effect=AssertionError('DNS on local miss'))
        self.fetcher._transport = Mock(side_effect=AssertionError('HTTP on local miss'))
        result = self.fetcher.fetch(self.url, dict(self.source, _cache_mode='local', robots_required=True))
        self.assertEqual(result['error_code'], 'local_cache_miss')
        self.assertEqual(result['network_requests'], 0)

    def test_parsed_index_keeps_redirect_policy_after_http_cache_is_removed(self):
        engine = Engine(self.temp.name)
        news = NewsAcquirer(engine)
        source = dict(self.source, api_url=self.url)
        final = 'https://news.example/editorial-index/posts'
        self.fetcher._transport = Mock(side_effect=[(302, {'location': final}, b''), (200, {}, b'[]')])
        result, _parsed = news._shared_page(source, {'url': self.url, 'page': 3}, self.fetcher)
        self.assertEqual(result['status'], 'ok')
        with self.fetcher._db() as con:
            con.execute('DELETE FROM http_cache')
            con.execute('INSERT OR REPLACE INTO robots VALUES(?,?,?,?)',
                        ('https://news.example', 'User-agent: *\nDisallow: /editorial-index/\n', self.clock[0] + 100, self.clock[0]))
        self.fetcher._dns = Mock(side_effect=AssertionError('DNS during metadata reuse'))
        self.fetcher._transport = Mock(side_effect=AssertionError('HTTP during metadata reuse'))
        news._active_cache_mode = 'local'
        result, parsed = news._shared_page(dict(source, robots_required=True), {'url': self.url, 'page': 3}, self.fetcher)
        self.assertEqual(result['status'], 'robots_denied')
        self.assertIsNone(parsed)
        self.fetcher._dns.assert_not_called()
        self.fetcher._transport.assert_not_called()

    def test_legacy_parsed_index_recovers_redirect_from_http_cache_and_fails_local_if_unknown(self):
        engine = Engine(self.temp.name)
        news = NewsAcquirer(engine)
        source = dict(self.source, api_url=self.url)
        self.fetcher._transport = Mock(return_value=(200, {}, b'[]'))
        news._shared_page(source, {'url': self.url, 'page': 3}, self.fetcher)
        with news.store.connection() as con:
            row = con.execute('SELECT parsed FROM news_shared_pages').fetchone()
            parsed = json.loads(row['parsed'])
            parsed.pop('_response_final_url')
            con.execute('UPDATE news_shared_pages SET parsed=?', (json.dumps(parsed),))
        news._active_cache_mode = 'local'
        self.fetcher.config['cache_mode'] = 'local'
        result, parsed = news._shared_page(source, {'url': self.url, 'page': 3}, self.fetcher)
        self.assertTrue(result['metadata_cache_hit'])
        with self.fetcher._db() as con:
            con.execute('DELETE FROM http_cache')
        self.fetcher._dns = Mock(side_effect=AssertionError('DNS during legacy local miss'))
        result, parsed = news._shared_page(source, {'url': self.url, 'page': 3}, self.fetcher)
        self.assertEqual(result['error_code'], 'local_cache_miss')
        self.assertIsNone(parsed)

    def test_duckduckgo_challenge_is_detected_without_matching_legitimate_snippet(self):
        self.assertTrue(self.fetcher._captcha('<form action="/anomaly.js?x=1"><div id="anomaly-modal"></div></form>'))
        self.assertFalse(self.fetcher._captcha('<article>El informe explica qué es un captcha.</article>'))


class PipelineCacheControls(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.engine = Engine(self.temp.name, online=False)
        self.news = NewsAcquirer(self.engine)
        self.store = self.engine.store
        self.source = dict(id='fixture', kind='rss', enabled=True, terms_approved=True,
                           robots_required=False, allowed_hosts=['news.example'], url='https://news.example/feed',
                           min_interval_seconds=0, download_articles=False)
        self.save()

    def save(self, strategy='feeds'):
        self.news.config_path.write_text(json.dumps({'sources': [self.source], 'search': {'strategy': strategy}}))

    def job(self, *, kind='news_feed', status='queued', error='', attempts=0, due=None):
        identifier = self.store.enqueue(kind, {'source_id': 'fixture', 'url': self.source['url']},
                                        key=digest(str(time.monotonic_ns())))
        with self.store.connection() as con:
            con.execute('UPDATE jobs SET status=?,error=?,attempts=?,due=? WHERE id=?',
                        (status, error, attempts, due or time.time(), identifier))
        return identifier

    def test_offline_local_miss_finishes_once_without_spending_attempt_and_can_resume_online(self):
        identifier = self.job(due=time.time() + 3600)
        with patch('socket.getaddrinfo', side_effect=AssertionError('DNS in local acquisition')):
            result = self.news.acquire(queue=False, cache_mode='local', seconds=1)
        self.assertEqual(result['processed'], 1)
        self.assertEqual(result['session']['network_requests'], 0)
        self.assertIsNotNone(result['session']['finished_at'])
        with self.store.connection() as con:
            row = dict(con.execute('SELECT * FROM jobs WHERE id=?', (identifier,)).fetchone())
        self.assertEqual(row['status'], 'blocked')
        self.assertEqual(row['attempts'], 0)
        other = self.job(status='blocked', error='HTTP 403', attempts=3)
        self.news._resume_local_misses()
        with self.store.connection() as con:
            states = {r['id']: dict(r) for r in con.execute('SELECT * FROM jobs')}
        self.assertEqual(states[identifier]['status'], 'queued')
        self.assertEqual(states[identifier]['attempts'], 0)
        self.assertEqual(states[other]['status'], 'blocked')
        self.assertEqual(states[other]['attempts'], 3)
        self.assertTrue(self.store.verify_audit()['ok'])

    def test_local_cached_feed_reports_live_real_cache_hits(self):
        identifier = self.job()
        fetcher = SafeFetcher(self.engine.config(), self.engine.data_dir)
        self.addCleanup(fetcher.close)
        fetcher._dns = lambda *a, **kw: PUBLIC
        fetcher._transport = Mock(return_value=(200, {'content-type': 'application/rss+xml'},
            b'<rss><channel><title>Fixture</title></channel></rss>'))
        fetcher.fetch(self.source['url'], self.source)
        observed = []
        def progress(_payload):
            observed.append(last_session(self.store))
        ticks = count(0, 2)
        with patch('peps.news_session.time', SimpleNamespace(monotonic=lambda: next(ticks))), \
             patch('socket.getaddrinfo', side_effect=AssertionError('DNS in local acquisition')):
            result = self.news.acquire(queue=False, cache_mode='local', seconds=1, progress=progress)
        self.assertEqual(result['completed'], 1)
        self.assertEqual(result['session']['http_cache_hits'], 1)
        self.assertEqual(result['session']['network_requests'], 0)
        self.assertEqual(result['session']['bank_revision'], '0')
        self.assertTrue(any(s and s['http_cache_hits'] == 1 and not s['finished_at'] for s in observed))
        self.assertEqual(last_session(self.store), result['session'])

    def test_targeted_retry_preserves_other_jobs_live_leases_and_future_due(self):
        later = time.time() + 3600
        blocked = self.job(status='blocked', error='HTTP 429', attempts=4, due=later)
        dead = self.job(status='dead', attempts=5)
        running = self.job(status='running', attempts=2)
        ordinary = self.job(kind='fetch', status='blocked', attempts=3)
        with self.store.connection() as con:
            con.execute("UPDATE jobs SET owner='live',lease_until=? WHERE id=?", (later, running))
        result = self.store.retry_news_jobs([blocked, dead, running, ordinary, blocked])
        self.assertEqual(result['retried'], 2)
        self.assertEqual(result['requested'], 4)
        with self.store.connection() as con:
            rows = {r['id']: dict(r) for r in con.execute('SELECT * FROM jobs')}
        self.assertEqual(rows[blocked]['due'], later)
        self.assertEqual(rows[blocked]['attempts'], 0)
        self.assertEqual(rows[running]['owner'], 'live')
        self.assertEqual(rows[running]['lease_until'], later)
        self.assertEqual(rows[ordinary]['status'], 'blocked')
        for bad in ([], ['not-an-id'], [blocked] * 101):
            with self.assertRaises(ValueError):
                self.store.retry_news_jobs(bad)
        self.assertTrue(self.store.verify_audit()['ok'])

    def test_session_publications_cannot_regress_when_first_writer_is_delayed(self):
        session = AcquisitionSession(self.store, 'reuse')
        connection = self.store.connection
        entered, release = threading.Event(), threading.Event()
        calls = [0]
        @contextmanager
        def slow_first():
            calls[0] += 1
            if calls[0] == 1:
                entered.set()
                self.assertTrue(release.wait(2))
            with connection() as con:
                yield con
        with patch.object(self.store, 'connection', slow_first):
            first = threading.Thread(target=lambda: session.save(force=True))
            first.start()
            self.assertTrue(entered.wait(2))
            session.note(network_requests=7)
            second = threading.Thread(target=lambda: session.save(force=True))
            second.start()
            release.set()
            first.join(2)
            second.join(2)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(last_session(self.store)['network_requests'], 7)

    def test_unknown_transport_metrics_remain_null(self):
        session = AcquisitionSession(self.store, 'reuse')
        session.note(network_requests=None)
        session.note(network_requests=2)
        session.save(force=True)
        self.assertIsNone(last_session(self.store)['network_requests'])

    def test_queue_closes_readonly_cache_connections_without_relying_on_garbage_collection(self):
        fetcher = SafeFetcher(self.engine.config(), self.engine.data_dir)
        fetcher.close()
        original_connect = sqlite3.connect
        retained = []
        class TrackedConnection(sqlite3.Connection):
            close_calls = 0
            def close(self):
                self.close_calls += 1
                super().close()
        def connect(database, *args, **kwargs):
            if kwargs.get('uri') and str(database).endswith('?mode=ro'):
                con = original_connect(database, *args, **dict(kwargs, factory=TrackedConnection))
                retained.append(con)
                return con
            return original_connect(database, *args, **kwargs)
        # Keep every connection strongly referenced. Linux permits unlinking an
        # open SQLite file; Windows does not, so file cleanup alone misses this
        # leak on Linux. Assert actual release before queue() returns each time.
        with patch('peps.news.sqlite3.connect', side_effect=connect):
            for iteration in range(3):
                self.news.queue(window=f'explicit-close-{iteration}')
                self.assertEqual(len(retained), iteration + 1)
                self.assertTrue(all(con.close_calls == 1 for con in retained))
                with self.assertRaises(sqlite3.ProgrammingError):
                    retained[-1].execute('SELECT 1')
            # An old/unusable HTTP cache is an accepted miss, but still owns a
            # live connection until explicitly closed in the exception path.
            with closing(original_connect(self.engine.data_dir / 'network.sqlite3')) as writer:
                writer.execute('DROP TABLE http_cache')
            self.news.queue(window='explicit-close-unusable-cache')
        self.assertEqual(len(retained), 4)
        self.assertTrue(all(con.close_calls == 1 for con in retained))
        with self.assertRaises(sqlite3.ProgrammingError):
            retained[-1].execute('SELECT 1')


if __name__ == '__main__':
    unittest.main()
