"""Opt-in, bounded HTTPS collection with process-shared politeness and provenance.

No proxy rotation, browser fingerprint spoofing or CAPTCHA bypass is attempted.
The direct TLS transport pins its socket to a previously validated public address,
while retaining the original hostname for certificate verification and SNI.
Inherited HTTP proxies use CONNECT to the approved hostname and verified TLS.
Such a trusted gateway resolves the destination remotely; local public-DNS
checks remain mandatory but are not end-to-end address pinning through a proxy.
"""
from __future__ import annotations

import hashlib
import base64
import http.client
import ipaddress
import io
import json
import os
import re
import socket
import sqlite3
import ssl
import threading
import time
import tempfile
import zlib
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from contextlib import contextmanager
from typing import Iterator
from urllib.parse import parse_qs, unquote, urljoin, urlsplit, urlunsplit
from urllib.request import getproxies_environment, proxy_bypass_environment
from urllib.robotparser import RobotFileParser

from .robots_policy import robots_allowed


# A publisher may request an arbitrarily large delay; retain that restriction
# within Python datetime's range, without passing infinity or year 10000 into
# datetime/SQLite. _iso uses UTC arithmetic to avoid Windows CRT date limits.
_MAX_RETRY_TIMESTAMP = 253402214400.0
_ERROR_PREFIX_LIMIT = 4096
_ENVOY_CONNECT_PREFIX = b'upstream connect error or disconnect/reset before headers'


def classify_http_error_prefix(headers, prefix):
    """A narrow diagnostic signature, never a permission or routing decision."""
    server = str(headers.get('server', '')).strip().casefold().split('/', 1)[0]
    encoding = str(headers.get('content-encoding', 'identity')).strip().casefold()
    return ('envoy_upstream_connect_error' if server == 'envoy'
            and encoding in ('', 'identity') and prefix.startswith(_ENVOY_CONNECT_PREFIX)
            else 'unknown_http_5xx')


def _iso(timestamp: float | None = None) -> str:
    # datetime.fromtimestamp delegates to platform gmtime(), which rejects
    # otherwise valid distant dates on Windows. Formatting a stored backoff
    # must not turn that restriction into a spurious transient network error.
    stamp = time.time() if timestamp is None else timestamp
    return (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=stamp)).isoformat()


class CollectionError(Exception):
    """A request failed a collection boundary."""


class _ArticlePathScopeError(CollectionError):
    """An article or redirect left its configured path scope."""


class _APIEndpointScopeError(CollectionError):
    """A documented JSON API request or redirect left its granted scope."""


class _ArchiveEndpointScopeError(CollectionError):
    """An archive discovery request left its exact metadata-only operation."""


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


class _ProxyHTTPSConnection(http.client.HTTPSConnection):
    """An explicitly trusted HTTP gateway; never falls back to direct egress.

    CONNECT uses the hostname so gateway domain policies and outbound identities
    keep working. The proxy sees Basic credentials only in CONNECT headers;
    publisher requests and exception messages never include those credentials.
    """
    def __init__(self, hostname: str, proxy: dict, timeout: float,
                 context: ssl.SSLContext, destination_addresses: list[tuple]):
        super().__init__(proxy['host'], port=proxy['port'], timeout=timeout, context=context)
        headers = {'Host': hostname + ':443'}
        if proxy.get('authorization'):
            headers['Proxy-Authorization'] = proxy['authorization']
        self.set_tunnel(hostname, port=443, headers=headers)
        self._destination_hostname = hostname
        # This marker validates pool reuse against the same locally checked DNS
        # answer set. It does not claim that the gateway used this address.
        self._peer_address = destination_addresses[0][4][0]

    def connect(self) -> None:
        deadline = time.monotonic() + self.timeout
        raw_socket = None
        try:
            addresses = socket.getaddrinfo(self.host, self.port, type=socket.SOCK_STREAM)
            for family, socktype, proto, _, sockaddr in addresses:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('Proxy connection deadline exceeded')
                raw_socket = socket.socket(family, socktype, proto)
                raw_socket.settimeout(remaining)
                try:
                    raw_socket.connect(sockaddr)
                    break
                except OSError:
                    raw_socket.close()
                    raw_socket = None
            if raw_socket is None:
                raise OSError('Proxy connection failed')
            self.sock = raw_socket
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Proxy connection deadline exceeded')
            self.sock.settimeout(remaining)
            # _transport supplies a deadline-aware response class; this also
            # bounds the CONNECT response headers against the request deadline.
            self._tunnel()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Proxy CONNECT deadline exceeded')
            self.sock.settimeout(remaining)
            self.sock = self._context.wrap_socket(self.sock, server_hostname=self._destination_hostname)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Proxy TLS deadline exceeded')
            self.sock.settimeout(remaining)
        except (OSError, http.client.HTTPException, ValueError) as exc:
            self.close()
            if raw_socket is not None:
                raw_socket.close()
            # A gateway can echo credentials or signed URLs in its reason text.
            # Keep transient exception categories so the request layer can
            # distinguish an exhausted session from a retryable network fault.
            message = 'Trusted proxy connection failed: ' + type(exc).__name__
            if isinstance(exc, TimeoutError):
                raise TimeoutError(message) from None
            if isinstance(exc, OSError):
                raise OSError(message) from None
            if isinstance(exc, http.client.HTTPException):
                raise http.client.HTTPException(message) from None
            raise CollectionError(message) from None


