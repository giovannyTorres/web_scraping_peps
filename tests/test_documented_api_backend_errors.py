"""Backend JSON diagnostics must not become API-host denials; no real HTTP."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from peps.collector import SafeFetcher
from peps.engine import Engine
from peps.news import NewsAcquirer
from peps.public_search import PublicSearchParseError, parse_public_results, public_search_url
from tests.test_documented_api_permissions import SOURCE


MARKER = 'Suspended: verify you are human'
EMPTY = {'results': [], 'unresponsive_engines': [['fixture_backend', MARKER]]}
RECORD = {'url': 'https://publisher.example.org/story', 'title': 'Información sintética',
          'content': 'Una referencia pública ficticia.', 'engine': 'fixture_backend',
          'engines': ['fixture_backend']}


def reply(payload, *, mime='application/json; charset=utf-8', status=200, headers=None):
    raw = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False, indent=2).encode('utf-8')
    return status, {'content-type': mime, 'cache-control': 'max-age=900', **(headers or {})}, raw


class DocumentedApiBackendErrorsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = [1800000000.]
        self.sequence = 0
        self.url = public_search_url('Persona enteramente ficticia', SOURCE)

    def fetcher(self, response):
        self.sequence += 1
        fetcher = SafeFetcher({'trust_env_proxy': False}, self.root / str(self.sequence))
        self.addCleanup(fetcher.close)
        fetcher._clock = lambda: self.now[0]
        fetcher._dns = lambda *_a, **_k: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))]
        calls = []
        def transport(url, *_args):
            calls.append(url)
            if url.endswith('/robots.txt'):
                return 200, {'content-type': 'text/plain'}, b'User-agent: *\nDisallow: /*?*q=*\n'
            self.assertEqual(url, self.url)
            return response
        fetcher._transport = transport
        return fetcher, calls

    def fetch_ready(self, fetcher, source=SOURCE):
        result = fetcher.fetch(self.url, source)
        if result['status'] == 'deferred' and result.get('error_code') == 'host_interval':
            self.now[0] += 2
            result = fetcher.fetch(self.url, source)
        return result

    def host(self, fetcher):
        with fetcher._db() as con:
            return dict(con.execute("SELECT * FROM hosts WHERE host='search.example'").fetchone())

    def test_empty_backend_challenge_preserves_raw_receipt_and_blocks_only_backend(self):
        response = reply(EMPTY)
        fetcher, calls = self.fetcher(response)
        result = self.fetch_ready(fetcher)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['error_code'], 'discovery_backend_restricted')
        self.assertIsNone(result['retry_at'])
        self.assertEqual(result['body'], response[2].decode('utf-8'))
        self.assertEqual(result['sha256'], hashlib.sha256(response[2]).hexdigest())
        with self.assertRaises(PublicSearchParseError) as raised:
            parse_public_results(dict(result, status='ok'), SOURCE)
        self.assertEqual(raised.exception.code, 'discovery_upstream_unavailable')
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(self.host(fetcher)['blocked_until'], 0)
        with fetcher._db() as con:
            captured = con.execute('SELECT body,response_sha256 FROM api_backend_observations').fetchone()
            self.assertEqual(con.execute('SELECT count(*) FROM http_cache').fetchone()[0], 0)
        self.assertEqual(tuple(captured), (response[2], result['sha256']))
        again = fetcher.fetch(self.url, SOURCE)
        self.assertEqual(again['error_code'], 'discovery_backend_restricted')
        self.assertEqual(again['network_requests'], 0)
        self.assertEqual(calls, ['https://search.example/robots.txt', self.url])

    def test_partial_results_retain_backend_diagnostics_without_host_denial(self):
        payload = dict(EMPTY, results=[RECORD])
        fetcher, _calls = self.fetcher(reply(payload))
        result = self.fetch_ready(fetcher)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['error_code'], 'discovery_backend_restricted')
        self.assertEqual(json.loads(result['body'])['unresponsive_engines'], EMPTY['unresponsive_engines'])
        self.assertEqual(self.host(fetcher)['blocked_until'], 0)

    def test_production_job_blocks_without_retry_candidate_promotion_or_host_ban(self):
        engine = Engine(self.root / 'bank', online=True)
        engine.store.import_entities([{'id': 'fixture', 'name': 'Persona enteramente ficticia'}])
        news = NewsAcquirer(engine, merge_defaults=False)
        news.config_path.write_text(json.dumps({'sources': [SOURCE], 'search': {
            'strategy': 'public', 'preset': 'identity', 'include_aliases': False, 'use_context': False,
            'max_queries': 1, 'execution_mode': 'quick', 'session_seconds': 120}}), encoding='utf-8')
        news.queue(window='backend-error-fixture')
        fetcher, _calls = self.fetcher(reply(EMPTY))
        job = engine.store.claim(kind_prefix='news_query')
        outcome = news.execute(job, fetcher)
        with engine.store.connection() as con:
            state = con.execute('SELECT status,error FROM jobs WHERE id=?', (job['id'],)).fetchone()
        if state['status'] == 'deferred':
            self.now[0] += 2
            with engine.store.connection() as con:
                con.execute('UPDATE jobs SET due=0 WHERE id=?', (job['id'],))
            job = engine.store.claim(kind_prefix='news_query')
            outcome = news.execute(job, fetcher)
        self.assertIsNone(outcome)
        with engine.store.connection() as con:
            state = con.execute('SELECT status,error FROM jobs WHERE id=?', (job['id'],)).fetchone()
            self.assertEqual(state['status'], 'blocked')
            self.assertIn('backend requires manual review', state['error'])
            self.assertEqual(con.execute('SELECT count(*) FROM documents').fetchone()[0], 0)
            self.assertEqual(con.execute('SELECT count(*) FROM news_articles').fetchone()[0], 0)
        with fetcher._db() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM http_cache').fetchone()[0], 0)
            self.assertEqual(con.execute('SELECT response_sha256 FROM api_backend_observations').fetchone()[0],
                             hashlib.sha256(reply(EMPTY)[2]).hexdigest())
        self.assertEqual(self.host(fetcher)['blocked_until'], 0)

    def test_global_message_error_and_result_challenges_are_still_host_denials(self):
        for extra in ({'message': 'verify you are human'}, {'error': '<title>Access denied'},
                      {'results': [dict(RECORD, content='verify you are human')]}):
            with self.subTest(field=next(iter(extra))):
                fetcher, _calls = self.fetcher(reply(dict(EMPTY, **extra)))
                result = self.fetch_ready(fetcher)
                self.assertEqual(result['error_code'], 'access_challenge')
                self.assertGreater(self.host(fetcher)['blocked_until'], self.now[0])

    def test_acquire_session_keeps_partial_backend_denial_terminal_and_unpromoted(self):
        engine = Engine(self.root / 'session-bank', online=True)
        engine.store.import_entities([{'id': 'fixture', 'name': 'Persona enteramente ficticia'}])
        news = NewsAcquirer(engine, merge_defaults=False)
        news.config_path.write_text(json.dumps({'sources': [SOURCE], 'search': {
            'strategy': 'public', 'preset': 'identity', 'include_aliases': False, 'use_context': False,
            'max_queries': 1, 'execution_mode': 'quick', 'session_seconds': 120}}), encoding='utf-8')
        news.queue(window='backend-session-fixture')
        fetcher, calls = self.fetcher(reply(dict(EMPTY, results=[RECORD])))
        with fetcher._db() as con:
            con.execute('INSERT INTO robots VALUES(?,?,?,?)', ('https://search.example',
                        'User-agent: *\nAllow: /\n', self.now[0] + 3600, self.now[0]))
        with patch('peps.collector.SafeFetcher', return_value=fetcher):
            result = news.acquire(workers=1, seconds=1, max_articles=1, queue=False)
        self.assertEqual(result['processed'], 1)
        self.assertEqual(calls, [self.url])
        with engine.store.connection() as con:
            state = con.execute('SELECT status,attempts FROM jobs WHERE kind=?', ('news_query',)).fetchone()
            self.assertEqual(tuple(state), ('blocked', 1))
            self.assertEqual(con.execute('SELECT count(*) FROM news_articles').fetchone()[0], 0)
            self.assertEqual(con.execute('SELECT count(*) FROM documents').fetchone()[0], 0)
        self.assertTrue(engine.store.verify_audit()['ok'])

    def test_wrong_mime_malformed_json_and_invalid_envelopes_are_not_exempt(self):
        cases = [
            reply(EMPTY, mime='text/html'), reply(EMPTY, mime='text/plain'),
            reply(b'<html><title>Just a moment</title>verify you are human</html>'),
            reply(reply(EMPTY)[2] + b' invalid trailing JSON'),
            reply(dict(EMPTY, results={})), reply({'results': [], 'unresponsive_engines': MARKER}),
            reply(b'{"results":[],"unresponsive_engines":["verify you are human"],"value":NaN}'),
            reply(b'{"message":"verify you are human","message":"","results":[],"unresponsive_engines":[]}'),
        ]
        for index, response in enumerate(cases):
            with self.subTest(case=index):
                fetcher, _calls = self.fetcher(response)
                self.assertEqual(self.fetch_ready(fetcher)['error_code'], 'access_challenge')

    def test_generic_or_unreviewed_sources_do_not_receive_the_exception(self):
        generic = dict(SOURCE, robots_required=False)
        generic.pop('documented_api_permission')
        fetcher, _calls = self.fetcher(reply(EMPTY))
        self.assertEqual(self.fetch_ready(fetcher, generic)['error_code'], 'access_challenge')
        unreviewed = deepcopy(SOURCE)
        unreviewed['documented_api_permission']['reviewed'] = False
        fetcher, calls = self.fetcher(reply(EMPTY))
        self.assertEqual(self.fetch_ready(fetcher, unreviewed)['error_code'], 'documented_api_scope')
        self.assertEqual(calls, [])

    def test_unknown_backend_diagnostic_shapes_with_challenges_fail_closed(self):
        diagnostics = [
            [MARKER], [{'error': MARKER}],
            [[MARKER]], [['fixture_backend', MARKER, 'unexpected']],
            [[None, MARKER]], [['fixture_backend', {'error': MARKER}]],
            [[['fixture_backend'], MARKER]],
            [['fixture_backend', MARKER], {'error': MARKER}],
            [['fixture_backend', MARKER], ['unexpected', 1]],
        ]
        for index, unresponsive in enumerate(diagnostics):
            with self.subTest(case=index):
                fetcher, _calls = self.fetcher(reply({'results': [], 'unresponsive_engines': unresponsive}))
                result = self.fetch_ready(fetcher)
                self.assertEqual(result['error_code'], 'access_challenge')
                self.assertGreater(self.host(fetcher)['blocked_until'], self.now[0])

    def test_http_access_denials_and_retry_after_still_restrict_future_requests(self):
        for status in (403, 451, 429, 503):
            with self.subTest(status=status):
                fetcher, calls = self.fetcher(reply(EMPTY, status=status, headers={'Retry-After': '120'}))
                result = self.fetch_ready(fetcher)
                self.assertEqual(result['status'], 'blocked' if status in (403, 451) else 'deferred')
                before = self.host(fetcher)
                self.now[0] += 10
                fetcher._dns = lambda *_a, **_k: self.fail('Known restriction attempted DNS')
                count = len(calls)
                again = fetcher.fetch(self.url, SOURCE)
                self.assertEqual(again['network_requests'], 0)
                self.assertEqual(len(calls), count)
                after = self.host(fetcher)
                self.assertGreaterEqual(after['blocked_until'], before['blocked_until'])
                self.assertGreaterEqual(after['server_retry_until'], before['server_retry_until'])
                self.assertGreaterEqual(after['next_at'], before['next_at'])

    def test_inflight_backend_response_cannot_clear_new_operator_or_server_floor(self):
        for blocked in (False, True):
            with self.subTest(operator_blocked=blocked):
                fetcher, calls = self.fetcher(reply(dict(EMPTY, results=[RECORD])))
                original = fetcher._transport
                floors = []
                def transport(url, *args):
                    if not url.endswith('/robots.txt'):
                        fetcher._backoff('search.example', {'Retry-After': '300'}, blocked=blocked,
                                         reason='Fixture restriction arriving after admission')
                        floors.append(self.host(fetcher))
                    return original(url, *args)
                fetcher._transport = transport
                self.assertEqual(self.fetch_ready(fetcher)['status'], 'blocked')
                before, after = floors[0], self.host(fetcher)
                for key in ('blocked_until', 'server_retry_until', 'next_at'):
                    self.assertGreaterEqual(after[key], before[key])
                fetcher._dns = lambda *_a, **_k: self.fail('Known restriction attempted DNS')
                count = len(calls)
                fresh_url = public_search_url('Otra persona completamente ficticia', SOURCE)
                result = fetcher.fetch(fresh_url, SOURCE)
                self.assertEqual(result['status'], 'blocked')
                self.assertEqual(result['error_code'], 'discovery_backend_restricted')
                self.assertEqual(result['network_requests'], 0)
                self.assertEqual(len(calls), count)


if __name__ == '__main__':
    unittest.main()
