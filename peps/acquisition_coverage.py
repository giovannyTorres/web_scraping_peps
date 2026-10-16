"""Observed acquisition coverage for the current bank, without NLP on reads.

Plans and queued jobs do not count as completed searches. Candidate IDs come
from literal metadata relevance or existing identity links, never query terms.
Local source documents are reported separately from acquired news articles.
"""
from __future__ import annotations

import json


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


def _fallback(con, tables, result):
    ids = {row[0] for row in con.execute('SELECT id FROM entities')}
    queried, pairs, bodies = set(), set(), set()
    article_rows = list(con.execute('SELECT id,document_id,scope FROM news_articles')) if 'news_articles' in tables else []
    articles = {row['id']: row['document_id'] for row in article_rows}
    full_articles = {row['id'] for row in article_rows if row['scope'] == 'full_article'}
    if 'news_discovery_runs' in tables:
        for row in con.execute('SELECT j.kind,j.payload,r.parser_metadata FROM news_discovery_runs r JOIN jobs j ON j.id=r.job_id'):
            payload = _json(row['payload'])
            targets = {str(value) for value in payload.get('entity_ids', [])} & ids
            if row['kind'] == 'news_query':
                queried.update(targets)
            if row['kind'] == 'news_feed' or targets:
                count = _json(row['parser_metadata']).get('candidate_filter') or {}
                if isinstance(count, dict) and type(count.get('excluded_records')) is int:
                    result['excluded_records'] += max(0, count['excluded_records'])
    if 'news_observations' in tables:
        for row in con.execute('SELECT article_id,details FROM news_observations'):
            relevant = _json(row['details']).get('retrieval_relevance') or {}
            if isinstance(relevant, dict) and relevant.get('relevant') is True:
                pairs.update((row['article_id'], str(value)) for value in relevant.get('entity_ids', []) if str(value) in ids)
    if 'news_identity_links' in tables:
        by_document = {}
        for article, document in articles.items():
            by_document.setdefault(document, []).append(article)
        for row in con.execute('SELECT document_id,entity_id,resolution FROM news_identity_links'):
            if row['entity_id'] in ids and _json(row['resolution']).get('status') != 'rejected':
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
                  metadata_only_articles=len(relevant_articles - bodies), local_documents=len(local_documents),
                  local_entities_with_bodies=len(local_entities))
    return result


def acquisition_coverage(store, sources):
    result = {'entities_total': 0, 'entities_queried': 0, 'entities_with_candidates': 0,
              'entities_with_bodies': 0, 'body_articles': 0, 'relevant_articles': 0,
              'metadata_only_articles': 0, 'excluded_records': 0,
              'local_documents': 0, 'local_entities_with_bodies': 0, 'permissions': _permissions(sources)}
    with store.connection() as con:
        con.execute('BEGIN')
        result['entities_total'] = con.execute('SELECT count(*) FROM entities').fetchone()[0]
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not store._audit_json:
            return _fallback(con, tables, result)
        observed = ("SELECT o.article_id,CAST(v.value AS TEXT) AS entity_id FROM news_observations o "
                    "JOIN json_each(CASE WHEN json_valid(o.details) THEN o.details ELSE '{}' END,'$.retrieval_relevance.entity_ids') v "
                    "JOIN entities e ON e.id=CAST(v.value AS TEXT) WHERE json_extract(o.details,'$.retrieval_relevance.relevant')=1") if 'news_observations' in tables else "SELECT NULL AS article_id,NULL AS entity_id WHERE 0"
        linked = ("SELECT n.id AS article_id,l.entity_id FROM news_identity_links l JOIN entities e ON e.id=l.entity_id "
                  "JOIN news_articles n ON n.document_id=l.document_id "
                  "WHERE COALESCE(json_extract(l.resolution,'$.status'),'candidate')!='rejected'") if 'news_articles' in tables else "SELECT NULL AS article_id,NULL AS entity_id WHERE 0"
        body_sql = ("SELECT n.id AS article_id FROM news_articles n JOIN documents d ON d.id=n.document_id "
                    "WHERE n.scope='full_article' AND d.source_kind NOT IN ('discovery','legacy') "
                    "AND NOT COALESCE(json_extract(d.metadata,'$.discovery_only'),0) "
                    "AND NOT COALESCE(json_extract(d.metadata,'$.requires_source_revalidation'),0)") if 'news_articles' in tables else "SELECT NULL AS article_id WHERE 0"
        counts = con.execute('WITH pairs AS (' + observed + ' UNION ' + linked + '), bodies AS (' + body_sql + ') '
                             'SELECT count(DISTINCT p.entity_id),count(DISTINCT CASE WHEN b.article_id IS NOT NULL THEN p.entity_id END),'
                             'count(DISTINCT p.article_id),count(DISTINCT b.article_id) FROM pairs p LEFT JOIN bodies b ON b.article_id=p.article_id').fetchone()
        result.update(entities_with_candidates=counts[0], entities_with_bodies=counts[1],
                      relevant_articles=counts[2], body_articles=counts[3], metadata_only_articles=counts[2] - counts[3])
        if 'news_discovery_runs' in tables:
            result['entities_queried'] = con.execute("SELECT count(DISTINCT e.id) FROM news_discovery_runs r "
                "JOIN jobs j ON j.id=r.job_id JOIN json_each(j.payload,'$.entity_ids') v JOIN entities e ON e.id=CAST(v.value AS TEXT) "
                "WHERE j.kind='news_query'").fetchone()[0]
            result['excluded_records'] = con.execute("SELECT COALESCE(sum(max(0,COALESCE(json_extract(r.parser_metadata,'$.candidate_filter.excluded_records'),0))),0) "
                "FROM news_discovery_runs r JOIN jobs j ON j.id=r.job_id WHERE j.kind='news_feed' OR EXISTS "
                "(SELECT 1 FROM json_each(j.payload,'$.entity_ids') v JOIN entities e ON e.id=CAST(v.value AS TEXT))").fetchone()[0]
        news_guard = 'AND d.id NOT IN (SELECT document_id FROM news_articles WHERE document_id IS NOT NULL)' if 'news_articles' in tables else ''
        local = con.execute("SELECT count(DISTINCT d.id),count(DISTINCT e.entity_id) FROM evidence e "
            "JOIN entities current ON current.id=e.entity_id JOIN documents d ON d.id=e.document_id "
            "WHERE d.source_kind NOT IN ('discovery','legacy') AND NOT COALESCE(json_extract(d.metadata,'$.discovery_only'),0) "
            "AND NOT COALESCE(json_extract(d.metadata,'$.requires_source_revalidation'),0) " + news_guard).fetchone()
        result.update(local_documents=local[0], local_entities_with_bodies=local[1])
    return result
