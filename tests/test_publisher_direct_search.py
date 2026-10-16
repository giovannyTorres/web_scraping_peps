"""Directed publisher retrieval: exact phrases, bounded leads, no fake bodies."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

from peps.shared_crawl import supports_wordpress_search, wordpress_search_url
from peps.shared_discovery import DiscoveryParseError, parse_wordpress_search
from peps.sources import parse_results


def source(**updates):
    return dict({'id': 'contralinea_index', 'kind': 'wordpress_api',
                 'url': 'https://contralinea.com.mx/wp-json/wp/v2/posts',
                 'allowed_hosts': ['contralinea.com.mx'], 'max_records': 100,
                 'enabled': False, 'terms_approved': False,
                 'download_articles': False, 'article_terms_approved': False}, **updates)


def posts(count=1):
    return [{'id': index + 1, 'link': 'https://contralinea.com.mx/article-' + str(index),
             'date_gmt': '2020-02-03T10:00:00', 'date': '2020-02-03T04:00:00',
             'title': {'rendered': '<b>Documento público</b>'},
             'excerpt': {'rendered': '<p>Extracto sin el nombre buscado.</p>'},
             'content': {'rendered': '<p>ORIGINAL_RESTRICTED Roberto Núñez y Bandera</p>'}}
            for index in range(count)]


def fetched(rows=None, *, url=None, headers=None):
    return {'status': 'ok', 'body': json.dumps(posts() if rows is None else rows),
            'final_url': url or wordpress_search_url(source(), '"Roberto Núñez y Bandera"',
                                                     '20160101000000', '20261017000000'),
            'pagination_headers': headers or {}}


class PublisherDirectUrlTests(unittest.TestCase):
    def test_saved_known_profile_gains_capability_without_enabling_permissions(self):
        saved = source()
        snapshot = deepcopy(saved)
        self.assertTrue(supports_wordpress_search(saved))
        wordpress_search_url(saved, '"Roberto Núñez y Bandera"',
                             '20160101000000', '20261017000000')
        self.assertEqual(saved, snapshot)
        self.assertFalse(saved['enabled'])
        self.assertFalse(saved['article_terms_approved'])

    def test_unknown_profiles_require_explicit_adapter_and_exact_scoped_endpoint(self):
        custom = source(id='custom', url='https://publisher.invalid/wp-json/wp/v2/posts',
                        allowed_hosts=['publisher.invalid'])
        self.assertFalse(supports_wordpress_search(custom))
        custom['search_kind'] = 'wordpress_api_search'
        self.assertTrue(supports_wordpress_search(custom))
        for changes in ({'kind': 'rss'}, {'search_kind': 'wordpress_rss'},
                        {'allowed_hosts': ['another.invalid']},
                        {'url': 'https://publisher.invalid/other'},
                        {'url': 'https://publisher.invalid/wp-json/wp/v2/posts?search=x'},
                        {'url': 'https://publisher.invalid/wp-json/wp/v2/posts#fragment'},
                        {'url': 'https://publisher.invalid:444/wp-json/wp/v2/posts'},
                        {'url': 'https://user:pass@publisher.invalid/wp-json/wp/v2/posts'},
                        {'url': 'https://localhost/wp-json/wp/v2/posts', 'allowed_hosts': ['localhost']},
                        {'url': 'https://127.0.0.1/wp-json/wp/v2/posts', 'allowed_hosts': ['127.0.0.1']}):
            with self.subTest(changes=changes):
                self.assertFalse(supports_wordpress_search({**custom, **changes}))

    def test_exact_query_is_encoded_once_and_does_not_request_original_content(self):
        query = '"Roberto Núñez y Bandera"'
        result = wordpress_search_url(source(), query, '20160101000000', '20261017000000')
        params = parse_qs(urlsplit(result).query)
        self.assertEqual(params['search'], [query])
        self.assertEqual(params['per_page'], ['10'])
        self.assertEqual(params['page'], ['1'])
        self.assertEqual(params['orderby'], ['relevance'])
        self.assertEqual(params['after'], ['2015-12-31T23:59:59Z'])
        self.assertEqual(params['before'], ['2026-10-17T00:00:00Z'])
        self.assertNotIn('content', params['_fields'][0])
        self.assertNotIn('password', params)

    def test_compact_and_datetime_boundaries_have_identical_utc_meaning(self):
        offset = timezone(-timedelta(hours=6))
        args = source(), '"Nombre Público"'
        from_compact = wordpress_search_url(*args, '20200101000000', '20210101000000')
        from_datetime = wordpress_search_url(*args, datetime(2019, 12, 31, 18, tzinfo=offset),
                                             datetime(2020, 12, 31, 18, tzinfo=offset))
        self.assertEqual(from_compact, from_datetime)

    def test_invalid_query_dates_and_limits_are_explicit_errors(self):
        for query in ('', ' ', 'a' * 1501, 'name\nother', None):
            with self.subTest(query=repr(query)[:30]), self.assertRaises(ValueError):
                wordpress_search_url(source(), query, '20200101000000', '20210101000000')
        for options in ({'page': 0}, {'page': True}, {'page': 10001},
                        {'per_page': 0}, {'per_page': 101}, {'per_page': '10'}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                wordpress_search_url(source(), '"Name"', '20200101000000', '20210101000000', **options)
        with self.assertRaises(ValueError):
            wordpress_search_url(source(), '"Name"', '20210101000000', '20200101000000')


class PublisherDirectParserTests(unittest.TestCase):
    def test_body_only_search_hit_retains_provenance_without_creating_body_or_identity(self):
        response = fetched()
        with patch('socket.getaddrinfo', side_effect=AssertionError('Parser must not use DNS')):
            records = parse_results(response, source())
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record['text'], 'Extracto sin el nombre buscado.')
        self.assertEqual(record['published_at'], '2020-02-03T10:00:00+00:00')
        self.assertEqual(record['source_kind'], 'news')
        self.assertEqual(record['metadata']['wordpress_id'], 1)
        self.assertTrue(record['metadata']['publisher_query_match'])
        self.assertEqual(record['metadata']['publisher_search_query'], '"Roberto Núñez y Bandera"')
        self.assertTrue(record['metadata']['discovery_only'])
        self.assertTrue(record['metadata']['excerpt_only'])
        self.assertNotIn('ORIGINAL_RESTRICTED', json.dumps(records))
        self.assertNotIn('identity_verified', record)

    def test_more_publisher_results_are_reported_as_partial_without_following_pages(self):
        response = fetched(posts(10), headers={'X-WP-Total': '29', 'X-WP-TotalPages': '3'})
        records = parse_results(response, source())
        self.assertEqual(len(records), 10)
        info = response['parse_metadata']
        self.assertTrue(info['truncated'])
        self.assertEqual(info['observed_entries'], 10)
        self.assertEqual(info['pagination']['total'], 29)
        self.assertEqual(info['pagination']['next_page'], 2)
        self.assertTrue(info['pagination']['continuation_required'])
        self.assertTrue(info['pagination']['page_bounded'])

    def test_configured_result_limit_applies_even_if_server_ignores_requested_page_size(self):
        response = fetched(posts(15))
        records = parse_results(response, source(search_max_records=3))
        self.assertEqual(len(records), 3)
        self.assertEqual(response['parse_metadata']['observed_entries'], 15)
        self.assertTrue(response['parse_metadata']['truncated'])

    def test_valid_empty_result_is_distinct_from_error_or_oversized_response(self):
        response = fetched([])
        self.assertEqual(parse_results(response, source()), [])
        self.assertFalse(response['parse_metadata']['truncated'])
        for body in ('{"code":"rest_forbidden"}', 'null', '[{}]', 'not json'):
            response = fetched()
            response['body'] = body
            with self.subTest(body=body), self.assertRaises(ValueError):
                parse_results(response, source())
        with self.assertRaisesRegex(ValueError, 'max_bytes'):
            parse_results(fetched(), source(max_bytes=20))

    def test_duplicate_or_malformed_query_parameters_cannot_complete_silently(self):
        base = source()['url']
        for params in ('search=A&search=B', 'search=', 'search=A&page=0',
                       'search=A&per_page=101', 'search=A&page=1&page=2',
                       'search=A&per_page=NaN'):
            with self.subTest(params=params), self.assertRaises(ValueError):
                parse_results(fetched(url=base + '?' + params), source())
        with self.assertRaises(ValueError):
            parse_results(fetched(), source(id='unknown_unmarked_profile'))

    def test_response_redirect_losing_query_is_not_asserted_as_direct_name_hit(self):
        response = fetched(url=source()['url'])
        records = parse_results(response, source())
        self.assertFalse(response['parse_metadata']['directed_publisher_search'])
        self.assertNotIn('publisher_query_match', records[0]['metadata'])

    def test_search_hit_marker_requires_response_from_the_declared_api(self):
        for url in ('https://another.invalid/wp-json/wp/v2/posts?search=A',
                    'https://contralinea.com.mx/unrelated.json?search=A',
                    'https://user:password@contralinea.com.mx/wp-json/wp/v2/posts?search=A',
                    'https://contralinea.com.mx/wp-json/wp/v2/posts?search=A#fragment'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                parse_results(fetched(url=url), source())

    def test_unsafe_duplicate_article_urls_and_scripts_are_not_retained(self):
        rows = posts(4)
        rows[1]['link'] = rows[0]['link']
        rows[2]['link'] = 'https://127.0.0.1/article'
        rows[3]['title'] = {'rendered': '<script>SECRET</script>Artículo'}
        response = fetched(rows)
        records = parse_results(response, source())
        self.assertEqual(len(records), 2)
        self.assertEqual(records[-1]['title'], 'Artículo')
        self.assertEqual(response['parse_metadata']['pagination']['excluded_urls'], 1)

    def test_direct_parser_validates_search_query_and_never_reuses_content_as_excerpt(self):
        for query in ('', 'x\n', None):
            with self.subTest(query=query), self.assertRaises(DiscoveryParseError):
                parse_wordpress_search(json.dumps(posts()), query=query)
        parsed = parse_wordpress_search(json.dumps(posts()), query='"Name"')
        self.assertNotIn('ORIGINAL_RESTRICTED', json.dumps(parsed))


if __name__ == '__main__':
    unittest.main()
