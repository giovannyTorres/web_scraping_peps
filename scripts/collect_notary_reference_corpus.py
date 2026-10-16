"""Collect a frozen, closed reference corpus for extraction/classification tests.

This arm receives known URLs. It measures neither native discovery nor search
recall, and never supplies these URLs to the native acquisition pipeline. The
only HTTP implementation and body extractor are the production SafeFetcher and
article_text. Bodies and HTML snapshots remain in a private ignored runtime
directory; the report contains paths, counts, metadata, and hashes.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
import gzip
import hashlib
import heapq
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from peps.collector import SafeFetcher
from peps.news import ARTICLE_EXTRACTION_VERSION, article_text, canonical_url, source_policy
from peps.sources import parse_results

DENIED_BODY_DOMAINS = frozenset({'periodicocentral.mx', 'alcaldesdemexico.com'})
REPORT_KIND = 'closed_reference_corpus'


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _utc():
    return datetime.now(timezone.utc).isoformat()


def _body_host_denied(host):
    host = SafeFetcher._hostname(host)
    return any(host == domain or host.endswith('.' + domain) for domain in DENIED_BODY_DOMAINS)


def _rows(value, keys):
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in keys:
            if isinstance(value.get(key), list):
                return value[key]
    raise ValueError('Expected a JSON list or an object with one of: ' + ', '.join(keys))


def load_inputs(references_path, cohort_path, references_hash, cohort_hash):
    """Validate frozen inputs before creating output or making requests."""
    references_raw, cohort_raw = Path(references_path).read_bytes(), Path(cohort_path).read_bytes()
    for actual, expected, label in ((_sha(references_raw), references_hash, 'references'),
                                    (_sha(cohort_raw), cohort_hash, 'cohort')):
        if not re.fullmatch(r'[0-9a-f]{64}', expected or '') or actual != expected:
            raise ValueError(label + ' SHA256 does not match its frozen manifest')
    references = _rows(json.loads(references_raw), ('references', 'reference_articles', 'articles', 'gold'))
    cohort = _rows(json.loads(cohort_raw), ('entities', 'cohort', 'persons', 'cases'))
    if not cohort or any(not isinstance(row, dict) or not isinstance(row.get('id'), str) or not row['id'] for row in cohort):
        raise ValueError('Cohort requires nonempty unique string entity IDs')
    identifiers = {row['id'] for row in cohort}
    if len(identifiers) != len(cohort):
        raise ValueError('Duplicate entity IDs in frozen cohort')
    if not references:
        raise ValueError('Frozen reference list is empty')
    jobs = {}
    for number, row in enumerate(references):
        if not isinstance(row, dict):
            raise ValueError('Every reference must be an object')
        entity_ids = row.get('entity_ids') or [row.get('entity_id')]
        if (not isinstance(entity_ids, list) or not entity_ids
                or any(identifier not in identifiers for identifier in entity_ids)):
            raise ValueError('Reference has unknown or missing entity IDs at row ' + str(number))
        url = canonical_url(row.get('url'))
        parts = urlsplit(url)
        if parts.scheme != 'https' or parts.port not in (None, 443):
            raise ValueError('Reference URL must use public HTTPS port443')
        item = jobs.setdefault(url, {'url': url, 'entity_ids': [], 'references': [], 'order': len(jobs)})
        item['entity_ids'] = sorted(set(item['entity_ids']) | set(entity_ids))
        item['references'].append({'reference_index': number, 'reference_id': row.get('reference_id', row.get('id')),
                                   'entity_ids': entity_ids, 'title': row.get('title'),
                                   'published_at': row.get('published_at')})
    return cohort, references, list(jobs.values())


def select_source(url, sources, timeout=12):
    host = (urlsplit(url).hostname or '').lower()
    if _body_host_denied(host):
        return None, 'metadata_only_source_policy'
    for original in sources:
        hosts = [SafeFetcher._hostname(value) for value in
                 (original.get('article_allowed_hosts') or original.get('allowed_hosts') or [])]
        if host not in hosts:
            continue
        if (not original.get('enabled') or not original.get('terms_approved')
                or not original.get('download_articles')
                or not (original.get('article_terms_approved') or original.get('body_terms_approved'))
                or original.get('discovery_only')):
            continue
        safe_hosts = [value for value in hosts if not _body_host_denied(value)]
        source = dict(original, allowed_hosts=safe_hosts, robots_required=True,
                      min_interval_seconds=max(5, float(original.get('min_interval_seconds', 5))),
                      timeout_seconds=min(timeout, float(original.get('timeout_seconds', timeout))),
                      max_bytes=min(2_000_000, int(original.get('max_bytes', 2_000_000))))
        return source, None
    return None, 'no_explicitly_approved_article_source'


class BudgetStop(RuntimeError):
    pass


class BoundedFetcher(SafeFetcher):
    """Reserve the global request budget before every actual transport call."""
    def __init__(self, config, state_dir, *, deadline, max_requests):
        super().__init__(config, state_dir)
        self.deadline, self.max_requests = deadline, max_requests
        self.events, self.event_lock = [], threading.Lock()

    def _transport(self, url, headers, timeout, max_bytes, addresses):
        with self.event_lock:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0 or len(self.events) >= self.max_requests:
                raise BudgetStop('Closed reference corpus request/time budget exhausted before transport')
            event = {'url': url, 'started_at': _utc(), 'http_status': None}
            self.events.append(event)
        began = time.monotonic()
        try:
            status, response_headers, body = super()._transport(
                url, headers, min(timeout, remaining), max_bytes, addresses)
            event.update(http_status=status, decoded_bytes=len(body), transport_sha256=_sha(body))
            return status, response_headers, body
        except Exception as exc:
            event['error_type'] = type(exc).__name__
            raise
        finally:
            event['seconds'] = round(time.monotonic() - began, 6)


def _write_private(path, data):
    """Content-addressed immutable output, including concurrent identical bodies."""
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.corpus-', dir=path.parent)
    temporary = Path(temporary)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != data:
                raise ValueError('Immutable content-addressed output collision: ' + str(path))
    finally:
        temporary.unlink(missing_ok=True)


def _utc_day(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).date().isoformat()
    except (ValueError, TypeError, OverflowError):
        return None


def _publication_quality(observed, references):
    reference_values = [row['published_at'] for row in references if row.get('published_at')]
    dates = sorted({day for value in reference_values if (day := _utc_day(value)) is not None})
    if not observed:
        return 'unknown_observed_date'
    if not dates:
        return 'invalid_reference_date' if reference_values else 'unknown_reference_date'
    return 'matches_reference_day' if _utc_day(observed) in dates else 'conflicts_reference_day'


class _ExplicitAccessPolicy(HTMLParser):
    """Read declared schema/microdata access flags without executing scripts."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.restricted, self.stack = False, []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        names = ' '.join(str(values.get(key) or '') for key in ('itemprop', 'property', 'name')).split()
        access = any(name.rsplit('/', 1)[-1].casefold() == 'isaccessibleforfree' for name in names)
        if access and str(values.get('content', values.get('value', ''))).strip().casefold() == 'false':
            self.restricted = True
        if tag not in ('meta', 'link', 'img', 'br', 'hr', 'input', 'source', 'wbr', 'area', 'base', 'embed', 'param', 'track'):
            self.stack.append((tag, access))

    def handle_endtag(self, tag):
        for position in range(len(self.stack) - 1, -1, -1):
            if self.stack[position][0] == tag:
                del self.stack[position:]
                break

    def handle_data(self, text):
        if text.strip().casefold() == 'false' and any(access for _, access in self.stack):
            self.restricted = True


