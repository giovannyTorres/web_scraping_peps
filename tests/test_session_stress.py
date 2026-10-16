"""Synthetic temporary SQLite banks; no HTTP, real lock/commit failures."""
from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from peps.news_session import AcquisitionSession, last_session
from peps.store import Store


class AcquisitionSessionStress(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='peps-synthetic-session-')
        self.addCleanup(self.temporary.cleanup)
        self.store = Store(Path(self.temporary.name) / 'synthetic.sqlite3')

    @contextmanager
    def locked_writer(self):
        # A real second SQLite writer, bounded to 50 ms per attempted save.
        connection = self.store.connection

        @contextmanager
        def short_timeout():
            with connection() as con:
                con.execute('PRAGMA busy_timeout=50')
                yield con

        with closing(sqlite3.connect(self.store.path)) as blocker:
            blocker.execute('BEGIN IMMEDIATE')
            try:
                with patch.object(self.store, 'connection', short_timeout):
                    yield
            finally:
                blocker.rollback()

    def finish_audits(self):
        with self.store.connection() as con:
            return con.execute("SELECT count(*) FROM audit WHERE action='news_acquisition_session_finished'").fetchone()[0]

    def test_failed_forced_publication_retries_immediately_without_throttle(self):
        session = AcquisitionSession(self.store, 'reuse')
        session.note(network_requests=7)
        with self.locked_writer(), self.assertRaisesRegex(sqlite3.OperationalError, 'locked'):
            session.save(force=True)
        self.assertEqual(last_session(self.store)['network_requests'], 0)
        session.save()
        self.assertEqual(last_session(self.store)['network_requests'], 7)

    def test_failed_finish_never_publishes_a_finish_without_its_audit(self):
        session = AcquisitionSession(self.store, 'reuse')
        session.note(network_requests=7)
        with self.locked_writer(), self.assertRaisesRegex(sqlite3.OperationalError, 'locked'):
            session.save(force=True, finished=True)
        self.assertIsNone(session.snapshot()['finished_at'])
        self.assertIsNone(last_session(self.store)['finished_at'])
        session.save()
        self.assertEqual(last_session(self.store)['network_requests'], 7)
        self.assertIsNone(last_session(self.store)['finished_at'])
        self.assertEqual(self.finish_audits(), 0)
        session.save(force=True, finished=True)
        self.assertEqual(last_session(self.store), session.snapshot())
        self.assertIsNotNone(last_session(self.store)['finished_at'])
        self.assertEqual(self.finish_audits(), 1)
        self.assertTrue(self.store.verify_audit()['ok'])

    def test_commit_failure_rolls_back_metadata_audit_and_in_memory_finish(self):
        session = AcquisitionSession(self.store, 'reuse')
        with self.store.connection() as con:
            con.executescript('CREATE TABLE synthetic_parent(id INTEGER PRIMARY KEY); '
                'CREATE TABLE synthetic_child(id INTEGER REFERENCES synthetic_parent(id) '
                'DEFERRABLE INITIALLY DEFERRED);')
        connection = self.store.connection

        @contextmanager
        def invalid_at_commit():
            with connection() as con:
                yield con
                # The save's statements succeed; SQLite rejects the commit.
                con.execute('INSERT INTO synthetic_child VALUES(1)')

        session.note(network_requests=9)
        with patch.object(self.store, 'connection', invalid_at_commit), \
             self.assertRaises(sqlite3.IntegrityError):
            session.save(force=True, finished=True)
        self.assertIsNone(session.snapshot()['finished_at'])
        self.assertEqual(last_session(self.store)['network_requests'], 0)
        self.assertIsNone(last_session(self.store)['finished_at'])
        self.assertEqual(self.finish_audits(), 0)
        session.save()
        self.assertEqual(last_session(self.store)['network_requests'], 9)
        self.assertIsNone(last_session(self.store)['finished_at'])
        session.save(force=True, finished=True)
        self.assertEqual(self.finish_audits(), 1)
        self.assertTrue(self.store.verify_audit()['ok'])

    def test_same_finish_is_idempotent(self):
        session = AcquisitionSession(self.store, 'local')
        session.save(force=True, finished=True)
        first = last_session(self.store)
        session.save(force=True, finished=True)
        self.assertEqual(last_session(self.store), first)
        self.assertEqual(self.finish_audits(), 1)

    def test_new_then_reused_body_is_counted_once_as_new(self):
        session = AcquisitionSession(self.store, 'reuse')
        for _ in range(5):
            session.body('article-a', new=True)
            session.body('article-a')
        snapshot = session.snapshot()
        self.assertEqual((snapshot['bodies_new'], snapshot['bodies_reused']), (1, 0))

    def test_reused_then_upgraded_body_is_reclassified_once(self):
        session = AcquisitionSession(self.store, 'reuse')
        session.body('article-a')
        session.body('article-b')
        before = session.snapshot()
        session.body('article-a', new=True)
        session.body('article-a')
        after = session.snapshot()
        self.assertEqual((before['bodies_new'], before['bodies_reused']), (0, 2))
        self.assertEqual((after['bodies_new'], after['bodies_reused']), (1, 1))
        self.assertEqual(before['bodies_new'] + before['bodies_reused'],
                         after['bodies_new'] + after['bodies_reused'])

    def test_eight_workers_publish_exact_monotone_counters_and_deduplicated_bodies(self):
        session = AcquisitionSession(self.store, 'reuse')
        barrier = threading.Barrier(9)
        failures, observed, snapshots = [], [], []

        def worker():
            try:
                barrier.wait(timeout=3)
                for index in range(200):
                    session.note(network_requests=1, http_cache_hits=1)
                    session.body('article-' + str(index % 32))
                    if index % 40 == 0:
                        session.save(force=True)
            except BaseException as exc:
                failures.append(repr(exc))

        workers = [threading.Thread(target=worker, daemon=True) for _ in range(8)]
        for worker_thread in workers:
            worker_thread.start()
        barrier.wait(timeout=3)
        deadline = time.monotonic() + 60
        while any(worker_thread.is_alive() for worker_thread in workers) and time.monotonic() < deadline:
            observed.append(last_session(self.store)['network_requests'])
            snapshots.append(session.snapshot()['network_requests'])
        for worker_thread in workers:
            worker_thread.join(timeout=1)
        self.assertTrue(all(not worker_thread.is_alive() for worker_thread in workers))
        self.assertEqual(failures, [])
        self.assertEqual(observed, sorted(observed))
        self.assertEqual(snapshots, sorted(snapshots))
        session.save(force=True, finished=True)
        result = last_session(self.store)
        self.assertEqual(result['network_requests'], 1600)
        self.assertEqual(result['http_cache_hits'], 1600)
        self.assertEqual((result['bodies_new'], result['bodies_reused']), (0, 32))
        self.assertEqual(result, session.snapshot())
        self.assertEqual(self.finish_audits(), 1)
        self.assertTrue(self.store.verify_audit()['ok'])

    def test_concurrent_new_and_reuse_preserve_exclusive_body_classification(self):
        session = AcquisitionSession(self.store, 'reuse')
        barrier = threading.Barrier(9)
        failures = []

        def worker(upgrade):
            try:
                barrier.wait(timeout=3)
                for _ in range(200):
                    for index in range(8):
                        session.body('article-' + str(index), new=upgrade)
            except BaseException as exc:
                failures.append(repr(exc))

        workers = [threading.Thread(target=worker, args=(index % 2 == 0,), daemon=True) for index in range(8)]
        for worker_thread in workers:
            worker_thread.start()
        barrier.wait(timeout=3)
        for worker_thread in workers:
            worker_thread.join(timeout=3)
        self.assertTrue(all(not worker_thread.is_alive() for worker_thread in workers))
        self.assertEqual(failures, [])
        result = session.snapshot()
        self.assertEqual((result['bodies_new'], result['bodies_reused']), (8, 0))


if __name__ == '__main__':
    unittest.main()
