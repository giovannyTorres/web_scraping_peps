"""Publisher-wide discovery with native SQLite/scheduler and fixture HTTP only."""
import json
from pathlib import Path
import socket
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from peps.acquisition_coverage import acquisition_coverage
from peps.collector import SafeFetcher
from peps.engine import Engine
from peps.news import NewsAcquirer, _BodyBudget
from peps.relevance import RelevanceIndex
from peps.store import digest, now


class NativeSharedRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.engine = Engine(self.temp.name, online=True)
        self.news = NewsAcquirer(self.engine)
        self.store = self.engine.store
        self.entities = [{'id': str(index), 'name': 'Persona Sintética Inventada ' + str(index)}
                         for index in range(300)]
        self.store.import_entities(self.entities)
        self.source = {'id': 'shared-fixture', 'kind': 'wordpress_api',
                       'url': 'https://publisher.invalid/wp-json/wp/v2/posts',
                       'api_url': 'https://publisher.invalid/wp-json/wp/v2/posts',
                       'allowed_hosts': ['publisher.invalid'], 'enabled': True,
                       'terms_approved': True, 'robots_required': True,
                       'download_articles': False, 'article_terms_approved': False,
                       'document_kind': 'news', 'min_interval_seconds': 5,
                       'index_ttl_seconds': 900, 'max_bytes': 2_000_000}
        self.settings = {'strategy': 'shared', 'start': '2020-01-01', 'end': '2020-12-31',
                         'execution_mode': 'quick', 'session_seconds': 120,
                         'include_aliases': True, 'use_context': True}
        self.save()
        self.calls = []

    def save(self):
        self.news.config_path.write_text(json.dumps({'sources': [self.source], 'search': self.settings}))

    def rows(self, *, count=100, hits=7, bodies=0):
        return [{'id': index + 1,
                 'link': ('https://publisher.invalid/' if index < bodies else 'https://unapproved.invalid/') + 'story-' + str(index),
                 'date_gmt': '2020-06-01T12:00:00',
                 'title': {'rendered': self.entities[index]['name'] + ' presenta informe' if index < hits else
                           'Persona Nueva Encontrable' if index == 7 else 'Noticia general de economía ' + str(index)},
                 'excerpt': {'rendered': '<p>Referencia pública de prueba sin cuerpo editorial.</p>'}}
                for index in range(count)]

    def fetcher(self, responses):
        owner = self
        class FixtureFetcher:
            def __init__(self, *_args, **_kwargs):
                pass

            def fetch(self, url, source):
                owner.calls.append(url)
                body = responses(url, source)
                if isinstance(body, dict):
                    return body
                return {'status': 'ok', 'url': url, 'body': body, 'sha256': digest(body),
                        'retrieved_at': '2026-10-16T00:00:00+00:00'}
        return FixtureFetcher

    def run_native(self, responses, **options):
        with patch('peps.collector.SafeFetcher', self.fetcher(responses)):
            return self.news.acquire(queue=False, workers=1, seconds=1, **options)

    def execute_feed(self, fetcher, *, budget=None):
        with self.store.connection() as con:
            con.execute("UPDATE jobs SET due=0 WHERE kind='news_feed' AND status IN ('queued','deferred')")
        job = self.store.claim(kind_prefix='news_feed')
        self.assertIsNotNone(job)
        self.news.execute(job, fetcher, relevance=RelevanceIndex(self.store.iter_entities()), body_budget=budget)
        return job

    def database_count(self, table):
        with self.store.connection() as con:
            return con.execute('SELECT count(*) FROM ' + table).fetchone()[0]

    def public_transport(self, transport):
        fetcher = SafeFetcher({'cache_ttl_seconds': 0}, Path(self.temp.name) / 'wp-end-transport')
        self.addCleanup(fetcher.close)
        fetcher._dns = lambda *_args, **_kwargs: [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('93.184.216.34', 443))]
        fetcher._transport = transport
        return fetcher

    def test_native_acquisition_matches_300_names_once_and_downloads_only_3_approved_bodies(self):
        self.source.update(download_articles=True, article_terms_approved=True)
        self.save()
        rows = self.rows(bodies=3)
        def responses(url, _source):
            if '/wp-json/' in url:
                return json.dumps(rows)
            index = int(url.rsplit('-', 1)[-1])
            return '<html><title>Nota pública</title><article>' + self.entities[index]['name'] + ' presentó un informe detallado de actividades públicas. ' * 5 + '</article></html>'
        self.news.queue('native-seven')
        result = self.run_native(responses, max_articles=4, body_budget=3)
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(sum('/wp-json/' in url for url in self.calls), 1)
        self.assertEqual(self.database_count('news_articles'), 7)
        self.assertEqual(self.database_count('documents'), 3)
        self.assertEqual(self.database_count('news_identity_links'), 0)
        self.assertEqual(result['discovery_records'], 100)
        self.assertEqual(result['relevant_records'], 7)
        coverage = acquisition_coverage(self.store, self.news.sources())
        self.assertEqual(coverage['entities_total'], 300)
        self.assertEqual(coverage['entities_with_candidates'], 7)
        self.assertEqual(coverage['entities_with_bodies'], 3)
        self.assertEqual(coverage['entities_queried'], 0)
        with self.store.connection() as con:
            metadata = json.loads(con.execute('SELECT parser_metadata FROM news_discovery_runs').fetchone()[0])
        self.assertEqual(len(metadata['locally_checked_entity_ids']), 300)
        self.assertEqual(metadata['candidate_filter']['excluded_records'], 93)
        self.assertTrue(self.store.verify_audit()['ok'])

    def test_metadata_without_body_permission_never_creates_documents(self):
        self.news.queue('metadata')
        self.run_native(lambda *_: json.dumps(self.rows()), max_articles=1)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.database_count('news_articles'), 7)
        self.assertEqual(self.database_count('documents'), 0)
        with self.store.connection() as con:
            details = json.loads(con.execute('SELECT details FROM news_observations LIMIT 1').fetchone()[0])
        self.assertTrue(details['discovery_metadata']['discovery_only'])
        self.assertFalse(details['retrieval_relevance']['identity_verified'])

    def test_pagination_parent_replay_is_idempotent_and_respects_exact_period(self):
        self.news.queue('pagination')
        fixture = self.fetcher(lambda *_: json.dumps(self.rows()))()
        parent = self.execute_feed(fixture)
        with self.store.connection() as con:
            children = [json.loads(row[0]) for row in con.execute("SELECT payload FROM jobs WHERE kind='news_feed' AND id!=?", (parent['id'],))]
        self.assertEqual(len(children), 1)
        child = children[0]
        self.assertEqual(child['page'], 2)
        for field in ('crawl_scope', 'start', 'end', 'scope_start', 'scope_end'):
            self.assertEqual(child[field], parent['payload'][field])
        query = parse_qs(urlsplit(child['url']).query)
        self.assertEqual(query['page'], ['2'])
        self.assertEqual(query['per_page'], ['100'])
        self.news.execute(parent, fixture, relevance=RelevanceIndex(self.store.iter_entities()))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.database_count('news_shared_queue'), 2)
        self.assertEqual(self.database_count('news_articles'), 7)
        self.assertEqual(self.database_count('news_observations'), 7)

    def test_inferred_wordpress_last_page_error_finishes_without_backoff(self):
        self.source.update(robots_required=False, min_interval_seconds=0)
        self.save()
        self.news.queue('wordpress-end')
        calls = []
        def transport(url, _headers, _timeout, _maximum, _addresses):
            calls.append(url)
            if parse_qs(urlsplit(url).query)['page'] == ['1']:
                return 200, {'content-type': 'application/json'}, json.dumps(self.rows()).encode()
            return 400, {'content-type': 'application/json'}, json.dumps({
                'code': 'rest_post_invalid_page_number', 'message': 'The page number requested is larger than the number of pages available.',
                'data': {'status': 400}}).encode()
        fetcher = self.public_transport(transport)
        self.execute_feed(fetcher)
        last = self.execute_feed(fetcher)
        with self.store.connection() as con:
            job = dict(con.execute('SELECT * FROM jobs WHERE id=?', (last['id'],)).fetchone())
        self.assertEqual(job['status'], 'done')
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.database_count('news_articles'), 7)
        self.assertIsNone(self.store.next_due('news_feed'))

    def test_first_wordpress_page_error_is_not_reported_as_successful_empty_index(self):
        self.source.update(robots_required=False, min_interval_seconds=0)
        self.save()
        self.news.queue('wordpress-first-error')
        fetcher = self.public_transport(lambda *_: (400, {'content-type': 'application/json'},
            b'{"code":"rest_post_invalid_page_number","data":{"status":400}}'))
        first = self.execute_feed(fetcher)
        with self.store.connection() as con:
            self.assertNotEqual(con.execute('SELECT status FROM jobs WHERE id=?', (first['id'],)).fetchone()[0], 'done')
        self.assertEqual(self.database_count('news_discovery_runs'), 0)

    def test_other_wordpress_400_error_after_full_page_is_not_reported_as_end_of_index(self):
        self.source.update(robots_required=False, min_interval_seconds=0)
        self.save()
        self.news.queue('wordpress-other-error')
        def transport(url, *_args):
            if parse_qs(urlsplit(url).query)['page'] == ['1']:
                return 200, {'content-type': 'application/json'}, json.dumps(self.rows()).encode()
            return 400, {'content-type': 'application/json'}, b'{"code":"rest_invalid_param","message":"bad request","data":{"status":400}}'
        fetcher = self.public_transport(transport)
        self.execute_feed(fetcher)
        last = self.execute_feed(fetcher)
        with self.store.connection() as con:
            self.assertNotEqual(con.execute('SELECT status FROM jobs WHERE id=?', (last['id'],)).fetchone()[0], 'done')
        self.assertEqual(self.database_count('news_discovery_runs'), 1)

    def test_new_bank_revision_reuses_warm_metadata_without_http_and_finds_new_name(self):
        self.news.queue('warm')
        fixture = self.fetcher(lambda *_: json.dumps(self.rows()))()
        first = self.execute_feed(fixture)
        self.store.import_entities([{'id': '299', 'name': 'Persona Nueva Encontrable'}])
        self.news.queue('warm')
        never_http = self.fetcher(lambda *_: self.fail('Warm metadata must not request HTTP'))()
        second = self.execute_feed(never_http)
        self.assertNotEqual(first['payload']['bank_revision'], second['payload']['bank_revision'])
        self.assertEqual(self.database_count('news_articles'), 8)
        self.assertEqual(len(self.calls), 1)
        with self.store.connection() as con:
            run = json.loads(con.execute('SELECT parser_metadata FROM news_discovery_runs WHERE job_id=?', (second['id'],)).fetchone()[0])
        self.assertTrue(run['metadata_cache_hit'])
        self.assertEqual(run['candidate_filter']['retained_records'], 8)

    def test_warm_paginated_bank_revision_refilters_both_pages_without_waiting_for_http_spacing(self):
        self.news.queue('warm-all-pages')
        fixture = self.fetcher(lambda url, _source: json.dumps(self.rows() if parse_qs(urlsplit(url).query)['page'] == ['1'] else []))()
        self.execute_feed(fixture)
        self.execute_feed(fixture)
        self.assertEqual(len(self.calls), 2)
        self.store.import_entities([{'id': '299', 'name': 'Persona Nueva Encontrable'}])
        self.news.queue('warm-all-pages')
        result = self.run_native(lambda *_: self.fail('Warm paginated crawl must not request HTTP'), max_articles=2)
        self.assertEqual(result['completed'], 2)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.database_count('news_articles'), 8)
        coverage = acquisition_coverage(self.store, self.news.sources())
        self.assertEqual(coverage['shared_pages_read'], 2)
        self.assertEqual(coverage['shared_records_checked'], 100)
        self.assertEqual(coverage['entities_checked_locally'], 300)
        self.assertEqual(coverage['shared_pages_pending'], 0)
        old_flag = self.store._audit_json
        try:
            self.store._audit_json = False
            fallback = acquisition_coverage(self.store, self.news.sources())
        finally:
            self.store._audit_json = old_flag
        for key in ('shared_pages_read', 'shared_records_checked', 'entities_checked_locally', 'shared_pages_pending'):
            self.assertEqual(coverage[key], fallback[key], key)

    def test_full_wordpress_pages_stop_at_24_requests_per_quick_scope(self):
        self.news.queue('cap')
        fixture = self.fetcher(lambda *_: json.dumps(self.rows(hits=0)))()
        for _ in range(24):
            self.execute_feed(fixture)
        self.assertEqual(len(self.calls), 24)
        self.assertEqual(self.database_count('news_shared_queue'), 24)
        with self.store.connection() as con:
            pages = [json.loads(row[0]) for row in con.execute('SELECT parser_metadata FROM news_discovery_runs')]
        self.assertEqual(sum(p['continuation']['page_limit_reached'] for p in pages), 1)
        self.assertIsNone(self.store.claim(kind_prefix='news_feed'))
        self.assertEqual(acquisition_coverage(self.store, self.news.sources())['entities_queried'], 0)

    def test_long_history_keeps_24_newest_year_seeds_in_descending_order(self):
        self.settings.update(start='1990-01-01', end='2026-10-15')
        self.save()
        self.news.queue('newest-seeds')
        with self.store.connection() as con:
            pages = [json.loads(row[0]) for row in con.execute("SELECT payload FROM jobs WHERE kind='news_feed' ORDER BY due,id")]
        self.assertEqual(len(pages), 24)
        self.assertEqual([int(page['start'][:4]) for page in pages], list(range(2026, 2002, -1)))
        self.assertTrue(all(page['page'] == 1 for page in pages))
        self.assertEqual(self.database_count('news_shared_queue'), 24)
        self.assertEqual(self.calls, [])

    def test_source_permissions_and_robots_denials_preserve_zero_network_boundary(self):
        self.source['terms_approved'] = False
        self.save()
        self.assertEqual(self.news.queue('unapproved')['tasks'], 0)
        self.source['terms_approved'] = True
        self.save()
        self.news.queue('robots')
        fetcher = SafeFetcher({}, Path(self.temp.name) / 'robots-test')
        self.addCleanup(fetcher.close)
        with fetcher._db() as con:
            con.execute('INSERT INTO robots VALUES(?,?,?,?)',
                        ('https://publisher.invalid', 'User-agent: *\nDisallow: /\n', time.time() + 900, time.time()))
        # The existing transport validates the destination before consulting
        # robots. DNS is an in-memory fixture; no real network is permitted.
        fetcher._dns = lambda *_args, **_kwargs: [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('93.184.216.34', 443))]
        fetcher._transport = Mock(side_effect=AssertionError('Cached robots disallow must not make HTTP'))
        job = self.execute_feed(fetcher)
        with self.store.connection() as con:
            self.assertEqual(con.execute('SELECT status FROM jobs WHERE id=?', (job['id'],)).fetchone()[0], 'blocked')
        self.assertEqual(self.database_count('news_articles'), 0)
        fetcher._transport.assert_not_called()

    def test_permission_revoked_after_queue_blocks_before_fetch_or_cached_parse(self):
        self.news.queue('permission-revoked')
        self.source['enabled'] = False
        self.save()
        fixture = self.fetcher(lambda *_: self.fail('Revoked source must not be fetched'))()
        job = self.execute_feed(fixture)
        self.assertEqual(self.calls, [])
        with self.store.connection() as con:
            self.assertEqual(con.execute('SELECT status FROM jobs WHERE id=?', (job['id'],)).fetchone()[0], 'blocked')

    def test_global_and_per_entity_body_quotas_apply_to_shared_results(self):
        self.source.update(download_articles=True, article_terms_approved=True)
        self.save()
        rows = self.rows(count=6, hits=6, bodies=6)
        for row in rows[:4]:
            row['title']['rendered'] = self.entities[0]['name']
        self.news.queue('budget')
        budget = _BodyBudget(maximum=3, per_entity=1)
        self.execute_feed(self.fetcher(lambda *_: json.dumps(rows))(), budget=budget)
        with self.store.connection() as con:
            payloads = [json.loads(row[0]) for row in con.execute("SELECT payload FROM jobs WHERE kind='news_article'")]
        self.assertEqual(len(payloads), 3)
        self.assertEqual(sum('0' in row['candidate_entity_ids'] for row in payloads), 1)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.database_count('documents'), 0)

    def test_alias_needs_nearby_context_and_does_not_become_identity_evidence(self):
        self.store.import_entities([dict(self.entities[0], aliases=['La Nube'], context={'state': 'Oaxaca'})])
        rows = self.rows(count=3, hits=0)
        rows[0]['title']['rendered'] = 'La Nube presenta informe'
        rows[1]['title']['rendered'] = 'La Nube presenta informe en Oaxaca'
        rows[2]['title']['rendered'] = self.entities[1]['name'] + ' presentó actividades'
        self.news.queue('aliases')
        self.execute_feed(self.fetcher(lambda *_: json.dumps(rows))())
        self.assertEqual(self.database_count('news_articles'), 2)
        with self.store.connection() as con:
            details = [json.loads(row[0]) for row in con.execute('SELECT details FROM news_observations')]
        self.assertEqual({entity for row in details for entity in row['retrieval_relevance']['entity_ids']}, {'0', '1'})
        self.assertEqual(self.database_count('news_identity_links'), 0)

    def test_sitemap_slug_and_lastmod_are_only_discovery_not_publication_or_full_coverage(self):
        self.source.update(kind='sitemap', url='https://publisher.invalid/sitemap.xml')
        self.source.pop('api_url')
        self.save()
        xml = '<urlset><url><loc>https://publisher.invalid/persona-sintetica-inventada-0</loc><lastmod>2026-10-16</lastmod></url></urlset>'
        self.news.queue('sitemap')
        self.execute_feed(self.fetcher(lambda *_: xml)())
        with self.store.connection() as con:
            article = dict(con.execute('SELECT * FROM news_articles').fetchone())
            detail = json.loads(con.execute('SELECT details FROM news_observations').fetchone()[0])
        self.assertEqual(article['scope'], 'feed_metadata_only')
        self.assertIsNone(article['published_at'])
        self.assertEqual(detail['discovery_metadata']['lastmod'], '2026-10-16')
        self.assertTrue(detail['discovery_metadata']['title_from_slug'])
        self.assertEqual(self.database_count('documents'), 0)
        self.assertEqual(acquisition_coverage(self.store, self.news.sources())['entities_queried'], 0)

    def test_warm_sitemap_cache_reapplies_depth_limit_for_current_path(self):
        self.source.update(kind='sitemap', url='https://publisher.invalid/sitemap.xml')
        self.source.pop('api_url')
        self.save()
        xml = '<sitemapindex><sitemap><loc>https://publisher.invalid/child.xml</loc></sitemap></sitemapindex>'
        self.news.queue('depth-cache')
        fixture = self.fetcher(lambda *_: xml)()
        parent = self.execute_feed(fixture)
        deep = dict(parent['payload'], depth=3, crawl_scope='synthetic-deep-path')
        identifier = self.store.enqueue('news_feed', deep, key='deep-cached-index', due=-1)
        job = self.store.claim(kind_prefix='news_feed')
        self.assertEqual(job['id'], identifier)
        self.news.execute(job, fixture, relevance=RelevanceIndex(self.store.iter_entities()))
        self.assertEqual(len(self.calls), 1)
        with self.store.connection() as con:
            count = con.execute('SELECT count(*) FROM news_shared_queue WHERE scope=?', ('synthetic-deep-path',)).fetchone()[0]
            metadata = json.loads(con.execute('SELECT parser_metadata FROM news_discovery_runs WHERE job_id=?', (identifier,)).fetchone()[0])
        self.assertEqual(count, 0)
        self.assertTrue(metadata['pagination']['depth_limited'])
        self.assertTrue(metadata['pagination']['truncated'])

    def test_returning_to_previous_profile_reactivates_cancelled_pagination_descendants(self):
        self.news.queue('reactivation')
        fixture = self.fetcher(lambda *_: json.dumps(self.rows()))()
        parent = self.execute_feed(fixture)
        with self.store.connection() as con:
            child_id = con.execute("SELECT id FROM jobs WHERE kind='news_feed' AND id!=?", (parent['id'],)).fetchone()[0]
        self.settings['strategy'] = 'feeds'
        self.save()
        self.news.queue('reactivation')
        with self.store.connection() as con:
            self.assertEqual(con.execute('SELECT status FROM jobs WHERE id=?', (child_id,)).fetchone()[0], 'cancelled')
        self.settings['strategy'] = 'shared'
        self.save()
        self.news.queue('reactivation')
        with self.store.connection() as con:
            child = dict(con.execute('SELECT * FROM jobs WHERE id=?', (child_id,)).fetchone())
        self.assertIn(child['status'], ('queued', 'deferred'))
        self.assertIsNone(child['owner'])


if __name__ == '__main__':
    unittest.main()
