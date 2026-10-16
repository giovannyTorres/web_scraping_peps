"""Bounded offline stress probe of current-bank news projection and coverage.

The fixture uses real SQLite and generated metadata: 90% unrelated excerpts,
mentions concentrated on 50 entities, and a small set of URLs with long,
repetitive observation histories. It measures local preparation, never HTTP or
web coverage. --source-root can select a clean baseline checkout of the package.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
from pathlib import Path
import sys
import tempfile
import threading
import time


def peak_rss_kib():
    try:
        import resource
    except ImportError:
        return None
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value / 1024 if sys.platform == 'darwin' else value


def table_signature(con, table):
    """Hash every raw column in stable order without retaining the corpus."""
    digest = hashlib.sha256()
    for row in con.execute(f'SELECT * FROM {table} ORDER BY id'):
        digest.update(json.dumps(tuple(row), ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
        digest.update(b'\n')
    return digest.hexdigest()


def probe(args, folder):
    # Import only after selecting a checkout. Neither the probe nor the package
    # requires HTTP, browsers, optional NLP models or external service accounts.
    sys.path.insert(0, str(args.source_root))
    from peps.engine import Engine
    from peps.news import NewsAcquirer
    from peps.news_projection import ensure_projection, projection_ready
    from peps.relevance import RelevanceIndex
    from peps.acquisition_coverage import acquisition_coverage
    from peps.store import canonical

    started = time.monotonic()
    result = {'schema_version': 1, 'synthetic': True, 'network_requests': 0,
              'fixture': {'deterministic': True, 'timestamp': '2026-01-01T00:00:00+00:00',
                          'entities': args.entities, 'refs': args.refs, 'unique_metadata': args.unique_metadata,
                          'summary_repeat': args.summary_repeat,
                          'hot_urls': min(100, args.refs), 'observations_per_hot_url': args.hot_observations,
                          'mentions': 'Every tenth reference; target IDs concentrated on at most 50 entities.',
                          'metadata': 'Repeated neutral excerpt, with an exact name in relevant observation titles.'},
              'budgets': {'projection_seconds': args.max_seconds,
                          'short_deadline_seconds': args.short_deadline_seconds,
                          'cancel_after_seconds': args.cancel_after_seconds, 'max_rss_mib': args.max_rss_mib},
              'python': platform.python_version(), 'source_root': str(args.source_root),
              'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'source_sha256': {name: hashlib.sha256((args.source_root / name).read_bytes()).hexdigest()
                               for name in ('peps/news_projection.py', 'peps/acquisition_coverage.py',
                                            'peps/news.py', 'peps/relevance.py')}}
    engine = Engine(folder)
    news = NewsAcquirer(engine)
    entities = [{'id': f'entity-{index:05}', 'name': f'Persona Sintética Lote {index:05}'}
                for index in range(args.entities)]
    engine.store.import_entities(entities)
    generic = ('Las autoridades presentaron el informe de actividades públicas y resultados del ejercicio. '
               * args.summary_repeat).strip()
    stamp = result['fixture']['timestamp']
    def card_title(index):
        return 'Información general del día' + (f' {index:06}' if args.unique_metadata else '')
    def card_summary(index):
        return generic + (f' Referencia editorial {index:06}.' if args.unique_metadata else '')
    with engine.store.connection() as con:
        con.execute('BEGIN IMMEDIATE')
        con.executemany('INSERT INTO news_articles(id,canonical_url,publisher_id,source_id,title,summary,discovered_at,scope,status) '
                        'VALUES(?,?,?,?,?,?,?,?,?)',
                        ((f'article-{index:06}', f'https://synthetic.invalid/news/{index}', 'synthetic', 'synthetic',
                          card_title(index), card_summary(index), stamp, 'feed_metadata_only', 'discovered')
                         for index in range(args.refs)))
        def observations():
            for index in range(args.refs):
                target = index % min(50, args.entities)
                title = f'Persona Sintética Lote {target:05} compareció.' if index % 10 == 0 else card_title(index)
                details = canonical({'title': title, 'summary': card_summary(index)})
                yield (f'observation-{index:06}', f'article-{index:06}', 'synthetic', 'https://synthetic.invalid/feed',
                       f'https://synthetic.invalid/news/{index}', stamp, None, details)
            for index in range(min(100, args.refs)):
                for offset in range(args.hot_observations):
                    details = canonical({'title': f'Actualización histórica {offset % 3}', 'summary': card_summary(index)})
                    yield (f'hot-{index:06}-{offset:03}', f'article-{index:06}', 'synthetic', 'https://synthetic.invalid/feed',
                           f'https://synthetic.invalid/news/{index}', stamp, None, details)
        con.executemany('INSERT INTO news_observations VALUES(?,?,?,?,?,?,?,?)', observations())
    result['setup_seconds'] = time.monotonic() - started
    with engine.store.connection() as con:
        result['initial_counts'] = {table: con.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                                    for table in ('entities', 'news_articles', 'news_observations', 'audit')}
        result['raw_before_sha256'] = {table: table_signature(con, table)
                                     for table in ('news_articles', 'news_observations')}
    index = RelevanceIndex(entities)
    before_rss = peak_rss_kib()
    start = time.monotonic()
    ready = ensure_projection(news, index, deadline=start + args.max_seconds)
    after_rss = peak_rss_kib()
    result['projection'] = {'ready': ready, 'elapsed_seconds': time.monotonic() - start,
                            'peak_rss_kib': after_rss,
                            'rss_peak_delta_kib': after_rss - before_rss if after_rss is not None and before_rss is not None else None}
    start = time.monotonic()
    result['coverage'] = acquisition_coverage(engine.store, [])
    result['coverage_first_seconds'] = time.monotonic() - start
    start = time.monotonic()
    for _ in range(args.cached_reads):
        acquisition_coverage(engine.store, [])
    result['coverage_cached_mean_seconds'] = (time.monotonic() - start) / args.cached_reads
    with engine.store.connection() as con:
        result['after_counts'] = {table: con.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                                  for table in ('entities', 'news_articles', 'news_observations', 'news_current_candidates', 'audit')}
        row = con.execute("SELECT payload FROM audit WHERE action='news_current_candidates_rebuilt' ORDER BY seq DESC LIMIT 1").fetchone()
        result['rebuild_audit'] = json.loads(row[0]) if row else None
    # A correction with an unchanged entity ID must hide all stale projections.
    engine.store.import_entities([dict(entities[0], name='Persona Sintética Corregida')])
    result['after_bank_change'] = acquisition_coverage(engine.store, [])
    start = time.monotonic()
    short_ready = ensure_projection(news, index, deadline=start + args.short_deadline_seconds)
    result['short_deadline'] = {'ready': short_ready, 'elapsed_seconds': time.monotonic() - start}
    engine.stop_event.set()
    start = time.monotonic()
    cancel_ready = ensure_projection(news, index, deadline=start + args.max_seconds)
    result['already_cancelled'] = {'ready': cancel_ready, 'elapsed_seconds': time.monotonic() - start}
    engine.stop_event.clear()
    requested = [None]
    def cancel():
        requested[0] = time.monotonic()
        engine.stop_event.set()
    timer = threading.Timer(args.cancel_after_seconds, cancel)
    start = time.monotonic()
    timer.start()
    try:
        cancel_ready = ensure_projection(news, index, deadline=start + args.max_seconds)
        finished = time.monotonic()
    finally:
        timer.cancel()
        timer.join()
        engine.stop_event.clear()
    result['inflight_cancel'] = {'ready': cancel_ready, 'elapsed_seconds': finished - start,
                                 'requested_after_seconds': requested[0] - start if requested[0] is not None else None,
                                 'response_after_request_seconds': finished - requested[0] if requested[0] is not None else None}
    with engine.store.connection() as con:
        result['rollback_counts'] = {table: con.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                                     for table in ('news_articles', 'news_observations', 'news_current_candidates')}
        result['raw_after_sha256'] = {table: table_signature(con, table)
                                    for table in ('news_articles', 'news_observations')}
        result['ready_after_abort'] = projection_ready(con)
    result['history_preserved'] = result['raw_before_sha256'] == result['raw_after_sha256']
    result['audit_valid'] = engine.store.verify_audit()['ok']
    result['db_bytes'] = engine.store.path.stat().st_size
    result['elapsed_total_seconds'] = time.monotonic() - started
    expected_candidates = (args.refs + 9) // 10
    expected_entities = len({index % min(50, args.entities) for index in range(0, args.refs, 10)})
    expected_observations = args.refs + min(100, args.refs) * args.hot_observations
    cache = (result['rebuild_audit'] or {}).get('relevance_cache')
    cancellation_response = result['inflight_cancel']['response_after_request_seconds']
    result['completed'] = ready
    result['checks'] = {
        'fixture_counts_correct': result['initial_counts']['entities'] == args.entities
            and result['initial_counts']['news_articles'] == args.refs
            and result['initial_counts']['news_observations'] == expected_observations,
        'projection_completed': ready,
        'projection_counts_correct': ready and result['coverage']['relevant_articles'] == expected_candidates
            and result['coverage']['entities_with_candidates'] == expected_entities
            and result['after_counts']['news_current_candidates'] == expected_candidates,
        'bounded_deadline_respected': result['projection']['elapsed_seconds'] <= args.max_seconds + .25,
        'short_deadline_respected': result['short_deadline']['elapsed_seconds'] <= args.short_deadline_seconds + .05,
        'already_cancelled_no_work': result['already_cancelled']['elapsed_seconds'] <= .1,
        'inflight_cancellation_respected': cancellation_response <= .1 if cancellation_response is not None else None,
        'aborted_rebuild_not_published': result['ready_after_abort'] is False
            if result['inflight_cancel']['ready'] is False else True,
        'rollback_keeps_previous_projection': result['rollback_counts']['news_current_candidates']
            == result['after_counts']['news_current_candidates'] if result['inflight_cancel']['ready'] is False else True,
        'history_preserved': result['history_preserved'],
        'audit_valid': result['audit_valid'],
        'memory_within_budget': after_rss <= args.max_rss_mib * 1024 if after_rss is not None else None,
        'cache_limits_respected': cache['entries'] <= cache['maximum_entries']
            and cache['accounted_bytes'] <= cache['maximum_bytes'] if isinstance(cache, dict) else None,
    }
    # Unmeasured optional counters stay null. An incomplete projection always
    # exits nonzero, even when its deadline, cancellation and history are sound.
    result['passed'] = all(value is not False for value in result['checks'].values())
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--state', type=Path, help='Keep the generated database in a new directory; default cleans up a temporary database.')
    parser.add_argument('--entities', type=int, default=3000)
    parser.add_argument('--refs', type=int, default=90001)
    parser.add_argument('--summary-repeat', type=int, default=10)
    parser.add_argument('--unique-metadata', action='store_true', help='Give every card distinct title and summary; defeats exact-metadata memoization.')
    parser.add_argument('--max-rss-mib', type=float, default=256)
    parser.add_argument('--hot-observations', type=int, default=100)
    parser.add_argument('--cached-reads', type=int, default=100)
    parser.add_argument('--max-seconds', type=float, default=60)
    parser.add_argument('--short-deadline-seconds', type=float, default=.05)
    parser.add_argument('--cancel-after-seconds', type=float, default=.05)
    args = parser.parse_args()
    args.source_root = args.source_root.resolve()
    if not (args.source_root / 'peps' / 'news_projection.py').is_file():
        parser.error('--source-root must contain peps/news_projection.py')
    for name in ('entities', 'refs', 'summary_repeat', 'cached_reads'):
        if getattr(args, name) < 1:
            parser.error(f'--{name.replace("_", "-")} must be positive')
    if args.hot_observations < 0:
        parser.error('--hot-observations cannot be negative')
    for name in ('max_seconds', 'short_deadline_seconds', 'cancel_after_seconds', 'max_rss_mib'):
        if not math.isfinite(getattr(args, name)) or getattr(args, name) <= 0:
            parser.error(f'--{name.replace("_", "-")} must be finite and positive')
    if args.state is not None:
        args.state.mkdir(parents=True, exist_ok=False)
        result = probe(args, args.state)
    else:
        with tempfile.TemporaryDirectory(prefix='webscope-projection-stress-') as folder:
            result = probe(args, folder)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'output': str(args.output), 'projection': result['projection'],
                      'coverage_first_seconds': result['coverage_first_seconds'],
                      'coverage_cached_mean_seconds': result['coverage_cached_mean_seconds'],
                      'short_deadline': result['short_deadline'], 'inflight_cancel': result['inflight_cancel'],
                      'history_preserved': result['history_preserved'], 'completed': result['completed'],
                      'passed': result['passed'], 'failed_checks': [key for key, value in result['checks'].items() if value is False]},
                     ensure_ascii=False, indent=2))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
