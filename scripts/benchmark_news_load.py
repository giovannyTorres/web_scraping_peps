"""Reproducible local acquisition load benchmark with entirely simulated HTTP.

Exercises the real SafeFetcher caps, compression, caching and HTTP parser, plus
NewsAcquirer, SQLite jobs, provenance, deduplication and raw snapshots. DNS and
TLS sockets are fixtures: no network call is possible. A synthetic clock adds
0.2 seconds per fetch to elapsed monotonic time, respecting five requests/second
per host while allowing queued future jobs to become due even on cache hits.
Wall times measure local ingestion only and must never be projected to web speed.
"""
from __future__ import annotations

import argparse
import gzip
import http.client
import io
import json
import platform
import socket
import sys
import tempfile
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from peps.collector import SafeFetcher
from peps.engine import Engine
from peps.news import NewsAcquirer, article_text
from peps.store import canonical, digest

PUBLIC_DNS = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('93.184.216.34', 443))]
REAL_TIME = time.time
SOURCE_HASHES = {name: digest((Path(__file__).resolve().parents[1] / 'peps' / (name + '.py')).read_text(encoding='utf-8'))
                 for name in ('collector', 'news', 'news_body_quality', 'store', 'identity', 'sources', 'engine', 'scoring')}
SCRIPT_HASH = digest(Path(__file__).read_text(encoding='utf-8'))


class VirtualClock:
    def __init__(self):
        self.value = REAL_TIME()
        self.start = self.value
        self.real_started = time.monotonic()
        self.lock = threading.RLock()

    def now(self):
        with self.lock:
            return self.value + time.monotonic() - self.real_started

    def advance(self):
        with self.lock:
            self.value += .200001


class FixtureWire:
    def __init__(self, host, response_for, events, clock):
        self.host, self.response_for, self.events, self.clock = host, response_for, events, clock
        self.stream = io.BytesIO()
        self.closed = False

    def settimeout(self, _):
        pass

    def recv_into(self, buffer):
        value = self.stream.read(len(buffer))
        buffer[:len(value)] = value
        return len(value)

    def sendall(self, request):
        head = request.decode('iso-8859-1')
        path = head.split(' ', 2)[1]
        headers = dict(line.split(': ', 1) for line in head.split('\r\n')[1:] if ': ' in line)
        body, content_type = self.response_for(self.host, path)
        raw = body.encode('utf-8')
        etag = '"' + digest(body) + '"'
        status = 304 if headers.get('If-None-Match') == etag else 200
        compressed = gzip.compress(raw, mtime=0) if status == 200 else b''
        self.events.append({'url': 'https://' + self.host + path, 'virtual_at': self.clock.now(),
                            'host': self.host, 'status': status, 'wire_bytes': len(compressed),
                            'decoded_bytes': len(raw) if status == 200 else 0})
        response = (f'HTTP/1.1 {status} OK\r\nContent-Type: {content_type}; charset=utf-8\r\n'
                    f'Content-Length: {len(compressed)}\r\nETag: {etag}\r\n'
                    'Cache-Control: max-age=86400\r\nContent-Encoding: gzip\r\n\r\n').encode('ascii')
        self.stream = io.BytesIO(response + compressed)

    def close(self):
        self.closed = True


class FixtureConnection(http.client.HTTPSConnection):
    def __init__(self, hostname, addresses, timeout, context, response_for, events, clock):
        super().__init__(hostname, timeout=timeout, context=context)
        self._peer_address = addresses[0][4][0]
        self.response_for, self.events, self.clock = response_for, events, clock

    def connect(self):
        self.sock = FixtureWire(self.host, self.response_for, self.events, self.clock)


class FixtureFetcher(SafeFetcher):
    def __init__(self, config, state_dir, clock, offline=False):
        # Synthetic sockets below replace the direct TLS connection only.
        # Inherited runner proxies must never escape this no-network fixture.
        super().__init__(dict(config, trust_env_proxy=False), state_dir, offline=offline)
        self.clock = clock
        self._clock = clock.now
        self._dns = lambda *args, **kwargs: PUBLIC_DNS

    def fetch(self, url, source):
        # Only the instantaneous fixture HTTP stage is serialized; extraction and
        # durable ingestion run concurrently through the real worker pool.
        with self.clock.lock:
            self.clock.advance()
            return super().fetch(url, source)


