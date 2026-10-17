"""Conservative text-availability checks; never infer an incident or identity.

A playable native media element inside the editorial body, paired with only
a short invitation/introduction, is an archived multimedia reference. This
does not transcribe, fetch or evaluate the media and does not reject ordinary
short factual reporting, or developed stories that also carry an audio player.
"""
from collections import Counter
import gzip
from html.parser import HTMLParser
import json
import re

from .store import canonical, digest, now

BODY_QUALITY_VERSION = 'editorial-body-quality-1'
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


def classify_body(html, text):
    """Check only multimedia introductions; the caller enforces its text minimum."""
    result = {'version': BODY_QUALITY_VERSION, 'status': 'full_article',
              'body_usable': True, 'text_chars': len(text), 'media_types': [],
              'reason': None}
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


def quarantine_reference(news, article_id, quality, snapshot, *, expected_document_id):
    """Detach current evidence atomically; preserve documents, bytes and history."""
    with news.store.connection() as con:
        con.execute('BEGIN IMMEDIATE')
        row = con.execute('SELECT document_id,scope FROM news_articles WHERE id=?', (article_id,)).fetchone()
        if row is None or row['document_id'] != expected_document_id:
            return {'status': 'concurrent_skip'}
        con.execute("UPDATE news_articles SET document_id=NULL,scope='multimedia_reference',"
                    "status='multimedia_reference',error=?,attributed_signature=NULL,"
                    'retrieved_at=COALESCE(?,retrieved_at),validated_at=COALESCE(?,validated_at) WHERE id=?',
                    (quality['reason'], snapshot.get('retrieved_at'), snapshot.get('retrieved_at'), article_id))
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
        news.store._audit(con, 'news_multimedia_reference_quarantined',
                          dict(article_id=article_id, previous_document_id=expected_document_id,
                               quality=quality, **snapshot))
    return {'status': 'multimedia_reference'}


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
