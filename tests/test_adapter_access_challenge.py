"""Adapter-only challenges persist host restrictions without any new HTTP."""
import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from peps.collector import SafeFetcher
from peps.engine import Engine
from peps.news import NewsAcquirer
from tests.test_discovery_cache_validation import FixtureFetcher
from tests.test_documented_api_permissions import SOURCE as API_SOURCE


# The adapter understands an absolute action that the transport's narrower
# marker list does not. The action destination must never become the host key.
CHALLENGE = (b'<html><body><form action="https://unrequested.example/anomaly.js?q=example">'
             b'<p>Challenge.</p></form></body></html>')


class AdapterAccessChallengeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.clock = [time.time()]
        self.engine = Engine(self.root, online=True)
        self.news = NewsAcquirer(self.engine, merge_defaults=False)
        self.engine.store.import_entities([{'id': 'example', 'name': 'Persona Pública Ejemplo'}])
        self.source = {'id': 'search', 'kind': 'duckduckgo_html',
            'search_url': 'https://search.example.org/search', 'allowed_hosts': ['search.example.org'],
            'enabled': True, 'terms_approved': True, 'robots_required': True,
            'min_interval_seconds': 1, 'download_articles': False, 'cache_ttl_seconds': 900}
        self.news.config_path.write_text(json.dumps({'sources': [self.source], 'search': {
            'strategy': 'public', 'preset': 'identity', 'include_aliases': False,
            'use_context': False, 'max_queries': 1, 'execution_mode': 'quick', 'session_seconds': 120}}))
        self.news.queue(window='challenge-fixture')
        self.fetcher = FixtureFetcher(self.root, self.clock, [
            (200, {'content-type': 'text/html', 'cache-control': 'max-age=900'}, CHALLENGE)])
        self.addCleanup(self.fetcher.close)
        with self.engine.store.connection() as con:
            self.url = json.loads(con.execute('SELECT payload FROM jobs').fetchone()[0])['url']

    def host_rows(self):
        with self.fetcher._db() as con:
            return [dict(row) for row in con.execute('SELECT * FROM hosts ORDER BY host')]

    def snapshot(self):
        with self.fetcher._db() as con:
            return {table: [tuple(row) for row in con.execute('SELECT * FROM ' + table + ' ORDER BY 1')]
                    for table in ('hosts', 'http_cache', 'request_history', 'robots', 'redirects')}

    def cached_challenge(self, mode):
        self.assertFalse(SafeFetcher._captcha(CHALLENGE.decode()))
        result = self.fetcher.fetch(self.url, self.source)
        self.assertEqual(result['status'], 'ok')
        calls = list(self.fetcher.calls)
        with self.fetcher._db() as con:
            cached_before = tuple(con.execute('SELECT * FROM http_cache').fetchone())
        self.fetcher.config['cache_mode'] = mode
        self.news._active_cache_mode = mode
        self.fetcher._dns = lambda *_a, **_k: self.fail('Cached challenge reporting must avoid DNS')
        job = self.engine.store.claim(kind_prefix='news_query')
        result = self.news.execute(job, self.fetcher)
        self.assertEqual(result['discovery_parse_error'], 'discovery_access_challenge')
        self.assertEqual(self.fetcher.calls, calls)
        rows = self.host_rows()
        self.assertEqual([row['host'] for row in rows], ['search.example.org'])
        self.assertGreater(rows[0]['blocked_until'], self.clock[0])
        with self.fetcher._db() as con:
            self.assertEqual(tuple(con.execute('SELECT * FROM http_cache').fetchone()), cached_before)
            event = con.execute("SELECT network_requests FROM request_history WHERE request_kind='source_validation'").fetchone()
            self.assertEqual(event[0], 0)
        with self.engine.store.connection() as con:
            self.assertEqual(con.execute('SELECT status FROM jobs WHERE id=?', (job['id'],)).fetchone()[0], 'blocked')
        # A different query has no cached body and must still stop before DNS.
        self.fetcher.config['cache_mode'] = 'reuse'
        self.fetcher._dns = lambda *_a, **_k: self.fail('Known access circuit must avoid DNS')
        blocked = self.fetcher.fetch('https://search.example.org/search?q=another', self.source)
        self.assertEqual(blocked['status'], 'blocked')
        self.assertEqual(blocked['retry_class'], 'operator_access')
        self.assertEqual(blocked['network_requests'], 0)
        self.assertEqual(self.fetcher.calls, calls)

    def test_cached_adapter_challenge_blocks_other_queries_without_http(self):
        self.cached_challenge('reuse')

    def test_local_adapter_challenge_blocks_other_queries_without_http(self):
        self.cached_challenge('local')

    def test_out_of_scope_or_mismatched_response_changes_no_policy(self):
        before = self.snapshot()
        self.fetcher._dns = lambda *_a, **_k: self.fail('Reporting a challenge must not resolve DNS')
        for result in (
            {'status': 'ok', 'body': CHALLENGE.decode(), 'final_url': 'https://unrequested.example/challenge'},
            {'status': 'ok', 'body': CHALLENGE.decode(), 'url': 'https://search.example.org/other'},
            {'status': 'error', 'body': CHALLENGE.decode()},
        ):
            with self.subTest(result=result):
                report = self.fetcher.record_access_challenge(self.url, self.source, result)
                self.assertFalse(report['challenge_recorded'])
                self.assertEqual(self.snapshot(), before)

    def test_disabled_source_cannot_write_a_challenge_report(self):
        before = self.snapshot()
        report = self.fetcher.record_access_challenge(self.url, dict(self.source, enabled=False),
            {'status': 'ok', 'body': CHALLENGE.decode()})
        self.assertFalse(report['challenge_recorded'])
        self.assertEqual(self.snapshot(), before)

    def test_snapshot_failure_does_not_drop_an_observed_access_restriction(self):
        self.assertEqual(self.fetcher.fetch(self.url, self.source)['status'], 'ok')
        calls = list(self.fetcher.calls)
        job = self.engine.store.claim(kind_prefix='news_query')
        with patch.object(self.news, '_capture_failed_discovery', side_effect=OSError('Fixture disk error')):
            with self.assertRaisesRegex(OSError, 'Fixture disk error'):
                self.news.execute(job, self.fetcher)
        self.assertGreater(self.host_rows()[0]['blocked_until'], self.clock[0])
        self.assertEqual(self.fetcher.calls, calls)

    def test_documented_api_grant_does_not_expand_to_form_or_article_urls(self):
        source = copy.deepcopy(API_SOURCE)
        source['allowed_hosts'].append('unrequested.example')
        url = 'https://search.example/search?q=example&format=json'
        before = self.snapshot()
        for target in ('https://unrequested.example/article', 'https://search.example/article',
                       'https://search.example/search?q=example&format=html'):
            with self.subTest(target=target):
                report = self.fetcher.record_access_challenge(url, source,
                    {'status': 'ok', 'body': CHALLENGE.decode(), 'final_url': target})
                self.assertFalse(report['challenge_recorded'])
                self.assertEqual(self.snapshot(), before)
        report = self.fetcher.record_access_challenge(url, source,
            {'status': 'ok', 'body': CHALLENGE.decode(), 'url': url, 'final_url': url})
        self.assertTrue(report['challenge_recorded'])
        self.assertEqual(report['admission_host'], 'search.example')
        self.assertEqual([row['host'] for row in self.host_rows()], ['search.example'])

    def test_acquire_session_forwards_cached_challenge_restriction(self):
        self.assertEqual(self.fetcher.fetch(self.url, self.source)['status'], 'ok')
        calls = list(self.fetcher.calls)
        with patch('peps.collector.SafeFetcher', return_value=self.fetcher):
            result = self.news.acquire(workers=1, seconds=1, max_articles=1,
                                       queue=False, cache_mode='local')
        self.assertEqual(result['processed'], 1)
        self.assertEqual(self.fetcher.calls, calls)
        self.assertGreater(self.host_rows()[0]['blocked_until'], self.clock[0])
        self.assertTrue(self.engine.store.verify_audit()['ok'])


if __name__ == '__main__':
    unittest.main()
