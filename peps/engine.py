"""Motor de ingestión, búsqueda y scoring. Todas las operaciones son reanudables."""
import json
import re
import threading
import time
import unicodedata
from itertools import islice
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

from .store import Store, canonical, digest, now


def normalized(text):
    text = ''.join(c for c in unicodedata.normalize('NFD', text.casefold()) if not unicodedata.combining(c))
    return ' '.join(re.findall(r'\w+', text, re.UNICODE))


class EntityIndex:
    """Índice por token raro. No compara cada documento con 40 mil nombres."""
    def __init__(self, entities):
        self.entities = {e['id']: e for e in entities}
        frequencies = {}
        self.names = {}
        for e in self.entities.values():
            names = [normalized(n) for n in [e['name'], *e.get('aliases', [])] if normalized(n)]
            self.names[e['id']] = names
            for token in {t for name in names for t in name.split()}:
                frequencies[token] = frequencies.get(token, 0) + 1
        self.postings = {}
        for entity_id, names in self.names.items():
            for name in names:
                token = min(name.split(), key=lambda t: frequencies[t])
                self.postings.setdefault(token, set()).add(entity_id)

    def match(self, text):
        norm = ' ' + normalized(text) + ' '
        candidates = set()
        for token in set(norm.split()):
            candidates.update(self.postings.get(token, ()))
        return [entity_id for entity_id in sorted(candidates)
                if any(' ' + n + ' ' in norm for n in self.names[entity_id])]


