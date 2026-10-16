"""Anonymous, bounded social leads; user posts are never article evidence.

Lemmy v3 searches the catalog known by one instance. It is neither a web search
engine nor an exhaustive Fediverse search. Network access, robots, courtesy,
cache, permissions and destination DNS validation stay with SafeFetcher.
"""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from html.parser import HTMLParser
import ipaddress
import json
import re
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from .identity import _sequence, _tokens

MAX_RECORDS = 50
MAX_RESPONSE_CHARS = 2_000_000
MAX_EXCERPT_CHARS = 500


def _public_https_url(value):
    if not isinstance(value, str) or not value or '\\' in value or any(ord(char) <= 32 for char in value):
        return None
    try:
        parts = urlsplit(value)
        if (parts.scheme != 'https' or not parts.hostname or parts.username is not None
                or parts.password is not None or parts.port not in (None, 443)):
            return None
        host = parts.hostname.rstrip('.').encode('idna').decode('ascii').lower()
        if host == 'localhost' or host.endswith(('.localhost', '.local', '.internal')):
            return None
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if ('.' not in host or not re.fullmatch(r'[a-z0-9.-]+', host)
                    or re.fullmatch(r'[0-9.]+', host)
                    or any(not label or label.startswith('-') or label.endswith('-') for label in host.split('.'))):
                return None
        else:
            if (not address.is_global or getattr(address, 'ipv4_mapped', None) is not None
                    or getattr(address, 'sixtofour', None) is not None or getattr(address, 'teredo', None) is not None):
                return None
        authority = '[' + host + ']' if ':' in host else host
        return urlunsplit(('https', authority, parts.path or '/', parts.query, ''))
    except (ValueError, UnicodeError):
        return None


def _query(value):
    if isinstance(value, dict):
        value = value.get('name')
    if not isinstance(value, str) or not value.strip() or len(value) > 300:
        raise ValueError('La consulta social requiere entre 1 y 300 caracteres')
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError('La consulta social contiene caracteres de control')
    return ' '.join(value.split())


def _bounded_int(value, name, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(name + ' debe ser un entero entre ' + str(low) + ' y ' + str(high))
    return value


def build_lemmy_search_url(entity_or_query, source, *, limit=10, page=1):
    """Build only the tested v3/q API, without invented Boolean/date semantics."""
    query = _query(entity_or_query)
    _bounded_int(limit, 'limit', 1, MAX_RECORDS)
    _bounded_int(page, 'page', 1, 10)
    raw = source.get('api_url') or source.get('url') or source.get('base_url')
    endpoint = _public_https_url(raw)
    if not endpoint:
        raise ValueError('La instancia Lemmy requiere una URL HTTPS pública')
    parts = urlsplit(endpoint)
    if parts.hostname not in {str(host).rstrip('.').lower() for host in source.get('allowed_hosts', [])}:
        raise ValueError('La instancia Lemmy debe estar en allowed_hosts')
    if parts.query or parts.fragment or urlsplit(raw).fragment:
        raise ValueError('El endpoint Lemmy no admite query ni fragmento predefinidos')
    if parts.path.rstrip('/') not in ('', '/api/v3/search'):
        raise ValueError('Sólo se ha verificado Lemmy API v3 /api/v3/search')
    endpoint = urlunsplit(('https', parts.netloc, '/api/v3/search', '', ''))
    return endpoint + '?' + urlencode({'q': query, 'type_': 'Posts', 'sort': 'TopAll',
                                      'listing_type': 'All', 'limit': limit, 'page': page})


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = [], []

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'template'):
            self.hidden.append(tag)

    def handle_endtag(self, tag):
        if self.hidden and self.hidden[-1] == tag:
            self.hidden.pop()

    def handle_data(self, value):
        if not self.hidden and value.strip():
            self.parts.append(value.strip())


def _text(value, limit):
    if not isinstance(value, str):
        return ''
    try:
        value.encode('utf-8')
    except UnicodeError as exc:
        raise ValueError('Lemmy devolvió texto Unicode inválido') from exc
    parser = _Text()
    parser.feed(value[:100_000])
    return ' '.join(' '.join(parser.parts).split())[:limit]


def _ugc_excerpt(body, query):
    text = _text(body, 100_000)
    if not text:
        return ''
    sequence, tokens = _sequence(query), _tokens(text)
    words = tuple(token.value for token in tokens)
    start = 0
    if sequence:
        for index in range(len(words) - len(sequence) + 1):
            if words[index:index + len(sequence)] == sequence:
                start = max(0, tokens[index].start - 140)
                break
    if start:
        # Start at a word boundary without sacrificing the nominal mention.
        space = text.find(' ', start, min(len(text), start + 60))
        if space >= 0:
            start = space + 1
    return text[start:start + MAX_EXCERPT_CHARS].strip()


def _date(value):
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return (stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp).astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError):
        return None


