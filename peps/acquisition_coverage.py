"""Observed acquisition coverage for the current bank, without NLP on reads.

Plans and queued jobs do not count as completed searches. Candidate IDs come
from literal metadata relevance or existing identity links, never query terms.
Local source documents are reported separately from acquired news articles.
"""
from __future__ import annotations

import json
from copy import deepcopy

from .news_projection import projection_ready, projection_signature


def _permissions(sources):
    prepared = [source for source in sources if source.get('enabled') and source.get('terms_approved')
                and source.get('automated_search') is not False]
    bodies = [source for source in prepared if source.get('download_articles')
              and (source.get('article_terms_approved') or source.get('body_terms_approved'))]
    return {'prepared_sources': len(prepared), 'body_authorized_sources': len(bodies),
            'discovery_only_sources': len(prepared) - len(bodies)}


def _json(value):
    try:
        result = json.loads(value)
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError):
        return {}


def _object_sql(column):
    # CASE is deliberately nested: SQLite may evaluate json_type before a
    # neighboring json_valid predicate and raise on malformed legacy JSON.
    return (f"CASE WHEN json_valid({column}) THEN CASE WHEN json_type({column})='object' "
            f"THEN {column} ELSE '{{}}' END ELSE '{{}}' END")


def _shared_coverage(con, tables, result, *, json_sql):
    """Count observed local index scans independently from directed searches.

    Parsed-page caches can be reused for another bank revision, but only a
    completed discovery receipt proves that the current bank was checked.
    """
    row = con.execute("SELECT value FROM meta WHERE key='news_identity_revision'").fetchone()
    if row is None:
        return
    revision = row[0]
    if not json_sql:
        current_ids = {row[0] for row in con.execute('SELECT id FROM entities')}
        checked = set()
        if 'news_discovery_runs' in tables:
            for row in con.execute('SELECT parser_metadata FROM news_discovery_runs'):
                metadata = _json(row[0])
                if metadata.get('shared_index') is not True or metadata.get('bank_revision') != revision:
                    continue
                result['shared_pages_read'] += 1
                candidate_filter = metadata.get('candidate_filter')
                count = candidate_filter.get('input_records') if isinstance(candidate_filter, dict) else None
                if type(count) is int:
                    result['shared_records_checked'] += max(0, count)
                identifiers = metadata.get('locally_checked_entity_ids')
                if isinstance(identifiers, list):
                    checked.update(str(identifier) for identifier in identifiers if str(identifier) in current_ids)
                continuation = metadata.get('continuation')
                pagination = metadata.get('pagination')
                limited = (isinstance(continuation, dict) and continuation.get('page_limit_reached') is True
                           or isinstance(pagination, dict) and pagination.get('truncated') is True)
                result['shared_pages_limited'] += int(limited)
        result['entities_checked_locally'] = len(checked)
        for row in con.execute("SELECT payload FROM jobs WHERE kind='news_feed' AND status IN ('queued','deferred','running')"):
            payload = _json(row[0])
            if payload.get('shared_index') is True and payload.get('bank_revision') == revision:
                result['shared_pages_pending'] += 1
        return

    if 'news_discovery_runs' in tables:
        receipts = ("WITH parsed AS (SELECT " + _object_sql('parser_metadata') + " AS metadata "
                    "FROM news_discovery_runs), current AS (SELECT metadata FROM parsed "
                    "WHERE json_type(metadata,'$.shared_index')='true' AND json_extract(metadata,'$.bank_revision')=?) ")
        counts = con.execute(receipts + "SELECT count(*),COALESCE(sum(CASE "
            "WHEN json_type(metadata,'$.candidate_filter.input_records')='integer' "
            "THEN max(0,json_extract(metadata,'$.candidate_filter.input_records')) ELSE 0 END),0),"
            "COALESCE(sum(json_type(metadata,'$.continuation.page_limit_reached')='true' "
            "OR json_type(metadata,'$.pagination.truncated')='true'),0) FROM current", (revision,)).fetchone()
        result.update(shared_pages_read=counts[0], shared_records_checked=counts[1], shared_pages_limited=counts[2])
        result['entities_checked_locally'] = con.execute(receipts +
            "SELECT count(DISTINCT e.id) FROM current r JOIN json_each(r.metadata,'$.locally_checked_entity_ids') v "
            "JOIN entities e ON e.id=CAST(v.value AS TEXT) "
            "WHERE json_type(r.metadata,'$.locally_checked_entity_ids')='array'", (revision,)).fetchone()[0]
    result['shared_pages_pending'] = con.execute("WITH parsed AS (SELECT "
        + _object_sql('payload') + " AS payload FROM jobs "
        "WHERE kind='news_feed' AND status IN ('queued','deferred','running')) SELECT count(*) FROM parsed "
        "WHERE json_type(payload,'$.shared_index')='true' AND json_extract(payload,'$.bank_revision')=?", (revision,)).fetchone()[0]


