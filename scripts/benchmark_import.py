"""Measure guided CSV preview/commit locally with invented data and fresh processes.

Run from the repository: python scripts/benchmark_import.py
Uses only Python's standard library. Does not start the app, score entities,
load NLP models, download resources, contact a network or render the frontend.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import platform
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def measured(call):
    wall, cpu = time.perf_counter(), time.process_time()
    result = call()
    return result, {'wall_ms': round((time.perf_counter() - wall) * 1000, 3),
                    'cpu_ms': round((time.process_time() - cpu) * 1000, 3)}


def peak_rss_mb():
    """Lifetime process high-water mark, not a per-stage allocation measurement."""
    if not (sys.platform.startswith('linux') or sys.platform == 'darwin'):
        return None
    try:
        import resource
        maximum = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # Darwin reports bytes; Linux reports KiB.
        return round(maximum / (1024 * 1024 if sys.platform == 'darwin' else 1024), 3)
    except (ImportError, OSError):
        return None


def fixture(size):
    """Semicolon CSV: duplicate headers, multiline quotes and ragged extra cells."""
    stream = io.StringIO(newline='')
    writer = csv.writer(stream, delimiter=';', lineterminator='\n')
    writer.writerow(['Código', 'Nombre completo', 'Clasificación', 'Alias',
                     'Residencia', 'Nota', 'Nota'])
    for index in range(size):
        row = ['benchmark-%06d' % index, 'Entidad sintética %06d' % index,
               ('persona', 'empresa', 'organización')[index % 3],
               'Alias %06d|Identificador %06d' % (index, index),
               ('León', 'Hermosillo', 'CDMX')[index % 3],
               'Nota sintética de prueba; campo entrecomillado.\nSegunda línea.',
               'Dato sintético ignorado']
        if index % 10 == 0:
            row.append('Celda adicional visible')
        writer.writerow(row)
    return {'mode': 'csv', 'text': stream.getvalue(), 'delimiter': ';',
            'has_header': True, 'mapping': ['id', 'name', 'kind', 'aliases',
                                          'city', 'context_note', 'ignore', 'ignore'],
            'skip_invalid': False, 'existing_policy': 'skip'}


def storage(path):
    sizes = {label: (Path(str(path) + suffix).stat().st_size
                     if Path(str(path) + suffix).exists() else 0)
             for label, suffix in (('sqlite_bytes', ''), ('wal_bytes', '-wal'), ('shm_bytes', '-shm'))}
    return dict(sizes, total_bytes=sum(sizes.values()))


def run_worker(size):
    # Imports and fixture generation are intentionally outside measured stages.
    from peps.importing import commit_import, preview_import
    from peps.store import Store
    payload = fixture(size)
    with tempfile.TemporaryDirectory(prefix='peps-import-benchmark-') as directory:
        path = Path(directory) / 'bank.sqlite'
        store = Store(path)
        before = storage(path)
        preview, preview_timing = measured(lambda: preview_import(store, payload))
        if not preview['ready'] or preview['counts']['new'] != size or preview['counts']['invalid']:
            raise AssertionError('Synthetic fixture did not validate completely')
        commit_payload = dict(payload, preview_hash=preview['preview_hash'])
        receipt, commit_timing = measured(lambda: commit_import(store, commit_payload))
        after = storage(path)
        # Counts/audit validation is outside the timed preview and commit calls.
        if receipt['imported'] != size or len(receipt['accepted_ids']) != size:
            raise AssertionError('Unexpected committed entity count')
        if store.stats()['entities'] != size or not store.verify_audit()['ok']:
            raise AssertionError('Committed entities or audit failed integrity checks')
        return {'rows': size, 'csv_utf8_bytes': len(payload['text'].encode('utf-8')),
                'preview': preview_timing, 'commit': commit_timing,
                'peak_process_rss_mb': peak_rss_mb(),
                'storage_before': before, 'storage_after': after,
                'storage_growth_bytes': after['total_bytes'] - before['total_bytes'],
                'validation': {'preview_ready': preview['ready'],
                               'accepted_rows': receipt['imported'], 'invalid_rows': preview['counts']['invalid'],
                               'audit_verified': True}}


def describe(samples):
    present = [sample for sample in samples if sample is not None]
    if not present:
        return None
    return {'n': len(present), 'median': round(statistics.median(present), 3),
            'min': min(present), 'max': max(present)}


def summarize(samples):
    return {'repetitions': len(samples),
            'preview': {key: describe([sample['preview'][key] for sample in samples])
                        for key in ('wall_ms', 'cpu_ms')},
            'commit': {key: describe([sample['commit'][key] for sample in samples])
                       for key in ('wall_ms', 'cpu_ms')},
            'peak_process_rss_mb': describe([sample['peak_process_rss_mb'] for sample in samples]),
            'storage_after_total_bytes': describe([sample['storage_after']['total_bytes'] for sample in samples]),
            'storage_growth_bytes': describe([sample['storage_growth_bytes'] for sample in samples])}


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sizes', nargs='+', type=int, default=[1000, 10000])
    parser.add_argument('--repetitions', type=int, default=3)
    parser.add_argument('--output', type=Path, default=ROOT / 'docs/benchmarks/import_performance.json')
    parser.add_argument('--worker', type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker is not None:
        if not 1 <= args.worker <= 10000:
            parser.error('worker size must be between 1 and 10000')
        print(json.dumps(run_worker(args.worker), ensure_ascii=False))
        return
    if not 1 <= args.repetitions <= 20 or any(not 1 <= size <= 10000 for size in args.sizes):
        parser.error('sizes: 1..10000; repetitions: 1..20')
    report = {
        'benchmark': 'guided_csv_import', 'schema_version': 1,
        'created_at_utc': datetime.now(timezone.utc).isoformat(),
        'environment': {'python': platform.python_version(), 'implementation': platform.python_implementation(),
                        'sqlite': sqlite3.sqlite_version, 'platform': platform.platform(),
                        'machine': platform.machine(), 'processor': platform.processor() or None,
                        'logical_cpu_count': os.cpu_count(), 'dependencies': 'Python standard library only'},
        'configuration': {'sizes': args.sizes, 'repetitions_per_size': args.repetitions,
                          'fresh_process_per_repetition': True, 'fresh_temporary_store_per_repetition': True,
                          'initial_bank_entities': 0, 'delimiter': ';', 'existing_policy': 'skip',
                          'skip_invalid': False, 'scoring_enabled': False},
        'fixture': {'data': 'Entirely synthetic; no personal datasets or external sources.',
                    'columns': 8, 'header_columns': 7, 'duplicate_header': 'Nota',
                    'mapped_fields': ['id', 'name', 'kind', 'aliases', 'city', 'context_note', 'ignore', 'ignore'],
                    'kind_values': ['persona', 'empresa', 'organización'],
                    'aliases_per_entity': 2, 'multiline_quoted_note_per_row': True,
                    'ragged_rows': 'Every tenth data row has an eighth column; other rows have seven.',
                    'ignored_columns': 2, 'expected_invalid_rows': 0,
                    'generator': 'fixture(size) in scripts/benchmark_import.py'},
        'measurement': {'timed_preview': 'preview_import: parse, validate, normalize, compare against empty bank, hash and sample.',
                        'timed_commit': 'commit_import: BEGIN IMMEDIATE, complete revalidation, insert, audit and durable SQLite commit.',
                        'excluded': ['Fixture generation', 'Store/schema initialization', 'Post-run integrity checks',
                                     'Downloads and network', 'Models and NLP', 'Entity scoring', 'HTTP API and browser rendering'],
                        'cpu': 'time.process_time per stage, in milliseconds; no utilization percentage inferred.',
                        'wall': 'time.perf_counter per stage, in milliseconds.',
                        'rss': 'resource.RUSAGE_SELF.ru_maxrss lifetime process high-water mark; Linux KiB/macOS bytes converted to MiB. Includes imports, fixture, preview, commit and validation; not incremental per-stage RAM. Windows/unsupported platforms: null.',
                        'storage': 'On-disk SQLite main file plus present WAL/SHM after stage connections close, in bytes.',
                        'summary': 'Actual raw samples plus median/min/max. Three repetitions do not establish tail percentiles.',
                        'limits': 'Synthetic empty-bank import only; timings depend on hardware/filesystem/cache and do not predict scoring or populated-bank performance.'},
        'source_sha256': {str(path.relative_to(ROOT)): file_hash(path) for path in
                          (Path(__file__).resolve(), ROOT / 'peps/importing.py', ROOT / 'peps/store.py')},
        'cases': []}
    for size in args.sizes:
        samples = []
        for repetition in range(1, args.repetitions + 1):
            completed = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--worker', str(size)],
                                       cwd=ROOT, check=True, text=True, capture_output=True)
            sample = json.loads(completed.stdout)
            sample['repetition'] = repetition
            samples.append(sample)
        report['cases'].append({'rows': size, 'samples': samples, 'summary': summarize(samples)})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'report': str(args.output),
                      'cases': [{'rows': case['rows'], 'summary': case['summary']} for case in report['cases']]},
                     ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
