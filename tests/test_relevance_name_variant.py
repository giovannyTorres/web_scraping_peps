"""Bounded retrieval recovery from a real municipal-president search miss.

The real-name example is neutral. Adverse scoring controls use invented names.
"""
import copy
import hashlib
import json
import sqlite3
import unittest

from peps.identity import IdentityResolver
from peps.news_projection import projection_ready, projection_signature
from peps.relevance import MODEL_VERSION, RelevanceIndex
from peps.scoring import score_entity


ENTITY = {'id': 'MUN-NL-040', 'name': 'Adrián Emilio De la Garza Santos', 'kind': 'person',
          'aliases': [], 'context': {'municipality': 'Monterrey', 'state': 'Nuevo León',
                                    'role': 'presidente municipal'}}
SHORT = 'Adrián de la Garza Santos'


class ContextualPersonalNameVariantTests(unittest.TestCase):
    def test_real_editorial_snippet_is_a_retrieval_lead_only(self):
        # Public pilot result, saved before this rule was developed.
        title = '¿Quién es Adrián de la Garza? El nuevo alcalde de Monterrey'
        summary = 'Adrián de la Garza Santos tomó posesión como alcalde de Monterrey, Nuevo León.'
        result = RelevanceIndex([ENTITY]).rank(title, summary)
        self.assertTrue(result['relevant'])
        self.assertEqual(result['entity_ids'], [ENTITY['id']])
        self.assertFalse(result['identity_verified'])
        self.assertEqual(result['score_effect'], 'none')
        self.assertEqual(result['model_version'], MODEL_VERSION)
        self.assertEqual(len(result['matches']), 1)
        match = result['matches'][0]
        self.assertEqual(match['kind'], 'name_variant')
        self.assertTrue(match['retrieval_only'])
        self.assertTrue(match['context_supported'])
        self.assertFalse(match['corporate_core'])
        self.assertEqual(summary[match['start']:match['end']], SHORT)
        self.assertLess(result['rank'], RelevanceIndex([ENTITY]).rank(ENTITY['name'], '')['rank'])

    def test_geography_is_required_and_generic_roles_or_country_do_not_substitute(self):
        index = RelevanceIndex([ENTITY])
        for suffix in ('', ', presidente municipal.', ', notario de México.',
                       ', alcalde de Guadalajara, Jalisco.'):
            with self.subTest(suffix=suffix):
                self.assertFalse(index.rank(SHORT + suffix, '')['relevant'])
        for context in ({'role': 'Monterrey'}, {'organization': 'Monterrey'},
                        {'state': 'México'}, {'municipality': 'Municipio'},
                        {'city': 'Centro'}, {'city': 'Norte'}, {'state': 'NL'},
                        {'state': 'presidente municipal'}, {'city': 'notario'}):
            with self.subTest(context=context):
                index = RelevanceIndex([dict(ENTITY, context=context)])
                self.assertFalse(index.rank(SHORT + ', Monterrey, México, Municipio, Centro, Norte, NL, presidente municipal, notario.', '')['relevant'])

    def test_full_state_or_city_labels_can_supply_distinctive_geography(self):
        for context in ({'state': 'Nuevo León'}, {'municipality': 'Monterrey'},
                        {'state': 'Ciudad de México'}, {'state': 'Estado de México'}):
            hint = next(iter(context.values()))
            with self.subTest(context=context):
                self.assertTrue(RelevanceIndex([dict(ENTITY, context=context)]).rank(SHORT + ', ' + hint, '')['relevant'])

    def test_geography_must_be_nearby_and_outside_the_name(self):
        index = RelevanceIndex([ENTITY])
        self.assertFalse(index.rank(SHORT, 'Distancia ' * 100 + 'Monterrey')['relevant'])
        self.assertTrue(index.rank(SHORT, 'En Monterrey presentó el informe.')['relevant'])
        coinciding = dict(ENTITY, name='Carlos Daniel Nuevo León', context={'state': 'Nuevo León'})
        index = RelevanceIndex([coinciding])
        self.assertFalse(index.rank('Carlos Nuevo León presentó un informe.', '')['relevant'])
        self.assertTrue(index.rank('Carlos Nuevo León presentó un informe en Nuevo León.', '')['relevant'])

    def test_only_second_significant_token_is_omitted_once(self):
        index = RelevanceIndex([ENTITY])
        for name in ('Emilio de la Garza Santos', 'Adrián Emilio de la Santos',
                     'Adrián Emilio de la Garza', 'Adrián de la Garza', 'Adrián Santos Garza',
                     'Adrián Garza Santos'):
            with self.subTest(name=name):
                self.assertFalse(index.rank(name + ', Monterrey.', '')['relevant'])
        self.assertTrue(index.rank(SHORT + ', Monterrey.', '')['relevant'])

    def test_short_names_initials_digits_and_companies_are_not_shortened(self):
        for full, shortened in (('José Peña Villaluz', 'José Villaluz'), ('Juan Pérez', 'Juan'),
                                ('José A Peña Villaluz', 'José Peña Villaluz'),
                                ('José Segundo Peña 123', 'José Peña 123')):
            with self.subTest(full=full):
                self.assertFalse(RelevanceIndex([dict(ENTITY, name=full)]).rank(shortened + ', Monterrey.', '')['relevant'])
        self.assertFalse(RelevanceIndex([dict(ENTITY, kind='company')]).rank(SHORT + ', Monterrey.', '')['relevant'])

    def test_does_not_generate_variants_of_bank_aliases(self):
        entity = dict(ENTITY, name='Otra Persona Distinta', aliases=[ENTITY['name']])
        self.assertFalse(RelevanceIndex([entity]).rank(SHORT + ', Monterrey.', '')['relevant'])

    def test_existing_explicit_alias_keeps_its_previous_policy(self):
        entity = dict(ENTITY, aliases=[SHORT])
        result = RelevanceIndex([entity]).rank(SHORT + ', presidente municipal.', '')
        self.assertTrue(result['relevant'])
        self.assertEqual({match['kind'] for match in result['matches']}, {'alias'})

    def test_a_rejected_alias_is_not_reintroduced_as_a_generated_lead(self):
        for details in ({'verified': False}, {'validated': False}, {'status': 'rejected'}, {'status': 'revoked'}):
            with self.subTest(details=details):
                entity = dict(ENTITY, aliases=[{'name': SHORT, **details}])
                self.assertFalse(RelevanceIndex([entity]).rank(SHORT + ', Monterrey.', '')['relevant'])

    def test_filters_keep_canonical_names_short_names_and_entity_ids_separate(self):
        index = RelevanceIndex([ENTITY, dict(ENTITY, id='homonym')])
        text = SHORT + ', Monterrey.'
        self.assertEqual(index.rank(text, '')['entity_ids'], [ENTITY['id'], 'homonym'])
        self.assertEqual(index.rank(text, '', entity_ids=['homonym'])['entity_ids'], ['homonym'])
        self.assertFalse(index.rank(text, '', entity_ids=['other'])['relevant'])
        self.assertFalse(index.rank(text, '', names=['Otra Persona Distinta'])['relevant'])
        self.assertFalse(index.rank(text, '', entity_ids=[])['relevant'])
        self.assertFalse(index.rank(text, '', names=[])['relevant'])
        self.assertTrue(index.rank(text, '', names=[ENTITY['name']])['relevant'])
        self.assertTrue(index.rank(text, '', names=[SHORT])['relevant'])

    def test_unicode_offsets_and_enye_remain_literal(self):
        entity = dict(ENTITY, name='José Andrés Peña Villaluz')
        index = RelevanceIndex([entity])
        text = '📰 JOSE\u0301 PEÑA VILLALUZ, Monterrey.'
        result = index.rank(text, '')
        self.assertTrue(result['relevant'])
        for match in result['matches']:
            self.assertEqual(text[match['start']:match['end']], match['text'])
        self.assertFalse(index.rank('José Pena Villaluz, Monterrey.', '')['relevant'])

    def test_bank_aliases_and_identity_resolver_are_unchanged(self):
        entity = copy.deepcopy(ENTITY)
        before = copy.deepcopy(entity)
        resolver = IdentityResolver([entity])
        text = SHORT + ', alcalde de Monterrey, Nuevo León.'
        self.assertEqual(resolver.resolve(text), [])
        self.assertTrue(RelevanceIndex([entity]).rank(text, '')['relevant'])
        self.assertEqual(resolver.resolve(text), [])
        self.assertEqual(IdentityResolver([entity]).resolve(text), [])
        self.assertEqual(entity, before)
        self.assertEqual(entity['aliases'], [])

    def test_retrieval_match_does_not_become_adverse_identity_evidence(self):
        # Invented controls prevent associating any real subject with allegations.
        entity = dict(ENTITY, id='synthetic', name='Persona Segunda Prueba Sintética',
                      context={'municipality': 'Villa Sintética'})
        text = 'Persona Prueba Sintética, de Villa Sintética, fue acusada de fraude.'
        relevance = RelevanceIndex([entity]).rank(text, '')
        self.assertTrue(relevance['relevant'])
        self.assertEqual(IdentityResolver([entity]).resolve(text), [])
        evidence = {'id': 'synthetic-note', 'text': text, 'source_kind': 'official',
                    'source_url': 'https://synthetic.invalid/note',
                    'content_hash': hashlib.sha256(text.encode()).hexdigest(),
                    'published_at': '2026-10-16',
                    'metadata': {'news_module': True, 'relevance': relevance}}
        result = score_entity(entity, [evidence], '2026-10-16')
        self.assertEqual(result['score'], 0)
        self.assertIn('news_identity_not_attributed', result['evidence_audit'][0]['exclusions'])
        # Even trying to substitute the retrieval dictionary cannot validate it.
        evidence['identity_resolution'] = relevance
        forged = score_entity(entity, [evidence], '2026-10-16')
        self.assertEqual(forged['score'], 0)
        self.assertIn('stale_or_invalid_identity_resolution', forged['evidence_audit'][0]['exclusions'])

    def test_projection_cache_version_changes_with_rule(self):
        from unittest.mock import patch
        self.assertEqual(MODEL_VERSION, 'metadata-relevance-1.2.0')
        with sqlite3.connect(':memory:') as con:
            con.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)')
            con.execute("INSERT INTO meta VALUES('news_identity_revision','bank-1')")
            current = projection_signature(con)
            with patch('peps.relevance.MODEL_VERSION', 'metadata-relevance-1.1.0'):
                previous = projection_signature(con)
                con.execute("INSERT INTO meta VALUES('news_projection_ready',?)", (json.dumps(previous),))
                self.assertTrue(projection_ready(con))
            self.assertNotEqual(current, previous)
            self.assertFalse(projection_ready(con))


if __name__ == '__main__':
    unittest.main()
