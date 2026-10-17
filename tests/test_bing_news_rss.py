"""RSS adapter boundaries; fixtures verify semantics, not live throughput."""
from html import escape
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.parse import quote, urlencode, urlsplit, parse_qs

from peps.news_search import build_search_plan
from peps.public_search import _bing_news_url, parse_public_results, public_search_url
from peps.sources import parse_results


SOURCE = {'id': 'bing_news_rss', 'kind': 'bing_news_rss', 'enabled': True,
          'terms_approved': True, 'discovery_only': True,
          'url': 'https://www.bing.com/news/search'}


def wrapper(target):
    return 'http://www.bing.com/news/apiclick.aspx?' + urlencode({'ref': 'FexRss', 'url': target})


def feed(links):
    return '<rss version="2.0"><channel>' + ''.join(
        '<item><title>Persona Pública Ejemplo</title><link>' + escape(link) + '</link>'
        '<description>Extracto candidato.</description><pubDate>Mon, 01 Jan 2024 12:00:00 GMT</pubDate></item>'
        for link in links) + '</channel></rss>'


def response(body):
    return {'status': 'ok', 'url': public_search_url('Persona Pública Ejemplo', SOURCE), 'body': body}


class BingNewsRssTests(unittest.TestCase):
    def test_exact_endpoint_preserves_name_and_uses_only_observed_rss_parameters(self):
        parsed = urlsplit(public_search_url('Peña & Ejemplo', SOURCE))
        self.assertEqual(parsed.path, '/news/search')
        self.assertEqual(parse_qs(parsed.query), {'q': ['Peña & Ejemplo'], 'format': ['rss'], 'mkt': ['es-MX']})
        for endpoint in ('http://www.bing.com/news/search', 'https://bing.com/news/search',
                         'https://www.bing.com/news/search/', 'https://www.bing.com/news/search?format=html',
                         'https://www.bing.com/news/search#fragment', 'https://www.bing.com/other',
                         'https://www.bing.com:443/news/search', 'https://example.org/news/search'):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                public_search_url('Persona Pública Ejemplo', dict(SOURCE, url=endpoint))

    def test_explicit_https_destination_is_unwrapped_without_network_or_byte_rewriting(self):
        target = 'https://editor.example/notas/a%2Fb?two=2&one=1'
        link = wrapper(target)
        with patch('socket.getaddrinfo', side_effect=AssertionError('No DNS during parsing')), \
                patch('peps.collector.SafeFetcher', side_effect=AssertionError('No HTTP during parsing')):
            result = parse_results(response(feed([link])), SOURCE)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['url'], target)
        self.assertEqual(result[0]['source_kind'], 'discovery')
        self.assertIsNone(result[0]['published_at'])
        self.assertEqual(result[0]['metadata']['provider_link'], link)
        self.assertTrue(result[0]['metadata']['provider_date_unverified'])
        self.assertEqual(result[0]['metadata']['score_effect'], 'none')

    def test_at_most_two_encoding_layers_and_no_recursive_wrappers(self):
        target = 'https://editor.example/a%2Fb'
        self.assertEqual(_bing_news_url(wrapper(target)), target)
        self.assertEqual(_bing_news_url(wrapper(quote(target, safe=''))), target)
        self.assertIsNone(_bing_news_url(wrapper(quote(quote(target, safe=''), safe=''))))
        self.assertIsNone(_bing_news_url(wrapper(wrapper(target))))
        self.assertIsNone(_bing_news_url(wrapper(wrapper(target).replace('http:', 'https:', 1))))

    def test_wrapper_and_target_security_boundaries(self):
        target = 'https://editor.example/story'
        invalid = [wrapper(value) for value in (
            'http://editor.example/story', 'https://user:secret@editor.example/story',
            'https://editor.example/story#fragment', 'https://editor.example/story#',
            'https://127.0.0.1/a', 'https://10.0.0.1/a',
            'https://169.254.169.254/a', 'https://[::1]/a', 'https://[::ffff:127.0.0.1]/a',
            'https://localhost/a', 'https://metadata.google.internal/a', 'https://service.internal/a',
            'https://editor.example:444/a', 'https://editor.example\\@127.0.0.1/a',
            'https://bing.com/news/apiclick.aspx', 'https://sub.bing.com/a',
            'https://editor.example/\nmalformed', '//editor.example/a', '')]
        link = wrapper(target)
        invalid += [link.replace('www.bing.com', 'bing.com', 1),
                    link.replace('www.bing.com', 'www.bing.com.evil.example', 1),
                    link.replace('www.bing.com', 'user@www.bing.com', 1),
                    link.replace('www.bing.com', 'www.bing.com:443', 1),
                    link.replace('/news/apiclick.aspx', '/news/apiclick.aspx/', 1),
                    link.replace('/news/apiclick.aspx', '/news/other', 1),
                    link + '#fragment', link + '#', link + '&url=' + quote(target),
                    link.replace('http:', 'ftp:', 1), '\n' + link,
                    'http://www.bing.com/news/apiclick.aspx?' + '&'.join('x=1' for _ in range(33)),
                    wrapper('https://editor.example/' + 'a' * 8192)]
        for value in invalid:
            with self.subTest(value=value[:160]):
                self.assertIsNone(_bing_news_url(value))
        fetch = response(feed(invalid))
        self.assertEqual(parse_public_results(fetch, SOURCE), [])
        self.assertEqual(fetch['parse_metadata']['excluded_unsafe_or_non_https_urls'], len(invalid))

    def test_valid_empty_feed_is_distinct_from_html_malformed_xml_or_error(self):
        self.assertEqual(parse_results(response(feed([])), SOURCE), [])
        bodies = ['<html><head><title>Bing</title></head><body></body></html>', '',
                  '<rss version="2.0">', '<rss version="2.0"/>', '<rss><channel/></rss>',
                  '<rss version="2.0"><channel/><channel/></rss>',
                  '<rss version="2.0"><channel><item><title>Missing URL</title></item></channel></rss>',
                  '<!DOCTYPE rss [<!ENTITY value "bad">]><rss version="2.0"><channel/></rss>']
        for body in bodies:
            with self.subTest(body=body), self.assertRaises(ValueError):
                parse_results(response(body), SOURCE)

    def test_bounded_deduplication_preserves_original_destination_and_provenance(self):
        links = [wrapper('https://editor.example/' + str(index)) for index in range(3)]
        fetch = response(feed([*links, links[0]]))
        rows = parse_results(fetch, dict(SOURCE, max_records=2))
        self.assertEqual(len(rows), 2)
        self.assertEqual(fetch['parse_metadata']['observed_entries'], 4)
        self.assertTrue(fetch['parse_metadata']['truncated'])

    def test_plan_has_one_format_per_provider_no_date_claim_and_respects_permissions(self):
        entity = {'id': 'person', 'name': 'Persona Pública Ejemplo'}
        config = {'strategy': 'public', 'start': '2000-01-01', 'include_aliases': False, 'use_context': False}
        plan = build_search_plan([entity], [SOURCE, dict(SOURCE, id='second-bing')], config)
        self.assertEqual(plan['query_count'], 1)
        self.assertEqual(plan['queries'][0]['query'], '"Persona Pública Ejemplo"')
        self.assertFalse(plan['sources'][0]['date_filter_supported'])
        self.assertFalse(plan['sources'][0]['historical'])
        self.assertTrue(any('personal y no comercial' in value for value in plan['warnings']))
        for disabled in (dict(SOURCE, enabled=False), dict(SOURCE, terms_approved=False)):
            self.assertEqual(build_search_plan([entity], [disabled], config)['query_count'], 0)

    def test_catalogue_is_disabled_with_all_permissions_false(self):
        root = Path(__file__).resolve().parents[1]
        sources = []
        for name in ('peps/defaults/news_sources.json', 'config/news_sources.example.json'):
            source = next(value for value in json.loads((root / name).read_text(encoding='utf-8'))['sources']
                          if value['id'] == 'bing_news_rss')
            sources.append(source)
            for key in ('enabled', 'terms_approved', 'download_articles', 'article_terms_approved', 'body_terms_approved'):
                self.assertIs(source[key], False)
            self.assertTrue(source['robots_required'])
            self.assertTrue(source['discovery_only'])
            self.assertEqual(source['allowed_hosts'], ['www.bing.com'])
        self.assertEqual(*sources)
