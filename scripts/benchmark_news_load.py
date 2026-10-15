"""Reproducible local acquisition load benchmark with entirely simulated HTTP.

Exercises the real SafeFetcher caps, compression, caching and HTTP parser, plus
NewsAcquirer, SQLite jobs, provenance, deduplication and raw snapshots. DNS and
TLS sockets are fixtures: no network call is possible. A virtual clock advances
0.2 seconds per HTTP fetch, respecting a configured five requests/second/host.
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from peps.collector import SafeFetcher
from peps.engine import Engine
from peps.news import NewsAcquirer
from peps.store import digest

PUBLIC_DNS = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('93.184.216.34', 443))]
REAL_TIME = time.time
SOURCE_HASHES = {name: digest((Path(__file__).resolve().parents[1] / 'peps' / (name + '.py')).read_text(encoding='utf-8'))
                 for name in ('collector', 'news', 'store', 'identity', 'sources', 'engine', 'scoring')}
SCRIPT_HASH = digest(Path(__file__).read_text(encoding='utf-8'))


class VirtualClock:
    def __init__(self):
        self.value = REAL_TIME()
        self.start = self.value
        self.lock = threading.RLock()

    def now(self):
        with self.lock:
            return self.value

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
        super().__init__(config, state_dir, offline=offline)
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


def run_case(size, workers, bank_size=40000, feed_size=400, seconds=1800):
    clock, events, created = VirtualClock(), [], []
    with tempfile.TemporaryDirectory(prefix='peps-news-load-') as directory:
        engine = Engine(directory, online=True)
        news = NewsAcquirer(engine)
        sources = make_sources(size, feed_size)
        news.config_path.write_text(json.dumps({'sources': sources, 'user_agent': 'PEPS synthetic fixture',
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
            before_events = len(events)
            started = time.perf_counter()
            warm = news.acquire(workers=workers, seconds=seconds, max_articles=60000,
                                download_all=True, window='fixture-warm')
            warm_wall = time.perf_counter() - started
            warm_events = list(events[before_events:])
        stats = news.stats()
        durable = engine.store.stats()
        expected_clusters = (size + 9) // 10
        expected_documents = sum(len({i % 2 for i in range(start, min(start + 10, size))})
                                 for start in range(0, size, 10))
        all_metrics = created[-1].metrics_snapshot()
        request_by_host = {}
        for event in events:
            request_by_host.setdefault(event['host'], []).append(event['virtual_at'])
        minimum_spacing = min((b - a for values in request_by_host.values()
                               for a, b in zip(values, values[1:])), default=None)
        checks = {'all_distinct_urls_retained': stats['articles'] == size,
                  'all_full_articles_fetched': stats['status'] == {'fetched': size},
                  'same_publisher_content_deduplicated': durable['documents'] == expected_documents,
                  'cross_publisher_documents_preserved': durable['documents'] == expected_documents,
                  'shared_content_clusters': stats['content_clusters'] == expected_clusters,
                  'warm_downloads_zero': len(warm_events) == 0,
                  'no_failed_jobs': durable['jobs'].get('dead', 0) + durable['jobs'].get('blocked', 0) + durable['jobs'].get('deferred', 0) == 0,
                  'virtual_host_rate_respected': minimum_spacing is not None and minimum_spacing >= .19999,
                  'request_metrics_match_fixture': all_metrics['network_requests'] == len(events),
                  'wire_metrics_match_fixture': all_metrics['wire_bytes'] == sum(e['wire_bytes'] for e in events),
                  'decoded_metrics_match_fixture': all_metrics['decoded_bytes'] == sum(e['decoded_bytes'] for e in events),
                  'audit_chain_valid': engine.store.verify_audit()['ok']}
        phase = lambda result, wall, phase_events: {
            'wall_seconds': round(wall, 6), 'processed_jobs': result['processed'],
            'unique_articles_per_local_second': round(size / wall, 3),
            'simulated_http_requests': len(phase_events),
            'article_requests': sum('/article/' in e['url'] for e in phase_events),
            'wire_bytes': sum(e['wire_bytes'] for e in phase_events),
            'decoded_bytes': sum(e['decoded_bytes'] for e in phase_events)}
        return {'urls': size, 'entity_bank': bank_size, 'workers': workers,
                'bank_import_seconds': round(bank_seconds, 6), 'cold': phase(cold, cold_wall, cold_events),
                'warm': phase(warm, warm_wall, warm_events), 'bank': stats,
                'documents': durable['documents'], 'expected_documents': expected_documents,
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
                  'source_sha256_at_process_start': SOURCE_HASHES, 'script_sha256': SCRIPT_HASH,
                  'limitations': ['Virtual time removes network and courtesy waiting; measured seconds only describe local ingestion.',
                                  'DNS/TLS endpoints and HTTP payloads are synthetic fixtures, not evidence of web throughput or coverage.',
                                  'Distinct publisher copies survive; repeated normalized content within a publisher shares one document.',
                                  'This run exercises acquisition only; identity attribution and reputational scoring are separate.']}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0 if all(c['passed'] for c in cases) else 1


if __name__ == '__main__':
    raise SystemExit(main())
