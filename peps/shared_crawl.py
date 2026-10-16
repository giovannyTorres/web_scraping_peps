"""A bounded publisher index, shared by every entity in a bank.

Discovery pages contain metadata, never admissible article bodies. Fetches use
the ordinary scheduler, permissions, host quotas and SafeFetcher. A durable
metadata cache permits re-filtering new bank names without downloading again.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
from urllib.parse import urlencode, urlsplit


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
        stamp = (datetime.strptime(value, '%Y%m%d%H%M%S')
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
