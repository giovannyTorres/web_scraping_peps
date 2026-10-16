"""Bounded, page-bound publication metadata; no network or inferred dates."""
from datetime import datetime, timezone
from html import unescape
import json
import re
from urllib.parse import urlsplit, urlunsplit

_TYPES = {'Article', 'NewsArticle'}
_VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link',
         'meta', 'param', 'source', 'track', 'wbr'}
_BLOCKED = {'nav', 'aside', 'footer', 'template', 'noscript', 'iframe', 'form', 'svg'}
_LIST_TYPES = {'ItemList', 'BreadcrumbList', 'SiteNavigationElement', 'CollectionPage', 'WebSite'}


def _types(value):
    values = [value] if isinstance(value, str) else value if isinstance(value, list) else []
    return {v.rsplit('/', 1)[-1] if v.startswith(('https://schema.org/', 'http://schema.org/')) else v
            for v in values if isinstance(v, str)}


def _url(value):
    from .shared_discovery import _public_https
    normalized = _public_https(value)
    if normalized is None:
        return None
    parts = urlsplit(normalized)
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip('/'), parts.query, ''))


def _title(value):
    return re.sub(r'\s+', ' ', re.sub('<[^>]+>', ' ', unescape(str(value or '')))).strip().casefold()


def _matches_title(headline, page_title):
    headline, page_title = _title(headline), _title(page_title)
    return bool(len(headline) >= 15 and (headline == page_title or
                any(page_title.startswith(headline + separator) for separator in (' | ', ' - ', ' – ', ' — ', ' :: '))))


def _date(value):
    if not isinstance(value, str) or len(value) > 50:
        return None
    value = value.strip()
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,9})?)?(?:Z|[+-]\d{2}:?\d{2})?)?', value):
        return None
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return (result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result).astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate structured metadata key')
        result[key] = value
    return result


class PublicationMetadata:
    """Receive the existing HTML parser callbacks only for article extraction.

    Bounds: 128 DOM levels, 32 microdata articles, eight 128-Ki-character JSON-LD
    scripts, and 500 JSON nodes at depth <=16. Oversized input fails closed.
    DateModified, visible dates and untyped/global datePublished are ignored.
    """
    def __init__(self, page_url):
        self.page_url = _url(page_url)
        self.stack, self.articles, self.scripts = [], [], []
        self.script_parts, self.script_length = None, 0
        self.exceeded = False

    def start(self, tag, attrs):
        if self.exceeded:
            return
        values = dict(attrs)
        parent = self.stack[-1] if self.stack else {'scope': None, 'blocked': False}
        scope, blocked = parent['scope'], parent['blocked'] or tag in _BLOCKED
        properties = str(values.get('itemprop') or '').split()
        if scope is not None and scope.get('article') and not blocked:
            if 'datePublished' in properties:
                scope['dates'].append(values.get('content') or values.get('datetime'))
            if 'mainEntityOfPage' in properties:
                scope['urls'].append(values.get('itemid') or values.get('href') or values.get('content'))
            if 'headline' in properties and values.get('content'):
                scope['headlines'].append(values['content'])
        node = None
        if 'itemscope' in values:
            scope = {'article': bool(_types(values.get('itemtype')) & _TYPES),
                     'dates': [], 'urls': [values.get('itemid')], 'headlines': [], 'closed': False}
            if scope['article'] and not blocked:
                if len(self.articles) >= 32:
                    self.exceeded = True
                    return
                self.articles.append(scope)
                node = scope
        if tag == 'script' and not blocked and str(values.get('type') or '').split(';')[0].strip().lower() == 'application/ld+json':
            if len(self.scripts) >= 8:
                self.exceeded = True
                return
            self.script_parts, self.script_length = [], 0
        if tag not in _VOID:
            if len(self.stack) >= 128:
                self.exceeded = True
                return
            self.stack.append({'tag': tag, 'scope': scope, 'blocked': blocked, 'node': node})

    def data(self, value):
        if self.script_parts is not None and not self.exceeded:
            self.script_length += len(value)
            if self.script_length > 131_072:
                self.exceeded = True
                self.script_parts = None
                return
            self.script_parts.append(value)

    def end(self, tag):
        if tag == 'script' and self.script_parts is not None:
            self.scripts.append(''.join(self.script_parts))
            self.script_parts = None
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index]['tag'] == tag:
                node = self.stack[index]['node']
                if node is not None:
                    node['closed'] = True
                del self.stack[index:]
                break

    def published_at(self, page_title, retrieved_at):
        cutoff = _date(retrieved_at)
        if self.exceeded or self.page_url is None or cutoff is None:
            return None
        candidates = [node for node in self.articles if node['closed']]
        visited = 0
        for script in self.scripts:
            try:
                pending = [(json.loads(script, object_pairs_hook=_unique), 0, False)]
            except (ValueError, RecursionError):
                continue
            while pending:
                node, depth, blocked = pending.pop()
                visited += 1
                if visited > 500 or depth > 16:
                    return None
                if isinstance(node, list):
                    pending.extend((value, depth + 1, blocked) for value in node)
                elif isinstance(node, dict):
                    types = _types(node.get('@type'))
                    if types & _TYPES and not blocked:
                        urls = []
                        for key in ('url', '@id', 'mainEntityOfPage'):
                            value = node.get(key)
                            urls.append(value.get('@id') or value.get('url') if isinstance(value, dict) else value)
                        candidates.append({'dates': [node.get('datePublished')], 'urls': urls,
                                           'headlines': [node.get('headline') or node.get('name')]})
                    blocked = blocked or bool(types & _LIST_TYPES)
                    pending.extend((value, depth + 1, blocked) for value in node.values() if isinstance(value, (list, dict)))
        accepted = set()
        for node in candidates:
            if not any(_url(value) == self.page_url for value in node['urls'] if value):
                continue
            headlines = [value for value in node['headlines'] if value]
            if headlines and not all(_matches_title(value, page_title) for value in headlines):
                continue
            for raw in node['dates']:
                date = _date(raw)
                if date is not None:
                    if date > cutoff:
                        return None
                    accepted.add(date.isoformat())
        return next(iter(accepted)) if len(accepted) == 1 else None
