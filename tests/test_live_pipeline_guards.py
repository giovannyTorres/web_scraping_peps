"""Input and restart guards only; these unit checks are not live benchmarks."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import time
import unittest

from peps.engine import Engine
from peps.news import NewsAcquirer
from peps.store import canonical, digest
from scripts.validate_live_pipeline import (
    BudgetStop, Journal, RequestBudget, analysis_provenance, compare_local_corpus, effective_config, parser,
    prepare_inputs, run, source_results, validate_resume, write_json,
)


class LivePipelineGuardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.roster = self.directory / 'roster.json'
        self.config = self.directory / 'sources.json'
        rows = [
            {'id': 'public-municipal', 'name': 'Andrés Manuel López Obrador',
             'entity_group': 'municipal_president', 'source_url': 'https://www.gob.mx/',
             'incident': 'not-an-acquisition-input', 'reference_urls': ['https://example.org/gold-do-not-query']},
            {'id': 'public-notary', 'name': 'Hugo Salgado Castañeda',
             'entity_group': 'notary', 'source_url': 'https://www.notariadomexicano.org.mx/'},
        ]
        write_json(self.roster, {'entities': rows})
        defaults = Path(__file__).resolve().parents[1] / 'peps/defaults/news_sources.json'
        config = json.loads(defaults.read_text(encoding='utf-8'))
        source = deepcopy(next(row for row in config['sources'] if row['id'] == 'contralinea_index'))
        source.update(enabled=True, terms_approved=True, robots_required=True,
                      download_articles=True, article_terms_approved=True, body_terms_approved=True)
        config['sources'] = [source]
        config['search'].update(strategy='targeted', preset='identity', include_aliases=False,
                                use_context=False, max_queries=10000)
        write_json(self.config, config)
        self.args = parser().parse_args(['--online', '--allow-small-roster', '--roster', str(self.roster),
            '--sources-config', str(self.config), '--data-dir', str(self.directory / 'bank'),
            '--output', str(self.directory / 'result.json')])

    def test_plan_contains_both_names_and_excludes_reference_labels(self):
        people, cases, roster, config, plan, frozen = prepare_inputs(self.args)
        self.assertEqual({identifier for query in plan['queries'] for identifier in query['entity_ids']},
                         {'public-municipal', 'public-notary'})
        self.assertFalse(plan['truncated'])
        self.assertFalse(config['search']['use_context'])
        self.assertTrue(all('incident' not in person and 'reference_urls' not in person for person in people))
        self.assertNotIn('gold-do-not-query', json.dumps(plan))
        self.assertNotIn('not-an-acquisition-input', json.dumps(plan))

    def test_incomplete_query_budget_is_rejected_before_bank_open(self):
        value = json.loads(self.config.read_text(encoding='utf-8'))
        value['search']['max_queries'] = 1
        write_json(self.config, value)
        with self.assertRaisesRegex(ValueError, 'recorta el plan'):
            prepare_inputs(self.args)
        self.assertFalse(self.args.data_dir.exists())

    def test_source_body_permission_cannot_be_inferred(self):
        value = json.loads(self.config.read_text(encoding='utf-8'))
        value['sources'][0]['body_terms_approved'] = False
        write_json(self.config, value)
        with self.assertRaisesRegex(ValueError, 'por separado'):
            effective_config(self.config)

    def test_general_index_strategy_is_rejected(self):
        value = json.loads(self.config.read_text(encoding='utf-8'))
        value['search']['strategy'] = 'shared'
        write_json(self.config, value)
        with self.assertRaisesRegex(ValueError, 'sin índices generales'):
            effective_config(self.config)

    def test_report_cannot_replace_a_future_bank_database(self):
        self.args.output = self.args.data_dir / 'peps.sqlite3'
        with self.assertRaisesRegex(ValueError, 'fuera del banco'):
            run(self.args)
        self.assertFalse(self.args.data_dir.exists())

    def test_source_volume_uses_original_body_adapter_not_host_or_first_discovery(self):
        engine = Engine(self.args.data_dir)
        NewsAcquirer(engine)
        document = engine.store.add_document({'url': 'https://publisher.example/unit-regression',
            'title': 'Unit metadata join regression', 'text': 'Unit fixture only; not a live article or benchmark.',
            'source_kind': 'news', 'source_id': 'approved_body_adapter'}, [], lambda *_: {})
        with engine.store.connection() as con:
            con.execute('INSERT INTO news_articles(id,canonical_url,publisher_id,source_id,title,summary,discovered_at,scope,status,document_id) '
                'VALUES(?,?,?,?,?,?,?,?,?,?)', ('unit-article', 'https://publisher.example/unit-regression',
                'publisher.example', 'first_discovery_adapter', 'Unit metadata join regression', '',
                '2026-10-17T00:00:00+00:00', 'full_article', 'downloaded', document))
        config = {'sources': [{'id': 'approved_body_adapter', 'enabled': True},
                              {'id': 'first_discovery_adapter', 'enabled': True}]}
        rows = {row['source_id']: row for row in source_results(config, [], [], [], engine.store)}
        self.assertEqual(rows['approved_body_adapter']['original_documents_cumulative'], 1)
        self.assertEqual(rows['first_discovery_adapter']['original_documents_cumulative'], 0)

    def reference_cache_rows(self):
        before = {table: {} for table in ('news_articles', 'news_observations', 'documents',
            'evidence', 'news_identity_links', 'scores', 'score_runs', 'audit')}
        before['news_articles']['article-key'] = {'id': 'reference', 'scope': 'feed_metadata_only',
            'document_id': 'discovery-document', 'status': 'discovered', 'error': '', 'canonical_url': 'https://example.org/reference'}
        before['documents']['document-key'] = {'id': 'discovery-document', 'source_kind': 'discovery',
            'metadata': canonical({'discovery_only': True}), 'text': 'Unit metadata reference, not an original article.'}
        before['evidence']['evidence-key'] = {'id': 'metadata-evidence', 'document_id': 'discovery-document'}
        after = deepcopy(before)
        after['news_articles']['article-key'].update(status='blocked', error='local_cache_miss: no actual saved body')
        jobs = [{'kind': 'news_article', 'status': 'blocked', 'error': 'local_cache_miss: no actual saved body',
                 'payload': canonical({'article_id': 'reference'})}]
        return before, after, jobs

    def test_cache_reference_transition_is_explicit_and_keeps_strict_difference(self):
        before, after, jobs = self.reference_cache_rows()
        result = compare_local_corpus(before, after, jobs)
        self.assertTrue(result['passed'])
        self.assertFalse(result['strict_first_pass_rows_unchanged'])
        self.assertEqual(len(result['first_pass_article_changes']), 1)
        self.assertTrue(result['first_pass_article_changes'][0]['linked_blocked_article_job'])

    def test_cache_reference_transition_without_matching_job_fails(self):
        before, after, jobs = self.reference_cache_rows()
        self.assertFalse(compare_local_corpus(before, after, [])['passed'])

    def test_cache_guard_rejects_original_body_or_evidence_mutation(self):
        before, after, jobs = self.reference_cache_rows()
        with self.subTest('original body'):
            before['news_articles']['article-key']['scope'] = 'full_article'
            after['news_articles']['article-key']['scope'] = 'full_article'
            self.assertFalse(compare_local_corpus(before, after, jobs)['passed'])
        before, after, jobs = self.reference_cache_rows()
        with self.subTest('evidence mutation'):
            after['evidence']['evidence-key']['document_id'] = 'another-document'
            self.assertFalse(compare_local_corpus(before, after, jobs)['passed'])

    def provenance(self, statuses, queue_complete=True, coverage=None):
        return analysis_provenance([{'status': status} for status in statuses], coverage or {},
            {'queue_complete': queue_complete}, plan_sha256='frozen-plan', attempt_dir='unit-attempt')

    def test_drained_failed_source_queue_is_partial(self):
        result = self.provenance(['done', 'blocked'])
        self.assertTrue(result['partial'])
        self.assertEqual(result['pending_jobs'], 0)
        self.assertEqual(result['blocked_count'], 1)
        self.assertTrue(result['known_queue_finished'])

    def test_drained_dead_source_queue_is_partial(self):
        result = self.provenance(['done', 'dead'])
        self.assertTrue(result['partial'])
        self.assertEqual(result['dead_count'], 1)

    def test_drained_explicit_failed_source_queue_is_partial(self):
        result = self.provenance(['failed', 'failed'])
        self.assertTrue(result['partial'])
        self.assertEqual(result['failed_count'], 2)
        self.assertEqual(result['source_failure_jobs'], 2)

    def test_each_live_pending_state_keeps_coverage_partial(self):
        result = self.provenance(['queued', 'deferred', 'running'])
        self.assertTrue(result['partial'])
        self.assertEqual(result['pending_jobs'], 3)
        self.assertFalse(result['known_queue_finished'])

    def test_incomplete_preparation_without_jobs_stays_partial(self):
        result = self.provenance([], queue_complete=False)
        self.assertTrue(result['partial'])
        self.assertIn('known_queue_preparation_or_execution_incomplete', result['partial_reason_codes'])

    def test_successful_known_queue_does_not_claim_full_history(self):
        result = self.provenance(['done'])
        self.assertFalse(result['partial'])
        self.assertFalse(result['historical_search_complete'])

    def test_replaced_body_candidate_does_not_invent_source_failure(self):
        result = self.provenance(['done', 'cancelled'])
        self.assertFalse(result['partial'])
        self.assertEqual(result['source_failure_jobs'], 0)

    def test_coverage_snapshot_and_plan_remain_explicit(self):
        coverage = {'counts': {'texts': 3}}
        result = self.provenance(['blocked'], coverage=coverage)
        coverage['counts']['texts'] = 0
        self.assertEqual(result['coverage']['counts']['texts'], 3)
        self.assertEqual(result['frozen_plan_sha256'], 'frozen-plan')
        self.assertEqual(result['coverage_snapshot_stage'], 'before_identity_attribution')

    def test_missing_queue_completion_cannot_claim_complete(self):
        result = analysis_provenance([], {}, {}, plan_sha256='frozen-plan', attempt_dir='unit-attempt')
        self.assertTrue(result['partial'])

    def make_resume_bank(self):
        people, cases, roster, config, plan, frozen = prepare_inputs(self.args)
        engine = Engine(self.args.data_dir)
        engine.store.import_entities(people)
        with engine.store.connection() as con:
            rows = [dict(row) for row in con.execute('SELECT id,name,kind,aliases,context FROM entities ORDER BY id')]
        write_json(self.args.data_dir / 'frozen_inputs.json',
                   {'hashes': frozen, 'imported_entities_sha256': digest(canonical(rows))})
        engine.store.audit('live_pipeline_started', {'frozen_inputs': frozen})
        return engine, frozen

    def test_identical_resume_retains_same_input_receipt(self):
        engine, frozen = self.make_resume_bank()
        counts_before = engine.store.stats()
        self.assertEqual(validate_resume(self.args.data_dir, frozen)['hashes'], frozen)
        self.assertEqual(engine.store.stats(), counts_before)

    def test_resume_rejects_changed_entity_values(self):
        engine, frozen = self.make_resume_bank()
        with engine.store.connection() as con:
            con.execute("UPDATE entities SET context=? WHERE id='public-notary'", (canonical({'state': 'Changed'}),))
        with self.assertRaisesRegex(ValueError, 'Cambió un nombre, alias, contexto'):
            validate_resume(self.args.data_dir, frozen)

    def test_resume_rejects_changed_frozen_catalogue_or_budget(self):
        engine, frozen = self.make_resume_bank()
        changed = dict(frozen, maximum_http_total=frozen['maximum_http_total'] - 1)
        with self.assertRaisesRegex(ValueError, 'límites congelados'):
            validate_resume(self.args.data_dir, changed)

    def test_request_budget_counts_prior_actual_requests_and_reserves_atomically(self):
        journal = Journal(self.directory)
        budget = RequestBudget(3, 2, time.monotonic() + 30, journal)
        event = budget.reserve('https://example.org/robots.txt', 'https://example.org/search', 'publisher')
        self.assertEqual(event['request_id'], 3)
        self.assertTrue(event['robots_request'])
        with self.assertRaises(BudgetStop):
            budget.reserve('https://example.org/search', 'https://example.org/search', 'publisher')
        self.assertEqual(len(budget.events), 1)
        self.assertEqual(budget.reason, 'cumulative_http_budget')


if __name__ == '__main__':
    unittest.main()
