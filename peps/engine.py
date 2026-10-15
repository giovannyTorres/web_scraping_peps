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


def _emit_progress(callback, phase, completed, total, unit, message, *, event='progress', **details):
    """Progress observers run after commits and cannot invalidate completed work."""
    if callback is not None:
        try:
            callback(dict(phase=phase, completed=completed, total=total, unit=unit,
                          message=message, event=event, **details))
        except Exception:
            pass


def _job_progress(store, kind_prefix=None, unreported_ids=()):
    with store.connection() as con:
        con.execute('BEGIN')
        rows = con.execute("SELECT status,count(*) AS n,"
                           "sum(kind IN ('discover','news_feed','news_query')) AS discovery "
                           'FROM jobs WHERE (? IS NULL OR kind LIKE ?) GROUP BY status',
                           (kind_prefix, (kind_prefix or '') + '%')).fetchall()
        jobs = {row['status']: row['n'] for row in rows}
        discovery_pending = sum(row['discovery'] for row in rows if row['status'] in ('queued', 'running', 'deferred'))
        # Other workers can commit before they obtain the callback lock. Keep
        # those finished units in the denominator until their own report arrives.
        unreported_completed = 0
        if unreported_ids:
            unreported_completed = con.execute("SELECT count(*) FROM jobs WHERE status='done' AND id IN (" +
                                               ','.join('?' for _ in unreported_ids) + ')',
                                               tuple(unreported_ids)).fetchone()[0]
    pending = sum(jobs.get(state, 0) for state in ('queued', 'running', 'deferred'))
    return dict(jobs=jobs, pending=pending, deferred=jobs.get('deferred', 0),
                blocked=jobs.get('blocked', 0), dead=jobs.get('dead', 0),
                failed=jobs.get('blocked', 0) + jobs.get('dead', 0), queue_complete=pending == 0,
                discovery_pending=discovery_pending, unreported_completed=unreported_completed)


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
        from .identity import IdentityResolver, entity_for_analysis
        from .news import NewsAcquirer, canonical_url
        NewsAcquirer(self)  # Imported articles use the same attribution queue.
        with self.store.connection() as con:
            con.execute('BEGIN')
            revision = int(con.execute("SELECT value FROM meta WHERE key='news_identity_revision'").fetchone()[0])
            snapshot = [self.store.entity(row) for row in con.execute('SELECT * FROM entities ORDER BY id')]
        index = EntityIndex(snapshot)
        resolver = None
        count, links, unmatched = 0, 0, 0
        by_name = {}
        for e in index.entities.values():
            by_name.setdefault(normalized(e['name']), []).append(e['id'])
        iterator = iter(rows)
        while batch := list(islice(iterator, 250)):
            with self.store.connection() as con:
                con.execute('BEGIN IMMEDIATE')
                for row in batch:
                    row = dict(row)
                    news_import = row.get('source_kind', 'news') not in ('official', 'discovery', 'legacy')
                    resolutions = []
                    if news_import:
                        resolver = resolver or IdentityResolver(snapshot)
                        resolutions = resolver.resolve(str(row.get('text', '')), title=str(row.get('title', '')),
                                                       metadata={'published_at': row.get('published_at')})
                        row['metadata'] = dict(row.get('metadata') or {}, news_module=True, identity_origin='local_article_import',
                                               publisher_id=urlsplit(str(row.get('url', ''))).hostname or 'local_import')
                    entity_ids = row.get('entity_ids')
                    if not entity_ids and row.get('entity_name'):
                        entity_ids = by_name.get(normalized(row['entity_name']), [])
                        if len(entity_ids) > 1 and not news_import:
                            raise ValueError('Nombre homónimo: especifique entity_ids para ' + row['entity_name'])
                    if entity_ids is None:
                        entity_ids = index.match(str(row.get('title', '')) + '\n' + str(row.get('text', '')))
                    if news_import:
                        entity_ids = list(dict.fromkeys([*entity_ids, *(r['entity_id'] for r in resolutions)]))
                    doc_id = self.store.add_document(row, entity_ids, analyze, connection=con, entity_lookup=index.entities)
                    if news_import:
                        for resolution in resolutions:
                            resolution['bank_revision'] = revision
                            entity = index.entities[resolution['entity_id']]
                            analysis = analyze(str(row.get('text', '')), entity_for_analysis(entity, resolution), 'news')
                            con.execute('UPDATE evidence SET analysis=? WHERE document_id=? AND entity_id=?',
                                        (canonical(analysis), doc_id, entity['id']))
                            con.execute('INSERT INTO news_identity_links VALUES(?,?,?) ON CONFLICT(document_id,entity_id) '
                                        'DO UPDATE SET resolution=excluded.resolution', (doc_id, entity['id'], canonical(resolution)))
                            self.store._audit(con, 'news_identity_resolved', {'entity_id': entity['id'], 'document_id': doc_id,
                                              'status': resolution['status'], 'resolution_hash': digest(canonical(resolution))})
                        try:
                            url = canonical_url(row.get('url', ''))
                        except ValueError:
                            url = None
                        if url:
                            con.execute('INSERT INTO news_articles(id,canonical_url,publisher_id,source_id,title,summary,published_at,'
                                        'discovered_at,retrieved_at,document_id,scope,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) '
                                        'ON CONFLICT(canonical_url) DO UPDATE SET document_id=excluded.document_id,attributed_signature=NULL',
                                        (digest(url), url, row['metadata']['publisher_id'], row.get('source_id', 'manual'),
                                         row.get('title', ''), '', row.get('published_at'), now(), row.get('retrieved_at') or now(),
                                         doc_id, 'full_article', 'imported'))
                    count += 1
                    links += len(entity_ids)
                    unmatched += not bool(entity_ids)
        return {'documents': count, 'links': links, 'unmatched': unmatched}

    def rescore(self, entity_id=None, *, progress=None, cancelable=False, acquisition=None):
        from .nlp import analyze
        from .scoring import score_entity
        from .contextual import analyze_evidence, get_method_info
        from .explainability import explain_score, scoring_fingerprint
        mode = self.context_mode()
        count = 0
        if entity_id and isinstance(entity_id, str):
            ids = [entity_id]
        else:
            requested = set(entity_id) if entity_id else None
            with self.store.connection() as con:
                ids = [row[0] for row in con.execute('SELECT id FROM entities ORDER BY id')
                       if requested is None or row[0] in requested]
        total = len(ids)
        _emit_progress(progress, 'scoring', 0, total, 'entidades', 'Iniciando el cálculo de resultados.', event='start')
        iterator = iter(ids)
        # Interactive progress commits each entity before reporting it. CLI
        # callers retain the existing 250-entity transaction throughput.
        batch_size = 1 if progress is not None or cancelable else 250
        while batch := list(islice(iterator, batch_size)):
            if cancelable and self.stop_event.is_set():
                break
            with self.store.connection() as con:
                con.execute('BEGIN IMMEDIATE')
                for current_id in batch:
                    row = con.execute('SELECT * FROM entities WHERE id=?', (current_id,)).fetchone()
                    if row is None:
                        raise ValueError('Entidad inexistente')
                    # The bank may change after iter_entities filled this batch.
                    # Score the entity and evidence from the same write snapshot.
                    entity = self.store.entity(row)
                    evidence = self.store.evidence(entity['id'], connection=con)
                    for item in evidence:
                        if item['source_kind'] == 'discovery' or item['metadata'].get('discovery_only'):
                            item['analysis'] = {'mentions': [], 'findings': [], 'identity_confidence': 0, 'review_required': True}
                        else:
                            analysis_entity = entity
                            if item.get('identity_resolution'):
                                from .identity import entity_for_analysis
                                analysis_entity = entity_for_analysis(entity, item['identity_resolution'])
                            item['analysis'] = analyze(item['text'], analysis_entity, item['source_kind'])
                        con.execute('UPDATE evidence SET analysis=? WHERE id=?', (canonical(item['analysis']), item['id']))
                    result = score_entity(entity, evidence)
                    result['contextual_analysis'] = [analyze_evidence(entity, item, mode) for item in evidence]
                    result['provenance'] = {'score_authority': scoring_fingerprint(),
                                            'context_method': get_method_info(mode)}
                    if acquisition is not None:
                        # Coverage is a historical limit, never a scoring input.
                        result['provenance']['acquisition'] = json.loads(canonical(acquisition))
                    result['explainability'] = explain_score(result)
                    self.store.save_score(entity['id'], result, connection=con,
                                          entity_snapshot=entity, evidence_snapshot=evidence)
            count += len(batch)
            _emit_progress(progress, 'scoring', count, total, 'entidades',
                           f'{count} de {total} entidades calculadas.', entity_id=batch[-1], pending=total - count)
        cancelled = bool(cancelable and self.stop_event.is_set() and count < total)
        _emit_progress(progress, 'scoring', count, total, 'entidades',
                       'Cálculo detenido.' if cancelled else 'Cálculo del lote finalizado.',
                       event='finish', cancelled=cancelled, pending=total - count)
        return count

    def context_mode(self):
        """Explicit local configuration; never implicitly import a large stack."""
        value = self.config().get('context_analysis') or {}
        if not isinstance(value, dict):
            raise ValueError('context_analysis debe ser un objeto')
        mode = value.get('mode', 'discriminative')
        if mode not in {'discriminative', 'advanced', 'enriched', 'hybrid'}:
            raise ValueError('context_analysis.mode no es válido')
        return mode

    def explanation(self, entity_id, run_id=None):
        from .explainability import VERSION, scoring_fingerprint
        from .contextual import get_method_info
        entity = self.store.get_entity(entity_id)
        if not entity:
            raise ValueError('Entidad inexistente')
        if run_id:
            run = self.store.get_score_run(run_id, entity_id)
            if not run:
                raise ValueError('Evaluación inexistente para esta entidad')
        else:
            run = self.store.latest_score_run(entity_id)
            provenance = (run or {}).get('result', {}).get('provenance', {})
            if (not run or provenance.get('score_authority') != scoring_fingerprint()
                    or provenance.get('context_method') != get_method_info(self.context_mode())
                    or run['result'].get('explainability', {}).get('version') != VERSION
                    or run['result'].get('as_of') != now()[:10]):
                self.rescore(entity_id)
                run = self.store.latest_score_run(entity_id)
        return {'entity': run['entity_snapshot'], 'result': run['result'],
                'explanation': run['result'].get('explainability', {}),
                'run': {'id': run['id'], 'created_at': run['created_at'], 'historical': bool(run_id)},
                'history': self.store.score_history(entity_id),
                'evidence_count': len(run['evidence_manifest'])}

    def replay_score(self, run_id):
        from .explainability import scoring_fingerprint
        from .scoring import score_entity
        run = self.store.get_score_run(run_id)
        if not run:
            raise ValueError('Evaluación inexistente')
        result = run['result']
        if result.get('provenance', {}).get('score_authority') != scoring_fingerprint():
            return {'status': 'unsupported', 'matched': False, 'run_id': run_id,
                    'reason': 'La versión, configuración o código del scorer difieren del snapshot. La evaluación histórica se conserva sin reinterpretarla.'}
        evidence = self.store.materialize_run_evidence(run)
        replayed = score_entity(run['entity_snapshot'], evidence, as_of=result['as_of'])
        recorded = {key: value for key, value in result.items()
                    if key not in {'contextual_analysis', 'provenance', 'explainability'}}
        matched = replayed == recorded
        return {'status': 'matched' if matched else 'mismatch', 'matched': matched,
                'run_id': run_id, 'recorded_score': result['score'], 'replayed_score': replayed['score'],
                'different_fields': sorted(key for key in set(recorded) | set(replayed)
                                           if recorded.get(key) != replayed.get(key)),
                'scope': 'authoritative_scoring_and_trace', 'auxiliary_replayed': False,
                'reason': 'Se reprodujeron el scoring y su traza con entradas y fecha históricas. Los análisis auxiliares permanecen archivados; no se reinfirieron.'}

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
                              self._retry_epoch(result.get('retry_at')),
                              consume_attempt=result.get('error_code') not in ('host_interval', 'robots_pending'))
        else:
            self.store.finish(job, 'blocked', result.get('error') or status)
        return True

    def _execute(self, job, fetcher):
        if job['kind'].startswith('news_'):
            from .news import NewsAcquirer
            from .identity import IdentityResolver
            news = getattr(self, '_news_acquirer', None) or NewsAcquirer(self)
            resolver = getattr(self, '_news_resolver', None) or IdentityResolver(self.store.iter_entities())
            self._news_acquirer, self._news_resolver = news, resolver
            news.execute(job, fetcher, resolver)
            return
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
            document_kind = source.get('document_kind') or (
                'news' if source.get('kind') in ('rss', 'atom', 'gdelt') else source.get('kind', 'news'))
            # A discovery transport describes the index/feed, not the fetched HTML.
            page_source = dict(source, kind=document_kind)
            extracted = parse_results(result, page_source)
            page = extracted[0] if extracted else {}
            record.update(url=result.get('final_url') or result.get('url') or record['url'],
                          text=extract_text(result.get('body', '')),
                          title=page.get('title') or record.get('title', ''),
                          published_at=page.get('published_at') or record.get('published_at'),
                          source_kind=document_kind, source_id=source['id'],
                          retrieved_at=result.get('retrieved_at') or now(),
                          metadata={'fetch_hash': result.get('sha256'), 'content_type': result.get('content_type')})
            self.import_documents([dict(record, entity_ids=payload['entity_ids'])])
            for entity_id in payload['entity_ids']:
                self.rescore(entity_id)
            self.store.finish(job)
        else:
            raise ValueError('Tipo de tarea desconocido')

    def run(self, workers=4, max_jobs=100, seconds=120, *, progress=None, reset_stop=True):
        from .collector import SafeFetcher
        if not self.online:
            _emit_progress(progress, 'download', 0, None, 'tareas', 'Comprobando el modo de descarga.', event='start')
            _emit_progress(progress, 'download', 0, None, 'tareas', 'La descarga está desactivada.',
                           event='finish', offline=True)
            return {'status': 'offline', 'processed': 0}
        if reset_stop:
            self.stop_event.clear()
        if self.stop_event.is_set():
            summary = _job_progress(self.store)
            _emit_progress(progress, 'download', 0, None, 'tareas', 'Iniciando el lote de descarga.',
                           event='start', processed=0, **summary)
            _emit_progress(progress, 'download', 0, None, 'tareas', 'Lote detenido antes de iniciar.',
                           event='finish', processed=0, cancelled=True, **summary)
            return dict(status='ok', processed=0, completed=0, cancelled=True, **summary)
        workers = min(max(int(workers), 1), 32)
        deadline, counter, completed, lock = time.monotonic() + seconds, [0], [0], threading.Lock()
        fetcher = SafeFetcher(self.config(), self.data_dir, offline=False)
        self._news_acquirer, self._news_resolver = None, None
        with self.store.connection() as con:
            has_news = con.execute("SELECT 1 FROM jobs WHERE kind LIKE 'news_%' AND status IN ('queued','deferred','running') LIMIT 1").fetchone()
        if has_news:
            from .news import NewsAcquirer
            from .identity import IdentityResolver
            self._news_acquirer = NewsAcquirer(self)
            self._news_resolver = IdentityResolver(self.store.iter_entities())
        _emit_progress(progress, 'download', 0, None, 'tareas', 'Iniciando el lote de descarga.',
                       event='start', processed=0, **_job_progress(self.store))

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
                lease_stop = threading.Event()
                def renew():
                    while not lease_stop.wait(30):
                        if not self.store.renew(job):
                            return
                heartbeat = threading.Thread(target=renew, daemon=True)
                heartbeat.start()
                try:
                    self._execute(job, fetcher)
                except Exception as exc:
                    self.store.finish(job, 'deferred', f'{type(exc).__name__}: {exc}',
                                      time.time() + min(3600, 30 * 2 ** job['attempts']))
                finally:
                    lease_stop.set()
                    heartbeat.join(timeout=1)
                with self.store.connection() as con:
                    state = con.execute('SELECT status,owner FROM jobs WHERE id=?', (job['id'],)).fetchone()
                with lock:
                    if state and state['status'] == 'done' and state['owner'] == job['owner']:
                        completed[0] += 1
                    if progress is not None:
                        _emit_progress(progress, 'download', completed[0], None, 'tareas',
                                       f"Tarea {state['status'] if state else 'desconocida'}; {completed[0]} completadas.",
                                       processed=counter[0], job_id=job['id'], job_status=state['status'] if state else None,
                                       **_job_progress(self.store))
        try:
            with self.store.connection() as anchor:
                anchor.execute('SELECT value FROM meta LIMIT 1').fetchone()
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    list(pool.map(lambda _: work(), range(workers)))
        finally:
            if hasattr(fetcher, 'close'):
                fetcher.close()
        summary = _job_progress(self.store)
        cancelled = self.stop_event.is_set()
        _emit_progress(progress, 'download', completed[0], None, 'tareas',
                       f"Lote {'detenido' if cancelled else 'finalizado'}; {summary['pending']} tareas pendientes.",
                       event='finish', processed=counter[0], cancelled=cancelled, **summary)
        return dict(status='ok', processed=counter[0], completed=completed[0], cancelled=cancelled, **summary)


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
