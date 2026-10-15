"""Independent acquisition integration review; fixtures never access the network."""
import json
import contextlib
import io
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from peps.engine import Engine
from peps.identity import IdentityResolver
from peps.news import NewsAcquirer, article_text, canonical_url, news_fingerprint
from peps.store import digest, now


class FixtureFetcher:
    def __init__(self, bodies, barrier=None):
        self.bodies, self.calls, self.barrier = bodies, [], barrier

    def fetch(self, url, source):
        self.calls.append((url, dict(source)))
        if self.barrier:
            self.barrier.wait(10)
        body = self.bodies[url] if isinstance(self.bodies, dict) else self.bodies
        return {'url': url, 'status': 'ok', 'body': body, 'retrieved_at': now(),
                'sha256': digest(body), 'request_attempted': True, 'network_requests': 1}


class NewsIntegrationReview(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.engine = Engine(self.directory.name, online=True)
        self.news = NewsAcquirer(self.engine)
        self.one = self.source('one')
        self.two = self.source('two')
        self.configure(self.one, self.two)

    @staticmethod
    def source(name):
        return {'id': name, 'publisher_id': 'publisher-' + name, 'kind': 'rss',
                'allowed_hosts': [name + '.example'], 'url': 'https://' + name + '.example/feed',
                'enabled': True, 'terms_approved': True, 'download_articles': True,
                'article_terms_approved': True, 'minimum_article_chars': 80,
                'refresh_seconds': 86400, 'min_interval_seconds': 5}

    def configure(self, *sources):
        strategy = 'targeted' if any(source.get('kind') == 'gdelt' for source in sources) else 'feeds'
        self.news.config_path.write_text(json.dumps({'sources': list(sources), 'search': {'strategy': strategy}}), encoding='utf-8')

    @staticmethod
    def html(body=None, title='Informe municipal público durante sesión ordinaria'):
        body = body or ('Contenido público sintético acerca de una sesión ordinaria y su informe municipal. ' * 5)
        return '<html><head><title>' + title + '</title></head><main><article><p>' + body + '</p></article></main></html>'

    def observe(self, url, source=None, title='Informe municipal público durante sesión ordinaria', summary='Resumen sintético'):
        source = source or self.one
        return self.news._observe({'url': url, 'title': title, 'text': summary}, source,
                                  source['url'], {'retrieved_at': now(), 'sha256': 'fixture'}, None, True)

    def claimed(self, prefix='news_article'):
        job = self.engine.store.claim(kind_prefix=prefix)
        self.assertIsNotNone(job)
        return job

    def test_canonical_tracking_duplicates_create_one_article_and_one_job(self):
        self.observe('https://one.example/nota?utm_source=feed&a=1#comentarios')
        self.observe('https://ONE.example/nota?a=1&fbclid=tracking')
        self.assertEqual(self.news.stats()['articles'], 1)
        self.assertEqual(self.news.stats()['observations'], 2)
        self.assertEqual(self.engine.store.stats()['jobs'], {'queued': 1})
        self.assertEqual(canonical_url('https://one.example/%7Eautor?a=1&a=2&utm_medium=rss'),
                         'https://one.example/~autor?a=1&a=2')
        # Query values and repeated-key ordering can carry meaning.
        self.assertNotEqual(canonical_url('https://one.example/n?a=1&a=2'),
                            canonical_url('https://one.example/n?a=2&a=1'))

    def test_article_fallback_excludes_hidden_metadata_and_templates(self):
        html = ('<html><head><title>Acusación solo en el título</title>'
                '<script type="application/ld+json">acusación estructurada</script></head>'
                '<body><template>acusación de una plantilla inactiva</template>'
                '<p>Contenido visible del documento.</p></body></html>')
        self.assertEqual(article_text(html), 'Contenido visible del documento.')
        omitted_head_end = '<html><head><title>Metadatos</title><body><p>Texto visible.</p></body></html>'
        self.assertEqual(article_text(omitted_head_end), 'Texto visible.')

    def test_same_publisher_duplicate_body_reuses_document_and_keeps_observations(self):
        fetcher = FixtureFetcher(self.html())
        for suffix in ('version-a', 'version-b'):
            self.observe('https://one.example/' + suffix)
            self.news.execute(self.claimed(), fetcher)
        rows = self.news.articles()
        self.assertEqual(len(rows), 2)
        self.assertEqual(len({row['document_id'] for row in rows}), 1)
        self.assertEqual(self.engine.store.stats()['documents'], 1)
        self.assertEqual(self.news.stats()['observations'], 2)

    def test_concurrent_same_publisher_duplicate_bodies_are_atomic(self):
        count = 6
        for index in range(count):
            self.observe('https://one.example/parallel-' + str(index))
        jobs = [self.claimed() for _ in range(count)]
        fetcher = FixtureFetcher(self.html(), threading.Barrier(count))
        with ThreadPoolExecutor(count) as pool:
            list(pool.map(lambda job: self.news.execute(job, fetcher), jobs))
        self.assertEqual(self.news.stats()['status'], {'fetched': count})
        self.assertEqual(self.engine.store.stats()['documents'], 1)
        self.assertEqual(len({row['document_id'] for row in self.news.articles()}), 1)

    def test_cross_publisher_syndication_is_retained_with_shared_cluster(self):
        fetcher = FixtureFetcher(self.html())
        for source in (self.one, self.two):
            self.observe('https://' + source['id'] + '.example/nota', source)
            self.news.execute(self.claimed(), fetcher)
        rows = self.news.articles()
        self.assertEqual(len({row['document_id'] for row in rows}), 2)
        self.assertEqual(len({row['publisher_id'] for row in rows}), 2)
        self.assertEqual(len({row['content_cluster'] for row in rows}), 1)
        self.assertEqual(len({row['topic_id'] for row in rows}), 1)
        self.assertEqual(self.engine.store.stats()['documents'], 2)
        self.assertEqual(self.news.stats()['observations'], 2)

    def test_body_authorization_is_checked_again_at_execution(self):
        self.observe('https://one.example/nota')
        self.configure(dict(self.one, article_terms_approved=False), self.two)
        fetcher = FixtureFetcher(self.html())
        self.news.execute(self.claimed(), fetcher)
        self.assertEqual(fetcher.calls, [])
        self.assertEqual(self.engine.store.stats()['documents'], 0)
        self.assertEqual(self.engine.store.stats()['jobs'], {'blocked': 1})

    def test_export_keeps_public_source_policy_without_transport_secrets(self):
        source = dict(self.one, license='synthetic fixture permission', api_key='DO_NOT_EXPORT_TOKEN',
                      headers={'Authorization': 'DO_NOT_EXPORT_HEADER'})
        self.configure(source, self.two)
        self.observe('https://one.example/public-policy', source)
        self.news.execute(self.claimed(), FixtureFetcher(self.html()))
        records = list(self.engine.store.export())
        exported = json.dumps(records, ensure_ascii=False)
        self.assertNotIn('DO_NOT_EXPORT_TOKEN', exported)
        self.assertNotIn('DO_NOT_EXPORT_HEADER', exported)
        document = next(row for row in records if row['record_type'] == 'document')
        self.assertEqual(document['metadata']['source_policy']['id'], 'one')
        self.assertTrue(document['metadata']['source_policy']['article_terms_approved'])
        self.assertEqual(document['metadata']['source_policy']['license'], 'synthetic fixture permission')
        observation = next(row for row in records if row['record_type'] == 'news_observation')
        details = json.loads(observation['details'])
        self.assertEqual(details['source_policy']['id'], 'one')
        self.assertEqual(len(details['source_config_hash']), 64)

    def test_feed_authorization_does_not_authorize_third_party_article_host(self):
        decision = self.observe('https://unapproved.example/nota')
        self.assertEqual(decision['status'], 'metadata_only')
        self.assertEqual(self.news.stats()['articles'], 1)
        self.assertEqual(self.engine.store.stats()['jobs'], {})
        self.assertEqual(self.news.stats()['scopes'], {'feed_metadata_only': 1})

    def test_disabled_source_blocks_even_previously_queued_body(self):
        self.observe('https://one.example/nota')
        self.configure(dict(self.one, enabled=False), self.two)
        fetcher = FixtureFetcher(self.html())
        self.news.execute(self.claimed(), fetcher)
        self.assertEqual(fetcher.calls, [])
        self.assertEqual(self.engine.store.stats()['jobs'], {'blocked': 1})

    def test_malformed_feed_is_not_completed_as_empty_success(self):
        self.engine.store.enqueue('news_feed', {'source_id': 'one', 'url': self.one['url']})
        job = self.claimed('news_feed')
        with self.assertRaises(ValueError):
            self.news.execute(job, FixtureFetcher('<rss><channel>'))
        self.assertEqual(self.news.stats()['articles'], 0)
        self.assertEqual(self.engine.store.stats()['jobs'], {'running': 1})
        # Worker boundaries are responsible for recording parser errors/retries.
        self.engine.store.finish(job, 'deferred', 'fixture: XML inválido')
        self.assertEqual(self.engine.store.stats()['jobs'], {'deferred': 1})

    def test_validation_failure_does_not_retry_forever_without_consuming_attempt(self):
        self.engine.store.enqueue('news_feed', {'source_id': 'one', 'url': self.one['url']})
        job = self.claimed('news_feed')
        class InvalidFetcher:
            def fetch(self, *_):
                return {'status': 'error', 'body': '', 'error': 'Host is not in the source allowlist',
                        'request_attempted': False, 'network_requests': 0}
        self.news.execute(job, InvalidFetcher())
        with self.engine.store.connection() as con:
            row = con.execute('SELECT status,attempts FROM jobs WHERE id=?', (job['id'],)).fetchone()
        self.assertIn(row['status'], ('blocked', 'deferred'))
        if row['status'] == 'deferred':
            self.assertEqual(row['attempts'], 1)

    def test_host_courtesy_deferral_preserves_attempt_budget(self):
        self.engine.store.enqueue('news_feed', {'source_id': 'one', 'url': self.one['url']})
        job = self.claimed('news_feed')
        class CourtesyFetcher:
            def fetch(self, *_):
                return {'status': 'deferred', 'body': '', 'error': 'Per-host collection interval',
                        'error_code': 'host_interval', 'request_attempted': False,
                        'network_requests': 0, 'retry_at': time.time() + 5}
        self.news.execute(job, CourtesyFetcher())
        with self.engine.store.connection() as con:
            row = con.execute('SELECT status,attempts FROM jobs WHERE id=?', (job['id'],)).fetchone()
        self.assertEqual((row['status'], row['attempts']), ('deferred', 0))

    def query_source(self):
        return dict(self.one, id='index', kind='gdelt', allowed_hosts=['api.gdeltproject.org'],
                    base_url='https://api.gdeltproject.org/api/v2/doc/doc', max_records=2,
                    download_articles=False, article_terms_approved=False)

    def query_fixture(self, start, end):
        source = self.query_source()
        self.configure(source)
        self.engine.store.import_entities([{'id': 'synthetic', 'name': 'Persona Sintética'}])
        self.news.queue('review-window', start=start, end=end)
        job = self.claimed('news_query')
        body = json.dumps({'articles': [{'url': 'https://one.example/q' + str(i),
                                       'title': 'Resultado sintético ' + str(i),
                                       'seendate': '20261015T120000Z'} for i in range(2)]})
        self.news.execute(job, FixtureFetcher(body))
        return job

    def test_saturated_top_n_query_is_split_and_never_marked_complete(self):
        job = self.query_fixture('20261014000000', '20261015000000')
        with self.engine.store.connection() as con:
            parent = dict(con.execute('SELECT * FROM news_query_windows WHERE job_id=?', (job['id'],)).fetchone())
            children = [dict(row) for row in con.execute('SELECT * FROM news_query_windows WHERE parent_id=? ORDER BY window_start', (job['id'],))]
        self.assertEqual(parent['status'], 'split')
        self.assertEqual(parent['saturated'], 1)
        self.assertEqual(len(children), 2)
        self.assertEqual(children[0]['window_start'], parent['window_start'])
        self.assertEqual(children[-1]['window_end'], parent['window_end'])
        self.assertEqual(children[0]['window_end'], children[1]['window_start'])
        self.assertEqual(self.engine.store.stats()['jobs'], {'done': 1, 'queued': 2})

    def test_saturated_minimum_window_is_explicitly_truncated(self):
        self.query_fixture('20261015000000', '20261015001000')
        self.assertEqual(self.news.stats()['query_windows'], {'truncated': 1})
        with self.engine.store.connection() as con:
            row = con.execute('SELECT saturated,error FROM news_query_windows').fetchone()
        self.assertEqual(row['saturated'], 1)
        self.assertIn('incompleta', row['error'])

    def test_name_only_metadata_remains_discovery_and_does_not_score_adverse_claim(self):
        self.engine.store.import_entities([{'id': 'one', 'name': 'Juan Pérez'}])
        self.observe('https://unapproved.example/nota', title='Juan Pérez enfrenta acusación de fraude',
                     summary='Juan Pérez fue acusado de fraude en una publicación pública.')
        self.news.attribute()
        evidence = self.engine.store.evidence('one')
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]['source_kind'], 'discovery')
        self.assertEqual(evidence[0]['identity_resolution']['status'], 'candidate')
        self.assertEqual(evidence[0]['analysis']['findings'], [])
        self.assertEqual(self.engine.store.list_entities()[0]['score']['score'], 0)

    def test_identity_partial_spans_preserved_on_document_link(self):
        context = {'state': 'Oaxaca', 'role': 'alcalde',
                   'verified_fields': {'state': {'verified': True, 'source': 'review:state'},
                                       'role': {'verified': True, 'source': 'review:role'}}}
        entity = {'id': 'one', 'name': 'Juan Pérez', 'context': context}
        self.engine.store.import_entities([entity])
        body = 'Juan Pérez, alcalde de Oaxaca, presentó un informe municipal.' + (' Texto neutral distante. ' * 30) + 'Otro Juan Pérez fue acusado de fraude.'
        self.observe('https://one.example/identidad')
        self.news.execute(self.claimed(), FixtureFetcher(self.html(body)))
        self.news.attribute()
        evidence = self.engine.store.evidence('one')
        self.assertEqual(len(evidence), 1)
        resolution = evidence[0]['identity_resolution']
        self.assertEqual(resolution['status'], 'matched')
        supported = [mention['identity_supported'] for mention in resolution['mentions']]
        self.assertIn(True, supported)
        self.assertIn(False, supported)
        for mention in resolution['mentions']:
            self.assertEqual(evidence[0]['text'][mention['start']:mention['end']], mention['text'])
        score = self.engine.store.list_entities()[0]['score']
        self.assertEqual(score['score'], 0)
        self.assertEqual(score['provisional_score'], 0)
        exclusions = [reason for item in score['evidence_audit'] for finding in item['findings']
                      for reason in finding['exclusions']]
        self.assertIn('finding_outside_corroborated_identity_span', exclusions)

    def test_publication_date_reaches_identity_validity_rules(self):
        context = {'roles': [{'name': 'alcalde', 'jurisdiction': 'Oaxaca',
                             'valid_from': '2018-01-01', 'valid_to': '2020-12-31',
                             'verified': True, 'source': 'review:role'}]}
        self.engine.store.import_entities([{'id': 'one', 'name': 'Juan Pérez', 'context': context}])
        body = 'Juan Pérez, alcalde de Oaxaca, presentó su informe municipal. ' * 4
        html = self.html(body).replace('</head>', '<meta property="article:published_time" content="2019-01-01T12:00:00Z"></head>')
        self.observe('https://one.example/fecha')
        self.news.execute(self.claimed(), FixtureFetcher(html))
        self.news.attribute()
        resolution = self.engine.store.evidence('one')[0]['identity_resolution']
        self.assertEqual(resolution['status'], 'matched')
        self.assertEqual(resolution['features']['published_at'], '2019-01-01')

    def test_bank_correction_retires_obsolete_active_identity_links(self):
        self.engine.store.import_entities([{'id': 'one', 'name': 'Juan Pérez'}])
        self.observe('https://unapproved.example/obsoleto', title='Juan Pérez participa', summary='Juan Pérez presentó un informe neutral.')
        self.news.attribute()
        self.assertEqual(len(self.engine.store.evidence('one')), 1)
        self.engine.store.import_entities([{'id': 'one', 'name': 'Persona Distinta'}])
        self.news.attribute()
        self.assertEqual(self.engine.store.evidence('one'), [])
        self.assertEqual(self.news.stats()['identity_links'], 0)
        self.assertTrue(self.engine.store.verify_audit()['ok'])
        records = list(self.engine.store.export())
        retired = [row for row in records if row['record_type'] == 'retired_identity']
        self.assertEqual(len(retired), 1)
        self.assertEqual(retired[0]['entity_id'], 'one')
        self.assertEqual(retired[0]['resolution']['entity_id'], 'one')
        self.assertIsInstance(retired[0]['analysis'], dict)
        self.assertIn('corrección', retired[0]['reason'])
        self.assertEqual(len([row for row in records if row['record_type'] == 'news_observation']), 1)

    def test_empty_metadata_does_not_starve_later_identity_work(self):
        self.engine.store.import_entities([{'id': 'one', 'name': 'Juan Pérez'}])
        valid = max(('https://unapproved.example/valid-' + str(i) for i in range(100)), key=digest)
        valid_id = digest(valid)
        empties = [url for i in range(200) if digest(url := 'https://unapproved.example/empty-' + str(i)) < valid_id][:30]
        self.assertEqual(len(empties), 30)
        for url in empties:
            self.observe(url, title='', summary='')
        self.observe(valid, title='Juan Pérez participa', summary='Juan Pérez presentó un informe neutral.')
        first = self.news.attribute(limit=25)
        second = self.news.attribute(limit=25)
        third = self.news.attribute(limit=25)
        self.assertEqual(first['articles_processed'], 25)
        self.assertEqual(first['identity_links'].get('empty_text'), 25)
        self.assertEqual(second['articles_processed'], 6)
        self.assertEqual(third['articles_processed'], 0)
        self.assertEqual(len(self.engine.store.evidence('one')), 1)

    def test_concurrent_bank_import_cannot_lend_new_revision_to_old_index(self):
        context = {'state': 'Oaxaca', 'role': 'alcalde',
                   'verified_fields': {'state': {'verified': True, 'source': 'review:state'},
                                       'role': {'verified': True, 'source': 'review:role'}}}
        self.engine.store.import_entities([{'id': 'one', 'name': 'Juan Pérez', 'context': context}])
        self.observe('https://one.example/concurrent-bank')
        body = 'Juan Pérez, alcalde de Oaxaca, fue acusado de fraude. ' + ('Información sintética de pruebas. ' * 5)
        self.news.execute(self.claimed(), FixtureFetcher(self.html(body)))
        with self.engine.store.connection() as con:
            old_revision = int(con.execute("SELECT value FROM meta WHERE key='news_identity_revision'").fetchone()[0])
        def import_during_index_build(entities):
            resolver = IdentityResolver(entities)
            self.engine.store.import_entities([{'id': 'two', 'name': 'Juan Pérez', 'context': context}])
            return resolver
        with patch('peps.identity.IdentityResolver', side_effect=import_during_index_build):
            self.news.attribute()
        evidence = self.engine.store.evidence('one')[0]
        self.assertEqual(evidence['identity_resolution']['bank_revision'], old_revision)
        self.assertGreater(evidence['identity_bank_revision'], old_revision)
        score = self.engine.store.list_entities(search='Juan Pérez')[0]['score']
        self.assertEqual(score['provisional_score'], 0)
        reasons = [reason for item in score['evidence_audit'] for reason in item['exclusions']]
        self.assertIn('stale_or_invalid_identity_resolution', reasons)

    def test_planner_does_not_double_capacity_for_two_feeds_on_one_host(self):
        bank = [{'id': str(i), 'name': 'Persona Sintética ' + str(i)} for i in range(100)]
        self.configure(self.one)
        one_feed = self.news.plan(bank)
        self.configure(self.one, dict(self.one, id='mirror-feed', url='https://one.example/alternate-feed'))
        two_feeds = self.news.plan(bank)
        self.assertEqual(two_feeds['configured_body_hosts'], 1)
        self.assertEqual(two_feeds['balanced_estimated_days'], one_feed['balanced_estimated_days'])
        self.assertEqual(two_feeds['single_host_article_days'], one_feed['single_host_article_days'])

    def test_planner_sums_search_queries_that_share_a_host(self):
        bank = [{'id': str(i), 'name': 'Persona Sintética ' + str(i)} for i in range(120)]
        search = self.query_source()
        self.configure(self.one, search)
        one_index = self.news.plan(bank)
        self.configure(self.one, search, dict(search, id='index-alternate'))
        two_indexes = self.news.plan(bank)
        self.assertEqual(two_indexes['batched_discovery_requests'], 2 * one_index['batched_discovery_requests'])
        self.assertGreater(two_indexes['balanced_estimated_days'], one_index['balanced_estimated_days'])

    def test_daily_worker_is_idempotent_and_exports_news_identity_provenance(self):
        from peps.cli import main
        self.engine.store.import_entities([{'id': 'one', 'name': 'Juan Pérez'}])
        feed = ('<rss><channel><item><title>Juan Pérez presenta informe</title>'
                '<link>https://one.example/daily</link><description>Juan Pérez presentó su informe.</description>'
                '</item></channel></rss>')
        body = self.html('Juan Pérez presentó su informe municipal neutral. ' * 5,
                         title='Juan Pérez presenta informe')
        bodies = {self.one['url']: feed, self.two['url']: '<rss><channel></channel></rss>',
                  'https://one.example/daily': body}
        fetchers = []
        def factory(*args, **kwargs):
            fetcher = FixtureFetcher(bodies)
            fetchers.append(fetcher)
            return fetcher
        def invoke():
            output = io.StringIO()
            args = ['peps', '--data-dir', self.directory.name, '--online', 'worker', '--daily',
                    '--max-jobs', '20', '--seconds', '5', '--workers', '2']
            with patch.object(sys, 'argv', args), contextlib.redirect_stdout(output):
                main()
            return json.loads(output.getvalue())
        with patch('peps.collector.SafeFetcher', side_effect=factory):
            first = invoke()
            second = invoke()
        self.assertEqual(first['processed'], 3)
        self.assertEqual(second['processed'], 0)
        self.assertEqual(sum(len(fetcher.calls) for fetcher in fetchers), 3)
        self.assertEqual(self.news.stats()['articles'], 1)
        self.assertTrue(self.engine.store.verify_audit()['ok'])
        records = list(self.engine.store.export())
        kinds = {row['record_type'] for row in records}
        self.assertTrue({'document', 'evidence', 'identity_resolution', 'news_article',
                         'news_observation', 'news_discovery_run'}.issubset(kinds))
        observation = next(row for row in records if row['record_type'] == 'news_observation')
        self.assertEqual(observation['observed_url'], 'https://one.example/daily')
        self.assertEqual(len(observation['discovery_hash']), 64)
        details = json.loads(observation['details'])
        self.assertEqual(len(details['source_config_hash']), 64)


class SyntheticLoadHarnessReview(unittest.TestCase):
    def test_virtual_http_exercises_pipeline_without_any_real_network(self):
        from scripts.benchmark_news_load import run_case
        with patch('socket.socket', side_effect=AssertionError('Real sockets forbidden in load fixture')):
            result = run_case(20, 2, bank_size=40)
        self.assertTrue(result['passed'], result['checks'])
        self.assertEqual(result['cold']['article_requests'], 20)
        self.assertEqual(result['warm']['simulated_http_requests'], 0)
        self.assertEqual(result['bank']['articles'], 20)
        self.assertEqual(result['documents'], 4)
        self.assertEqual(result['bank']['content_clusters'], 2)


if __name__ == '__main__':
    unittest.main()