def parse_lemmy_results(fetch_result, source, *, max_records=None):
    """Keep bounded, auditable outgoing links plus explicitly untrusted excerpts.

    Discussion bodies and creator profiles are never returned. A post date is
    retained separately from the unknown publication date of its linked article.
    Self-posts are excluded: this adapter prioritizes original publisher reading.
    """
    if fetch_result.get('status') not in ('ok', 'not_modified'):
        return []
    limit = source.get('max_records', 10) if max_records is None else max_records
    _bounded_int(limit, 'max_records', 1, MAX_RECORDS)
    body = fetch_result.get('body')
    if not isinstance(body, str) or len(body) > MAX_RESPONSE_CHARS:
        raise ValueError('Respuesta Lemmy ausente o demasiado grande')
    try:
        payload = json.loads(body)
    except (ValueError, RecursionError) as exc:
        raise ValueError('Lemmy devolvió JSON inválido') from exc
    if (not isinstance(payload, dict) or payload.get('error') or payload.get('errors')
            or payload.get('success') is False or payload.get('status') in ('error', 'failed')
            or not isinstance(payload.get('posts'), list)):
        raise ValueError('Lemmy no devolvió una lista válida de posts')
    response_url = (fetch_result.get('final_url') or fetch_result.get('url')
                    or source.get('api_url') or source.get('url') or '')
    response_url = _public_https_url(response_url)
    if not response_url or urlsplit(response_url).hostname not in source.get('allowed_hosts', []):
        raise ValueError('No se puede verificar la instancia Lemmy de procedencia')
    request_host = urlsplit(response_url).hostname
    query = parse_qs(urlsplit(response_url).query, max_num_fields=100).get('q', [''])[0]
    records, seen = [], {}
    info = {'observed_entries': len(payload['posts']), 'truncated': len(payload['posts']) > limit,
            'excluded_non_editorial': 0, 'duplicate_editorial_urls': 0,
            'social_discovery': True, 'user_generated_content': True, 'discovery_only': True,
            'scope': 'one_instance_known_catalog', 'identity_verified': False,
            'score_effect': 'none', 'ugc_excerpt_limit': MAX_EXCERPT_CHARS,
            'post_dates_are_not_editorial_publication_dates': True}
    for view in payload['posts'][:limit]:
        if not isinstance(view, dict) or not isinstance(view.get('post'), dict):
            raise ValueError('Lemmy devolvió un post mal formado')
        post = view['post']
        title = _text(post.get('name'), 1000)
        if not title:
            raise ValueError('Lemmy devolvió un post sin título')
        link, discussion = _public_https_url(post.get('url')), _public_https_url(post.get('ap_id'))
        if discussion is None and type(post.get('id')) is int and post['id'] > 0:
            discussion = 'https://' + request_host + '/post/' + str(post['id'])
        # Neither a same-instance link nor the discussion itself is an original
        # article. External HTTPS links remain candidates pending publisher checks.
        if (not link or not discussion or urlsplit(link).hostname == request_host or link == discussion
                or (discussion and urlsplit(link).hostname == urlsplit(discussion).hostname)):
            info['excluded_non_editorial'] += 1
            continue
        raw_ugc = post.get('body') if isinstance(post.get('body'), str) else ''
        excerpt = _ugc_excerpt(raw_ugc, query)
        posted_at = _date(post.get('published'))
        ugc_hash = sha256(raw_ugc.encode('utf-8')).hexdigest()
        referrer = {'social_post_url': discussion, 'post_published_at': posted_at,
                    'ugc_body_sha256': ugc_hash}
        if link in seen:
            info['duplicate_editorial_urls'] += 1
            seen[link]['metadata']['social_referrers'].append(referrer)
            continue
        metadata = {'social_discovery': True, 'discovery_only': True, 'user_generated_content': True,
                    'content_origin': 'untrusted_social_post_excerpt', 'article_body_available': False,
                    'source_instance': request_host, 'social_post_url': discussion,
                    'social_post_id': post.get('id') if type(post.get('id')) is int else None,
                    'post_published_at': posted_at, 'editorial_link_url': link,
                    'ugc_excerpt': excerpt, 'ugc_body_sha256': ugc_hash,
                    'social_referrers': [referrer],
                    'ugc_excerpt_truncated': len(_text(raw_ugc, MAX_EXCERPT_CHARS + 1)) > MAX_EXCERPT_CHARS,
                    'identity_verified': False, 'score_effect': 'none'}
        record = {'url': link, 'title': title, 'text': excerpt, 'published_at': None,
                  'discovered_at': None, 'source_kind': 'discovery',
                  'source_id': source.get('id', 'lemmy'), 'metadata': metadata}
        seen[link] = record
        records.append(record)
    fetch_result['parse_metadata'] = info
    return records


def external_social_search_links(entity):
    """Explicit browser searches; no claim of anonymous automated collection."""
    query = _query(entity)
    platforms = (
        ('bluesky', 'Bluesky', 'https://bsky.app/search', 'q'),
        ('mastodon', 'Mastodon', 'https://mastodon.social/search', 'q'),
        ('youtube', 'YouTube', 'https://www.youtube.com/results', 'search_query'),
        ('x', 'X', 'https://x.com/search', 'q'),
        ('reddit', 'Reddit', 'https://www.reddit.com/search/', 'q'),
    )
    return [{'id': key, 'platform': label, 'label': 'Buscar en ' + label + ' · búsqueda externa',
             'url': base + '?' + urlencode({parameter: query}), 'external': True,
             'automated': False, 'may_require_login': True}
            for key, label, base, parameter in platforms]