def make_sources(size, feed_size):
    sources = []
    for publisher in range(2):
        slots = [i for i in range(size) if i % 2 == publisher]
        urls = [f'https://publisher-{publisher}.example/feed/{start}'
                for start in range(0, len(slots), feed_size)]
        sources.append({'id': f'fixture-{publisher}', 'publisher_id': f'publisher-{publisher}',
                        'kind': 'rss', 'urls': urls,
                        'allowed_hosts': [f'publisher-{publisher}.example'], 'enabled': True,
                        'terms_approved': True, 'robots_required': True, 'download_articles': True,
                        'article_terms_approved': True, 'max_records': 60000, 'max_items': 60000,
                        'max_bytes': 5_000_000, 'min_interval_seconds': .2,
                        'refresh_seconds': 86400, 'cache_ttl_seconds': 86400,
                        'minimum_article_chars': 80, 'terms_url': 'fixture:synthetic-permission'})
    return sources


def response_factory(size, feed_size):
    def response_for(host, path):
        if path == '/robots.txt':
            return 'User-agent: *\nAllow: /\n', 'text/plain'
        publisher = int(host.split('-')[1].split('.')[0])
        slots = [i for i in range(size) if i % 2 == publisher]
        if path.startswith('/feed/'):
            start = int(path.rsplit('/', 1)[1])
            entries = []
            for index in slots[start:start + feed_size]:
                group = index // 10
                title = f'Informe municipal grupo{group:05d} Persona Sintética {group:05d}'
                url = f'https://{host}/article/{index}'
                item = f'<item><title>{title}</title><link>{url}</link><description>Persona Sintética {group:05d} presentó su informe neutral.</description></item>'
                entries.append(item)
                if index % 10 == 0:
                    entries.append(item.replace('</link>', '?utm_source=duplicate</link>'))
            return '<rss><channel>' + ''.join(entries) + '</channel></rss>', 'application/rss+xml'
        index = int(path.rsplit('/', 1)[1])
        group = index // 10
        title = f'Informe municipal grupo{group:05d} Persona Sintética {group:05d}'
        text = (f'Persona Sintética {group:05d} presentó un informe neutral del grupo {group:05d}. '
                'Esta información completamente inventada sirve exclusivamente para pruebas técnicas. ') * 4
        return f'<html><head><title>{title}</title></head><article><p>{text}</p></article></html>', 'text/html'
    return response_for


def difference(after, before, key):
    return after.get(key, 0) - before.get(key, 0)