class SafeFetcher:
    """A fetch returns a decision; scheduling retries belongs to the durable queue.

    Config: user_agent (must identify the operator), cache_ttl_seconds,
    robots_ttl_seconds, circuit_seconds, max_redirects and optional ca_bundle.
    trust_env_proxy defaults to True and honors HTTPS_PROXY/HTTP_PROXY/NO_PROXY
    (lowercase takes precedence). HTTP gateways only; unsupported configured
    proxy schemes fail closed, without a direct-connection fallback.
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
                CREATE TABLE IF NOT EXISTS http_error_diagnostics (
                    id INTEGER PRIMARY KEY, occurred_at TEXT NOT NULL, url TEXT NOT NULL,
                    source_id TEXT, request_kind TEXT NOT NULL, http_status INTEGER NOT NULL,
                    prefix_sha256 TEXT NOT NULL, prefix_bytes INTEGER NOT NULL,
                    prefix_path TEXT, receipt TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS api_backend_observations (
                    endpoint TEXT NOT NULL, requested_engines TEXT NOT NULL,
                    response_sha256 TEXT NOT NULL, observed_at REAL NOT NULL,
                    content_type TEXT NOT NULL, body BLOB NOT NULL,
                    reported_engines TEXT NOT NULL, diagnostics TEXT NOT NULL,
                    PRIMARY KEY(endpoint,requested_engines,response_sha256)
                );
                CREATE TABLE IF NOT EXISTS api_backend_restrictions (
                    endpoint TEXT NOT NULL, engine TEXT NOT NULL,
                    reason TEXT NOT NULL, first_seen REAL NOT NULL, last_seen REAL NOT NULL,
                    pending_review INTEGER NOT NULL DEFAULT 1,
                    evidence_sha256 TEXT NOT NULL, PRIMARY KEY(endpoint,engine)
                );
                CREATE TABLE IF NOT EXISTS api_route_restrictions (
                    endpoint TEXT NOT NULL, requested_engines TEXT NOT NULL,
                    reason TEXT NOT NULL, first_seen REAL NOT NULL, last_seen REAL NOT NULL,
                    evidence_sha256 TEXT NOT NULL, PRIMARY KEY(endpoint,requested_engines)
                );
            """)
            db.execute("BEGIN IMMEDIATE")
            if "last_at" not in {row[1] for row in db.execute("PRAGMA table_info(hosts)")}:
                db.execute("ALTER TABLE hosts ADD COLUMN last_at REAL NOT NULL DEFAULT 0")
            host_columns = {row[1] for row in db.execute("PRAGMA table_info(hosts)")}
            retry_columns = {
                'retry_state_version': 'INTEGER NOT NULL DEFAULT 0',
                'courtesy_until': 'REAL NOT NULL DEFAULT 0',
                'legacy_retry_until': 'REAL NOT NULL DEFAULT 0',
                'legacy_reason': "TEXT NOT NULL DEFAULT ''",
                'client_retry_until': 'REAL NOT NULL DEFAULT 0',
                'client_failures': 'INTEGER NOT NULL DEFAULT 0',
                'client_generation': 'INTEGER NOT NULL DEFAULT 0',
                'server_retry_until': 'REAL NOT NULL DEFAULT 0',
                'server_failures': 'INTEGER NOT NULL DEFAULT 0',
                'server_generation': 'INTEGER NOT NULL DEFAULT 0',
                'server_reason': "TEXT NOT NULL DEFAULT ''",
                'access_reason': "TEXT NOT NULL DEFAULT ''",
                'retry_class': "TEXT NOT NULL DEFAULT ''",
            }
            for column, declaration in retry_columns.items():
                if column not in host_columns:
                    db.execute(f'ALTER TABLE hosts ADD COLUMN {column} {declaration}')
            # Old next_at combines courtesy, network and server waits. Do not
            # guess its provenance or silently shorten an operator restriction.
            db.execute("UPDATE hosts SET legacy_retry_until=next_at,legacy_reason=reason,"
                       "access_reason=CASE WHEN blocked_until>0 THEN reason ELSE '' END,"
                       "retry_class=CASE WHEN blocked_until>0 AND blocked_until>=next_at THEN 'operator_access' "
                       "WHEN next_at>0 THEN 'legacy_unclassified' ELSE '' END,retry_state_version=1 "
                       "WHERE retry_state_version=0")
            cache_columns = {row[1] for row in db.execute("PRAGMA table_info(http_cache)")}
            for column in ("cache_control", "cache_expires", "final_url", "pagination_headers", "validation_error", "api_backend_metadata"):
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
                SUM(CASE WHEN cache_status IN ('fresh','revalidated','local') THEN 1 ELSE 0 END) cache_hits,
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
        proxy = self._proxy_for_host(host)
        pool_key = (host, proxy['key'] if proxy else '')
        now = time.monotonic()
        idle = max(1.0, min(float(self.config.get("keepalive_seconds", 30)), 120.0))
        allowed = {record[4][0] for record in addresses}
        for key, (connection, peer, used_at) in list(pool.items()):
            if now - used_at > idle or (key == pool_key and peer not in allowed):
                pool.pop(key)
                self._discard_connection(connection)
        entry = pool.pop(pool_key, None)
        if entry:
            connection, peer, _ = entry
            # The TLS session authenticates this exact destination hostname.
            # A changed DNS answer cannot reuse a direct pinned connection (or
            # the proxy connection's locally validated DNS marker).
            if connection.sock is not None and peer in allowed:
                connection.timeout = timeout
                connection.sock.settimeout(timeout)
                return connection, True
            self._discard_connection(connection)
        connection = (_ProxyHTTPSConnection(host, proxy, timeout, self._tls_context, addresses)
                      if proxy else _PinnedHTTPSConnection(host, addresses, timeout, self._tls_context))
        connection._pool_key = pool_key
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
        pool[getattr(connection, '_pool_key', (host, ''))] = (connection, peer, time.monotonic())

    def _proxy_for_host(self, host: str) -> dict | None:
        if self.config.get('trust_env_proxy', True) is False:
            return None
        proxies = getproxies_environment()
        if proxy_bypass_environment(host, proxies):
            return None
        value = proxies.get('https') or proxies.get('http')
        if not value:
            return None
        try:
            parts = urlsplit(value)
            if (parts.scheme.lower() != 'http' or not parts.hostname
                    or parts.path not in ('', '/') or parts.query or parts.fragment
                    or any(ord(char) < 33 or ord(char) == 127 for char in value)):
                raise ValueError('Invalid proxy endpoint')
            proxy_host = self._hostname(parts.hostname)
            port = parts.port if parts.port is not None else 80
            if not 1 <= port <= 65535:
                raise ValueError('Invalid proxy port')
            authorization = None
            if parts.username is not None or parts.password is not None:
                credentials = unquote(parts.username or '') + ':' + unquote(parts.password or '')
                authorization = 'Basic ' + base64.b64encode(credentials.encode('utf-8')).decode('ascii')
            # Avoid storing a credential-bearing proxy URL in pool keys or logs.
            return dict(host=proxy_host, port=port, authorization=authorization,
                        key=hashlib.sha256(value.encode('utf-8')).hexdigest())
        except (ValueError, UnicodeError, TypeError):
            raise CollectionError('Invalid or unsupported inherited HTTPS proxy configuration') from None

    @staticmethod
    def _hostname(value: str) -> str:
        try:
            return value.rstrip(".").encode("idna").decode("ascii").lower()
        except (UnicodeError, AttributeError) as exc:
            raise CollectionError("Invalid hostname") from exc

    def _validate_url(self, url: str, source: dict, *, resolve=True) -> tuple[str, str, list[tuple]]:
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
        if source.get('kind') == 'arquivo_fulltext':
            from .archive_text_search import search_url
            try:
                if url != 'https://arquivo.pt/robots.txt':
                    query = parse_qs(parts.query, keep_blank_values=True, strict_parsing=True, max_num_fields=3)
                    expected = search_url(query.get('q', [None])[0], source)
                    if url != expected or source.get('_arquivo_request_url') not in (None, url):
                        raise ValueError('URL differs from the exact metadata search operation')
            except (ValueError, TypeError, IndexError) as exc:
                raise _ArchiveEndpointScopeError('Arquivo permits only its exact metadata search or robots.txt') from exc
        if source.get('_documented_api_scope'):
            from .api_permissions import documented_search_api
            if not documented_search_api(url, source):
                raise _APIEndpointScopeError('Request is outside the reviewed JSON search API operation')
        from .article_paths import ARTICLE_PATH_SCOPE_FLAG, article_path_allowed
        if source.get(ARTICLE_PATH_SCOPE_FLAG) and not article_path_allowed(url, source):
            raise _ArticlePathScopeError('Article URL path is outside the configured source scope')
        if host in {"localhost", "metadata.google.internal"} or host.endswith((".local", ".internal", ".localhost")):
            raise CollectionError("Private hostnames are forbidden")
        normalized = urlunsplit(("https", host, parts.path or "/", parts.query, ""))
        exact_article = source.get('_public_article_exact_url')
        if exact_article is not None and normalized != exact_article:
            raise _ArticlePathScopeError('Public article request left the exact observed URL')
        if not resolve:
            return normalized, host, []
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
            if 500 <= response.status <= 599:
                # A diagnostic prefix is never fed to article/discovery parsing.
                # Keep the original response deadline and both byte ceilings;
                # no second request or extra connection is made for diagnostics.
                limit = min(_ERROR_PREFIX_LIMIT, max_bytes, max_wire)
                # Error inspection is optional: at most one second of the
                # original request budget, including low-level socket receives.
                diagnostic_seconds = min(1., max(0., deadline - time.monotonic()))
                deadline = min(deadline, time.monotonic() + diagnostic_seconds)
                reader = getattr(getattr(response, 'fp', None), 'raw', None)
                if isinstance(reader, _DeadlineReader):
                    reader.deadline = min(reader.deadline, deadline)
                chunks, captured, eof, read_error = [], 0, False, None
                try:
                    while captured < limit:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError('Diagnostic response deadline exceeded')
                        if connection.sock is not None:
                            connection.sock.settimeout(remaining)
                        chunk = response.read1(limit - captured)
                        if not chunk:
                            eof = True
                            break
                        chunks.append(chunk)
                        captured += len(chunk)
                except (OSError, http.client.HTTPException, CollectionError) as exc:
                    # Receiving 503 headers already establishes its unchanged
                    # backoff policy. An optional prefix read cannot turn that
                    # response into a client failure or erase its restriction.
                    read_error = type(exc).__name__
                prefix = b''.join(chunks)[:limit]
                declared = response_headers.get('content-length', '').strip()
                known_length = int(declared) if re.fullmatch(r'[0-9]{1,20}', declared) else None
                complete = (read_error is None and
                            (len(prefix) == known_length if known_length is not None
                             else eof or response.isclosed() is True))
                metric.update(wire_bytes=len(prefix), decoded_bytes=(len(prefix) if
                    response_headers.get('content-encoding', 'identity').strip().lower() in ('', 'identity') else 0))
                metric['error_response_capture'] = {
                    'scope': 'http_error_prefix', 'limit_bytes': limit, 'captured_bytes': len(prefix),
                    'read_budget_seconds': diagnostic_seconds,
                    'sha256': hashlib.sha256(prefix).hexdigest(), 'complete': complete,
                    'truncated': not complete, 'read_error': read_error,
                    'content_encoding': response_headers.get('content-encoding', 'identity'),
                    'classification': classify_http_error_prefix(response_headers, prefix),
                    'truncation_meaning': 'Response not confirmed complete; no byte beyond the prefix cap is probed.'}
                return response.status, response_headers, prefix
            # No body is consumed for redirects or explicit collection restrictions.
            if response.status in (301, 302, 303, 307, 308, 304, 403, 429, 451):
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

    @staticmethod
    def _retry_deadline(row, interval=0):
        """Read the effective host deadline without losing its policy class."""
        legacy = row['legacy_retry_until']
        if row['retry_state_version'] == 0:
            legacy = max(legacy, row['next_at'])
        options = [
            (max(row['courtesy_until'], row['last_at'] + max(0., interval)), 0,
             'courtesy', 'Per-host collection interval'),
            (row['client_retry_until'], 1, 'client_network', 'Client network retry'),
            (legacy, 2, 'legacy_unclassified', row['legacy_reason'] or row['reason']),
            (row['server_retry_until'], 3, 'server_backoff', row['server_reason'] or 'Scheduled HTTP retry'),
            (row['blocked_until'], 4, 'operator_access', row['access_reason'] or row['reason']),
        ]
        due, _, retry_class, reason = max(options)
        return due, retry_class, reason

    def _remember_retry(self, row, interval=0, *, materialized=True):
        due, retry_class, reason = self._retry_deadline(row, interval)
        if materialized and row['next_at'] > due:
            # State written by an older process or external operator remains a
            # restriction until its provenance can be reviewed explicitly.
            due, retry_class, reason = row['next_at'], 'legacy_unclassified', row['reason']
        self._local.retry_class = retry_class if due > self._clock() else 'none'
        self._local.retry_generation = (row['client_generation'], row['server_generation'])
        return due, retry_class, reason

    def _preserve_legacy_retry(self, db, row):
        known_due = self._retry_deadline(row)[0]
        if row['retry_state_version'] == 0 or row['next_at'] > known_due:
            db.execute('UPDATE hosts SET legacy_retry_until=MAX(legacy_retry_until,next_at),'
                       'legacy_reason=reason,retry_state_version=1 WHERE host=?', (row['host'],))
            return db.execute('SELECT * FROM hosts WHERE host=?', (row['host'],)).fetchone()
        return row

    def _publish_retry(self, db, host):
        row = db.execute('SELECT * FROM hosts WHERE host=?', (host,)).fetchone()
        due, retry_class, reason = self._remember_retry(row, materialized=False)
        db.execute('UPDATE hosts SET next_at=?,retry_class=?,reason=?,failures=? WHERE host=?',
                   (due, self._local.retry_class, reason,
                    max(row['client_failures'], row['server_failures']), host))
        return due

    def _host_readiness(self, host: str, interval: float) -> tuple[str | None, float | None, str]:
        """Avoid DNS for known waits without reserving a future transport."""
        now = self._clock()
        with self._db() as db:
            row = db.execute('SELECT * FROM hosts WHERE host=?', (host,)).fetchone()
        self._local.retry_generation = (0, 0)
        self._local.retry_class = 'none'
        if row is not None:
            due, _, reason = self._remember_retry(row, interval)
            if row['blocked_until'] > now:
                self._local.retry_class = 'operator_access'
                return 'blocked', max(due, row['blocked_until']), row['access_reason'] or row['reason']
            if due > now:
                return 'deferred', due, reason
        return None, None, ''

    def _reserve(self, host: str, interval: float, *, api_operation=None) -> tuple[str | None, float | None, str]:
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            self._local.api_restriction = None
            if api_operation is not None:
                self._local.api_restriction = self._api_policy_from_db(db, api_operation)
                if self._local.api_restriction:
                    return 'blocked', None, self._local.api_restriction['error']
            now = self._clock()
            db.execute('INSERT OR IGNORE INTO hosts(host,retry_state_version) VALUES (?,1)', (host,))
            row = db.execute('SELECT * FROM hosts WHERE host=?', (host,)).fetchone()
            row = self._preserve_legacy_retry(db, row)
            due, _, reason = self._remember_retry(row, interval)
            if row['blocked_until'] > now:
                self._local.retry_class = 'operator_access'
                return 'blocked', max(due, row['blocked_until']), row['access_reason'] or row['reason']
            if due > now:
                return 'deferred', due, reason
            db.execute('UPDATE hosts SET courtesy_until=?,last_at=? WHERE host=?',
                       (now + max(0., interval), now, host))
            self._publish_retry(db, host)
        return None, None, ''

    def _retry_after_delay(self, headers, now):
        value = (headers or {}).get('retry-after')
        if not value:
            return 0.
        value = str(value).strip()
        if re.fullmatch(r'[0-9]+', value):
            return min(float(value), max(0., _MAX_RETRY_TIMESTAMP - now))
        try:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return max(0., parsed.timestamp() - now)
        except (TypeError, ValueError, OverflowError):
            return 0.

    def _backoff(self, host: str, headers: dict | None = None, blocked: bool = False,
                 reason: str = 'HTTP backoff', *, kind='server_backoff', generation=None) -> float:
        """Coalesce admitted failures; client recovery cannot clear server policy."""
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            now = self._clock()
            db.execute('INSERT OR IGNORE INTO hosts(host,retry_state_version) VALUES (?,1)', (host,))
            row = db.execute('SELECT * FROM hosts WHERE host=?', (host,)).fetchone()
            row = self._preserve_legacy_retry(db, row)
            if kind == 'client_network' and not blocked:
                # Every request admitted before this episode has the old token.
                # Once success invalidates that token, late timeouts cannot
                # reopen the client pause or multiply its exponential counter.
                if generation is None or generation[0] == row['client_generation']:
                    failures = min(row['client_failures'] + 1, 12)
                    base = max(1., min(float(self.config.get('client_backoff_seconds', 5)), 120.))
                    ceiling = max(base, min(float(self.config.get('client_backoff_max_seconds', 60)), 120.))
                    retry = min(_MAX_RETRY_TIMESTAMP, now + min(ceiling, base * 2 ** (failures - 1)))
                    db.execute('UPDATE hosts SET client_retry_until=MAX(client_retry_until,?),'
                               'client_failures=?,client_generation=client_generation+1 WHERE host=?',
                               (retry, failures, host))
            else:
                fresh = generation is None or generation[1] == row['server_generation']
                failures = min(row['server_failures'] + int(fresh), 12)
                delay = (float(self.config.get('circuit_seconds', 86400)) if blocked
                         else min(3600., 30 * 2 ** (max(1, failures) - 1)))
                delay = max(delay, self._retry_after_delay(headers, now))
                retry = min(_MAX_RETRY_TIMESTAMP, now + max(1., delay))
                db.execute('UPDATE hosts SET server_retry_until=MAX(server_retry_until,?),'
                           'server_failures=?,server_generation=server_generation+?,server_reason=? WHERE host=?',
                           (retry, failures, int(fresh), reason, host))
                if blocked:
                    db.execute('UPDATE hosts SET blocked_until=MAX(blocked_until,?),access_reason=? WHERE host=?',
                               (retry, reason, host))
            due = self._publish_retry(db, host)
        return max(now, due)

    def _content_success(self, host):
        """A decoded HTTP response without a challenge recovers client transport.

        Adapter schema validation remains a separate step; this proves HTTP
        connectivity, not that a search envelope contains usable results.
        """
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM hosts WHERE host=?', (host,)).fetchone()
            if not row:
                return
            row = self._preserve_legacy_retry(db, row)
            # Server deadlines and access circuits remain immutable here. A
            # counter can reset after its delay has already elapsed, never
            # because an older in-flight HTTP response happened to succeed.
            reset_server = row['server_retry_until'] <= self._clock() and row['blocked_until'] <= self._clock()
            db.execute('UPDATE hosts SET client_retry_until=0,client_failures=0,'
                       'client_generation=client_generation+1,server_failures=CASE WHEN ? THEN 0 ELSE server_failures END '
                       'WHERE host=?', (reset_server, host))
            self._publish_retry(db, host)

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

    def _record_http_error_prefix(self, url, source, status, headers, data, request_kind):
        """Store bounded private diagnostics, outside HTTP cache and evidence.

        Only transport metadata and immutable binary prefix references leave
        this method. Diagnostic text is never returned as a fetch result body.
        Recording failure must not undo the already persisted retry policy.
        """
        limit = max(1, min(_ERROR_PREFIX_LIMIT, int(source.get('max_bytes', 2_000_000)),
                           int(source.get('max_wire_bytes', source.get('max_bytes', 2_000_000)))))
        prefix = data[:limit]
        metric = getattr(self._local, 'transport_metric', {}) or {}
        captured = metric.get('error_response_capture')
        info = dict(captured) if isinstance(captured, dict) else {
            'scope': 'http_error_prefix', 'limit_bytes': limit,
            'complete': False, 'truncated': True, 'read_error': None,
            'content_encoding': headers.get('content-encoding', 'identity'),
            'truncation_meaning': 'Custom transport returned bytes; complete response framing is not certified.'}
        info.update(version='http-error-prefix-1', sha256=hashlib.sha256(prefix).hexdigest(),
                    captured_bytes=len(prefix), classification=classify_http_error_prefix(headers, prefix),
                    documentary_evidence=False, body_returned_to_ui=False, persisted=False)
        allowed = ('server', 'content-type', 'retry-after', 'date', 'via',
                   'x-envoy-upstream-service-time', 'x-envoy-overloaded')
        info['response_headers'] = {key: str(headers[key])[:512] for key in allowed if key in headers}
        info['response_headers_truncated'] = [key for key in allowed if key in headers and len(str(headers[key])) > 512]
        relative = Path('http_error_diagnostics') / info['sha256'][:2] / (info['sha256'] + '.prefix.bin')
        path = self.db_path.parent / relative
        try:
            private_root = self.db_path.parent / 'http_error_diagnostics'
            private_root.mkdir(exist_ok=True, mode=0o700)
            if private_root.is_symlink() or path.parent.is_symlink():
                raise OSError('Diagnostic directories must not be symlinks')
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if (private_root.is_symlink() or not path.parent.resolve().is_relative_to(private_root.resolve())
                    or path.is_symlink()):
                raise OSError('Diagnostic path must not be a symlink')
            if not path.exists():
                with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
                    temporary = Path(output.name)
                    output.write(prefix)
                    output.flush()
                    os.fsync(output.fileno())
                try:
                    os.link(temporary, path)
                except FileExistsError:
                    pass
                finally:
                    temporary.unlink(missing_ok=True)
            with path.open('rb') as saved:
                if saved.read(_ERROR_PREFIX_LIMIT + 1) != prefix:
                    raise OSError('Diagnostic prefix hash mismatch')
            info.update(prefix_path=str(relative), persisted=True)
            serialized = json.dumps(info, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
            with self._db() as db:
                cursor = db.execute('INSERT INTO http_error_diagnostics('
                    'occurred_at,url,source_id,request_kind,http_status,prefix_sha256,prefix_bytes,prefix_path,receipt) '
                    'VALUES(?,?,?,?,?,?,?,?,?)', (_iso(self._clock()), url, source.get('id'), request_kind,
                        status, info['sha256'], len(prefix), str(relative), serialized))
                info['receipt_id'] = cursor.lastrowid
        except (OSError, sqlite3.Error):
            info.update(persisted=False, storage_error='diagnostic_receipt_not_persisted')
        return info

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
                # Syntax and source scope are checked before reading state.
                # Resolve DNS only if this host may actually be requested.
                normalized, host, _ = self._validate_url(current, source, resolve=False)
                api_operation = None
                if request_kind != 'robots':
                    policy = self._api_policy_error(normalized, source)
                    if policy:
                        return policy, normalized, None, {}, b''
                    api_operation = self._api_operation(normalized, source)
                if normalized in seen:
                    raise CollectionError("Redirect loop")
                seen.add(normalized)
                with self._db() as db:
                    redirect = db.execute("SELECT target FROM redirects WHERE url=? AND expires_at>?", (normalized, self._clock())).fetchone()
                if redirect:
                    if source.get('kind') == 'arquivo_fulltext':
                        raise _ArchiveEndpointScopeError('Arquivo metadata searches cannot follow redirects')
                    # Preserve validation of both ends of a cached redirect.
                    self._validate_url(normalized, source)
                    target = redirect["target"]
                    self._validate_url(target, source, resolve=False)
                    policy = self._api_policy_error(target, source) if request_kind != 'robots' else None
                    if policy:
                        return policy, target, None, {}, b''
                    self._validate_url(target, source)
                    if check_redirect_robots and source.get("robots_required", True):
                        denied_redirect = self._robots_allowed(target, source)
                        if denied_redirect:
                            return denied_redirect, target, None, {}, b""
                    request_headers.pop("If-None-Match", None)
                    request_headers.pop("If-Modified-Since", None)
                    current = target
                    continue
                denied, retry, reason = self._host_readiness(host, float(source.get("min_interval_seconds", 2)))
                if denied:
                    return self._result(url, denied, error=reason, retry_at=_iso(retry),
                                        error_code="host_interval" if denied == "deferred" else "host_circuit",
                                        admission_host=host, retry_class=self._local.retry_class,
                                        request_attempted=False), normalized, None, {}, b""
                attempt_generation = self._local.retry_generation
                # DNS can be slow or concurrent work can change host policy.
                # Pin the answer, then reserve atomically immediately before
                # transport so DNS latency cannot consume the courtesy pause.
                normalized, host, addresses = self._validate_url(normalized, source)
                self._local.max_wire_bytes = max(1, min(int(source.get("max_wire_bytes", max_bytes)), max_bytes))
                request_timeout = timeout
                if session_deadline is not None:
                    remaining = session_deadline - time.monotonic()
                    if remaining <= 0:
                        return exhausted()
                    request_timeout = min(timeout, remaining)
                reserve_args = {'api_operation': api_operation} if api_operation is not None else {}
                denied, transport_retry, reason = self._reserve(
                    host, float(source.get("min_interval_seconds", 2)), **reserve_args)
                if denied:
                    if api_operation is not None and self._local.api_restriction:
                        return self._local.api_restriction, normalized, None, {}, b''
                    return self._result(url, denied, error=reason, retry_at=_iso(transport_retry),
                                        error_code="host_interval" if denied == "deferred" else "host_circuit",
                                        admission_host=host, retry_class=self._local.retry_class,
                                        request_attempted=False), normalized, None, {}, b""
                attempt_generation = self._local.retry_generation
                status, response_headers, data = self._network_request(normalized, request_headers, request_timeout, max_bytes, addresses, request_kind)
                response_headers = {str(key).lower(): str(value) for key, value in response_headers.items()}
                if len(data) > max_bytes:
                    raise CollectionError("Response exceeds max_bytes")
                if status in (301, 302, 303, 307, 308):
                    if source.get('kind') == 'arquivo_fulltext':
                        raise _ArchiveEndpointScopeError('Arquivo metadata searches cannot follow redirects')
                    location = response_headers.get("location")
                    if not location:
                        raise CollectionError("Redirect without Location")
                    target = urljoin(normalized, location)
                    self._validate_url(target, source, resolve=False)
                    policy = self._api_policy_error(target, source) if request_kind != 'robots' else None
                    if policy:
                        return policy, target, None, {}, b''
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
                    retry = self._backoff(host, response_headers, True, "HTTP access restriction", generation=attempt_generation)
                    return self._result(url, "blocked", http_status=status, error="HTTP access restriction; manual review required", retry_at=_iso(retry), retry_class=self._local.retry_class, admission_host=host), normalized, status, response_headers, data
                if status in (429, 503):
                    retry = self._backoff(host, response_headers, generation=attempt_generation)
                    diagnostic = (self._record_http_error_prefix(normalized, source, status,
                                  response_headers, data, request_kind) if status == 503 else None)
                    return self._result(url, "deferred", http_status=status, error="Retry scheduled after HTTP " + str(status), retry_at=_iso(retry), error_code="server_backoff", retry_class=self._local.retry_class, admission_host=host, request_attempted=True,
                                        error_diagnostic=diagnostic), normalized, status, response_headers, b""
                if status >= 500:
                    retry = self._backoff(host, response_headers, generation=attempt_generation)
                    diagnostic = self._record_http_error_prefix(normalized, source, status, response_headers, data, request_kind)
                    return self._result(url, "deferred", http_status=status, error="Transient server failure", retry_at=_iso(retry), error_code="server_failure", retry_class=self._local.retry_class, admission_host=host, request_attempted=True,
                                        error_diagnostic=diagnostic), normalized, status, response_headers, b""
                return None, normalized, status, response_headers, data
            raise CollectionError("Redirect limit exceeded")
        except (OSError, http.client.HTTPException) as exc:
            if session_deadline is not None and time.monotonic() >= session_deadline:
                # Exhausting the operator's session allowance is not a server
                # failure and must not create a new host penalty/backoff.
                return exhausted()
            if "host" in locals():
                retry = self._backoff(host, reason="Transient network failure", kind="client_network", generation=locals().get("attempt_generation"))
                return self._result(url, "deferred", error="Transient network failure: " + type(exc).__name__, retry_at=_iso(retry), error_code="network_failure", retry_class=self._local.retry_class, admission_host=host, request_attempted=True), current, None, {}, b""
            return self._result(url, "error", error=type(exc).__name__), current, None, {}, b""
        except _ArchiveEndpointScopeError as exc:
            return self._result(url, 'blocked', error=str(exc), error_code='archive_discovery_scope'), current, None, {}, b''
        except _APIEndpointScopeError as exc:
            return self._result(url, 'blocked', error=str(exc), error_code='documented_api_scope'), current, None, {}, b''
        except _ArticlePathScopeError as exc:
            return self._result(url, 'blocked', error=str(exc), error_code='article_path_scope'), current, None, {}, b''
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
        if not robots_allowed(body, self.user_agent, url):
            from .api_permissions import documented_search_api
            if not documented_search_api(url, source):
                return self._result(url, "robots_denied", error="robots.txt disallows this URL")
            source['min_interval_seconds'] = max(1., float(source.get('min_interval_seconds', 2)))
            self._record_event(url, 'documented_api_permission', network_requests=0,
                               outcome='reviewed_json_api_operation')
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
        from .article_paths import ARTICLE_PATH_SCOPE_FLAG
        robot_source.pop(ARTICLE_PATH_SCOPE_FLAG, None)  # robots has its own bounded policy, not article paths
        robot_source.pop('_public_article_exact_url', None)
        robot_source.pop('_documented_api_scope', None)  # robots is not a JSON search request
        failure, _, status, headers, raw = self._request(origin + "/robots.txt", robot_source, request_kind="robots")
        if failure:
            if failure["status"] in ("blocked", "deferred"):
                return self._result(url, failure["status"], http_status=failure["http_status"], error="robots.txt: " + str(failure["error"]), retry_at=failure["retry_at"], error_code=failure.get("error_code"), admission_host=failure.get("admission_host"), retry_class=failure.get("retry_class"), error_diagnostic=failure.get('error_diagnostic')), ""
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
        return any(marker in lower for marker in ("<title>just a moment", "<title>access denied", "cf-chl-", "id=\"captcha\"", "id='captcha'", "class=\"g-recaptcha\"", "class='g-recaptcha'", "verify you are human", "unusual traffic from your computer", "id=\"anomaly-modal\"", "id='anomaly-modal'", "class=\"anomaly-modal", "class='anomaly-modal", "action=\"/anomaly.js", "action='/anomaly.js"))

    @staticmethod
    def _api_json(body):
        def unique_object(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError('Duplicate JSON key')
                result[key] = value
            return result

        def reject_constant(_value):
            raise ValueError('Invalid JSON constant')
        return json.loads(body, object_pairs_hook=unique_object, parse_constant=reject_constant)

    @staticmethod
    def _api_engine(value):
        if (not isinstance(value, str) or not value.strip() or len(value) > 128
                or ',' in value or any(ord(char) < 32 or ord(char) > 126 for char in value)):
            raise CollectionError('Ambiguous API engine identifier')
        return ' '.join(value.casefold().split())

    def _api_operation(self, url, source):
        """Canonical GET endpoint and explicit set, independent of profile/query."""
        def selected(values):
            if 'categories' in values or len(values.get('engines', [])) != 1:
                raise CollectionError('Documented API requests require explicit engines without categories')
            engines = tuple(sorted({self._api_engine(value) for value in values['engines'][0].split(',')}))
            if not engines or len(engines) > 32:
                raise CollectionError('Ambiguous API engine selection')
            return engines
        def canonical_endpoint(parts):
            host = self._hostname(parts.hostname)
            if ':' in host:
                host = '[' + host + ']'
            return 'https://' + host + parts.path
        parts = urlsplit(url)
        if source.get('documented_api_permission') is None:
            # Existing negative policy is endpoint-scoped, not a grant tied to
            # a source ID/kind. Dropping a grant cannot remove a known denial.
            # An unobserved generic /search remains governed by normal policy.
            if parts.path != '/search':
                return None
            try:
                engines = selected(parse_qs(parts.query, keep_blank_values=True, strict_parsing=True,
                                            max_num_fields=12))
            except (CollectionError, ValueError):
                engines = None
            return {'endpoint': canonical_endpoint(parts), 'engines': engines,
                    'selection': json.dumps(engines, separators=(',', ':')), 'url': url}
        from .api_permissions import documented_search_api
        if not documented_search_api(url, source):
            raise CollectionError('URL is outside the documented JSON API operation')
        params = parse_qs(parts.query, keep_blank_values=True, max_num_fields=12)
        configured = parse_qs(urlsplit(source.get('search_url') or source.get('api_url') or source.get('url')).query,
                              keep_blank_values=True, max_num_fields=12)
        engines = selected(params)
        if engines != selected(configured):
            raise CollectionError('Requested API engines differ from the configured engine set')
        expected_url = source.get('_documented_api_request_url')
        if expected_url:
            expected = parse_qs(urlsplit(expected_url).query, keep_blank_values=True, max_num_fields=12)
            current = dict(params, engines=list(engines))
            expected['engines'] = list(selected(expected))
            if current != expected:
                raise CollectionError('Redirect changed the requested API query or parameters')
        endpoint = urlsplit(source['documented_api_permission']['endpoint'])
        return {'endpoint': canonical_endpoint(endpoint), 'engines': engines,
                'selection': json.dumps(engines, separators=(',', ':')), 'url': url}

    def _api_policy_from_db(self, db, operation):
        endpoint, selection = operation['endpoint'], operation['selection']
        if operation['engines'] is None:
            known = db.execute('SELECT 1 FROM api_route_restrictions WHERE endpoint=? UNION ALL '
                               'SELECT 1 FROM api_backend_restrictions WHERE endpoint=? LIMIT 1',
                               (endpoint, endpoint)).fetchone()
            if known:
                return self._result(operation['url'], 'blocked',
                    error='Known API backend restrictions require an explicit unambiguous engine set',
                    error_code='documented_api_backend_scope', backend_policy='pending_review',
                    api_endpoint=endpoint)
            return None
        route = db.execute('SELECT reason,evidence_sha256 FROM api_route_restrictions '
                           'WHERE endpoint=? AND requested_engines=?', (endpoint, selection)).fetchone()
        if route:
            return self._result(operation['url'], 'blocked',
                error='Documented API engine provenance requires manual review for this engine set',
                error_code='discovery_backend_provenance_mismatch', backend_policy='pending_review',
                api_endpoint=endpoint, requested_engines=list(operation['engines']),
                policy_evidence_sha256=route['evidence_sha256'])
        marks = ','.join('?' for _ in operation['engines'])
        rows = db.execute('SELECT engine,evidence_sha256 FROM api_backend_restrictions '
                          'WHERE endpoint=? AND engine IN (' + marks + ')',
                          (endpoint, *operation['engines'])).fetchall()
        if rows:
            return self._result(operation['url'], 'blocked',
                error='Documented API backend requires manual review: ' + ', '.join(sorted(row['engine'] for row in rows)),
                error_code='discovery_backend_restricted', backend_policy='pending_review',
                api_endpoint=endpoint, requested_engines=list(operation['engines']),
                restricted_engines=sorted(row['engine'] for row in rows))
        return None

    def _api_policy_error(self, url, source):
        try:
            operation = self._api_operation(url, source)
        except (CollectionError, ValueError, TypeError):
            return self._result(url, 'blocked', error='Documented API engine selection or query is outside its explicit scope',
                                error_code='documented_api_backend_scope')
        if operation is None:
            return None
        with self._db() as db:
            return self._api_policy_from_db(db, operation)

    @classmethod
    def _api_backend_denial(cls, message):
        return bool(cls._captcha(message) or re.search(
            r'\b(?:401|403|407|429|451)\b|suspend(?:ed|sion)?|'
            r'access[ _-]*denied|too[ _-]*many[ _-]*requests|rate[ _-]*limit|ratelimit|'
            r'captcha|challenge|verif(?:y|ication)[^\n]{0,60}human|human[ _-]*verification|'
            r'unusual[ _-]*traffic|forbidden|\bdenied\b|\bblocked\b|\bblocking\b|\bbanned\b|'
            r'cf-chl-|anomaly-modal|/anomaly\.js|just[ _-]*a[ _-]*moment|'
            r'are[ _-]*you[ _-]*(?:a[ _-]*)?(?:robot|human)|robot[ _-]*check|'
            r'bot[ _-]*(?:detection|detected|protection)|automated[ _-]*(?:queries|requests)', message, re.I))

    def _api_response_policy(self, body, raw, source, url, final_url, mime, content_type):
        """Record facts before enforcing policy; never grant an alternate engine.

        SearXNG reports result provenance as engine, engines (a nonempty list),
        or both consistently. Backend diagnostics are two-string lists. Only
        access restrictions create pending-review rows; a plain Timeout/OSError
        remains an observed transient error. No expiry is inferred from text.
        """
        if source.get('documented_api_permission') is None or mime != 'application/json':
            return {}, None
        try:
            self._api_operation(url, source)
            operation = self._api_operation(final_url, dict(source, _documented_api_request_url=url))
        except (CollectionError, ValueError, TypeError):
            return {}, self._result(url, 'blocked', error='API response left the exact requested query operation',
                                    error_code='documented_api_backend_scope')
        try:
            payload = self._api_json(body)
        except (ValueError, RecursionError):
            return {}, None  # Original CAPTCHA and adapter validation still apply.
        if not isinstance(payload, dict):
            return {}, None
        results = payload.get('results')
        malformed = not isinstance(results, list)
        if malformed:
            results = []
        reported, diagnostics = set(), []
        errors = payload.get('unresponsive_engines')
        if not isinstance(errors, list):
            errors, malformed = [], True
        for item in errors:
            if not isinstance(item, list) or len(item) != 2 or not all(isinstance(value, str) for value in item):
                malformed = True
                continue
            try:
                engine = self._api_engine(item[0])
            except CollectionError:
                malformed = True
                continue
            diagnostics.append((engine, item[1]))
            reported.add(engine)
        for record in results:
            try:
                if not isinstance(record, dict) or not ({'engine', 'engines'} & set(record)):
                    raise CollectionError('Missing result engine provenance')
                primary = self._api_engine(record['engine']) if 'engine' in record else None
                engines = record.get('engines')
                if 'engines' in record and (not isinstance(engines, list) or not engines):
                    raise CollectionError('Invalid result engines')
                names = {self._api_engine(value) for value in engines} if engines is not None else {primary}
                if primary is not None and primary not in names:
                    raise CollectionError('Inconsistent result engine provenance')
                reported.update(names)
            except (CollectionError, TypeError):
                malformed = True
        mismatch = malformed or bool(reported - set(operation['engines']))
        metadata = {'backend_provenance': 'invalid' if mismatch else 'reported' if reported else 'unobserved',
                    'reported_engines': sorted(reported), 'backend_diagnostics_count': len(diagnostics)}
        if diagnostics or mismatch:
            sha = hashlib.sha256(raw).hexdigest()
            stamp = self._clock()
            with self._db() as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute('INSERT OR IGNORE INTO api_backend_observations VALUES(?,?,?,?,?,?,?,?)',
                    (operation['endpoint'], operation['selection'], sha, stamp, content_type, raw,
                     json.dumps(sorted(reported)), json.dumps(diagnostics, ensure_ascii=False)))
                for engine, message in diagnostics:
                    if self._api_backend_denial(message):
                        db.execute('''INSERT INTO api_backend_restrictions VALUES(?,?,?,?,?,1,?)
                            ON CONFLICT(endpoint,engine) DO UPDATE SET last_seen=MAX(last_seen,excluded.last_seen)''',
                            (operation['endpoint'], engine, message[:2000], stamp, stamp, sha))
                if mismatch:
                    db.execute('''INSERT INTO api_route_restrictions VALUES(?,?,?,?,?,?)
                        ON CONFLICT(endpoint,requested_engines) DO UPDATE SET last_seen=MAX(last_seen,excluded.last_seen)''',
                        (operation['endpoint'], operation['selection'], 'Reported API engines differ from explicit request or have invalid provenance',
                         stamp, stamp, sha))
                failure = self._api_policy_from_db(db, operation)
            return metadata, failure
        with self._db() as db:
            return metadata, self._api_policy_from_db(db, operation)

    def _response_captcha(self, body: str, source: dict, url: str, final_url: str, mime: str) -> bool:
        if not self._captcha(body):
            return False
        from .api_permissions import documented_search_api
        if (mime != 'application/json' or not documented_search_api(url, source)
                or not documented_search_api(final_url, source)):
            return True

        try:
            payload = self._api_json(body)
            if (not isinstance(payload, dict) or not isinstance(payload.get('results'), list)
                    or not isinstance(payload.get('unresponsive_engines'), list)
                    or any(not isinstance(item, list) or len(item) != 2
                           or any(not isinstance(value, str) for value in item)
                           for item in payload['unresponsive_engines'])):
                return True
            # Backend diagnostics are not a challenge issued by the API host.
            # Inspect every other field, including global errors and results;
            # keep the original body/bytes for the adapter, cache and receipt.
            host_response = {key: value for key, value in payload.items() if key != 'unresponsive_engines'}
            return self._captcha(json.dumps(host_response, ensure_ascii=False))
        except (ValueError, RecursionError):
            return True

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

    def cached_response_url(self, url: str) -> str | None:
        with self._db() as db:
            row = db.execute('SELECT final_url FROM http_cache WHERE url=?', (url,)).fetchone()
        return (row['final_url'] or None) if row else None

    def record_access_challenge(self, url: str, source: dict, fetch_result: dict) -> dict:
        """Persist an adapter-recognized restriction without requesting a URL.

        Adapters may understand challenge markup beyond the transport detector.
        Restrict the host of the response they actually parsed, never a form
        action or other URL found inside its body. Validate the requested URL
        and response URL against the same current scope before any state write.
        Cached/local responses can add a restriction but cannot authorize HTTP.
        """
        if (not source.get('enabled') or not source.get('terms_approved')
                or not isinstance(fetch_result, dict)
                or fetch_result.get('status') not in ('ok', 'not_modified')
                or not isinstance(fetch_result.get('body'), str)):
            return self._result(url, 'blocked', error='Invalid access challenge report',
                                error_code='invalid_challenge_report', challenge_recorded=False)
        scoped_source = dict(source)
        if scoped_source.get('documented_api_permission') is not None:
            scoped_source['_documented_api_scope'] = True
        try:
            requested, _, _ = self._validate_url(url, scoped_source, resolve=False)
            reported, _, _ = self._validate_url(fetch_result.get('url') or url, scoped_source, resolve=False)
            if requested != reported:
                raise CollectionError('Challenge response does not belong to the requested URL')
            response_url, host, _ = self._validate_url(
                fetch_result.get('final_url') or reported, scoped_source, resolve=False)
        except (CollectionError, ValueError, TypeError) as exc:
            return self._result(url, 'blocked', error=str(exc),
                                error_code='challenge_report_scope', challenge_recorded=False)
        retry = self._backoff(host, blocked=True, reason='Adapter recognized access challenge')
        self._record_event(response_url, 'source_validation', network_requests=0,
                           http_status=fetch_result.get('http_status'),
                           outcome='adapter_access_challenge', cache_status=fetch_result.get('cache_status', 'none'))
        return self._result(requested, 'blocked', error='Access challenge; manual review required',
                            error_code='access_challenge', retry_at=_iso(retry),
                            retry_class='operator_access', admission_host=host,
                            challenge_recorded=True)

    def expire_cached_response(self, url: str, expected_sha256=None, reason='discovery_parse_invalid') -> bool:
        """Expire one semantically invalid envelope while retaining its receipt.

        This is not a retry permission. Robots, courtesy, server delays and
        access circuits remain mandatory. A compare-hash prevents an older
        parser result from invalidating a newer representation.
        """
        if not isinstance(reason, str) or not re.fullmatch(r'[a-z0-9_]{1,64}', reason):
            raise ValueError('Cache validation reason must be a bounded code')
        if expected_sha256 is not None and (not isinstance(expected_sha256, str)
                or not re.fullmatch(r'[0-9a-f]{64}', expected_sha256)):
            raise ValueError('Expected response SHA256 must be a hexadecimal digest')
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT sha256,expires_at,validation_error FROM http_cache WHERE url=?', (url,)).fetchone()
            if (not row or (expected_sha256 is not None and row['sha256'] != expected_sha256)
                    or (row['expires_at'] <= self._clock() and row['validation_error'] == reason)):
                return False
            db.execute('UPDATE http_cache SET expires_at=MIN(expires_at,?),validation_error=? WHERE url=?',
                       (self._clock(), reason, url))
        self._record_event(url, 'cache_validation', network_requests=0,
                           outcome=reason, cache_status='invalidated')
        return True

    def admission_uncached_urls(self, urls) -> set[str]:
        """Scheduling hint only: never postpone a URL with any saved response.

        Stale representations and cached redirects are excluded too. Reuse,
        revalidation and policy decisions remain the normal fetcher's job.
        No host is reserved, no permission is inferred, and no HTTP occurs.
        Call outside the jobs write transaction to keep lock ordering simple.
        """
        values = list(dict.fromkeys(urls))
        uncached = set(values)
        with self._db() as db:
            for offset in range(0, len(values), 250):
                chunk = values[offset:offset + 250]
                placeholders = ','.join('?' for _ in chunk)
                rows = db.execute('SELECT url FROM http_cache WHERE url IN (' + placeholders + ') '
                                  'UNION SELECT url FROM redirects WHERE url IN (' + placeholders + ')',
                                  chunk + chunk)
                uncached.difference_update(row[0] for row in rows)
        return uncached

    def cached_policy_error(self, url: str, source: dict, final_url: str | None = None) -> dict | None:
        """Apply current scope and known robots restrictions without networking.

        Missing or expired robots data cannot establish a new permission; this
        check only restricts reuse of a representation already held locally.
        Both ends of a previously followed redirect remain subject to policy.
        """
        if not source.get('enabled') or not source.get('terms_approved'):
            return self._result(url, 'blocked', error='Source must be enabled and terms must be reviewed explicitly')
        if source.get('documented_api_permission') is not None:
            source = dict(source, _documented_api_scope=True, _documented_api_request_url=url)
        if source.get('kind') == 'arquivo_fulltext':
            source = dict(source, _arquivo_request_url=url)
        final_url = final_url or self.cached_response_url(url)
        if source.get('kind') == 'arquivo_fulltext' and final_url not in (None, url):
            return self._result(url, 'blocked', error='Cached Arquivo search left its exact requested operation',
                                error_code='archive_discovery_scope')
        for target in dict.fromkeys((url, final_url or url)):
            try:
                normalized, _, _ = self._validate_url(target, source, resolve=False)
            except (CollectionError, ValueError, TypeError) as exc:
                return self._result(url, 'blocked', error='Cached response is outside the current source scope: ' + str(exc))
            policy = self._api_policy_error(normalized, source)
            if policy:
                return policy
            if source.get('robots_required', True):
                origin = 'https://' + urlsplit(normalized).netloc
                with self._db() as db:
                    known = db.execute('SELECT body FROM robots WHERE origin=? AND expires_at>?',
                                       (origin, self._clock())).fetchone()
                if known and not robots_allowed(known['body'], self.user_agent, normalized):
                    from .api_permissions import documented_search_api
                    if not documented_search_api(normalized, source):
                        return self._result(url, 'robots_denied', error='Current cached robots policy disallows this URL')
        return None

    def _fetch(self, url: str, source: dict) -> dict:
        if self._closed:
            return self._result(url, "error", error="Fetcher is closed")
        mode = source.get('_cache_mode', self.config.get('cache_mode', 'reuse'))
        if mode not in ('reuse', 'revalidate', 'local'):
            return self._result(url, 'error', error='cache_mode must be reuse, revalidate or local')
        if self.offline and mode != 'local':
            return self._result(url, "offline", error="Network collection is disabled")
        if not source.get("enabled", False) or not source.get("terms_approved", False):
            return self._result(url, "blocked", error="Source must be enabled and terms must be reviewed explicitly")
        source = dict(source)
        if source.get('documented_api_permission') is not None:
            source['_documented_api_scope'] = True
        try:
            normalized, host, _ = self._validate_url(url, source, resolve=False)
        except _APIEndpointScopeError as exc:
            return self._result(url, 'blocked', error=str(exc), error_code='documented_api_scope')
        except _ArchiveEndpointScopeError as exc:
            return self._result(url, 'blocked', error=str(exc), error_code='archive_discovery_scope')
        except _ArticlePathScopeError as exc:
            return self._result(url, 'blocked', error=str(exc), error_code='article_path_scope')
        except (CollectionError, ValueError, TypeError) as exc:
            return self._result(url, "error", error=str(exc)[:500])
        if source.get('documented_api_permission') is not None:
            source['_documented_api_request_url'] = normalized
        if source.get('kind') == 'arquivo_fulltext':
            source['_arquivo_request_url'] = normalized
        policy = self._api_policy_error(normalized, source)
        if policy:
            return policy
        now = self._clock()
        with self._db() as db:
            cached = db.execute("SELECT * FROM http_cache WHERE url=?", (normalized,)).fetchone()
        # Legacy receipts cannot prove that the requested engines were used.
        # Require a full representation; a 304 cannot establish provenance.
        legacy_api_cache = (cached is not None and source.get('documented_api_permission') is not None
                            and not cached['api_backend_metadata'])
        # Older cache rows have no response URL; revalidate before resolving
        # relative links against a base that may have redirected elsewhere.
        fresh = (cached and cached['expires_at'] > now and cached['final_url']
                 and not cached['validation_error'] and not legacy_api_cache)
        if cached and ((mode == 'reuse' and fresh) or mode == 'local'):
            decision = self.cached_policy_error(normalized, source, cached['final_url'])
            if decision:
                return decision
            if cached['validation_error']:
                return self._result(normalized, 'error', error='Cached response failed source validation',
                                    error_code='invalid_cached_response', sha256=cached['sha256'],
                                    cache_status='invalidated', local_reuse_only=mode == 'local')
            if legacy_api_cache:
                return self._result(normalized, 'blocked', error='Cached API backend provenance needs full revalidation',
                                    error_code='documented_api_backend_scope', local_reuse_only=True)
            cache_status = 'local' if mode == 'local' else 'fresh'
            self._record_event(normalized, "content", network_requests=0, outcome="cache_hit", cache_status=cache_status)
            result = self._result(normalized, "not_modified", body=cached["body"], content_type=cached["content_type"], retrieved_at=cached["retrieved_at"], sha256=cached["sha256"], etag=cached["etag"], last_modified=cached["last_modified"], final_url=cached["final_url"] or normalized, from_cache=True, cache_status=cache_status, cache_valid_until=_iso(cached["expires_at"]), validated_at=_iso(cached['fetched_at']), local_reuse_only=mode == 'local', cache_stale=not bool(fresh))
            if cached['pagination_headers']:
                result['pagination_headers'] = json.loads(cached['pagination_headers'])
            if cached['api_backend_metadata']:
                result.update(json.loads(cached['api_backend_metadata']))
            return result
        if mode == 'local':
            return self._result(normalized, 'cache_miss', error_code='local_cache_miss',
                error='local_cache_miss: No hay una copia local; cambia a reutilizar o revalidar para consultar esta URL.',
                retry_at=_iso(now), local_reuse_only=True)
        if source.get('robots_required', True):
            decision = self._robots_allowed(normalized, source)
            if decision:
                return decision
        headers = {}
        if cached and not cached['validation_error'] and not legacy_api_cache:
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
        pagination_headers = {key: value.strip() for key in ('x-wp-total', 'x-wp-totalpages')
                              if isinstance(value := response_headers.get(key), str)
                              and re.fullmatch(r'[0-9]{1,12}', value.strip())}
        if status == 304 and cached and not pagination_headers and cached['pagination_headers']:
            pagination_headers = json.loads(cached['pagination_headers'])
        ttl, store_cache = self._cache_policy(response_headers, source)
        if status == 304:
            if not cached:
                return self._result(normalized, "error", http_status=304, error="304 without a cached representation")
            if cached['validation_error']:
                return self._result(normalized, 'error', http_status=304,
                                    error='304 cannot validate a semantically invalid representation',
                                    error_code='invalid_cache_revalidation')
            if legacy_api_cache:
                return self._result(normalized, 'blocked', http_status=304,
                                    error='304 cannot establish API backend provenance',
                                    error_code='documented_api_backend_scope')
            policy = self._api_policy_error(final_url, source)
            if policy:
                return policy
            with self._db() as db:
                operation = self._api_operation(final_url, source)
                if operation is not None:
                    db.execute('BEGIN IMMEDIATE')
                    policy = self._api_policy_from_db(db, operation)
                    if policy:
                        return policy
                if store_cache:
                    db.execute("UPDATE http_cache SET fetched_at=?,expires_at=?,final_url=?,pagination_headers=? WHERE url=?", (now, now + ttl, final_url, json.dumps(pagination_headers), normalized))
                else:
                    db.execute("DELETE FROM http_cache WHERE url=?", (normalized,))
            self._content_success(urlsplit(final_url).hostname)
            self._record_event(normalized, "content", network_requests=0, outcome="cache_revalidated", cache_status="revalidated")
            return self._result(normalized, "not_modified", http_status=304, body=cached["body"], content_type=cached["content_type"], retrieved_at=cached["retrieved_at"], sha256=cached["sha256"], etag=cached["etag"], last_modified=cached["last_modified"], final_url=final_url, validated_at=_iso(now), from_cache=True, cache_status="revalidated", cache_valid_until=_iso(now + ttl), original_retrieved_at=cached["retrieved_at"], pagination_headers=pagination_headers, **json.loads(cached['api_backend_metadata'] or '{}'))
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
        if status in (404, 410):
            # A publisher search can return an HTML error page for an absent
            # route. This is not an empty valid feed and retrying the same URL
            # five times only repeats the error. Keep the actual HTTP status;
            # block this resource for manual review without penalizing its host.
            return self._result(normalized, 'blocked', http_status=status,
                                error='HTTP resource not found; review the publisher URL before retrying',
                                error_code='resource_not_found', final_url=final_url,
                                content_type=response_headers.get('content-type', ''),
                                sha256=hashlib.sha256(raw).hexdigest())
        if status != 200:
            # DDG can return a bounded HTML challenge with HTTP 202. A status
            # alone is not proof: require the configured DDG adapter, HTML MIME
            # and an explicit existing challenge marker before opening a
            # manual-review circuit. Never switch endpoints after a challenge.
            if (status == 202 and source.get('kind') in ('duckduckgo_html', 'duckduckgo_lite')
                    and response_headers.get('content-type', '').split(';', 1)[0].strip().lower()
                    in ('text/html', 'application/xhtml+xml')
                    and self._captcha(raw[:100_000].decode('utf-8', errors='replace'))):
                final_host = urlsplit(final_url).hostname
                retry = self._backoff(final_host, response_headers, True, "CAPTCHA or access challenge")
                return self._result(normalized, "blocked", http_status=status,
                                    error="CAPTCHA or access challenge; manual review required",
                                    error_code="access_challenge", retry_at=_iso(retry), final_url=final_url,
                                    retry_class=self._local.retry_class, admission_host=final_host)
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
            return self._result(normalized, "blocked", http_status=status, content_type=content_type,
                                error="Unsupported content type", error_code="unsupported_content_type", final_url=final_url)
        charset = "utf-8"
        for item in content_type.split(";")[1:]:
            if item.strip().lower().startswith("charset="):
                charset = item.split("=", 1)[1].strip(" \"'")
        try:
            body = raw.decode(charset, errors="replace")
        except (LookupError, UnicodeError):
            body = raw.decode("utf-8", errors="replace")
        backend_metadata, backend_failure = self._api_response_policy(
            body, raw, source, normalized, final_url, mime, content_type)
        if self._response_captcha(body, source, normalized, final_url, mime):
            final_host = urlsplit(final_url).hostname
            retry = self._backoff(final_host, response_headers, True, "CAPTCHA or access challenge")
            return self._result(normalized, "blocked", http_status=status, error="CAPTCHA or access challenge; manual review required", retry_at=_iso(retry), error_code="access_challenge", retry_class=self._local.retry_class, admission_host=final_host)
        if backend_failure:
            return dict(backend_failure, http_status=status, body=body, content_type=content_type,
                        sha256=hashlib.sha256(raw).hexdigest(), final_url=final_url, **backend_metadata)
        self._content_success(urlsplit(final_url).hostname)
        digest = hashlib.sha256(raw).hexdigest()
        retrieved = _iso(self._clock())
        etag, modified = response_headers.get("etag"), response_headers.get("last-modified")
        with self._db() as db:
            operation = self._api_operation(final_url, source)
            if operation is not None:
                db.execute('BEGIN IMMEDIATE')
                policy = self._api_policy_from_db(db, operation)
                if policy:
                    return dict(policy, http_status=status, body=body, content_type=content_type,
                                sha256=digest, final_url=final_url, **backend_metadata)
            if store_cache:
                db.execute("INSERT OR REPLACE INTO http_cache(url,body,content_type,etag,last_modified,sha256,retrieved_at,fetched_at,expires_at,cache_control,cache_expires,final_url,pagination_headers,api_backend_metadata) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (normalized, body, content_type, etag, modified, digest, retrieved, now, now + ttl, response_headers.get("cache-control", ""), response_headers.get("expires", ""), final_url, json.dumps(pagination_headers), json.dumps(backend_metadata) if backend_metadata else ''))
            else:
                db.execute("DELETE FROM http_cache WHERE url=?", (normalized,))
        return self._result(normalized, "ok", http_status=status, body=body, content_type=content_type, retrieved_at=retrieved, sha256=digest, etag=etag, last_modified=modified, final_url=final_url, cache_status="refreshed" if cached else "miss", pagination_headers=pagination_headers, cache_valid_until=_iso(now + ttl if store_cache else now), validated_at=_iso(now), **backend_metadata)
