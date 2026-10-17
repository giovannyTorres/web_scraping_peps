"""Keyless public discovery: bounded metadata, never article evidence.

These adapters do not rotate endpoints, evade challenges, execute JavaScript or
download result pages. HTTP, robots, budgets and destination permissions belong
to the ordinary collector. A search hit never establishes identity or conduct.
"""
from __future__ import annotations

import ipaddress
import json
import re
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlsplit
from xml.etree import ElementTree

from .identity import COMPANY_KINDS, _fold

PUBLIC_SEARCH_KINDS = frozenset({'mwmbl', 'duckduckgo_html', 'duckduckgo_lite', 'searxng', 'lemmy_search', 'bing_news_rss', 'arquivo_fulltext'})
DEFAULT_ENDPOINTS = {
    'mwmbl': 'https://mwmbl.org/api/v2/search/',
    'duckduckgo_html': 'https://html.duckduckgo.com/html/',
    'duckduckgo_lite': 'https://lite.duckduckgo.com/lite/',
    'bing_news_rss': 'https://www.bing.com/news/search',
}
FOCUS_TERMS = {'adverse': ('corrupción', 'sanción'),
               'public_role': ('contrato', 'nombramiento')}


class PublicSearchParseError(ValueError):
    """A response failure, distinct from an invalid source configuration."""
    def __init__(self, message, *, category='malformed_response'):
        super().__init__(message)
        self.category = category
        self.retryable = category != 'access_challenge'
        self.invalidate_cache = self.retryable
        self.code = {'malformed_response': 'discovery_parse_invalid',
                     'upstream_unavailable': 'discovery_upstream_unavailable',
                     'access_challenge': 'discovery_access_challenge'}[category]


def _query(value):
    value = value.get('name', '') if isinstance(value, dict) else value
    if not isinstance(value, str):
        raise ValueError('La búsqueda requiere un nombre o consulta de texto')
    value = ' '.join(re.sub(r'[\x00-\x1f\x7f]', ' ', value).split())
    if not 1 <= len(value) <= 500:
        raise ValueError('La consulta pública requiere entre 1 y 500 caracteres')
    return value


def safe_result_url(value, base=''):
    """Retain the original HTTPS spelling; DNS is checked before any fetch."""
    if not isinstance(value, str) or not value or '\\' in value or any(ord(c) < 33 or ord(c) == 127 for c in value):
        return None
    try:
        value = urljoin(base, value)
        parts = urlsplit(value)
        if (parts.scheme.lower() != 'https' or not parts.hostname or parts.username is not None
                or parts.password is not None or parts.port not in (None, 443)):
            return None
        host = parts.hostname.rstrip('.').encode('idna').decode('ascii').lower()
        if host in {'localhost', 'metadata.google.internal'} or host.endswith(('.local', '.internal', '.localhost')):
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
        return value
    except (ValueError, UnicodeError):
        return None


def public_search_url(query, source):
    query = _query(query)
    kind = source.get('kind')
    if kind == 'arquivo_fulltext':
        from .archive_text_search import search_url
        return search_url(query, source)
    if kind == 'lemmy_search':
        from .social_discovery import build_lemmy_search_url
        return build_lemmy_search_url(query, source, limit=min(50, max(1, int(source.get('max_records', 10)))))
    endpoint = source.get('search_url') or source.get('api_url') or source.get('url') or DEFAULT_ENDPOINTS.get(kind)
    if kind not in PUBLIC_SEARCH_KINDS or not safe_result_url(endpoint):
        raise ValueError('Se requiere un endpoint HTTPS público para el buscador')
    if kind in {'mwmbl', 'duckduckgo_html', 'duckduckgo_lite'}:
        args = {'q': query}
    elif kind == 'searxng':
        if source.get('public_search_quote') is True:
            query = '"' + query.replace('"', ' ') + '"'
        page = source.get('public_search_page', 1)
        if type(page) is not int or not 1 <= page <= 10:
            raise ValueError('La página pública requiere un entero entre 1 y 10')
        language = source.get('public_search_language', 'es')
        if not isinstance(language, str) or not re.fullmatch(r'[a-z]{2}(?:-[A-Z]{2})?', language):
            raise ValueError('Idioma público no válido')
        args = {'q': query, 'format': 'json', 'language': language, 'pageno': page}
    elif kind == 'bing_news_rss':
        parts = urlsplit(endpoint)
        if (parts.scheme != 'https' or parts.netloc != 'www.bing.com'
                or parts.path != '/news/search' or parts.query or parts.fragment):
            raise ValueError('Bing News RSS requiere su endpoint público declarado')
        args = {'q': query, 'format': 'rss', 'mkt': 'es-MX'}
    else:
        raise ValueError('Buscador público no reconocido')
    return endpoint + ('&' if '?' in endpoint else '?') + urlencode(args)


