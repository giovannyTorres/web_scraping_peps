"""Evidence fidelity and nonlinear contribution regressions; invented fixtures."""
import copy
import hashlib
import math
import unittest

from peps.explainability import explain_score
from peps.scoring import PARAMETERS, _combine, score_entity

ENTITY = {'id': 'synthetic-trace', 'name': 'Entidad Sintética Aurora', 'kind': 'company', 'aliases': [], 'context': {}}
AS_OF = '2026-10-15'


def document(identifier='ev1', text='Entidad Sintética Aurora fue acusada de fraude.', **extras):
    return {'id': identifier, 'source_url': 'https://fixture.invalid/' + identifier,
            'source_kind': 'official', 'published_at': AS_OF, 'text': text,
            'content_hash': hashlib.sha256(text.encode()).hexdigest(), **extras}


class ExplainabilityTests(unittest.TestCase):
    def test_every_factor_links_to_actual_scorer_finding_and_exact_body(self):
        rows = [document(), document('second', 'Entidad Sintética Aurora fue detenida por corrupción.', event_id='other')]
        result = score_entity(ENTITY, rows, AS_OF)
        explanation = explain_score(result)
        inputs = {row['id']: row for row in rows}
        self.assertEqual(len(explanation['factors']), len(result['components']))
        for factor in explanation['factors']:
            for cite in factor['evidence']:
                text = inputs[cite['evidence_id']]['text']
                self.assertEqual(text[cite['start']:cite['end']], cite['quote'])
                values = cite['calculation_inputs']
                product = math.prod(value for key, value in values.items() if key != 'denial_zero_override')
                self.assertAlmostEqual(cite['points_before_dedup'], round(product, 6), places=6)

    def test_event_removal_deltas_use_both_real_branches_not_linear_sums(self):
        rows = [document(str(i), f'Entidad Sintética Aurora fue condenada por fraude. Evento sintético independiente {i}.', event_id=str(i)) for i in range(5)]
        result = score_entity(ENTITY, rows, AS_OF)
        explanation = explain_score(result)
        self.assertNotEqual(sum(f['score_delta_when_removed'] for f in explanation['factors']), result['score'])
        for factor in explanation['factors']:
            others = [c for c in result['components'] if c['event_group'] != factor['event_group']]
            expected = max(_combine([c['provisional_points'] for c in others]), _combine([c['validated_points'] for c in others]))
            self.assertEqual(expected, factor['score_without_event'])
            self.assertEqual(result['score']-expected, factor['score_delta_when_removed'])

    def test_duplicate_maximum_is_not_double_counted_and_ties_are_visible(self):
        row = document()
        result = score_entity(ENTITY, [row, dict(row, id='copy')], AS_OF)
        factors = explain_score(result)['factors']
        self.assertEqual(len(factors), 1)
        self.assertEqual(len(factors[0]['evidence']), 2)
        self.assertTrue(all(c['provisional_selected'] for c in factors[0]['evidence']))
        self.assertEqual(factors[0]['provisional_points'], factors[0]['evidence'][0]['points_before_dedup'])

    def test_resolution_and_exclusions_remain_inspectable_without_new_reasons(self):
        rows = [document(event_id='case'), document('resolution', 'Entidad Sintética Aurora fue absuelta de fraude.', event_id='case')]
        result = score_entity(ENTITY, rows, AS_OF)
        explanation = explain_score(result)
        self.assertEqual(result['score'], 0)
        self.assertTrue(any(c['resolution_applied'] for c in explanation['factors'][0]['evidence']))
        self.assertTrue(any('Cero' in text for text in explanation['warnings']))
        rejected = score_entity(ENTITY, [document(review={'status': 'rejected'})], AS_OF)
        self.assertEqual(explain_score(rejected)['factors'], [])
        self.assertEqual(explain_score(rejected)['exclusions'][0]['code'], 'human_rejected')

    def test_auxiliary_claims_cannot_become_numeric_factors(self):
        result = score_entity(ENTITY, [document()], AS_OF)
        before = explain_score(result)['factors']
        result['contextual_analysis'] = [{'evidence_id': 'ev1', 'analysis': {'role': 'victim', 'findings': [{'quote': 'Invented'}]}}]
        explanation = explain_score(result)
        self.assertEqual(explanation['factors'], before)
        self.assertTrue(any('Posible falso positivo' in warning for warning in explanation['warnings']))

    def test_scale_thresholds_and_returned_parameters_are_defensive(self):
        result = score_entity(ENTITY, [], AS_OF)
        original = copy.deepcopy(PARAMETERS)
        result['parameters']['category_points']['fraud'] = 999
        self.assertEqual(PARAMETERS, original)
        meaning = explain_score(result)['interpretation']
        self.assertEqual(meaning['scale']['maximum'], 95)
        self.assertIn('No equivalen', meaning['comparison'])
        self.assertEqual([x['min'] for x in meaning['ranges']], [0, 1, 20, 50])

    def test_invalid_support_metadata_cannot_crash_or_invent_more_points(self):
        baseline = score_entity(ENTITY, [document()], AS_OF)
        for value in (['unsupported'], {'publisher': 'unsupported'}, 17):
            result = score_entity(ENTITY, [document(metadata={'publisher_id': value, 'content_cluster': value})], AS_OF)
            self.assertEqual(result['score'], baseline['score'])
            self.assertEqual(result['components'][0]['source_support']['distinct_content_clusters'], 1)
            self.assertTrue(result['warnings'])

    def test_pending_resolution_cannot_make_provisional_reason_explain_validated_score(self):
        review = {'status': 'confirmed', 'actor': 'fixture', 'reason': 'Synthetic evidence checked'}
        rows = [document(event_id='case', review=review),
                document('resolution', 'Entidad Sintética Aurora fue absuelta de fraude.', event_id='case')]
        result = score_entity(ENTITY, rows, AS_OF)
        self.assertEqual(result['provisional_score'], 0)
        self.assertGreater(result['validated_score'], 0)
        factor = explain_score(result)['factors'][0]
        selected = [cite for cite in factor['evidence'] if cite['selected_for_score']]
        self.assertEqual([cite['evidence_id'] for cite in selected], ['ev1'])
        self.assertTrue(selected[0]['validated_selected'])

    def test_saturation_can_hide_removal_delta_without_erasing_real_event_points(self):
        rows = [document(str(i), f'Entidad Sintética Aurora fue condenada por fraude. Evento independiente {i}.', event_id=str(i)) for i in range(20)]
        result = score_entity(ENTITY, rows, AS_OF)
        self.assertEqual(result['score'], 95)
        for factor in explain_score(result)['factors']:
            self.assertEqual(factor['score_delta_when_removed'], 0)
            self.assertGreater(factor['provisional_points'], 0)

    def test_pending_resolution_of_one_category_is_not_hidden_by_another_reviewed_resolution(self):
        review = {'status': 'confirmed', 'actor': 'fixture', 'reason': 'Synthetic evidence checked'}
        rows = [document('fraud', 'Entidad Sintética Aurora fue condenada por fraude.', event_id='mixed', review=review),
                document('fraud-clear', 'Entidad Sintética Aurora fue absuelta de fraude.', event_id='mixed', review=review),
                document('corruption', 'Entidad Sintética Aurora fue condenada por corrupción.', event_id='mixed', review=review),
                document('corruption-clear', 'Entidad Sintética Aurora fue absuelta de corrupción.', event_id='mixed')]
        result = score_entity(ENTITY, rows, AS_OF)
        self.assertEqual((result['provisional_score'], result['validated_score']), (0, 32))
        self.assertTrue(result['components'][0]['resolution_review_required'])
        self.assertTrue(any('resolución exculpatoria pendiente' in warning for warning in explain_score(result)['warnings']))

    def test_paths_on_one_host_do_not_become_independent_publishers(self):
        rows = [document(str(i), f'Entidad Sintética Aurora fue acusada de fraude. Copia editorial {i}.', event_id='same') for i in range(4)]
        result = score_entity(ENTITY, rows, AS_OF)
        support = result['components'][0]['source_support']
        self.assertEqual(support['distinct_publishers'], 1)
        self.assertEqual(support['distinct_source_hosts'], 1)
        self.assertFalse(support['publisher_ownership_verified'])


if __name__ == '__main__':
    unittest.main()
