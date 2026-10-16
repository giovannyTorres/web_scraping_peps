"""Synthetic loopback API regressions for the one-action pipeline.

No public source is contacted. Acquisition is an explicit fixture; workflow
receipts, queue state, scoring and HTTP authorization use the real backend.
"""
import http.client
import json
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from peps.app import create_server
from peps.engine import Engine
from peps.news import NewsAcquirer
from peps.store import digest
from peps.workflow import GuidedWorkflow


class PipelineApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.engine = Engine(self.temporary.name)
        self.engine.store.import_entities([{'id': 'synthetic', 'name': 'Persona Pública Sintética'}])
        self.news = NewsAcquirer(self.engine)
        self.news.config_path.write_text(json.dumps({
            'sources': [{'id': 'fixture', 'source_name': 'Fuente sintética', 'kind': 'rss',
                         'search_kind': 'wordpress_rss', 'search_url': 'https://publisher.invalid/',
                         'url': 'https://publisher.invalid/feed/', 'allowed_hosts': ['publisher.invalid'],
                         'enabled': False, 'terms_approved': False,
                         'download_articles': False, 'article_terms_approved': False}],
            'search': {'strategy': 'targeted', 'start': '2020-01-01', 'end': '2020-12-31',
                       'preset': 'identity', 'window': 'year', 'max_queries': 1,
                       'include_aliases': False, 'use_context': False,
                       'execution_mode': 'quick', 'session_seconds': 120}}), encoding='utf-8')
        self.server, self.token = create_server(self.engine)
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=.01), daemon=True)
        self.thread.start()
        self.releases = []

    def tearDown(self):
        for event in self.releases:
            event.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if not self.get('/api/stats')['operation']['busy']:
                break
            threading.Event().wait(.01)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temporary.cleanup()

    def request(self, path, method='GET', data=None, *, token=True, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        supplied = {'X-PEPS-Token': self.token} if token else {}
        supplied.update(headers or {})
        try:
            connection.request(method, path, json.dumps(data) if data is not None else None, supplied)
            response = connection.getresponse()
            raw = response.read()
            return response.status, json.loads(raw), dict(response.getheaders())
        finally:
            connection.close()

    def get(self, path):
        status, result, _ = self.request(path)
        self.assertEqual(status, 200, result)
        return result

    def post(self, path, data):
        status, result, _ = self.request(path, 'POST', data)
        self.assertEqual(status, 200, result)
        return result

    def authorize(self):
        self.post('/api/news-source', {'id': 'fixture', 'enabled': True, 'terms_approved': True,
                                      'download_articles': False, 'article_terms_approved': False})

    def wait_finished(self, expected):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            operation = self.get('/api/stats')['operation']
            if not operation['busy']:
                self.assertEqual(operation['status'], expected, operation)
                return operation
            threading.Event().wait(.01)
        self.fail('Synthetic pipeline did not acknowledge completion within 10 seconds')

    @staticmethod
    def report_download(options, *, pending=0):
        progress = options['progress']
        progress({'phase': 'download', 'event': 'start', 'completed': 0, 'total': 1, 'unit': 'tareas'})
        progress({'phase': 'download', 'event': 'finish', 'completed': 1, 'total': 1,
                  'unit': 'tareas', 'pending': pending, 'failed': 0, 'queue_complete': pending == 0})
        return {'status': 'ok', 'processed': 1, 'completed': 1, 'pending': pending, 'failed': 0}

    def seed_paused_acquisition(self):
        self.authorize()
        self.post('/api/workflow-confirm-sources', {})
        workflow = GuidedWorkflow(self.engine.store, self.news.sources)
        workflow.mark_download(status='running')
        job = self.engine.store.enqueue('news_query', {'source_id': 'fixture', 'entity_ids': ['synthetic'],
                          'url': 'https://publisher.invalid/?s=synthetic'}, due=time.time() + 3600)
        workflow.mark_download(status='paused')
        return job

    def test_run_rejects_string_booleans_and_invalid_cache_modes_without_side_effects(self):
        self.authorize()
        before = self.news.config_path.read_bytes()
        invalid = []
        for field in ('online', 'resume', 'analyze_after_download'):
            for value in ('true', 'false', 1, 0, None, [], {}):
                invalid.append({'online': True, field: value})
        invalid.extend({'online': True, 'cache_mode': mode} for mode in ('fresh', '', None, [], {}))
        invalid.extend(({'online': True, 'cache_mode': 'local'},
                        {'online': False, 'cache_mode': 'reuse'},
                        {'online': False, 'cache_mode': 'revalidate'},
                        {'online': True, 'search': []}))
        with patch('peps.news.NewsAcquirer.acquire') as acquire:
            for payload in invalid:
                with self.subTest(payload=payload):
                    status, body, _ = self.request('/api/workflow-run', 'POST', payload)
                    self.assertEqual(status, 400, body)
            acquire.assert_not_called()
        self.assertFalse(self.engine.online)
        self.assertEqual(self.news.config_path.read_bytes(), before)
        self.assertFalse(self.get('/api/workflow')['sources_confirmed'])

    def test_invalid_or_oversized_request_length_cannot_start_an_operation(self):
        before = self.news.config_path.read_bytes()
        audit_before = self.engine.store.stats()['audit']
        with patch('peps.news.NewsAcquirer.acquire') as acquire:
            for length, expected in (('0', 413), ('-1', 413), ('16000001', 413), ('invalid', 400)):
                with self.subTest(length=length):
                    status, _, _ = self.request('/api/workflow-run', 'POST', {},
                                                headers={'Content-Length': length})
                    self.assertEqual(status, expected)
        acquire.assert_not_called()
        self.assertFalse(self.get('/api/stats')['operation']['busy'])
        self.assertEqual(self.news.config_path.read_bytes(), before)
        self.assertEqual(self.engine.store.stats()['audit'], audit_before)

    def test_one_action_never_enables_a_source_or_approves_editorial_permissions(self):
        before = self.news.config_path.read_bytes()
        with patch('peps.news.NewsAcquirer.acquire') as acquire:
            status, body, _ = self.request('/api/workflow-run', 'POST', {'online': True,
                'enabled': True, 'terms_approved': True, 'download_articles': True,
                'article_terms_approved': True, 'analyze_after_download': True})
        self.assertEqual(status, 409, body)
        acquire.assert_not_called()
        self.assertFalse(self.engine.online)
        self.assertEqual(self.news.config_path.read_bytes(), before)
        self.assertFalse(self.get('/api/workflow')['sources_confirmed'])

    def test_new_start_saves_settings_confirms_existing_consent_and_passes_cache_mode(self):
        self.authorize()
        def acquire(**options):
            return self.report_download(options)
        with patch('peps.news.NewsAcquirer.acquire', side_effect=acquire) as mocked:
            started = self.post('/api/workflow-run', {'online': True, 'cache_mode': 'revalidate',
                'search': {'preset': 'public_role'}, 'analyze_after_download': False})
            operation = self.wait_finished('completed')
        self.assertEqual(started['operation_id'], operation['operation_id'])
        self.assertEqual(self.get('/api/news-search')['config']['preset'], 'public_role')
        self.assertTrue(self.get('/api/workflow')['sources_confirmed'])
        self.assertEqual(mocked.call_args.kwargs['cache_mode'], 'revalidate')
        self.assertTrue(mocked.call_args.kwargs['queue'])
        source = next(item for item in self.news.sources() if item['id'] == 'fixture')
        self.assertFalse(source['download_articles'])
        self.assertFalse(source['article_terms_approved'])

    def test_local_cache_start_keeps_network_disabled(self):
        self.authorize()
        with patch('peps.news.NewsAcquirer.acquire', side_effect=lambda **options: self.report_download(options)) as acquire:
            self.post('/api/workflow-run', {'online': False, 'cache_mode': 'local'})
            self.wait_finished('completed')
        self.assertFalse(self.engine.online)
        self.assertEqual(acquire.call_args.kwargs['cache_mode'], 'local')

    def test_busy_run_cannot_save_settings_change_network_or_start_another_analysis(self):
        self.authorize()
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        def acquire(**options):
            entered.set()
            if not release.wait(5):
                raise AssertionError('Synthetic acquisition was not released')
            return self.report_download(options)
        with patch('peps.news.NewsAcquirer.acquire', side_effect=acquire) as acquisition, \
             patch('peps.news.NewsAcquirer.attribute') as attribute:
            self.post('/api/workflow-run', {'online': False, 'cache_mode': 'local'})
            self.assertTrue(entered.wait(5))
            before = self.news.config_path.read_bytes()
            for path, payload in (('/api/workflow-run', {'online': True, 'search': {'preset': 'adverse'}}),
                                  ('/api/workflow-analyze-available', {}),
                                  ('/api/news-retry', {'job_ids': [digest('synthetic-retry')]}),
                                  ('/api/news-search', {'preset': 'adverse'})):
                status, body, _ = self.request(path, 'POST', payload)
                self.assertEqual(status, 409, body)
            self.assertFalse(self.engine.online)
            self.assertEqual(self.news.config_path.read_bytes(), before)
            self.assertEqual(acquisition.call_count, 1)
            attribute.assert_not_called()
            release.set()
            self.wait_finished('completed')

    def test_resume_rejects_new_scope_and_keeps_existing_queue(self):
        job = self.seed_paused_acquisition()
        before = self.news.config_path.read_bytes()
        with patch('peps.news.NewsAcquirer.acquire', return_value={'pending': 1, 'failed': 0}) as acquire:
            status, body, _ = self.request('/api/workflow-run', 'POST', {'online': True, 'resume': True,
                                                                      'search': {'preset': 'adverse'}})
            self.assertEqual(status, 400, body)
            acquire.assert_not_called()
            self.assertEqual(self.news.config_path.read_bytes(), before)
            self.post('/api/workflow-run', {'online': True, 'resume': True})
            self.wait_finished('paused')
        self.assertFalse(acquire.call_args.kwargs['queue'])
        with self.engine.store.connection() as con:
            self.assertEqual(con.execute('SELECT status FROM jobs WHERE id=?', (job,)).fetchone()[0], 'queued')

    def test_rejected_resume_does_not_confirm_a_changed_source_scope(self):
        self.seed_paused_acquisition()
        self.post('/api/news-source', {'id': 'fixture', 'enabled': True, 'terms_approved': True,
                                      'download_articles': True, 'article_terms_approved': False})
        self.assertFalse(self.get('/api/workflow')['sources_confirmed'])
        with patch('peps.news.NewsAcquirer.acquire') as acquire:
            status, body, _ = self.request('/api/workflow-run', 'POST', {'online': True, 'resume': True})
        self.assertEqual(status, 409, body)
        acquire.assert_not_called()
        self.assertFalse(self.get('/api/workflow')['sources_confirmed'])
        self.assertFalse(self.engine.online)

    def test_automatic_pipeline_stops_before_attribution_on_pending_or_limited_coverage(self):
        self.authorize()
        self.engine.store.add_document({'url': 'https://publisher.invalid/local', 'title': 'Synthetic source',
                                       'text': 'A synthetic neutral source document.', 'source_kind': 'official'}, [], lambda *_: {})
        for pending in (1, 0):
            with self.subTest(pending=pending):
                def acquire(**options):
                    if pending:
                        self.engine.store.enqueue('news_fixture', {'source_id': 'fixture'}, key='synthetic-pending')
                    result = self.report_download(options, pending=pending)
                    if not pending:
                        result['queued'] = {'search_plan': {'truncated': True, 'warnings': ['Synthetic limited plan']}}
                    return result
                with patch('peps.news.NewsAcquirer.acquire', side_effect=acquire), \
                     patch('peps.news.NewsAcquirer.attribute') as attribute, patch.object(self.engine, 'rescore') as score:
                    self.post('/api/workflow-run', {'online': True, 'analyze_after_download': True})
                    operation = self.wait_finished('paused')
                attribute.assert_not_called()
                score.assert_not_called()
                self.assertFalse(self.get('/api/workflow')['coverage']['accepted_partial'])
                phases = {phase['id']: phase for phase in operation['task_progress']['phases']}
                self.assertIn('attribution', phases)
                self.assertIn('scoring', phases)
                self.assertNotEqual(phases['scoring']['status'], 'completed')
                if pending:
                    with self.engine.store.connection() as con:
                        con.execute("UPDATE jobs SET status='cancelled' WHERE id=?", (digest('synthetic-pending'),))
                else:
                    self.assertTrue(self.get('/api/workflow')['coverage']['plan_truncated'])

    def test_complete_pipeline_reports_all_phases_and_scores_the_real_synthetic_bank(self):
        self.authorize()
        self.engine.store.add_document({'url': 'https://publisher.invalid/local', 'title': 'Synthetic source',
                                       'text': 'A synthetic neutral source document.', 'source_kind': 'official'}, [], lambda *_: {})
        with patch('peps.news.NewsAcquirer.acquire', side_effect=lambda **options: self.report_download(options)):
            self.post('/api/workflow-run', {'online': True, 'analyze_after_download': True})
            operation = self.wait_finished('completed')
        self.assertEqual(operation['kind'], 'pipeline')
        self.assertEqual([phase['id'] for phase in operation['task_progress']['phases']],
                         ['download', 'attribution', 'scoring'])
        self.assertTrue(all(phase['status'] == 'completed' for phase in operation['task_progress']['phases']))
        self.assertEqual(self.engine.store.stats()['scores'], 1)
        self.assertTrue(self.get('/api/workflow')['analysis_current'])

    def test_incomplete_preparation_pauses_zero_pending_pipeline_before_analysis(self):
        self.authorize()
        self.engine.store.add_document({'url': 'https://publisher.invalid/local', 'title': 'Synthetic source',
                                       'text': 'A synthetic neutral source document.', 'source_kind': 'official'}, [], lambda *_: {})
        report = {'processed': 0, 'completed': 0, 'pending': 0, 'failed': 0,
                  'preparation_incomplete': True, 'queue_complete': False, 'status': 'paused'}
        with patch('peps.news.NewsAcquirer.acquire', return_value=report), \
             patch('peps.news.NewsAcquirer.attribute') as attribute, patch.object(self.engine, 'rescore') as score:
            self.post('/api/workflow-run', {'online': True, 'analyze_after_download': True})
            operation = self.wait_finished('paused')
        attribute.assert_not_called()
        score.assert_not_called()
        workflow = self.get('/api/workflow')
        self.assertEqual(workflow['coverage']['status'], 'paused')
        self.assertFalse(workflow['actions']['analyze']['enabled'])
        self.assertFalse(workflow['analysis_current'])
        self.assertEqual(operation['result']['pending'], 0)
        self.assertTrue(operation['result']['preparation_incomplete'])

    def test_explicit_analyze_available_archives_partial_consent_without_deleting_queue(self):
        job = self.seed_paused_acquisition()
        with self.engine.store.connection() as con:
            before = tuple(con.execute('SELECT * FROM jobs WHERE id=?', (job,)).fetchone())
        self.post('/api/workflow-analyze-available', {})
        self.wait_finished('completed')
        workflow = self.get('/api/workflow')
        self.assertTrue(workflow['coverage']['accepted_partial'])
        self.assertTrue(workflow['analysis_current'])
        self.assertEqual(workflow['coverage']['limitations']['pending_downloads'], 1)
        explanation = self.get('/api/explanation?id=synthetic')
        self.assertTrue(explanation['result']['provenance']['acquisition']['accepted_partial'])
        with self.engine.store.connection() as con:
            after = tuple(con.execute('SELECT * FROM jobs WHERE id=?', (job,)).fetchone())
            audits = con.execute("SELECT count(*) FROM audit WHERE action='workflow_download_closed_partial'").fetchone()[0]
        self.assertEqual(after, before)
        self.assertEqual(audits, 1)
        self.assertTrue(self.engine.store.verify_audit()['ok'])

    def test_cancel_requested_during_attribution_keeps_mutations_blocked_and_skips_scoring(self):
        self.authorize()
        self.engine.store.add_document({'url': 'https://publisher.invalid/local', 'title': 'Synthetic source',
                                       'text': 'A synthetic neutral source document.', 'source_kind': 'official'}, [], lambda *_: {})
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        def attribute(**options):
            options['progress']({'phase': 'attribution', 'event': 'start', 'completed': 0,
                                 'total': 1, 'unit': 'artículos'})
            entered.set()
            if not release.wait(5):
                raise AssertionError('Synthetic attribution was not released')
            return {'pending': 1, 'cancelled': True}
        with patch('peps.news.NewsAcquirer.acquire', side_effect=lambda **options: self.report_download(options)), \
             patch('peps.news.NewsAcquirer.attribute', side_effect=attribute), patch.object(self.engine, 'rescore') as score:
            started = self.post('/api/workflow-run', {'online': True, 'analyze_after_download': True})
            self.assertTrue(entered.wait(5))
            active = self.get('/api/stats')
            self.assertEqual(active['operation']['phase'], 'attribution')
            steps = {step['id']: step for step in active['workflow']['steps']}
            self.assertEqual(steps['analysis']['status'], 'running')
            self.assertNotEqual(steps['download']['status'], 'running')
            self.assertEqual(self.request('/api/stop', 'POST', {'operation_id': 'stale'})[0], 409)
            self.post('/api/stop', {'operation_id': started['operation_id']})
            self.assertEqual(self.get('/api/stats')['operation']['status'], 'cancel_requested')
            self.assertEqual(self.request('/api/workflow-analyze-available', 'POST', {})[0], 409)
            self.assertEqual(self.request('/api/workflow-run', 'POST', {'online': True})[0], 409)
            release.set()
            operation = self.wait_finished('paused')
        score.assert_not_called()
        self.assertNotEqual({phase['id']: phase for phase in operation['task_progress']['phases']}['scoring']['status'], 'completed')

    def test_task_pages_are_bounded_filterable_and_do_not_start_acquisition(self):
        for index in range(7):
            job = self.engine.store.enqueue('news_query', {'source_id': 'fixture' if index < 5 else 'other',
                    'url': f'https://publisher.invalid/query/{index}'}, key=f'page-{index}', due=time.time() + 3600)
            if index < 5:
                with self.engine.store.connection() as con:
                    con.execute("UPDATE jobs SET status='deferred',attempts=2,error='HTTP 429' WHERE id=?", (job,))
        with patch('peps.news.NewsAcquirer.acquire') as acquire:
            first = self.get('/api/news-tasks?status=deferred&source_id=fixture&limit=2&offset=0')
            second = self.get('/api/news-tasks?status=deferred&source_id=fixture&limit=2&offset=2')
            last = self.get('/api/news-tasks?status=deferred&source_id=fixture&limit=2&offset=4')
        acquire.assert_not_called()
        self.assertEqual((first['total'], len(first['items']), first['has_more']), (5, 2, True))
        self.assertEqual((len(last['items']), last['has_more']), (1, False))
        self.assertFalse({row['id'] for row in first['items']} & {row['id'] for row in second['items']})
        self.assertTrue(all(row['next_retry_seconds'] > 3500 for row in first['items']))
        for query in ('limit=0', 'limit=101', 'limit=x', 'offset=-1', 'offset=1000001', 'status=unknown'):
            self.assertEqual(self.request('/api/news-tasks?' + query)[0], 400)
        self.assertEqual(self.get('/api/news-tasks?source_id=fixture%27%20OR%201%3D1--')['total'], 0)

    def test_selective_retry_validates_identifiers_before_any_queue_mutation(self):
        job = self.engine.store.enqueue('news_query', {'source_id': 'fixture'}, key='retry-validation')
        with self.engine.store.connection() as con:
            con.execute("UPDATE jobs SET status='dead',attempts=5,error='Synthetic failure' WHERE id=?", (job,))
            before = tuple(con.execute('SELECT * FROM jobs WHERE id=?', (job,)).fetchone())
        with patch('peps.news.NewsAcquirer.acquire') as acquire:
            for value in (None, job, [], [True], ['short'], [job] * 101):
                with self.subTest(value=value):
                    status, body, _ = self.request('/api/news-retry', 'POST', {'job_ids': value})
                    self.assertEqual(status, 400, body)
        acquire.assert_not_called()
        with self.engine.store.connection() as con:
            self.assertEqual(tuple(con.execute('SELECT * FROM jobs WHERE id=?', (job,)).fetchone()), before)
        self.assertFalse(self.engine.online)

    def test_selective_retry_preserves_future_due_and_skips_running_or_unrelated_jobs(self):
        future = time.time() + 3600
        selected = []
        for index, (kind, status) in enumerate((('news_query', 'dead'), ('news_article', 'blocked'),
                    ('news_feed', 'running'), ('news_query', 'deferred'), ('collect', 'blocked'),
                    ('news_query', 'cancelled'))):
            job = self.engine.store.enqueue(kind, {'source_id': 'fixture'}, key=f'retry-selection-{index}', due=future)
            selected.append(job)
            with self.engine.store.connection() as con:
                con.execute('UPDATE jobs SET status=?,attempts=5,error=?,owner=?,lease_until=? WHERE id=?',
                            (status, 'Synthetic ' + status, 'lease-owner', future, job))
        with patch('peps.news.NewsAcquirer.acquire') as acquire:
            status, body, _ = self.request('/api/news-retry', 'POST', {'job_ids': selected})
            self.assertEqual(status, 409, body)  # A live external worker blocks every mutation.
            with self.engine.store.connection() as con:
                con.execute('UPDATE jobs SET lease_until=? WHERE id=?', (time.time() - 1, selected[2]))
                before = {row['id']: dict(row) for row in con.execute('SELECT * FROM jobs')}
            response = self.post('/api/news-retry', {'job_ids': selected})
        acquire.assert_not_called()
        report = response
        self.assertEqual(response['requeued'], 2)
        self.assertEqual(report['retried'], 2)
        self.assertEqual(set(report['job_ids']), set(selected[:2]))
        with self.engine.store.connection() as con:
            after = {row['id']: dict(row) for row in con.execute('SELECT * FROM jobs')}
            audit = json.loads(con.execute("SELECT payload FROM audit WHERE action='news_jobs_requeued'").fetchone()[0])
        for job in selected[:2]:
            self.assertEqual(after[job]['status'], 'queued')
            self.assertEqual(after[job]['attempts'], 0)
            self.assertEqual(after[job]['due'], future)
            self.assertIsNone(after[job]['lease_until'])
            self.assertIsNone(after[job]['owner'])
        for job in selected[2:]:
            self.assertEqual(after[job], before[job])
        self.assertEqual({item['status'] for item in audit['previous']}, {'dead', 'blocked'})
        self.assertTrue(audit['transport_backoff_preserved'])
        self.assertFalse(self.engine.online)
        self.assertTrue(self.engine.store.verify_audit()['ok'])

    def test_task_links_never_expose_credentials_or_private_network_destinations(self):
        for index, url in enumerate(('https://operator:synthetic-secret@publisher.invalid/private',
                'https://127.0.0.1/private', 'https://localhost/private', 'javascript:alert(1)',
                'file:///synthetic/local', 'https://publisher.invalid/public')):
            self.engine.store.enqueue('news_query', {'source_id': 'fixture', 'url': url}, key=f'url-validation-{index}')
        items = self.get('/api/news-tasks')['items']
        self.assertEqual([item['url'] for item in items if item['url']], ['https://publisher.invalid/public'])
        self.assertNotIn('synthetic-secret', json.dumps(items))

    def test_task_pages_only_include_the_literal_news_namespace(self):
        included = self.engine.store.enqueue('news_query', {'source_id': 'fixture'}, key='literal-news')
        self.engine.store.enqueue('newsflash', {'source_id': 'fixture'}, key='not-news-namespace')
        self.engine.store.enqueue('newsXarticle', {'source_id': 'fixture'}, key='not-news-underscore')
        result = self.get('/api/news-tasks')
        self.assertEqual(result['total'], 1)
        self.assertEqual([row['id'] for row in result['items']], [included])

    def test_unreadable_legacy_task_metadata_does_not_break_or_change_a_queue_page(self):
        invalid = ('[]', 'null', 'true', '42', '"legacy"', '{broken', '{"source_id": []}')
        with self.engine.store.connection() as con:
            for index, raw in enumerate(invalid):
                identifier = digest('legacy-task-' + str(index))
                con.execute('INSERT INTO jobs(id,kind,payload,status,due,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                            (identifier, 'news_article', raw, 'blocked', time.time(), 'fixture', 'fixture'))
            before = [tuple(row) for row in con.execute('SELECT * FROM jobs ORDER BY id')]
        audit_before = self.engine.store.stats()['audit']
        result = self.get('/api/news-tasks?status=blocked')
        self.assertEqual(result['total'], len(invalid))
        self.assertEqual(len(result['items']), len(invalid))
        self.assertTrue(all(row['url'] == '' and row['source_id'] is None and row['metadata_warning']
                            for row in result['items']))
        self.assertEqual(self.get('/api/news-tasks?source_id=fixture')['total'], 0)
        with self.engine.store.connection() as con:
            self.assertEqual([tuple(row) for row in con.execute('SELECT * FROM jobs ORDER BY id')], before)
        self.assertEqual(self.engine.store.stats()['audit'], audit_before)

    def test_new_routes_preserve_token_host_and_origin_protections(self):
        routes = (('/api/workflow-run', 'POST', {'online': True}),
                  ('/api/workflow-analyze-available', 'POST', {}),
                  ('/api/news-retry', 'POST', {'job_ids': ['synthetic']}),
                  ('/api/news-tasks', 'GET', None),
                  ('/api/public-search?id=synthetic', 'GET', None))
        with patch('peps.news.NewsAcquirer.acquire') as acquire:
            for path, method, data in routes:
                with self.subTest(path=path):
                    self.assertEqual(self.request(path, method, data, token=False)[0], 403)
                    self.assertEqual(self.request(path, method, data, headers={'Host': 'attacker.invalid'})[0], 403)
                    self.assertEqual(self.request(path, method, data, headers={'Origin': 'https://attacker.invalid'})[0], 403)
        acquire.assert_not_called()
        self.assertFalse(self.engine.online)

    def test_public_links_are_manual_public_urls_without_network_or_source_consent(self):
        before = self.news.config_path.read_bytes()
        audit_before = self.engine.store.stats()['audit']
        with patch('peps.news.NewsAcquirer.acquire') as acquire, patch.object(self.engine, 'rescore') as score:
            links = self.get('/api/public-search?id=synthetic')
        acquire.assert_not_called()
        score.assert_not_called()
        self.assertTrue(links['links'])
        self.assertTrue(links['note'])
        for link in links['links']:
            parsed = urlsplit(link['url'])
            self.assertEqual(parsed.scheme, 'https')
            self.assertTrue(parsed.hostname)
            self.assertIsNone(parsed.username)
            self.assertIsNone(parsed.password)
            self.assertIn(link['channel'], ('web', 'social'))
        self.assertFalse(self.engine.online)
        self.assertEqual(self.news.config_path.read_bytes(), before)
        self.assertEqual(self.engine.store.stats()['audit'], audit_before)
        self.assertEqual(self.request('/api/public-search?id=absent')[0], 404)


if __name__ == '__main__':
    unittest.main()
