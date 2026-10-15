"""Ensayo reproducible local. No confundirlo con una medición de cobertura web."""
import argparse
import concurrent.futures
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from peps.engine import Engine, EntityIndex, capacity
from peps.store import Store, now
from peps import nlp, scoring
import hashlib


def drain(path):
    store = Store(path)
    count = 0
    while True:
        job = store.claim()
        if not job:
            break
        store.finish(job)
        count += 1
    print(count)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--size', type=int, default=40000)
    parser.add_argument('--drain', action='store_true')
    args = parser.parse_args()
    if args.drain:
        drain(args.data_dir)
        return
    if (args.data_dir / 'peps.sqlite3').exists():
        raise ValueError('Use una carpeta nueva para evitar resultados acumulados')
    engine = Engine(args.data_dir)
    timings = {}
    module_hashes = {name: hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
                     for name, module in [('nlp', nlp), ('scoring', scoring)]}
    def stage(name, fn):
        start = time.perf_counter()
        result = fn()
        timings[name] = round(time.perf_counter() - start, 6)
        print(json.dumps({'stage': name, 'seconds': timings[name], 'result': result}), flush=True)
        return result
    entities = [{'id': f'bench-{i:06}', 'name': f'Persona Sintética Mexicana {i:06}', 'kind': 'person',
                 'context': {'fixture': 'synthetic_not_real'}} for i in range(args.size)]
    stage('import_entities', lambda: engine.store.import_entities(entities))
    index = EntityIndex(entities)
    def matches():
        found = 0
        for i, e in enumerate(entities):
            ids = index.match(e['name'] + ' presentó su informe de actividades.')
            if ids != [e['id']]:
                raise AssertionError((i, ids))
            found += 1
        return found
    stage('indexed_linkage', matches)
    def docs():
        for i, e in enumerate(entities):
            adverse = i % 10 == 0
            yield {'url': f'https://example.org/synthetic/{i}', 'title': 'Fixture sintético de carga',
                   'text': e['name'] + (' fue condenado por corrupción.' if adverse else ' presentó su informe de actividades.'),
                   'published_at': now()[:10], 'source_kind': 'official',
                   'metadata': {'synthetic': True}, 'entity_ids': [e['id']]}
    stage('persist_analyze_documents', lambda: engine.import_documents(docs()))
    stage('score_entities', engine.rescore)
    failures, positives, validated = [], 0, 0
    with engine.store.connection() as con:
        for row in con.execute('SELECT entity_id,result FROM scores'):
            value = json.loads(row['result'])
            expected = int(row['entity_id'].split('-')[1]) % 10 == 0
            positives += value['score'] > 0
            validated += value['validated_score'] > 0
            if (value['score'] > 0) != expected:
                failures.append(row['entity_id'])
    # Un mismo conjunto de tareas, cuatro procesos independientes y una base local.
    for i in range(1000):
        engine.store.enqueue('benchmark', {'n': i})
    def process(_):
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--data-dir', str(engine.store.path), '--drain'],
                                capture_output=True, text=True, check=True)
        return int(result.stdout.strip())
    def processes():
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            return list(pool.map(process, range(4)))
    counts = stage('four_process_queue_1000', processes)
    stats = engine.store.stats()
    audit = stage('verify_audit', engine.store.verify_audit)
    report = {'generated_at': now(), 'scope': 'synthetic_local_end_to_end_not_web_coverage',
              'machine': {'platform': platform.platform(), 'python': sys.version, 'cpu_count': os.cpu_count()},
              'entities': args.size, 'timings_seconds': timings,
              'module_sha256': module_hashes, 'model_version': scoring.MODEL_VERSION,
              'parameters_hash': scoring.PARAMETERS_HASH,
              'database_bytes': sum(p.stat().st_size for p in args.data_dir.glob('*.sqlite3*')),
              'throughput_documents_per_second': round(args.size / timings['persist_analyze_documents'], 2),
              'throughput_scores_per_second': round(args.size / timings['score_entities'], 2),
              'queue_process_counts': counts, 'queue_unique_done': stats['jobs'].get('done'),
              'scoring_fixture': {'expected_positives': (args.size + 9) // 10, 'observed_positives': positives,
                                  'mismatches': failures[:100], 'validated_without_review': validated},
              'stats': stats, 'audit': audit, 'capacity_model': capacity(args.size),
              'pass': not failures and validated == 0 and sum(counts) == 1000 and audit['ok']}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    if not report['pass']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
