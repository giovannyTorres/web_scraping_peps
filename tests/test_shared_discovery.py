"""Shared publisher discovery stays bounded and never becomes article evidence."""
import gzip
import json
import unittest
from unittest.mock import patch

from peps.shared_discovery import DiscoveryParseError, parse_sitemap, parse_wordpress_posts


def posts(count=1):
    return [{'id': index + 1, 'link': 'https://publisher.invalid/story-' + str(index),
             'title': {'rendered': 'Persona &amp; <b>Sintética</b>'},
             'excerpt': {'rendered': '<p>Resumen <strong>público</strong></p><p>Otro dato</p>'},
             'date_gmt': '2020-01-02T03:04:05', 'date': '2020-01-01T21:04:05',
             'content': {'rendered': 'UNTRUSTED CONTENT NEVER USED'}} for index in range(count)]


class SharedSitemapTests(unittest.TestCase):
    def test_news_titles_dates_and_syntax_are_metadata_only(self):
        xml = '''<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
          xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">
          <url><loc>https://publisher.invalid/a?x=1&amp;y=2#tracking</loc><lastmod>2026-10-16</lastmod>
            <news:news><news:title>Persona &amp; entidad</news:title>
              <news:publication_date>2020-01-02T03:04:05Z</news:publication_date></news:news></url>
          </urlset>'''
        result = parse_sitemap(xml)
        record = result['records'][0]
        self.assertEqual(record['url'], 'https://publisher.invalid/a?x=1&y=2')
        self.assertEqual(record['title'], 'Persona & entidad')
        self.assertEqual(record['text'], '')
        self.assertEqual(record['published_at'], '2020-01-02T03:04:05+00:00')
        self.assertEqual(record['metadata']['lastmod'], '2026-10-16')
        self.assertTrue(record['metadata']['discovery_only'])
        self.assertFalse(record['metadata']['title_from_slug'])

    def test_slug_is_decoded_and_lastmod_never_becomes_publication(self):
        xml = '<urlset><url><loc>https://publisher.invalid/2020/ni%C3%B1a-y-persona_sintetica.html</loc><lastmod>2026-10-16</lastmod></url></urlset>'
        record = parse_sitemap(xml)['records'][0]
        self.assertEqual(record['title'], 'niña y persona sintetica')
        self.assertIsNone(record['published_at'])
        self.assertTrue(record['metadata']['title_from_slug'])

    def test_only_real_news_namespace_supplies_publication_date(self):
        xml = '<urlset xmlns:n="https://untrusted.invalid/news"><url><loc>https://publisher.invalid/a</loc><n:news><n:title>False</n:title><n:publication_date>2020-01-01</n:publication_date></n:news></url></urlset>'
        record = parse_sitemap(xml)['records'][0]
        self.assertEqual(record['title'], 'a')
        self.assertIsNone(record['published_at'])

    def test_nested_index_links_are_data_and_depth_limit_is_explicit(self):
        xml = '<sitemapindex><sitemap><loc>https://publisher.invalid/posts.xml</loc><lastmod>2026-10-16</lastmod></sitemap></sitemapindex>'
        with patch('socket.getaddrinfo', side_effect=AssertionError('No DNS in parsers')):
            result = parse_sitemap(xml, depth=1, max_depth=2)
            stopped = parse_sitemap(xml, depth=2, max_depth=2)
        self.assertEqual(result['records'], [])
        self.assertEqual(result['children'], [{'url': 'https://publisher.invalid/posts.xml', 'lastmod': '2026-10-16'}])
        self.assertFalse(result['pagination']['truncated'])
        self.assertEqual(stopped['children'], [])
        self.assertTrue(stopped['pagination']['depth_limited'])
        self.assertTrue(stopped['pagination']['truncated'])

    def test_deduplication_and_max_items_preserve_actual_observed_count(self):
        xml = '<urlset>' + ''.join('<url><loc>https://publisher.invalid/' + str(index // 2) + '</loc></url>' for index in range(6)) + '</urlset>'
        result = parse_sitemap(xml, max_items=4)
        self.assertEqual(len(result['records']), 2)
        self.assertEqual(result['pagination']['observed_items'], 6)
        self.assertTrue(result['pagination']['truncated'])

    def test_unsafe_and_non_https_links_never_reach_output(self):
        urls = ['http://publisher.invalid/a', 'https://localhost/a', 'https://host.local/a',
                'https://127.0.0.1/a', 'https://169.254.169.254/a', 'https://[::1]/a',
                'https://user:password@publisher.invalid/a', 'https://publisher.invalid:444/a',
                'https://publisher.invalid\\@127.0.0.1/a', 'file:///etc/passwd', '//publisher.invalid/a']
        xml = '<urlset>' + ''.join('<url><loc>' + value + '</loc></url>' for value in urls) + '</urlset>'
        result = parse_sitemap(xml)
        self.assertEqual(result['records'], [])
        self.assertEqual(result['pagination']['excluded_urls'], len(urls))

    def test_gzip_accepts_only_bounded_expansion_and_plain_xml_is_not_decompressed(self):
        xml = b'<urlset><url><loc>https://publisher.invalid/a</loc></url></urlset>'
        self.assertEqual(parse_sitemap(xml), parse_sitemap(gzip.compress(xml)))
        with self.assertRaisesRegex(DiscoveryParseError, 'expandido'):
            parse_sitemap(gzip.compress(b'<urlset>' + b' ' * 5000 + b'</urlset>'), max_bytes=300)
        with self.assertRaisesRegex(DiscoveryParseError, 'Gzip'):
            parse_sitemap(b'\x1f\x8bcorrupt')
        with self.assertRaisesRegex(DiscoveryParseError, 'XML'):
            parse_sitemap(gzip.compress(gzip.compress(xml)))

    def test_forbids_entities_and_dtd_in_utf8_utf16_utf32(self):
        xml = '<!DOCTYPE urlset [<!ENTITY example "injected">]><urlset><url><loc>https://publisher.invalid/a</loc></url></urlset>'
        for encoding in ('utf-8', 'utf-16', 'utf-32'):
            with self.subTest(encoding=encoding), self.assertRaisesRegex(DiscoveryParseError, 'DTD'):
                parse_sitemap(xml.encode(encoding))

    def test_rejects_invalid_types_sizes_roots_and_bad_xml(self):
        for payload in (None, {}, b'', '<html/>', '<urlset>', b'\xff\xff'):
            with self.subTest(payload=payload), self.assertRaises(DiscoveryParseError):
                parse_sitemap(payload)
        for options in ({'max_items': 0}, {'max_bytes': True}, {'depth': -1}, {'max_depth': False}):
            with self.subTest(options=options), self.assertRaises(DiscoveryParseError):
                parse_sitemap('<urlset/>', **options)
        with self.assertRaisesRegex(DiscoveryParseError, 'max_bytes'):
            parse_sitemap('<urlset/>', max_bytes=2)

    def test_bom_xml_and_invalid_publication_dates(self):
        xml = '<urlset xmlns:n="http://www.google.com/schemas/sitemap-news/0.9"><url><loc>https://publisher.invalid/a</loc><n:news><n:publication_date>yesterday</n:publication_date></n:news></url></urlset>'
        self.assertIsNone(parse_sitemap(xml.encode('utf-16'))['records'][0]['published_at'])


class SharedWordPressTests(unittest.TestCase):
    def test_extracts_only_excerpt_title_and_publication_metadata(self):
        result = parse_wordpress_posts(json.dumps(posts()))
        record = result['records'][0]
        self.assertEqual(record['title'], 'Persona & Sintética')
        self.assertEqual(record['text'], 'Resumen público Otro dato')
        self.assertEqual(record['published_at'], '2020-01-02T03:04:05+00:00')
        self.assertTrue(record['metadata']['excerpt_only'])
        self.assertTrue(record['metadata']['discovery_only'])
        self.assertNotIn('UNTRUSTED', json.dumps(result))
        self.assertIsNone(result['pagination']['next_page'])

    def test_exact_pagination_headers_are_case_insensitive(self):
        result = parse_wordpress_posts(json.dumps(posts(2)), headers={'X-WP-Total': '202', 'X-WP-TotalPages': '3'}, page=2, per_page=100)
        self.assertEqual(result['pagination']['total'], 202)
        self.assertEqual(result['pagination']['next_page'], 3)
        self.assertFalse(result['pagination']['pagination_inferred'])
        final = parse_wordpress_posts(json.dumps(posts(2)), headers={'x-wp-totalpages': '3'}, page=3)
        self.assertIsNone(final['pagination']['next_page'])

    def test_without_headers_full_page_probes_one_more_page(self):
        full = parse_wordpress_posts(json.dumps(posts(100)), page=2)
        self.assertEqual(full['pagination']['next_page'], 3)
        self.assertTrue(full['pagination']['pagination_inferred'])
        empty = parse_wordpress_posts('[]', page=3)
        self.assertIsNone(empty['pagination']['next_page'])
        self.assertEqual(empty['records'], [])

    def test_invalid_headers_fall_back_to_inferred_pagination(self):
        for value in ('NaN', '-1', 'Infinity', '1.2', None):
            with self.subTest(value=value):
                result = parse_wordpress_posts(json.dumps(posts(2)), per_page=2, headers={'X-WP-TotalPages': value})
                self.assertEqual(result['pagination']['next_page'], 2)
                self.assertTrue(result['pagination']['pagination_inferred'])

    def test_malformed_and_authentication_error_json_are_explicit_failures(self):
        for payload in ('not json', '{}', '{"code":"rest_forbidden","message":"Denied"}', '[null]', '[{}]', '[{"link":123}]', '42', 'null'):
            with self.subTest(payload=payload), self.assertRaises(DiscoveryParseError):
                parse_wordpress_posts(payload)
        with self.assertRaises(DiscoveryParseError):
            parse_wordpress_posts('[' * 2000 + ']' * 2000)

    def test_unsafe_urls_deduplicated_and_row_limit_is_explicit(self):
        rows = posts(5)
        rows[1]['link'] = rows[0]['link']
        rows[2]['link'] = 'https://127.0.0.1/a'
        result = parse_wordpress_posts(json.dumps(rows), max_items=4)
        self.assertEqual(len(result['records']), 2)
        self.assertEqual(result['pagination']['excluded_urls'], 1)
        self.assertEqual(result['pagination']['observed_items'], 5)
        self.assertTrue(result['pagination']['truncated'])

    def test_date_fallback_does_not_invent_gmt_for_local_date(self):
        rows = posts()
        rows[0].update(date_gmt='not a date', date='2020-01-01T21:04:05')
        result = parse_wordpress_posts(json.dumps(rows))
        self.assertEqual(result['records'][0]['published_at'], '2020-01-01T21:04:05')
        rows[0].update(date='bad date')
        self.assertIsNone(parse_wordpress_posts(json.dumps(rows))['records'][0]['published_at'])

    def test_strips_scripts_and_caps_untrusted_text_fields(self):
        rows = posts()
        rows[0]['title'] = {'rendered': '<script>SECRET</script>' + 'a' * 2000}
        rows[0]['excerpt'] = {'rendered': '<style>HIDDEN</style>' + 'b' * 30_000}
        record = parse_wordpress_posts(json.dumps(rows))['records'][0]
        self.assertEqual(len(record['title']), 1000)
        self.assertEqual(len(record['text']), 20_000)
        self.assertNotIn('SECRET', record['title'])
        self.assertNotIn('HIDDEN', record['text'])

    def test_gzip_bounded_no_network_and_bad_options(self):
        raw = json.dumps(posts()).encode()
        with patch('socket.getaddrinfo', side_effect=AssertionError('No DNS in parsers')):
            self.assertEqual(parse_wordpress_posts(raw), parse_wordpress_posts(gzip.compress(raw)))
        with self.assertRaisesRegex(DiscoveryParseError, 'max_bytes'):
            parse_wordpress_posts(raw, max_bytes=2)
        for options in ({'page': 0}, {'per_page': 101}, {'max_items': True}):
            with self.subTest(options=options), self.assertRaises(DiscoveryParseError):
                parse_wordpress_posts(raw, **options)


if __name__ == '__main__':
    unittest.main()
