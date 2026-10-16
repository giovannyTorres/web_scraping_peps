"""News acquisition is independent of offline identity attribution.

Acquire feeds once, batch historical discovery, fetch each canonical URL once
per refresh window. Preserve distinct publishers even for identical syndication.
Identity decisions and adverse conduct remain separate, auditable operations.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import math
import re
import threading
import time
import os
import tempfile
import unicodedata
from collections import Counter
from contextlib import nullcontext
from copy import deepcopy
from itertools import islice
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from .store import canonical, digest, now

TRACKING = {'fbclid', 'gclid', 'dclid', 'msclkid', 'mc_cid', 'mc_eid', '_ga', 'igshid'}
UNRESERVED = frozenset('ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~')
SUPERSEDED_PLAN = 'Plan sustituido (superseded_news_plan): '


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


class _BodyBudget:
    """Bound body selection across queries and resumes, never identity proof."""

    def __init__(self, maximum=None, per_entity=None):
        self.maximum, self.per_entity = maximum, per_entity
        self.articles, self.by_entity, self.lock = set(), {}, threading.Lock()

    def reserve(self, article_id, entity_ids, *, existing=False):
        with self.lock:
            if article_id in self.articles:
                return True
            entity_ids = {str(value) for value in entity_ids}
            if not existing:
                if self.maximum is not None and len(self.articles) >= self.maximum:
                    return False
                if self.per_entity is not None and not any(
                        len(self.by_entity.get(value, ())) < self.per_entity for value in entity_ids):
                    return False
            self.articles.add(article_id)
            for value in entity_ids:
                self.by_entity.setdefault(value, set()).add(article_id)
            return True

    def release(self, article_id):
        with self.lock:
            self.articles.discard(article_id)
            for articles in self.by_entity.values():
                articles.discard(article_id)


class NewsAcquirer:
    def __init__(self, engine, config_path=None):
        self.engine, self.store = engine, engine.store
        self._plan_cache, self._plan_lock = None, threading.Lock()
        self.config_path = Path(config_path) if config_path else engine.data_dir / 'news_sources.json'
        if not self.config_path.exists():
            default = Path(__file__).parent / 'defaults' / 'news_sources.json'
            config = json.loads(default.read_text(encoding='utf-8')) if default.exists() else {'sources': []}
            self.config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
        else:
            from .news_search import merge_source_defaults
            default = Path(__file__).parent / 'defaults' / 'news_sources.json'
            existing = json.loads(self.config_path.read_text(encoding='utf-8'))
            merged = merge_source_defaults(existing, json.loads(default.read_text(encoding='utf-8')))
            if merged != existing:
                with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.config_path.parent, delete=False) as output:
                    temporary = Path(output.name)
                    json.dump(merged, output, ensure_ascii=False, indent=2)
                try:
                    # Rebind before replacing the file: an interrupted replace
                    # can be retried from the original catalogue on next start.
                    self._migrate_disabled_catalogue(existing, merged)
                    os.replace(temporary, self.config_path)
                finally:
                    temporary.unlink(missing_ok=True)
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
              CREATE TABLE IF NOT EXISTS news_shared_pages(
                source_id TEXT NOT NULL,url TEXT NOT NULL,config_hash TEXT NOT NULL,
                content_hash TEXT,retrieved_at TEXT NOT NULL,valid_until REAL NOT NULL,
                parsed TEXT NOT NULL,PRIMARY KEY(source_id,url));
              CREATE TABLE IF NOT EXISTS news_shared_queue(
                scope TEXT NOT NULL,url TEXT NOT NULL,job_id TEXT NOT NULL,
                PRIMARY KEY(scope,url));
            ''')

    def _migrate_disabled_catalogue(self, previous, current):
        """Preserve recorded scope only when the upgrade adds disabled sources."""
        old_other = {key: value for key, value in previous.items() if key != 'sources'}
        new_other = {key: value for key, value in current.items() if key != 'sources'}
        if 'search' not in old_other:
            new_other.pop('search', None)  # A future default is not a recorded historical period.
        if old_other != new_other:
            return False
        with self.store.connection() as con:
            if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_state'").fetchone():
                return False
        from .workflow import GuidedWorkflow
        workflow = GuidedWorkflow(self.store, lambda: deepcopy(current['sources']))
        return workflow.migrate_disabled_catalogue(previous['sources'], current['sources'])

    def config(self):
        value = json.loads(self.config_path.read_text(encoding='utf-8'))
        if not isinstance(value, dict) or not isinstance(value.get('sources'), list):
            raise ValueError('Configuración de noticias requiere sources: lista')
        return value

    def sources(self):
        return self.config()['sources']

    def search_config(self):
        from .news_search import normalize_search_config
        return normalize_search_config(self.config().get('search'))

    def set_search_config(self, settings):
        from .news_search import normalize_search_config
        if not isinstance(settings, dict):
            raise ValueError('La búsqueda requiere un objeto de configuración')
        config = self.config()
        current = self.search_config()
        config['search'] = normalize_search_config({**current, **settings})
        if config['search'] == current:
            return current
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.config_path.parent, delete=False) as output:
            temporary = Path(output.name)
            json.dump(config, output, ensure_ascii=False, indent=2)
        try:
            os.replace(temporary, self.config_path)
        finally:
            temporary.unlink(missing_ok=True)
        self.store.audit('news_search_configured', {'config_hash': digest(canonical(config['search']))})
        return config['search']

    def search_plan(self):
        from .news_search import build_search_plan, summarize_plan, normalize_search_config
        config = self.config()
        settings = normalize_search_config(config.get('search'))
        with self.store.connection() as con:
            revision = con.execute("SELECT value FROM meta WHERE key='news_identity_revision'").fetchone()[0]
        key = (revision, digest(canonical([config['sources'], settings])), now()[:10])
        with self._plan_lock:
            if self._plan_cache and self._plan_cache[0] == key and time.monotonic() - self._plan_cache[1] < 30:
                return deepcopy(self._plan_cache[2])
            summary = summarize_plan(build_search_plan(self.store.iter_entities(), config['sources'], settings))
            self._plan_cache = (key, time.monotonic(), summary)
            return deepcopy(summary)

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
        from .news_search import build_search_plan, summarize_plan
        window = window or now()[:10]
        settings = self.search_config()
        anchor_key = 'news_search_anchor:' + digest(canonical([window, settings, start, end]))
        with self.store.connection() as con:
            con.execute('BEGIN IMMEDIATE')
            con.execute('INSERT OR IGNORE INTO meta VALUES(?,?)', (anchor_key, now()))
            anchor = datetime.fromisoformat(con.execute('SELECT value FROM meta WHERE key=?', (anchor_key,)).fetchone()[0])
        plan = build_search_plan(self.store.iter_entities(), self.sources(), settings, start=start, end=end, now=anchor)
        queued, query_count, anchor_due = 0, 0, time.time()
        reactivated, superseded = [], []
        due_by_host = {}
        sources = {source['id']: source for source in self.sources()}
        with self.store.connection() as con:
            con.execute('BEGIN IMMEDIATE')
            # A temp table avoids SQLite parameter limits for large plans. New
            # jobs, restoration and retirement publish as one atomic queue.
            con.execute('CREATE TEMP TABLE current_news_plan(job_id TEXT PRIMARY KEY)')
            bank_revision = con.execute("SELECT value FROM meta WHERE key='news_identity_revision'").fetchone()[0]
            for item in plan['queries']:
                payload = dict(item, window=window)
                kind = payload.pop('kind')
                if payload.get('shared_index'):
                    payload['bank_revision'] = bank_revision
                    payload['crawl_scope'] = digest(canonical([window, item['source_id'], bank_revision,
                        item['scope_start'], item['scope_end'], item['page_limit'],
                        digest(canonical(sources[item['source_id']]))]))
                key = kind + ':' + digest(canonical([window, payload]))
                host = urlsplit(item['url']).hostname or item['source_id']
                due = due_by_host.get(host, anchor_due)
                cached_page = (con.execute('SELECT 1 FROM news_shared_pages WHERE source_id=? AND url=? AND config_hash=? AND valid_until>?',
                    (item['source_id'], item['url'], digest(canonical(sources[item['source_id']])), anchor_due)).fetchone()
                    if payload.get('shared_index') else None)
                if cached_page:
                    due = anchor_due
                job_id = self.store.enqueue(kind, payload, key=key, due=due, connection=con)
                con.execute('INSERT OR IGNORE INTO current_news_plan VALUES(?)', (job_id,))
                if payload.get('shared_index'):
                    con.execute('INSERT OR IGNORE INTO news_shared_queue VALUES(?,?,?)',
                                (payload['crawl_scope'], payload['url'], job_id))
                    con.execute('INSERT OR IGNORE INTO current_news_plan SELECT job_id FROM news_shared_queue WHERE scope=?',
                                (payload['crawl_scope'],))
                previous = con.execute('SELECT status,error,due FROM jobs WHERE id=?', (job_id,)).fetchone()
                if previous['status'] == 'cancelled' and (previous['error'] or '').startswith(SUPERSEDED_PLAN):
                    try:
                        saved = json.loads(previous['error'][len(SUPERSEDED_PLAN):])
                    except (ValueError, TypeError):
                        saved = None
                    if isinstance(saved, dict) and saved.get('status') in ('queued', 'deferred', 'running'):
                        restored = 'deferred' if previous['due'] > anchor_due else 'queued'
                        con.execute("UPDATE jobs SET status=?,error=?,lease_until=NULL,owner=NULL,updated_at=? WHERE id=?",
                                    (restored, str(saved.get('error') or ''), now(), job_id))
                        con.execute('UPDATE news_query_windows SET status=?,error=? WHERE job_id=?',
                                    (restored, str(saved.get('error') or ''), job_id))
                        reactivated.append(job_id)
                if not cached_page:
                    due_by_host[host] = due + max(1, float(sources[item['source_id']].get('min_interval_seconds', 5)))
                queued += 1
                if kind == 'news_query':
                    query_count += 1
                    con.execute('INSERT OR IGNORE INTO news_query_windows VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                                (job_id, item['source_id'], canonical(item['entity_ids']), item['query'],
                                 item['start'], item['end'], 'queued', 0, 0, None, ''))
            # Descendants are part of the same durable crawl even though only
            # its roots appear in the offline plan. Restore their saved backoff
            # when returning to a previously superseded scope, not just seeds.
            for descendant in con.execute("SELECT j.id,j.error,j.due FROM jobs j JOIN current_news_plan p ON p.job_id=j.id "
                                           "WHERE j.status='cancelled'").fetchall():
                if not (descendant['error'] or '').startswith(SUPERSEDED_PLAN):
                    continue
                try:
                    saved = json.loads(descendant['error'][len(SUPERSEDED_PLAN):])
                except (ValueError, TypeError):
                    continue
                if isinstance(saved, dict) and saved.get('status') in ('queued', 'deferred', 'running'):
                    restored = 'deferred' if descendant['due'] > anchor_due else 'queued'
                    con.execute("UPDATE jobs SET status=?,error=?,lease_until=NULL,owner=NULL,updated_at=? WHERE id=?",
                                (restored, str(saved.get('error') or ''), now(), descendant['id']))
                    reactivated.append(descendant['id'])
            obsolete = con.execute("SELECT id,status,error,due,attempts FROM jobs WHERE kind IN ('news_query','news_feed') "
                                   "AND (status IN ('queued','deferred') OR "
                                   "(status='running' AND COALESCE(lease_until,0)<=?)) "
                                   'AND NOT EXISTS (SELECT 1 FROM current_news_plan p WHERE p.job_id=jobs.id)',
                                   (time.time(),)).fetchall()
            for old in obsolete:
                marker = SUPERSEDED_PLAN + canonical({'status': old['status'], 'error': old['error'] or ''})
                con.execute("UPDATE jobs SET status='cancelled',error=?,lease_until=NULL,owner=NULL,updated_at=? WHERE id=?",
                            (marker, now(), old['id']))
                con.execute("UPDATE news_query_windows SET status='cancelled',error=? WHERE job_id=?", (marker, old['id']))
                superseded.append({'id': old['id'], 'previous_status': old['status'], 'due': old['due'],
                                   'attempts': old['attempts'], 'previous_error_hash': digest(old['error'] or '')})
            if superseded:
                self.store._audit(con, 'news_discovery_plan_superseded', {'window': window, 'count': len(superseded),
                                  'jobs': superseded, 'bodies_and_live_leases_preserved': True})
            if reactivated:
                self.store._audit(con, 'news_discovery_plan_reactivated', {'window': window, 'job_ids': reactivated,
                                  'due_and_attempts_preserved': True})
            self.store._audit(con, 'news_acquisition_queued', {'window': window, 'tasks': queued,
                              'batched_queries': query_count, 'strategy': settings['strategy'],
                              'planning_truncated': plan['truncated'], 'superseded': len(superseded),
                              'reactivated': len(reactivated)})
        return {'tasks': queued, 'batched_queries': query_count, 'search_plan': summarize_plan(plan, 3),
                'superseded': len(superseded), 'reactivated': len(reactivated)}

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
        changed = self.store.finish(job, state, result.get('error') or result.get('status', 'error'),
                                    self._epoch(result.get('retry_at')),
                                    consume_attempt=result.get('error_code') not in (
                                        'host_interval', 'robots_pending', 'session_limit', 'session_cancelled'))
        if not changed:
            return True
        with self.store.connection() as con:
            state = con.execute('SELECT status FROM jobs WHERE id=?', (job['id'],)).fetchone()[0]
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

    def _stored_candidate(self, row, relevance):
        candidate = relevance.rank(row['title'], row['summary'])
        if candidate['relevant']:
            return candidate
        with self.store.connection() as con:
            observations = con.execute('SELECT details FROM news_observations WHERE article_id=? '
                                       'ORDER BY retrieved_at DESC,id DESC LIMIT 10', (row['id'],)).fetchall()
        # Preserve the original card and raw observations. A newer snippet can
        # make the same canonical URL relevant without rewriting that history.
        for observation in observations:
            detail = json.loads(observation['details'])
            alternative = relevance.rank(detail.get('title', ''), detail.get('summary', ''))
            if alternative['rank'] > candidate['rank']:
                candidate = alternative
        return candidate

    def _observe(self, record, source, discovery_url, result, resolver, download_all=False, connection=None, all_sources=None,
                 candidate=None, body_budget=None):
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
        if candidate is not None:
            # Retrieval relevance helps decide which bodies to request. It is
            # deliberately separate from subsequent identity attribution.
            details['retrieval_relevance'] = candidate
        if record.get('metadata'):
            details['discovery_metadata'] = record['metadata']
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
        candidates = (resolver.resolve(summary, title=title, metadata={'published_at': record.get('published_at')})
                      if candidate is None and resolver else [])
        candidate_count = len(candidate['entity_ids']) if candidate is not None else len(candidates)
        eligible = download_all or (candidate['relevant'] if candidate is not None else
                                    any(r['status'] != 'rejected' for r in candidates))
        body_source = self._source_for_article(url, source, all_sources)
        fresh = row['retrieved_at'] and (time.time() - self._epoch(row['retrieved_at'])) < float((body_source or {}).get('refresh_seconds', 86400))
        if eligible and body_source and not fresh:
            candidate_ids = candidate['entity_ids'] if candidate is not None else [r['entity_id'] for r in candidates]
            if body_budget is not None and not body_budget.reserve(article_id, candidate_ids):
                return {'status': 'body_budget_limited', 'id': article_id, 'candidates': candidate_count}
            period = int(time.time() // max(60, float(body_source.get('refresh_seconds', 86400))))
            body_payload = {'article_id': article_id, 'source_id': body_source['id'], 'url': url}
            if candidate is not None:
                body_payload.update(candidate_entity_ids=candidate_ids, relevance_rank=candidate['rank'])
            with (nullcontext(connection) if connection is not None else self.store.connection()) as queue_con:
                before = queue_con.total_changes
                self.store.enqueue('news_article', body_payload,
                                   key='news_article:' + article_id + ':' + str(period), connection=queue_con)
                inserted = queue_con.total_changes > before
            return {'status': 'article_queued' if inserted else 'article_already_queued',
                    'id': article_id, 'candidates': candidate_count}
        return {'status': 'cached' if fresh else 'metadata_only', 'id': article_id, 'candidates': candidate_count}

    def _query_url(self, source, payload):
        from .news_search import query_url
        return query_url(source, payload)

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
        if source.get('search_kind') == 'wordpress_rss':
            from .news_search import split_query_windows
            windows = split_query_windows(source, payload)
            if not windows:
                return False
        else:
            middle = datetime.fromtimestamp((start.timestamp() + end.timestamp()) / 2, timezone.utc).strftime(fmt)
            windows = ((payload['start'], middle), (middle, payload['end']))
        interval = max(1., float(source.get('min_interval_seconds', 5)))
        with self.store.connection() as con:
            con.execute('BEGIN IMMEDIATE')
            reserved = con.execute("SELECT max(due) FROM jobs WHERE kind='news_query' "
                                   "AND status IN ('queued','deferred','running') "
                                   "AND json_extract(payload,'$.source_id')=?", (source['id'],)).fetchone()[0]
            due = max(time.time(), reserved or 0.) + interval
            for a, b in windows:
                child = dict(payload, start=a, end=b)
                child['url'] = self._query_url(source, child)
                key = 'news_query_split:' + source['id'] + ':' + digest(canonical(child))
                job_id = self.store.enqueue('news_query', child, key=key, due=due, connection=con)
                con.execute('INSERT OR IGNORE INTO news_query_windows VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                            (job_id, source['id'], canonical(child['entity_ids']), child['query'], a, b,
                             'queued', 0, 0, job['id'], ''))
                due += interval
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

    def _shared_page(self, source, payload, fetcher):
        from .shared_crawl import allowed_index_url
        from .shared_discovery import parse_sitemap, parse_wordpress_posts
        url = payload['url']
        if not allowed_index_url(url, source):
            return {'status': 'blocked', 'error': 'Índice fuera del alcance autorizado', 'body': ''}, None
        config_hash = digest(canonical(source))
        with self.store.connection() as con:
            cached = con.execute('SELECT * FROM news_shared_pages WHERE source_id=? AND url=?',
                                 (source['id'], url)).fetchone()
        if cached and cached['config_hash'] == config_hash and cached['valid_until'] > time.time():
            return ({'status': 'not_modified', 'body': '[cached discovery metadata]', 'url': url,
                     'sha256': cached['content_hash'], 'retrieved_at': cached['retrieved_at'],
                     'metadata_cache_hit': True}, self._shared_depth(json.loads(cached['parsed']), source, payload))
        request_source = dict(source, _wordpress_inferred_page=True) if payload.get('inferred_page') else source
        result = fetcher.fetch(url, request_source)
        if result.get('status') not in ('ok', 'not_modified') or not result.get('body'):
            return result, None
        maximum = min(8_000_000, int(source.get('max_bytes', 2_000_000)))
        if source['kind'] == 'wordpress_api':
            parsed = parse_wordpress_posts(result['body'], headers=result.get('pagination_headers'),
                                           page=payload.get('page', 1), max_bytes=maximum)
            if result.get('pagination_end'):
                parsed['pagination'].update(publisher_pagination_end=True, publisher_http_status=result.get('http_status'))
        else:
            parsed = parse_sitemap(result['body'], max_bytes=maximum,
                                   max_items=min(50000, int(source.get('max_records', 50000))),
                                   depth=0, max_depth=3)
        # This is a reusable metadata index, not a second store of article
        # bodies. Respect the HTTP cache's freshness when it is available.
        ttl = min(86400, max(0, float(source.get('index_ttl_seconds', 900))))
        valid_until = time.time() + ttl
        if result.get('cache_valid_until'):
            valid_until = min(valid_until, self._epoch(result['cache_valid_until']))
        with self.store.connection() as con:
            con.execute('INSERT OR REPLACE INTO news_shared_pages VALUES(?,?,?,?,?,?,?)',
                        (source['id'], url, config_hash, result.get('sha256'), result.get('retrieved_at') or now(),
                         valid_until, canonical(parsed)))
        return result, self._shared_depth(deepcopy(parsed), source, payload)

    @staticmethod
    def _shared_depth(parsed, source, payload):
        # Cached metadata is independent of where a URL occurs in a crawl.
        # Apply depth to the current traversal, not the original cache writer.
        if source.get('kind') == 'sitemap':
            depth = payload.get('depth', 0)
            parsed['pagination']['depth'] = depth
            if depth >= 3 and parsed['children']:
                parsed['children'] = []
                parsed['pagination'].update(depth_limited=True, truncated=True)
        return parsed

    def _queue_shared_children(self, job, source, children):
        from .shared_crawl import allowed_index_url
        payload = job['payload']
        result = {'queued': 0, 'duplicate_pages': 0, 'out_of_scope': 0, 'page_limit_reached': False}
        # Missing old/manual payloads cannot create an unbounded crawl.
        scope = payload.get('crawl_scope')
        maximum = min(240, max(1, int(payload.get('page_limit', 24))))
        if not scope:
            return result
        with self.store.connection() as con:
            con.execute('BEGIN IMMEDIATE')
            count = con.execute('SELECT count(*) FROM news_shared_queue WHERE scope=?', (scope,)).fetchone()[0]
            # Children follow the seeds, so the first page of every historical
            # year runs before a popular year can consume the whole session.
            due = max(time.time(), con.execute('SELECT COALESCE(max(j.due),0) FROM news_shared_queue q '
                        'JOIN jobs j ON j.id=q.job_id WHERE q.scope=?', (scope,)).fetchone()[0])
            for child in children:
                if not allowed_index_url(child['url'], source):
                    result['out_of_scope'] += 1
                    continue
                if con.execute('SELECT 1 FROM news_shared_queue WHERE scope=? AND url=?', (scope, child['url'])).fetchone():
                    result['duplicate_pages'] += 1
                    continue
                if count >= maximum:
                    result['page_limit_reached'] = True
                    break
                cached = con.execute('SELECT 1 FROM news_shared_pages WHERE source_id=? AND url=? AND config_hash=? AND valid_until>?',
                                     (source['id'], child['url'], digest(canonical(source)), time.time())).fetchone()
                if not cached:
                    due += max(1, float(source.get('min_interval_seconds', 5)))
                key = 'news_shared:' + digest(canonical([scope, child['url']]))
                identifier = self.store.enqueue('news_feed', child, key=key, due=time.time() if cached else due, connection=con)
                con.execute('INSERT INTO news_shared_queue VALUES(?,?,?)', (scope, child['url'], identifier))
                count += 1
                result['queued'] += 1
        return result

    def execute(self, job, fetcher, resolver=None, download_all=False, relevance=None, body_budget=None):
        from .sources import parse_results
        payload = job['payload']
        source = next((s for s in self.sources() if s['id'] == payload['source_id']), None)
        if not source or not source.get('enabled') or not source.get('terms_approved'):
            self.store.finish(job, 'blocked', 'Fuente deshabilitada o condiciones sin revisar')
            return
        if job['kind'] == 'news_article' and not (source.get('download_articles') and (source.get('article_terms_approved') or source.get('body_terms_approved'))):
            self.store.finish(job, 'blocked', 'No se ha autorizado conservar el cuerpo editorial')
            return
        if job['kind'] == 'news_article' and relevance is not None:
            with self.store.connection() as con:
                stored = con.execute('SELECT id,title,summary FROM news_articles WHERE id=?', (payload['article_id'],)).fetchone()
            relevant = stored is not None and self._stored_candidate(stored, relevance)['relevant']
            if stored is not None and not relevant:
                # Queues created by older versions can contain every body from
                # a general feed. Keep their metadata/audit, avoid an irrelevant
                # editorial request and do not classify this as a server error.
                if self.store.finish(job):
                    self.store.audit('news_body_excluded_irrelevant', {'article_id': payload['article_id'],
                                     'source_id': source['id'], 'identity_verified': False,
                                     'reason': 'No current bank name or contextual alias in stored metadata'})
                    return {'body_skipped_irrelevant': 1}
                return
        if job['kind'] in ('news_feed', 'news_query'):
            url = self._query_url(source, payload) if job['kind'] == 'news_query' else payload['url']
            shared = bool(payload.get('shared_index'))
            if shared:
                result, parsed = self._shared_page(source, payload, fetcher)
            else:
                result, parsed = fetcher.fetch(url, source), None
            if self._failure(job, result):
                return
            records = parsed['records'] if shared else parse_results(result, source)
            statuses = Counter()
            sources = self.sources()
            filtering = shared or job['kind'] == 'news_query' or relevance is not None
            if filtering and relevance is None:
                from .relevance import RelevanceIndex
                relevance = RelevanceIndex(self.store.iter_entities())
            candidates = []
            for record in records:
                if shared:
                    from .shared_crawl import in_period
                    if not in_period(record, payload):
                        statuses['excluded_period'] += 1
                        continue
                ranked = (relevance.rank(record.get('title', ''), record.get('text', ''),
                                         entity_ids=payload.get('entity_ids'), names=payload.get('names'))
                          if filtering else None)
                if ranked is not None and not ranked['relevant']:
                    statuses['excluded_irrelevant'] += 1
                else:
                    candidates.append((record, ranked))
            # Relevant candidates first, without promoting retrieval to an
            # identity match or changing the editorial permissions.
            candidates.sort(key=lambda item: -(item[1]['rank'] if item[1] is not None else 0))
            parse_info = result.setdefault('parse_metadata', {})
            if shared:
                from .shared_crawl import next_pages
                continuation = self._queue_shared_children(job, source, next_pages(source, payload, parsed))
                parse_info.update(shared_index=True, pagination=parsed['pagination'],
                    continuation=continuation, metadata_cache_hit=bool(result.get('metadata_cache_hit')),
                    locally_checked_entity_ids=[str(entity['id']) for entity in self.store.iter_entities()],
                    bank_revision=payload.get('bank_revision'), excluded_period=statuses['excluded_period'])
            parse_info['candidate_filter'] = {'enabled': filtering, 'input_records': len(records),
                                              'retained_records': len(candidates),
                                              'excluded_records': statuses['excluded_irrelevant'],
                                              'identity_verified': False}
            iterator = iter(candidates)
            while batch := list(islice(iterator, 250)):
                with self.store.connection() as con:
                    con.execute('BEGIN IMMEDIATE')
                    for record, ranked in batch:
                        statuses[self._observe(record, source, url, result, resolver, download_all,
                                               connection=con, all_sources=sources, candidate=ranked,
                                               body_budget=body_budget)['status']] += 1
            parse_info['body_budget'] = {'global_limit': body_budget.maximum if body_budget else None,
                                         'per_entity_limit': body_budget.per_entity if body_budget else None,
                                         'queued': statuses['article_queued'],
                                         'skipped': statuses['body_budget_limited']}
            result_limit = max(1, min(250, int(payload.get('result_limit', source.get('max_records', 250)))))
            saturated = job['kind'] == 'news_query' and (parse_info.get('observed_entries', len(records)) >= result_limit or parse_info.get('truncated'))
            # An upstream endpoint can ignore the search phrase and return its
            # ordinary feed. Subdividing that same irrelevant top-N endlessly
            # does not improve entity coverage. Preserve the limitation instead.
            split = (self._split_saturated(job, source) if saturated and candidates
                     and payload.get('split_policy', 'expand') != 'defer' else False)
            incomplete_reason = ('Cobertura incompleta: respuesta limitada sin candidatos pertinentes; no se subdivide automáticamente.'
                                 if filtering and not candidates else
                                 'Límite top-N alcanzado; ampliación pendiente del barrido inicial.'
                                 if payload.get('split_policy') == 'defer' else
                                 'Límite top-N alcanzado; cobertura incompleta')
            with self.store.connection() as con:
                con.execute('INSERT OR REPLACE INTO news_discovery_runs VALUES(?,?,?,?,?,?,?,?)',
                            (job['id'], source['id'], job['kind'], url, len(records), canonical(parse_info),
                             result.get('sha256'), result.get('retrieved_at') or now()))
                con.execute('UPDATE news_query_windows SET status=?,records=?,saturated=?,error=? WHERE job_id=?',
                            ('split' if split else 'truncated' if saturated else 'complete', len(records), int(saturated),
                             incomplete_reason if saturated and not split else '', job['id']))
            self.store.audit('news_discovery_completed', {'source_id': source['id'], 'url': url,
                             'hash': result.get('sha256'), 'records': len(records), 'decisions': dict(statuses),
                             'saturated': saturated, 'window_split': split, 'retrieved_at': result.get('retrieved_at')})
            self.store.finish(job)
            return {'discovery_metadata': parse_info}
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

    def _runtime_queue(self, active_tasks=0, unreported_ids=()):
        from .engine import _job_progress
        summary = _job_progress(self.store, 'news_', unreported_ids=unreported_ids, readiness=True)
        due = summary.pop('next_due') if not summary['ready'] else None
        summary.pop('next_due', None)
        retry = None
        if due is not None:
            try:
                retry = datetime.fromtimestamp(due, timezone.utc).isoformat()
            except (ValueError, OverflowError, OSError):
                pass
        wait_seconds = max(0., due - time.time()) if due is not None else None
        waiting = bool(summary['pending'] and not active_tasks and not summary['ready'])
        reason = ('external_lease' if summary['active_running'] > active_tasks else
                  'backoff' if summary['deferred'] else 'scheduled') if waiting else None
        return dict(summary, active_tasks=active_tasks, waiting=waiting, stop_reason=reason,
                    next_retry=retry, next_retry_at=due, wait_seconds=wait_seconds)

    def _rescue_bodies(self, relevance, sources, budget, deadline=None):
        """Queue eligible stored candidates without repeating discovery HTTP.

        Read metadata in pages, not source bodies. Existing fresh/queued bodies
        reserve quota first, so repeated resume cannot reset per-entity limits.
        """
        counts = Counter()
        with self.store.connection() as con:
            pending = {row[0] for row in con.execute("SELECT json_extract(payload,'$.article_id') FROM jobs "
                       "WHERE kind='news_article' AND status IN ('queued','running','deferred')")}

        def rows():
            previous = ''
            while True:
                with self.store.connection() as con:
                    batch = con.execute('SELECT id,canonical_url,source_id,title,summary,retrieved_at FROM news_articles '
                                        'WHERE id>? ORDER BY id LIMIT 250', (previous,)).fetchall()
                if not batch:
                    return
                yield from batch
                previous = batch[-1]['id']

        source_ids = {source['id']: source for source in sources}
        for reserve_existing in (True, False):
            for row in rows():
                if self.engine.stop_event.is_set() or (deadline is not None and time.monotonic() >= deadline):
                    return counts
                source = self._source_for_article(row['canonical_url'], source_ids.get(row['source_id']), sources)
                if not source:
                    continue
                fresh = row['retrieved_at'] and time.time() - self._epoch(row['retrieved_at']) < float(source.get('refresh_seconds', 86400))
                existing = bool(fresh or row['id'] in pending)
                if existing != reserve_existing:
                    continue
                candidate = self._stored_candidate(row, relevance)
                if not candidate['relevant']:
                    continue
                if not budget.reserve(row['id'], candidate['entity_ids'], existing=existing):
                    counts['body_skipped_budget'] += 1
                    continue
                if existing:
                    continue
                period = int(time.time() // max(60, float(source.get('refresh_seconds', 86400))))
                payload = {'article_id': row['id'], 'source_id': source['id'], 'url': row['canonical_url'],
                           'candidate_entity_ids': candidate['entity_ids'], 'relevance_rank': candidate['rank']}
                with self.store.connection() as con:
                    con.execute('BEGIN IMMEDIATE')
                    job_id = self.store.enqueue('news_article', payload, key='news_article:' + row['id'] + ':' + str(period), connection=con)
                    job = con.execute('SELECT status,error FROM jobs WHERE id=?', (job_id,)).fetchone()
                    if job['status'] == 'blocked' and job['error'] in (
                            'No se ha autorizado conservar el cuerpo editorial',
                            'Fuente deshabilitada o condiciones sin revisar'):
                        con.execute("UPDATE jobs SET status='queued',attempts=0,due=?,lease_until=NULL,owner=NULL,error='',updated_at=? WHERE id=?",
                                    (time.time(), now(), job_id))
                        job = dict(status='queued')
                    if job['status'] in ('queued', 'running', 'deferred'):
                        self.store._audit(con, 'news_body_recovered', {'article_id': row['id'], 'source_id': source['id'],
                                          'candidate_entity_ids': candidate['entity_ids'], 'identity_verified': False})
                        counts['body_queued'] += 1
                        pending.add(row['id'])
                    else:
                        budget.release(row['id'])
        return counts

    def acquire(self, workers=4, seconds=120, max_articles=1000, download_all=False, queue=True,
                window=None, start=None, end=None, *, progress=None, reset_stop=True, relevant_only=None,
                body_budget=None, per_entity_body_limit=None):
        from .engine import _emit_progress
        if not self.engine.online:
            _emit_progress(progress, 'download', 0, None, 'tareas', 'Comprobando el modo de descarga.', event='start')
            _emit_progress(progress, 'download', 0, None, 'tareas', 'La descarga está desactivada.',
                           event='finish', offline=True)
            return {'status': 'offline', 'processed': 0}
        seconds, max_articles = float(seconds), int(max_articles)
        if not math.isfinite(seconds) or seconds < 0 or max_articles < 0:
            raise ValueError('La duración y el límite de tareas deben ser finitos y no negativos')
        if reset_stop:
            self.engine.stop_event.clear()
        if self.engine.stop_event.is_set():
            summary = self._runtime_queue()
            summary['stop_reason'] = 'cancelled'
            _emit_progress(progress, 'download', 0, None, 'tareas', 'Iniciando el lote de noticias.',
                           event='start', processed=0, **summary)
            _emit_progress(progress, 'download', 0, None, 'tareas', 'Lote detenido antes de iniciar.',
                           event='finish', processed=0, cancelled=True, **summary)
            return dict(status='ok', processed=0, completed=0, cancelled=True, queued=None,
                        elapsed_seconds=0.0, network_before={}, network_after={}, bank=self.stats(), **summary)
        from .collector import SafeFetcher
        from .identity import IdentityResolver
        started = time.monotonic()
        self.store.reconcile_jobs('news_')
        _emit_progress(progress, 'download', 0, None, 'tareas', 'Preparando la reanudación y las fuentes autorizadas.',
                       event='start', processed=0, **self._runtime_queue())
        entities = list(self.store.iter_entities())
        resolver = IdentityResolver(entities)
        news_config = self.config()
        from .relevance import RelevanceIndex
        filtering = (news_config.get('search', {}).get('strategy', 'targeted') != 'feeds'
                     if relevant_only is None else bool(relevant_only))
        relevance = RelevanceIndex(entities) if filtering else None
        quick = news_config.get('search', {}).get('execution_mode') == 'quick'
        if body_budget is None and quick:
            body_budget = min(900, 3 * len(entities))
        if per_entity_body_limit is None and quick:
            per_entity_body_limit = 3
        for value in (body_budget, per_entity_body_limit):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError('Los límites de cuerpos deben ser enteros no negativos')
        budget = _BodyBudget(body_budget, per_entity_body_limit) if relevance is not None else None
        recovered = self._rescue_bodies(relevance, news_config['sources'], budget, started + seconds) if relevance is not None else Counter()
        queued = self.queue(window, start, end) if queue and not self.engine.stop_event.is_set() else None
        source_labels = {source['id']: str(source.get('source_name') or source.get('name') or source['id'])[:200]
                         for source in news_config['sources']}
        config = dict(self.engine.config(), **{k: v for k, v in news_config.items() if k != 'sources'})
        fetcher = SafeFetcher(config, self.engine.data_dir, offline=False)
        before = fetcher.metrics_snapshot() if hasattr(fetcher, 'metrics_snapshot') else {}
        deadline, counter, completed = started + seconds, [0], [0]
        condition = threading.Condition()
        in_flight = set()
        last_activity = [0.]
        task_context = threading.local()
        discovery_counts = Counter(recovered)
        cached_summary = [self._runtime_queue()]
        observed_at = [time.monotonic()]

        def activity_summary(claimed=None):
            if time.monotonic() - observed_at[0] >= .25:
                cached_summary[0] = self._runtime_queue(len(in_flight), in_flight)
                observed_at[0] = time.monotonic()
            elif claimed:
                # Claim committed already. Only these local counters changed;
                # discoveries and every finished job still refresh from SQLite.
                summary = deepcopy(cached_summary[0])
                previous = claimed['status']
                if previous != 'running':
                    summary['jobs'][previous] = max(0, summary['jobs'].get(previous, 0) - 1)
                    summary['jobs']['running'] = summary['jobs'].get('running', 0) + 1
                summary['ready'] = max(0, summary['ready'] - 1)
                summary['active_running'] += 1
                cached_summary[0] = summary
            summary = dict(cached_summary[0], active_tasks=len(in_flight))
            if in_flight:
                summary.update(waiting=False, stop_reason=None)
            return summary
        def download_total(summary):
            # Feed/query results can still create work. Backoff includes mandatory
            # waiting time that a throughput-only ETA cannot represent honestly.
            if summary['discovery_pending'] or summary['deferred']:
                return None
            return completed[0] + summary['pending'] + summary['failed'] + summary['unreported_completed']
        class SessionFetcher:
            def fetch(_, url, source):
                cancelled = self.engine.stop_event.is_set()
                if cancelled or time.monotonic() >= deadline:
                    return dict(status='deferred', body='', request_attempted=False, network_requests=0,
                                retry_at=time.time(), error_code='session_cancelled' if cancelled else 'session_limit',
                                error='Sesión detenida antes de solicitar el documento; puede reanudarlo.')
                task = getattr(task_context, 'value', None)
                began = time.monotonic()
                try:
                    # One session budget includes robots and redirect hops, not
                    # a fresh timeout allowance for every network request.
                    remaining = max(.001, deadline - time.monotonic())
                    request_source = dict(source, timeout_seconds=min(float(source.get('timeout_seconds', 20)), remaining),
                                          _deadline_monotonic=deadline)
                    result = fetcher.fetch(url, request_source)
                finally:
                    if task is not None:
                        task['fetch_seconds'] += max(0., time.monotonic() - began)
                        task['fetches'] += 1
                if task is not None:
                    task['result'] = result
                    stage = 'discovery' if task['kind'] in ('news_feed', 'news_query') else 'process'
                    with condition:
                        report_activity(activity_summary(),
                                        'Revisando resultados de la fuente.' if stage == 'discovery' else 'Extrayendo y conservando el documento.',
                                        task_event='stage', task_id=task['id'], task_stage=stage,
                                        task_label=task['label'], current_source=task['source'], job_kind=task['kind'])
                return result
        session_fetcher = SessionFetcher()

        def report_activity(summary, message, **details):
            if progress is not None:
                _emit_progress(progress, 'download', completed[0], download_total(summary), 'tareas', message,
                               event='activity', processed=counter[0], **summary, **details)

        def work():
            while not self.engine.stop_event.is_set() and time.monotonic() < deadline:
                with condition:
                    if counter[0] >= max_articles:
                        return
                    job = self.store.claim(lease_seconds=300, kind_prefix='news_')
                    if job:
                        counter[0] += 1
                        in_flight.add(job['id'])
                        task = {'id': job['id'], 'kind': job['kind'], 'source': job['payload'].get('source_id'),
                                'label': {'news_feed': 'Leer fuente', 'news_query': 'Buscar noticias',
                                          'news_article': 'Descargar artículo'}.get(job['kind'], 'Procesar tarea'),
                                'started': time.monotonic(), 'fetch_seconds': 0., 'fetches': 0, 'result': None}
                        if task['source'] in source_labels:
                            task['label'] = source_labels[task['source']] + ' · ' + task['label']
                        task_context.value = task
                        if progress is not None:
                            report_activity(activity_summary(job),
                                            f"Consultando {job['payload'].get('source_id', 'fuente')} · {len(in_flight)} tareas en curso.",
                                            current_source=task['source'], job_kind=job['kind'], task_id=job['id'],
                                            task_event='start', task_stage='fetch', task_label=task['label'])
                    else:
                        # Idle workers remain available while a feed/query can
                        # enqueue articles. Previously they exited at this point.
                        if not in_flight:
                            summary = self._runtime_queue()
                            due = summary['next_retry_at']
                            if (due is None or due > time.time() + max(0., deadline - time.monotonic())):
                                return
                            if time.monotonic() - last_activity[0] >= 1:
                                last_activity[0] = time.monotonic()
                                report_activity(summary, f"Esperando el reintento programado hasta {summary['next_retry'] or 'la fecha permitida'}.")
                        condition.wait(timeout=min(.25, max(0., deadline - time.monotonic())))
                        continue
                lease_stop = threading.Event()
                def renew():
                    while not lease_stop.wait(30):
                        if not self.store.renew(job):
                            return
                heartbeat = threading.Thread(target=renew, daemon=True)
                heartbeat.start()
                execution_result = None
                try:
                    execution_result = self.execute(job, session_fetcher, resolver, download_all, relevance, budget)
                except Exception as exc:
                    self.store.finish(job, 'deferred', f'{type(exc).__name__}: {exc}', time.time() + min(3600, 30 * 2 ** job['attempts']))
                finally:
                    lease_stop.set()
                    heartbeat.join(timeout=1)
                with self.store.connection() as con:
                    state = con.execute('SELECT status,owner,error FROM jobs WHERE id=?', (job['id'],)).fetchone()
                    if state and state['owner'] == job['owner'] and state['status'] in ('deferred', 'blocked', 'dead'):
                        if job['kind'] == 'news_article':
                            con.execute('UPDATE news_articles SET status=?,error=? WHERE id=?',
                                        (state['status'], state['error'] or '', job['payload'].get('article_id')))
                        elif job['kind'] == 'news_query':
                            con.execute('UPDATE news_query_windows SET status=?,error=? WHERE job_id=?',
                                        (state['status'], state['error'] or '', job['id']))
                with condition:
                    if state and state['status'] == 'done' and state['owner'] == job['owner']:
                        completed[0] += 1
                        if isinstance(execution_result, dict):
                            discovery_counts['body_skipped_irrelevant'] += execution_result.get('body_skipped_irrelevant', 0)
                        discovery_info = ((execution_result or {}).get('discovery_metadata') or
                                          (task.get('result') or {}).get('parse_metadata') or {})
                        info = discovery_info.get('candidate_filter') or {}
                        discovery_counts['discovery_records'] += info.get('input_records', 0)
                        discovery_counts['excluded_records'] += info.get('excluded_records', 0)
                        if info.get('enabled'):
                            discovery_counts['relevant_records'] += info.get('retained_records', 0)
                        body_info = discovery_info.get('body_budget') or {}
                        discovery_counts['body_queued'] += body_info.get('queued', 0)
                        discovery_counts['body_skipped_budget'] += body_info.get('skipped', 0)
                    in_flight.discard(job['id'])
                    if progress is not None:
                        summary = self._runtime_queue(len(in_flight), in_flight)
                        cached_summary[0], observed_at[0] = summary, time.monotonic()
                        elapsed = max(0., time.monotonic() - task['started'])
                        processing_stage = 'discovery' if job['kind'] in ('news_feed', 'news_query') else 'process'
                        timings = {processing_stage: max(0., elapsed - task['fetch_seconds'])}
                        if task['fetches']:
                            timings['fetch'] = task['fetch_seconds']
                        outcome = {'done': 'completada', 'deferred': 'aplazada', 'blocked': 'bloqueada', 'dead': 'agotada'}.get(
                            state['status'] if state else None, 'sin confirmar')
                        _emit_progress(progress, 'download', completed[0], download_total(summary), 'tareas',
                                       f"Tarea {outcome}; {completed[0]} completadas y {summary['pending']} pendientes.",
                                       processed=counter[0], job_id=job['id'], job_status=state['status'] if state else None,
                                       task_event='finish', task_id=job['id'], task_stage=processing_stage,
                                       task_label=task['label'], task_seconds=elapsed, task_timings=timings,
                                       task_status=state['status'] if state and state['owner'] == job['owner'] else 'cancelled',
                                       current_source=task['source'], job_kind=job['kind'],
                                       last_error=(state['error'] or '')[:500] if state else None,
                                       **dict(discovery_counts),
                                       **summary)
                    condition.notify_all()
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
        summary = self._runtime_queue()
        summary.update(discovery_records=discovery_counts['discovery_records'],
                       relevant_records=discovery_counts['relevant_records'],
                       excluded_records=discovery_counts['excluded_records'],
                       body_queued=discovery_counts['body_queued'], body_skipped_budget=discovery_counts['body_skipped_budget'],
                       body_skipped_irrelevant=discovery_counts['body_skipped_irrelevant'],
                       body_budget=body_budget, per_entity_body_limit=per_entity_body_limit, session_seconds=seconds)
        cancelled = self.engine.stop_event.is_set()
        stop_reason = ('cancelled' if cancelled else 'completed' if not summary['pending'] else
                       'job_limit' if counter[0] >= max_articles else 'time_limit' if time.monotonic() >= deadline else
                       summary['stop_reason'] or 'scheduled')
        summary['stop_reason'] = stop_reason
        messages = {
            'completed': 'El lote terminó; no quedan tareas pendientes de descarga.',
            'cancelled': 'Pausa solicitada. Las tareas pendientes se conservan para reanudar.',
            'job_limit': 'Se alcanzó el límite de tareas del lote. Puede reanudar sin repetir la búsqueda.',
            'time_limit': 'Se alcanzó el tiempo del lote; las solicitudes en curso terminaron con su timeout de transporte. Puede reanudar.',
            'backoff': 'La fuente exige esperar antes de reintentar; no se omitió su pausa.',
            'scheduled': 'Quedan tareas con ejecución programada para más adelante.',
            'external_lease': 'Otro proceso conserva tareas activas; se reanudarán al finalizar o vencer su reserva.',
        }
        if stop_reason == 'completed' and summary['failed']:
            messages['completed'] = (f"El lote terminó con {summary['failed']} tareas con error; "
                                     'revise las incidencias o continúe con la cobertura disponible.')
        message = messages[stop_reason]
        if summary['waiting'] and summary['next_retry']:
            message += f" Próximo intento permitido: {summary['next_retry']}."
        total = download_total(summary)
        _emit_progress(progress, 'download', completed[0], total, 'tareas',
                       message,
                       event='finish', processed=counter[0], cancelled=cancelled, **summary)
        return {'status': 'ok', 'processed': counter[0], 'completed': completed[0], 'total': total, 'cancelled': cancelled,
                **summary, 'message': message, 'queued': queued,
                'elapsed_seconds': round(time.monotonic() - started, 4),
                'network_before': before, 'network_after': after, 'bank': self.stats()}

    def attribute(self, limit=1000, force=False, *, progress=None, rescore=True, cancelable=False):
        from .engine import _emit_progress
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
        with self.store.connection() as con:
            available = con.execute('SELECT count(*) FROM news_articles WHERE '
                                    '(? OR attributed_signature IS NULL OR attributed_signature!=?)',
                                    (int(force), bank_signature)).fetchone()[0]
        total = available if limit is None else min(available, max(1, int(limit)))
        _emit_progress(progress, 'attribution', 0, total, 'artículos', 'Iniciando la atribución de identidad.',
                       event='start', pending=available)
        def pending_rows():
            last_id, remaining = '', total
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
            if cancelable and self.engine.stop_event.is_set():
                break
            if row['document_id'] in seen_docs:
                with self.store.connection() as con:
                    con.execute('UPDATE news_articles SET attributed_signature=? WHERE id=?', (bank_signature, row['id']))
                processed += 1
                _emit_progress(progress, 'attribution', processed, total, 'artículos',
                               f'{processed} de {total} artículos atribuidos.', article_id=row['id'])
                continue
            text = row['text'] if row['document_id'] else (row['title'] + '\n' + row['summary']).strip()
            if not text:
                with self.store.connection() as con:
                    con.execute('UPDATE news_articles SET attributed_signature=? WHERE id=?', (bank_signature, row['id']))
                counts['empty_text'] += 1
                processed += 1
                _emit_progress(progress, 'attribution', processed, total, 'artículos',
                               f'{processed} de {total} artículos atribuidos.', article_id=row['id'])
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
            processed += 1
            _emit_progress(progress, 'attribution', processed, total, 'artículos',
                           f'{processed} de {total} artículos atribuidos.', article_id=row['id'])
        with self.store.connection() as con:
            pending = (max(0, available - processed) if force else
                       con.execute('SELECT count(*) FROM news_articles WHERE attributed_signature IS NULL OR attributed_signature!=?',
                                   (bank_signature,)).fetchone()[0])
        cancelled = bool(cancelable and self.engine.stop_event.is_set() and processed < total)
        _emit_progress(progress, 'attribution', processed, total, 'artículos',
                       'Atribución detenida.' if cancelled else f'Lote de atribución finalizado; {pending} artículos pendientes.',
                       event='finish', pending=pending, cancelled=cancelled)
        scored = 0
        if affected and rescore and not (cancelable and self.engine.stop_event.is_set()):
            scored = self.engine.rescore(affected, progress=progress, cancelable=cancelable)
        return {'articles_processed': processed, 'identity_links': dict(counts), 'entities_scored': scored,
                'entities_affected': len(affected), 'pending': pending, 'cancelled': cancelled,
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

    def articles(self, limit=100, *, relevant_only=False):
        with self.store.connection() as con:
            if relevant_only:
                # Legacy observations are retained for audit. They enter this
                # optional view after retrieval relevance or identity review;
                # polling never rebuilds a resolver or scans source bodies.
                if not self.store._audit_json:
                    return []
                relevant = ("WHERE EXISTS (SELECT 1 FROM news_observations o JOIN json_each(o.details,'$.retrieval_relevance.entity_ids') v "
                            "JOIN entities e ON e.id=CAST(v.value AS TEXT) WHERE o.article_id=n.id "
                            "AND json_extract(o.details,'$.retrieval_relevance.relevant')=1) OR EXISTS "
                            "(SELECT 1 FROM news_identity_links l JOIN entities e ON e.id=l.entity_id WHERE l.document_id=n.document_id "
                            "AND COALESCE(json_extract(l.resolution,'$.status'),'rejected')!='rejected' "
                            "AND json_extract(l.resolution,'$.bank_revision')=CAST((SELECT value FROM meta WHERE key='news_identity_revision') AS INTEGER)) ")
            else:
                relevant = ''
            return [dict(r) for r in con.execute('SELECT n.* FROM news_articles n ' + relevant +
                        'ORDER BY n.discovered_at DESC LIMIT ?', (min(500, max(1, int(limit))),))]

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
