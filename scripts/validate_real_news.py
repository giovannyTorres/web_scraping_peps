"""Opt-in, bounded real-network validation against public publisher archives.

Uses an isolated bank of public example names, the production collector and the
real attribution/scoring pipeline. A recording subclass only caps and observes
HTTP; it never substitutes responses, clocks, parsers or source text. Reports
contain technical counts, URLs and hashes, not copies of article bodies or a
claim of validated risk predictions.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sqlite3
import sys
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from peps.acquisition_coverage import acquisition_coverage
from peps.collector import CollectionError, SafeFetcher
from peps.engine import Engine
from peps.news import NewsAcquirer
from peps.relevance import RelevanceIndex, assessment_summary
from peps.store import canonical, digest, now


CASES = [
    {'id': 'epn', 'name': 'Enrique Peña Nieto', 'kind': 'person', 'aliases': ['Peña Nieto'],
     'context': {'role': ['presidente', 'expresidente'], 'state': 'Estado de México'}},
    {'id': 'pemex', 'name': 'Pemex', 'kind': 'company', 'aliases': ['Petróleos Mexicanos']},
    {'id': 'odebrecht', 'name': 'Odebrecht', 'kind': 'company'},
    {'id': 'ahmsa', 'name': 'Altos Hornos de México, S.A.B. de C.V.', 'kind': 'company', 'aliases': ['AHMSA']},
    {'id': 'bimbo', 'name': 'Grupo Bimbo, S.A.B. de C.V.', 'kind': 'company', 'aliases': ['Grupo Bimbo']},
    {'id': 'femsa', 'name': 'Fomento Económico Mexicano, S.A.B. de C.V.', 'kind': 'company', 'aliases': ['FEMSA']},
    {'id': 'cemex', 'name': 'CEMEX, S.A.B. de C.V.', 'kind': 'company', 'aliases': ['Cemex']},
    {'id': 'ohl', 'name': 'OHL México, S.A.B. de C.V.', 'kind': 'company', 'aliases': ['OHL México']},
]


def article_report(engine):
    """Metadata-only documents are not downloaded editorial bodies."""
    rows = []
    index = RelevanceIndex(CASES)
    with engine.store.connection() as con:
        for row in con.execute('SELECT n.id,n.canonical_url,n.title,n.scope,n.status,n.document_id,d.text '
                               'FROM news_articles n LEFT JOIN documents d ON d.id=n.document_id'):
            text = (row['text'] or '') if row['scope'] == 'full_article' else ''
            rows.append({key: row[key] for key in ('id', 'canonical_url', 'title', 'scope', 'status', 'document_id')} |
                        {'body_chars': len(text), 'body_sha256': digest(text) if text else None,
                         'body_target_mentions': index.rank('', text)['entity_ids'] if text else []})
    return rows


def assessment_report(engine):
    assessments = []
    with engine.store.connection() as con:
        links = {entity['id']: dict(Counter(row[0] for row in con.execute(
            "SELECT json_extract(resolution,'$.status') FROM news_identity_links WHERE entity_id=?",
            (entity['id'],)))) for entity in CASES}
    for entity in CASES:
        explanation = engine.explanation(entity['id'])
        result = explanation['result']
        assessments.append({'id': entity['id'], 'name': entity['name'], 'score': result['score'],
            'assessment': assessment_summary(result), 'evidence_items': len(result.get('evidence_audit') or []),
            'identity_statuses': links[entity['id']],
            'exclusion_counts': dict(Counter(code for item in result.get('evidence_audit') or []
                                            for code in item.get('exclusions') or [])),
            'run_id': explanation['run']['id'], 'replay_matched': engine.replay_score(explanation['run']['id'])['matched']})
    return assessments


def real_run(args):
    hashes_before = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in (ROOT / 'peps').glob('*.py')}
    directory = args.data_dir.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    engine = Engine(directory, online=True)
    existing = list(engine.store.iter_entities())
    if existing and {entity['id'] for entity in existing} != {entity['id'] for entity in CASES}:
        raise ValueError('Use un banco aislado para estas entidades públicas; no se modifica otro padrón.')
    engine.store.import_entities(CASES)
    cache_seed = {'seeded': False}
    if args.http_cache_seed:
        target = directory / 'network.sqlite3'
        if target.exists():
            raise ValueError('La caché inicial sólo se puede copiar a un banco aislado nuevo.')
        with sqlite3.connect(args.http_cache_seed.resolve().as_uri() + '?mode=ro', uri=True) as source:
            with sqlite3.connect(target) as destination:
                source.backup(destination)
        cache_seed = {'seeded': True, 'method': 'SQLite backup of previous actual HTTP cache and host courtesy state; no responses synthesized.'}
    config = json.loads((ROOT / 'peps/defaults/news_sources.json').read_text(encoding='utf-8'))
    selected = set(args.sources.split(','))
    config['sources'] = [source for source in config['sources'] if source['id'] in selected]
    if {source['id'] for source in config['sources']} != selected:
        raise ValueError('Fuente de validación desconocida')
    for source in config['sources']:
        source.update(enabled=True, terms_approved=True, download_articles=True,
                      article_terms_approved=True, body_terms_approved=True,
                      timeout_seconds=min(15, args.seconds), robots_required=True)
    config['user_agent'] = 'WebScope-Live-Validation/2.8 (+https://github.com/giovannyTorres/web_scraping_peps)'
    config['search'].update(strategy=args.strategy, preset=args.preset, start=args.start, end=args.end,
                            execution_mode='quick', session_seconds=120)
    path = directory / 'news_sources.json'
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
    news = NewsAcquirer(engine)
    events, progress, lock = [], [], threading.Lock()
    deadline = time.monotonic() + args.seconds

    class ObservedRealFetcher(SafeFetcher):
        def _transport(self, url, headers, timeout, max_bytes, addresses):
            with lock:
                if len(events) >= args.max_requests or time.monotonic() >= deadline:
                    raise CollectionError('Real validation HTTP budget exhausted')
                event = {'url': url, 'http_status': None, 'started_at': now()}
                events.append(event)
            began = time.monotonic()
            try:
                status, response_headers, body = super()._transport(
                    url, headers, min(timeout, max(.001, deadline - began)), max_bytes, addresses)
                event.update(http_status=status, decoded_bytes=len(body))
                return status, response_headers, body
            except Exception as exc:
                event['error_type'] = type(exc).__name__
                raise
            finally:
                event['seconds'] = round(time.monotonic() - began, 6)

    began = time.monotonic()
    print('Adquisición pública real: iniciando.', flush=True)
    with patch('peps.collector.SafeFetcher', ObservedRealFetcher):
        acquisition = news.acquire(workers=8, seconds=args.seconds, queue=True,
            window='public-real-validation-' + now(), max_articles=10000,
            relevant_only=True, per_entity_body_limit=args.bodies_per_entity,
            body_budget=args.bodies_per_entity * len(CASES), progress=progress.append)
    acquisition_seconds = time.monotonic() - began
    print(f'Adquisición: {acquisition_seconds:.2f}s; atribuyendo textos reales.', flush=True)
    began = time.monotonic()
    attribution = news.attribute(limit=None, rescore=False)
    attribution_seconds = time.monotonic() - began
    print(f'Atribución: {attribution_seconds:.2f}s; calculando y verificando replay.', flush=True)
    began = time.monotonic()
    scored = engine.rescore(acquisition={'mode': 'real_public_validation',
        'partial': True, 'observed': acquisition_coverage(engine.store, news.sources())})
    scoring_seconds = time.monotonic() - began

    # Re-run the old and corrected candidate filters over exactly the same
    # publisher metadata already fetched above. No second HTTP corpus or fake
    # response is introduced for this comparison.
    baseline_path = directory / 'baseline_relevance_2_7.py'
    baseline_path.write_bytes(subprocess.check_output(['git', 'show', 'd81d8e2:peps/relevance.py'], cwd=ROOT))
    spec = importlib.util.spec_from_file_location('peps._real_baseline_relevance', baseline_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    before, after = module.RelevanceIndex(CASES), RelevanceIndex(CASES)
    old_counts, new_counts, pages = Counter(), Counter(), []
    with engine.store.connection() as con:
        for row in con.execute('SELECT source_id,url,content_hash,retrieved_at,parsed FROM news_shared_pages'):
            parsed = json.loads(row['parsed'])
            pages.append({'source_id': row['source_id'], 'url': row['url'], 'sha256': row['content_hash'],
                          'retrieved_at': row['retrieved_at'], 'records': len(parsed['records'])})
            for record in parsed['records']:
                old_counts.update(before.rank(record.get('title'), record.get('text'))['entity_ids'])
                new_counts.update(after.rank(record.get('title'), record.get('text'))['entity_ids'])
        states = [dict(row) for row in con.execute("SELECT kind,status,error,count(*) AS count FROM jobs GROUP BY kind,status,error")]
    report = {'recorded_at': now(), 'method': {'real_network': True, 'response_substitution': False,
        'recording_subclass': 'Only caps/observes production HTTP; clocks/parsers/SQLite/attribution/scoring unchanged.',
        'entity_hints_verified': False, 'accuracy_or_guilt_validated': False, 'raw_article_bodies_exported': False},
        'initial_http_cache': cache_seed,
        'parameters': {'sources': sorted(selected), 'start': args.start, 'end': args.end, 'seconds': args.seconds,
                       'strategy': args.strategy, 'preset': args.preset,
                       'max_http_requests': args.max_requests, 'bodies_per_entity': args.bodies_per_entity},
        'cases': CASES, 'timings': {'acquisition': acquisition_seconds, 'attribution': attribution_seconds,
                                  'scoring': scoring_seconds}, 'http_events': events,
        'http_status_counts': dict(Counter(str(event['http_status']) for event in events)),
        'acquisition': acquisition, 'coverage': acquisition_coverage(engine.store, news.sources()),
        'attribution': attribution, 'scored_entities': scored, 'metadata_pages': pages,
        'retrieval_comparison_same_real_metadata': {'before': dict(old_counts), 'after': dict(new_counts)},
        'articles': article_report(engine), 'jobs': states, 'assessments': assessment_report(engine),
        'progress_events': len(progress), 'last_progress': progress[-1] if progress else None,
        'audit': engine.store.verify_audit(), 'source_code_sha256': {
            str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT / 'peps').glob('*.py')},
        'limitations': ['A bounded archive sample; no exhaustive history or recall estimate.',
            'Retrieval relevance does not prove identity, culpability or absence of risk.',
            'Positive/zero scores are heuristic pipeline outputs; unresolved identity needs review.']}
    report['checks'] = {'production_unchanged_during_run': hashes_before == report['source_code_sha256'],
                        'audit_ok': report['audit']['ok'],
                        'all_cases_scored': scored == len(CASES),
                        'all_current_runs_replay': all(row['replay_matched'] for row in report['assessments']),
                        'metadata_not_counted_as_body': all(not row['body_chars'] for row in report['articles']
                                                          if row['scope'] != 'full_article')}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'output': str(args.output), 'coverage': report['coverage'], 'timings': report['timings'],
                      'requests': len(events), 'http_statuses': report['http_status_counts'],
                      'retrieval_before': dict(old_counts), 'retrieval_after': dict(new_counts),
                      'audit_ok': report['audit']['ok']}, ensure_ascii=False))
    if not all(report['checks'].values()):
        raise SystemExit('Real validation integrity checks failed; inspect the report.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--online', action='store_true', help='Explicitly execute bounded public-source HTTP')
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--seconds', type=int, default=120)
    parser.add_argument('--max-requests', type=int, default=60)
    parser.add_argument('--bodies-per-entity', type=int, default=2)
    parser.add_argument('--start', default='2014-01-01')
    parser.add_argument('--end', default='2022-12-31')
    parser.add_argument('--sources', default='contralinea_index,aristegui_index,desinformemonos_index')
    parser.add_argument('--http-cache-seed', type=Path, help='Copy previous public-test HTTP cache/courtesy state into a new isolated bank')
    parser.add_argument('--strategy', choices=('shared', 'targeted'), default='shared')
    parser.add_argument('--preset', choices=('identity', 'adverse', 'public_role'), default='identity')
    args = parser.parse_args()
    if not args.online:
        parser.error('La validación real requiere --online y un banco aislado.')
    if not 1 <= args.seconds <= 600 or not 1 <= args.max_requests <= 120 or not 0 <= args.bodies_per_entity <= 3:
        parser.error('Use 1..600 segundos, 1..120 peticiones y 0..3 cuerpos por entidad.')
    real_run(args)


if __name__ == '__main__':
    main()
