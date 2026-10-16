"""Reproduce mass acquisition using real queue/attribution and fixture responses.

The fake fetcher advances a virtual wall clock by the host courtesy interval
before every response. This avoids waiting minutes for the queue's reserved due
times while preserving their order. It does NOT benchmark SafeFetcher, HTTP,
robots, transport throughput or coverage. Network sockets are forbidden.

Use --source-root to compare clean checkouts or a frozen package snapshot. The
report records the source hashes before and after every run, and keeps clean,
polluted and mixed results separate. It has no machine-dependent timing tests.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
import hashlib
import html
import inspect
import json
import math
from pathlib import Path
import platform
import socket
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

REAL_TIME = time.time
REAL_MONOTONIC = time.monotonic
ROOT = Path(__file__).resolve().parents[1]
SOURCE = {
    'id': 'benchmark-300-source', 'source_name': 'Fuente sintética de 300 identidades',
    'publisher_id': 'benchmark-300-publisher', 'kind': 'rss',
    'search_kind': 'wordpress_rss', 'search_url': 'https://benchmark-300.invalid/',
    'base_url': 'https://benchmark-300.invalid/', 'allowed_hosts': ['benchmark-300.invalid'],
    'enabled': True, 'terms_approved': True, 'download_articles': True,
    'article_terms_approved': True, 'robots_required': True,
    'search_max_records': 250, 'max_records': 250, 'max_items': 250,
    'min_interval_seconds': 5, 'min_window_seconds': 86400,
    'refresh_seconds': 86400, 'minimum_article_chars': 80,
    'terms_url': 'fixture:synthetic-no-network-permission',
}
SETTINGS = {'strategy': 'targeted', 'preset': 'identity',
            'start': '2020-01-15', 'end': '2020-01-15', 'window': 'year',
            'max_queries': 10000, 'include_aliases': False, 'use_context': False}


def digest_bytes(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_hashes(root):
    return {str(p.relative_to(root)): digest_bytes(p)
            for p in sorted((root / 'peps').rglob('*.py'))}


def git_head(root):
    try:
        return subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'],
                                       text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


@contextmanager
def no_network():
    def forbidden(*_a, **_k):
        raise AssertionError('Network forbidden in the 300-entity fixture')
    with patch.object(socket, 'getaddrinfo', forbidden), \
         patch.object(socket, 'create_connection', forbidden), \
         patch.object(socket.socket, 'connect', forbidden), \
         patch.object(socket.socket, 'connect_ex', forbidden):
        yield


class Clock:
    def __init__(self, interval):
        self.start = self.value = REAL_TIME()
        self.real_monotonic_start = REAL_MONOTONIC()
        self.interval = interval
        self.lock = threading.RLock()

    def now(self):
        with self.lock:
            return self.value

    def advance(self):
        with self.lock:
            self.value += self.interval
            return self.value

    def monotonic(self):
        with self.lock:
            return self.real_monotonic_start + (self.value - self.start) + (REAL_MONOTONIC() - self.real_monotonic_start)


def entity_name(index, identity_mode):
    return 'Persona Sintética Banco Compartida' if identity_mode == 'homonyms' else f'Persona Sintética Banco {index:05d}'


def body_variant(index, scenario):
    if scenario != 'mixed_risk':
        return 'neutral'
    return 'direct_adverse' if index % 2 == 0 else ('neutral', 'negated', 'victim')[(index // 2) % 3]


def fixture_sentence(index, identity_mode, variant, anchors=False):
    name = entity_name(index, identity_mode)
    subject = name + (', alcalde de Oaxaca,' if anchors else '')
    endings = {'direct_adverse': ' fue detenido por fraude.',
               'neutral': ' participó en una sesión ordinaria.',
               'negated': ' no fue detenido por fraude.',
               'victim': ' fue víctima de fraude.'}
    return subject + endings[variant]


def rss_item(index, relevant, identity_mode, scenario='clean'):
    name = entity_name(index, identity_mode)
    title = f'{name} presenta informe neutral' if relevant else f'Boletín meteorológico externo {index:05d}'
    summary = (f'{name} participó en una sesión ordinaria.' if relevant else
               f'Resumen meteorológico sin personas del banco, boletín externo {index:05d}.')
    if relevant and scenario == 'mixed_risk':
        # Put the adverse phrase in discovery metadata as well. A pending body
        # must still contribute zero: metadata is never article evidence.
        variant = body_variant(index, scenario)
        title = fixture_sentence(index, identity_mode, variant)
        summary = fixture_sentence(index, identity_mode, variant, anchors=True)
    path = '/article/relevant/' if relevant else '/article/irrelevant/'
    return (f'<item><title>{html.escape(title)}</title>'
            f'<link>https://benchmark-300.invalid{path}{index}</link>'
            f'<description>{html.escape(summary)}</description>'
            '<pubDate>Wed, 15 Jan 2020 12:00:00 GMT</pubDate></item>')


def response_for(url, scenario, irrelevant_count, identity_mode):
    parts = urlsplit(url)
    if parts.path.startswith('/article/'):
        index = int(parts.path.rsplit('/', 1)[1])
        relevant = '/relevant/' in parts.path
        text = (fixture_sentence(index, identity_mode, body_variant(index, scenario), anchors=True) + ' '
                if relevant else f'Boletín meteorológico externo {index:05d}. ')
        text += ('Esta noticia inventada describe información neutral para probar el sistema local. ' * 4)
        return '<html><article><p>' + text + '</p></article></html>', 'article'
    name = parse_qs(parts.query).get('s', [''])[0].strip('"')
    if not name.startswith('Persona Sintética Banco '):
        raise AssertionError('Expected an entity-directed query: ' + url)
    index = 0 if identity_mode == 'homonyms' else int(name.rsplit(' ', 1)[1])
    if scenario == 'clean':
        entries = rss_item(index, True, identity_mode, scenario)
    elif scenario == 'polluted':
        # Deliberately ignore the upstream s/date arguments and repeat an RSS
        # list with no exact name or alias from the synthetic identity bank.
        entries = ''.join(rss_item(i, False, identity_mode, scenario) for i in range(irrelevant_count))
    elif scenario in ('mixed', 'mixed_risk'):
        # Keep the same total upstream result count as the polluted case: one
        # exact bank name plus the remaining unrelated records at the top-N cap.
        entries = rss_item(index, True, identity_mode, scenario) + ''.join(rss_item(i, False, identity_mode, scenario) for i in range(max(0, irrelevant_count - 1)))
    else:
        raise ValueError(scenario)
    return '<rss><channel>' + entries + '</channel></rss>', 'query'


def phase_call(fn):
    wall, cpu = time.perf_counter(), time.process_time()
    result = fn()
    return result, {'wall_seconds': time.perf_counter() - wall,
                    'cpu_seconds': time.process_time() - cpu}


def run_case(args, scenario, identity_mode, Engine, NewsAcquirer, canonical, digest, now):
    from peps.news_search import SEARCH_KEYS
    clock, fetch_events, progress = Clock(SOURCE['min_interval_seconds']), [], Counter()
    effective_settings = dict(SETTINGS)
    if 'execution_mode' in SEARCH_KEYS:
        effective_settings.update(execution_mode='quick', session_seconds=120)
    with tempfile.TemporaryDirectory(prefix='peps-news-300-') as directory:
        engine = Engine(directory, online=True)
        news = NewsAcquirer(engine)
        news.config_path.write_text(json.dumps({'sources': [SOURCE], 'search': effective_settings}), encoding='utf-8')
        imported, setup = phase_call(lambda: engine.store.import_entities(
            {'id': f'synthetic-bank-{i:05d}', 'name': entity_name(i, identity_mode),
             'context': {'state': 'Oaxaca', 'role': 'alcalde',
                         'verified_fields': {key: {'status': 'verified', 'source': 'fixture:synthetic-bank-review'}
                                             for key in ('state','role')}}}
            for i in range(args.entities)))

        class FakeFetcher:
            def __init__(self, *_a, **_k):
                pass

            def fetch(self, url, source):
                at = clock.advance()
                body, kind = response_for(url, scenario, args.irrelevant, identity_mode)
                fetch_events.append({'kind': kind, 'virtual_at': at, 'url': url,
                                     'body_chars': len(body)})
                return {'url': url, 'status': 'ok', 'body': body,
                        'sha256': digest(body), 'retrieved_at': now()}

        def observe(event):
            progress[(event.get('phase'), event.get('event'))] += 1

        # A virtual monotonic budget includes the courtesy time represented by
        # each fixture request. perf_counter/process_time remain real clocks.
        clock_patch = (patch('peps.news.time.monotonic', side_effect=clock.monotonic)
                       if args.budget_clock == 'virtual' else nullcontext())
        supports_relevant_only = 'relevant_only' in inspect.signature(news.acquire).parameters
        acquire_options = {'relevant_only': True} if supports_relevant_only else {}
        with no_network(), patch('peps.collector.SafeFetcher', FakeFetcher), \
             patch('peps.news.time.time', side_effect=clock.now), clock_patch:
            queued, queue_time = phase_call(lambda: news.queue(window='offline-300-' + scenario))
            acquired, acquire_time = phase_call(lambda: news.acquire(
                queue=False, workers=args.workers, seconds=args.seconds,
                max_articles=1000, download_all=False, progress=observe, **acquire_options))
            before_attribute = news.stats()
            attributed, attribute_time = phase_call(lambda: news.attribute(
                limit=None, rescore=False, progress=observe))
            scored, scoring_time = phase_call(lambda: engine.rescore(progress=observe))

        with engine.store.connection() as con:
            scalar = lambda sql: con.execute(sql).fetchone()[0]
            job_counts = [dict(row) for row in con.execute(
                'SELECT kind,status,count(*) AS count,sum(attempts) AS attempts FROM jobs GROUP BY kind,status ORDER BY kind,status')]
            counts = {'entities': scalar('SELECT count(*) FROM entities'),
                      'query_jobs': scalar("SELECT count(*) FROM jobs WHERE kind='news_query'"),
                      'entities_queried': scalar("SELECT count(DISTINCT e.id) FROM news_query_windows q "
                                                "JOIN news_discovery_runs r ON r.job_id=q.job_id "
                                                "JOIN json_each(q.entity_ids) j JOIN entities e ON e.id=j.value "
                                                "WHERE q.status IN ('complete','truncated','split')"),
                      'body_jobs': scalar("SELECT count(*) FROM jobs WHERE kind='news_article'"),
                      'retrieved_articles': scalar("SELECT count(*) FROM news_articles WHERE retrieved_at IS NOT NULL"),
                      'discovered_articles': scalar('SELECT count(*) FROM news_articles'),
                      'discovery_observations': scalar('SELECT count(*) FROM news_observations'),
                      'discovery_runs': scalar('SELECT count(*) FROM news_discovery_runs'),
                      'query_record_occurrences': scalar('SELECT coalesce(sum(records),0) FROM news_discovery_runs'),
                      'saturated_query_windows': scalar('SELECT count(*) FROM news_query_windows WHERE saturated=1'),
                      'documents': scalar('SELECT count(*) FROM documents'),
                      'identity_links': scalar('SELECT count(*) FROM news_identity_links'),
                      'entities_with_candidates': scalar('SELECT count(DISTINCT entity_id) FROM news_identity_links'),
                      'relevant_candidates': scalar('SELECT count(DISTINCT document_id) FROM news_identity_links'),
                      'evidence': scalar('SELECT count(*) FROM evidence'),
                      'evidence_full_body': scalar("SELECT count(*) FROM evidence e JOIN documents d ON d.id=e.document_id "
                                                   "WHERE d.source_kind!='discovery' AND coalesce(json_extract(d.metadata,'$.discovery_only'),0)=0"),
                      'evidence_metadata_only': scalar("SELECT count(*) FROM evidence e JOIN documents d ON d.id=e.document_id "
                                                       "WHERE d.source_kind='discovery' OR json_extract(d.metadata,'$.discovery_only')=1"),
                      'scores': scalar('SELECT count(*) FROM scores'),
                      'score_runs': scalar('SELECT count(*) FROM score_runs')}
            links = Counter(json.loads(row[0]).get('status') for row in con.execute('SELECT resolution FROM news_identity_links'))
            score_by_entity = {row['entity_id']: json.loads(row['result']) for row in con.execute('SELECT entity_id,result FROM scores')}
            score_payloads = list(score_by_entity.values())
            matched_body_ids = {row[0] for row in con.execute(
                "SELECT DISTINCT e.entity_id FROM evidence e JOIN news_articles n ON n.document_id=e.document_id "
                "JOIN news_identity_links i ON i.document_id=e.document_id AND i.entity_id=e.entity_id "
                "WHERE n.scope='full_article' AND json_extract(i.resolution,'$.status')='matched'")}
            score_exclusions = Counter(reason for result in score_payloads
                                       for item in result.get('evidence_audit', [])
                                       for reason in item.get('exclusions', []))
            metadata_audits = [item for result in score_payloads for item in result.get('evidence_audit', [])
                               if item.get('source_kind') == 'discovery']
            discovery_records = [dict(row) for row in con.execute(
                'SELECT status,count(*) AS count FROM news_query_windows GROUP BY status')]
        http_kinds = Counter(event['kind'] for event in fetch_events)
        request_times = sorted(event['virtual_at'] for event in fetch_events)
        intervals = [b - a for a, b in zip(request_times, request_times[1:])]
        phases = {'import': setup, 'queue': queue_time, 'acquire': acquire_time,
                  'attribute': attribute_time, 'rescore': scoring_time}
        counts['entities_unqueried'] = counts['entities'] - counts['entities_queried']
        counts['entities_unqueried_percent'] = 100 * counts['entities_unqueried'] / counts['entities']
        positive_ids = {key for key, result in score_by_entity.items() if result['score'] > 0}
        expected_positive_ids = {key for key in matched_body_ids
                                 if body_variant(int(key.rsplit('-', 1)[1]), scenario) == 'direct_adverse'}
        controls = matched_body_ids - expected_positive_ids
        scoring_probe = {'positive_scores': len(positive_ids),
                         'positive_entity_examples': sorted(positive_ids)[:10],
                         'minimum_positive_score': min((score_by_entity[key]['score'] for key in positive_ids), default=None),
                         'maximum_positive_score': max((score_by_entity[key]['score'] for key in positive_ids), default=None),
                         'matched_body_entities': len(matched_body_ids),
                         'direct_adverse_matched_body_entities': len(expected_positive_ids),
                         'control_matched_body_entities': len(controls),
                         'body_variants_fetched': dict(Counter(body_variant(int(urlsplit(event['url']).path.rsplit('/', 1)[1]), scenario)
                                                              for event in fetch_events if event['kind'] == 'article'))}
        checks = {'audit_valid': engine.store.verify_audit()['ok'],
                  'all_bank_entities_scored': scored == counts['scores'] == args.entities,
                  'neutral_fixture_scores_zero': scenario == 'mixed_risk' or not positive_ids,
                  'positive_requires_matched_full_body': positive_ids <= matched_body_ids,
                  'only_direct_adverse_bodies_can_score': positive_ids <= expected_positive_ids,
                  'control_bodies_score_zero': not positive_ids.intersection(controls),
                  'known_risk_fixture_scores_positive': scenario != 'mixed_risk' or identity_mode != 'known' or bool(positive_ids),
                  'all_supported_direct_adverse_bodies_score': scenario != 'mixed_risk' or identity_mode != 'known'
                      or positive_ids == expected_positive_ids,
                  'metadata_evidence_always_excluded': all('discovery_index_is_not_article_evidence' in item.get('exclusions', [])
                                                          for item in metadata_audits),
                  'homonym_scores_zero': identity_mode != 'homonyms' or not positive_ids,
                  'all_evidence_has_explicit_scope': counts['evidence_full_body'] + counts['evidence_metadata_only'] == counts['evidence'],
                  'unrelated_fixture_has_no_identity_links': scenario != 'polluted' or counts['identity_links'] == 0,
                  'homonyms_never_confirmed': identity_mode != 'homonyms' or not links.get('matched'),
                  'known_fetched_fixture_has_supported_identity': identity_mode != 'known' or scenario == 'polluted'
                      or len(matched_body_ids) == counts['retrieved_articles'],
                  'virtual_spacing_respects_interval': not intervals or min(intervals) >= clock.interval}
        return {'scenario': scenario, 'identity_mode': identity_mode,
                'download_all': False, 'relevant_only_supported': supports_relevant_only,
                'relevant_only_requested': True if supports_relevant_only else None,
                'effective_search_config': effective_settings,
                'imported': imported, 'phases': phases,
                'fixture_records_per_query': 1 if scenario == 'clean' else args.irrelevant,
                'fixture_relevant_records_per_query': 0 if scenario == 'polluted' else 1,
                'pipeline_wall_seconds': sum(p['wall_seconds'] for k,p in phases.items() if k != 'import'),
                'pipeline_cpu_seconds': sum(p['cpu_seconds'] for k,p in phases.items() if k != 'import'),
                'queued': queued, 'acquire': {k: acquired.get(k) for k in (
                    'processed','completed','pending','stop_reason','failed','blocked','dead','elapsed_seconds',
                    'discovery_records','relevant_records','excluded_records','body_queued',
                    'body_skipped_budget','body_budget','per_entity_body_limit')},
                'attribution': attributed, 'scored': scored, 'counts': counts,
                'identity_statuses': dict(links), 'jobs': job_counts,
                'score_audit_exclusions': dict(score_exclusions),
                'scoring_probe': scoring_probe,
                'query_window_statuses': discovery_records, 'articles_before_attribute': before_attribute,
                'fixture_requests': dict(http_kinds),
                'courtesy': {'interval_seconds': clock.interval,
                             'query_only_lower_bound_seconds': max(0, counts['query_jobs'] - 1) * clock.interval,
                             'all_fixture_requests_lower_bound_seconds': max(0, len(fetch_events) - 1) * clock.interval,
                             'virtual_advance_seconds': clock.now() - clock.start,
                             'minimum_virtual_request_spacing_seconds': min(intervals, default=None)},
                'progress_events': {str(k): v for k,v in progress.items()},
                'checks': checks, 'checks_passed': all(checks.values()),
                'audit_valid': checks['audit_valid']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', type=Path, default=ROOT)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--label', default='working-tree')
    parser.add_argument('--entities', type=int, default=300)
    parser.add_argument('--irrelevant', type=int, default=250)
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--seconds', type=float, default=120)
    parser.add_argument('--budget-clock', choices=['virtual','real'], default='virtual',
                        help='virtual includes represented courtesy time in the session budget; real measures local compute only')
    parser.add_argument('--identity-modes', nargs='+', choices=['known','homonyms'], default=['known','homonyms'])
    parser.add_argument('--scenarios', nargs='+', choices=['clean','polluted','mixed','mixed_risk'],
                        default=['clean','polluted','mixed','mixed_risk'])
    args = parser.parse_args()
    if args.entities < 1 or not 1 <= args.irrelevant <= SOURCE['max_records'] or not 1 <= args.workers <= 16 or not math.isfinite(args.seconds) or args.seconds <= 0:
        parser.error('entities and seconds must be positive; irrelevant requires 1..250; workers requires 1..16')
    args.source_root = args.source_root.resolve()
    sys.path.insert(0, str(args.source_root))
    from peps.engine import Engine
    from peps.news import NewsAcquirer
    from peps.store import canonical, digest, now
    import peps
    before = source_hashes(args.source_root)
    report = {'label': args.label, 'created_at': datetime.now(timezone.utc).isoformat(),
              'python': platform.python_version(), 'platform': platform.platform(),
              'package_version': peps.__version__,
              'source_root': str(args.source_root), 'git_head': git_head(args.source_root),
              'script_sha256': digest_bytes(Path(__file__)), 'source_hashes_before': before,
              'fixture': {'source': SOURCE, 'settings': SETTINGS, 'entities': args.entities,
                          'polluted_records_per_query': args.irrelevant,
                          'mixed_irrelevant_records_per_query': max(0, args.irrelevant - 1),
                          'workers': args.workers, 'acquisition_seconds_limit': args.seconds,
                          'download_all': False, 'max_articles': 1000,
                          'relevant_only': 'True only when the acquisition signature supports it; baseline unchanged',
                          'budget_clock': args.budget_clock, 'identity_modes': args.identity_modes,
                          'scenarios': args.scenarios,
                          'entity_pattern': 'Persona Sintética Banco {index:05d}',
                          'body': 'Neutral synthetic article, unique index, minimum 80 characters',
                          'mixed_risk_body': 'Even indices: direct synthetic fraud arrest; odd indices: rotating neutral, negated arrest, victim. Discovery metadata repeats the same phrase; bodies carry verified synthetic role/state anchors.'},
              'limitations': [
                  'Local compute and SQLite only: fake fetcher, no network and no SafeFetcher transport.',
                  'Virtual wall clock advances five seconds per fixture request; CPU and perf_counter clocks stay real.',
                  'Virtual budget mode includes courtesy in monotonic time; real mode omits network wait and is local-compute only.',
                  'Workers may claim queries and bodies in different orders; exact partial-session counts can vary.',
                  'Theoretical host courtesy bounds exclude robots, transport time, retries and publisher limits.',
                  'Known fixture includes verified synthetic role/state anchors; homonyms share anchors and must remain ambiguous.',
                  'Day windows at the configured result cap remain top-N truncated; no archive coverage claim.',
                  'download_all=False follows UI acquisition; baseline has no relevant_only argument and retains its original behavior.',
              ], 'cases': []}
    for identity_mode in args.identity_modes:
        for scenario in args.scenarios:
            report['cases'].append(run_case(args, scenario, identity_mode, Engine, NewsAcquirer, canonical, digest, now))
            print(json.dumps({'scenario': scenario, 'identity_mode': identity_mode,
                              'seconds': report['cases'][-1]['pipeline_wall_seconds'],
                              'counts': report['cases'][-1]['counts']}, ensure_ascii=False), flush=True)
    report['source_hashes_after'] = source_hashes(args.source_root)
    report['source_unchanged_during_measurement'] = before == report['source_hashes_after']
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(str(args.output), flush=True)
    if not report['source_unchanged_during_measurement'] or not all(case['checks_passed'] for case in report['cases']):
        raise SystemExit('Benchmark validation failed; inspect the saved report checks.')


if __name__ == '__main__':
    main()
