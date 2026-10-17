"""Candidate-only regressions; imported against the runtime copy by the runner."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import socket
import tempfile
import time
import unittest
from unittest.mock import Mock

from peps.collector import SafeFetcher


PUBLIC = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('93.184.216.34', 443))]
PRIVATE = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('127.0.0.1', 443))]
CHALLENGE = b'<html><form action="/anomaly.js"><div id="anomaly-modal">Verify</div></form></html>'


class TransportCandidateTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.fetcher = SafeFetcher({'trust_env_proxy': False, 'cache_ttl_seconds': 0}, Path(directory.name))
        self.addCleanup(self.fetcher.close)
        self.now = 1_800_000_000.
        self.fetcher._clock = lambda: self.now
        self.fetcher._dns = Mock(return_value=PUBLIC)
        self.fetcher._transport = Mock(return_value=(200, {'content-type': 'text/plain'}, b'fixture body'))
        self.url = 'https://news.example/query'
        self.source = dict(enabled=True, terms_approved=True, robots_required=False,
                           allowed_hosts=['news.example'], min_interval_seconds=5, kind='duckduckgo_html')

    def test_interval_and_circuit_deferrals_never_resolve_dns_or_request_http(self):
        for blocked in (False, True):
            with self.subTest(blocked=blocked):
                with self.fetcher._db() as db:
                    db.execute('INSERT OR REPLACE INTO hosts(host,next_at,blocked_until,reason) VALUES(?,?,?,?)',
                               ('news.example', self.now + 300, self.now + 300 if blocked else 0, 'manual restriction'))
                result = self.fetcher.fetch(self.url, self.source)
                self.assertEqual(result['status'], 'blocked' if blocked else 'deferred')
                self.assertEqual(result.get('error_code'), 'host_circuit' if blocked else 'host_interval')
                self.assertEqual(result['network_requests'], 0)
        self.fetcher._dns.assert_not_called()
        self.fetcher._transport.assert_not_called()

    def test_cached_robots_decision_still_respects_host_interval_without_dns(self):
        with self.fetcher._db() as db:
            db.execute('INSERT INTO hosts(host,next_at) VALUES(?,?)', ('news.example', self.now + 300))
            db.execute('INSERT INTO robots(origin,body,expires_at,checked_at) VALUES(?,?,?,?)',
                       ('https://news.example', 'User-agent: *\nAllow: /\n', self.now + 1000, self.now))
        result = self.fetcher.fetch(self.url, dict(self.source, robots_required=True))
        self.assertEqual(result.get('error_code'), 'host_interval')
        self.fetcher._dns.assert_not_called()
        self.fetcher._transport.assert_not_called()

    def test_scope_and_syntax_are_rejected_before_any_reservation_or_dns(self):
        self.fetcher._reserve = Mock(side_effect=AssertionError('Invalid scope must not reserve a host'))
        for url in ('https://other.example/query', 'https://u:password@news.example/query',
                    'http://news.example/query', 'https://news.example/query\r\nHeader: value'):
            with self.subTest(url=url):
                self.assertEqual(self.fetcher.fetch(url, self.source)['status'], 'error')
        self.fetcher._dns.assert_not_called()
        self.fetcher._transport.assert_not_called()
        self.fetcher._reserve.assert_not_called()

    def test_public_dns_is_mandatory_and_same_checked_addresses_reach_transport(self):
        result = self.fetcher.fetch(self.url, self.source)
        self.assertEqual(result['status'], 'ok')
        self.fetcher._dns.assert_called_once()
        self.assertIs(self.fetcher._transport.call_args.args[-1], PUBLIC)
        self.fetcher._dns.reset_mock()
        self.fetcher._transport.reset_mock()
        self.now += 10
        self.fetcher._dns.return_value = PRIVATE
        rejected = self.fetcher.fetch(self.url + '?changed=1', self.source)
        self.assertEqual(rejected['status'], 'error')
        self.assertIn('Non-public', rejected['error'])
        self.assertEqual(rejected['network_requests'], 0)
        self.fetcher._transport.assert_not_called()

    def test_redirect_private_destination_is_rejected_before_target_http(self):
        self.fetcher._dns.side_effect = [PUBLIC, PRIVATE]
        self.fetcher._transport.return_value = (302, {'location': 'https://other.example/target'}, b'')
        result = self.fetcher.fetch(self.url, dict(self.source, allowed_hosts=['news.example', 'other.example']))
        self.assertEqual(result['status'], 'error')
        self.assertIn('Non-public', result['error'])
        self.fetcher._transport.assert_called_once()
        self.assertEqual(result['network_requests'], 1)

    def test_concurrent_host_reservation_keeps_courtesy_and_skips_known_wait_dns(self):
        with ThreadPoolExecutor(max_workers=32) as pool:
            results = list(pool.map(lambda i: self.fetcher.fetch(self.url + '?q=' + str(i), self.source), range(64)))
        self.assertEqual(sum(row['status'] == 'ok' for row in results), 1)
        self.assertEqual(sum(row.get('error_code') == 'host_interval' for row in results), 63)
        # Workers that read readiness together may resolve DNS concurrently;
        # the final post-DNS reservation still admits exactly one transport.
        self.assertGreaterEqual(self.fetcher._dns.call_count, 1)
        self.fetcher._transport.assert_called_once()
        self.fetcher._dns.reset_mock()
        waiting = self.fetcher.fetch(self.url + '?known-wait=1', self.source)
        self.assertEqual(waiting['error_code'], 'host_interval')
        self.fetcher._dns.assert_not_called()

    def test_expired_deadline_never_resolves_dns_or_requests_http(self):
        result = self.fetcher.fetch(self.url, dict(self.source, _deadline_monotonic=time.monotonic() - 1))
        self.assertEqual(result.get('error_code'), 'session_limit')
        self.fetcher._dns.assert_not_called()
        self.fetcher._transport.assert_not_called()

    def test_explicit_ddg_202_challenge_blocks_and_does_not_retry_or_change_endpoint(self):
        self.fetcher._transport.return_value = (202, {'content-type': 'text/html; charset=utf-8'}, CHALLENGE)
        first = self.fetcher.fetch(self.url, self.source)
        self.assertEqual(first['status'], 'blocked')
        self.assertEqual(first['http_status'], 202)
        self.assertEqual(first.get('error_code'), 'access_challenge')
        self.fetcher._dns.reset_mock()
        second = self.fetcher.fetch(self.url, self.source)
        self.assertEqual(second.get('error_code'), 'host_circuit')
        self.fetcher._dns.assert_not_called()
        self.fetcher._transport.assert_called_once()

    def test_202_status_without_explicit_ddg_html_marker_is_not_classified_as_challenge(self):
        cases = ((self.source, 'text/html', b'<html><p>Still processing</p></html>'),
                 (dict(self.source, kind='news'), 'text/html', CHALLENGE),
                 (self.source, 'application/json', CHALLENGE))
        for index, (source, mime, body) in enumerate(cases):
            with self.subTest(index=index):
                self.now += 10
                self.fetcher._transport.return_value = (202, {'content-type': mime}, body)
                result = self.fetcher.fetch(self.url + '?case=' + str(index), source)
                self.assertEqual(result['status'], 'error')
                self.assertEqual(result['http_status'], 202)
                self.assertEqual(result['error'], 'Unexpected HTTP status')
                with self.fetcher._db() as db:
                    row = db.execute("SELECT blocked_until FROM hosts WHERE host='news.example'").fetchone()
                self.assertEqual(row['blocked_until'], 0)
