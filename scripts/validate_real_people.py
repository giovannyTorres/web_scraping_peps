"""Opt-in real public-web validation of a provenance-backed municipal/notary roster.

No HTTP responses, clocks, names or article bodies are synthesized. Search
snippets stay discovery metadata. Original bodies are downloaded only from the
explicit publisher catalogue, then production attribution/scoring runs unchanged.
An independent copy verifies local cache reuse with DNS and TCP forbidden.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import closing, nullcontext
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from unittest.mock import patch
from urllib.parse import parse_qsl, urlencode, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from peps.acquisition_coverage import acquisition_coverage
from peps.collector import SafeFetcher, _iso
from peps.engine import Engine
from peps.news import NewsAcquirer, canonical_url, source_policy, _body_selection
from peps.public_search import parse_public_results
from peps.relevance import MODEL_VERSION, assessment_summary
from peps.sources import parse_results
from peps.store import canonical, digest, now

PUBLIC_SOURCES = {'mwmbl_web', 'duckduckgo_web'}
BODY_SOURCES = {'contralinea_index', 'aristegui_index', 'desinformemonos_index'}
GROUPS = {'municipal_president': 'municipal_president', 'municipal': 'municipal_president',
          'mayor': 'municipal_president', 'notary': 'notary', 'notario': 'notary'}
# Keep historical origins explicit while tracking the actual current model.
KNOWN_METADATA_MIGRATIONS = {
    (historical, MODEL_VERSION)
    for historical in ('metadata-relevance-1.1.0', 'metadata-relevance-1.2.0')
    if historical != MODEL_VERSION
}


class _ValidationBudgetStopped(RuntimeError):
    """Instrumentation boundary, deliberately not a collector/source failure."""


def budget_result(url, network_requests=0):
    return {'url': url, 'status': 'deferred', 'body': '', 'http_status': None,
            'error': 'Explicit real-validation request/time budget reached',
            'error_code': 'session_limit', 'retry_at': time.time(),
            'network_requests': network_requests, 'request_attempted': network_requests > 0}


def budget_exhausted(events, maximum, deadline):
    return len(events) >= maximum or time.monotonic() >= deadline


def load_roster(path, *, allow_small=False):
    """Retain provenance; office-to-role is a disclosed hint, never verification."""
    raw = json.loads(path.read_text(encoding='utf-8-sig'))
    rows = raw if isinstance(raw, list) else raw.get('people', raw.get('entities', [])) if isinstance(raw, dict) else []
    if not isinstance(rows, list) or not rows:
        raise ValueError('El padrón requiere una lista JSON no vacía: people/entities o lista raíz.')
    people, identifiers, names, adjustments = [], set(), set(), []
    for position, value in enumerate(rows):
        if not isinstance(value, dict):
            raise ValueError('Cada persona del padrón debe ser un objeto.')
        person = deepcopy(value)
        context = person.get('context') or {}
        if not isinstance(context, dict):
            raise ValueError('context debe ser un objeto JSON.')
        name = person.get('name')
        identifier = person.get('id')
        group = GROUPS.get(person.get('entity_group') or person.get('category') or person.get('group'))
        if not isinstance(name, str) or len(name.split()) < 2 or not isinstance(identifier, str) or not identifier or group is None:
            raise ValueError(f'Fila {position + 1}: requiere id, nombre completo y entity_group municipal_president/notary.')
        folded = ' '.join(name.casefold().split())
        if identifier in identifiers or folded in names:
            raise ValueError('El padrón contiene identificadores o nombres duplicados.')
        # A source pointer is mandatory. Its presence is provenance, not a claim
        # that this harness has corroborated every identity or office tenure.
        provenance = [person.get(key) or context.get(key) for key in
                      ('source_url', 'source_urls', 'source', 'provenance')]
        if not any(provenance):
            raise ValueError(f'Fila {position + 1}: falta procedencia pública del padrón.')
        if person.get('kind', 'person') != 'person':
            raise ValueError('Esta validación sólo admite personas.')
        if context.get('office') and not context.get('role'):
            context['role'] = deepcopy(context['office'])
            adjustments.append({'entity_id': identifier, 'copy': 'context.office -> context.role',
                                'verified_by_harness': False})
        person.update(name=name.strip(), kind='person', entity_group=group, context=context)
        people.append(person)
        identifiers.add(identifier)
        names.add(folded)
    groups = Counter(person['entity_group'] for person in people)
    if not allow_small and (len(people) < 300 or any(groups[group] < 150 for group in ('municipal_president', 'notary'))):
        raise ValueError('La prueba completa requiere al menos 150 presidentes municipales y 150 notarios distintos.')
    return people, {'path': str(path.resolve()), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                    'people': len(people), 'groups': dict(groups), 'small_pilot': allow_small,
                    'import_hint_adjustments': adjustments}


def make_config(args, people_count):
    config = json.loads((ROOT / 'peps/defaults/news_sources.json').read_text(encoding='utf-8'))
    public = set(filter(None, args.sources.split(',')))
    bodies = set(filter(None, args.body_sources.split(',')))
    if not public or not public <= PUBLIC_SOURCES or not bodies <= BODY_SOURCES:
        raise ValueError('Use los buscadores públicos o los tres editores expresamente admitidos por esta validación.')
    selected = public | bodies
    config['sources'] = [source for source in config['sources'] if source['id'] in selected]
    if {source['id'] for source in config['sources']} != selected:
        raise ValueError('La fuente solicitada no existe en el catálogo de producción.')
    for source in config['sources']:
        body = source['id'] in bodies
        source.update(enabled=True, terms_approved=True, download_articles=body,
                      article_terms_approved=body, body_terms_approved=body,
                      robots_required=True, timeout_seconds=12)
    config['user_agent'] = 'WebScope-Real-People-Validation/2.9 (+https://github.com/giovannyTorres/web_scraping_peps)'
    config['search'].update(strategy='public', preset='identity', include_aliases=False, use_context=True,
                            execution_mode='quick', session_seconds=120, max_queries=people_count,
                            start=args.start, end=args.end)
    return config


def source_hashes():
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted((ROOT / 'peps').glob('*.py'))}


def db_counts(store):
    with store.connection() as con:
        return {table: con.execute('SELECT count(*) FROM ' + table).fetchone()[0]
                for table in ('entities', 'news_articles', 'news_observations', 'documents',
                              'evidence', 'news_identity_links', 'scores', 'score_runs')}


def queue_receipt(store):
    with store.connection() as con:
        return [dict(row) for row in con.execute('SELECT id,kind,status,attempts,due,lease_until,error,payload '
                                                "FROM jobs WHERE kind GLOB 'news_*' ORDER BY id")]


def validate_resume_bank(directory, people, roster):
    """Read-only checks run before opening or changing the existing bank."""
    database = directory / 'peps.sqlite3'
    if not database.exists():
        raise ValueError('--resume requiere un banco existente de esta validación.')
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as con:
        con.row_factory = sqlite3.Row
        receipt = con.execute("SELECT payload FROM audit WHERE action='real_people_validation_started' ORDER BY seq LIMIT 1").fetchone()
        if receipt is None:
            raise ValueError('El banco no tiene procedencia de validación real de personas.')
        original = json.loads(receipt[0])
        if original.get('roster_sha256') != roster['sha256'] or original.get('groups') != roster['groups']:
            raise ValueError('El padrón, sus grupos o su procedencia difieren del banco original.')
        actual = {row['id']: dict(row) for row in con.execute('SELECT id,name,kind,aliases,context FROM entities')}
        if con.execute("SELECT 1 FROM jobs WHERE kind GLOB 'news_*' AND status='running' AND lease_until>? LIMIT 1",
                       (time.time(),)).fetchone():
            raise ValueError('El banco todavía tiene un trabajador activo; espere antes de reanudar.')
    # Apply the production import normalizer in a throwaway bank so optional
    # top-level role/state fields are compared exactly, without reimporting or
    # incrementing the revision of the real bank.
    with tempfile.TemporaryDirectory(prefix='webscope-resume-roster-') as temporary:
        expected_engine = Engine(temporary)
        expected_engine.store.import_entities(people)
        with expected_engine.store.connection() as con:
            expected = {row['id']: dict(row) for row in con.execute('SELECT id,name,kind,aliases,context FROM entities')}
    if actual != expected:
        raise ValueError('Los IDs, nombres, alias o contexto/procedencia importados han cambiado.')
    return original


def validate_resume_plan(news, config):
    """Reject scope changes that would create a second nominal search sweep."""
    from peps.news_search import build_search_plan
    plan = build_search_plan(news.store.iter_entities(), config['sources'], config['search'])
    expected = {json.dumps({key: value for key, value in item.items() if key != 'kind'},
                           sort_keys=True, ensure_ascii=False) for item in plan['queries']}
    previous = []
    for job in queue_receipt(news.store):
        payload = json.loads(job['payload'])
        if job['kind'] != 'news_query' or payload.get('window') != 'real-people-public':
            continue
        signature = json.dumps({key: value for key, value in payload.items() if key not in ('window', 'bank_revision')},
                               sort_keys=True, ensure_ascii=False)
        if signature not in expected:
            raise ValueError('El período, fuentes o plan nominal difieren: --resume no debe repetir búsquedas terminadas.')
        previous.append(job)
    return previous


def http_history(directory):
    database = directory / 'network.sqlite3'
    if not database.exists():
        return []
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as con:
        con.row_factory = sqlite3.Row
        return [dict(row) for row in con.execute('SELECT id,occurred_at,url,request_kind,network_requests,http_status,outcome '
                                               'FROM request_history WHERE network_requests>0 ORDER BY id')]


def request_key(url):
    parts = urlsplit(url)
    return parts.hostname, parts.path, urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))


def person_report(engine, people, public_jobs, http_events):
    """A queued/failed search never counts as a completed or observed query."""
    by_person = defaultdict(Counter)
    requested = Counter(request_key(event['url']) for event in http_events
                        if urlsplit(event['url']).path != '/robots.txt')
    with engine.store.connection() as con:
        receipts = {row[0] for row in con.execute('SELECT job_id FROM news_discovery_runs')}
        candidates = defaultdict(set)
        bodies = defaultdict(set)
        for row in con.execute('SELECT c.entity_id,c.article_id,n.scope,n.document_id '
                               'FROM news_current_candidates c JOIN news_articles n ON n.id=c.article_id'):
            candidates[row['entity_id']].add(row['article_id'])
            if row['scope'] == 'full_article' and row['document_id']:
                bodies[row['entity_id']].add(row['document_id'])
        identity = defaultdict(Counter)
        for row in con.execute('SELECT l.entity_id,l.resolution FROM news_identity_links l '
                               'JOIN documents d ON d.id=l.document_id WHERE d.source_kind != \'discovery\''):
            identity[row['entity_id']][json.loads(row['resolution']).get('status', 'unknown')] += 1
        evidence = dict(con.execute('SELECT entity_id,count(*) FROM evidence GROUP BY entity_id'))
        scores = {row['entity_id']: json.loads(row['result']) for row in con.execute('SELECT entity_id,result FROM scores')}
    for job in public_jobs:
        if job['kind'] != 'news_query':
            continue
        payload = json.loads(job['payload'])
        if payload.get('source_id') not in PUBLIC_SOURCES:
            continue
        for identifier in payload.get('entity_ids') or []:
            values = by_person[identifier]
            values['queries_planned'] += 1
            values['query_http_requests'] += requested[request_key(payload['url'])]
            values['queries_completed'] += int(job['id'] in receipts)
            values['queries_' + job['status']] += 1
    rows = []
    for person in people:
        identifier = person['id']
        result = scores.get(identifier)
        summary = assessment_summary(result) if result is not None else {}
        rows.append({'id': identifier, 'name': person['name'], 'entity_group': person['entity_group'],
                     'search': dict(by_person[identifier]),
                     'queried_over_network': by_person[identifier]['query_http_requests'] > 0,
                     'search_completed': by_person[identifier]['queries_completed'] > 0,
                     'relevant_candidates': len(candidates[identifier]),
                     'relevant_full_bodies': len(bodies[identifier]),
                     'identity_statuses': dict(identity[identifier]), 'evidence_items': evidence.get(identifier, 0),
                     'score_computed': result is not None, 'assessment_state': summary.get('state'),
                     'score_is_not_a_guilt_probability': True})
    return rows, {'score_zero': sum(result.get('score') == 0 for result in scores.values()),
                  'score_nonzero': sum(result.get('score', 0) > 0 for result in scores.values()),
                  'assessment_states': dict(Counter(assessment_summary(result)['state'] for result in scores.values()))}


def article_report(store):
    with store.connection() as con:
        rows = []
        for row in con.execute('SELECT n.id,n.canonical_url,n.publisher_id,n.scope,n.status,n.document_id,'
                               'd.text,d.content_hash,d.source_kind,d.metadata FROM news_articles n '
                               'LEFT JOIN documents d ON d.id=n.document_id ORDER BY n.id'):
            full = row['scope'] == 'full_article'
            metadata = json.loads(row['metadata']) if row['metadata'] else {}
            rows.append({key: row[key] for key in ('id', 'canonical_url', 'publisher_id', 'scope', 'status', 'document_id')} |
                        {'body_chars': len(row['text'] or '') if full else 0,
                         'body_sha256': row['content_hash'] if full else None,
                         'source_kind': row['source_kind'],
                         'raw_snapshot_sha256': metadata.get('raw_snapshot_sha256') if full else None})
        return rows


def network_state(directory):
    database = directory / 'network.sqlite3'
    if not database.exists():
        return {'hosts': [], 'cached_responses': 0, 'recorded_http_requests': 0}
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as con:
        con.row_factory = sqlite3.Row
        return {'hosts': [dict(row) for row in con.execute('SELECT * FROM hosts ORDER BY host')],
                'cached_responses': con.execute('SELECT count(*) FROM http_cache').fetchone()[0],
                'recorded_http_requests': con.execute('SELECT COALESCE(sum(network_requests),0) FROM request_history').fetchone()[0]}


def copy_bank(source, target):
    """An independent copy keeps cold jobs, backoff and audit untouched."""
    shutil.copytree(source, target)
    for name in ('peps.sqlite3', 'network.sqlite3'):
        if not (source / name).exists():
            continue
        for suffix in ('-wal', '-shm'):
            (target / (name + suffix)).unlink(missing_ok=True)
        with closing(sqlite3.connect((source / name).as_uri() + '?mode=ro', uri=True)) as original:
            with closing(sqlite3.connect(target / name)) as copied:
                original.backup(copied)


def corpus_snapshot(engine):
    """Exact historical rows remain local; reports contain only their hashes."""
    keys = {'news_articles': ('id',), 'news_observations': ('id',), 'documents': ('id',),
            'evidence': ('id',), 'news_identity_links': ('document_id', 'entity_id'),
            'scores': ('entity_id',), 'score_runs': ('id',), 'audit': ('seq',)}
    with engine.store.connection() as con:
        return {table: {canonical([row[key] for key in columns]): dict(row)
                        for row in con.execute('SELECT * FROM ' + table)} for table, columns in keys.items()}


def cached_snapshot(directory):
    if not (directory / 'network.sqlite3').exists():
        return {}
    with closing(sqlite3.connect((directory / 'network.sqlite3').as_uri() + '?mode=ro', uri=True)) as con:
        con.row_factory = sqlite3.Row
        return {row['url']: digest(canonical(dict(row))) for row in con.execute('SELECT * FROM http_cache')}


def public_cached_body_hash_matches(cached):
    """Narrow public-adapter proof: a strict UTF-8/ASCII roundtrip, never lossy."""
    encoding = 'utf-8'
    for parameter in cached['content_type'].split(';')[1:]:
        key, separator, value = parameter.partition('=')
        if separator and key.strip().casefold() == 'charset':
            label = value.strip().strip('\"\'').casefold()
            if label not in ('utf-8', 'utf8', 'ascii', 'us-ascii'):
                return False
            encoding = 'ascii' if label in ('ascii', 'us-ascii') else 'utf-8'
    try:
        return hashlib.sha256(cached['body'].encode(encoding, errors='strict')).hexdigest() == cached['sha256']
    except UnicodeError:
        return False


def verify_cache_enrichment(news, before, after, cached_before):
    """Validate every new observation against unchanged cached bytes and policy.

    A model migration can recover earlier rejected metadata and append versioned
    observations. It cannot replace old rows, invent URLs, change permission,
    create source bodies, or pass without a subsequent exact idempotence check.
    """
    from peps.relevance import RelevanceIndex, MODEL_VERSION
    old_observations, observations = before['news_observations'], after['news_observations']
    old_articles, articles = before['news_articles'], after['news_articles']
    new_observations = [row for key, row in observations.items() if key not in old_observations]
    new_articles = [row for key, row in articles.items() if key not in old_articles]
    def lineage_key(row):
        return tuple(row[key] for key in ('article_id', 'source_id', 'discovery_url', 'observed_url', 'retrieved_at', 'discovery_hash'))
    ancestors = defaultdict(list)
    for row in old_observations.values():
        ancestors[lineage_key(row)].append(row)
    def preserved(table):
        return all(after[table].get(key) == value for key, value in before[table].items())
    checks = {'historical_observations_byte_equal': preserved('news_observations'),
              'historical_articles_byte_equal': preserved('news_articles'),
              'historical_audit_is_unchanged_prefix': preserved('audit'),
              'bodies_identity_scores_and_history_byte_equal': all(before[table] == after[table] for table in
                  ('documents', 'evidence', 'news_identity_links', 'scores', 'score_runs')),
              'cached_http_bytes_and_metadata_unchanged': cached_snapshot(news.engine.data_dir) == cached_before,
              'canonical_urls_unique': len({row['canonical_url'] for row in articles.values()}) == len(articles)}
    versions = Counter(json.loads(row['details']).get('retrieval_relevance', {}).get('model_version', 'unknown')
                       for row in old_observations.values())
    checks['older_model_present_if_metadata_grows'] = not new_observations or any(
        (version, MODEL_VERSION) in KNOWN_METADATA_MIGRATIONS for version in versions)
    fingerprints = []
    for row in observations.values():
        details = json.loads(row['details'])
        relevance = details.get('retrieval_relevance') or {}
        fingerprints.append((row['article_id'], row['source_id'], row['discovery_url'], row['observed_url'],
                             row['discovery_hash'], details.get('source_config_hash'),
                             relevance.get('model_version'), relevance.get('bank_revision')))
    checks['no_same_model_snapshot_policy_observation_duplicates'] = len(fingerprints) == len(set(fingerprints))
    checks['observation_ids_match_production_content_hash'] = all(row['id'] == digest(canonical([
        row['article_id'], row['source_id'], row['discovery_url'], row['discovery_hash'], json.loads(row['details'])]))
        for row in observations.values())
    sources = {source['id']: source for source in news.sources()}
    index = RelevanceIndex(news.store.iter_entities())
    with news.store.connection() as con:
        revision = con.execute("SELECT value FROM meta WHERE key='news_identity_revision'").fetchone()[0]
    payloads = defaultdict(list)
    for job in queue_receipt(news.store):
        payload = json.loads(job['payload'])
        if job['kind'] == 'news_query':
            payloads[(payload.get('source_id'), payload.get('url'))].append(payload)
    violations, verified_new, verified_versioned_lineages = [], [], []
    parsed_cache = {}
    with (closing(sqlite3.connect((news.engine.data_dir / 'network.sqlite3').as_uri() + '?mode=ro', uri=True))
          if new_observations else nullcontext(None)) as con:
        if con is not None:
            con.row_factory = sqlite3.Row
        for row in new_observations:
            details = json.loads(row['details'])
            source = sources.get(row['source_id'])
            if not source or row['source_id'] not in PUBLIC_SOURCES:
                violations.append({'id': row['id'], 'reason': 'New observations require a permitted public-search source.'})
                continue
            cached = con.execute('SELECT * FROM http_cache WHERE url=?', (row['discovery_url'],)).fetchone()
            if (cached is None or row['discovery_url'] not in cached_before
                    or cached['sha256'] != row['discovery_hash']):
                violations.append({'id': row['id'], 'reason': 'No unchanged original HTTP snapshot matches this observation.'})
                continue
            if not public_cached_body_hash_matches(cached):
                violations.append({'id': row['id'], 'reason': 'Cached public body does not losslessly reproduce its raw UTF-8/ASCII SHA256.'})
                continue
            cache_key = (row['source_id'], row['discovery_url'])
            if cache_key not in parsed_cache:
                try:
                    parsed_cache[cache_key] = parse_results({'status': 'not_modified', 'body': cached['body'],
                        'url': cached['url'], 'final_url': cached['final_url'] or cached['url']}, source)
                except ValueError:
                    parsed_cache[cache_key] = []
            record = next((record for record in parsed_cache[cache_key] if record['url'] == row['observed_url']
                           and record['title'] == details.get('title') and record.get('text', '') == details.get('summary')), None)
            candidates = []
            if record:
                for payload in payloads.get(cache_key, []):
                    candidate = index.rank(record['title'], record.get('text', ''),
                                           entity_ids=payload.get('entity_ids'), names=payload.get('names'))
                    candidate['bank_revision'] = revision
                    candidates.append(candidate)
            relevance = details.get('retrieval_relevance') or {}
            expected_details = None
            if record and relevance in candidates:
                title, summary = str(record.get('title') or ''), str(record.get('text') or '')
                expected_details = {'title': title, 'summary': summary, 'published_at': record.get('published_at'),
                    'discovered_at': record.get('discovered_at'), 'source_config_hash': digest(canonical(source)),
                    'terms_url': source.get('terms_url'), 'source_policy': source_policy(source), 'scope': 'discovery_metadata',
                    'raw_url': record['url'], 'cache_validated_at': _iso(cached['fetched_at']),
                    'body_selection': _body_selection(title, summary, relevance, canonical_url(record['url'])),
                    'retrieval_relevance': relevance}
                if record.get('metadata'):
                    expected_details['discovery_metadata'] = record['metadata']
            parents = ancestors.get(lineage_key(row), [])
            base_details = {key: value for key, value in details.items() if key != 'retrieval_relevance'}
            valid_versioned_lineage = not parents or any(
                (json.loads(parent['details']).get('retrieval_relevance', {}).get('model_version'), MODEL_VERSION)
                in KNOWN_METADATA_MIGRATIONS
                and {key: value for key, value in json.loads(parent['details']).items() if key != 'retrieval_relevance'} == base_details
                for parent in parents)
            valid = (record is not None and relevance.get('model_version') == MODEL_VERSION
                     and relevance.get('relevant') is True and relevance.get('identity_verified') is False
                     and relevance.get('score_effect') == 'none' and relevance in candidates
                     and details == expected_details and row['retrieved_at'] == cached['retrieved_at']
                     and valid_versioned_lineage
                     and details.get('source_config_hash') == digest(canonical(source))
                     and details.get('source_policy') == source_policy(source)
                     and row['article_id'] == digest(canonical_url(row['observed_url'])))
            if not valid:
                violations.append({'id': row['id'], 'reason': 'Cached metadata, exact current relevance, policy or canonical URL differs.'})
            else:
                verified_new.append(row['id'])
                if parents:
                    verified_versioned_lineages.append(row['id'])
    checks['every_new_observation_reproduced_from_original_cache_current_model_and_policy'] = len(verified_new) == len(new_observations)
    new_article_ids = {row['id'] for row in new_articles}
    checks['every_new_url_has_verified_metadata_only_lineage'] = all(row['scope'] == 'feed_metadata_only'
        and row['document_id'] is None and row['id'] in {observation['article_id'] for observation in new_observations
                                                       if observation['id'] in set(verified_new)} for row in new_articles)
    return {'passed': all(checks.values()), 'checks': checks, 'current_model': MODEL_VERSION,
            'historical_model_counts': dict(versions), 'new_articles': len(new_article_ids),
            'new_observations': len(new_observations), 'verified_new_observations': len(verified_new),
            'verified_prior_model_lineages_with_identical_non_relevance_details': len(verified_versioned_lineages),
            'violations': violations, 'raw_hash_proof_scope': 'Public adapters only; strict UTF-8/ASCII bytes must match original raw response SHA256; unsupported/lossy charsets fail.',
            'first_pass_count_changes_are_not_called_idempotent': True}


def make_observed_fetcher(engine, args, http_events, fetch_events, public_metadata, lock, deadline):
    class ObservedFetcher(SafeFetcher):
        def _transport(self, url, headers, timeout, max_bytes, addresses):
            with lock:
                if budget_exhausted(http_events, args.max_requests, deadline):
                    engine.stop_event.set()
                    # Final race/redirect guard; fetch translates it to
                    # session_limit outside the collector, without a host
                    # failure increment or an invented HTTP attempt.
                    raise _ValidationBudgetStopped()
                event = {'url': url, 'http_status': None, 'started_at': now()}
                http_events.append(event)
                self._local.validation_http_events.append(event)
            started = time.monotonic()
            try:
                status, response_headers, body = super()._transport(
                    url, headers, min(timeout, max(.001, deadline - started)), max_bytes, addresses)
                event.update(http_status=status, decoded_bytes=len(body))
                return status, response_headers, body
            except Exception as exc:
                event['error_type'] = type(exc).__name__
                raise
            finally:
                event['seconds'] = round(time.monotonic() - started, 6)

        def fetch(self, url, source):
            self._local.validation_http_events = []
            with lock:
                stopped = budget_exhausted(http_events, args.max_requests, deadline)
            if stopped:
                engine.stop_event.set()
                result = budget_result(url)
            else:
                source = dict(source, _deadline_monotonic=min(
                    source.get('_deadline_monotonic', deadline), deadline))
                try:
                    result = super().fetch(url, source)
                except _ValidationBudgetStopped:
                    result = budget_result(url, len(self._local.validation_http_events))
            metadata = None
            if source['id'] in PUBLIC_SOURCES and result.get('status') in ('ok', 'not_modified'):
                # Keep the actual pre-filter search corpus for a local audit of
                # omissions/abbreviations. This file is not part of the report;
                # no search excerpt is promoted into an editorial document.
                metadata = {'url': url, 'source_id': source['id'], 'response_sha256': result.get('sha256')}
                try:
                    metadata['records'] = parse_public_results(dict(result), source)
                except ValueError as exc:
                    metadata['parse_error'] = str(exc)
            with lock:
                if metadata is not None:
                    public_metadata.append(metadata)
                fetch_events.append({'url': url, 'source_id': source['id'],
                    **{key: result.get(key) for key in ('status', 'http_status', 'error_code', 'retry_at',
                                                       'network_requests', 'cache_status', 'from_cache')}})
            return result

    return ObservedFetcher


def validate(args):
    people, roster = load_roster(args.roster, allow_small=args.allow_small_roster)
    config = make_config(args, len(people))
    directory = args.data_dir.resolve()
    local_directory = (args.local_dir.resolve() if args.local_dir else
                       directory.with_name(directory.name + '-local-validation' + ('-' + args.output.stem if args.resume else '')))
    metadata_path = directory / ('public_search_metadata-' + args.output.stem + '.jsonl' if args.resume else 'public_search_metadata.jsonl')
    if local_directory == directory or directory in local_directory.parents:
        raise ValueError('La copia local debe quedar fuera del banco original.')
    if args.output.exists() or local_directory.exists() or metadata_path.exists():
        raise ValueError('Use un informe y un destino de copia local nuevos; no se sobrescriben resultados previos.')
    if args.resume:
        validate_resume_bank(directory, people, roster)
    elif directory.exists() and any(directory.iterdir()):
        raise ValueError('Use un banco vacío o --resume explícito con el mismo padrón.')
    directory.mkdir(parents=True, exist_ok=True)
    engine = Engine(directory, online=True)
    began = time.monotonic()
    hashes = source_hashes()
    if not engine.store.verify_audit()['ok']:
        raise ValueError('La auditoría del banco no es válida; no se modifica ni reanuda.')
    news = NewsAcquirer(engine)
    previous_jobs = validate_resume_plan(news, config) if args.resume else []
    previous_done_ids = {job['id'] for job in previous_jobs if job['status'] == 'done'}
    history_before = http_history(directory)
    previous_http_id = max((event['id'] for event in history_before), default=0)
    with engine.store.connection() as con:
        previous_score_ids = {row[0] for row in con.execute('SELECT id FROM score_runs')}
    if not args.resume:
        engine.store.import_entities(people)
    engine.store.audit('real_people_validation_resumed' if args.resume else 'real_people_validation_started', {'roster_sha256': roster['sha256'],
        'people': len(people), 'groups': roster['groups'], 'entity_hints_verified_by_harness': False,
        'previous_done_public_queries': len(previous_done_ids), 'previous_http_requests': sum(event['network_requests'] for event in history_before),
        'body_source_ids': sorted(set(filter(None, args.body_sources.split(','))))})
    def configure(value):
        news.config_path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    configure(config)
    timings = {'import': time.monotonic() - began}
    deadline = time.monotonic() + args.seconds
    http_events, fetch_events, progress_events, public_metadata, lock = [], [], [], [], threading.Lock()

    ObservedFetcher = make_observed_fetcher(engine, args, http_events, fetch_events, public_metadata, lock, deadline)

    last_log = [0.]
    def progress(event):
        progress_events.append(event)
        if time.monotonic() - last_log[0] >= 15 or event.get('event') == 'finish':
            last_log[0] = time.monotonic()
            print(json.dumps({key: event.get(key) for key in
                ('stage', 'completed', 'processed', 'pending', 'active_tasks', 'stop_reason', 'next_retry', 'message')},
                ensure_ascii=False), flush=True)

    def acquire(seconds, window):
        return news.acquire(workers=args.workers, seconds=max(.001, seconds), queue=True, window=window,
                            max_articles=10000, relevant_only=True, body_budget=args.body_budget,
                            per_entity_body_limit=args.per_person_body, progress=progress, cache_mode='reuse')

    print(f'Padrón real: {len(people)} personas. Consultas públicas con transporte y reloj reales.', flush=True)
    with patch('peps.collector.SafeFetcher', ObservedFetcher):
        started = time.monotonic()
        public = acquire(min(args.public_seconds, max(.001, deadline - started)), 'real-people-public')
        timings['public_acquisition'] = time.monotonic() - started
        public_jobs = queue_receipt(engine.store)
        public_coverage = acquisition_coverage(engine.store, news.sources())
        fallback = None
        remaining = deadline - time.monotonic()
        if args.body_sources and args.fallback_seconds > 0 and remaining > .1 and not engine.stop_event.is_set():
            config['search'].update(strategy='shared', max_queries=24)
            configure(config)
            started = time.monotonic()
            fallback = acquire(min(args.fallback_seconds, remaining), 'real-people-publisher-index')
            timings['publisher_acquisition'] = time.monotonic() - started
    engine.stop_event.clear()
    with metadata_path.open('w', encoding='utf-8') as stream:
        for metadata in public_metadata:
            stream.write(json.dumps(metadata, ensure_ascii=False) + '\n')
    coverage = acquisition_coverage(engine.store, news.sources())
    # This is an explicit diagnostic partial assessment, not a UI bypass or a
    # claim that unfinished acquisition completed. Its limitation is immutable.
    provenance = {'mode': 'real_people_diagnostic_validation', 'partial': True,
                  'public_phase': public_coverage, 'observed': coverage,
                  'pending_acquisition_is_not_completed': True}
    engine.store.audit('real_people_partial_analysis_requested', provenance)
    print('Atribuyendo cuerpos originales y calculando resultados diagnósticos; sin confirmaciones humanas automáticas.', flush=True)
    started = time.monotonic()
    attribution = news.attribute(limit=None, rescore=False)
    timings['attribution'] = time.monotonic() - started
    started = time.monotonic()
    scored = engine.rescore(acquisition=provenance)
    timings['scoring'] = time.monotonic() - started
    started = time.monotonic()
    with engine.store.connection() as con:
        run_ids = [row[0] for row in con.execute('SELECT run_id FROM scores')]
    replays = [engine.replay_score(identifier)['matched'] for identifier in run_ids]
    timings['score_replay'] = time.monotonic() - started
    cumulative_http_events = http_history(directory)
    session_http_history = [event for event in cumulative_http_events if event['id'] > previous_http_id]
    rows, score_summary = person_report(engine, people, public_jobs, cumulative_http_events)
    session_rows, _ = person_report(engine, people, public_jobs, http_events)
    session_by_id = {row['id']: row for row in session_rows}
    for row in rows:
        current = session_by_id[row['id']]
        row['session_query_http_requests'] = current['search'].get('query_http_requests', 0)
        row['queried_over_network_this_session'] = current['queried_over_network']
        row['search_http_scope'] = 'Cumulative actual network request_history; completion requires a durable discovery receipt.'
    articles = article_report(engine.store)
    cold_counts, original_queue = db_counts(engine.store), queue_receipt(engine.store)
    original_network = network_state(directory)
    original_audit = engine.store.verify_audit()
    corpus_before_local = corpus_snapshot(engine)
    cache_before_local = cached_snapshot(directory)
    final_public_by_id = {job['id']: job for job in public_jobs}
    with engine.store.connection() as con:
        current_score_ids = {row[0] for row in con.execute('SELECT id FROM score_runs')}
    print('Verificando caché en una copia independiente; DNS y TCP bloqueados.', flush=True)
    started = time.monotonic()
    copy_bank(directory, local_directory)
    local_engine = Engine(local_directory, online=False)
    local_news = NewsAcquirer(local_engine)
    local_config = deepcopy(config)
    local_config['search'].update(strategy='public', max_queries=len(people))
    local_news.config_path.write_text(json.dumps(local_config, ensure_ascii=False, indent=2), encoding='utf-8')
    with patch('socket.getaddrinfo', side_effect=AssertionError('DNS forbidden in local validation')) as dns, \
            patch('socket.socket.connect', side_effect=AssertionError('TCP forbidden in local validation')) as tcp:
        local = local_news.acquire(workers=args.workers, seconds=args.local_seconds, queue=True,
            window='real-people-local-only', max_articles=10000, relevant_only=True,
            body_budget=args.body_budget, per_entity_body_limit=args.per_person_body, cache_mode='local')
        corpus_after_first_local = corpus_snapshot(local_engine)
        first_local_counts = db_counts(local_engine.store)
        enrichment = verify_cache_enrichment(local_news, corpus_before_local, corpus_after_first_local, cache_before_local)
        first_pass_strict_counts = {table: first_local_counts[table] == cold_counts[table]
                                   for table in ('news_articles', 'news_observations', 'documents')}
        repeated_local = local_news.acquire(workers=args.workers, seconds=args.local_seconds, queue=True,
            window='real-people-local-current-model-repeat', max_articles=10000, relevant_only=True,
            body_budget=args.body_budget, per_entity_body_limit=args.per_person_body, cache_mode='local')
        corpus_after_repeated_local = corpus_snapshot(local_engine)
    repeat_checks = {table: corpus_after_first_local[table] == corpus_after_repeated_local[table]
                     for table in corpus_after_first_local if table != 'audit'}
    repeat_checks['historical_audit_prefix_unchanged'] = all(corpus_after_repeated_local['audit'].get(key) == value
        for key, value in corpus_after_first_local['audit'].items())
    repeat_checks['cached_http_bytes_and_metadata_unchanged'] = cached_snapshot(local_directory) == cache_before_local
    timings['local_cache_copy_and_reuse'] = time.monotonic() - started
    timings['total'] = time.monotonic() - began
    local_counts = db_counts(local_engine.store)
    after_hashes = source_hashes()
    content_documents = defaultdict(set)
    for article in articles:
        if article['scope'] == 'full_article':
            content_documents[(article['publisher_id'], article['body_sha256'])].add(article['document_id'])
    checks = {'source_unchanged_during_run': hashes == after_hashes, 'audit_ok': original_audit['ok'],
        'all_imported_people_scored': scored == len(people) == cold_counts['scores'],
        'all_scores_replay': len(replays) == len(people) and all(replays),
        'all_people_queries_planned': all(row['search'].get('queries_planned', 0) > 0 for row in rows),
        'previous_done_public_queries_preserved': all(final_public_by_id.get(identifier, {}).get('status') == 'done'
                                                     for identifier in previous_done_ids),
        'previous_score_history_preserved': previous_score_ids <= current_score_ids,
        'no_dns_in_local_phase': dns.call_count == 0, 'no_tcp_in_local_phase': tcp.call_count == 0,
        'no_http_in_local_phase': local.get('session', {}).get('network_requests') == 0 and repeated_local.get('session', {}).get('network_requests') == 0,
        'local_copy_audit_ok': local_engine.store.verify_audit()['ok'],
        'local_cache_enrichment_has_exact_model_snapshot_policy_lineage': enrichment['passed'],
        'local_current_model_replay_is_exactly_idempotent': all(repeat_checks.values()),
        'local_cache_did_not_duplicate_candidates': enrichment['checks']['canonical_urls_unique'] and repeat_checks['news_articles'],
        'local_cache_did_not_duplicate_observations': enrichment['checks']['no_same_model_snapshot_policy_observation_duplicates'] and repeat_checks['news_observations'],
        'local_cache_did_not_duplicate_bodies': first_pass_strict_counts['documents'] and repeat_checks['documents'],
        'cold_bank_counts_unchanged_by_local_copy': db_counts(engine.store) == cold_counts,
        'cold_queue_unchanged_by_local_copy': queue_receipt(engine.store) == original_queue,
        'cold_host_backoff_unchanged_by_local_copy': network_state(directory) == original_network,
        'canonical_urls_unique': len({row['canonical_url'] for row in articles}) == len(articles),
        'same_publisher_body_content_deduplicated': all(len(identifiers) == 1 for identifiers in content_documents.values()),
        'metadata_not_reported_as_body': all(not row['body_chars'] for row in articles if row['scope'] != 'full_article'),
        'original_bodies_have_source_snapshots': all(row['body_chars'] > 0 and row['raw_snapshot_sha256']
                                                   for row in articles if row['scope'] == 'full_article')}
    coverage_checks = {'all_people_queried_over_network': all(row['queried_over_network'] for row in rows),
                       'all_people_search_completed': all(row['search_completed'] for row in rows),
                       'actual_local_http_cache_reuse_observed': local.get('session', {}).get('http_cache_hits', 0) > 0,
                       'at_least_one_original_body': any(row['scope'] == 'full_article' for row in articles)}
    report = {'recorded_at': now(), 'method': {'real_transport': True, 'real_clock': True,
        'responses_substituted': False, 'production_pipeline': True, 'synthetic_documents_imported': False,
        'automatic_human_confirmations': False, 'partial_analysis_explicitly_audited': True,
        'local_phase': 'Independent SQLite/file copy; all DNS/TCP forbidden; cold queue and backoff preserved.',
        'identity_accuracy_or_risk_prediction_validated': False, 'raw_bodies_exported': False},
        'roster': roster, 'parameters': {key: str(value) if isinstance(value, Path) else value
                                       for key, value in vars(args).items()},
        'checks': checks, 'coverage_checks': coverage_checks,
        'integrity_passed': all(checks.values()), 'complete_300_live_search_passed': all(coverage_checks.values()) and not args.allow_small_roster,
        'summary': {'people_imported': len(people), 'people_network_queried': sum(row['queried_over_network'] for row in rows),
            'people_network_queried_this_session': sum(row['queried_over_network_this_session'] for row in rows),
            'cumulative_http_requests': sum(event['network_requests'] for event in cumulative_http_events),
            'session_http_requests': sum(event['network_requests'] for event in session_http_history),
            'people_search_completed': sum(row['search_completed'] for row in rows),
            'people_not_network_queried': sum(not row['queried_over_network'] for row in rows),
            'people_with_relevant_candidates': sum(row['relevant_candidates'] > 0 for row in rows),
            'people_with_relevant_full_bodies': sum(row['relevant_full_bodies'] > 0 for row in rows),
            'groups': {group: {'imported': sum(row['entity_group'] == group for row in rows),
                'network_queried': sum(row['queried_over_network'] and row['entity_group'] == group for row in rows),
                'search_completed': sum(row['search_completed'] and row['entity_group'] == group for row in rows)}
                for group in ('municipal_president', 'notary')}, **score_summary},
        'timings_seconds': timings, 'http_events': http_events, 'fetch_events': fetch_events,
        'resume': {'enabled': args.resume, 'previous_done_public_queries': len(previous_done_ids),
                   'previous_http_requests': sum(event['network_requests'] for event in history_before),
                   'same_nominal_window': 'real-people-public', 'completed_queries_revalidated': False},
        'cumulative_http_events': cumulative_http_events, 'session_http_history': session_http_history,
        'cumulative_http_status_counts': dict(Counter(str(event['http_status']) for event in cumulative_http_events)),
        'http_status_counts': dict(Counter(str(event['http_status']) for event in http_events)),
        'pre_filter_public_metadata': {'path': str(metadata_path),
            'sha256': hashlib.sha256(metadata_path.read_bytes()).hexdigest(),
            'responses': len(public_metadata), 'records': sum(len(value.get('records', [])) for value in public_metadata),
            'contents_exported_in_report': False},
        'fetch_outcome_counts': dict(Counter(str(event['error_code'] or event['status']) for event in fetch_events)),
        'public_acquisition': public, 'public_coverage_before_fallback': public_coverage,
        'publisher_acquisition': fallback, 'final_coverage': coverage, 'attribution': attribution,
        'counts': cold_counts, 'public_jobs_before_fallback': public_jobs, 'final_jobs': original_queue,
        'articles': articles, 'people': rows, 'audit': original_audit, 'network_state': original_network,
        'local_acquisition': local, 'local_counts': local_counts,
        'local_cache_verification': {'first_pass_strict_counts_equal_cold': first_pass_strict_counts,
            'first_pass_counts': first_local_counts,
            'first_pass_count_deltas': {table: first_local_counts[table] - cold_counts[table] for table in first_pass_strict_counts},
            'enrichment_lineage': enrichment, 'second_pass': repeated_local, 'second_pass_exact_checks': repeat_checks,
            'first_pass_row_sha256': {table: digest(canonical(value)) for table, value in corpus_after_first_local.items()},
            'second_pass_row_sha256': {table: digest(canonical(value)) for table, value in corpus_after_repeated_local.items()},
            'note': 'First-pass metadata growth is reported explicitly, never called idempotence. Only a verified version migration and an exact second replay can pass.'},
        'progress_event_count': len(progress_events), 'source_code_sha256': hashes,
        'limitations': ['Search names and top results are not verified identities or exhaustive history.',
            'Zero scores may mean insufficient source text, unresolved identity or no assessable findings.',
            'Public HTTP backoff, robots restrictions, omissions and incomplete jobs remain visible.',
            'Scores are diagnostic document-review outputs; no guilt or absence of risk is established.']}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'output': str(args.output), 'summary': report['summary'], 'checks': checks,
                      'coverage_checks': coverage_checks, 'timings': timings}, ensure_ascii=False), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--online', action='store_true')
    parser.add_argument('--roster', type=Path, required=True)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--resume', action='store_true', help='Resume only the identical provenance-backed roster and nominal plan; completed searches are retained')
    parser.add_argument('--local-dir', type=Path, help='New independent local-cache validation copy; existing destinations are refused')
    parser.add_argument('--sources', default='mwmbl_web,duckduckgo_web')
    parser.add_argument('--body-sources', default='contralinea_index,aristegui_index,desinformemonos_index')
    parser.add_argument('--seconds', type=float, default=900)
    parser.add_argument('--public-seconds', type=float, default=810)
    parser.add_argument('--fallback-seconds', type=float, default=90)
    parser.add_argument('--local-seconds', type=float, default=60)
    parser.add_argument('--max-requests', type=int, default=700)
    parser.add_argument('--workers', type=int, default=8)
    parser.add_argument('--body-budget', type=int, default=90)
    parser.add_argument('--per-person-body', type=int, default=3)
    parser.add_argument('--start', default='2016-01-01')
    parser.add_argument('--end', default=now()[:10])
    parser.add_argument('--allow-small-roster', action='store_true', help='Explicit pilot; never reported as a complete 300-person test')
    args = parser.parse_args()
    if (not args.online or not 0 < args.seconds <= 900 or not 0 < args.public_seconds <= args.seconds
            or not 0 <= args.fallback_seconds <= 900 or not 0 < args.local_seconds <= 120
            or not 1 <= args.max_requests <= 1000 or not 1 <= args.workers <= 16
            or not 0 <= args.body_budget <= 900 or not 0 <= args.per_person_body <= 3):
        parser.error('Requiere --online, presupuestos positivos de hasta 900s/1000 HTTP/16 workers/900 cuerpos/3 por persona.')
    try:
        report = validate(args)
    except ValueError as exc:
        parser.error(str(exc))
    if not report['integrity_passed']:
        raise SystemExit(1)
    if not args.allow_small_roster and not report['complete_300_live_search_passed']:
        raise SystemExit(2)


if __name__ == '__main__':
    main()
