"""Diagnostic capture only; fictitious unit transport, never benchmark data."""
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from peps.collector import SafeFetcher
from peps.engine import Engine
from scripts.validate_live_pipeline import (
    Journal, RequestBudget, observed_fetcher, observed_response_headers,
)


class HeaderReceiptTests(unittest.TestCase):
    def test_headers_are_allowlisted_bounded_and_do_not_expose_auth_or_cookies(self):
        incoming = {'Server': 'unit server', 'Via': 'unit gateway', 'Retry-After': '120',
                    'X-Cache': 'x' * 900, 'Set-Cookie': 'private-cookie',
                    'Authorization': 'private-authorization', 'Proxy-Authorization': 'private-proxy',
                    'X-Unreviewed-Header': 'private-unreviewed', 'Date': 1234}
        result = observed_response_headers(incoming)
        self.assertEqual(result['response_headers'], {'server': 'unit server', 'via': 'unit gateway',
                                                     'retry-after': '120', 'x-cache': 'x' * 512})
        self.assertEqual(result['response_headers_truncated'], ['x-cache'])
        self.assertNotIn('private', repr(result))
        self.assertEqual(incoming['Set-Cookie'], 'private-cookie')

    def test_single_real_observer_call_preserves_status_bytes_and_counts(self):
        # The mocked response is a functional instrument test. It is never
        # written to any online run or claimed as real publisher performance.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bank, attempt = root / 'bank', root / 'attempt'
            attempt.mkdir()
            engine = Engine(bank, online=False)
            journal = Journal(attempt)
            budget = RequestBudget(10, 0, time.monotonic() + 10, journal)
            observer = observed_fetcher(engine, budget, journal, attempt, [])
            fetcher = observer({}, bank, offline=True)
            fetcher._local.live_requests = 0
            headers = {'server': 'functional fixture', 'retry-after': '90', 'set-cookie': 'not-logged'}
            try:
                with patch.object(SafeFetcher, '_transport', return_value=(503, headers, b'')) as transport:
                    result = fetcher._transport('https://fixture.invalid/robots.txt', {}, 5, 1000, ())
                self.assertEqual(transport.call_count, 1)
                self.assertEqual(result, (503, headers, b''))
                self.assertEqual(len(budget.events), 1)
                self.assertEqual(fetcher._local.live_requests, 1)
                event = budget.events[0]
                self.assertEqual(event['response_headers'], {'server': 'functional fixture', 'retry-after': '90'})
                self.assertEqual(event['response_bytes'], 0)
                self.assertEqual((attempt / event['response_capture_path']).read_bytes(), b'')
                self.assertNotIn('not-logged', (attempt / 'http-completed.jsonl').read_text())
            finally:
                fetcher.close()

    def test_error_prefix_is_not_reported_as_complete_article_body(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bank, attempt = root / 'bank', root / 'attempt'
            attempt.mkdir()
            engine = Engine(bank, online=False)
            journal = Journal(attempt)
            budget = RequestBudget(10, 0, time.monotonic() + 10, journal)
            fetcher = observed_fetcher(engine, budget, journal, attempt, [])({}, bank, offline=True)
            fetcher._local.live_requests = 0
            prefix = b'upstream connect error or disconnect/reset before headers'
            diagnostic = {'scope': 'http_error_prefix', 'complete': False, 'truncated': True,
                          'captured_bytes': len(prefix), 'classification': 'envoy_upstream_connect_error'}
            def transport(_self, *_args):
                _self._local.transport_metric = {'error_response_capture': diagnostic}
                return 503, {'server': 'envoy'}, prefix
            try:
                with patch.object(SafeFetcher, '_transport', transport):
                    fetcher._transport('https://fixture.invalid/story', {}, 5, 1000, ())
                event = budget.events[0]
                self.assertEqual(event['response_capture_scope'], 'http_error_prefix')
                self.assertFalse(event['response_body_complete'])
                self.assertTrue(event['response_body_truncated'])
                self.assertEqual(event['error_response_capture'], diagnostic)
                self.assertEqual((attempt / event['response_capture_path']).read_bytes(), prefix)
                self.assertEqual(len(budget.events), 1)
            finally:
                fetcher.close()


if __name__ == '__main__':
    unittest.main()