def audit_url_captures(engine, sources):
    """Verify every URL's local original and version-bound alias provenance.

    One snapshot file may encode identical HTML at several publishers; the
    separate URL receipts must still exist, identify the effective source and
    resolve to exactly the original Unicode body, rather than another version.
    This verification uses captured bytes only and is outside timed phases.
    """
    with engine.store.connection() as con:
        rows = [dict(row) for row in con.execute('SELECT * FROM news_articles')]
        documents = {row['id']: dict(row) for row in con.execute('SELECT * FROM documents')}
        quality_receipts = {row['article_id']: dict(row) for row in con.execute('SELECT * FROM news_body_quality_receipts')}
        extraction_receipts = {row['article_id']: dict(row) for row in con.execute('SELECT * FROM news_extraction_receipts')}
        has_aliases = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='news_body_aliases'").fetchone()
        aliases = [dict(row) for row in con.execute('SELECT * FROM news_body_aliases')] if has_aliases else []
    by_article = {}
    for alias in aliases:
        by_article.setdefault(alias['article_id'], []).append(alias)
    source_by_id = {source['id']: source for source in sources}
    valid, resolved, publisher_clusters = 0, {}, {}
    raw_root = (engine.data_dir / 'news_snapshots').resolve()
    provenance_ok = versions_ok = receipts_ok = True
    for row in rows:
        if row['scope'] == 'full_article' and row['document_id'] in documents:
            doc = documents[row['document_id']]
            receipt = json.loads(doc['metadata'])
            policy = receipt.get('source_policy', {})
            capture = receipt
            extraction = extraction_receipts.get(row['id'])
            if (extraction is None or extraction['document_id'] != doc['id']
                    or extraction['raw_snapshot_sha256'] != receipt.get('raw_snapshot_sha256')
                    or extraction['version'] != receipt.get('extraction_version')):
                receipts_ok = False
                continue
        elif row['scope'] == 'duplicate_reference' and row['document_id'] is None:
            captures = by_article.get(row['id'], [])
            if len(captures) != 1:
                provenance_ok = False
                continue
            receipt = captures[0]
            doc = documents.get(receipt['canonical_document_id'])
            if doc is None:
                versions_ok = False
                continue
            classification = json.loads(receipt['classification'])
            policy = json.loads(receipt['source_policy'])
            capture = classification.get('capture_metadata', {})
            quality = quality_receipts.get(row['id'])
            if (quality is None or quality['raw_snapshot_sha256'] != receipt['raw_snapshot_sha256']
                    or quality['raw_snapshot_path'] != receipt['raw_snapshot_path']):
                receipts_ok = False
                continue
            quality_classification = json.loads(quality['classification'])
            if (quality_classification.get('status') != 'duplicate_reference'
                    or quality_classification.get('canonical_document_id') != doc['id']
                    or quality_classification.get('body_sha256') != doc['content_hash']):
                receipts_ok = False
                continue
            if (receipt['body_sha256'] != doc['content_hash']
                    or classification.get('body_sha256') != doc['content_hash']
                    or classification.get('canonical_document_id') != doc['id']):
                versions_ok = False
                continue
            if receipt['observed_url'] != row['canonical_url']:
                provenance_ok = False
                continue
        else:
            provenance_ok = False
            continue
        expected_source = source_by_id.get(row['source_id'])
        if (expected_source is None or policy.get('id') != row['source_id']
                or policy.get('publisher_id') != row['publisher_id']
                or urlsplit(row['canonical_url']).hostname not in expected_source.get('allowed_hosts', [])
                or policy.get('article_terms_approved') is not True
                or capture.get('source_config_hash') != digest(canonical(expected_source))):
            provenance_ok = False
            continue
        if digest(doc['text']) != doc['content_hash']:
            versions_ok = False
            continue
        try:
            path = (engine.data_dir / receipt['raw_snapshot_path']).resolve()
            if not path.is_relative_to(raw_root) or path.name != receipt['raw_snapshot_sha256'] + '.html.gz':
                raise ValueError('Captured HTML path outside snapshot root')
            html = gzip.decompress(path.read_bytes()).decode('utf-8')
            if digest(html) != receipt['raw_snapshot_sha256'] or article_text(html) != doc['text']:
                raise ValueError('Captured original does not match its canonical body version')
        except (OSError, ValueError, EOFError, UnicodeError):
            provenance_ok = False
            continue
        valid += 1
        resolved[row['canonical_url']] = doc['id']
        publisher_clusters.setdefault((row['publisher_id'], row['content_cluster']), set()).add(doc['id'])
    return {'verified_url_captures': valid, 'canonical_bodies': sum(row['scope'] == 'full_article' for row in rows),
            'alias_captures': len(aliases), 'publisher_ids': sorted({row['publisher_id'] for row in rows}),
            'resolved_url_documents': resolved,
            'effective_source_provenance_valid': provenance_ok,
            'conservation_receipts_valid': receipts_ok,
            'immutable_body_versions_valid': versions_ok,
            'same_publisher_cluster_has_one_canonical_body': all(len(ids) == 1 for ids in publisher_clusters.values()),
            'all_document_hashes_unique': len(documents) == len({doc['content_hash'] for doc in documents.values()})}


