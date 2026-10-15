"""Prerequisite, persistence and truthful progress regressions without network."""
from copy import deepcopy
import concurrent.futures
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from peps.engine import Engine
from peps.news import NewsAcquirer
from peps.nlp import analyze
from peps.workflow import GuidedWorkflow, OperationTracker, WorkflowError


class Clock:
    def __init__(self):
        self.value = 0.

    def __call__(self):
        return self.value


class OperationTrackerTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.tracker = OperationTracker(clock=self.clock)

    def test_counts_define_progress_and_eta_requires_real_samples(self):
        operation_id = self.tracker.start('download', phase='fetching', total=10)
        self.assertTrue(self.tracker.busy)
        self.assertEqual(self.tracker.snapshot()['operation_id'], operation_id)
        self.assertIsNone(self.tracker.snapshot()['eta_seconds'])
        self.clock.value = 10
        self.tracker.update({'completed': 2, 'unit': 'documentos'})
        self.assertIsNone(self.tracker.snapshot()['eta_seconds'])
        self.tracker.update(completed=3)
        self.assertEqual(self.tracker.snapshot()['percent'], 30)
        self.assertEqual(self.tracker.snapshot()['eta_seconds'], 24)
        self.assertEqual(self.tracker.snapshot()['unit'], 'documentos')
        self.tracker.update(completed=9)
        self.assertGreaterEqual(self.tracker.snapshot()['eta_seconds'], 1)

    def test_unknown_totals_and_short_samples_do_not_invent_estimates(self):
        self.tracker.start('analysis')
        self.clock.value = 30
        self.tracker.update(completed=15)
        self.assertIsNone(self.tracker.snapshot()['percent'])
        self.assertIsNone(self.tracker.snapshot()['eta_seconds'])
        self.tracker.update(phase='scoring', completed=0, total=10)
        self.clock.value = 32
        self.tracker.update(completed=3)
        self.assertIsNone(self.tracker.snapshot()['eta_seconds'])

    def test_paused_and_failed_downloads_never_claim_one_hundred_percent(self):
        for status in ('paused', 'failed'):
            self.tracker.start('download', total=3)
            self.tracker.update(completed=3)
            self.assertLess(self.tracker.snapshot()['percent'], 100)
            self.tracker.finish(status=status, message='Descarga incompleta')
            self.assertLess(self.tracker.snapshot()['percent'], 100)
            self.assertIsNone(self.tracker.snapshot()['eta_seconds'])
            self.assertFalse(self.tracker.busy)
        self.tracker.start('analysis', total=3)
        self.tracker.update(completed=3)
        self.tracker.finish(result={'scored': 3})
        self.assertEqual(self.tracker.snapshot()['percent'], 100)
        self.assertEqual(self.tracker.snapshot()['last_result'], {'scored': 3})

    def test_start_is_atomic_and_old_callbacks_cannot_finish_the_next_operation(self):
        def start(_):
            try:
                return self.tracker.start('download')
            except WorkflowError as exc:
                self.assertEqual(exc.status, 409)
                return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            identifiers = [value for value in pool.map(start, range(16)) if value]
        self.assertEqual(len(identifiers), 1)
        self.tracker.finish()
        current = self.tracker.start('analysis')
        self.assertFalse(self.tracker.update(completed=1, operation_id=identifiers[0]))
        self.assertFalse(self.tracker.finish(operation_id=identifiers[0]))
        self.assertEqual(self.tracker.snapshot()['operation_id'], current)
        self.assertTrue(self.tracker.busy)

    def test_cancel_remains_busy_until_worker_acknowledges_and_snapshots_are_copies(self):
        self.tracker.start('download', total=8)
        self.tracker.request_cancel()
        self.assertTrue(self.tracker.cancel_event.is_set())
        self.assertTrue(self.tracker.busy)
        self.assertEqual(self.tracker.snapshot()['status'], 'cancel_requested')
        with self.assertRaises(WorkflowError):
            self.tracker.start('analysis')
        self.tracker.finish('paused', result={'pending': [1]})
        snapshot = self.tracker.snapshot()
        snapshot['result']['pending'].append(2)
        self.assertEqual(self.tracker.snapshot()['result']['pending'], [1])
        elapsed = snapshot['elapsed_seconds']
        self.clock.value += 90
        self.assertEqual(self.tracker.snapshot()['elapsed_seconds'], elapsed)

    def test_invalid_or_backward_counts_are_rejected(self):
        self.tracker.start('analysis', total=10)
        for value in (-1, True, 2.5):
            with self.subTest(value=value), self.assertRaises(WorkflowError):
                self.tracker.update(completed=value)
        self.tracker.update(completed=5)
        with self.assertRaises(WorkflowError):
            self.tracker.update(completed=4)
        with self.assertRaises(WorkflowError):
            self.tracker.update(completed=11)

    def test_runtime_activity_metadata_is_bounded_copied_and_kept_in_final_receipt(self):
        self.tracker.start('download')
        event = {'event': 'activity', 'completed': 0, 'pending': 64, 'deferred': 60,
                 'ready': 2, 'active_tasks': 2, 'waiting': True, 'next_retry_at': 1100.,
                 'wait_seconds': 100., 'next_retry': '1970-01-01T00:18:20+00:00',
                 'current_source': 'Fuente ficticia', 'job_kind': 'news_article',
                 'jobs': {'queued': 4, 'deferred': 60}, 'headers': {'Authorization': 'not public'},
                 'failed': True, 'processed': -1, 'unknown': object(), 'last_error': 'x' * 3000}
        with patch('peps.workflow.time.time', return_value=1000.):
            self.tracker.update(event)
            snapshot = self.tracker.snapshot()
        self.assertEqual(snapshot['pending'], 64)
        self.assertEqual(snapshot['next_retry_seconds'], 100.)
        self.assertEqual(snapshot['current_source'], 'Fuente ficticia')
        self.assertEqual(len(snapshot['last_error']), 2000)
        self.assertNotIn('headers', snapshot)
        self.assertNotIn('unknown', snapshot['details'])
        self.assertNotIn('failed', snapshot['details'])
        self.assertNotIn('processed', snapshot['details'])
        event['jobs']['queued'] = 999
        snapshot['jobs']['queued'] = 888
        self.assertEqual(self.tracker.snapshot()['jobs']['queued'], 4)
        self.tracker.finish('paused', result={'stop_reason': 'backoff', 'pending': 64})
        with patch('peps.workflow.time.time', return_value=1050.):
            final = self.tracker.snapshot()
        self.assertEqual(final['stop_reason'], 'backoff')
        self.assertEqual(final['details']['stop_reason'], 'backoff')
        self.assertEqual(final['next_retry_seconds'], 50.)
        self.tracker.start('analysis')
        self.assertEqual(self.tracker.snapshot()['details'], {})

    def test_waiting_for_retry_does_not_publish_a_throughput_eta(self):
        self.tracker.start('download', total=10)
        self.clock.value = 10
        self.tracker.update(completed=3, waiting=True)
        self.assertIsNone(self.tracker.snapshot()['eta_seconds'])


class GuidedWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.engine = Engine(Path(self.temp.name))
        self.news = NewsAcquirer(self.engine)
        self.sources = [{'id': 'synthetic', 'kind': 'rss', 'enabled': True, 'terms_approved': True}]
        self.workflow = GuidedWorkflow(self.engine.store, lambda: deepcopy(self.sources))
        self.entity = {'id': 'synthetic', 'name': 'Persona Sintética Inventada', 'kind': 'person'}

    def import_bank(self):
        self.engine.store.import_entities([self.entity])
        self.workflow.confirm_sources()

    def document(self):
        return self.engine.store.add_document({'url': 'https://synthetic.invalid/note',
            'text': 'Persona Sintética Inventada inauguró una biblioteca.', 'source_kind': 'official'}, ['synthetic'], analyze)

    def local_analysis(self):
        self.workflow.set_mode('local')
        self.workflow.confirm_sources()
        self.workflow.mark_analysis(status='running')
        self.engine.rescore()
        self.workflow.mark_analysis()

    def test_read_only_snapshot_has_five_steps_and_never_calculates_a_score(self):
        with patch('peps.scoring.score_entity', side_effect=AssertionError('GET must not score')):
            snapshot = self.workflow.snapshot()
        self.assertEqual([step['id'] for step in snapshot['steps']], ['data', 'sources', 'download', 'analysis', 'results'])
        self.assertEqual(snapshot['recommended_step'], 'data')
        self.assertFalse(snapshot['actions']['analyze']['enabled'])
        self.assertEqual(self.engine.store.stats()['scores'], 0)

    def test_local_mode_is_explicit_persisted_and_can_report_insufficient_documents(self):
        self.import_bank()
        self.assertFalse(self.workflow.snapshot()['actions']['analyze']['enabled'])
        snapshot = self.workflow.set_mode('local')
        self.assertFalse(snapshot['actions']['analyze']['enabled'])
        snapshot = self.workflow.confirm_sources()
        self.assertTrue(snapshot['actions']['analyze']['enabled'])
        self.assertTrue(snapshot['coverage']['insufficient_documents'])
        restored = GuidedWorkflow(self.engine.store, lambda: self.sources)
        self.assertEqual(restored.snapshot()['mode'], 'local')
        self.local_analysis()
        self.assertTrue(restored.snapshot()['analysis_current'])
        self.assertTrue(restored.snapshot()['actions']['view_results']['enabled'])

    def test_busy_operation_blocks_mutations_analysis_results_and_partial_acceptance(self):
        self.import_bank()
        self.local_analysis()
        tracker = OperationTracker()
        tracker.start('download')
        for action in self.workflow.ACTIONS:
            with self.subTest(action=action), self.assertRaises(WorkflowError) as caught:
                self.workflow.require(action, tracker)
            self.assertEqual(caught.exception.status, 409)
        with self.assertRaises(WorkflowError):
            self.workflow.accept_partial(tracker)

    def test_local_analysis_ignores_inactive_online_queue_without_mutating_it(self):
        self.import_bank()
        self.workflow.set_mode('local')
        self.workflow.confirm_sources()
        job_id = self.engine.store.enqueue('news_feed', {'synthetic': True})
        for status in ('queued', 'deferred', 'running'):
            with self.engine.store.connection() as con:
                con.execute('UPDATE jobs SET status=?,lease_until=?,owner=?,due=? WHERE id=?',
                            (status, time.time() - 100 if status == 'running' else None,
                             'expired-worker' if status == 'running' else None, time.time() + 500, job_id))
                before = dict(con.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())
            snapshot = self.workflow.snapshot()
            self.assertEqual(snapshot['counts']['pending_downloads'], 1)
            with self.subTest(status=status):
                self.assertTrue(snapshot['actions']['analyze']['enabled'])
                self.workflow.mark_analysis(status='running')
                self.engine.rescore()
                self.workflow.mark_analysis()
                self.assertTrue(self.workflow.snapshot()['analysis_current'])
                for action in ('view_results', 'export', 'review'):
                    self.assertTrue(self.workflow.snapshot()['actions'][action]['enabled'])
                with self.engine.store.connection() as con:
                    self.assertEqual(before, dict(con.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()))

    def test_partial_coverage_requires_a_finished_current_acquisition_and_explicit_acceptance(self):
        self.import_bank()
        with self.assertRaises(WorkflowError):
            self.workflow.accept_partial()
        self.workflow.mark_download(status='running')
        with self.assertRaises(WorkflowError):
            self.workflow.accept_partial()
        self.workflow.mark_download(status='failed')
        self.assertFalse(self.workflow.snapshot()['actions']['analyze']['enabled'])
        snapshot = self.workflow.accept_partial()
        self.assertTrue(snapshot['coverage']['accepted_partial'])
        self.assertTrue(snapshot['actions']['analyze']['enabled'])

    def test_pending_download_requires_explicit_closure_and_completed_receipt_becomes_paused(self):
        self.import_bank()
        self.workflow.mark_download(status='running')
        self.engine.store.enqueue('news_article', {'synthetic': True})
        self.assertEqual(self.workflow.mark_download(), 'paused')
        self.assertFalse(self.workflow.snapshot()['actions']['analyze']['enabled'])
        self.assertTrue(self.workflow.snapshot()['actions']['finish_download']['enabled'])
        snapshot = self.workflow.finish_download()
        self.assertTrue(snapshot['coverage']['closed_partial'])
        self.assertTrue(snapshot['coverage']['resume_available'])
        self.assertTrue(snapshot['actions']['analyze']['enabled'])

    def test_paused_235_articles_64_jobs_can_be_closed_and_analyzed_without_losing_queue(self):
        self.import_bank()
        self.document()
        self.workflow.mark_download(status='running')
        with self.engine.store.connection() as con:
            con.executemany('INSERT INTO news_articles(id,canonical_url,publisher_id,source_id,title,summary,'
                            'discovered_at,scope,status) VALUES(?,?,?,?,?,?,?,?,?)', [
                ('fictional-' + str(number), 'https://synthetic.invalid/news/' + str(number), 'synthetic',
                 'synthetic', 'Noticia ficticia ' + str(number), 'Resumen ficticio.', '2026-10-15',
                 'feed_metadata_only', 'discovered') for number in range(235)])
        for number in range(64):
            self.engine.store.enqueue('news_article', {'synthetic': number})
        with self.engine.store.connection() as con:
            rows = con.execute('SELECT id FROM jobs ORDER BY id').fetchall()
            for index, row in enumerate(rows):
                con.execute('UPDATE jobs SET status=?,due=?,lease_until=?,owner=? WHERE id=?',
                            ('queued' if index < 32 else 'deferred' if index < 62 else 'running',
                             time.time() + 600 if 32 <= index < 62 else time.time() - 5,
                             time.time() - 30 if index >= 62 else None,
                             'expired-synthetic-worker' if index >= 62 else None, row['id']))
            before = [dict(row) for row in con.execute('SELECT * FROM jobs ORDER BY id')]
        self.workflow.mark_download(status='paused', details={'stop_reason': 'time_limit', 'pending': 64})
        snapshot = self.workflow.snapshot()
        self.assertEqual(snapshot['counts']['articles'], 235)
        self.assertEqual(snapshot['counts']['pending_downloads'], 64)
        self.assertEqual(snapshot['coverage']['active_running'], 0)
        self.assertEqual(snapshot['coverage']['expired_running'], 2)
        self.assertEqual(snapshot['coverage']['stop_reason'], 'time_limit')
        self.assertIsNotNone(snapshot['coverage']['next_retry_at'])
        self.assertFalse(snapshot['actions']['analyze']['enabled'])
        snapshot = self.workflow.finish_download()
        self.assertTrue(snapshot['coverage']['accepted_partial'])
        self.assertTrue(snapshot['coverage']['partial'])
        self.assertTrue(snapshot['coverage']['closed_partial'])
        self.assertEqual(snapshot['coverage']['limitations']['articles'], 235)
        self.assertEqual(snapshot['coverage']['limitations']['pending_downloads'], 64)
        self.assertTrue(snapshot['actions']['analyze']['enabled'])
        self.workflow.mark_analysis(status='running')
        self.engine.rescore()
        self.workflow.mark_analysis()
        restored = GuidedWorkflow(self.engine.store, lambda: deepcopy(self.sources))
        self.assertTrue(restored.snapshot()['analysis_current'])
        for action in ('view_results', 'export', 'review'):
            self.assertTrue(restored.snapshot()['actions'][action]['enabled'])
        restored.finish_download()  # Same decision is idempotent and keeps current scores.
        self.assertTrue(restored.snapshot()['analysis_current'])
        with self.engine.store.connection() as con:
            self.assertEqual(before, [dict(row) for row in con.execute('SELECT * FROM jobs ORDER BY id')])
            self.assertEqual(con.execute('SELECT count(*) FROM news_articles').fetchone()[0], 235)
            self.assertEqual(con.execute("SELECT count(*) FROM audit WHERE action='workflow_download_closed_partial'").fetchone()[0], 1)
        self.assertTrue(self.engine.store.verify_audit()['ok'])
        restored.mark_download(status='running')
        snapshot = restored.snapshot()
        self.assertFalse(snapshot['coverage']['accepted_partial'])
        self.assertFalse(snapshot['analysis_current'])
        self.assertFalse(snapshot['actions']['analyze']['enabled'])

    def test_active_cli_lease_blocks_partial_closure_and_local_analysis_until_expiry(self):
        self.import_bank()
        self.workflow.mark_download(status='paused')
        self.engine.store.enqueue('news_article', {'synthetic': True})
        job = self.engine.store.claim(lease_seconds=300, kind_prefix='news_')
        snapshot = self.workflow.snapshot()
        self.assertTrue(snapshot['coverage']['worker_active'])
        self.assertFalse(snapshot['actions']['finish_download']['enabled'])
        with self.assertRaisesRegex(WorkflowError, 'en ejecución'):
            self.workflow.finish_download()
        with self.engine.store.connection() as con:
            con.execute('UPDATE jobs SET lease_until=? WHERE id=?', (time.time() - 1, job['id']))
        self.assertTrue(self.workflow.finish_download()['actions']['analyze']['enabled'])
        self.workflow.set_mode('local')
        self.workflow.confirm_sources()
        self.engine.store.claim(lease_seconds=300, kind_prefix='news_')
        self.assertFalse(self.workflow.snapshot()['actions']['analyze']['enabled'])
        with self.assertRaisesRegex(WorkflowError, 'en ejecución'):
            self.workflow.mark_analysis(status='running')

    def test_partial_closure_expires_after_queue_activity_and_search_configuration_change(self):
        self.import_bank()
        self.document()
        self.engine.store.enqueue('news_article', {'synthetic': True})
        self.workflow.mark_download(status='paused')
        self.workflow.finish_download()
        self.engine.store.claim(lease_seconds=-1, kind_prefix='news_')
        self.assertFalse(self.workflow.snapshot()['coverage']['accepted_partial'])
        self.assertFalse(self.workflow.snapshot()['actions']['analyze']['enabled'])
        self.workflow.finish_download()
        self.assertTrue(self.workflow.snapshot()['coverage']['accepted_partial'])
        self.engine.store.audit('news_search_configured', {'days': 7})
        self.assertFalse(self.workflow.snapshot()['coverage']['accepted_partial'])
        self.assertFalse(self.workflow.snapshot()['sources_confirmed'])
        self.assertFalse(self.workflow.snapshot()['coverage']['resume_available'])
        self.assertTrue(self.workflow.snapshot()['coverage']['needs_new_search'])
        with self.assertRaisesRegex(WorkflowError, 'otra configuración'):
            self.workflow.mark_download(status='running', resume=True)
        with self.assertRaises(WorkflowError):
            self.workflow.finish_download()

    def test_local_corpus_import_does_not_change_the_scope_of_resumable_queries(self):
        self.import_bank()
        self.engine.store.enqueue('news_article', {'synthetic': True})
        self.workflow.mark_download(status='paused')
        self.document()
        snapshot = self.workflow.snapshot()
        self.assertFalse(snapshot['coverage']['acquisition_current'])
        self.assertTrue(snapshot['coverage']['resume_available'])
        self.assertFalse(snapshot['coverage']['needs_new_search'])

    def test_disabled_source_added_on_upgrade_requires_confirmation_and_resume_without_data_loss(self):
        self.import_bank()
        self.document()
        self.engine.store.enqueue('news_article', {'synthetic': True})
        self.workflow.mark_download(status='paused', details={'pending': 1})
        self.sources.append({'id': 'new-disabled-public-feed', 'enabled': False, 'terms_approved': False})
        restored = GuidedWorkflow(self.engine.store, lambda: deepcopy(self.sources))
        self.assertFalse(restored.snapshot()['sources_confirmed'])
        with self.assertRaises(WorkflowError):
            restored.finish_download()
        restored.confirm_sources()
        with self.assertRaises(WorkflowError):
            restored.finish_download()
        restored.mark_download(status='running')
        restored.mark_download(status='paused', details={'pending': 1})
        self.assertTrue(restored.finish_download()['actions']['analyze']['enabled'])
        self.assertEqual(self.engine.store.stats()['documents'], 1)
        self.assertEqual(self.engine.store.stats()['jobs']['queued'], 1)

    def test_truncated_historical_plan_requires_partial_closure_and_resume_keeps_original_period(self):
        self.import_bank()
        self.document()
        original = {'search': {'days': 7}}
        plan = {'config': {'days': 7}, 'queries': [{'query': 'Ficticia'}], 'truncated': True,
                'queries_required': 100, 'query_count': 40, 'entities_covered': 40,
                'entities_total': 100, 'warnings': ['Presupuesto de consultas recortado.'],
                'coverage': 'best_effort_not_exhaustive',
                'sources': [{'id': 'synthetic', 'historical': True, 'complete_archive': False}]}
        self.workflow.mark_download(status='running', details={'acquisition_context': original})
        self.workflow.mark_download(details={'queued': {'search_plan': plan}})
        snapshot = self.workflow.snapshot()
        self.assertEqual(snapshot['coverage']['acquisition_context'], original)
        self.assertTrue(snapshot['coverage']['partial'])
        self.assertTrue(snapshot['coverage']['plan_truncated'])
        self.assertNotIn('queries', snapshot['coverage']['search_plan'])
        self.assertFalse(snapshot['actions']['analyze']['enabled'])
        snapshot = self.workflow.finish_download()
        self.assertEqual(snapshot['coverage']['limitations']['search_plan']['query_count'], 40)
        self.workflow.mark_download(status='running', resume=True)
        self.workflow.mark_download(status='paused', details={'queued': None, 'stop_reason': 'backoff'})
        snapshot = self.workflow.snapshot()
        self.assertEqual(snapshot['coverage']['acquisition_context'], original)
        self.assertEqual(snapshot['coverage']['search_plan']['config'], {'days': 7})
        self.assertEqual(snapshot['coverage']['plan_warnings'], plan['warnings'])
        self.assertFalse(snapshot['coverage']['accepted_partial'])

    def test_external_discovery_between_attribution_and_scoring_checkpoint_is_rejected(self):
        self.import_bank()
        self.workflow.set_mode('local')
        self.workflow.confirm_sources()
        self.workflow.mark_analysis(status='running')
        self.engine.store.audit('news_discovery_completed', {'source_id': 'synthetic', 'records': 1})
        with self.assertRaisesRegex(WorkflowError, 'Los datos cambiaron durante la atribución'):
            self.workflow.mark_analysis(status='scoring')

    def test_bank_changes_source_saves_corpus_imports_and_reviews_invalidate_current_results(self):
        self.import_bank()
        self.document()
        self.workflow.mark_download(status='running')
        self.workflow.mark_download()
        self.workflow.mark_analysis(status='running')
        self.engine.rescore()
        self.workflow.mark_analysis()
        self.assertTrue(self.workflow.snapshot()['analysis_current'])
        evidence = self.engine.store.evidence('synthetic')[0]
        self.engine.store.review(evidence['id'], 'confirmed', 'Synthetic reviewer', 'Checked source and identity')
        self.assertFalse(self.workflow.snapshot()['analysis_current'])
        self.engine.rescore()
        self.workflow.mark_analysis()
        self.assertTrue(self.workflow.snapshot()['analysis_current'])
        self.engine.store.audit('news_source_configured', {'id': 'synthetic'})
        snapshot = self.workflow.snapshot()
        self.assertFalse(snapshot['analysis_current'])
        self.assertFalse(snapshot['actions']['analyze']['enabled'])
        self.workflow.mark_download(status='running')
        self.workflow.mark_download()
        self.workflow.confirm_sources()
        self.assertTrue(self.workflow.snapshot()['actions']['analyze']['enabled'])
        self.engine.store.import_entities([dict(self.entity, context={'state': 'Oaxaca'})])
        self.assertFalse(self.workflow.snapshot()['actions']['analyze']['enabled'])

    def test_external_bank_change_during_acquisition_does_not_relabel_coverage_current(self):
        self.import_bank()
        self.document()
        self.workflow.mark_download(status='running')
        self.engine.store.import_entities([{'id': 'new', 'name': 'Segunda Persona Inventada'}])
        self.workflow.mark_download()
        self.assertFalse(self.workflow.snapshot()['actions']['analyze']['enabled'])
        with self.assertRaises(WorkflowError):
            self.workflow.accept_partial()

    def test_external_input_change_during_analysis_cannot_mark_new_scores_complete(self):
        self.import_bank()
        self.workflow.set_mode('local')
        self.workflow.mark_analysis(status='running')
        self.engine.store.import_entities([{'id': 'new', 'name': 'Segunda Persona Inventada'}])
        self.engine.rescore()
        with self.assertRaisesRegex(WorkflowError, 'Los datos cambiaron'):
            self.workflow.mark_analysis()
        self.assertFalse(self.workflow.snapshot()['analysis_current'])

    def test_metadata_only_attribution_checkpoint_can_finish_without_scoring_its_index(self):
        self.import_bank()
        self.workflow.set_mode('local')
        self.workflow.confirm_sources()
        with self.engine.store.connection() as con:
            con.execute('INSERT INTO news_articles(id,canonical_url,publisher_id,source_id,title,summary,published_at,'
                        'discovered_at,scope,status) VALUES(?,?,?,?,?,?,?,?,?,?)',
                        ('synthetic-feed-note', 'https://synthetic.invalid/metadata-only', 'synthetic', 'synthetic',
                         'Persona Sintética Inventada fue acusada de fraude.', 'Resumen de un feed sin artículo descargado.',
                         '2026-10-01', '2026-10-15', 'feed_metadata_only', 'discovered'))
        self.workflow.mark_analysis(status='running')
        self.news.attribute(limit=None, rescore=False)
        self.assertGreater(self.engine.store.stats()['documents'], 0)
        self.workflow.mark_analysis(status='scoring')
        self.engine.rescore()
        self.workflow.mark_analysis()
        self.assertTrue(self.workflow.snapshot()['analysis_current'])
        self.assertEqual(self.engine.store.get_score('synthetic')['score'], 0)

    def test_scoring_checkpoint_cannot_accept_a_bank_changed_during_attribution(self):
        self.import_bank()
        self.workflow.set_mode('local')
        self.workflow.confirm_sources()
        self.workflow.mark_analysis(status='running')
        self.engine.store.import_entities([{'id': 'new', 'name': 'Segunda Persona Inventada'}])
        with self.assertRaisesRegex(WorkflowError, 'Los datos cambiaron durante la atribución'):
            self.workflow.mark_analysis(status='scoring')
        self.assertFalse(self.workflow.snapshot()['analysis_current'])

    def test_external_discovery_during_scoring_invalidates_even_without_new_bodies(self):
        self.import_bank()
        self.workflow.set_mode('local')
        self.workflow.confirm_sources()
        self.workflow.mark_analysis(status='running')
        self.workflow.mark_analysis(status='scoring')
        self.engine.rescore()
        documents = self.engine.store.stats()['documents']
        self.engine.store.audit('news_discovery_completed', {'source_id': 'synthetic', 'records': 1})
        self.assertEqual(self.engine.store.stats()['documents'], documents)
        with self.assertRaisesRegex(WorkflowError, 'Los datos cambiaron durante el análisis'):
            self.workflow.mark_analysis()
        self.assertFalse(self.workflow.snapshot()['analysis_current'])

    def test_changing_source_file_without_an_audit_event_also_invalidates_readiness(self):
        self.import_bank()
        self.document()
        self.workflow.mark_download()
        self.assertTrue(self.workflow.snapshot()['actions']['analyze']['enabled'])
        self.sources[0]['url'] = 'https://changed.invalid/feed'
        self.assertFalse(self.workflow.snapshot()['actions']['analyze']['enabled'])

    def test_invalid_mode_and_unknown_action_have_bad_request_status(self):
        for function, value in ((self.workflow.set_mode, 'silent'), (self.workflow.require, 'unknown')):
            with self.assertRaises(WorkflowError) as caught:
                function(value)
            self.assertEqual(caught.exception.status, 400)

    def test_confirming_sources_is_persisted_and_rebound_to_mode_and_configuration(self):
        self.engine.store.import_entities([self.entity])
        self.assertFalse(self.workflow.snapshot()['sources_confirmed'])
        self.assertFalse(self.workflow.snapshot()['actions']['download']['enabled'])
        self.workflow.confirm_sources()
        restored = GuidedWorkflow(self.engine.store, lambda: self.sources)
        self.assertTrue(restored.snapshot()['sources_confirmed'])
        self.sources[0]['url'] = 'https://changed.invalid/feed'
        self.assertFalse(restored.snapshot()['sources_confirmed'])
        self.workflow.confirm_sources()
        self.workflow.set_mode('local')
        self.assertFalse(restored.snapshot()['sources_confirmed'])
        self.assertFalse(restored.snapshot()['actions']['analyze']['enabled'])
        self.workflow.confirm_sources()
        self.assertTrue(restored.snapshot()['actions']['analyze']['enabled'])

    def test_expired_cli_lease_can_resume_but_cannot_be_analyzed(self):
        self.import_bank()
        self.engine.store.enqueue('news_feed', {'synthetic': True})
        self.engine.store.claim(lease_seconds=-1, kind_prefix='news_')
        snapshot = self.workflow.snapshot()
        self.assertEqual(snapshot['coverage']['running'], 1)
        self.assertEqual(snapshot['coverage']['active_running'], 0)
        self.assertTrue(snapshot['actions']['download']['enabled'])
        self.assertTrue(snapshot['actions']['configure_sources']['enabled'])
        self.assertFalse(snapshot['actions']['analyze']['enabled'])


if __name__ == '__main__':
    unittest.main()
