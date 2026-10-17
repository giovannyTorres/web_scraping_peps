"""Offline loopback checks for bounded, integrity-checked explanation reads."""
import http.client
import json
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from peps.app import create_server
from peps.engine import Engine
from peps.news import NewsAcquirer
from peps.store import canonical, digest
from peps.workflow import GuidedWorkflow


class ExplanationBatchHttpTests(unittest.TestCase):
    def fixture(self, count=2):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.engine = Engine(directory.name)
        self.store = self.engine.store
        names = ['Empresa Sintética Aurora', 'Empresa Sintética Brisa']
        names += [f'Empresa Sintética Horizonte {index:02d}' for index in range(2, count)]
        self.entities = [{'id': f'fixture-{index:02d}', 'name': name, 'kind': 'company'}
                         for index, name in enumerate(names[:count])]
        self.store.import_entities(self.entities)
        self.engine.import_documents([{
            'url': 'https://fixture.invalid/shared-batch-body',
            'title': 'Documento enteramente sintético', 'source_kind': 'official',
            'entity_ids': [entity['id'] for entity in self.entities],
            'published_at': '2026-10-10',
            'text': '📌 ' + ' '.join(entity['name'] + ' fue acusada de fraude.'
                                    for entity in self.entities),
        }])
        self.engine.rescore()
        with self.store.connection() as con:
            self.run_ids = [row[0] for row in con.execute('SELECT run_id FROM scores ORDER BY entity_id')]
        self.server, self.token = create_server(self.engine)
        # Capture this server: integrity subtests may create another fixture.
        server = self.server
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
        thread.start()

        def close():
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.addCleanup(close)

    def request(self, path, method='GET', data=None, *, token=True, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        supplied = {'X-PEPS-Token': self.token} if token else {}
        supplied.update(headers or {})
        try:
            connection.request(method, path, json.dumps(data) if data is not None else None, supplied)
            response = connection.getresponse()
            return response.status, json.loads(response.read()), dict(response.getheaders())
        finally:
            connection.close()

    def batch(self, run_ids=None):
        return self.request('/api/explanations', 'POST',
                            {'run_ids': self.run_ids if run_ids is None else run_ids})

    def individual(self, index=0, *, historical=True):
        query = {'id': self.entities[index]['id']}
        if historical:
            query['run_id'] = self.run_ids[index]
        return self.request('/api/explanation?' + urlencode(query))

    def counts(self):
        with self.store.connection() as con:
            return {table: con.execute('SELECT count(*) FROM ' + table).fetchone()[0]
                    for table in ('audit', 'scores', 'score_runs', 'documents')}

    def rewrite_run(self, run_id, mutate):
        """Simulate a coordinated rewrite so the semantic invariant is tested.

        The altered payload, pointer and complete audit chain are rehashed.
        A rejection must therefore come from the manifest/entity check rather
        than just an obsolete outer SHA or broken chain.
        """
        with self.store.connection() as con:
            con.execute('BEGIN IMMEDIATE')
            con.execute('PRAGMA defer_foreign_keys=ON')
            payload = json.loads(con.execute('SELECT payload FROM score_runs WHERE id=?',
                                             (run_id,)).fetchone()[0])
            mutate(payload)
            encoded = canonical(payload)
            replacement = digest(encoded)
            con.execute('DROP TRIGGER score_runs_no_update')
            con.execute('UPDATE score_runs SET id=?,payload=? WHERE id=?', (replacement, encoded, run_id))
            con.execute('UPDATE scores SET run_id=? WHERE run_id=?', (replacement, run_id))
            previous = '0' * 64
            for row in con.execute('SELECT * FROM audit ORDER BY seq').fetchall():
                event = json.loads(row['payload'])
                for key in ('run_hash', 'run_id'):
                    if event.get(key) == run_id:
                        event[key] = replacement
                body = canonical(event)
                event_hash = digest(canonical([row['at'], row['actor'], row['action'], body, previous]))
                con.execute('UPDATE audit SET payload=?,previous_hash=?,event_hash=? WHERE seq=?',
                            (body, previous, event_hash, row['seq']))
                previous = event_hash
        return replacement

    def test_twenty_items_match_get_exactly_with_one_integrity_snapshot_and_no_writes(self):
        self.fixture(20)
        expected = [self.individual(index)[1] for index in reversed(range(20))]
        before = self.counts()
        original_connect = socket.socket.connect

        def loopback_only(sock, address):
            if address[0] != '127.0.0.1':
                raise AssertionError('Explanation reads cannot contact external hosts')
            return original_connect(sock, address)

        with patch.object(self.store, 'score_run_batch', wraps=self.store.score_run_batch) as batch, \
                patch.object(self.store, '_assert_audit_chain', wraps=self.store._assert_audit_chain) as chain, \
                patch.object(self.store, '_document_record_hash', wraps=self.store._document_record_hash) as documents, \
                patch.object(self.engine, 'rescore', side_effect=AssertionError('No scoring writes')), \
                patch.object(self.engine, 'explanation', side_effect=AssertionError('No implicit rescore path')), \
                patch.object(self.engine, 'replay_score_batch', side_effect=AssertionError('No scorer replay')), \
                patch('peps.scoring.score_entity', side_effect=AssertionError('No score recomputation')), \
                patch('peps.contextual.analyze_evidence', side_effect=AssertionError('No contextual inference')), \
                patch.object(socket.socket, 'connect', new=loopback_only):
            status, result, headers = self.batch(list(reversed(self.run_ids)))
        self.assertEqual(status, 200, result)
        self.assertEqual(result, {'items': expected})
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(batch.call_count, 1)
        self.assertEqual(chain.call_count, 1)
        self.assertEqual(documents.call_count, 1, 'One shared original is hashed once per snapshot')
        self.assertEqual(self.counts(), before)
        self.assertTrue(all(item['run']['historical'] for item in result['items']))
        self.assertTrue(all(item['document_review']['total_leads'] for item in result['items']))
        self.assertEqual(set(result['items'][0]), {
            'entity', 'result', 'document_review', 'explanation', 'run', 'history', 'evidence_count', 'assessment'})

    def test_fifty_allowed_and_invalid_batches_rejected_before_store(self):
        self.fixture(50)
        status, result, _ = self.batch()
        self.assertEqual(status, 200, result)
        self.assertEqual([item['run']['id'] for item in result['items']], self.run_ids)
        invalid = [{}, {'run_ids': None}, {'run_ids': ''}, {'run_ids': {}}, {'run_ids': []},
                   {'run_ids': [self.run_ids[0]] * 2}, {'run_ids': self.run_ids + ['missing']},
                   {'run_ids': [None]}, {'run_ids': [True]}, {'run_ids': [1]}, {'run_ids': ['']}]
        with patch.object(self.store, 'score_run_batch', side_effect=AssertionError('Invalid batch reached Store')):
            for payload in invalid:
                with self.subTest(payload=str(payload)[:80]):
                    status, result, _ = self.request('/api/explanations', 'POST', payload)
                    self.assertEqual(status, 400, result)
                    self.assertNotIn('items', result)

    def test_post_session_host_and_origin_are_required_before_reading_store(self):
        self.fixture()
        rejected = [{'token': False}, {'headers': {'X-PEPS-Token': 'wrong'}},
                    {'headers': {'Host': 'untrusted.invalid'}},
                    {'headers': {'Origin': 'https://untrusted.invalid'}}]
        with patch.object(self.store, 'score_run_batch', side_effect=AssertionError('Unauthorized read')):
            for options in rejected:
                with self.subTest(options=options):
                    status, result, _ = self.request('/api/explanations', 'POST',
                                                     {'run_ids': self.run_ids}, **options)
                    self.assertEqual(status, 403, result)
        status, _, _ = self.request('/api/explanations', 'POST', {'run_ids': self.run_ids},
                                   headers={'Origin': f'http://127.0.0.1:{self.server.server_port}'})
        self.assertEqual(status, 200)

    def test_historical_access_preserves_current_workflow_permission_and_get_contract(self):
        self.fixture()
        self.assertEqual(self.individual(historical=False)[0], 409)
        self.assertEqual(self.batch()[0], 200)
        workflow = GuidedWorkflow(self.store, NewsAcquirer(self.engine).sources)
        workflow.set_mode('local')
        workflow.confirm_sources()
        workflow.mark_analysis()
        status, current, _ = self.individual(historical=False)
        self.assertEqual(status, 200, current)
        self.assertFalse(current['run']['historical'])
        historical = self.individual()[1]
        self.assertEqual(dict(current, run=historical['run']), historical)
        operation = self.request('/api/stats')[1]['operation']
        operation.update(busy=True, status='running', kind='analysis', phase='scoring')
        with patch('peps.app.OperationTracker.snapshot', return_value=operation):
            self.assertEqual(self.individual(historical=False)[0], 409)
            self.assertEqual(self.batch()[1]['items'][0], historical)
        evidence_id = self.store.evidence(self.entities[0]['id'])[0]['id']
        self.store.review(evidence_id, 'rejected', 'Synthetic reviewer', 'Synthetic stale-workflow check')
        self.assertEqual(self.individual(historical=False)[0], 409)
        self.assertEqual(self.batch()[1]['items'][0], self.individual()[1])

    def test_audit_and_original_body_are_rechecked_on_the_next_request(self):
        for corruption, message in (('audit', 'Cadena de auditoría inválida'), ('document', 'cuerpo')):
            with self.subTest(corruption=corruption):
                self.fixture()
                with patch.object(self.store, '_assert_audit_chain', wraps=self.store._assert_audit_chain) as chain:
                    self.assertEqual(self.batch()[0], 200)
                    with self.store.connection() as con:
                        if corruption == 'audit':
                            con.execute("UPDATE audit SET payload='{}' WHERE seq=(SELECT max(seq) FROM audit)")
                        else:
                            con.execute("UPDATE documents SET text='Altered original body'")
                    status, result, _ = self.batch()
                self.assertEqual(status, 400, result)
                self.assertIn(message, result['error'])
                self.assertNotIn('items', result)
                self.assertEqual(chain.call_count, 2)

    def test_rehashed_manifest_and_entity_mismatch_fail_closed_for_entire_batch(self):
        mutations = [('manifest', lambda payload: payload['evidence_manifest'][0].update(
                         document_record_hash='0' * 64), 'Documento histórico'),
                     ('entity', lambda payload: payload['entity_snapshot'].update(
                         id='another-entity'), 'Formato de evaluación histórica inválido')]
        for kind, mutate, message in mutations:
            with self.subTest(kind=kind):
                self.fixture()
                self.run_ids[1] = self.rewrite_run(self.run_ids[1], mutate)
                with self.store.connection() as con:
                    con.execute('BEGIN')
                    self.store._assert_audit_chain(con)
                status, result, _ = self.batch()
                self.assertEqual(status, 400, result)
                self.assertIn(message, result['error'])
                self.assertNotIn('items', result, 'A valid first item must not leak a partial response')

    def test_concurrent_score_stays_outside_snapshot_and_next_batch_observes_it(self):
        self.fixture()
        original = self.store._load_score_run
        written = []

        def load_then_write(*args, **kwargs):
            run = original(*args, **kwargs)
            if not written:
                written.append(None)
                result = dict(run['result'])
                result['provenance'] = dict(result['provenance'], concurrent_revision='later')
                written[0] = self.store.save_score(run['entity_id'], result,
                    entity_snapshot=run['entity_snapshot'], evidence_snapshot=self.store.evidence(run['entity_id']))
            return run

        with patch.object(self.store, '_load_score_run', side_effect=load_then_write):
            status, before, _ = self.batch()
        self.assertEqual(status, 200, before)
        first = before['items'][0]
        self.assertEqual(len(first['history']), 1)
        self.assertEqual(first['history'][0]['id'], self.run_ids[0])
        self.assertTrue(first['history'][0]['current'])
        status, after, _ = self.batch()
        self.assertEqual(status, 200, after)
        self.assertEqual(after['items'][0]['result'], first['result'])
        self.assertEqual(len(after['items'][0]['history']), 2)
        self.assertEqual([entry['id'] for entry in after['items'][0]['history'] if entry['current']], written)

    def test_missing_run_or_entity_rejects_the_entire_batch(self):
        self.fixture()
        status, result, _ = self.batch([self.run_ids[0], '0' * 64])
        self.assertEqual(status, 400, result)
        self.assertNotIn('items', result)
        entity_id = self.entities[1]['id']
        with self.store.connection() as con:
            con.execute('DELETE FROM evidence WHERE entity_id=?', (entity_id,))
            con.execute('DELETE FROM scores WHERE entity_id=?', (entity_id,))
            con.execute('DELETE FROM entities WHERE id=?', (entity_id,))
        status, result, _ = self.batch()
        self.assertEqual(status, 404, result)
        self.assertEqual(result, {'error': 'Entidad inexistente'})


if __name__ == '__main__':
    unittest.main()
