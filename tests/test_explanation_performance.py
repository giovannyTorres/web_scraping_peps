"""Generous local regression bounds, never a latency SLA or external benchmark."""
import tempfile
import time
import unittest
from unittest.mock import patch

from peps.engine import Engine
from peps.scoring import score_entity
from scripts.benchmark_explanation_pipeline import (
    assert_faithful_counterfactuals, fixture_documents, fixture_entities, network_guard,
)


class ExplanationPerformanceTests(unittest.TestCase):
    def test_twenty_entity_batch_is_bounded_offline_and_faithful(self):
        with tempfile.TemporaryDirectory() as directory, network_guard() as guard, \
             patch('peps.contextual._advanced_module', side_effect=AssertionError('optional NLP must not load')):
            engine = Engine(directory)
            entities = fixture_entities(20, heavy_notes=0)
            engine.store.import_entities(entities)
            engine.import_documents(fixture_documents(entities, notes_per_entity=2, heavy_notes=0))
            start, cpu_start = time.perf_counter(), time.process_time()
            self.assertEqual(engine.rescore(), 20)
            with patch.object(engine.store, '_assert_audit_chain', wraps=engine.store._assert_audit_chain) as chain, \
                 patch.object(engine.store, '_decode_score_run', wraps=engine.store._decode_score_run) as decode:
                explanations = [engine.explanation(entity['id']) for entity in entities]
            elapsed = time.perf_counter()-start
            cpu_elapsed = time.process_time()-cpu_start
            # Count the costly work directly; allow OS filesystem scheduling to
            # vary independently from this process's computational budget.
            self.assertEqual(chain.call_count, 20)
            self.assertEqual(decode.call_count, 20)
            self.assertLessEqual(cpu_elapsed, 5.0, f'20-entity local batch consumed {cpu_elapsed:.3f}s CPU')
            self.assertLessEqual(elapsed, 5.0, f'20-entity local batch took {elapsed:.3f}s wall time')
            first = explanations[0]
            canonical = score_entity(first['entity'], engine.store.evidence(first['entity']['id']),
                                     first['result']['as_of'])
            for key, value in canonical.items():
                self.assertEqual(first['result'][key], value, key)
            checked = assert_faithful_counterfactuals(engine, first)
            self.assertGreater(checked['source_document_removal_counterfactuals_checked'], 0)
            self.assertGreater(checked['literal_scoring_citations_checked'], 0)
            self.assertTrue(engine.replay_score(first['run']['id'])['matched'])
            self.assertFalse(guard, 'Default pipeline attempted a Python socket operation')

    def test_replay_uses_one_verified_read_snapshot_and_hashes_each_source_once(self):
        with tempfile.TemporaryDirectory() as directory, network_guard():
            engine = Engine(directory)
            entities = fixture_entities(1, heavy_notes=0)
            engine.store.import_entities(entities)
            engine.import_documents(fixture_documents(entities, notes_per_entity=10, heavy_notes=0))
            engine.rescore()
            run = engine.store.latest_score_run(entities[0]['id'])
            with patch.object(engine.store, '_assert_audit_chain', wraps=engine.store._assert_audit_chain) as chain, \
                 patch.object(engine.store, '_document_record_hash', wraps=engine.store._document_record_hash) as documents:
                self.assertTrue(engine.replay_score(run['id'])['matched'])
            self.assertEqual(chain.call_count, 1)
            self.assertEqual(documents.call_count, 10)

    def test_targeted_historical_note_is_not_lost_after_page_200(self):
        with tempfile.TemporaryDirectory() as directory, network_guard() as guard:
            engine = Engine(directory)
            entities = fixture_entities(1, heavy_notes=205)
            engine.store.import_entities(entities)
            engine.import_documents(fixture_documents(entities, notes_per_entity=1, heavy_notes=205))
            value = engine.explanation('pipeline-heavy')
            evidence = engine.store.evidence('pipeline-heavy')
            visible = {row['id'] for row in engine.store.evidence('pipeline-heavy', limit=200)}
            target = evidence[-1]
            self.assertEqual(len(evidence), 205)
            self.assertNotIn(target['id'], visible)
            retrieved = engine.store.run_evidence(value['run']['id'], target['id'])
            self.assertEqual(retrieved['text'], target['text'])
            run = engine.store.get_score_run(value['run']['id'])
            self.assertEqual(len(run['evidence_manifest']), 205)
            self.assertTrue(all('text' not in entry and 'body' not in entry for entry in run['evidence_manifest']))
            with engine.store.connection() as con:
                bodies = con.execute('SELECT COUNT(*) FROM documents').fetchone()[0]
            engine.rescore('pipeline-heavy')
            with engine.store.connection() as con:
                self.assertEqual(bodies, con.execute('SELECT COUNT(*) FROM documents').fetchone()[0])
            self.assertTrue(engine.replay_score(value['run']['id'])['matched'])
            self.assertFalse(guard)


if __name__ == '__main__':
    unittest.main()