def run_case(size, workers, bank_size=40000, feed_size=400, seconds=1800):
    clock, events, created = VirtualClock(), [], []
    with tempfile.TemporaryDirectory(prefix='peps-news-load-') as directory:
        engine = Engine(directory, online=True)
        news = NewsAcquirer(engine)
        sources = make_sources(size, feed_size)
        news.config_path.write_text(json.dumps({'sources': sources, 'search': {'strategy': 'feeds'}, 'user_agent': 'PEPS synthetic fixture',
                                                'cache_ttl_seconds': 86400}), encoding='utf-8')
        bank_started = time.perf_counter()
        engine.store.import_entities({'id': f'synthetic-{i:05d}', 'name': f'Persona Sintética {i:05d}'}
                                     for i in range(bank_size))
        bank_seconds = time.perf_counter() - bank_started
        response_for = response_factory(size, feed_size)
        def fetcher_factory(config, state_dir, offline=False):
            fetcher = FixtureFetcher(config, state_dir, clock, offline)
            created.append(fetcher)
            # Prime robots through the real request boundary, before article/feed
            # GETs; this avoids substituting sleeps with a safety bypass.
            for source in sources:
                host = source['allowed_hosts'][0]
                with clock.lock:
                    clock.advance()
                    decision = fetcher._robots_allowed('https://' + host + '/probe', dict(source))
                    if decision:
                        raise AssertionError(decision)
            return fetcher
        def connection_factory(hostname, addresses, timeout, context):
            return FixtureConnection(hostname, addresses, timeout, context, response_for, events, clock)
        with patch('peps.collector.SafeFetcher', side_effect=fetcher_factory), \
             patch('peps.collector._PinnedHTTPSConnection', side_effect=connection_factory), \
             patch('peps.news.time.time', side_effect=clock.now):
            before_events = len(events)
            started = time.perf_counter()
            cold = news.acquire(workers=workers, seconds=seconds, max_articles=60000,
                                download_all=True, window='fixture-cold')
            cold_wall = time.perf_counter() - started
            cold_events = list(events[before_events:])
            cold_jobs = dict(engine.store.stats()['jobs'])
            before_events = len(events)
            started = time.perf_counter()
            warm = news.acquire(workers=workers, seconds=seconds, max_articles=60000,
                                download_all=True, window='fixture-warm')
            warm_wall = time.perf_counter() - started
            warm_events = list(events[before_events:])
            warm_jobs = dict(engine.store.stats()['jobs'])
        stats = news.stats()
        durable = engine.store.stats()
        expected_clusters = (size + 9) // 10
        expected_documents = expected_clusters
        captures = audit_url_captures(engine, sources)
        expected_urls = {f'https://publisher-{i % 2}.example/article/{i}' for i in range(size)}
        # An expired preparation budget can stop before any fetcher exists.
        # Keep that incomplete phase measurable instead of raising IndexError.
        all_metrics = created[-1].metrics_snapshot() if created else {
            'network_requests': 0, 'wire_bytes': 0, 'decoded_bytes': 0}
        request_by_host = {}
        for event in events:
            request_by_host.setdefault(event['host'], []).append(event['virtual_at'])
        minimum_spacing = min((b - a for values in request_by_host.values()
                               for a, b in zip(values, values[1:])), default=None)
        def phase_completed(result, jobs):
            pending = sum(jobs.get(state, 0) for state in ('queued', 'running', 'deferred'))
            return (not result.get('preparation_incomplete') and result.get('queue_complete') is True
                    and result.get('stop_reason') == 'completed' and result.get('pending') == 0
                    and not pending and not result.get('failed') and not result.get('cancelled'))
        cold_complete = phase_completed(cold, cold_jobs)
        warm_complete = phase_completed(warm, warm_jobs)
        checks = {'all_distinct_urls_retained': stats['articles'] == size and set(captures['resolved_url_documents']) == expected_urls,
                  'all_original_url_captures_verified': captures['verified_url_captures'] == size,
                  'canonical_and_alias_counts_match': (captures['canonical_bodies'] == expected_documents
                      and captures['alias_captures'] == size - expected_documents
                      and stats['status'].get('fetched', 0) == expected_documents
                      and stats['status'].get('duplicate_reference', 0) == size - expected_documents),
                  'same_publisher_content_deduplicated': captures['same_publisher_cluster_has_one_canonical_body'],
                  'global_exact_bodies_unique': durable['documents'] == expected_documents and captures['all_document_hashes_unique'],
                  'cross_publisher_capture_provenance_preserved': captures['effective_source_provenance_valid'],
                  'alias_document_versions_immutable': captures['immutable_body_versions_valid'],
                  'all_url_conservation_receipts_valid': captures['conservation_receipts_valid'],
                  'shared_content_clusters': stats['content_clusters'] == expected_clusters,
                  'warm_downloads_zero': len(warm_events) == 0,
                  'cold_phase_completed': cold_complete,
                  'warm_phase_completed': warm_complete,
                  'no_pending_jobs': sum(durable['jobs'].get(state, 0)
                                         for state in ('queued', 'running', 'deferred')) == 0,
                  'no_failed_jobs': durable['jobs'].get('dead', 0) + durable['jobs'].get('blocked', 0) + durable['jobs'].get('deferred', 0) == 0,
                  'virtual_host_rate_respected': minimum_spacing is not None and minimum_spacing >= .19999,
                  'request_metrics_match_fixture': all_metrics['network_requests'] == len(events),
                  'wire_metrics_match_fixture': all_metrics['wire_bytes'] == sum(e['wire_bytes'] for e in events),
                  'decoded_metrics_match_fixture': all_metrics['decoded_bytes'] == sum(e['decoded_bytes'] for e in events),
                  'audit_chain_valid': engine.store.verify_audit()['ok']}
        phase = lambda result, wall, phase_events, jobs, complete: {
            'wall_seconds': round(wall, 6), 'processed_jobs': result['processed'],
            'completed': complete, 'stop_reason': result.get('stop_reason'),
            'preparation_incomplete': bool(result.get('preparation_incomplete')),
            'queue_complete': result.get('queue_complete'),
            'pending_jobs': sum(jobs.get(state, 0) for state in ('queued', 'running', 'deferred')),
            'job_status_counts': jobs,
            'unique_articles_per_local_second': round(size / wall, 3) if complete else None,
            'throughput_basis': 'retained articles per local phase second, only when every phase job completed; warm uses cached metadata, not body downloads',
            'simulated_http_requests': len(phase_events),
            'article_requests': sum('/article/' in e['url'] for e in phase_events),
            'wire_bytes': sum(e['wire_bytes'] for e in phase_events),
            'decoded_bytes': sum(e['decoded_bytes'] for e in phase_events)}
        return {'urls': size, 'entity_bank': bank_size, 'workers': workers,
                'bank_import_seconds': round(bank_seconds, 6),
                'cold': phase(cold, cold_wall, cold_events, cold_jobs, cold_complete),
                'warm': phase(warm, warm_wall, warm_events, warm_jobs, warm_complete), 'bank': stats,
                'documents': durable['documents'], 'expected_documents': expected_documents,
                'capture_audit': {key: value for key, value in captures.items() if key != 'resolved_url_documents'},
                'expected_content_clusters': expected_clusters,
                'virtual_clock_elapsed_seconds': round(clock.now() - clock.start, 4),
                'minimum_virtual_request_spacing_seconds': minimum_spacing,
                'metrics': all_metrics, 'checks': checks, 'passed': all(checks.values()),
                'database_bytes': engine.store.path.stat().st_size,
                'snapshot_files': len(list((Path(directory) / 'news_snapshots').rglob('*.gz')))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases', nargs='+', default=['1000:1', '1000:4', '10000:4'], help='URL count:workers')
    parser.add_argument('--bank-size', type=int, default=40000)
    parser.add_argument('--seconds-per-phase', type=float, default=1800)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    cases = []
    for spec in args.cases:
        size, workers = map(int, spec.split(':'))
        if not 1 <= size <= 10000 or not 1 <= workers <= 32:
            raise ValueError('cases must contain 1-10000 URLs and 1-32 workers')
        result = run_case(size, workers, args.bank_size, seconds=args.seconds_per_phase)
        cases.append(result)
        print(json.dumps({'case': spec, 'cold_seconds': result['cold']['wall_seconds'],
                          'warm_seconds': result['warm']['wall_seconds'], 'passed': result['passed']}, ensure_ascii=False), flush=True)
        report = {'generated_at': datetime.now(timezone.utc).isoformat(),
                  'benchmark_kind': 'local_pipeline_with_simulated_http_not_real_web',
                  'platform': platform.platform(), 'python': platform.python_version(),
                  'network_access': False, 'configured_requests_per_second_per_host': 5,
                  'max_job_budget': 60000, 'all_passed': all(c['passed'] for c in cases), 'cases': cases,
                  'seconds_per_phase_budget': args.seconds_per_phase,
                  'clock_model': 'real monotonic elapsed seconds plus 0.200001 synthetic seconds per fetch; no real HTTP',
                  'source_sha256_at_process_start': SOURCE_HASHES, 'script_sha256': SCRIPT_HASH,
                  'limitations': ['Synthetic fetch time removes network waiting; monotonic time still advances queued courtesy deadlines. Measured seconds only describe local ingestion.',
                                  'DNS/TLS endpoints and HTTP payloads are synthetic fixtures, not evidence of web throughput or coverage.',
                                  'Every publisher URL retains captured HTML and effective source provenance; exact duplicate text shares one immutable body globally and other URLs remain explicit references.',
                                  'This run exercises acquisition only; identity attribution and reputational scoring are separate.']}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0 if all(c['passed'] for c in cases) else 1


if __name__ == '__main__':
    raise SystemExit(main())
