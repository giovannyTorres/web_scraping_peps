"""Adversarial HTTP/parser boundaries; no DNS or socket is used."""
import json
import socket
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

from peps.collector import SafeFetcher
from peps.public_search import _DuckResults, parse_public_results
from peps.social_discovery import parse_lemmy_results


PUBLIC_DNS = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('93.184.216.34', 443))]


class TransportStressTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.fetcher = SafeFetcher({'trust_env_proxy': False}, Path(self.directory.name))
        self.addCleanup(self.fetcher.close)
        self.now = 1_800_000_000.0
        self.fetcher._clock = lambda: self.now
        self.fetcher._dns = Mock(return_value=PUBLIC_DNS)
        self.source = dict(enabled=True, terms_approved=True, robots_required=False,
                           allowed_hosts=['news.example'], min_interval_seconds=0)
        self.url = 'https://news.example/article'

    def test_invalid_retry_after_numbers_use_normal_backoff_and_do_not_crash(self):
        for index, value in enumerate(('Infinity', 'NaN', '1e999', '-5', '120.5', '+120')):
            host = 'news' + str(index) + '.example'
            with self.subTest(value=value):
                self.fetcher._transport = Mock(return_value=(429, {'retry-after': value}, b''))
                result = self.fetcher.fetch('https://' + host + '/article', dict(self.source, allowed_hosts=[host]))
                self.assertEqual(result['status'], 'deferred')
                self.assertEqual(result['retry_at'], datetime.fromtimestamp(self.now + 30, timezone.utc).isoformat())
                self.assertEqual(result['network_requests'], 1)

    def test_enormous_valid_retry_after_persists_a_representable_restriction(self):
        self.fetcher._transport = Mock(return_value=(503, {'retry-after': '9' * 5000}, b''))
        result = self.fetcher.fetch(self.url, self.source)
        self.assertEqual(result['status'], 'deferred')
        self.assertEqual(result['retry_at'], '9999-12-31T00:00:00+00:00')
        self.assertEqual(result['network_requests'], 1)
        repeated = self.fetcher.fetch(self.url, self.source)
        self.assertEqual(repeated['status'], 'deferred')
        self.assertEqual(repeated['retry_at'], result['retry_at'])
        self.assertEqual(repeated['network_requests'], 0)
        self.fetcher._transport.assert_called_once()

    def test_unusable_declared_text_codecs_fall_back_to_utf8(self):
        for index, charset in enumerate(('idna', 'punycode', 'base64_codec', 'unknown-codec')):
            with self.subTest(charset=charset):
                self.fetcher._transport = Mock(return_value=(200, {'content-type': 'text/plain; charset=' + charset}, b'\xffInvalid'))
                result = self.fetcher.fetch(self.url + '?case=' + str(index), self.source)
                self.assertEqual(result['status'], 'ok')
                self.assertEqual(result['body'], '\ufffdInvalid')
                self.assertEqual(result['network_requests'], 1)


class ParserStressTests(unittest.TestCase):
    def test_unmatched_html_closures_do_not_scan_the_open_stack(self):
        class NoScanStack(list):
            def __getitem__(self, _key):
                raise AssertionError('An unmatched closing tag must not scan any open element')

        parser = _DuckResults('https://html.duckduckgo.com/html/')
        parser.feed('<div>' * 10_000)
        parser.stack = NoScanStack(parser.stack)
        parser.feed('</absent>' * 10_000)
        self.assertEqual(len(parser.stack), 10_000)

    def test_deep_malformed_html_keeps_real_result_and_recovers_hidden_stack(self):
        body = ('<div>' * 10_000 + '</absent>' * 10_000 +
                '<script><a class="result__a" href="https://hidden.example/">Hidden</a></script>' +
                '<a class="result__a" href="https://editor.example/story">Visible <b>title</b></a>' +
                '<span class="result__snippet">Useful text</span>' + '</div>' * 10_000)
        rows = parse_public_results({'status': 'ok', 'body': body}, {'kind': 'duckduckgo_html'})
        self.assertEqual([(row['url'], row['title'], row['text']) for row in rows],
                         [('https://editor.example/story', 'Visible title', 'Useful text')])

    def test_public_json_surrogates_are_controlled_parse_errors(self):
        for field in ('title', 'content'):
            record = {'url': 'https://editor.example/story', 'title': 'Title', 'content': 'Excerpt'}
            record[field] = '\ud800'
            payload = {'results': [record]}
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'Unicode'):
                parse_public_results({'status': 'ok', 'body': json.dumps(payload)}, {'kind': 'searxng'})

    @staticmethod
    def lemmy_response(**changes):
        source = {'url': 'https://lemmy.example/api/v3/search', 'allowed_hosts': ['lemmy.example']}
        post = dict(id=1, name='Title', body='Excerpt', url='https://editor.example/story',
                    ap_id='https://lemmy.example/post/1', published='2026-10-01T00:00:00Z')
        post.update(changes)
        response = {'status': 'ok', 'url': source['url'], 'body': json.dumps({'posts': [{'post': post}]})}
        return response, source

    def test_extreme_social_dates_do_not_abort_discovery_or_invent_dates(self):
        for value in ('9999-12-31T23:59:59-23:59', '0001-01-01T00:00:00+23:59'):
            with self.subTest(value=value):
                rows = parse_lemmy_results(*self.lemmy_response(published=value))
                self.assertEqual(len(rows), 1)
                self.assertIsNone(rows[0]['metadata']['post_published_at'])
                self.assertIsNone(rows[0]['published_at'])

    def test_social_json_surrogates_are_controlled_parse_errors(self):
        for field in ('name', 'body'):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'Unicode'):
                parse_lemmy_results(*self.lemmy_response(**{field: '\ud800'}))


if __name__ == '__main__':
    unittest.main()
