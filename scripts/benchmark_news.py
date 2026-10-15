"""Bounded real HTTP benchmark, distinct from coverage and synthetic load tests.

No source is enabled by this script. Supply an operator-reviewed config with
explicit RSS and, separately, article permissions. Raw article bodies are never
included in the exported benchmark report; SafeFetcher's local cache is private.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import math
import platform
import sys
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from peps.collector import CollectionError, SafeFetcher
from peps.sources import parse_results


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def canonical_url(url):
    """Only remove tracking; never discard functional article parameters."""
    parts = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
             if not key.lower().startswith("utm_") and key.lower() not in
             {"fbclid", "gclid", "msclkid", "mc_cid", "mc_eid"}]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/",
                       urlencode(query), ""))


def percentile(values, fraction):
    values = sorted(values)
    return round(values[max(0, math.ceil(len(values) * fraction) - 1)], 6) if values else None


class RecordingFetcher(SafeFetcher):
    """The regular transport with one global HTTP-count and wall-clock cap."""

    def __init__(self, config, state_dir, max_requests, seconds):
        super().__init__(config, state_dir)
        self.events = []
        self.cap = max_requests
        self.deadline = time.monotonic() + seconds
        self.record_lock = threading.Lock()
        self.record_local = threading.local()

    def tagged_fetch(self, url, source, phase, stage):
        self.record_local.tags = dict(source_id=source["id"], phase=phase, stage=stage)
        return self.fetch(url, source)

    def _transport(self, url, headers, timeout, max_bytes, addresses):
        with self.record_lock:
            if len(self.events) >= self.cap:
                raise CollectionError("Benchmark HTTP request cap reached")
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise CollectionError("Benchmark wall-clock budget reached")
            event = dict(getattr(self.record_local, "tags", {}), url=url,
                         request_kind="robots" if urlsplit(url).path == "/robots.txt" else "content",
                         started_at=timestamp(), http_status=None, wire_bytes=None,
                         decoded_bytes=None, content_encoding=None, error=None)
            self.events.append(event)
        start = time.monotonic()
        try:
            status, response_headers, data = super()._transport(
                url, headers, min(timeout, max(.01, remaining)), max_bytes, addresses)
            event.update(http_status=status, decoded_bytes=len(data),
                         content_encoding=response_headers.get("content-encoding", "identity"))
            if event["content_encoding"] in ("", "identity"):
                event["wire_bytes"] = len(data)
            return status, response_headers, data
        except Exception as exc:
            event["error"] = type(exc).__name__ + ": " + str(exc)[:200]
            raise
        finally:
            event["elapsed_seconds"] = round(time.monotonic() - start, 6)
            # Collector v2 records compressed transfer bytes even on failures.
            metric = getattr(getattr(self, "_local", None), "transport_metric", None)
            if isinstance(metric, dict):
                for key in ("wire_bytes", "decoded_bytes", "reused_connection", "http_status"):
                    if key in metric:
                        event[key] = metric[key]


def probe(fetcher, source, url, phase, stage):
    start = time.monotonic()
    decisions = []
    while True:
        result = fetcher.tagged_fetch(url, source, phase, stage)
        decisions.append({key: value for key, value in result.items() if key != "body"})
        if (result.get("status") != "deferred" or result.get("error_code") != "host_interval"
                or len(decisions) >= 5 or not result.get("retry_at")):
            break
        retry = datetime.fromisoformat(result["retry_at"].replace("Z", "+00:00")).timestamp()
        delay = max(.05, retry - time.time() + .05)
        if delay > 10 or time.monotonic() + delay >= fetcher.deadline:
            break
        # Only the local host interval is waited out. 429/503/backoff is retained.
        time.sleep(delay)
    records, parse_error = [], None
    if stage == "feed" and result.get("status") in {"ok", "not_modified"}:
        try:
            records = parse_results(result, source)
        except ValueError as exc:
            parse_error = str(exc)
    dates = sorted(item["published_at"] for item in records if item.get("published_at"))
    return dict(source_id=source["id"], source_kind=source.get("kind"), phase=phase, stage=stage, url=url,
                status=result.get("status"), http_status=result.get("http_status"),
                elapsed_seconds=round(time.monotonic() - start, 6),
                body_chars=len(result.get("body", "")), sha256=result.get("sha256"),
                from_cache=result.get("from_cache", False), parse_error=parse_error,
                candidates=len(records), earliest_published_at=dates[0] if dates else None,
                latest_published_at=dates[-1] if dates else None,
                record_cap_reached=len(records) >= int(source.get("max_records", 250)),
                identity_assessed=False, decisions=decisions), records


def summarize(events, probes, unique_urls, article_urls, elapsed):
    latencies = [item["elapsed_seconds"] for item in events]
    wire = [item["wire_bytes"] for item in events if isinstance(item.get("wire_bytes"), int)]
    decoded = [item["decoded_bytes"] for item in events if isinstance(item.get("decoded_bytes"), int)]
    successful_articles = {canonical_url(item["url"]) for item in probes
                           if item["stage"] == "article" and item["status"] in {"ok", "not_modified"}}
    return dict(elapsed_seconds=round(elapsed, 6), network_requests=len(events),
                wire_body_bytes=sum(wire), decoded_body_bytes=sum(decoded),
                byte_metrics_complete=len(wire) == len(events),
                byte_scope="HTTP response bodies; excludes headers, TLS and TCP overhead",
                unique_discovered_urls=len(unique_urls), attempted_article_urls=len(article_urls),
                successful_article_urls=len(successful_articles),
                requests_per_unique_discovered_url=round(len(events) / len(unique_urls), 6) if unique_urls else None,
                requests_per_successful_article=round(len(events) / len(successful_articles), 6) if successful_articles else None,
                discovered_urls_per_second=round(len(unique_urls) / elapsed, 3) if elapsed else None,
                http_status_counts=dict(Counter(str(item["http_status"]) for item in events)),
                outcome_counts=dict(Counter(item["status"] for item in probes)),
                latency_p50_seconds=percentile(latencies, .5), latency_p95_seconds=percentile(latencies, .95),
                gzip_responses=sum(item.get("content_encoding") == "gzip" for item in events),
                per_kind=dict(Counter(item["request_kind"] for item in events)))


def capacity_scenarios(summary, entities=40000):
    """Explicit workload assumptions, never a claim of historical coverage."""
    duration = 7 * 24 * 3600
    scenarios = []
    for notes in (5, 20, 100):
        article_total = entities * notes
        for overlap in (0.0, 0.5):
            distinct = math.ceil(article_total * (1 - overlap))
            scenarios.append(dict(entities=entities, notes_per_entity=notes,
                                  assumed_url_overlap=overlap, distinct_articles=distinct,
                                  required_articles_per_second=round(distinct / duration, 6)))
    return dict(deadline_seconds=duration, scenarios=scenarios,
                measured_successful_articles=summary["successful_article_urls"],
                coverage_demonstrated=False,
                limitation="RSS is a recent rolling window, not an exhaustive history. Throughput does not prove a note exists for every identity or that its identity is resolvable. No seven-day SLA is inferred from this small probe.")


def run(config, state_dir, max_requests=36, seconds=180, workers=4, max_articles=6, reuse_state=False):
    if not 1 <= max_requests <= 200 or not 1 <= seconds <= 3600 or not 1 <= workers <= 16:
        raise ValueError("Use 1..200 requests, 1..3600 seconds and 1..16 workers")
    if not 0 <= max_articles <= 100:
        raise ValueError("max_articles must be 0..100")
    selected = [dict(item) for item in config.get("sources", [])
                if item.get("enabled") and item.get("terms_approved") and item.get("kind") in {"rss", "atom", "gdelt"}
                and item.get("url") and "{query}" not in item["url"]]
    if not selected:
        raise ValueError("No explicitly approved static discovery URLs; this script does not enable sources")
    for source in selected:
        source["robots_required"] = True
        source["timeout_seconds"] = min(20, float(source.get("timeout_seconds", 20)))
        source["cache_ttl_seconds"] = max(60, float(source.get("cache_ttl_seconds", 900)))
    state_dir = Path(state_dir)
    existing_state = (state_dir / "network.sqlite3").exists()
    if existing_state and not reuse_state:
        raise ValueError("Use a fresh state directory for a true cold-cache benchmark")
    fetcher = RecordingFetcher(config, state_dir, max_requests, seconds)
    if existing_state:
        # Retain origin restrictions and server backoff across repeated probes.
        # Only the representation cache is cleared; origin policies stay intact.
        with fetcher._db() as db:
            db.execute("DELETE FROM http_cache")
    initial_collector_metrics = fetcher.metrics_snapshot() if hasattr(fetcher, "metrics_snapshot") else None
    probes, unique, article_jobs = [], set(), []
    phases = []
    overall = time.monotonic()
    for phase in ("cold", "warm"):
        start, event_start = time.monotonic(), len(fetcher.events)
        phase_probes, phase_urls = [], set()
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(probe, fetcher, item, item["url"], phase, "feed") for item in selected]
            for source, future in zip(selected, futures):
                outcome, records = future.result()
                probes.append(outcome)
                phase_probes.append(outcome)
                urls = {canonical_url(item["url"]) for item in records}
                phase_urls.update(urls)
                unique.update(urls)
                if phase == "cold" and source.get("download_articles") and source.get("article_terms_approved"):
                    # Separate explicit body permission; feed opt-in never implies it.
                    for item in records[:min(2, max_articles)]:
                        if len(article_jobs) < max_articles and urlsplit(item["url"]).hostname in source.get("allowed_hosts", []):
                            article_jobs.append((source, item["url"]))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(probe, fetcher, item, url, phase, "article") for item, url in article_jobs]
            for future in futures:
                outcome, _ = future.result()
                probes.append(outcome)
                phase_probes.append(outcome)
        phase_events = fetcher.events[event_start:]
        phases.append(dict(phase=phase, summary=summarize(phase_events, phase_probes, phase_urls,
                                                         {url for _, url in article_jobs}, time.monotonic() - start)))
    summary = summarize(fetcher.events, probes, unique, {url for _, url in article_jobs}, time.monotonic() - overall)
    with fetcher._db() as db:
        robots = [dict(row) for row in db.execute("SELECT origin,body,checked_at,expires_at FROM robots")]
    transport_metrics = fetcher.metrics_snapshot() if hasattr(fetcher, "metrics_snapshot") else None
    fetcher.close() if hasattr(fetcher, "close") else None
    report = dict(generated_at=timestamp(), scope="bounded_real_http_feed_discovery_and_explicitly_permitted_articles",
                  environment=dict(platform=platform.platform(), python=sys.version),
                  caps=dict(max_network_requests=max_requests, max_seconds=seconds, workers=workers, max_articles=max_articles),
                  source_config_sha256=hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest(),
                  sources=[{key: value for key, value in item.items() if key in
                            {"id", "source_name", "url", "terms_url", "rss_policy_url", "review_note", "article_terms_url",
                             "download_articles", "article_terms_approved", "permission_scope", "terms_reviewed_at"}} for item in selected],
                  summary=summary, phases=phases, probes=probes, network_events=fetcher.events,
                  collector_metrics_before=initial_collector_metrics, collector_metrics_after=transport_metrics,
                  cold_cache_scope="response_cache_only; robots_and_origin_restrictions_retained" if existing_state else "fresh_response_and_robots_cache",
                  robots=[dict(origin=row["origin"], robots_url=row["origin"] + "/robots.txt", checked_at=row["checked_at"],
                               expires_at=row["expires_at"], sha256=hashlib.sha256(row["body"].encode()).hexdigest(),
                               body=row["body"][:32000]) for row in robots],
                  capacity=capacity_scenarios(summary),
                  content_exported=False, identity_assessed=False,
                  identity_coverage="Not measured; all discovered names remain unassigned in this benchmark. Name-only matches cannot establish identity.",
                  limitation="Network metrics cover this machine and this moment. Titles and summaries are discovery; no reputation score or identity conclusion is inferred. Public RSS/robots allowance is not a blanket licence for commercial reuse or republication.")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-requests", type=int, default=36)
    parser.add_argument("--seconds", type=float, default=180)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--max-articles", type=int, default=6)
    parser.add_argument("--reuse-state", action="store_true", help="Clear only the response cache; retain robots, restrictions and backoff. Labels the narrower cold-cache scope.")
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8-sig"))
    report = run(config, args.state_dir, args.max_requests, args.seconds, args.workers, args.max_articles, args.reuse_state)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(dict(output=str(args.output), summary=report["summary"], phases=report["phases"]), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
