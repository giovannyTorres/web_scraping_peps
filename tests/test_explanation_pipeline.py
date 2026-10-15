"""Offline integration invariants for historical, faithful explanations."""
import json
import socket
import tempfile
import unittest
from unittest.mock import patch

from peps.engine import Engine
from peps.scoring import score_entity


class ExplanationPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = Engine(self.temp.name)
        self.engine.store.import_entities([{'id': 'synthetic', 'name': 'Entidad Sintética Aurora', 'kind': 'company'}])
        self.engine.import_documents([{'url': 'https://fixture.invalid/pipeline', 'source_kind': 'official',
            'entity_ids': ['synthetic'], 'text': 'Entidad Sintética Aurora fue acusada de fraude.', 'published_at': '2026-10-10'}])

    def tearDown(self):
        self.temp.cleanup()

    def test_scoring_unchanged_by_auxiliary_processing_and_entire_pipeline_offline(self):
        with patch.object(socket.socket, 'connect', side_effect=AssertionError('no network')):
            value = self.engine.explanation('synthetic')
            evidence = self.engine.store.evidence('synthetic')
            canonical = score_entity(value['entity'], evidence, value['result']['as_of'])
            for key in canonical:
                self.assertEqual(canonical[key], value['result'][key], key)
            self.assertTrue(self.engine.replay_score(value['run']['id'])['matched'])
            self.assertTrue(self.engine.store.verify_audit()['ok'])

    def test_review_creates_new_run_and_old_run_retains_its_inputs(self):
        first = self.engine.explanation('synthetic')
        evidence_id = first['result']['evidence_audit'][0]['evidence_id']
        self.engine.store.review(evidence_id, 'confirmed', 'synthetic-reviewer', 'Fixture source and identity checked')
        second = self.engine.explanation('synthetic')
        self.assertNotEqual(first['run']['id'], second['run']['id'])
        historical = self.engine.explanation('synthetic', first['run']['id'])
        self.assertEqual(historical['result'], first['result'])
        self.assertTrue(historical['run']['historical'])
        self.assertTrue(self.engine.replay_score(first['run']['id'])['matched'])
        self.assertEqual(len(self.engine.store.score_history('synthetic')), 2)

    def test_runtime_changes_make_replay_explicitly_unsupported(self):
        value = self.engine.explanation('synthetic')
        with patch('peps.explainability.scoring_fingerprint', return_value={'different': True}):
            replay = self.engine.replay_score(value['run']['id'])
        self.assertEqual(replay['status'], 'unsupported')
        self.assertFalse(replay['matched'])

    def test_invalid_method_is_explicit_and_cannot_silently_change_score(self):
        config = self.engine.config()
        config['context_analysis'] = {'mode': 'external_api'}
        self.engine.config_path.write_text(json.dumps(config), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'mode'):
            self.engine.rescore('synthetic')


if __name__ == '__main__':
    unittest.main()
