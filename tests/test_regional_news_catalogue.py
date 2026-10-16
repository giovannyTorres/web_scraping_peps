"""Optional public publisher adapters never grant source or body permission."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit

from peps.engine import Engine
from peps.news import NewsAcquirer
from peps.news_search import build_search_plan, merge_source_defaults
from peps.shared_crawl import supports_wordpress_search


ROOT = Path(__file__).resolve().parents[1]
REGIONAL_IDS = {'plazadearmas_search', 'elqueretano_search', 'laevidencia_search', 'zonadocs_search',
                'rioaxaca_search', 'periodicocontexto_search', 'avimexnews_search', 'notisistema_search'}
PERMISSIONS = ('enabled', 'terms_approved', 'download_articles', 'article_terms_approved', 'body_terms_approved')


class RegionalNewsCatalogueTests(unittest.TestCase):
    def setUp(self):
        self.defaults = json.loads((ROOT / 'peps/defaults/news_sources.json').read_text(encoding='utf-8'))
        self.additions = [source for source in self.defaults['sources'] if source['id'] in REGIONAL_IDS]

    def test_eight_public_adapters_are_disabled_clean_and_exposed_to_the_normal_source_ui(self):
        self.assertEqual(self.defaults, json.loads((ROOT / 'config/news_sources.example.json').read_text(encoding='utf-8')))
        self.assertEqual({source['id'] for source in self.additions}, REGIONAL_IDS)
        self.assertEqual(len(self.additions), 8)
        self.assertEqual(len({source['id'] for source in self.defaults['sources']}), len(self.defaults['sources']))
        forbidden = {'technical_probe', 'technical_status', 'backtest_partition', 'body_use_review', 'query_url'}
        for source in self.additions:
            with self.subTest(source=source['id']):
                self.assertTrue(all(source[field] is False for field in PERMISSIONS))
                self.assertTrue(source['robots_required'])
                self.assertTrue(source['discovery'])
                self.assertTrue(supports_wordpress_search(source))
                self.assertEqual(source['min_interval_seconds'], 5)
                self.assertEqual(source['timeout_seconds'], 12)
                self.assertEqual(source['max_bytes'], 2_000_000)
                self.assertEqual(source['search_max_records'], 50)
                self.assertTrue(source['coverage_region'])
                self.assertTrue(source['review_note'])
                self.assertTrue(source['terms_url'].startswith('https://'))
                self.assertFalse(forbidden & source.keys())
                self.assertNotIn('runtime/', json.dumps(source))
        with tempfile.TemporaryDirectory() as directory:
            news = NewsAcquirer(Engine(directory))
            self.assertEqual({source['id'] for source in news.sources()} & REGIONAL_IDS, REGIONAL_IDS)
            self.assertEqual(news.search_plan()['query_count'], 0)

    def test_permission_review_enables_nominal_queries_without_enabling_bodies_or_general_indices(self):
        entity = {'id': 'synthetic', 'name': 'Persona Sintética Notarial', 'kind': 'person'}
        self.assertEqual(build_search_plan([entity], self.additions)['query_count'], 0)
        reviewed = [dict(source, enabled=True, terms_approved=True) for source in self.additions]
        plan = build_search_plan([entity], reviewed, {'strategy': 'targeted', 'preset': 'identity',
            'execution_mode': 'extended', 'session_seconds': 900, 'include_aliases': False, 'use_context': False,
            'start': '2000-01-01', 'end': '2026-10-16'})
        self.assertEqual(len(plan['queries']), 8)
        self.assertNotIn('shared_discovery', plan)
        for query in plan['queries']:
            self.assertEqual(query['kind'], 'news_query')
            self.assertEqual(query['query_type'], 'identity')
            self.assertEqual(parse_qs(urlsplit(query['url']).query)['per_page'], ['50'])
            self.assertIn('search=', query['url'])
        notisistema = next(query for query in plan['queries'] if query['source_id'] == 'notisistema_search')
        self.assertEqual(urlsplit(notisistema['url']).path, '/noticias/wp-json/wp/v2/posts')
        self.assertTrue(all(not source['download_articles'] and not source['body_terms_approved'] for source in reviewed))

    def test_additive_upgrade_preserves_existing_permissions_and_user_order_exactly(self):
        existing = deepcopy(self.defaults)
        existing['sources'] = [source for source in existing['sources'] if source['id'] not in REGIONAL_IDS]
        existing['sources'][0].update(enabled=True, terms_approved=True, max_bytes=12345)
        original = deepcopy(existing)
        merged = merge_source_defaults(existing, self.defaults)
        self.assertEqual(merged['sources'][:len(existing['sources'])], existing['sources'])
        self.assertEqual(existing, original)
        self.assertEqual({source['id'] for source in merged['sources'][len(existing['sources']):]}, REGIONAL_IDS)
        self.assertTrue(all(source[permission] is False for source in merged['sources'][len(existing['sources']):]
                            for permission in PERMISSIONS))
        # Existing discovery-only profiles are not silently converted into
        # body-enabled profiles by the regional catalogue addition.
        for identifier in ('periodicocentral_probe', 'alcaldesdemexico_probe'):
            self.assertFalse(any(source['id'] == identifier and source.get('body_terms_approved')
                                 for source in merged['sources']))


if __name__ == '__main__':
    unittest.main()
