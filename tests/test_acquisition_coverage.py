"""Coverage reports observed work for current entities, not queued plans."""
import tempfile
import unittest

from peps.acquisition_coverage import acquisition_coverage
from peps.engine import Engine
from peps.news import NewsAcquirer
from peps.nlp import analyze
from peps.store import canonical, now


class AcquisitionCoverageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.engine = Engine(self.temp.name)
        self.store = self.engine.store
        self.news = NewsAcquirer(self.engine)
        self.store.import_entities([{'id': 'one', 'name': 'Primera Persona Sintética'},
                                    {'id': 'two', 'name': 'Segunda Persona Sintética'}])
        self.serial = 0
        self.sources = [{'enabled': True, 'terms_approved': True},
                        {'enabled': True, 'terms_approved': True, 'download_articles': True, 'article_terms_approved': True},
                        {'enabled': True, 'terms_approved': True, 'automated_search': False},
                        {'enabled': True, 'terms_approved': False, 'download_articles': True, 'article_terms_approved': True}]

    def article(self, document_id=None, scope='feed_metadata_only'):
        self.serial += 1
        article_id = 'article-' + str(self.serial)
        with self.store.connection() as con:
            con.execute('INSERT INTO news_articles(id,canonical_url,publisher_id,source_id,title,summary,discovered_at,document_id,scope,status) '
                        'VALUES(?,?,?,?,?,?,?,?,?,?)', (article_id, 'https://synthetic.invalid/' + article_id, 'synthetic', 'synthetic',
                                                       'Nota sintética', 'Resumen de prueba', now(), document_id, scope, 'discovered'))
        return article_id

    def observe(self, article_id, ids, relevant=True):
        self.serial += 1
        with self.store.connection() as con:
            con.execute('INSERT INTO news_observations(id,article_id,source_id,discovery_url,observed_url,retrieved_at,details) '
                        'VALUES(?,?,?,?,?,?,?)', ('observation-' + str(self.serial), article_id, 'synthetic', 'https://synthetic.invalid/feed',
                                                'https://synthetic.invalid/' + article_id, now(),
                                                canonical({'retrieval_relevance': {'relevant': relevant, 'entity_ids': ids, 'identity_verified': False}})))

    def document(self, kind='news', linked=()):
        self.serial += 1
        return self.store.add_document({'url': 'https://synthetic.invalid/body-' + str(self.serial),
            'text': 'Primera Persona Sintética presentó un informe de actividades.', 'source_kind': kind}, list(linked), analyze)

    def search(self, ids, *, completed=True, records=0, excluded=0, kind='news_query'):
        self.serial += 1
        job_id = self.store.enqueue(kind, {'entity_ids': ids}, key='search-' + str(self.serial))
        if completed:
            with self.store.connection() as con:
                con.execute('INSERT INTO news_discovery_runs VALUES(?,?,?,?,?,?,?,?)',
                            (job_id, 'synthetic', kind, 'https://synthetic.invalid/search', records,
                             canonical({'candidate_filter': {'excluded_records': excluded}}), 'synthetic-hash', now()))

    def test_queued_searches_do_not_count_and_successful_empty_results_count(self):
        self.search(['one'], completed=False)
        self.assertEqual(acquisition_coverage(self.store, self.sources)['entities_queried'], 0)
        self.search(['two', 'retired-entity'], records=0, excluded=4)
        value = acquisition_coverage(self.store, self.sources)
        self.assertEqual(value['entities_total'], 2)
        self.assertEqual(value['entities_queried'], 1)
        self.assertEqual(value['excluded_records'], 4)
        self.search(['one'], kind='news_feed')
        self.assertEqual(acquisition_coverage(self.store, self.sources)['entities_queried'], 1)

    def test_metadata_candidates_bodies_homonyms_and_unrelated_notes_are_distinct(self):
        metadata = self.article()
        self.observe(metadata, ['one', 'two', 'retired-entity'])
        irrelevant = self.article()
        self.observe(irrelevant, ['one'], relevant=False)
        body = self.article(self.document(), scope='full_article')
        self.observe(body, ['one'])
        value = acquisition_coverage(self.store, self.sources)
        self.assertEqual(value['entities_with_candidates'], 2)
        self.assertEqual(value['entities_with_bodies'], 1)
        self.assertEqual(value['relevant_articles'], 2)
        self.assertEqual(value['body_articles'], 1)
        self.assertEqual(value['metadata_only_articles'], 1)

    def test_existing_identity_links_supply_legacy_candidates_without_rerunning_identity(self):
        document = self.document()
        self.article(document, scope='full_article')
        with self.store.connection() as con:
            con.execute('INSERT INTO news_identity_links VALUES(?,?,?)', (document, 'one', canonical({'status': 'candidate'})))
            con.execute('INSERT INTO news_identity_links VALUES(?,?,?)', (document, 'two', canonical({'status': 'rejected'})))
        value = acquisition_coverage(self.store, self.sources)
        self.assertEqual(value['relevant_articles'], 1)
        self.assertEqual(value['entities_with_candidates'], 1)
        self.assertEqual(value['entities_with_bodies'], 1)

    def test_discovery_placeholder_never_counts_as_an_original_body(self):
        article = self.article(self.document(kind='discovery'), scope='feed_metadata_only')
        self.observe(article, ['one'])
        value = acquisition_coverage(self.store, self.sources)
        self.assertEqual(value['body_articles'], 0)
        self.assertEqual(value['metadata_only_articles'], 1)

    def test_local_documents_are_reported_separately_without_inventing_news_acquisition(self):
        self.document(kind='official', linked=['one'])
        value = acquisition_coverage(self.store, self.sources)
        self.assertEqual(value['local_documents'], 1)
        self.assertEqual(value['local_entities_with_bodies'], 1)
        self.assertEqual(value['body_articles'], 0)
        self.assertEqual(value['entities_queried'], 0)

    def test_source_permissions_are_separate_from_source_and_body_counts(self):
        value = acquisition_coverage(self.store, self.sources)
        self.assertEqual(value['permissions'], {'prepared_sources': 2, 'body_authorized_sources': 1, 'discovery_only_sources': 1})

    def test_older_sqlite_fallback_has_the_same_counts_and_reads_do_not_create_results(self):
        metadata = self.article()
        self.observe(metadata, ['one', 'two'])
        body = self.article(self.document(), scope='full_article')
        self.observe(body, ['one'])
        self.document(kind='official', linked=['two'])
        self.search(['one', 'two'], excluded=3)
        before = self.store.stats()
        expected = acquisition_coverage(self.store, self.sources)
        self.store._audit_json = False
        self.assertEqual(acquisition_coverage(self.store, self.sources), expected)
        self.assertEqual(self.store.stats(), before)


if __name__ == '__main__':
    unittest.main()
