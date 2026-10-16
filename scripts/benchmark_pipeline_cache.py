"""Measure local news-cache paths with an explicitly synthetic SQLite corpus.

Run: python scripts/benchmark_pipeline_cache.py --output runtime/pipeline-cache.json

Defaults reproduce the precursor's 300 entities, 9,000 articles and 95,000
observations. Each invocation uses new temporary databases. DNS and sockets
are forbidden; only the collector's injected fake transport can return HTTP
responses. Timings describe local code and SQLite, not public-web throughput
or a download SLA. There are correctness checks, but no timing thresholds.
The optional --baseline is read only and its data is embedded in the report;
it may also point at a saved report containing the original embedded baseline.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import platform
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SOURCE_FILES = ('peps/news.py', 'peps/collector.py', 'peps/store.py',
                'peps/acquisition_coverage.py', 'peps/news_projection.py',
                'peps/news_session.py', 'peps/relevance.py')
SOURCE = {'id': 'fixture', 'kind': 'wordpress_api',
          'url': 'https://example.org/wp-json/wp/v2/posts',
          'api_url': 'https://example.org/wp-json/wp/v2/posts',
          'allowed_hosts': ['example.org'], 'enabled': True, 'terms_approved': True,
          'download_articles': True, 'article_terms_approved': True,
          'refresh_seconds': 86400, 'robots_required': False, 'min_interval_seconds': 0}


@contextmanager
def no_network():
    counts = Counter()

    def forbidden(*_args, **_kwargs):
        counts['blocked_operations'] += 1
        raise AssertionError('Real DNS and sockets are forbidden in this synthetic benchmark')

    with patch.object(socket, 'getaddrinfo', forbidden), \
         patch.object(socket, 'create_connection', forbidden), \
         patch.object(socket.socket, 'connect', forbidden), \
         patch.object(socket.socket, 'connect_ex', forbidden):
        yield counts


@contextmanager
def traced_connections(store):
    counts = Counter(connections_opened=0, sql_statements=0)
    original = store.connection

    @contextmanager
    def traced():
        counts['connections_opened'] += 1
        with original() as con:
            def trace(_sql):
                counts['sql_statements'] += 1
            con.set_trace_callback(trace)
            yield con

    store.connection = traced
    try:
        yield counts
    finally:
        store.connection = original


def hashes(source_root):
    return {name: hashlib.sha256((source_root / name).read_bytes()).hexdigest()
            for name in SOURCE_FILES if (source_root / name).exists()}


def git_state(source_root):
    def git(*args):
        try:
            return subprocess.check_output(['git', '-C', str(source_root), *args],
                                           text=True, stderr=subprocess.DEVNULL).strip()
        except (OSError, subprocess.CalledProcessError):
            return None
    status = git('status', '--porcelain')
    return {'head': git('rev-parse', 'HEAD'), 'dirty': bool(status) if status is not None else None}


def seed(store, args, canonical, digest):
    entities = [{'id': f'e{i:03d}', 'name': f'Persona Sintética Archivo {i:03d}', 'kind': 'person'}
                for i in range(args.entities)]
    store.import_entities(entities)
    stamp = datetime(2020, 1, 1, tzinfo=timezone.utc)
    relevant_ids, relevant_entities = [], set()

    def articles():
        for i in range(args.articles):
            entity = entities[i % args.entities]
            relevant = i % 5 == 0
            title = entity['name'] + ' presentó su informe.' if relevant else f'Boletín meteorológico externo {i}'
            if relevant:
                relevant_ids.append(f'{i:064x}')
                relevant_entities.add(entity['id'])
            yield (f'{i:064x}', f'https://example.org/note/{i}', 'fixture-publisher', 'fixture', title,
                   'Resumen editorial original de la prueba. ' * 5, stamp.isoformat(),
                   'feed_metadata_only', 'discovered')

    def observations():
        for i in range(args.articles):
            entity = entities[i % args.entities]
            relevant = i % 5 == 0
            title = entity['name'] + ' presentó su informe.' if relevant else f'Boletín meteorológico externo {i}'
            details = canonical({'title': title, 'summary': 'Resumen editorial original de la prueba. ' * 5,
                                 'retrieval_relevance': {'relevant': relevant,
                                                         'entity_ids': [entity['id']] if relevant else []}})
            for j in range(args.observations_per_article):
                yield (digest(f'{i}-{j}'), f'{i:064x}', 'fixture', f'https://example.org/index/{j}',
                       f'https://example.org/note/{i}', (stamp + timedelta(seconds=j)).isoformat(),
                       'fixture-sha', details)
        details = canonical({'title': 'Boletín ajeno', 'summary': 'Registro histórico sintético'})
        for i in range(args.popular_observations):
            yield (digest('popular-' + str(i)), f'{1:064x}', 'fixture', 'https://example.org/popular',
                   'https://example.org/note/1', (stamp + timedelta(seconds=i)).isoformat(),
                   'fixture-sha', details)

    with store.connection() as con:
        con.executemany('INSERT INTO news_articles(id,canonical_url,publisher_id,source_id,title,summary,'
                        'discovered_at,scope,status) VALUES(?,?,?,?,?,?,?,?,?)', articles())
        con.executemany('INSERT INTO news_observations VALUES(?,?,?,?,?,?,?,?)', observations())
        counts = {table: con.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                  for table in ('entities', 'news_articles', 'news_observations')}
    return entities, counts, len(relevant_ids), len(relevant_entities)


def measure(store, operation):
    with traced_connections(store) as counts:
        start = time.perf_counter()
        result = operation()
        elapsed = time.perf_counter() - start
    return dict(seconds=elapsed, **counts, result=result)


def benchmark(args):
    sys.path.insert(0, str(args.source_root))
    from peps.acquisition_coverage import acquisition_coverage
    from peps.collector import SafeFetcher
    from peps.engine import Engine
    from peps.news import NewsAcquirer, _BodyBudget
    from peps.news_projection import ensure_projection
    from peps.relevance import RelevanceIndex
    from peps.shared_discovery import parse_wordpress_posts
    from peps.store import digest, canonical, now

    before = hashes(args.source_root)
    report = {'schema_version': 1, 'generated_at': now(), 'synthetic': True,
              'method': 'Fresh temporary SQLite; real production read/cache paths; injected HTTP only; no public network.',
              'limits': ['Local wall times are not public-network speed or coverage guarantees.',
                         'The corpus has relevant articles for only one fifth of entity IDs by construction.',
                         'The precursor baseline used a persistent fixture and did not measure a versioned projection rebuild.',
                         'Connection tracing adds overhead; no latency threshold determines success.'],
              'python': platform.python_version(), 'sqlite': sqlite3.sqlite_version,
              'platform': platform.platform(), 'git': git_state(args.source_root),
              'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'source_sha256': before, 'measurements': {}, 'checks': {}}
    if args.baseline:
        raw = args.baseline.read_bytes()
        baseline = json.loads(raw)
        if isinstance(baseline.get('baseline'), dict):
            baseline = baseline['baseline']['report']
        report['baseline'] = {'file': args.baseline.name, 'sha256': hashlib.sha256(raw).hexdigest(),
                              'report': baseline}
    checks, measurements = report['checks'], report['measurements']
    with no_network() as guard, tempfile.TemporaryDirectory(prefix='peps-pipeline-cache-') as temporary:
        state = Path(temporary)
        engine = Engine(state / 'bank', online=True)
        news = NewsAcquirer(engine)
        store = engine.store
        news.config_path.write_text(json.dumps({'sources': [SOURCE], 'search': {'strategy': 'shared'}}), encoding='utf-8')
        start = time.perf_counter()
        entities, counts, expected_articles, expected_entities = seed(store, args, canonical, digest)
        report['fixture'] = dict(counts, setup_seconds=time.perf_counter() - start,
                                relevant_articles=expected_articles, entities_with_candidates=expected_entities,
                                observations_per_article=args.observations_per_article,
                                extra_popular_observations=args.popular_observations,
                                synthetic_source=SOURCE, fresh_temporary_databases=True)
        checks['exact_fixture_counts'] = counts == {
            'entities': args.entities, 'news_articles': args.articles,
            'news_observations': args.articles * args.observations_per_article + args.popular_observations}
        index = RelevanceIndex(entities)
        measurements['rescue_no_bodies'] = measure(store, lambda: dict(news._rescue_bodies(
            index, [SOURCE], _BodyBudget(maximum=0, per_entity=0))))
        measurements['rescue_no_bodies_repeat'] = measure(store, lambda: dict(news._rescue_bodies(
            index, [SOURCE], _BodyBudget(maximum=0, per_entity=0))))
        checks['rescue_reports_all_relevant_budget_exclusions'] = (
            measurements['rescue_no_bodies']['result'].get('body_skipped_budget') == expected_articles)
        checks['rescue_repeat_is_idempotent'] = (measurements['rescue_no_bodies']['result'] ==
                                                measurements['rescue_no_bodies_repeat']['result'])
        measurements['current_bank_projection_build'] = measure(store, lambda: ensure_projection(news, index))
        measurements['coverage_read'] = measure(store, lambda: acquisition_coverage(store, [SOURCE]))
        measurements['coverage_read_warm'] = measure(store, lambda: acquisition_coverage(store, [SOURCE]))
        coverage = measurements['coverage_read']['result']
        checks['coverage_matches_current_synthetic_bank'] = (
            coverage['relevant_articles'] == expected_articles and
            coverage['entities_with_candidates'] == expected_entities and
            coverage['entities_with_bodies'] == 0 and coverage['entities_queried'] == 0)
        checks['coverage_warm_is_identical'] = coverage == measurements['coverage_read_warm']['result']
        query = 'SELECT details FROM news_observations WHERE article_id=? ORDER BY retrieved_at DESC,id DESC LIMIT 10'
        with store.connection() as con:
            params = (f'{1:064x}',)
            plan = [row[3] for row in con.execute('EXPLAIN QUERY PLAN ' + query, params)]
            start = time.perf_counter()
            for _ in range(args.lookup_repeats):
                con.execute(query, params).fetchall()
            measurements['observation_lookup'] = {'query_plan': plan, 'repeats': args.lookup_repeats,
                                                   'seconds': time.perf_counter() - start}
            checks['latest_observations_use_index_without_sort'] = not any('TEMP B-TREE' in value for value in plan)
            checks['rescue_did_not_queue_bodies'] = con.execute("SELECT count(*) FROM jobs WHERE kind='news_article'").fetchone()[0] == 0

        fetcher = SafeFetcher({'trust_env_proxy': False}, state / 'collector')
        dns, http = [], []
        fetcher._dns = lambda host, *a, **kw: dns.append(host) or [(2, 1, 6, '', ('8.8.8.8', 443))]
        fetcher._clock = lambda: 1000.
        posts = [{'id': i, 'link': f'https://example.org/article/{i}', 'date_gmt': '2020-01-01T00:00:00',
                  'title': {'rendered': 'Nota sintética'}, 'excerpt': {'rendered': 'Resumen'}} for i in range(100)]

        def transport(url, *_args):
            http.append(url)
            return 200, {'content-type': 'application/json', 'cache-control': 'max-age=3600',
                         'x-wp-total': '300', 'x-wp-totalpages': '3'}, json.dumps(posts).encode()

        fetcher._transport = transport
        url = SOURCE['url'] + '?page=3&per_page=100'
        try:
            cold = fetcher.fetch(url, SOURCE)
            counts_after_cold = (len(dns), len(http))
            warm = fetcher.fetch(url, SOURCE)
            counts_after_warm = (len(dns), len(http))
            local = fetcher.fetch(url, dict(SOURCE, _cache_mode='local'))
            counts_after_local = (len(dns), len(http))
            miss = fetcher.fetch(SOURCE['url'] + '?page=99', dict(SOURCE, _cache_mode='local'))
            cold_page = parse_wordpress_posts(cold['body'], headers=cold.get('pagination_headers'), page=3)['pagination']
            warm_page = parse_wordpress_posts(warm['body'], headers=warm.get('pagination_headers'), page=3)['pagination']
            measurements['collector_cold_warm'] = {
                'http_transport_calls': len(http), 'dns_calls': len(dns),
                'cold_status': cold['status'], 'warm_status': warm['status'], 'local_status': local['status'],
                'local_miss_status': miss['status'], 'cold_network_requests': cold['network_requests'],
                'warm_network_requests': warm['network_requests'], 'local_network_requests': local['network_requests'],
                'cold_pagination': cold_page, 'warm_pagination': warm_page}
            checks['warm_and_local_do_not_resolve_or_fetch'] = counts_after_cold == counts_after_warm == counts_after_local == (len(dns), len(http))
            checks['cache_preserves_terminal_pagination'] = cold_page == warm_page and warm_page['next_page'] is None and warm_page['total_pages'] == 3
            checks['local_miss_never_uses_network'] = miss['status'] == 'cache_miss' and miss['network_requests'] == 0
        finally:
            fetcher.close()

        noop_engine = Engine(state / 'body-bank', online=True)
        noop_news = NewsAcquirer(noop_engine)
        noop_news.config_path.write_text(json.dumps({'sources': [SOURCE], 'search': {'strategy': 'shared'}}), encoding='utf-8')
        noop_engine.store.import_entities([entities[0]])
        record = {'url': 'https://example.org/one', 'title': entities[0]['name'] + ' presentó un informe.', 'text': 'Un resumen neutral.'}
        observed = noop_news._observe(record, SOURCE, SOURCE['url'], {}, None)
        html = '<article><p>' + entities[0]['name'] + ' presentó un informe público neutral. ' + ('Texto editorial original de control. ' * 20) + '</p></article>'
        response = {'url': record['url'], 'final_url': record['url'], 'body': html,
                    'retrieved_at': '2020-01-01T00:00:00+00:00', 'sha256': digest(html), 'status': 'ok'}
        first = noop_news._conserve_article({'article_id': observed['id'], 'url': record['url']}, SOURCE, response)
        noop_news.attribute(rescore=False)

        def signature():
            with noop_engine.store.connection() as con:
                return con.execute('SELECT attributed_signature FROM news_articles WHERE id=?', (observed['id'],)).fetchone()[0]

        before_signature = signature()
        second = noop_news._conserve_article({'article_id': observed['id'], 'url': record['url']}, SOURCE,
                                            dict(response, status='not_modified', validated_at=now()))
        after_signature = signature()
        again = noop_news.attribute(rescore=False)
        measurements['unchanged_body_revalidation'] = {
            'first_result': first, 'second_result': second,
            'attributed_signature_present_before': bool(before_signature),
            'attributed_signature_present_after': bool(after_signature),
            'redundant_articles_attributed': again['articles_processed']}
        checks['unchanged_body_preserves_attribution_signature'] = bool(before_signature) and before_signature == after_signature
        checks['unchanged_body_skips_redundant_attribution'] = again['articles_processed'] == 0
        checks['audit_chains_valid'] = store.verify_audit()['ok'] and noop_engine.store.verify_audit()['ok']
        report['fixture_database_bytes'] = (state / 'bank' / 'peps.sqlite3').stat().st_size
        report['network_guard'] = dict(guard, real_network_allowed=False)
        checks['no_real_dns_or_sockets_attempted'] = guard['blocked_operations'] == 0
    report['source_sha256_after'] = hashes(args.source_root)
    checks['production_files_unchanged_during_measurement'] = before == report['source_sha256_after']
    if args.baseline:
        checks['baseline_unchanged'] = hashlib.sha256(args.baseline.read_bytes()).hexdigest() == report['baseline']['sha256']
        baseline = report['baseline']['report']['measurements']['rescue_no_bodies']
        current = measurements['rescue_no_bodies']
        comparable = (args.entities, args.articles, args.observations_per_article, args.popular_observations) == (300, 9000, 10, 5000)
        report['comparison'] = {'scope': 'Precursor instrumentation and current versioned projection differences are explicit above.',
                                'same_corpus_cardinalities': comparable,
                                'rescue_seconds_before': baseline['seconds'], 'rescue_seconds_after': current['seconds'],
                                'rescue_speedup': baseline['seconds'] / current['seconds'] if comparable and current['seconds'] else None,
                                'rescue_connections_before': baseline['connections_opened'],
                                'rescue_connections_after': current['connections_opened']}
    report['ok'] = all(checks.values())
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--source-root', type=Path, default=ROOT)
    parser.add_argument('--baseline', type=Path, help='Optional existing JSON to read and embed, never regenerate')
    parser.add_argument('--entities', type=int, default=300)
    parser.add_argument('--articles', type=int, default=9000)
    parser.add_argument('--observations-per-article', type=int, default=10)
    parser.add_argument('--popular-observations', type=int, default=5000)
    parser.add_argument('--lookup-repeats', type=int, default=200)
    args = parser.parse_args()
    if args.entities < 1 or args.articles < 2 or args.observations_per_article < 1 or args.popular_observations < 0 or args.lookup_repeats < 1:
        parser.error('Positive sizes are required (articles >= 2); popular observations may be zero')
    if args.baseline and args.output.resolve() == args.baseline.resolve():
        parser.error('--output must differ from the immutable --baseline')
    report = benchmark(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'output': str(args.output), 'ok': report['ok'], 'checks': report['checks'],
                      'rescue': report['measurements']['rescue_no_bodies']}, ensure_ascii=False, indent=2))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
