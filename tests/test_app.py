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


if __name__ == '__main__':
    unittest.main()