def _fallback(con, tables, result, revision):
    ids = {row[0] for row in con.execute('SELECT id FROM entities')}
    queried, pairs, bodies = set(), set(), set()
    article_rows = list(con.execute('SELECT id,document_id,scope FROM news_articles')) if 'news_articles' in tables else []
    articles = {row['id']: row['document_id'] for row in article_rows}
    full_articles = {row['id'] for row in article_rows if row['scope'] == 'full_article'}
    multimedia_references = {row['id'] for row in article_rows if row['scope'] == 'multimedia_reference'}
    if 'news_discovery_runs' in tables:
        for row in con.execute('SELECT j.kind,j.payload,r.parser_metadata FROM news_discovery_runs r JOIN jobs j ON j.id=r.job_id'):
            payload = _json(row['payload'])
            raw_targets = payload.get('entity_ids')
            targets = {str(value) for value in raw_targets} & ids if isinstance(raw_targets, list) else set()
            if str(payload.get('bank_revision')) != revision:
                continue
            if row['kind'] == 'news_query':
                queried.update(targets)
            if row['kind'] == 'news_feed' or targets:
                count = _json(row['parser_metadata']).get('candidate_filter') or {}
                if isinstance(count, dict) and type(count.get('excluded_records')) is int:
                    result['excluded_records'] += max(0, count['excluded_records'])
    if result['current_bank_index_ready']:
        pairs.update((row['article_id'], row['entity_id']) for row in con.execute(
            'SELECT article_id,entity_id FROM news_current_candidates WHERE bank_revision=? AND relevance_version=?',
            (revision, projection_signature(con)['relevance_version'])) if row['entity_id'] in ids)
    if 'news_identity_links' in tables:
        by_document = {}
        for article, document in articles.items():
            by_document.setdefault(document, []).append(article)
        for row in con.execute('SELECT document_id,entity_id,resolution FROM news_identity_links'):
            resolution = _json(row['resolution'])
            if (row['entity_id'] in ids and resolution.get('status') != 'rejected'
                    and str(resolution.get('bank_revision')) == revision):
                pairs.update((article, row['entity_id']) for article in by_document.get(row['document_id'], []))
    valid_documents = set()
    for row in con.execute("SELECT id,metadata FROM documents WHERE source_kind NOT IN ('discovery','legacy')"):
        metadata = _json(row['metadata'])
        if not metadata.get('discovery_only') and not metadata.get('requires_source_revalidation'):
            valid_documents.add(row['id'])
    bodies = {article for article, document in articles.items() if document in valid_documents and article in full_articles}
    relevant_articles = {article for article, _ in pairs}
    body_pairs = {(article, entity) for article, entity in pairs if article in bodies}
    local_documents, local_entities = set(), set()
    news_documents = set(articles.values())
    for row in con.execute('SELECT document_id,entity_id FROM evidence'):
        if row['entity_id'] in ids and row['document_id'] in valid_documents and row['document_id'] not in news_documents:
            local_documents.add(row['document_id'])
            local_entities.add(row['entity_id'])
    result.update(entities_queried=len(queried), entities_with_candidates=len({entity for _, entity in pairs}),
                  entities_with_bodies=len({entity for _, entity in body_pairs}),
                  relevant_articles=len(relevant_articles), body_articles=len({article for article, _ in body_pairs}),
                  metadata_only_articles=len(relevant_articles - bodies),
                  multimedia_reference_articles=len(relevant_articles & multimedia_references),
                  pending_text_articles=len(relevant_articles - bodies - multimedia_references),
                  local_documents=len(local_documents),
                  local_entities_with_bodies=len(local_entities))
    return result


