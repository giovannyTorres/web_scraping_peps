"""Benchmark boundary tests; no request leaves this process."""
import importlib.util
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from peps.collector import SafeFetcher

SPEC = importlib.util.spec_from_file_location("benchmark_news", Path(__file__).resolve().parents[1] / "scripts/benchmark_news.py")
bench = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bench)
PUBLIC = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443))]
FEED = b'<rss><channel><item><title>Note</title><link>https://news.example/note?utm_source=rss</link></item><item><title>same</title><link>https://news.example/note</link></item></channel></rss>'


class NewsBenchmarkTests(unittest.TestCase):
    def config(self):
        return {"sources": [dict(id="first", kind="rss", url="https://news.example/feed", allowed_hosts=["news.example"],
                                 enabled=True, terms_approved=True, min_interval_seconds=0, max_bytes=100000)]}

    @staticmethod
    def transport(instance, url, headers, timeout, max_bytes, addresses):
        if url.endswith("/robots.txt"):
            return 200, {"content-type": "text/plain"}, b"User-agent: *\nAllow: /\n"
        if url.endswith("/feed"):
            return 200, {"content-type": "application/rss+xml"}, FEED
        return 200, {"content-type": "text/html"}, b"<article><p>Article body</p></article>"

    def test_unapproved_config_never_enables_network(self):
        config = self.config()
        config["sources"][0]["terms_approved"] = False
        with tempfile.TemporaryDirectory() as directory, patch.object(SafeFetcher, "_transport", side_effect=AssertionError("Network")):
            with self.assertRaisesRegex(ValueError, "No explicitly approved"):
                bench.run(config, Path(directory))

    def test_cold_warm_dedup_and_separate_article_permission(self):
        with tempfile.TemporaryDirectory() as directory, patch("socket.getaddrinfo", return_value=PUBLIC), patch.object(SafeFetcher, "_transport", self.transport):
            result = bench.run(self.config(), Path(directory), max_requests=6, seconds=5, workers=1)
        self.assertEqual(result["summary"]["network_requests"], 2)
        self.assertEqual(result["summary"]["unique_discovered_urls"], 1)
        self.assertEqual(result["summary"]["successful_article_urls"], 0)
        self.assertEqual(result["phases"][1]["summary"]["network_requests"], 0)
        self.assertFalse(result["capacity"]["coverage_demonstrated"])

    def test_body_requires_both_flags(self):
        for approved in (False, True):
            config = self.config()
            config["sources"][0].update(download_articles=True, article_terms_approved=approved)
            with tempfile.TemporaryDirectory() as directory, patch("socket.getaddrinfo", return_value=PUBLIC), patch.object(SafeFetcher, "_transport", self.transport):
                result = bench.run(config, Path(directory), max_requests=6, seconds=5, workers=1, max_articles=1)
            self.assertEqual(result["summary"]["successful_article_urls"], int(approved))

    def test_atomic_request_cap_includes_robots(self):
        with tempfile.TemporaryDirectory() as directory, patch("socket.getaddrinfo", return_value=PUBLIC), patch.object(SafeFetcher, "_transport", self.transport):
            result = bench.run(self.config(), Path(directory), max_requests=1, seconds=5, workers=2)
        self.assertEqual(result["summary"]["network_requests"], 1)
        self.assertEqual(result["summary"]["unique_discovered_urls"], 0)

    def test_cold_waits_for_interval_after_robots_network_request(self):
        config = self.config()
        config["sources"][0]["min_interval_seconds"] = .05
        with tempfile.TemporaryDirectory() as directory, patch("socket.getaddrinfo", return_value=PUBLIC), patch.object(SafeFetcher, "_transport", self.transport):
            result = bench.run(config, Path(directory), max_requests=6, seconds=5, workers=1)
        cold = result["probes"][0]
        self.assertEqual(cold["status"], "ok")
        self.assertEqual(cold["decisions"][0]["status"], "deferred")
        self.assertTrue(cold["decisions"][0]["request_attempted"])
        self.assertEqual(result["phases"][0]["summary"]["network_requests"], 2)

    def test_reused_state_retains_origin_denials(self):
        with tempfile.TemporaryDirectory() as directory, patch("socket.getaddrinfo", return_value=PUBLIC), patch.object(SafeFetcher, "_transport", self.transport):
            bench.run(self.config(), Path(directory), max_requests=6, seconds=5, workers=1)
            fetcher = SafeFetcher({}, Path(directory))
            fetcher._backoff("news.example", blocked=True)
            fetcher.close()
            result = bench.run(self.config(), Path(directory), max_requests=6, seconds=5, workers=1, reuse_state=True)
        self.assertEqual(result["summary"]["network_requests"], 0)
        self.assertEqual(result["probes"][0]["status"], "blocked")
        self.assertIn("restrictions_retained", result["cold_cache_scope"])

    def test_canonical_url_preserves_article_identity_parameters(self):
        self.assertEqual(bench.canonical_url("https://News.Example/noticia?id=1&utm_source=x#top"), "https://news.example/noticia?id=1")
        self.assertNotEqual(bench.canonical_url("https://news.example/noticia?id=1"), bench.canonical_url("https://news.example/noticia?id=2"))


if __name__ == "__main__":
    unittest.main()
