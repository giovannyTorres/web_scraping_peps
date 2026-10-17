"""Functional/security tests, never simulated acquisition benchmarks."""
import base64
import copy
import gzip
import hashlib
import json
import unittest
from urllib.parse import parse_qs, urlsplit

from peps.archive_discovery import (
    ArchiveNameMatcher, ArchiveValidationError, archive_original_url,
    build_shared_index_plan, commoncrawl_page_info, commoncrawl_page_url,
    decode_warc_response, parse_commoncrawl_index, parse_wayback_cdx,
    warc_range_request, wayback_cdx_url, wayback_document, wayback_snapshot_url,
)


def sha1(value):
    return base64.b32encode(hashlib.sha1(value).digest()).decode().rstrip("=")


def capture(*, target="https://news.example.org/story", body=b"<html><article>Neutral test article.</article></html>",
            date="2024-10-05T08:47:00Z", extra_warc="", extra_http="",
            mime="text/html; charset=utf-8", status=200, digest=None):
    http = (f"HTTP/1.1 {status} Example\r\nContent-Type: {mime}\r\n"
            f"Content-Length: {len(body)}\r\n{extra_http}\r\n").encode() + body
    warc = ("WARC/1.0\r\nWARC-Type: response\r\n"
            f"WARC-Date: {date}\r\nWARC-Target-URI: {target}\r\n"
            "WARC-Protocol: h2\r\nWARC-Protocol: tls/1.3\r\n"
            f"WARC-Payload-Digest: sha1:{sha1(body)}\r\n"
            f"WARC-Block-Digest: sha1:{sha1(http)}\r\n"
            f"Content-Length: {len(http)}\r\n{extra_warc}\r\n").encode() + http + b"\r\n\r\n"
    raw = gzip.compress(warc, mtime=0)
    row = {"url": "https://news.example.org/story", "timestamp": "20241005084700",
           "mime": "text/html", "status": "200", "digest": digest or sha1(body),
           "filename": "crawl-data/CC-MAIN-2024-42/segments/123.45/warc/CC-MAIN-20241005000000-00001.warc.gz",
           "offset": "100", "length": str(len(raw)), "encoding": "UTF-8"}
    headers = {"Content-Range": f"bytes 100-{99 + len(raw)}/{1000 + len(raw)}",
               "Content-Length": str(len(raw))}
    return row, raw, headers


def decoded(row, raw, headers, **kwargs):
    return decode_warc_response(row, raw, http_status=206, response_headers=headers, **kwargs)


def cdx_payload(original="https://news.example.org/story", rows=1):
    schema = ["urlkey", "timestamp", "original", "mimetype", "statuscode", "digest", "length"]
    row = ["org,example,news)/story", "20241005084700", original, "text/html", "200", "A" * 32, "123"]
    return json.dumps([schema] + [row] * rows)


class ArchivePlanningTests(unittest.TestCase):
    def test_three_hundred_entities_share_host_queries_and_www_index_alias(self):
        # This checks plan cardinality, not simulated execution or timing.
        people = [{"id": str(i), "name": "Neutral Public Person"} for i in range(300)]
        plan = build_shared_index_plan(people, ["news.example.org", "www.news.example.org"],
                                       ["CC-MAIN-2024-42", "CC-MAIN-2026-39"])
        self.assertEqual(len(plan), 2)
        self.assertTrue(all(row["entity_count"] == 300 for row in plan))
        self.assertTrue(all("Neutral" not in row["url"] for row in plan))

    def test_invalid_domains_collections_and_unbounded_pages_fail_closed(self):
        for host in ["localhost", "127.0.0.1", "private.internal", "user@news.example.org", "news.example.org/path"]:
            with self.subTest(host=host), self.assertRaises(ArchiveValidationError):
                commoncrawl_page_url("CC-MAIN-2024-42", host)
        for collection in ["../secret", "CC-MAIN-2024-42?bad=1"]:
            with self.assertRaises(ArchiveValidationError):
                commoncrawl_page_url(collection, "news.example.org")
        with self.assertRaises(ArchiveValidationError):
            commoncrawl_page_url("CC-MAIN-2024-42", "news.example.org", page_size=50)

    def test_page_counts_do_not_claim_entity_investigation(self):
        self.assertEqual(commoncrawl_page_info('{"pages":3,"pageSize":5,"blocks":13}')["pages"], 3)
        with self.assertRaises(ArchiveValidationError):
            commoncrawl_page_info('{"pages":2,"pageSize":5,"blocks":13}')

    def test_name_variants_are_local_retrieval_only_and_do_not_mutate_aliases(self):
        person = {"id": "person", "name": "Horacio Aguilar Álvarez de Alba", "kind": "person", "aliases": []}
        before = copy.deepcopy(person)
        matches = ArchiveNameMatcher([person]).match("https://news.example.org/horacio-aguilar-investigacion")
        self.assertEqual(matches[0]["entity_id"], "person")
        self.assertTrue(matches[0]["derived_retrieval_variant"])
        self.assertFalse(matches[0]["identity_verified"])
        self.assertEqual(person, before)
        self.assertEqual(ArchiveNameMatcher([person], derive_short_names=False).match("horacio-aguilar"), [])

    def test_unverified_surname_alias_does_not_override_retrieval_guard(self):
        person = {"id": "person", "name": "Persona Ejemplo Apellido Unico", "aliases": ["Apellido Unico"]}
        self.assertEqual(ArchiveNameMatcher([person], derive_short_names=False).match("apellido-unico"), [])

    def test_shared_wayback_page_uses_resume_key_and_a_bounded_limit(self):
        url = wayback_cdx_url("www.news.example.org", limit=100, resume_key="org,example,news)/story 20240101000000")
        query = parse_qs(urlsplit(url).query)
        self.assertEqual(query["url"], ["news.example.org"])
        self.assertEqual(query["showResumeKey"], ["true"])
        self.assertEqual(query["limit"], ["100"])
        with self.assertRaises(ArchiveValidationError):
            wayback_cdx_url("news.example.org", resume_key="key\nInjected: bad")


