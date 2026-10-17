"""Opt-in real publisher search for six public Mexican notaries.

Uses the production planner, durable queue, SafeFetcher and original-article
conservation. It never attributes identity or calculates scores. A separate
copy verifies cache reuse while DNS, TCP and HTTP transports are forbidden.
Runtime-only captures are not republished or added to the source catalogue.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import gzip
import hashlib
import json
from pathlib import Path
import re
import sys
import threading
import time
import unicodedata
from unittest.mock import patch
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from peps.acquisition_coverage import acquisition_coverage
from peps.collector import SafeFetcher
from peps.engine import Engine
from peps.news import NewsAcquirer
from peps.relevance import RelevanceIndex
from peps.store import now
from scripts.validate_real_people import (
    article_report, cached_snapshot, copy_bank, corpus_snapshot, db_counts,
    http_history, network_state, queue_receipt, source_hashes,
)

NOTARY_IDS = (
    'mx-notary-cdmx-001', 'mx-notary-cdmx-128', 'mx-notary-cdmx-129',
    'mx-notary-cdmx-095', 'mx-notary-cdmx-012', 'mx-notary-cdmx-062',
)
PIPELINE_MODULES = (
    'peps/collector.py', 'peps/news.py', 'peps/news_search.py',
    'peps/news_projection.py', 'peps/acquisition_coverage.py',
    'peps/shared_crawl.py', 'peps/shared_discovery.py', 'peps/relevance.py',
    'peps/sources.py', 'peps/news_session.py', 'peps/engine.py', 'peps/store.py',
)


class _ProbeBudgetStop(RuntimeError):
    """An instrumentation boundary, never a fabricated HTTP response."""


def _budget_result(url, requests=0):
    return {'url': url, 'status': 'deferred', 'body': '', 'http_status': None,
            'error': 'Explicit notary validation request/time budget reached',
            'error_code': 'session_limit', 'retry_at': time.time(),
            'network_requests': requests, 'request_attempted': requests > 0}


def _fold(value):
    return ' '.join(''.join(char for char in unicodedata.normalize('NFD', value.casefold())
                            if not unicodedata.combining(char)).split())


def make_config(args):
    config = json.loads((ROOT / 'peps/defaults/news_sources.json').read_text(encoding='utf-8'))
    source = deepcopy(next(item for item in config['sources'] if item['id'] == 'contralinea_index'))
    source.update(enabled=True, terms_approved=True, robots_required=True,
                  download_articles=True, article_terms_approved=True, body_terms_approved=True,
                  search_max_records=5, timeout_seconds=12)
    # These flags apply solely to the isolated user-authorized technical probe.
    # They do not establish a commercial licence or alter application defaults.
    config['sources'] = [source]
    config['user_agent'] = 'WebScope-Notary-Pipeline-Validation/2.9 (+https://github.com/giovannyTorres/web_scraping_peps)'
    config['search'].update(strategy='targeted', preset='identity', include_aliases=False,
                            use_context=False, execution_mode='quick', session_seconds=120,
                            max_queries=6, start=args.start, end=args.end)
    return config


def observed_fetcher(engine, args, events, fetches, directory, deadline):
    lock = threading.Lock()

    class ObservedFetcher(SafeFetcher):
        def _transport(self, url, headers, timeout, max_bytes, addresses):
            with lock:
                if len(events) >= args.max_requests or time.monotonic() >= deadline:
                    engine.stop_event.set()
                    raise _ProbeBudgetStop()
                event = {'url': url, 'started_at': now(), 'http_status': None}
                events.append(event)
                self._local.notary_events.append(event)
            started = time.monotonic()
            try:
                status, response_headers, body = super()._transport(
                    url, headers, min(timeout, max(.001, deadline - started)), max_bytes, addresses)
                event.update(http_status=status, decoded_bytes=len(body),
                             response_sha256=hashlib.sha256(body).hexdigest())
                return status, response_headers, body
            except Exception as exc:
                event['error_type'] = type(exc).__name__
                raise
            finally:
                event['seconds'] = round(time.monotonic() - started, 6)

        def fetch(self, url, source):
            self._local.notary_events = []
            with lock:
                stopped = len(events) >= args.max_requests or time.monotonic() >= deadline
            if stopped:
                engine.stop_event.set()
                result = _budget_result(url)
            else:
                bounded = dict(source, _deadline_monotonic=min(source.get('_deadline_monotonic', deadline), deadline))
                try:
                    result = super().fetch(url, bounded)
                except _ProbeBudgetStop:
                    result = _budget_result(url, len(self._local.notary_events))
            record = {'url': url, 'source_id': source['id'],
                      **{key: result.get(key) for key in ('status', 'http_status', 'error_code', 'error',
                          'retry_at', 'network_requests', 'cache_status', 'from_cache', 'elapsed_seconds')}}
            body = result.get('body')
            if body and result.get('status') in ('ok', 'not_modified'):
                captures = directory / 'validation_captures'
                captures.mkdir(exist_ok=True)
                name = hashlib.sha256(url.encode()).hexdigest() + ('.json' if '/wp-json/' in url else '.html')
                (captures / name).write_text(body, encoding='utf-8')
                record['capture_path'] = str((captures / name).relative_to(directory))
                record['capture_text_sha256'] = hashlib.sha256(body.encode()).hexdigest()
            with lock:
                fetches.append(record)
            return result

    return ObservedFetcher


def _body_proof(engine, people):
    """Report original-body hashes and matches, without exporting article text."""
    index = RelevanceIndex(people)
    folded_names = {person['id']: _fold(person['name']) for person in people}
    rows = []
    with engine.store.connection() as con:
        for row in con.execute('SELECT n.id,n.canonical_url,n.title,n.summary,n.published_at,n.scope,'
                               'n.document_id,d.text,d.content_hash,d.metadata FROM news_articles n '
                               'JOIN documents d ON d.id=n.document_id WHERE n.scope=\'full_article\' ORDER BY n.id'):
            metadata = json.loads(row['metadata'])
            snapshot_hash = metadata.get('raw_snapshot_sha256')
            snapshot = engine.data_dir / 'news_snapshots' / str(snapshot_hash)[:2] / (str(snapshot_hash) + '.html.gz')
            raw = gzip.decompress(snapshot.read_bytes()).decode('utf-8') if snapshot.exists() else ''
            body_rank = index.rank('', row['text'])
            metadata_rank = index.rank(row['title'], row['summary'])
            folded_text = _fold(row['text'])
            rows.append({key: row[key] for key in ('id', 'canonical_url', 'published_at', 'scope', 'document_id')} |
                        {'body_chars': len(row['text']), 'body_sha256': row['content_hash'],
                         'raw_snapshot_sha256': snapshot_hash,
                         'raw_snapshot_hash_valid': bool(raw) and hashlib.sha256(raw.encode()).hexdigest() == snapshot_hash,
                         'explicit_paywall_metadata': bool(re.search(r'"isAccessibleForFree"\s*:\s*(?:false|"false")', raw, re.I)),
                         'literal_name_mentions': {identifier: folded_text.count(name) for identifier, name in folded_names.items()},
                         'metadata_relevance': metadata_rank, 'original_body_relevance': body_rank,
                         'identity_confirmed': False, 'scored': False})
    return rows


def _candidate_rows(engine):
    with engine.store.connection() as con:
        return [dict(row) for row in con.execute('SELECT * FROM news_current_candidates ORDER BY article_id,entity_id')]


def _people_report(engine, people, jobs, events):
    with engine.store.connection() as con:
        receipts = {row[0] for row in con.execute('SELECT job_id FROM news_discovery_runs')}
        candidates = list(con.execute('SELECT c.entity_id,c.article_id,n.scope,n.document_id '
            'FROM news_current_candidates c JOIN news_articles n ON n.id=c.article_id'))
    queries = [(job, json.loads(job['payload'])) for job in jobs if job['kind'] == 'news_query']
    rows = []
    for person in people:
        selected = [(job, payload) for job, payload in queries if person['id'] in payload.get('entity_ids', [])]
        rows.append({'id': person['id'], 'name': person['name'], 'queries_planned': len(selected),
                     'queries_completed': sum(job['id'] in receipts for job, _ in selected),
                     'query_http_requests': sum(event['url'] == payload['url'] for _, payload in selected for event in events),
                     'relevant_candidates': len({row['article_id'] for row in candidates if row['entity_id'] == person['id']}),
                     'relevant_original_bodies': len({row['document_id'] for row in candidates
                         if row['entity_id'] == person['id'] and row['scope'] == 'full_article' and row['document_id']}),
                     'identity_confirmed': False, 'score_computed': False})
    return rows


def validate(args):
    directory = args.data_dir.resolve()
    local_directory = (args.local_dir or directory.with_name(directory.name + '-local')).resolve()
    if directory == local_directory or directory in local_directory.parents or local_directory in directory.parents:
        raise ValueError('El banco y la copia local deben ser carpetas independientes.')
    if any('real-300' in path.parts for path in (directory, local_directory, args.output.resolve())):
        raise ValueError('Esta prueba no modifica bancos de validaciones anteriores de300personas.')
    if args.output.exists() or local_directory.exists() or directory.exists() and any(directory.iterdir()):
        raise ValueError('Use un banco vacío y rutas de informe/copia local nuevas; no se sobrescriben resultados.')
    roster = json.loads(args.roster.read_text(encoding='utf-8-sig'))
    if not isinstance(roster, list):
        raise ValueError('Se requiere el padrón público JSON de300personas del repositorio.')
    people = [deepcopy(next(person for person in roster if person['id'] == identifier)) for identifier in NOTARY_IDS]
    if any(person.get('entity_group') != 'notary' or not person.get('context', {}).get('source_url') for person in people):
        raise ValueError('Los seis casos deben conservar su procedencia pública y categoría de notario.')
    config = make_config(args)
    directory.mkdir(parents=True, exist_ok=True)
    engine = Engine(directory, online=True)
    news = NewsAcquirer(engine)
    engine.store.import_entities(people)
    news.config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
    engine.store.audit('notary_pipeline_validation_started', {'people': len(people), 'body_budget': 1,
        'permission_scope': 'Isolated technical probe only; no commercial licence inferred',
        'no_identity_or_scoring': True})
    hashes = source_hashes()
    plan = news.search_plan()
    events, fetches, progress = [], [], []
    began = time.monotonic()
    deadline = began + args.seconds
    last_log = [0.]
    first_body = [None]

    def observe(event):
        progress.append(event)
        if (first_body[0] is None and event.get('job_kind') == 'news_article'
                and event.get('task_event') == 'finish' and event.get('job_status') == 'done'):
            first_body[0] = time.monotonic() - began
        if time.monotonic() - last_log[0] > 10 or event.get('event') == 'finish':
            last_log[0] = time.monotonic()
            print(json.dumps({key: event.get(key) for key in ('stage', 'completed', 'pending', 'active_tasks',
                'stop_reason', 'next_retry', 'message')}, ensure_ascii=False), flush=True)

    Observed = observed_fetcher(engine, args, events, fetches, directory, deadline)
    with patch('peps.collector.SafeFetcher', Observed):
        cold = news.acquire(workers=2, seconds=args.seconds, max_articles=100, queue=True,
            window='notary-directed-real', relevant_only=True, body_budget=1,
            per_entity_body_limit=1, cache_mode='reuse', progress=observe)
    cold_seconds = time.monotonic() - began
    cold_counts = db_counts(engine.store)
    coverage = acquisition_coverage(engine.store, news.sources())
    bodies = _body_proof(engine, people)
    jobs = queue_receipt(engine.store)
    people_report = _people_report(engine, people, jobs, events)
    initial_candidates = _candidate_rows(engine)
    initial_corpus = corpus_snapshot(engine)
    initial_cache = cached_snapshot(directory)
    initial_network = network_state(directory)
    copy_bank(directory, local_directory)
    local_engine = Engine(local_directory, online=False)
    local_news = NewsAcquirer(local_engine)
    local_started = time.monotonic()
    with patch('socket.getaddrinfo', side_effect=AssertionError('DNS forbidden in local validation')) as dns, \
         patch('socket.socket.connect', side_effect=AssertionError('TCP forbidden in local validation')) as tcp, \
         patch.object(SafeFetcher, '_transport', side_effect=AssertionError('HTTP forbidden in local validation')) as transport:
        warm = local_news.acquire(workers=2, seconds=args.local_seconds, max_articles=100, queue=True,
            window='notary-directed-local-reuse', relevant_only=True, body_budget=1,
            per_entity_body_limit=1, cache_mode='local')
        first_warm = corpus_snapshot(local_engine)
        first_warm_candidates = _candidate_rows(local_engine)
        repeat = local_news.acquire(workers=2, seconds=args.local_seconds, max_articles=100, queue=True,
            window='notary-directed-local-repeat', relevant_only=True, body_budget=1,
            per_entity_body_limit=1, cache_mode='local')
        repeated = corpus_snapshot(local_engine)
        repeated_candidates = _candidate_rows(local_engine)
    after_hashes = source_hashes()
    local_counts = db_counts(local_engine.store)
    actual_history = http_history(directory)
    body_targets = {target for body in bodies for target, mentions in body['literal_name_mentions'].items() if mentions}
    checks = {
        'six_real_public_notaries_imported': cold_counts['entities'] == 6,
        'positive_roberto_original_body_found': NOTARY_IDS[0] in body_targets,
        'positive_roberto_projected_from_original_body': any(row['id'] == NOTARY_IDS[0] and row['relevant_original_bodies'] > 0
                                                            for row in people_report),
        'at_most_one_original_body': 0 < len(bodies) <= 1,
        'original_snapshots_hash_valid': bool(bodies) and all(body['raw_snapshot_hash_valid'] for body in bodies),
        'no_explicit_paywall_metadata': all(not body['explicit_paywall_metadata'] for body in bodies),
        'request_budget_respected': len(events) <= args.max_requests,
        'transport_observations_match_durable_http_count': len(events) == sum(event['network_requests'] for event in actual_history),
        'at_most_six_nominal_queries': len([job for job in jobs if job['kind'] == 'news_query']) <= 6,
        'all_six_people_have_completed_search_receipt': len(people_report) == 6 and all(row['queries_completed'] > 0 for row in people_report),
        'only_approved_publisher_host_requested': all(urlsplit(event['url']).hostname == 'contralinea.com.mx' for event in events),
        'no_identity_attribution_or_scoring': all(cold_counts[table] == local_counts[table] == 0 for table in
            ('evidence', 'news_identity_links', 'scores', 'score_runs')),
        'no_duplicate_canonical_urls': len({article['canonical_url'] for article in article_report(engine.store)}) == cold_counts['news_articles'],
        'local_zero_dns_tcp_http': dns.call_count == tcp.call_count == transport.call_count == 0,
        'local_preserves_article_observation_document_counts': all(local_counts[table] == cold_counts[table] for table in
            ('news_articles', 'news_observations', 'documents')),
        'local_documents_and_observations_unchanged': all(first_warm[table] == initial_corpus[table] for table in
            ('documents', 'news_observations')),
        'repeat_local_corpus_idempotent': all(repeated[table] == first_warm[table] for table in repeated if table != 'audit'),
        'local_original_body_candidate_projection_stable': initial_candidates == first_warm_candidates == repeated_candidates,
        'local_cache_bytes_unchanged': cached_snapshot(local_directory) == initial_cache,
        'local_no_recorded_http': network_state(local_directory)['recorded_http_requests'] == initial_network['recorded_http_requests'],
        'cold_bank_unchanged_by_local_validation': corpus_snapshot(engine) == initial_corpus and
            cached_snapshot(directory) == initial_cache and network_state(directory) == initial_network,
        'cold_audit_valid': engine.store.verify_audit()['ok'],
        'local_audit_valid': local_engine.store.verify_audit()['ok'],
        'pipeline_modules_stable_during_measurement': all(hashes[path] == after_hashes[path] for path in PIPELINE_MODULES),
    }
    report = {'schema_version': 1, 'generated_at': now(),
        'scope': 'Six public Mexican notaries, actual production directed publisher search and one original body maximum.',
        'roster_sha256': hashlib.sha256(args.roster.read_bytes()).hexdigest(),
        'people': [{'id': person['id'], 'name': person['name'], 'source_url': person['context']['source_url']} for person in people],
        'period': {'start': args.start, 'end': args.end}, 'source_config': config,
        'source_hashes_before': hashes, 'source_hashes_after': after_hashes,
        'plan': plan, 'cold_acquisition': cold, 'coverage': coverage, 'counts': cold_counts,
        'person_results': people_report, 'current_candidate_projection': initial_candidates,
        'original_bodies': bodies, 'http_events': events, 'fetch_events': fetches,
        'actual_http_history': actual_history, 'jobs': jobs, 'progress': progress,
        'timings_seconds': {'first_original_body_durable_job_complete': first_body[0],
                            'cold_acquisition': cold_seconds, 'local_cache_reuse_and_repeat': time.monotonic() - local_started,
                            'total': time.monotonic() - began},
        'local_cache_validation': {'first': warm, 'repeat': repeat, 'counts': local_counts,
            'dns_calls': dns.call_count, 'tcp_calls': tcp.call_count, 'http_transport_calls': transport.call_count},
        'checks': checks, 'passed': all(checks.values()),
        'limitations': ['Six names and one publisher are a diagnostic sample, not complete web coverage or a652-person SLA.',
            'A search hit and a literal original-body mention do not confirm identity or establish an adverse finding.',
            'The source body flags authorize only this isolated technical read; no commercial licence is inferred or global permissions changed.',
            'The local-only queue may finish because uncached requests are blocked; that does not complete remote coverage.',
            'Original article text and HTML remain in the private runtime bank; this report contains only hashes, URLs and match metadata.']}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'report': str(args.output), 'passed': report['passed'], 'checks': checks,
                      'actual_http_requests': len(events), 'original_bodies': len(bodies),
                      'timings_seconds': report['timings_seconds']}, ensure_ascii=False), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--online', action='store_true', help='Explicitly authorize this bounded real-internet test')
    parser.add_argument('--roster', type=Path, default=ROOT / 'examples/personas_publicas_mexico_300.json')
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--local-dir', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--seconds', type=float, default=120)
    parser.add_argument('--local-seconds', type=float, default=30)
    parser.add_argument('--max-requests', type=int, default=12)
    parser.add_argument('--start', default='2016-01-01')
    parser.add_argument('--end', default=now()[:10])
    args = parser.parse_args()
    if not args.online or not 0 < args.seconds <= 180 or not 0 < args.local_seconds <= 60 or not 1 <= args.max_requests <= 16:
        parser.error('Requiere --online y presupuestos acotados:180s,60s locales y16HTTP como máximo.')
    try:
        report = validate(args)
    except (ValueError, StopIteration) as exc:
        parser.error(str(exc) or 'El padrón no contiene los seis identificadores públicos requeridos.')
    if not report['passed']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
