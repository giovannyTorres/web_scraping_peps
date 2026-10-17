"""Bounded public-archive discovery and verifiable historical documents.

This module performs no HTTP, changes no source permissions and confirms no
identity. Callers use the ordinary source policy/network controls. Common Crawl
is queried once per publisher/collection/page, then matched against the whole
local entity list. An archived capture is never labelled a current download.
"""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import re
import unicodedata
import zlib
from collections import Counter, defaultdict
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote, unquote, urlencode, urlsplit, urlunsplit

from .retrieval_aliases import retrieval_alias_allowed


class ArchiveValidationError(ValueError):
    """Archive metadata or bytes could not establish the claimed provenance."""


_COLLECTION = re.compile(r"CC-MAIN-20\d{2}-\d{2}\Z")
_DOMAIN = re.compile(r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}\Z")
_DIGEST = re.compile(r"[A-Z2-7]{32}\Z")
_FILENAME = re.compile(r"crawl-data/(CC-MAIN-20\d{2}-\d{2})/segments/[A-Za-z0-9._-]+/warc/[A-Za-z0-9._-]+\.warc\.gz\Z")
_PARTICLES = frozenset({"de", "del", "la", "las", "los", "y", "el"})
_COMMON_GIVEN = frozenset({"jose", "maria", "luis", "manuel", "carlos", "juan",
                          "miguel", "angel", "fernando", "jesus", "antonio",
                          "francisco", "javier", "alejandro", "eduardo", "daniel",
                          "ignacio", "roberto", "sergio", "pedro", "victor",
                          "ricardo", "alfredo", "arturo", "jorge", "ramon",
                          "enrique", "raul", "cesar", "alberto"})


def _integer(value, name, minimum=0, maximum=10**15):
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]{1,16}", str(value)):
        raise ArchiveValidationError(f"Invalid {name}")
    number = int(value)
    if not minimum <= number <= maximum:
        raise ArchiveValidationError(f"Out-of-range {name}")
    return number


