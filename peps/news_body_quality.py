"""Conservative text-availability checks; never infer an incident or identity.

A playable native media element inside the editorial body, paired with only
a short invitation/introduction, is an archived multimedia reference. This
does not transcribe, fetch or evaluate the media and does not reject ordinary
short factual reporting, or developed stories that also carry an audio player.
Explicit professional profiles are references; exact copies retain separate
capture provenance while pointing at one immutable canonical body version.
"""
from collections import Counter
from contextlib import nullcontext
import gzip
from html.parser import HTMLParser
import json
import re
from urllib.parse import urlsplit

from .store import canonical, digest, now

BODY_QUALITY_VERSION = 'editorial-body-quality-2'
REFERENCE_SCOPES = frozenset({'multimedia_reference', 'profile_reference', 'duplicate_reference'})
_VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link',
         'meta', 'param', 'source', 'track', 'wbr'}
_EDITORIAL = {'entry-content', 'post-content', 'article-content', 'article-body',
              'story-body', 'td-post-content', 'elementor-widget-theme-post-content', 'et_pb_post_content'}
_INTRO = re.compile(r'\b(?:nos\s+(?:habla|cuenta|comenta)\s+(?:sobre|acerca\s+de)|'
                    r'(?:escucha|escuche|escuchar|reproduce|reproduzca)\b[^.!?]{0,100}'
                    r'\b(?:audio|entrevista|v[ií]deo)|'
                    r'(?:audio|v[ií]deo|entrevista)\s+(?:complet[oa]|disponible))\b', re.I)
_REASON = ('Referencia multimedia sin transcripción: el texto conservado sólo '
           'presenta el contenido del reproductor y no acredita un cuerpo informativo.')
_PROFILE_REASON = ('Ficha de perfil o directorio: se conserva su procedencia, '
                   'pero no se cuenta como una noticia ni como evidencia de un incidente.')


