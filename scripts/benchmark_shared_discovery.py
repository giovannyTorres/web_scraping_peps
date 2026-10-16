"""Measure shared discovery with real SQLite, scheduling and five-second waits.

Only SafeFetcher is replaced: fixture WordPress metadata contains exactly three
pages of 100 records. Sockets are forbidden. No clock, parser, queue, relevance,
SQLite or scoring implementation is mocked. This measures local processing and
real scheduler waits, not public network throughput or historical web coverage.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import platform
import socket
import sys
import tempfile
import threading
import time
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from peps.acquisition_coverage import acquisition_coverage
from peps.engine import Engine
from peps.news import NewsAcquirer
from peps.news_search import build_search_plan
from peps.store import canonical, digest, now


SOURCE = {
    'id': 'synthetic-shared-index', 'source_name': 'Índice público simulado',
    'kind': 'wordpress_api', 'url': 'https://example.org/wp-json/wp/v2/posts',
    'api_url': 'https://example.org/wp-json/wp/v2/posts',
    'allowed_hosts': ['example.org'], 'enabled': True, 'terms_approved': True,
    'robots_required': True, 'discovery': True, 'download_articles': False,
    'article_terms_approved': False, 'body_terms_approved': False,
    'min_interval_seconds': 5, 'max_records': 100, 'max_bytes': 2_000_000,
    'timeout_seconds': 20, 'index_ttl_seconds': 3600,
    'terms_url': 'fixture:synthetic-metadata-only-no-network',
}
SETTINGS = {
    'strategy': 'shared', 'preset': 'identity', 'start': '2020-01-01',
    'end': '2020-12-31', 'window': 'year', 'max_queries': 1000,
    'include_aliases': False, 'use_context': False,
    'execution_mode': 'quick', 'session_seconds': 120,
}
TRACKED_FILES = (
    'peps/news.py', 'peps/news_search.py', 'peps/shared_crawl.py',
    'peps/shared_discovery.py', 'peps/acquisition_coverage.py',
    'peps/relevance.py', 'peps/store.py', 'peps/engine.py',
)


@contextmanager
def no_network():
    def forbidden(*_args, **_kwargs):
        raise AssertionError('Public network forbidden in synthetic shared benchmark')
    with patch.object(socket, 'getaddrinfo', forbidden), \
         patch.object(socket, 'create_connection', forbidden), \
         patch.object(socket.socket, 'connect', forbidden), \
         patch.object(socket.socket, 'connect_ex', forbidden):
        yield


def source_hashes():
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in TRACKED_FILES}


def entities(review=1):
    return [
        {'id': f'shared-person-{index:05d}',
         'name': f'Persona Sintética Archivo {index:05d}',
         'context': {'state': 'Oaxaca', 'synthetic_review': review}}
        for index in range(300)
    ]


def fixture_posts(scenario):
    endings = (
        'fue detenido por fraude.',
        'presentó un informe de actividad pública neutral.',
        'no fue detenido por fraude.',
        'fue víctima de fraude.',
    )
    records, variants = [], Counter()
    for index in range(300):
        relevant = scenario == 'all_relevant' or index % 10 == 0
        position = index if scenario == 'all_relevant' else index // 10
        variant = ('direct_adverse', 'neutral', 'negated', 'victim')[position % 4]
        subject = (f'Persona Sintética Archivo {index:05d}' if relevant
                   else f'Boletín meteorológico externo {index:05d}')
        sentence = (subject + ', alcalde de Oaxaca, ' + endings[position % 4]
                    if relevant else subject + ' informa sobre el clima regional.')
        records.append({
            'id': index + 1, 'link': f'https://example.org/2020/nota-{index:05d}',
            'date': '2020-06-15T06:00:00', 'date_gmt': '2020-06-15T12:00:00',
            'title': {'rendered': sentence},
            'excerpt': {'rendered': '<p>' + sentence + '</p>'},
        })
        variants[variant if relevant else 'irrelevant'] += 1
    return records, dict(variants)


def database_counts(store):
    with store.connection() as con:
        return {
            'entities': con.execute('SELECT count(*) FROM entities').fetchone()[0],
            'articles': con.execute('SELECT count(*) FROM news_articles').fetchone()[0],
            'observations': con.execute('SELECT count(*) FROM news_observations').fetchone()[0],
            'cached_metadata_pages': con.execute('SELECT count(*) FROM news_shared_pages').fetchone()[0],
            'documents': con.execute('SELECT count(*) FROM documents').fetchone()[0],
            'evidence': con.execute('SELECT count(*) FROM evidence').fetchone()[0],
            'body_jobs': con.execute("SELECT count(*) FROM jobs WHERE kind='news_article'").fetchone()[0],
            'identity_links': con.execute('SELECT count(*) FROM news_identity_links').fetchone()[0],
            'positive_scores': con.execute(
                "SELECT count(*) FROM scores WHERE json_extract(result,'$.score')>0").fetchone()[0],
            'bank_revision': int(con.execute(
                "SELECT value FROM meta WHERE key='news_identity_revision'").fetchone()[0]),
        }


def run_case(scenario, budget):
    posts, variants = fixture_posts(scenario)
    expected = 300 if scenario == 'all_relevant' else 30
    events, event_lock, progress = [], threading.Lock(), Counter()

    class FakeFetcher:
        def __init__(self, *_args, **_kwargs):
            pass

        def fetch(self, url, source):
            parts, began = urlsplit(url), time.monotonic()
            if parts.hostname != 'example.org' or parts.path != '/wp-json/wp/v2/posts':
                raise AssertionError('Unexpected body or external request: ' + url)
            query = parse_qs(parts.query)
            fields = set(query.get('_fields', [''])[0].split(','))
            if fields != {'id', 'link', 'date_gmt', 'date', 'title.rendered', 'excerpt.rendered'}:
                raise AssertionError('Expected compact metadata fields, never content: ' + str(fields))
            page = int(query['page'][0])
            if page not in (1, 2, 3) or query.get('per_page') != ['100']:
                raise AssertionError('Unexpected pagination: ' + url)
            if not query.get('after') or not query.get('before'):
                raise AssertionError('Historical bounds missing: ' + url)
            body = canonical(posts[(page - 1) * 100:page * 100])
            with event_lock:
                events.append({'page': page, 'real_monotonic': began, 'url': url,
                               'response_bytes': len(body.encode('utf-8'))})
            return {
                'url': url, 'status': 'ok', 'body': body, 'sha256': digest(body),
                'retrieved_at': now(),
                'pagination_headers': {'X-WP-Total': '300', 'X-WP-TotalPages': '3'},
            }

    def observe(event):
        progress[(event.get('event'), event.get('task_event'))] += 1

    with tempfile.TemporaryDirectory(prefix='webscope-shared-benchmark-') as directory:
        engine = Engine(directory, online=True)
        news = NewsAcquirer(engine)
        news.config_path.write_text(json.dumps({'sources': [SOURCE], 'search': SETTINGS}), encoding='utf-8')
        engine.store.import_entities(entities())
        targeted_source = dict(SOURCE, kind='rss', search_kind='wordpress_rss',
                               search_url='https://example.org/', search_max_records=100)
        baseline = build_search_plan(engine.store.iter_entities(), [targeted_source],
                                     dict(SETTINGS, strategy='targeted'))
        baseline_count = sum(item['kind'] == 'news_query' for item in baseline['queries'])
        phases = {}
        with no_network(), patch('peps.collector.SafeFetcher', FakeFetcher):
            for phase in ('cold', 'warm'):
                import_seconds = 0.0
                if phase == 'warm':
                    began = time.perf_counter()
                    engine.store.import_entities(entities(review=2))
                    import_seconds = time.perf_counter() - began
                before_requests = len(events)
                before = database_counts(engine.store)
                began = time.perf_counter()
                queue_result = news.queue(window='synthetic-shared-2020')
                queue_seconds = time.perf_counter() - began
                cpu, began = time.process_time(), time.perf_counter()
                acquired = news.acquire(queue=False, workers=4, seconds=budget,
                                        max_articles=24, relevant_only=True,
                                        download_all=False, progress=observe)
                acquire_seconds = time.perf_counter() - began
                acquire_cpu_seconds = time.process_time() - cpu
                coverage = acquisition_coverage(engine.store, news.sources())
                counts_before_scoring = database_counts(engine.store)
                # Acquisition metadata cannot produce scoring evidence. Do not
                # call attribution here: that separately creates auditable,
                # excluded discovery records by design.
                scored = engine.rescore()
                counts = database_counts(engine.store)
                revision = str(counts['bank_revision'])
                with engine.store.connection() as con:
                    receipts = [json.loads(row['parser_metadata']) for row in con.execute(
                        "SELECT parser_metadata FROM news_discovery_runs "
                        "WHERE json_extract(parser_metadata,'$.bank_revision')=?", (revision,))]
                phase_events = events[before_requests:]
                times = sorted(event['real_monotonic'] for event in phase_events)
                intervals = [later - earlier for earlier, later in zip(times, times[1:])]
                checks = {
                    'all_300_entities_preserved': counts['entities'] == 300,
                    'exact_relevant_candidates': counts['articles'] == expected,
                    'all_300_checked_locally': coverage['entities_checked_locally'] == 300,
                    'no_named_queries_claimed': coverage['entities_queried'] == 0,
                    'three_pages_and_300_records_checked': coverage['shared_pages_read'] == 3
                        and coverage['shared_records_checked'] == 300,
                    'no_pending_shared_pages': coverage['shared_pages_pending'] == 0,
                    'three_cached_metadata_pages': counts['cached_metadata_pages'] == 3,
                    'no_documents_or_evidence_from_acquisition_metadata':
                        counts_before_scoring['documents'] == counts_before_scoring['evidence'] == 0,
                    'no_body_requests_or_jobs': counts['body_jobs'] == 0,
                    'no_identity_assertions': counts['identity_links'] == 0,
                    'all_300_scores_zero_without_bodies': scored == 300 and counts['positive_scores'] == 0,
                    'correct_fixture_request_count': len(phase_events) == (3 if phase == 'cold' else 0),
                    'scheduler_respects_real_five_second_interval':
                        not intervals or min(intervals) >= SOURCE['min_interval_seconds'] - .025,
                    'three_current_revision_receipts': len(receipts) == 3,
                    'correct_cache_receipts': all(bool(row.get('metadata_cache_hit')) == (phase == 'warm')
                                                  for row in receipts),
                    'session_completed': acquired.get('stop_reason') == 'completed'
                        and acquired.get('pending') == 0,
                    'audit_valid': engine.store.verify_audit()['ok'],
                }
                if phase == 'warm':
                    checks.update({
                        'bank_revision_changed': counts['bank_revision'] > phases['cold']['counts']['bank_revision'],
                        'context_only_revision_kept_names':
                            [entity['name'] for entity in engine.store.iter_entities()]
                            == [entity['name'] for entity in entities(review=2)],
                        'warm_acquire_faster_than_cold': acquire_seconds < phases['cold']['acquire_wall_seconds']})
                phases[phase] = {
                    'context_reimport_seconds': import_seconds, 'queue_wall_seconds': queue_seconds,
                    'acquire_wall_seconds': acquire_seconds, 'acquire_cpu_seconds': acquire_cpu_seconds,
                    'fixture_http_requests': len(phase_events),
                    'fixture_response_bytes': sum(row['response_bytes'] for row in phase_events),
                    'requested_pages': [row['page'] for row in phase_events],
                    'real_request_intervals_seconds': intervals,
                    'queue': {key: queue_result.get(key) for key in ('queued', 'queries', 'superseded')},
                    'acquire': {key: acquired.get(key) for key in (
                        'processed', 'completed', 'pending', 'stop_reason', 'elapsed_seconds',
                        'discovery_records', 'relevant_records', 'excluded_records', 'body_queued')},
                    'coverage': coverage, 'counts_before': before, 'counts': counts,
                    'checks': checks, 'checks_passed': all(checks.values()),
                }
        return {
            'scenario': scenario, 'bank_entities': 300, 'fixture_records': 300,
            'fixture_relevant_records': expected, 'fixture_irrelevant_records': 300 - expected,
            'fixture_relevant_variants': variants,
            'baseline': {'targeted_single_name_query_count': baseline_count,
                         'plan_only_not_executed': True,
                         'minimum_time_to_start_300_serial_queries_seconds': (baseline_count - 1) * 5},
            'phases': phases,
            'checks_passed': baseline_count == 300 and all(row['checks_passed'] for row in phases.values()),
            'progress_event_counts': {str(key): count for key, count in progress.items()},
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'runtime/shared-discovery-benchmark.json')
    parser.add_argument('--seconds', type=float, default=120)
    args = parser.parse_args()
    if not 15 <= args.seconds <= 120:
        parser.error('--seconds must be between 15 and 120; real scheduler waits are not skipped')
    hashes_before, began = source_hashes(), time.perf_counter()
    cases = [run_case(scenario, args.seconds) for scenario in ('all_relevant', 'mixed')]
    hashes_after = source_hashes()
    result = {
        'schema': 'webscope.shared_discovery.synthetic_benchmark.v1',
        'recorded_at_utc': now(), 'python': platform.python_version(),
        'platform': platform.platform(), 'wall_seconds': time.perf_counter() - began,
        'method': {'synthetic_only': True, 'real_public_network': False,
                   'mocked_component': 'peps.collector.SafeFetcher only; socket calls forbidden',
                   'real_components': ['SQLite', 'Store', 'scheduler', 'NewsAcquirer',
                                       'WordPress parser', 'RelevanceIndex', 'metadata cache',
                                       'bank revision', 'acquisition coverage', 'Engine.rescore'],
                   'clock': 'real wall clock and real five-second scheduler due times; no clock patch',
                   'budget_seconds_per_acquisition': args.seconds,
                   'metadata_page_count': 3, 'metadata_records_per_page': 100,
                   'body_permissions': False,
                   'warm_revision_change': 'Reimport300 context-only edits; preserve names and source permissions',
                   'limitations': ['No public HTTP latency, robots fetch, throttling or site availability measured.',
                                   'No historical web coverage, real identity accuracy or positive scoring measured.',
                                   'Attribution is not run; discovery rows remain candidates without evidence.',
                                   'Warm reuse applies only to still-fresh metadata with unchanged source config.']},
        'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'production_sha256_before': hashes_before, 'production_sha256_after': hashes_after,
        'production_unchanged_during_run': hashes_before == hashes_after,
        'cases': cases, 'checks_passed': all(case['checks_passed'] for case in cases),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'output': str(args.output), 'checks_passed': result['checks_passed'],
                      'wall_seconds': round(result['wall_seconds'], 3),
                      'production_unchanged_during_run': result['production_unchanged_during_run'],
                      'cases': [{'scenario': case['scenario'], 'checks_passed': case['checks_passed'],
                                 'cold_seconds': round(case['phases']['cold']['acquire_wall_seconds'], 3),
                                 'warm_seconds': round(case['phases']['warm']['acquire_wall_seconds'], 3),
                                 'cold_requests': case['phases']['cold']['fixture_http_requests'],
                                 'warm_requests': case['phases']['warm']['fixture_http_requests']}
                                for case in cases]}, ensure_ascii=False))
    if not result['checks_passed']:
        for case in cases:
            for phase, row in case['phases'].items():
                for name, passed in row['checks'].items():
                    if not passed:
                        print(f'FAIL {case["scenario"]}/{phase}: {name}', file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
