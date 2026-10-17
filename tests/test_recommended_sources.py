"""Catalogue promotion and persisted-choice regressions; no source requests."""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from urllib.parse import parse_qs, urlsplit

from peps.api_permissions import documented_search_api
from peps.news_search import merge_source_defaults, normalize_search_config
from peps.public_search import public_search_url


ROOT = Path(__file__).resolve().parents[1]
PERMISSIONS = ('enabled', 'terms_approved', 'download_articles',
               'article_terms_approved', 'body_terms_approved')


class RecommendedSourcesTests(unittest.TestCase):
    def setUp(self):
        self.defaults = json.loads((ROOT / 'peps/defaults/news_sources.json').read_text())
        self.sources = {source['id']: source for source in self.defaults['sources']}

    def test_new_install_has_reviewed_api_and_independent_reading_both_off(self):
        api = self.sources['lumy_general_v1']
        reader = self.sources['public_research_articles']
        self.assertEqual(self.defaults, json.loads((ROOT / 'config/news_sources.example.json').read_text()))
        for source in (api, reader):
            self.assertTrue(all(source[key] is False for key in PERMISSIONS))
            self.assertIs(source['robots_required'], True)
        self.assertEqual(self.defaults['search']['strategy'], 'targeted')
        self.assertIs(api['discovery_only'], True)
        self.assertEqual(api['documented_api_permission']['review_receipt_sha256'],
                         'bfd752831c2dfdfde287d0079d7770b3cad54be42b4a5c5d5f8f6243eb72f110')
        self.assertEqual(reader['public_research_scope'], 'personal_local_research')
        self.assertIs(reader['discover_public_article_hosts'], True)
        self.assertIs(reader['discovery'], False)
        self.assertIs(reader['automated_search'], False)
        self.assertEqual((reader['allowed_hosts'], reader['article_allowed_hosts']), ([], []))
        self.assertNotIn('documented_api_permission', reader)

    def test_api_query_uses_unquoted_yandex_without_category_and_grant_cannot_cover_article(self):
        source = self.sources['lumy_general_v1']
        query = public_search_url('Persona Sintética de Prueba', source)
        params = parse_qs(urlsplit(query).query)
        self.assertEqual(params['q'], ['Persona Sintética de Prueba'])
        self.assertEqual(params['engines'], ['yandex'])
        self.assertEqual(params['format'], ['json'])
        self.assertNotIn('categories', params)
        self.assertTrue(documented_search_api(query, source))
        self.assertFalse(documented_search_api('https://publisher.invalid/article', source))
        self.assertFalse(documented_search_api(query, dict(source, download_articles=True)))

    def test_merge_preserves_existing_shared_settings_and_both_independent_permission_choices(self):
        saved_api = deepcopy(self.sources['lumy_general_v1'])
        saved_api.update(enabled=True, terms_approved=True, public_search_quote=True,
                         search_url='https://search.lumy.live/search?engines=yandex&language=es')
        saved_reader = deepcopy(self.sources['public_research_articles'])
        search = {'strategy': 'shared', 'preset': 'identity', 'execution_mode': 'extended',
                  'session_seconds': 1800, 'start': '2020-01-01', 'end': '2026-10-16',
                  'include_aliases': False, 'use_context': False}
        saved = {'sources': [saved_api, saved_reader], 'search': search,
                 'user_agent': 'Existing operator contact'}
        original = deepcopy(saved)
        merged = merge_source_defaults(saved, self.defaults)
        self.assertEqual(saved, original)
        self.assertEqual(merged['sources'][:2], original['sources'])
        self.assertEqual(merged['search'], search)
        self.assertEqual(normalize_search_config(merged['search'])['strategy'], 'shared')
        self.assertEqual(merged['user_agent'], original['user_agent'])
        for sid in ('lumy_general_v1', 'public_research_articles'):
            self.assertEqual(sum(source['id'] == sid for source in merged['sources']), 1)
        older = merge_source_defaults({'sources': [], 'search': search}, self.defaults)
        additions = [source for source in older['sources'] if source['id'] in
                     ('lumy_general_v1', 'public_research_articles')]
        self.assertEqual(len(additions), 2)
        self.assertTrue(all(source[key] is False for source in additions for key in PERMISSIONS))


if __name__ == '__main__':
    unittest.main()
