"""Directed historical discovery plans: no external requests in tests."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from peps.engine import Engine
from peps.news import NewsAcquirer
from peps.news_search import (build_search_plan, default_search_config, entity_queries,
                              merge_source_defaults, normalize_search_config, query_url, split_query_windows)

NOW = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)
ENTITY = {'id': 'synthetic', 'name': 'Persona Sintética Inventada', 'aliases': ['Alias Sintético'],
          'context': {'state': 'Oaxaca', 'role': 'alcalde'}}
WP = {'id': 'publisher', 'kind': 'rss', 'search_kind': 'wordpress_rss', 'search_url': 'https://publisher.invalid/',
      'base_url': 'https://publisher.invalid/', 'enabled': True, 'terms_approved': True,
      'search_max_records': 10, 'min_window_seconds': 86400, 'min_interval_seconds': 5}
GDELT = {'id': 'index', 'kind': 'gdelt', 'base_url': 'https://api.gdeltproject.org/api/v2/doc/doc',
         'enabled': True, 'terms_approved': True, 'max_records': 250, 'max_history_days': 90}


class SearchPlanTests(unittest.TestCase):
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

    def test_new_install_defaults_are_quick_adverse_without_source_permissions(self):
        news = json.loads((Path(__file__).resolve().parents[1] / 'peps/defaults/news_sources.json').read_text(encoding='utf-8'))
        settings = normalize_search_config(news['search'], NOW)
        self.assertEqual((settings['execution_mode'], settings['session_seconds'], settings['preset']), ('quick', 120, 'adverse'))
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
        sources = [source for source in defaults['sources'] if source['kind'] in ('wordpress_api', 'sitemap')]
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
