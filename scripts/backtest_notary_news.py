"""Bounded real-web acquisition for a frozen public-notary backtest cohort.

The input labels and reference articles are never supplied to the search plan.
This runner measures acquisition, not identity or incident classification. Full
responses and texts stay in the isolated runtime directory for independent
review; relevance and incident judgments are deliberately left unknown.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import sys
import threading
import time
from unittest.mock import patch
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from peps.acquisition_coverage import acquisition_coverage
from peps.collector import SafeFetcher
from peps.engine import Engine
from peps.news import NewsAcquirer
from peps.news_search import build_search_plan, normalize_search_config
from peps.store import canonical, now
from scripts.validate_notary_search import _body_proof, _candidate_rows, _fold
from scripts.validate_real_people import (
    cached_snapshot, copy_bank, corpus_snapshot, db_counts, http_history,
    network_state, queue_receipt, request_key, source_hashes,
)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def load_cohort(path):
    """Strip labels and gold URLs from the entities consumed by production."""
    raw = json.loads(path.read_text(encoding='utf-8-sig'))
    rows = raw.get('entities') if isinstance(raw, dict) else raw
    if not isinstance(rows, list) or not rows:
        raise ValueError('La cohorte requiere entities: una lista no vacía.')
    people, cases, identifiers, names = [], [], set(), set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('Cada entidad debe ser un objeto.')
        identifier, name = row.get('id'), row.get('name')
        if (not isinstance(identifier, str) or not identifier.strip() or len(identifier) > 150
                or not isinstance(name, str) or len(name.split()) < 2 or len(name) > 300):
            raise ValueError('Cada entidad requiere id único y nombre completo.')
        if identifier in identifiers or _fold(name) in names:
            raise ValueError('La cohorte contiene IDs o nombres repetidos.')
        aliases = row.get('aliases') or []
        context = row.get('context') or {}
        if not isinstance(context, dict) or not isinstance(aliases, list):
            raise ValueError('context debe ser objeto y aliases debe ser lista.')
        if (len(aliases) > 50 or any(not isinstance(alias, str) or not alias.strip()
                                   or len(alias) > 300 for alias in aliases)):
            raise ValueError('Alias inválidos en la cohorte.')
        # Provenance is retained for audit, but use_context=False means that
        # even a source_url in context cannot affect a nominal query.
        people.append({'id': identifier, 'name': name.strip(), 'kind': 'person',
                       'aliases': deepcopy(aliases), 'context': deepcopy(context)})
        cases.append({'entity_id': identifier, 'name': name.strip(),
                      'group': row.get('group') or row.get('cohort') or row.get('entity_group')})
        identifiers.add(identifier)
        names.add(_fold(name))
    return people, cases


def effective_config(args):
    config = json.loads(args.sources_config.read_text(encoding='utf-8-sig'))
    sources = config.get('sources') if isinstance(config, dict) else None
    if not isinstance(sources, list) or not sources:
        raise ValueError('Se requiere un catálogo congelado con sources.')
    enabled = [source for source in sources if source.get('enabled')]
    if not enabled:
        raise ValueError('El catálogo no tiene fuentes habilitadas.')
    identifiers = [source.get('id') for source in sources]
    if any(not isinstance(identifier, str) or not identifier for identifier in identifiers) or len(set(identifiers)) != len(identifiers):
        raise ValueError('Las fuentes requieren IDs únicos.')
    for source in enabled:
        if source.get('terms_approved') is not True or source.get('robots_required') is not True:
            raise ValueError('Cada fuente habilitada requiere revisión registrada y robots obligatorio.')
        if source.get('download_articles') and not (source.get('article_terms_approved') is True
                                                   and source.get('body_terms_approved') is True):
            raise ValueError('La conservación de cuerpos requiere sus permisos separados.')
        for field in ('url', 'base_url', 'search_url', 'api_url'):
            if source.get(field):
                parts = urlsplit(source[field])
                if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password:
                    raise ValueError('Sólo endpoints públicos HTTPS sin credenciales.')
    settings = dict(config.get('search') or {})
    if settings.get('strategy', 'targeted') != 'targeted':
        raise ValueError('Esta prueba usa búsqueda dirigida, sin recorrer índices generales.')
    # Labels, incident keywords and reference URLs never become query inputs.
    settings.update(strategy='targeted', preset='identity', use_context=False)
    if args.max_queries is not None:
        settings['max_queries'] = args.max_queries
    if args.start is not None:
        settings['start'] = args.start
    if args.end is not None:
        settings['end'] = args.end
    config['search'] = normalize_search_config(settings)
    return config


class BudgetStop(RuntimeError):
    """Instrumentation boundary; not a fabricated upstream response."""


class RequestBudget:
    """Reserve transports atomically, including robots and redirect hops."""
    def __init__(self, maximum, deadline):
        self.maximum, self.deadline = maximum, deadline
        self.lock = threading.Lock()
        self.events = []
        self.reason = None

    def exhausted(self):
        with self.lock:
            if len(self.events) >= self.maximum or time.monotonic() >= self.deadline:
                self.reason = 'http_budget' if len(self.events) >= self.maximum else 'wall_deadline'
                return True
            return False

    def reserve(self, url, parent_url, source_id):
        with self.lock:
            if len(self.events) >= self.maximum or time.monotonic() >= self.deadline:
                self.reason = 'http_budget' if len(self.events) >= self.maximum else 'wall_deadline'
                raise BudgetStop(self.reason)
            event = {'request_id': len(self.events) + 1, 'url': url,
                     'parent_fetch_url': parent_url, 'source_id': source_id,
                     'started_at': now(), 'http_status': None,
                     'robots_request': urlsplit(url).path == '/robots.txt'}
            self.events.append(event)
            return event


def budget_result(url, requests=0):
    return {'url': url, 'status': 'deferred', 'body': '', 'http_status': None,
            'error': 'Frozen backtest HTTP/time budget reached; pending work retained',
            'error_code': 'session_limit', 'retry_at': time.time(),
            'network_requests': requests, 'request_attempted': requests > 0}


def observed_fetcher(engine, budget, fetches, directory):
    fetch_lock = threading.Lock()
    captures = directory / 'response_captures'
    captures.mkdir()

    class ObservedFetcher(SafeFetcher):
        def _transport(self, url, headers, timeout, max_bytes, addresses):
            try:
                event = budget.reserve(url, getattr(self._local, 'backtest_url', url),
                                       getattr(self._local, 'backtest_source', None))
            except BudgetStop:
                engine.stop_event.set()
                raise
            self._local.backtest_requests += 1
            began = time.monotonic()
            try:
                status, response_headers, body = super()._transport(
                    url, headers, min(timeout, max(.001, budget.deadline - began)), max_bytes, addresses)
                digest = sha256(body)
                path = captures / f"{event['request_id']:06d}-{digest}.response.bin"
                path.write_bytes(body)
                event.update(http_status=status, response_bytes=len(body), response_sha256=digest,
                             response_capture_path=str(path.relative_to(directory)))
                return status, response_headers, body
            except Exception as exc:
                event['error_type'] = type(exc).__name__
                raise
            finally:
                event['seconds'] = round(time.monotonic() - began, 6)

        def fetch(self, url, source):
            self._local.backtest_requests = 0
            self._local.backtest_url, self._local.backtest_source = url, source['id']
            if budget.exhausted():
                engine.stop_event.set()
                result = budget_result(url)
            else:
                bounded = dict(source, _deadline_monotonic=min(source.get('_deadline_monotonic', budget.deadline), budget.deadline))
                try:
                    result = super().fetch(url, bounded)
                except BudgetStop:
                    result = budget_result(url, self._local.backtest_requests)
            record = {'url': url, 'source_id': source['id'], **{key: result.get(key) for key in (
                'status', 'http_status', 'error_code', 'error', 'retry_at', 'network_requests',
                'cache_status', 'from_cache', 'elapsed_seconds')}}
            if result.get('body'):
                record['decoded_text_sha256'] = sha256(result['body'].encode())
            with fetch_lock:
                fetches.append(record)
            return result

    return ObservedFetcher


def export_articles(engine, directory):
    texts = directory / 'review_texts'
    texts.mkdir()
    candidates = _candidate_rows(engine)
    pairs = defaultdict(set)
    for candidate in candidates:
        pairs[candidate['article_id']].add(candidate['entity_id'])
    articles, retrieved = [], []
    with engine.store.connection() as con:
        for value in con.execute('SELECT n.*,d.text,d.content_hash FROM news_articles n '
                                 'LEFT JOIN documents d ON d.id=n.document_id ORDER BY n.id'):
            row = dict(value)
            full = row['scope'] == 'full_article' and bool(row['text'])
            text_path = None
            if full:
                path = texts / (sha256(row['document_id'].encode()) + '.txt')
                path.write_text(row['text'], encoding='utf-8')
                text_path = str(path.relative_to(directory))
            article = {key: row[key] for key in ('id', 'canonical_url', 'publisher_id', 'source_id',
                       'title', 'summary', 'published_at', 'document_id', 'scope', 'status', 'error')}
            article.update(has_text=full, body_chars=len(row['text'] or '') if full else 0,
                           body_sha256=row['content_hash'] if full else None, text_path=text_path,
                           candidate_entity_ids=sorted(pairs[row['id']]))
            articles.append(article)
            for identifier in sorted(pairs[row['id']]):
                retrieved.append({'entity_id': identifier, 'url': row['canonical_url'],
                    'article_id': row['id'], 'document_id': row['document_id'],
                    'published_at': row['published_at'], 'has_text': full,
                    'body_sha256': article['body_sha256'], 'text_path': text_path,
                    'relevance': 'unknown', 'incident': 'unknown', 'reviewed_independently': False})
    return articles, retrieved, candidates


def person_results(cases, jobs, events, candidates, articles, store):
    by_article = {article['id']: article for article in articles}
    by_person = defaultdict(set)
    for candidate in candidates:
        by_person[candidate['entity_id']].add(candidate['article_id'])
    with store.connection() as con:
        receipts = {row[0] for row in con.execute('SELECT job_id FROM news_discovery_runs')}
    queries = [(job, json.loads(job['payload'])) for job in jobs if job['kind'] == 'news_query']
    rows = []
    for case in cases:
        selected = [(job, payload) for job, payload in queries if case['entity_id'] in payload.get('entity_ids', [])]
        query_urls = {request_key(payload['url']) for _, payload in selected}
        body_urls = {request_key(by_article[identifier]['canonical_url']) for identifier in by_person[case['entity_id']]}
        rows.append({**case, 'queries_planned': len(selected),
            'queries_completed': sum(job['id'] in receipts for job, _ in selected),
            'query_status_counts': dict(Counter(job['status'] for job, _ in selected)),
            'query_http_requests': sum(not event['robots_request'] and request_key(event['parent_fetch_url']) in query_urls for event in events),
            'body_http_requests': sum(not event['robots_request'] and request_key(event['parent_fetch_url']) in body_urls for event in events),
            'candidate_articles': len(by_person[case['entity_id']]),
            'original_bodies': len({by_article[identifier]['document_id'] for identifier in by_person[case['entity_id']]
                                    if by_article[identifier]['has_text']}),
            'identity_confirmed': False, 'score_computed': False})
    return rows


def source_results(config, jobs, events, candidates, articles, store):
    """Simultaneous source contributions, including overlap, not a speed A/B."""
    by_article = {article['id']: article for article in articles}
    current = {candidate['article_id'] for candidate in candidates}
    observed = defaultdict(set)
    records, receipts = Counter(), set()
    with store.connection() as con:
        for row in con.execute('SELECT source_id,article_id FROM news_observations'):
            observed[row['source_id']].add(row['article_id'])
        for row in con.execute('SELECT job_id,source_id,records FROM news_discovery_runs'):
            receipts.add(row['job_id'])
            records[row['source_id']] += row['records']
    queries = [(job, json.loads(job['payload'])) for job in jobs if job['kind'] == 'news_query']
    rows, partitions = [], defaultdict(lambda: {'urls': set(), 'documents': set(), 'sources': []})
    for source in config['sources']:
        if not source.get('enabled'):
            continue
        identifier = source['id']
        selected = [(job, payload) for job, payload in queries if payload.get('source_id') == identifier]
        ids = observed[identifier] & current
        urls = {by_article[article_id]['canonical_url'] for article_id in ids}
        documents = {by_article[article_id]['document_id'] for article_id in ids if by_article[article_id]['has_text']}
        group = source.get('backtest_partition', 'unassigned')
        partitions[group]['urls'].update(urls)
        partitions[group]['documents'].update(documents)
        partitions[group]['sources'].append(identifier)
        rows.append({'source_id': identifier, 'partition': group,
            'body_download_approved': bool(source.get('download_articles') and source.get('article_terms_approved') and source.get('body_terms_approved')),
            'queries_planned': len(selected), 'queries_completed': sum(job['id'] in receipts for job, _ in selected),
            'query_status_counts': dict(Counter(job['status'] for job, _ in selected)),
            'discovery_records': records[identifier],
            'http_requests_including_robots': sum(event['source_id'] == identifier for event in events),
            'robots_requests': sum(event['source_id'] == identifier and event['robots_request'] for event in events),
            'candidate_urls': len(urls), 'candidate_entity_article_pairs': sum(candidate['article_id'] in ids for candidate in candidates),
            'candidate_original_documents': len(documents), 'observed_current_candidate_urls': sorted(urls)})
    baseline = partitions.get('baseline_three_publishers', {'urls': set(), 'documents': set()})
    partition_rows = [{'partition': group, 'source_ids': sorted(value['sources']),
        'unique_candidate_urls': len(value['urls']), 'unique_original_documents': len(value['documents']),
        'candidate_urls_outside_baseline': len(value['urls'] - baseline['urls']),
        'original_documents_outside_baseline': len(value['documents'] - baseline['documents'])}
        for group, value in sorted(partitions.items())]
    return rows, {'design': 'Simultaneous source contribution; overlapping URLs count once per partition. This is not a causal timing comparison.',
                  'partitions': partition_rows}


def check_paths(args):
    directory, output = args.data_dir.resolve(), args.output.resolve()
    local = (args.local_dir or directory.with_name(directory.name + '-offline')).resolve()
    if directory == local or directory in local.parents or local in directory.parents:
        raise ValueError('Los bancos frío y local deben ser carpetas independientes.')
    if any('real-300' in path.parts for path in (directory, local, output)):
        raise ValueError('No se modifican los bancos de validación anteriores.')
    if output.exists() or local.exists() or directory.exists():
        raise ValueError('Use rutas nuevas: no se sobrescriben bancos ni informes.')
    inputs = (args.cohort.resolve(), args.sources_config.resolve())
    if any(path == output or directory == path or directory in path.parents or local == path or local in path.parents for path in inputs):
        raise ValueError('Los insumos congelados deben quedar fuera de los bancos nuevos.')
    return directory, local, output


def run(args):
    directory, local_directory, output = check_paths(args)
    people, cases = load_cohort(args.cohort)
    config = effective_config(args)
    # Planning is entirely local and happens before the runtime directory is
    # created. Gold references and incident group names are not passed here.
    plan = build_search_plan(people, config['sources'], config['search'])
    if any(query.get('kind') != 'news_query' for query in plan['queries']):
        raise ValueError('El plan incluyó una tarea de índice general no autorizada por esta prueba.')
    if not plan['queries']:
        raise ValueError('Las fuentes no producen consultas nominales; revise el catálogo.')
    directory.mkdir(parents=True)
    frozen = {'cohort_sha256': sha256(args.cohort.read_bytes()),
              'source_config_input_sha256': sha256(args.sources_config.read_bytes()),
              'effective_source_config_sha256': sha256(canonical(config).encode()),
              'search_entities_sha256': sha256(canonical(people).encode()),
              'plan_sha256': sha256(canonical(plan).encode())}
    write_json(directory / 'frozen_inputs.json', {'hashes': frozen, 'sources': config,
               'search_entities': people, 'plan': plan, 'arguments': {key: str(value) if isinstance(value, Path) else value
               for key, value in vars(args).items()}})
    engine = Engine(directory, online=True)
    news = NewsAcquirer(engine)
    engine.store.import_entities(people)
    write_json(news.config_path, config)
    def measured_hashes():
        paths = (Path(__file__), ROOT / 'scripts/validate_notary_search.py', ROOT / 'scripts/validate_real_people.py')
        return source_hashes() | {str(path.relative_to(ROOT)): sha256(path.read_bytes()) for path in paths}
    hashes = measured_hashes()
    engine.store.audit('notary_backtest_started', {**frozen, 'entities': len(people),
        'no_identity_or_scoring': True, 'body_budget': args.body_budget,
        'scope': 'Isolated public technical backtest, no licence or identity inferred'})
    began, started_at = time.monotonic(), now()
    budget = RequestBudget(args.max_http, began + args.seconds)
    fetches, progress, first_body = [], [], [None]
    last_print = [0.]

    def observe(event):
        progress.append(event)
        if (first_body[0] is None and event.get('job_kind') == 'news_article'
                and event.get('task_event') == 'finish' and event.get('job_status') == 'done'):
            with engine.store.connection() as con:
                if con.execute("SELECT 1 FROM news_articles WHERE scope='full_article' AND document_id IS NOT NULL LIMIT 1").fetchone():
                    first_body[0] = time.monotonic() - began
        if time.monotonic() - last_print[0] >= 20 or event.get('event') == 'finish':
            last_print[0] = time.monotonic()
            print(json.dumps({'elapsed_seconds': round(time.monotonic() - began, 2),
                'http_requests': len(budget.events), 'first_body_seconds': first_body[0],
                **{key: event.get(key) for key in ('completed', 'pending', 'active_tasks', 'stop_reason', 'message')}},
                ensure_ascii=False), flush=True)

    Observed = observed_fetcher(engine, budget, fetches, directory)
    with patch('peps.collector.SafeFetcher', Observed):
        cold = news.acquire(workers=args.workers, seconds=args.seconds, max_articles=args.max_articles,
            queue=True, window='notary-backtest-frozen', relevant_only=True, body_budget=args.body_budget,
            per_entity_body_limit=args.per_entity_body_limit, cache_mode='reuse', progress=observe)
    cold_seconds, cold_completed_at = time.monotonic() - began, now()
    counts, coverage, jobs = db_counts(engine.store), acquisition_coverage(engine.store, news.sources()), queue_receipt(engine.store)
    articles, retrieved, candidates = export_articles(engine, directory)
    bodies = _body_proof(engine, people)
    persons = person_results(cases, jobs, budget.events, candidates, articles, engine.store)
    sources, source_comparison = source_results(config, jobs, budget.events, candidates, articles, engine.store)
    corpus, cache, network = corpus_snapshot(engine), cached_snapshot(directory), network_state(directory)
    copy_bank(directory, local_directory)
    local_engine = Engine(local_directory, online=False)
    local_news = NewsAcquirer(local_engine)
    warm_started = time.monotonic()
    with patch('socket.getaddrinfo', side_effect=AssertionError('DNS forbidden during warm validation')) as dns, \
         patch('socket.socket.connect', side_effect=AssertionError('TCP forbidden during warm validation')) as tcp, \
         patch.object(SafeFetcher, '_transport', side_effect=AssertionError('HTTP forbidden during warm validation')) as transport:
        warm_results = []
        snapshots, projections = [], []
        for pass_number in range(2):
            warm_results.append(local_news.acquire(workers=args.workers, seconds=args.local_seconds,
                max_articles=args.max_articles, queue=True, window=f'notary-backtest-local-{pass_number}',
                relevant_only=True, body_budget=args.body_budget,
                per_entity_body_limit=args.per_entity_body_limit, cache_mode='local'))
            snapshots.append(corpus_snapshot(local_engine))
            projections.append(_candidate_rows(local_engine))
    warm_seconds = time.monotonic() - warm_started
    local_counts, after_hashes = db_counts(local_engine.store), measured_hashes()
    history = http_history(directory)
    checks = {
        'all_cohort_entities_imported': counts['entities'] == len(people),
        'request_budget_respected_including_robots': len(budget.events) <= args.max_http,
        'transport_observations_match_durable_http_count': len(budget.events) == sum(row['network_requests'] for row in history),
        'no_duplicate_canonical_urls': len({article['canonical_url'] for article in articles}) == counts['news_articles'],
        'no_duplicate_entity_article_pairs': len({(row['entity_id'], row['url']) for row in retrieved}) == len(retrieved),
        'original_snapshots_hash_valid': all(body['raw_snapshot_hash_valid'] for body in bodies),
        'captured_transport_response_hashes_valid': all(sha256((directory / event['response_capture_path']).read_bytes()) == event['response_sha256']
            for event in budget.events if event.get('response_capture_path')),
        'no_explicit_paywall_metadata': all(not body['explicit_paywall_metadata'] for body in bodies),
        'no_identity_attribution_or_scoring': all(counts[table] == local_counts[table] == 0 for table in
            ('evidence', 'news_identity_links', 'scores', 'score_runs')),
        'offline_zero_dns_tcp_http': dns.call_count == tcp.call_count == transport.call_count == 0,
        'offline_no_new_documents_articles_observations': all(local_counts[table] == counts[table] for table in
            ('news_articles', 'news_observations', 'documents')),
        'offline_documents_observations_unchanged': all(snapshots[0][table] == corpus[table] for table in ('documents', 'news_observations')),
        'offline_repeat_corpus_idempotent': all(snapshots[0][table] == snapshots[1][table] for table in corpus if table != 'audit'),
        'offline_projection_stable': candidates == projections[0] == projections[1],
        'offline_cache_bytes_unchanged': cached_snapshot(local_directory) == cache,
        'offline_no_recorded_http': network_state(local_directory)['recorded_http_requests'] == network['recorded_http_requests'],
        'cold_bank_unchanged_by_offline_validation': corpus_snapshot(engine) == corpus and cached_snapshot(directory) == cache and network_state(directory) == network,
        'cold_audit_valid': engine.store.verify_audit()['ok'],
        'offline_audit_valid': local_engine.store.verify_audit()['ok'],
        'all_production_modules_stable_during_measurement': hashes == after_hashes,
        'effective_cold_source_config_unchanged': sha256(canonical(news.config()).encode()) == frozen['effective_source_config_sha256'],
        'frozen_inputs_unchanged': frozen['cohort_sha256'] == sha256(args.cohort.read_bytes()) and
            frozen['source_config_input_sha256'] == sha256(args.sources_config.read_bytes()),
    }
    report = {'schema_version': 1, 'generated_at': now(),
        'scope': 'Production directed acquisition against a frozen public-notary cohort; independent judgments pending.',
        'run': {'started_at': started_at, 'cold_completed_at': cold_completed_at, 'completed_at': now(),
                'data_dir': str(directory), 'offline_dir': str(local_directory)},
        'frozen_inputs': frozen, 'source_config': config, 'source_hashes_before': hashes,
        'source_hashes_after': after_hashes, 'plan': plan, 'cold_acquisition': cold,
        'coverage': coverage, 'counts': counts, 'person_results': persons,
        'source_results': sources, 'source_comparison': source_comparison,
        'article_results': articles, 'retrieved': retrieved, 'current_candidate_projection': candidates,
        'original_bodies': bodies, 'http_events': budget.events, 'fetch_events': fetches,
        'actual_http_history': history, 'jobs': jobs, 'progress': progress,
        'operational_summary': {'unique_candidate_urls': len(articles),
            'unique_original_documents': len({row['document_id'] for row in articles if row['has_text']}),
            'entity_candidate_pairs': len(retrieved), 'entity_text_pairs': sum(row['has_text'] for row in retrieved),
            'entities_with_text': sum(row['original_bodies'] > 0 for row in persons),
            'actual_http_requests': len(budget.events), 'robots_requests': sum(row['robots_request'] for row in budget.events),
            'transport_budget_stop_reason': budget.reason,
            'job_status_counts': dict(Counter(job['status'] for job in jobs)),
            'known_queue_drained_without_errors': not cold.get('pending') and not cold.get('failed') and not plan.get('truncated'),
            'historical_coverage_complete': False},
        'timings_seconds': {'first_original_body_durable_job_complete': first_body[0],
            'cold_acquisition': cold_seconds, 'offline_reuse_and_repeat': warm_seconds, 'total': time.monotonic() - began},
        'offline_validation': {'passes': warm_results, 'counts': local_counts, 'dns_calls': dns.call_count,
            'tcp_calls': tcp.call_count, 'http_transport_calls': transport.call_count},
        'operational_checks': checks, 'operational_passed': all(checks.values()),
        'limitations': ['Operational checks do not establish retrieval quality, incident status, identity or risk.',
            'Relevance and incident labels remain unknown until independent original-text review.',
            'A no-known-incident sampling group is not proof of a clean record.',
            'Top-result limits, source availability, robots and the frozen budget can leave incomplete coverage.',
            'Original responses and texts stay in the isolated runtime bank and must not be committed to Git.']}
    write_json(output, report)
    print(json.dumps({'report': str(output), 'operational_passed': report['operational_passed'],
                      'summary': report['operational_summary'], 'timings_seconds': report['timings_seconds']}, ensure_ascii=False), flush=True)
    return report


def parser():
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument('--online', action='store_true', help='Authorize this bounded real-web experiment')
    value.add_argument('--cohort', type=Path, required=True)
    value.add_argument('--sources-config', type=Path, required=True)
    value.add_argument('--data-dir', type=Path, required=True)
    value.add_argument('--local-dir', type=Path)
    value.add_argument('--output', type=Path, required=True)
    value.add_argument('--seconds', type=float, default=900)
    value.add_argument('--local-seconds', type=float, default=60)
    value.add_argument('--max-http', type=int, default=400)
    value.add_argument('--body-budget', type=int, default=200)
    value.add_argument('--per-entity-body-limit', type=int, default=15)
    value.add_argument('--max-articles', type=int, default=2000, help='Maximum queue jobs processed, not a coverage claim')
    value.add_argument('--max-queries', type=int)
    value.add_argument('--workers', type=int, default=4)
    value.add_argument('--start')
    value.add_argument('--end')
    return value


def main():
    value = parser()
    args = value.parse_args()
    if (not args.online or not math.isfinite(args.seconds) or not 0 < args.seconds <= 7200
            or not math.isfinite(args.local_seconds) or not 0 < args.local_seconds <= 300
            or not 1 <= args.max_http <= 10000 or not 0 <= args.body_budget <= 5000
            or not 0 <= args.per_entity_body_limit <= 500 or not 1 <= args.max_articles <= 100000
            or not 1 <= args.workers <= 16 or args.max_queries is not None and not 1 <= args.max_queries <= 10000):
        value.error('Use --online y presupuestos finitos: 7200s, 10000 HTTP, 5000 cuerpos, 16 workers como máximo.')
    try:
        report = run(args)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        value.error(str(exc))
    if not report['operational_passed']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
