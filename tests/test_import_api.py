"""End-to-end checks for local, reviewed name/CSV import (synthetic fixtures)."""
import http.client
import json
import tempfile
import threading
import time
import unittest

from peps.app import create_server
from peps.engine import Engine


class ReviewedImportAPITests(unittest.TestCase):
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

    def request(self, path, data=None, token=True, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port)
        request_headers = {'Content-Type': 'application/json'}
        if token:
            request_headers['X-PEPS-Token'] = self.token
        request_headers.update(headers or {})
        conn.request('POST' if data is not None else 'GET', path,
                     body=json.dumps(data, ensure_ascii=False).encode('utf-8') if data is not None else None,
                     headers=request_headers)
        response = conn.getresponse()
        raw = response.read()
        code, result_headers = response.status, dict(response.getheaders())
        conn.close()
        return code, json.loads(raw) if 'application/json' in result_headers.get('Content-Type', '') else raw, result_headers

    def preview(self, **overrides):
        payload = {
            'text': 'Nombre;Clase;Municipio\nEmpresa Sintética Aurora;Sociedad;Puebla\nPersona Sintética Luna;Individuo;Toluca\n',
            'delimiter': ';', 'has_header': True,
            'mapping': ['name', 'kind', 'city'],
            'kind_values': {'Sociedad': 'company', 'Individuo': 'person'},
            'existing_policy': 'skip', 'skip_invalid': False,
        }
        payload.update(overrides)
        code, value, _ = self.request('/api/import-preview', payload)
        self.assertEqual(200, code, value)
        return payload, value

    def confirm(self, payload, preview):
        data = dict(payload, preview_hash=preview['preview_hash'])
        code, value, _ = self.request('/api/import-confirm', data)
        self.assertEqual(202, code, value)
        return value['operation_id']

    def wait_result(self, operation_id):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            code, value, _ = self.request('/api/import-status?id=' + operation_id)
            self.assertEqual(200, code, value)
            if value['status'] not in ('queued', 'running'):
                return value
            threading.Event().wait(.01)
        self.fail('La importación local no terminó dentro de 10 segundos')

    def assert_rejected_commit(self, payload, error_code=None):
        code, value, _ = self.request('/api/import-confirm', payload)
        if code == 202:
            value = self.wait_result(value['operation_id'])
            self.assertEqual('error', value['status'], value)
            self.assertTrue(value['retry_preview'], value)
            if error_code:
                self.assertEqual(error_code, value['error_code'], value)
        else:
            self.assertIn(code, (400, 409), value)
        return value

    def test_import_endpoints_and_asset_keep_session_host_origin_protection(self):
        for path, data in (
            ('/api/import-preview', {'text': 'Nombre\nEntidad Sintética'}),
            ('/api/import-confirm', {'preview_hash': 'unreviewed'}),
            ('/api/import-status?id=unknown', None),
        ):
            self.assertEqual(403, self.request(path, data, token=False)[0], path)
            self.assertEqual(403, self.request(path, data, headers={'Host': 'attacker.invalid'})[0], path)
            self.assertEqual(403, self.request(path, data, headers={'Origin': 'https://attacker.invalid'})[0], path)
        code, body, headers = self.request('/import.js', token=False)
        self.assertEqual(200, code)
        self.assertTrue(body)
        self.assertIn("script-src 'self'", headers['Content-Security-Policy'])

    def test_non_object_request_and_unknown_operation_are_clear_client_errors(self):
        for path in ('/api/import-preview', '/api/import-confirm'):
            self.assertEqual(400, self.request(path, ['not', 'an', 'object'])[0])
        self.assertEqual(404, self.request('/api/import-status?id=missing')[0])

    def test_csv_requires_reviewed_mapping_then_imports_and_opens_real_explanation(self):
        _, initial = self.preview(mapping=None)
        self.assertFalse(initial['ready'])
        self.assertEqual(3, len(initial['columns']))
        self.assertEqual('name', initial['columns'][0]['suggested_field'])
        self.assertEqual(0, self.engine.store.stats()['entities'])
        payload, preview = self.preview()
        self.assertTrue(preview['ready'], preview)
        self.assertEqual(0, self.engine.store.stats()['entities'])
        operation_id = self.confirm(payload, preview)
        result = self.wait_result(operation_id)
        self.assertEqual('completed', result['status'], result)
        self.assertEqual(2, self.engine.store.stats()['entities'])
        saved = {e['name']: e for e in self.engine.store.iter_entities()}
        self.assertEqual('company', saved['Empresa Sintética Aurora']['kind'])
        self.assertEqual('Puebla', saved['Empresa Sintética Aurora']['context']['city'])
        entity_id = saved['Persona Sintética Luna']['id']
        code, explanation, _ = self.request('/api/explanation?id=' + entity_id)
        self.assertEqual(200, code, explanation)
        self.assertEqual(0, explanation['result']['score'])
        self.assertEqual([], explanation['explanation']['factors'])
        self.assertTrue(self.engine.store.verify_audit()['ok'])

    def test_single_name_requires_explicit_kind_and_gets_auditable_score(self):
        code, value, _ = self.request('/api/import-preview', {'mode': 'single', 'entity': {'name': 'Persona Sintética Cielo'}})
        self.assertIn(code, (200, 400), value)
        if code == 200:
            self.assertFalse(value['ready'])
        payload = {'mode': 'single', 'entity': {'name': 'Persona Sintética Cielo', 'kind': 'person', 'aliases': ['Cielo de prueba'],
                                                  'city': 'Toluca', 'role': 'Docente'}}
        code, preview, _ = self.request('/api/import-preview', payload)
        self.assertEqual(200, code, preview)
        self.assertTrue(preview['ready'], preview)
        result = self.wait_result(self.confirm(payload, preview))
        self.assertEqual('completed', result['status'], result)
        entity = next(self.engine.store.iter_entities())
        self.assertEqual('Persona Sintética Cielo', entity['name'])
        self.assertEqual(['Cielo de prueba'], entity['aliases'])
        self.assertEqual('Toluca', entity['context']['city'])
        self.assertEqual('Docente', entity['context']['role'])
        self.assertEqual(200, self.request('/api/explanation?id=' + entity['id'])[0])

    def test_scoring_error_reports_saved_entities_and_allows_local_retry(self):
        payload, preview = self.preview()
        real_rescore = self.engine.rescore
        def unavailable_scoring(*_):
            raise ValueError('Synthetic scorer failure')
        self.engine.rescore = unavailable_scoring
        try:
            receipt = self.wait_result(self.confirm(payload, preview))
        finally:
            self.engine.rescore = real_rescore
        self.assertEqual('completed', receipt['status'], receipt)
        self.assertEqual('pending', receipt['result']['scoring_status'])
        self.assertEqual(2, self.engine.store.stats()['entities'])
        self.assertIn('guardaron', receipt['result']['scoring_error'])
        for entity in self.engine.store.iter_entities():
            code, explanation, _ = self.request('/api/explanation?id=' + entity['id'])
            self.assertEqual(200, code, explanation)
            self.assertEqual(0, explanation['result']['score'])

    def test_confirmation_without_preview_or_with_modified_content_is_rejected(self):
        payload, preview = self.preview()
        self.assert_rejected_commit(payload)
        modified = dict(payload, text=payload['text'].replace('Puebla', 'Monterrey'), preview_hash=preview['preview_hash'])
        self.assert_rejected_commit(modified, 'preview_stale')
        self.assertEqual(0, self.engine.store.stats()['entities'])

    def test_stale_preview_does_not_overwrite_newer_bank(self):
        payload, preview = self.preview()
        self.engine.store.import_entities([{'id': 'newer', 'name': 'Entidad Sintética Posterior', 'kind': 'company'}])
        self.assert_rejected_commit(dict(payload, preview_hash=preview['preview_hash']), 'preview_stale')
        self.assertEqual(1, self.engine.store.stats()['entities'])

    def test_repeated_confirm_does_not_duplicate_or_rescore(self):
        payload, preview = self.preview()
        operation_id = self.confirm(payload, preview)
        first = self.wait_result(operation_id)
        self.assertEqual('completed', first['status'], first)
        before = {e['id']: self.engine.explanation(e['id'])['run']['id'] for e in self.engine.store.iter_entities()}
        code, second, _ = self.request('/api/import-confirm', dict(payload, preview_hash=preview['preview_hash']))
        self.assertIn(code, (200, 202, 409), second)
        if code in (200, 202):
            self.assertEqual(operation_id, second['operation_id'])
            self.wait_result(second['operation_id'])
        self.assertEqual(2, self.engine.store.stats()['entities'])
        after = {e['id']: self.engine.explanation(e['id'])['run']['id'] for e in self.engine.store.iter_entities()}
        self.assertEqual(before, after)

    def test_irregular_duplicate_blank_columns_quoted_lines_and_user_type_values(self):
        payload, preview = self.preview(
            text='Reporte sintético\nNombre;Nombre;;Clase;Dato extra\n"Empresa Sintética\nBrisa";Brisa Comercial;Toluca;S.A.;no almacenar\n',
            skip_rows=1, mapping=['name', 'aliases', 'city', 'kind', 'ignore'],
            kind_values={'S.A.': 'company'})
        self.assertTrue(preview['ready'], preview)
        self.assertEqual(5, len(preview['columns']))
        self.assertNotEqual(preview['columns'][0]['index'], preview['columns'][1]['index'])
        result = self.wait_result(self.confirm(payload, preview))
        self.assertEqual('completed', result['status'], result)
        entity = next(self.engine.store.iter_entities())
        self.assertEqual('Empresa Sintética\nBrisa', entity['name'])
        self.assertEqual(['Brisa Comercial'], entity['aliases'])
        self.assertEqual('Toluca', entity['context']['city'])
        self.assertNotIn('no almacenar', json.dumps(entity, ensure_ascii=False))

    def test_invalid_rows_are_atomic_unless_explicitly_skipped(self):
        payload, preview = self.preview(text='Nombre;Clase;Municipio\nEmpresa Sintética Clara;Sociedad;Puebla\n;Sociedad;Toluca\n')
        self.assertFalse(preview['ready'])
        self.assertTrue(preview['errors'])
        self.assert_rejected_commit(dict(payload, preview_hash=preview['preview_hash']), 'invalid_import')
        self.assertEqual(0, self.engine.store.stats()['entities'])
        payload, preview = self.preview(text=payload['text'], skip_invalid=True)
        self.assertTrue(preview['ready'], preview)
        result = self.wait_result(self.confirm(payload, preview))
        self.assertEqual('completed', result['status'], result)
        self.assertEqual(1, self.engine.store.stats()['entities'])
        self.assertEqual('Empresa Sintética Clara', next(self.engine.store.iter_entities())['name'])

    def test_invalid_csv_quoting_never_creates_entities(self):
        code, value, _ = self.request('/api/import-preview', {
            'text': 'Nombre;Clase\n"Empresa Sintética Rota;company', 'delimiter': ';',
            'has_header': True, 'mapping': ['name', 'kind'], 'default_kind': 'company'})
        self.assertIn(code, (200, 400), value)
        if code == 200:
            self.assertFalse(value['ready'])
        self.assertEqual(0, self.engine.store.stats()['entities'])

    def test_existing_id_preserved_by_default_and_only_updates_after_new_preview(self):
        self.engine.store.import_entities([{'id': 'known', 'name': 'Empresa Sintética Original', 'kind': 'company', 'city': 'Puebla'}])
        payload, preview = self.preview(text='ID;Nombre;Clase;Ciudad\nknown;Empresa Sintética Corregida;company;Toluca\n',
                                        mapping=['id', 'name', 'kind', 'city'], kind_values={})
        result = self.wait_result(self.confirm(payload, preview))
        self.assertEqual('completed', result['status'], result)
        self.assertEqual('Empresa Sintética Original', self.engine.store.get_entity('known')['name'])
        payload, preview = self.preview(text=payload['text'], mapping=payload['mapping'], kind_values={}, existing_policy='update')
        self.assertTrue(preview['ready'], preview)
        result = self.wait_result(self.confirm(payload, preview))
        self.assertEqual('completed', result['status'], result)
        self.assertEqual('Empresa Sintética Corregida', self.engine.store.get_entity('known')['name'])
        self.assertEqual('Toluca', self.engine.store.get_entity('known')['context']['city'])


if __name__ == '__main__':
    unittest.main()
