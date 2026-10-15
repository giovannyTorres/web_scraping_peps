import http.client
import json
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from peps.app import create_server
from peps.engine import Engine


class LocalAppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = Engine(self.temp.name)
        self.server, self.token = create_server(self.engine)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, path, method='GET', data=None, token=True, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port)
        extra = {'X-PEPS-Token': self.token} if token else {}
        extra.update(headers or {})
        conn.request(method, path, body=json.dumps(data) if data is not None else None, headers=extra)
        response = conn.getresponse()
        body = response.read()
        code, result_headers = response.status, dict(response.getheaders())
        conn.close()
        return code, body, result_headers

    def wait_operation(self):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            code, body, _ = self.request('/api/stats')
            self.assertEqual(200, code, body)
            operation = json.loads(body)['operation']
            if not operation['busy']:
                self.assertEqual('completed', operation['status'], operation)
                return operation
            threading.Event().wait(.01)
        self.fail('La operación local no terminó dentro de 10 segundos')

    def analyze_local(self):
        code, body, _ = self.request('/api/workflow-mode', 'POST', {'mode': 'local'})
        self.assertEqual(200, code, body)
        code, body, _ = self.request('/api/workflow-confirm-sources', 'POST', {})
        self.assertEqual(200, code, body)
        code, body, _ = self.request('/api/workflow-analyze', 'POST', {})
        self.assertEqual(200, code, body)
        return self.wait_operation()

    def authorize_news_source(self):
        code, body, _ = self.request('/api/news')
        self.assertEqual(200, code, body)
        source = {'id': json.loads(body)['sources'][0]['id'], 'enabled': True, 'terms_approved': True,
                  'download_articles': False, 'article_terms_approved': False}
        code, body, _ = self.request('/api/news-source', 'POST', source)
        self.assertEqual(200, code, body)
        code, body, _ = self.request('/api/workflow-confirm-sources', 'POST', {})
        self.assertEqual(200, code, body)
        return source

    def test_assets_and_session(self):
        code, body, headers = self.request('/', token=False)
        self.assertEqual(200, code)
        self.assertIn(self.token.encode(), body)
        self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
        self.assertEqual(403, self.request('/api/stats', token=False)[0])
        self.assertEqual(200, self.request('/api/stats')[0])
        for name in ('workflow.js', 'app.js', 'import.js', 'style.css'):
            with self.subTest(asset=name):
                code, body, _ = self.request('/' + name, token=False)
                self.assertEqual(200, code)
                self.assertTrue(body)

    def test_workflow_endpoints_keep_session_host_and_origin_protection(self):
        for path, method, data in (
            ('/api/workflow', 'GET', None),
            ('/api/workflow-confirm-sources', 'POST', {}),
            ('/api/workflow-analyze', 'POST', {}),
            ('/api/news-acquire', 'POST', {'online': True}),
        ):
            with self.subTest(path=path):
                self.assertEqual(403, self.request(path, method, data, token=False)[0])
                self.assertEqual(403, self.request(path, method, data, headers={'Host': 'attacker.invalid'})[0])
                self.assertEqual(403, self.request(path, method, data,
                    headers={'Origin': 'https://attacker.invalid'})[0])
        self.assertFalse(self.engine.online)

    def test_dns_rebinding_and_cross_origin_rejected(self):
        self.assertEqual(403, self.request('/api/stats', headers={'Host': 'attacker.example'})[0])
        self.assertEqual(403, self.request('/api/score', 'POST', {}, headers={'Origin': 'https://attacker.example'})[0])

    def test_non_ascii_session_token_is_rejected_without_dropping_connection(self):
        code, body, _ = self.request('/api/stats', headers={'X-PEPS-Token': 'sesi\u00f3n-inv\u00e1lida'})
        self.assertEqual(403, code)
        self.assertEqual('Sesi\u00f3n inv\u00e1lida', json.loads(body)['error'])

    def test_collection_requires_json_boolean_consent_without_changing_mode(self):
        with patch.object(self.engine, 'queue_discovery') as queue:
            for path in ('/api/collect', '/api/news-acquire'):
                for value in ('false', 'true', 1, 0, [], {}, None, False):
                    with self.subTest(path=path, value=value):
                        code, body, _ = self.request(path, 'POST', {'online': value})
                        self.assertEqual(400, code, body)
                        self.assertFalse(self.engine.online)
            queue.assert_not_called()

    def test_rejected_busy_collection_cannot_enable_network_mode(self):
        self.engine.store.import_entities([{'id': 'busy', 'name': 'Entidad Sintética Ocupada'}])
        self.assertEqual(200, self.request('/api/workflow-mode', 'POST', {'mode': 'local'})[0])
        self.assertEqual(200, self.request('/api/workflow-confirm-sources', 'POST', {})[0])
        entered, release = threading.Event(), threading.Event()
        real_rescore = self.engine.rescore
        def blocked_rescore(*args, **kwargs):
            entered.set()
            release.wait(10)
            return real_rescore(*args, **kwargs)
        with patch.object(self.engine, 'rescore', blocked_rescore):
            try:
                self.assertEqual(200, self.request('/api/score', 'POST', {})[0])
                self.assertTrue(entered.wait(2))
                for path in ('/api/collect', '/api/news-acquire'):
                    code, body, _ = self.request(path, 'POST', {'online': True})
                    self.assertEqual(409, code, body)
                    self.assertFalse(self.engine.online)
            finally:
                release.set()
        self.wait_operation()

    def test_explicit_collection_enables_network_only_for_accepted_operation(self):
        self.engine.store.import_entities([{'id': 'consent', 'name': 'Entidad Sintética Consentida'}])
        self.authorize_news_source()
        collected, observed_modes = threading.Event(), []
        def collect(**_):
            observed_modes.append(self.engine.online)
            collected.set()
            return {'completed': 0}
        with patch.object(self.engine, 'queue_discovery', return_value=0), patch.object(self.engine, 'run', collect):
            self.assertEqual(200, self.request('/api/collect', 'POST', {'online': True})[0])
            self.assertTrue(collected.wait(2))
        self.assertEqual([True], observed_modes)
        self.assertTrue(self.engine.online)

    def test_source_permissions_and_forced_attribution_require_booleans(self):
        source_id = self.engine.sources()[0]['id']
        _, news_body, _ = self.request('/api/news')
        news_source_id = json.loads(news_body)['sources'][0]['id']
        original_sources = self.engine.config_path.read_bytes()
        for path, payload, fields in (
            ('/api/source', {'id': source_id, 'enabled': False, 'terms_approved': False},
             ('enabled', 'terms_approved')),
            ('/api/news-source', {'id': news_source_id, 'enabled': False, 'terms_approved': False,
                                  'download_articles': False, 'article_terms_approved': False},
             ('enabled', 'terms_approved', 'download_articles', 'article_terms_approved')),
            ('/api/news-attribute', {'force': False}, ('force',)),
        ):
            for field in fields:
                with self.subTest(path=path, field=field):
                    code, body, _ = self.request(path, 'POST', dict(payload, **{field: 'false'}))
                    self.assertEqual(400, code, body)
                    self.assertIn(field, json.loads(body)['error'])
        self.assertEqual(original_sources, self.engine.config_path.read_bytes())
        self.assertEqual(0, self.engine.store.stats()['documents'])
        self.assertTrue(self.engine.store.verify_audit()['ok'])

    def test_bad_input_and_path(self):
        self.assertEqual(404, self.request('/../store.py')[0])
        self.assertEqual(400, self.request('/api/review', 'POST', {})[0])
        self.assertEqual(404, self.request('/api/entity?id=unknown')[0])

    def test_export_and_review_auth(self):
        self.engine.store.import_entities([{'id': 'e', 'name': 'Nombre de prueba'}])
        self.analyze_local()
        code, body, _ = self.request('/api/export')
        self.assertEqual(200, code)
        exported = [json.loads(line) for line in body.splitlines()]
        self.assertEqual('e', next(row for row in exported if row['record_type'] == 'entity')['id'])
        self.assertEqual(403, self.request('/api/review', 'POST', {}, token=False)[0])

    def test_download_ticket_is_single_use(self):
        self.engine.store.import_entities([{'id': 'ticket', 'name': 'Entidad Sintética Descargable'}])
        self.analyze_local()
        code, body, _ = self.request('/api/export-ticket', 'POST', {})
        self.assertEqual(200, code)
        url = json.loads(body)['url']
        self.assertEqual(200, self.request(url, token=False)[0])
        self.assertEqual(403, self.request(url, token=False)[0])

    def test_existing_explanation_is_read_without_implicit_recalculation(self):
        self.engine.store.import_entities([{'id': 'snapshot', 'name': 'Entidad Sintética Histórica'}])
        self.analyze_local()
        original = self.engine.explanation('snapshot')
        with patch('peps.explainability.scoring_fingerprint', return_value={'changed': True}), \
                patch.object(self.engine, 'rescore', side_effect=AssertionError('HTTP must not calculate scores')) as scorer:
            code, body, _ = self.request('/api/explanation?id=snapshot')
            self.assertEqual(200, code, body)
            stored = json.loads(body)
            self.assertEqual(original['run']['id'], stored['run']['id'])
            self.assertEqual(original['result'], stored['result'])
            scorer.assert_not_called()
        self.assertEqual(1, len(self.engine.store.score_history('snapshot')))

    def test_download_locks_mutations_and_current_results_but_allows_explicit_history(self):
        self.engine.store.import_entities([
            {'id': 'download-fixture', 'name': 'Entidad Sintética Aurora', 'kind': 'company'},
            {'id': 'pending-fixture', 'name': 'Entidad Sintética Pendiente', 'kind': 'company'},
        ])
        self.engine.import_documents([{'url': 'https://fixture.invalid/download-lock', 'source_kind': 'official',
            'entity_ids': ['download-fixture'], 'text': 'Entidad Sintética Aurora fue acusada de fraude.',
            'published_at': '2026-10-10'}])
        self.engine.rescore('download-fixture')
        snapshot = self.engine.explanation('download-fixture')
        evidence_id = snapshot['result']['evidence_audit'][0]['evidence_id']
        run_id = snapshot['run']['id']
        source = self.authorize_news_source()
        import_payload = {'mode': 'single', 'entity': {'name': 'Entidad Sintética Bloqueada', 'kind': 'company'}}
        code, body, _ = self.request('/api/import-preview', 'POST', import_payload)
        self.assertEqual(200, code, body)
        import_payload['preview_hash'] = json.loads(body)['preview_hash']
        sources_before = self.engine.config_path.read_bytes()
        news_sources_before = (self.engine.data_dir / 'news_sources.json').read_bytes()
        entered, release = threading.Event(), threading.Event()

        def download(*_, **__):
            entered.set()
            release.wait(10)
            return {'completed': 0, 'downloaded': 0}

        with patch('peps.news.NewsAcquirer.acquire', side_effect=download):
            try:
                code, body, _ = self.request('/api/news-acquire', 'POST', {'online': True})
                self.assertEqual(200, code, body)
                active_id = json.loads(body)['operation_id']
                self.assertTrue(entered.wait(2), 'La descarga aceptada debe haber comenzado')
                with patch.object(self.engine.stop_event, 'set') as stop:
                    code, body, _ = self.request('/api/stop', 'POST', {'operation_id': 'stale-' + active_id})
                    self.assertEqual(409, code, body)
                    stop.assert_not_called()
                self.assertFalse(self.engine.stop_event.is_set())
                code, body, _ = self.request('/api/stats')
                self.assertEqual(200, code, body)
                operation = json.loads(body)['operation']
                self.assertTrue(operation['busy'])
                self.assertEqual(active_id, operation['operation_id'])
                self.assertEqual('running', operation['status'])
                with patch.object(self.engine, 'rescore') as scorer, \
                        patch.object(self.engine.store, 'review') as review, \
                        patch.object(self.engine, 'set_source') as legacy_source, \
                        patch('peps.news.NewsAcquirer.set_source') as news_source, \
                        patch('peps.importing.commit_import') as commit_import, \
                        patch.object(self.engine.store, 'import_entities') as import_entities, \
                        patch.object(self.engine, 'import_documents') as import_documents:
                    for entity_id in ('download-fixture', 'pending-fixture'):
                        code, body, _ = self.request('/api/explanation?id=' + entity_id)
                        self.assertEqual(409, code, body)
                    for path, payload in (
                        ('/api/score', {}),
                        ('/api/workflow-analyze', {}),
                        ('/api/workflow-mode', {'mode': 'local'}),
                        ('/api/export-ticket', {}),
                        ('/api/source', {'id': self.engine.sources()[0]['id'], 'enabled': False, 'terms_approved': False}),
                        ('/api/news-source', dict(source, enabled=False, terms_approved=False)),
                        ('/api/review', {'id': evidence_id, 'status': 'confirmed', 'actor': 'synthetic-reviewer',
                            'reason': 'Synthetic lock regression', 'expected_run_id': run_id}),
                        ('/api/import-confirm', import_payload),
                        ('/api/import-preview', import_payload),
                        ('/api/import-entities', {'text': 'id,name\nblocked,Entidad Sintética Bloqueada\n'}),
                        ('/api/import-documents', {'text': '{}'}),
                    ):
                        with self.subTest(path=path):
                            code, body, _ = self.request(path, 'POST', payload)
                            self.assertEqual(409, code, body)
                    code, body, _ = self.request('/api/export')
                    self.assertEqual(409, code, body)
                    for path in ('/api/collect', '/api/news-acquire'):
                        code, body, _ = self.request(path, 'POST', {'online': 'true'})
                        self.assertEqual(400, code, body)
                    code, body, _ = self.request('/api/explanation?id=download-fixture&run_id=' + run_id)
                    self.assertEqual(200, code, body)
                    self.assertEqual(run_id, json.loads(body)['run']['id'])
                    for mutation in (scorer, review, legacy_source, news_source,
                                     commit_import, import_entities, import_documents):
                        mutation.assert_not_called()
                self.assertEqual(sources_before, self.engine.config_path.read_bytes())
                self.assertEqual(news_sources_before, (self.engine.data_dir / 'news_sources.json').read_bytes())
                self.assertEqual(2, self.engine.store.stats()['entities'])
                self.assertEqual(1, len(self.engine.store.score_history('download-fixture')))
                self.assertEqual([], self.engine.store.score_history('pending-fixture'))
                self.assertTrue(self.engine.store.verify_audit()['ok'])
            finally:
                release.set()
        self.wait_operation()
        code, body, _ = self.request('/api/explanation?id=download-fixture')
        self.assertEqual(409, code, body)
        code, body, _ = self.request('/api/entities')
        self.assertEqual(200, code, body)
        stale = next(row for row in json.loads(body) if row['id'] == 'download-fixture')
        self.assertTrue(stale['score_available'])
        self.assertFalse(stale['score_current'])
        self.assertIsNone(stale['score'])

    def test_explanation_snapshot_evidence_and_replay_keep_session_protection(self):
        self.engine.store.import_entities([{'id': 'why-fixture', 'name': 'Entidad Sintética Aurora', 'kind': 'company'}])
        self.engine.import_documents([{'url': 'https://fixture.invalid/why', 'source_kind': 'official',
            'entity_ids': ['why-fixture'], 'text': '🧭 Entidad Sintética Aurora fue acusada de fraude.',
            'published_at': '2026-10-10'}])
        self.analyze_local()
        self.assertEqual(403, self.request('/api/explanation?id=why-fixture', token=False)[0])
        code, body, _ = self.request('/api/explanation?id=why-fixture')
        self.assertEqual(code, 200)
        value = json.loads(body)
        run_id = value['run']['id']
        self.assertEqual(value['result']['score'], value['explanation']['interpretation']['score'])
        evidence_id = value['result']['evidence_audit'][0]['evidence_id']
        path = '/api/run-evidence?run_id=' + run_id + '&id=' + evidence_id
        self.assertEqual(self.request(path, token=False)[0], 403)
        code, body, _ = self.request(path)
        self.assertEqual(code, 200)
        note = json.loads(body)
        citation = value['explanation']['factors'][0]['evidence'][0]
        self.assertEqual(note['text'][citation['start']:citation['end']], citation['quote'])
        self.assertEqual(self.request('/api/run-evidence?run_id='+run_id+'&id=other')[0], 404)
        self.assertEqual(self.request('/api/replay', 'POST', {'run_id': run_id}, token=False)[0], 403)
        code, body, _ = self.request('/api/replay', 'POST', {'run_id': run_id})
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body)['matched'])
        self.assertFalse(json.loads(body)['auxiliary_replayed'])
        self.assertEqual(self.request('/api/explanation?id=missing')[0], 404)


if __name__ == '__main__':
    unittest.main()
