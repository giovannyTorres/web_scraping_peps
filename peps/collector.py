"""Opt-in, bounded HTTPS collection with process-shared politeness and provenance.

No proxy rotation, browser fingerprint spoofing or CAPTCHA bypass is attempted.
The direct TLS transport pins its socket to a previously validated public address,
while retaining the original hostname for certificate verification and SNI.
"""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import io
import re
import socket
import sqlite3
import ssl
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from contextlib import contextmanager
from typing import Iterator
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser


def _iso(timestamp: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if timestamp is None else timestamp, timezone.utc).isoformat()


class CollectionError(Exception):
    """A request failed a collection boundary."""


class _DeadlineReader(io.RawIOBase):
    """Enforce one wall-clock budget on every underlying socket receive."""
    def __init__(self, sock, deadline: float):
        super().__init__()
        self.sock, self.deadline = sock, deadline

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Response deadline exceeded")
        self.sock.settimeout(remaining)
        return self.sock.recv_into(buffer)


class _DeadlineSocket:
    def __init__(self, sock, deadline: float):
        self.sock, self.deadline = sock, deadline

    def makefile(self, mode: str, buffering=None):
        if mode != "rb":
            raise CollectionError("Unexpected HTTP stream mode")
        return io.BufferedReader(_DeadlineReader(self.sock, self.deadline))


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, hostname: str, addresses: list[tuple], timeout: float, context: ssl.SSLContext):
        super().__init__(hostname, port=443, timeout=timeout, context=context)
        self._addresses = addresses

    def connect(self) -> None:
        last_error = None
        deadline = time.monotonic() + self.timeout
        for family, socktype, proto, _, sockaddr in self._addresses:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Connection deadline exceeded")
            raw_socket = socket.socket(family, socktype, proto)
            raw_socket.settimeout(remaining)
            try:
                raw_socket.connect(sockaddr)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Connection deadline exceeded")
                raw_socket.settimeout(remaining)
                self.sock = self._context.wrap_socket(raw_socket, server_hostname=self.host)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.sock.close()
                    raise TimeoutError("TLS deadline exceeded")
                self.sock.settimeout(remaining)
                return
            except (OSError, ssl.SSLError) as exc:
                last_error = exc
                raw_socket.close()
        raise CollectionError("TLS connection failed: " + type(last_error).__name__)


