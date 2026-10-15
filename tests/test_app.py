import http.client
import json
import tempfile
import threading
import unittest

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
