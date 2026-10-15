"""News acquisition is independent of offline identity attribution.

Acquire feeds once, batch historical discovery, fetch each canonical URL once
per refresh window. Preserve distinct publishers even for identical syndication.
Identity decisions and adverse conduct remain separate, auditable operations.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import re
import threading
import time
import os
import tempfile
import unicodedata
from collections import Counter
from contextlib import nullcontext
from itertools import islice
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from .store import canonical, digest, now

TRACKING = {'fbclid', 'gclid', 'dclid', 'msclkid', 'mc_cid', 'mc_eid', '_ga', 'igshid'}
UNRESERVED = frozenset('ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~')


def source_policy(source):
    """Public acquisition policy at observation time; omit transport secrets."""
    fields = ('id', 'publisher_id', 'kind', 'url', 'base_url', 'allowed_hosts', 'article_allowed_hosts',
              'enabled', 'terms_approved', 'download_articles', 'article_terms_approved', 'body_terms_approved',
              'terms_url', 'license', 'review_note', 'min_interval_seconds', 'refresh_seconds')
    return {key: source[key] for key in fields if key in source}


def canonical_url(url):
    """Remove tracking/fragment only. Preserve semantic query, scheme and slash."""
    if not isinstance(url, str) or any(ord(c) < 33 for c in url):
        raise ValueError('URL inválida')
    parts = urlsplit(url)
    if parts.scheme.lower() not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
        raise ValueError('URL pública HTTP(S) sin credenciales requerida')
    host = parts.hostname.rstrip('.').encode('idna').decode('ascii').lower()
    port = parts.port
    if port and port != (443 if parts.scheme.lower() == 'https' else 80):
        host += ':' + str(port)
    if ':' in parts.hostname and not host.startswith('['):
        host = '[' + parts.hostname.lower() + ']' + (':' + str(port) if port else '')
    def decode_unreserved(match):
        char = chr(int(match[1], 16))
        return char if char in UNRESERVED else '%' + match[1].upper()
    path = quote(re.sub(r'%([0-9a-fA-F]{2})', decode_unreserved, parts.path or '/'), safe="/%:@!$&'()*+,;=-._~")
    pairs = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.casefold().startswith('utm_') and k.casefold() not in TRACKING]
    # Repeated keys can be order-sensitive; never reorder a signed URL.
    protected = any(k.casefold() in ('signature', 'sig', 'token', 'x-amz-signature') for k, _ in pairs)
    query = parts.query if protected else urlencode(pairs, doseq=True)
    return urlunsplit((parts.scheme.lower(), host, path, query, ''))


class _ArticleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.all, self.main, self.article = [], [], [], []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        # HTML permits </head> to be omitted before visible body content.
        if (self.stack and self.stack[-1][0] == 'head'
                and tag not in ('base', 'link', 'meta', 'title', 'noscript', 'style', 'script', 'template')):
            self.stack.pop()
        hidden = tag in ('head', 'title', 'template', 'iframe', 'script', 'style', 'nav',
                         'aside', 'footer', 'header', 'form', 'noscript', 'svg')
        hidden = hidden or attrs.get('aria-hidden') == 'true' or 'hidden' in attrs
        hidden = hidden or bool(re.search(r'\b(?:advert|related|social-share|cookie|menu|sidebar)\b', attrs.get('class', ''), re.I))
        parent = self.stack[-1][1] if self.stack else (False, False, None)
        article = parent[2]
        if tag == 'article' or attrs.get('itemprop') == 'articleBody':
            article = len(self.article)
            self.article.append([])
        state = (hidden or parent[0], parent[1] or tag == 'main', article)
        if tag not in ('meta', 'link', 'img', 'br', 'hr', 'input', 'source', 'wbr', 'area', 'base', 'embed', 'param', 'track'):
            self.stack.append((tag, state))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                return

    def handle_data(self, value):
        state = self.stack[-1][1] if self.stack else (False, False, None)
        if not state[0] and value.strip():
            self.all.append(value.strip())
            if state[1]:
                self.main.append(value.strip())
            if state[2] is not None:
                self.article[state[2]].append(value.strip())


def article_text(html):
    parser = _ArticleText()
    parser.feed(html)
    # A tiny <article> card is not sufficient to discard the actual main text.
    article = max(parser.article, key=lambda parts: len(' '.join(parts)), default=[])
    chosen = article if len(' '.join(article)) >= 100 else parser.main if parser.main else parser.all
    return re.sub(r'\s+', ' ', ' '.join(chosen)).strip()


def news_fingerprint(text):
    """Exact normalized text fingerprint; cross-publisher content is retained."""
    import unicodedata
    value = unicodedata.normalize('NFC', text).casefold()
    return digest(' '.join(re.findall(r'\w+', value)))


def batch_queries(entities, max_terms=6, max_chars=500, language='SPANISH', country=None):
    """Local request budget, not an assertion about an undocumented API limit."""
    names = {}
    for entity in entities:
        # One nominal query per shared name even when several homonyms exist.
        variants = [entity['name'], *entity.get('aliases', [])]
        context = entity.get('context') or {}
        if isinstance(context, dict):
            for field in ('verified_aliases', 'trade_names', 'business_names', 'legal_name', 'legal_names', 'abbreviations'):
                values = context.get(field) or []
                if field == 'verified_aliases' and isinstance(values, dict):
                    values = [dict(value, name=name) if isinstance(value, dict) else {'name': name}
                              for name, value in values.items()]
                variants.extend(values if isinstance(values, list) else [values])
        for name in variants:
            if isinstance(name, dict):
                if (name.get('validated') is False or name.get('verified') is False
                        or str(name.get('status', '')).casefold() in ('rejected', 'revoked')):
                    continue
                name = name.get('name') or name.get('value') or name.get('alias')
            if not isinstance(name, str):
                continue
            name = ' '.join(str(name).replace('"', ' ').split())
            if name:
                names.setdefault(name.casefold(), {'name': name, 'entity_ids': set()})['entity_ids'].add(entity['id'])
    suffix = ' sourcelang:' + language if language else ''
    if country:
        suffix += ' sourcecountry:' + str(country)
    batch = []
    def output(items):
        terms = ['"' + item['name'] + '"' for item in items]
        return {'query': ('(' + ' OR '.join(terms) + ')' if len(terms) > 1 else terms[0]) + suffix,
                'entity_ids': sorted({i for item in items for i in item['entity_ids']}),
                'names': [item['name'] for item in items]}
    for key in sorted(names):
        item = names[key]
        candidate = [*batch, item]
        if batch and (len(candidate) > max_terms or len(output(candidate)['query']) > max_chars):
            yield output(batch)
            batch = []
        if len(output([item])['query']) > max_chars:
            yield dict(output([item]), unqueryable=True, error='Nombre excede el presupuesto local de longitud')
        else:
            batch.append(item)
    if batch:
        yield output(batch)


class NewsAcquirer:
    def __init__(self, engine, config_path=None):
        self.engine, self.store = engine, engine.store
        self.config_path = Path(config_path) if config_path else engine.data_dir / 'news_sources.json'
        if not self.config_path.exists():
            default = Path(__file__).parent / 'defaults' / 'news_sources.json'
            config = json.loads(default.read_text(encoding='utf-8')) if default.exists() else {'sources': []}
            self.config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
        with self.store.connection() as con:
            con.executescript('''
              CREATE TABLE IF NOT EXISTS news_articles(
                id TEXT PRIMARY KEY,canonical_url TEXT UNIQUE NOT NULL,publisher_id TEXT NOT NULL,
                source_id TEXT NOT NULL,title TEXT NOT NULL,summary TEXT NOT NULL,published_at TEXT,
                discovered_at TEXT NOT NULL,retrieved_at TEXT,document_id TEXT REFERENCES documents(id),
                fingerprint TEXT,topic_id TEXT,content_cluster TEXT,scope TEXT NOT NULL,
                status TEXT NOT NULL,error TEXT NOT NULL DEFAULT '',attributed_signature TEXT);
              CREATE INDEX IF NOT EXISTS news_content ON news_articles(publisher_id,fingerprint);
              CREATE TABLE IF NOT EXISTS news_publisher_content(publisher_id TEXT NOT NULL,fingerprint TEXT NOT NULL,
                document_id TEXT NOT NULL REFERENCES documents(id),PRIMARY KEY(publisher_id,fingerprint));
              CREATE INDEX IF NOT EXISTS news_topic ON news_articles(topic_id);
              CREATE TABLE IF NOT EXISTS news_observations(
                id TEXT PRIMARY KEY,article_id TEXT NOT NULL REFERENCES news_articles(id),source_id TEXT NOT NULL,
                discovery_url TEXT NOT NULL,observed_url TEXT NOT NULL,retrieved_at TEXT NOT NULL,
                discovery_hash TEXT,details TEXT NOT NULL);
              CREATE INDEX IF NOT EXISTS news_observation_article ON news_observations(article_id);
              CREATE TABLE IF NOT EXISTS news_query_windows(
                job_id TEXT PRIMARY KEY,source_id TEXT NOT NULL,entity_ids TEXT NOT NULL,query TEXT NOT NULL,
                window_start TEXT,window_end TEXT,status TEXT NOT NULL,records INTEGER NOT NULL DEFAULT 0,
                saturated INTEGER NOT NULL DEFAULT 0,parent_id TEXT,error TEXT NOT NULL DEFAULT '');
              CREATE TABLE IF NOT EXISTS news_topic_index(
                token TEXT NOT NULL,article_id TEXT NOT NULL REFERENCES news_articles(id),PRIMARY KEY(token,article_id));
              CREATE TABLE IF NOT EXISTS news_discovery_runs(job_id TEXT PRIMARY KEY,source_id TEXT NOT NULL,
                kind TEXT NOT NULL,url TEXT NOT NULL,records INTEGER NOT NULL,parser_metadata TEXT NOT NULL,
                content_hash TEXT,retrieved_at TEXT NOT NULL);
            ''')

    def config(self):
        value = json.loads(self.config_path.read_text(encoding='utf-8'))
        if not isinstance(value, dict) or not isinstance(value.get('sources'), list):
            raise ValueError('Configuración de noticias requiere sources: lista')
        return value

    def sources(self):
        return self.config()['sources']

    def set_source(self, source_id, enabled, terms_approved, download_articles=False, body_terms_approved=False):
        config = self.config()
        source = next((s for s in config['sources'] if s['id'] == source_id), None)
        if source is None:
            raise ValueError('Fuente de noticias inexistente')
        source.update(enabled=bool(enabled), terms_approved=bool(terms_approved),
                      download_articles=bool(download_articles), article_terms_approved=bool(body_terms_approved))
        self.config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
        self.store.audit('news_source_configured', {'id': source_id, 'config_hash': digest(canonical(source))})

    def queue(self, window=None, start=None, end=None):
        window = window or now()[:10]
        queued, query_count = 0, 0
        for source in self.sources():
            if not source.get('enabled') or not source.get('terms_approved'):
                continue
            kind = source.get('kind')
            if kind in ('rss', 'atom'):
                urls = source.get('urls') or [source.get('url') or source.get('base_url')]
                for url in urls:
                    if not url:
                        continue
                    self.store.enqueue('news_feed', {'source_id': source['id'], 'url': url, 'window': window},
                                       key='news_feed:' + source['id'] + ':' + str(url) + ':' + window)
                    queued += 1
            elif kind == 'gdelt':
                query_start, query_end = start, end
                if not query_start and not query_end:
                    anchor_key = 'news_window:' + source['id'] + ':' + window
                    with self.store.connection() as con:
                        previous = con.execute('SELECT value FROM meta WHERE key=?', (anchor_key,)).fetchone()
                        if previous:
                            query_start, query_end = json.loads(previous[0])
                        else:
                            current = datetime.now(timezone.utc).replace(microsecond=0)
                            span = re.fullmatch(r'(\d+)([dhw])', source.get('timespan', '7d'))
                            days = int(span[1]) * (7 if span[2] == 'w' else 1 / 24 if span[2] == 'h' else 1) if span else 7
                            query_start, query_end = [(current - timedelta(days=min(90, days))).strftime('%Y%m%d%H%M%S'), current.strftime('%Y%m%d%H%M%S')]
                            con.execute('INSERT INTO meta VALUES(?,?)', (anchor_key, canonical([query_start, query_end])))
                batches = batch_queries(self.store.iter_entities(), int(source.get('batch_names', source.get('batch_size', 6))),
                                        int(source.get('max_query_chars', 500)), source.get('language', source.get('source_lang', 'SPANISH')),
                                        source.get('source_country'))
                anchor = time.time()
                with self.store.connection() as con:
                    con.execute('BEGIN IMMEDIATE')
                    for i, batch in enumerate(batches):
                        payload = dict(batch, source_id=source['id'], window=window, start=query_start, end=query_end)
                        key = 'news_query:' + source['id'] + ':' + digest(canonical([batch['query'], query_start, query_end, window]))
                        job_id = self.store.enqueue('news_query', payload, key=key,
                                                   due=anchor + i * max(1, float(source.get('min_interval_seconds', 5))), connection=con)
                        con.execute('INSERT OR IGNORE INTO news_query_windows VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                                    (job_id, source['id'], canonical(batch['entity_ids']), batch['query'], query_start, query_end,
                                     'unqueryable' if batch.get('unqueryable') else 'queued', 0, 0, None, batch.get('error', '')))
                        if batch.get('unqueryable'):
                            con.execute("UPDATE jobs SET status='blocked',error=? WHERE id=?", (batch['error'], job_id))
                        queued += 1
                        query_count += 1
        self.store.audit('news_acquisition_queued', {'window': window, 'tasks': queued, 'batched_queries': query_count})
        return {'tasks': queued, 'batched_queries': query_count}

    @staticmethod
    def _epoch(value):
        if isinstance(value, (int, float)):
            return float(value)
        try:
            return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
        except (ValueError, TypeError, AttributeError):
            return time.time() + 60

    def _failure(self, job, result):
        if result.get('status') in ('ok', 'not_modified') and result.get('body'):
            return False
        deferred = result.get('status') in ('deferred', 'error', 'not_modified')
        state = 'deferred' if deferred else 'blocked'
        self.store.finish(job, state, result.get('error') or result.get('status', 'error'),
                          self._epoch(result.get('retry_at')), consume_attempt=result.get('error_code') not in ('host_interval', 'robots_pending'))
        with self.store.connection() as con:
            if job['kind'] == 'news_query':
                con.execute('UPDATE news_query_windows SET status=?,error=? WHERE job_id=?',
                            (state, result.get('error') or result.get('status'), job['id']))
            if job['kind'] == 'news_article':
                con.execute('UPDATE news_articles SET status=?,error=? WHERE id=?',
                            (state, result.get('error') or result.get('status'), job['payload']['article_id']))
        return True

    def _source_for_article(self, url, originating_source=None, all_sources=None):
        host = (urlsplit(url).hostname or '').lower()
        sources = ([originating_source] if originating_source else []) + (all_sources if all_sources is not None else self.sources())
        return next((s for s in sources if s and s.get('enabled') and s.get('terms_approved')
                     and s.get('download_articles') and (s.get('article_terms_approved') or s.get('body_terms_approved'))
                     and host in (s.get('article_allowed_hosts') or s.get('allowed_hosts', []))), None)

    def _observe(self, record, source, discovery_url, result, resolver, download_all=False, connection=None, all_sources=None):
        try:
            url = canonical_url(record['url'])
        except (ValueError, KeyError):
            return {'status': 'invalid_url'}
        article_id = digest(url)
        publisher = source.get('publisher_id') if urlsplit(url).hostname in source.get('allowed_hosts', []) else None
        publisher = publisher or (urlsplit(url).hostname or '').lower()
        stamp = result.get('retrieved_at') or now()
        title, summary = str(record.get('title') or ''), str(record.get('text') or '')
        details = {'title': title, 'summary': summary, 'published_at': record.get('published_at'),
                   'discovered_at': record.get('discovered_at'), 'source_config_hash': digest(canonical(source)),
                   'terms_url': source.get('terms_url'), 'source_policy': source_policy(source), 'scope': 'discovery_metadata',
                   'raw_url': record['url'], 'cache_validated_at': result.get('validated_at')}
        observation_id = digest(canonical([article_id, source['id'], discovery_url, result.get('sha256'), details]))
        with (nullcontext(connection) if connection is not None else self.store.connection()) as con:
            if not con.in_transaction:
                con.execute('BEGIN IMMEDIATE')
            con.execute('INSERT OR IGNORE INTO news_articles(id,canonical_url,publisher_id,source_id,title,summary,published_at,'
                        'discovered_at,scope,status) VALUES(?,?,?,?,?,?,?,?,?,?)',
                        (article_id, url, publisher, source['id'], title, summary, record.get('published_at'), stamp,
                         'feed_metadata_only', 'discovered'))
            con.execute('INSERT OR IGNORE INTO news_observations VALUES(?,?,?,?,?,?,?,?)',
                        (observation_id, article_id, source['id'], discovery_url, record['url'], stamp,
                         result.get('sha256'), canonical(details)))
            row = con.execute('SELECT * FROM news_articles WHERE id=?', (article_id,)).fetchone()
        candidates = resolver.resolve(summary, title=title, metadata={'published_at': record.get('published_at')}) if resolver else []
        eligible = download_all or any(r['status'] != 'rejected' for r in candidates)
        body_source = self._source_for_article(url, source, all_sources)
        fresh = row['retrieved_at'] and (time.time() - self._epoch(row['retrieved_at'])) < float((body_source or {}).get('refresh_seconds', 86400))
        if eligible and body_source and not fresh:
            period = int(time.time() // max(60, float(body_source.get('refresh_seconds', 86400))))
            self.store.enqueue('news_article', {'article_id': article_id, 'source_id': body_source['id'], 'url': url},
                               key='news_article:' + article_id + ':' + str(period), connection=connection)
            return {'status': 'article_queued', 'id': article_id, 'candidates': len(candidates)}
        return {'status': 'cached' if fresh else 'metadata_only', 'id': article_id, 'candidates': len(candidates)}

    def _query_url(self, source, payload):
        args = {'query': payload['query'], 'mode': 'ArtList', 'format': 'json', 'sort': 'DateDesc',
                'maxrecords': min(250, max(1, int(source.get('max_records', 250))))}
        if payload.get('start') and payload.get('end'):
            args.update(startdatetime=payload['start'], enddatetime=payload['end'])
        else:
            args['timespan'] = source.get('timespan', '7d')
        return source.get('base_url', 'https://api.gdeltproject.org/api/v2/doc/doc') + '?' + urlencode(args)

    def _split_saturated(self, job, source):
        payload = job['payload']
        if not payload.get('start') or not payload.get('end'):
            return False
        fmt = '%Y%m%d%H%M%S'
        try:
            start, end = [datetime.strptime(payload[k], fmt).replace(tzinfo=timezone.utc) for k in ('start', 'end')]
        except ValueError:
            return False
        if (end - start).total_seconds() <= max(900, int(source.get('min_window_seconds', 900))):
            return False
        middle = datetime.fromtimestamp((start.timestamp() + end.timestamp()) / 2, timezone.utc).strftime(fmt)
        for a, b in ((payload['start'], middle), (middle, payload['end'])):
            child = dict(payload, start=a, end=b)
            key = 'news_query_split:' + source['id'] + ':' + digest(canonical(child))
            job_id = self.store.enqueue('news_query', child, key=key)
            with self.store.connection() as con:
                con.execute('INSERT OR IGNORE INTO news_query_windows VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                            (job_id, source['id'], canonical(child['entity_ids']), child['query'], a, b,
                             'queued', 0, 0, job['id'], ''))
        return True

    @staticmethod
    def _topic_tokens(title):
        # The output is a candidate topic, never a verified common event.
        text = ''.join('ñ' if c == 'ñ' else ''.join(ch for ch in unicodedata.normalize('NFD', c)
                                                  if not unicodedata.combining(ch))
                       for c in unicodedata.normalize('NFC', title).casefold())
        stop = {'para', 'como', 'ante', 'desde', 'sobre', 'entre', 'esta', 'este', 'tras', 'dice', 'contra', 'news'}
        return {t for t in re.findall(r'\w+', text) if len(t) >= 4 and t not in stop}

    def _topic(self, con, article_id, title):
        tokens = self._topic_tokens(title)
        matches = Counter()
        for token in sorted(tokens):
            matches.update(r[0] for r in con.execute('SELECT article_id FROM news_topic_index WHERE token=? LIMIT 100', (token,)))
        topic_id = None
        for candidate, overlap in matches.most_common(20):
            row = con.execute('SELECT title,topic_id FROM news_articles WHERE id=?', (candidate,)).fetchone()
            other = self._topic_tokens(row['title'])
            union = len(tokens | other)
            if union and overlap >= 3 and len(tokens & other) / union >= .6:
                topic_id = row['topic_id']
                break
        topic_id = topic_id or digest('topic:' + article_id)[:24]
        for token in tokens:
            con.execute('INSERT OR IGNORE INTO news_topic_index VALUES(?,?)', (token, article_id))
        return topic_id

    def execute(self, job, fetcher, resolver=None, download_all=False):
        from .sources import parse_results
        payload = job['payload']
        source = next((s for s in self.sources() if s['id'] == payload['source_id']), None)
        if not source or not source.get('enabled') or not source.get('terms_approved'):
            self.store.finish(job, 'blocked', 'Fuente deshabilitada o condiciones sin revisar')
            return
        if job['kind'] == 'news_article' and not (source.get('download_articles') and (source.get('article_terms_approved') or source.get('body_terms_approved'))):
            self.store.finish(job, 'blocked', 'No se ha autorizado conservar el cuerpo editorial')
            return
        if job['kind'] in ('news_feed', 'news_query'):
            url = self._query_url(source, payload) if job['kind'] == 'news_query' else payload['url']
            result = fetcher.fetch(url, source)
            if self._failure(job, result):
                return
            records = parse_results(result, source)
            statuses = Counter()
            sources = self.sources()
            iterator = iter(records)
            while batch := list(islice(iterator, 250)):
                with self.store.connection() as con:
                    con.execute('BEGIN IMMEDIATE')
                    for record in batch:
                        statuses[self._observe(record, source, url, result, resolver, download_all, connection=con, all_sources=sources)['status']] += 1
            parse_info = result.get('parse_metadata') or {}
            saturated = job['kind'] == 'news_query' and (parse_info.get('observed_entries', len(records)) >= min(250, int(source.get('max_records', 250))) or parse_info.get('truncated'))
            split = self._split_saturated(job, source) if saturated else False
            with self.store.connection() as con:
                con.execute('INSERT OR REPLACE INTO news_discovery_runs VALUES(?,?,?,?,?,?,?,?)',
                            (job['id'], source['id'], job['kind'], url, len(records), canonical(parse_info),
                             result.get('sha256'), result.get('retrieved_at') or now()))
                con.execute('UPDATE news_query_windows SET status=?,records=?,saturated=?,error=? WHERE job_id=?',
                            ('split' if split else 'truncated' if saturated else 'complete', len(records), int(saturated),
                             'Límite top-N alcanzado; cobertura incompleta' if saturated and not split else '', job['id']))
            self.store.audit('news_discovery_completed', {'source_id': source['id'], 'url': url,
                             'hash': result.get('sha256'), 'records': len(records), 'decisions': dict(statuses),
                             'saturated': saturated, 'window_split': split, 'retrieved_at': result.get('retrieved_at')})
            self.store.finish(job)
            return
        if job['kind'] != 'news_article':
            raise ValueError('Tipo de trabajo de noticias desconocido')
        article_source = dict(source, allowed_hosts=source.get('article_allowed_hosts') or source.get('allowed_hosts', []))
        result = fetcher.fetch(payload['url'], article_source)
        if self._failure(job, result):
            return
        # Parse metadata as an article, regardless of the source's RSS transport.
        page_source = dict(source, kind=source.get('document_kind', 'news'))
        pages = parse_results(result, page_source)
        page = pages[0] if pages else {}
        body = article_text(result['body'])
        if len(body) < int(source.get('minimum_article_chars', 80)):
            raise ValueError('Cuerpo insuficiente; no se presenta como artículo leído')
        url = canonical_url(result.get('final_url') or result.get('url') or payload['url'])
        fingerprint = news_fingerprint(body)
        with self.store.connection() as con:
            row = con.execute('SELECT * FROM news_articles WHERE id=?', (payload['article_id'],)).fetchone()
        snapshot_hash = digest(result['body'])
        snapshots = self.engine.data_dir / 'news_snapshots' / snapshot_hash[:2]
        snapshots.mkdir(parents=True, exist_ok=True)
        snapshot = snapshots / (snapshot_hash + '.html.gz')
        if not snapshot.exists():
            with tempfile.NamedTemporaryFile(dir=snapshots, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(gzip.compress(result['body'].encode('utf-8'), mtime=0))
            try:
                os.link(temporary, snapshot)
            except FileExistsError:
                pass
            except OSError:
                if not snapshot.is_file() or digest(gzip.decompress(snapshot.read_bytes()).decode('utf-8')) != snapshot_hash:
                    raise
            finally:
                temporary.unlink(missing_ok=True)
        metadata = {'news_module': True, 'article_id': row['id'], 'publisher_id': row['publisher_id'],
                    'content_cluster': fingerprint, 'scope': 'full_article', 'source_config_hash': digest(canonical(source)),
                    'terms_url': source.get('terms_url'), 'source_policy': source_policy(source), 'raw_snapshot_sha256': snapshot_hash,
                    'raw_snapshot_path': str(snapshot.relative_to(self.engine.data_dir)),
                    'fetch_sha256': result.get('sha256'), 'extraction_version': 'article-main-2',
                    'etag': result.get('etag'), 'last_modified': result.get('last_modified')}
        with self.store.connection() as con:
            con.execute('BEGIN IMMEDIATE')
            existing = con.execute('SELECT document_id FROM news_publisher_content WHERE publisher_id=? AND fingerprint=?',
                                   (row['publisher_id'], fingerprint)).fetchone()
            doc_id = existing[0] if existing else self.store.add_document({
                'url': url, 'title': page.get('title') or row['title'], 'text': body,
                'source_kind': source.get('document_kind', 'news'), 'source_id': source['id'],
                'published_at': page.get('published_at') or row['published_at'],
                'retrieved_at': result.get('retrieved_at') or now(), 'metadata': metadata}, [], lambda *_: {}, connection=con)
            con.execute('INSERT OR IGNORE INTO news_publisher_content VALUES(?,?,?)', (row['publisher_id'], fingerprint, doc_id))
            topic = self._topic(con, row['id'], page.get('title') or row['title'])
            con.execute('UPDATE news_articles SET document_id=?,fingerprint=?,content_cluster=?,topic_id=?,title=?,published_at=?, '
                        "retrieved_at=?,scope='full_article',status='fetched',error='',attributed_signature=NULL WHERE id=?",
                        (doc_id, fingerprint, fingerprint, topic, page.get('title') or row['title'],
                         page.get('published_at') or row['published_at'], result.get('retrieved_at') or now(), row['id']))
            self.store._audit(con, 'news_article_acquired', {'article_id': row['id'], 'document_id': doc_id,
                              'publisher': row['publisher_id'], 'same_publisher_duplicate': bool(existing),
                              'raw_snapshot_sha256': snapshot_hash, 'content_cluster': fingerprint,
                              'topic_candidate': topic, 'canonical_response_url': url})
        self.store.finish(job)

    def acquire(self, workers=4, seconds=120, max_articles=1000, download_all=False, queue=True,
                window=None, start=None, end=None):
        if not self.engine.online:
            return {'status': 'offline', 'processed': 0}
        from .collector import SafeFetcher
        from .identity import IdentityResolver
        resolver = IdentityResolver(self.store.iter_entities())
        queued = self.queue(window, start, end) if queue else None
        config = dict(self.engine.config(), **{k: v for k, v in self.config().items() if k != 'sources'})
        fetcher = SafeFetcher(config, self.engine.data_dir, offline=False)
        before = fetcher.metrics_snapshot() if hasattr(fetcher, 'metrics_snapshot') else {}
        self.engine.stop_event.clear()
        deadline, counter, lock = time.monotonic() + seconds, [0], threading.Lock()
        def work():
            while not self.engine.stop_event.is_set() and time.monotonic() < deadline:
                with lock:
                    if counter[0] >= max_articles:
                        return
                    job = self.store.claim(lease_seconds=300, kind_prefix='news_')
                    if job:
                        counter[0] += 1
                if not job:
                    due = self.store.next_due(kind_prefix='news_')
                    if due is None or due > time.time() + max(0, deadline - time.monotonic()):
                        return
                    self.engine.stop_event.wait(min(1, max(.05, due - time.time())))
                    continue
                lease_stop = threading.Event()
                def renew():
                    while not lease_stop.wait(30):
                        if not self.store.renew(job):
                            return
                heartbeat = threading.Thread(target=renew, daemon=True)
                heartbeat.start()
                try:
                    self.execute(job, fetcher, resolver, download_all)
                except Exception as exc:
                    self.store.finish(job, 'deferred', f'{type(exc).__name__}: {exc}', time.time() + min(3600, 30 * 2 ** job['attempts']))
                finally:
                    lease_stop.set()
                    heartbeat.join(timeout=1)
        started = time.monotonic()
        try:
            # Keep one idle reader connection open during the batch. Otherwise
            # SQLite checkpoints WAL on every final connection close per job.
            with self.store.connection() as anchor:
                anchor.execute('SELECT value FROM meta LIMIT 1').fetchone()
                with ThreadPoolExecutor(max_workers=min(32, max(1, int(workers)))) as pool:
                    list(pool.map(lambda _: work(), range(min(32, max(1, int(workers))))))
            after = fetcher.metrics_snapshot() if hasattr(fetcher, 'metrics_snapshot') else {}
        finally:
            if hasattr(fetcher, 'close'):
                fetcher.close()
        return {'status': 'ok', 'processed': counter[0], 'queued': queued,
                'elapsed_seconds': round(time.monotonic() - started, 4),
                'network_before': before, 'network_after': after, 'bank': self.stats()}

    def attribute(self, limit=1000, force=False):
        from .identity import IdentityResolver, entity_for_analysis, MODEL_VERSION, RULES_HASH
        from .nlp import analyze
        with self.store.connection() as con:
            # Read the entire identity universe and its revision in one snapshot.
            # A concurrent bank import must not lend a new revision to an old index.
            con.execute('BEGIN')
            bank_revision = int(con.execute("SELECT value FROM meta WHERE key='news_identity_revision'").fetchone()[0])
            entities = [self.store.entity(row) for row in con.execute('SELECT * FROM entities ORDER BY id')]
        resolver = IdentityResolver(entities)
        by_id = {e['id']: e for e in entities}
        bank_signature = digest(canonical([entities, MODEL_VERSION, RULES_HASH]))
        def pending_rows():
            last_id, remaining = '', max(1, int(limit))
            while remaining > 0:
                with self.store.connection() as con:
                    rows = con.execute('SELECT n.*,d.text,d.content_hash FROM news_articles n LEFT JOIN documents d ON d.id=n.document_id '
                                       'WHERE n.id>? AND (? OR n.attributed_signature IS NULL OR n.attributed_signature!=?) ORDER BY n.id LIMIT ?',
                                       (last_id, int(force), bank_signature, min(25, remaining))).fetchall()
                if not rows:
                    return
                yield from rows
                remaining -= len(rows)
                last_id = rows[-1]['id']
        counts, affected, seen_docs, processed = Counter(), set(), set(), 0
        for row in pending_rows():
            processed += 1
            if row['document_id'] in seen_docs:
                with self.store.connection() as con:
                    con.execute('UPDATE news_articles SET attributed_signature=? WHERE id=?', (bank_signature, row['id']))
                continue
            text = row['text'] if row['document_id'] else (row['title'] + '\n' + row['summary']).strip()
            if not text:
                with self.store.connection() as con:
                    con.execute('UPDATE news_articles SET attributed_signature=? WHERE id=?', (bank_signature, row['id']))
                counts['empty_text'] += 1
                continue
            resolutions = resolver.resolve(text, title=row['title'], metadata={'published_at': row['published_at']})
            doc_id = row['document_id']
            if not doc_id:
                doc_id = self.store.add_document({'url': row['canonical_url'], 'title': row['title'], 'text': text,
                            'source_kind': 'discovery', 'source_id': row['source_id'], 'published_at': row['published_at'],
                            'retrieved_at': row['discovered_at'],
                            'metadata': {'news_module': True, 'scope': 'feed_metadata_only', 'discovery_only': True,
                                         'publisher_id': row['publisher_id'], 'article_id': row['id']}}, [], lambda *_: {})
            with self.store.connection() as con:
                con.execute('BEGIN IMMEDIATE')
                current_ids = {r['entity_id'] for r in resolutions}
                previous = con.execute('SELECT n.entity_id,n.resolution,e.analysis,e.review FROM news_identity_links n '
                                       'LEFT JOIN evidence e ON e.entity_id=n.entity_id AND e.document_id=n.document_id '
                                       'WHERE n.document_id=?', (doc_id,)).fetchall()
                for old in previous:
                    if old['entity_id'] in current_ids:
                        continue
                    history_id = digest(canonical([doc_id, old['entity_id'], old['resolution'], bank_revision]))
                    reason = 'La corrección del banco eliminó la coincidencia de identidad; se conserva la historia, no el vínculo activo.'
                    con.execute('INSERT OR IGNORE INTO news_identity_history VALUES(?,?,?,?,?,?,?,?)',
                                (history_id, doc_id, old['entity_id'], old['resolution'], old['analysis'] or '{}',
                                 old['review'] or '{}', now(), reason))
                    con.execute('DELETE FROM news_identity_links WHERE document_id=? AND entity_id=?', (doc_id, old['entity_id']))
                    con.execute('DELETE FROM evidence WHERE document_id=? AND entity_id=?', (doc_id, old['entity_id']))
                    con.execute('DELETE FROM scores WHERE entity_id=?', (old['entity_id'],))
                    self.store._audit(con, 'news_identity_retired', {'history_id': history_id, 'entity_id': old['entity_id'],
                                      'document_id': doc_id, 'previous_hash': digest(old['resolution']), 'reason': reason})
                    affected.add(old['entity_id'])
                for resolution in resolutions:
                    resolution['bank_revision'] = bank_revision
                    entity = by_id[resolution['entity_id']]
                    analysis = analyze(text, entity_for_analysis(entity, resolution), 'news')
                    evidence_id = digest(entity['id'] + doc_id)
                    con.execute('INSERT INTO evidence(id,entity_id,document_id,analysis) VALUES(?,?,?,?) '
                                'ON CONFLICT(entity_id,document_id) DO UPDATE SET analysis=excluded.analysis',
                                (evidence_id, entity['id'], doc_id, canonical(analysis)))
                    con.execute('INSERT INTO news_identity_links VALUES(?,?,?) ON CONFLICT(document_id,entity_id) '
                                'DO UPDATE SET resolution=excluded.resolution', (doc_id, entity['id'], canonical(resolution)))
                    con.execute('DELETE FROM scores WHERE entity_id=?', (entity['id'],))
                    self.store._audit(con, 'news_identity_resolved', {'entity_id': entity['id'], 'document_id': doc_id,
                                      'status': resolution['status'], 'resolution_hash': digest(canonical(resolution))})
                    counts[resolution['status']] += 1
                    affected.add(entity['id'])
                con.execute('UPDATE news_articles SET document_id=?,attributed_signature=? WHERE id=?', (doc_id, bank_signature, row['id']))
                seen_docs.add(doc_id)
        if affected:
            self.engine.rescore(affected)
        return {'articles_processed': processed, 'identity_links': dict(counts), 'entities_scored': len(affected),
                'identity_module': MODEL_VERSION,
                'name_only_is_not_unique_identity': True}

    def stats(self):
        with self.store.connection() as con:
            status = dict(con.execute('SELECT status,count(*) FROM news_articles GROUP BY status'))
            scopes = dict(con.execute('SELECT scope,count(*) FROM news_articles GROUP BY scope'))
            counts = {name: con.execute('SELECT count(*) FROM ' + table).fetchone()[0] for name, table in
                      [('articles', 'news_articles'), ('observations', 'news_observations'), ('identity_links', 'news_identity_links')]}
            counts.update(status=status, scopes=scopes,
                identity_status={r[0]: r[1] for r in con.execute("SELECT json_extract(resolution,'$.status'),count(*) FROM news_identity_links GROUP BY 1")},
                publishers=con.execute('SELECT count(DISTINCT publisher_id) FROM news_articles').fetchone()[0],
                content_clusters=con.execute('SELECT count(DISTINCT content_cluster) FROM news_articles WHERE content_cluster IS NOT NULL').fetchone()[0],
                query_windows=dict(con.execute('SELECT status,count(*) FROM news_query_windows GROUP BY status')))
            counts['truncated_feed_runs'] = con.execute("SELECT count(*) FROM news_discovery_runs WHERE kind='news_feed' AND json_extract(parser_metadata,'$.truncated')=1").fetchone()[0]
            return counts

    def articles(self, limit=100):
        with self.store.connection() as con:
            return [dict(r) for r in con.execute('SELECT * FROM news_articles ORDER BY discovered_at DESC LIMIT ?', (min(500, max(1, int(limit))),))]

    def plan(self, entities=None, articles_per_entity=5, unique_url_ratio=.6, target_days=7):
        entities = list(entities if entities is not None else self.store.iter_entities())
        sources = [s for s in self.sources() if s.get('enabled') and s.get('terms_approved')]
        queries, search_hosts = 0, {}
        for source in sources:
            if source.get('kind') == 'gdelt':
                batches = list(batch_queries(entities, source.get('batch_names', source.get('batch_size', 6)), source.get('max_query_chars', 500),
                                             source.get('language', source.get('source_lang', 'SPANISH')), source.get('source_country')))
                queries += len(batches)
                host = urlsplit(source.get('base_url', 'https://api.gdeltproject.org')).hostname
                count, interval = search_hosts.get(host, (0, 0))
                search_hosts[host] = (count + len(batches), max(interval, float(source.get('min_interval_seconds', 5))))
        search_seconds = max((count * max(1, interval) for count, interval in search_hosts.values()), default=0)
        body_sources = [s for s in sources if s.get('download_articles') and (s.get('article_terms_approved') or s.get('body_terms_approved'))]
        body_hosts = {}
        for source in body_sources:
            host = source.get('article_host') or urlsplit(source.get('url', source.get('base_url', ''))).hostname
            # Two feeds from one host share the same persisted courtesy limit.
            if host:
                body_hosts[host] = max(body_hosts.get(host, 1), max(1, float(source.get('min_interval_seconds', 5))))
        rates = [1 / interval for interval in body_hosts.values()]
        unique_articles = len(entities) * articles_per_entity * unique_url_ratio
        balanced = unique_articles / sum(rates) if rates else None
        single_host = unique_articles * max(body_hosts.values()) if body_hosts else None
        days = (search_seconds + balanced) / 86400 if balanced is not None else None
        return {'entities': len(entities), 'batched_discovery_requests': queries,
                'expected_unique_article_requests': unique_articles, 'configured_body_sources': len(body_sources),
                'configured_body_hosts': len(body_hosts),
                'balanced_estimated_days': round(days, 4) if days is not None else None,
                'single_host_article_days': round(single_host / 86400, 4) if single_host is not None else None,
                'target_days': target_days, 'model_within_target': days is not None and days < target_days,
                'web_coverage_demonstrated': False,
                'assumptions': {'articles_per_entity': articles_per_entity, 'unique_url_ratio': unique_url_ratio,
                                'publisher_distribution': 'balanced across independently limited hosts',
                                'host_model': 'article_host or feed host; excludes network latency, robots crawl-delay and failures',
                                'additional_time_partition_queries': 'not included; saturated windows increase requests'},
                'limitations': ['RSS cubre su ventana disponible, no el archivo completo.',
                               'La proyección no incluye bloqueos, caídas, revisión ni límites top-N no resueltos.',
                               'Un nombre procesado por el índice no significa que su investigación web esté completa.']}
