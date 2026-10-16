"""Bounded parsers for publisher-wide discovery; no network requests or scoring.

Sitemaps and WordPress excerpts are leads, never article bodies. Callers retain
responsibility for source permissions, host allowlists, DNS validation and queue
budgets. Sharing one discovery page across an entity bank avoids one request per
name without asserting that the whole bank has been searched exhaustively.
"""
from __future__ import annotations

from datetime import datetime, timezone
import gzip
from html import unescape
from html.parser import HTMLParser
import io
import ipaddress
import json
import re
import zlib
from urllib.parse import unquote, urlsplit, urlunsplit
import xml.etree.ElementTree as ET


class DiscoveryParseError(ValueError):
    """A shared discovery response is invalid or exceeds its parsing budget."""


def _integer(value, name, minimum=1):
    if type(value) is not int or value < minimum:
        raise DiscoveryParseError(name + ' debe ser un entero >= ' + str(minimum))
    return value


def _payload(payload, max_bytes):
    _integer(max_bytes, 'max_bytes')
    if not isinstance(payload, (str, bytes)):
        raise DiscoveryParseError('Respuesta debe ser texto o bytes')
    raw = payload.encode('utf-8') if isinstance(payload, str) else payload
    if len(raw) > max_bytes:
        raise DiscoveryParseError('Respuesta excede max_bytes')
    # A fetcher may already have decoded Content-Encoding. Only gzip magic
    # activates decompression, with a hard cap on the expanded bytes as well.
    if raw.startswith(b'\x1f\x8b'):
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
                raw = stream.read(max_bytes + 1)
        except (EOFError, OSError, ValueError, zlib.error) as exc:
            raise DiscoveryParseError('Gzip inválido') from exc
        if len(raw) > max_bytes:
            raise DiscoveryParseError('Gzip expandido excede max_bytes')
    return raw


def _public_https(value):
    """Reject obviously unsafe URLs without resolving DNS or fetching anything."""
    if not isinstance(value, str):
        return None
    value = unescape(value).strip()
    if not value or any(ord(char) <= 32 for char in value) or '\\' in value:
        return None
    try:
        parts = urlsplit(value)
        host = parts.hostname
        if (parts.scheme.lower() != 'https' or not host or parts.username or parts.password
                or parts.port not in (None, 443)):
            return None
        host = host.rstrip('.').encode('idna').decode('ascii').lower()
        if host == 'localhost' or host.endswith(('.localhost', '.local', '.internal')):
            return None
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            # No DNS lookup here. SafeFetcher checks resolved addresses later.
            if '.' not in host or not re.fullmatch(r'[a-z0-9.-]+', host):
                return None
            if any(not label or label.startswith('-') or label.endswith('-') for label in host.split('.')):
                return None
        else:
            if not address.is_global:
                return None
        netloc = '[' + host + ']' if ':' in host else host
        return urlunsplit(('https', netloc, parts.path or '/', parts.query, ''))
    except (ValueError, UnicodeError):
        return None


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = []

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'template'):
            self.hidden.append(tag)
        if not self.hidden and tag in ('p', 'br', 'div', 'li', 'h1', 'h2', 'h3'):
            self.parts.append(' ')

    def handle_endtag(self, tag):
        if self.hidden and tag == self.hidden[-1]:
            self.hidden.pop()
        if not self.hidden:
            self.parts.append(' ')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _text(value, limit=20_000):
    if isinstance(value, dict):
        value = value.get('rendered', '')
    if not isinstance(value, str):
        return ''
    parser = _Text()
    parser.feed(value[:max(100_000, limit * 5)])
    return ' '.join(unescape(''.join(parser.parts)).split())[:limit]


def _date(value, *, assume_utc=False):
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    if len(value) > 50:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    if assume_utc and parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat()


def _tag(element):
    return element.tag.rsplit('}', 1)[-1] if isinstance(element.tag, str) else ''


def _direct(element, name):
    return next((child.text for child in element if _tag(child) == name), None)


def _slug_title(url):
    path = urlsplit(url).path.rstrip('/')
    slug = unquote(path.rsplit('/', 1)[-1])
    slug = re.sub(r'\.(?:html?|php|aspx?)$', '', slug, flags=re.I)
    return _text(re.sub(r'[-_]+', ' ', slug), 1000)