class ArchiveIndexTests(unittest.TestCase):
    def test_jsonl_partial_prefix_is_retained_but_never_completed(self):
        row, _, _ = capture()
        payload = json.dumps(row).encode() + b'\n{"url":"truncated'
        result = parse_commoncrawl_index(payload)
        self.assertEqual(len(result["records"]), 1)
        self.assertFalse(result["complete"])
        self.assertIn("unterminated_jsonl", [e["code"] for e in result["errors"]])

    def test_valid_jsonl_requires_exact_length_and_allowed_publishers(self):
        row, _, _ = capture()
        payload = json.dumps(row) + "\n"
        self.assertTrue(parse_commoncrawl_index(payload, allowed_domains=["example.org"])["complete"])
        self.assertFalse(parse_commoncrawl_index(payload, declared_bytes=1)["complete"])
        rejected = parse_commoncrawl_index(payload, allowed_domains=["other.example.org"])
        self.assertEqual(rejected["records"], [])
        self.assertFalse(rejected["complete"])

    def test_html_gateway_response_cannot_become_an_empty_success(self):
        result = parse_commoncrawl_index("<html>504 Gateway timeout</html>\n")
        self.assertEqual(result["records"], [])
        self.assertFalse(result["complete"])
        for empty in [b"", b"\n  \n"]:
            with self.subTest(empty=empty):
                self.assertFalse(parse_commoncrawl_index(empty)["complete"])

    def test_warc_paths_and_nonpublic_originals_are_rejected(self):
        row, _, _ = capture()
        for changes in [{"filename": "../../private.warc.gz"}, {"url": "https://localhost/a"},
                        {"url": "http://127.0.0.1/a"}, {"url": "https://user:password@example.org/a"},
                        {"url": "https://news.example.org:444/a"}]:
            with self.subTest(changes=changes), self.assertRaises(ArchiveValidationError):
                warc_range_request(dict(row, **changes))

    def test_cdx_limits_resume_and_trailing_corruption_remain_visible(self):
        result = parse_wayback_cdx(cdx_payload(rows=2), max_records=2)
        self.assertFalse(result["complete"])
        self.assertTrue(result["limit_reached"])
        result = parse_wayback_cdx(cdx_payload() + "\n\norg,example,news)/story 20241005084700\n")
        self.assertFalse(result["complete"])
        self.assertIsNotNone(result["resume_key"])
        with self.assertRaises(ArchiveValidationError):
            parse_wayback_cdx(cdx_payload() + "trailing corruption")
        with self.assertRaises(ArchiveValidationError):
            parse_wayback_cdx(cdx_payload()[:-4])

    def test_actual_public_json_resume_envelope_roundtrips_without_decoding(self):
        # Structure captured from the public CDX service: [] then [opaque key].
        token = "eJzLrdBJzs_VKSjKT04tztfUNzA01E3OyMxJLEhUMDIwNDOwBEIzI0MDEwApJgxJ"
        envelope = json.loads(cdx_payload(rows=2)) + [[], [token]]
        result = parse_wayback_cdx(json.dumps(envelope), max_records=2)
        self.assertEqual(len(result["records"]), 2)
        self.assertEqual(result["resume_key"], token)
        self.assertFalse(result["complete"])
        query = parse_qs(urlsplit(wayback_cdx_url("news.example.org", resume_key=token)).query)
        self.assertEqual(query["resumeKey"], [token])
        with self.assertRaises(ArchiveValidationError):
            parse_wayback_cdx(json.dumps(json.loads(cdx_payload()) + [[], ["bad\ntoken"]]))


