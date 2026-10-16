"""Bounded preparation, history recall and progress under synthetic load."""
import json
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from peps.engine import Engine
from peps.news import NewsAcquirer, _BodyBudget, _MetadataRelevanceCache, _MetadataScanStopped
from peps.news_search import build_search_plan, SearchPlanningStopped
from peps.relevance import RelevanceIndex
from peps.relevance import MODEL_VERSION


class MetadataMemoTests(unittest.TestCase):
    def test_exact_keys_and_copied_results_do_not_leak_candidate_mutations(self):
        index = RelevanceIndex([{'id': 'one', 'name': 'Empresa Sintética', 'kind': 'company'}])
        memo = _MetadataRelevanceCache(index)
        first = memo.rank('Empresa Sintética', '')
        first['entity_ids'].append('poison')
        first['matches'][0]['text'] = 'poison'
        again = memo.rank('Empresa Sintética', '')
        self.assertEqual(again['entity_ids'], ['one'])
        self.assertEqual(again['matches'][0]['text'], 'Empresa Sintética')
        self.assertFalse(memo.rank('Empresa', 'Sintética')['relevant'])
        self.assertEqual(memo.stats()['hits'], 1)

    def test_distinct_metadata_cannot_exceed_entry_or_accounted_memory_limits(self):
        index = RelevanceIndex([])
        memo = _MetadataRelevanceCache(index, maximum_bytes=4096, maximum_entries=4)
        for item in range(1000):
            result = memo.rank(str(item), 'Contenido de prueba. ' * (item % 50))
            self.assertFalse(result['relevant'])
            stats = memo.stats()
            self.assertLessEqual(stats['entries'], 4)
            self.assertLessEqual(stats['accounted_bytes'], 4096)
        self.assertEqual(memo.rank('outside', 'x' * 100_000), index.rank('outside', 'x' * 100_000))
        self.assertLessEqual(memo.stats()['accounted_bytes'], 4096)