def public_queries(entity, config, variants):
    """A nominal first sweep, then context, plain-text focus and aliases.

    Extra words guide discovery without asserting an event or relying on an
    engine's Boolean/date syntax. Identity adds no thematic requests.
    """
    if not variants:
        return []
    primary = _query(variants[0])
    queries = [{'query': primary, 'query_type': 'identity'}]
    context = entity.get('context') or {}
    if config.get('use_context') and isinstance(context, dict):
        # One secondary contextual request combines at most one geography and
        # one supplied role, useful for notaries and other common public names.
        # Keep the broad nominal query and never present either clue as
        # independent identity evidence.
        hints = {'geography': [], 'role': []}
        for key in ('municipality', 'city', 'state', 'role', 'position', 'cargo'):
            values = context.get(key) or []
            for value in values if isinstance(values, list) else [values]:
                if isinstance(value, dict):
                    value = value.get('value') or value.get('name')
                if isinstance(value, str):
                    hint = ' '.join(re.sub(r'[\x00-\x1f\x7f]', ' ', value).split())
                    if hint and len(primary + ' ' + hint) <= 500:
                        category = 'geography' if key in ('municipality', 'city', 'state') else 'role'
                        if hint.casefold() not in {value.casefold() for value in hints[category]}:
                            hints[category].append(hint)
        selected = []
        for category in ('geography', 'role'):
            for hint in hints[category]:
                if hint.casefold() in primary.casefold() or hint.casefold() in {value.casefold() for value in selected}:
                    continue
                if len(' '.join([primary, *selected, hint])) <= 500:
                    selected.append(hint)
                    break
        if selected:
            context_hint = ' '.join(selected)
            text = primary + ' ' + context_hint
            if text.casefold() not in {query['query'].casefold() for query in queries}:
                queries.append({'query': _query(text), 'query_type': 'context',
                                'context_hint': context_hint, 'context_hints': selected})
    queries.extend({'query': _query(primary + ' ' + term), 'query_type': 'focus',
                    'focus_term': term} for term in FOCUS_TERMS.get(config.get('preset'), ()))
    company = _fold(entity.get('kind', 'person')) in COMPANY_KINDS
    for name in variants[1:4]:
        # Short personal aliases remain contextual. The canonical full name is
        # always first; unrestricted aliases are never used as global keywords.
        if not company and len(name.split()) < 2:
            continue
        queries.append({'query': _query(name), 'query_type': 'alias'})
    return queries


def _ddg_url(value, base):
    target = urljoin(base, value or '')
    try:
        parts = urlsplit(target)
        if parts.hostname in {'duckduckgo.com', 'html.duckduckgo.com', 'lite.duckduckgo.com'}:
            if parts.path.rstrip('/') != '/l':
                return None
            values = parse_qs(parts.query).get('uddg', [])
            if len(values) != 1:
                return None
            target = values[0]
        return safe_result_url(target)
    except (ValueError, UnicodeError):
        return None


def _bing_news_url(value):
    """Read one explicit destination; never request an HTTP click wrapper.

    The live RSS spells wrappers as HTTP. Only the exact observed Bing host
    and path may provide an HTTPS destination; this does not authorize any
    publisher, follow redirects or permit nested wrappers.
    """
    if (not isinstance(value, str) or not value or len(value) > 16384 or '\\' in value or '#' in value
            or any(ord(char) < 33 or ord(char) == 127 for char in value)):
        return None
    try:
        parts = urlsplit(value)
        if parts.hostname != 'www.bing.com':
            return None
        if (parts.scheme not in {'http', 'https'} or parts.path != '/news/apiclick.aspx'
                or parts.username is not None or parts.password is not None or parts.fragment
                or parts.port not in (None, 80 if parts.scheme == 'http' else 443)):
            return None
        values = parse_qs(parts.query, keep_blank_values=True, max_num_fields=32).get('url', [])
        if len(values) != 1 or not values[0] or len(values[0]) > 8192:
            return None
        target = values[0]
        # parse_qs already decoded one layer. A second encoded absolute scheme
        # may be decoded once; existing escaped path/query bytes stay intact.
        if re.match(r'^https%3a%2f%2f', target, re.I):
            target = unquote(target, errors='strict')
        target = safe_result_url(target)
        if target is None or '#' in target:
            return None
        destination = urlsplit(target)
        host = destination.hostname.rstrip('.').lower()
        if destination.fragment or host == 'bing.com' or host.endswith('.bing.com'):
            return None
        return target
    except (ValueError, UnicodeError):
        return None


