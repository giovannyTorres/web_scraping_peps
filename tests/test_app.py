import http.client
import json
import tempfile
import threading
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

    def test_assets_and_session(self):
        code, body, headers = self.request('/', token=False)
        self.assertEqual(200, code)
        self.assertIn(self.token.encode(), body)
        self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
        self.assertEqual(403, self.request('/api/stats', token=False)[0])
        self.assertEqual(200, self.request('/api/stats')[0])

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
        entered, release = threading.Event(), threading.Event()
        def blocked_rescore():
            entered.set()
            release.wait(10)
            return 0
        with patch.object(self.engine, 'rescore', blocked_rescore):
            try:
                self.assertEqual(200, self.request('/api/score', 'POST', {})[0])
                self.assertTrue(entered.wait(2))
                for path in ('/api/collect', '/api/news-acquire'):
                    code, body, _ = self.request(path, 'POST', {'online': True})
                    self.assertEqual(400, code, body)
                    self.assertFalse(self.engine.online)
            finally:
                release.set()

    def test_explicit_collection_enables_network_only_for_accepted_operation(self):
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
        code, body, _ = self.request('/api/export')
        self.assertEqual(200, code)
        self.assertEqual('e', json.loads(body)['id'])
        self.assertEqual(403, self.request('/api/review', 'POST', {}, token=False)[0])

    def test_download_ticket_is_single_use(self):
        code, body, _ = self.request('/api/export-ticket', 'POST', {})
        self.assertEqual(200, code)
        url = json.loads(body)['url']
        self.assertEqual(200, self.request(url, token=False)[0])
        self.assertEqual(403, self.request(url, token=False)[0])

    def test_explanation_snapshot_evidence_and_replay_keep_session_protection(self):
        self.engine.store.import_entities([{'id': 'why-fixture', 'name': 'Entidad Sintética Aurora', 'kind': 'company'}])
        self.engine.import_documents([{'url': 'https://fixture.invalid/why', 'source_kind': 'official',
            'entity_ids': ['why-fixture'], 'text': '🧭 Entidad Sintética Aurora fue acusada de fraude.',
            'published_at': '2026-10-10'}])
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
