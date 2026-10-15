"""Reproduce offline result-read costs without latency assertions or real data.

Compares separate validated result/history reads with the bundled snapshot on
the same synthetic bank. Each operation still verifies its own audit snapshot.
"""
from __future__ import annotations

import argparse
import cProfile
import json
from pathlib import Path
import platform
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from peps.engine import Engine
from scripts.benchmark_explanation_pipeline import fixture_documents, fixture_entities, network_guard


def measured(call):
    wall, cpu = time.perf_counter(), time.process_time()
    value = call()
    return value, {'wall_seconds': time.perf_counter() - wall, 'cpu_seconds': time.process_time() - cpu}


def benchmark(entities=20, notes=50, repeats=3, profile=None):
    with tempfile.TemporaryDirectory() as directory, network_guard() as network:
        engine = Engine(directory)
        rows = fixture_entities(entities, heavy_notes=0)
        engine.store.import_entities(rows)
        _, ingestion = measured(lambda: engine.import_documents(fixture_documents(rows, notes, 0)))
        _, scoring = measured(engine.rescore)
        original_chain, original_decode = engine.store._assert_audit_chain, engine.store._decode_score_run
        timings = {}
        profiler = cProfile.Profile() if profile else None
        for name, operation in (
                ('separate_result_and_history', lambda entity: (engine.store.latest_score_run(entity['id']), engine.store.score_history(entity['id']))),
                ('bundled_result_and_history', lambda entity: engine.store.score_run_bundle(entity['id'])),
                ('actual_engine_explanation', lambda entity: engine.explanation(entity['id']))):
            with patch.object(engine.store, '_assert_audit_chain', wraps=original_chain) as chain, \
                 patch.object(engine.store, '_decode_score_run', wraps=original_decode) as decode:
                if profiler and name == 'actual_engine_explanation':
                    profiler.enable()
                _, timing = measured(lambda: [operation(entity) for _ in range(repeats) for entity in rows])
                if profiler and name == 'actual_engine_explanation':
                    profiler.disable()
                timings[name] = {**timing, 'reads': entities * repeats,
                                 'full_chain_verifications': chain.call_count, 'run_decodes': decode.call_count}
        listings = {}
        for compact in (False, True):
            values, timing = measured(lambda: engine.store.list_entities(limit=500, summary=compact))
            listings['summary' if compact else 'full'] = {**timing, 'response_bytes': len(json.dumps(values, ensure_ascii=False).encode())}
        if profiler:
            Path(profile).parent.mkdir(parents=True, exist_ok=True)
            profiler.dump_stats(str(profile))
        audit = engine.store.verify_audit()
        assert audit['ok'] and not network
        return {'python': platform.python_version(), 'platform': platform.platform(),
                'fixture': {'entities': entities, 'documents': entities * notes, 'notes_per_entity': notes, 'repeats': repeats},
                'ingestion': ingestion, 'scoring': scoring, 'reads': timings, 'listings': listings,
                'integrity_verified': True, 'network_operations': dict(network),
                'limitations': ['Synthetic local source documents; no web collection or browser rendering.',
                                'Shared host observations, not a latency SLA; filesystem and antivirus affect wall time.',
                                'Separate-read comparison uses the current implementation with two independent snapshots.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--entities', type=int, default=20)
    parser.add_argument('--notes', type=int, default=50)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--profile', type=Path)
    args = parser.parse_args()
    if min(args.entities, args.notes, args.repeats) < 1:
        parser.error('entities, notes and repeats must be positive')
    result = benchmark(args.entities, args.notes, args.repeats, args.profile)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
