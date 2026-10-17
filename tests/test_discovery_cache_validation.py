"""Response failures must not become permanently fresh search results.

These are functional fixtures using production fetch/cache/queue controls;
only DNS and HTTPS transport are replaced. No volume or retrieval claim.
"""
import gzip
import hashlib
import json
from pathlib import Path
import socket
import tempfile
import time
import unittest
from unittest.mock import patch

from peps.collector import SafeFetcher
from peps.engine import Engine
from peps.news import NewsAcquirer
from peps.public_search import PublicSearchParseError, parse_public_results


BAD = json.dumps({'results': [], 'unresponsive_engines': [['upstream', 'timeout']]}, ensure_ascii=False).encode()
GOOD = json.dumps({'results': [{'url': 'https://publisher.example.org/story',
    'title': 'Persona Pública Ejemplo presenta informe', 'content': 'Información pública de ejemplo.'}]},
    ensure_ascii=False).encode()


class FixtureFetcher(SafeFetcher):
    def __init__(self, root, clock, replies):
        super().__init__({'user_agent': 'CacheValidationFixture/1', 'cache_ttl_seconds': 900,
                          'trust_env_proxy': False}, root)
        self._clock = lambda: clock[0]
        self._dns = lambda *_a, **_k: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))]
        self.clock, self.replies, self.calls = clock, list(replies), []

    def _transport(self, url, headers, *_args):
        self.calls.append((url, dict(headers)))
        self.clock[0] += 2
        if url.endswith('/robots.txt'):
            return 200, {'content-type': 'text/plain'}, b'User-agent: *\nAllow: /\n'
        if not self.replies:
            raise AssertionError('Unexpected extra HTTPS request: ' + url)
        return self.replies.pop(0)


def reply(body, **headers):
    return 200, {'content-type': 'application/json; charset=utf-8',
                 'cache-control': 'max-age=900', 'etag': '"fixture"', **headers}, body


class DiscoveryCacheValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.clock = [time.time()]
        timer = patch('peps.store.time.time', side_effect=lambda: self.clock[0])
        timer.start()
        self.addCleanup(timer.stop)
        self.engine = Engine(self.root, online=True)
        self.news = NewsAcquirer(self.engine, merge_defaults=False)
        self.store = self.engine.store
        self.store.import_entities([{'id': 'example', 'name': 'Persona Pública Ejemplo'}])
        self.source = {'id': 'search', 'kind': 'searxng', 'search_url': 'https://search.example.org/search',
            'allowed_hosts': ['search.example.org'], 'enabled': True, 'terms_approved': True,
            'robots_required': True, 'min_interval_seconds': 1, 'download_articles': False,
            'max_records': 20}
        self.save()
        self.news.queue(window='fixture')

    def save(self):
        self.news.config_path.write_text(json.dumps({'sources': [self.source], 'search': {
            'strategy': 'public', 'preset': 'identity', 'include_aliases': False,
            'use_context': False, 'max_queries': 1, 'execution_mode': 'quick', 'session_seconds': 120}}))

    def fetcher(self, *replies):
        fetcher = FixtureFetcher(self.root, self.clock, replies)
        self.addCleanup(fetcher.close)
        return fetcher

    def execute(self, fetcher):
        job = self.store.claim(kind_prefix='news_query')
        self.assertIsNotNone(job)
        result = self.news.execute(job, fetcher)
        with self.store.connection() as con:
            state = dict(con.execute('SELECT * FROM jobs WHERE id=?', (job['id'],)).fetchone())
        return result, state

    def cached(self, fetcher):
        with fetcher._db() as con:
            return dict(con.execute('SELECT * FROM http_cache').fetchone())

    def failures(self):
        with self.store.connection() as con:
            return [json.loads(row[0]) for row in con.execute(
                "SELECT payload FROM audit WHERE action='news_discovery_response_invalid' ORDER BY seq")]

    def assert_no_documentary_evidence(self):
        with self.store.connection() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM documents').fetchone()[0], 0)

    def test_bad200_expires_then_retry_fetches_same_url_and_preserves_failed_bytes(self):
        fetcher = self.fetcher(reply(BAD), reply(GOOD))
        result, failed = self.execute(fetcher)
        self.assertEqual(result['discovery_parse_error'], 'discovery_upstream_unavailable')
        self.assertEqual(failed['status'], 'deferred')
        cached = self.cached(fetcher)
        self.assertLessEqual(cached['expires_at'], self.clock[0])
        self.assertEqual(cached['sha256'], hashlib.sha256(BAD).hexdigest())
        capture = self.failures()[0]['snapshot']
        snapshot = self.root / capture['snapshot_path']
        self.assertEqual(gzip.decompress(snapshot.read_bytes()), BAD)
        self.assertTrue(capture['original_response_bytes_verified'])
        self.assert_no_documentary_evidence()
        self.clock[0] = failed['due'] + 1
        _result, completed = self.execute(fetcher)
        self.assertEqual(completed['status'], 'done')
        requests = [r for r in fetcher.calls if not r[0].endswith('/robots.txt')]
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0][0], requests[1][0])
        self.assertNotIn('If-None-Match', requests[1][1])
        self.assertEqual(self.cached(fetcher)['validation_error'], '')
        self.assertEqual(gzip.decompress(snapshot.read_bytes()), BAD)
        with self.store.connection() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM news_articles').fetchone()[0], 1)
        self.assert_no_documentary_evidence()
        self.assertTrue(self.store.verify_audit()['ok'])

    def test_valid_empty_and_partial_responses_keep_freshness(self):
        for payload in [{'results': []}, json.loads(GOOD) | {'unresponsive_engines': [['other', 'timeout']]}]:
            with self.subTest(payload=payload):
                result = {'status': 'ok', 'body': json.dumps(payload)}
                records = parse_public_results(result, self.source)
                self.assertEqual(bool(records), bool(payload['results']))
                self.assertEqual(result['parse_metadata']['partial_upstream_response'], bool(payload.get('unresponsive_engines')))
        fetcher = self.fetcher(reply(b'{"results":[]}'))
        _result, state = self.execute(fetcher)
        self.assertEqual(state['status'], 'done')
        cached = self.cached(fetcher)
        self.assertGreater(cached['expires_at'], self.clock[0])
        self.assertEqual(cached['validation_error'], '')
        self.assertEqual(self.failures(), [])
        self.assertFalse((self.root / 'news_discovery_failures').exists())

    def test304_does_not_restore_freshness_of_invalid_envelope(self):
        fetcher = self.fetcher(reply(BAD), (304, {'etag': '"fixture"'}, b''), reply(GOOD))
        _result, first = self.execute(fetcher)
        self.clock[0] = first['due'] + 1
        _result, second = self.execute(fetcher)
        self.assertEqual(second['status'], 'deferred')
        self.assertIn('304', second['error'])
        self.assertLessEqual(self.cached(fetcher)['expires_at'], self.clock[0])
        self.assertEqual(self.cached(fetcher)['validation_error'], 'discovery_upstream_unavailable')
        self.clock[0] = second['due'] + 1
        _result, final = self.execute(fetcher)
        self.assertEqual(final['status'], 'done')
        self.assertEqual(len(self.failures()), 1)

    def test_local_bad_copy_blocks_without_new_http(self):
        fetcher = self.fetcher(reply(BAD))
        with self.store.connection() as con:
            payload = json.loads(con.execute('SELECT payload FROM jobs').fetchone()[0])
        self.assertEqual(fetcher.fetch(payload['url'], self.source)['status'], 'ok')
        calls = list(fetcher.calls)
        fetcher.config['cache_mode'] = 'local'
        self.news._active_cache_mode = 'local'
        _result, state = self.execute(fetcher)
        self.assertEqual(state['status'], 'blocked')
        self.assertEqual(fetcher.calls, calls)
        self.assertTrue(self.failures()[0]['local_only'])
        self.assertEqual(fetcher.fetch(payload['url'], self.source)['error_code'], 'invalid_cached_response')
        self.assertEqual(fetcher.calls, calls)
        self.news.queue(window='second-local-session', cache_mode='local')
        _result, repeated = self.execute(fetcher)
        self.assertEqual(repeated['status'], 'blocked')
        self.assertIn('copia local', repeated['error'])
        self.assertEqual(fetcher.calls, calls)
        self.assertEqual(len(self.failures()), 1)

    def test_config_valueerror_never_invalidates_a_valid_http_cache(self):
        fetcher = self.fetcher(reply(GOOD))
        self.source['max_records'] = 'invalid configuration'
        self.save()
        job = self.store.claim(kind_prefix='news_query')
        with self.assertRaises(ValueError) as raised:
            self.news.execute(job, fetcher)
        self.assertNotIsInstance(raised.exception, PublicSearchParseError)
        self.assertGreater(self.cached(fetcher)['expires_at'], self.clock[0])
        self.assertEqual(self.cached(fetcher)['validation_error'], '')
        self.assertEqual(self.failures(), [])

    def test_host_circuit_remains_binding_after_expiry(self):
        fetcher = self.fetcher(reply(BAD))
        _result, failed = self.execute(fetcher)
        calls = list(fetcher.calls)
        fetcher._backoff('search.example.org', blocked=True, reason='Fixture access restriction')
        self.clock[0] = failed['due'] + 1
        _result, blocked = self.execute(fetcher)
        self.assertEqual(blocked['status'], 'blocked')
        self.assertEqual(fetcher.calls, calls)
        self.assertEqual(self.cached(fetcher)['validation_error'], 'discovery_upstream_unavailable')

    def test_source_revocation_remains_binding_after_expiry(self):
        fetcher = self.fetcher(reply(BAD))
        _result, failed = self.execute(fetcher)
        calls = list(fetcher.calls)
        self.source['enabled'] = False
        self.save()
        self.clock[0] = failed['due'] + 1
        _result, state = self.execute(fetcher)
        self.assertEqual(state['status'], 'blocked')
        self.assertEqual(fetcher.calls, calls)

    def test_robots_denial_remains_binding_after_expiry(self):
        fetcher = self.fetcher(reply(BAD))
        _result, failed = self.execute(fetcher)
        calls = list(fetcher.calls)
        with fetcher._db() as con:
            con.execute('UPDATE robots SET body=?', ('User-agent: *\nDisallow: /\n',))
        self.clock[0] = failed['due'] + 1
        _result, state = self.execute(fetcher)
        self.assertEqual(state['status'], 'blocked')
        self.assertIn('robots', state['error'])
        self.assertEqual(fetcher.calls, calls)

    def test_older_parse_failure_cannot_invalidate_newer_concurrent_representation(self):
        fetcher = self.fetcher(reply(BAD))
        original_fetch = fetcher.fetch
        def replaced_after_fetch(url, source):
            result = original_fetch(url, source)
            with fetcher._db() as con:
                con.execute('UPDATE http_cache SET body=?,sha256=?,expires_at=? WHERE url=?',
                    (GOOD.decode(), hashlib.sha256(GOOD).hexdigest(), self.clock[0] + 900, url))
            return result
        fetcher.fetch = replaced_after_fetch
        result, failed = self.execute(fetcher)
        self.assertFalse(result['cache_freshness_expired'])
        cached = self.cached(fetcher)
        self.assertEqual(cached['sha256'], hashlib.sha256(GOOD).hexdigest())
        self.assertEqual(cached['validation_error'], '')
        self.assertGreater(cached['expires_at'], self.clock[0])
        self.assertTrue(self.failures()[0]['snapshot']['original_response_bytes_verified'])
        fetcher.fetch = original_fetch
        calls = list(fetcher.calls)
        self.clock[0] = failed['due'] + 1
        _result, final = self.execute(fetcher)
        self.assertEqual(final['status'], 'done')
        self.assertEqual(fetcher.calls, calls)

    def test_failed_snapshot_is_verified_and_never_overwritten(self):
        fetcher = self.fetcher(reply(BAD))
        self.execute(fetcher)
        capture = self.failures()[0]['snapshot']
        snapshot = self.root / capture['snapshot_path']
        snapshot.write_bytes(gzip.compress(b'Corrupted fixture file'))
        result = {'body': BAD.decode(), 'sha256': hashlib.sha256(BAD).hexdigest(),
                  'content_type': 'application/json; charset=utf-8'}
        with self.assertRaisesRegex(ValueError, 'immutable content hash'):
            self.news._capture_failed_discovery('https://search.example.org/search', self.source,
                                                result, 'discovery_upstream_unavailable')
        self.assertEqual(gzip.decompress(snapshot.read_bytes()), b'Corrupted fixture file')

    def test_malformed_empty_body_is_also_captured_and_expired(self):
        fetcher = self.fetcher(reply(b''))
        result, state = self.execute(fetcher)
        self.assertEqual(result['discovery_parse_error'], 'discovery_parse_invalid')
        self.assertEqual(state['status'], 'deferred')
        self.assertLessEqual(self.cached(fetcher)['expires_at'], self.clock[0])
        capture = self.failures()[0]['snapshot']
        self.assertTrue(capture['original_response_bytes_verified'])
        self.assertEqual(gzip.decompress((self.root / capture['snapshot_path']).read_bytes()), b'')

    def test_declared_non_utf8_failed_bytes_are_reconstructed_and_verified(self):
        raw = '{"malformed":"niño"'.encode('latin-1')
        fetcher = self.fetcher(reply(raw, **{'content-type': 'application/json; charset=iso-8859-1'}))
        self.execute(fetcher)
        capture = self.failures()[0]['snapshot']
        self.assertTrue(capture['original_response_bytes_verified'])
        self.assertEqual(gzip.decompress((self.root / capture['snapshot_path']).read_bytes()), raw)

    def test_irrecoverable_original_bytes_are_not_mislabeled_as_verified_raw(self):
        raw = b'{"malformed":"\xff"'
        fetcher = self.fetcher(reply(raw))
        self.execute(fetcher)
        capture = self.failures()[0]['snapshot']
        self.assertFalse(capture['original_response_bytes_verified'])
        self.assertEqual(capture['original_response_sha256'], hashlib.sha256(raw).hexdigest())
        saved = gzip.decompress((self.root / capture['snapshot_path']).read_bytes())
        self.assertEqual(saved, raw.decode('utf-8', errors='replace').encode())
        self.assertEqual(hashlib.sha256(saved).hexdigest(), capture['snapshot_sha256'])

    def test_acquire_session_forwards_semantic_invalidation_to_real_cache(self):
        fetcher = self.fetcher(reply(BAD))
        with patch('peps.collector.SafeFetcher', return_value=fetcher):
            result = self.news.acquire(workers=1, seconds=1, max_articles=1, queue=False)
        self.assertEqual(result['processed'], 1)
        self.assertEqual(len(self.failures()), 1)
        self.assertTrue(self.failures()[0]['cache_freshness_expired'])
        self.assertLessEqual(self.cached(fetcher)['expires_at'], self.clock[0])


if __name__ == '__main__':
    unittest.main()
