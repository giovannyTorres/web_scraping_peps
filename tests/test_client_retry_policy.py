"""No external HTTP: exercise durable retry policy through controlled transports."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime
from pathlib import Path
import socket
import sqlite3
import tempfile
import threading
import unittest

from peps.collector import SafeFetcher

PUBLIC = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))]


class ClientRetryEpisodesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = [1_800_000_000.]
        self.source = {'id': 'fixture', 'enabled': True, 'terms_approved': True,
                       'allowed_hosts': ['news.example'], 'robots_required': False,
                       'min_interval_seconds': 0, 'cache_ttl_seconds': 0}
        self.fetcher = self.make_fetcher()

    def make_fetcher(self):
        result = SafeFetcher({'trust_env_proxy': False}, self.root)
        self.addCleanup(result.close)
        result._clock = lambda: self.now[0]
        result._dns = lambda *_a, **_k: PUBLIC
        return result

    def row(self):
        with self.fetcher._db() as con:
            return dict(con.execute("SELECT * FROM hosts WHERE host='news.example'").fetchone())

    def concurrent(self, responses):
        admitted = threading.Barrier(len(responses) + 1)
        releases = {name: threading.Event() for name in responses}
        pool = ThreadPoolExecutor(max_workers=len(responses))
        self.addCleanup(pool.shutdown, wait=True)
        for event in releases.values():
            self.addCleanup(event.set)
        def transport(url, *_args):
            name = url.rsplit('/', 1)[-1]
            admitted.wait(5)
            if not releases[name].wait(5):
                raise AssertionError('Fixture transport release timed out')
            response = responses[name]
            if isinstance(response, Exception):
                raise response
            return response
        self.fetcher._transport = transport
        futures = {name: pool.submit(self.fetcher.fetch, 'https://news.example/' + name, self.source)
                   for name in responses}
        admitted.wait(5)
        return futures, releases

    def test_nine_already_admitted_timeouts_are_one_client_episode(self):
        futures, releases = self.concurrent({str(i): TimeoutError() for i in range(9)})
        for event in releases.values():
            event.set()
        results = [future.result(5) for future in futures.values()]
        row = self.row()
        self.assertEqual(row['client_failures'], 1)
        self.assertEqual(row['failures'], 1)
        self.assertEqual(row['client_generation'], 1)
        self.assertEqual(row['client_retry_until'], self.now[0] + 5)
        self.assertEqual({r['retry_class'] for r in results}, {'client_network'})
        self.assertEqual(len({r['retry_at'] for r in results}), 1)

    def test_success_clears_only_client_and_old_inflight_timeout_cannot_reopen_it(self):
        futures, releases = self.concurrent({'failure': TimeoutError(), 'success': (200, {'content-type': 'text/plain'}, b'Useful body'), 'old': TimeoutError()})
        releases['failure'].set()
        self.assertEqual(futures['failure'].result(5)['status'], 'deferred')
        releases['success'].set()
        self.assertEqual(futures['success'].result(5)['status'], 'ok')
        self.assertEqual(self.row()['client_retry_until'], 0)
        releases['old'].set()
        old = futures['old'].result(5)
        self.assertEqual(old['status'], 'deferred')
        self.assertEqual(self.row()['client_retry_until'], 0)
        self.assertEqual(self.row()['client_failures'], 0)
        self.assertLessEqual(self.row()['next_at'], self.now[0])

    def test_genuine_new_failure_rounds_grow_boundedly_and_survive_restart(self):
        self.fetcher._transport = lambda *_a: (_ for _ in ()).throw(TimeoutError())
        delays = []
        for i in range(7):
            result = self.fetcher.fetch('https://news.example/attempt-' + str(i), self.source)
            self.assertEqual(result['retry_class'], 'client_network')
            due = datetime.fromisoformat(result['retry_at']).timestamp()
            delays.append(due - self.now[0])
            self.now[0] = due + .1
        for observed, expected in zip(delays, [5, 10, 20, 40, 60, 60, 60]):
            self.assertAlmostEqual(observed, expected, places=5)
        before = self.row()
        other = self.make_fetcher()
        with other._db() as con:
            after = dict(con.execute("SELECT * FROM hosts WHERE host='news.example'").fetchone())
        self.assertEqual(before, after)

    def test_inflight_success_never_shortens_retry_after(self):
        futures, releases = self.concurrent({'limited': (429, {'Retry-After': '120'}, b''), 'success': (200, {'content-type': 'text/plain'}, b'Useful body')})
        releases['limited'].set()
        limited = futures['limited'].result(5)
        self.assertEqual(limited['retry_class'], 'server_backoff')
        before = self.row()['server_retry_until']
        releases['success'].set()
        self.assertEqual(futures['success'].result(5)['status'], 'ok')
        self.assertEqual(self.row()['server_retry_until'], before)
        self.assertEqual(self.row()['next_at'], before)
        self.fetcher._dns = lambda *_a, **_k: self.fail('Known wait must not resolve DNS')
        again = self.fetcher.fetch('https://news.example/another', self.source)
        self.assertEqual(again['retry_class'], 'server_backoff')
        self.assertEqual(again['retry_at'], limited['retry_at'])

    def test_multiple_retry_after_responses_extend_deadline_without_new_exponential_episode(self):
        futures, releases = self.concurrent({'short': (503, {'Retry-After': '45'}, b''), 'long': (429, {'Retry-After': '120'}, b'')})
        releases['short'].set()
        futures['short'].result(5)
        releases['long'].set()
        futures['long'].result(5)
        row = self.row()
        self.assertEqual(row['server_retry_until'], self.now[0] + 120)
        self.assertEqual(row['server_failures'], 1)

    def test_inflight_success_does_not_reset_operator_access_circuit(self):
        futures, releases = self.concurrent({'denied': (403, {}, b''), 'success': (200, {'content-type': 'text/plain'}, b'Useful body')})
        releases['denied'].set()
        futures['denied'].result(5)
        blocked = self.row()['blocked_until']
        releases['success'].set()
        futures['success'].result(5)
        self.assertEqual(self.row()['blocked_until'], blocked)
        result = self.fetcher.fetch('https://news.example/later', self.source)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['retry_class'], 'operator_access')

    def test_challenge_is_not_a_success_that_clears_client_episode(self):
        futures, releases = self.concurrent({'timeout': TimeoutError(), 'challenge': (200, {'content-type': 'text/html'}, b'<title>Just a moment...</title><p>Verify you are human</p>')})
        releases['timeout'].set()
        futures['timeout'].result(5)
        releases['challenge'].set()
        self.assertEqual(futures['challenge'].result(5)['status'], 'blocked')
        self.assertEqual(self.row()['client_failures'], 1)
        self.assertGreater(self.row()['blocked_until'], self.now[0])

    def test_robots_success_does_not_clear_content_client_episode(self):
        barrier = threading.Barrier(3)
        content_release, robots_release = threading.Event(), threading.Event()
        def transport(url, *_a):
            barrier.wait(5)
            event = robots_release if url.endswith('robots.txt') else content_release
            if not event.wait(5):
                raise AssertionError('Fixture release timed out')
            if url.endswith('robots.txt'):
                return 200, {'content-type': 'text/plain'}, b'User-agent: *\nAllow: /\n'
            raise TimeoutError()
        self.fetcher._transport = transport
        with ThreadPoolExecutor(max_workers=2) as pool:
            content = pool.submit(self.fetcher.fetch, 'https://news.example/article', self.source)
            robots = pool.submit(self.fetcher._request, 'https://news.example/robots.txt', self.source, request_kind='robots')
            try:
                barrier.wait(5)
                content_release.set()
                content.result(5)
                robots_release.set()
                self.assertIsNone(robots.result(5)[0])
            finally:
                content_release.set()
                robots_release.set()
        self.assertEqual(self.row()['client_failures'], 1)
        self.assertEqual(self.row()['client_retry_until'], self.now[0] + 5)

    def test_semantic_cache_expiry_is_exact_hash_guarded_and_does_not_change_host_policy(self):
        source = dict(self.source, cache_ttl_seconds=900)
        calls = []
        values = [(200, {'content-type': 'application/json', 'etag': '"bad"'}, b'{"results":[],"unresponsive_engines":[["x","timeout"]]}'),
                  (200, {'content-type': 'application/json', 'etag': '"good"'}, b'{"results":[]}')]
        def transport(url, headers, *_a):
            calls.append((url, dict(headers)))
            return values.pop(0)
        self.fetcher._transport = transport
        url = 'https://news.example/search'
        first = self.fetcher.fetch(url, source)
        before = self.row()
        self.assertFalse(self.fetcher.expire_cached_response(url, '0' * 64, 'discovery_upstream_unavailable'))
        self.assertTrue(self.fetcher.expire_cached_response(url, first['sha256'], 'discovery_upstream_unavailable'))
        self.assertEqual(self.row(), before)
        with self.fetcher._db() as con:
            invalid = dict(con.execute('SELECT * FROM http_cache WHERE url=?', (url,)).fetchone())
            event = dict(con.execute("SELECT * FROM request_history WHERE request_kind='cache_validation'").fetchone())
        self.assertEqual(invalid['body'], first['body'])
        self.assertEqual(invalid['sha256'], first['sha256'])
        self.assertEqual(invalid['etag'], '"bad"')
        self.assertEqual(event['network_requests'], 0)
        second = self.fetcher.fetch(url, source)
        self.assertEqual(second['status'], 'ok')
        self.assertNotIn('If-None-Match', calls[1][1])
        with self.fetcher._db() as con:
            valid = con.execute('SELECT validation_error FROM http_cache WHERE url=?', (url,)).fetchone()
        self.assertEqual(valid['validation_error'], '')
        self.assertEqual(len(calls), 2)

    def test_invalidated_cache_cannot_be_revalidated_by_304_or_used_offline(self):
        values = [(200, {'content-type': 'application/json', 'etag': '"bad"'}, b'broken'), (304, {}, b'')]
        self.fetcher._transport = lambda *_a: values.pop(0)
        url = 'https://news.example/search'
        source = dict(self.source, cache_ttl_seconds=900)
        first = self.fetcher.fetch(url, source)
        self.fetcher.expire_cached_response(url, first['sha256'], 'discovery_parse_invalid')
        local = self.fetcher.fetch(url, dict(source, _cache_mode='local'))
        self.assertEqual(local['error_code'], 'invalid_cached_response')
        self.assertEqual(local['network_requests'], 0)
        again = self.fetcher.fetch(url, source)
        self.assertEqual(again['error_code'], 'invalid_cache_revalidation')
        with self.fetcher._db() as con:
            row = con.execute('SELECT * FROM http_cache WHERE url=?', (url,)).fetchone()
        self.assertEqual(row['body'], first['body'])
        self.assertEqual(row['validation_error'], 'discovery_parse_invalid')

    def test_ambiguous_legacy_deadline_is_preserved_by_migration(self):
        with tempfile.TemporaryDirectory() as other:
            path = Path(other)
            with closing(sqlite3.connect(path / 'network.sqlite3')) as con, con:
                con.execute("CREATE TABLE hosts(host TEXT PRIMARY KEY,next_at REAL NOT NULL DEFAULT 0,blocked_until REAL NOT NULL DEFAULT 0,reason TEXT NOT NULL DEFAULT '',failures INTEGER NOT NULL DEFAULT 0,last_at REAL NOT NULL DEFAULT 0)")
                con.execute('INSERT INTO hosts VALUES(?,?,?,?,?,?)', ('news.example', self.now[0] + 1800, 0, 'Transient network failure', 0, self.now[0] - 5))
            migrated = SafeFetcher({'trust_env_proxy': False}, path)
            try:
                migrated._clock = self.fetcher._clock
                migrated._dns = lambda *_a, **_k: self.fail('Legacy wait may not be silently bypassed')
                result = migrated.fetch('https://news.example/legacy', self.source)
                self.assertEqual(result['status'], 'deferred')
                self.assertEqual(result['retry_class'], 'legacy_unclassified')
                self.assertEqual(datetime.fromisoformat(result['retry_at']).timestamp(), self.now[0] + 1800)
            finally:
                migrated.close()

    def test_external_operator_deadline_survives_client_recovery(self):
        self.assertIsNone(self.fetcher._reserve('news.example', 0)[0])
        with self.fetcher._db() as con:
            con.execute("UPDATE hosts SET next_at=?,reason='External operator pause' WHERE host='news.example'",
                        (self.now[0] + 1800,))
        self.fetcher._content_success('news.example')
        row = self.row()
        self.assertEqual(row['legacy_retry_until'], self.now[0] + 1800)
        self.assertEqual(row['next_at'], self.now[0] + 1800)
        self.fetcher._dns = lambda *_a, **_k: self.fail('Operator wait cannot resolve DNS')
        result = self.fetcher.fetch('https://news.example/paused', self.source)
        self.assertEqual(result['retry_class'], 'legacy_unclassified')

    def test_robots_wait_keeps_client_retry_class_for_queue_scheduler(self):
        self.fetcher._backoff('news.example', kind='client_network')
        self.fetcher._dns = lambda *_a, **_k: self.fail('Known client wait cannot resolve DNS')
        result = self.fetcher.fetch('https://news.example/article', dict(self.source, robots_required=True))
        self.assertEqual(result['error_code'], 'host_interval')
        self.assertEqual(result['retry_class'], 'client_network')
        self.assertEqual(result['admission_host'], 'news.example')


if __name__ == '__main__':
    unittest.main()