class WarcValidationTests(unittest.TestCase):
    def test_exact_record_preserves_original_capture_and_both_digests(self):
        row, raw, headers = capture()
        document = decoded(row, raw, headers)
        self.assertEqual(document["url"], row["url"])
        self.assertIn("Neutral test article", document["html"])
        provenance = document["archive_provenance"]
        self.assertTrue(provenance["index_and_warc_digest_verified"])
        self.assertFalse(provenance["current_original"])
        self.assertFalse(provenance["publication_date_inferred_from_capture"])
        self.assertFalse(document["identity_verified"])

    def test_ignored_or_wrong_range_is_rejected_before_decompression(self):
        row, raw, headers = capture()
        with self.assertRaises(ArchiveValidationError):
            decode_warc_response(row, raw, http_status=200, response_headers=headers)
        for value in ["bytes 0-100/200", "bytes 100-200/*", "bytes 100-200/200"]:
            with self.subTest(value=value), self.assertRaises(ArchiveValidationError):
                decoded(row, raw, {"Content-Range": value})

    def test_original_date_and_payload_digest_must_match_the_index(self):
        for kwargs in [{"target": "https://other.example.org/story"},
                       {"date": "2024-10-06T08:47:00Z"}, {"digest": "A" * 32}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ArchiveValidationError):
                row, raw, headers = capture(**kwargs)
                decoded(row, raw, headers)

    def test_duplicate_framing_and_bad_http_responses_cannot_enter_the_bank(self):
        for kwargs in [{"extra_warc": "WARC-Target-URI: https://other.example.org/\r\n"},
                       {"extra_http": "Content-Length: 1\r\n"}, {"status": 404},
                       {"mime": "application/javascript"},
                       {"extra_http": "Transfer-Encoding: chunked\r\n"}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ArchiveValidationError):
                row, raw, headers = capture(**kwargs)
                decoded(row, raw, headers)

    def test_gzip_bombs_truncation_and_concatenated_records_are_bounded(self):
        row, raw, headers = capture(body=b"a" * 10000)
        with self.assertRaises(ArchiveValidationError):
            decoded(row, raw, headers, max_uncompressed=1000)
        for budget in [-1, 0, True, 100_000_000]:
            with self.subTest(budget=budget), self.assertRaises(ArchiveValidationError):
                decoded(row, raw, headers, max_uncompressed=budget)
        for bad in [raw[:-5], raw + raw]:
            changed = dict(row, length=str(len(bad)))
            changed_headers = {"Content-Range": f"bytes 100-{99+len(bad)}/{1000+len(bad)}"}
            with self.assertRaises(ArchiveValidationError):
                decoded(changed, bad, changed_headers)


class WaybackDocumentTests(unittest.TestCase):
    def result(self, record):
        return {"url": wayback_snapshot_url(record), "final_url": wayback_snapshot_url(record),
                "status": "ok", "http_status": 200, "content_type": "text/html",
                "body": "<html><article>Neutral test article</article></html>", "sha256": "a" * 64}

    def test_snapshot_is_dated_and_never_becomes_a_current_original(self):
        record = parse_wayback_cdx(cdx_payload())["records"][0]
        result = self.result(record)
        document = wayback_document(record, result,
                                    response_headers={"Memento-Datetime": "Sat, 05 Oct 2024 08:47:00 GMT"})
        self.assertFalse(document["archive_provenance"]["current_original"])
        self.assertTrue(document["archive_provenance"]["memento_date_verified"])
        self.assertFalse(document["archive_provenance"]["cdx_payload_digest_verified"])

    def test_adjacent_capture_is_recorded_instead_of_claiming_requested_date(self):
        record = parse_wayback_cdx(cdx_payload())["records"][0]
        result = self.result(record)
        result["final_url"] = result["final_url"].replace("20241005084700", "20241006084700")
        provenance = wayback_document(record, result)["archive_provenance"]
        self.assertNotEqual(provenance["captured_at"], provenance["requested_captured_at"])

    def test_normal_safe_fetcher_cache_without_new_http_status_remains_usable(self):
        record = parse_wayback_cdx(cdx_payload())["records"][0]
        result = dict(self.result(record), status="not_modified", http_status=None,
                      from_cache=True, cache_status="local")
        self.assertFalse(wayback_document(record, result)["archive_provenance"]["current_original"])
        with self.assertRaises(ArchiveValidationError):
            wayback_document(record, dict(result, from_cache=False))

    def test_redirects_error_pages_and_memento_mismatches_are_rejected(self):
        record = parse_wayback_cdx(cdx_payload())["records"][0]
        for changes in [{"final_url": "https://other.example.org/story"},
                        {"final_url": wayback_snapshot_url(record).replace("news.example.org", "other.example.org")},
                        {"body": "<title>Wayback Machine</title>Not archived"},
                        {"status": "deferred", "http_status": None}]:
            with self.subTest(changes=changes), self.assertRaises(ArchiveValidationError):
                wayback_document(record, dict(self.result(record), **changes))
        with self.assertRaises(ArchiveValidationError):
            wayback_document(record, self.result(record),
                             response_headers={"Memento-Datetime": "Sun, 06 Oct 2024 08:47:00 GMT"})

    def test_original_escapes_and_historical_http_are_preserved_without_fetching_http(self):
        original = "http://news.example.org:80/a%2Fb?x=1&y=2"
        record = parse_wayback_cdx(cdx_payload(original))["records"][0]
        self.assertEqual(archive_original_url(original), original)
        self.assertTrue(wayback_snapshot_url(record).startswith("https://web.archive.org/"))
        self.assertEqual(wayback_document(record, self.result(record))["url"], original)


if __name__ == "__main__":
    unittest.main()
