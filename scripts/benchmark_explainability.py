"""Evaluate frozen local contextual methods on the independent challenge.

Never trains, downloads models, or changes thresholds. Each measured method is
run in a fresh subprocess. A separate Laya result file can be imported by ID.
See docs/BENCHMARK_PROTOCOL.md before interpreting these synthetic measurements.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import importlib
import json
import math
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
ROLES = ('accused', 'victim', 'complainant', 'incidental', 'negated',
         'resolved', 'insufficient', 'indirect')
RELEVANT_ROLES = frozenset(ROLES) - {'incidental', 'insufficient'}
CHALLENGE_SHA256 = '8425c256e6dcc97a1664577b6aa593b09a75123fc28adf6f07cc20f16e36816a'


def ratio(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator else 0.0


def percentile(values: list[float], q: float) -> float | None:
    return sorted(values)[max(0, math.ceil(q * len(values))-1)] if values else None


def distribution(values: list[float]) -> dict[str, Any]:
    return {'count': len(values), 'mean': ratio(sum(values), len(values)),
            'p50': percentile(values, .5), 'p95': percentile(values, .95),
            'max': max(values) if values else None, 'total': sum(values)}


def prf(tp: int, fp: int, fn: int) -> dict[str, Any]:
    return {'precision': ratio(tp, tp+fp), 'recall': ratio(tp, tp+fn),
            'f1': ratio(2*tp, 2*tp+fp+fn), 'tp': tp, 'fp': fp, 'fn': fn}


def classification(truth: list[str], predicted: list[str], labels=ROLES) -> dict[str, Any]:
    columns = list(labels) + sorted(set(predicted)-set(labels))
    confusion = {label: dict.fromkeys(columns, 0) for label in labels}
    per_label = {}
    for true, pred in zip(truth, predicted):
        confusion[true][pred] += 1
    for label in labels:
        tp = sum(t == p == label for t, p in zip(truth, predicted))
        fp = sum(t != label and p == label for t, p in zip(truth, predicted))
        fn = sum(t == label and p != label for t, p in zip(truth, predicted))
        per_label[label] = {**prf(tp, fp, fn), 'support': truth.count(label)}
    return {'n': len(truth), 'correct': sum(t == p for t, p in zip(truth, predicted)),
            'accuracy': ratio(sum(t == p for t, p in zip(truth, predicted)), len(truth)),
            'macro_f1': ratio(sum(row['f1'] for row in per_label.values()), len(labels)),
            'per_label': per_label, 'confusion': confusion}


def load_dataset(path: Path, allow_other: bool = False) -> tuple[list[dict], str]:
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if not allow_other and digest != CHALLENGE_SHA256:
        raise ValueError('Challenge hash changed; use an explicitly versioned new protocol or --allow-other-dataset.')
    rows = [json.loads(line) for line in raw.decode('utf-8').splitlines() if line.strip()]
    if len({row['id'] for row in rows}) != len(rows):
        raise ValueError('Duplicate case IDs')
    for row in rows:
        if row['role'] not in ROLES:
            raise ValueError(f"Unknown reference role: {row['id']}")
        for span in [*row.get('ent_spans', []), *row.get('evidence', [])]:
            if row['text'][span['start']:span['end']] != span['text']:
                raise ValueError(f"Invalid reference offsets: {row['id']}")
        if row['expected_relevant'] != (row['role'] in RELEVANT_ROLES):
            raise ValueError(f"Unexpected relevance convention: {row['id']}")
    return rows, digest


def peak_rss() -> dict[str, Any]:
    """Label a real process high-water mark separately from sampled current RSS."""
    try:
        import resource
        value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return {'bytes': int(value if sys.platform == 'darwin' else value * 1024),
                'measurement': 'resource.ru_maxrss_process_high_water_mark'}
    except ImportError:
        try:
            import psutil
            mem = psutil.Process().memory_info()
            return {'bytes': int(getattr(mem, 'peak_wset', mem.rss)),
                    'measurement': 'psutil.peak_wset' if hasattr(mem, 'peak_wset')
                    else 'psutil.current_rss_sample_not_high_water_mark'}
        except ImportError:
            return {'bytes': None, 'measurement': 'unavailable_on_this_platform'}


def directory_bytes(path: Path) -> int:
    """Logical file bytes, excludes symlinks; not compressed installer size."""
    return sum(file.stat().st_size for file in path.rglob('*')
               if file.is_file() and not file.is_symlink())


def external_runtime_storage(environment: Path) -> dict:
    """Inspect pinned installed distributions, including inherited packages.

    No model/data import or inference happens here. Summing only a venv directory
    would undercount a research environment created with system-site-packages.
    """
    interpreter = environment / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    names = [line.split('==', 1)[0] for line in
             (ROOT/'requirements-poc-laya.lock.txt').read_text(encoding='utf-8').splitlines()
             if line.strip() and not line.lstrip().startswith('#')]
    code = '''import importlib.metadata as m,json,pathlib,sys
prefix=pathlib.Path(sys.prefix).resolve()
all_files=set(); outside=set(); details={}
for name in json.loads(sys.argv[1]):
    d=m.distribution(name)
    files={pathlib.Path(d.locate_file(f)).resolve() for f in d.files or []}
    files={p for p in files if p.is_file()}
    external={p for p in files if not p.is_relative_to(prefix)}
    all_files.update(files);outside.update(external)
    details[name]={'version':d.version,'installed_file_bytes':sum(p.stat().st_size for p in files),
      'inherited_file_bytes':sum(p.stat().st_size for p in external)}
cfg=prefix/'pyvenv.cfg'
print(json.dumps({'pinned_runtime_distribution_bytes':sum(p.stat().st_size for p in all_files),
 'inherited_pinned_distribution_bytes':sum(p.stat().st_size for p in outside),
 'distributions':details,'pyvenv_cfg':cfg.read_text() if cfg.exists() else None,
 'scope':'Pinned distribution files incl inherited; excludes base Python and model weights.'}))'''
    completed = subprocess.run([str(interpreter), '-c', code, json.dumps(names)],
                               capture_output=True, text=True, check=True)
    return json.loads(completed.stdout)


def baseline_output(module, text: str, entity: dict) -> dict:
    """Frozen adapter for the pre-existing rule engine, not a new classifier.

    The old schema cannot express victims vs complainants. Context relations
    therefore remain incidental; no new keyword matching is added for benchmark.
    """
    result = module.analyze(text, entity)
    findings = []
    for finding in result.get('findings', []):
        if finding.get('negated') or finding.get('stage') == 'denial':
            role = 'negated'
        elif finding.get('stage') in {'acquittal', 'dismissed'}:
            role = 'resolved'
        elif finding.get('relationship') in {'family', 'associate'}:
            role = 'indirect'
        elif finding.get('relationship') == 'direct':
            role = 'accused'
        else:
            role = 'incidental'
        findings.append(dict(finding, role=role))
    roles = {finding['role'] for finding in findings}
    role = (next(iter(roles)) if len(roles) == 1 else 'insufficient' if roles
            else 'incidental' if result.get('mentions') else 'insufficient')
    return {**result, 'method': 'legacy_rules_adapter', 'role': role,
            'abstained': role == 'insufficient', 'findings': findings,
            'score_effect': 'benchmark_adapter_only_no_scoring_changes'}


def hybrid_output(discriminative: dict, advanced: dict) -> dict:
    a, b = discriminative.get('role'), advanced.get('role')
    role = a if a == b and a in ROLES else 'insufficient'
    # These exact fragments/NER belong to a named component, not to an
    # independently trained hybrid extractor. Keep the origin in every finding.
    findings = [dict(finding, component_method=source)
                for source, component in [('discriminative', discriminative), ('advanced', advanced)]
                for finding in component.get('findings', [])]
    return {'method': 'hybrid_agreement', 'role': role, 'abstained': role == 'insufficient',
            'available': advanced.get('available', True),
            'component_roles': {'discriminative': a, 'advanced': b},
            'mentions': advanced.get('mentions', []), 'entities': advanced.get('entities', []),
            'findings': findings, 'relations': advanced.get('relations', []),
            'warnings': ['Fixed agreement policy; disagreements abstain.'],
            'score_effect': 'auxiliary_only'}


def compact_output(result: dict) -> dict:
    """Keep audit references and decisions, without copying complete notes."""
    result_keys = ('method', 'role', 'predicted_role', 'abstained', 'available',
                   'relevance', 'component_roles', 'mentions', 'entities',
                   'model_version', 'model_hash', 'config_hash', 'score_effect', 'warnings')
    selected = {key: result[key] for key in result_keys if key in result}
    selected['findings'] = [{key: finding[key] for key in
                            ('role', 'start', 'end', 'quote', 'component_method') if key in finding}
                           for finding in result.get('findings', [])]
    return selected


def worker(args) -> dict:
    rows, digest = load_dataset(Path(args.dataset), args.allow_other_dataset)
    start = time.perf_counter()
    start_cpu = time.process_time()
    modules = {}
    names = ('discriminative', 'advanced') if args.method == 'hybrid' else (args.method,)
    for name in names:
        modules[name] = importlib.import_module({'discriminative': 'peps.context_model',
                                                'advanced': 'peps.advanced_nlp',
                                                'baseline': 'peps.nlp'}[name])
    infos = {name: module.get_model_info() if name != 'baseline' else
             {'model_version': module.MODEL_VERSION, 'rules_version': module.RULES_VERSION,
              'config_hash': module.RULES_HASH,
              'implementation_sha256': hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest(),
              'model_bytes': 0, 'scope': 'legacy_rules_frozen_adapter_no_learned_weights'}
             for name, module in modules.items()}
    for name, module in modules.items():
        infos[name]['implementation_sha256'] = hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
    if 'discriminative' in modules:
        infos['discriminative']['model_bytes'] = modules['discriminative'].MODEL_PATH.stat().st_size
    if any(info.get('available') is False for info in infos.values()):
        raise RuntimeError('Requested optional model unavailable: ' + json.dumps(infos))
    import_load_ms = (time.perf_counter()-start)*1000
    load_cpu_ms = (time.process_time()-start_cpu)*1000

    def analyze(row):
        if args.method == 'baseline':
            return baseline_output(modules['baseline'], row['text'], row['entity'])
        if args.method == 'hybrid':
            return hybrid_output(modules['discriminative'].analyze(row['text'], row['entity']),
                                 modules['advanced'].analyze(row['text'], row['entity']))
        return modules[args.method].analyze(row['text'], row['entity'])

    first_start, first_cpu = time.perf_counter(), time.process_time()
    analyze(rows[0])
    first_ms = (time.perf_counter()-first_start)*1000
    first_cpu_ms = (time.process_time()-first_cpu)*1000
    predictions = []
    quality_start = time.perf_counter()
    quality_cpu = time.process_time()
    for row in rows:
        t, c = time.perf_counter(), time.process_time()
        result = analyze(row)
        elapsed_ms, cpu_ms = (time.perf_counter()-t)*1000, (time.process_time()-c)*1000
        predictions.append({'id': row['id'], 'role': result.get('role', '__invalid__'),
                            'latency_ms': elapsed_ms, 'cpu_ms': cpu_ms,
                            'output': compact_output(result)})
    quality_wall = time.perf_counter()-quality_start
    quality_cpu_seconds = time.process_time()-quality_cpu
    warm_wall, warm_cpu, entity_times = [], [], defaultdict(list)
    warm_start, warm_cpu_start = time.perf_counter(), time.process_time()
    for _ in range(args.repeats):
        repetition_entities = Counter()
        for row in rows:
            t, c = time.perf_counter(), time.process_time()
            analyze(row)
            elapsed_ms, cpu_ms = (time.perf_counter()-t)*1000, (time.process_time()-c)*1000
            warm_wall.append(elapsed_ms)
            warm_cpu.append(cpu_ms)
            repetition_entities[row['entity']['id']] += elapsed_ms
        for entity_id, elapsed in repetition_entities.items():
            entity_times[entity_id].append(elapsed)
    wall_seconds = time.perf_counter()-warm_start
    cpu_seconds = time.process_time()-warm_cpu_start
    memory = peak_rss()
    return {'method': args.method, 'dataset_sha256': digest,
            'model_info': infos,
            'environment': {'python': platform.python_version(), 'executable': sys.executable,
                            'pid': os.getpid(), 'prefix': sys.prefix,
                            'platform': platform.platform(), 'machine': platform.machine(),
                            'processor': platform.processor(), 'logical_cpus': os.cpu_count(),
                            'gpu_used': False,
                            'thread_environment': {key: os.environ.get(key) for key in
                                ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS')}},
            'performance': {'cold_import_and_model_load_ms': import_load_ms,
                            'cold_import_and_model_load_cpu_ms': load_cpu_ms,
                            'first_note_ms': first_ms, 'first_note_cpu_ms': first_cpu_ms,
                            'cold_load_plus_first_note_ms': import_load_ms+first_ms,
                            'quality_pass_wall_seconds': quality_wall,
                            'quality_pass_cpu_seconds': quality_cpu_seconds,
                            'warm_repetitions': args.repeats,
                            'warm_note_wall_ms': distribution(warm_wall),
                            'warm_note_cpu_ms': distribution(warm_cpu),
                            'warm_entity_wall_ms': {key: distribution(value)
                                                    for key, value in entity_times.items()},
                            'warm_wall_seconds': wall_seconds, 'warm_cpu_seconds': cpu_seconds,
                            'mean_process_cpu_cores_during_warm': ratio(cpu_seconds, wall_seconds),
                            'peak_rss': memory},
            'storage': {'model_artifact_bytes': sum(info.get('model_bytes', 0) for info in infos.values()),
                        'benchmark_environment_directory': sys.prefix,
                        'benchmark_environment_file_bytes': directory_bytes(Path(sys.prefix)),
                        'measurement': 'logical_regular_file_bytes_excluding_symlinks',
                        'required_runtime': 'Python_stdlib' if args.method in {'baseline', 'discriminative'}
                        else 'Python_plus_optional_spacy_environment',
                        'note': 'Benchmark environment includes optional packages; model files may be inside it. Do not sum overlapping sizes.'},
            'predictions': predictions}


def span_counts(rows, indexed, *, typed, field):
    tp = fp = fn = 0
    for row in rows:
        gold = {(s['start'], s['end'], s['label']) if typed else (s['start'], s['end'])
                for s in row.get('ent_spans', []) if s['label'] in {'PER', 'ORG'}}
        predicted = set()
        for span in indexed[row['id']].get('output', {}).get(field, []):
            label = {'PERSON': 'PER'}.get(span.get('label'), span.get('label'))
            if typed and label not in {'PER', 'ORG'}:
                continue
            if field == 'entities' and label not in {'PER', 'ORG'}:
                continue
            if isinstance(span.get('start'), int) and isinstance(span.get('end'), int):
                predicted.add((span['start'], span['end'], label) if typed
                              else (span['start'], span['end']))
        tp += len(gold & predicted)
        fp += len(predicted-gold)
        fn += len(gold-predicted)
    return {**prf(tp, fp, fn), 'gold_spans': tp+fn,
            'scope': 'named_PER_ORG_spans_in_manual_synthetic_challenge_only'}


def relation_counts(rows, indexed):
    """Compare one canonical target span + final role, not semantic graph recall."""
    tp = fp = fn = 0
    for row in rows:
        gold = {(r['start'], r['end'], r['role']) for r in row.get('relations', [])}
        prediction = indexed[row['id']]
        mentions = prediction.get('output', {}).get('mentions', [])
        valid = [m for m in mentions if isinstance(m.get('start'), int)
                 and isinstance(m.get('end'), int)]
        predicted = set()
        if valid:
            first = min(valid, key=lambda m: (m['start'], -m['end']))
            predicted.add((first['start'], first['end'], prediction['role']))
        tp += len(gold & predicted)
        fp += len(predicted-gold)
        fn += len(gold-predicted)
    return {**prf(tp, fp, fn), 'gold_relations': tp+fn,
            'adapter': 'first_model_target_mention_plus_final_note_role',
            'scope': 'actor_role_attribution_only_not_general_relation_extraction'}


def evidence_metrics(rows, indexed):
    fragments = valid = notes_with_fragments = correct_role_with_overlap = 0
    gold_chars = covered_chars = 0
    for row in rows:
        prediction = indexed[row['id']]
        findings = prediction.get('output', {}).get('findings', [])
        if findings:
            notes_with_fragments += 1
        correct = prediction['role'] == row['role']
        covered = set()
        for finding in findings:
            fragments += 1
            a, b, quote = finding.get('start'), finding.get('end'), finding.get('quote')
            literal = (isinstance(a, int) and isinstance(b, int) and isinstance(quote, str)
                       and 0 <= a < b <= len(row['text']) and row['text'][a:b] == quote)
            valid += literal
            if literal and correct and finding.get('role') == row['role']:
                covered.update(range(a, b))
        reference = set()
        for span in row.get('evidence', []):
            reference.update(range(span['start'], span['end']))
        overlap = reference & covered
        correct_role_with_overlap += bool(overlap)
        covered_chars += len(overlap)
        gold_chars += len(reference)
    return {'emitted_fragments': fragments, 'literal_valid_fragments': valid,
            'literal_valid_fraction': ratio(valid, fragments) if fragments else None,
            'notes_with_fragments': notes_with_fragments, 'notes_total': len(rows),
            'correct_role_and_gold_overlap_notes': correct_role_with_overlap,
            'gold_char_coverage_with_correct_role': ratio(covered_chars, gold_chars),
            'covered_gold_characters': covered_chars, 'gold_characters': gold_chars,
            'semantic_claim_support': None,
            'limitation': 'Literal offsets and gold overlap do not establish semantic support or truth.'}


def score_method(rows: list[dict], result: dict) -> dict:
    predictions = result['predictions']
    indexed = {item['id']: item for item in predictions}
    if len(indexed) != len(predictions) or set(indexed) != {row['id'] for row in rows}:
        raise ValueError('Prediction IDs must match the challenge exactly once.')
    truth, predicted = [row['role'] for row in rows], [indexed[row['id']]['role'] for row in rows]
    report = classification(truth, predicted)
    report['abstention_fraction'] = ratio(predicted.count('insufficient'), len(rows))
    report['abstention_flags'] = sum(bool(item.get('abstained', item.get('output', {}).get('abstained', False)))
                                     for item in predictions)
    relevant_truth = ['relevant' if row['expected_relevant'] else 'not_relevant' for row in rows]
    relevant_prediction = ['relevant' if pred in RELEVANT_ROLES else 'not_relevant' for pred in predicted]
    report['relevance'] = classification(relevant_truth, relevant_prediction, ('relevant', 'not_relevant'))
    non_accused = [(row, indexed[row['id']]) for row in rows if row['role'] != 'accused']
    identities = [(row, indexed[row['id']]) for row in rows
                  if row.get('identity_reference') in {'ambiguous', 'excluded', 'not_mentioned'}]
    protected_roles = [(row, indexed[row['id']]) for row in rows if row['role'] in {'victim', 'complainant'}]
    report['false_accused_attributions'] = {}
    for label, subset in [('all_non_accused', non_accused), ('ambiguous_excluded_absent_identity', identities),
                          ('victim_or_complainant', protected_roles)]:
        count = sum(pred['role'] == 'accused' for _, pred in subset)
        report['false_accused_attributions'][label] = {'count': count, 'denominator': len(subset),
                                                      'fraction': ratio(count, len(subset))}
    outputs = [indexed[row['id']].get('output', {}) for row in rows]
    has_entities = any('entities' in output for output in outputs)
    has_mentions = any('mentions' in output for output in outputs)
    report['ner_typed'] = span_counts(rows, indexed, typed=True, field='entities') if has_entities else None
    report['ner_untyped'] = span_counts(rows, indexed, typed=False, field='entities') if has_entities else None
    report['literal_target_mentions_untyped'] = (span_counts(rows, indexed, typed=False, field='mentions')
                                                if has_mentions else None)
    report['actor_role'] = relation_counts(rows, indexed) if has_mentions else None
    report['evidence'] = evidence_metrics(rows, indexed) if any('findings' in o for o in outputs) else None
    report['per_tag'] = {}
    for tag in sorted({tag for row in rows for tag in row.get('tags', [])}):
        subset = [row for row in rows if tag in row.get('tags', [])]
        present_labels = tuple(role for role in ROLES if any(row['role'] == role for row in subset))
        subreport = classification([row['role'] for row in subset],
                                   [indexed[row['id']]['role'] for row in subset], present_labels)
        report['per_tag'][tag] = {'n': len(subset), 'accuracy': subreport['accuracy'],
                                  'macro_f1_present_reference_classes': subreport['macro_f1']}
    entities = defaultdict(list)
    for row in rows:
        entities[row['entity']['id']].append(row)
    entity_accuracy = []
    exact_role_sets = 0
    scenarios = []
    for entity_id, subset in entities.items():
        entity_accuracy.append(ratio(sum(row['role'] == indexed[row['id']]['role'] for row in subset), len(subset)))
        gold_roles = sorted({row['role'] for row in subset})
        pred_roles = sorted({indexed[row['id']]['role'] for row in subset})
        exact_role_sets += gold_roles == pred_roles
        if len(subset) > 1:
            scenarios.append({'entity_id': entity_id, 'case_ids': [row['id'] for row in subset],
                              'reference_roles': gold_roles, 'predicted_roles': pred_roles,
                              'role_set_preserved': gold_roles == pred_roles})
    report['entity_aggregate'] = {'entities': len(entities),
        'mean_per_entity_note_accuracy': ratio(sum(entity_accuracy), len(entity_accuracy)),
        'exact_role_set_entities': exact_role_sets,
        'exact_role_set_fraction': ratio(exact_role_sets, len(entities)),
        'multi_note_scenarios': scenarios,
        'limitation': 'No final entity score or automatic chronology resolution is evaluated.'}
    report['score_fidelity'] = {'measured': False,
        'reason': 'Auxiliary classifiers do not contribute to the source-of-truth scoring formula.'}
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', default=str(ROOT/'tests/data/context_challenge.jsonl'))
    parser.add_argument('--allow-other-dataset', action='store_true')
    parser.add_argument('--methods', nargs='+', choices=('discriminative', 'advanced', 'hybrid', 'baseline'),
                        default=['discriminative', 'advanced', 'hybrid', 'baseline'])
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--python', default=sys.executable, help='Interpreter used for every isolated worker')
    parser.add_argument('--laya', help='Existing local Laya results JSON; never runs external inference')
    parser.add_argument('--laya-environment', help='Optional local Laya venv directory for separate disk measurement')
    parser.add_argument('--output', default=str(ROOT/'docs/benchmarks/phase4_comparison.json'))
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--method', choices=('discriminative', 'advanced', 'hybrid', 'baseline'), help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.repeats < 1 or args.repeats > 100:
        parser.error('--repeats must be between 1 and 100')
    if args.worker:
        print(json.dumps(worker(args), ensure_ascii=False, separators=(',', ':')))
        return
    rows, digest = load_dataset(Path(args.dataset), args.allow_other_dataset)
    report = {'phase': 4, 'executed_at': datetime.now(timezone.utc).isoformat(),
              'protocol': 'docs/BENCHMARK_PROTOCOL.md',
              'dataset': {'path': str(Path(args.dataset).resolve().relative_to(ROOT))
                          if Path(args.dataset).resolve().is_relative_to(ROOT) else str(Path(args.dataset)),
                          'sha256': digest, 'cases': len(rows),
                          'roles': dict(Counter(row['role'] for row in rows)),
                          'source': 'manual_synthetic_not_real_project_notes'},
              'execution': {'repeats': args.repeats, 'python': args.python,
                            'evaluator_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                            'method_order': args.methods,
                            'workers': 'sequential_fresh_subprocess_per_method',
                            'laya_import': args.laya, 'training_or_tuning': False},
              'methods': {}, 'errors': {},
              'limitations': ['Synthetic labels from one author; no independent human adjudication.',
                  'Balanced roles are not production prevalence.',
                  'Literal evidence is not semantic correctness or fact verification.',
                  'No Windows or real-project-note performance claim is made by this run.']}
    env = dict(os.environ)
    # Controlled single-thread math workers. CPU is reported as CPU/wall, not a
    # percentage of all host cores. Laya runner conditions are preserved separately.
    env.update(OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1', MKL_NUM_THREADS='1',
               CUDA_VISIBLE_DEVICES='')
    for method in args.methods:
        command = [args.python, str(Path(__file__).resolve()), '--worker', '--method', method,
                   '--dataset', str(Path(args.dataset).resolve()), '--repeats', str(args.repeats)]
        if args.allow_other_dataset:
            command.append('--allow-other-dataset')
        completed = subprocess.run(command, capture_output=True, text=True, env=env, cwd=ROOT)
        if completed.returncode:
            report['errors'][method] = {'exit_code': completed.returncode,
                                        'stderr': completed.stderr[-6000:], 'command': command}
            continue
        result = json.loads(completed.stdout)
        result['metrics'] = score_method(rows, result)
        result['command'] = command
        report['methods'][method] = result
    if args.laya:
        source = json.loads(Path(args.laya).read_text(encoding='utf-8'))
        if source.get('protocol', {}).get('corpus_sha256') != digest:
            raise ValueError('Laya source corpus hash does not match evaluator corpus.')
        # The independent runner owns model/environment/resource measurements;
        # this evaluator recomputes quality metrics against the same gold IDs.
        result = {'method': 'laya_raw', 'source_report': args.laya,
                  'source_report_sha256': hashlib.sha256(Path(args.laya).read_bytes()).hexdigest(),
                  'source_metadata': {key: value for key, value in source.items()
                                      if key not in {'predictions', 'results'}},
                  'predictions': source['predictions']}
        result['storage'] = {'model_artifact_bytes': source.get('disk_model_bytes'),
                             'environment_directory': args.laya_environment,
                             'environment_file_bytes': directory_bytes(Path(args.laya_environment))
                             if args.laya_environment else None,
                             'measurement': 'logical_regular_file_bytes_excluding_symlinks'}
        if args.laya_environment:
            details = external_runtime_storage(Path(args.laya_environment))
            result['storage'].update(details)
            result['storage']['venv_plus_used_inherited_distribution_bytes'] = (
                result['storage']['environment_file_bytes'] + details['inherited_pinned_distribution_bytes'])
        result['metrics'] = score_method(rows, result)
        report['methods']['laya_raw'] = result
        abstaining = {**result, 'method': 'laya_abstaining', 'predictions': [
            {**item, 'raw_role': item['role'], 'role': 'insufficient' if item.get('abstained') else item['role']}
            for item in source['predictions']]}
        abstaining['metrics'] = score_method(rows, abstaining)
        abstaining['policy'] = 'Apply frozen runner abstention flags; keep raw choices separately.'
        report['methods']['laya_abstaining'] = abstaining
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({'output': str(output), 'dataset_sha256': digest,
                      'methods': {name: {'accuracy': result['metrics']['accuracy'],
                                         'macro_f1': result['metrics']['macro_f1']}
                                  for name, result in report['methods'].items()},
                      'errors': list(report['errors'])}, ensure_ascii=False))
    if report['errors']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
