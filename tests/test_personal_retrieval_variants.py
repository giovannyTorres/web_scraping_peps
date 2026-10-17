"""Retrieval variants stay distinct from verified aliases and identity proof."""
from copy import deepcopy
from datetime import datetime, timezone
import unittest
from urllib.parse import parse_qs, urlsplit

from peps.news_search import (_personal_terminal_phrase, build_search_plan,
                              normalize_search_config, wordpress_queries)


NOW = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)
SETTINGS = {'strategy': 'targeted', 'preset': 'identity', 'start': '2000-01-01',
            'end': '2026-10-16', 'include_aliases': False, 'use_context': False,
            'execution_mode': 'extended', 'session_seconds': 1800, 'max_queries': 10000}
WP = {'id': 'publisher', 'kind': 'wordpress_api', 'search_kind': 'wordpress_api_search',
      'url': 'https://publisher.invalid/wp-json/wp/v2/posts',
      'base_url': 'https://publisher.invalid/', 'allowed_hosts': ['publisher.invalid'],
      'enabled': True, 'terms_approved': True, 'search_max_records': 50,
      'min_interval_seconds': 1}


class PersonalRetrievalVariantTests(unittest.TestCase):
    def test_terminal_phrase_preserves_connectors_and_compound_surnames(self):
        cases = {'José Higinio Núñez y Bandera': 'Núñez y Bandera',
                 'Juan Carlos de la Fuente Pérez': 'de la Fuente Pérez',
                 'María del Carmen García Pérez': 'García Pérez',
                 'Juan Ríos-Santos Martínez': 'Ríos-Santos Martínez',
                 'Carlos Fernando Licona Rivemar': 'Licona Rivemar'}
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(_personal_terminal_phrase(name), expected)

    def test_two_word_ambiguous_and_non_name_inputs_do_not_generate_a_query(self):
        for name in ('Juan Pérez', 'Juan de la Rosa', 'Juan Li An', 'Persona 123 Pérez', ''):
            with self.subTest(name=name):
                self.assertIsNone(_personal_terminal_phrase(name))

    def test_variant_is_an_explicit_retrieval_lead_without_changing_identity(self):
        entity = {'id': 'one', 'name': 'Carlos Fernando Licona Rivemar', 'kind': 'person',
                  'aliases': ['Alias documental'], 'context': {'role': 'notario', 'state': 'Hidalgo'}}
        original = deepcopy(entity)
        config = normalize_search_config(SETTINGS, NOW)
        old = wordpress_queries(entity, config)
        new = wordpress_queries(entity, config, personal_fallbacks=True)
        self.assertEqual(new[:-1], old)
        self.assertEqual(new[0]['query'], '"Carlos Fernando Licona Rivemar"')
        self.assertEqual(new[-1]['query'], '"Carlos" "Licona Rivemar"')
        self.assertEqual(new[-1]['query_type'], 'retrieval_fallback')
        self.assertEqual(new[-1]['retrieval_variant'], 'given_and_terminal_phrase')
        self.assertEqual(new[-1]['retrieval_origin'], 'canonical_name')
        self.assertFalse(new[-1]['identity_confirmation'])
        self.assertEqual(entity, original)

    def test_only_opted_in_publisher_api_sources_gain_the_variant(self):
        entity = {'id': 'one', 'name': 'Carlos Fernando Licona Rivemar'}
        for enabled in (None, False, 'true'):
            source = dict(WP, personal_name_fallbacks=enabled)
            with self.subTest(enabled=enabled):
                plan = build_search_plan([entity], [source], SETTINGS, now=NOW)
                self.assertEqual(plan['query_count'], 1)
        source = dict(WP, personal_name_fallbacks=True)
        plan = build_search_plan([entity], [source], SETTINGS, now=NOW)
        self.assertEqual(plan['query_count'], 2)
        primary, fallback = plan['queries']
        self.assertEqual(primary['query_type'], 'identity')
        self.assertEqual(fallback['query_type'], 'retrieval_fallback')
        self.assertEqual(fallback['names'], [entity['name']])
        self.assertEqual(fallback['entity_ids'], ['one'])
        self.assertEqual(parse_qs(urlsplit(fallback['url']).query)['search'], ['"Carlos" "Licona Rivemar"'])
        self.assertEqual(fallback['retrieval_phrase'], 'Licona Rivemar')

    def test_company_and_two_word_people_remain_exact(self):
        entities = [{'id': 'company', 'name': 'Compañía Nacional de Servicios', 'kind': 'company'},
                    {'id': 'person', 'name': 'Juan Pérez', 'kind': 'person'}]
        plan = build_search_plan(entities, [dict(WP, personal_name_fallbacks=True)], SETTINGS, now=NOW)
        self.assertFalse(any(q['query_type'] == 'retrieval_fallback' for q in plan['queries']))

    def test_first_pass_covers_canonical_names_before_terminal_queries(self):
        entities = [{'id': 'one', 'name': 'Carlos Fernando Licona Rivemar'},
                    {'id': 'two', 'name': 'José Higinio Núñez y Bandera'}]
        sources = [dict(WP, id='first', personal_name_fallbacks=True),
                   dict(WP, id='second', url='https://second.invalid/wp-json/wp/v2/posts',
                        base_url='https://second.invalid/', allowed_hosts=['second.invalid'],
                        personal_name_fallbacks=True)]
        plan = build_search_plan(entities, sources, SETTINGS, now=NOW)
        types = [q['query_type'] for q in plan['queries']]
        self.assertEqual(types[:4], ['identity'] * 4)
        self.assertEqual(types[4:], ['retrieval_fallback'] * 4)
        self.assertFalse(plan['truncated'])

    def test_context_is_supplied_only_when_requested(self):
        entity = {'id': 'one', 'name': 'Carlos Fernando Licona Rivemar',
                  'context': {'state': 'Hidalgo', 'role': 'notario público'}}
        for use_context, expected in ((False, '"Carlos" "Licona Rivemar"'),
                                      (True, '"Carlos" "Licona Rivemar" "Hidalgo"')):
            config = normalize_search_config(dict(SETTINGS, use_context=use_context), NOW)
            with self.subTest(use_context=use_context):
                self.assertEqual(wordpress_queries(entity, config, personal_fallbacks=True)[-1]['query'], expected)

    def test_complete_300_person_plan_accounts_for_every_fallback_within_budget(self):
        # This tests scheduling/accounting only; it is not a performance result.
        entities = [{'id': str(index), 'name': 'Juan Carlos Apellido '
                     + chr(65 + index // 26) + chr(65 + index % 26)}
                    for index in range(300)]
        sources = [dict(WP, id='publisher' + str(index),
                        url='https://publisher' + str(index) + '.invalid/wp-json/wp/v2/posts',
                        base_url='https://publisher' + str(index) + '.invalid/',
                        allowed_hosts=['publisher' + str(index) + '.invalid'],
                        personal_name_fallbacks=index < 8)
                   for index in range(24)]
        plan = build_search_plan(entities, sources, SETTINGS, now=NOW)
        self.assertFalse(plan['truncated'])
        for source in sources:
            actual = {i for q in plan['queries'] if q['source_id'] == source['id'] for i in q['entity_ids']}
            self.assertEqual(actual, {e['id'] for e in entities})
        self.assertEqual(plan['query_count'], 300 * 24 + 300 * 8)
        self.assertEqual(sum(q['query_type'] == 'retrieval_fallback' for q in plan['queries']), 300 * 8)

    def test_expanded_budget_remains_explicit_and_bounded(self):
        self.assertEqual(normalize_search_config(dict(SETTINGS, max_queries=20000), NOW)['max_queries'], 20000)
        for invalid in (20001, 20000.0, True, 0):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                normalize_search_config(dict(SETTINGS, max_queries=invalid), NOW)


if __name__ == '__main__':
    unittest.main()