class SafeFetcher:
    """A fetch returns a decision; scheduling retries belongs to the durable queue.

    Config: user_agent (must identify the operator), cache_ttl_seconds,
    robots_ttl_seconds, circuit_seconds, max_redirects and optional ca_bundle.
    Source hosts must be enumerated exactly. Only HTTPS port 443 is supported.
    Terms and robots are collection controls, not a legal opinion.
    """

    def __init__(self, config: dict, state_dir: Path, offline: bool = False):
        self.config = dict(config or {})
        self.offline = offline
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.state_dir / "network.sqlite3"
        self.user_agent = str(self.config.get("user_agent", "PEPSResearch/2.0 (+operator contact required)"))
        self._clock = time.time
        self._dns = socket.getaddrinfo
        self._tls_context = ssl.create_default_context(cafile=self.config.get("ca_bundle") or None)
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS hosts (
                    host TEXT PRIMARY KEY, next_at REAL NOT NULL DEFAULT 0,
                    blocked_until REAL NOT NULL DEFAULT 0, reason TEXT NOT NULL DEFAULT '',
                    failures INTEGER NOT NULL DEFAULT 0, last_at REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS robots (
                    origin TEXT PRIMARY KEY, body TEXT NOT NULL, expires_at REAL NOT NULL,
                    checked_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS http_cache (
                    url TEXT PRIMARY KEY, body TEXT NOT NULL, content_type TEXT NOT NULL,
                    etag TEXT, last_modified TEXT, sha256 TEXT NOT NULL,
                    retrieved_at TEXT NOT NULL, fetched_at REAL NOT NULL, expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS redirects (
                    url TEXT PRIMARY KEY, target TEXT NOT NULL, expires_at REAL NOT NULL
                );
            """)
            if "last_at" not in {row[1] for row in db.execute("PRAGMA table_info(hosts)")}:
                db.execute("ALTER TABLE hosts ADD COLUMN last_at REAL NOT NULL DEFAULT 0")

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.db_path, timeout=15)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA busy_timeout=15000")
            db.execute("PRAGMA journal_mode=WAL")
            with db:
                yield db
        finally:
            db.close()

    def _result(self, url: str, status: str, **kwargs) -> dict:
        result = dict(url=url, status=status, http_status=None, body="", content_type="",
                      retrieved_at=_iso(self._clock()), sha256=None, error=None,
                      retry_at=None, etag=None, last_modified=None)
        result.update(kwargs)
        return result

    @staticmethod
    def _hostname(value: str) -> str:
        try:
            return value.rstrip(".").encode("idna").decode("ascii").lower()
        except (UnicodeError, AttributeError) as exc:
            raise CollectionError("Invalid hostname") from exc

    def _validate_url(self, url: str, source: dict) -> tuple[str, str, list[tuple]]:
        if not isinstance(url, str) or any(ord(char) < 33 or ord(char) == 127 for char in url):
            raise CollectionError("Invalid URL characters")
        try:
            parts = urlsplit(url)
            if parts.scheme.lower() != "https" or not parts.hostname:
                raise CollectionError("Only absolute HTTPS URLs are allowed")
            if parts.username is not None or parts.password is not None or parts.port not in (None, 443):
                raise CollectionError("Credentials and nonstandard ports are forbidden")
        except ValueError as exc:
            raise CollectionError("Invalid URL") from exc
        host = self._hostname(parts.hostname)
        allowed = {self._hostname(item) for item in source.get("allowed_hosts", [])}
        if host not in allowed:
            raise CollectionError("Host is not in the source allowlist: " + host)
        if host in {"localhost", "metadata.google.internal"} or host.endswith((".local", ".internal", ".localhost")):
            raise CollectionError("Private hostnames are forbidden")
        try:
            records = self._dns(host, 443, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise CollectionError("DNS lookup failed") from exc
        if not records:
            raise CollectionError("DNS returned no addresses")
        for record in records:
            try:
                address = ipaddress.ip_address(record[4][0].split("%", 1)[0])
            except ValueError as exc:
                raise CollectionError("Invalid DNS address") from exc
            if (not address.is_global or getattr(address, "ipv4_mapped", None) is not None
                    or getattr(address, "sixtofour", None) is not None
                    or getattr(address, "teredo", None) is not None):
                raise CollectionError("Non-public addresses are forbidden")
        normalized = urlunsplit(("https", host, parts.path or "/", parts.query, ""))
        return normalized, host, records

    def _transport(self, url: str, headers: dict, timeout: float, max_bytes: int,
                   addresses: list[tuple]) -> tuple[int, dict, bytes]:
        parts = urlsplit(url)
        deadline = time.monotonic() + timeout
        connection = _PinnedHTTPSConnection(parts.hostname, addresses, timeout, self._tls_context)
        # HTTPResponse's buffered readline can perform many small receives.
        # Guard the raw read too, so a slow header/body cannot reset the budget.
        connection.response_class = lambda sock, *args, **kwargs: http.client.HTTPResponse(
            _DeadlineSocket(sock, deadline), *args, **kwargs)
        try:
            path = parts.path or "/"
            if parts.query:
                path += "?" + parts.query
            connection.request("GET", path, headers=headers)
            response = connection.getresponse()
            response_headers = {key.lower(): value for key, value in response.getheaders()}
            length = response_headers.get("content-length")
            if length and int(length) > max_bytes:
                raise CollectionError("Response exceeds max_bytes")
            if response_headers.get("content-encoding", "identity").lower() not in ("", "identity"):
                raise CollectionError("Compressed responses are not accepted")
            # Redirect and denial bodies need not be read. Close the connection.
            if response.status in (301, 302, 303, 307, 308, 304, 403, 429, 451, 503):
                return response.status, response_headers, b""
            chunks, size = [], 0
            while size <= max_bytes:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CollectionError("Response deadline exceeded")
                if connection.sock is not None:
                    connection.sock.settimeout(remaining)
                chunk = response.read1(min(65536, max_bytes + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            data = b"".join(chunks)
            if len(data) > max_bytes:
                raise CollectionError("Response exceeds max_bytes")
            return response.status, response_headers, data
        finally:
            connection.close()

    def _reserve(self, host: str, interval: float) -> tuple[str | None, float | None, str]:
        now = self._clock()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO hosts(host) VALUES (?)", (host,))
            row = db.execute("SELECT * FROM hosts WHERE host=?", (host,)).fetchone()
            if row["blocked_until"] > now:
                return "blocked", row["blocked_until"], row["reason"]
            due = max(row["next_at"], row["last_at"] + max(0.0, interval))
            if due > now:
                return "deferred", due, "Per-host collection interval"
            db.execute("UPDATE hosts SET next_at=?,last_at=? WHERE host=?", (now + max(0.0, interval), now, host))
        return None, None, ""

    def _backoff(self, host: str, headers: dict | None = None, blocked: bool = False,
                 reason: str = "HTTP backoff") -> float:
        now = self._clock()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO hosts(host) VALUES (?)", (host,))
            row = db.execute("SELECT failures FROM hosts WHERE host=?", (host,)).fetchone()
            failures = min(row["failures"] + 1, 12)
            delay = float(self.config.get("circuit_seconds", 86400)) if blocked else min(3600, 30 * 2 ** (failures - 1))
            value = (headers or {}).get("retry-after")
            if value:
                try:
                    delay = max(delay, float(value))
                except (TypeError, ValueError):
                    try:
                        parsed = parsedate_to_datetime(value)
                        if parsed.tzinfo is None:
                            parsed = parsed.replace(tzinfo=timezone.utc)
                        delay = max(delay, parsed.timestamp() - now)
                    except (TypeError, ValueError, OverflowError):
                        pass
            retry = now + max(1.0, delay)
            if blocked:
                db.execute("UPDATE hosts SET blocked_until=?, next_at=MAX(next_at,?), reason=?, failures=? WHERE host=?",
                           (retry, retry, reason, failures, host))
            else:
                db.execute("UPDATE hosts SET next_at=MAX(next_at,?), reason=?, failures=? WHERE host=?",
                           (retry, reason, failures, host))
        return retry

    def _request(self, url: str, source: dict, headers: dict | None = None,
                 check_redirect_robots: bool = False) -> tuple[dict | None, str, int | None, dict, bytes]:
        current, seen = url, set()
        max_bytes = max(1, min(int(source.get("max_bytes", 2_000_000)), 20_000_000))
        timeout = max(1.0, min(float(source.get("timeout_seconds", 20)), 120.0))
        request_headers = {"User-Agent": self.user_agent, "Accept": "text/html,application/json,application/rss+xml,application/xml,text/plain;q=0.8", "Accept-Encoding": "identity"}
        request_headers.update(headers or {})
        try:
            for _ in range(max(0, min(int(self.config.get("max_redirects", 4)), 8)) + 1):
                normalized, host, addresses = self._validate_url(current, source)
                if normalized in seen:
                    raise CollectionError("Redirect loop")
                seen.add(normalized)
                with self._db() as db:
                    redirect = db.execute("SELECT target FROM redirects WHERE url=? AND expires_at>?", (normalized, self._clock())).fetchone()
                if redirect:
                    target = redirect["target"]
                    self._validate_url(target, source)
                    if check_redirect_robots and source.get("robots_required", True):
                        denied_redirect = self._robots_allowed(target, source)
                        if denied_redirect:
                            return denied_redirect, target, None, {}, b""
                    request_headers.pop("If-None-Match", None)
                    request_headers.pop("If-Modified-Since", None)
                    current = target
                    continue
                denied, retry, reason = self._reserve(host, float(source.get("min_interval_seconds", 2)))
                if denied:
                    return self._result(url, denied, error=reason, retry_at=_iso(retry),
                                        error_code="host_interval" if denied == "deferred" else "host_circuit",
                                        request_attempted=False), normalized, None, {}, b""
                status, response_headers, data = self._transport(normalized, request_headers, timeout, max_bytes, addresses)
                response_headers = {str(key).lower(): str(value) for key, value in response_headers.items()}
                if len(data) > max_bytes:
                    raise CollectionError("Response exceeds max_bytes")
                if status in (301, 302, 303, 307, 308):
                    location = response_headers.get("location")
                    if not location:
                        raise CollectionError("Redirect without Location")
                    target = urljoin(normalized, location)
                    # Validate before the next request, including non-public DNS.
                    self._validate_url(target, source)
                    with self._db() as db:
                        db.execute("INSERT OR REPLACE INTO redirects(url,target,expires_at) VALUES (?,?,?)", (normalized, target, self._clock() + 3600))
                    if check_redirect_robots and source.get("robots_required", True):
                        denied_redirect = self._robots_allowed(target, source)
                        if denied_redirect:
                            return denied_redirect, target, None, {}, b""
                    request_headers.pop("If-None-Match", None)
                    request_headers.pop("If-Modified-Since", None)
                    current = target
                    continue
                if status in (403, 451):
                    retry = self._backoff(host, response_headers, True, "HTTP access restriction")
                    return self._result(url, "blocked", http_status=status, error="HTTP access restriction; manual review required", retry_at=_iso(retry)), normalized, status, response_headers, data
                if status in (429, 503):
                    retry = self._backoff(host, response_headers)
                    return self._result(url, "deferred", http_status=status, error="Server requested backoff", retry_at=_iso(retry), error_code="server_backoff", request_attempted=True), normalized, status, response_headers, data
                if status >= 500:
                    retry = self._backoff(host, response_headers)
                    return self._result(url, "deferred", http_status=status, error="Transient server failure", retry_at=_iso(retry), error_code="server_failure", request_attempted=True), normalized, status, response_headers, data
                with self._db() as db:
                    db.execute("UPDATE hosts SET failures=0 WHERE host=?", (host,))
                return None, normalized, status, response_headers, data
            raise CollectionError("Redirect limit exceeded")
        except (OSError, http.client.HTTPException) as exc:
            if "host" in locals():
                retry = self._backoff(host, reason="Transient network failure")
                return self._result(url, "deferred", error="Transient network failure: " + type(exc).__name__, retry_at=_iso(retry), error_code="network_failure", request_attempted=True), current, None, {}, b""
            return self._result(url, "error", error=type(exc).__name__), current, None, {}, b""
        except (CollectionError, ValueError, UnicodeError) as exc:
            return self._result(url, "error", error=str(exc)[:500]), current, None, {}, b""

    def _robots_allowed(self, url: str, source: dict) -> dict | None:
        parts = urlsplit(url)
        origin = "https://" + parts.netloc
        now = self._clock()
        with self._db() as db:
            row = db.execute("SELECT * FROM robots WHERE origin=? AND expires_at>?", (origin, now)).fetchone()
        if row:
            body = row["body"]
        else:
            robot_source = dict(source, max_bytes=min(int(source.get("max_bytes", 2_000_000)), 512_000))
            failure, _, status, headers, raw = self._request(origin + "/robots.txt", robot_source)
            if failure:
                if failure["status"] in ("blocked", "deferred"):
                    return self._result(url, failure["status"], http_status=failure["http_status"], error="robots.txt: " + str(failure["error"]), retry_at=failure["retry_at"], error_code=failure.get("error_code"), request_attempted=failure.get("request_attempted", True))
                return self._result(url, "robots_denied", error="robots.txt unavailable; fail closed")
            if status in (404, 410):
                body = "User-agent: *\nAllow: /\n"
            elif status == 200:
                body = raw.decode("utf-8-sig", errors="replace")
                if self._captcha(body) or "<html" in body[:1000].lower():
                    return self._result(url, "robots_denied", error="robots.txt returned HTML or a challenge; fail closed")
                if body.strip() and not re.search(r"(?im)^\s*user-agent\s*:", body):
                    return self._result(url, "robots_denied", error="Malformed robots.txt; fail closed")
            else:
                return self._result(url, "robots_denied", http_status=status, error="robots.txt could not be verified; fail closed")
            ttl = max(60.0, min(float(self.config.get("robots_ttl_seconds", 86400)), 86400.0))
            with self._db() as db:
                db.execute("INSERT OR REPLACE INTO robots(origin,body,expires_at,checked_at) VALUES (?,?,?,?)", (origin, body, now + ttl, now))
        parser = RobotFileParser(origin + "/robots.txt")
        parser.parse(body.splitlines())
        if not parser.can_fetch(self.user_agent, url):
            return self._result(url, "robots_denied", error="robots.txt disallows this URL")
        # Honor explicit crawl-delay in addition to configured politeness.
        delay = parser.crawl_delay(self.user_agent) or parser.crawl_delay("*")
        request_rate = parser.request_rate(self.user_agent) or parser.request_rate("*")
        if request_rate and request_rate.requests > 0:
            delay = max(float(delay or 0), request_rate.seconds / request_rate.requests)
        if delay:
            source["min_interval_seconds"] = max(float(source.get("min_interval_seconds", 2)), float(delay))
        return None

    @staticmethod
    def _captcha(body: str) -> bool:
        lower = body[:100_000].lower()
        return any(marker in lower for marker in ("<title>just a moment", "<title>access denied", "cf-chl-", "id=\"captcha\"", "id='captcha'", "class=\"g-recaptcha\"", "class='g-recaptcha'", "verify you are human", "unusual traffic from your computer"))

    def fetch(self, url: str, source: dict) -> dict:
        if self.offline:
            return self._result(url, "offline", error="Network collection is disabled")
        if not source.get("enabled", False) or not source.get("terms_approved", False):
            return self._result(url, "blocked", error="Source must be enabled and terms must be reviewed explicitly")
        source = dict(source)
        try:
            normalized, host, _ = self._validate_url(url, source)
        except (CollectionError, ValueError, TypeError) as exc:
            return self._result(url, "error", error=str(exc)[:500])
        if source.get("robots_required", True):
            decision = self._robots_allowed(normalized, source)
            if decision:
                return decision
        now = self._clock()
        with self._db() as db:
            cached = db.execute("SELECT * FROM http_cache WHERE url=?", (normalized,)).fetchone()
        if cached and cached["expires_at"] > now:
            return self._result(normalized, "not_modified", body=cached["body"], content_type=cached["content_type"], retrieved_at=cached["retrieved_at"], sha256=cached["sha256"], etag=cached["etag"], last_modified=cached["last_modified"], from_cache=True, cache_valid_until=_iso(cached["expires_at"]))
        headers = {}
        if cached:
            if cached["etag"]:
                headers["If-None-Match"] = cached["etag"]
            if cached["last_modified"]:
                headers["If-Modified-Since"] = cached["last_modified"]
        failure, final_url, status, response_headers, raw = self._request(normalized, source, headers, check_redirect_robots=True)
        if failure:
            return failure
        ttl = max(0.0, min(float(source.get("cache_ttl_seconds", self.config.get("cache_ttl_seconds", 900))), 86400))
        if status == 304:
            if not cached:
                return self._result(normalized, "error", http_status=304, error="304 without a cached representation")
            with self._db() as db:
                db.execute("UPDATE http_cache SET fetched_at=?,expires_at=? WHERE url=?", (now, now + ttl, normalized))
            return self._result(normalized, "not_modified", http_status=304, body=cached["body"], content_type=cached["content_type"], retrieved_at=cached["retrieved_at"], sha256=cached["sha256"], etag=cached["etag"], last_modified=cached["last_modified"], validated_at=_iso(now), from_cache=True, original_retrieved_at=cached["retrieved_at"])
        if status != 200:
            return self._result(normalized, "error", http_status=status, error="Unexpected HTTP status")
        content_type = response_headers.get("content-type", "text/plain")
        mime = content_type.split(";", 1)[0].strip().lower()
        if mime not in {"text/html", "text/plain", "application/json", "application/rss+xml", "application/atom+xml", "application/xml", "text/xml", "application/xhtml+xml"}:
            return self._result(normalized, "error", http_status=status, content_type=content_type, error="Unsupported content type")
        charset = "utf-8"
        for item in content_type.split(";")[1:]:
            if item.strip().lower().startswith("charset="):
                charset = item.split("=", 1)[1].strip(" \"'")
        try:
            body = raw.decode(charset, errors="replace")
        except LookupError:
            body = raw.decode("utf-8", errors="replace")
        if self._captcha(body):
            final_host = urlsplit(final_url).hostname
            retry = self._backoff(final_host, response_headers, True, "CAPTCHA or access challenge")
            return self._result(normalized, "blocked", http_status=status, error="CAPTCHA or access challenge; manual review required", retry_at=_iso(retry))
        digest = hashlib.sha256(raw).hexdigest()
        retrieved = _iso(self._clock())
        etag, modified = response_headers.get("etag"), response_headers.get("last-modified")
        with self._db() as db:
            db.execute("INSERT OR REPLACE INTO http_cache(url,body,content_type,etag,last_modified,sha256,retrieved_at,fetched_at,expires_at) VALUES (?,?,?,?,?,?,?,?,?)", (normalized, body, content_type, etag, modified, digest, retrieved, now, now + ttl))
        return self._result(normalized, "ok", http_status=status, body=body, content_type=content_type, retrieved_at=retrieved, sha256=digest, etag=etag, last_modified=modified, final_url=final_url)
