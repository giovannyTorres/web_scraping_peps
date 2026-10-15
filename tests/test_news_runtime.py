"""Recovery and bounded news sessions use real SQLite jobs and fixture HTTP."""
import json
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from peps.engine import Engine
from peps.news import NewsAcquirer
from peps.store import digest, now


class NoNetworkFetcher:
    def __init__(self, *_args, **_kwargs):
        pass

    def fetch(self, *_args, **_kwargs):
        raise AssertionError('This runtime fixture must not access a source')


class NewsRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.engine = Engine(self.directory.name, online=True)
        self.news = NewsAcquirer(self.engine)
        self.store = self.engine.store
        self.source = {'id': 'fixture', 'kind': 'rss', 'allowed_hosts': ['example.org'],
                       'enabled': True, 'terms_approved': True, 'download_articles': True,
                       'article_terms_approved': True, 'url': 'https://example.org/feed'}
        self.news.config_path.write_text(json.dumps({'sources': [self.source], 'search': {'strategy': 'feeds'}}), encoding='utf-8')

    def run_fixture(self, **kwargs):
        kwargs.setdefault('seconds', 1)
        with patch('peps.collector.SafeFetcher', NoNetworkFetcher):
            return self.news.acquire(queue=False, **kwargs)

    def test_exhausted_queued_and_deferred_jobs_are_retired_instead_of_remaining_pending(self):
        for state in ('queued', 'deferred'):
            job_id = self.store.enqueue('news_feed', {'state': state}, key=state)
            with self.store.connection() as con:
                con.execute('UPDATE jobs SET status=?,attempts=5 WHERE id=?', (state, job_id))
        result = self.run_fixture()
        self.assertEqual(result['processed'], 0)
        self.assertEqual(result['pending'], 0)
        self.assertEqual(result['dead'], 2)
        self.assertEqual(result['stop_reason'], 'completed')
        self.assertTrue(self.store.verify_audit()['ok'])
        self.assertEqual(self.store.retry_failed(), 2)
        self.assertIsNotNone(self.store.claim(kind_prefix='news_'))

    def test_live_lease_is_preserved_and_its_expiry_is_reported(self):
        self.store.enqueue('news_feed', {'state': 'other-worker'})
        job = self.store.claim(lease_seconds=3600, kind_prefix='news_')
        with self.store.connection() as con:
            con.execute('UPDATE jobs SET attempts=5 WHERE id=?', (job['id'],))
        result = self.run_fixture()
        self.assertEqual(result['processed'], 0)
        self.assertEqual(result['pending'], 1)
        self.assertEqual(result['stop_reason'], 'external_lease')
        self.assertTrue(result['waiting'])
        self.assertGreater(result['next_retry_at'], time.time() + 3500)
        self.assertTrue(self.store.finish(job))

    def test_waiting_can_be_cancelled_without_network_or_fabricated_progress(self):
        self.store.enqueue('news_feed', {'source_id': 'fixture'}, due=time.time() + .5)
        events = []

        def observe(event):
            events.append(event)
            if event['event'] == 'activity' and event['waiting']:
                self.engine.stop_event.set()

        start = time.monotonic()
        result = self.run_fixture(progress=observe, workers=1)
        self.assertLess(time.monotonic() - start, 2)
        self.assertEqual(result['stop_reason'], 'cancelled')
        self.assertEqual(result['processed'], 0)
        self.assertEqual(result['pending'], 1)
        self.assertTrue(any(event['waiting'] and event['event'] == 'activity' for event in events))
        self.assertTrue(all(event['completed'] == 0 for event in events))

    def test_expired_session_leaves_ready_jobs_with_explicit_reason(self):
        self.store.enqueue('news_feed', {'source_id': 'fixture'})
        result = self.run_fixture(seconds=0)
        self.assertEqual(result['stop_reason'], 'time_limit')
        self.assertEqual(result['processed'], 0)
        self.assertEqual(result['ready'], 1)
        self.assertFalse(result['waiting'])

    def test_resume_reports_server_backoff_and_never_queues_duplicate_discovery(self):
        job_id = self.store.enqueue('news_feed', {'source_id': 'fixture', 'url': self.source['url']})
        job = self.store.claim(kind_prefix='news_')
        due = time.time() + 3600
        self.store.finish(job, 'deferred', 'HTTP 429: Retry-After', due)
        with patch.object(self.news, 'queue', side_effect=AssertionError('Resume must not queue a new search')):
            result = self.run_fixture()
        self.assertEqual(result['processed'], 0)
        self.assertEqual(result['pending'], 1)
        self.assertEqual(result['stop_reason'], 'backoff')
        self.assertEqual(result['next_retry_at'], due)
        self.assertIn('fuente exige esperar', result['message'])
        with self.store.connection() as con:
            row = con.execute('SELECT attempts,due FROM jobs WHERE id=?', (job_id,)).fetchone()
        self.assertEqual(row['attempts'], 1)
        self.assertEqual(row['due'], due)

    def test_single_feed_keeps_all_workers_available_for_new_article_jobs(self):
        barrier = threading.Barrier(3)
        threads = set()
        threads_lock = threading.Lock()

        class Fetcher(NoNetworkFetcher):
            def fetch(self, url, _source):
                if url.endswith('/feed'):
                    body = '<rss><channel>' + ''.join(
                        f'<item><title>Informe {index}</title><link>https://example.org/a-{index}</link></item>'
                        for index in range(6)) + '</channel></rss>'
                else:
                    with threads_lock:
                        threads.add(threading.get_ident())
                    barrier.wait(timeout=2)
                    body = '<article>' + ('Información pública sobre la sesión ordinaria. ' * 5) + url + '</article>'
                return dict(status='ok', url=url, body=body, sha256=digest(body), retrieved_at=now())

        with patch('peps.collector.SafeFetcher', Fetcher):
            result = self.news.acquire(workers=3, seconds=4, download_all=True)
        self.assertEqual(result['completed'], 7)
        self.assertEqual(result['pending'], 0)
        self.assertEqual(len(threads), 3)

    def test_job_limit_preserves_ready_jobs_for_resume_with_a_reason(self):
        for index in range(3):
            self.store.enqueue('news_fixture', {'index': index})

        def execute(job, *_):
            self.store.finish(job)

        with patch.object(self.news, 'execute', side_effect=execute):
            first = self.run_fixture(max_articles=1, workers=1)
            rest = self.run_fixture(max_articles=3, workers=1)
        self.assertEqual(first['completed'], 1)
        self.assertEqual(first['pending'], 2)
        self.assertEqual(first['ready'], 2)
        self.assertEqual(first['stop_reason'], 'job_limit')
        self.assertFalse(first['waiting'])
        self.assertEqual(rest['completed'], 2)
        self.assertEqual(rest['pending'], 0)

    def test_missing_lease_is_recoverable_and_fifth_failure_has_terminal_article_status(self):
        job_id = self.store.enqueue('news_fixture', {})
        with self.store.connection() as con:
            con.execute("UPDATE jobs SET status='running',owner='lost',attempts=1,lease_until=NULL WHERE id=?", (job_id,))
        recovered = self.store.claim(kind_prefix='news_')
        self.assertEqual(recovered['id'], job_id)
        self.assertEqual(recovered['attempts'], 2)
        self.store.finish(recovered)
        self.news._observe({'url': 'https://example.org/failed', 'title': 'Informe', 'text': 'Resumen'},
                           self.source, self.source['url'], {'retrieved_at': now(), 'sha256': 'fixture'}, None, True)
        article_job = self.store.claim(kind_prefix='news_')
        with self.store.connection() as con:
            con.execute('UPDATE jobs SET attempts=5 WHERE id=?', (article_job['id'],))
        article_job['attempts'] = 5
        self.news._failure(article_job, {'status': 'error', 'error': 'Transport timeout'})
        article = self.news.articles()[0]
        self.assertEqual(article['status'], 'dead')
        self.assertEqual(self.store.stats()['jobs'], {'dead': 1, 'done': 1})


if __name__ == '__main__':
    unittest.main()