class NewsStressControls(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.engine = Engine(self.temp.name, online=True)
        self.news = NewsAcquirer(self.engine)
        self.store = self.engine.store
        self.source = dict(id='fixture', kind='rss', url='https://editorial.invalid/feed',
                           allowed_hosts=['editorial.invalid'], enabled=True, terms_approved=True,
                           robots_required=False, download_articles=False)
        self.news.config_path.write_text(json.dumps({'sources': [self.source], 'search': {'strategy': 'feeds'}}))

    def test_zero_preparation_budget_never_plans_or_opens_a_network_fetcher(self):
        self.store.import_entities([{'id': str(i), 'name': f'Persona Sintética {i}'} for i in range(100)])
        events = []
        with patch.object(self.news, 'queue', side_effect=AssertionError('Planning after deadline')), \
             patch('peps.collector.SafeFetcher', side_effect=AssertionError('Fetcher after deadline')):
            result = self.news.acquire(seconds=0, progress=events.append)
        self.assertEqual(result['status'], 'paused')
        self.assertTrue(result['preparation_incomplete'])
        self.assertFalse(result['queue_complete'])
        self.assertEqual(result['pending'], 0)
        self.assertIsNotNone(result['session']['finished_at'])
        self.assertTrue(events[-1]['preparation_incomplete'])
        self.assertFalse(events[-1]['queue_complete'])

    def test_deadline_exhausted_by_projection_cannot_start_planning(self):
        clock = [10.]
        def exhaust(*args, **kwargs):
            clock[0] = 12.
            return False
        with patch('peps.news.time', SimpleNamespace(monotonic=lambda: clock[0], time=time.time)), \
             patch('peps.news_projection.ensure_projection', side_effect=exhaust), \
             patch.object(self.news, 'queue', side_effect=AssertionError('Planning after deadline')):
            result = self.news.acquire(seconds=1, relevant_only=True)
        self.assertTrue(result['preparation_incomplete'])
        self.assertEqual(result['stop_reason'], 'time_limit')

    def test_cancel_during_queue_publication_rolls_back_the_whole_plan(self):
        self.source['urls'] = ['https://editorial.invalid/first', 'https://editorial.invalid/second']
        self.news.config_path.write_text(json.dumps({'sources': [self.source], 'search': {'strategy': 'feeds'}}))
        enqueue = self.store.enqueue
        def cancel_after_first(*args, **kwargs):
            result = enqueue(*args, **kwargs)
            self.engine.stop_event.set()
            return result
        with patch.object(self.store, 'enqueue', side_effect=cancel_after_first), self.assertRaises(_MetadataScanStopped):
            self.news.queue(deadline=time.monotonic() + 10)
        with self.store.connection() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM jobs').fetchone()[0], 0)
            self.assertEqual(con.execute("SELECT count(*) FROM audit WHERE action='news_acquisition_queued'").fetchone()[0], 0)
        self.assertTrue(self.store.verify_audit()['ok'])

    def test_local_cache_controls_never_reactivate_or_reschedule_another_queue(self):
        ids = []
        later = time.time() + 3600
        for status in ('blocked', 'queued'):
            identifier = self.store.enqueue('newsflash', {'topic': status}, due=later)
            with self.store.connection() as con:
                con.execute('UPDATE jobs SET status=?,error=? WHERE id=?',
                            (status, 'local_cache_miss: synthetic unrelated task', identifier))
            ids.append(identifier)
        with self.store.connection() as con:
            before = [tuple(row) for row in con.execute('SELECT * FROM jobs ORDER BY id')]
        self.news._resume_local_misses()
        self.news.acquire(queue=False, cache_mode='local', seconds=.05)
        with self.store.connection() as con:
            after = [tuple(row) for row in con.execute('SELECT * FROM jobs ORDER BY id')]
        self.assertEqual(after, before)

    def test_progress_receipts_remain_exact_without_a_full_queue_scan_per_finish(self):
        for item in range(1000):
            self.store.enqueue('news_article', {'source_id': 'fixture', 'url': self.source['url'],
                                                'article_id': str(item)}, key=f'task-{item}')
        events = []
        def complete(job, *args, **kwargs):
            self.store.finish(job)
        frozen = SimpleNamespace(monotonic=lambda: 100., time=time.time)
        with patch('peps.news.time', frozen), patch.object(self.news, 'execute', side_effect=complete), \
             patch.object(self.news, '_runtime_queue', wraps=self.news._runtime_queue) as snapshots:
            result = self.news.acquire(queue=False, seconds=1, workers=8, max_articles=50, progress=events.append)
        receipts = [event for event in events if event.get('task_event') == 'finish']
        self.assertEqual(len(receipts), 50)
        self.assertEqual(len({event['task_id'] for event in receipts}), 50)
        self.assertTrue(all(event['task_status'] == 'done' for event in receipts))
        self.assertTrue(all(event['queue_snapshot_exact'] is False and not event['queue_complete'] for event in receipts))
        self.assertLessEqual(snapshots.call_count, 6)
        self.assertEqual(result['completed'], 50)
        self.assertEqual(result['pending'], 950)
        self.assertEqual(result['total'], 1000)
        self.assertFalse(result['queue_complete'])

    def test_malformed_article_job_cannot_break_local_rescue(self):
        identifier = self.store.enqueue('news_article', {})
        with self.store.connection() as con:
            con.execute('UPDATE jobs SET payload=? WHERE id=?', ('{broken', identifier))
        result = self.news._rescue_bodies(RelevanceIndex([]), [self.source], _BodyBudget(0, 0))
        self.assertEqual(result.get('body_queued', 0), 0)

    def test_current_projection_skips_unrelated_cards_but_an_unversioned_index_scans_safely(self):
        self.store.import_entities([{'id': 'one', 'name': 'Empresa Sintética', 'kind': 'company'}])
        entities, revision = self.news._entity_snapshot()
        index = RelevanceIndex(entities)
        index.bank_revision, index.model_version = revision, MODEL_VERSION
        self.source.update(download_articles=True, article_terms_approved=True)
        for item in range(30):
            title = 'Empresa Sintética niega la acusación.' if item == 0 else f'Boletín general ajeno {item}'
            candidate = dict(index.rank(title, ''), bank_revision=revision)
            self.news._observe({'url': f'https://editorial.invalid/{item}', 'title': title},
                               self.source, self.source['url'], {}, None, candidate=candidate,
                               body_budget=_BodyBudget(0, 0))
        with patch.object(self.news, '_stored_candidate', wraps=self.news._stored_candidate) as rank:
            projected = self.news._rescue_bodies(index, [self.source], _BodyBudget(0, 0))
        self.assertEqual(rank.call_count, 1)
        self.assertEqual(projected['body_skipped_budget'], 1)
        with patch.object(self.news, '_stored_candidate', wraps=self.news._stored_candidate) as rank:
            fallback = self.news._rescue_bodies(RelevanceIndex(entities), [self.source], _BodyBudget(0, 0))
        self.assertEqual(rank.call_count, 30)
        self.assertEqual(fallback, projected)

    def test_unhashable_source_id_is_reported_and_cannot_abandon_a_running_lease(self):
        identifier = self.store.enqueue('news_feed', {'source_id': [], 'url': self.source['url']})
        result = self.news.acquire(queue=False, seconds=1, progress=lambda _event: None)
        self.assertEqual(result['processed'], 1)
        with self.store.connection() as con:
            job = dict(con.execute('SELECT * FROM jobs WHERE id=?', (identifier,)).fetchone())
        self.assertEqual(job['status'], 'blocked')
        self.assertIsNone(job['lease_until'])
        self.assertEqual(json.loads(job['payload'])['source_id'], [])


class SearchPreparationCancellation(unittest.TestCase):
    def test_planner_stops_inside_large_bank_without_publishing_partial_queries(self):
        calls = [0]
        def stop():
            calls[0] += 1
            return calls[0] >= 125
        entities = ({'id': str(i), 'name': f'Persona Sintética {i}'} for i in range(10000))
        source = dict(id='fixture', kind='rss', search_kind='wordpress_rss',
                      url='https://editorial.invalid/feed', search_url='https://editorial.invalid/',
                      enabled=True, terms_approved=True)
        with self.assertRaises(SearchPlanningStopped):
            build_search_plan(entities, [source], {'strategy': 'targeted'}, should_stop=stop)
        self.assertEqual(calls[0], 125)


if __name__ == '__main__':
    unittest.main()
