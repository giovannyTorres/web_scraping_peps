"""Acquisition candidates are useful context, never positive scoring evidence."""
import hashlib
import unittest

from peps.relevance import RelevanceIndex, assessment_summary, zero_score_reasons
from peps.scoring import score_entity


ENTITY = {'id': 'one', 'name': 'José Peña Villaluz', 'aliases': ['JPV', 'Peña Villaluz'],
          'kind': 'person', 'context': {'state': 'Oaxaca', 'role': 'alcalde'}}


class RelevanceTests(unittest.TestCase):
    def test_full_name_in_title_or_summary_is_literal_unicode_candidate(self):
        index = RelevanceIndex([ENTITY])
        title = '📰 JOSE\u0301 PEÑA VILLALUZ presentó su informe.'
        result = index.rank(title, '')
        self.assertTrue(result['relevant'])
        self.assertFalse(result['identity_verified'])
        self.assertEqual(result['score_effect'], 'none')
        self.assertEqual(result['entity_ids'], ['one'])
        for match in result['matches']:
            self.assertEqual(title[match['start']:match['end']], match['text'])
        self.assertGreater(result['rank'], index.rank('', 'José Peña Villaluz presentó su informe.')['rank'])
        self.assertFalse(index.rank('José Pena Villaluz', '')['relevant'])

    def test_alias_requires_nearby_context_and_can_use_the_other_metadata_field(self):
        index = RelevanceIndex([ENTITY])
        self.assertFalse(index.rank('JPV presentó un informe.', '')['relevant'])
        self.assertTrue(index.rank('JPV presentó un informe.', 'El alcalde de Oaxaca informó sus resultados.')['relevant'])
        self.assertFalse(index.rank('JPV presentó un informe.', 'Distancia ' * 100 + 'Oaxaca')['relevant'])
        self.assertFalse(index.rank('XJPVX habló en Oaxaca.', '')['relevant'])

    def test_context_inside_the_alias_is_not_an_independent_clue(self):
        index = RelevanceIndex([dict(ENTITY, aliases=['Oaxaca'])])
        self.assertFalse(index.rank('Oaxaca recibe visitantes.', '')['relevant'])

    def test_target_subset_and_homonyms_preserve_all_ids_without_attributing_identity(self):
        index = RelevanceIndex([ENTITY, dict(ENTITY, id='two'),
                                dict(id='other', name='Otra Persona Sintética', aliases=[])])
        result = index.rank('José Peña Villaluz compareció.', '')
        self.assertEqual(result['entity_ids'], ['one', 'two'])
        self.assertEqual(index.rank('José Peña Villaluz compareció.', '', entity_ids=['two'])['entity_ids'], ['two'])
        self.assertFalse(index.rank('Otra Persona Sintética compareció.', '', entity_ids=['one'])['relevant'])
        self.assertFalse(index.rank('José Peña Villaluz compareció.', '', names=['Alias Distinto'])['relevant'])

    def test_victim_negation_acquittal_and_neutral_news_are_not_filtered_as_irrelevant(self):
        index = RelevanceIndex([ENTITY])
        for text in ('José Peña Villaluz fue víctima de fraude.', 'José Peña Villaluz no fue detenido.',
                     'José Peña Villaluz fue absuelto de fraude.', 'José Peña Villaluz inauguró una biblioteca.'):
            with self.subTest(text=text):
                self.assertTrue(index.rank(text, '')['relevant'])

    def test_300_names_do_not_match_a_generic_feed_or_partial_name_tokens(self):
        entities = [dict(id=str(i), name=f'Persona Sintética Alba {i:03}', aliases=[]) for i in range(300)]
        index = RelevanceIndex(entities)
        self.assertFalse(index.rank('El Congreso presentó una iniciativa.', 'México aprobó una medida.')['relevant'])
        self.assertEqual(index.rank('Persona Sintética Alba 299 acudió al Congreso.', '')['entity_ids'], ['299'])
        self.assertFalse(index.rank('Persona Sintética Alba 2999 acudió al Congreso.', '')['relevant'])


