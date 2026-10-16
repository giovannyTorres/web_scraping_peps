"""A successful original without body is terminal, not a network retry.

Fixtures replace only transport and DNS; production fetch policy, robots,
cache, parser, SQLite queue, leases and audit remain in use. Zero host interval
is solely a no-network fixture convenience, never a publisher setting.
"""
from collections import Counter
import json
from pathlib import Path
import socket
import sqlite3
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
import threading
from unittest.mock import patch
from urllib.parse import urlsplit

from peps.collector import SafeFetcher
from peps.engine import Engine
from peps.news import NewsAcquirer, article_text
from peps.store import now


class FixtureHTTP(SafeFetcher):
    responses, calls = {}, []
    api_payload, forbid_dns = None, False
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        def resolve(*_args, **_kwargs):
            if self.forbid_dns:
                raise AssertionError('DNS forbidden during local replay')
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))]
        self._dns = resolve
    def _transport(self, url, headers, timeout, max_bytes, addresses):
        self.calls.append((url, dict(headers)))
        if url.endswith('/robots.txt'):
            return 200, {'content-type': 'text/plain'}, b'User-agent: *\nAllow: /\n'
        if self.api_payload is not None and urlsplit(url).path == '/wp-json/wp/v2/posts':
            return 200, {'content-type': 'application/json', 'cache-control': 'max-age=900',
                         'etag': 'api-v1', 'x-wp-total': str(len(self.api_payload)), 'x-wp-totalpages': '1'}, json.dumps(self.api_payload).encode()
        return self.responses[url]


class InsufficientArticleTerminalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.engine = Engine(self.directory, online=True)
        self.news = NewsAcquirer(self.engine)
        self.engine.store.import_entities([{'id': 'synthetic', 'name': 'Persona Pública Sintética'}])
        self.source = self.make_source('one')
        self.configure(self.source)
        FixtureHTTP.responses, FixtureHTTP.calls = {}, []
        FixtureHTTP.api_payload, FixtureHTTP.forbid_dns = None, False
        self.short_html = '<html><head><title>Referencia</title></head><article class="entry-content">Texto de veinte chars</article></html>'
        self.good_html = '<html><head><title>Referencia original</title></head><article class="entry-content"><p>' + ('Persona Pública Sintética presenta un informe público ante su comunidad. ' * 8) + '</p></article></html>'

    @staticmethod
    def make_source(name):
        return {'id': name, 'kind': 'rss', 'url': 'https://' + name + '.example.org/feed',
                'allowed_hosts': [name + '.example.org'], 'enabled': True,
                'terms_approved': True, 'download_articles': True, 'article_terms_approved': True,
                'body_terms_approved': True, 'robots_required': True, 'min_interval_seconds': 0,
                'minimum_article_chars': 80, 'timeout_seconds': 1}

    def configure(self, *sources):
        self.news.config_path.write_text(json.dumps({'sources': list(sources), 'search': {'strategy': 'feeds'}}))

    def seed(self, url, source=None, html=None, status=200):
        source = source or self.source
        self.news._observe({'url': url, 'title': 'Persona Pública Sintética presenta informe',
                            'text': 'Referencia pública sin cuerpo.'}, source, source['url'],
                           {'retrieved_at': now(), 'sha256': 'fixture'}, None, True)
        FixtureHTTP.responses[url] = (status, {'content-type': 'text/html', 'cache-control': 'max-age=900',
                                               'etag': 'fixture-v1'}, (html or self.short_html).encode())
        with self.engine.store.connection() as con:
            return con.execute("SELECT id FROM jobs WHERE kind='news_article' "
                               "AND json_extract(payload,'$.url')=? ORDER BY created_at DESC LIMIT 1", (url,)).fetchone()[0]

    def run_acquire(self, **kwargs):
        with patch('peps.collector.SafeFetcher', FixtureHTTP):
            return self.news.acquire(queue=False, workers=2, seconds=kwargs.pop('seconds', 3), **kwargs)

    def job(self, identifier):
        with self.engine.store.connection() as con:
            return dict(con.execute('SELECT * FROM jobs WHERE id=?', (identifier,)).fetchone())

    def test_short_success_is_blocked_once_without_automatic_cache_reparse(self):
        url = 'https://one.example.org/short';identifier = self.seed(url)
        events = []
        with patch('peps.news.article_text', wraps=article_text) as extraction:
            first = self.run_acquire(progress=events.append)
            second = self.run_acquire()
            self.assertEqual(extraction.call_count, 1)
        self.assertEqual(self.job(identifier)['status'], 'blocked')
        self.assertEqual(self.job(identifier)['attempts'], 1)
        self.assertTrue(self.job(identifier)['error'].startswith('article_text_insufficient:'))
        self.assertEqual(first['pending'], second['pending'])
        self.assertEqual(first['pending'], 0)
        self.assertEqual(first['jobs']['blocked'], 1)
        self.assertEqual(FixtureHTTP.calls, [('https://one.example.org/robots.txt', unittest.mock.ANY),
                                            (url, unittest.mock.ANY)])
        self.assertEqual(self.engine.store.stats()['documents'], 0)
        with self.engine.store.connection() as con:
            row = con.execute('SELECT scope,status,error,document_id FROM news_articles').fetchone()
            self.assertEqual(row['scope'], 'feed_metadata_only')
            self.assertEqual(row['status'], 'blocked')
            self.assertIsNone(row['document_id'])
            self.assertTrue(row['error'].startswith('article_text_insufficient:'))
            self.assertEqual(con.execute('SELECT count(*) FROM news_observations').fetchone()[0], 1)
        with sqlite3.connect(self.directory / 'network.sqlite3') as con:
            self.assertEqual(con.execute('SELECT sum(network_requests) FROM request_history').fetchone()[0], 2)
            self.assertEqual(con.execute('SELECT body FROM http_cache WHERE url=?', (url,)).fetchone()[0], self.short_html)
        self.assertTrue(self.engine.store.verify_audit()['ok'])

        self.assertTrue(any(event.get('job_status') == 'blocked' for event in events))

    def make_legacy(self, identifier, *, status='deferred', error='_InsufficientArticleText: Cuerpo insuficiente', lease=None):
        with self.engine.store.connection() as con:
            con.execute('UPDATE jobs SET status=?,attempts=3,due=?,owner=?,lease_until=?,error=? WHERE id=?',
                        (status, time.time() + 3600, 'historical-owner', lease, error, identifier))

    def test_legacy_future_backoff_closes_locally_and_preserves_cached_originals_and_valid_document(self):
        good_url, short_url = 'https://one.example.org/good-legacy', 'https://one.example.org/short-legacy'
        good_id = self.seed(good_url, html=self.good_html)
        self.run_acquire()
        short_id = self.seed(short_url)
        with FixtureHTTP({}, self.directory) as fetcher:
            self.assertEqual(fetcher.fetch(short_url, self.source)['status'], 'ok')
        for identifier in (good_id, short_id):
            self.make_legacy(identifier, lease=time.time() - 1)
        before_jobs = {identifier: self.job(identifier) for identifier in (good_id, short_id)}
        with self.engine.store.connection() as con:
            valid_before = dict(con.execute('SELECT * FROM news_articles WHERE canonical_url=?', (good_url,)).fetchone())
            documents_before = [tuple(row) for row in con.execute('SELECT * FROM documents')]
            observations_before = [tuple(row) for row in con.execute('SELECT * FROM news_observations ORDER BY id')]
        with sqlite3.connect(self.directory / 'network.sqlite3') as con:
            cache_before = con.execute('SELECT * FROM http_cache ORDER BY url').fetchall()
            http_before = con.execute('SELECT sum(network_requests) FROM request_history').fetchone()[0]
        with patch.object(FixtureHTTP, '_transport', side_effect=AssertionError('No HTTP for legacy settlement')) as transport, \
             patch.object(FixtureHTTP, 'forbid_dns', True), \
             patch('socket.socket.connect', side_effect=AssertionError('No TCP for legacy settlement')) as tcp:
            result = self.run_acquire(cache_mode='local')
            self.assertEqual(result['pending'], 0)
            self.assertEqual(self.news.settle_legacy_insufficient_articles(), 0)
        self.assertEqual(transport.call_count, 0)
        self.assertEqual(tcp.call_count, 0)
        for identifier, before in before_jobs.items():
            after = self.job(identifier)
            self.assertEqual(after['status'], 'blocked')
            self.assertTrue(after['error'].startswith('article_text_insufficient:'))
            for field in ('due', 'attempts', 'owner', 'lease_until', 'payload'):
                self.assertEqual(after[field], before[field], field)
        with self.engine.store.connection() as con:
            self.assertEqual(dict(con.execute('SELECT * FROM news_articles WHERE canonical_url=?', (good_url,)).fetchone()), valid_before)
            self.assertEqual([tuple(row) for row in con.execute('SELECT * FROM documents')], documents_before)
            self.assertEqual([tuple(row) for row in con.execute('SELECT * FROM news_observations ORDER BY id')], observations_before)
            audits = [json.loads(row[0]) for row in con.execute("SELECT payload FROM audit WHERE action='news_legacy_insufficient_articles_settled'")]
            self.assertEqual(len(audits), 1)
            self.assertEqual(set(audits[0]['job_ids']), {good_id, short_id})
        with sqlite3.connect(self.directory / 'network.sqlite3') as con:
            self.assertEqual(con.execute('SELECT * FROM http_cache ORDER BY url').fetchall(), cache_before)
            self.assertEqual(con.execute('SELECT sum(network_requests) FROM request_history').fetchone()[0], http_before)
        self.assertTrue(self.engine.store.verify_audit()['ok'])

    def test_legacy_settlement_skips_live_running_unrelated_and_malformed_payloads(self):
        cases = [('eligible', 'queued', '_InsufficientArticleText: exact', None),
                 ('live', 'deferred', '_InsufficientArticleText: exact', time.time() + 3600),
                 ('running', 'running', '_InsufficientArticleText: exact', time.time() - 1),
                 ('other', 'deferred', 'HTTP 503: _InsufficientArticleText: not prefix', None),
                 ('wildcard', 'deferred', 'xInsufficientArticleText: not exact', None),
                 ('malformed', 'deferred', '_InsufficientArticleText: exact', None)]
        ids = {}
        for name, status, error, lease in cases:
            ids[name] = self.seed('https://one.example.org/' + name)
            self.make_legacy(ids[name], status=status, error=error, lease=lease)
        with self.engine.store.connection() as con:
            con.execute('UPDATE jobs SET payload=? WHERE id=?', ('{invalid', ids['malformed']))
        before = {name: self.job(identifier) for name, identifier in ids.items()}
        self.assertEqual(self.news.settle_legacy_insufficient_articles(), 1)
        for name, identifier in ids.items():
            if name != 'eligible':
                self.assertEqual(self.job(identifier), before[name])
        self.assertEqual(self.news.settle_legacy_insufficient_articles(), 0)
        self.assertTrue(self.engine.store.verify_audit()['ok'])

    def test_legacy_new_owner_and_live_lease_winning_write_transaction_are_preserved(self):
        identifier = self.seed('https://one.example.org/lease-race')
        self.make_legacy(identifier)
        started = threading.Event()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.engine.store.connection() as con:
                con.execute('BEGIN IMMEDIATE')
                def settle():
                    started.set()
                    return self.news.settle_legacy_insufficient_articles()
                future = pool.submit(settle)
                self.assertTrue(started.wait(2))
                con.execute("UPDATE jobs SET status='running',owner='new-worker',lease_until=? WHERE id=?",
                            (time.time() + 300, identifier))
            self.assertEqual(future.result(timeout=3), 0)
        self.assertEqual(self.job(identifier)['owner'], 'new-worker')
        self.assertEqual(self.job(identifier)['status'], 'running')

    def test_legacy_settlement_rolls_back_all_changes_when_its_batch_audit_fails(self):
        identifier = self.seed('https://one.example.org/audit-rollback')
        self.make_legacy(identifier)
        before = self.job(identifier)
        with patch.object(self.engine.store, '_audit', side_effect=RuntimeError('Fixture audit failure')):
            with self.assertRaisesRegex(RuntimeError, 'Fixture audit failure'):
                self.news.settle_legacy_insufficient_articles()
        self.assertEqual(self.job(identifier), before)
        self.engine.stop_event.set()
        self.assertEqual(self.news.settle_legacy_insufficient_articles(), 0)
        self.assertEqual(self.job(identifier), before)
        self.assertTrue(self.engine.store.verify_audit()['ok'])

    def test_explicit_manual_retry_and_revalidation_can_acquire_a_changed_original(self):
        url = 'https://one.example.org/changed';identifier = self.seed(url)
        self.run_acquire()
        retried = self.engine.store.retry_news_jobs([identifier])
        self.assertEqual(retried['retried'], 1)
        FixtureHTTP.responses[url] = (200, {'content-type': 'text/html', 'etag': 'fixture-v2'}, self.good_html.encode())
        result = self.run_acquire(cache_mode='revalidate')
        self.assertEqual(result['pending'], 0)
        self.assertEqual(self.job(identifier)['status'], 'done')
        self.assertEqual(self.job(identifier)['attempts'], 1)
        self.assertEqual(self.engine.store.stats()['documents'], 1)
        self.assertEqual(len(FixtureHTTP.calls), 3)
        self.assertEqual(FixtureHTTP.calls[-1][1].get('If-None-Match'), 'fixture-v1')
        self.assertTrue(self.engine.store.verify_audit()['ok'])

    def test_another_host_valid_body_finishes_without_waiting_for_short_original(self):
        other = self.make_source('two');self.configure(self.source, other)
        self.seed('https://one.example.org/short')
        self.seed('https://two.example.org/good', other, self.good_html)
        result = self.run_acquire()
        self.assertEqual(result['pending'], 0)
        self.assertEqual(result['jobs']['blocked'], 1)
        self.assertEqual(result['jobs']['done'], 1)
        self.assertEqual(self.engine.store.stats()['documents'], 1)
        self.assertEqual(self.news.stats()['scopes']['full_article'], 1)
        self.assertTrue(self.engine.store.verify_audit()['ok'])

    def test_transient_http_failure_keeps_backoff_and_is_never_terminal_quality_failure(self):
        identifier = self.seed('https://one.example.org/transient', status=503)
        result = self.run_acquire(max_articles=1)
        job = self.job(identifier)
        self.assertEqual(job['status'], 'deferred')
        self.assertGreater(job['due'], time.time() + 20)
        self.assertEqual(result['pending'], 1)
        self.assertNotIn('article_text_insufficient', job['error'])
        self.assertEqual(self.engine.store.stats()['documents'], 0)

    def test_empty_explicit_editorial_container_never_falls_back_to_long_related_navigation(self):
        html = '<div class="entry-content"></div><aside><article>' + self.good_html + '</article></aside>'
        identifier = self.seed('https://one.example.org/empty-main', html=html)
        self.run_acquire()
        self.assertEqual(self.job(identifier)['status'], 'blocked')
        self.assertEqual(self.engine.store.stats()['documents'], 0)

    def test_invalid_discovery_response_stays_a_parser_retry_not_article_quality_block(self):
        self.engine.store.enqueue('news_feed', {'source_id': 'one', 'url': self.source['url']})
        FixtureHTTP.responses[self.source['url']] = (200, {'content-type': 'text/html'}, b'<html>Not an RSS feed</html>')
        result = self.run_acquire(max_articles=1)
        self.assertEqual(result['pending'], 1)
        with self.engine.store.connection() as con:
            row = con.execute("SELECT status,error FROM jobs WHERE kind='news_feed'").fetchone()
            self.assertEqual(row['status'], 'deferred')
            self.assertTrue(row['error'].startswith('ValueError:'))
            self.assertNotIn('article_text_insufficient', row['error'])
        self.assertEqual(self.news.stats()['articles'], 0)

    def test_two_queued_local_query_replays_preserve_blocked_rows_and_never_reparse_body(self):
        source = dict(self.source, kind='wordpress_api', search_kind='wordpress_api_search',
                      url='https://one.example.org/wp-json/wp/v2/posts', base_url='https://one.example.org/',
                      search_max_records=10)
        self.news.config_path.write_text(json.dumps({'sources': [source], 'search': {
            'strategy': 'targeted', 'preset': 'identity', 'include_aliases': False,
            'use_context': False, 'execution_mode': 'quick', 'max_queries': 1,
            'start': '2020-01-01', 'end': '2020-12-31'}}))
        url = 'https://one.example.org/short'
        FixtureHTTP.api_payload = [{'id': 1, 'link': url, 'date_gmt': '2020-02-03T12:00:00',
                                   'title': {'rendered': 'Persona Pública Sintética presenta un informe'},
                                   'excerpt': {'rendered': '<p>Persona Pública Sintética participa.</p>'}}]
        FixtureHTTP.responses[url] = (200, {'content-type': 'text/html', 'cache-control': 'max-age=900'}, self.short_html.encode())
        def snapshot():
            with self.engine.store.connection() as con:
                return {table: [tuple(row) for row in con.execute('SELECT * FROM ' + table + ' ORDER BY id')]
                        for table in ('news_articles', 'news_observations', 'documents')}
        with patch('peps.collector.SafeFetcher', FixtureHTTP), patch('peps.news.article_text', wraps=article_text) as extraction:
            first = self.news.acquire(queue=True, workers=2, seconds=3, window='short-cold')
            before = snapshot()
            with self.engine.store.connection() as con:
                original_body_jobs = [tuple(row) for row in con.execute("SELECT * FROM jobs WHERE kind='news_article' ORDER BY id")]
            self.assertEqual(len(FixtureHTTP.calls), 3)
            with patch.object(FixtureHTTP, '_transport', side_effect=AssertionError('HTTP forbidden during local replay')) as transport, \
                 patch.object(FixtureHTTP, 'forbid_dns', True), \
                 patch('socket.socket.connect', side_effect=AssertionError('TCP forbidden during local replay')) as tcp:
                for number in range(2):
                    result = self.news.acquire(queue=True, workers=2, seconds=3, window='short-local-' + str(number), cache_mode='local')
                    self.assertEqual(result['pending'], 0)
                    self.assertEqual(snapshot(), before)
                    with self.engine.store.connection() as con:
                        self.assertEqual([tuple(row) for row in con.execute("SELECT * FROM jobs WHERE kind='news_article' ORDER BY id")], original_body_jobs)
            self.assertEqual(transport.call_count, 0)
            self.assertEqual(tcp.call_count, 0)
            self.assertEqual(extraction.call_count, 1)
        self.assertEqual(first['pending'], 0)
        self.assertEqual(first['jobs']['blocked'], 1)
        self.assertEqual(len(FixtureHTTP.calls), 3)
        self.assertTrue(self.engine.store.verify_audit()['ok'])


    def test_local_missing_frontier_reserves_entity_quota_until_explicit_network_retry(self):
        self.seed('https://one.example.org/already-conserved', html=self.good_html)
        self.run_acquire()
        self.assertEqual(self.engine.store.stats()['documents'], 1)
        source = dict(self.source, kind='wordpress_api', search_kind='wordpress_api_search',
                      url='https://one.example.org/wp-json/wp/v2/posts', base_url='https://one.example.org/',
                      search_max_records=10)
        self.news.config_path.write_text(json.dumps({'sources': [source], 'search': {
            'strategy': 'targeted', 'preset': 'identity', 'include_aliases': False,
            'use_context': False, 'execution_mode': 'quick', 'max_queries': 1,
            'start': '2020-01-01', 'end': '2020-12-31'}}))
        FixtureHTTP.api_payload = [{'id': number, 'link': 'https://one.example.org/missing-' + str(number),
            'date_gmt': '2020-02-03T12:00:00',
            'title': {'rendered': 'Persona Pública Sintética presenta informe ' + str(number)},
            'excerpt': {'rendered': '<p>Persona Pública Sintética participa.</p>'}} for number in (1, 2)]
        def snapshot():
            with self.engine.store.connection() as con:
                return {table: [tuple(row) for row in con.execute('SELECT * FROM ' + table + ' ORDER BY id')]
                        for table in ('news_articles', 'news_observations', 'documents')}
        def body_jobs():
            with self.engine.store.connection() as con:
                return [dict(row) for row in con.execute("SELECT * FROM jobs WHERE kind='news_article' ORDER BY id")]
        with patch('peps.collector.SafeFetcher', FixtureHTTP):
            self.news.acquire(queue=True, workers=2, seconds=3, window='missing-seed', body_budget=0, per_entity_body_limit=2)
            self.assertEqual(len(FixtureHTTP.calls), 3)
            self.assertEqual(len(body_jobs()), 1)
            self.assertEqual(body_jobs()[0]['status'], 'done')
            with patch.object(FixtureHTTP, '_transport', side_effect=AssertionError('HTTP forbidden during local replay')) as transport, \
                 patch.object(FixtureHTTP, 'forbid_dns', True), \
                 patch('socket.socket.connect', side_effect=AssertionError('TCP forbidden during local replay')) as tcp:
                self.news.acquire(queue=True, workers=2, seconds=3, window='missing-local-1', cache_mode='local', body_budget=2, per_entity_body_limit=2)
                before, before_jobs = snapshot(), body_jobs()
                self.assertEqual(len(before_jobs), 2)
                missing_jobs = [row for row in before_jobs if row['status'] == 'blocked']
                self.assertEqual(len(missing_jobs), 1)
                self.assertTrue(missing_jobs[0]['error'].startswith('local_cache_miss:'))
                result = self.news.acquire(queue=True, workers=2, seconds=3, window='missing-local-2', cache_mode='local', body_budget=2, per_entity_body_limit=2)
                self.assertEqual(result['pending'], 0)
                self.assertEqual(snapshot(), before)
                self.assertEqual(body_jobs(), before_jobs)
            self.assertEqual(transport.call_count, 0)
            self.assertEqual(tcp.call_count, 0)
            self.assertEqual(len(FixtureHTTP.calls), 3)
            identifier = missing_jobs[0]['id']
            original_url = json.loads(missing_jobs[0]['payload'])['url']
            changed_html = self.good_html.replace('presenta un informe público', 'publica una nueva nota documental')
            FixtureHTTP.responses[original_url] = (200, {'content-type': 'text/html'}, changed_html.encode())
            self.assertEqual(self.engine.store.retry_news_jobs([identifier])['retried'], 1)
            result = self.news.acquire(queue=False, workers=2, seconds=3, cache_mode='revalidate', body_budget=2, per_entity_body_limit=2)
            self.assertEqual(result['pending'], 0)
            self.assertEqual(self.job(identifier)['status'], 'done')
            self.assertEqual(self.engine.store.stats()['documents'], 2)
            # Revalidation also refreshes the previously conserved original.
            # Worker order may vary; each authorized URL is requested once.
            self.assertEqual(len(FixtureHTTP.calls), 5)
            self.assertEqual(Counter(call[0] for call in FixtureHTTP.calls[3:]),
                             Counter([original_url, 'https://one.example.org/already-conserved']))
            with self.engine.store.connection() as con:
                saved = con.execute('SELECT d.text FROM news_articles n JOIN documents d ON d.id=n.document_id WHERE n.canonical_url=?', (original_url,)).fetchone()[0]
                self.assertIn('publica una nueva nota documental', saved)
        self.assertTrue(self.engine.store.verify_audit()['ok'])


if __name__ == '__main__':
    unittest.main()
