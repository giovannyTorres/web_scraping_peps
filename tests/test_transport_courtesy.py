"""Durable transport courtesy under DNS delay; fixture transport only."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
import socket
import sqlite3
import tempfile
import threading
import unittest

from peps.collector import SafeFetcher


PUBLIC = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))]


class TransportCourtesyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.clock, self.calls = [1_800_000_000.], []
        self.source = {'id': 'fixture', 'enabled': True, 'terms_approved': True,
                       'allowed_hosts': ['news.example'], 'robots_required': False,
                       'min_interval_seconds': 1, 'cache_ttl_seconds': 900}
        self.a = self.fetcher()
        self.b = self.fetcher()

    def fetcher(self):
        result = SafeFetcher({'trust_env_proxy': False}, self.root)
        self.addCleanup(result.close)
        result._clock = lambda: self.clock[0]
        result._dns = lambda *_a, **_k: PUBLIC
        def transport(url, *_args):
            self.calls.append((url, self.clock[0]))
            return 200, {'content-type': 'text/plain'}, b'Public fixture body'
        result._transport = transport
        return result

    def test_slow_dns_inverts_admission_order_but_never_compresses_transports(self):
        first = [True]
        base = self.clock[0]
        def delayed_dns(*_args, **_kwargs):
            if first[0]:
                first[0] = False
                self.clock[0] = base + 1
                inner = self.b.fetch('https://news.example/faster', self.source)
                self.assertEqual(inner['status'], 'ok')
                self.clock[0] = base + 1.2
            return PUBLIC
        self.a._dns = delayed_dns
        slow = self.a.fetch('https://news.example/slower', self.source)
        self.assertEqual(slow['status'], 'deferred')
        self.assertEqual(slow['error_code'], 'host_interval')
        self.assertEqual([url for url, _ in self.calls], ['https://news.example/faster'])
        self.clock[0] = base + 2.1
        self.assertEqual(self.a.fetch('https://news.example/slower', self.source)['status'], 'ok')
        self.assertGreaterEqual(self.calls[1][1] - self.calls[0][1], 1)

    def test_circuit_revoked_during_dns_is_rechecked_before_any_request(self):
        def revoked_dns(*_args, **_kwargs):
            self.b._backoff('news.example', blocked=True, reason='Operator restriction')
            return PUBLIC
        self.a._dns = revoked_dns
        result = self.a.fetch('https://news.example/revoked', self.source)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['error_code'], 'host_circuit')
        self.assertFalse(result['request_attempted'])
        self.assertEqual(self.calls, [])

    def test_retry_after_published_during_dns_is_never_shortened(self):
        def throttled_dns(*_args, **_kwargs):
            self.b._backoff('news.example', {'retry-after': '120'})
            return PUBLIC
        self.a._dns = throttled_dns
        result = self.a.fetch('https://news.example/throttled', self.source)
        self.assertEqual(result['status'], 'deferred')
        self.assertEqual(result['error_code'], 'host_interval')
        self.assertFalse(result['request_attempted'])
        self.assertEqual(self.calls, [])
        with self.a._db() as con:
            row = con.execute("SELECT next_at,last_at FROM hosts WHERE host='news.example'").fetchone()
        self.assertGreaterEqual(row['next_at'], self.clock[0] + 120)
        self.assertEqual(row['last_at'], 0)

    def test_cache_hit_does_not_reserve_or_start_transport(self):
        url = 'https://news.example/cache'
        self.assertEqual(self.a.fetch(url, self.source)['status'], 'ok')
        self.a._dns = lambda *_a, **_k: self.fail('Cache reuse attempted DNS')
        self.clock[0] += .1
        result = self.a.fetch(url, self.source)
        self.assertEqual(result['status'], 'not_modified')
        self.assertEqual(result['network_requests'], 0)
        self.assertEqual(len(self.calls), 1)

    def test_final_transport_reservations_are_atomic_across_workers(self):
        def reserve(_):
            worker = self.fetcher()
            return worker._reserve('news.example', 1)[0]
        with ThreadPoolExecutor(max_workers=8) as pool:
            result = list(pool.map(reserve, range(8)))
        self.assertEqual(result.count(None), 1)
        self.assertEqual(result.count('deferred'), 7)

    def test_sqlite_writer_wait_cannot_backdate_the_transport_reservation(self):
        attempted = threading.Event()
        original_db = self.a._db
        @contextmanager
        def observed_db():
            with original_db() as con:
                class Connection:
                    def execute(_self, sql, *args):
                        if sql == 'BEGIN IMMEDIATE':
                            attempted.set()
                        return con.execute(sql, *args)
                yield Connection()
        self.a._db = observed_db
        with sqlite3.connect(self.a.db_path) as blocker:
            blocker.execute('BEGIN IMMEDIATE')
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(self.a._reserve, 'news.example', 1)
                try:
                    self.assertTrue(attempted.wait(3), 'Worker never reached SQLite write admission')
                    self.clock[0] += 10
                finally:
                    blocker.commit()
                self.assertIsNone(future.result(timeout=3)[0])
        with original_db() as con:
            row = con.execute("SELECT last_at,next_at FROM hosts WHERE host='news.example'").fetchone()
        self.assertEqual(row['last_at'], self.clock[0])
        self.assertEqual(row['next_at'], self.clock[0] + 1)

    def test_existing_host_database_preserves_backoff_without_new_schema(self):
        with tempfile.TemporaryDirectory() as other:
            path = Path(other)
            with sqlite3.connect(path / 'network.sqlite3') as con:
                con.execute("""CREATE TABLE hosts(host TEXT PRIMARY KEY,
                    next_at REAL NOT NULL DEFAULT 0,blocked_until REAL NOT NULL DEFAULT 0,
                    reason TEXT NOT NULL DEFAULT '',failures INTEGER NOT NULL DEFAULT 0,
                    last_at REAL NOT NULL DEFAULT 0)""")
                con.execute('INSERT INTO hosts VALUES(?,?,?,?,?,?)', ('news.example', 123, 456, 'Existing circuit', 2, 120))
            fetcher = SafeFetcher({}, path)
            self.addCleanup(fetcher.close)
            with fetcher._db() as con:
                row = dict(con.execute("SELECT * FROM hosts WHERE host='news.example'").fetchone())
            self.assertNotIn('last_transport_at', row)
            self.assertEqual(row['next_at'], 123)
            self.assertEqual(row['blocked_until'], 456)
            self.assertEqual(row['failures'], 2)

    def test_known_wait_or_circuit_never_resolves_dns(self):
        self.a._dns = lambda *_a, **_k: self.fail('Known host wait attempted DNS')
        self.b._reserve('news.example', 5)
        waiting = self.a.fetch('https://news.example/waiting', self.source)
        self.assertEqual(waiting['status'], 'deferred')
        self.assertEqual(waiting['error_code'], 'host_interval')
        self.b._backoff('news.example', blocked=True)
        blocked = self.a.fetch('https://news.example/blocked', self.source)
        self.assertEqual(blocked['status'], 'blocked')
        self.assertEqual(blocked['error_code'], 'host_circuit')
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
