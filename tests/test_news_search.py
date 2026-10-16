"""Directed historical discovery plans: no external requests in tests."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from collections import Counter
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from peps.engine import Engine
from peps.news import NewsAcquirer
from peps.news_search import (build_search_plan, default_search_config, entity_queries,
                              merge_source_defaults, normalize_search_config, query_url, split_query_windows,
                              wordpress_queries)

NOW = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)
ENTITY = {'id': 'synthetic', 'name': 'Persona Sintética Inventada', 'aliases': ['Alias Sintético'],
          'context': {'state': 'Oaxaca', 'role': 'alcalde'}}
WP = {'id': 'publisher', 'kind': 'rss', 'search_kind': 'wordpress_rss', 'search_url': 'https://publisher.invalid/',
      'base_url': 'https://publisher.invalid/', 'enabled': True, 'terms_approved': True,
      'search_max_records': 10, 'min_window_seconds': 86400, 'min_interval_seconds': 5}
GDELT = {'id': 'index', 'kind': 'gdelt', 'base_url': 'https://api.gdeltproject.org/api/v2/doc/doc',
         'enabled': True, 'terms_approved': True, 'max_records': 250, 'max_history_days': 90}


class SearchPlanTests(unittest.TestCase):
    def test_652_names_start_with_nominal_search_without_scanning_unrelated_archives(self):
        """An enabled archive must not consume a nominal search's job budget."""
        entities = [{'id': str(index), 'name': f'Persona Sintética Notarial {index}',
                     'context': {'state': 'Ciudad de México', 'role': 'notario público'}}
                    for index in range(652)]
        archive = {'id': 'archive', 'kind': 'wordpress_api',
                   'url': 'https://publisher.invalid/wp-json/wp/v2/posts',
                   'enabled': True, 'terms_approved': True}
        web = {'id': 'web', 'kind': 'mwmbl', 'url': 'https://mwmbl.org/api/v2/search/',
               'enabled': True, 'terms_approved': True}
        second = dict(web, id='second', kind='duckduckgo_html',
                      url='https://html.duckduckgo.com/html/')
        settings = {'strategy': 'targeted', 'preset': 'adverse', 'execution_mode': 'quick',
                    'max_queries': 652, 'start': '2016-01-01', 'end': '2026-10-15'}
        sources = [archive, WP, web, second]
        original = deepcopy(sources)
        plan = build_search_plan(entities, sources, settings, now=NOW)
        self.assertEqual(plan['entities_planned'], 652)
        self.assertEqual(plan['query_count'], 652)
        self.assertNotIn('shared_discovery', plan)
        self.assertTrue(all(query['query_type'] == 'identity' and query['kind'] == 'news_query'
                            for query in plan['queries']))
        self.assertEqual({query['source_id'] for query in plan['queries']}, {'web', 'second', WP['id']})
        self.assertEqual([query['source_id'] for query in plan['queries'][:3]], ['web', 'second', WP['id']])
        self.assertEqual([query['entity_ids'] for query in plan['queries']],
                         [[str(index)] for index in range(652)])
        self.assertEqual([query['query'].strip('"') for query in plan['queries']],
                         [entity['name'] for entity in entities])
        self.assertEqual(sources, original)
        self.assertEqual(settings['preset'], 'adverse')
        self.assertTrue(any('índice general omitido' in warning for warning in plan['warnings']))

    def test_652_name_budget_does_not_starve_direct_publishers_behind_public_provider_combinations(self):
        root = Path(__file__).resolve().parents[1]
        defaults = json.loads((root / 'peps/defaults/news_sources.json').read_text(encoding='utf-8'))
        identifiers = {'mwmbl_web', 'duckduckgo_web', 'contralinea_index',
                       'aristegui_index', 'desinformemonos_index'}
        sources = [dict(source, enabled=True, terms_approved=True) for source in defaults['sources']
                   if source['id'] in identifiers]
        entities = [{'id': str(index), 'name': f'Persona Sintética Notarial {index}',
                     'context': {'state': 'Ciudad de México', 'role': 'notario público'}}
                    for index in range(652)]
        settings = {'strategy': 'targeted', 'preset': 'identity', 'execution_mode': 'quick',
                    'max_queries': 1000, 'start': '2016-01-01', 'end': '2026-10-15'}
        plan = build_search_plan(entities, sources, settings, now=NOW)
        self.assertEqual(plan['query_count'], 1000)
        first = plan['queries'][:652]
        self.assertEqual([query['entity_ids'] for query in first],
                         [[str(index)] for index in range(652)])
        self.assertTrue(all(query['query_type'] == 'identity' for query in plan['queries']))
        self.assertEqual(Counter(query['source_id'] for query in first),
                         {'mwmbl_web': 131, 'duckduckgo_web': 131, 'contralinea_index': 130,
                          'aristegui_index': 130, 'desinformemonos_index': 130})
        publisher_ids = {'contralinea_index', 'aristegui_index', 'desinformemonos_index'}
        self.assertEqual({query['source_id'] for query in first[:5]}, identifiers)
        self.assertGreaterEqual(sum(query['source_id'] in publisher_ids for query in plan['queries']), 390)
        # Changing the cap cannot change assignments already published. The
        # schedule is determined without observing HTTP responses or blocks.
        bounded = build_search_plan(entities, sources, dict(settings, max_queries=652), now=NOW)
        self.assertEqual(bounded['queries'], first)
        self.assertNotIn('shared_discovery', plan)

    def test_only_explicit_shared_strategy_walks_an_archive_and_preserves_saved_choices(self):
        archive = {'id': 'archive', 'kind': 'wordpress_api',
                   'url': 'https://publisher.invalid/wp-json/wp/v2/posts',
                   'enabled': True, 'terms_approved': True, 'download_articles': False}
        settings = {'strategy': 'targeted', 'preset': 'adverse', 'execution_mode': 'extended',
                    'session_seconds': 3600, 'start': '2020-01-01', 'end': '2022-12-31'}
        directed = build_search_plan([ENTITY], [archive], settings, now=NOW)
        self.assertEqual(directed['query_count'], 0)
        self.assertEqual(directed['config']['strategy'], 'targeted')
        self.assertTrue(any('buscador web público' in warning for warning in directed['warnings']))
        explicit = dict(settings, strategy='shared')
        shared = build_search_plan([ENTITY], [archive], explicit, now=NOW)
        self.assertEqual(shared['query_count'], 3)
        self.assertTrue(all(query['shared_index'] for query in shared['queries']))
        self.assertEqual(shared['config'], normalize_search_config(explicit, NOW))
        self.assertEqual(shared['execution']['session_seconds'], 3600)
        self.assertFalse(archive['download_articles'])

    def test_old_approved_publisher_profiles_search_652_names_in_one_interval_without_policy_changes(self):
        root = Path(__file__).resolve().parents[1]
        defaults = json.loads((root / 'peps/defaults/news_sources.json').read_text(encoding='utf-8'))
        legacy_ids = {'contralinea_index', 'aristegui_index', 'desinformemonos_index'}
        sources = [deepcopy(source) for source in defaults['sources'] if source['id'] in legacy_ids]
        self.assertEqual(len(sources), 3)
        for source in sources:
            source.pop('search_kind', None)
            source.update(enabled=True, terms_approved=True, search_max_records=50)
        original = deepcopy(sources)
        entities = [{'id': str(index), 'name': f'Persona Sintética Notarial {index}',
                     'context': {'state': 'Ciudad de México', 'role': 'notario público'}}
                    for index in range(652)]
        settings = {'strategy': 'targeted', 'preset': 'identity', 'execution_mode': 'quick',
                    'max_queries': 652, 'start': '2016-01-01', 'end': '2026-10-15'}
        plan = build_search_plan(entities, sources, settings, now=NOW)
        self.assertEqual(plan['entities_planned'], 652)
        self.assertEqual(plan['query_count'], 652)
        self.assertNotIn('shared_discovery', plan)
        self.assertEqual([query['entity_ids'] for query in plan['queries']],
                         [[str(index)] for index in range(652)])
        self.assertTrue(all(source['windows'] == 1 and source['window_unit'] == 'publisher_search_interval'
                            for source in plan['sources']))
        for query, entity in zip(plan['queries'], entities):
            self.assertEqual(query['query_type'], 'identity')
            self.assertEqual(query['result_limit'], 10)
            args = parse_qs(urlsplit(query['url']).query)
            self.assertEqual(args['search'], ['"' + entity['name'] + '"'])
            self.assertEqual(args['per_page'], ['10'])
            self.assertEqual(args['page'], ['1'])
            self.assertEqual(args['after'], ['2015-12-31T23:59:59Z'])
            self.assertEqual(args['before'], ['2026-10-16T00:00:00Z'])
        self.assertEqual(sources, original)
        # Explicit archive searches keep their original yearly sampling and
        # approved policy; planning the directed route does not replace them.
        shared = build_search_plan(entities, sources, dict(settings, strategy='shared'), now=NOW)
        self.assertEqual(shared['query_count'], 33)
        self.assertTrue(all(query['shared_index'] for query in shared['queries']))
        self.assertEqual(sources, original)

    def test_publisher_context_is_one_supplied_geography_and_role_after_the_nominal_query(self):
        entity = dict(ENTITY, context={'state': 'Ciudad de México', 'role': 'notario público'})
        queries = wordpress_queries(entity, normalize_search_config({'preset': 'adverse',
                                    'include_aliases': False}, NOW))
        self.assertEqual([query['query_type'] for query in queries[:3]], ['identity', 'context', 'focus'])
        self.assertEqual(queries[0]['query'], '"' + ENTITY['name'] + '"')
        self.assertEqual(queries[1]['query'], '"' + ENTITY['name'] + '" "Ciudad de México" "notario público"')
        self.assertNotIn('corrupción', queries[1]['query'])
        self.assertEqual(len([query for query in queries if query['query_type'] == 'context']), 1)

    def test_adverse_profile_refines_publisher_search_after_unrestricted_nominal_queries(self):
        entities = [dict(ENTITY, id=str(index), name=f'Persona Sintética Notarial {index}')
                    for index in range(4)]
        settings = {'preset': 'adverse', 'use_context': False, 'include_aliases': False,
                    'start': '2026-10-01', 'end': '2026-10-15', 'window': 'year'}
        for source in (WP, dict(GDELT, batch_size=1)):
            with self.subTest(source=source['id']):
                plan = build_search_plan(entities, [source], settings, now=NOW)
                first = plan['queries'][:len(entities)]
                self.assertTrue(all(query['query_type'] == 'identity' for query in first))
                self.assertEqual([query['query'] for query in first],
                                 ['"' + entity['name'] + '"' for entity in entities])
                self.assertTrue(any(query['query_type'] == 'focus' and 'corrupción' in query['query']
                                    for query in plan['queries'][len(entities):]))

    def test_quick_auto_yearly_first_sweep_removes_twelvefold_queue_for_8_and_300_entities(self):
        for count in (8, 300):
            with self.subTest(entities=count):
                entities = [{'id': str(i), 'name': f'Persona Pública Sintética {i}'} for i in range(count)]
                settings = {'execution_mode': 'quick', 'preset': 'identity', 'start': '2022-01-01',
                            'end': '2022-12-31', 'include_aliases': False, 'use_context': False,
                            'max_queries': 10000}
                annual = build_search_plan(entities, [WP], settings, now=NOW)
                monthly = build_search_plan(entities, [WP], dict(settings, window='month'), now=NOW)
                self.assertEqual(annual['query_count'], count)
                self.assertEqual(monthly['query_count'], count * 12)
                self.assertEqual(annual['sources'][0]['window_unit'], 'year')
                self.assertEqual(annual['sources'][0]['windows'], 1)
                self.assertTrue(all(query['split_policy'] == 'defer' for query in annual['queries']))
                for query in annual['queries']:
                    self.assertEqual((query['start'], query['end']), ('20220101000000', '20230101000000'))
                    args = parse_qs(urlsplit(query['url']).query)
                    self.assertEqual(args['year'], ['2022'])
                    self.assertNotIn('monthnum', args)
                extended = build_search_plan(entities, [WP], dict(settings, execution_mode='extended'), now=NOW)
                self.assertEqual(extended['query_count'], count * 12)
                self.assertEqual(extended['sources'][0]['window_unit'], 'month')

    def test_quick_auto_annual_payload_preserves_partial_date_bounds_and_google_filters(self):
        google = dict(WP, id='google', search_kind='google_news_rss',
                      base_url='https://news.google.com/rss/search')
        settings = {'execution_mode': 'quick', 'preset': 'identity', 'start': '2021-11-17',
                    'end': '2022-03-08', 'include_aliases': False, 'use_context': False}
        for source in (WP, google):
            with self.subTest(source=source['id']):
                plan = build_search_plan([ENTITY], [source], settings, now=NOW)
                self.assertEqual([(query['start'], query['end']) for query in plan['queries']],
                                 [('20220101000000', '20220309000000'),
                                  ('20211117000000', '20220101000000')])
                if source['search_kind'] == 'google_news_rss':
                    for query in plan['queries']:
                        args = parse_qs(urlsplit(query['url']).query)
                        first = datetime.strptime(query['start'], '%Y%m%d%H%M%S').date().isoformat()
                        last = datetime.strptime(query['end'], '%Y%m%d%H%M%S').date().isoformat()
                        self.assertIn('after:' + first, args['q'][0])
                        self.assertIn('before:' + last, args['q'][0])

    def test_corporate_queries_keep_legal_name_and_prioritize_suffix_free_editorial_name(self):
        config = normalize_search_config({'preset': 'identity', 'use_context': False}, NOW)
        for legal, core in (('CEMEX, S.A.B. de C.V.', 'CEMEX'),
                            ('Altos Hornos de México S.A.B. de C.V.', 'Altos Hornos de México'),
                            ('ACME Sociedad Anónima de Capital Variable', 'ACME')):
            with self.subTest(legal=legal):
                company = {'id': 'company', 'kind': 'empresa', 'name': legal}
                queries = wordpress_queries(company, config)
                self.assertEqual(queries[0], {'query': '"' + core + '"', 'query_type': 'identity'})
                self.assertTrue(any('"' + legal + '"' in query['query'] for query in queries))
                google_query = entity_queries(company, config)[0]['query']
                self.assertIn('"' + core + '"', google_query)
                self.assertIn('"' + legal + '"', google_query)

    def test_business_names_corporate_brands_retrieve_without_relaxing_personal_aliases(self):
        config = normalize_search_config({'preset': 'identity', 'use_context': False}, NOW)
        company = {'id': 'pemex', 'kind': 'company', 'name': 'Petróleos Mexicanos',
                   'context': {'business_names': ['Pemex', 'AHMSA', 'CEMEX', 'Grupo', 'ABC',
                       {'name': 'Revocada', 'status': 'revoked'}, {'name': 'NoValidada', 'validated': False}]}}
        for builder in (entity_queries, wordpress_queries):
            queries = [query['query'] for query in builder(company, config)]
            text = ' '.join(queries)
            for brand in ('Pemex', 'AHMSA', 'CEMEX'):
                self.assertIn('"' + brand + '"', text)
            for omitted in ('Grupo', 'ABC', 'Revocada', 'NoValidada'):
                self.assertNotIn('"' + omitted + '"', text)
            person = {**company, 'kind': 'person', 'name': 'Persona Sintética Inventada'}
            personal = ' '.join(query['query'] for query in builder(person, config))
            for brand in ('Pemex', 'AHMSA', 'CEMEX'):
                self.assertNotIn('"' + brand + '"', personal)
        disabled = normalize_search_config({'include_aliases': False, 'use_context': False}, NOW)
        self.assertTrue(all('Pemex' not in query['query'] for query in wordpress_queries(company, disabled)))

    def test_legal_suffix_normalization_is_company_only_and_does_not_emit_generic_core(self):
        config = normalize_search_config({'preset': 'identity', 'use_context': False,
                                          'include_aliases': False}, NOW)
        for builder in (entity_queries, wordpress_queries):
            person = {'id': 'person', 'name': 'Persona Sintética S.A.', 'kind': 'person'}
            queries = builder(person, config)
            self.assertTrue(all('"Persona Sintética S.A."' in query['query'] for query in queries))
            self.assertFalse(any('"Persona Sintética"' in query['query'] for query in queries))
            generic = builder({'id': 'company', 'name': 'Grupo S.A.', 'kind': 'company'}, config)
            self.assertFalse(any('"Grupo"' in query['query'] for query in generic))
            company = builder({'id': 'company', 'name': 'CEMEX S.A.B. de C.V.', 'kind': 'company'}, config)
            self.assertIn('"CEMEX"', company[0]['query'])

    def test_gdelt_first_sweep_corporate_cores_and_brands_respect_character_and_query_budgets(self):
        entities = [{'id': 'cemex', 'kind': 'company', 'name': 'CEMEX, S.A.B. de C.V.'},
                    {'id': 'pemex', 'kind': 'company', 'name': 'Petróleos Mexicanos',
                     'context': {'business_names': ['Pemex']}},
                    {'id': 'ahmsa', 'kind': 'company', 'name': 'Altos Hornos de México, S.A.B. de C.V.',
                     'context': {'business_names': ['AHMSA']}}]
        settings = {'preset': 'identity', 'use_context': False, 'max_queries': 100,
                    'start': '2026-10-01', 'end': '2026-10-15'}
        source = dict(GDELT, max_query_chars=250, batch_size=6)
        plan = build_search_plan(entities, [source], settings, now=NOW)
        first = plan['queries'][0]
        for name in ('CEMEX', 'Petróleos Mexicanos', 'Pemex', 'Altos Hornos de México', 'AHMSA'):
            self.assertIn('"' + name + '"', first['query'])
        self.assertNotIn('S.A.B.', first['query'])
        all_queries = ' '.join(query['query'] for query in plan['queries'])
        self.assertIn('"CEMEX, S.A.B. de C.V."', all_queries)
        self.assertIn('"Altos Hornos de México, S.A.B. de C.V."', all_queries)
        for query in plan['queries']:
            self.assertLessEqual(len(query['query']), 250)
            self.assertLessEqual(query['query'].count('('), 1)
        limited = build_search_plan(entities, [source], dict(settings, max_queries=1), now=NOW)
        self.assertEqual(limited['query_count'], 1)
        self.assertTrue(limited['truncated'])
        self.assertEqual(set(limited['queries'][0]['entity_ids']), {'cemex', 'pemex', 'ahmsa'})

    def test_legacy_settings_keep_original_period_preset_and_900_second_budget(self):
        legacy = {'preset': 'identity', 'start': '2020-01-01', 'end': '2020-12-31', 'include_aliases': False}
        normalized = normalize_search_config(legacy, NOW)
        self.assertEqual(normalized['execution_mode'], 'extended')
        self.assertEqual(normalized['session_seconds'], 900)
        self.assertTrue(all(normalized[key] == value for key, value in legacy.items()))
        self.assertNotIn('execution_mode', legacy)
        merged = merge_source_defaults({'sources': [WP]}, {'sources': [WP], 'search': {'execution_mode': 'quick', 'preset': 'adverse'}})
        self.assertEqual(merged['search']['execution_mode'], 'extended')
        self.assertEqual(merged['search']['preset'], 'identity')

    def test_new_install_defaults_are_quick_nominal_without_source_permissions(self):
        news = json.loads((Path(__file__).resolve().parents[1] / 'peps/defaults/news_sources.json').read_text(encoding='utf-8'))
        settings = normalize_search_config(news['search'], NOW)
        self.assertEqual((settings['execution_mode'], settings['session_seconds'], settings['preset']), ('quick', 120, 'identity'))
        self.assertTrue(all(source.get('enabled') is False and source.get('terms_approved') is False for source in news['sources']))
        for invalid in ({'execution_mode': 'imaginary'}, {'execution_mode': 'quick', 'session_seconds': 900},
                        {'execution_mode': 'quick', 'session_seconds': True}, {'execution_mode': 'extended', 'session_seconds': 86400}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                normalize_search_config(invalid, NOW)
        for seconds in (900, 1800, 3600, 7200):
            self.assertEqual(normalize_search_config({'execution_mode': 'extended', 'session_seconds': seconds}, NOW)['session_seconds'], seconds)

    def test_publisher_search_additions_preserve_existing_catalogue_and_require_permission(self):
        root = Path(__file__).resolve().parents[1]
        defaults = json.loads((root / 'peps/defaults/news_sources.json').read_text(encoding='utf-8'))
        self.assertEqual(defaults, json.loads((root / 'config/news_sources.example.json').read_text(encoding='utf-8')))
        additions = [source for source in defaults['sources'] if source['id'] in ('contralinea_search', 'aristegui_search')]
        self.assertEqual([source['id'] for source in additions], ['contralinea_search', 'aristegui_search'])
        existing = deepcopy([source for source in defaults['sources'] if source['id'] not in
                             ('contralinea_search', 'aristegui_search') and source['kind'] not in ('wordpress_api', 'sitemap')])
        existing_rss = next(source for source in existing if source['id'] == 'aristegui_rss')
        existing_rss.update(enabled=True, terms_approved=True, download_articles=True, max_bytes=12345)
        merged = merge_source_defaults({'sources': existing}, defaults)
        self.assertEqual(merged['sources'][:len(existing)], existing)
        self.assertEqual(build_search_plan([ENTITY], additions, now=NOW)['query_count'], 0)
        for source, count in zip(additions, (6, 15)):
            with self.subTest(source=source['id']):
                self.assertTrue(all(source[field] is False for field in
                    ('enabled', 'terms_approved', 'download_articles', 'article_terms_approved', 'body_terms_approved')))
                self.assertEqual(source['timeout_seconds'], 15)
                self.assertEqual(source['min_interval_seconds'], 5)
                approved = dict(source, enabled=True, terms_approved=True)
                plan = build_search_plan([ENTITY], [approved], {'start': '2020-01-01', 'end': '2020-12-31',
                    'include_aliases': False, 'use_context': False, 'window': 'year'}, now=NOW)
                query = plan['queries'][0]
                self.assertEqual(query['result_limit'], count)
                self.assertEqual(urlsplit(query['url']).hostname, urlsplit(source['search_url']).hostname)
                self.assertEqual(parse_qs(urlsplit(query['url']).query)['year'], ['2020'])

    def test_shared_300_entities_use_yearly_pages_not_300_queries_and_obey_budget(self):
        source = dict(id='shared', kind='wordpress_api', url='https://example.org/wp-json/wp/v2/posts',
                      allowed_hosts=['example.org'], enabled=True, terms_approved=True)
        entities = [dict(id=str(i), name=f'Persona Pública Sintética {i}') for i in range(300)]
        plan = build_search_plan(entities, [source], {'strategy': 'shared', 'start': '2016-01-01',
            'end': '2026-01-15', 'execution_mode': 'quick'}, now=NOW)
        self.assertEqual(plan['entities_total'], 300)
        self.assertEqual(plan['entities_planned'], 0)
        self.assertEqual(plan['shared_discovery']['initial_pages'], 11)
        self.assertEqual(plan['shared_discovery']['max_pages_per_source'], 24)
        self.assertEqual(len(plan['queries']), 11)
        for query in plan['queries']:
            self.assertEqual(query['kind'], 'news_feed')
            args = parse_qs(urlsplit(query['url']).query)
            self.assertNotIn('search', args)
            self.assertNotIn('content', args['_fields'][0])
            self.assertEqual(args['per_page'], ['100'])
        limited = build_search_plan(entities, [source], dict(plan['config'], max_queries=2), now=NOW)
        self.assertEqual(len(limited['queries']), 2)
        self.assertTrue(limited['truncated'])
        complete_years = build_search_plan(entities, [source], dict(plan['config'], start='2020-01-01', end='2025-12-31'), now=NOW)
        self.assertEqual(complete_years['shared_discovery']['initial_pages'], 6)
        self.assertFalse(any('años antiguos sin planificar' in warning or 'nombres canónicos' in warning
                             for warning in complete_years['warnings']))

    def test_shared_sources_are_added_disabled_and_old_permissions_remain_exact(self):
        defaults = json.loads((Path(__file__).resolve().parents[1] / 'peps/defaults/news_sources.json').read_text(encoding='utf-8'))
        legacy_shared_ids = {'contralinea_index', 'aristegui_index', 'desinformemonos_index',
                             'contralinea_sitemap', 'mcci_sitemap', 'articulo19_sitemap'}
        # Keep this historical migration fixture bounded to its original
        # shared sources; optional directed regional adapters have their own
        # catalogue and migration regression below.
        sources = [source for source in defaults['sources'] if source['id'] in legacy_shared_ids]
        self.assertEqual(len(sources), 6)
        for source in sources:
            for permission in ('enabled', 'terms_approved', 'download_articles', 'article_terms_approved'):
                self.assertFalse(source[permission])
        old = {'sources': [dict(id='fixture', enabled=True, terms_approved=True, kind='rss', max_bytes=123)]}
        merged = merge_source_defaults(old, defaults)
        self.assertEqual(merged['sources'][0], old['sources'][0])

    def test_quick_gdelt_batches_are_flat_bounded_and_keep_homonym_ids(self):
        entities = [dict(id=str(index), name='Persona Sintética Inventada ' + str(index)) for index in range(18)]
        entities.append(dict(entities[0], id='homonym'))
        source = dict(GDELT, max_query_chars=250, batch_size=6, source_lang='spanish', source_country='mexico')
        plan = build_search_plan(entities, [source], {'execution_mode': 'quick', 'preset': 'adverse',
                                'start': '2026-07-01', 'end': '2026-10-15', 'include_aliases': False, 'use_context': False}, now=NOW)
        self.assertEqual(plan['sources'][0]['windows'], 1)
        self.assertEqual(plan['entities_planned'], 19)
        first = plan['queries'][0]
        self.assertIn('homonym', first['entity_ids'])
        self.assertGreater(len(first['names']), 1)
        self.assertEqual(first['query'].count('('), 1)
        for query in plan['queries']:
            self.assertLessEqual(len(query['query']), 250)
            self.assertLessEqual(len(query['names']), 6)
            self.assertEqual(query['split_policy'], 'defer')
            depth = maximum = 0
            for character in query['query']:
                depth += (character == '(') - (character == ')')
                maximum = max(maximum, depth)
            self.assertLessEqual(maximum, 1)
            self.assertTrue(all('"' + name + '"' in query['query'] for name in query['names']))
            if query['query_type'] == 'identity':
                self.assertFalse(any(term in query['query'] for term in ('corrupción', 'fraude', 'sanción', 'lavado de dinero')))
            else:
                self.assertEqual(query['query_type'], 'focus')
                self.assertTrue(any(term in query['query'] for term in ('corrupción', 'fraude', 'sanción', 'lavado de dinero')))
        self.assertIn('no garantiza', plan['execution']['scope_note'])

    def test_both_modes_batch_first_sweep_without_claiming_execution(self):
        entities = [dict(id=str(index), name='Persona Sintética ' + str(index), aliases=['Alias Inventado ' + str(index)],
                         context={'state': 'Oaxaca'}) for index in range(300)]
        source = dict(GDELT, max_query_chars=250, batch_size=6, min_interval_seconds=5,
                      source_lang='spanish', source_country='mexico')
        settings = {'execution_mode': 'quick', 'preset': 'adverse', 'start': '2016-01-01', 'end': '2026-10-15'}
        quick = build_search_plan(entities, [WP, source], settings, now=NOW)
        extended = build_search_plan(entities, [WP, source], dict(settings, execution_mode='extended'), now=NOW)
        self.assertEqual(extended['execution']['session_seconds'], 900)
        extended_scheduled, extended_consulted = {}, set()
        for query in extended['queries']:
            host = urlsplit(query['url']).hostname
            due = extended_scheduled.get(host, 0)
            extended_scheduled[host] = due + 5
            if due < 120:
                extended_consulted.update(query['entity_ids'])
                self.assertNotIn('Alias Inventado', query['query'])
                self.assertNotIn('"Oaxaca"', query['query'])
        self.assertGreaterEqual(len(extended_consulted), 120)
        scheduled, consulted = {}, set()
        for query in quick['queries']:
            host = urlsplit(query['url']).hostname
            due = scheduled.get(host, 0)
            scheduled[host] = due + 5
            if due < 120:
                consulted.update(query['entity_ids'])
                self.assertNotIn('Alias Inventado', query['query'])
                self.assertNotIn('"Oaxaca"', query['query'])
        self.assertEqual(quick['estimated_entities_first_session'], len(consulted))
        self.assertGreaterEqual(len(consulted), 120)
        self.assertLess(len(consulted), 300)
        self.assertTrue(all(source['entities_planned'] == 300 for source in quick['sources']))
        self.assertIn('No son consultas ejecutadas', quick['first_session_estimate_note'])
        self.assertIn('no equivale', quick['entity_count_note'])
        wordpress = [query for query in quick['queries'] if query['source_id'] == WP['id']]
        self.assertTrue(all(len(query['entity_ids']) == 1 and ' OR ' not in query['query'] for query in wordpress))

    def test_extended_300_entities_use_bounded_batches_and_expand_only_saturated_windows(self):
        entities = [dict(id=str(index), name=f'Persona Sintética Apellido {index:04d}') for index in range(300)]
        entities.append(dict(entities[0], id='homonym'))
        source = dict(GDELT, max_query_chars=250, batch_size=6, source_lang='spanish', source_country='mexico')
        settings = {'execution_mode': 'extended', 'session_seconds': 3600, 'preset': 'identity',
                    'start': '2016-01-01', 'end': '2026-10-15', 'include_aliases': False, 'use_context': False}
        plan = build_search_plan(entities, [source], settings, now=NOW)
        # Before batching and lazy windows this workload needed at least
        # 300 names x 3 monthly windows. A neutral identity search stays broad.
        self.assertLessEqual(plan['query_count'], 75)
        self.assertEqual(plan['entities_planned'], 301)
        self.assertFalse(plan['truncated'])
        self.assertEqual(plan['sources'][0]['windows'], 1)
        self.assertEqual(plan['sources'][0]['window_unit'], 'recent_interval')
        self.assertIn('homonym', plan['queries'][0]['entity_ids'])
        for query in plan['queries']:
            self.assertLessEqual(len(query['query']), 250)
            self.assertLessEqual(len(query['names']), 6)
            self.assertEqual(query['split_policy'], 'expand')
            self.assertNotIn('corrupción', query['query'])
            self.assertEqual(query['query'].count('('), 1)
            args = parse_qs(urlsplit(query['url']).query)
            self.assertEqual(args['startdatetime'], [query['start']])
            self.assertEqual(args['enddatetime'], [query['end']])

    def test_extended_batches_prioritize_canonical_names_before_aliases_and_context(self):
        entities = [dict(id=str(index), name=f'Persona Sintética {index:04d}',
                         aliases=[f'Alias Inventado {index:04d}'], context={'state': 'Oaxaca'}) for index in range(18)]
        source = dict(GDELT, max_query_chars=250, batch_size=6)
        plan = build_search_plan(entities, [source], {'execution_mode': 'extended', 'preset': 'adverse',
                                'start': '2026-07-01', 'end': '2026-10-15'}, now=NOW)
        canonical, secondary = [], []
        for index, query in enumerate(plan['queries']):
            (canonical if query['query_type'] == 'identity' else secondary).append((index, query))
        self.assertTrue(secondary)
        self.assertLess(canonical[-1][0], secondary[0][0])
        self.assertEqual({identifier for _, query in canonical for identifier in query['entity_ids']},
                         {str(index) for index in range(18)})
        self.assertTrue(any('Alias Inventado' in query['query'] for _, query in secondary))
        self.assertTrue(any('"Oaxaca"' in query['query'] for _, query in secondary))
        self.assertTrue(all(query['split_policy'] == 'expand' for query in plan['queries']))

    def test_shared_name_queries_keep_all_distinct_identities_in_input_order(self):
        entities = [dict(ENTITY, id=str(index)) for index in range(10000)]
        plan = build_search_plan(entities, [WP], {'start': '2020-01-01', 'end': '2020-12-31',
                                                'include_aliases': False, 'use_context': False}, now=NOW)
        self.assertEqual(plan['query_count'], 12)
        self.assertEqual(plan['queries'][0]['entity_ids'], [str(index) for index in range(10000)])
    def test_names_aliases_and_context_remain_literal_and_presets_never_become_keyword_only(self):
        for preset in ('identity', 'public_role', 'adverse'):
            config = normalize_search_config({'preset': preset}, NOW)
            queries = entity_queries(ENTITY, config)
            self.assertTrue(queries)
            self.assertTrue(any('"Alias Sintético"' in item['query'] for item in queries))
            self.assertTrue(any('"Oaxaca"' in item['query'] for item in queries))
            self.assertTrue(all('"Persona Sintética Inventada"' in item['query'] or '"Alias Sintético"' in item['query'] for item in queries))

    def test_targeted_skips_general_feeds_and_robots_blocked_search_sources(self):
        sources = [dict(WP, search_kind=None), dict(WP, id='blocked', search_kind='google_news_rss', automated_search=False)]
        plan = build_search_plan([ENTITY], sources, now=NOW)
        self.assertEqual(plan['query_count'], 0)
        self.assertTrue(any('robots' in warning for warning in plan['warnings']))
        self.assertTrue(any('feed general omitido' in warning for warning in plan['warnings']))

    def test_wordpress_name_queries_have_year_month_and_bounded_calendar_splits(self):
        config = {'start': '2020-01-01', 'end': '2020-12-31', 'window': 'year', 'include_aliases': False, 'use_context': False}
        plan = build_search_plan([ENTITY], [WP], config, now=NOW)
        self.assertEqual(plan['query_count'], 1)
        task = plan['queries'][0]
        args = parse_qs(urlsplit(task['url']).query)
        self.assertEqual(args['s'], ['"Persona Sintética Inventada"'])
        self.assertEqual(args['year'], ['2020'])
        self.assertNotIn('monthnum', args)
        windows = split_query_windows(WP, task)
        self.assertEqual(len(windows), 12)
        january = dict(task, start=windows[0][0], end=windows[0][1])
        self.assertEqual(parse_qs(urlsplit(query_url(WP, january)).query)['monthnum'], ['1'])
        days = split_query_windows(WP, january)
        self.assertEqual(len(days), 31)
        self.assertEqual(split_query_windows(WP, dict(task, start=days[0][0], end=days[0][1])), [])
        self.assertEqual(task['result_limit'], 10)

    def test_gdelt_explicitly_clips_its_recent_scope_and_never_claims_old_history(self):
        plan = build_search_plan([ENTITY], [GDELT], {'start': '2010-01-01', 'end': '2026-10-15'}, now=NOW)
        source = plan['sources'][0]
        self.assertEqual(source['scope'], 'recent_rolling_window')
        self.assertFalse(source['historical'])
        self.assertGreaterEqual(source['start'][:10], '2026-07-15')
        self.assertTrue(any('3 meses' in warning for warning in plan['warnings']))
        old = build_search_plan([ENTITY], [GDELT], {'start': '2020-01-01', 'end': '2020-12-31'}, now=NOW)
        self.assertEqual(old['query_count'], 0)

    def test_gdelt_short_alias_and_context_never_create_nested_or_groups(self):
        entity = dict(ENTITY, aliases=['ABC'])
        plan = build_search_plan([entity], [GDELT],
                                 {'start': '2026-10-01', 'end': '2026-10-15', 'preset': 'adverse'}, now=NOW)
        self.assertTrue(plan['queries'])
        queries = [item['query'] for item in plan['queries']]
        self.assertTrue(any('"ABC" "Oaxaca"' in query for query in queries))
        for query in queries:
            self.assertNotIn('(', query)
            self.assertNotIn(' OR ', query)
            self.assertTrue('"Persona Sintética Inventada"' in query or '"ABC"' in query)

    def test_326_entities_share_budget_before_older_windows_and_coverage_is_explicit(self):
        entities = [dict(id=str(index), name='Persona Sintética ' + str(index)) for index in range(326)]
        plan = build_search_plan(entities, [WP], {'include_aliases': False, 'use_context': False}, now=NOW)
        self.assertEqual(plan['entities_total'], 326)
        self.assertEqual(plan['entities_covered'], 326)
        self.assertEqual(plan['query_count'], 1000)
        self.assertEqual(plan['queries_required'], 3586)
        self.assertTrue(plan['truncated'])
        self.assertFalse(plan['sources'][0]['complete_archive'])
        self.assertGreaterEqual(plan['estimated_schedule_seconds'], 4995)
        self.assertTrue(any('Presupuesto' in warning for warning in plan['warnings']))

    def test_identical_name_queries_merge_homonym_ids_instead_of_losing_one(self):
        entities = [dict(id='one', name='Persona Homónima'), dict(id='two', name='Persona Homónima')]
        plan = build_search_plan(entities, [WP], {'start': '2020-01-01', 'end': '2020-12-31', 'window': 'year'}, now=NOW)
        self.assertEqual(plan['query_count'], 1)
        self.assertEqual(plan['queries'][0]['entity_ids'], ['one', 'two'])
        self.assertEqual(plan['entities_covered'], 2)

    def test_feeds_strategy_is_explicit_and_cannot_claim_historical_search(self):
        source = dict(WP, search_kind=None, url='https://publisher.invalid/feed/')
        plan = build_search_plan([ENTITY], [source], {'strategy': 'feeds'}, now=NOW)
        self.assertEqual(plan['queries'][0]['kind'], 'news_feed')
        self.assertEqual(plan['sources'][0]['scope'], 'current_feed_only')
        self.assertFalse(plan['sources'][0]['historical'])

    def test_invalid_ranges_boolean_strings_and_excessive_budgets_are_rejected(self):
        for settings in ({'start': '2027-01-01'}, {'end': '2027-01-01'}, {'max_queries': 10001},
                         {'include_aliases': 'false'}, {'preset': 'invented'}, {'start': '2026-02-30'}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                normalize_search_config(settings, NOW)

    def test_permission_migration_preserves_user_choices_and_adds_sources_disabled(self):
        old = {'sources': [dict(WP, enabled=True, terms_approved=True, download_articles=True, max_query_chars=321)]}
        defaults = {'sources': [dict(WP, max_query_chars=500), dict(WP, id='new')], 'search': {'strategy': 'targeted'}}
        merged = merge_source_defaults(old, defaults)
        self.assertEqual(merged['sources'][0]['max_query_chars'], 321)
        self.assertTrue(merged['sources'][0]['download_articles'])
        self.assertTrue(merged['sources'][0]['terms_approved'])
        self.assertFalse(merged['sources'][1]['enabled'])
        self.assertFalse(merged['sources'][1]['terms_approved'])
        self.assertNotIn('search', old)


class SearchQueueIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.engine = Engine(self.temp.name)
        self.news = NewsAcquirer(self.engine)
        self.engine.store.import_entities([ENTITY])

    def configure(self, sources):
        self.news.config_path.write_text(json.dumps({'sources': sources, 'search': {'strategy': 'targeted'}}), encoding='utf-8')

    def test_preview_cache_avoids_rereading_the_bank_and_invalidates_on_edits(self):
        self.configure([WP])
        with patch.object(self.engine.store, 'iter_entities', wraps=self.engine.store.iter_entities) as reads:
            first = self.news.search_plan()
            first['queries'].clear()
            self.assertTrue(self.news.search_plan()['queries'])
            self.assertEqual(reads.call_count, 1)
            self.engine.store.import_entities([dict(ENTITY, id='new', name='Otra Persona Sintética')])
            self.assertEqual(self.news.search_plan()['entities_total'], 2)
            self.assertEqual(reads.call_count, 2)
            self.configure([dict(WP, search_url='https://changed.invalid/')])
            self.news.search_plan()
            self.assertEqual(reads.call_count, 3)

    def test_saturation_children_have_correct_urls_and_reserved_intervals_without_duplicate_jobs(self):
        self.configure([WP])
        self.news.set_search_config({'start': '2020-01-01', 'end': '2020-12-31', 'window': 'year',
                                    'include_aliases': False, 'use_context': False})
        self.news.queue('split-reservations')
        parent = self.engine.store.claim(kind_prefix='news_')
        self.assertTrue(self.news._split_saturated(parent, WP))
        with self.engine.store.connection() as con:
            children = [(row['id'], json.loads(row['payload']), row['due']) for row in con.execute(
                "SELECT * FROM jobs WHERE id!=? ORDER BY due", (parent['id'],))]
        self.assertEqual(len(children), 12)
        for _, payload, _ in children:
            self.assertEqual(payload['url'], query_url(WP, payload))
        self.assertTrue(all(second[2] - first[2] >= 5 for first, second in zip(children, children[1:])))
        self.news._split_saturated(parent, WP)
        with self.engine.store.connection() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM jobs').fetchone()[0], 13)

    def test_extended_gdelt_saturation_preserves_batched_names_ids_and_query(self):
        self.engine.store.import_entities([dict(ENTITY, id='second', name='Otra Persona Sintética')])
        source = dict(GDELT, max_query_chars=250, batch_size=6, min_interval_seconds=5)
        self.configure([source])
        self.news.set_search_config({'execution_mode': 'extended', 'preset': 'identity',
                                    'start': '2026-08-01', 'end': '2026-10-15',
                                    'include_aliases': False, 'use_context': False})
        self.news.queue('gdelt-extended-batches')
        parent = self.engine.store.claim(kind_prefix='news_')
        self.assertEqual(len(parent['payload']['entity_ids']), 2)
        self.assertEqual(parent['payload']['split_policy'], 'expand')
        self.assertTrue(self.news._split_saturated(parent, source))
        with self.engine.store.connection() as con:
            children = [json.loads(row['payload']) for row in con.execute(
                'SELECT j.payload FROM jobs j JOIN news_query_windows w ON w.job_id=j.id '
                'WHERE w.parent_id=? ORDER BY w.window_start', (parent['id'],))]
        self.assertEqual(len(children), 2)
        self.assertEqual(children[0]['start'], parent['payload']['start'])
        self.assertEqual(children[0]['end'], children[1]['start'])
        self.assertEqual(children[1]['end'], parent['payload']['end'])
        for payload in children:
            self.assertEqual(payload['entity_ids'], parent['payload']['entity_ids'])
            self.assertEqual(payload['names'], parent['payload']['names'])
            self.assertEqual(payload['query'], parent['payload']['query'])
            self.assertEqual(payload['url'], query_url(source, payload))

    def test_search_settings_persist_and_emit_the_revision_event(self):
        self.configure([WP])
        config = self.news.set_search_config({'start': '2020-01-01', 'end': '2020-12-31', 'window': 'year'})
        self.assertEqual(self.news.search_config(), config)
        with self.engine.store.connection() as con:
            self.assertEqual(con.execute("SELECT count(*) FROM audit WHERE action='news_search_configured'").fetchone()[0], 1)
        self.assertGreater(self.news.search_plan()['query_count'], 0)
        before = self.news.config_path.stat().st_mtime_ns
        self.news.set_search_config(config)
        self.assertEqual(self.news.config_path.stat().st_mtime_ns, before)
        with self.engine.store.connection() as con:
            self.assertEqual(con.execute("SELECT count(*) FROM audit WHERE action='news_search_configured'").fetchone()[0], 1)

    def test_queue_is_idempotent_and_independent_hosts_do_not_wait_for_each_other(self):
        self.configure([WP, dict(WP, id='second', search_url='https://second.invalid/', base_url='https://second.invalid/')])
        self.news.set_search_config({'start': '2020-01-01', 'end': '2020-12-31', 'window': 'year', 'include_aliases': False, 'use_context': False})
        first = self.news.queue('stable-window')
        second = self.news.queue('stable-window')
        self.assertEqual(first['tasks'], 2)
        self.assertEqual(first, second)
        with self.engine.store.connection() as con:
            jobs = list(con.execute('SELECT payload,due FROM jobs'))
            self.assertEqual(len(jobs), 2)
            self.assertEqual(jobs[0]['due'], jobs[1]['due'])
            self.assertEqual(con.execute('SELECT count(*) FROM news_query_windows').fetchone()[0], 2)
        self.assertEqual(self.engine.store.stats()['scores'], 0)

    def test_plan_api_and_persisted_queue_receipts_have_bounded_previews(self):
        self.configure([WP])
        self.news.set_search_config({'start': '2010-01-01', 'end': '2020-12-31', 'window': 'year'})
        preview = self.news.search_plan()
        self.assertGreater(preview['query_count'], 10)
        self.assertEqual(len(preview['queries']), 10)
        self.assertTrue(preview['queries_preview_truncated'])
        queued = self.news.queue('bounded-receipt')
        self.assertEqual(queued['tasks'], preview['query_count'])
        self.assertEqual(len(queued['search_plan']['queries']), 3)
        with self.engine.store.connection() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM jobs').fetchone()[0], queued['tasks'])


if __name__ == '__main__':
    unittest.main()
