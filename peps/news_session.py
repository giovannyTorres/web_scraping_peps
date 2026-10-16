"""Small persisted acquisition counters, independent of historical evidence."""
from __future__ import annotations

import json
import threading
import time
import uuid

from .store import canonical, now


class AcquisitionSession:
    def __init__(self, store, cache_mode):
        self.store, self.lock, self.save_lock = store, threading.Lock(), threading.Lock()
        self.values = dict(id=uuid.uuid4().hex, started_at=now(), finished_at=None,
                           cache_mode=cache_mode, network_requests=0, http_cache_hits=0,
                           metadata_cache_hits=0, bodies_new=0, bodies_reused=0,
                           duplicate_candidates=0, excluded_records=0)
        self.new_bodies, self.reused_bodies, self.saved_at = set(), set(), -float('inf')
        self._publication_failed = False
        self.save(force=True)

    def context(self, *, bank_revision, scope_hash):
        with self.lock:
            self.values.update(bank_revision=str(bank_revision), scope_hash=scope_hash)
        self.save(force=True)

    def note(self, **counters):
        with self.lock:
            for key, value in counters.items():
                if key not in self.values:
                    continue
                current = self.values[key]
                self.values[key] = current + value if type(current) is int and type(value) is int else None

    def body(self, article_id, *, new=False):
        with self.lock:
            # An article belongs to one category. A real upgrade supersedes
            # earlier reuse, so only that reclassification can lower reused;
            # rediscovery after acquisition must not count the same body twice.
            if new:
                self.reused_bodies.discard(article_id)
                self.new_bodies.add(article_id)
            elif article_id not in self.new_bodies:
                self.reused_bodies.add(article_id)

    def snapshot(self):
        with self.lock:
            return dict(self.values, bodies_new=len(self.new_bodies), bodies_reused=len(self.reused_bodies))

    def save(self, *, force=False, finished=False):
        # Serialize publication, not counter updates. A slow writer must never
        # publish an older snapshot after a newer worker has already saved it.
        if not self.save_lock.acquire(blocking=force):
            return
        try:
            with self.lock:
                if not force and not self._publication_failed and time.monotonic() - self.saved_at < 1:
                    return
                finish = finished and self.values['finished_at'] is None
                snapshot = dict(self.values, finished_at=now() if finish else self.values['finished_at'],
                                bodies_new=len(self.new_bodies), bodies_reused=len(self.reused_bodies))
            with self.store.connection() as con:
                con.execute('BEGIN IMMEDIATE')
                con.execute("INSERT OR REPLACE INTO meta VALUES('news_last_session',?)", (canonical(snapshot),))
                if finish:
                    self.store._audit(con, 'news_acquisition_session_finished', snapshot)
            # The context manager commits on exit. A failed write or commit
            # must leave both the finish marker and throttle unconfirmed.
            with self.lock:
                self.saved_at = time.monotonic()
                self._publication_failed = False
                if finish:
                    self.values['finished_at'] = snapshot['finished_at']
        except BaseException:
            with self.lock:
                self._publication_failed = True
            raise
        finally:
            self.save_lock.release()


def last_session(store):
    with store.connection() as con:
        row = con.execute("SELECT value FROM meta WHERE key='news_last_session'").fetchone()
    try:
        value = json.loads(row[0]) if row else None
        return value if isinstance(value, dict) else None
    except (ValueError, TypeError):
        return None
