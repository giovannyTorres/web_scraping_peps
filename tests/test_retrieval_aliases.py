"""Surname collisions must not become personal article candidates or queries."""
from copy import deepcopy
import json
import tempfile
import unittest

from peps.engine import Engine
from peps.news import NewsAcquirer
from peps.news_projection import ensure_projection, projection_ready, projection_signature
from peps.news_search import entity_queries, wordpress_queries, normalize_search_config
from peps.relevance import MODEL_VERSION, RelevanceIndex
from peps.retrieval_aliases import retrieval_alias_allowed
from peps.store import canonical, now


ANA = {'id': 'ana', 'name': 'Ana Lima Nogal', 'kind': 'person',
       'aliases': ['Lima Nogal'], 'context': {'state': 'Oaxaca', 'role': 'notario'}}


class RetrievalAliasGuardTests(unittest.TestCase):
    def queries(self, entity):
        config = normalize_search_config({'preset': 'identity', 'use_context': True})
        return ' '.join(row['query'] for build in (entity_queries, wordpress_queries)
                        for row in build(entity, config))

    def test_other_given_name_same_state_and_profession_does_not_match_surnames(self):
        text = 'Bruno Lima Nogal, notario de Oaxaca, presentó su informe.'
        self.assertFalse(RelevanceIndex([ANA]).rank(text, '')['relevant'])
        query = self.queries(ANA)
        self.assertIn('"Ana Lima Nogal"', query)
        self.assertNotIn('"Lima Nogal"', query)
        self.assertFalse(retrieval_alias_allowed(ANA, 'Nogal'))
        bruno = dict(ANA, id='bruno', name='Bruno Lima Nogal')
        self.assertEqual(RelevanceIndex([ANA, bruno]).rank(text, '')['entity_ids'], ['bruno'])

    def test_canonical_name_and_equal_alias_remain_available(self):
        for name in ('Ana Lima Nogal', 'Ana Nogal', 'Nogal'):
            entity = dict(ANA, name=name, aliases=[name])
            self.assertTrue(retrieval_alias_allowed(entity, name))
            self.assertTrue(RelevanceIndex([entity]).rank(name + ', notario de Oaxaca.', '')['relevant'])
            self.assertIn('"' + name + '"', self.queries(entity))

    def test_significant_particles_do_not_disguise_a_short_suffix(self):
        entity = dict(ANA, name='Ana de la Lima y Nogal', aliases=['de la Lima y Nogal'])
        self.assertFalse(retrieval_alias_allowed(entity, entity['aliases'][0]))
        self.assertFalse(RelevanceIndex([entity]).rank('Bruno de la Lima y Nogal, notario de Oaxaca.', '')['relevant'])

    def test_verified_flag_requires_existing_valid_source_or_provenance(self):
        for provenance in ('review:record-1', {'url': 'https://synthetic.invalid/registry'}, {'review_id': 'review-1'}):
            for key in ('source', 'provenance'):
                alias = {'name': 'Lima Nogal', 'verified': True, key: provenance}
                entity = dict(ANA, aliases=[alias])
                with self.subTest(key=key, provenance=provenance):
                    self.assertTrue(retrieval_alias_allowed(entity, alias))
                    self.assertTrue(RelevanceIndex([entity]).rank('Lima Nogal, notario de Oaxaca.', '')['relevant'])
                    self.assertIn('"Lima Nogal"', self.queries(entity))
                    self.assertFalse(RelevanceIndex([entity]).rank('Lima Nogal compareció.', '')['relevant'])

    def test_bare_flags_empty_provenance_and_rejected_aliases_do_not_bypass_guard(self):
        invalid = [
            {'name': 'Lima Nogal', 'verified': True},
            {'name': 'Lima Nogal', 'verified': True, 'source': ''},
            {'name': 'Lima Nogal', 'verified': True, 'source': {}},
            {'name': 'Lima Nogal', 'verified': True, 'source': True},
            {'name': 'Lima Nogal', 'validated': True, 'source': 'review:1'},
            {'name': 'Lima Nogal', 'status': 'verified', 'source': 'review:1'},
            {'name': 'Lima Nogal', 'verified': True, 'source': 'review:1', 'status': 'revoked'},
            {'name': 'Lima Nogal', 'verified': True, 'source': 'review:1', 'validated': False},
            {'name': 'Lima Nogal', 'verified': True, 'source': 'review:1', 'status': 'pending'}]
        for alias in invalid:
            entity = dict(ANA, aliases=[alias])
            with self.subTest(alias=alias):
                self.assertFalse(retrieval_alias_allowed(entity, alias))
                self.assertFalse(RelevanceIndex([entity]).rank('Lima Nogal, notario de Oaxaca.', '')['relevant'])
                self.assertNotIn('"Lima Nogal"', self.queries(entity))

    def test_verified_container_does_not_promote_strings_or_source_mapping(self):
        for values in (['Lima Nogal'], {'Lima Nogal': 'review:record-1'}):
            entity = dict(ANA, aliases=[], context={**ANA['context'], 'verified_aliases': values})
            self.assertFalse(RelevanceIndex([entity]).rank('Lima Nogal, notario de Oaxaca.', '')['relevant'])
            self.assertNotIn('"Lima Nogal"', self.queries(entity))
        entity = dict(ANA, aliases=[], context={**ANA['context'], 'verified_aliases': {
            'Lima Nogal': {'verified': True, 'source': 'review:record-1'}}})
        self.assertTrue(RelevanceIndex([entity]).rank('Lima Nogal, notario de Oaxaca.', '')['relevant'])
        self.assertIn('"Lima Nogal"', self.queries(entity))

    def test_three_token_abbreviation_and_distinct_nickname_keep_context_policy(self):
        entity = dict(ANA, name='Ana Lucía Lima Nogal', aliases=['Ana Lima Nogal', 'La Colibrí'])
        for alias in entity['aliases']:
            self.assertTrue(retrieval_alias_allowed(entity, alias))
            self.assertTrue(RelevanceIndex([entity]).rank(alias + ', notario de Oaxaca.', '')['relevant'])
            self.assertIn('"' + alias + '"', self.queries(entity))
            self.assertFalse(RelevanceIndex([entity]).rank(alias + ' compareció.', '')['relevant'])

    def test_company_suffix_aliases_and_legal_core_are_unchanged(self):
        entity = dict(ANA, kind='company', name='Industrias Lima Nogal S.A. de C.V.')
        self.assertTrue(retrieval_alias_allowed(entity, 'Lima Nogal'))
        rank = RelevanceIndex([entity]).rank('Lima Nogal presentó resultados.', '')
        self.assertTrue(rank['relevant'])
        self.assertFalse(rank['identity_verified'])
        query = self.queries(entity)
        for phrase in ('"Lima Nogal"', '"Industrias Lima Nogal"', '"Industrias Lima Nogal S.A. de C.V."'):
            self.assertIn(phrase, query)

    def test_helper_does_not_mutate_existing_bank_verification(self):
        entity = deepcopy(ANA)
        preserved = deepcopy(entity)
        retrieval_alias_allowed(entity, entity['aliases'][0])
        RelevanceIndex([entity]).rank('Bruno Lima Nogal, notario de Oaxaca.', '')
        self.queries(entity)
        self.assertEqual(entity, preserved)

    def test_model_upgrade_rebuilds_only_projection_and_preserves_archive_and_jobs(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = Engine(directory)
            store = engine.store
            store.import_entities([ANA])
            news = NewsAcquirer(engine)
            text = 'Bruno Lima Nogal, notario de Oaxaca, presentó su informe.'
            store.enqueue('news_article', {'article_id': 'historical'}, key='retained-job')
            with store.connection() as con:
                con.execute('INSERT INTO news_articles(id,canonical_url,publisher_id,source_id,title,summary,discovered_at,scope,status) '
                            'VALUES(?,?,?,?,?,?,?,?,?)', ('historical', 'https://synthetic.invalid/article',
                             'synthetic', 'synthetic', text, '', now(), 'feed_metadata_only', 'discovered'))
                con.execute('INSERT INTO news_observations(id,article_id,source_id,discovery_url,observed_url,retrieved_at,details) '
                            'VALUES(?,?,?,?,?,?,?)', ('observation', 'historical', 'synthetic',
                             'https://synthetic.invalid/feed', 'https://synthetic.invalid/article', now(),
                             canonical({'title': text, 'retrieval_relevance': {'relevant': True, 'entity_ids': ['ana'],
                                        'model_version': 'metadata-relevance-1.2.0'}})))
                old_signature = dict(projection_signature(con), relevance_version='metadata-relevance-1.2.0')
                con.execute("INSERT OR REPLACE INTO meta VALUES('news_projection_ready',?)", (canonical(old_signature),))
                con.execute('INSERT INTO news_current_candidates VALUES(?,?,?,?)',
                            ('historical', 'ana', old_signature['bank_revision'], 'metadata-relevance-1.2.0'))
                before = {table: [tuple(row) for row in con.execute('SELECT * FROM ' + table)]
                          for table in ('news_articles', 'news_observations', 'jobs')}
                self.assertFalse(projection_ready(con))
            self.assertTrue(ensure_projection(news, RelevanceIndex(store.iter_entities())))
            with store.connection() as con:
                self.assertTrue(projection_ready(con))
                self.assertEqual(con.execute('SELECT COUNT(*) FROM news_current_candidates').fetchone()[0], 0)
                for table, expected in before.items():
                    self.assertEqual([tuple(row) for row in con.execute('SELECT * FROM ' + table)], expected)
            self.assertEqual(MODEL_VERSION, 'metadata-relevance-1.2.1')
            self.assertTrue(store.verify_audit()['ok'])


if __name__ == '__main__':
    unittest.main()
