"""Reproduce current-list state polling with a large, entirely synthetic queue.

Uses a temporary bank, one transaction for completed jobs and blocked sockets.
Checks query-plan/index use and exact scopes; timings are observations, not a
claim about Internet search throughput. No source documents or scores exist.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def production_hashes():
    """Identify the actual code/configuration measured, without Git or network."""
    paths = [path for base in (ROOT / 'peps', ROOT / 'config')
             for path in base.rglob('*') if path.is_file()
             and path.suffix in ('.py', '.js', '.html', '.css', '.json')
             and '__pycache__' not in path.parts]
    paths.append(ROOT / 'pyproject.toml')
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(paths)}


def benchmark(*, jobs=100001, current_completed=1000, samples=5):
    from peps.engine import Engine
    from peps.news import NewsAcquirer
    from peps.workflow import GuidedWorkflow

    if jobs < 2 or not 0 <= current_completed < jobs or samples < 1:
        raise ValueError('Require jobs >= 2, 0 <= current-completed < jobs, samples >= 1')
    started = time.perf_counter()
    report = {'kind': 'synthetic_current_bank_state_benchmark',
              'recorded_at': datetime.now(timezone.utc).isoformat(),
              'external_network': False, 'entities': 652, 'jobs': jobs,
              'production_sha256_before': production_hashes(), 'checks': {}}
    with tempfile.TemporaryDirectory(prefix='webscope-state-benchmark-') as directory, \
            patch('socket.socket.connect', side_effect=AssertionError('Network is forbidden')) as connect, \
            patch('socket.socket.connect_ex', side_effect=AssertionError('Network is forbidden')) as connect_ex:
        engine = Engine(Path(directory))
        NewsAcquirer(engine)  # Initialize the same discovery schema as the app.
        engine.store.import_entities({'id': f'synthetic-{index:04d}',
            'name': f'Persona Sintética de Prueba {index:04d}', 'kind': 'person'}
            for index in range(652))
        workflow = GuidedWorkflow(engine.store, lambda: [])
        with engine.store.connection() as con:
            con.execute('BEGIN IMMEDIATE')
            revision = con.execute("SELECT value FROM meta WHERE key='news_identity_revision'").fetchone()[0]
            queued = engine.store.enqueue('news_query',
                {'bank_revision': revision, 'source_id': 'synthetic'}, connection=con)
            template = dict(con.execute('SELECT * FROM jobs WHERE id=?', (queued,)).fetchone())
            columns = list(template)
            current_payload = json.dumps({'bank_revision': revision, 'source_id': 'synthetic',
                                          'padding': 'x' * 100}, sort_keys=True)
            historical_payload = json.dumps({'bank_revision': 'historical-synthetic-bank',
                                             'source_id': 'synthetic', 'padding': 'x' * 100}, sort_keys=True)

            def completed_rows():
                for index in range(jobs - 1):
                    row = dict(template, id=f'synthetic-completed-{index:08d}', status='done',
                               payload=current_payload if index < current_completed else historical_payload)
                    yield tuple(row[column] for column in columns)

            con.executemany('INSERT INTO jobs (' + ','.join(columns) + ') VALUES ('
                            + ','.join('?' for _ in columns) + ')', completed_rows())
            query = ("SELECT status,count(*) FROM jobs WHERE kind GLOB 'news_*' AND "
                     "CAST(json_extract(CASE WHEN json_valid(payload) THEN payload ELSE '{}' END,"
                     "'$.bank_revision') AS TEXT)=? GROUP BY status")
            plan = [row[3] for row in con.execute('EXPLAIN QUERY PLAN ' + query, (revision,))]
        report['query_plan'] = plan
        assert any('SEARCH jobs USING INDEX jobs_news_bank_workflow' in item for item in plan), plan
        report['checks']['scoped_expression_index_used'] = True

        durations = []
        for _ in range(samples + 1):
            before = time.perf_counter()
            snapshot = workflow.snapshot()
            durations.append(round((time.perf_counter() - before) * 1000, 3))
            current = snapshot['activity']['current_bank_tasks']
            historical = snapshot['activity']['historical_tasks']
            assert current['scope'] == 'current_bank' and str(current['bank_revision']) == str(revision), current
            assert current['done'] == current_completed and current['pending'] == current['queued'] == 1, current
            assert historical['scope'] == 'all_saved' and historical['done'] == jobs - 1, historical
            assert historical['pending'] == historical['queued'] == 1, historical
            assert snapshot['counts']['entities'] == 652, snapshot['counts']
            assert snapshot['activity']['material']['analyzable_documents'] == 0
            assert not snapshot['actions']['analyze_available']['enabled']
        assert not connect.called and not connect_ex.called
        with engine.store.connection() as con:
            assert con.execute('SELECT count(*) FROM jobs').fetchone()[0] == jobs
            assert con.execute('SELECT count(*) FROM scores').fetchone()[0] == 0
            assert con.execute('SELECT count(*) FROM documents').fetchone()[0] == 0
        report['checks'].update(exact_scope_counts=True, no_documents_or_scores_created=True,
                                zero_material_cannot_analyze=True, no_socket_connections=True)
        report['observed_counts'] = {'current_bank_completed': current['done'],
            'current_bank_pending': current['pending'], 'all_saved_completed': historical['done'],
            'all_saved_pending': historical['pending']}
        report['first_snapshot_ms'] = durations[0]
        report['warm_snapshot_ms'] = durations[1:]
        report['warm_median_ms'] = round(statistics.median(durations[1:]), 3)
    report['production_sha256_after'] = production_hashes()
    report['checks']['production_unchanged_during_benchmark'] = (
        report['production_sha256_before'] == report['production_sha256_after'])
    report['elapsed_seconds'] = round(time.perf_counter() - started, 3)
    report['passed'] = all(report['checks'].values())
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--jobs', type=int, default=100001)
    parser.add_argument('--current-completed', type=int, default=1000)
    parser.add_argument('--samples', type=int, default=5)
    parser.add_argument('--max-warm-ms', type=float, default=None,
                        help='Optional coarse regression budget for the warm median; no Internet SLA.')
    parser.add_argument('--output', type=Path,
                        default=ROOT / 'runtime/search-clarity/search-state-benchmark.json')
    args = parser.parse_args()
    report = benchmark(jobs=args.jobs, current_completed=args.current_completed, samples=args.samples)
    if args.max_warm_ms is not None:
        report['warm_budget_ms'] = args.max_warm_ms
        report['checks']['optional_warm_budget'] = report['warm_median_ms'] <= args.max_warm_ms
        report['passed'] = all(report['checks'].values())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({key: report[key] for key in ('passed', 'entities', 'jobs', 'first_snapshot_ms',
          'warm_median_ms', 'elapsed_seconds')} | {'output': str(args.output)}, ensure_ascii=False))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
