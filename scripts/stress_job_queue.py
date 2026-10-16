"""Exercise real SQLite job queues with synthetic data and no networking.

python scripts/stress_job_queue.py --output runtime/job-queue-stress.json

Use --source-root for another checkout; Git and a baseline revision are not
required. To repeat the sequential 100,000-row measurement, use --cases queue
--workers 1 --pending 50000 --history 50000 --processed 100. Run the same command
against two source roots for a comparison. There are no speed thresholds.
Every case runs in a separate process with a hard timeout and fresh temporary
databases. A failed correctness check or timeout returns exit status 1.
These are local storage measurements, not public-web throughput guarantees.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import importlib
import json
from pathlib import Path
import platform
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
CASE_NAMES = ('queue', 'retry', 'lock', 'malformed')


@contextmanager
def no_network():
    counters = {'blocked_operations': 0}

    def forbidden(*_args, **_kwargs):
        counters['blocked_operations'] += 1
        raise AssertionError('Real DNS and sockets are forbidden in this synthetic stress test')

    with patch.object(socket, 'getaddrinfo', forbidden), \
         patch.object(socket, 'create_connection', forbidden), \
         patch.object(socket.socket, 'connect', forbidden), \
         patch.object(socket.socket, 'connect_ex', forbidden):
        yield counters


def source_hash(source_root):
    return hashlib.sha256((source_root / 'peps/store.py').read_bytes()).hexdigest()


def seed(store, rows, stamp):
    with store.connection() as con:
        con.executemany('INSERT INTO jobs(id,kind,payload,status,attempts,due,created_at,updated_at) '
                        'VALUES(?,?,?,?,?,?,?,?)', ((*row, stamp, stamp) for row in rows))


def queue_case(args, module, state):
    store = module.Store(state / 'queue.sqlite3')
    started = time.perf_counter()
    seed(store, ((module.digest(str(i)), 'news_article', module.canonical({'synthetic': i}),
                 'queued' if i < args.pending else 'done', 0, 0)
                for i in range(args.pending + args.history)), module.now())
    setup_seconds = time.perf_counter() - started
    gate, reserved = threading.Lock(), [0]

    def worker():
        delivered, finishes = [], []
        while True:
            with gate:
                if reserved[0] >= args.processed:
                    return delivered, finishes
                reserved[0] += 1
            job = store.claim(kind_prefix='news_')
            if job is None:
                return delivered, finishes
            delivered.append(job['id'])
            finishes.append(store.finish(job))

    with store.connection() as anchor:
        anchor.execute('SELECT value FROM meta LIMIT 1').fetchone()
        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            results = list(pool.map(lambda _: worker(), range(args.workers)))
        seconds = time.perf_counter() - started
    delivered = [value for batch, _ in results for value in batch]
    finished = [value for _, batch in results for value in batch]
    with store.connection() as con:
        counts = dict(con.execute('SELECT status,count(*) FROM jobs GROUP BY status'))
    expected = {key: value for key, value in {'queued': args.pending - args.processed,
                                             'done': args.history + args.processed}.items() if value}
    return {'setup_seconds': setup_seconds, 'seconds': seconds, 'jobs_processed': len(delivered),
            'checks': {'exact_number_delivered': len(delivered) == args.processed,
                       'unique_deliveries': len(set(delivered)) == len(delivered),
                       'all_finishes_accepted': len(finished) == args.processed and all(finished),
                       'persisted_counts_exact': counts == expected,
                       'audit_valid': store.verify_audit()['ok']}}


def retry_case(args, module, state):
    store = module.Store(state / 'retry.sqlite3')
    later = time.time() + 3600
    delayed = max(1, args.retry_jobs // 10)
    identifiers = [module.digest('retry-' + str(i)) for i in range(args.retry_jobs)]
    unrelated, live = module.digest('unrelated'), module.digest('live')
    seed(store, [(identifier, 'news_article', '{}', 'dead', 5, later if i < delayed else 0)
                 for i, identifier in enumerate(identifiers)] +
         [(unrelated, 'newsflash', '{}', 'queued', 0, 0),
          (live, 'news_article', '{}', 'running', 2, 0)], module.now())
    with store.connection() as con:
        con.execute('UPDATE jobs SET owner=?,lease_until=? WHERE id=?', ('synthetic-live-worker', later, live))
    groups = [identifiers[i:i + 100] for i in range(0, len(identifiers), 100)]
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        retries = list(pool.map(store.retry_news_jobs, groups * 3))

    def drain(_):
        result, accepted = [], []
        while True:
            job = store.claim(kind_prefix='news_')
            if job is None:
                return result, accepted
            result.append(job['id'])
            accepted.append(store.finish(job))

    with store.connection() as anchor, ThreadPoolExecutor(max_workers=args.workers) as pool:
        anchor.execute('SELECT value FROM meta LIMIT 1').fetchone()
        drains = [pool.submit(drain, i) for i in range(args.workers)]
        repeated = [pool.submit(store.retry_news_jobs, group) for group in groups]
        drained = [future.result() for future in drains]
        repeated_results = [future.result() for future in repeated]
    delivered = [value for batch, _ in drained for value in batch]
    accepted = [value for _, batch in drained for value in batch]
    seconds = time.perf_counter() - started
    with store.connection() as con:
        future_count = con.execute('SELECT count(*) FROM jobs WHERE due=? AND attempts=0 AND status=?',
                                   (later, 'queued')).fetchone()[0]
        foreign_state = con.execute('SELECT status,attempts FROM jobs WHERE id=?', (unrelated,)).fetchone()
        live_state = con.execute('SELECT status,owner,lease_until FROM jobs WHERE id=?', (live,)).fetchone()
    expected = set(identifiers[delayed:])
    return {'seconds': seconds, 'jobs_processed': len(delivered), 'delayed_jobs': delayed,
            'checks': {'retried_once': sum(value['retried'] for value in retries) == args.retry_jobs,
                       'repeated_retry_does_not_reset_live_or_done': all(value['retried'] == 0 for value in repeated_results),
                       'unique_deliveries': len(delivered) == len(set(delivered)),
                       'exact_ready_jobs_delivered': set(delivered) == expected,
                       'all_finishes_accepted': all(accepted), 'future_backoff_preserved': future_count == delayed,
                       'unrelated_prefix_untouched': tuple(foreign_state) == ('queued', 0),
                       'live_lease_preserved': tuple(live_state) == ('running', 'synthetic-live-worker', later),
                       'audit_valid': store.verify_audit()['ok']}}


def lock_case(args, module, state):
    store = module.Store(state / 'lock.sqlite3')
    identifier = store.enqueue('news_article', {'synthetic': True}, due=0)
    original = store.connection

    @contextmanager
    def brief_timeout():
        with original() as con:
            con.execute('PRAGMA busy_timeout=25')
            yield con

    lock_timeout = False
    started = time.perf_counter()
    with original() as blocker:
        blocker.execute('BEGIN IMMEDIATE')
        store.connection = brief_timeout
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(store.claim, kind_prefix='news_')
                try:
                    future.result(timeout=min(args.timeout_seconds, 5))
                except sqlite3.OperationalError as exc:
                    lock_timeout = 'locked' in str(exc).lower()
        finally:
            store.connection = original
        row = blocker.execute('SELECT status,attempts,owner,lease_until FROM jobs WHERE id=?', (identifier,)).fetchone()
    job = store.claim(kind_prefix='news_')
    recovered = bool(job and job['id'] == identifier and job['attempts'] == 1 and store.finish(job))
    return {'seconds': time.perf_counter() - started,
            'checks': {'real_sqlite_lock_timeout_observed': lock_timeout,
                       'failed_claim_did_not_mutate_job': tuple(row) == ('queued', 0, None, None),
                       'recovered_after_lock_released': recovered, 'audit_valid': store.verify_audit()['ok']}}


def malformed_case(args, module, state):
    store = module.Store(state / 'malformed.sqlite3')
    raw = ['{invalid', '[]', 'null', '3', '"synthetic-secret-marker"']
    identifiers = [module.digest('bad-' + str(i)) for i in range(len(raw))]
    valid = module.digest('valid')
    seed(store, [(identifier, 'news_article', value, 'queued', 0, i)
                 for i, (identifier, value) in enumerate(zip(identifiers, raw))] +
         [(valid, 'news_article', '{}', 'queued', 0, len(raw) + 1)], module.now())
    started = time.perf_counter()
    job = store.claim(kind_prefix='news_')
    with store.connection() as con:
        rows = [con.execute('SELECT status,attempts,payload FROM jobs WHERE id=?', (identifier,)).fetchone()
                for identifier in identifiers]
        audits = [row[0] for row in con.execute("SELECT payload FROM audit WHERE action='job_invalid_payload'")]
    return {'seconds': time.perf_counter() - started,
            'checks': {'valid_work_not_starved': bool(job and job['id'] == valid and store.finish(job)),
                       'malformed_rows_quarantined': all(tuple(row[:2]) == ('dead', 0) for row in rows),
                       'raw_history_preserved': [row['payload'] for row in rows] == raw,
                       'quarantines_audited_without_payload': len(audits) == len(raw) and 'synthetic-secret-marker' not in ''.join(audits),
                       'audit_valid': store.verify_audit()['ok']}}


CASES = {'queue': queue_case, 'retry': retry_case, 'lock': lock_case, 'malformed': malformed_case}


def worker(args):
    started = time.perf_counter()
    try:
        before = source_hash(args.source_root)
        sys.path.insert(0, str(args.source_root))
        with no_network() as network:
            module = importlib.import_module('peps.store')
            result = CASES[args.worker_case](args, module, args.worker_state)
        result['checks'].update(no_network_attempted=network['blocked_operations'] == 0,
                                production_code_unchanged=before == source_hash(args.source_root))
        result.update(case=args.worker_case, network_guard=network, source_sha256=before)
        result['passed'] = all(result['checks'].values())
    except Exception as exc:
        result = {'case': args.worker_case, 'passed': False, 'error': type(exc).__name__ + ': ' + str(exc)}
    result['total_case_seconds'] = time.perf_counter() - started
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return 0 if result['passed'] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', type=Path, default=ROOT)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--pending', type=int, default=50000)
    parser.add_argument('--history', type=int, default=50000)
    parser.add_argument('--processed', type=int, default=100)
    parser.add_argument('--workers', type=int, default=8)
    parser.add_argument('--retry-jobs', type=int, default=300)
    parser.add_argument('--timeout-seconds', type=float, default=60)
    parser.add_argument('--cases', nargs='+', choices=CASE_NAMES, default=list(CASE_NAMES))
    parser.add_argument('--worker-case', choices=CASE_NAMES, help=argparse.SUPPRESS)
    parser.add_argument('--worker-state', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    args.source_root = args.source_root.resolve()
    if not (args.source_root / 'peps/store.py').is_file():
        parser.error('--source-root must contain peps/store.py')
    if not 1 <= args.workers <= 32 or args.pending < 1 or args.history < 0 or not 1 <= args.processed <= args.pending or args.retry_jobs < 2 or not 0 < args.timeout_seconds <= 120:
        parser.error('Require workers1..32, pending>=processed>=1, history>=0, retry-jobs>=2 and timeout-seconds>0..120')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.worker_case:
        return worker(args)
    before = source_hash(args.source_root)
    report = {'schema_version': 1, 'synthetic': True, 'generated_at': datetime.now(timezone.utc).isoformat(),
              'method': 'Fresh temporary SQLite per case, subprocess timeouts, no baseline or Git dependency. No speed thresholds or network-throughput claims.',
              'python': platform.python_version(), 'sqlite': sqlite3.sqlite_version, 'platform': platform.platform(),
              'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), 'source_sha256': before,
              'parameters': {key: getattr(args, key) for key in ('pending', 'history', 'processed', 'workers', 'retry_jobs', 'timeout_seconds')},
              'cases': []}
    with tempfile.TemporaryDirectory(prefix='peps-job-queue-stress-') as temporary:
        for name in dict.fromkeys(args.cases):
            state = Path(temporary) / name
            state.mkdir()
            output = state / 'result.json'
            command = [sys.executable, str(Path(__file__).resolve()), '--source-root', str(args.source_root),
                       '--output', str(output), '--worker-case', name, '--worker-state', str(state)]
            for key in ('pending', 'history', 'processed', 'workers', 'retry_jobs', 'timeout_seconds'):
                command.extend(['--' + key.replace('_', '-'), str(getattr(args, key))])
            try:
                process = subprocess.run(command, capture_output=True, text=True, timeout=args.timeout_seconds)
            except subprocess.TimeoutExpired:
                result = {'case': name, 'passed': False, 'timed_out': True, 'error': 'Case exceeded its hard subprocess timeout'}
            else:
                result = json.loads(output.read_text(encoding='utf-8')) if output.exists() else {
                    'case': name, 'passed': False, 'error': 'Worker did not return a report'}
                result.update(timed_out=False, worker_exit_code=process.returncode)
                result['passed'] = result['passed'] and process.returncode == 0
            report['cases'].append(result)
    report['production_code_unchanged'] = before == source_hash(args.source_root)
    report['passed'] = report['production_code_unchanged'] and all(case['passed'] for case in report['cases'])
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'output': str(args.output), 'passed': report['passed'],
                      'cases': [{key: case[key] for key in ('case', 'passed', 'timed_out', 'seconds') if key in case}
                                for case in report['cases']]}, ensure_ascii=False, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
