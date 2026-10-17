"""Bounded 5xx diagnostics and permanent MIME errors; no external HTTP."""
import gzip
import hashlib
import http.client
import io
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import Mock, patch

from peps.collector import SafeFetcher, classify_http_error_prefix
from peps.engine import Engine
from peps.news import NewsAcquirer
from peps.store import digest, now

PUBLIC = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('93.184.216.34', 443))]
PREFIX = b'upstream connect error or disconnect/reset before headers. reset reason: connection failure'


class ErrorDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.fetcher = SafeFetcher({'trust_env_proxy': False}, self.root)
        self.addCleanup(self.fetcher.close)
        self.fetcher._clock = lambda: 1_800_000_000.
        self.fetcher._dns = lambda *_a, **_k: PUBLIC
        self.url = 'https://news.example/original'
        self.source = {'id': 'fixture', 'enabled': True, 'terms_approved': True,
                       'allowed_hosts': ['news.example'], 'robots_required': False,
                       'min_interval_seconds': 0, 'max_bytes': 100_000}

    def response(self, status=503, body=PREFIX, headers=None, read_error=None):
        response, connection = Mock(), Mock()
        response.status, response.will_close = status, False
        h = {'server': 'envoy', 'content-type': 'text/plain', 'content-length': str(len(body))}
        h.update(headers or {})
        response.getheaders.return_value = list(h.items())
        stream = io.BytesIO(body)
        response.read1.side_effect = ([body, read_error] if read_error else stream.read)
        response.isclosed.return_value = False
        connection.getresponse.return_value = response
        connection._peer_address = '93.184.216.34'
        return response, connection, stream

    def fetch_response(self, **kwargs):
        response, connection, stream = self.response(**kwargs)
        with patch('peps.collector._PinnedHTTPSConnection', return_value=connection):
            result = self.fetcher.fetch(self.url, self.source)
        return result, response, connection, stream

    def test_real_http_parser_reads_bounded_503_response_without_extra_request(self):
        class Wire:
            def __init__(self):
                self.stream = io.BytesIO(b'HTTP/1.1 503 Service Unavailable\r\nServer: envoy\r\n'
                    b'Content-Type: text/plain\r\nContent-Length: ' + str(len(PREFIX)).encode() + b'\r\n\r\n' + PREFIX)
                self.sent = 0
            def settimeout(self, _): pass
            def recv_into(self, buffer):
                value = self.stream.read(len(buffer));buffer[:len(value)] = value;return len(value)
            def sendall(self, _): self.sent += 1
            def close(self): pass
        connection = http.client.HTTPSConnection('news.example')
        wire = connection.sock = Wire()
        with patch('peps.collector._PinnedHTTPSConnection', return_value=connection):
            result = self.fetcher.fetch(self.url, self.source)
        diagnostic = result['error_diagnostic']
        self.assertEqual(wire.sent, 1)
        self.assertEqual(result['network_requests'], 1)
        self.assertEqual(result['body'], '')
        self.assertEqual(diagnostic['classification'], 'envoy_upstream_connect_error')
        self.assertTrue(diagnostic['complete'])
        self.assertFalse(diagnostic['truncated'])
        self.assertEqual(diagnostic['sha256'], hashlib.sha256(PREFIX).hexdigest())
        self.assertLessEqual(diagnostic['read_budget_seconds'], 1.)
        self.assertEqual((self.root / diagnostic['prefix_path']).read_bytes(), PREFIX)
        self.assertNotIn(PREFIX.decode(), json.dumps(result))
        with self.fetcher._db() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM http_cache').fetchone()[0], 0)
            receipt = dict(con.execute('SELECT * FROM http_error_diagnostics').fetchone())
        self.assertEqual(receipt['prefix_sha256'], diagnostic['sha256'])
        self.assertEqual(receipt['prefix_bytes'], len(PREFIX))

    def test_large_5xx_is_prefix_only_not_complete_or_cached(self):
        result, response, connection, stream = self.fetch_response(body=PREFIX + b'x' * 90_000)
        diagnostic = result['error_diagnostic']
        self.assertEqual(stream.tell(), 4096)
        self.assertEqual(diagnostic['captured_bytes'], 4096)
        self.assertFalse(diagnostic['complete'])
        self.assertTrue(diagnostic['truncated'])
        self.assertEqual(result['wire_bytes'], 4096)
        self.assertEqual(result['body'], '')
        self.assertEqual(len((self.root / diagnostic['prefix_path']).read_bytes()), 4096)
        connection.close.assert_called_once()

    def test_smaller_wire_cap_and_compressed_error_are_respected_without_inflation(self):
        self.source['max_wire_bytes'] = 32
        body = gzip.compress(PREFIX * 1000)
        result, _, _, stream = self.fetch_response(body=body, headers={'content-encoding': 'gzip'})
        self.assertEqual(stream.tell(), 32)
        diagnostic = result['error_diagnostic']
        self.assertEqual(diagnostic['captured_bytes'], 32)
        self.assertEqual(diagnostic['content_encoding'], 'gzip')
        self.assertEqual(diagnostic['classification'], 'unknown_http_5xx')
        self.assertEqual(result['decoded_bytes'], 0)

    def test_optional_diagnostic_read_failure_cannot_replace_503_backoff_with_client_failure(self):
        result, _, _, _ = self.fetch_response(body=PREFIX, headers={'content-length': '999'}, read_error=TimeoutError())
        self.assertEqual(result['status'], 'deferred')
        self.assertEqual(result['error_code'], 'server_backoff')
        self.assertEqual(result['error_diagnostic']['read_error'], 'TimeoutError')
        self.assertTrue(result['error_diagnostic']['truncated'])
        with self.fetcher._db() as con:
            host = con.execute('SELECT * FROM hosts').fetchone()
        self.assertEqual(host['server_failures'], 1)
        self.assertEqual(host['client_failures'], 0)

    def test_503_fallback_message_does_not_claim_retry_after_and_policy_is_preserved(self):
        result, _, _, _ = self.fetch_response()
        self.assertEqual(result['error'], 'Retry scheduled after HTTP 503')
        self.assertEqual(result['retry_at'], '2027-01-15T08:00:30+00:00')
        again = self.fetcher.fetch(self.url, self.source)
        self.assertEqual(again['network_requests'], 0)
        self.assertEqual(again['retry_at'], result['retry_at'])
        self.assertEqual(again['body'], '')

    def test_retry_after_and_access_restrictions_are_not_shortened_or_read_as_evidence(self):
        result, response, _, _ = self.fetch_response(status=429, headers={'retry-after': '120'})
        response.read1.assert_not_called()
        self.assertEqual(result['retry_at'], '2027-01-15T08:02:00+00:00')
        self.assertEqual(result['body'], '')
        with self.fetcher._db() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM http_error_diagnostics').fetchone()[0], 0)

    def test_diagnostic_storage_failure_does_not_change_persisted_backoff(self):
        with patch('peps.collector.tempfile.NamedTemporaryFile', side_effect=OSError('Fixture storage fault')):
            result, _, _, _ = self.fetch_response()
        self.assertEqual(result['status'], 'deferred')
        self.assertFalse(result['error_diagnostic']['persisted'])
        self.assertEqual(result['retry_at'], '2027-01-15T08:00:30+00:00')

    def test_signature_requires_both_exact_prefix_and_envoy_header(self):
        cases = [({'server': 'nginx'}, PREFIX), ({'server': 'envoy'}, b'<p>' + PREFIX),
                 ({'server': 'envoy'}, b'unrelated upstream failure'),
                 ({'server': 'not-envoy'}, PREFIX),
                 ({'server': 'envoy', 'content-encoding': 'gzip'}, PREFIX)]
        for headers, prefix in cases:
            with self.subTest(headers=headers, prefix=prefix[:20]):
                self.assertEqual(classify_http_error_prefix(headers, prefix), 'unknown_http_5xx')
        self.assertEqual(classify_http_error_prefix({'server': 'Envoy/1.0'}, PREFIX), 'envoy_upstream_connect_error')

    def test_unsupported_mime_blocks_only_resource_and_does_not_open_host_circuit(self):
        replies = [(200, {'content-type': 'application/pdf'}, b'%PDF-1.5 fixture'),
                   (200, {'content-type': 'text/plain'}, b'A supported public response.')]
        self.fetcher._transport = lambda *_a: replies.pop(0)
        first = self.fetcher.fetch(self.url, self.source)
        self.assertEqual((first['status'], first['error_code']), ('blocked', 'unsupported_content_type'))
        self.assertEqual(first['body'], '')
        self.assertEqual(self.fetcher.fetch(self.url + '/supported', self.source)['status'], 'ok')
        with self.fetcher._db() as con:
            host = con.execute('SELECT * FROM hosts').fetchone()
        self.assertEqual(host['blocked_until'], 0)
        self.assertEqual(host['server_failures'], 0)
        self.assertEqual(host['client_failures'], 0)


