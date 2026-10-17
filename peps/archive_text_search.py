"""Arquivo.pt full-text search discovers original URLs, never archived bodies."""
from datetime import datetime, timezone
from html.parser import HTMLParser
import json
import threading
import time
from urllib.parse import urlencode, urlsplit

ENDPOINT = 'https://arquivo.pt/textsearch'
SESSION_LIMIT = ('Límite local del complemento histórico alcanzado; las consultas '
                 'pendientes se conservan para otra sesión.')


class SessionBudget:
    """One shared archive window per session, including concurrent sources."""
    def __init__(self, seconds, session_deadline, clock=None):
        self.seconds, self.session_deadline = seconds, session_deadline
        self.clock = clock or time.monotonic
        self._lock, self._deadline = threading.Lock(), None

    def deadline(self):
        with self._lock:
            if self._deadline is None:
                self._deadline = min(self.session_deadline, self.clock() + self.seconds * .1)
            return self._deadline


class _Snippet(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden += 1
        elif tag in ('br', 'p', 'div'):
            self.parts.append(' ')

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.hidden = max(0, self.hidden - 1)
        elif tag in ('p', 'div'):
            self.parts.append(' ')

    def handle_data(self, text):
        if not self.hidden:
            self.parts.append(text)


def _text(value):
    parser = _Snippet()
    parser.feed(value)
    parser.close()
    return ' '.join(''.join(parser.parts).split())


def search_url(query, source):
    from .public_search import _query
    if (source.get('url') or ENDPOINT) != ENDPOINT:
        raise ValueError('Arquivo requiere su endpoint público exacto de búsqueda de metadatos')
    limit = source.get('max_records', 50)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('Arquivo max_records requiere un entero entre 1 y 100')
    # No capture-date parameters: a capture is not a publication date. Search
    # the available archive; the normal acquisition checks editorial dates.
    return ENDPOINT + '?' + urlencode({'q': _query(query), 'maxItems': limit,
        'fields': 'title,originalURL,tstamp,snippet,digest,mimeType,collection'})


def records(fetch_result, source):
    from .public_search import PublicSearchParseError, safe_result_url

    def fail(message):
        raise PublicSearchParseError('Arquivo: ' + message)

    def unique(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError('Duplicate JSON key')
            value[key] = item
        return value

    def reject_constant(_value):
        raise ValueError('Invalid JSON constant')

    try:
        payload = json.loads(fetch_result['body'], object_pairs_hook=unique,
                             parse_constant=reject_constant)
    except (ValueError, RecursionError, KeyError) as exc:
        raise PublicSearchParseError('Arquivo devolvió JSON inválido') from exc
    if (not isinstance(payload, dict) or payload.get('error') or payload.get('errors')
            or not isinstance(payload.get('response_items'), list)
            or len(payload['response_items']) > 500
            or not isinstance(payload.get('request_parameters'), dict)
            or not isinstance(payload['request_parameters'].get('q'), str)):
        fail('respuesta de búsqueda incompleta o incompatible')
    expected = urlsplit(fetch_result.get('url') or search_url(payload['request_parameters'].get('q'), source))
    from urllib.parse import parse_qs
    if payload['request_parameters'].get('q') != parse_qs(expected.query).get('q', [None])[0]:
        fail('la consulta devuelta no coincide con la solicitud')
    result, seen, unsafe, duplicates = [], {}, 0, 0
    fields = ('originalURL', 'title', 'snippet', 'tstamp', 'digest', 'mimeType', 'collection')
    for item in payload['response_items']:
        if (not isinstance(item, dict) or any(not isinstance(item.get(key), str) for key in fields)
                or any(len(item[key]) > 20000 for key in fields)):
            fail('registro de metadatos inválido')
        try:
            if len(item['tstamp']) != 14 or not item['tstamp'].isascii() or not item['tstamp'].isdigit():
                raise ValueError('Invalid capture timestamp')
            captured = datetime.strptime(item['tstamp'], '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc).isoformat()
        except ValueError as exc:
            raise PublicSearchParseError('Arquivo devolvió una fecha de captura inválida') from exc
        url = safe_result_url(item['originalURL'])
        if not url:
            unsafe += 1
            continue
        capture = {'captured_at': captured, 'digest': item['digest'],
                   'collection': item['collection'], 'mime_type': item['mimeType']}
        if url in seen:
            seen[url]['archive_metadata']['archive_captures'].append(capture)
            duplicates += 1
            continue
        record = {'url': url, 'title': _text(item['title']), 'text': _text(item['snippet']),
            'archive_metadata': {'archive_provider': 'arquivo.pt', 'archive_metadata_only': True,
                'archive_original_url': url, 'archive_captures': [capture],
                'capture_date_is_not_publication_date': True}}
        seen[url] = record
        result.append(record)
    fetch_result['_archive_parse_metadata'] = {'archive_metadata_only': True,
        'raw_archive_captures': len(payload['response_items']), 'duplicate_archive_captures': duplicates,
        'excluded_unsafe_or_non_https_originals': unsafe, 'archive_complete': False,
        'capture_dates_are_not_publication_dates': True}
    return result
