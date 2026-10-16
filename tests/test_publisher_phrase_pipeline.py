"""Publisher body-only hits stay separate from relevance and identity."""
from copy import deepcopy
import json
import tempfile
import unittest
from unittest.mock import patch

from peps.acquisition_coverage import acquisition_coverage
from peps.engine import Engine
from peps.news import NewsAcquirer, _BodyBudget
from peps.news_projection import ensure_projection, projection_ready, PROJECTION_VERSION
from peps.relevance import RelevanceIndex, MODEL_VERSION
from peps.store import canonical, digest


class PublisherPhrasePipelineTests(unittest.TestCase):
    NAME = 'Persona Notaria Sintética'

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.engine = Engine(temporary.name, online=True)
        self.news, self.store = NewsAcquirer(self.engine), self.engine.store
        self.store.import_entities([{'id': 'notary', 'name': self.NAME}])
        self.source = {'id': 'publisher', 'kind': 'wordpress_api',
                       'search_kind': 'wordpress_api_search',
                       'url': 'https://publisher.invalid/wp-json/wp/v2/posts',
                       'allowed_hosts': ['publisher.invalid'], 'enabled': True,
                       'terms_approved': True, 'download_articles': True,
                       'article_terms_approved': True, 'robots_required': True,
                       'search_max_records': 2, 'minimum_article_chars': 80}
        self.extra_sources = []
        self.save()
        self.calls = []
        self.serial = 0

    def save(self):
        self.news.config_path.write_text(json.dumps({'sources': [self.source, *self.extra_sources],
                                                    'search': {'strategy': 'targeted'}}))

    def index(self):
        index = RelevanceIndex(self.store.iter_entities())
        with self.store.connection() as con:
            index.bank_revision = con.execute("SELECT value FROM meta WHERE key='news_identity_revision'").fetchone()[0]
        index.model_version = MODEL_VERSION
        return index

    def query(self, *, query=None, query_type='identity', result_limit=2):
        payload = {'source_id': self.source['id'], 'query': query or '"' + self.NAME + '"',
                   'query_type': query_type, 'entity_ids': ['notary'], 'names': [self.NAME],
                   'start': '20200101000000', 'end': '20210101000000',
                   'result_limit': result_limit, 'split_policy': 'expand',
                   'bank_revision': self.index().bank_revision}
        payload['url'] = self.news._query_url(self.source, payload)
        self.serial += 1
        self.store.enqueue('news_query', payload, key='test-query-' + str(self.serial))
        return self.store.claim(kind_prefix='news_query')

    def rows(self, count=1):
        return [{'id': index + 1, 'link': 'https://publisher.invalid/original-' + str(index),
                 'date_gmt': '2020-06-01T00:00:00',
                 'title': {'rendered': 'Documento público del editor'},
                 'excerpt': {'rendered': '<p>Extracto general sin nombres propios.</p>'},
                 'content': {'rendered': '<p>UNAPPROVED_API_BODY ' + self.NAME + '</p>'}}
                for index in range(count)]

    def fetcher(self, *, rows=None, headers=None, mentions=True):
        owner = self
        class Fixture:
            def fetch(self, url, source):
                owner.calls.append((url, deepcopy(source)))
                if '/wp-json/' in url:
                    body = json.dumps(owner.rows() if rows is None else rows)
                    pagination = {'x-wp-total': '1', 'x-wp-totalpages': '1'} if headers is None else headers
                else:
                    text = ('La persona ' + owner.NAME + ' acudió a la sesión pública. ' if mentions else
                            'Una sesión pública abordó la modernización del archivo municipal. ')
                    body = '<html><head><title>Documento público del editor</title></head><body><article><p>' + text * 5 + '</p></article></body></html>'
                    pagination = {}
                return {'status': 'ok', 'url': url, 'final_url': url, 'body': body,
                        'sha256': digest(body), 'retrieved_at': '2026-10-16T00:00:00+00:00',
                        'pagination_headers': pagination}
        return Fixture()

    def discover(self, *, budget=None, **fetch_options):
        job = self.query()
        result = self.news.execute(job, self.fetcher(**fetch_options), relevance=self.index(),
                                   body_budget=budget)
        return job, result

    def count(self, table):
        with self.store.connection() as con:
            return con.execute('SELECT count(*) FROM ' + table).fetchone()[0]

    def body_jobs(self):
        with self.store.connection() as con:
            return [dict(row, payload=json.loads(row['payload']))
                    for row in con.execute("SELECT * FROM jobs WHERE kind='news_article'")]

    def body(self, *, mentions=True, index=None):
        job = self.store.claim(kind_prefix='news_article')
        self.assertIsNotNone(job)
        result = self.news.execute(job, self.fetcher(mentions=mentions), relevance=index or self.index())
        return job, result

    def test_body_only_exact_hit_is_a_separate_lead_and_never_faked_metadata_relevance(self):
        self.discover()
        self.assertEqual(len(self.body_jobs()), 1)
        self.assertEqual(self.count('documents'), 0)
        self.assertEqual(self.count('news_current_candidates'), 0)
        with self.store.connection() as con:
            detail = json.loads(con.execute('SELECT details FROM news_observations').fetchone()[0])
        candidate = detail['retrieval_relevance']
        self.assertFalse(candidate['relevant'])
        self.assertEqual(candidate['rank'], 0)
        self.assertEqual(candidate['entity_ids'], [])
        self.assertEqual(detail['publisher_phrase_lead']['entity_ids'], ['notary'])
        self.assertFalse(detail['publisher_phrase_lead']['identity_verified'])
        self.assertNotIn('UNAPPROVED_API_BODY', canonical(detail))

    def test_original_body_promotes_only_retrieval_projection_without_attribution_or_scoring(self):
        self.discover()
        self.body()
        self.assertEqual(self.count('documents'), 1)
        self.assertEqual(self.count('news_current_candidates'), 1)
        self.assertEqual(self.count('evidence'), 0)
        with self.store.connection() as con:
            self.assertTrue(projection_ready(con))
            row = con.execute('SELECT id,title,summary FROM news_articles').fetchone()
            candidate = self.news._stored_candidate(row, self.index(), con)
        self.assertTrue(candidate['relevant'])
        self.assertEqual(candidate['match_scope'], 'full_article')
        self.assertFalse(candidate['identity_verified'])
        self.assertEqual(candidate['score_effect'], 'none')
        coverage = acquisition_coverage(self.store, self.news.sources())
        self.assertEqual(coverage['entities_with_bodies'], 1)

    def test_original_body_without_name_is_archived_without_current_list_coverage(self):
        self.discover()
        self.body(mentions=False)
        self.assertEqual(self.count('documents'), 1)
        self.assertEqual(self.count('news_current_candidates'), 0)
        self.assertEqual(acquisition_coverage(self.store, self.news.sources())['entities_with_bodies'], 0)
        self.assertTrue(ensure_projection(self.news, self.index()))
        self.assertEqual(self.count('news_current_candidates'), 0)

    def test_acquisition_reads_a_ready_original_before_remaining_discovery_queries(self):
        first = self.query()
        # Release the helper's first claim without changing its payload or due.
        with self.store.connection() as con:
            con.execute("UPDATE jobs SET status='queued',owner=NULL,lease_until=NULL WHERE id=?", (first['id'],))
        second = self.query()
        with self.store.connection() as con:
            con.execute("UPDATE jobs SET status='queued',owner=NULL,lease_until=NULL WHERE id=?", (second['id'],))
        fixture = self.fetcher()
        with patch('peps.collector.SafeFetcher', lambda *_args, **_kwargs: fixture):
            result = self.news.acquire(queue=False, workers=1, seconds=1, max_articles=2,
                                       body_budget=1, per_entity_body_limit=1)
        self.assertEqual(result['completed'], 2)
        self.assertEqual(len(self.calls), 2)
        self.assertIn('/wp-json/', self.calls[0][0])
        self.assertIn('/original-', self.calls[1][0])
        self.assertEqual(self.count('documents'), 1)

    def test_unquoted_context_or_alias_queries_cannot_bypass_metadata_filter(self):
        for query, query_type in ((self.NAME, 'identity'), ('"' + self.NAME + '"', 'context'),
                                  ('"Persona Sintética"', 'identity')):
            job = self.query(query=query, query_type=query_type)
            self.news.execute(job, self.fetcher(), relevance=self.index())
        self.assertEqual(self.body_jobs(), [])
        self.assertEqual(self.count('news_observations'), 0)

    def test_existing_metadata_match_keeps_its_actual_relevance_without_a_body_only_label(self):
        rows = self.rows()
        rows[0]['title']['rendered'] = self.NAME + ' presenta informe'
        self.discover(rows=rows)
        with self.store.connection() as con:
            detail = json.loads(con.execute('SELECT details FROM news_observations').fetchone()[0])
        self.assertTrue(detail['retrieval_relevance']['relevant'])
        self.assertNotIn('publisher_phrase_lead', detail)
        self.assertEqual(len(self.body_jobs()), 1)

    def test_response_without_immutable_content_hash_cannot_create_a_body_only_lead(self):
        fixture = self.fetcher()
        fetch = fixture.fetch
        fixture.fetch = lambda url, source: dict(fetch(url, source), sha256=None)
        self.news.execute(self.query(), fixture, relevance=self.index())
        self.assertEqual(self.body_jobs(), [])
        self.assertEqual(self.count('news_observations'), 0)

    def test_changed_name_with_same_id_invalidates_a_saved_lead_before_any_body_request(self):
        self.discover()
        self.store.import_entities([{'id': 'notary', 'name': 'Otra Persona Pública'}])
        before = len(self.calls)
        _job, result = self.body()
        self.assertEqual(result['body_skipped_irrelevant'], 1)
        self.assertEqual(len(self.calls), before)
        self.assertEqual(self.count('documents'), 0)

    def test_body_permissions_stay_required_after_a_valid_publisher_lead(self):
        self.discover()
        self.source['article_terms_approved'] = False
        self.save()
        before = len(self.calls)
        self.body()
        self.assertEqual(len(self.calls), before)
        self.assertEqual(self.count('documents'), 0)
        self.assertFalse(self.news.sources()[0]['article_terms_approved'])

    def test_global_and_per_entity_budgets_bound_body_only_leads(self):
        self.discover(rows=self.rows(2), headers={'x-wp-totalpages': '1'},
                      budget=_BodyBudget(maximum=1, per_entity=1))
        self.assertEqual(self.count('news_observations'), 2)
        self.assertEqual(len(self.body_jobs()), 1)
        self.assertEqual(self.body_jobs()[0]['payload']['bank_revision'], self.index().bank_revision)

    def test_exact_page_at_known_total_is_complete_but_more_results_never_split_dates(self):
        job, result = self.discover(rows=self.rows(2), headers={'x-wp-total': '2', 'x-wp-totalpages': '1'})
        self.assertFalse(result['discovery_metadata']['truncated'])
        with self.store.connection() as con:
            event = json.loads(con.execute("SELECT payload FROM audit WHERE action='news_discovery_completed' ORDER BY seq DESC LIMIT 1").fetchone()[0])
        self.assertFalse(event['saturated'])
        job = self.query()
        result = self.news.execute(job, self.fetcher(rows=self.rows(2), headers={'x-wp-total': '20', 'x-wp-totalpages': '10'}), relevance=self.index())
        self.assertTrue(result['discovery_metadata']['truncated'])
        self.assertEqual(self.count('jobs'), 4)  # Two searches, two canonical bodies.

    def test_fake_body_payload_flags_without_preserved_observation_cannot_authorize_fetch(self):
        self.discover(budget=_BodyBudget(maximum=0, per_entity=0))
        with self.store.connection() as con:
            row = con.execute('SELECT id,canonical_url FROM news_articles').fetchone()
            con.execute('DELETE FROM news_observations')
        self.store.enqueue('news_article', {'source_id': self.source['id'], 'article_id': row['id'],
                           'url': row['canonical_url'], 'publisher_phrase_lead': True,
                           'candidate_entity_ids': ['notary']})
        before = len(self.calls)
        _job, result = self.body()
        self.assertEqual(result['body_skipped_irrelevant'], 1)
        self.assertEqual(len(self.calls), before)

    def test_separate_body_authorized_profile_can_read_an_originating_publisher_lead(self):
        self.source.update(discovery_only=True, download_articles=False, article_terms_approved=False)
        self.extra_sources = [{'id': 'publisher-body', 'kind': 'rss', 'url': 'https://publisher.invalid/feed',
                              'allowed_hosts': ['publisher.invalid'], 'enabled': True, 'terms_approved': True,
                              'download_articles': True, 'article_terms_approved': True}]
        self.save()
        self.discover()
        self.assertEqual(self.body_jobs()[0]['payload']['source_id'], 'publisher-body')
        self.body()
        self.assertEqual(self.count('news_current_candidates'), 1)

    def test_resume_recovers_a_saved_lead_after_separate_body_approval_without_new_search(self):
        self.source['article_terms_approved'] = False
        self.save()
        self.discover()
        self.assertEqual(self.body_jobs(), [])
        self.source['article_terms_approved'] = True
        self.save()
        with patch('socket.getaddrinfo', side_effect=AssertionError('No network for rescue')):
            result = self.news._rescue_bodies(self.index(), self.news.sources(), _BodyBudget(1, 1))
        self.assertEqual(result['body_queued'], 1)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.body_jobs()[0]['payload']['bank_revision'], self.index().bank_revision)
        self.body()
        observations = self.count('news_observations')
        documents = self.count('documents')
        result = self.news._rescue_bodies(self.index(), self.news.sources(), _BodyBudget(1, 1))
        self.assertEqual(result['body_queued'], 0)
        self.assertEqual(self.count('news_observations'), observations)
        self.assertEqual(self.count('documents'), documents)

    def test_upgrade_rebuilds_old_projection_from_original_text_and_preserves_raw_history(self):
        self.discover()
        self.body()
        with self.store.connection() as con:
            raw = [tuple(row) for row in con.execute('SELECT * FROM news_observations')]
            con.execute('DELETE FROM news_current_candidates')
            signature = {'bank_revision': str(self.index().bank_revision), 'relevance_version': MODEL_VERSION,
                         'projection_version': 'all-history-1'}
            con.execute("INSERT OR REPLACE INTO meta VALUES('news_projection_ready',?)", (canonical(signature),))
        with patch('peps.nlp.analyze', side_effect=AssertionError('Projection never runs NLP')), \
             patch('socket.getaddrinfo', side_effect=AssertionError('Projection never uses network')):
            self.assertTrue(ensure_projection(self.news, self.index()))
        with self.store.connection() as con:
            self.assertEqual([tuple(row) for row in con.execute('SELECT * FROM news_observations')], raw)
            marker = json.loads(con.execute("SELECT value FROM meta WHERE key='news_projection_ready'").fetchone()[0])
        self.assertEqual(marker['projection_version'], PROJECTION_VERSION)
        self.assertEqual(self.count('news_current_candidates'), 1)

    def test_body_save_with_stale_acquisition_revision_invalidates_projection(self):
        self.discover()
        stale = self.index()
        body_job = self.body_jobs()[0]
        self.store.import_entities([{'id': 'notary', 'name': 'Otra Persona Pública'}])
        fixture = self.fetcher()
        result = fixture.fetch(body_job['payload']['url'], self.source)
        self.news._conserve_article(body_job['payload'], self.source, result, relevance=stale)
        with self.store.connection() as con:
            self.assertFalse(projection_ready(con))
        self.assertTrue(ensure_projection(self.news, self.index()))
        self.assertEqual(self.count('news_current_candidates'), 0)


if __name__ == '__main__':
    unittest.main()