class Engine:
    def __init__(self, data_dir, config_path=None, online=False):
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.data_dir / 'peps.sqlite3')
        self.config_path = Path(config_path) if config_path else self.data_dir / 'sources.json'
        if not self.config_path.exists():
            default = Path(__file__).parent / 'defaults' / 'sources.json'
            config = json.loads(default.read_text(encoding='utf-8')) if default.exists() else {'sources': []}
            self.config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
        self.online = online
        self.stop_event = threading.Event()

    def config(self):
        value = json.loads(self.config_path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {'sources': value}

    def sources(self):
        return self.config().get('sources', [])

    def set_source(self, source_id, enabled, terms_approved):
        config = self.config()
        found = False
        for source in config['sources']:
            if source['id'] == source_id:
                source.update(enabled=bool(enabled), terms_approved=bool(terms_approved))
                found = True
        if not found:
            raise ValueError('Fuente inexistente')
        self.config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
        self.store.audit('source_configured', {'id': source_id, 'enabled': enabled, 'terms_approved': terms_approved})

    def import_documents(self, rows):
        from .nlp import analyze
        index = EntityIndex(self.store.iter_entities())
        count, links, unmatched = 0, 0, 0
        by_name = {}
        for e in index.entities.values():
            by_name.setdefault(normalized(e['name']), []).append(e['id'])
        iterator = iter(rows)
        while batch := list(islice(iterator, 250)):
            with self.store.connection() as con:
                con.execute('BEGIN IMMEDIATE')
                for row in batch:
                    entity_ids = row.get('entity_ids')
                    if not entity_ids and row.get('entity_name'):
                        entity_ids = by_name.get(normalized(row['entity_name']), [])
                        if len(entity_ids) > 1:
                            raise ValueError('Nombre homónimo: especifique entity_ids para ' + row['entity_name'])
                    if entity_ids is None:
                        entity_ids = index.match(str(row.get('title', '')) + '\n' + str(row.get('text', '')))
                    self.store.add_document(row, entity_ids, analyze, connection=con, entity_lookup=index.entities)
                    count += 1
                    links += len(entity_ids)
                    unmatched += not bool(entity_ids)
        return {'documents': count, 'links': links, 'unmatched': unmatched}

    def rescore(self, entity_id=None):
        from .nlp import analyze
        from .scoring import score_entity
        count = 0
        entities = [self.store.get_entity(entity_id)] if entity_id else self.store.iter_entities()
        iterator = iter(entities)
        while batch := list(islice(iterator, 250)):
            with self.store.connection() as con:
                con.execute('BEGIN IMMEDIATE')
                for entity in batch:
                    if entity is None:
                        raise ValueError('Entidad inexistente')
                    evidence = self.store.evidence(entity['id'], connection=con)
                    for item in evidence:
                        if item['source_kind'] == 'discovery' or item['metadata'].get('discovery_only'):
                            item['analysis'] = {'mentions': [], 'findings': [], 'identity_confidence': 0, 'review_required': True}
                        else:
                            item['analysis'] = analyze(item['text'], entity, item['source_kind'])
                        con.execute('UPDATE evidence SET analysis=? WHERE id=?', (canonical(item['analysis']), item['id']))
                    result = score_entity(entity, evidence)
                    self.store.save_score(entity['id'], result, connection=con)
                    count += 1
        return count

    def reanalyze(self):
        from .nlp import analyze
        count = 0
        for entity in self.store.iter_entities():
            for evidence in self.store.evidence(entity['id']):
                result = ({'mentions': [], 'findings': [], 'identity_confidence': 0, 'review_required': True}
                          if evidence['source_kind'] == 'discovery' or evidence['metadata'].get('discovery_only') else
                          analyze(evidence['text'], entity, evidence['source_kind']))
                with self.store.connection() as con:
                    con.execute('UPDATE evidence SET analysis=? WHERE id=?', (canonical(result), evidence['id']))
                    con.execute('DELETE FROM scores WHERE entity_id=?', (entity['id'],))
                count += 1
        self.store.audit('bank_reanalyzed', {'evidence': count})
        return count

    def queue_discovery(self, day=None):
        day = day or now()[:10]
        count = 0
        for source in self.sources():
            if not source.get('enabled') or not source.get('terms_approved') or not source.get('discovery'):
                continue
            start = time.time()
            interval = max(1, float(source.get('min_interval_seconds', 5)))
            iterator = enumerate(self.store.iter_entities())
            while batch := list(islice(iterator, 250)):
                with self.store.connection() as con:
                    con.execute('BEGIN IMMEDIATE')
                    for index, entity in batch:
                        self.store.enqueue('discover', {'entity_id': entity['id'], 'source_id': source['id'], 'window': day},
                                           key=f"discover:{day}:{entity['id']}:{source['id']}", due=start + index * interval,
                                           connection=con)
                        count += 1
        self.store.audit('discovery_queued', {'count': count, 'window': day})
        return count

    @staticmethod
    def _retry_epoch(value):
        if isinstance(value, (int, float)):
            return float(value)
        if value:
            from datetime import datetime
            try:
                return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
            except ValueError:
                pass
        return time.time() + 60

    def _handle_fetch_failure(self, result, job):
        status = result.get('status')
        if status == 'ok' or (status == 'not_modified' and result.get('body')):
            return False
        if status in ('deferred', 'error', 'not_modified'):
            self.store.finish(job, 'deferred', result.get('error') or status,
                              self._retry_epoch(result.get('retry_at')), consume_attempt=result.get('request_attempted', True))
        else:
            self.store.finish(job, 'blocked', result.get('error') or status)
        return True

    def _execute(self, job, fetcher):
        from .sources import build_search_url, parse_results, extract_text
        from .nlp import analyze
        payload = job['payload']
        sources = {s['id']: s for s in self.sources()}
        source = sources.get(payload.get('source_id'))
        if not source or not source.get('enabled') or not source.get('terms_approved'):
            self.store.finish(job, 'blocked', 'Fuente deshabilitada o sin autorización de uso')
            return
        if job['kind'] == 'discover':
            entity = self.store.get_entity(payload['entity_id'])
            if not entity:
                raise ValueError('Entidad ya no existe')
            url = build_search_url(entity, source)
            result = fetcher.fetch(url, source)
            if self._handle_fetch_failure(result, job):
                return
            records = parse_results(result, source)
            for record in records:
                hostname = (urlsplit(record['url']).hostname or '').lower()
                approved = next((s for s in sources.values() if s.get('enabled') and s.get('terms_approved')
                                 and hostname in s.get('allowed_hosts', [])), None)
                if approved:
                    self.store.enqueue('article', {'entity_ids': [entity['id']], 'source_id': approved['id'],
                                       'record': record}, key='article:' + payload['window'] + ':' + entity['id'] + ':' + record['url'])
                else:
                    # Conservar el hallazgo del índice sin hacerlo pasar por contenido editorial leído.
                    record.update(source_kind='discovery', source_id=source['id'],
                                  text=record.get('text') or record.get('title') or 'Resultado de búsqueda',
                                  retrieved_at=result.get('retrieved_at') or now(),
                                  metadata={'discovery_only': True, 'publisher_approved': False, 'query_url': url,
                                            'query_content_hash': result.get('sha256'),
                                            'cache_validated_at': result.get('validated_at'),
                                            'discovered_at': record.get('discovered_at'),
                                            'updated_at': record.get('updated_at'),
                                            'max_records': source.get('max_records', 100),
                                            'search_window': source.get('timespan', 'source_default')})
                    self.store.add_document(record, [entity['id']], lambda *_: {
                        'mentions': [], 'identity_confidence': 0, 'findings': [], 'review_required': True,
                        'coverage': 'index_only', 'nlp_version': 'not_analyzed'})
            self.store.audit('discovery_completed', {'entity_id': entity['id'], 'source_id': source['id'],
                             'records': len(records), 'url': url, 'content_hash': result.get('sha256')})
            self.store.finish(job)
        elif job['kind'] == 'article':
            record = payload['record']
            result = fetcher.fetch(record['url'], source)
            if self._handle_fetch_failure(result, job):
                return
            extracted = parse_results(result, source)
            page = extracted[0] if extracted else {}
            record.update(url=result.get('url') or record['url'], text=extract_text(result.get('body', '')),
                          title=page.get('title') or record.get('title', ''),
                          published_at=page.get('published_at') or record.get('published_at'),
                          source_kind=source.get('kind', 'news'), source_id=source['id'],
                          retrieved_at=result.get('retrieved_at') or now(),
                          metadata={'fetch_hash': result.get('sha256'), 'content_type': result.get('content_type')})
            self.store.add_document(record, payload['entity_ids'], analyze)
            for entity_id in payload['entity_ids']:
                self.rescore(entity_id)
            self.store.finish(job)
        else:
            raise ValueError('Tipo de tarea desconocido')

    def run(self, workers=4, max_jobs=100, seconds=120):
        from .collector import SafeFetcher
        if not self.online:
            return {'status': 'offline', 'processed': 0}
        self.stop_event.clear()
        workers = min(max(int(workers), 1), 32)
        deadline, counter, lock = time.monotonic() + seconds, [0], threading.Lock()
        fetcher = SafeFetcher(self.config(), self.data_dir, offline=False)

        def work():
            while not self.stop_event.is_set() and time.monotonic() < deadline:
                with lock:
                    if counter[0] >= max_jobs:
                        return
                    job = self.store.claim(lease_seconds=300)
                    if job:
                        counter[0] += 1
                if not job:
                    next_due = self.store.next_due()
                    if next_due is None or next_due > time.time() + max(0, deadline - time.monotonic()):
                        return
                    self.stop_event.wait(min(1, max(.05, next_due - time.time())))
                    continue
                try:
                    self._execute(job, fetcher)
                except Exception as exc:
                    self.store.finish(job, 'deferred', f'{type(exc).__name__}: {exc}',
                                      time.time() + min(3600, 30 * 2 ** job['attempts']))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(lambda _: work(), range(workers)))
        return {'status': 'ok', 'processed': counter[0], 'jobs': self.store.stats()['jobs']}


def capacity(entities=40000, queries=2, search_interval=5, articles=5, article_rate=1, overlap=.5):
    requests = entities * queries
    article_requests = entities * articles * (1 - overlap)
    seconds = requests * search_interval + article_requests / max(article_rate, .001)
    return {'entities': entities, 'discovery_requests': requests, 'unique_article_requests': article_requests,
            'estimated_days_sequential_upper_model': round(seconds / 86400, 3),
            'search_days_lower_bound': round(requests * search_interval / 86400, 3),
            'assumptions': {'queries_per_entity': queries, 'seconds_per_search_host': search_interval,
                            'articles_per_entity': articles, 'aggregate_article_rps': article_rate,
                            'deduplication_ratio': overlap},
            'qualification': 'Modelo de capacidad, no SLA. No incluye bloqueos, revisiones humanas ni falta de cobertura.'}