class PermanentFormatJobTests(unittest.TestCase):
    def test_legacy_error_status_is_permanent_and_explicit_revalidation_may_fetch_new_format(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = Engine(directory)
            news = NewsAcquirer(engine)
            source = {'id': 'fixture', 'kind': 'rss', 'enabled': True, 'terms_approved': True,
                      'allowed_hosts': ['news.example'], 'url': 'https://news.example/feed',
                      'download_articles': True, 'article_terms_approved': True}
            news.config_path.write_text(json.dumps({'sources': [source]}))
            url = 'https://news.example/original'
            news._observe({'url': url, 'title': 'Información pública', 'text': 'Resumen.'},
                          source, source['url'], {'retrieved_at': now()}, None, True)
            job = engine.store.claim(kind_prefix='news_article')
            news._failure(job, {'status': 'error', 'error': 'Unsupported content type',
                               'error_code': 'unsupported_content_type'})
            self.assertIsNone(engine.store.claim(kind_prefix='news_article'))
            with engine.store.connection() as con:
                self.assertEqual(con.execute('SELECT status FROM jobs').fetchone()[0], 'blocked')
            class Session:
                values = {'id': 'explicit-new-validation'}
                def note(self, **_): pass
                def body(self, *_a, **_k): pass
            news._session = Session()
            news._active_cache_mode = 'revalidate'
            repeated = news._observe({'url': url, 'title': 'Información pública', 'text': 'Resumen.'},
                                    source, source['url'], {'retrieved_at': now()}, None, True)
            self.assertEqual(repeated['status'], 'article_queued')
            self.assertIsNotNone(engine.store.claim(kind_prefix='news_article'))
            self.assertEqual(engine.store.stats()['documents'], 0)


if __name__ == '__main__':
    unittest.main()