def _has_explicit_paywall(html):
    if re.search(r'["\']isAccessibleForFree["\']\s*:\s*(?:false|["\']false["\'])', html, re.I):
        return True
    parser = _ExplicitAccessPolicy()
    parser.feed(html)
    parser.close()
    return parser.restricted


def extract_article(result, source, references, directory):
    """Report editorial quality separately from transport or reference truth."""
    html = result.get('body') or ''
    raw_hash = _sha(html.encode('utf-8'))
    snapshot = directory / 'snapshots' / raw_hash[:2] / (raw_hash + '.html.gz')
    _write_private(snapshot, gzip.compress(html.encode('utf-8'), mtime=0))
    receipt = {'raw_snapshot_sha256': raw_hash, 'raw_snapshot_path': str(snapshot.resolve()),
               'transport_response_sha256': result.get('sha256'), 'extraction_version': ARTICLE_EXTRACTION_VERSION,
               'body_chars': 0, 'body_sha256': None, 'text_path': None}
    mime = (result.get('content_type') or '').split(';', 1)[0].strip().lower()
    if mime not in ('text/html', 'application/xhtml+xml'):
        return dict(receipt, status='rejected_article_quality', quality_reason='non_html_original')
    # Explicit publisher access metadata stops this arm. No alternate URL,
    # cached mirror, login, script execution or hidden body is attempted.
    if _has_explicit_paywall(html):
        return dict(receipt, status='rejected_article_quality', quality_reason='explicit_paywall_metadata')
    pages = parse_results(dict(result), dict(source, kind='news'))
    page = pages[0] if pages else {}
    body = article_text(html)
    receipt.update(title=page.get('title') or '', published_at=page.get('published_at'),
                   publication_date_quality=_publication_quality(page.get('published_at'), references),
                   body_chars=len(body))
    minimum = max(80, int(source.get('minimum_article_chars', 80)))
    if len(body) < minimum:
        return dict(receipt, status='rejected_article_quality', quality_reason='insufficient_editorial_body',
                    minimum_article_chars=minimum)
    body_hash = _sha(body.encode('utf-8'))
    text_path = directory / 'texts' / body_hash[:2] / (body_hash + '.txt')
    _write_private(text_path, body.encode('utf-8'))
    return dict(receipt, status='collected', quality_reason=None, body_sha256=body_hash,
                text_path=str(text_path.resolve()))