def parse_sitemap(payload, *, max_bytes=2_000_000, max_items=50_000, depth=0, max_depth=3):
    """Parse an XML index/urlset into metadata records and bounded child links.

    Only ``news:publication_date`` populates published_at; ``lastmod`` describes
    a modification and is preserved separately. A title derived from the URL is
    explicitly marked. Child URLs at max_depth are omitted and flagged, allowing
    callers to report incomplete discovery rather than silently claiming it.
    """
    _integer(max_items, 'max_items')
    _integer(depth, 'depth', 0)
    _integer(max_depth, 'max_depth', 0)
    raw = _payload(payload, max_bytes)
    if re.search(br'<!\s*(?:DOCTYPE|ENTITY)\b', raw.replace(b'\x00', b''), re.I):
        raise DiscoveryParseError('DTD y entidades XML no están permitidos')
    try:
        tree = ET.fromstring(raw)
    except (ET.ParseError, ValueError, LookupError) as exc:
        raise DiscoveryParseError('Sitemap XML inválido') from exc
    kind = _tag(tree)
    if kind not in ('sitemapindex', 'urlset'):
        raise DiscoveryParseError('Se esperaba sitemapindex o urlset')
    records, children, seen = [], [], set()
    observed = excluded = 0
    depth_limited = False
    for entry in tree:
        if _tag(entry) != ('sitemap' if kind == 'sitemapindex' else 'url'):
            continue
        observed += 1
        if observed > max_items:
            continue
        url = _public_https(_direct(entry, 'loc'))
        if not url:
            excluded += 1
            continue
        if url in seen:
            continue
        seen.add(url)
        lastmod = _direct(entry, 'lastmod')
        lastmod = lastmod.strip()[:100] if isinstance(lastmod, str) else None
        if kind == 'sitemapindex':
            if depth >= max_depth:
                depth_limited = True
            else:
                children.append({'url': url, 'lastmod': lastmod})
            continue
        news = next((child for child in entry if child.tag == '{http://www.google.com/schemas/sitemap-news/0.9}news'), None)
        title = _text(_direct(news, 'title'), 1000) if news is not None else ''
        publication = _direct(news, 'publication_date') if news is not None else None
        records.append({'url': url, 'title': title or _slug_title(url), 'text': '',
                        'published_at': _date(publication),
                        'metadata': {'discovery_kind': 'sitemap', 'discovery_only': True,
                                     'lastmod': lastmod, 'title_from_slug': not bool(title)}})
    return {'records': records, 'children': children,
            'pagination': {'kind': 'sitemap', 'sitemap_kind': kind, 'depth': depth,
                           'observed_items': observed, 'retained_items': len(records) + len(children),
                           'excluded_urls': excluded, 'depth_limited': depth_limited,
                           'truncated': observed > max_items or depth_limited}}


def parse_wordpress_posts(payload, *, headers=None, page=1, per_page=100,
                          max_bytes=2_000_000, max_items=100):
    """Parse public WP REST post metadata; content fields are never used.

    X-WP-Total(Pages) supplies an exact pagination bound when available. Without
    it, a full page implies one more page to probe, not a claim of completeness.
    Authentication/error objects and malformed post rows fail explicitly.
    """
    _integer(page, 'page')
    _integer(per_page, 'per_page')
    _integer(max_items, 'max_items')
    if per_page > 100:
        raise DiscoveryParseError('per_page debe ser <= 100')
    raw = _payload(payload, max_bytes)
    try:
        posts = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise DiscoveryParseError('WordPress JSON inválido') from exc
    if not isinstance(posts, list):
        raise DiscoveryParseError('WordPress requiere una lista de posts, no un objeto de error')
    if any(not isinstance(post, dict) or not isinstance(post.get('link'), str) for post in posts):
        raise DiscoveryParseError('Post WordPress inválido: falta link')
    records, excluded, seen = [], 0, set()
    for post in posts[:max_items]:
        url = _public_https(post['link'])
        if not url:
            excluded += 1
            continue
        if url in seen:
            continue
        seen.add(url)
        publication = _date(post.get('date_gmt'), assume_utc=True) or _date(post.get('date'))
        records.append({'url': url, 'title': _text(post.get('title', ''), 1000),
                        'text': _text(post.get('excerpt', '')), 'published_at': publication,
                        'metadata': {'discovery_kind': 'wordpress_rest', 'discovery_only': True,
                                     'excerpt_only': True, 'wordpress_id': post.get('id'),
                                     'date_gmt': post.get('date_gmt'), 'date': post.get('date')}})
    normalized_headers = {str(key).lower(): value for key, value in (headers or {}).items()}

    def header_integer(key):
        value = normalized_headers.get(key)
        try:
            return int(value) if str(value).isdigit() else None
        except (ValueError, OverflowError):
            return None

    total, total_pages = header_integer('x-wp-total'), header_integer('x-wp-totalpages')
    if total_pages is not None:
        next_page = page + 1 if page < total_pages and posts else None
    else:
        next_page = page + 1 if len(posts) >= per_page else None
    return {'records': records, 'children': [],
            'pagination': {'kind': 'wordpress_rest', 'page': page, 'per_page': per_page,
                           'total': total, 'total_pages': total_pages, 'next_page': next_page,
                           'observed_items': len(posts), 'retained_items': len(records),
                           'excluded_urls': excluded, 'truncated': len(posts) > max_items,
                           'pagination_inferred': total_pages is None}}