class ZeroReasonsTests(unittest.TestCase):
    @staticmethod
    def evidence(text, **kwargs):
        return {'id': 'e', 'text': text, 'source_url': 'https://synthetic.invalid/note', 'source_kind': 'official',
                'content_hash': hashlib.sha256(text.encode()).hexdigest(), 'published_at': '2026-10-15', **kwargs}

    def test_no_attributed_evidence_does_not_claim_no_source_bodies_exist(self):
        reasons = zero_score_reasons(score_entity(ENTITY, [], '2026-10-16'))
        self.assertEqual([reason['code'] for reason in reasons], ['no_entity_evidence'])
        self.assertNotIn('cuerpo', reasons[0]['label'])

    def test_metadata_discovery_and_missing_source_body_have_distinct_trace_reasons(self):
        text = 'José Peña Villaluz fue acusado de fraude.'
        discovery = score_entity(ENTITY, [self.evidence(text, source_kind='discovery')], '2026-10-16')
        self.assertEqual(discovery['score'], 0)
        self.assertEqual(zero_score_reasons(discovery)[0]['code'], 'metadata_only_not_body')
        missing = score_entity(ENTITY, [self.evidence('')], '2026-10-16')
        self.assertEqual(zero_score_reasons(missing)[0]['code'], 'missing_source_body')

    def test_missing_identity_resolution_and_neutral_findings_are_different(self):
        text = 'José Peña Villaluz fue acusado de fraude.'
        unresolved = score_entity(ENTITY, [self.evidence(text, metadata={'news_module': True})], '2026-10-16')
        self.assertEqual(zero_score_reasons(unresolved)[0]['code'], 'identity_pending')
        neutral = score_entity(ENTITY, [self.evidence('José Peña Villaluz inauguró una biblioteca.')], '2026-10-16')
        self.assertEqual(zero_score_reasons(neutral)[0]['code'], 'no_adverse_findings')

    def test_negation_and_exculpatory_context_explain_zero_without_fabricating_positive(self):
        for text in ('José Peña Villaluz no fue detenido por fraude.', 'José Peña Villaluz fue absuelto de fraude.'):
            result = score_entity(ENTITY, [self.evidence(text)], '2026-10-16')
            self.assertEqual(result['score'], 0)
            self.assertIn('negated_or_exculpatory', {reason['code'] for reason in zero_score_reasons(result)})
        positive = score_entity(ENTITY, [self.evidence('José Peña Villaluz fue acusado de fraude.')], '2026-10-16')
        self.assertGreater(positive['score'], 0)
        self.assertEqual(zero_score_reasons(positive), [])

    def test_assessment_distinguishes_legacy_absence_text_identity_and_evaluated_zero(self):
        self.assertEqual(assessment_summary({'score': 0})['state'], 'insufficient_evidence')
        self.assertIsNone(assessment_summary({'score': 0})['display_score'])
        text = 'José Peña Villaluz fue acusado de fraude.'
        discovery = score_entity(ENTITY, [self.evidence(text, source_kind='discovery')], '2026-10-16')
        self.assertEqual(assessment_summary(discovery)['state'], 'insufficient_text')
        self.assertIsNone(assessment_summary(discovery)['display_score'])
        identity = score_entity(ENTITY, [self.evidence(text, metadata={'news_module': True})], '2026-10-16')
        self.assertEqual(assessment_summary(identity)['state'], 'pending_identity')
        self.assertIsNone(assessment_summary(identity)['display_score'])
        for text in ('José Peña Villaluz no fue detenido por fraude.', 'José Peña Villaluz fue absuelto de fraude.',
                     'José Peña Villaluz inauguró una biblioteca.', 'José Peña Villaluz fue víctima de fraude.'):
            result = score_entity(ENTITY, [self.evidence(text)], '2026-10-16')
            with self.subTest(text=text):
                self.assertEqual(result['score'], 0)
                self.assertEqual(assessment_summary(result)['state'], 'evaluated')
                self.assertEqual(assessment_summary(result)['display_score'], 0)

    def test_an_admissible_neutral_source_can_be_assessed_beside_an_unresolved_homonym(self):
        rows = [self.evidence('José Peña Villaluz inauguró una biblioteca.'),
                self.evidence('José Peña Villaluz fue acusado de fraude.', id='homonym', metadata={'news_module': True})]
        result = score_entity(ENTITY, rows, '2026-10-16')
        self.assertEqual(result['score'], 0)
        self.assertEqual(assessment_summary(result)['state'], 'evaluated')
        self.assertEqual(assessment_summary(result)['display_score'], 0)
        self.assertIn('identity_pending', assessment_summary(result)['reason_codes'])


if __name__ == '__main__':
    unittest.main()