def acquisition_coverage(store, sources):
    result = {'entities_total': 0, 'entities_queried': 0, 'entities_with_candidates': 0,
              'entities_with_bodies': 0, 'body_articles': 0, 'relevant_articles': 0,
              'metadata_only_articles': 0, 'multimedia_reference_articles': 0,
              'pending_text_articles': 0, 'excluded_records': 0,
              'shared_pages_read': 0, 'shared_records_checked': 0, 'entities_checked_locally': 0,
              'shared_pages_pending': 0, 'shared_pages_limited': 0,
              'local_documents': 0, 'local_entities_with_bodies': 0, 'permissions': _permissions(sources),
              'current_bank_index_ready': False, 'current_bank_revision': None,
              'session_matches_bank': None, 'session': None}
    with store.connection() as con:
        con.execute('BEGIN')
        signature = projection_signature(con)
        revision = signature['bank_revision']
        meta = dict(con.execute("SELECT key,value FROM meta WHERE key IN ('news_data_revision','news_last_session')"))
        ready = projection_ready(con)
        result.update(current_bank_index_ready=ready, current_bank_revision=revision)
        try:
            session = json.loads(meta.get('news_last_session', 'null'))
        except (ValueError, TypeError):
            session = None
        if not isinstance(session, dict):
            session = None
        session_revision = session.get('bank_revision') if session is not None else None
        session_fields = {'session': session, 'current_bank_revision': revision,
                          'session_matches_bank': str(session_revision) == revision if session_revision is not None else None}
        token = (meta.get('news_data_revision'), revision, signature['relevance_version'], ready, store._audit_json)
        cache = getattr(store, '_news_acquisition_coverage_cache', None)
        if meta.get('news_data_revision') is not None and cache is not None and cache[0] == token:
            value = deepcopy(cache[1])
            value.update(session_fields, permissions=_permissions(sources))
            return value
        result['entities_total'] = con.execute('SELECT count(*) FROM entities').fetchone()[0]
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        _shared_coverage(con, tables, result, json_sql=store._audit_json)
        if not store._audit_json:
            result = _fallback(con, tables, result, revision)
            store._news_acquisition_coverage_cache = (token, deepcopy(result))
            result.update(session_fields)
            return result
        observed = ("SELECT p.article_id,p.entity_id FROM news_current_candidates p JOIN entities e ON e.id=p.entity_id "
                    "WHERE p.bank_revision=? AND p.relevance_version=?") if ready and 'news_current_candidates' in tables else "SELECT NULL AS article_id,NULL AS entity_id WHERE 0 AND ?=?"
        linked = ("SELECT n.id AS article_id,l.entity_id FROM news_identity_links l JOIN entities e ON e.id=l.entity_id "
                  "JOIN news_articles n ON n.document_id=l.document_id "
                  "WHERE COALESCE(json_extract(CASE WHEN json_valid(l.resolution) THEN l.resolution ELSE '{}' END,'$.status'),'candidate')!='rejected' "
                  "AND CAST(json_extract(CASE WHEN json_valid(l.resolution) THEN l.resolution ELSE '{}' END,'$.bank_revision') AS TEXT)=?") if 'news_articles' in tables else "SELECT NULL AS article_id,NULL AS entity_id WHERE 0 AND ? IS NULL"
        body_sql = ("SELECT n.id AS article_id FROM news_articles n JOIN documents d ON d.id=n.document_id "
                    "WHERE n.scope='full_article' AND d.source_kind NOT IN ('discovery','legacy') "
                    "AND NOT COALESCE(json_extract(d.metadata,'$.discovery_only'),0) "
                    "AND NOT COALESCE(json_extract(d.metadata,'$.requires_source_revalidation'),0)") if 'news_articles' in tables else "SELECT NULL AS article_id WHERE 0"
        multimedia_sql = ("SELECT id AS article_id FROM news_articles WHERE scope='multimedia_reference'"
                          if 'news_articles' in tables else 'SELECT NULL AS article_id WHERE 0')
        counts = con.execute('WITH pairs AS (' + observed + ' UNION ' + linked + '), bodies AS (' + body_sql + '), '
                             'multimedia AS (' + multimedia_sql + ') '
                             'SELECT count(DISTINCT p.entity_id),count(DISTINCT CASE WHEN b.article_id IS NOT NULL THEN p.entity_id END),'
                             'count(DISTINCT p.article_id),count(DISTINCT b.article_id),count(DISTINCT m.article_id) '
                             'FROM pairs p LEFT JOIN bodies b ON b.article_id=p.article_id '
                             'LEFT JOIN multimedia m ON m.article_id=p.article_id',
                             (revision, signature['relevance_version'], revision)).fetchone()
        result.update(entities_with_candidates=counts[0], entities_with_bodies=counts[1],
                      relevant_articles=counts[2], body_articles=counts[3], metadata_only_articles=counts[2] - counts[3],
                      multimedia_reference_articles=counts[4], pending_text_articles=counts[2] - counts[3] - counts[4])
        if 'news_discovery_runs' in tables:
            # Normalize corrupt/scalar legacy payloads before any json_each or
            # json_extract. A malformed receipt never mutates the stored job.
            receipts = ("WITH parsed AS (SELECT j.kind," + _object_sql('j.payload') + " AS payload,"
                        + _object_sql('r.parser_metadata') + " AS metadata "
                        "FROM news_discovery_runs r JOIN jobs j ON j.id=r.job_id), "
                        "current AS (SELECT * FROM parsed WHERE CAST(json_extract(payload,'$.bank_revision') AS TEXT)=?) ")
            counts = con.execute(receipts + "SELECT (SELECT count(DISTINCT e.id) FROM current r "
                "JOIN json_each(r.payload,'$.entity_ids') v JOIN entities e ON e.id=CAST(v.value AS TEXT) "
                "WHERE r.kind='news_query' AND json_type(r.payload,'$.entity_ids')='array'),"
                "(SELECT COALESCE(sum(CASE WHEN json_type(r.metadata,'$.candidate_filter.excluded_records')='integer' "
                "THEN max(0,json_extract(r.metadata,'$.candidate_filter.excluded_records')) ELSE 0 END),0) "
                "FROM current r WHERE r.kind='news_feed' OR (json_type(r.payload,'$.entity_ids')='array' AND EXISTS "
                "(SELECT 1 FROM json_each(r.payload,'$.entity_ids') v JOIN entities e ON e.id=CAST(v.value AS TEXT))))",
                (revision,)).fetchone()
            result.update(entities_queried=counts[0], excluded_records=counts[1])
        news_guard = 'AND d.id NOT IN (SELECT document_id FROM news_articles WHERE document_id IS NOT NULL)' if 'news_articles' in tables else ''
        local = con.execute("SELECT count(DISTINCT d.id),count(DISTINCT e.entity_id) FROM evidence e "
            "JOIN entities current ON current.id=e.entity_id JOIN documents d ON d.id=e.document_id "
            "WHERE d.source_kind NOT IN ('discovery','legacy') AND NOT COALESCE(json_extract(d.metadata,'$.discovery_only'),0) "
            "AND NOT COALESCE(json_extract(d.metadata,'$.requires_source_revalidation'),0) " + news_guard).fetchone()
        result.update(local_documents=local[0], local_entities_with_bodies=local[1])
        store._news_acquisition_coverage_cache = (token, deepcopy(result))
        result.update(session_fields)
    return result
