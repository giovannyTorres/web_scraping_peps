"""Reproduce local news planner, scheduler and preview costs without network.

All identities and jobs are synthetic. The scheduler uses real SQLite claims,
leases, commits and progress callbacks, but replaces task execution with a
durable completion. Wall times therefore exclude HTTP and article extraction;
they must not be projected to download speed, coverage or an ETA guarantee.

Run this same script with --source-root pointing at separate clean checkouts
to compare revisions. Each report records actual module hashes and Git state;
there are no timing thresholds. The original exploratory runtime/core JSONs
predate this script and did not record source hashes.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import socket
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_NOW = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)
SETTINGS = {'strategy': 'targeted', 'start': '2016-01-01', 'end': '2026-10-15',
            'max_queries': 10000, 'include_aliases': True, 'use_context': True}
SOURCE = {'id': 'synthetic-publisher', 'source_name': 'Synthetic publisher',
          'kind': 'rss', 'search_kind': 'wordpress_rss',
          'search_url': 'https://publisher.invalid/', 'base_url': 'https://publisher.invalid/',
          'allowed_hosts': ['publisher.invalid'], 'enabled': True, 'terms_approved': True,
          'search_max_records': 10, 'min_interval_seconds': 5, 'min_window_seconds': 86400}


@contextmanager
def no_network():
    def forbidden(*_args, **_kwargs):
        raise AssertionError('Network is forbidden in this synthetic benchmark')
    with patch.object(socket, 'getaddrinfo', forbidden), \
         patch.object(socket, 'create_connection', forbidden), \
         patch.object(socket.socket, 'connect', forbidden), \
         patch.object(socket.socket, 'connect_ex', forbidden):
        yield


class NoNetworkFetcher:
    def __init__(self, *_args, **_kwargs):
        pass

    def fetch(self, *_args, **_kwargs):
        raise AssertionError('Scheduler fixture must not fetch a document')


def fixture_entities(count, homonyms=False):
    for index in range(count):
        yield {'id': f'synthetic-{index:05d}',
               'name': 'Persona Sintética Homónima' if homonyms else f'Persona Sintética {index:05d}',
               'aliases': [] if homonyms else [f'Alias Sintético {index:05d}'],
               'context': {} if homonyms else {'state': 'Oaxaca', 'role': 'alcalde'}}


def source_hashes(source_root):
    paths = ['peps/' + name + '.py' for name in
             ('news', 'news_search', 'engine', 'store', 'identity', 'collector')]
    return {path: hashlib.sha256((source_root / path).read_bytes()).hexdigest() for path in paths}


def git_state(source_root):
    def git(*args):
        try:
            return subprocess.check_output(['git', '-C', str(source_root), *args],
                                           text=True, stderr=subprocess.DEVNULL).strip()
        except (OSError, subprocess.CalledProcessError):
            return None
    return {'head': git('rev-parse', 'HEAD'), 'status_porcelain': git('status', '--porcelain')}


def benchmark(args):
    sys.path.insert(0, str(args.source_root))
    from peps.engine import Engine
    from peps.news import NewsAcquirer
    from peps.news_search import build_search_plan

    before_hashes = source_hashes(args.source_root)
    report = {'schema_version': 1, 'label': args.label,
              'created_at': datetime.now(timezone.utc).isoformat(),
              'python': platform.python_version(), 'platform': platform.platform(),
              'source_root': str(args.source_root), 'git': git_state(args.source_root),
              'source_hashes_before': before_hashes,
              'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'fixture': {'now': FIXTURE_NOW.isoformat(), 'source': SOURCE, 'settings': SETTINGS,
                          'entity_sizes': args.entities, 'queue_sizes': args.queue_sizes,
                          'max_processed': args.processed, 'workers': args.workers,
                          'preview_entities': args.preview_entities},
              'plans': [], 'homonyms': [], 'scheduler': [],
              'limitations': ['Synthetic SQLite tasks completed without HTTP or article extraction.',
                              'Wall time varies with CPU, storage and antivirus; no latency thresholds.',
                              'Timing excludes fixture setup; no claim about coverage, identity or ETA accuracy.',
                              'Compare source hashes as well as Git HEAD when the checkout is dirty.']}
    with no_network():
        for count in args.entities:
            rows = list(fixture_entities(count))
            started = time.perf_counter()
            plan = build_search_plan(rows, [SOURCE], SETTINGS, now=FIXTURE_NOW)
            report['plans'].append({'entities': count, 'seconds': time.perf_counter() - started,
                                    'queries': plan['query_count'], 'required': plan['queries_required']})
            rows = list(fixture_entities(count, homonyms=True))
            started = time.perf_counter()
            plan = build_search_plan(rows, [SOURCE],
                                     {'start': '2020-01-01', 'end': '2020-12-31',
                                      'include_aliases': False, 'use_context': False}, now=FIXTURE_NOW)
            report['homonyms'].append({'entities': count, 'seconds': time.perf_counter() - started,
                                      'queries': plan['query_count'],
                                      'query_links': len(plan['queries'][0]['entity_ids'])})
            if report['homonyms'][-1]['query_links'] != count:
                raise AssertionError('Homonym planner lost synthetic identity links')

        for size in args.queue_sizes:
            with tempfile.TemporaryDirectory(prefix='peps-news-runtime-') as directory:
                engine = Engine(directory, online=True)
                news = NewsAcquirer(engine)
                news.config_path.write_text(json.dumps({'sources': [SOURCE], 'search': SETTINGS}), encoding='utf-8')
                with engine.store.connection() as con:
                    con.execute('BEGIN IMMEDIATE')
                    for index in range(size):
                        engine.store.enqueue('news_fixture', {'index': index}, due=0, connection=con)
                events = []
                def execute(job, *_args):
                    engine.store.finish(job)
                with patch('peps.collector.SafeFetcher', NoNetworkFetcher), \
                     patch.object(news, 'execute', side_effect=execute), \
                     patch.object(news, '_runtime_queue', wraps=news._runtime_queue) as snapshots:
                    started = time.perf_counter()
                    result = news.acquire(queue=False, workers=args.workers, seconds=300,
                                          max_articles=args.processed, progress=events.append)
                    elapsed = time.perf_counter() - started
                if result['completed'] != min(size, args.processed):
                    raise AssertionError('Scheduler fixture did not complete its intended task budget')
                report['scheduler'].append({'queue': size, 'processed': result['completed'],
                                            'seconds': elapsed, 'snapshots': snapshots.call_count,
                                            'events': len(events), 'pending': result['pending'],
                                            'stop_reason': result['stop_reason']})

        if args.preview_entities:
            with tempfile.TemporaryDirectory(prefix='peps-news-preview-') as directory:
                engine = Engine(directory)
                news = NewsAcquirer(engine)
                news.config_path.write_text(json.dumps({'sources': [SOURCE], 'search': SETTINGS}), encoding='utf-8')
                engine.store.import_entities(fixture_entities(args.preview_entities))
                started = time.perf_counter()
                cold = news.search_plan()
                cold_seconds = time.perf_counter() - started
                started = time.perf_counter()
                warm = news.search_plan()
                report['preview'] = {'entities': args.preview_entities, 'cold_seconds': cold_seconds,
                                     'warm_seconds': time.perf_counter() - started,
                                     'queries': cold['query_count'], 'same_result': cold == warm}
    report['source_hashes_after'] = source_hashes(args.source_root)
    report['source_unchanged_during_measurement'] = before_hashes == report['source_hashes_after']
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--entities', type=int, nargs='+', default=[326, 10000])
    parser.add_argument('--queue-sizes', type=int, nargs='+', default=[1000, 10000])
    parser.add_argument('--processed', type=int, default=200)
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--preview-entities', type=int, default=10000, help='0 disables preview measurement')
    parser.add_argument('--source-root', type=Path, default=ROOT, help='Repository checkout to import and measure')
    parser.add_argument('--label', default='working-tree')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    args.source_root = args.source_root.resolve()
    if min([*args.entities, *args.queue_sizes, args.processed, args.workers]) < 1 or args.preview_entities < 0:
        parser.error('sizes, processed and workers must be positive; preview-entities may be zero')
    result = benchmark(args)
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding='utf-8')
    print(rendered, end='')


if __name__ == '__main__':
    main()
