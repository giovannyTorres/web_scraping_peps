"""Opt-in, resumable public-web validation from names through production scoring.

Every search, response, body, clock and scoring decision is real. The collector
subclass only observes the original transport and enforces disclosed budgets.
No reference URLs, labels, fabricated documents or human confirmations are
passed to acquisition. Exports and individual diagnostics remain local.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import closing, contextmanager
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import signal
import sqlite3
import sys
import threading
import time
import traceback
import uuid
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from peps.acquisition_coverage import acquisition_coverage
from peps.cli import export_jsonl
from peps.collector import SafeFetcher
from peps.engine import Engine
from peps.news import NewsAcquirer
from peps.news_search import build_search_plan, normalize_search_config
from peps.relevance import assessment_summary
from peps.store import canonical, digest, now
from scripts.validate_notary_search import _body_proof, _candidate_rows
from scripts.validate_real_people import (
    cached_snapshot, copy_bank, corpus_snapshot, db_counts, http_history,
    load_roster, network_state, queue_receipt, request_key, source_hashes,
)

SCHEMA_VERSION = 1
WINDOW = 'live-e2e-frozen-nominal-plan-v1'


def sha256(value):
    return hashlib.sha256(value).hexdigest()


def measured_hashes():
    helpers = (Path(__file__).resolve(), ROOT / 'scripts/validate_real_people.py',
               ROOT / 'scripts/validate_notary_search.py')
    return source_hashes() | {str(path.relative_to(ROOT)): sha256(path.read_bytes()) for path in helpers}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


class Journal:
    """Append each observation before a later phase can fail or be stopped."""
    def __init__(self, directory):
        self.directory = directory
        self.lock = threading.Lock()

    def append(self, name, value):
        with self.lock:
            with (self.directory / (name + '.jsonl')).open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(value, ensure_ascii=False) + '\n')
                stream.flush()


def effective_config(path):
    config = json.loads(path.read_text(encoding='utf-8-sig'))
    sources = config.get('sources') if isinstance(config, dict) else None
    if not isinstance(sources, list) or not sources:
        raise ValueError('El catálogo congelado requiere sources: lista no vacía.')
    identifiers = [source.get('id') for source in sources if isinstance(source, dict)]
    if len(identifiers) != len(sources) or any(not identifier for identifier in identifiers) or len(set(identifiers)) != len(identifiers):
        raise ValueError('Cada fuente debe ser un objeto con id único.')
    enabled = [source for source in sources if source.get('enabled')]
    if not enabled:
        raise ValueError('Se requiere al menos una fuente habilitada.')
    for source in enabled:
        if source.get('terms_approved') is not True or source.get('robots_required') is not True:
            raise ValueError('Toda fuente activa requiere permisos explícitos y robots obligatorio.')
        if source.get('download_articles') and not (source.get('article_terms_approved') is True and source.get('body_terms_approved') is True):
            raise ValueError('Los permisos para conservar cuerpos deben registrarse por separado.')
        for key in ('url', 'base_url', 'search_url', 'api_url'):
            if source.get(key):
                parts = urlsplit(source[key])
                if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password:
                    raise ValueError('Sólo endpoints públicos HTTPS sin credenciales.')
    settings = deepcopy(config.get('search') or {})
    if settings.get('strategy', 'targeted') not in ('targeted', 'public'):
        raise ValueError('La prueba exige búsqueda nominal: targeted o public, sin índices generales.')
    # Labels and the provenance/reference URLs never enter nominal queries.
    settings.update(preset='identity', use_context=False)
    config['search'] = normalize_search_config(settings)
    return config


def prepare_inputs(args):
    raw_people, roster = load_roster(args.roster, allow_small=args.allow_small_roster)
    people = [{key: deepcopy(row[key]) for key in ('id', 'name', 'kind', 'aliases', 'context') if key in row}
              for row in raw_people]
    cases = [{'entity_id': row['id'], 'name': row['name'], 'entity_group': row['entity_group']} for row in raw_people]
    config = effective_config(args.sources_config)
    plan = build_search_plan(people, config['sources'], config['search'])
    if not plan['queries'] or any(query.get('kind') != 'news_query' for query in plan['queries']):
        raise ValueError('El plan debe contener únicamente búsquedas reales por nombre.')
    if plan.get('truncated'):
        raise ValueError('El max_queries del catálogo recorta el plan. Amplíelo antes de congelar la prueba; no se omiten entidades.')
    planned = {identifier for query in plan['queries'] for identifier in query.get('entity_ids', [])}
    if planned != {person['id'] for person in people}:
        raise ValueError('El plan debe incluir búsqueda nominal para todas las personas del padrón.')
    frozen = {'schema_version': SCHEMA_VERSION, 'roster_sha256': roster['sha256'],
              'source_config_input_sha256': sha256(args.sources_config.read_bytes()),
              'effective_config_sha256': digest(canonical(config)), 'people_sha256': digest(canonical(people)),
              'plan_sha256': digest(canonical(plan)), 'maximum_http_total': args.max_http,
              'body_budget': args.body_budget, 'per_entity_body_limit': args.per_entity_body_limit,
              'nominal_window': WINDOW}
    return people, cases, roster, config, plan, frozen


def validate_resume(directory, frozen):
    """Check immutable inputs, audit receipt and live leases before opening writable."""
    path = directory / 'frozen_inputs.json'
    database = directory / 'peps.sqlite3'
    if not path.exists() or not database.exists():
        raise ValueError('--resume requiere un banco anterior y su plan congelado.')
    previous = json.loads(path.read_text(encoding='utf-8'))
    if previous.get('hashes') != frozen:
        raise ValueError('Cambió el padrón, catálogo, plan o límites congelados. Use otro banco para otro experimento.')
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as con:
        con.row_factory = sqlite3.Row
        receipt = con.execute("SELECT payload FROM audit WHERE action='live_pipeline_started' ORDER BY seq LIMIT 1").fetchone()
        if receipt is None or json.loads(receipt[0]).get('frozen_inputs') != frozen:
            raise ValueError('El banco no tiene un recibo de inicio compatible con los insumos congelados.')
        actual = [dict(row) for row in con.execute('SELECT id,name,kind,aliases,context FROM entities ORDER BY id')]
        if previous.get('imported_entities_sha256') != digest(canonical(actual)):
            raise ValueError('Cambió un nombre, alias, contexto o procedencia importados en el banco.')
        if con.execute("SELECT 1 FROM jobs WHERE status='running' AND lease_until>? LIMIT 1", (time.time(),)).fetchone():
            raise ValueError('Todavía existe un trabajador con lease vigente. No se inicia otro proceso concurrente.')
    return previous


class BudgetStop(RuntimeError):
    """A local instrumentation limit, never a fabricated publisher response."""


class RequestBudget:
    def __init__(self, maximum, prior_requests, deadline, journal):
        self.maximum = maximum
        self.prior_requests = prior_requests
        self.deadline = deadline
        self.journal = journal
        self.lock = threading.Lock()
        self.events = []
        self.reason = None

    def exhausted(self):
        with self.lock:
            if self.prior_requests + len(self.events) >= self.maximum:
                self.reason = 'cumulative_http_budget'
            elif time.monotonic() >= self.deadline:
                self.reason = 'real_wall_deadline'
            return self.reason is not None

    def reserve(self, url, parent_url, source_id):
        with self.lock:
            if self.prior_requests + len(self.events) >= self.maximum:
                self.reason = 'cumulative_http_budget'
            elif time.monotonic() >= self.deadline:
                self.reason = 'real_wall_deadline'
            if self.reason:
                raise BudgetStop(self.reason)
            event = {'request_id': self.prior_requests + len(self.events) + 1,
                     'url': url, 'parent_fetch_url': parent_url, 'source_id': source_id,
                     'started_at': now(), 'http_status': None,
                     'robots_request': urlsplit(url).path == '/robots.txt'}
            self.events.append(event)
            self.journal.append('http-started', event)
            return event


def instrumentation_stop(url, requests=0):
    return {'url': url, 'status': 'deferred', 'body': '', 'http_status': None,
            'error': 'Explicit live E2E budget reached; remaining real jobs retained',
            'error_code': 'session_limit', 'retry_at': time.time(),
            'network_requests': requests, 'request_attempted': requests > 0}


def observed_fetcher(engine, budget, journal, directory, fetches):
    """Call the original SafeFetcher transport; capture the real returned bytes."""
    capture_dir = directory / 'response_captures'
    capture_dir.mkdir()
    lock = threading.Lock()

    class ObservedFetcher(SafeFetcher):
        def _transport(self, url, headers, timeout, max_bytes, addresses):
            try:
                event = budget.reserve(url, getattr(self._local, 'live_url', url), getattr(self._local, 'live_source', None))
            except BudgetStop:
                engine.stop_event.set()
                raise
            self._local.live_requests += 1
            started = time.monotonic()
            try:
                status, response_headers, body = super()._transport(
                    url, headers, min(timeout, max(.001, budget.deadline - started)), max_bytes, addresses)
                body_hash = sha256(body)
                path = capture_dir / f"{event['request_id']:06d}-{body_hash}.response.bin"
                path.write_bytes(body)
                event.update(http_status=status, response_bytes=len(body), response_sha256=body_hash,
                             response_capture_path=str(path.relative_to(directory)))
                return status, response_headers, body
            except Exception as exc:
                event['error_type'] = type(exc).__name__
                raise
            finally:
                event['seconds'] = round(time.monotonic() - started, 6)
                journal.append('http-completed', event)

        def fetch(self, url, source):
            self._local.live_requests = 0
            self._local.live_url, self._local.live_source = url, source['id']
            began = time.monotonic()
            if budget.exhausted():
                engine.stop_event.set()
                result = instrumentation_stop(url)
            else:
                bounded = dict(source, _deadline_monotonic=min(source.get('_deadline_monotonic', budget.deadline), budget.deadline))
                try:
                    result = super().fetch(url, bounded)
                except BudgetStop:
                    result = instrumentation_stop(url, self._local.live_requests)
            record = {'url': url, 'source_id': source['id'], 'observed_at': now(),
                      'seconds': round(time.monotonic() - began, 6),
                      **{key: result.get(key) for key in ('status', 'http_status', 'error_code', 'error',
                          'retry_at', 'network_requests', 'cache_status', 'from_cache')}}
            if result.get('body'):
                record['decoded_body_sha256'] = sha256(result['body'].encode('utf-8'))
            with lock:
                fetches.append(record)
            journal.append('fetches', record)
            return result

    return ObservedFetcher


@contextmanager
def instrument_collector(observer):
    # Production resolves this class locally inside acquire. No transport,
    # response, article body, resolver, scorer or clock is replaced with a mock.
    import peps.collector as collector
    original = collector.SafeFetcher
    collector.SafeFetcher = observer
    try:
        yield
    finally:
        collector.SafeFetcher = original


def corpus_fingerprints(engine):
    snapshot = corpus_snapshot(engine)
    return {table: digest(canonical(value)) for table, value in snapshot.items()}


def scored_people(engine, cases, jobs, history, fetches):
    queries = [(job, json.loads(job['payload'])) for job in jobs if job['kind'] == 'news_query']
    attempts = Counter(request_key(value['url']) for value in fetches)
    requests = Counter()
    for value in history:
        if urlsplit(value['url']).path != '/robots.txt':
            requests[request_key(value['url'])] += value['network_requests']
    with engine.store.connection() as con:
        receipts = {row[0] for row in con.execute('SELECT job_id FROM news_discovery_runs')}
        candidates, bodies, identities = defaultdict(set), defaultdict(set), defaultdict(Counter)
        for row in con.execute('SELECT c.entity_id,c.article_id,n.scope,n.document_id FROM news_current_candidates c '
                               'JOIN news_articles n ON n.id=c.article_id'):
            candidates[row['entity_id']].add(row['article_id'])
            if row['scope'] == 'full_article' and row['document_id']:
                bodies[row['entity_id']].add(row['document_id'])
        for row in con.execute('SELECT l.entity_id,l.resolution FROM news_identity_links l '
                               "JOIN documents d ON d.id=l.document_id WHERE d.source_kind!='discovery'"):
            identities[row['entity_id']][json.loads(row['resolution']).get('status', 'unknown')] += 1
        evidence = dict(con.execute('SELECT entity_id,count(*) FROM evidence GROUP BY entity_id'))
        scores = {row['entity_id']: (json.loads(row['result']), row['run_id']) for row in con.execute('SELECT entity_id,result,run_id FROM scores')}
    rows = []
    for case in cases:
        identifier = case['entity_id']
        selected = [(job, payload) for job, payload in queries if identifier in payload.get('entity_ids', [])]
        urls = {request_key(payload['url']) for _, payload in selected}
        result, run_id = scores.get(identifier, (None, None))
        summary = assessment_summary(result) if result is not None else {'state': 'not_computed', 'display_score': None}
        row = {**case, 'queries_planned': len(selected),
               'queries_completed': sum(job['id'] in receipts for job, _ in selected),
               'query_status_counts': dict(Counter(job['status'] for job, _ in selected)),
               'query_fetch_attempts_this_session': sum(attempts[url] for url in urls),
               'query_actual_http_requests_cumulative': sum(requests[url] for url in urls),
               'current_candidate_urls': len(candidates[identifier]), 'original_documents': len(bodies[identifier]),
               'identity_statuses': dict(identities[identifier]), 'evidence_items': evidence.get(identifier, 0),
               'score_computed': result is not None, 'score_run_id': run_id, 'assessment': summary,
               'score_is_review_priority_not_guilt_probability': True,
               'historical_search_complete': False, 'human_confirmation_automated': False}
        rows.append(row)
    return rows


def query_report(jobs, store):
    with store.connection() as con:
        receipts = {row[0] for row in con.execute('SELECT job_id FROM news_discovery_runs')}
    rows = []
    for job in jobs:
        if job['kind'] != 'news_query':
            continue
        payload = json.loads(job['payload'])
        rows.append({key: job[key] for key in ('id', 'status', 'attempts', 'due', 'lease_until', 'error')} |
                    {'source_id': payload.get('source_id'), 'entity_ids': payload.get('entity_ids'),
                     'query_url': payload.get('url'), 'discovery_receipt_saved': job['id'] in receipts})
    return rows


def analysis_provenance(jobs, coverage, acquisition, *, plan_sha256, attempt_dir):
    """A drained queue can still have source failures and partial coverage."""
    statuses = Counter(job['status'] for job in jobs)
    pending = sum(statuses[status] for status in ('queued', 'deferred', 'running'))
    failed_sources = sum(statuses[status] for status in ('blocked', 'failed', 'dead'))
    queue_finished = acquisition.get('queue_complete') is True and pending == 0
    reasons = []
    if pending:
        reasons.append('unfinished_acquisition_jobs')
    if failed_sources:
        reasons.append('blocked_failed_or_dead_source_jobs')
    if not queue_finished:
        reasons.append('known_queue_preparation_or_execution_incomplete')
    return {'mode': 'live_e2e_explicit_diagnostic', 'partial': bool(reasons),
            'partial_reason_codes': reasons, 'coverage': deepcopy(coverage),
            'coverage_snapshot_stage': 'before_identity_attribution',
            'pending_jobs': pending, 'blocked_count': statuses['blocked'],
            'failed_count': statuses['failed'], 'dead_count': statuses['dead'],
            'source_failure_jobs': failed_sources, 'job_status_counts': dict(statuses),
            'known_queue_finished': queue_finished, 'historical_search_complete': False,
            'frozen_plan_sha256': plan_sha256, 'attempt_dir': str(attempt_dir),
            'pending_acquisition_is_not_completed': True}


def safe_checkpoint(engine, attempt, state):
    value = {**state, 'updated_at': now()}
    try:
        value['counts'] = db_counts(engine.store)
        value['queue_status_counts'] = dict(Counter(job['status'] for job in queue_receipt(engine.store)))
    except Exception as exc:
        value['checkpoint_read_error'] = type(exc).__name__ + ': ' + str(exc)
    write_json(attempt / 'checkpoint.json', value)


def compare_local_corpus(before, after, local_jobs):
    """Keep immutable rows exact; disclose a narrow reference-only cache miss.

    Discovery-only documents can already have metadata identity/evidence links;
    the scorer excludes these documents unconditionally. Those rows, their
    bodies and all scoring history must still remain byte-for-byte unchanged.
    """
    tables = ('news_articles', 'news_observations', 'documents', 'evidence',
              'news_identity_links', 'scores', 'score_runs')
    immutable = tables[1:]
    membership = {table: set(before[table]) == set(after[table]) for table in tables}
    exact = {table: before[table] == after[table] for table in tables}
    blocked_misses = set()
    for job in local_jobs:
        if (job['kind'] == 'news_article' and job['status'] == 'blocked'
                and str(job.get('error') or '').startswith('local_cache_miss:')):
            try:
                payload = json.loads(job['payload']) if isinstance(job['payload'], str) else job['payload']
            except (ValueError, TypeError):
                continue
            if isinstance(payload, dict) and payload.get('article_id'):
                blocked_misses.add(payload['article_id'])
    documents = {row['id']: row for row in before['documents'].values()}
    changed, allowed = [], True
    for key in sorted(set(before['news_articles']) & set(after['news_articles'])):
        old, new = before['news_articles'][key], after['news_articles'][key]
        fields = {name for name in set(old) | set(new) if old.get(name) != new.get(name)}
        if not fields:
            continue
        document = documents.get(old.get('document_id'))
        try:
            metadata = json.loads(document['metadata']) if document else {}
        except (ValueError, TypeError):
            metadata = {}
        reference_only = old['scope'] != 'full_article' and (old.get('document_id') is None or (
            document is not None and document['source_kind'] == 'discovery'
            and isinstance(metadata, dict) and metadata.get('discovery_only') is True))
        permitted = (fields <= {'status', 'error'} and reference_only
            and old['status'] == 'discovered' and not old.get('error')
            and new['status'] == 'blocked' and str(new.get('error') or '').startswith('local_cache_miss:')
            and old['id'] in blocked_misses)
        allowed = allowed and permitted
        changed.append({'article_id': old['id'], 'scope': old['scope'], 'document_id': old.get('document_id'),
            'document_source_kind': document.get('source_kind') if document else None,
            'changed_fields': sorted(fields), 'before': {field: old.get(field) for field in sorted(fields)},
            'after': {field: new.get(field) for field in sorted(fields)},
            'explicit_reference_only_local_cache_miss': permitted,
            'linked_blocked_article_job': old['id'] in blocked_misses})
    checks = {'all_corpus_ids_unchanged': all(membership.values()),
              'all_documents_observations_evidence_identity_scores_and_history_byte_equal': all(exact[table] for table in immutable),
              'article_changes_limited_to_explicit_reference_only_local_cache_miss': allowed,
              'historical_audit_prefix_unchanged': all(after.get('audit', {}).get(key) == row
                  for key, row in before.get('audit', {}).items())}
    return {'checks': checks, 'passed': all(checks.values()), 'table_membership_equal': membership,
            'strict_first_pass_rows_unchanged': all(exact.values()), 'strict_table_rows_unchanged': exact,
            'first_pass_article_changes': changed,
            'scope': 'Only status/error of a reference without an original body may record an explicitly linked blocked local_cache_miss; all other article fields and all immutable tables remain exact.'}


def local_cache_validation(engine, news, args, attempt, progress):
    """Real saved responses, independent SQLite copy, unchanged scoring inputs."""
    directory = attempt / 'offline-bank'
    before = corpus_snapshot(engine)
    before_cache = cached_snapshot(engine.data_dir)
    before_network = network_state(engine.data_dir)
    before_catalogue = news.config_path.read_bytes()
    copy_bank(engine.data_dir, directory)
    local_engine = Engine(directory, online=False)
    local_news = NewsAcquirer(local_engine, merge_defaults=False)
    passes, snapshots, projections = [], [], []
    began = time.monotonic()
    for index in range(2):
        passes.append(local_news.acquire(workers=args.workers, seconds=args.local_seconds,
            max_articles=args.max_jobs, queue=True, window=WINDOW, relevant_only=True,
            body_budget=args.body_budget, per_entity_body_limit=args.per_entity_body_limit,
            cache_mode='local', progress=progress))
        snapshots.append(corpus_snapshot(local_engine))
        projections.append(_candidate_rows(local_engine))
    after_network = network_state(directory)
    tables = ('news_articles', 'news_observations', 'documents', 'evidence', 'news_identity_links', 'scores', 'score_runs')
    first_pass = compare_local_corpus(before, snapshots[0], queue_receipt(local_engine.store))
    checks = {'no_actual_http_requests': after_network['recorded_http_requests'] == before_network['recorded_http_requests'],
              'no_original_cache_bytes_changed': cached_snapshot(directory) == before_cache,
              'no_new_body_or_candidate_duplicates': first_pass['passed'],
              'second_pass_exactly_idempotent': all(snapshots[0][table] == snapshots[1][table] for table in tables),
              'candidate_projection_stable': _candidate_rows(engine) == projections[0] == projections[1],
              'cold_bank_unchanged': corpus_snapshot(engine) == before and cached_snapshot(engine.data_dir) == before_cache,
              'offline_audit_valid': local_engine.store.verify_audit()['ok'],
              'cold_and_local_frozen_source_catalogue_bytes_unchanged': local_news.config_path.read_bytes() == before_catalogue
                  and news.config_path.read_bytes() == before_catalogue}
    checks.update(first_pass['checks'])
    return {'method': 'Independent copy; production cache_mode=local against actual saved responses. No response substitution.',
            'seconds': time.monotonic() - began, 'passes': passes, 'checks': checks, 'passed': all(checks.values()),
            'first_pass_corpus_comparison': first_pass,
            'network_requests_before': before_network['recorded_http_requests'],
            'network_requests_after': after_network['recorded_http_requests']}


def document_integrity(store):
    with store.connection() as con:
        hashes_valid = all(sha256(row['text'].encode()) == row['content_hash'] for row in
            con.execute("SELECT text,content_hash FROM documents WHERE source_kind!='discovery'"))
        duplicate_urls = con.execute('SELECT canonical_url,count(*) FROM news_articles GROUP BY canonical_url HAVING count(*)>1').fetchall()
        duplicate_publisher_bodies = con.execute("SELECT n.publisher_id,d.content_hash,count(DISTINCT d.id) FROM news_articles n "
            "JOIN documents d ON d.id=n.document_id WHERE n.scope='full_article' GROUP BY n.publisher_id,d.content_hash "
            'HAVING count(DISTINCT d.id)>1').fetchall()
    return {'original_body_content_hashes_valid': hashes_valid, 'canonical_urls_unique': not duplicate_urls,
            'same_publisher_body_content_deduplicated': not duplicate_publisher_bodies}


def latency_summary(values):
    ordered = sorted(values)
    if not ordered:
        return {'count': 0, 'total_seconds': 0, 'p50_seconds': None, 'p95_seconds': None, 'max_seconds': None}
    return {'count': len(ordered), 'total_seconds': sum(ordered),
            'p50_seconds': ordered[(len(ordered) - 1) // 2],
            'p95_seconds': ordered[min(len(ordered) - 1, math.ceil(.95 * len(ordered)) - 1)],
            'max_seconds': ordered[-1]}


def source_results(config, queries, events, fetches, store):
    with store.connection() as con:
        discovered = {row['source_id']: row['records'] for row in con.execute(
            'SELECT source_id,sum(records) AS records FROM news_discovery_runs GROUP BY source_id')}
        bodies = {row['source_id']: row['bodies'] for row in con.execute(
            'SELECT d.source_id,count(DISTINCT d.id) AS bodies FROM news_articles n '
            "JOIN documents d ON d.id=n.document_id WHERE n.scope='full_article' GROUP BY d.source_id")}
    rows = []
    for source in config['sources']:
        if not source.get('enabled'):
            continue
        identifier = source['id']
        selected = [query for query in queries if query['source_id'] == identifier]
        transports = [event for event in events if event['source_id'] == identifier]
        fetch_rows = [event for event in fetches if event['source_id'] == identifier]
        rows.append({'source_id': identifier, 'queries_planned': len(selected),
            'queries_completed_cumulative': sum(row['discovery_receipt_saved'] for row in selected),
            'query_status_counts': dict(Counter(row['status'] for row in selected)),
            'discovery_records_cumulative': discovered.get(identifier, 0), 'original_documents_cumulative': bodies.get(identifier, 0),
            'original_document_count_scope': 'documents.source_id of the approved original-body adapter; not the first metadata discovery source.',
            'actual_http_this_session': len(transports), 'robots_http_this_session': sum(row['robots_request'] for row in transports),
            'http_status_counts': dict(Counter(str(row['http_status']) for row in transports)),
            'fetch_outcome_counts': dict(Counter(str(row['error_code'] or row['status']) for row in fetch_rows)),
            'transport_latency': latency_summary([row['seconds'] for row in transports if 'seconds' in row]),
            'fetch_latency_including_local_cache': latency_summary([row['seconds'] for row in fetch_rows])})
    return rows


def run(args):
    people, cases, roster, config, plan, frozen = prepare_inputs(args)
    directory, output = args.data_dir.resolve(), args.output.resolve()
    if output == directory or directory in output.parents:
        raise ValueError('El informe debe quedar fuera del banco, separado de SQLite y sus archivos auxiliares.')
    if output.exists():
        raise ValueError('El informe de salida ya existe; los resultados anteriores no se sobrescriben.')
    if args.roster.resolve() == output or args.sources_config.resolve() == output:
        raise ValueError('La salida no puede reemplazar un insumo congelado.')
    if directory == args.roster.resolve() or directory in args.roster.resolve().parents or directory in args.sources_config.resolve().parents:
        raise ValueError('El padrón y catálogo deben quedar fuera del banco nuevo.')
    if args.resume:
        validate_resume(directory, frozen)
    elif directory.exists() and any(directory.iterdir()):
        raise ValueError('La prueba fría requiere un banco vacío. Use --resume para el mismo plan.')
    directory.mkdir(parents=True, exist_ok=True)
    # Attempts are outside the bank so an independent cache copy never copies
    # itself, complete exports or the entire history of response captures.
    attempt = directory.with_name(directory.name + '-attempts') / (now().replace(':', '').replace('-', '') + '-' + uuid.uuid4().hex[:8])
    attempt.mkdir(parents=True)
    journal = Journal(attempt)
    began, started_at = time.monotonic(), now()
    engine = Engine(directory, online=True)
    news = NewsAcquirer(engine, merge_defaults=False)
    hashes = measured_hashes()
    if not engine.store.verify_audit()['ok']:
        raise ValueError('La auditoría del banco falla. No se inicia ni reanuda la prueba.')
    if not args.resume:
        write_json(directory / 'frozen_inputs.json', {'hashes': frozen, 'roster': roster, 'people': people, 'cases': cases,
                   'config': config, 'plan': plan})
    previous_jobs = queue_receipt(engine.store)
    previous_done = {job['id'] for job in previous_jobs if job['status'] == 'done'}
    prior_history = http_history(directory)
    prior_requests = sum(value['network_requests'] for value in prior_history)
    budget = RequestBudget(args.max_http, prior_requests, began + args.seconds, journal)
    fetches, phase_results, phase_seconds = [], {}, {}
    state = {'started_at': started_at, 'attempt_dir': str(attempt), 'phase': 'setup', 'resume': args.resume,
             'frozen_inputs': frozen, 'interrupted_signal': None, 'source_hashes': hashes,
             'first_original_body_seconds': None}
    checkpoint_lock = threading.Lock()
    last_checkpoint = [0.]
    def progress(event):
        journal.append('progress', {'observed_at': now(), 'elapsed_seconds': time.monotonic() - began, **event})
        if (state['first_original_body_seconds'] is None and event.get('job_kind') == 'news_article'
                and event.get('task_event') == 'finish' and event.get('job_status') == 'done'):
            with engine.store.connection() as con:
                if con.execute("SELECT 1 FROM news_articles WHERE scope='full_article' AND document_id IS NOT NULL LIMIT 1").fetchone():
                    state['first_original_body_seconds'] = time.monotonic() - began
        if time.monotonic() >= budget.deadline:
            engine.stop_event.set()
        if time.monotonic() - last_checkpoint[0] >= 15 or event.get('event') == 'finish':
            with checkpoint_lock:
                last_checkpoint[0] = time.monotonic()
                state.update(elapsed_seconds=time.monotonic() - began, session_http_requests=len(budget.events),
                             latest_progress=event, budget_reason=budget.reason)
                safe_checkpoint(engine, attempt, state)
                print(json.dumps({'phase': state['phase'], 'elapsed_seconds': round(time.monotonic() - began, 2),
                    'session_http_requests': len(budget.events), **{key: event.get(key) for key in
                    ('completed', 'pending', 'active_tasks', 'stop_reason', 'message')}}, ensure_ascii=False), flush=True)
    @contextmanager
    def phase(name):
        state['phase'] = name
        started = time.monotonic()
        journal.append('phases', {'phase': name, 'event': 'started', 'at': now()})
        safe_checkpoint(engine, attempt, state)
        try:
            yield
        except BaseException as exc:
            journal.append('phases', {'phase': name, 'event': 'failed', 'at': now(), 'error_type': type(exc).__name__, 'error': str(exc)})
            raise
        finally:
            phase_seconds[name] = time.monotonic() - started
            state['timings_seconds'] = dict(phase_seconds)
            journal.append('phases', {'phase': name, 'event': 'finished', 'at': now(), 'seconds': phase_seconds[name]})
            safe_checkpoint(engine, attempt, state)
    original_handlers = {}
    def interrupted(signum, frame):
        state['interrupted_signal'] = signum
        engine.stop_event.set()
    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGINT, signal.SIGTERM):
            original_handlers[signum] = signal.signal(signum, interrupted)
    finished = threading.Event()
    def deadline_watch():
        if not finished.wait(max(0, budget.deadline - time.monotonic())):
            state['wall_deadline_observed'] = now()
            engine.stop_event.set()
    watchdog = threading.Thread(target=deadline_watch, daemon=True)
    watchdog.start()
    failure = None
    try:
        with phase('import'):
            if not args.resume:
                engine.store.import_entities(people)
                with engine.store.connection() as con:
                    imported = [dict(row) for row in con.execute('SELECT id,name,kind,aliases,context FROM entities ORDER BY id')]
                frozen_record = json.loads((directory / 'frozen_inputs.json').read_text(encoding='utf-8'))
                frozen_record['imported_entities_sha256'] = digest(canonical(imported))
                write_json(directory / 'frozen_inputs.json', frozen_record)
            write_json(news.config_path, config)
            engine.store.audit('live_pipeline_resumed' if args.resume else 'live_pipeline_started',
                {'frozen_inputs': frozen, 'attempt_dir': str(attempt), 'people': len(people), 'groups': roster['groups'],
                 'previous_requests': prior_requests, 'source_hashes': hashes, 'real_transport': True,
                 'no_automated_human_confirmation': True})
        with phase('search_and_original_download'):
            Observed = observed_fetcher(engine, budget, journal, attempt, fetches)
            with instrument_collector(Observed):
                phase_results['acquisition'] = news.acquire(workers=args.workers,
                    seconds=min(args.acquire_seconds, max(.001, budget.deadline - time.monotonic())),
                    max_articles=args.max_jobs, queue=True, window=WINDOW, relevant_only=True,
                    body_budget=args.body_budget, per_entity_body_limit=args.per_entity_body_limit,
                    cache_mode='reuse', progress=progress, reset_stop=False)
        jobs = queue_receipt(engine.store)
        pending = [job for job in jobs if job['status'] in ('queued', 'deferred', 'running')]
        coverage = acquisition_coverage(engine.store, news.sources())
        provenance = analysis_provenance(jobs, coverage, phase_results['acquisition'],
            plan_sha256=frozen['plan_sha256'], attempt_dir=attempt)
        engine.store.audit('live_pipeline_analysis_requested', provenance)
        # A transport budget stop ends acquisition, while the separately
        # reserved real processing time remains available for the actual texts.
        if not state['interrupted_signal'] and time.monotonic() < budget.deadline:
            engine.stop_event.clear()
            with phase('identity_attribution'):
                phase_results['attribution'] = news.attribute(limit=None, rescore=False, progress=progress, cancelable=True)
            if not state['interrupted_signal'] and time.monotonic() < budget.deadline:
                with phase('production_scoring'):
                    phase_results['scored'] = engine.rescore(acquisition=provenance, progress=progress, cancelable=True)
        with phase('replay_and_explanations'):
            with engine.store.connection() as con:
                runs = [dict(row) for row in con.execute('SELECT entity_id,run_id FROM scores ORDER BY entity_id')]
            replays, explanations, verification_batches = [], [], 0
            for offset in range(0, len(runs), 1000):
                if state['interrupted_signal'] or time.monotonic() >= budget.deadline:
                    break
                selected = runs[offset:offset + 1000]
                # The production API verifies the complete audit once inside
                # the same read snapshot that validates every run and body.
                # It returns all real replays and historical explanations.
                bundle = engine.replay_score_batch([row['run_id'] for row in selected])
                replays.extend(bundle['replays'])
                explanations.extend(bundle['explanations'])
                verification_batches += 1
                state.update(score_replays_completed=len(replays), score_explanations_completed=len(explanations))
                journal.append('score-verification', {'observed_at': now(), 'completed': len(replays),
                    'requested': len(runs), 'batch': verification_batches,
                    'method': 'production_batch_verified_single_read_snapshot'})
                safe_checkpoint(engine, attempt, state)
            write_json(attempt / 'score-replays.json', replays)
            write_json(attempt / 'individual-explanations-private.json', explanations)
            phase_results['replay'] = {'requested': len(runs), 'completed': len(replays),
                                       'matched': sum(value['matched'] for value in replays),
                                       'explanations_completed': len(explanations),
                                       'production_verified_snapshot_batches': verification_batches}
        with phase('production_export'):
            export_jsonl(engine.store, attempt / 'production-export-private.jsonl')
        if not args.skip_local_validation and not state['interrupted_signal']:
            # This actual-cache integrity phase is reported separately from the
            # real online end-to-end wall time; it does not create benchmark data.
            state['online_e2e_seconds'] = time.monotonic() - began
            finished.set()
            with phase('actual_local_cache_reuse'):
                phase_results['local_cache'] = local_cache_validation(engine, news, args, attempt, progress)
    except BaseException as exc:
        failure = {'type': type(exc).__name__, 'message': str(exc), 'traceback': traceback.format_exc()}
        write_json(attempt / 'failure.json', failure)
    finally:
        finished.set()
        for signum, handler in original_handlers.items():
            signal.signal(signum, handler)
        state['phase'] = 'report'
        safe_checkpoint(engine, attempt, state)
    with phase('report'):
        history = http_history(directory)
        jobs = queue_receipt(engine.store)
        people_results = scored_people(engine, cases, jobs, history, fetches)
        bodies = _body_proof(engine, people)
        # These are production outcomes after attribution/scoring, replacing
        # the acquisition-only helper's intentionally false placeholders.
        for body in bodies:
            body.pop('identity_confirmed', None)
            body.pop('scored', None)
        candidates = _candidate_rows(engine)
        counts = db_counts(engine.store)
        queries = query_report(jobs, engine.store)
        audit = engine.store.verify_audit()
        remaining = [job for job in jobs if job['status'] in ('queued', 'deferred', 'running')]
        replay = phase_results.get('replay', {})
        state_counts = dict(Counter(row['assessment']['state'] for row in people_results))
        current_done = {job['id'] for job in jobs if job['status'] == 'done'}
        body_hash_groups = defaultdict(set)
        for body in bodies:
            body_hash_groups[body['body_sha256']].add(body['document_id'])
        checks = {'all_people_imported': counts['entities'] == len(people),
            'all_people_have_nominal_plan': all(row['queries_planned'] for row in people_results),
            'plan_not_truncated': not plan.get('truncated'), 'audit_valid': audit['ok'],
            'cumulative_http_budget_respected': sum(value['network_requests'] for value in history) <= args.max_http,
            'actual_transport_and_durable_http_match': len(budget.events) == sum(value['network_requests'] for value in history) - prior_requests,
            'previous_completed_jobs_preserved': previous_done <= current_done,
            'original_snapshots_hash_valid': all(body['raw_snapshot_hash_valid'] for body in bodies),
            'captured_real_transport_bytes_hash_valid': all(sha256((attempt / event['response_capture_path']).read_bytes()) == event['response_sha256']
                for event in budget.events if event.get('response_capture_path')),
            'candidate_entity_article_pairs_unique': len({(row['entity_id'], row['article_id']) for row in candidates}) == len(candidates),
            'all_people_received_production_score': counts['scores'] == len(people),
            'all_current_scores_replayed_exactly': replay.get('completed') == replay.get('matched') == len(people),
            'all_current_score_explanations_saved': replay.get('explanations_completed') == len(people),
            'source_modules_stable_within_attempt': hashes == measured_hashes(),
            'frozen_inputs_unchanged': prepare_inputs(args)[5] == frozen,
            'no_fatal_error': failure is None}
        checks.update(document_integrity(engine.store))
        if 'local_cache' in phase_results:
            checks['actual_cache_reuse_idempotent'] = phase_results['local_cache']['passed']
        partial_reasons = []
        if remaining:
            partial_reasons.append(f'{len(remaining)} queued/deferred/running jobs remain')
        if any(job['status'] in ('blocked', 'failed', 'dead') for job in jobs):
            partial_reasons.append('Some jobs are blocked/failed/dead; their exact errors remain in jobs/query_results')
        if budget.reason:
            partial_reasons.append(budget.reason)
        if state['interrupted_signal']:
            partial_reasons.append('interrupted_signal_' + str(state['interrupted_signal']))
        if not all(row['queries_completed'] for row in people_results):
            partial_reasons.append('At least one entity lacks a durable completed nominal search')
        if failure:
            partial_reasons.append('fatal_error_' + failure['type'])
        summary = {'people': len(people), 'people_with_completed_nominal_search': sum(bool(row['queries_completed']) for row in people_results),
            'people_with_at_least_one_completed_query': sum(bool(row['queries_completed']) for row in people_results),
            'people_with_all_queries_completed': sum(row['queries_planned'] > 0 and row['queries_completed'] == row['queries_planned'] for row in people_results),
            'people_with_actual_query_http': sum(bool(row['query_actual_http_requests_cumulative']) for row in people_results),
            'people_with_fetch_attempt_this_session': sum(bool(row['query_fetch_attempts_this_session']) for row in people_results),
            'people_with_current_candidates': sum(bool(row['current_candidate_urls']) for row in people_results),
            'people_with_original_text': sum(bool(row['original_documents']) for row in people_results),
            'current_candidate_urls': len({row['article_id'] for row in candidates}), 'current_candidate_entity_pairs': len(candidates),
            'unique_original_documents': len({body['document_id'] for body in bodies}),
            'unique_original_body_hashes': len(body_hash_groups), 'original_entity_text_pairs': sum(row['original_documents'] for row in people_results),
            'production_scores_computed': counts['scores'], 'assessment_states': state_counts,
            'evaluated_zero_scores': sum(row['assessment']['state'] == 'evaluated' and row['assessment']['display_score'] == 0 for row in people_results),
            'zeros_without_assessable_evidence_are_not_evaluated': True,
            'actual_http_requests_cumulative': sum(value['network_requests'] for value in history),
            'actual_http_requests_this_session': len(budget.events), 'pending_jobs': len(remaining),
            'job_status_counts': dict(Counter(job['status'] for job in jobs)),
            'query_status_counts': dict(Counter(row['status'] for row in queries)),
            'http_status_counts_this_session': dict(Counter(str(value['http_status']) for value in budget.events)),
            'fetch_outcome_counts_this_session': dict(Counter(str(value['error_code'] or value['status']) for value in fetches))}
        summary['groups'] = {group: {'people': sum(row['entity_group'] == group for row in people_results),
            'with_completed_nominal_search': sum(row['entity_group'] == group and bool(row['queries_completed']) for row in people_results),
            'with_at_least_one_completed_query': sum(row['entity_group'] == group and bool(row['queries_completed']) for row in people_results),
            'with_all_queries_completed': sum(row['entity_group'] == group and row['queries_planned'] > 0
                and row['queries_completed'] == row['queries_planned'] for row in people_results),
            'with_original_text': sum(row['entity_group'] == group and bool(row['original_documents']) for row in people_results),
            'assessment_states': dict(Counter(row['assessment']['state'] for row in people_results if row['entity_group'] == group))}
            for group in ('municipal_president', 'notary')}
        report = {'schema_version': SCHEMA_VERSION, 'generated_at': now(),
            'method': {'real_names': True, 'real_transport': True, 'real_clock': True, 'production_pipeline': True,
                       'response_substitution': False, 'synthetic_documents': False, 'gold_urls_used_for_search': False,
                       'automated_human_confirmation': False, 'score_is_not_guilt_or_risk_probability': True,
                       'individual_outputs_local_private': True},
            'run': {'started_at': started_at, 'completed_at': now(), 'bank_dir': str(directory), 'attempt_dir': str(attempt),
                    'resume': args.resume, 'online_e2e_seconds': state.get('online_e2e_seconds', time.monotonic() - began),
                    'first_original_body_seconds': state['first_original_body_seconds']},
            'frozen_inputs': frozen, 'roster': roster, 'plan': plan, 'source_config': config,
            'source_hashes_before': hashes, 'summary': summary, 'counts': counts,
            'timings_seconds': {**phase_seconds, 'total_until_report': time.monotonic() - began},
            'phase_results': phase_results, 'coverage': acquisition_coverage(engine.store, news.sources()),
            'people_results': people_results, 'original_bodies': bodies, 'current_candidates': candidates,
            'query_results': queries, 'jobs': jobs, 'audit': audit, 'network_state': network_state(directory),
            'source_results': source_results(config, queries, budget.events, fetches, engine.store),
            'actual_http_history': history, 'session_http_events': budget.events, 'session_fetch_events': fetches,
            'resume_proof': {'previous_done_jobs': len(previous_done), 'preserved_done_jobs': len(previous_done & current_done),
                             'previous_http_requests': prior_requests, 'same_frozen_plan': True},
            'operational_checks': checks, 'operational_passed': all(checks.values()),
            'partial': bool(partial_reasons), 'partial_reasons': partial_reasons, 'failure': failure,
            'limitations': ['Name search and detected mentions do not certify identity or an exhaustive historical search.',
                'Computed scores without assessable evidence remain insufficient/pending; they are not clean records.',
                'No precision, recall or accuracy is inferred without independently reviewed original texts and reference denominators.',
                'Original texts and individual scoring explanations/exports stay local; public reporting must aggregate them.',
                'The real deadline stops new transport and cooperative processing; a currently executing unit may finish after it.']}
        write_json(attempt / 'report.json', report)
        write_json(output, report)
        state.update(phase='finished', operational_passed=report['operational_passed'], partial=report['partial'])
        safe_checkpoint(engine, attempt, state)
    print(json.dumps({'output': str(output), 'attempt_dir': str(attempt), 'summary': summary,
                      'operational_checks': checks, 'partial_reasons': partial_reasons}, ensure_ascii=False), flush=True)
    return report


def parser():
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument('--online', action='store_true')
    value.add_argument('--roster', type=Path, required=True)
    value.add_argument('--sources-config', type=Path, required=True)
    value.add_argument('--data-dir', type=Path, required=True)
    value.add_argument('--output', type=Path, required=True)
    value.add_argument('--resume', action='store_true')
    value.add_argument('--seconds', type=float, default=2400, help='Real online E2E wall budget; processing cooperatively stops at this deadline')
    value.add_argument('--acquire-seconds', type=float, default=2100, help='Real acquisition budget, leaving time for actual attribution/scoring/export')
    value.add_argument('--max-http', type=int, default=5000, help='Cumulative actual HTTP limit across all resumptions, including robots and redirects')
    value.add_argument('--workers', type=int, default=8)
    value.add_argument('--body-budget', type=int, default=1800)
    value.add_argument('--per-entity-body-limit', type=int, default=12)
    value.add_argument('--max-jobs', type=int, default=30000)
    value.add_argument('--local-seconds', type=float, default=60)
    value.add_argument('--skip-local-validation', action='store_true')
    value.add_argument('--allow-small-roster', action='store_true', help='Explicit real pilot, never represented as the full 300-person experiment')
    value.add_argument('--plan-only', action='store_true', help='Validate and print the complete frozen nominal plan without opening a bank or requesting the web')
    return value


def main():
    value = parser()
    args = value.parse_args()
    if (not args.online and not args.plan_only or not math.isfinite(args.seconds) or not 0 < args.seconds <= 2400
            or not math.isfinite(args.acquire_seconds) or not 0 < args.acquire_seconds <= args.seconds
            or not 1 <= args.max_http <= 5000 or not 1 <= args.workers <= 16
            or not 0 <= args.body_budget <= 5000 or not 0 <= args.per_entity_body_limit <= 100
            or not 1 <= args.max_jobs <= 100000 or not math.isfinite(args.local_seconds) or not 0 < args.local_seconds <= 300):
        value.error('Use --online, hasta 2400 s reales/5000 HTTP acumuladas/16 workers y límites finitos.')
    try:
        if args.plan_only:
            people, cases, roster, config, plan, frozen = prepare_inputs(args)
            print(json.dumps({'frozen_inputs': frozen, 'people': len(people), 'groups': roster['groups'],
                'queries': len(plan['queries']), 'entities_planned': plan['entities_planned'],
                'truncated': plan['truncated'], 'source_ids': [source['id'] for source in config['sources'] if source.get('enabled')],
                'network_requests': 0, 'bank_opened': False}, ensure_ascii=False, indent=2))
            return
        report = run(args)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        value.error(str(exc))
    if not report['operational_passed']:
        raise SystemExit(1)
    if report['partial']:
        raise SystemExit(2)


if __name__ == '__main__':
    main()
