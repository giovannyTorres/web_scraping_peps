"""Offline evaluation boundaries; all personal names here are synthetic."""
from copy import deepcopy
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from scripts import backtest_notary_models as models


class NotaryModelBacktestTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.bank = Path(self.temporary.name)
        self.entity = {'id': 'synthetic-notary', 'name': 'José Peña Villaluz',
                       'aliases': [], 'context': {'occupation': 'notario'}}
        self.path = self.bank / 'review_texts' / 'article.txt'
        self.path.parent.mkdir()
        self.path.write_text(self.entity['name'] + ' fue acusado de fraude por la fiscalía.', encoding='utf-8')
        self.payload = {'entities': [self.entity], 'cutoff': '2026-01-01',
                        'retrieved': [{'entity_id': self.entity['id'],
                                       'url': 'https://example.org/article',
                                       'text_path': 'review_texts/article.txt',
                                       'published_at': '2020-01-01', 'has_text': True,
                                       'relevance': 'yes', 'incident': 'yes',
                                       'reviewed_independently': True}]}

    def evaluate(self, payload=None, **kwargs):
        return models.evaluate_models(payload or self.payload, self.bank, **kwargs)

    def row(self, result, variant):
        return next(row for row in result['document_predictions'] if row['variant'] == variant)

    def test_predictions_are_independent_of_gold_and_review_status(self):
        original = self.evaluate()
        changed = deepcopy(self.payload)
        changed['entities'][0].update(incident='no', group='control', incident_label='no')
        changed['retrieved'][0].update(incident='no', relevance='no', reviewed_independently=False,
                                        reviewed_doc_target='different_target', current_adverse_role='no')
        changed['reference_articles'] = [{'entity_id': 'unrelated', 'url': 'http://not-an-input'}]
        after = self.evaluate(changed)
        self.assertEqual(original['prediction_inputs_sha256'], after['prediction_inputs_sha256'])
        self.assertEqual(original['predictions_sha256'], after['predictions_sha256'])
        self.assertEqual(self.row(original, 'nlp_direct_adverse')['prediction'], 'yes')
        self.assertEqual(self.row(original, 'context_role_auxiliary')['prediction'], 'yes')

    def test_frozen_semantic_hash_changes_with_body_and_no_fact_claims_are_returned(self):
        original = self.evaluate()
        self.path.write_text(self.entity['name'] + ' asistió a un foro sobre prevención de fraude.', encoding='utf-8')
        after = self.evaluate()
        self.assertNotEqual(original['prediction_inputs_sha256'], after['prediction_inputs_sha256'])
        self.assertNotEqual(original['predictions_sha256'], after['predictions_sha256'])
        self.assertEqual(self.row(after, 'nlp_direct_adverse')['prediction'], 'no')
        serialized = json.dumps(after)
        self.assertNotIn('identity_confidence', serialized)
        self.assertNotIn('"quote"', serialized)
        self.assertNotIn('risk_score', serialized)
        self.assertFalse(after['policy']['scoring_performed'])
        self.assertEqual(after['policy']['identity_status'], 'not_verified')

    def test_provenance_identifies_sources_artifact_config_and_timing(self):
        result = self.evaluate()
        provenance = result['provenance']
        self.assertEqual(provenance['nlp_rules_hash'], models.nlp.RULES_HASH)
        self.assertEqual(provenance['context_artifact_sha256'],
                         hashlib.sha256(models.context_model.MODEL_PATH.read_bytes()).hexdigest())
        self.assertEqual(provenance['source_sha256']['nlp'],
                         hashlib.sha256(Path(models.nlp.__file__).read_bytes()).hexdigest())
        self.assertGreaterEqual(result['total_cpu_seconds'], 0)
        self.assertTrue(all(row['cpu_seconds'] >= 0 for row in result['document_predictions']))
        self.assertEqual(result['policy']['nlp_positive_stages'],
                         sorted(set(models.nlp.STAGE_PATTERNS) - {'acquittal', 'dismissed', 'denial'}))

    def test_missing_empty_non_utf8_and_oversized_bodies_abstain_without_analyzing_prefix(self):
        fixtures = [(None, 'missing_body'), (b'', 'empty_body'),
                    (b'\xff', 'invalid_utf8'), (b'A' * 81, 'text_byte_budget_exceeded')]
        for data, reason in fixtures:
            with self.subTest(reason=reason):
                if data is None:
                    self.path.unlink(missing_ok=True)
                else:
                    self.path.write_bytes(data)
                with patch.object(models.nlp, 'analyze', side_effect=AssertionError('must not infer on incomplete body')):
                    result = self.evaluate(max_text_bytes=80)
                self.assertTrue(all(row['prediction'] == 'abstain' and row['reason'] == reason
                                    for row in result['document_predictions']))

    def test_body_hash_mismatch_and_acquisition_truncation_abstain(self):
        payload = deepcopy(self.payload)
        payload['retrieved'][0]['body_sha256'] = '0' * 64
        result = self.evaluate(payload)
        self.assertTrue(all(row['reason'] == 'body_hash_mismatch' for row in result['document_predictions']))
        payload['retrieved'][0].pop('body_sha256')
        payload['retrieved'][0]['truncated'] = True
        with patch.object(models, '_body', side_effect=AssertionError('must not reopen known incomplete body')):
            result = self.evaluate(payload)
        self.assertTrue(all(row['prediction'] == 'abstain' for row in result['document_predictions']))

    def test_paths_must_remain_in_authorized_bank_including_symlinks(self):
        with tempfile.TemporaryDirectory() as outside:
            forbidden = Path(outside) / 'secret.txt'
            forbidden.write_text('do not read', encoding='utf-8')
            payload = deepcopy(self.payload)
            payload['retrieved'][0]['text_path'] = str(forbidden)
            with self.assertRaisesRegex(ValueError, 'escapes'):
                self.evaluate(payload)
            link = self.bank / 'escape.txt'
            try:
                link.symlink_to(forbidden)
            except OSError:
                return  # Windows without symlink privilege still tested absolute escape.
            payload['retrieved'][0]['text_path'] = 'escape.txt'
            with self.assertRaisesRegex(ValueError, 'escapes'):
                self.evaluate(payload)

    def test_future_or_unknown_publication_abstains_before_opening_body(self):
        for value, reason in [('2026-01-02', 'publication_after_cutoff'),
                              (None, 'publication_date_unknown')]:
            payload = deepcopy(self.payload)
            payload['retrieved'][0]['published_at'] = value
            with self.subTest(reason=reason), patch.object(models, '_body', side_effect=AssertionError('body not eligible')):
                result = self.evaluate(payload)
            self.assertTrue(all(row['reason'] == reason and row['prediction'] == 'abstain'
                                for row in result['document_predictions']))
        payload = deepcopy(self.payload)
        payload['cutoff'] = (date.today() + timedelta(days=2)).isoformat()
        with self.assertRaisesRegex(ValueError, 'future'):
            self.evaluate(payload)

    def test_target_must_be_literal_and_negations_hypotheticals_and_resolutions_are_not_positive(self):
        fixtures = [
            ('Otra Persona fue acusada de fraude.', 'abstain'),
            (self.entity['name'] + ' no fue acusado de fraude.', 'no'),
            (self.entity['name'] + ' podría ser acusado de fraude.', 'no'),
            (self.entity['name'] + ' fue absuelto de fraude.', 'no'),
            (self.entity['name'] + ' denunció a los responsables de fraude.', 'no'),
            (self.entity['name'] + ' fue acusado de fraude. ' + self.entity['name'] + ' fue absuelto de fraude.', 'abstain'),
        ]
        for text, expected in fixtures:
            with self.subTest(text=text):
                self.path.write_text(text, encoding='utf-8')
                self.assertEqual(self.row(self.evaluate(), 'nlp_direct_adverse')['prediction'], expected)

    def test_production_reputational_fallback_is_a_valid_direct_adverse_signal(self):
        fixtures = [
            (' fue investigado por irregularidades notariales.', 'yes'),
            (' no fue investigado por irregularidades notariales.', 'no'),
            (' podría ser investigado por irregularidades notariales.', 'no'),
            (' denunció a los responsables de irregularidades notariales.', 'no'),
            (' negó las acusaciones por irregularidades notariales.', 'no'),
        ]
        for suffix, expected in fixtures:
            with self.subTest(suffix=suffix):
                text = self.entity['name'] + suffix
                self.path.write_text(text, encoding='utf-8')
                result = self.evaluate()
                prediction = self.row(result, 'nlp_direct_adverse')
                self.assertEqual(prediction['prediction'], expected)
                if expected == 'yes':
                    self.assertEqual(prediction['qualifying_findings'][0]['category'], 'reputational')
                    self.assertEqual(prediction['qualifying_findings'][0]['stage'], 'investigation')
        self.assertEqual(result['policy']['version'], 'notary-document-adapters-2')
        self.assertEqual(result['policy']['revision_kind'], 'evaluation_adapter_correction_not_model_improvement')
        self.assertFalse(result['policy']['production_model_or_rules_modified'])
        self.assertIn('reputational', result['policy']['nlp_adverse_categories'])

    def test_auxiliary_role_mapping_never_confuses_victim_with_implicated_target(self):
        fixtures = [('accused', 'yes'), ('victim', 'no'), ('complainant', 'no'),
                    ('incidental', 'no'), ('negated', 'no'), ('resolved', 'no'),
                    ('indirect', 'no'), ('insufficient', 'abstain')]
        for role, expected in fixtures:
            result = {'role': role, 'abstained': role == 'insufficient', 'truncated': False,
                      'predicted_role': role, 'decision_margin': 2.0,
                      'competing_roles': [], 'processed_chars': 50, 'content_hash': '0' * 64}
            with self.subTest(role=role), patch.object(models.context_model, 'analyze', return_value=result):
                self.assertEqual(models._context_prediction('article', self.entity)['prediction'], expected)
        result.update(role='accused', abstained=False, truncated=True)
        with patch.object(models.context_model, 'analyze', return_value=result):
            self.assertEqual(models._context_prediction('article', self.entity)['prediction'], 'abstain')

    def test_context_runtime_truncation_is_visible_and_never_a_prefix_success(self):
        self.path.write_text(self.entity['name'] + ' fue acusado de fraude. ' + 'relleno ' * 9000,
                             encoding='utf-8')
        result = self.row(self.evaluate(), 'context_role_auxiliary')
        self.assertTrue(result['truncated'])
        self.assertEqual(result['prediction'], 'abstain')
        self.assertEqual(result['reason'], 'context_budget_exceeded')

    def test_duplicate_canonical_pairs_and_bad_entity_aliases_fail_before_metrics(self):
        payload = deepcopy(self.payload)
        payload['retrieved'].append({**payload['retrieved'][0], 'url': 'https://example.org/article#duplicate'})
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            self.evaluate(payload)
        payload = deepcopy(self.payload)
        payload['entities'][0]['aliases'] = ['Alias'] * 33
        with self.assertRaisesRegex(ValueError, 'aliases'):
            self.evaluate(payload)

    def test_inference_does_not_access_network(self):
        def deny(*args, **kwargs):
            raise AssertionError('offline adapter attempted network')
        with patch.object(socket, 'create_connection', side_effect=deny), patch.object(socket, 'socket', side_effect=deny):
            result = self.evaluate()
        self.assertEqual(len(result['document_predictions']), 2)


if __name__ == '__main__':
    unittest.main()
