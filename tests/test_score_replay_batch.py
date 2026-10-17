"""Snapshot/integrity regressions; fixtures are not performance benchmarks."""
import json
import socket
import tempfile
import unittest
from unittest.mock import patch

from peps.engine import Engine
from peps.store import canonical, digest


class ScoreReplayBatchTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.engine = Engine(directory.name)
        self.store = self.engine.store
        self.store.import_entities([
            {'id': 'first', 'name': 'Empresa Sintética Aurora', 'kind': 'company'},
            {'id': 'second', 'name': 'Empresa Sintética Brisa', 'kind': 'company'}])
        self.engine.import_documents([{'url': 'https://fixture.invalid/shared', 'source_kind': 'official',
            'entity_ids': ['first', 'second'], 'published_at': '2026-10-10',
            'text': 'Empresa Sintética Aurora y Empresa Sintética Brisa fueron acusadas de fraude.'}])
        self.engine.rescore()
        with self.store.connection() as con:
            self.ids = [row[0] for row in con.execute('SELECT run_id FROM scores ORDER BY entity_id')]

    def test_exact_individual_results_one_chain_check_and_one_original_hash(self):
        expected_replays = [self.engine.replay_score(run_id) for run_id in self.ids]
        expected_explanations = []
        for run_id in self.ids:
            run = self.store.get_score_run(run_id)
            expected_explanations.append(self.engine.explanation(run['entity_id'], run_id))
        with patch.object(self.store, '_assert_audit_chain', wraps=self.store._assert_audit_chain) as chain, \
                patch.object(self.store, '_document_record_hash', wraps=self.store._document_record_hash) as documents, \
                patch.object(self.engine, 'rescore', side_effect=AssertionError('Historical reads cannot rescore')), \
                patch('peps.contextual.analyze_evidence', side_effect=AssertionError('No auxiliary inference')), \
                patch.object(socket.socket, 'connect', side_effect=AssertionError('No network')):
            result = self.engine.replay_score_batch(self.ids)
        self.assertEqual(result, {'replays': expected_replays, 'explanations': expected_explanations})
        self.assertEqual(chain.call_count, 1)
        self.assertEqual(documents.call_count, 1)

    def test_concurrent_new_score_is_outside_batch_snapshot_and_next_read_sees_it(self):
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
            before = self.engine.replay_score_batch(self.ids)
        first = before['explanations'][0]
        self.assertEqual(len(first['history']), 1)
        self.assertEqual(first['history'][0]['id'], self.ids[0])
        self.assertTrue(first['history'][0]['current'])
        after = self.engine.replay_score_batch(self.ids)['explanations'][0]
        self.assertEqual(after['result'], first['result'])
        self.assertEqual(len(after['history']), 2)
        self.assertEqual([entry['id'] for entry in after['history'] if entry['current']], written)

    def test_chain_integrity_is_not_cached_between_batches(self):
        self.engine.replay_score_batch(self.ids)
        with self.store.connection() as con:
            con.execute("UPDATE audit SET payload='{}' WHERE seq=(SELECT max(seq) FROM audit)")
        with self.assertRaisesRegex(ValueError, 'Cadena de auditoría inválida'):
            self.engine.replay_score_batch(self.ids)

    def test_document_integrity_is_not_cached_between_batches(self):
        self.engine.replay_score_batch(self.ids)
        with self.store.connection() as con:
            con.execute("UPDATE documents SET text='Altered source body'")
        with self.assertRaisesRegex(ValueError, 'cuerpo'):
            self.engine.replay_score_batch(self.ids)

    def test_every_run_announcement_is_checked_even_with_a_rehashed_valid_chain(self):
        with self.store.connection() as con:
            previous = '0' * 64
            for row in con.execute('SELECT * FROM audit ORDER BY seq').fetchall():
                body = row['payload']
                if row['action'] == 'score_run_saved':
                    event = json.loads(body)
                    if event['run_hash'] == self.ids[1]:
                        event['result_hash'] = '0' * 64
                        body = canonical(event)
                event_hash = digest(canonical([row['at'], row['actor'], row['action'], body, previous]))
                con.execute('UPDATE audit SET payload=?,previous_hash=?,event_hash=? WHERE seq=?',
                            (body, previous, event_hash, row['seq']))
                previous = event_hash
        with self.assertRaisesRegex(ValueError, 'evento de auditoría'):
            self.engine.replay_score_batch(self.ids)

    def test_current_pointer_result_cannot_be_replaced_without_announcement(self):
        with self.store.connection() as con:
            con.execute("UPDATE scores SET result='{}' WHERE entity_id='first'")
        with self.assertRaisesRegex(ValueError, 'Puntero de evaluación actual inválido'):
            self.engine.replay_score_batch(self.ids)

    def test_run_payload_hash_is_checked(self):
        with self.store.connection() as con:
            con.execute('DROP TRIGGER score_runs_no_update')
            value = json.loads(con.execute('SELECT payload FROM score_runs WHERE id=?', (self.ids[1],)).fetchone()[0])
            value['result']['score'] = 99
            con.execute('UPDATE score_runs SET payload=? WHERE id=?', (canonical(value), self.ids[1]))
        with self.assertRaisesRegex(ValueError, 'Hash de evaluación histórica inválido'):
            self.engine.replay_score_batch(self.ids)

    def test_unsupported_authority_remains_unsupported_with_archived_explanations(self):
        with patch('peps.explainability.scoring_fingerprint', return_value={'different': True}):
            result = self.engine.replay_score_batch(self.ids)
            self.assertEqual(result['replays'], [self.engine.replay_score(run_id) for run_id in self.ids])
        self.assertTrue(all(row['status'] == 'unsupported' for row in result['replays']))
        self.assertTrue(all(row['run']['historical'] for row in result['explanations']))

    def test_input_bounds_missing_runs_empty_batch_and_history_limit(self):
        self.assertEqual(self.engine.replay_score_batch([]), {'replays': [], 'explanations': []})
        self.assertTrue(all(not row['history'] for row in self.engine.replay_score_batch(self.ids, history_limit=0)['explanations']))
        for invalid in ('not-a-list', [self.ids[0], self.ids[0]], [None], ['x'] * 1001):
            with self.subTest(invalid=str(invalid)[:40]), self.assertRaises(ValueError):
                self.engine.replay_score_batch(invalid)
        for invalid in (-1, 201, True, '50'):
            with self.subTest(history=invalid), self.assertRaises(ValueError):
                self.engine.replay_score_batch(self.ids, history_limit=invalid)
        with self.assertRaisesRegex(ValueError, 'Evaluación inexistente'):
            self.engine.replay_score_batch([self.ids[0], '0' * 64])
