"""Measure the real offline explanation/storage/API pipeline in fresh processes.

All entities and notes are invented. No model training or external requests occur.
HTTP measurements use only the application's authenticated loopback API; browser
rendering, real-news accuracy, and network collection are outside this benchmark.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import http.client
import importlib
import json
import math
import os
from pathlib import Path
import platform
import socket
import subprocess
import sys
import threading
import time
from unittest.mock import patch
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def summary(samples):
    values = sorted(samples)
    return {'n': len(values), 'mean_ms': sum(values)/len(values) if values else None,
            'p50_ms': values[max(0, math.ceil(.5*len(values))-1)] if values else None,
            'p95_ms': values[max(0, math.ceil(.95*len(values))-1)] if values else None,
            'max_ms': max(values) if values else None, 'total_ms': sum(values)}


def measure(call):
    wall, cpu = time.perf_counter(), time.process_time()
    value = call()
    return value, (time.perf_counter()-wall)*1000, (time.process_time()-cpu)*1000


def peak_rss():
    # Linux ru_maxrss can retain the parent's high-water mark across fork/exec.
    # VmHWM belongs to this process address space and avoids that inheritance.
    status = Path('/proc/self/status')
    if sys.platform.startswith('linux') and status.exists():
        values = {}
        for line in status.read_text().splitlines():
            if line.startswith(('VmHWM:', 'VmRSS:')):
                key, rest = line.split(':', 1)
                values[key] = int(rest.split()[0])*1024
        if 'VmHWM' in values:
            return {'bytes': values['VmHWM'], 'current_rss_bytes': values.get('VmRSS'),
                    'method': 'proc_self_status_VmHWM_current_address_space'}
    try:
        import resource
        value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return {'bytes': int(value if sys.platform == 'darwin' else value*1024),
                'method': 'resource.ru_maxrss_may_include_parent_preexec_high_water_mark'}
    except ImportError:
        try:
            import psutil
            value = psutil.Process().memory_info()
            return {'bytes': int(getattr(value, 'peak_wset', value.rss)),
                    'method': 'psutil.peak_wset' if hasattr(value, 'peak_wset') else 'sampled_current_rss'}
        except ImportError:
            return {'bytes': None, 'method': 'unavailable'}


@contextmanager
def network_guard(allow_loopback=False):
    """Count Python socket operations; block DNS and connections off loopback."""
    counts = Counter()
    original = {name: getattr(socket, name) for name in ('getaddrinfo', 'create_connection')}
    original.update(connect=socket.socket.connect, connect_ex=socket.socket.connect_ex)

    def allowed(address):
        return allow_loopback and isinstance(address, tuple) and address[0] in {'127.0.0.1', '::1'}

    def check(address, operation):
        if not allowed(address):
            counts['blocked_operations'] += 1
            raise AssertionError('External networking forbidden during local benchmark')
        counts['allowed_loopback_'+operation] += 1

    def connect(sock, address):
        check(address, 'connect')
        return original['connect'](sock, address)

    def connect_ex(sock, address):
        check(address, 'connect_ex')
        return original['connect_ex'](sock, address)

    def getaddrinfo(host, port, *args, **kwargs):
        check((host, port), 'getaddrinfo')
        return original['getaddrinfo'](host, port, *args, **kwargs)

    def create_connection(address, *args, **kwargs):
        check(address, 'create_connection')
        return original['create_connection'](address, *args, **kwargs)

    with patch.object(socket.socket, 'connect', connect), patch.object(socket.socket, 'connect_ex', connect_ex), \
         patch.object(socket, 'getaddrinfo', getaddrinfo), patch.object(socket, 'create_connection', create_connection):
        yield counts


def fixture_entities(count=20, heavy_notes=205):
    rows = [{'id': f'pipeline-{i:03}', 'name': f'Persona Sintética Alba {i:03}',
             'kind': 'person', 'aliases': [], 'context': {'fixture': 'synthetic_not_real'}}
            for i in range(count)]
    if heavy_notes:
        rows.append({'id': 'pipeline-heavy', 'name': 'Organización Sintética Lumbre',
                     'kind': 'organization', 'aliases': [], 'context': {'fixture': 'synthetic_not_real'}})
    return rows


def fixture_documents(entities, notes_per_entity=4, heavy_notes=205):
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    endings = (' fue acusada de fraude.', ' fue detenida por corrupción.',
               ' sufrió extorsión por parte de un tercero.', ' presentó su informe anual de actividades.')
    for entity in entities:
        heavy = entity['id'] == 'pipeline-heavy'
        for note in range(heavy_notes if heavy else notes_per_entity):
            event = (' fue condenada por fraude.' if note % 11 == 0 else
                     ' presentó su informe de actividades.') if heavy else endings[note % len(endings)]
            text = entity['name']+event+f' Registro sintético de prueba {note:04}; sin datos reales.'
            yield {'url': f"https://fixture.invalid/pipeline/{entity['id']}/{note:04}",
                   'title': 'Nota sintética de rendimiento', 'text': text,
                   'source_kind': 'official', 'published_at': today.date().isoformat(),
                   'retrieved_at': (today+timedelta(seconds=note)).isoformat(),
                   'event_id': f"{entity['id']}-event-{note:04}",
                   'entity_ids': [entity['id']], 'metadata': {'synthetic': True, 'note_index': note}}


def assert_faithful_counterfactuals(engine, value):
    """Remove source documents and rescore, rather than reproducing aggregation."""
    from peps.scoring import score_entity
    evidence = engine.store.evidence(value['entity']['id'])
    lookup = {row['id']: row for row in evidence}
    literal = deltas = 0
    for factor in value['explanation']['factors']:
        identifiers = {cite['evidence_id'] for cite in factor['evidence']}
        for cite in factor['evidence']:
            assert lookup[cite['evidence_id']]['text'][cite['start']:cite['end']] == cite['quote']
            literal += 1
        alternative = score_entity(value['entity'], [row for row in evidence if row['id'] not in identifiers],
                                   as_of=value['result']['as_of'])
        assert alternative['score'] == factor['score_without_event']
        assert value['result']['score']-alternative['score'] == factor['score_delta_when_removed']
        deltas += 1
    return {'literal_scoring_citations_checked': literal, 'source_document_removal_counterfactuals_checked': deltas}


def code_hashes():
    result = {}
    for name in ('engine','store','scoring','nlp','identity','contextual','context_model','explainability','app'):
        module = importlib.import_module('peps.'+name)
        result['peps/'+name+'.py'] = hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
    result['scripts/benchmark_explanation_pipeline.py'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    for file in ('app.js','style.css','index.html'):
        result['peps/web/'+file] = hashlib.sha256((ROOT/'peps/web'/file).read_bytes()).hexdigest()
    return result


def lookup_worker(args):
    with network_guard() as guard:
        start = time.perf_counter()
        from peps.engine import Engine
        engine = Engine(args.data_dir)
        operation = {
            'current_explanation': lambda: engine.explanation(args.entity),
            'snapshot_lookup': lambda: engine.store.get_score_run(args.run_id, args.entity),
            'replay': lambda: engine.replay_score(args.run_id),
            'historical_target_note': lambda: engine.store.run_evidence(args.run_id, args.evidence_id),
        }[args.lookup_operation]
        value, wall, cpu = measure(operation)
        assert value
        if args.lookup_operation == 'replay':
            assert value['matched']
        return {'pid': os.getpid(), 'warm_process_preexisting': False,
                'operation': args.lookup_operation,
                'startup_import_engine_plus_first_operation_ms': (time.perf_counter()-start)*1000,
                'first_operation_wall_ms': wall, 'first_operation_cpu_ms': cpu,
                'replay_matched': value.get('matched') if args.lookup_operation == 'replay' else None,
                'peak_rss': peak_rss(),
                'network_guard': dict(guard)}


def storage_report(engine):
    with engine.store.connection() as con:
        counts = {table: con.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                  for table in ('entities','documents','evidence','scores','score_runs','audit')}
        source_body_bytes = con.execute('SELECT COALESCE(SUM(length(CAST(text AS BLOB))),0) FROM documents').fetchone()[0]
        run_payload_bytes = con.execute('SELECT COALESCE(SUM(length(CAST(payload AS BLOB))),0) FROM score_runs').fetchone()[0]
        current_payload_bytes = con.execute('SELECT COALESCE(SUM(length(CAST(result AS BLOB))),0) FROM scores').fetchone()[0]
        manifests = [json.loads(row[0])['evidence_manifest'] for row in con.execute('SELECT payload FROM score_runs')]
        top_level_body_copies = sum(any(key in entry for key in ('text','body','original_text'))
                                    for manifest in manifests for entry in manifest)
        try:
            by_table = dict(con.execute('SELECT name,SUM(pgsize) FROM dbstat GROUP BY name'))
        except Exception as exc:
            by_table = {'unavailable': type(exc).__name__}
        con.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    files = {path.name: path.stat().st_size for path in engine.data_dir.glob('*.sqlite3*')}
    return {'counts': counts, 'sqlite_files': files, 'sqlite_total_bytes': sum(files.values()),
            'sqlite_page_bytes_by_object': by_table, 'original_body_utf8_bytes': source_body_bytes,
            'historical_payload_utf8_bytes': run_payload_bytes, 'current_result_utf8_bytes': current_payload_bytes,
            'manifest_top_level_body_copies': top_level_body_copies,
            'body_storage': 'Original bodies reside in documents.text; manifests reference document_id and content hashes.',
            'redundancy_limit': 'Literal citations and structured results remain in traces/current cache/history. This is not zero total textual redundancy.'}


def pipeline_worker(args):
    if (Path(args.data_dir)/'peps.sqlite3').exists():
        raise ValueError('Use a new --data-dir to avoid accumulated benchmark data')
    wall_start, cpu_start = time.perf_counter(), time.process_time()
    from peps.engine import Engine
    from peps.app import create_server
    entities = fixture_entities(args.entities, args.heavy_notes)
    small = [row for row in entities if row['id'] != 'pipeline-heavy']
    checks, timings = {}, {}
    with network_guard(allow_loopback=True) as guard:
        engine = Engine(args.data_dir)
        _, ms, cpu = measure(lambda: engine.store.import_entities(entities))
        timings['import_entities'] = {'wall_ms': ms, 'cpu_ms': cpu}
        imported, ms, cpu = measure(lambda: engine.import_documents(fixture_documents(entities, args.notes, args.heavy_notes)))
        timings['import_documents'] = {'wall_ms': ms, 'cpu_ms': cpu, 'result': imported}
        # First actual UI backend call calculates and persists its first snapshot.
        first, ms, cpu = measure(lambda: engine.explanation(small[0]['id']))
        timings['cold_first_entity_compute'] = {'wall_ms': ms, 'cpu_ms': cpu}
        _, ms, cpu = measure(lambda: engine.rescore([row['id'] for row in small]))
        timings['small_batch_rescore'] = {'wall_ms': ms, 'cpu_ms': cpu, 'entities': len(small)}
        if args.heavy_notes:
            heavy, ms, cpu = measure(lambda: engine.explanation('pipeline-heavy'))
            timings['heavy_entity_compute'] = {'wall_ms': ms, 'cpu_ms': cpu, 'notes': args.heavy_notes}
        else:
            heavy = first
        runs = {row['id']: engine.explanation(row['id']) for row in entities}
        checks['faithfulness'] = assert_faithful_counterfactuals(engine, first)
        warm, warm_cpu, snapshots, replay_times = [], [], [], []
        heavy_current, historical = [], []
        for _ in range(args.repeats):
            for entity in small:
                value, ms, cpu = measure(lambda e=entity: engine.explanation(e['id']))
                warm.append(ms); warm_cpu.append(cpu)
                _, ms, _ = measure(lambda v=value: engine.store.get_score_run(v['run']['id'], entity['id']))
                snapshots.append(ms)
                replay, ms, _ = measure(lambda v=value: engine.replay_score(v['run']['id']))
                assert replay['matched']
                replay_times.append(ms)
            if args.heavy_notes:
                _, ms, _ = measure(lambda: engine.explanation('pipeline-heavy'))
                heavy_current.append(ms)
                _, ms, _ = measure(lambda: engine.explanation('pipeline-heavy', heavy['run']['id']))
                historical.append(ms)
        timings['warm_small_explanation'] = summary(warm)
        timings['warm_small_explanation_cpu'] = summary(warm_cpu)
        timings['warm_small_snapshot_lookup'] = summary(snapshots)
        timings['warm_small_replay'] = summary(replay_times)
        checks['warm_replay_successes'] = len(replay_times)
        timings['warm_heavy_explanation'] = summary(heavy_current)
        timings['warm_heavy_historical_explanation'] = summary(historical)

        # Explicitly complete the local workflow for this synthetic fixture.
        # Reading a previously computed result must never trigger scoring.
        from peps.news import NewsAcquirer
        from peps.workflow import GuidedWorkflow
        workflow = GuidedWorkflow(engine.store, NewsAcquirer(engine).sources)
        workflow.set_mode('local')
        workflow.confirm_sources()
        workflow.mark_analysis()
        checks['http_workflow'] = {'mode': 'local', 'explicitly_confirmed': True,
                                   'analysis_current': workflow.snapshot()['analysis_current']}
        # Calls made by the real UI, including a historical note beyond its page cap.
        server, token = create_server(engine)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .05}, daemon=True)
        thread.start()

        def request(path, query):
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=10)
            try:
                connection.request('GET', path+'?'+urlencode(query), headers={'X-PEPS-Token': token})
                response = connection.getresponse()
                raw = response.read()
                assert response.status == 200, (response.status, raw[:300])
                return json.loads(raw), len(raw)
            finally:
                connection.close()

        read_guard = patch.object(engine, 'rescore', side_effect=AssertionError('HTTP reads must not calculate scores'))
        implicit_scoring = read_guard.start()
        try:
            http_small, sizes = [], []
            for _ in range(args.repeats):
                for entity in small:
                    (_, size), ms, _ = measure(lambda e=entity: request('/api/explanation', {'id': e['id']}))
                    http_small.append(ms); sizes.append(size)
            timings['http_small_explanation'] = summary(http_small)
            checks['http_small_response_bytes'] = {'min': min(sizes), 'max': max(sizes)}
            entity_id = 'pipeline-heavy' if args.heavy_notes else small[0]['id']
            current, _ = request('/api/entity', {'id': entity_id})
            all_evidence = engine.store.evidence(entity_id)
            oldest = all_evidence[-1]
            visible = {row['id'] for row in current['evidence']}
            outside = oldest['id'] not in visible
            target_times, api_heavy = [], []
            for _ in range(args.repeats):
                (item, size), ms, _ = measure(lambda: request('/api/run-evidence', {'run_id': heavy['run']['id'], 'id': oldest['id']}))
                assert item['text'] == oldest['text']
                target_times.append(ms)
                (_, exp_size), ms, _ = measure(lambda: request('/api/explanation', {'id': entity_id}))
                api_heavy.append(ms)
            timings['http_target_historical_note'] = summary(target_times)
            timings['http_heavy_explanation'] = summary(api_heavy)
            checks['note_beyond_page'] = {'total_notes': len(all_evidence),
                'visible_notes': len(current['evidence']), 'api_page_limit': current['evidence_limit'],
                'target_outside_visible_page': outside, 'target_exact_original_text': True,
                'target_response_bytes': size, 'explanation_response_bytes': exp_size}
            if args.heavy_notes > 200:
                assert outside and len(current['evidence']) == 200
            checks['http_reads_implicit_scoring_calls'] = implicit_scoring.call_count
            assert implicit_scoring.call_count == 0
        finally:
            read_guard.stop()
            server.shutdown(); server.server_close(); thread.join(timeout=2)

        with engine.store.connection() as con:
            before = con.execute('SELECT COUNT(*) FROM documents').fetchone()[0]
            previous_runs = con.execute('SELECT COUNT(*) FROM score_runs').fetchone()[0]
        engine.rescore(small[0]['id'])
        with engine.store.connection() as con:
            after = con.execute('SELECT COUNT(*) FROM documents').fetchone()[0]
            after_runs = con.execute('SELECT COUNT(*) FROM score_runs').fetchone()[0]
        checks['repeat_identical_rescore'] = {'documents_before': before, 'documents_after': after,
            'runs_before': previous_runs, 'runs_after': after_runs,
            'original_bodies_not_reinserted': before == after,
            'identical_snapshot_reused': previous_runs == after_runs}
        checks['audit'] = engine.store.verify_audit()
        assert checks['audit']['ok'] and before == after and previous_runs == after_runs
        storage = storage_report(engine)
        assert storage['manifest_top_level_body_copies'] == 0

    # Fresh process measures lookup after restart, with no HTTP/socket permission.
    fresh_lookup = {}
    for operation in ('current_explanation','snapshot_lookup','replay','historical_target_note'):
        entity_id = ('pipeline-heavy' if args.heavy_notes and operation == 'historical_target_note'
                     else small[0]['id'])
        evidence = engine.store.evidence(entity_id)[-1]
        command = [sys.executable, str(Path(__file__).resolve()), '--lookup-worker',
                   '--lookup-operation', operation,
                   '--data-dir', str(Path(args.data_dir).resolve()), '--entity', entity_id,
                   '--run-id', runs[entity_id]['run']['id'], '--evidence-id', evidence['id']]
        cold = subprocess.run(command, capture_output=True, text=True, check=True, cwd=ROOT)
        fresh_lookup[operation] = json.loads(cold.stdout)
    return {'phase': 7, 'generated_at': datetime.now(timezone.utc).isoformat(),
        'scope': 'invented_local_default_pipeline_and_actual_loopback_UI_API_not_browser_rendering',
        'environment': {'pid': os.getpid(), 'python': platform.python_version(),
                        'executable': sys.executable, 'platform': platform.platform(),
                        'logical_cpus': os.cpu_count(), 'gpu_used': False,
                        'context_mode': engine.context_mode()},
        'fixture': {'entities': len(entities), 'small_entities': len(small),
                    'notes_per_small_entity': args.notes, 'heavy_notes': args.heavy_notes,
                    'total_documents': imported['documents'], 'all_synthetic': True},
        'data_dir': str(Path(args.data_dir).resolve()), 'code_sha256': code_hashes(),
        'timings': timings, 'checks': checks, 'storage': storage,
        'fresh_restart_lookup': fresh_lookup, 'peak_rss': peak_rss(),
        'process_wall_seconds': time.perf_counter()-wall_start,
        'process_cpu_seconds': time.process_time()-cpu_start,
        'network_guard': {'python_socket_guard': True, **dict(guard),
                          'scope': 'Blocks off-loopback Python DNS/connect operations; not packet capture of native libraries.'},
        'pass': True,
        'limitations': ['Synthetic official-source fixtures; not real notes or web coverage.',
            'Single shared Linux machine; no Windows/GPU/browser-render timing.',
            'Warm lookups include integrity checking/JSON decode and current provenance checks.',
            'No SLA inferred from these observations; only generous regression threshold belongs in tests.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--entities', type=int, default=20)
    parser.add_argument('--notes', type=int, default=4)
    parser.add_argument('--heavy-notes', type=int, default=205)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--lookup-worker', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--lookup-operation', choices=('current_explanation','snapshot_lookup','replay','historical_target_note'),
                        default='current_explanation', help=argparse.SUPPRESS)
    parser.add_argument('--entity', help=argparse.SUPPRESS)
    parser.add_argument('--run-id', help=argparse.SUPPRESS)
    parser.add_argument('--evidence-id', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if min(args.entities, args.notes, args.repeats) < 1 or args.heavy_notes < 0:
        parser.error('entities, notes and repeats must be positive; heavy-notes nonnegative')
    if args.lookup_worker:
        print(json.dumps(lookup_worker(args), ensure_ascii=False))
        return
    if args.worker:
        print(json.dumps(pipeline_worker(args), ensure_ascii=False))
        return
    command = [sys.executable, str(Path(__file__).resolve()), '--worker', '--data-dir', str(args.data_dir),
               '--entities', str(args.entities), '--notes', str(args.notes),
               '--heavy-notes', str(args.heavy_notes), '--repeats', str(args.repeats)]
    value = subprocess.run(command, capture_output=True, text=True, cwd=ROOT)
    if value.returncode:
        sys.stderr.write(value.stderr)
        raise SystemExit(value.returncode)
    report = json.loads(value.stdout)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({'output': str(args.output), 'pass': report['pass'],
                      'fixture': report['fixture'], 'timings': report['timings'],
                      'storage_bytes': report['storage']['sqlite_total_bytes'],
                      'peak_rss': report['peak_rss']}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
