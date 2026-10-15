"""Directed historical discovery plans: no external requests in tests."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit

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
