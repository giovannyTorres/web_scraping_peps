"""Original bodies must not wait behind a ready bulk discovery backlog."""
from contextlib import contextmanager
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from peps.store import Store, digest, now


class ReadyBodyPriorityTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(Path(directory.name) / 'jobs.sqlite3')

    def seed_queries(self, count=652):
        stamp = now()
        with self.store.connection() as con:
            con.executemany("INSERT INTO jobs(id,kind,payload,status,attempts,due,created_at,updated_at) "
                            "VALUES(?,'news_query','{}','queued',0,0,?,?)",
                            [(digest('query-' + str(index)), stamp, stamp) for index in range(count)])

    def row(self, identifier):
        with self.store.connection() as con:
            return dict(con.execute('SELECT * FROM jobs WHERE id=?', (identifier,)).fetchone())

    def test_ready_body_precedes_652_older_queries_without_changing_default_order(self):
        self.seed_queries()
        identifier = self.store.enqueue('news_article', {'article_id': 'synthetic'}, due=90)
        with patch('peps.store.time.time', return_value=100):
            ordinary = self.store.claim(kind_prefix='news_')
            self.assertEqual(ordinary['kind'], 'news_query')
            self.store.finish(ordinary)
            preferred = self.store.claim(kind_prefix='news_', preferred_kind='news_article')
            self.assertEqual(preferred['id'], identifier)
            self.assertEqual(preferred['attempts'], 1)
            self.assertEqual(preferred['due'], 90)
            self.store.finish(preferred)
            following = self.store.claim(kind_prefix='news_', preferred_kind='news_article')
            self.assertEqual(following['kind'], 'news_query')
        self.assertTrue(self.store.verify_audit()['ok'])

    def test_future_retry_is_not_claimed_early_or_reset_by_preference(self):
        self.seed_queries(1)
        identifier = self.store.enqueue('news_article', {}, due=101)
        with self.store.connection() as con:
            con.execute("UPDATE jobs SET status='deferred',attempts=3,error='HTTP 429: Retry-After' WHERE id=?",
                        (identifier,))
        original = self.row(identifier)
        with patch('peps.store.time.time', return_value=100):
            query = self.store.claim(kind_prefix='news_', preferred_kind='news_article')
            self.assertEqual(query['kind'], 'news_query')
            self.assertEqual(self.row(identifier), original)
            self.store.finish(query)
            self.assertIsNone(self.store.claim(kind_prefix='news_', preferred_kind='news_article'))
        with patch('peps.store.time.time', return_value=101):
            ready = self.store.claim(kind_prefix='news_', preferred_kind='news_article')
            self.assertEqual(ready['id'], identifier)
            self.assertEqual((ready['attempts'], ready['due'], ready['error']),
                             (4, 101, 'HTTP 429: Retry-After'))

    def test_live_body_lease_is_preserved_and_expired_owner_is_fenced(self):
        self.seed_queries(1)
        identifier = self.store.enqueue('news_article', {}, due=0)
        with self.store.connection() as con:
            con.execute("UPDATE jobs SET status='running',attempts=3,owner='original',lease_until=200 WHERE id=?",
                        (identifier,))
        original = self.row(identifier)
        with patch('peps.store.time.time', return_value=100):
            query = self.store.claim(kind_prefix='news_', preferred_kind='news_article')
            self.assertEqual(query['kind'], 'news_query')
            self.assertEqual(self.row(identifier), original)
        with patch('peps.store.time.time', return_value=201):
            recovered = self.store.claim(kind_prefix='news_', preferred_kind='news_article')
            self.assertEqual((recovered['id'], recovered['attempts']), (identifier, 4))
            self.assertNotEqual(recovered['owner'], 'original')
            self.assertFalse(self.store.finish(dict(recovered, owner='original')))
            self.assertTrue(self.store.finish(recovered))

    def test_preference_respects_kind_scope_and_exhausted_attempt_limit(self):
        self.seed_queries(1)
        outside = self.store.enqueue('official', {}, due=0)
        exhausted = self.store.enqueue('news_article', {}, due=0)
        with self.store.connection() as con:
            con.execute('UPDATE jobs SET attempts=5 WHERE id=?', (exhausted,))
        with patch('peps.store.time.time', return_value=100):
            query = self.store.claim(kind_prefix='news_', preferred_kind='official')
            self.assertEqual(query['kind'], 'news_query')
            self.assertEqual(self.row(outside)['status'], 'queued')
            self.assertEqual(self.row(outside)['attempts'], 0)
            self.assertEqual(self.row(exhausted)['status'], 'dead')
            self.assertEqual(self.row(exhausted)['attempts'], 5)

    def test_invalid_preferred_payload_is_quarantined_without_poisoning_queries(self):
        self.seed_queries(1)
        identifier = self.store.enqueue('news_article', {}, due=0)
        with self.store.connection() as con:
            con.execute("UPDATE jobs SET payload='[]' WHERE id=?", (identifier,))
        with patch('peps.store.time.time', return_value=100):
            query = self.store.claim(kind_prefix='news_', preferred_kind='news_article')
        self.assertEqual(query['kind'], 'news_query')
        self.assertEqual(self.row(identifier)['status'], 'dead')
        self.assertEqual(self.row(identifier)['attempts'], 0)
        self.assertTrue(self.store.verify_audit()['ok'])

    def test_preferred_lookup_uses_exact_kind_index_without_sorting_bulk_queue(self):
        self.seed_queries()
        original, statements = self.store.connection, []

        @contextmanager
        def traced():
            with original() as con:
                con.set_trace_callback(statements.append)
                yield con

        with patch.object(self.store, 'connection', traced):
            self.assertIsNotNone(self.store.claim(kind_prefix='news_', preferred_kind='news_article'))
        preferred = next(sql for sql in statements if sql.startswith('SELECT * FROM jobs'))
        with original() as con:
            plan = [row[3] for row in con.execute('EXPLAIN QUERY PLAN ' + preferred)]
        self.assertTrue(any('SEARCH jobs USING INDEX jobs_claim_kind_pending' in row for row in plan), plan)
        self.assertFalse(any('TEMP B-TREE' in row for row in plan), plan)


if __name__ == '__main__':
    unittest.main()
