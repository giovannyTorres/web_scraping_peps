"""Correct only context-component RSS measurements; never recompute quality.

Linux /proc VmHWM belongs to the current address space, unlike ru_maxrss that
can retain a parent's pre-exec high-water mark. Every method gets a new process.
"""
import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import platform
import socket
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.benchmark_explainability import compact_output, hybrid_output, load_dataset


def memory():
    values = {}
    path = Path('/proc/self/status')
    if not path.exists():
        raise RuntimeError('This corrective measurement explicitly requires Linux /proc.')
    for line in path.read_text().splitlines():
        if line.startswith(('VmHWM:', 'VmRSS:')):
            key, rest = line.split(':', 1)
            values[key] = int(rest.split()[0])*1024
    return {'peak_rss_bytes': values['VmHWM'], 'current_rss_bytes': values['VmRSS'],
            'method': 'proc_self_status_VmHWM_current_address_space'}


def worker(args):
    def deny(*_args, **_kwargs):
        raise AssertionError('No sockets are permitted in resource-only inference')
    socket.socket.connect = socket.socket.connect_ex = deny
    socket.create_connection = socket.getaddrinfo = deny
    rows, digest = load_dataset(Path(args.dataset))
    names = ('discriminative','advanced') if args.method == 'hybrid' else (args.method,)
    before = memory()
    modules, info = {}, {}
    for name in names:
        module = importlib.import_module({'baseline':'peps.nlp', 'discriminative':'peps.context_model',
                                          'advanced':'peps.advanced_nlp'}[name])
        modules[name] = module
        metadata = module.get_model_info() if name != 'baseline' else {'model_version': module.MODEL_VERSION,
                    'config_hash': module.RULES_HASH}
        if metadata.get('available') is False:
            raise RuntimeError('Optional local model unavailable')
        info[name] = {key: metadata.get(key) for key in
                      ('model_version','artifact_sha256','model_hash','config_hash','runtime_config_hash')}
        info[name]['implementation_sha256'] = hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
    after_load = memory()
    retained = []
    for row in rows:
        if args.method == 'hybrid':
            value = hybrid_output(modules['discriminative'].analyze(row['text'],row['entity']),
                                  modules['advanced'].analyze(row['text'],row['entity']))
        else:
            value = modules[args.method].analyze(row['text'],row['entity'])
        assert isinstance(value, dict)
        # Retain the same compact prediction fields as the original benchmark,
        # without computing classification metrics or repeating warm timing runs.
        retained.append({'id': row['id'], 'role': value.get('role'), 'output': compact_output(value)})
    return {'method':args.method,'pid':os.getpid(),'dataset_sha256':digest,
            'notes_processed_once':len(rows),'compact_predictions_retained':len(retained),'before_model_load':before,
            'after_model_load':after_load,'after_single_inference_pass':memory(),
            'model_info':info,'network_guard':'all_Python_socket_connect_and_DNS_calls_denied',
            'environment':{'python':platform.python_version(),'platform':platform.platform(),
                           'executable':sys.executable,'gpu_used':False,'math_threads':1}}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--python',default=sys.executable)
    parser.add_argument('--dataset',default=str(ROOT/'tests/data/context_challenge.jsonl'))
    parser.add_argument('--source-report',default=str(ROOT/'docs/benchmarks/phase4_comparison.json'))
    parser.add_argument('--output',default=str(ROOT/'docs/benchmarks/phase4_resources_corrected.json'))
    parser.add_argument('--worker',action='store_true')
    parser.add_argument('--method',choices=('baseline','discriminative','advanced','hybrid'))
    args=parser.parse_args()
    if args.worker:
        print(json.dumps(worker(args),ensure_ascii=False));return
    from datetime import datetime,timezone
    source=Path(args.source_report)
    report={'generated_at':datetime.now(timezone.utc).isoformat(),
            'scope':'RSS_correction_only_no_quality_latency_training_or_threshold_changes',
            'reason':'Linux ru_maxrss may retain parent preexec high-water mark; /proc VmHWM measures current address space.',
            'source_report':str(source),'source_report_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
            'replaces_field':'methods.<baseline|discriminative|advanced|hybrid>.performance.peak_rss',
            'preserved':'All original quality, predictions, timings and Laya psutil observed RSS remain unchanged.',
            'evaluator_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'methods':{}}
    env=dict(os.environ,OMP_NUM_THREADS='1',OPENBLAS_NUM_THREADS='1',MKL_NUM_THREADS='1',CUDA_VISIBLE_DEVICES='')
    for method in ('baseline','discriminative','advanced','hybrid'):
        command=[args.python,str(Path(__file__).resolve()),'--worker','--method',method,'--dataset',args.dataset]
        process=subprocess.run(command,capture_output=True,text=True,check=True,cwd=ROOT,env=env)
        report['methods'][method]=json.loads(process.stdout)
    path=Path(args.output);path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'output':str(path),'corrected_peak_rss_bytes':{
        name:value['after_single_inference_pass']['peak_rss_bytes'] for name,value in report['methods'].items()}},ensure_ascii=False))


if __name__=='__main__':
    main()
