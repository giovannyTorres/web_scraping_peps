"""Stress HTTP and parser boundaries locally; real DNS and TCP are forbidden.

Run: python scripts/stress_transport.py --output runtime/transport-stress.json

Fresh temporary SQLite, actual production collector/parser code, injected HTTP
responses and real elapsed time. No external fixture or baseline is required.
Checks cover bounded expansion, parsing, concurrent cache reuse and audit rows;
elapsed time is descriptive and never determines success. An optional --baseline
may point to an earlier report or a list of per-case measurements.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import platform
import socket
import sys
import tempfile
import time
from unittest.mock import Mock, patch
import zlib


ROOT = Path(__file__).resolve().parents[1]
SOURCE_FILES = ('peps/collector.py', 'peps/public_search.py',
                'peps/social_discovery.py', 'peps/robots_policy.py')
SOURCE = dict(enabled=True, terms_approved=True, robots_required=False,
              allowed_hosts=['news.example'], min_interval_seconds=0, max_bytes=32768)
URL = 'https://news.example/article'
PUBLIC_DNS = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('93.184.216.34', 443))]


@contextmanager
def no_network():
    counts = Counter()

    def forbidden(*_args, **_kwargs):
        counts['blocked_operations'] += 1
        raise AssertionError('Real DNS and TCP are forbidden in this transport stress test')

    with patch.object(socket, 'getaddrinfo', forbidden), \
         patch.object(socket, 'create_connection', forbidden), \
         patch.object(socket.socket, 'connect', forbidden), \
         patch.object(socket.socket, 'connect_ex', forbidden):
        yield counts


def hashes(source_root):
    return {name: hashlib.sha256((source_root / name).read_bytes()).hexdigest()
            for name in SOURCE_FILES}


def baseline_measurements(path):
    raw = path.read_bytes()
    payload = json.loads(raw)
    rows = payload if isinstance(payload, list) else payload.get('cases', [])
    return {'file': path.name, 'sha256': hashlib.sha256(raw).hexdigest(),
            'cases': [{'case': row['case'], 'seconds': row['seconds']}
                      for row in rows if isinstance(row, dict) and isinstance(row.get('case'), str)
                      and isinstance(row.get('seconds'), (float, int)) and row['seconds'] >= 0]}


def stress(args):
    sys.path.insert(0, str(args.source_root.resolve()))
    from peps.collector import SafeFetcher
    from peps.public_search import parse_public_results

    before = hashes(args.source_root)
    report = {'schema_version': 1, 'generated_at': datetime.now(timezone.utc).isoformat(),
              'synthetic': True, 'external_network_requests': 0,
              'method': 'Temporary SQLite; production HTTP/parser/cache paths; injected HTTP; real DNS/TCP forbidden.',
              'limits': ['Elapsed times describe local processing, not internet throughput.',
                         'No latency threshold determines success.'],
              'python': platform.python_version(), 'platform': platform.platform(),
              'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'source_sha256': before, 'cases': [], 'checks': {}}

    def run_case(name, operation):
        started = time.perf_counter()
        try:
            values, checks = operation()
            row = dict(case=name, seconds=time.perf_counter() - started, **values)
            row['passed'] = all(checks.values())
            report['checks'].update({name + '.' + key: value for key, value in checks.items()})
        except Exception as exc:
            row = dict(case=name, seconds=time.perf_counter() - started, passed=False,
                       error_type=type(exc).__name__, error=str(exc)[:500])
            report['checks'][name + '.completed'] = False
        report['cases'].append(row)

    with no_network() as guard:
        for size in (1000, 5000, 10000, 150000):
            def malformed_html(size=size):
                body = ('<div>' * size + '</bogus>' * size +
                        '<a class="result__a" href="https://editor.example/story">Visible title</a>')
                rows = parse_public_results({'status': 'ok', 'body': body}, {'kind': 'duckduckgo_html'})
                return {'bytes': len(body), 'opened_tags': size, 'unmatched_closing_tags': size}, {
                    'visible_result_preserved': len(rows) == 1 and rows[0]['title'] == 'Visible title',
                    'result_is_discovery_only': bool(rows) and rows[0]['metadata']['discovery_only'] is True}

            run_case('html_unmatched_' + str(size), malformed_html)

        def compressed_expansion():
            compressor = zlib.compressobj(wbits=31)
            wire = b''.join(compressor.compress(b'x' * 1_000_000) for _ in range(20)) + compressor.flush()
            with tempfile.TemporaryDirectory() as temporary, \
                    SafeFetcher({'trust_env_proxy': False}, Path(temporary)) as fetcher:
                fetcher._dns = Mock(return_value=PUBLIC_DNS)
                response, connection = Mock(), Mock()
                response.status, response.will_close = 200, False
                response.getheaders.return_value = [('Content-Type', 'text/plain'), ('Content-Encoding', 'gzip'),
                                                     ('Content-Length', str(len(wire)))]
                response.read1.side_effect = io.BytesIO(wire).read
                connection.getresponse.return_value = response
                connection._peer_address = '93.184.216.34'
                with patch('peps.collector._PinnedHTTPSConnection', return_value=connection):
                    result = fetcher.fetch(URL, SOURCE)
                with fetcher._db() as db:
                    cache_rows = db.execute('SELECT COUNT(*) FROM http_cache').fetchone()[0]
                return {'wire_bytes': len(wire), 'unbounded_decoded_bytes': 20_000_000,
                        'decoded_bytes_allocated': result['decoded_bytes'], 'cache_rows': cache_rows}, {
                    'expansion_rejected': result['status'] == 'error',
                    'decoded_output_bounded': result['decoded_bytes'] == SOURCE['max_bytes'] + 1,
                    'socket_closed': connection.close.call_count == 1,
                    'no_body_returned': result['body'] == '', 'no_cache_entry': cache_rows == 0}

        run_case('gzip_20_megabyte_expansion', compressed_expansion)

        def concurrent_cache_reuse():
            with tempfile.TemporaryDirectory() as temporary, \
                    SafeFetcher({'trust_env_proxy': False}, Path(temporary)) as fetcher:
                fetcher._dns = Mock(return_value=PUBLIC_DNS)
                fetcher._transport = Mock(return_value=(200, {'content-type': 'text/plain'}, b'Cached body'))
                source = dict(SOURCE, cache_ttl_seconds=60)
                initial = fetcher.fetch(URL, source)
                fetcher._dns.reset_mock()
                fetcher._dns.side_effect = AssertionError('Even fixture DNS forbidden for cached reuse')
                started = time.perf_counter()
                with ThreadPoolExecutor(max_workers=64) as pool:
                    rows = list(pool.map(lambda _: fetcher.fetch(URL, source), range(2048)))
                reuse_seconds = time.perf_counter() - started
                with fetcher._db() as db:
                    cache_rows = db.execute('SELECT COUNT(*) FROM http_cache').fetchone()[0]
                    audit_rows = db.execute('SELECT COUNT(*) FROM request_history').fetchone()[0]
                return {'requests': 2048, 'workers': 64, 'reuse_seconds': reuse_seconds,
                        'extra_dns': fetcher._dns.call_count, 'extra_http': max(0, fetcher._transport.call_count - 1),
                        'cache_rows': cache_rows, 'audit_rows': audit_rows}, {
                    'initial_fetch_succeeded': initial['status'] == 'ok',
                    'all_reused': all(row['status'] == 'not_modified' and row['network_requests'] == 0 for row in rows),
                    'no_extra_http': fetcher._transport.call_count == 1,
                    'no_dns_for_reuse': fetcher._dns.call_count == 0,
                    'one_cached_copy': cache_rows == 1, 'all_accesses_audited': audit_rows == 2049}

        run_case('concurrent_cache_reuse', concurrent_cache_reuse)
        report['network_guard'] = {'blocked_operations': guard['blocked_operations']}
        report['checks']['no_real_network_attempts'] = guard['blocked_operations'] == 0

    report['checks']['source_files_unchanged'] = hashes(args.source_root) == before
    if args.baseline:
        report['baseline'] = baseline_measurements(args.baseline)
        earlier = {row['case']: row['seconds'] for row in report['baseline']['cases']}
        report['comparisons'] = [dict(case=row['case'], baseline_seconds=earlier[row['case']],
                                      current_seconds=row['seconds'],
                                      speedup=earlier[row['case']] / row['seconds'] if row['seconds'] else None)
                                 for row in report['cases'] if row['case'] in earlier]
    report['passed'] = bool(report['checks']) and all(report['checks'].values())
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', type=Path, default=ROOT)
    parser.add_argument('--output', type=Path, help='Optional JSON report path; otherwise prints JSON to stdout.')
    parser.add_argument('--baseline', type=Path, help='Optional prior JSON report for descriptive time comparisons.')
    args = parser.parse_args()
    report = stress(args)
    encoded = json.dumps(report, indent=2, ensure_ascii=False) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding='utf-8')
    print(encoded, end='')
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