def _bing_news_records(body):
    if body.lstrip('\ufeff \t\r\n').lower().startswith(('<!doctype html', '<html')):
        raise PublicSearchParseError('Bing News RSS devolvió HTML; formato no compatible, búsqueda no completada')
    if '<!DOCTYPE' in body.upper() or '<!ENTITY' in body.upper():
        raise PublicSearchParseError('Bing News RSS no admite declaraciones ni entidades XML')
    try:
        root = ElementTree.fromstring(body)
    except (ElementTree.ParseError, ValueError) as exc:
        raise PublicSearchParseError('Bing News RSS devolvió XML inválido; búsqueda no completada') from exc
    channels = root.findall('channel')
    if root.tag != 'rss' or root.attrib.get('version') != '2.0' or len(channels) != 1:
        raise PublicSearchParseError('Bing News RSS devolvió un formato no reconocido; no equivale a cero resultados')
    records = []
    for item in channels[0].findall('item'):
        title, wrapper = item.findtext('title'), item.findtext('link')
        if not isinstance(title, str) or not isinstance(wrapper, str):
            raise PublicSearchParseError('Bing News RSS devolvió una noticia sin título o enlace')
        records.append({'url': _bing_news_url(wrapper), 'title': title,
                        'text': item.findtext('description') or '',
                        'provider_link': wrapper,
                        'provider_publication_date': (item.findtext('pubDate') or '')[:200] or None})
    return records


class _DuckResults(HTMLParser):
    """Parse explicit organic cards in HTML and table rows in Lite."""
    def __init__(self, base):
        super().__init__(convert_charrefs=True)
        self.base, self.records, self.current = base, [], None
        self.stack, self.capture, self.chunks = [], None, []
        self._tag_positions = {}
        self.capture_chars = 0
        self.challenge, self.no_results, self.structure = False, False, False
        self.ignored = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = set((attrs.get('class') or '').split())
        identity = (attrs.get('id') or '') + ' ' + (attrs.get('class') or '')
        if (re.search(r'(?:anomaly|captcha|challenge)[-_]', identity, re.I)
                or (tag == 'form' and any(s in (attrs.get('action') or '').lower() for s in ('anomaly', 'captcha', 'challenge')))):
            self.challenge = True
        if classes & {'no-results', 'no-results__message', 'result--no-result', 'no-results__title'}:
            self.no_results = True
        if classes & {'results', 'result', 'result-link'} or attrs.get('id') == 'links':
            self.structure = True
        parent_hidden = self.stack[-1][1] if self.stack else False
        hidden = parent_hidden or tag in {'script', 'style', 'noscript', 'head', 'nav', 'footer'} or bool(classes & {'result--ad', 'result--ads', 'ads', 'advertisement'})
        if tag not in {'br', 'hr', 'img', 'input', 'meta', 'link', 'wbr', 'source', 'area', 'base', 'embed', 'param', 'track'}:
            self._tag_positions.setdefault(tag, []).append(len(self.stack))
            self.stack.append((tag, hidden))
        if hidden:
            return
        if tag == 'a' and classes & {'result__a', 'result-link'}:
            self._finish_capture()
            self.current = {'url': _ddg_url(attrs.get('href'), self.base), 'title': '', 'text': ''}
            self.records.append(self.current)
            self.capture, self.chunks = ('title', tag, len(self.stack)), []
        elif self.current is not None and classes & {'result__snippet', 'result-snippet'}:
            self._finish_capture()
            self.capture, self.chunks = ('text', tag, len(self.stack)), []

    def handle_endtag(self, tag):
        if self.capture is not None and tag == self.capture[1] and len(self.stack) == self.capture[2]:
            self._finish_capture()
        positions = self._tag_positions.get(tag)
        if not positions:
            return
        index = positions[-1]
        # A scan for every unmatched closing tag becomes quadratic on a small
        # malformed response. Indexed open tags skip misses in O(1); every
        # matched stack entry is removed only once across the whole document.
        for removed_tag, _ in self.stack[index:]:
            opened = self._tag_positions[removed_tag]
            opened.pop()
            if not opened:
                del self._tag_positions[removed_tag]
        del self.stack[index:]

    def _finish_capture(self):
        if self.capture is not None and self.current is not None:
            self.current[self.capture[0]] = re.sub(r'\s+', ' ', ''.join(self.chunks)).strip()[:2000]
        self.capture, self.chunks = None, []
        self.capture_chars = 0

    def handle_data(self, data):
        if self.capture is not None and not (self.stack and self.stack[-1][1]):
            value = data[:max(0, 4000 - self.capture_chars)]
            if value:
                self.chunks.append(value)
                self.capture_chars += len(value)

    def close(self):
        super().close()
        self._finish_capture()


