"""Network tests use fake DNS and fake HTTPS; no external request is made."""
import hashlib
import gzip
import http.client
import io
import json
import socket
import ssl
import tempfile
import threading
import unittest
import zlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from peps.collector import CollectionError, SafeFetcher, _PinnedHTTPSConnection, _DeadlineReader
from peps.sources import build_search_url, extract_text, parse_results


PUBLIC_DNS = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443))]


class FakeTransport:
    def __init__(self, responses):
        self.responses = {url: list(items) for url, items in responses.items()}
        self.calls = []

    def __call__(self, url, headers, timeout, max_bytes, addresses):
        self.calls.append((url, dict(headers), addresses))
        if url not in self.responses or not self.responses[url]:
            raise AssertionError("Unexpected network request: " + url)
        response = self.responses[url].pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class SafeFetcherTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.now = [1_800_000_000.0]
        self.fetcher = SafeFetcher({"cache_ttl_seconds": 0}, Path(self.directory.name))
        self.addCleanup(self.fetcher.close)
        self.fetcher._clock = lambda: self.now[0]
        self.fetcher._dns = lambda *args, **kwargs: PUBLIC_DNS
        self.source = dict(id="test", kind="news", allowed_hosts=["news.example"], enabled=True,
                           terms_approved=True, robots_required=False, min_interval_seconds=0,
                           max_bytes=100_000, timeout_seconds=5)
        self.url = "https://news.example/article"

    def transport(self, responses):
        transport = FakeTransport(responses)
        self.fetcher._transport = transport
        return transport

    def test_offline_never_looks_up_dns(self):
        self.fetcher.offline = True
        self.fetcher._dns = Mock(side_effect=AssertionError("DNS forbidden"))
        self.assertEqual(self.fetcher.fetch(self.url, self.source)["status"], "offline")

    def test_explicit_terms_and_enable_required(self):
        transport = self.transport({})
        for source in (dict(self.source, enabled=False), dict(self.source, terms_approved=False)):
            self.assertEqual(self.fetcher.fetch(self.url, source)["status"], "blocked")
        self.assertEqual(transport.calls, [])

    def test_private_and_mixed_dns_denied(self):
        transport = self.transport({})
        for address in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "fc00::1", "::ffff:93.184.216.34", "2002:7f00:1::"):
            records = PUBLIC_DNS + [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 443))]
            self.fetcher._dns = lambda *args, **kwargs: records
            result = self.fetcher.fetch(self.url, self.source)
            self.assertEqual(result["status"], "error", address)
        self.assertFalse(transport.calls)

    def test_url_boundaries(self):
        transport = self.transport({})
        for url in ("http://news.example/article", "file:///etc/passwd", "https://user:pass@news.example", "https://news.example:8443/", "https://other.example", "https://news.example/\r\nX: value"):
            self.assertEqual(self.fetcher.fetch(url, self.source)["status"], "error", url)
        self.assertFalse(transport.calls)

    def test_dns_revalidated_immediately_before_transport(self):
        transport = self.transport({})
        replies = iter((PUBLIC_DNS, [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 443))]))
        self.fetcher._dns = lambda *args, **kwargs: next(replies)
        self.assertEqual(self.fetcher.fetch(self.url, self.source)["status"], "error")
        self.assertFalse(transport.calls)

    def test_transport_pins_address_and_preserves_tls_hostname(self):
        raw_socket, wrapped_socket, context = Mock(), Mock(), Mock()
        context.wrap_socket.return_value = wrapped_socket
        with patch("peps.collector.socket.socket", return_value=raw_socket):
            connection = _PinnedHTTPSConnection("news.example", PUBLIC_DNS, 5, context)
            connection.connect()
        raw_socket.connect.assert_called_once_with(("93.184.216.34", 443))
        context.wrap_socket.assert_called_once_with(raw_socket, server_hostname="news.example")
        self.assertIs(connection.sock, wrapped_socket)

    def test_tls_validation_is_always_on(self):
        self.assertEqual(self.fetcher._tls_context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(self.fetcher._tls_context.check_hostname)

    def test_multiple_addresses_share_one_connect_budget(self):
        first, second, wrapped, context = Mock(), Mock(), Mock(), Mock()
        first.connect.side_effect = OSError("unreachable")
        context.wrap_socket.return_value = wrapped
        addresses = PUBLIC_DNS + [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.35", 443))]
        with patch("peps.collector.socket.socket", side_effect=[first, second]), patch("peps.collector.time.monotonic", side_effect=[0, 0, 4, 4.2, 4.5]):
            connection = _PinnedHTTPSConnection("news.example", addresses, 5, context)
            connection.connect()
        first.settimeout.assert_called_once_with(5)
        self.assertEqual(second.settimeout.call_args_list[0].args[0], 1)
        wrapped.settimeout.assert_called_once_with(.5)

    def test_raw_reader_cannot_extend_header_deadline(self):
        sock = Mock()
        reader = _DeadlineReader(sock, 5)
        with patch("peps.collector.time.monotonic", return_value=6):
            with self.assertRaises(TimeoutError):
                reader.readinto(bytearray(20))
        sock.recv_into.assert_not_called()

    def test_http_response_works_through_deadline_stream(self):
        class WireSocket:
            def __init__(self):
                self.stream = io.BytesIO(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nContent-Type: text/plain\r\n\r\nhello")
            def settimeout(self, _):
                pass
            def recv_into(self, buffer):
                value = self.stream.read(len(buffer))
                buffer[:len(value)] = value
                return len(value)
            def sendall(self, data):
                pass
            def close(self):
                pass
        connection = http.client.HTTPSConnection("news.example")
        connection.sock = WireSocket()
        with patch("peps.collector._PinnedHTTPSConnection", return_value=connection):
            status, headers, body = self.fetcher._transport(self.url, {}, 5, 100, PUBLIC_DNS)
        self.assertEqual((status, body), (200, b"hello"))

    def test_transport_read_cap_covers_absent_content_length(self):
        response, connection = Mock(), Mock()
        response.status = 200
        response.getheaders.return_value = [("Content-Type", "text/plain")]
        stream = io.BytesIO(b"x" * 1000)
        response.read1.side_effect = stream.read
        connection.getresponse.return_value = response
        with patch("peps.collector._PinnedHTTPSConnection", return_value=connection):
            with self.assertRaisesRegex(Exception, "max_bytes"):
                self.fetcher._transport(self.url, {}, 5, 100, PUBLIC_DNS)
        self.assertEqual(stream.tell(), 101)
        connection.close.assert_called_once()

    def test_declared_oversize_rejected_before_reading_body(self):
        response, connection = Mock(), Mock()
        response.status = 200
        response.getheaders.return_value = [("Content-Length", "1001")]
        connection.getresponse.return_value = response
        with patch("peps.collector._PinnedHTTPSConnection", return_value=connection):
            with self.assertRaisesRegex(Exception, "max_bytes"):
                self.fetcher._transport(self.url, {}, 5, 100, PUBLIC_DNS)
        response.read1.assert_not_called()
        connection.close.assert_called_once()

    def test_parallel_fetchers_share_one_host_reservation(self):
        def reserve(_):
            fetcher = SafeFetcher({}, Path(self.directory.name))
            # Cleanups run after the ThreadPoolExecutor has joined its workers,
            # before TemporaryDirectory cleanup closes the shared Windows files.
            self.addCleanup(fetcher.close)
            fetcher._clock = lambda: self.now[0]
            return fetcher._reserve("news.example", 10)[0]
        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(reserve, range(24)))
        self.assertEqual(outcomes.count(None), 1)
        self.assertEqual(outcomes.count("deferred"), 23)

    def test_redirect_unapproved_host_denied_before_second_request(self):
        transport = self.transport({self.url: [(302, {"location": "https://other.example/private"}, b"")]})
        result = self.fetcher.fetch(self.url, self.source)
        self.assertEqual(result["status"], "error")
        self.assertEqual(len(transport.calls), 1)

    def test_redirect_to_private_address_denied(self):
        transport = self.transport({self.url: [(302, {"location": "https://127.0.0.1/private"}, b"")]})
        self.source["allowed_hosts"].append("127.0.0.1")
        self.fetcher._dns = lambda host, *args, **kwargs: PUBLIC_DNS if host == "news.example" else [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (host, 443))]
        self.assertEqual(self.fetcher.fetch(self.url, self.source)["status"], "error")
        self.assertEqual(len(transport.calls), 1)

    def test_redirect_rate_limit_can_resume_without_refetching_redirect(self):
        target = "https://news.example/final"
        transport = self.transport({self.url: [(302, {"location": "/final"}, b"")], target: [(200, {"content-type": "text/html"}, b"<p>Final</p>")]})
        self.source["min_interval_seconds"] = 5
        result = self.fetcher.fetch(self.url, self.source)
        self.assertEqual(result["status"], "deferred")
        self.now[0] += 5
        result = self.fetcher.fetch(self.url, self.source)
        self.assertEqual(result["status"], "ok")
        self.assertEqual([call[0] for call in transport.calls], [self.url, target])

    def test_redirect_loop_is_bounded(self):
        transport = self.transport({self.url: [(302, {"location": "/article"}, b"")]})
        result = self.fetcher.fetch(self.url, self.source)
        self.assertEqual(result["status"], "error")
        self.assertIn("loop", result["error"].lower())
        self.assertEqual(len(transport.calls), 1)

    def test_429_retry_after_shared_across_instances(self):
        transport = self.transport({self.url: [(429, {"Retry-After": "120"}, b"")]})
        result = self.fetcher.fetch(self.url, self.source)
        self.assertEqual(result["status"], "deferred")
        expected = datetime.fromtimestamp(self.now[0] + 120, timezone.utc).isoformat()
        self.assertEqual(result["retry_at"], expected)
        other = SafeFetcher({}, Path(self.directory.name))
        self.addCleanup(other.close)
        other._clock, other._dns, other._transport = self.fetcher._clock, self.fetcher._dns, transport
        self.assertEqual(other.fetch(self.url, self.source)["status"], "deferred")
        self.assertEqual(len(transport.calls), 1)

    def test_503_retry_after_http_date(self):
        transport = self.transport({self.url: [(503, {"retry-after": "Fri, 15 Jan 2027 08:10:00 GMT"}, b"")]})
        result = self.fetcher.fetch(self.url, self.source)
        self.assertEqual(result["status"], "deferred")
        self.assertEqual(result["retry_at"], "2027-01-15T08:10:00+00:00")
        self.assertEqual(len(transport.calls), 1)

    def test_403_and_captcha_open_persisted_circuit(self):
        transport = self.transport({self.url: [(403, {}, b"")]})
        self.assertEqual(self.fetcher.fetch(self.url, self.source)["status"], "blocked")
        self.assertEqual(self.fetcher.fetch(self.url, self.source)["status"], "blocked")
        self.assertEqual(len(transport.calls), 1)

    def test_captcha_page_is_not_ingested(self):
        transport = self.transport({self.url: [(200, {"content-type": "text/html"}, b"<title>Just a moment...</title><div>Verify you are human</div>")]})
        result = self.fetcher.fetch(self.url, self.source)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["body"], "")
        self.assertEqual(self.fetcher.fetch(self.url, self.source)["status"], "blocked")
        self.assertEqual(len(transport.calls), 1)

    def test_network_outage_uses_retry_and_not_busy_loop(self):
        transport = self.transport({self.url: [TimeoutError("connection timeout")]})
        self.assertEqual(self.fetcher.fetch(self.url, self.source)["status"], "deferred")
        self.assertEqual(self.fetcher.fetch(self.url, self.source)["status"], "deferred")
        self.assertEqual(len(transport.calls), 1)

    def test_robots_disallow_is_cached_and_article_not_fetched(self):
        self.source["robots_required"] = True
        transport = self.transport({"https://news.example/robots.txt": [(200, {}, b"User-agent: *\nDisallow: /article\n")]})
        for _ in range(2):
            self.assertEqual(self.fetcher.fetch(self.url, self.source)["status"], "robots_denied")
        self.assertEqual(len(transport.calls), 1)

    def test_robots_missing_allows_and_crawl_delay_is_honored(self):
        self.source["robots_required"] = True
        transport = self.transport({"https://news.example/robots.txt": [(200, {}, b"User-agent: *\nAllow: /\nCrawl-delay: 8\n")], self.url: [(200, {}, b"article")]})
        self.assertEqual(self.fetcher.fetch(self.url, self.source)["status"], "deferred")
        self.now[0] += 8
        self.assertEqual(self.fetcher.fetch(self.url, self.source)["status"], "ok")
        self.assertEqual(self.fetcher.fetch(self.url + "?next=1", self.source)["status"], "deferred")
        self.assertEqual(len(transport.calls), 2)

    def test_missing_robots_404_allows(self):
        self.source["robots_required"] = True
        transport = self.transport({"https://news.example/robots.txt": [(404, {}, b"")], self.url: [(200, {}, b"article")]})
        self.assertEqual(self.fetcher.fetch(self.url, self.source)["status"], "ok")
        self.assertEqual(len(transport.calls), 2)

    def test_robots_unknown_html_and_malformed_fail_closed(self):
        self.source["robots_required"] = True
        for raw in (b"<html>site login</html>", b"service failure", b"<!DOCTYPE html><title>Just a moment</title>"):
            transport = self.transport({"https://news.example/robots.txt": [(200, {}, raw)]})
            self.assertEqual(self.fetcher.fetch(self.url, self.source)["status"], "robots_denied")
            self.assertEqual(len(transport.calls), 1)

    def test_redirect_target_robots_rechecked(self):
        self.source["robots_required"] = True
        self.source["allowed_hosts"].append("second.example")
        transport = self.transport({"https://news.example/robots.txt": [(200, {}, b"User-agent: *\nAllow: /\n")], self.url: [(302, {"location": "https://second.example/private"}, b"")], "https://second.example/robots.txt": [(200, {}, b"User-agent: *\nDisallow: /private\n")]})
        self.assertEqual(self.fetcher.fetch(self.url, self.source)["status"], "robots_denied")
        self.assertEqual(len(transport.calls), 3)

    def test_response_body_cap(self):
        self.source["max_bytes"] = 10
        self.transport({self.url: [(200, {}, b"x" * 11)]})
        result = self.fetcher.fetch(self.url, self.source)
        self.assertEqual(result["status"], "error")
        self.assertIn("max_bytes", result["error"])

    def test_etag_304_reuses_body_and_retains_original_timestamp(self):
        transport = self.transport({self.url: [(200, {"content-type": "text/plain; charset=utf-8", "etag": '"v1"', "last-modified": "Wed, 01 Jan 2025 00:00:00 GMT"}, "México".encode()), (304, {}, b"")]})
        first = self.fetcher.fetch(self.url, self.source)
        self.now[0] += 10
        second = self.fetcher.fetch(self.url, self.source)
        self.assertEqual(second["status"], "not_modified")
        self.assertEqual(second["body"], "México")
        self.assertEqual(second["retrieved_at"], first["retrieved_at"])
        self.assertEqual(second["sha256"], hashlib.sha256("México".encode()).hexdigest())
        self.assertEqual(transport.calls[1][1]["If-None-Match"], '"v1"')
        self.assertIn("If-Modified-Since", transport.calls[1][1])

    def test_fresh_ttl_is_not_presented_as_new_retrieval(self):
        self.source["cache_ttl_seconds"] = 20
        transport = self.transport({self.url: [(200, {}, b"cached"), (200, {}, b"changed")]})
        first = self.fetcher.fetch(self.url, self.source)
        self.now[0] += 5
        cached = self.fetcher.fetch(self.url, self.source)
        self.assertEqual(cached["status"], "not_modified")
        self.assertEqual(cached["retrieved_at"], first["retrieved_at"])
        self.assertTrue(cached["from_cache"])
        self.assertEqual(len(transport.calls), 1)
        self.now[0] += 16
        refreshed = self.fetcher.fetch(self.url, self.source)
        self.assertEqual(refreshed["status"], "ok")
        self.assertEqual(refreshed["body"], "changed")
        self.assertEqual(len(transport.calls), 2)

    def test_unsupported_binary_type(self):
        self.transport({self.url: [(200, {"content-type": "application/pdf"}, b"%PDF")]})
        self.assertEqual(self.fetcher.fetch(self.url, self.source)["status"], "error")

    @staticmethod
    def wire_connection(data, encoding="identity", length=True, peer="93.184.216.34"):
        response, connection = Mock(), Mock()
        response.status, response.will_close = 200, False
        response.getheaders.return_value = [("Content-Type", "text/plain; charset=utf-8"),
                                            ("Content-Encoding", encoding)]
        if length:
            response.getheaders.return_value.append(("Content-Length", str(len(data))))
        stream = io.BytesIO(data)
        response.read1.side_effect = stream.read
        connection.getresponse.return_value = response
        connection._peer_address = peer
        return connection, response, stream

    def test_compressed_responses_are_bounded_and_report_actual_wire_bytes(self):
        body = "Información mexicana. ".encode() * 100
        raw_deflate = zlib.compressobj(wbits=-15)
        raw = raw_deflate.compress(body) + raw_deflate.flush()
        for encoding, wire in (("gzip", gzip.compress(body)), ("deflate", zlib.compress(body)), ("deflate", raw)):
            with self.subTest(encoding=encoding, length=len(wire)):
                connection, _, _ = self.wire_connection(wire, encoding)
                with patch("peps.collector._PinnedHTTPSConnection", return_value=connection):
                    result = self.fetcher.fetch(self.url + "?n=" + str(len(wire)), self.source)
                self.assertEqual(result["body"], body.decode())
                self.assertEqual(result["wire_bytes"], len(wire))
                self.assertEqual(result["decoded_bytes"], len(body))
                self.assertEqual(result["network_requests"], 1)
                self.assertTrue(result["request_attempted"])
                self.fetcher._discard_connection(connection)
                self.fetcher._local.pool.clear()

    def test_decompression_bomb_reads_only_bounded_decoded_output(self):
        wire = gzip.compress(b"x" * 1_000_000)
        connection, _, _ = self.wire_connection(wire, "gzip")
        with patch("peps.collector._PinnedHTTPSConnection", return_value=connection):
            result = self.fetcher.fetch(self.url, dict(self.source, max_bytes=2000))
        self.assertEqual(result["status"], "error")
        self.assertIn("decoded limit", result["error"])
        self.assertEqual(result["decoded_bytes"], 2001)
        self.assertLess(result["wire_bytes"], 2000)
        self.assertEqual(result["network_requests"], 1)
        connection.close.assert_called_once()

    def test_corrupt_and_truncated_compression_rejected(self):
        good = gzip.compress(b"contenido " * 10)
        for wire in (good[:-3], good[:-1] + bytes([good[-1] ^ 255]), good + b"trailing"):
            with self.subTest(wire=wire):
                connection, _, _ = self.wire_connection(wire, "gzip")
                with patch("peps.collector._PinnedHTTPSConnection", return_value=connection):
                    result = self.fetcher.fetch(self.url, self.source)
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["network_requests"], 1)
                self.assertEqual(result["body"], "")
                connection.close.assert_called_once()

    def test_wire_limit_independent_from_decoded_limit(self):
        connection, _, stream = self.wire_connection(b"a" * 500, length=False)
        with patch("peps.collector._PinnedHTTPSConnection", return_value=connection):
            result = self.fetcher.fetch(self.url, dict(self.source, max_bytes=1000, max_wire_bytes=100))
        self.assertEqual(result["status"], "error")
        self.assertIn("wire limit", result["error"])
        self.assertEqual(result["wire_bytes"], 101)
        self.assertEqual(stream.tell(), 101)

    def test_keepalive_reuses_connection_after_fresh_dns_validation(self):
        connection, response, _ = self.wire_connection(b"one")
        second_connection, second_response, _ = self.wire_connection(b"two")
        connection.getresponse.side_effect = [response, second_response]
        self.fetcher._dns = Mock(return_value=PUBLIC_DNS)
        with patch("peps.collector._PinnedHTTPSConnection", return_value=connection) as factory:
            first = self.fetcher.fetch(self.url, self.source)
            second = self.fetcher.fetch(self.url + "2", self.source)
        self.assertEqual((first["body"], second["body"]), ("one", "two"))
        self.assertEqual(factory.call_count, 1)
        self.assertEqual(self.fetcher._dns.call_count, 4)
        connection.close.assert_not_called()
        self.assertEqual(self.fetcher.metrics_snapshot()["reused_connections"], 1)
        self.fetcher.close()
        connection.close.assert_called_once()

    def test_keepalive_rebinding_to_private_dns_is_denied(self):
        connection, _, _ = self.wire_connection(b"one")
        with patch("peps.collector._PinnedHTTPSConnection", return_value=connection):
            self.assertEqual(self.fetcher.fetch(self.url, self.source)["status"], "ok")
            private = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 443))]
            self.fetcher._dns = Mock(side_effect=[PUBLIC_DNS, private])
            result = self.fetcher.fetch(self.url + "2", self.source)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["network_requests"], 0)
        self.assertEqual(connection.request.call_count, 1)
        self.assertEqual(self.fetcher._dns.call_count, 2)

    def test_real_http_parser_keeps_one_socket_for_two_responses(self):
        class KeepaliveWire:
            def __init__(self):
                self.stream = io.BytesIO()
                self.requests = 0
                self.closed = False
            def settimeout(self, _):
                pass
            def recv_into(self, buffer):
                value = self.stream.read(len(buffer))
                buffer[:len(value)] = value
                return len(value)
            def sendall(self, data):
                self.requests += 1
                self.stream = io.BytesIO(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nContent-Type: text/plain\r\n\r\nhello")
            def close(self):
                self.closed = True
        wire = KeepaliveWire()
        connection = http.client.HTTPSConnection("news.example")
        connection.sock, connection._peer_address = wire, "93.184.216.34"
        with patch("peps.collector._PinnedHTTPSConnection", return_value=connection) as factory:
            first = self.fetcher.fetch(self.url, self.source)
            second = self.fetcher.fetch(self.url + "2", self.source)
        self.assertEqual((first["body"], second["body"]), ("hello", "hello"))
        self.assertEqual(wire.requests, 2)
        self.assertEqual(factory.call_count, 1)
        self.assertFalse(wire.closed)
        self.assertEqual(self.fetcher.metrics_snapshot()["reused_connections"], 1)
        self.fetcher.close()
        self.assertTrue(wire.closed)

    def test_keepalive_changed_public_dns_closes_old_socket(self):
        first, _, _ = self.wire_connection(b"one")
        second, _, _ = self.wire_connection(b"two", peer="93.184.216.35")
        with patch("peps.collector._PinnedHTTPSConnection", side_effect=[first, second]) as factory:
            self.fetcher.fetch(self.url, self.source)
            self.fetcher._dns = lambda *_, **__: [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.35", 443))]
            result = self.fetcher.fetch(self.url + "2", self.source)
        self.assertEqual(result["body"], "two")
        self.assertEqual(factory.call_count, 2)
        first.close.assert_called_once()
        self.assertEqual(self.fetcher.metrics_snapshot()["reused_connections"], 0)

    def test_warm_cache_counts_zero_http_and_persisted_metrics_survive_restart(self):
        transport = self.transport({self.url: [(200, {}, b"cached")]})
        source = dict(self.source, cache_ttl_seconds=10)
        first = self.fetcher.fetch(self.url, source)
        warm = self.fetcher.fetch(self.url, source)
        self.assertEqual((first["network_requests"], warm["network_requests"]), (1, 0))
        self.assertEqual((warm["wire_bytes"], warm["decoded_bytes"]), (0, 0))
        self.assertFalse(warm["request_attempted"])
        self.assertEqual(warm["cache_status"], "fresh")
        other = SafeFetcher({}, Path(self.directory.name))
        self.addCleanup(other.close)
        metrics = other.metrics_snapshot()
        self.assertEqual(metrics["network_requests"], len(transport.calls))
        self.assertEqual(metrics["wire_bytes"], 6)
        self.assertEqual(metrics["cache_hits"], 1)
        self.assertEqual(metrics["status_counts"], {"200": 1})

    def test_robots_and_courtesy_deferrals_have_distinct_request_counts(self):
        self.source.update(robots_required=True, min_interval_seconds=5)
        transport = self.transport({"https://news.example/robots.txt": [(404, {}, b"")], self.url: [(200, {}, b"body")]})
        deferred = self.fetcher.fetch(self.url, self.source)
        self.assertEqual(deferred["status"], "deferred")
        self.assertEqual(deferred["network_requests"], 1)
        again = self.fetcher.fetch(self.url, self.source)
        self.assertEqual(again["network_requests"], 0)
        self.now[0] += 5
        self.assertEqual(self.fetcher.fetch(self.url, self.source)["network_requests"], 1)
        metrics = self.fetcher.metrics_snapshot()
        self.assertEqual(metrics["network_requests"], 2)
        self.assertEqual(metrics["by_kind"]["robots"]["network_requests"], 1)
        self.assertEqual(metrics["by_kind"]["content"]["network_requests"], 1)
        self.assertEqual(metrics["status_counts"], {"200": 1, "404": 1})
        self.assertEqual(len(transport.calls), 2)

    def test_robots_refresh_is_coalesced_across_fetchers(self):
        self.source["robots_required"] = True
        entered, release = threading.Event(), threading.Event()
        calls = []
        def transport(url, *_):
            calls.append(url)
            if url.endswith("robots.txt"):
                entered.set()
                self.assertTrue(release.wait(3))
                return 200, {}, b"User-agent: *\nDisallow: /article\n"
            raise AssertionError("Article was forbidden")
        self.fetcher._transport = transport
        other = SafeFetcher({}, Path(self.directory.name))
        self.addCleanup(other.close)
        other._clock, other._dns, other._transport = self.fetcher._clock, self.fetcher._dns, transport
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = pool.submit(self.fetcher.fetch, self.url, self.source)
            self.assertTrue(entered.wait(3))
            concurrent = other.fetch(self.url, self.source)
            release.set()
            first = pending.result()
        self.assertEqual(concurrent["error_code"], "robots_pending")
        self.assertEqual(concurrent["network_requests"], 0)
        self.assertEqual(first["status"], "robots_denied")
        self.assertEqual(len(calls), 1)

    def test_cache_directives_prevent_false_freshness(self):
        source = dict(self.source, cache_ttl_seconds=100)
        self.transport({self.url: [(200, {"cache-control": "max-age=8", "age": "5", "etag": '"v1"'}, b"old"),
                                   (304, {}, b""), (200, {"cache-control": "no-store"}, b"new"),
                                   (200, {}, b"newer")]})
        self.fetcher.fetch(self.url, source)
        self.now[0] += 2
        self.assertEqual(self.fetcher.fetch(self.url, source)["cache_status"], "fresh")
        self.now[0] += 2
        self.assertEqual(self.fetcher.fetch(self.url, source)["cache_status"], "revalidated")
        self.now[0] += 9
        self.assertEqual(self.fetcher.fetch(self.url, source)["body"], "new")
        self.assertEqual(self.fetcher.fetch(self.url, source)["body"], "newer")

    def test_no_cache_directive_survives_304(self):
        source = dict(self.source, cache_ttl_seconds=100)
        transport = self.transport({self.url: [(200, {"cache-control": "no-cache", "etag": '"v1"'}, b"old"), (304, {}, b""), (304, {}, b"")]})
        for _ in range(3):
            self.fetcher.fetch(self.url, source)
        self.assertEqual(len(transport.calls), 3)

    def test_closed_fetcher_performs_no_request(self):
        self.fetcher.close()
        transport = self.transport({})
        result = self.fetcher.fetch(self.url, self.source)
        self.assertEqual(result["status"], "error")
        self.assertFalse(result["request_attempted"])
        self.assertEqual(transport.calls, [])

    def test_transport_budget_rejection_before_http_does_not_invent_download(self):
        self.fetcher._transport = Mock(side_effect=CollectionError("Fixture request cap reached"))
        result = self.fetcher.fetch(self.url, self.source)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["network_requests"], 0)
        self.assertFalse(result["request_attempted"])
        self.assertEqual(self.fetcher.metrics_snapshot()["network_requests"], 0)

    def test_sqlite_connections_are_reused_per_thread_and_closed_explicitly(self):
        with self.fetcher._db() as first:
            first_id = id(first)
        with self.fetcher._db() as second:
            self.assertEqual(first_id, id(second))
        def in_worker():
            with self.fetcher._db() as worker:
                return id(worker)
        with ThreadPoolExecutor(1) as pool:
            worker_id = pool.submit(in_worker).result()
        self.assertNotEqual(first_id, worker_id)
        self.fetcher.close()
        with self.assertRaises(Exception):
            first.execute('SELECT 1')
        self.assertEqual(self.fetcher.metrics_snapshot()["network_requests"], 0)


class SourceAdapterTests(unittest.TestCase):
    def test_search_name_is_quoted_spanish_and_encoded(self):
        url = build_search_url("María López & Asociados", {"id": "gdelt"})
        self.assertTrue(url.startswith("https://api.gdeltproject.org/"))
        from urllib.parse import parse_qs, urlsplit
        query = parse_qs(urlsplit(url).query)["query"][0]
        self.assertEqual(query, '"María López & Asociados" (México OR Mexico)')

    def test_rss_template_escapes_query(self):
        url = build_search_url({"name": "Empresa & Hijos"}, {"kind": "rss", "url": "https://news.example/rss?q={query}"})
        self.assertIn("%26", url)
        self.assertNotIn("{query}", url)

    def test_visible_text_excludes_executable_markup(self):
        html = "<head><title>Título</title><style>secret</style></head><nav>menu</nav><p>Acusación &amp; respuesta.</p><script>bad()</script><p>Información pública.</p>"
        self.assertEqual(extract_text(html), "Acusación & respuesta. Información pública.")
        self.assertEqual(len(extract_text("x" * 5000, 100)), 100)

    def test_gdelt_discovery_is_candidate_not_adverse_fact(self):
        response = {"url": "https://api.gdeltproject.org/api/v2/doc/doc", "status": "ok", "body": json.dumps({"articles": [{"url": "https://news.example/article", "title": "Título de noticia", "seendate": "20261015T120000Z"}, {"url": "file:///secret", "title": "bad"}, {"url": "https://news.example/article", "title": "duplicate"}]})}
        results = parse_results(response, {"id": "gdelt", "kind": "gdelt"})
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["source_kind"], "news")
        self.assertEqual(results[0]["text"], "")
        self.assertIsNone(results[0]["published_at"])
        self.assertEqual(results[0]["discovered_at"], "2026-10-15T12:00:00+00:00")

    def test_rss_and_atom_and_unsafe_xml(self):
        rss = '<rss><channel><item><title>Nota</title><link>https://news.example/a</link><description>&lt;p&gt;Resumen&lt;/p&gt;</description><pubDate>Thu, 15 Oct 2026 12:00:00 GMT</pubDate></item></channel></rss>'
        response = {"url": "https://news.example/rss", "status": "ok", "body": rss}
        result = parse_results(response, {"id": "feed", "kind": "rss"})
        self.assertEqual(result[0]["text"], "Resumen")
        self.assertEqual(result[0]["published_at"], "2026-10-15T12:00:00+00:00")
        atom = '<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Nota Atom</title><link rel="alternate" href="/b"/><summary>Texto</summary><updated>2026-10-15T13:00:00Z</updated></entry></feed>'
        response["body"] = atom
        atom_result = parse_results(response, {"kind": "atom"})[0]
        self.assertEqual(atom_result["url"], "https://news.example/b")
        self.assertIsNone(atom_result["published_at"])
        self.assertEqual(atom_result["updated_at"], "2026-10-15T13:00:00+00:00")
        response["body"] = '<!DOCTYPE rss [<!ENTITY ex SYSTEM "file:///etc/passwd">]><rss>&ex;</rss>'
        with self.assertRaisesRegex(ValueError, "forbidden"):
            parse_results(response, {"kind": "rss"})

    def test_gdelt_empty_articles_is_valid_but_malformed_json_is_not(self):
        response = {"url": "https://api.gdeltproject.org/api/v2/doc/doc", "status": "ok", "body": '{"articles": []}'}
        source = {"id": "gdelt", "kind": "gdelt"}
        self.assertEqual(parse_results(response, source), [])
        for body in ("", "Please limit your requests", "<html>Temporary failure</html>", '{"articles":', "[]", "null", "{}", '{"articles": null}', '{"articles": {}}', '{"articles": [null]}', '{"articles": [{}]}'):
            with self.subTest(body=body), self.assertRaises(ValueError):
                parse_results(dict(response, body=body), source)

    def test_gdelt_error_envelope_is_not_an_empty_success(self):
        response = {"url": "https://api.gdeltproject.org/api/v2/doc/doc", "status": "ok"}
        source = {"id": "gdelt", "kind": "gdelt"}
        for payload in ({"error": "Rate limit", "articles": []}, {"errors": ["Unavailable"], "articles": []}, {"status": "error", "articles": []}, {"success": False, "articles": []}):
            with self.subTest(payload=payload), self.assertRaisesRegex(ValueError, "error envelope"):
                parse_results(dict(response, body=json.dumps(payload)), source)

    def test_valid_empty_rss_atom_and_rdf_are_distinct_from_invalid_xml(self):
        response = {"url": "https://news.example/feed", "status": "ok"}
        for body in ('<rss version="2.0"><channel><title>Empty feed</title></channel></rss>', '<feed xmlns="http://www.w3.org/2005/Atom"/>', '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"/>'):
            self.assertEqual(parse_results(dict(response, body=body), {"kind": "rss"}), [])
        for body in ("", "<rss><channel>", "<error>Unavailable</error>", "<html><body>Service unavailable</body></html>", "<rss/>"):
            with self.subTest(body=body), self.assertRaises(ValueError):
                parse_results(dict(response, body=body), {"kind": "rss"})

    def test_direct_page_metadata_and_status_gate(self):
        response = {"url": "https://news.example/a", "status": "ok", "body": '<head><title>Investigación</title><meta property="article:published_time" content="2026-10-14T10:00:00Z"></head><article><p>Texto público</p></article>'}
        result = parse_results(response, {"id": "official", "kind": "official"})
        self.assertEqual(result[0]["title"], "Investigación")
        self.assertEqual(result[0]["source_kind"], "official")
        self.assertEqual(result[0]["text"], "Texto público")
        self.assertEqual(result[0]["published_at"], "2026-10-14T10:00:00+00:00")
        response["status"] = "blocked"
        self.assertEqual(parse_results(response, {"kind": "news"}), [])


if __name__ == "__main__":
    unittest.main()
