"""Stress discoveries: literal queue scopes and interrupted preparation."""
from copy import deepcopy
import tempfile
import unittest

from peps.engine import Engine, _job_progress
from peps.news import NewsAcquirer
from peps.nlp import analyze
from peps.workflow import GuidedWorkflow, OperationTracker


class StressWorkflowTests(unittest.TestCase):
    def test_progress_scope_treats_sql_wildcards_as_literal_characters(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Engine(directory).store
            for kind in ('news_article', 'newsflash', 'scope%one', 'scopeXone', 'path\\one', 'pathone'):
                store.enqueue(kind, {}, due=0)
            for prefix in ('news_', 'scope%', 'path\\'):
                with self.subTest(prefix=prefix):
                    result = _job_progress(store, prefix, readiness=True)
                    self.assertEqual(result['jobs'], {'queued': 1})
                    self.assertEqual(result['pending'], 1)
                    self.assertEqual(result['ready'], 1)
            self.assertEqual(_job_progress(store)['pending'], 6)

    def test_unfinished_preparation_never_completes_or_trains_eta_with_an_empty_queue(self):
        tracker = OperationTracker()
        tracker.start('pipeline', phase='download', total=0)
        tracker.update({'event': 'finish'}, completed=0, total=0, pending=0, failed=0,
                       queue_complete=False, preparation_incomplete=True,
                       queue_snapshot_exact=True, queue_snapshot_age_seconds=0., stop_reason='time_limit')
        tracker.finish(result={'pending': 0, 'preparation_incomplete': True})
        value = tracker.snapshot()
        self.assertEqual(value['status'], 'paused')
        self.assertTrue(value['preparation_incomplete'])
        self.assertEqual(value['task_progress']['phases'][0]['status'], 'paused')
        self.assertNotEqual(value['percent'], 100)
        self.assertIsNone(tracker.estimate_phases('download', {'download': 10})[0]['eta_seconds'])

    def test_preparation_pause_survives_restart_and_requires_explicit_partial_decision(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = Engine(directory)
            NewsAcquirer(engine)
            store = engine.store
            sources = [{'id': 'fixture', 'kind': 'rss', 'enabled': True, 'terms_approved': True}]
            workflow = GuidedWorkflow(store, lambda: deepcopy(sources))
            store.import_entities([{'id': 'fixture', 'name': 'Persona Sintética de Estrés'}])
            workflow.confirm_sources()
            store.add_document({'url': 'https://example.org/synthetic', 'source_kind': 'official',
                                'text': 'Persona Sintética de Estrés inauguró una biblioteca.'}, ['fixture'], analyze)
            status = workflow.mark_download('completed', {'preparation_incomplete': True,
                                                          'queue_complete': False, 'stop_reason': 'time_limit'})
            self.assertEqual(status, 'paused')
            restored = GuidedWorkflow(store, lambda: deepcopy(sources))
            value = restored.snapshot()
            self.assertEqual(value['coverage']['pending_downloads'], 0)
            self.assertTrue(value['coverage']['preparation_incomplete'])
            self.assertTrue(value['coverage']['partial'])
            self.assertTrue(value['coverage']['needs_new_search'])
            self.assertFalse(value['coverage']['resume_available'])
            self.assertFalse(value['actions']['analyze']['enabled'])
            value = restored.accept_partial()
            self.assertTrue(value['actions']['analyze']['enabled'])
            self.assertTrue(value['coverage']['limitations']['preparation_incomplete'])
            self.assertTrue(store.verify_audit()['ok'])


if __name__ == '__main__':
    unittest.main()