def parse_public_results(fetch_result, source):
    if fetch_result.get('status') not in {'ok', 'not_modified'}:
        return []
    body = fetch_result.get('body')
    if not isinstance(body, str) or not body.strip() or len(body) > 2_000_000:
        raise PublicSearchParseError('Respuesta pública vacía o demasiado grande; descubrimiento no completado')
    kind = source.get('kind')
    limit = min(100, max(1, int(source.get('max_records', 50))))
    base = fetch_result.get('final_url') or fetch_result.get('url') or source.get('url', '')
    if kind == 'arquivo_fulltext':
        from .archive_text_search import records as archive_records
        records = archive_records(fetch_result, source)
    elif kind == 'bing_news_rss':
        records = _bing_news_records(body)
    elif kind in {'duckduckgo_html', 'duckduckgo_lite'}:
        parser = _DuckResults(base)
        parser.feed(body)
        parser.close()
        if parser.challenge:
            raise PublicSearchParseError('El buscador devolvió una verificación; no se elude ni se alterna el endpoint',
                                         category='access_challenge')
        if not parser.records and not (parser.structure and parser.no_results):
            raise PublicSearchParseError('HTML no reconocido como resultados; no se presenta como búsqueda vacía')
        records = parser.records
    else:
        try:
            payload = json.loads(body)
        except (ValueError, RecursionError) as exc:
            raise PublicSearchParseError('El buscador devolvió JSON inválido') from exc
        if isinstance(payload, dict) and (payload.get('error') or payload.get('errors')
                or payload.get('success') is False or str(payload.get('status', '')).lower() in {'error', 'failed'}):
            raise PublicSearchParseError('El buscador informó una falla al consultar sus resultados',
                                         category='upstream_unavailable')
        if not isinstance(payload, dict) or not isinstance(payload.get('results'), list):
            raise PublicSearchParseError('El buscador no devolvió una lista de resultados válida')
        records = payload['results']
        if kind == 'mwmbl' and (not isinstance(payload.get('query'), str)
                               or type(payload.get('number_of_results')) is not int
                               or payload['number_of_results'] != len(records)):
            raise PublicSearchParseError('Respuesta Mwmbl incompatible o incompleta')
        if kind == 'searxng' and payload.get('unresponsive_engines') and not records:
            raise PublicSearchParseError('SearXNG no pudo consultar sus motores; no equivale a cero resultados',
                                         category='upstream_unavailable')
    result, seen, rejected = [], set(), 0
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get('title'), str):
            raise PublicSearchParseError('Registro de buscador inválido')
        url = safe_result_url(record.get('url'))
        if not url:
            rejected += 1
            continue
        if url in seen:
            continue
        seen.add(url)
        if len(result) >= limit:
            continue
        excerpt = record.get('text', record.get('content', ''))
        if not isinstance(excerpt, str):
            raise PublicSearchParseError('Extracto de buscador inválido')
        try:
            record['title'].encode('utf-8')
            excerpt.encode('utf-8')
        except UnicodeError as exc:
            raise PublicSearchParseError('El buscador devolvió texto Unicode inválido') from exc
        result.append({'url': url, 'title': record['title'][:2000], 'text': excerpt[:2000],
            'published_at': None, 'source_kind': 'discovery', 'source_id': source.get('id', kind),
            'metadata': {'discovery_only': True, 'public_web_search': True, 'search_provider': kind,
                         'publication_date_unknown': True, 'score_effect': 'none'}})
        if kind == 'bing_news_rss':
            result[-1]['metadata'].update(provider_link=record['provider_link'],
                provider_publication_date=record['provider_publication_date'],
                provider_date_unverified=True, explicit_destination_unwrapped=True)
        if kind == 'arquivo_fulltext':
            result[-1]['metadata'].update(record['archive_metadata'])
    fetch_result['parse_metadata'] = {'observed_entries': len(records), 'truncated': len(seen) > limit,
        'excluded_unsafe_or_non_https_urls': rejected, 'scope': 'public_web_top_results',
        'publication_dates': 'unknown', 'complete_archive': False, 'discovery_only': True}
    if kind == 'searxng':
        fetch_result['parse_metadata']['partial_upstream_response'] = bool(payload.get('unresponsive_engines'))
    if kind == 'arquivo_fulltext':
        fetch_result['parse_metadata'].update(fetch_result.pop('_archive_parse_metadata', {}))
    return result


def public_search_links(entity):
    query = _query(entity)
    links = [{'label': label, 'url': base + urlencode({'q': query}), 'channel': 'web'} for label, base in (
        ('Google', 'https://www.google.com/search?'), ('Bing', 'https://www.bing.com/search?'),
        ('DuckDuckGo', 'https://duckduckgo.com/?'), ('Mwmbl', 'https://mwmbl.org/?'))]
    from .social_discovery import external_social_search_links
    links.extend({'label': row['label'], 'url': row['url'], 'channel': 'social'}
                 for row in external_social_search_links(entity))
    return {'links': links, 'note': 'Se abren en tu navegador. No se descarga ni puntúa contenido desde estos enlaces; algunas plataformas requieren iniciar sesión.'}
