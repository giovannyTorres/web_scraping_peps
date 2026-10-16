"""Offline regression proposal; copy into tests after applying the runtime patch."""
from copy import deepcopy
import json
import unittest
from urllib.parse import parse_qs, urlsplit

from peps.shared_crawl import supports_wordpress_search, wordpress_search_url
from peps.sources import parse_results


def profile(**changes):
    value = dict(id='subdirectory_example', kind='wordpress_api',
                 search_kind='wordpress_api_search',
                 url='https://publisher.invalid/noticias/wp-json/wp/v2/posts',
                 base_url='https://publisher.invalid/noticias/',
                 allowed_hosts=['publisher.invalid'], search_max_records=3, max_records=3,
                 enabled=False, terms_approved=False, download_articles=False,
                 article_terms_approved=False, body_terms_approved=False)
    return dict(value, **changes)


def response(source, final_url=None):
    url = wordpress_search_url(source, '"Nombre público"', '20200101000000', '20210101000000')
    rows = [dict(id=1, link='https://publisher.invalid/noticias/documento/',
                 date_gmt='2020-05-06T10:00:00', date='2020-05-06T04:00:00',
                 title={'rendered': 'Documento público'},
                 excerpt={'rendered': '<p>Un notario atiende un trámite.</p>'},
                 content={'rendered': '<p>Texto del original que nunca debe atribuirse al extracto.</p>'})]
    return dict(status='ok', final_url=final_url or url, body=json.dumps(rows), pagination_headers={})


class WordPressSubdirectoryTests(unittest.TestCase):
    def test_public_installation_prefix_builds_exact_bounded_metadata_query(self):
        source = profile()
        before = deepcopy(source)
        self.assertTrue(supports_wordpress_search(source))
        built = wordpress_search_url(source, '"Nombre público"', '20200101000000', '20210101000000')
        parts = urlsplit(built)
        self.assertEqual(parts.path, '/noticias/wp-json/wp/v2/posts')
        params = parse_qs(parts.query)
        self.assertEqual(params['search'], ['"Nombre público"'])
        self.assertEqual(params['per_page'], ['3'])
        self.assertNotIn('content', params['_fields'][0])
        self.assertEqual(source, before)
        self.assertTrue(all(source[key] is False for key in
                            ('enabled', 'terms_approved', 'download_articles',
                             'article_terms_approved', 'body_terms_approved')))

    def test_declared_nested_installation_and_optional_single_trailing_slash(self):
        for path in ('/wp-json/wp/v2/posts', '/wp-json/wp/v2/posts/',
                     '/site_2/news-es/wp-json/wp/v2/posts'):
            with self.subTest(path=path):
                self.assertTrue(supports_wordpress_search(profile(url='https://publisher.invalid' + path)))

    def test_twenty_non_normalized_or_misleading_paths_are_rejected(self):
        paths = ('/noticias/../wp-json/wp/v2/posts', '/noticias/./wp-json/wp/v2/posts',
                 '//noticias/wp-json/wp/v2/posts', '/noticias//wp-json/wp/v2/posts',
                 '/noticias%2fwp-json/wp/v2/posts', '/noticias/%2e%2e/wp-json/wp/v2/posts',
                 '/noticias/%252e%252e/wp-json/wp/v2/posts', '/noticias\\wp-json/wp/v2/posts',
                 '/noticias/wp-json/wp/v2/posts/extra', '/noticias/wp-json/wp/v2/postscript',
                 '/noticias/not-wp-json/wp/v2/posts', '/noticias/wp-json/wp/v2/posts.json',
                 '/noticias/wp-json/wp/v2/posts;', '/noticias/wp-json/wp/v2/posts//',
                 '/noticias/wp-json/wp/v2/posts?search=otro', '/noticias/wp-json/wp/v2/posts#otro',
                 '/noticias/\nwp-json/wp/v2/posts', '/noticias/%00/wp-json/wp/v2/posts',
                 '/.private/wp-json/wp/v2/posts', '/noticias:80/wp-json/wp/v2/posts')
        self.assertEqual(len(paths), 20)
        for path in paths:
            with self.subTest(path=path):
                source = profile(url='https://publisher.invalid' + path)
                self.assertFalse(supports_wordpress_search(source))
                with self.assertRaises(ValueError):
                    wordpress_search_url(source, 'Name', '20200101000000', '20210101000000')

    def test_explicit_source_host_https_and_public_scope_still_required(self):
        for changes in ({'search_kind': None}, {'search_kind': 'wordpress_rss'},
                        {'kind': 'rss'}, {'allowed_hosts': ['other.invalid']},
                        {'url': 'http://publisher.invalid/noticias/wp-json/wp/v2/posts'},
                        {'url': 'https://user:password@publisher.invalid/noticias/wp-json/wp/v2/posts'},
                        {'url': 'https://publisher.invalid:444/noticias/wp-json/wp/v2/posts'},
                        {'url': 'https://127.0.0.1/noticias/wp-json/wp/v2/posts', 'allowed_hosts': ['127.0.0.1']},
                        {'url': 'https://localhost/noticias/wp-json/wp/v2/posts', 'allowed_hosts': ['localhost']}):
            with self.subTest(changes=changes):
                self.assertFalse(supports_wordpress_search(profile(**changes)))

    def test_legacy_known_profiles_remain_exact_and_custom_prefix_requires_marker(self):
        source = profile(id='contralinea_index', search_kind=None,
                         url='https://contralinea.com.mx/wp-json/wp/v2/posts',
                         allowed_hosts=['contralinea.com.mx'])
        self.assertTrue(supports_wordpress_search(source))
        source['url'] = 'https://contralinea.com.mx/noticias/wp-json/wp/v2/posts'
        self.assertFalse(supports_wordpress_search(source))

    def test_directed_parser_accepts_exact_declared_subdirectory_without_original_body(self):
        parsed = parse_results(response(profile()), profile())
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]['url'], 'https://publisher.invalid/noticias/documento/')
        self.assertNotIn('Texto del original', json.dumps(parsed))

    def test_redirected_root_sibling_path_and_allowed_other_origin_are_rejected(self):
        source = profile(allowed_hosts=['publisher.invalid', 'other.invalid'])
        for endpoint in ('https://publisher.invalid/wp-json/wp/v2/posts',
                         'https://publisher.invalid/other/wp-json/wp/v2/posts',
                         'https://other.invalid/noticias/wp-json/wp/v2/posts'):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                parse_results(response(source, endpoint + '?search=Name&per_page=3&page=1'), source)

    def test_prefix_and_segment_sizes_are_bounded(self):
        for path in ('/' + 'a' * 129 + '/wp-json/wp/v2/posts',
                     '/x' * 9 + '/wp-json/wp/v2/posts'):
            with self.subTest(path=path):
                self.assertFalse(supports_wordpress_search(profile(url='https://publisher.invalid' + path)))


if __name__ == '__main__':
    unittest.main()
