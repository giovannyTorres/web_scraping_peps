"""A bounded publisher index, shared by every entity in a bank.

Discovery pages contain metadata, never admissible article bodies. Fetches use
the ordinary scheduler, permissions, host quotas and SafeFetcher. A durable
metadata cache permits re-filtering new bank names without downloading again.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


_LEGACY_WORDPRESS_SEARCH_ENDPOINTS = {
    'contralinea_index': 'https://contralinea.com.mx/wp-json/wp/v2/posts',
    'aristegui_index': 'https://editorial.aristeguinoticias.com/wp-json/wp/v2/posts',
    'desinformemonos_index': 'https://desinformemonos.org/wp-json/wp/v2/posts',
}


def supports_wordpress_search(source):
    """Recognize an explicit adapter or one of three existing public profiles.

    This is a capability check only. Enabled/terms/body permissions continue to
    be enforced by the caller. Existing saved profiles gain a directed-search
    option without changing their stored permissions or selected strategy.
    """
    if not isinstance(source, dict) or source.get('kind') != 'wordpress_api':
        return False
    marker = source.get('search_kind')
    if marker not in (None, 'wordpress_api_search'):
        return False
    base = source.get('api_url') or source.get('url')
    if not isinstance(base, str) or not allowed_index_url(base, source):
        return False
    from .shared_discovery import _public_https
    if _public_https(base) is None:
        return False
    parts = urlsplit(base)
    if parts.path.rstrip('/') != '/wp-json/wp/v2/posts' or parts.query or parts.fragment:
        return False
    return (marker == 'wordpress_api_search'
            or base == _LEGACY_WORDPRESS_SEARCH_ENDPOINTS.get(source.get('id')))


def wordpress_search_url(source, query, first, last, *, page=1, per_page=None):
    """Build one bounded publisher search, separate from a shared archive crawl.

    WordPress searches the publisher's original posts, including their bodies.
    Its results remain retrieval leads: a search hit does not assert that the
    name occurs in the returned title/excerpt or establish the person's identity.
    Body permissions are deliberately not inferred from this public endpoint.
    """
    if not supports_wordpress_search(source):
        raise ValueError('La fuente no declara búsqueda dirigida de WordPress')
    if not isinstance(query, str) or not query.strip() or len(query) > 1500:
        raise ValueError('La consulta WordPress requiere entre 1 y 1500 caracteres')
    if any(ord(char) < 32 for char in query):
        raise ValueError('La consulta WordPress contiene caracteres de control')
    if type(page) is not int or not 1 <= page <= 10000:
        raise ValueError('La página WordPress requiere un entero entre 1 y 10000')
    per_page = source.get('search_max_records', 10) if per_page is None else per_page
    if type(per_page) is not int or not 1 <= per_page <= 100:
        raise ValueError('El límite WordPress requiere un entero entre 1 y 100')
    first, last = _period_timestamp(first), _period_timestamp(last)
    if first is None or last is None or first >= last:
        raise ValueError('La búsqueda WordPress requiere un período UTC válido')
    base = source.get('api_url') or source.get('url')
    if not isinstance(base, str) or not allowed_index_url(base, source):
        raise ValueError('La API WordPress está fuera del alcance autorizado')
    parts = urlsplit(base)
    # Preserve explicit endpoint routing (e.g. rest_route) while overriding
    # request parameters exactly once; duplicate search/date keys are unsafe.
    args = dict(parse_qsl(parts.query, keep_blank_values=True))
    args.update(search=query.strip(), per_page=per_page, page=page,
                orderby='relevance', order='desc',
                after=(first - timedelta(seconds=1)).strftime('%Y-%m-%dT%H:%M:%SZ'),
                before=last.strftime('%Y-%m-%dT%H:%M:%SZ'),
                _fields='id,link,date_gmt,date,title.rendered,excerpt.rendered')
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(args), ''))


def wordpress_url(source, page, first, last):
    base = source.get('api_url') or source['url']
    def stamp(value):
        return value.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    args = {'per_page': 100, 'page': page, 'orderby': 'date', 'order': 'desc',
            'after': stamp(first - timedelta(seconds=1)), 'before': stamp(last),
            '_fields': 'id,link,date_gmt,date,title.rendered,excerpt.rendered'}
    return base + ('&' if '?' in base else '?') + urlencode(args)


def seed_pages(source, config, first, last):
    from .news_search import _windows
    maximum = 24 if config['execution_mode'] == 'quick' else 240
    common = {'source_id': source['id'], 'kind': 'news_feed', 'query_type': 'shared_index',
              'shared_index': True, 'page_limit': maximum, 'depth': 0,
              'scope_start': first.isoformat(), 'scope_end': last.isoformat()}
    if source['kind'] == 'wordpress_api':
        # Fair historical sampling: every year gets a first page before any
        # year gets its second. Pagination preserves the exact same window.
        return [dict(common, page=1, start=a.isoformat(), end=b.isoformat(),
                     url=wordpress_url(source, 1, a, b))
                for a, b in _windows(first, last, 'year')[:maximum]]
    urls = source.get('urls') or [source.get('sitemap_url') or source.get('url')]
    return [dict(common, start=first.isoformat(), end=last.isoformat(), url=url)
            for url in urls[:maximum] if url]


def allowed_index_url(url, source):
    """Syntactic scope only; SafeFetcher still validates DNS and redirects."""
    try:
        parts = urlsplit(url)
        return (parts.scheme == 'https' and parts.hostname in source.get('allowed_hosts', [])
                and parts.port in (None, 443) and not parts.username and not parts.password
                and not any(ord(char) < 33 for char in url))
    except (TypeError, ValueError):
        return False


def next_pages(source, payload, parsed):
    """Return scoped metadata requests, never article links."""
    if source['kind'] == 'wordpress_api':
        page = parsed['pagination'].get('next_page')
        if page:
            child = dict(payload, page=page,
                         inferred_page=bool(parsed['pagination'].get('pagination_inferred')))
            child['url'] = wordpress_url(source, page, datetime.fromisoformat(payload['start']),
                                         datetime.fromisoformat(payload['end']))
            yield child
    else:
        children = sorted(parsed['children'], key=lambda child: (child.get('lastmod') or '', child['url']), reverse=True)
        for item in children:
            if allowed_index_url(item['url'], source):
                yield dict(payload, url=item['url'], depth=payload.get('depth', 0) + 1)


def _period_timestamp(value):
    try:
        stamp = (value if isinstance(value, datetime) else
                 datetime.strptime(value, '%Y%m%d%H%M%S')
                 if isinstance(value, str) and re.fullmatch(r'\d{14}', value)
                 else datetime.fromisoformat(value.replace('Z', '+00:00')))
        return stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError):
        return None


def publication_period_status(record, payload):
    """Classify publication dates against exact half-open UTC boundaries.

    Targeted jobs use compact 14-digit timestamps; shared pages use ISO dates.
    Missing/invalid publication dates remain unknown candidates, never covered
    dates. Sitemap lastmod is not a publication date.
    """
    stamp = _period_timestamp(record.get('published_at'))
    if stamp is None:
        return 'publication_date_unknown'
    first, last = [_period_timestamp(payload.get(key)) for key in ('start', 'end')]
    if first is None or last is None or first >= last:
        return 'period_bounds_unknown'
    return 'in_period' if first <= stamp < last else 'outside_period'


def in_period(record, payload):
    """Only a known publication date can exclude a candidate, not lastmod."""
    return publication_period_status(record, payload) != 'outside_period'
