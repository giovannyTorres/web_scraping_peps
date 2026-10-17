"""Functional API boundaries, not external acquisition/performance evidence."""
import copy
import socket
import tempfile
import unittest
from pathlib import Path

from peps.api_permissions import documented_search_api
from peps.collector import SafeFetcher
from peps.public_search import public_search_url

SOURCE = {'id': 'fictional-public-api', 'kind': 'searxng', 'enabled': True,
          'terms_approved': True, 'robots_required': True, 'min_interval_seconds': 0,
          'search_url': 'https://search.example/search', 'allowed_hosts': ['search.example'],
          'documented_api_permission': {'scope': 'public_json_search_api', 'method': 'GET',
             'reviewed': True, 'endpoint': 'https://search.example/search',
             'operator_documentation_url': 'https://search.example/info/en/about',
             'api_documentation_url': 'https://docs.searxng.org/dev/search_api.html',
             'review_receipt_sha256': 'a' * 64}}


class DocumentedApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.fetcher = SafeFetcher({'cache_ttl_seconds': 60, 'trust_env_proxy': False}, Path(self.directory.name))
        self.addCleanup(self.fetcher.close)
        self.now = [1800000000.]
        self.fetcher._clock = lambda: self.now[0]
        self.fetcher._dns = lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('93.184.216.34', 443))]
        self.calls = []
        self.url = public_search_url('Persona enteramente ficticia', SOURCE)

    def transport(self, response=None):
        def call(url, headers, *args):
            self.calls.append(url)
            if url.endswith('/robots.txt'):
                return 200, {'content-type': 'text/plain'}, b'User-agent: *\nDisallow: /*?*q=*\n'
            self.assertEqual(url, self.url)
            return response or (200, {'content-type': 'application/json'}, b'{"results":[]}')
        self.fetcher._transport = call

    def fetch_ready(self, source=SOURCE):
        first = self.fetcher.fetch(self.url, source)
        if first['status'] == 'deferred' and first.get('error_code') == 'host_interval':
            self.now[0] += 2
            return self.fetcher.fetch(self.url, source)
        return first

    def test_only_reviewed_json_api_operation_receives_permission(self):
        self.assertTrue(documented_search_api(self.url, SOURCE))
        for url in (self.url.replace('json', 'html'), self.url + '&format=json',
                    self.url.replace('/search?', '/article?'), self.url.replace('search.example', 'other.example'),
                    self.url + '&url=https%3A%2F%2Flocalhost', self.url + '#fragment'):
            self.assertFalse(documented_search_api(url, SOURCE), url)
        for changes in ({'kind': 'rss'}, {'download_articles': True}, {'article_terms_approved': True}):
            self.assertFalse(documented_search_api(self.url, dict(SOURCE, **changes)))

    def test_unreviewed_permission_fails_closed_without_transport(self):
        source = copy.deepcopy(SOURCE)
        source['documented_api_permission']['reviewed'] = False
        self.transport()
        result = self.fetch_ready(source)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['error_code'], 'documented_api_scope')
        self.assertEqual(self.calls, [])

    def test_documented_api_and_cache_work_without_allowing_result_html(self):
        self.transport()
        result = self.fetch_ready()
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(self.calls, ['https://search.example/robots.txt', self.url])
        again = self.fetcher.fetch(self.url, SOURCE)
        self.assertTrue(again['from_cache'])
        self.assertEqual(again['network_requests'], 0)
        self.assertEqual(len(self.calls), 2)
        html = self.fetcher.fetch(self.url.replace('json', 'html'), SOURCE)
        self.assertEqual(html['status'], 'blocked')
        self.assertEqual(len(self.calls), 2)

    def test_generic_source_still_obeys_robots(self):
        source = dict(SOURCE)
        source.pop('documented_api_permission')
        self.transport()
        self.assertEqual(self.fetch_ready(source)['status'], 'robots_denied')
        self.assertEqual(self.calls, ['https://search.example/robots.txt'])

    def test_api_redirect_to_another_path_is_blocked_before_request(self):
        self.transport((302, {'location': 'https://search.example/other'}, b''))
        result = self.fetch_ready()
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['error_code'], 'documented_api_scope')
        self.assertEqual(self.calls, ['https://search.example/robots.txt', self.url])

    def test_403_opens_circuit_without_alternate_api_or_html(self):
        self.transport((403, {}, b''))
        self.assertEqual(self.fetch_ready()['status'], 'blocked')
        self.now[0] += 10
        result = self.fetcher.fetch(self.url, SOURCE)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(len(self.calls), 2)

    def test_explicit_phrase_language_and_page_are_query_capabilities_only(self):
        source = dict(SOURCE, public_search_quote=True, public_search_language='es-MX', public_search_page=2)
        url = public_search_url('Persona enteramente ficticia', source)
        self.assertIn('q=%22Persona+enteramente+ficticia%22', url)
        self.assertIn('pageno=2', url)
        self.assertTrue(documented_search_api(url, source))
        for page in (True, 0, 11, '2'):
            with self.assertRaises(ValueError):
                public_search_url('Ficción', dict(source, public_search_page=page))


if __name__ == '__main__':
    unittest.main()
