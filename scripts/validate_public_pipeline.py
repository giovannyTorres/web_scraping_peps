"""Opt-in public web discovery followed by guarded local-cache reuse.

The live phase uses the real transport, clock, parsers and SQLite. Public names
are examples, not verified identities. The second phase forbids all DNS/TCP;
neither phase downloads article bodies or validates a risk assessment. Reports
contain counts, public URLs and hashes, never copied result descriptions.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
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
from peps.store import now


CASES = [
    {'id': 'epn', 'name': 'Enrique Peña Nieto', 'kind': 'person'},
    {'id': 'pemex', 'name': 'Pemex', 'kind': 'company'},
    {'id': 'bimbo', 'name': 'Grupo Bimbo', 'kind': 'company'},
]


def counts(store):
    with store.connection() as con:
        return {table: con.execute('SELECT count(*) FROM ' + table).fetchone()[0]
                for table in ('news_articles', 'documents', 'evidence', 'news_observations', 'score_runs')}


def validate(args):
    directory = args.data_dir.resolve()
    if (directory / 'bank.sqlite3').exists() or directory.exists() and any(directory.iterdir()):
        raise ValueError('Use un directorio vacío para no modificar bancos existentes')
    directory.mkdir(parents=True, exist_ok=True)
    engine = Engine(directory, online=True)
    engine.store.import_entities(CASES)
    config = json.loads((ROOT / 'peps/defaults/news_sources.json').read_text(encoding='utf-8'))
    selected = set(args.sources.split(','))
    config['sources'] = [source for source in config['sources'] if source['id'] in selected]
    if {source['id'] for source in config['sources']} != selected:
        raise ValueError('Fuente pública desconocida')
    for source in config['sources']:
        source.update(enabled=True, terms_approved=True, download_articles=False,
                      article_terms_approved=False, body_terms_approved=False,
                      timeout_seconds=12, robots_required=True)
    config['user_agent'] = 'WebScope-Public-Validation/2.9 (+https://github.com/giovannyTorres/web_scraping_peps)'
    config['search'].update(strategy='public', preset='identity', include_aliases=False,
                            execution_mode='quick', session_seconds=120, max_queries=30)
    (directory / 'news_sources.json').write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
    news = NewsAcquirer(engine)
    source_hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in (ROOT / 'peps').glob('*.py')}
    events, lock = [], threading.Lock()
    deadline = time.monotonic() + args.seconds

    class ObservedFetcher(SafeFetcher):
        def _transport(self, url, headers, timeout, max_bytes, addresses):
            with lock:
                if len(events) >= args.max_requests or time.monotonic() >= deadline:
                    raise CollectionError('Public validation transport budget exhausted')
                event = {'url': url, 'status': None}
                events.append(event)
            began = time.monotonic()
            try:
                status, response_headers, body = super()._transport(
                    url, headers, min(timeout, max(.001, deadline - began)), max_bytes, addresses)
                event.update(status=status, decoded_bytes=len(body))
                return status, response_headers, body
            except Exception as exc:
                event['error_type'] = type(exc).__name__
                raise
            finally:
                event['seconds'] = round(time.monotonic() - began, 6)

    print('Consultando buscadores públicos con transporte real…', flush=True)
    began = time.monotonic()
    with patch('peps.collector.SafeFetcher', ObservedFetcher):
        cold = news.acquire(workers=4, seconds=args.seconds, queue=True,
                            window='public-validation-cold', relevant_only=True,
                            per_entity_body_limit=0, body_budget=0, cache_mode='reuse')
    cold_seconds, cold_counts = time.monotonic() - began, counts(engine.store)
    print(f'Búsqueda real: {cold_seconds:.2f}s. Verificando caché con DNS/TCP prohibidos…', flush=True)
    engine.online = False
    began = time.monotonic()
    with patch('socket.getaddrinfo', side_effect=AssertionError('Unexpected DNS during local-only validation')) as dns, \
            patch('socket.socket.connect', side_effect=AssertionError('Unexpected TCP during local-only validation')) as tcp:
        warm = news.acquire(workers=4, seconds=30, queue=True,
                            window='public-validation-local', relevant_only=True,
                            per_entity_body_limit=0, body_budget=0, cache_mode='local')
    local_seconds, warm_counts = time.monotonic() - began, counts(engine.store)
    coverage = acquisition_coverage(engine.store, news.sources())
    after_hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in (ROOT / 'peps').glob('*.py')}
    with engine.store.connection() as con:
        articles = [dict(row) for row in con.execute('SELECT canonical_url,scope FROM news_articles ORDER BY canonical_url')]
    checks = {
        'actual_search_responses_observed': any(event['status'] == 200 and '/robots.txt' not in event['url'] for event in events),
        'no_dns_in_local_mode': dns.call_count == 0,
        'no_tcp_in_local_mode': tcp.call_count == 0,
        'local_mode_did_not_add_candidates': warm_counts['news_articles'] == cold_counts['news_articles'],
        'local_mode_did_not_duplicate_observations': warm_counts['news_observations'] == cold_counts['news_observations'],
        'no_article_bodies_or_evidence_created': all(not warm_counts[key] for key in ('documents', 'evidence', 'score_runs')),
        'canonical_urls_unique': len({row['canonical_url'] for row in articles}) == len(articles),
        'audit_ok': engine.store.verify_audit()['ok'],
        'production_unchanged_during_run': source_hashes == after_hashes,
    }
    report = {'recorded_at': now(), 'method': {
        'live_transport': True, 'response_substitution': False, 'public_examples_only': True,
        'body_download': False, 'identity_or_prediction_quality_validated': False,
        'local_phase': 'Real downloaded cache, DNS/TCP guarded; no synthesized HTTP responses'},
        'cases': CASES, 'sources': sorted(selected), 'checks': checks,
        'timings_seconds': {'live': cold_seconds, 'local': local_seconds},
        'http_status_counts': dict(Counter(str(event['status']) for event in events)),
        'http_events': events, 'live_acquisition': cold, 'local_acquisition': warm,
        'counts_before_local': cold_counts, 'counts_after_local': warm_counts,
        'coverage': coverage, 'articles': articles, 'source_code_sha256': source_hashes,
        'limitations': ['Bounded discovery sample; coverage and relevance require review.',
                       'Metadata excerpts are not article text or verified identity.',
                       'Local cache reflects previous acquisitions, not current internet coverage.']}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'output': str(args.output), 'checks': checks, 'timings': report['timings_seconds'],
                      'counts': warm_counts, 'http_statuses': report['http_status_counts']}, ensure_ascii=False))
    if not all(checks.values()):
        raise SystemExit('Public pipeline validation failed; inspect the report')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--online', action='store_true')
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--sources', default='mwmbl_web,duckduckgo_web')
    parser.add_argument('--seconds', type=int, default=90)
    parser.add_argument('--max-requests', type=int, default=24)
    args = parser.parse_args()
    if not args.online or not 1 <= args.seconds <= 180 or not 1 <= args.max_requests <= 40:
        parser.error('Requiere --online, 1..180 segundos y 1..40 solicitudes máximas')
    validate(args)


if __name__ == '__main__':
    main()
