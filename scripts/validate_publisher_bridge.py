"""Real, derived publisher-body acquisition from a finished nominal-search bank.

This is a new experiment, never a resume of the frozen cold plan. It inherits
the real discovered URLs, raw snapshots, audit, cache and host backoff through
SQLite backup. It adds only reviewed body-only publishers and runs production
attribution, scoring, replay and export. No search, gold URL, mock response or
automatic human confirmation is introduced. Individual results stay local.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing, contextmanager
from copy import deepcopy
import hashlib
import ipaddress
import json
import math
from fnmatch import fnmatchcase
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import traceback
import uuid
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from peps.acquisition_coverage import acquisition_coverage
from peps.cli import export_jsonl
from peps.engine import Engine
from peps.news import NewsAcquirer
from peps.store import canonical, digest, now
from scripts.validate_live_pipeline import (
    Journal, RequestBudget, document_integrity, instrument_collector,
    latency_summary, observed_fetcher, scored_people, write_json,
)
from scripts.validate_notary_search import _body_proof as body_proof, _candidate_rows
from scripts.validate_real_people import (
    copy_bank, db_counts, http_history, network_state, queue_receipt, source_hashes,
)

METHOD = 'derived-real-publisher-bridge-v1'


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_rows(database, table):
    """Stable, complete real rows; callers keep individual data private."""
    if not database.exists():
        return []
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as con:
        con.row_factory = sqlite3.Row
        return sorted((dict(row) for row in con.execute('SELECT * FROM "' + table + '"')),
                      key=canonical)


def bank_fingerprint(directory):
    """Hash a consistent SQLite backup, plus every ordinary bank file."""
    result = {}
    with tempfile.TemporaryDirectory(prefix='webscope-bank-proof-') as temporary:
        for path in sorted(directory.rglob('*')):
            if not path.is_file() or path.name.endswith(('-wal', '-shm', '-journal')):
                continue
            relative = str(path.relative_to(directory))
            if path.suffix == '.sqlite3':
                copied = Path(temporary) / (hashlib.sha256(relative.encode()).hexdigest() + '.sqlite3')
                with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as original:
                    with closing(sqlite3.connect(copied)) as target:
                        original.backup(target)
                result[relative] = file_hash(copied)
            else:
                result[relative] = file_hash(path)
    return result


def valid_host(host):
    if not isinstance(host, str) or host != host.lower() or host.endswith('.'):
        return False
    if any(char not in 'abcdefghijklmnopqrstuvwxyz0123456789-.' for char in host):
        return False
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        return '.' in host and all(part and not part.startswith('-') and not part.endswith('-') for part in host.split('.'))


def derived_config(original, additions):
    """Return a strict superset; never weaken an original source or plan."""
    if not isinstance(additions, dict) or not isinstance(additions.get('sources'), list) or not additions['sources']:
        raise ValueError('El catálogo derivado requiere sources: lista no vacía.')
    result = deepcopy(original)
    previous = {source['id'] for source in result['sources']}
    for source in additions['sources']:
        if not isinstance(source, dict) or not source.get('id') or source['id'] in previous:
            raise ValueError('Los editores agregados requieren IDs nuevos y únicos.')
        previous.add(source['id'])
        if (source.get('kind') != 'rss' or source.get('discovery') is not False
                or source.get('automated_search') is not False):
            raise ValueError('Sólo editores de cuerpos, sin descubrimiento ni búsqueda automática.')
        flags = ('enabled', 'terms_approved', 'robots_required', 'download_articles',
                 'article_terms_approved', 'body_terms_approved')
        if any(source.get(flag) is not True for flag in flags):
            raise ValueError('Los permisos técnicos de sesión deben ser explícitos y separados.')
        hosts = source.get('allowed_hosts')
        if (not isinstance(hosts, list) or not hosts or len(set(hosts)) != len(hosts)
                or not all(valid_host(host) for host in hosts)
                or source.get('article_allowed_hosts') != hosts):
            raise ValueError('Se requieren hosts públicos exactos para transporte y cuerpos.')
        for key in ('url', 'base_url'):
            parts = urlsplit(source.get(key, ''))
            if (parts.scheme != 'https' or parts.hostname not in hosts or parts.username or parts.password
                    or parts.port not in (None, 443) or parts.query or parts.fragment or parts.path not in ('', '/')):
                raise ValueError('Los editores deben declarar su origen público HTTPS exacto.')
        if any(source.get(key) for key in ('search_url', 'api_url', 'search_endpoint')):
            raise ValueError('Esta ampliación no admite endpoints de búsqueda.')
        paths = source.get('article_path_globs')
        if (not isinstance(paths, list) or not paths or any(not isinstance(path, str) or not path.startswith('/')
                or path in ('/', '/*', '/**') or '..' in path or '\\' in path or '?' in path or '#' in path
                for path in paths)):
            raise ValueError('Cada editor requiere article_path_globs delimitados a artículos.')
        allowlist = source.get('article_metadata_url_allowlist')
        if (not isinstance(allowlist, list) or not allowlist
                or any(not permitted_body_url(url, source) for url in allowlist)):
            raise ValueError('Cada editor requiere una allowlist exacta de URLs editoriales descubiertas.')
        if not source.get('permission_scope') or not source.get('review_note'):
            raise ValueError('Debe conservarse la procedencia de los permisos y de la revisión técnica.')
        if (not isinstance(source.get('min_interval_seconds'), (int, float))
                or not math.isfinite(source['min_interval_seconds']) or source['min_interval_seconds'] < 5):
            raise ValueError('La ampliación no reduce las pausas por host por debajo de cinco segundos.')
        result['sources'].append(deepcopy(source))
    return result


def permitted_body_url(url, source, *, exact=True):
    """Enforce body source origins and paths before real transport starts."""
    if not isinstance(url, str):
        return False
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    path = unquote(parts.path or '/')
    hosts = source.get('article_allowed_hosts') or []
    if (parts.scheme != 'https' or parts.hostname not in hosts or port not in (None, 443)
            or parts.username or parts.password or parts.fragment or '\\' in path or '..' in path):
        return False
    if not any(fnmatchcase(path, pattern) for pattern in source.get('article_path_globs', [])):
        return False
    if any(fnmatchcase(path, pattern) for pattern in source.get('article_excluded_path_globs', [])):
        return False
    return not exact or url in source.get('article_metadata_url_allowlist', [])


def production_path_guard(sources):
    """Refuse a live extension until the reviewed production guard is present."""
    try:
        from peps.article_paths import ARTICLE_PATH_SCOPE_VERSION, article_path_allowed, validated_article_globs
    except ImportError as exc:
        raise ValueError('Instale primero el guard de rutas editoriales revisado en producción.') from exc
    if ARTICLE_PATH_SCOPE_VERSION != 'article-path-scope-1':
        raise ValueError('La versión del guard editorial difiere de la metodología preparada.')
    for source in sources:
        validated_article_globs(source)
        if not all(article_path_allowed(url, source) for url in source['article_metadata_url_allowlist']):
            raise ValueError('El guard de producción rechaza alguna ruta editorial del catálogo derivado.')
    return ARTICLE_PATH_SCOPE_VERSION


def validate_origin(directory, report_path, allow_small=False):
    """Require a fully written original result and zero active leases."""
    record = json.loads((directory / 'frozen_inputs.json').read_text(encoding='utf-8'))
    report = json.loads(report_path.read_text(encoding='utf-8'))
    if Path(report.get('run', {}).get('bank_dir', '')).resolve() != directory:
        raise ValueError('El informe original no corresponde al banco de origen.')
    if not report.get('run', {}).get('completed_at') or report.get('frozen_inputs') != record['hashes']:
        raise ValueError('Se requiere el informe final del experimento frío y sus insumos idénticos.')
    database = directory / 'peps.sqlite3'
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as con:
        con.row_factory = sqlite3.Row
        if con.execute("SELECT 1 FROM jobs WHERE status='running' AND lease_until>? LIMIT 1", (time.time(),)).fetchone():
            raise ValueError('El banco frío todavía tiene trabajadores activos.')
        entities = [dict(row) for row in con.execute('SELECT id,name,kind,aliases,context FROM entities ORDER BY id')]
    if record.get('imported_entities_sha256') != digest(canonical(entities)):
        raise ValueError('Los nombres, alias o procedencia del banco frío cambiaron.')
    if not allow_small and len(entities) < 300:
        raise ValueError('La prueba masiva requiere al menos 300 entidades reales.')
    current_config = json.loads((directory / 'news_sources.json').read_text(encoding='utf-8'))
    if current_config != record['config']:
        raise ValueError('El catálogo del banco frío ya no coincide con el catálogo congelado.')
    return record, report


@contextmanager
def hold_original_jobs(store, until):
    """Reversible scheduling isolation in the derived copy, never the cold bank.

    queue=False still consumes already queued queries. Hold all inherited jobs
    during the body-only phase and restore their complete, exact saved rows in
    finally. The inherited network backoff is never edited. Both operations
    leave audit receipts, including original schedules and hashes.
    """
    with store.connection() as con:
        con.execute('BEGIN IMMEDIATE')
        rows = [dict(row) for row in con.execute('SELECT * FROM jobs ORDER BY id')]
        if any(row['status'] == 'running' and (row['lease_until'] or 0) > time.time() for row in rows):
            raise ValueError('No se aíslan trabajos con lease vigente.')
        con.executemany("UPDATE jobs SET due=MAX(due,?) WHERE id=? AND status IN ('queued','deferred','running')",
                        ((until, row['id']) for row in rows))
        store._audit(con, 'publisher_bridge_inherited_jobs_held', {
            'count': len(rows), 'original_rows_sha256': digest(canonical(rows)),
            'hold_until': until, 'original_schedules_restored_in_finally': True,
            'network_backoff_unchanged': True})
    try:
        yield rows
    finally:
        with store.connection() as con:
            con.execute('BEGIN IMMEDIATE')
            for row in rows:
                columns = [column for column in row if column != 'id']
                con.execute('UPDATE jobs SET ' + ','.join('"' + key + '"=?' for key in columns) + ' WHERE id=?',
                            [row[key] for key in columns] + [row['id']])
            identifiers = {value['id'] for value in rows}
            actual = [dict(row) for row in con.execute('SELECT * FROM jobs ORDER BY id') if row['id'] in identifiers]
            if actual != rows:
                raise ValueError('No se pudieron restaurar exactamente los trabajos heredados.')
            store._audit(con, 'publisher_bridge_inherited_jobs_restored', {
                'count': len(rows), 'original_rows_sha256': digest(canonical(rows)),
                'restored_rows_sha256': digest(canonical(actual)), 'complete_rows_restored': True})


def run(args):
    began = time.monotonic()
    started_at = now()
    if not callable(getattr(Engine, 'replay_score_batch', None)):
        raise ValueError('Instale primero la API de repetición en lote revisada; no se inicia adquisición sin ella.')
    origin, directory, output = args.origin.resolve(), args.data_dir.resolve(), args.output.resolve()
    if directory == origin or origin in directory.parents or directory in origin.parents:
        raise ValueError('El banco derivado debe estar separado del original.')
    if output == origin or origin in output.parents:
        raise ValueError('El informe derivado debe quedar fuera del banco original.')
    if directory.exists() or output.exists() or output == args.sources_config.resolve() or output == args.origin_report.resolve():
        raise ValueError('El banco derivado y el informe deben ser nuevos; no se reemplazan insumos.')
    frozen, original_report = validate_origin(origin, args.origin_report.resolve(), args.allow_small_bank)
    additions = json.loads(args.sources_config.read_text(encoding='utf-8'))
    config = derived_config(frozen['config'], additions)
    path_guard_version = production_path_guard(additions['sources'])
    original_manifest = bank_fingerprint(origin)
    origin_db = origin / 'peps.sqlite3'
    inherited = {table: read_rows(origin_db, table) for table in
        ('audit', 'entities', 'jobs', 'news_query_windows', 'news_discovery_runs', 'news_observations')}
    original_history = read_rows(origin / 'network.sqlite3', 'request_history')
    original_hosts = network_state(origin)['hosts']
    known_urls = {row['canonical_url'] for row in read_rows(origin_db, 'news_articles')}
    body_sources = {source['id']: source for source in additions['sources']}
    if any(url not in known_urls for source in body_sources.values() for url in source['article_metadata_url_allowlist']):
        raise ValueError('La allowlist contiene una URL ajena a las observaciones reales del banco frío.')
    new_hosts = {host for source in body_sources.values() for host in source['allowed_hosts']}
    copy_bank(origin, directory)
    if bank_fingerprint(origin) != original_manifest or bank_fingerprint(directory) != original_manifest:
        raise ValueError('El respaldo SQLite o los archivos heredados no coinciden con el original estable.')
    attempt = directory.with_name(directory.name + '-attempts') / (now().replace(':', '').replace('-', '') + '-' + uuid.uuid4().hex[:8])
    attempt.mkdir(parents=True)
    journal = Journal(attempt)
    engine = Engine(directory, online=True)
    # Construct production schema first: its standard onboarding migration may
    # append disabled defaults. Then install this separately frozen, strict
    # source superset, exactly as the cold runner installs its frozen catalogue.
    news = NewsAcquirer(engine)
    write_json(news.config_path, config)
    if news.config() != config or not engine.store.verify_audit()['ok']:
        raise ValueError('El catálogo derivado o la auditoría heredada cambiaron inesperadamente.')
    before_bodies = body_proof(engine, frozen['people'])
    before_people = scored_people(engine, frozen['cases'], queue_receipt(engine.store), http_history(directory), [])
    prior_requests = sum(row['network_requests'] for row in original_history)
    budget = RequestBudget(prior_requests + args.max_http, prior_requests, began + args.seconds, journal)
    fetches, timings, phases = [], {'validation_backup_and_setup': time.monotonic() - began}, {}
    hashes = source_hashes() | {str(Path(__file__).relative_to(ROOT)): file_hash(Path(__file__))}
    method = {'method': METHOD, 'frozen_at': now(), 'origin_bank': str(origin),
        'origin_report_sha256': file_hash(args.origin_report), 'origin_bank_fingerprint': original_manifest,
        'origin_operational_passed': original_report.get('operational_passed'),
        'origin_partial': original_report.get('partial'),
        'original_frozen_inputs': frozen['hashes'], 'derived_catalogue_input_sha256': file_hash(args.sources_config),
        'production_article_path_guard_version': path_guard_version,
        'derived_effective_config_sha256': digest(canonical(config)), 'added_source_ids': sorted(body_sources),
        'maximum_new_actual_http': args.max_http, 'maximum_new_jobs': args.max_jobs,
        'real_acquisition_seconds': args.acquire_seconds, 'transport_deadline_seconds': args.seconds,
        'processing_finishes_and_is_timed_separately': True,
        'workers': args.workers, 'body_budget': args.body_budget, 'per_entity_body_limit': args.per_entity_body_limit,
        'new_search_queries': False, 'cold_bank': False, 'gold_urls_injected': False,
        'real_transport': True, 'production_scoring': True, 'source_hashes': hashes}
    write_json(directory / 'publisher_bridge_frozen.json', method)
    engine.store.audit('publisher_bridge_forked', method)
    def progress(event):
        journal.append('progress', {'observed_at': now(), 'elapsed_seconds': time.monotonic() - began, **event})
    @contextmanager
    def phase(name):
        start = time.monotonic()
        print(json.dumps({'phase': name, 'event': 'start', 'at': now()}), flush=True)
        try:
            yield
        finally:
            timings[name] = time.monotonic() - start
            journal.append('phases', {'phase': name, 'seconds': timings[name], 'at': now()})
    failure = None
    acquisition_statuses = Counter(row['status'] for row in queue_receipt(engine.store))
    try:
        BaseObserved = observed_fetcher(engine, budget, journal, attempt, fetches)
        class BodyOnlyObserved(BaseObserved):
            def fetch(self, url, source):
                if (source.get('id') not in body_sources or url not in known_urls
                        or not permitted_body_url(url, body_sources[source['id']])):
                    raise ValueError('Publisher bridge rejected a new search or undiscovered parent URL before transport.')
                return super().fetch(url, source)

            def _transport(self, url, headers, timeout, max_bytes, addresses):
                source = body_sources.get(getattr(self._local, 'live_source', None))
                parts = urlsplit(url)
                robots = parts.path == '/robots.txt' and not parts.query
                if (source is None or parts.scheme != 'https' or parts.hostname not in source['allowed_hosts']
                        or parts.username or parts.password or parts.port not in (None, 443)
                        or not robots and not permitted_body_url(url, source, exact=False)):
                    raise ValueError('Publisher bridge rejected an off-scope redirect before transport.')
                return super()._transport(url, headers, timeout, max_bytes, addresses)
        with phase('real_discovered_publisher_bodies'):
            with hold_original_jobs(engine.store, time.time() + args.seconds + 60):
                with instrument_collector(BodyOnlyObserved):
                    phases['acquisition'] = news.acquire(queue=False, workers=args.workers,
                        seconds=args.acquire_seconds, max_articles=args.max_jobs, relevant_only=True,
                        body_budget=args.body_budget, per_entity_body_limit=args.per_entity_body_limit,
                        cache_mode='reuse', progress=progress)
        engine.stop_event.clear()
        acquisition_jobs = queue_receipt(engine.store)
        acquisition_statuses = Counter(row['status'] for row in acquisition_jobs)
        provenance = {'mode': METHOD, 'partial': True, 'historical_search_complete': False,
            'origin_frozen_plan_sha256': frozen['hashes']['plan_sha256'],
            'coverage': acquisition_coverage(engine.store, news.sources()),
            'pending_jobs': sum(acquisition_statuses[status] for status in ('queued', 'deferred', 'running')),
            'blocked_count': acquisition_statuses['blocked'], 'failed_count': acquisition_statuses['failed'],
            'dead_count': acquisition_statuses['dead'],
            'pending_acquisition_is_not_completed': True, 'fork_receipt': str(directory / 'publisher_bridge_frozen.json')}
        engine.store.audit('publisher_bridge_analysis_requested', provenance)
        with phase('production_identity_attribution'):
            phases['attribution'] = news.attribute(limit=None, rescore=False, progress=progress)
        with phase('production_scoring'):
            phases['scored'] = engine.rescore(acquisition=provenance, progress=progress)
        with phase('production_replay_and_explanations'):
            with engine.store.connection() as con:
                runs = [dict(row) for row in con.execute('SELECT entity_id,run_id FROM scores ORDER BY entity_id')]
            bundles = engine.replay_score_batch([row['run_id'] for row in runs])
            replays, explanations = bundles['replays'], bundles['explanations']
            write_json(attempt / 'score-replays-private.json', replays)
            write_json(attempt / 'individual-explanations-private.json', explanations)
            phases['replay'] = {'completed': len(replays), 'matched': sum(row['matched'] for row in replays),
                                'explanations': len(explanations)}
        with phase('production_export'):
            export_jsonl(engine.store, attempt / 'production-export-private.jsonl')
    except BaseException as exc:
        failure = {'type': type(exc).__name__, 'message': str(exc), 'traceback': traceback.format_exc()}
        write_json(attempt / 'failure.json', failure)
    jobs, history = queue_receipt(engine.store), http_history(directory)
    people = scored_people(engine, frozen['cases'], jobs, history, fetches)
    bodies = body_proof(engine, frozen['people'])
    for body in bodies:
        # Attribution and scoring have now run. Acquisition-only helper
        # placeholders must not contradict the real identity/score receipts.
        body.pop('identity_confirmed', None)
        body.pop('scored', None)
    before_ids = {row['document_id'] for row in before_bodies}
    before_articles = {row['id'] for row in before_bodies}
    new_bodies = [row for row in bodies if row['id'] not in before_articles]
    candidates = _candidate_rows(engine)
    audit = engine.store.verify_audit()
    target_db = directory / 'peps.sqlite3'
    def unchanged_rows(table):
        old = inherited[table]
        actual = {canonical(row) for row in read_rows(target_db, table)}
        return all(canonical(row) in actual for row in old)
    after_history = read_rows(directory / 'network.sqlite3', 'request_history')
    after_hosts = {row['host']: row for row in network_state(directory)['hosts']}
    replay = phases.get('replay', {})
    with engine.store.connection() as con:
        score_provenance = [json.loads(row[0]).get('provenance', {}).get('acquisition', {}) for row in con.execute('SELECT result FROM scores')]
    checks = {'cold_bank_fingerprint_unchanged': bank_fingerprint(origin) == original_manifest,
        'inherited_audit_prefix_preserved': unchanged_rows('audit'),
        'entities_exactly_preserved': read_rows(target_db, 'entities') == inherited['entities'],
        'inherited_jobs_exactly_preserved': unchanged_rows('jobs'),
        'original_query_windows_unchanged': read_rows(target_db, 'news_query_windows') == inherited['news_query_windows'],
        'no_new_discovery_or_search_receipts': read_rows(target_db, 'news_discovery_runs') == inherited['news_discovery_runs'],
        'original_raw_discovery_observations_preserved': unchanged_rows('news_observations'),
        'inherited_http_history_preserved': all(row in after_history for row in original_history),
        'original_host_backoff_unchanged_outside_added_publishers': all(after_hosts.get(row['host']) == row for row in original_hosts if row['host'] not in new_hosts),
        'new_http_budget_respected': len(budget.events) <= args.max_http,
        'new_http_observed_matches_durable': len(budget.events) == sum(row['network_requests'] for row in history) - prior_requests,
        'only_new_body_sources_fetched': all(row['source_id'] in body_sources and row['url'] in known_urls for row in fetches),
        'all_new_bodies_from_original_discovered_urls': all(row['canonical_url'] in known_urls for row in new_bodies),
        'all_original_body_snapshots_valid': all(row['raw_snapshot_hash_valid'] for row in bodies),
        'captured_real_response_hashes_valid': all(file_hash(attempt / row['response_capture_path']) == row['response_sha256'] for row in budget.events if row.get('response_capture_path')),
        'all_people_received_production_score': db_counts(engine.store)['scores'] == len(frozen['people']),
        'all_people_rescored_in_this_derived_attempt': phases.get('scored') == len(frozen['people']),
        'all_current_scores_preserve_partial_derived_scope': len(score_provenance) == len(frozen['people']) and all(
            row.get('mode') == METHOD and row.get('partial') is True
            and row.get('blocked_count') == acquisition_statuses['blocked']
            and row.get('dead_count') == acquisition_statuses['dead']
            and row.get('failed_count') == acquisition_statuses['failed'] for row in score_provenance),
        'all_scores_replayed_and_explained': replay.get('completed') == replay.get('matched') == replay.get('explanations') == len(frozen['people']),
        'frozen_profile_input_unchanged': file_hash(args.sources_config) == method['derived_catalogue_input_sha256'],
        'origin_report_unchanged': file_hash(args.origin_report) == method['origin_report_sha256'],
        'derived_effective_catalogue_unchanged': news.config() == config,
        'source_modules_stable_within_attempt': hashes == (source_hashes() | {str(Path(__file__).relative_to(ROOT)): file_hash(Path(__file__))}),
        'audit_valid': audit['ok'], 'no_fatal_error': failure is None}
    checks.update(document_integrity(engine.store))
    summary = {'people': len(people), 'unique_original_documents_before': len(before_ids),
        'unique_original_documents_after': len({row['document_id'] for row in bodies}),
        'new_unique_original_documents': len({row['document_id'] for row in bodies} - before_ids),
        'new_article_urls_with_text': len(new_bodies),
        'people_with_original_text_before': sum(bool(row['original_documents']) for row in before_people),
        'people_with_original_text_after': sum(bool(row['original_documents']) for row in people),
        'current_candidate_urls': len({row['article_id'] for row in candidates}),
        'production_scores_computed': db_counts(engine.store)['scores'],
        'assessment_states': dict(Counter(row['assessment']['state'] for row in people)),
        'evidence_items': db_counts(engine.store)['evidence'],
        'new_actual_http_requests': len(budget.events), 'inherited_actual_http_requests': prior_requests,
        'job_status_counts': dict(Counter(row['status'] for row in jobs)),
        'blocked_count': sum(row['status'] == 'blocked' for row in jobs),
        'failed_count': sum(row['status'] == 'failed' for row in jobs),
        'dead_count': sum(row['status'] == 'dead' for row in jobs),
        'pending_jobs': sum(row['status'] in ('queued', 'deferred', 'running') for row in jobs),
        'http_status_counts': dict(Counter(str(row['http_status']) for row in budget.events)),
        'fetch_outcome_counts': dict(Counter(str(row['error_code'] or row['status']) for row in fetches)),
        'transport_latency': latency_summary([row['seconds'] for row in budget.events if 'seconds' in row])}
    report = {'schema_version': 1, 'generated_at': now(), 'method': method,
        'run': {'bank_dir': str(directory), 'attempt_dir': str(attempt), 'started_at': started_at,
                'completed_at': now(), 'total_seconds': time.monotonic() - began},
        'summary': summary, 'counts': db_counts(engine.store), 'timings_seconds': timings,
        'phase_results': phases, 'people_results': people, 'original_bodies': bodies,
        'jobs': jobs,
        'current_candidates': candidates, 'new_body_documents': new_bodies,
        'session_http_events': budget.events, 'session_fetch_events': fetches,
        'source_config': config, 'audit': audit, 'operational_checks': checks,
        'operational_passed': all(checks.values()), 'failure': failure,
        'partial': True, 'partial_reasons': ['Derived body-only extension, not a fresh or exhaustive search of 300 people.'] +
            ([f"{summary['blocked_count']} blocked, {summary['failed_count']} failed and {summary['dead_count']} dead jobs limit acquisition."]
             if summary['blocked_count'] or summary['failed_count'] or summary['dead_count'] else []) +
            ([f"{summary['pending_jobs']} queued/deferred/running jobs remain."] if summary['pending_jobs'] else []),
        'limitations': ['A real production score is not a confirmed identity, guilt finding or clean record.',
            'No precision, recall or accuracy is inferred from unreviewed production outputs.',
            'Publisher selection is post-discovery technical remediation; this is not a held-out efficacy estimate.',
            'The original search may be partial; all inherited jobs, backoff and errors remain preserved.',
            'Individual scores, names, bodies and explanations are private; publish aggregate results only.']}
    write_json(attempt / 'report-private.json', report)
    write_json(output, report)
    print(json.dumps({'output': str(output), 'summary': summary, 'operational_checks': checks}, ensure_ascii=False), flush=True)
    return report


def parser():
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument('--online', action='store_true')
    value.add_argument('--origin', type=Path, required=True)
    value.add_argument('--origin-report', type=Path, required=True)
    value.add_argument('--sources-config', type=Path, required=True)
    value.add_argument('--data-dir', type=Path, required=True)
    value.add_argument('--output', type=Path, required=True)
    value.add_argument('--seconds', type=float, default=600,
                       help='Real transport deadline; attribution/scoring finish on the acquired texts and their time is reported separately.')
    value.add_argument('--acquire-seconds', type=float, default=240)
    value.add_argument('--max-http', type=int, default=120)
    value.add_argument('--max-jobs', type=int, default=120)
    value.add_argument('--workers', type=int, default=16)
    value.add_argument('--body-budget', type=int, default=600)
    value.add_argument('--per-entity-body-limit', type=int, default=8)
    value.add_argument('--allow-small-bank', action='store_true', help='Explicit functional pilot; never the massive experiment.')
    return value


def main():
    value = parser()
    args = value.parse_args()
    if (not args.online or not math.isfinite(args.seconds) or not 0 < args.seconds <= 900
            or not math.isfinite(args.acquire_seconds) or not 0 < args.acquire_seconds <= min(240, args.seconds)
            or not 1 <= args.max_http <= 120 or not 1 <= args.max_jobs <= 120 or not 1 <= args.workers <= 16
            or not 0 <= args.body_budget <= 600 or not 0 <= args.per_entity_body_limit <= 8):
        value.error('Use --online, máximo 240 s de adquisición, 120 HTTP nuevos, 120 trabajos y 16 workers.')
    try:
        report = run(args)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        value.error(str(exc))
    if not report['operational_passed']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
