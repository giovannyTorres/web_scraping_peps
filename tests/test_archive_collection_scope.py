"""Arquivo discovery cannot request archived bodies or follow redirects."""
from pathlib import Path
import socket
import tempfile
import unittest

from peps.archive_text_search import search_url
from peps.collector import SafeFetcher


SOURCE = {'id': 'fictional-archive', 'kind': 'arquivo_fulltext', 'enabled': True,
          'terms_approved': True, 'robots_required': True, 'min_interval_seconds': 5,
          'url': 'https://arquivo.pt/textsearch', 'allowed_hosts': ['arquivo.pt'], 'max_records': 5}


class ArchiveCollectionScopeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.fetcher = SafeFetcher({'trust_env_proxy': False}, Path(self.directory.name))
        self.addCleanup(self.fetcher.close)
        self.now = [1800000000.]
        self.fetcher._clock = lambda: self.now[0]
        self.fetcher._dns = lambda *_a, **_k: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))]
        self.calls = []
        self.url = search_url('Completely fictional query', SOURCE)
        self.payload = (200, {'content-type': 'application/json'}, b'{"response_items":[]}')
        self.robots = b'User-agent: *\nAllow: /textsearch\n'
        def transport(url, *_args):
            self.calls.append(url)
            return (200, {'content-type': 'text/plain'}, self.robots) if url.endswith('/robots.txt') else self.payload
        self.fetcher._transport = transport

    def ready(self):
        result = self.fetcher.fetch(self.url, SOURCE)
        if result.get('error_code') == 'host_interval':
            self.assertEqual(self.calls[-1], 'https://arquivo.pt/robots.txt')
            self.now[0] += 5
            result = self.fetcher.fetch(self.url, SOURCE)
        return result

    def test_exact_builder_operation_and_robots_remain_separate_from_archive_bodies(self):
        result = self.ready()
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(self.calls, ['https://arquivo.pt/robots.txt', self.url])
        self.assertEqual(self.fetcher.fetch(self.url, SOURCE)['network_requests'], 0)

    def test_wrong_paths_replay_text_extraction_and_parameters_never_resolve(self):
        self.fetcher._dns = lambda *_a, **_k: self.fail('Out-of-scope URL attempted DNS')
        urls = ['https://arquivo.pt/wayback/20200101000000/https://example.org/',
                'https://arquivo.pt/noFrame/replay/20200101000000/https://example.org/',
                'https://arquivo.pt/textextracted?m=fixture', 'https://arquivo.pt/robots.txt?q=x',
                self.url + '&from=20000101', self.url.replace('maxItems=5', 'maxItems=100'),
                self.url.replace('fields=title', 'fields=linkToArchive,title'),
                self.url + '&q=duplicate', self.url + '#fragment',
                self.url.replace('https://arquivo.pt/', 'https://arquivo.pt:443/')]
        for url in urls:
            with self.subTest(url=url):
                result = self.fetcher.fetch(url, SOURCE)
                self.assertEqual(result['status'], 'blocked')
                self.assertEqual(result['error_code'], 'archive_discovery_scope')
                self.assertEqual(result['network_requests'], 0)
        self.assertEqual(self.calls, [])

    def test_every_search_redirect_is_blocked_before_second_http(self):
        for status in (301, 302, 303, 307, 308):
            for target in (self.url, search_url('A different fictional query', SOURCE),
                           'https://arquivo.pt/noFrame/replay/fixture', 'https://other.example/story'):
                with self.subTest(status=status, target=target):
                    self.now[0] += 5
                    self.payload = status, {'location': target}, b''
                    before = len(self.calls)
                    result = self.ready()
                    self.assertEqual(result['error_code'], 'archive_discovery_scope')
                    expected = 2 if before == 0 else 1
                    self.assertEqual(len(self.calls) - before, expected)

    def test_cached_redirect_is_blocked_before_dns_or_http(self):
        with self.fetcher._db() as con:
            con.execute('INSERT INTO redirects VALUES(?,?,?)',
                        (self.url, search_url('Different fictional query', SOURCE), self.now[0] + 3600))
            con.execute('INSERT INTO robots VALUES(?,?,?,?)',
                        ('https://arquivo.pt', self.robots.decode(), self.now[0] + 3600, self.now[0]))
        self.fetcher._dns = lambda *_a, **_k: self.fail('Archive redirect attempted DNS')
        self.assertEqual(self.ready()['error_code'], 'archive_discovery_scope')
        self.assertEqual(self.calls, [])

    def test_legacy_search_cache_cannot_inherit_a_robots_response_role(self):
        self.assertEqual(self.ready()['status'], 'ok')
        with self.fetcher._db() as con:
            con.execute('UPDATE http_cache SET final_url=? WHERE url=?',
                        ('https://arquivo.pt/robots.txt', self.url))
        count = len(self.calls)
        self.fetcher._dns = lambda *_a, **_k: self.fail('Invalid cached role attempted DNS')
        for mode in ('local', 'reuse'):
            with self.subTest(mode=mode):
                result = self.fetcher.fetch(self.url, dict(SOURCE, _cache_mode=mode))
                self.assertEqual(result['error_code'], 'archive_discovery_scope')
                self.assertEqual(result['network_requests'], 0)
        self.assertEqual(len(self.calls), count)

    def test_robots_denial_and_http_access_floor_are_preserved(self):
        self.robots = b'User-agent: *\nDisallow: /textsearch\n'
        self.assertEqual(self.ready()['status'], 'robots_denied')
        self.assertEqual(self.calls, ['https://arquivo.pt/robots.txt'])
        with self.fetcher._db() as con:
            con.execute('DELETE FROM robots')
        self.robots = b'User-agent: *\nAllow: /textsearch\n'
        self.now[0] += 5
        self.payload = 403, {'Retry-After': '120'}, b''
        result = self.ready()
        self.assertEqual(result['status'], 'blocked')
        self.fetcher._dns = lambda *_a, **_k: self.fail('Known API host ban attempted DNS')
        self.assertEqual(self.fetcher.fetch(self.url, SOURCE)['network_requests'], 0)


if __name__ == '__main__':
    unittest.main()
