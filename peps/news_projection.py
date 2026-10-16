"""Versioned metadata relevance projection for the current entity bank.

Only this disposable lookup is rebuilt. Raw observations, identity history and
source documents remain unchanged. No body parsing, NLP or network is used.
"""
from __future__ import annotations

import json
import sqlite3
import time

from . import relevance as relevance_model
from .store import canonical

_READY_KEY = 'news_projection_ready'


def projection_signature(con):
    row = con.execute("SELECT value FROM meta WHERE key='news_identity_revision'").fetchone()
    return {'bank_revision': str(row[0]) if row else '0',
            'relevance_version': relevance_model.MODEL_VERSION}


def projection_ready(con):
    row = con.execute('SELECT value FROM meta WHERE key=?', (_READY_KEY,)).fetchone()
    if row is None:
        return False
    try:
        return json.loads(row[0]) == projection_signature(con)
    except (TypeError, ValueError):
        return False


def _ready(con, signature):
    con.execute('INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                (_READY_KEY, canonical(signature)))


def _touch(con):
    con.execute("UPDATE meta SET value=CAST(value AS INTEGER)+1 WHERE key='news_data_revision'")


def update_projection(con, article_id, candidate, *, bank_revision=None):
    """Add a current observation without discarding another snippet's IDs.

    The caller holds the observation transaction and supplies the bank revision
    captured atomically with its relevance index. Historical IDs are filtered by
    bank revision and model version; a repeated URL can add another current ID.
    """
    signature = projection_signature(con)
    changed = con.total_changes
    origin = bank_revision if bank_revision is not None else (candidate.get('bank_revision') if isinstance(candidate, dict) else None)
    model = candidate.get('model_version') if isinstance(candidate, dict) else None
    if origin is None or str(origin) != signature['bank_revision'] or model not in (None, signature['relevance_version']):
        # An index built before a bank import must never stamp old names with
        # the new revision. Unknown provenance requests a complete local scan.
        con.execute('DELETE FROM meta WHERE key=?', (_READY_KEY,))
        if con.total_changes != changed:
            _touch(con)
        return
    if isinstance(candidate, dict) and candidate.get('relevant') is True:
        identifiers = sorted({str(value) for value in candidate.get('entity_ids', [])})
        con.executemany('INSERT INTO news_current_candidates(article_id,entity_id,bank_revision,relevance_version) '
                        'SELECT ?,id,?,? FROM entities WHERE id=? '
                        'ON CONFLICT(article_id,entity_id) DO UPDATE SET '
                        'bank_revision=excluded.bank_revision,relevance_version=excluded.relevance_version '
                        'WHERE news_current_candidates.bank_revision!=excluded.bank_revision '
                        'OR news_current_candidates.relevance_version!=excluded.relevance_version',
                        ((article_id, signature['bank_revision'], signature['relevance_version'], identifier)
                         for identifier in identifiers))
    if not projection_ready(con):
        # The first observation of a fresh corpus is already a complete scan.
        # A one-article legacy corpus with many snippets still needs rebuilding.
        articles = con.execute('SELECT id FROM news_articles LIMIT 2').fetchall()
        observations = con.execute('SELECT id FROM news_observations LIMIT 2').fetchall()
        if len(articles) == 1 and articles[0][0] == article_id and len(observations) <= 1:
            _ready(con, signature)
    if con.total_changes != changed:
        _touch(con)


def ensure_projection(news, relevance, deadline=None):
    """Rebuild once per bank/model revision, publishing only a complete scan.

    Keyset pages bound memory. Cancellation or deadline rolls back the derived
    cache, so an interrupted rebuild cannot advertise complete current coverage.
    The bank index is captured inside the write transaction: an import racing
    with the caller's supplied index cannot stamp old names as the new bank.
    """
    def stopped():
        return (news.engine.stop_event.is_set()
                or deadline is not None and time.monotonic() >= deadline)

    with news.store.connection() as con:
        if projection_ready(con):
            return True
        if stopped():
            return False
        lock_deadline = min(deadline if deadline is not None else float('inf'), time.monotonic() + 30)
        while True:
            con.execute('PRAGMA busy_timeout=' + str(max(1, min(250, int((lock_deadline - time.monotonic()) * 1000)))))
            try:
                con.execute('BEGIN IMMEDIATE')
                break
            except sqlite3.OperationalError as exc:
                if 'locked' not in str(exc).lower():
                    raise
                if stopped():
                    return False
                if time.monotonic() >= lock_deadline:
                    if deadline is not None:
                        return False
                    raise
        if projection_ready(con):
            return True
        if stopped():
            con.rollback()
            return False
        signature = projection_signature(con)
        current_index = relevance_model.RelevanceIndex([news.store.entity(row)
                        for row in con.execute('SELECT * FROM entities ORDER BY id')])
        con.execute('DELETE FROM news_current_candidates')
        previous, articles, pairs = '', 0, 0
        while True:
            rows = con.execute('SELECT id,title,summary FROM news_articles WHERE id>? ORDER BY id LIMIT 250',
                               (previous,)).fetchall()
            if not rows:
                break
            for row in rows:
                if stopped():
                    con.rollback()
                    return False
                candidate = news._stored_candidate(row, current_index, connection=con)
                identifiers = sorted({str(value) for value in candidate.get('entity_ids', [])}) if candidate.get('relevant') else []
                con.executemany('INSERT OR IGNORE INTO news_current_candidates VALUES(?,?,?,?)',
                                ((row['id'], identifier, signature['bank_revision'], signature['relevance_version'])
                                 for identifier in identifiers))
                articles += 1
                pairs += len(identifiers)
            previous = rows[-1]['id']
        if stopped():
            con.rollback()
            return False
        _ready(con, signature)
        _touch(con)
        news.store._audit(con, 'news_current_candidates_rebuilt',
                          dict(signature, articles_checked=articles, candidate_pairs=pairs,
                               identity_verified=False, score_effect='none'))
        return True
