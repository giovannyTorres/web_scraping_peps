"""Bounded, offline stress suite for the real news pipeline.

python scripts/stress_pipeline.py --profile limit --output runtime/stress.json

Each case runs in its own process and temporary directory. Real DNS/socket
connections are forbidden. Public HTTP responses are synthetic: wall times
describe local processing, never internet speed or editorial coverage.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import runpy
import socket
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def source_hashes(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((root / 'peps').rglob('*.py'))}


def worker(script, arguments, root, receipt):
    attempts = [0]

    def forbidden(*_args, **_kwargs):
        attempts[0] += 1
        raise AssertionError('Real DNS/sockets forbidden in offline stress')

    started, cpu = time.perf_counter(), time.process_time()
    code, error = 0, None
    sys.path.insert(0, str(root))
    sys.argv = [str(root / script), *arguments]
    try:
        with patch.object(socket, 'getaddrinfo', forbidden), \
             patch.object(socket, 'create_connection', forbidden), \
             patch.object(socket.socket, 'connect', forbidden), \
             patch.object(socket.socket, 'connect_ex', forbidden):
            runpy.run_path(str(root / script), run_name='__main__')
    except SystemExit as exc:
        code = exc.code if type(exc.code) is int else int(exc.code is not None)
    except Exception as exc:
        code, error = 1, f'{type(exc).__name__}: {exc}'[:1000]
        import traceback
        traceback.print_exc()
    try:
        import resource
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        rss = rss if sys.platform == 'darwin' else rss * 1024
    except ImportError:
        rss = None
    receipt.write_text(json.dumps({'exit_code': code, 'error': error,
        'wall_seconds': time.perf_counter() - started, 'cpu_seconds': time.process_time() - cpu,
        'peak_process_rss_bytes': rss, 'blocked_real_network_attempts': attempts[0]}, indent=2) + '\n', encoding='utf-8')
    return code


def cases(profile, results):
    limit = profile == 'limit'
    return {
        'acquisition': ('scripts/benchmark_news_load.py', [
            '--cases', '10000:32' if limit else '100:8', '--bank-size', '40000' if limit else '1000',
            '--seconds-per-phase', '300' if limit else '60', '--output', str(results / 'acquisition.json')]),
        'queue': ('scripts/benchmark_news_runtime.py', [
            '--entities', '300', '10000' if limit else '1000',
            '--queue-sizes', '100000' if limit else '10000',
            '--processed', '1000' if limit else '100', '--workers', '32' if limit else '8',
            '--preview-entities', '10000' if limit else '1000', '--output', str(results / 'queue.json')]),
        'mixed': ('scripts/benchmark_mass_acquisition.py', [
            '--entities', '300' if limit else '30', '--irrelevant', '250', '--workers', '16' if limit else '8',
            '--seconds', '120', '--budget-clock', 'real', '--identity-modes', 'known', 'homonyms',
            '--scenarios', 'mixed_risk', 'polluted', '--output', str(results / 'mixed.json')]),
        'transport': ('scripts/stress_transport.py', ['--output', str(results / 'transport.json')]),
        'projection': ('scripts/stress_news_projection.py', [
            '--entities', '10000' if limit else '300', '--refs', '90001' if limit else '1001',
            '--output', str(results / 'projection.json')]),
        'unique_projection': ('scripts/stress_news_projection.py', [
            '--entities', '10000' if limit else '300', '--refs', '90001' if limit else '1001',
            '--unique-metadata', '--max-seconds', '5' if limit else '.05',
            '--output', str(results / 'unique_projection.json')]),
        'rescue': ('scripts/stress_news_rescue.py', [
            '--entities', '3000' if limit else '300', '--refs', '90000' if limit else '1000',
            '--build-projection', '--output', str(results / 'rescue.json')]),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', choices=('quick', 'limit'), default='limit')
    parser.add_argument('--source-root', type=Path, default=ROOT)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--cases', nargs='+', choices=('acquisition', 'queue', 'mixed', 'transport',
                        'projection', 'unique_projection', 'rescue'),
                        default=['acquisition', 'queue', 'mixed', 'transport', 'projection', 'unique_projection', 'rescue'])
    parser.add_argument('--case-timeout', type=float, default=420.)
    parser.add_argument('--worker-script', help=argparse.SUPPRESS)
    parser.add_argument('--worker-arguments', help=argparse.SUPPRESS)
    parser.add_argument('--worker-receipt', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    root, output = args.source_root.resolve(), args.output.resolve()
    if args.worker_script:
        return worker(args.worker_script, json.loads(args.worker_arguments), root, args.worker_receipt)
    if not 1 <= args.case_timeout <= 1800:
        parser.error('--case-timeout must be between 1 and 1800 seconds')
    output.parent.mkdir(parents=True, exist_ok=True)
    results = output.with_name(output.stem + '-cases')
    results.mkdir(exist_ok=True)
    before = source_hashes(root)
    report = {'schema_version': 1, 'generated_at': datetime.now(timezone.utc).isoformat(),
        'kind': 'synthetic_offline_stress_not_web_throughput', 'profile': args.profile,
        'source_root': str(root), 'source_sha256': before,
        'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'platform': platform.platform(), 'python': platform.python_version(),
        'case_timeout_seconds': args.case_timeout, 'cases': [],
        'limits': ['Synthetic HTTP only; no claim about public search speed, relevance or coverage.',
                   'Peak RSS is the child process lifetime high-water mark; null on unsupported platforms.',
                   'A timed-out case fails and has no completed timing/RSS measurement.',
                   'unique_projection deliberately exhausts its preparation budget: success here means a controlled, audited rollback, never completed coverage. Its individual report retains passed:false and exit code1.',
                   'The fixture generators and full integrity checks are included in process wall time.']}
    for name in args.cases:
        script, arguments = cases(args.profile, results)[name]
        receipt, log = results / (name + '-worker.json'), results / (name + '.log')
        receipt.unlink(missing_ok=True)
        with tempfile.TemporaryDirectory(prefix='webscope-stress-') as temporary, log.open('w', encoding='utf-8') as stream:
            env = dict(os.environ, TMPDIR=temporary, TEMP=temporary, TMP=temporary)
            command = [sys.executable, str(Path(__file__).resolve()), '--source-root', str(root),
                       '--output', str(output), '--worker-script', script,
                       '--worker-arguments', json.dumps(arguments), '--worker-receipt', str(receipt)]
            started, timed_out = time.perf_counter(), False
            try:
                result = subprocess.run(command, env=env, stdout=stream, stderr=subprocess.STDOUT,
                                        timeout=args.case_timeout, check=False)
                exit_code = result.returncode
            except subprocess.TimeoutExpired:
                exit_code, timed_out = None, True
            measured = json.loads(receipt.read_text(encoding='utf-8')) if receipt.exists() else {}
            result_path = results / (name + '.json')
            result_checks = {}
            expected_exit = 0
            if result_path.exists():
                value = json.loads(result_path.read_text(encoding='utf-8'))
                if name == 'mixed':
                    result_checks['nonempty_cases'] = bool(value.get('cases'))
                    result_checks['all_queries_completed'] = all(case['counts']['entities_unqueried'] == 0
                                                                  for case in value.get('cases', []))
                    result_checks['all_acquisition_jobs_completed'] = all(case['acquire']['pending'] == 0
                        and case['acquire']['stop_reason'] == 'completed' for case in value.get('cases', []))
                elif name == 'acquisition':
                    result_checks['complete_cold_and_warm_acquisition'] = value.get('all_passed') is True
                elif name == 'unique_projection':
                    # Negative control: do not turn arbitrary errors into a
                    # passing stress case. Only the two completion checks may
                    # fail; history, rollback, cancellation and memory must pass.
                    expected_exit = 1
                    checks = value.get('checks', {})
                    completion = {'projection_completed', 'projection_counts_correct'}
                    result_checks['explicitly_incomplete'] = value.get('completed') is False and value.get('passed') is False
                    result_checks['budget_controls_pass'] = bool(checks) and all(v is not False
                        for key, v in checks.items() if key not in completion)
                    result_checks['only_completion_checks_failed'] = {key for key, v in checks.items()
                        if v is False} == completion
            else:
                result_checks['result_exists'] = False
            passed = (exit_code == expected_exit and measured.get('blocked_real_network_attempts') == 0
                      and not timed_out and all(result_checks.values()))
            report['cases'].append(dict(name=name, passed=passed, timed_out=timed_out,
                expected_outcome='controlled_budget_stop' if name == 'unique_projection' else 'complete_and_correct',
                process_wall_seconds=time.perf_counter() - started, exit_code=exit_code,
                worker=measured, checks=result_checks, log=str(log.relative_to(output.parent)),
                result=str((results / (name + '.json')).relative_to(output.parent))))
        report['source_sha256_after'] = source_hashes(root)
        report['sources_stable'] = before == report['source_sha256_after']
        report['passed'] = report['sources_stable'] and all(item['passed'] for item in report['cases'])
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps(report['cases'][-1], ensure_ascii=False), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
