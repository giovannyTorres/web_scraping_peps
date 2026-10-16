"""Synthetic concurrent queues: real SQLite, controlled clocks, no network."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
from pathlib import Path
from queue import Empty, Queue
import sqlite3
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from peps.store import Store, canonical, digest, now


class StoreStressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'synthetic.sqlite3')

    def seed(self, rows):
        stamp = now()
        with self.store.connection() as con:
            con.executemany('INSERT INTO jobs(id,kind,payload,status,attempts,due,created_at,updated_at) '
                            'VALUES(?,?,?,?,?,?,?,?)',
                            ((digest(key), kind, payload, state, attempts, due, stamp, stamp)
                             for key, kind, payload, state, attempts, due in rows))

    def test_corrupt_legacy_payloads_are_quarantined_and_do_not_starve_valid_work(self):
        invalid = ['{broken', '[]', 'null', 'false', '3', '"sensitive-synthetic-marker"']
        self.seed([(f'bad-{i}', 'news_article', value, 'queued', 0, i) for i, value in enumerate(invalid)] +
                  [('valid', 'news_article', '{"ok":true}', 'queued', 0, 20)])
        claimed = self.store.claim(kind_prefix='news_')
        self.assertEqual(claimed['id'], digest('valid'))
        self.assertEqual(claimed['payload'], {'ok': True})
        with self.store.connection() as con:
            for i, raw in enumerate(invalid):
                row = con.execute('SELECT * FROM jobs WHERE id=?', (digest(f'bad-{i}'),)).fetchone()
                self.assertEqual((row['status'], row['attempts']), ('dead', 0))
                self.assertIsNone(row['owner'])
                self.assertIsNone(row['lease_until'])
                self.assertEqual(row['payload'], raw)
            events = [json.loads(row[0]) for row in con.execute(
                "SELECT payload FROM audit WHERE action='job_invalid_payload'")]
        self.assertEqual(len(events), len(invalid))
        self.assertNotIn('sensitive-synthetic-marker', canonical(events))
        self.assertEqual({event['payload_sha256'] for event in events}, {digest(raw) for raw in invalid})
        self.assertTrue(self.store.finish(claimed))
        self.assertTrue(self.store.verify_audit()['ok'])

    def test_quarantine_and_claim_roll_back_if_audit_cannot_commit(self):
        self.seed([('bad', 'news_article', '[]', 'queued', 0, 0),
                   ('valid', 'news_article', '{}', 'queued', 0, 1)])
        with patch.object(self.store, '_audit', side_effect=RuntimeError('Synthetic audit failure')):
            with self.assertRaisesRegex(RuntimeError, 'Synthetic audit failure'):
                self.store.claim(kind_prefix='news_')
        with self.store.connection() as con:
            self.assertEqual(dict(con.execute('SELECT status,count(*) FROM jobs GROUP BY status')), {'queued': 2})
            self.assertEqual(con.execute('SELECT count(*) FROM audit').fetchone()[0], 0)
        self.assertEqual(self.store.claim(kind_prefix='news_')['id'], digest('valid'))

    def test_new_jobs_require_object_payloads(self):
        for value in ([], None, True, 1, 'text'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.store.enqueue('synthetic', value)

    def test_global_manual_retry_preserves_scheduled_backoff_and_live_workers(self):
        later = time.time() + 3600
        self.seed([('failed', 'news_article', '{}', 'dead', 5, later),
                   ('blocked', 'official', '{}', 'blocked', 1, 0),
                   ('live', 'news_article', '{}', 'running', 2, 0)])
        with self.store.connection() as con:
            con.execute("UPDATE jobs SET owner='synthetic-worker',lease_until=?", (later,))
        self.assertEqual(self.store.retry_failed(), 2)
        with self.store.connection() as con:
            rows = {row['id']: row for row in con.execute('SELECT * FROM jobs')}
        self.assertEqual(rows[digest('failed')]['due'], later)
        self.assertEqual(rows[digest('failed')]['status'], 'queued')
        self.assertIsNone(rows[digest('failed')]['owner'])
        self.assertIsNone(rows[digest('failed')]['lease_until'])
        self.assertEqual(rows[digest('live')]['status'], 'running')
        self.assertEqual(rows[digest('live')]['owner'], 'synthetic-worker')
        self.assertEqual(rows[digest('live')]['lease_until'], later)
        self.assertTrue(self.store.verify_audit()['ok'])

    def test_large_queue_claim_does_not_sort_or_scan_the_ready_backlog(self):
        self.seed([(str(i), 'news_article', '{}', 'queued', 0, 0) for i in range(5000)])
        original, statements = self.store.connection, []

        @contextmanager
        def traced():
            with original() as con:
                con.set_trace_callback(statements.append)
                yield con

        with patch.object(self.store, 'connection', traced):
            self.assertIsNotNone(self.store.claim(kind_prefix='news_'))
        claim = next(sql for sql in statements if sql.startswith('SELECT * FROM jobs'))
        retire = next(sql for sql in statements if sql.startswith('UPDATE jobs') and "status='dead'" in sql)
        with original() as con:
            claim_plan = [row[3] for row in con.execute('EXPLAIN QUERY PLAN ' + claim)]
            retire_plan = [row[3] for row in con.execute('EXPLAIN QUERY PLAN ' + retire)]
        self.assertFalse(any('TEMP B-TREE' in row for row in claim_plan), claim_plan)
        self.assertTrue(any('jobs_claim_pending' in row for row in claim_plan), claim_plan)
        self.assertTrue(any('jobs_exhausted_pending' in row for row in retire_plan), retire_plan)

    def test_kind_prefix_does_not_claim_reconcile_or_count_similar_names(self):
        self.seed([('foreign-ready', 'newsflash', '{}', 'queued', 0, 0),
                   ('foreign-exhausted', 'newsXarticle', '{}', 'queued', 5, 0),
                   ('wanted-exhausted', 'news_article', '{}', 'queued', 5, 0),
                   ('wanted-future', 'news_article', '{}', 'queued', 0, time.time() + 3600)])
        self.assertIsNone(self.store.claim(kind_prefix='news_'))
        self.assertEqual(self.store.queue_readiness('news_')['ready'], 0)
        self.assertGreater(self.store.next_due('news_'), time.time())
        with self.store.connection() as con:
            rows = {row['id']: row['status'] for row in con.execute('SELECT id,status FROM jobs')}
        self.assertEqual(rows[digest('wanted-exhausted')], 'dead')
        self.assertEqual(rows[digest('foreign-exhausted')], 'queued')
        self.assertEqual(rows[digest('foreign-ready')], 'queued')
        self.seed([('literal', 'literal_%\\article', '{}', 'queued', 0, 0),
                   ('wildcard', 'literalXYZarticle', '{}', 'queued', 0, 0)])
        self.assertEqual(self.store.claim(kind_prefix='literal_%\\')['id'], digest('literal'))

    def _blocked_clock_operation(self, operation, clock):
        original = self.store.connection
        attempted = threading.Event()

        @contextmanager
        def observed():
            with original() as con:
                con.set_trace_callback(lambda sql: attempted.set() if sql.startswith('BEGIN') else None)
                yield con

        # A real SQLite writer blocks the worker. The virtual clock advances
        # while it waits, without a long sleep or a latency-based assertion.
        with original() as writer:
            writer.execute('BEGIN IMMEDIATE')
            with patch.object(self.store, 'connection', observed), \
                 patch.object(sys.modules[Store.__module__], 'time', SimpleNamespace(time=lambda: clock[0])), \
                 ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(operation)
                try:
                    self.assertTrue(attempted.wait(2), 'Worker never attempted its SQLite transaction')
                    clock[0] = 200.
                finally:
                    writer.rollback()
                return future.result(timeout=2)

    def test_claim_lease_starts_after_waiting_for_sqlite_writer(self):
        self.seed([('lease', 'synthetic', '{}', 'queued', 0, 0)])
        clock = [100.]
        job = self._blocked_clock_operation(lambda: self.store.claim(lease_seconds=5), clock)
        with self.store.connection() as con:
            persisted = con.execute('SELECT lease_until FROM jobs').fetchone()[0]
        self.assertEqual(persisted, 205.)
        self.assertEqual(job['lease_until'], persisted)

    def test_renewed_lease_starts_after_waiting_for_sqlite_writer(self):
        self.seed([('lease', 'synthetic', '{}', 'queued', 0, 0)])
        job = self.store.claim()
        self.assertTrue(self._blocked_clock_operation(lambda: self.store.renew(job, lease_seconds=5), [100.]))
        with self.store.connection() as con:
            self.assertEqual(con.execute('SELECT lease_until FROM jobs').fetchone()[0], 205.)

    def test_sqlite_timeout_does_not_claim_or_lose_work(self):
        self.seed([('locked', 'synthetic', '{}', 'queued', 0, 0)])
        original = self.store.connection

        @contextmanager
        def brief_timeout():
            with original() as con:
                con.execute('PRAGMA busy_timeout=25')
                yield con

        with original() as writer:
            writer.execute('BEGIN IMMEDIATE')
            with patch.object(self.store, 'connection', brief_timeout), ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(self.store.claim)
                with self.assertRaises(sqlite3.OperationalError):
                    future.result(timeout=2)
        job = self.store.claim()
        self.assertEqual(job['id'], digest('locked'))
        self.assertEqual(job['attempts'], 1)
        self.assertTrue(self.store.finish(job))

    def test_concurrent_mass_retry_and_workers_deliver_each_ready_job_once(self):
        future_due = time.time() + 3600
        count, delayed = 300, 30
        self.seed([(str(i), 'news_article', canonical({'synthetic': i}), 'dead', 5,
                    future_due if i < delayed else 0) for i in range(count)] +
                  [('unrelated', 'newsflash', '{}', 'dead', 5, 0)])
        groups = [[digest(str(i)) for i in range(offset, min(count, offset + 100))]
                  for offset in range(0, count, 100)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            retried = list(pool.map(self.store.retry_news_jobs, groups * 3))
        self.assertEqual(sum(row['retried'] for row in retried), count)

        progress = Queue()
        stop = threading.Event()
        progress_lock = threading.Lock()
        durable_finishes = [0]

        def drain(_):
            claimed = []
            while not stop.is_set():
                job = self.store.claim(kind_prefix='news_')
                if job is None:
                    return claimed
                claimed.append(job['id'])
                self.assertTrue(self.store.finish(job))
                # Notify only after the durable finish transaction commits.
                with progress_lock:
                    durable_finishes[0] += 1
                progress.put(None)
            return claimed

        with self.store.connection() as anchor, ThreadPoolExecutor(max_workers=8) as pool:
            anchor.execute('SELECT count(*) FROM jobs').fetchone()
            drains = [pool.submit(drain, i) for i in range(8)]
            retries = [pool.submit(self.store.retry_news_jobs, group) for group in groups * 2]
            futures = drains + retries
            for future in futures:
                future.add_done_callback(lambda _: progress.put(None))

            def diagnostics():
                with progress_lock:
                    completed = durable_finishes[0]
                done = [future for future in futures if future.done() and not future.cancelled()]
                errors = [repr(future.exception()) for future in done if future.exception() is not None]
                return f'{completed} durable finishes; {len(done)}/{len(futures)} tasks finished; errors={errors}'

            # Each SQLite write can legitimately wait 30 seconds. Requiring
            # an entire 270-job drain to finish within that same deadline
            # confuses disk throughput with a stalled worker on Windows.
            # Keep the full workload, detect missing forward progress and
            # bound the entire test independently; this is a correctness test,
            # not a filesystem-speed assertion.
            deadline = time.monotonic() + 180
            try:
                while any(not future.done() for future in futures):
                    remaining = deadline - time.monotonic()
                    self.assertGreater(remaining, 0, 'Concurrent queue exceeded its test budget: ' + diagnostics())
                    try:
                        progress.get(timeout=min(60, remaining))
                    except Empty:
                        reason = ('Concurrent queue exceeded its test budget: ' if time.monotonic() >= deadline
                                  else 'Concurrent queue made no progress for 60 seconds: ')
                        self.fail(reason + diagnostics())
                delivered = [value for worker in drains for value in worker.result()]
                self.assertTrue(all(retry.result()['retried'] == 0 for retry in retries))
            finally:
                stop.set()
                for future in futures:
                    future.cancel()
        self.assertEqual(len(delivered), count - delayed)
        self.assertEqual(len(set(delivered)), count - delayed)
        with self.store.connection() as con:
            self.assertEqual(con.execute('SELECT status FROM jobs WHERE id=?', (digest('unrelated'),)).fetchone()[0], 'dead')
            self.assertEqual(con.execute('SELECT count(*) FROM jobs WHERE due=? AND attempts=0', (future_due,)).fetchone()[0], delayed)
        self.assertTrue(self.store.verify_audit()['ok'])


if __name__ == '__main__':
    unittest.main()