def _timestamp(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{14}", value):
        raise ArchiveValidationError("Invalid capture timestamp")
    try:
        return datetime.strptime(value, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ArchiveValidationError("Invalid capture timestamp") from exc


def _domain(value):
    if not isinstance(value, str) or value != value.strip() or any(c in value for c in "/:@\\"):
        raise ArchiveValidationError("Expected a public publisher hostname")
    try:
        host = value.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ArchiveValidationError("Invalid publisher hostname") from exc
    if not _DOMAIN.fullmatch(host) or host.endswith((".local", ".internal", ".localhost")):
        raise ArchiveValidationError("Expected a public publisher hostname")
    return host


def archive_original_url(value):
    """Validate a metadata URL without contacting it or upgrading archived HTTP."""
    if not isinstance(value, str) or any(ord(c) <= 32 or ord(c) == 127 for c in value) or "\\" in value:
        raise ArchiveValidationError("Invalid archived original URL")
    try:
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or parts.username is not None or parts.password is not None:
            raise ArchiveValidationError("Invalid archived original URL")
        if parts.port not in (None, 80 if parts.scheme == "http" else 443):
            raise ArchiveValidationError("Nonstandard original URL port")
        host = _domain(parts.hostname or "")
        # Hostnames are admitted later by the normal network resolver. Literal
        # IPs, alternate numeric IP notation and local names are never metadata
        # candidates here. No archived URL is itself fetched by this validator.
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ArchiveValidationError("IP-literal original URL")
        authority = host + (":" + str(parts.port) if parts.port is not None else "")
        return urlunsplit((parts.scheme, authority, parts.path or "/", parts.query, ""))
    except (ValueError, UnicodeError, AttributeError) as exc:
        raise ArchiveValidationError("Invalid archived original URL") from exc


def _normal_words(value):
    value = unicodedata.normalize("NFKD", unquote(str(value)).casefold())
    value = "".join(c for c in value if not unicodedata.combining(c))
    return " ".join(re.findall(r"[^\W_]+", value, re.UNICODE))


class ArchiveNameMatcher:
    """Shared retrieval matcher; derived name variants never establish identity.

    Full names and permitted explicit aliases are retained. Optional variants
    combine the first given name with a later uncommon name token, because news
    slugs commonly omit other given names/surnames. These deliberately broaden
    retrieval and must pass body attribution and identity review downstream.
    """

    def __init__(self, entities, *, derive_short_names=True, max_variants=6):
        max_variants = _integer(max_variants, "retrieval variants", 1, 32)
        self._by_anchor = defaultdict(list)
        candidates = []
        for entity in entities:
            entity_id, name = entity.get("id"), entity.get("name")
            if not isinstance(entity_id, str) or not entity_id or not isinstance(name, str):
                continue
            raw = [(name, False)]
            for alias in entity.get("aliases") or []:
                if not retrieval_alias_allowed(entity, alias):
                    continue
                text = (alias.get("name") or alias.get("value") or alias.get("alias")) if isinstance(alias, dict) else alias
                if isinstance(text, str):
                    raw.append((text, False))
            words = [w for w in _normal_words(name).split() if w not in _PARTICLES]
            if derive_short_names and entity.get("kind", "person") == "person" and len(words) >= 3:
                for word in words[1:]:
                    if word not in _COMMON_GIVEN and len(word) >= 4:
                        raw.append((words[0] + " " + word, True))
            seen = set()
            for text, derived in raw:
                variant = _normal_words(text)
                if len(variant.split()) < 2 or variant in seen:
                    continue
                seen.add(variant)
                candidates.append((entity_id, variant, derived))
                if len(seen) >= max_variants:
                    break
        frequency = Counter(word for _, variant, _ in candidates for word in set(variant.split()))
        for entity_id, variant, derived in candidates:
            anchor = min(variant.split(), key=lambda word: (frequency[word], -len(word)))
            self._by_anchor[anchor].append((entity_id, variant, derived))

    def match(self, value):
        normalized = " " + _normal_words(value) + " "
        found = {}
        for word in set(normalized.split()):
            for entity_id, variant, derived in self._by_anchor.get(word, ()):
                if " " + variant + " " in normalized:
                    key = (entity_id, variant)
                    found[key] = {"entity_id": entity_id, "matched_variant": variant,
                                  "derived_retrieval_variant": derived,
                                  "identity_verified": False}
        return [found[key] for key in sorted(found)]


def commoncrawl_page_url(collection, domain, *, page=0, page_size=1, page_info=False):
    if not isinstance(collection, str) or not _COLLECTION.fullmatch(collection):
        raise ArchiveValidationError("Invalid Common Crawl collection")
    domain = _domain(domain)
    params = {"url": domain, "matchType": "domain", "output": "json",
              "pageSize": _integer(page_size, "page size", 1, 5)}
    if page_info:
        params["showNumPages"] = "true"
    else:
        params["page"] = _integer(page, "page", 0, 100000)
    return f"https://index.commoncrawl.org/{collection}-index?" + urlencode(params)


def build_shared_index_plan(entities, domains, collections, *, page_size=1):
    """One count request per host/collection, independently of list size.

    Callers cache pages by URL and record pending/cut pages. `entity_count`
    describes local matching only, never complete historical investigation.
    """
    entity_ids = {e.get("id") for e in entities if isinstance(e, dict) and e.get("id")}
    return [{"kind": "commoncrawl_index", "domain": domain, "collection": collection,
             "url": commoncrawl_page_url(collection, domain, page_size=page_size, page_info=True),
             "page_size": page_size, "entity_count": len(entity_ids),
             "scope": "shared_metadata_index_not_complete_entity_investigation"}
            for domain in sorted({_domain(d).removeprefix("www.") for d in domains})
            for collection in sorted(set(collections))]


def wayback_cdx_url(domain, *, limit=1000, resume_key=None):
    """A bounded shared metadata page, not one full-domain scan per person."""
    params = [("url", _domain(domain).removeprefix("www.")), ("matchType", "domain"),
              ("output", "json"), ("filter", "statuscode:200"),
              ("filter", "mimetype:text/html"), ("collapse", "urlkey"),
              ("limit", _integer(limit, "CDX page limit", 1, 3000)),
              ("showResumeKey", "true")]
    if resume_key is not None:
        if not _valid_resume_key(resume_key):
            raise ArchiveValidationError("Invalid archive resume key")
        params.append(("resumeKey", resume_key))
    return "https://web.archive.org/cdx/search/cdx?" + urlencode(params)


def _valid_resume_key(value):
    # Current public IA JSON uses an opaque base64url token; older CDX servers
    # exposed a SURT key and timestamp. Tokens are never decoded or executed.
    return (isinstance(value, str) and len(value) <= 2048
            and bool(re.fullmatch(r"(?:[A-Za-z0-9_-]{16,}={0,2}|[^\s\x00-\x1f\x7f]+ \d{14})", value)))


def _payload_bytes(payload, max_bytes):
    if isinstance(payload, str):
        if len(payload) > max_bytes:
            raise ArchiveValidationError("Archive response exceeds its byte budget")
        payload = payload.encode("utf-8")
    if not isinstance(payload, bytes) or len(payload) > max_bytes:
        raise ArchiveValidationError("Archive response exceeds its byte budget")
    return payload


def commoncrawl_page_info(payload, *, max_pages=10000):
    raw = _payload_bytes(payload, 16384)
    try:
        data = json.loads(raw)
        pages = _integer(data["pages"], "pages", 0, max_pages)
        size = _integer(data["pageSize"], "page size", 1, 5)
        blocks = _integer(data["blocks"], "blocks", 0, max_pages * 5)
    except (ValueError, TypeError, KeyError) as exc:
        raise ArchiveValidationError("Invalid Common Crawl page count") from exc
    if pages != (blocks + size - 1) // size:
        raise ArchiveValidationError("Inconsistent Common Crawl page count")
    return {"pages": pages, "page_size": size, "blocks": blocks,
            "complete": pages == 0, "scope": "index_pages_only"}


def _cc_record(row, allowed_domains=None):
    if not isinstance(row, dict):
        raise ArchiveValidationError("Invalid index record")
    original = archive_original_url(row.get("url"))
    host = urlsplit(original).hostname
    if allowed_domains and not any(host == d or host.endswith("." + d) for d in allowed_domains):
        raise ArchiveValidationError("Index original is outside selected publishers")
    _timestamp(row.get("timestamp"))
    if str(row.get("status")) != "200" or row.get("mime") not in ("text/html", "application/xhtml+xml"):
        return None
    if not _DIGEST.fullmatch(str(row.get("digest", ""))):
        raise ArchiveValidationError("Missing index payload digest")
    filename = row.get("filename", "")
    if not isinstance(filename, str) or not _FILENAME.fullmatch(filename) or ".." in filename:
        raise ArchiveValidationError("Invalid WARC object path")
    _integer(row.get("offset"), "WARC offset")
    _integer(row.get("length"), "WARC length", 1, 5_000_000)
    return dict(row, original_url=original, identity_verified=False)


def parse_commoncrawl_index(payload, *, allowed_domains=None, max_bytes=8_000_000,
                            max_records=10000, declared_bytes=None):
    """Keep valid records but expose malformed/truncated pages as incomplete."""
    raw = _payload_bytes(payload, max_bytes)
    allowed_domains = {_domain(d) for d in allowed_domains} if allowed_domains else None
    errors, records = [], []
    if not raw.strip():
        errors.append({"code": "empty_index_response", "message": "Missing JSONL index representation"})
    if raw and not raw.endswith(b"\n"):
        errors.append({"code": "unterminated_jsonl", "message": "Possible truncated index page"})
    if declared_bytes is not None and _integer(declared_bytes, "declared bytes") != len(raw):
        errors.append({"code": "content_length_mismatch"})
    lines = raw.splitlines()
    if len(lines) > max(10000, max_records * 4):
        raise ArchiveValidationError("Index line budget exceeded")
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        if len(records) >= max_records:
            errors.append({"code": "record_budget", "line": index})
            break
        try:
            row = _cc_record(json.loads(line), allowed_domains)
            if row is not None:
                records.append(row)
        except (ValueError, TypeError, UnicodeError) as exc:
            errors.append({"code": "invalid_index_record", "line": index,
                           "message": str(exc)[:200]})
    return {"records": records, "complete": not errors, "errors": errors,
            "line_count": len(lines), "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "scope": "one_index_page_not_all_historical_articles"}


def warc_range_request(record):
    row = _cc_record(record)
    if row is None:
        raise ArchiveValidationError("Only successful HTML captures are downloadable")
    start = int(row["offset"])
    size = int(row["length"])
    return {"url": "https://data.commoncrawl.org/" + row["filename"],
            "headers": {"Range": f"bytes={start}-{start + size - 1}"},
            "max_bytes": size, "original_url": row["url"]}


def _headers(block, *, first_line_prefix=None):
    if len(block) > 65536:
        raise ArchiveValidationError("Oversized archive headers")
    lines = block.split(b"\r\n")
    if first_line_prefix and not lines[0].startswith(first_line_prefix):
        raise ArchiveValidationError("Unexpected archive record type")
    result = {}
    for line in lines[1:]:
        key, separator, value = line.partition(b":")
        if not separator or not key or key[:1] in (b" ", b"\t"):
            raise ArchiveValidationError("Malformed archive header")
        key = key.decode("ascii").lower()
        if key in result:
            # WARC-Protocol is legitimately repeated for h2 and TLS. Only
            # provenance/framing fields need a unique interpretation.
            critical = {"content-length", "content-type", "content-encoding", "transfer-encoding",
                        "warc-type", "warc-target-uri", "warc-date", "warc-payload-digest",
                        "warc-block-digest"}
            if key in critical:
                raise ArchiveValidationError("Duplicate archive header")
            continue
        result[key] = value.decode("iso-8859-1").strip()
    return result


def _html_decode(payload, content_type, encoding=None):
    if content_type.split(";", 1)[0].strip().lower() not in ("text/html", "application/xhtml+xml"):
        raise ArchiveValidationError("Capture is not an HTML article")
    match = re.search(r"charset\s*=\s*[\"']?([A-Za-z0-9_-]+)", content_type, re.I)
    charset = (match.group(1) if match else encoding or "utf-8").lower().replace("_", "-")
    if charset not in ("utf-8", "utf8", "iso-8859-1", "latin-1", "windows-1252", "cp1252", "ascii"):
        raise ArchiveValidationError("Unsupported capture character encoding")
    try:
        return payload.decode(charset)
    except UnicodeError as exc:
        raise ArchiveValidationError("Invalid capture text encoding") from exc


def decode_warc_response(record, payload, *, http_status, response_headers, max_uncompressed=5_000_000):
    """Validate a single exact gzip WARC range before exposing article HTML."""
    max_uncompressed = _integer(max_uncompressed, "decompression budget", 1, 10_000_000)
    request = warc_range_request(record)
    raw = _payload_bytes(payload, request["max_bytes"])
    headers = {str(k).lower(): str(v) for k, v in response_headers.items()}
    expected_range = request["headers"]["Range"][6:]
    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", headers.get("content-range", ""))
    if (http_status != 206 or len(raw) != request["max_bytes"] or not match
            or match.group(1) + "-" + match.group(2) != expected_range
            or int(match.group(3)) <= int(match.group(2))):
        raise ArchiveValidationError("Server did not return the exact requested WARC range")
    if "content-length" in headers and _integer(headers["content-length"], "range content length") != len(raw):
        raise ArchiveValidationError("WARC range content length mismatch")
    try:
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        warc = decoder.decompress(raw, max_uncompressed + 1)
    except zlib.error as exc:
        raise ArchiveValidationError("Invalid gzip WARC record") from exc
    if len(warc) > max_uncompressed or decoder.unconsumed_tail:
        raise ArchiveValidationError("WARC decompression budget exceeded")
    if not decoder.eof or decoder.unused_data:
        raise ArchiveValidationError("Truncated or concatenated WARC gzip")
    warc_header, separator, remainder = warc.partition(b"\r\n\r\n")
    if not separator:
        raise ArchiveValidationError("Missing WARC framing")
    wh = _headers(warc_header, first_line_prefix=b"WARC/1.")
    if wh.get("warc-type") != "response" or wh.get("warc-target-uri") != record["url"]:
        raise ArchiveValidationError("WARC target does not match discovery metadata")
    capture = _timestamp(record["timestamp"])
    if wh.get("warc-date") != capture.strftime("%Y-%m-%dT%H:%M:%SZ"):
        raise ArchiveValidationError("WARC capture date does not match index")
    block_length = _integer(wh.get("content-length"), "WARC block length", 1, max_uncompressed)
    block = remainder[:block_length]
    if len(block) != block_length or remainder[block_length:] not in (b"\r\n\r\n", b"\r\n", b""):
        raise ArchiveValidationError("WARC block framing mismatch")
    http_header, separator, html_bytes = block.partition(b"\r\n\r\n")
    if not separator or not re.match(rb"HTTP/1\.[01] 200(?: |\r\n|$)", http_header):
        raise ArchiveValidationError("Archived HTTP response was not successful HTML")
    hh = _headers(http_header, first_line_prefix=b"HTTP/1.")
    if hh.get("transfer-encoding") or hh.get("content-encoding", "identity").lower() not in ("", "identity"):
        raise ArchiveValidationError("Unsupported archived transfer/content encoding")
    if "content-length" in hh and _integer(hh["content-length"], "archived HTTP length") != len(html_bytes):
        raise ArchiveValidationError("Archived HTTP content length mismatch")
    digest = base64.b32encode(hashlib.sha1(html_bytes).digest()).decode().rstrip("=")
    if record["digest"] != digest or wh.get("warc-payload-digest", "").upper() != "SHA1:" + digest:
        raise ArchiveValidationError("Archive payload digest mismatch")
    if wh.get("warc-block-digest"):
        block_digest = base64.b32encode(hashlib.sha1(block).digest()).decode().rstrip("=")
        if wh["warc-block-digest"].upper() != "SHA1:" + block_digest:
            raise ArchiveValidationError("Archive block digest mismatch")
    html = _html_decode(html_bytes, hh.get("content-type", ""), record.get("encoding"))
    return {"url": archive_original_url(record["url"]), "html": html,
            "content_type": hh["content-type"], "identity_verified": False,
            "archive_provenance": {
                "provider": "commoncrawl", "captured_at": capture.isoformat(),
                "original_url": record["url"], "archive_fetch_url": request["url"],
                "collection": _FILENAME.fullmatch(record["filename"]).group(1),
                "filename": record["filename"], "offset": int(record["offset"]),
                "length": int(record["length"]), "compressed_sha256": hashlib.sha256(raw).hexdigest(),
                "payload_sha256": hashlib.sha256(html_bytes).hexdigest(),
                "payload_sha1_base32": digest, "index_and_warc_digest_verified": True,
                "current_original": False, "publication_date_inferred_from_capture": False}}


def parse_wayback_cdx(payload, *, max_bytes=2_000_000, max_records=1000, allowed_domains=None):
    raw = _payload_bytes(payload, max_bytes)
    try:
        text = raw.decode("utf-8").lstrip()
        data, consumed = json.JSONDecoder().raw_decode(text)
        trailing = text[consumed:].strip()
        resume_key = None
        if trailing:
            if len(trailing) > 2048 or not re.fullmatch(r"[^\s\x00-\x1f\x7f]+ \d{14}", trailing):
                raise ArchiveValidationError("Unexpected trailing CDX response data")
            resume_key = trailing
    except (ValueError, UnicodeError) as exc:
        raise ArchiveValidationError("Invalid or truncated CDX JSON") from exc
    if data == []:
        return {"records": [], "complete": not resume_key, "resume_key": resume_key,
                "scope": "requested_query_only"}
    if (isinstance(data, list) and len(data) >= 3 and data[-2] == []
            and isinstance(data[-1], list) and len(data[-1]) == 1):
        if resume_key or not _valid_resume_key(data[-1][0]):
            raise ArchiveValidationError("Invalid CDX resume envelope")
        resume_key = data[-1][0]
        data = data[:-2]
    required = {"original", "timestamp", "statuscode", "mimetype", "digest"}
    if (not isinstance(data, list) or not isinstance(data[0], list)
            or not required.issubset(data[0]) or len(set(data[0])) != len(data[0])):
        raise ArchiveValidationError("Missing CDX result schema")
    if len(data) - 1 > max_records:
        raise ArchiveValidationError("CDX record budget exceeded")
    domains = {_domain(d) for d in allowed_domains} if allowed_domains else None
    rows = []
    for item in data[1:]:
        if not isinstance(item, list) or len(item) != len(data[0]):
            raise ArchiveValidationError("Invalid CDX row")
        row = dict(zip(data[0], item))
        original = archive_original_url(row["original"])
        _timestamp(row["timestamp"])
        host = urlsplit(original).hostname
        if domains and not any(host == d or host.endswith("." + d) for d in domains):
            raise ArchiveValidationError("CDX original is outside selected publishers")
        if str(row["statuscode"]) == "200" and row["mimetype"] in ("text/html", "application/xhtml+xml"):
            if not _DIGEST.fullmatch(str(row["digest"])):
                raise ArchiveValidationError("Invalid CDX payload digest")
            rows.append(dict(row, original_url=original, identity_verified=False))
    # A limit reached is a cut, not proof the query was exhausted. Resume-key
    # planning is caller-owned and must remain visible in the durable queue.
    return {"records": rows, "complete": not resume_key and len(data) - 1 < max_records,
            "limit_reached": len(data) - 1 == max_records,
            "resume_key": resume_key,
            "scope": "requested_query_only", "sha256": hashlib.sha256(raw).hexdigest()}


def wayback_snapshot_url(record):
    _timestamp(record["timestamp"])
    original = archive_original_url(record.get("original", record.get("original_url")))
    return "https://web.archive.org/web/" + record["timestamp"] + "id_/" + quote(original, safe=":/?=&%+;,@!$()[]")


def wayback_document(record, fetch_result, *, response_headers=None):
    """Describe a real SafeFetcher result; preserve requested vs actual capture."""
    cached = (fetch_result.get("status") == "not_modified"
              and fetch_result.get("from_cache") is True
              and fetch_result.get("cache_status") in ("fresh", "local", "revalidated"))
    if (fetch_result.get("status") not in ("ok", "not_modified")
            or (fetch_result.get("http_status") not in (200, 304) and not cached)):
        raise ArchiveValidationError("Snapshot acquisition did not succeed")
    body = fetch_result.get("body")
    if not isinstance(body, str) or not body or len(body.encode("utf-8")) > 5_000_000:
        raise ArchiveValidationError("Missing or oversized snapshot HTML")
    mime = fetch_result.get("content_type", "").split(";", 1)[0].strip().lower()
    if mime not in ("text/html", "application/xhtml+xml"):
        raise ArchiveValidationError("Snapshot is not HTML")
    final = fetch_result.get("final_url") or fetch_result.get("url")
    parts = urlsplit(final or "")
    match = re.fullmatch(r"/web/(\d{14})(?:id_)?/(https?://.*)", parts.path + ("?" + parts.query if parts.query else ""))
    if (parts.scheme != "https" or parts.hostname != "web.archive.org" or parts.username is not None
            or parts.password is not None or parts.port not in (None, 443) or not match):
        raise ArchiveValidationError("Snapshot redirected outside the archive scope")
    original = archive_original_url(record.get("original", record.get("original_url")))
    if archive_original_url(match.group(2)) != original:
        raise ArchiveValidationError("Snapshot original changed during acquisition")
    capture = _timestamp(match.group(1))
    headers = {str(k).lower(): str(v) for k, v in (response_headers or {}).items()}
    if headers.get("memento-datetime"):
        try:
            memento = parsedate_to_datetime(headers["memento-datetime"])
            if memento.tzinfo is None:
                raise ValueError("Missing memento timezone")
            memento = memento.astimezone(timezone.utc)
        except (ValueError, TypeError) as exc:
            raise ArchiveValidationError("Invalid snapshot memento date") from exc
        if memento != capture:
            raise ArchiveValidationError("Snapshot memento date mismatch")
    if re.search(r"<title>\s*(?:Wayback Machine|Internet Archive)\s*</title>", body, re.I):
        raise ArchiveValidationError("Archive error/navigation page is not an article")
    digest = fetch_result.get("sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ArchiveValidationError("Missing transport payload hash")
    return {"url": original, "html": body, "content_type": fetch_result["content_type"],
            "identity_verified": False, "archive_provenance": {
                "provider": "internetarchive", "original_url": original,
                "archive_fetch_url": final, "requested_archive_url": wayback_snapshot_url(record),
                "requested_captured_at": _timestamp(record["timestamp"]).isoformat(),
                "captured_at": capture.isoformat(), "transport_payload_sha256": digest,
                "cdx_payload_digest": record.get("digest"),
                "cdx_payload_digest_verified": False,
                "memento_date_verified": bool(headers.get("memento-datetime")),
                "current_original": False, "publication_date_inferred_from_capture": False}}