def _retry_timestamp(value):
    try:
        return float(value) if isinstance(value, (int, float)) else datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None


def collect(args, *, fetcher_class=BoundedFetcher):
    cohort, references, jobs = load_inputs(args.references, args.cohort, args.references_sha256, args.cohort_sha256)
    config_raw = Path(args.sources).read_bytes()
    config = json.loads(config_raw)
    sources = _rows(config, ('sources',))
    implementation_paths = [Path(__file__), ROOT / 'peps/collector.py', ROOT / 'peps/news.py', ROOT / 'peps/sources.py']
    implementation_before = {str(path.relative_to(ROOT)): _sha(path.read_bytes()) for path in implementation_paths}
    directory = args.output_dir.resolve()
    if not directory.is_relative_to(ROOT / 'runtime'):
        raise ValueError('Private corpus output must be inside the ignored repository runtime directory')
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    config = dict(config) if isinstance(config, dict) else {}
    config.update(user_agent='WebScope-Closed-Notary-Reference-Backtest/2.9 (+https://github.com/giovannyTorres/web_scraping_peps)',
                  max_redirects=min(4, int(config.get('max_redirects', 4))))
    began, began_at = time.monotonic(), _utc()
    deadline = began + args.seconds
    fetcher = fetcher_class(config, directory / 'network', deadline=deadline, max_requests=args.max_requests)
    pending, receipts, inflight, active_hosts, attempts = [], {}, {}, set(), Counter()
    for job in jobs:
        source, reason = select_source(job['url'], sources, args.timeout)
        if source is None:
            receipts[job['url']] = dict(job, status='skipped_source_policy', policy_reason=reason, attempts=[])
            continue
        job.update(source=source, host=urlsplit(job['url']).hostname, attempts=[])
        heapq.heappush(pending, (began, job['order'], job))

    def worker(job):
        result = fetcher.fetch(job['url'], dict(job['source'], _deadline_monotonic=deadline))
        attempt = {key: result.get(key) for key in ('status', 'http_status', 'error', 'error_code', 'retry_at',
                                                   'network_requests', 'elapsed_seconds', 'cache_status')}
        if result.get('status') in ('ok', 'not_modified'):
            quality = extract_article(result, job['source'], job['references'], directory)
            quality.update(final_url=result.get('final_url') or job['url'], retrieved_at=result.get('retrieved_at'))
            return result, attempt, quality
        return result, attempt, None

    stopped, last_progress = None, 0
    try:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            while pending or inflight:
                current = time.monotonic()
                if current >= deadline:
                    stopped = 'wall_time_budget'
                if len(fetcher.events) >= args.max_requests:
                    stopped = stopped or 'http_request_budget'
                if not stopped:
                    deferred = []
                    while pending and len(inflight) < args.workers and pending[0][0] <= current:
                        due, order, job = heapq.heappop(pending)
                        if job['host'] in active_hosts:
                            deferred.append((due, order, job)); continue
                        active_hosts.add(job['host'])
                        inflight[pool.submit(worker, job)] = job
                    for item in deferred:
                        heapq.heappush(pending, item)
                if not inflight:
                    if stopped:
                        break
                    next_due = pending[0][0] if pending else deadline
                    if next_due >= deadline:
                        stopped = 'source_wait_exceeds_session_budget'; break
                    time.sleep(min(.25, max(.001, next_due - time.monotonic())))
                    continue
                done, _ = wait(inflight, timeout=.25, return_when=FIRST_COMPLETED)
                for future in done:
                    job = inflight.pop(future)
                    active_hosts.remove(job['host'])
                    base = {key: job[key] for key in ('url', 'entity_ids', 'references', 'order', 'attempts')}
                    base.update(source_id=job['source']['id'], source_policy=source_policy(job['source']))
                    try:
                        result, attempt, quality = future.result()
                    except BudgetStop:
                        receipts[job['url']] = dict(base, status='not_collected_budget', budget_reason='before_transport_budget_guard')
                        continue
                    except Exception as exc:
                        receipts[job['url']] = dict(base, status='collector_exception', error_type=type(exc).__name__, error=str(exc)[:500])
                        continue
                    job['attempts'].append(attempt)
                    if quality is not None:
                        receipts[job['url']] = dict(base, **quality)
                    else:
                        retry = _retry_timestamp(result.get('retry_at'))
                        if result.get('status') == 'deferred' and retry is not None and not stopped:
                            # Keep source/robots backoff intact. Network errors
                            # receive at most one retry; local courtesy deferrals
                            # do not consume that retry allowance.
                            if result.get('error_code') not in ('host_interval', 'robots_pending'):
                                attempts[job['url']] += 1
                            if attempts[job['url']] <= 1:
                                due = time.monotonic() + max(.05, retry - time.time())
                                heapq.heappush(pending, (due, job['order'], job)); continue
                        receipts[job['url']] = dict(base, status='not_collected_transport',
                                                   transport_status=result.get('status'), error=result.get('error'),
                                                   http_status=result.get('http_status'), retry_at=result.get('retry_at'))
                if len(receipts) >= last_progress + 20:
                    last_progress = len(receipts)
                    print(json.dumps({'arm': REPORT_KIND, 'completed_urls': len(receipts), 'total_urls': len(jobs),
                                      'transport_attempts': len(fetcher.events), 'seconds': round(time.monotonic() - began, 2)}), flush=True)
        for _, _, job in pending:
            receipts[job['url']] = {key: job[key] for key in ('url', 'entity_ids', 'references', 'order', 'attempts')}
            receipts[job['url']].update(status='not_collected_budget', budget_reason=stopped or 'session_ended')
        rows = sorted(receipts.values(), key=lambda row: row['order'])
        body_seen = {}
        for row in rows:
            value = row.get('body_sha256')
            if value:
                row['same_body_as_url'] = body_seen.get(value)
                body_seen.setdefault(value, row['url'])
        report = {'kind': REPORT_KIND, 'started_at': began_at, 'finished_at': _utc(),
                  'seconds': round(time.monotonic() - began, 6),
                  'discovery_evaluated': False, 'search_recall_evaluated': False,
                  'reference_urls_seed_native_pipeline': False, 'native_pipeline_touched': False,
                  'scope': 'Known reference URLs supplied only to this isolated extraction/classification arm; not search discovery, native coverage, identity confirmation, or validated risk predictions.',
                  'private_texts_republished': False,
                  'frozen_inputs': {'references_sha256': args.references_sha256, 'cohort_sha256': args.cohort_sha256,
                                    'sources_sha256': _sha(config_raw)},
                  'implementation_sha256': implementation_before,
                  'implementation_unchanged_during_run': implementation_before == {
                      str(path.relative_to(ROOT)): _sha(path.read_bytes()) for path in implementation_paths},
                  'budgets': {'max_transport_attempts': args.max_requests, 'wall_seconds': args.seconds,
                              'workers': args.workers, 'request_timeout_seconds': args.timeout, 'minimum_host_interval_seconds': 5},
                  'total_entities': len(cohort), 'total_reference_rows': len(references),
                  'unique_reference_urls': len(jobs), 'duplicate_reference_rows': len(references) - len(jobs),
                  'status_counts': dict(Counter(row['status'] for row in rows)),
                  'article_quality_reasons': dict(Counter(row['quality_reason'] for row in rows if row.get('quality_reason'))),
                  'observed_date_quality': dict(Counter(row['publication_date_quality'] for row in rows if row.get('publication_date_quality'))),
                  'unique_collected_bodies': len(body_seen), 'transport_attempts': len(fetcher.events),
                  'collector_metrics': fetcher.metrics_snapshot(), 'transport_events': fetcher.events, 'articles': rows}
        raw = (json.dumps(report, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
        _write_private(directory / 'report.json', raw)
        _write_private(directory / 'report.sha256', (_sha(raw) + '  report.json\n').encode())
        print(json.dumps({'report': str(directory / 'report.json'), 'sha256': _sha(raw),
                          'statuses': report['status_counts'], 'unique_collected_bodies': len(body_seen),
                          'transport_attempts': len(fetcher.events), 'seconds': report['seconds']}), flush=True)
        return report
    finally:
        fetcher.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--references', type=Path, required=True)
    parser.add_argument('--cohort', type=Path, required=True)
    parser.add_argument('--sources', type=Path, required=True)
    parser.add_argument('--references-sha256', required=True)
    parser.add_argument('--cohort-sha256', required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--max-requests', type=int, default=300)
    parser.add_argument('--seconds', type=float, default=600)
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--timeout', type=float, default=12)
    args = parser.parse_args()
    if not 1 <= args.max_requests <= 300 or not 0 < args.seconds <= 600 or not 1 <= args.workers <= 4 or not 1 <= args.timeout <= 12:
        parser.error('Bounds: requests1–300, seconds(0,600], workers1–4, timeout1–12')
    collect(args)


if __name__ == '__main__':
    main()
