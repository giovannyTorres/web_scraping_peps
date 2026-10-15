"""Real committed units, cancellation and dynamic queue progress; no network."""
import json
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from peps.engine import Engine
from peps.news import NewsAcquirer
from peps.store import digest, now
from peps.workflow import OperationTracker


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.engine = Engine(self.directory.name, online=True)
        self.engine.store.import_entities([
            {'id': 'one', 'name': 'Persona Sintética Uno'},
            {'id': 'two', 'name': 'Persona Sintética Dos'},
            {'id': 'three', 'name': 'Persona Sintética Tres'},
        ])

    def news_fixture(self, count=3):
        news = NewsAcquirer(self.engine)
        source = {'id': 'fixture', 'kind': 'rss', 'allowed_hosts': ['example.org'],
                  'enabled': True, 'terms_approved': True, 'url': 'https://example.org/feed'}
        news.config_path.write_text(json.dumps({'sources': [source]}), encoding='utf-8')
        for index in range(count):
            news._observe({'url': f'https://example.org/{index}', 'title': 'Informe público',
                           'text': 'Persona Sintética Uno inauguró una biblioteca.'},
                          source, source['url'], {'retrieved_at': now(), 'sha256': str(index)}, None)
        return news

    def test_scoring_reports_each_entity_after_its_commit_and_honors_optional_cancel(self):
        events, committed, writable = [], [], []

        def observe(event):
            events.append(event)
            if event['event'] == 'progress':
                with self.engine.store.connection() as con:
                    committed.append(con.execute('SELECT count(*) FROM scores').fetchone()[0])
                # A progress observer can write without waiting for a scoring transaction.
                self.engine.store.audit('progress_observed', {'completed': event['completed']})
                writable.append(event['completed'])
                self.engine.stop_event.set()

        self.assertEqual(self.engine.rescore(progress=observe, cancelable=True), 1)
        self.assertEqual([event['completed'] for event in events], [0, 1, 1])
        self.assertEqual(committed, [1])
        self.assertEqual(writable, [1])
        self.assertTrue(events[-1]['cancelled'])
        self.assertEqual(events[-1]['pending'], 2)
        self.assertTrue(all(event['total'] == 3 and event['phase'] == 'scoring' for event in events))
        # A previous stop signal does not alter the established CLI behavior.
        self.assertEqual(self.engine.rescore(), 3)

    def test_progress_observer_failure_does_not_roll_back_scoring(self):
        observer = Mock(side_effect=RuntimeError('UI observer failed'))
        self.assertEqual(self.engine.rescore(progress=observer), 3)
        self.assertEqual(observer.call_count, 5)
        self.assertEqual(len(self.engine.store.list_entities()), 3)
        self.assertTrue(all(row['score'] for row in self.engine.store.list_entities()))

    def test_attribution_limit_reports_pending_and_none_drains_remaining_without_scoring(self):
        news = self.news_fixture()
        events, signatures = [], []

        def observe(event):
            events.append(event)
            if event['event'] == 'progress':
                with self.engine.store.connection() as con:
                    row = con.execute('SELECT attributed_signature FROM news_articles WHERE id=?',
                                      (event['article_id'],)).fetchone()
                    signatures.append(row[0])

        with patch.object(self.engine, 'rescore', side_effect=AssertionError('Unexpected scoring')):
            first = news.attribute(limit=1, rescore=False, progress=observe)
            rest = news.attribute(limit=None, rescore=False)
        self.assertEqual(first['articles_processed'], 1)
        self.assertEqual(len(signatures), 1)
        self.assertIsNotNone(signatures[0])
        self.assertEqual(first['entities_scored'], 0)
        self.assertEqual(first['entities_affected'], 1)
        self.assertEqual(first['pending'], 2)
        self.assertFalse(first['cancelled'])
        self.assertEqual(events[-1]['total'], 1)
        self.assertEqual(events[-1]['pending'], 2)
        self.assertEqual(rest['articles_processed'], 2)
        self.assertEqual(rest['pending'], 0)

    def test_attribution_can_stop_between_committed_articles(self):
        news = self.news_fixture()
        events = []

        def observe(event):
            events.append(event)
            if event['event'] == 'progress':
                self.engine.stop_event.set()

        result = news.attribute(limit=None, rescore=False, progress=observe, cancelable=True)
        self.assertEqual(result['articles_processed'], 1)
        self.assertEqual(result['pending'], 2)
        self.assertTrue(result['cancelled'])
        self.assertEqual(events[-1]['completed'], 1)
        self.assertEqual(events[-1]['total'], 3)
        self.assertEqual(news.attribute(limit=None, rescore=False)['pending'], 0)

    def exercise_download(self, news_only):
        store = self.engine.store
        prefix = 'news_fixture' if news_only else 'fixture'
        for state in ('spawn', 'deferred', 'blocked', 'dead'):
            store.enqueue(prefix, {'state': state}, key=state)

        def execute(job, *_):
            state = job['payload']['state']
            if state == 'spawn':
                store.enqueue(prefix, {'state': 'child'}, key='dynamic-child')
            store.finish(job, state if state in ('deferred', 'blocked', 'dead') else 'done',
                         retry_at=time.time() + 3600 if state == 'deferred' else None)

        events, committed = [], []

        def observe(event):
            events.append(event)
            with store.connection() as con:
                committed.append(con.execute("SELECT count(*) FROM jobs WHERE status='done'").fetchone()[0])

        class Fetcher:
            def __init__(self, *_args, **_kwargs):
                pass

        with patch('peps.collector.SafeFetcher', Fetcher):
            if news_only:
                news = self.news_fixture(count=0)
                with patch.object(news, 'execute', side_effect=execute):
                    result = news.acquire(queue=False, workers=1, seconds=2, progress=observe)
            else:
                with patch.object(self.engine, '_execute', side_effect=execute):
                    result = self.engine.run(workers=1, seconds=2, progress=observe)
        self.assertEqual(result['processed'], 5)
        self.assertEqual(result['completed'], 2)
        self.assertEqual(result['pending'], 1)
        self.assertEqual(result['deferred'], 1)
        self.assertEqual(result['blocked'], 1)
        self.assertEqual(result['dead'], 1)
        self.assertFalse(result['queue_complete'])
        self.assertEqual(events[0]['event'], 'start')
        self.assertEqual(events[-1]['event'], 'finish')
        self.assertEqual(events[-1]['pending'], 1)
        self.assertEqual(committed, [event['completed'] for event in events])
        self.assertTrue(all(event['phase'] == 'download' for event in events))
        self.assertIsNone(events[0]['total'])
        if news_only:
            self.assertTrue(all(event['total'] is None for event in events if event['deferred']))
        else:
            self.assertTrue(all(event['total'] is None for event in events))
        return result, events

    def test_engine_download_does_not_count_deferrals_as_completed(self):
        self.exercise_download(news_only=False)

    def test_news_download_distinguishes_finished_batch_and_pending_dynamic_queue(self):
        self.exercise_download(news_only=True)

    def test_news_total_and_eta_become_known_after_feed_discovery_commits(self):
        news = self.news_fixture(count=0)
        source = dict(news.sources()[0], download_articles=True, article_terms_approved=True,
                      urls=[f'https://example.org/feed-{index}' for index in range(3)])
        news.config_path.write_text(json.dumps({'sources': [source]}), encoding='utf-8')
        events, snapshots, clock = [], [], [0.]
        tracker = OperationTracker(clock=lambda: clock[0])
        tracker.start('download', phase='download')
        article_workers = threading.Barrier(3)
        feed_workers = threading.Barrier(3)

        class Fetcher:
            def __init__(self, *_args, **_kwargs):
                pass

            def fetch(self, url, _source):
                if '/feed-' in url:
                    feed_workers.wait(timeout=3)
                    body = '<rss><channel>' + ''.join(
                        f'<item><title>Informe {i}</title><link>https://example.org/article-{i}</link></item>'
                        for i in range(6)) + '</channel></rss>'
                else:
                    article_workers.wait(timeout=3)
                    body = '<html><article><p>' + ('Contenido público de la sesión municipal. ' * 5) + url + '</p></article></html>'
                return {'status': 'ok', 'url': url, 'body': body, 'sha256': digest(body), 'retrieved_at': now()}

        def observe(event):
            events.append(event)
            if event['event'] == 'progress':
                clock[0] += 1.5
            tracker.update(event)
            snapshots.append(tracker.snapshot())

        with patch('peps.collector.SafeFetcher', Fetcher):
            result = news.acquire(workers=3, seconds=5, download_all=True, progress=observe)
        self.assertEqual(result['completed'], 9)
        self.assertEqual(result['pending'], 0)
        self.assertEqual(result['total'], 9)
        self.assertIsNone(events[0]['total'])
        self.assertEqual(events[0]['discovery_pending'], 3)
        self.assertTrue(all(event['total'] is None for event in events if event['discovery_pending']))
        self.assertTrue(all(event['total'] == 9 for event in events[1:] if not event['discovery_pending']))
        self.assertIsNone(snapshots[0]['eta_seconds'])
        self.assertTrue(any(snapshot['eta_seconds'] is not None for snapshot in snapshots))

    def test_new_discovery_withdraws_a_previously_known_total(self):
        news = self.news_fixture(count=0)
        store = self.engine.store
        for index in range(2):
            store.enqueue('news_article', {'index': index}, key='known-article:' + str(index))
        events = []

        def execute(job, *_):
            store.finish(job)

        def observe(event):
            events.append(event)
            if event['event'] == 'progress' and event['completed'] == 1:
                store.enqueue('news_query', {'fixture': True}, key='new-discovery', due=time.time() + 3600)

        class Fetcher:
            def __init__(self, *_args, **_kwargs):
                pass

        with patch('peps.collector.SafeFetcher', Fetcher), patch.object(news, 'execute', side_effect=execute):
            result = news.acquire(queue=False, workers=1, seconds=1, progress=observe)
        self.assertEqual(events[1]['total'], 2)
        self.assertIsNone(events[2]['total'])
        self.assertIsNone(events[-1]['total'])
        self.assertEqual(result['discovery_pending'], 1)
        self.assertEqual(result['completed'], 2)
        self.assertEqual(result['pending'], 1)

    def test_terminal_failures_remain_in_known_total_without_counting_as_completed(self):
        news = self.news_fixture(count=0)
        store = self.engine.store
        store.enqueue('news_article', {'outcome': 'done'}, key='done-article')
        store.enqueue('news_article', {'outcome': 'blocked'}, key='blocked-article')
        events = []

        class Fetcher:
            def __init__(self, *_args, **_kwargs):
                pass

        def execute(job, *_):
            store.finish(job, job['payload']['outcome'])

        with patch('peps.collector.SafeFetcher', Fetcher), patch.object(news, 'execute', side_effect=execute):
            result = news.acquire(queue=False, workers=1, seconds=1, progress=events.append)
        self.assertEqual(result['completed'], 1)
        self.assertEqual(result['total'], 2)
        self.assertEqual(result['failed'], 1)
        self.assertEqual(result['pending'], 0)
        self.assertTrue(all(event['total'] == 2 for event in events[1:]))

    def test_early_stop_is_preserved_before_downloader_worker_starts(self):
        news = self.news_fixture(count=0)
        self.engine.stop_event.set()
        with patch('peps.collector.SafeFetcher', side_effect=AssertionError('Transport must not start')):
            engine_result = self.engine.run(reset_stop=False)
            news_result = news.acquire(reset_stop=False)
        self.assertTrue(engine_result['cancelled'])
        self.assertTrue(news_result['cancelled'])
        self.assertEqual(engine_result['processed'], 0)
        self.assertEqual(news_result['processed'], 0)
        self.assertTrue(self.engine.stop_event.is_set())


if __name__ == '__main__':
    unittest.main()
