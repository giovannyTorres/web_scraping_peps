"""Coverage reports observed work for current entities, not queued plans."""
import tempfile
import unittest

from peps.acquisition_coverage import acquisition_coverage
from peps.engine import Engine
from peps.news import NewsAcquirer
from peps.news_projection import update_projection
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

    def test_multimedia_references_are_separate_from_usable_text_and_pending_references(self):
        document = self.document()
        body = self.article(document, 'full_article')
        self.observe(body, ['one'])
        media = self.article(scope='multimedia_reference')
        pending = self.article()
        unrelated = self.article(scope='multimedia_reference')
        for article in (media, pending):
            self.observe(article, ['one'])
        self.observe(media, ['two'])
        self.observe(unrelated, [], relevant=False)
        for json_sql in (True, False):
            with self.subTest(json_sql=json_sql):
                self.store._audit_json = json_sql
                self.store._news_acquisition_coverage_cache = None
                actual = acquisition_coverage(self.store, self.sources)
                self.assertEqual(actual['relevant_articles'], 3)
                self.assertEqual(actual['body_articles'], 1)
                self.assertEqual(actual['metadata_only_articles'], 2)
                self.assertEqual(actual['multimedia_reference_articles'], 1)
                self.assertEqual(actual['pending_text_articles'], 1)
                self.assertEqual(actual['entities_with_bodies'], 1)
        self.store._audit_json = True
        self.assertEqual({row['id'] for row in self.news.articles(relevant_only=True, scope='multimedia_reference')}, {media})
        self.assertEqual({row['id'] for row in self.news.articles(relevant_only=True, scope='full_article')}, {body})
        self.assertEqual({row['id'] for row in self.news.articles(scope='multimedia_reference')}, {media, unrelated})
        with self.assertRaises(ValueError):
            self.news.articles(scope='untrusted_sql')

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
            revision = con.execute("SELECT value FROM meta WHERE key='news_identity_revision'").fetchone()[0]
            update_projection(con, article_id, {'relevant': relevant, 'entity_ids': ids}, bank_revision=revision)

    def document(self, kind='news', linked=()):
        self.serial += 1
        return self.store.add_document({'url': 'https://synthetic.invalid/body-' + str(self.serial),
            'text': 'Primera Persona Sintética presentó un informe de actividades.', 'source_kind': kind}, list(linked), analyze)

    def search(self, ids, *, completed=True, records=0, excluded=0, kind='news_query'):
        self.serial += 1
        with self.store.connection() as con:
            revision = con.execute("SELECT value FROM meta WHERE key='news_identity_revision'").fetchone()[0]
        job_id = self.store.enqueue(kind, {'entity_ids': ids, 'bank_revision': revision}, key='search-' + str(self.serial))
        if completed:
            with self.store.connection() as con:
                con.execute('INSERT INTO news_discovery_runs VALUES(?,?,?,?,?,?,?,?)',
                            (job_id, 'synthetic', kind, 'https://synthetic.invalid/search', records,
                             canonical({'candidate_filter': {'excluded_records': excluded}}), 'synthetic-hash', now()))

    def shared_page(self, ids=(), *, revision=None, completed=True, records=0, status='queued',
                    page_limit=False, truncated=False, metadata_extra=None):
        self.serial += 1
        with self.store.connection() as con:
            current = con.execute("SELECT value FROM meta WHERE key='news_identity_revision'").fetchone()[0]
        revision = current if revision is None else revision
        payload = {'shared_index': True, 'bank_revision': revision, 'url': f'https://synthetic.invalid/page-{self.serial}'}
        job_id = self.store.enqueue('news_feed', payload, key=f'shared-page-{self.serial}')
        with self.store.connection() as con:
            con.execute('UPDATE jobs SET status=? WHERE id=?', ('done' if completed else status, job_id))
            if completed:
                metadata = {'shared_index': True, 'bank_revision': revision, 'locally_checked_entity_ids': list(ids),
                            'candidate_filter': {'input_records': records, 'excluded_records': 0},
                            'continuation': {'page_limit_reached': page_limit}, 'pagination': {'truncated': truncated}}
                metadata.update(metadata_extra or {})
                con.execute('INSERT INTO news_discovery_runs VALUES(?,?,?,?,?,?,?,?)',
                            (job_id, 'synthetic', 'news_feed', payload['url'], 0,
                             canonical(metadata), 'synthetic-hash', now()))
        return job_id

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
            revision = int(con.execute("SELECT value FROM meta WHERE key='news_identity_revision'").fetchone()[0])
            con.execute('INSERT INTO news_identity_links VALUES(?,?,?)', (document, 'one', canonical({'status': 'candidate', 'bank_revision': revision})))
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

    def test_shared_scan_reports_300_local_checks_without_inventing_directed_queries_or_evidence(self):
        additional = [{'id': f'bank-{index}', 'name': f'Persona Sintética {index}'} for index in range(298)]
        self.store.import_entities(additional)
        identifiers = ['one', 'two', *(entity['id'] for entity in additional)]
        self.shared_page([*identifiers, 'one', 'retired-entity'], records=100)
        self.shared_page(['one', 'two'], records=80)
        before = self.store.stats()
        value = acquisition_coverage(self.store, self.sources)
        self.assertEqual(value['entities_total'], 300)
        self.assertEqual(value['shared_pages_read'], 2)
        self.assertEqual(value['shared_records_checked'], 180)
        self.assertEqual(value['entities_checked_locally'], 300)
        self.assertEqual(value['entities_queried'], 0)
        self.assertEqual(value['shared_pages_pending'], 0)
        self.assertEqual(value['entities_with_candidates'], 0)
        self.assertEqual(value['body_articles'], 0)
        self.assertEqual(self.store.stats(), before)
        self.search(['one'])
        self.assertEqual(acquisition_coverage(self.store, self.sources)['entities_queried'], 1)

    def test_shared_receipts_and_queue_must_match_current_bank_revision_even_when_ids_are_unchanged(self):
        self.shared_page(['one', 'two'], records=100, page_limit=True)
        self.shared_page(completed=False)
        self.store.import_entities([{'id': 'one', 'name': 'Nombre Sintético Corregido'}])
        value = acquisition_coverage(self.store, self.sources)
        for key in ('shared_pages_read', 'shared_records_checked', 'entities_checked_locally',
                    'shared_pages_pending', 'shared_pages_limited'):
            self.assertEqual(value[key], 0, key)
        self.shared_page(['one', 'retired-entity'], records=25)
        self.shared_page(completed=False)
        value = acquisition_coverage(self.store, self.sources)
        self.assertEqual(value['shared_pages_read'], 1)
        self.assertEqual(value['shared_records_checked'], 25)
        self.assertEqual(value['entities_checked_locally'], 1)
        self.assertEqual(value['shared_pages_pending'], 1)

    def test_shared_pending_counts_only_active_shared_feed_jobs_and_limits_count_pages_once(self):
        for status in ('queued', 'deferred', 'running', 'blocked', 'dead', 'cancelled', 'done'):
            self.shared_page(completed=False, status=status)
        self.shared_page(completed=False, revision='stale-revision')
        self.store.enqueue('news_feed', {'entity_ids': ['one']}, key='general-feed')
        self.shared_page(['one'], records=7, page_limit=True)
        self.shared_page(['two'], records=8, truncated=True)
        self.shared_page(['one'], records=9, page_limit=True, truncated=True)
        value = acquisition_coverage(self.store, self.sources)
        self.assertEqual(value['shared_pages_pending'], 3)
        self.assertEqual(value['shared_pages_read'], 3)
        self.assertEqual(value['shared_pages_limited'], 3)
        self.assertEqual(value['shared_records_checked'], 24)
        self.assertEqual(value['entities_checked_locally'], 2)

    def test_shared_metadata_cache_is_neither_a_completed_current_bank_scan_nor_article_evidence(self):
        parsed = {'records': [{'title': 'Primera Persona Sintética', 'text': 'Una mención en índice'}],
                  'pagination': {'truncated': True}}
        with self.store.connection() as con:
            con.execute('INSERT INTO news_shared_pages VALUES(?,?,?,?,?,?,?)',
                        ('synthetic', 'https://synthetic.invalid/sitemap.xml', 'configuration',
                         'metadata-hash', now(), 9999999999, canonical(parsed)))
        value = acquisition_coverage(self.store, self.sources)
        self.assertEqual(value['shared_pages_read'], 0)
        self.assertEqual(value['entities_checked_locally'], 0)
        self.assertEqual(value['shared_pages_limited'], 0)
        self.assertEqual(value['entities_with_candidates'], 0)
        self.assertEqual(value['body_articles'], 0)

    def test_shared_fallback_has_identical_counts_for_current_stale_limited_and_malformed_receipts(self):
        self.shared_page(['one', 'two', 'retired-entity'], records=40, page_limit=True)
        self.shared_page(['one'], records=0, truncated=True)
        self.shared_page(['two'], records=500, revision='stale')
        self.shared_page(['one'], records=99, metadata_extra={'shared_index': False})
        self.shared_page(['two'], records=-10)
        self.shared_page(['one'], records=True)
        self.shared_page(['one'], records='99')
        self.shared_page(completed=False, status='running')
        self.shared_page(completed=False, revision='stale')
        self.search(['two'])
        before = self.store.stats()
        expected = acquisition_coverage(self.store, self.sources)
        self.assertEqual(expected['shared_records_checked'], 40)
        self.assertEqual(expected['shared_pages_read'], 5)
        self.assertEqual(expected['entities_queried'], 1)
        self.store._audit_json = False
        self.assertEqual(acquisition_coverage(self.store, self.sources), expected)
        self.assertEqual(self.store.stats(), before)

    def test_unreadable_and_wrong_shape_discovery_json_preserves_queue_and_coverage(self):
        self.search(['one'], excluded=2)
        with self.store.connection() as con:
            revision = con.execute("SELECT value FROM meta WHERE key='news_identity_revision'").fetchone()[0]
        invalid_payloads = ['{broken', '[]', 'null', '"legacy"', '42',
                            canonical({'bank_revision': revision, 'entity_ids': None}),
                            canonical({'bank_revision': revision, 'entity_ids': {'one': 'two'}})]
        invalid_metadata = ['{broken', '[]', 'null', '"legacy"', '42',
                            canonical({'candidate_filter': {'excluded_records': True}}),
                            canonical({'candidate_filter': {'excluded_records': '999'}})]
        for index, payload in enumerate(invalid_payloads):
            job = self.store.enqueue('news_query', {'entity_ids': []}, key=f'invalid-payload-{index}')
            with self.store.connection() as con:
                con.execute('UPDATE jobs SET payload=? WHERE id=?', (payload, job))
                con.execute('INSERT INTO news_discovery_runs VALUES(?,?,?,?,?,?,?,?)',
                            (job, 'synthetic', 'news_query', 'https://synthetic.invalid/search', 0,
                             canonical({'candidate_filter': {'excluded_records': 999}}), 'hash', now()))
        for index, metadata in enumerate(invalid_metadata):
            job = self.store.enqueue('news_query', {'entity_ids': ['one'], 'bank_revision': revision}, key=f'invalid-metadata-{index}')
            with self.store.connection() as con:
                con.execute('INSERT INTO news_discovery_runs VALUES(?,?,?,?,?,?,?,?)',
                            (job, 'synthetic', 'news_query', 'https://synthetic.invalid/search', 0, metadata, 'hash', now()))
        with self.store.connection() as con:
            before = [tuple(row) for row in con.execute('SELECT * FROM jobs ORDER BY id')]
        expected = acquisition_coverage(self.store, self.sources)
        self.assertEqual(expected['entities_queried'], 1)
        self.assertEqual(expected['excluded_records'], 2)
        self.store._audit_json = False
        self.assertEqual(acquisition_coverage(self.store, self.sources), expected)
        with self.store.connection() as con:
            self.assertEqual([tuple(row) for row in con.execute('SELECT * FROM jobs ORDER BY id')], before)

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
