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
import json
import re
import socket
import sqlite3
import ssl
import threading
import time
import zlib
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from contextlib import contextmanager
from typing import Iterator
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit
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
        self._peer_address = None

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
                self._peer_address = sockaddr[0]
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
        self._local = threading.local()
        self._pool_lock = threading.Lock()
        self._connections = set()
        self._db_connections = set()
        self._closed = False
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
                CREATE TABLE IF NOT EXISTS robots_refresh (
                    origin TEXT PRIMARY KEY, lease_until REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS http_cache (
                    url TEXT PRIMARY KEY, body TEXT NOT NULL, content_type TEXT NOT NULL,
                    etag TEXT, last_modified TEXT, sha256 TEXT NOT NULL,
                    retrieved_at TEXT NOT NULL, fetched_at REAL NOT NULL, expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS redirects (
                    url TEXT PRIMARY KEY, target TEXT NOT NULL, expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS request_history (
                    id INTEGER PRIMARY KEY, occurred_at TEXT NOT NULL,
                    url TEXT NOT NULL, request_kind TEXT NOT NULL,
                    network_requests INTEGER NOT NULL, wire_bytes INTEGER NOT NULL,
                    decoded_bytes INTEGER NOT NULL, elapsed_seconds REAL NOT NULL,
                    http_status INTEGER, outcome TEXT NOT NULL,
                    cache_status TEXT NOT NULL DEFAULT 'none', reused_connection INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS request_history_time ON request_history(occurred_at);
            """)
            db.execute("BEGIN IMMEDIATE")
            if "last_at" not in {row[1] for row in db.execute("PRAGMA table_info(hosts)")}:
                db.execute("ALTER TABLE hosts ADD COLUMN last_at REAL NOT NULL DEFAULT 0")
            cache_columns = {row[1] for row in db.execute("PRAGMA table_info(http_cache)")}
            for column in ("cache_control", "cache_expires", "final_url"):
                if column not in cache_columns:
                    db.execute(f"ALTER TABLE http_cache ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        # Reopening the last WAL connection for every lookup forces repeated
        # checkpoints and fsyncs. Each worker retains its own connection; no SQL
        # transaction crosses a context boundary and synchronous remains FULL.
        ephemeral = self._closed
        db = None if ephemeral else getattr(self._local, "db_connection", None)
        if db is None:
            db = sqlite3.connect(self.db_path, timeout=15, check_same_thread=False)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA busy_timeout=15000")
            db.execute("PRAGMA journal_mode=WAL")
            if not ephemeral:
                self._local.db_connection = db
                with self._pool_lock:
                    self._db_connections.add(db)
        try:
            with db:
                yield db
        finally:
            if ephemeral:
                db.close()

    def _result(self, url: str, status: str, **kwargs) -> dict:
        result = dict(url=url, status=status, http_status=None, body="", content_type="",
                      retrieved_at=_iso(self._clock()), sha256=None, error=None,
                      retry_at=None, etag=None, last_modified=None,
                      request_attempted=False, network_requests=0, wire_bytes=0,
                      decoded_bytes=0, elapsed_seconds=0.0, cache_status="none")
        result.update(kwargs)
        return result

    def _record_event(self, url: str, request_kind: str, *, network_requests: int = 1,
                      wire_bytes: int = 0, decoded_bytes: int = 0, elapsed_seconds: float = 0,
                      http_status: int | None = None, outcome: str = "error",
                      cache_status: str = "none", reused_connection: bool = False) -> None:
        """Count real HTTP attempts separately from cache decisions and courtesy deferrals.

        wire_bytes is HTTP body bytes, excluding headers and TLS framing. A failed
        capped response records bytes actually read, including its one-byte probe.
        """
        event = dict(network_requests=network_requests, wire_bytes=wire_bytes,
                     decoded_bytes=decoded_bytes, elapsed_seconds=elapsed_seconds,
                     http_status=http_status, outcome=outcome, request_kind=request_kind,
                     cache_status=cache_status, reused_connection=int(reused_connection))
        with self._db() as db:
            db.execute("""INSERT INTO request_history(occurred_at,url,request_kind,
                network_requests,wire_bytes,decoded_bytes,elapsed_seconds,http_status,
                outcome,cache_status,reused_connection) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (_iso(self._clock()), url, request_kind, network_requests, wire_bytes,
                 decoded_bytes, elapsed_seconds, http_status, outcome, cache_status,
                 int(reused_connection)))
        if getattr(self._local, "fetch_events", None) is not None:
            self._local.fetch_events.append(event)

    def metrics_snapshot(self) -> dict:
        """Persisted cumulative metrics shared by all fetchers using this state dir."""
        totals = dict(network_requests=0, wire_bytes=0, decoded_bytes=0,
                      elapsed_seconds=0.0, cache_hits=0, reused_connections=0)
        by_kind, statuses = {}, {}
        with self._db() as db:
            rows = db.execute("""SELECT request_kind, SUM(network_requests) network_requests,
                SUM(wire_bytes) wire_bytes, SUM(decoded_bytes) decoded_bytes,
                SUM(elapsed_seconds) elapsed_seconds,
                SUM(CASE WHEN cache_status IN ('fresh','revalidated') THEN 1 ELSE 0 END) cache_hits,
                SUM(reused_connection) reused_connections FROM request_history GROUP BY request_kind""").fetchall()
            for row in rows:
                values = {key: row[key] or 0 for key in totals}
                by_kind[row["request_kind"]] = values
                for key, value in values.items():
                    totals[key] += value
            for row in db.execute("SELECT outcome,SUM(network_requests) n FROM request_history WHERE network_requests>0 GROUP BY outcome"):
                statuses[row["outcome"]] = row["n"]
        return dict(totals, by_kind=by_kind, status_counts=statuses,
                    wire_bytes_definition="HTTP body bytes, excluding headers and TLS framing")

    def close(self) -> None:
        """Release idle/active pooled sockets after all caller workers have joined."""
        with self._pool_lock:
            self._closed = True
            connections = list(self._connections)
            self._connections.clear()
            databases = list(self._db_connections)
            self._db_connections.clear()
        for connection in connections:
            connection.close()
        for db in databases:
            db.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _discard_connection(self, connection) -> None:
        connection.close()
        with self._pool_lock:
            self._connections.discard(connection)

    def _connection(self, host: str, addresses: list[tuple], timeout: float):
        if self._closed:
            raise CollectionError("Fetcher is closed")
        pool = getattr(self._local, "pool", None)
        if pool is None:
            pool = self._local.pool = {}
        now = time.monotonic()
        idle = max(1.0, min(float(self.config.get("keepalive_seconds", 30)), 120.0))
        allowed = {record[4][0] for record in addresses}
        for key, (connection, peer, used_at) in list(pool.items()):
            if now - used_at > idle or (key == host and peer not in allowed):
                pool.pop(key)
                self._discard_connection(connection)
        entry = pool.pop(host, None)
        if entry:
            connection, peer, _ = entry
            # Socket remains TLS-authenticated to this host. DNS was revalidated by
            # _request; changed answers cannot reuse a previously pinned address.
            if connection.sock is not None and peer in allowed:
                connection.timeout = timeout
                connection.sock.settimeout(timeout)
                return connection, True
            self._discard_connection(connection)
        connection = _PinnedHTTPSConnection(host, addresses, timeout, self._tls_context)
        with self._pool_lock:
            if self._closed:
                connection.close()
                raise CollectionError("Fetcher is closed")
            self._connections.add(connection)
        return connection, False

    def _release_connection(self, host: str, connection, reusable: bool) -> None:
        peer = getattr(connection, "_peer_address", None)
        if not reusable or not isinstance(peer, str) or self._closed:
            self._discard_connection(connection)
            return
        pool = self._local.pool
        max_hosts = max(1, min(int(self.config.get("keepalive_hosts_per_thread", 8)), 64))
        while len(pool) >= max_hosts:
            oldest = min(pool, key=lambda item: pool[item][2])
            self._discard_connection(pool.pop(oldest)[0])
        pool[host] = (connection, peer, time.monotonic())

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
        connection, reused = self._connection(parts.hostname, addresses, timeout)
        metric = self._local.transport_metric = dict(wire_bytes=0, decoded_bytes=0,
                                                     reused_connection=reused,
                                                     request_attempted=False)
        max_wire = min(max_bytes, getattr(self._local, "max_wire_bytes", max_bytes))
        # Each response receives a new absolute deadline, even over keepalive.
        connection.response_class = lambda sock, *args, **kwargs: http.client.HTTPResponse(
            _DeadlineSocket(sock, deadline), *args, **kwargs)
        response, reusable = None, False
        try:
            path = parts.path or "/"
            if parts.query:
                path += "?" + parts.query
            metric["request_attempted"] = True
            connection.request("GET", path, headers=headers)
            response = connection.getresponse()
            response_headers = {key.lower(): value for key, value in response.getheaders()}
            metric["http_status"] = response.status
            # No body is consumed for redirects or explicit collection restrictions.
            if response.status in (301, 302, 303, 307, 308, 304, 403, 429, 451, 503):
                reusable = response.status == 304 and not response.will_close
                return response.status, response_headers, b""
            length = response_headers.get("content-length")
            if length:
                if not re.fullmatch(r"[0-9]+", length.strip()):
                    raise CollectionError("Invalid Content-Length")
                if int(length) > max_wire:
                    raise CollectionError("Response exceeds max_bytes wire limit")
            encoding = response_headers.get("content-encoding", "identity").strip().lower()
            if encoding not in ("", "identity", "gzip", "deflate"):
                raise CollectionError("Unsupported Content-Encoding")
            decoder = zlib.decompressobj(31 if encoding == "gzip" else 15) if encoding in ("gzip", "deflate") else None
            chunks = []
            while metric["wire_bytes"] <= max_wire:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Response deadline exceeded")
                if connection.sock is not None:
                    connection.sock.settimeout(remaining)
                chunk = response.read1(min(65536, max_wire + 1 - metric["wire_bytes"]))
                if not chunk:
                    break
                metric["wire_bytes"] += len(chunk)
                if metric["wire_bytes"] > max_wire:
                    raise CollectionError("Response exceeds max_bytes wire limit")
                if decoder:
                    try:
                        value = decoder.decompress(chunk, max_bytes + 1 - metric["decoded_bytes"])
                    except zlib.error as exc:
                        # RFC 2616 deployments occasionally send raw DEFLATE. A
                        # retry is confined to the first chunk before any output.
                        if encoding == "deflate" and metric["wire_bytes"] == len(chunk) and not metric["decoded_bytes"]:
                            decoder = zlib.decompressobj(-15)
                            try:
                                value = decoder.decompress(chunk, max_bytes + 1)
                            except zlib.error as raw_exc:
                                raise CollectionError("Corrupt compressed response") from raw_exc
                        else:
                            raise CollectionError("Corrupt compressed response") from exc
                    if decoder.unused_data:
                        raise CollectionError("Trailing compressed data is unsupported")
                else:
                    value = chunk
                metric["decoded_bytes"] += len(value)
                if metric["decoded_bytes"] > max_bytes:
                    raise CollectionError("Response exceeds max_bytes decoded limit")
                chunks.append(value)
            if decoder and not decoder.eof:
                raise CollectionError("Truncated compressed response")
            if length and metric["wire_bytes"] != int(length):
                raise CollectionError("Truncated response body")
            reusable = not response.will_close and connection.sock is not None
            return response.status, response_headers, b"".join(chunks)
        finally:
            if response is not None:
                response.close()
            self._release_connection(parts.hostname, connection, reusable)

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

    def _network_request(self, url: str, headers: dict, timeout: float,
                         max_bytes: int, addresses: list[tuple], kind: str):
        started = time.monotonic()
        self._local.transport_metric = dict(request_attempted=False)
        status, data, outcome, transport_error = None, b"", "network_error", None
        try:
            status, response_headers, data = self._transport(url, headers, timeout, max_bytes, addresses)
            outcome = str(status)
            return status, response_headers, data
        except Exception as exc:
            transport_error = exc
            outcome = type(exc).__name__
            raise
        finally:
            metric = getattr(self._local, "transport_metric", None) or {}
            # Custom bounded transports may reject before making any network
            # attempt. Successful fixture transports and OSError failures keep
            # the historical callable contract without inventing HTTP activity.
            attempted = bool(metric.get("request_attempted") or status is not None
                             or isinstance(transport_error, (OSError, http.client.HTTPException)))
            self._record_event(url, kind, network_requests=int(attempted), wire_bytes=metric.get("wire_bytes", len(data)),
                               decoded_bytes=metric.get("decoded_bytes", len(data)),
                               elapsed_seconds=time.monotonic() - started,
                               http_status=status if status is not None else metric.get("http_status"),
                               outcome=outcome, reused_connection=metric.get("reused_connection", False))

    def _request(self, url: str, source: dict, headers: dict | None = None,
                 check_redirect_robots: bool = False, request_kind: str = "content") -> tuple[dict | None, str, int | None, dict, bytes]:
        current, seen = url, set()
        max_bytes = max(1, min(int(source.get("max_bytes", 2_000_000)), 20_000_000))
        timeout = max(1.0, min(float(source.get("timeout_seconds", 20)), 120.0))
        session_deadline = source.get('_deadline_monotonic')

        def exhausted():
            return (self._result(url, 'deferred', error='Session budget expired before the next request',
                                 retry_at=_iso(self._clock()), error_code='session_limit', request_attempted=False),
                    current, None, {}, b'')
        request_headers = {"User-Agent": self.user_agent, "Accept": "text/html,application/json,application/rss+xml,application/xml,text/plain;q=0.8", "Accept-Encoding": "gzip, deflate"}
        request_headers.update(headers or {})
        try:
            for _ in range(max(0, min(int(self.config.get("max_redirects", 4)), 8)) + 1):
                if session_deadline is not None and time.monotonic() >= session_deadline:
                    return exhausted()
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
                self._local.max_wire_bytes = max(1, min(int(source.get("max_wire_bytes", max_bytes)), max_bytes))
                request_timeout = timeout
                if session_deadline is not None:
                    remaining = session_deadline - time.monotonic()
                    if remaining <= 0:
                        return exhausted()
                    request_timeout = min(timeout, remaining)
                status, response_headers, data = self._network_request(normalized, request_headers, request_timeout, max_bytes, addresses, request_kind)
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
            if session_deadline is not None and time.monotonic() >= session_deadline:
                # Exhausting the operator's session allowance is not a server
                # failure and must not create a new host penalty/backoff.
                return exhausted()
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
        # A short durable lease prevents hundreds of concurrent workers from
        # re-fetching the same robots file. No worker sleeps holding a DB lock.
        lease_until = now + max(5.0, min(float(source.get("timeout_seconds", 20)), 120.0)) * 10
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM robots WHERE origin=? AND expires_at>?", (origin, now)).fetchone()
            if not row:
                lease = db.execute("SELECT lease_until FROM robots_refresh WHERE origin=?", (origin,)).fetchone()
                if lease and lease["lease_until"] > now:
                    return self._result(url, "deferred", error="robots.txt verification is in progress",
                                        retry_at=_iso(min(lease["lease_until"], now + 2)),
                                        error_code="robots_pending")
                db.execute("INSERT OR REPLACE INTO robots_refresh(origin,lease_until) VALUES (?,?)", (origin, lease_until))
        if row:
            body = row["body"]
            self._record_event(origin + "/robots.txt", "robots", network_requests=0,
                               outcome="cache_hit", cache_status="fresh")
        else:
            try:
                decision, body = self._refresh_robots(url, origin, source, now)
                if decision:
                    return decision
            finally:
                with self._db() as db:
                    db.execute("DELETE FROM robots_refresh WHERE origin=? AND lease_until=?", (origin, lease_until))
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

    def _refresh_robots(self, url: str, origin: str, source: dict, now: float):
        robot_source = dict(source, max_bytes=min(int(source.get("max_bytes", 2_000_000)), 512_000))
        failure, _, status, headers, raw = self._request(origin + "/robots.txt", robot_source, request_kind="robots")
        if failure:
            if failure["status"] in ("blocked", "deferred"):
                return self._result(url, failure["status"], http_status=failure["http_status"], error="robots.txt: " + str(failure["error"]), retry_at=failure["retry_at"], error_code=failure.get("error_code")), ""
            return self._result(url, "robots_denied", error="robots.txt unavailable; fail closed"), ""
        if status in (404, 410):
            body = "User-agent: *\nAllow: /\n"
        elif status == 200:
            body = raw.decode("utf-8-sig", errors="replace")
            if self._captcha(body) or "<html" in body[:1000].lower():
                return self._result(url, "robots_denied", error="robots.txt returned HTML or a challenge; fail closed"), ""
            if body.strip() and not re.search(r"(?im)^\s*user-agent\s*:", body):
                return self._result(url, "robots_denied", error="Malformed robots.txt; fail closed"), ""
        else:
            return self._result(url, "robots_denied", http_status=status, error="robots.txt could not be verified; fail closed"), ""
        ttl = max(60.0, min(float(self.config.get("robots_ttl_seconds", 86400)), 86400.0))
        with self._db() as db:
            db.execute("INSERT OR REPLACE INTO robots(origin,body,expires_at,checked_at) VALUES (?,?,?,?)", (origin, body, now + ttl, now))
        return None, body

    @staticmethod
    def _captcha(body: str) -> bool:
        lower = body[:100_000].lower()
        return any(marker in lower for marker in ("<title>just a moment", "<title>access denied", "cf-chl-", "id=\"captcha\"", "id='captcha'", "class=\"g-recaptcha\"", "class='g-recaptcha'", "verify you are human", "unusual traffic from your computer"))

    def _cache_policy(self, headers: dict, source: dict) -> tuple[float, bool]:
        ttl = max(0.0, min(float(source.get("cache_ttl_seconds", self.config.get("cache_ttl_seconds", 900))), 86400))
        directives = headers.get("cache-control", "").lower()
        if re.search(r"(?:^|,)\s*no-store(?:\s*(?:,|$))", directives):
            return 0, False
        if re.search(r"(?:^|,)\s*no-cache(?:\s*(?:=|,|$))", directives):
            ttl = 0
        max_age = re.search(r'(?:^|,)\s*max-age\s*=\s*"?(\d+)', directives)
        if max_age:
            try:
                age = max(0.0, float(headers.get("age", 0)))
            except (ValueError, TypeError):
                age = 0
            # Values larger than this already exceed our one-day ceiling. A
            # hostile oversized decimal must not trigger Python's int limit.
            ttl = min(ttl, max(0.0, int(max_age.group(1)[:12]) - age))
        expires = headers.get("expires")
        if expires and not max_age:
            try:
                value = parsedate_to_datetime(expires)
                if value.tzinfo is None:
                    value = value.replace(tzinfo=timezone.utc)
                ttl = min(ttl, max(0.0, value.timestamp() - self._clock()))
            except (TypeError, ValueError, OverflowError):
                ttl = 0
        return ttl, bool(source.get("cache_body", True))

    def fetch(self, url: str, source: dict) -> dict:
        started = time.monotonic()
        self._local.fetch_events = []
        try:
            result = self._fetch(url, source)
            events = self._local.fetch_events
            result.update(network_requests=sum(event["network_requests"] for event in events),
                          wire_bytes=sum(event["wire_bytes"] for event in events),
                          decoded_bytes=sum(event["decoded_bytes"] for event in events),
                          elapsed_seconds=time.monotonic() - started)
            result["request_attempted"] = result["network_requests"] > 0
            return result
        finally:
            self._local.fetch_events = None

    def _fetch(self, url: str, source: dict) -> dict:
        if self._closed:
            return self._result(url, "error", error="Fetcher is closed")
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
        # Older cache rows have no response URL; revalidate before resolving
        # relative links against a base that may have redirected elsewhere.
        if cached and cached["expires_at"] > now and cached["final_url"]:
            self._record_event(normalized, "content", network_requests=0, outcome="cache_hit", cache_status="fresh")
            return self._result(normalized, "not_modified", body=cached["body"], content_type=cached["content_type"], retrieved_at=cached["retrieved_at"], sha256=cached["sha256"], etag=cached["etag"], last_modified=cached["last_modified"], final_url=cached["final_url"] or normalized, from_cache=True, cache_status="fresh", cache_valid_until=_iso(cached["expires_at"]))
        headers = {}
        if cached:
            if cached["etag"]:
                headers["If-None-Match"] = cached["etag"]
            if cached["last_modified"]:
                headers["If-Modified-Since"] = cached["last_modified"]
        failure, final_url, status, response_headers, raw = self._request(normalized, source, headers, check_redirect_robots=True)
        if failure:
            failure["cache_status"] = "stale" if cached else "miss"
            return failure
        if status == 304 and cached:
            # A 304 can omit cache directives; keep the original freshness policy.
            response_headers.setdefault("cache-control", cached["cache_control"])
            response_headers.setdefault("expires", cached["cache_expires"])
        ttl, store_cache = self._cache_policy(response_headers, source)
        if status == 304:
            if not cached:
                return self._result(normalized, "error", http_status=304, error="304 without a cached representation")
            with self._db() as db:
                if store_cache:
                    db.execute("UPDATE http_cache SET fetched_at=?,expires_at=?,final_url=? WHERE url=?", (now, now + ttl, final_url, normalized))
                else:
                    db.execute("DELETE FROM http_cache WHERE url=?", (normalized,))
            self._record_event(normalized, "content", network_requests=0, outcome="cache_revalidated", cache_status="revalidated")
            return self._result(normalized, "not_modified", http_status=304, body=cached["body"], content_type=cached["content_type"], retrieved_at=cached["retrieved_at"], sha256=cached["sha256"], etag=cached["etag"], last_modified=cached["last_modified"], final_url=final_url, validated_at=_iso(now), from_cache=True, cache_status="revalidated", cache_valid_until=_iso(now + ttl), original_retrieved_at=cached["retrieved_at"])
        # Without X-WP-TotalPages a full page implies a tentative next page.
        # WordPress reports the end with a specific 400 envelope. Only that
        # inferred continuation at the configured posts endpoint becomes an
        # empty discovery page; all other HTTP errors retain their meaning.
        if status == 400 and source.get("kind") == "wordpress_api" and source.get("_wordpress_inferred_page") is True:
            try:
                endpoint = urlsplit(source.get("api_url") or source.get("url") or "")
                response_url = urlsplit(final_url)
                pages = parse_qs(response_url.query, keep_blank_values=True, max_num_fields=100).get("page", [])
                valid_page = (len(pages) == 1 and re.fullmatch(r"[0-9]{1,12}", pages[0])
                              and int(pages[0]) > 1)
                error_data = json.loads(raw) if valid_page and endpoint.path == response_url.path else None
            except (ValueError, UnicodeError, RecursionError):
                error_data = None
            if (isinstance(error_data, dict) and error_data.get("code") == "rest_post_invalid_page_number"
                    and isinstance(error_data.get("data"), dict) and error_data["data"].get("status") == 400):
                if cached:
                    with self._db() as db:
                        db.execute("DELETE FROM http_cache WHERE url=?", (normalized,))
                return self._result(normalized, "ok", http_status=400, body="[]", content_type="application/json",
                                    sha256=hashlib.sha256(raw).hexdigest(), final_url=final_url,
                                    pagination_end=True, error_code="publisher_pagination_end", pagination_headers={})
        if status != 200:
            return self._result(normalized, "error", http_status=status, error="Unexpected HTTP status")
        content_type = response_headers.get("content-type", "text/plain")
        mime = content_type.split(";", 1)[0].strip().lower()
        # A .xml.gz sitemap is often a gzip *file*, without HTTP
        # Content-Encoding. Restrict magic-based decoding to this source kind;
        # the same input/output ceiling applies to transport and file decoding.
        sitemap_gzip = source.get("kind") == "sitemap" and raw.startswith(b"\x1f\x8b")
        if sitemap_gzip:
            maximum = max(1, min(int(source.get("max_bytes", 2_000_000)), 20_000_000))
            try:
                decoder = zlib.decompressobj(31)
                decoded = decoder.decompress(raw, maximum + 1)
                if len(decoded) > maximum:
                    raise CollectionError("Sitemap exceeds max_bytes decoded limit")
                if decoder.unused_data:
                    raise CollectionError("Trailing or concatenated sitemap gzip data is unsupported")
                if not decoder.eof:
                    raise CollectionError("Truncated sitemap gzip file")
            except (zlib.error, CollectionError) as exc:
                return self._result(normalized, "error", http_status=status, content_type=content_type,
                                    error=str(exc)[:500] if isinstance(exc, CollectionError) else "Corrupt sitemap gzip file")
            raw = decoded
        accepted_mimes = {"text/html", "text/plain", "application/json", "application/rss+xml", "application/atom+xml", "application/xml", "text/xml", "application/xhtml+xml"}
        if sitemap_gzip:
            accepted_mimes.update({"application/gzip", "application/x-gzip", "application/octet-stream"})
        if mime not in accepted_mimes:
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
            if store_cache:
                db.execute("INSERT OR REPLACE INTO http_cache(url,body,content_type,etag,last_modified,sha256,retrieved_at,fetched_at,expires_at,cache_control,cache_expires,final_url) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (normalized, body, content_type, etag, modified, digest, retrieved, now, now + ttl, response_headers.get("cache-control", ""), response_headers.get("expires", ""), final_url))
            else:
                db.execute("DELETE FROM http_cache WHERE url=?", (normalized,))
        pagination_headers = {key: value.strip() for key in ("x-wp-total", "x-wp-totalpages")
                              if isinstance(value := response_headers.get(key), str)
                              and re.fullmatch(r"[0-9]{1,12}", value.strip())}
        return self._result(normalized, "ok", http_status=status, body=body, content_type=content_type, retrieved_at=retrieved, sha256=digest, etag=etag, last_modified=modified, final_url=final_url, cache_status="refreshed" if cached else "miss", pagination_headers=pagination_headers, cache_valid_until=_iso(now + ttl if store_cache else now))