class _PageKind(HTMLParser):
    """Read page-level declarations, never author/sidebar Person objects."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.profile = self.news = False
        self.structured_types, self.signals = set(), set()
        self._json = None

    def handle_starttag(self, tag, attrs):
        attrs = {key: value or '' for key, value in attrs}
        if tag == 'script' and attrs.get('type', '').split(';', 1)[0].lower() == 'application/ld+json':
            self._json = []
        if tag == 'body':
            classes = set(attrs.get('class', '').lower().split())
            if classes & {'single-perfil', 'single-profile', 'single-directory',
                          'perfil-template-default', 'profile-template-default'}:
                self.signals.add('page_profile_post_type')
        if tag == 'meta' and attrs.get('property', '').lower() == 'og:type':
            if attrs.get('content', '').lower() == 'profile':
                self.signals.add('open_graph_profile')
            if attrs.get('content', '').lower() == 'newsarticle':
                self.news = True

    def handle_data(self, data):
        if self._json is not None:
            self._json.append(data)

    def handle_endtag(self, tag):
        if tag != 'script' or self._json is None:
            return
        raw, self._json = ''.join(self._json), None
        if len(raw) > 1_000_000:
            return
        try:
            value = json.loads(raw)
        except (ValueError, TypeError):
            return
        # Only top-level graph nodes describe the page. Descending into author,
        # publisher, mentions or related links would mistake ordinary reporting
        # for the professional biography of its author or subject.
        nodes = value if isinstance(value, list) else [value]
        for node in list(nodes):
            if isinstance(node, dict) and isinstance(node.get('@graph'), list):
                nodes.extend(node['@graph'])
        for node in nodes:
            if not isinstance(node, dict):
                continue
            kinds = node.get('@type') or []
            kinds = [kinds] if isinstance(kinds, str) else kinds
            if not isinstance(kinds, list):
                continue
            kinds = {str(kind).rsplit('/', 1)[-1] for kind in kinds}
            self.structured_types.update(kinds)
            self.news |= bool(kinds & {'NewsArticle', 'ReportageNewsArticle',
                                        'AnalysisNewsArticle', 'OpinionNewsArticle'})
            main = node.get('mainEntity')
            if 'ProfilePage' in kinds and isinstance(main, dict):
                main_kinds = main.get('@type', [])
                main_kinds = [main_kinds] if isinstance(main_kinds, str) else main_kinds
                if isinstance(main_kinds, list) and any(str(kind).rsplit('/', 1)[-1] in
                                                       ('Person', 'Organization') for kind in main_kinds):
                    self.profile = True
                    self.signals.add('profile_page_main_entity')


def profile_page_signals(html, url=''):
    """Conservative structural separation; names and allegation polarity unused."""
    parser = _PageKind()
    parser.feed(html)
    if parser.news:
        return []
    profile_path = bool(re.search(r'/(?:perfil|perfiles|profile|profiles|directorio|directory)/',
                                  urlsplit(str(url)).path.lower()))
    if parser.profile or profile_path and parser.signals:
        return sorted(parser.signals)
    return []


def _prose(text):
    return '\n\n'.join(part.strip() for part in str(text or '').split('\n\n')
                       if not re.fullmatch(r'\s*(?:audio|v[ií]deo|escuchar|reproducir)\s*', part, re.I)).strip()


def potential_multimedia_intro(text):
    prose = _prose(text)
    return bool(prose and len(prose) <= 400 and len(re.findall(r'\w+', prose)) <= 70
                and len([part for part in re.split(r'[.!?]+(?:\s|$)', prose) if part.strip()]) <= 2
                and _INTRO.search(prose))


class _EditorialMedia(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.editorial, self.articles = [], [], []

    def handle_starttag(self, tag, attrs):
        attrs = {key: value if value is not None else '' for key, value in attrs}
        parent = self.stack[-1][1] if self.stack else (False, None, None, None)
        classes = set(attrs.get('class', '').lower().split())
        hidden = (parent[0] or tag in ('head', 'script', 'style', 'template', 'nav',
                                      'aside', 'header', 'footer', 'noscript', 'form', 'svg')
                  or 'hidden' in attrs or attrs.get('aria-hidden') == 'true')
        if tag not in ('html', 'body'):
            navigation_classes = classes
            if tag == 'article' and 'has-sidebar' in classes:
                navigation_classes = classes - {'has-sidebar', 'sidebar-left', 'sidebar-right'}
            hidden = hidden or bool(re.search(r'\b(?:advert|related|social-share|cookie|menu|sidebar)\b',
                                               ' '.join(navigation_classes), re.I))
        article, editorial, media = parent[1:]
        if tag == 'article' and not hidden:
            article = {'media': set(), 'transcript': False}
            self.articles.append(article)
        explicit = ('articleBody' in attrs.get('itemprop', '').split()
                    or bool(classes & _EDITORIAL)
                    or attrs.get('data-widget_type', '').split('.', 1)[0] == 'theme-post-content')
        if explicit and editorial is None and not hidden:
            editorial = {'media': set(), 'transcript': False}
            self.editorial.append(editorial)
        scope = editorial if editorial is not None else article
        if tag in ('audio', 'video') and not hidden:
            media = {'kind': tag, 'scope': scope}
            if attrs.get('src') and scope is not None:
                scope['media'].add(tag)
        if tag == 'source' and attrs.get('src') and media is not None and not hidden:
            if media['scope'] is not None:
                media['scope']['media'].add(media['kind'])
        transcript = ('transcript' in attrs.get('itemprop', '').split()
                      or bool(classes & {'transcript', 'transcription'}))
        if tag not in _VOID:
            self.stack.append((tag, (hidden, article, editorial, media), transcript))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                return

    def handle_data(self, value):
        if not value.strip() or not self.stack or self.stack[-1][1][0]:
            return
        for _tag, state, transcript in reversed(self.stack):
            if transcript:
                scope = state[2] if state[2] is not None else state[1]
                if scope is not None:
                    scope['transcript'] = True
                return


def classify_body(html, text, url=''):
    """Separate explicitly declared profiles and untranscribed introductions."""
    result = {'version': BODY_QUALITY_VERSION, 'status': 'full_article',
              'body_usable': True, 'text_chars': len(text), 'media_types': [],
              'reason': None}
    signals = profile_page_signals(html, url)
    if signals:
        result.update(status='profile_reference', body_usable=False,
                      structural_signals=signals, reason=_PROFILE_REASON)
        return result
    if not potential_multimedia_intro(text):
        return result
    parser = _EditorialMedia()
    parser.feed(html)
    scopes = parser.editorial if parser.editorial else parser.articles
    media = set().union(*(scope['media'] for scope in scopes)) if scopes else set()
    if media and not any(scope['transcript'] for scope in scopes):
        result.update(status='multimedia_reference', body_usable=False,
                      media_types=sorted(media), reason=_REASON)
    return result


def quarantine_reference(news, article_id, quality, snapshot, *, expected_document_id, connection=None):
    """Detach current evidence atomically; preserve documents, bytes and history."""
    with (nullcontext(connection) if connection is not None else news.store.connection()) as con:
        if not con.in_transaction:
            con.execute('BEGIN IMMEDIATE')
        row = con.execute('SELECT document_id,scope FROM news_articles WHERE id=?', (article_id,)).fetchone()
        if row is None or row['document_id'] != expected_document_id:
            return {'status': 'concurrent_skip'}
        status = quality.get('status', 'multimedia_reference')
        if status not in REFERENCE_SCOPES:
            raise ValueError('Invalid body reference classification')
        if status == 'duplicate_reference':
            _record_body_alias(con, article_id, quality, snapshot)
        con.execute("UPDATE news_articles SET document_id=NULL,scope=?,"
                    "status=?,error=?,attributed_signature=NULL,"
                    'retrieved_at=COALESCE(?,retrieved_at),validated_at=COALESCE(?,validated_at),'
                    'fingerprint=COALESCE(?,fingerprint),content_cluster=COALESCE(?,content_cluster),'
                    'topic_id=COALESCE(?,topic_id) WHERE id=?',
                    (status, status, quality['reason'], snapshot.get('retrieved_at'), snapshot.get('retrieved_at'),
                     quality.get('fingerprint'), quality.get('fingerprint'), quality.get('topic_id'), article_id))
        con.execute('INSERT INTO news_body_quality_receipts VALUES(?,?,?,?,?,?,?) '
                    'ON CONFLICT(article_id) DO UPDATE SET previous_document_id=excluded.previous_document_id,'
                    'version=excluded.version,raw_snapshot_sha256=excluded.raw_snapshot_sha256,'
                    'raw_snapshot_path=excluded.raw_snapshot_path,classification=excluded.classification,'
                    'checked_at=excluded.checked_at',
                    (article_id, expected_document_id, BODY_QUALITY_VERSION, snapshot['raw_snapshot_sha256'],
                     snapshot['raw_snapshot_path'], canonical(quality), now()))
        if expected_document_id:
            news._retire_replaced_document(con, expected_document_id, None, article_id,
                                           reason=quality['reason'])
        # The retired body may be the only place a current name appeared.
        con.execute("DELETE FROM meta WHERE key='news_projection_ready'")
        con.execute("UPDATE meta SET value=CAST(value AS INTEGER)+1 WHERE key='news_data_revision'")
        action = ('news_multimedia_reference_quarantined' if status == 'multimedia_reference'
                  else 'news_' + status + '_conserved')
        news.store._audit(con, action,
                          dict(article_id=article_id, previous_document_id=expected_document_id,
                               quality=quality, **snapshot))
    return {'status': status}


def _record_body_alias(con, article_id, quality, snapshot):
    """Append capture provenance tied to an immutable document *version*."""
    con.execute('CREATE TABLE IF NOT EXISTS news_body_aliases('
                'id TEXT PRIMARY KEY,article_id TEXT NOT NULL REFERENCES news_articles(id),'
                'canonical_document_id TEXT NOT NULL REFERENCES documents(id),body_sha256 TEXT NOT NULL,'
                'raw_snapshot_sha256 TEXT NOT NULL,raw_snapshot_path TEXT NOT NULL,'
                'observed_url TEXT NOT NULL,retrieved_at TEXT,source_policy TEXT NOT NULL,'
                'classification TEXT NOT NULL,created_at TEXT NOT NULL)')
    identity = digest(canonical([article_id, quality['canonical_document_id'], snapshot]))
    classification = dict(quality, capture_metadata={key: value for key, value in snapshot.items()
        if key not in ('raw_snapshot_sha256', 'raw_snapshot_path', 'source_policy', 'observed_url', 'retrieved_at')})
    con.execute('INSERT OR IGNORE INTO news_body_aliases VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (identity, article_id, quality['canonical_document_id'], quality['body_sha256'],
                 snapshot['raw_snapshot_sha256'], snapshot['raw_snapshot_path'], snapshot['observed_url'],
                 snapshot.get('retrieved_at'), canonical(snapshot.get('source_policy') or {}),
                 canonical(classification), now()))


def quarantine_existing_references(news):
    """Scoring/attribution preflight over verified local captures; no network."""
    counts = Counter()
    with news.store.connection() as con:
        rows = con.execute("SELECT n.id,n.document_id,d.text,d.metadata FROM news_articles n "
                           "JOIN documents d ON d.id=n.document_id WHERE n.scope='full_article' "
                           'AND length(d.text)<=420').fetchall()
    root = (news.engine.data_dir / 'news_snapshots').resolve()
    for row in rows:
        if not potential_multimedia_intro(row['text']):
            continue
        counts['examined'] += 1
        metadata = json.loads(row['metadata'])
        expected, relative = metadata.get('raw_snapshot_sha256'), metadata.get('raw_snapshot_path')
        try:
            if not isinstance(expected, str) or not re.fullmatch('[0-9a-f]{64}', expected) or not isinstance(relative, str):
                raise ValueError('Missing captured HTML provenance')
            path = (news.engine.data_dir / relative).resolve()
            if not path.is_relative_to(root) or path.name != expected + '.html.gz':
                raise ValueError('Invalid captured HTML path')
            with gzip.open(path, 'rb') as stream:
                raw = stream.read(20_000_001)
            if len(raw) > 20_000_000:
                raise ValueError('Captured HTML too large')
            html = raw.decode('utf-8')
            if digest(html) != expected:
                raise ValueError('Captured HTML hash mismatch')
        except (OSError, EOFError, ValueError, UnicodeError):
            counts['unverified_capture'] += 1
            continue
        quality = classify_body(html, row['text'])
        if not quality['body_usable']:
            result = quarantine_reference(news, row['id'], quality,
                       {'raw_snapshot_sha256': expected, 'raw_snapshot_path': relative},
                       expected_document_id=row['document_id'])
            counts[result['status']] += 1
    return dict(counts)
