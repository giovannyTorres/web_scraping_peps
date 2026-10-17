"""Import ONLY monotonic negative backend/route policy into a future cold DB.

No HTTP, response bodies, observations, requests, query names, gold, permissions
or source configuration cross the boundary. SafeFetcher must initialize the
new DB first. Content/cache/history/observations must all still be empty. This
helper never touches hosts, so separate host/robots/MAX floors remain intact.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlsplit, urlunsplit

EMPTY_TABLES = ('http_cache', 'robots', 'robots_refresh', 'redirects', 'request_history',
                'http_error_diagnostics', 'api_backend_observations')
BACKEND_FIELDS = {'endpoint', 'engine', 'reason', 'first_seen', 'last_seen', 'pending_review', 'evidence_sha256'}
ROUTE_FIELDS = {'endpoint', 'requested_engines', 'reason', 'first_seen', 'last_seen', 'evidence_sha256'}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def strict_json(raw):
    def obj(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate JSON key')
            result[key] = value
        return result
    def reject(_value):
        raise ValueError('Nonfinite JSON constant')
    return json.loads(raw, object_pairs_hook=obj, parse_constant=reject)


def engine(value):
    if (not isinstance(value, str) or not value or len(value) > 128 or ',' in value
            or any(ord(c) < 32 or ord(c) > 126 for c in value)
            or value != ' '.join(value.casefold().split())):
        raise ValueError('Noncanonical backend engine')
    return value


def endpoint(value):
    if not isinstance(value, str):
        raise ValueError('Malformed endpoint')
    parts = urlsplit(value)
    if (parts.scheme != 'https' or parts.username or parts.password or parts.query or parts.fragment
            or parts.port is not None or not parts.hostname or not parts.path.startswith('/')
            or not re.fullmatch(r'[a-z0-9.-]+', parts.hostname)
            or value != urlunsplit(('https', parts.hostname, parts.path, '', ''))):
        raise ValueError('Negative API endpoint must be canonical HTTPS without query, port or credentials')
    return value


def check_row(row, kind):
    expected = BACKEND_FIELDS if kind == 'backend' else ROUTE_FIELDS
    if not isinstance(row, dict) or set(row) != expected:
        raise ValueError('Only exact negative-policy fields may be imported')
    endpoint(row['endpoint'])
    if (not isinstance(row['reason'], str) or not row['reason'].strip() or len(row['reason']) > 300
            or not isinstance(row['evidence_sha256'], str) or not re.fullmatch(r'[a-f0-9]{64}', row['evidence_sha256'])):
        raise ValueError('Missing bounded policy reason/evidence hash')
    for field in ('first_seen', 'last_seen'):
        value = row[field]
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError('Invalid policy observation time')
    if row['first_seen'] > row['last_seen']:
        raise ValueError('Reversed observation interval')
    if kind == 'backend':
        engine(row['engine'])
        if type(row['pending_review']) is not int or row['pending_review'] != 1:
            raise ValueError('Only a negative pending-review fact is portable')
    else:
        if not isinstance(row['requested_engines'], str):
            raise ValueError('Requested engine set must use canonical JSON')
        selected = strict_json(row['requested_engines'])
        if (not isinstance(selected, list) or not 1 <= len(selected) <= 32
                or any(not isinstance(s, str) for s in selected)):
            raise ValueError('Unknown/empty engine selection')
        for item in selected:
            engine(item)
        if selected != sorted(set(selected)) or row['requested_engines'] != json.dumps(selected, separators=(',', ':')):
            raise ValueError('Requested engine set is not compact canonical sorted unique JSON')


def import_policy(network_db, facts_path, expected_sha256):
    network_db, facts_path = Path(network_db), Path(facts_path)
    raw = facts_path.read_bytes()
    if (not network_db.is_file() or not re.fullmatch(r'[a-f0-9]{64}', expected_sha256)
            or hashlib.sha256(raw).hexdigest() != expected_sha256):
        raise ValueError('Require existing future DB and exact frozen negative-policy input hash')
    facts = strict_json(raw.decode('utf8'))
    if facts.get('schema_version') != 1 or facts.get('method') != 'GET':
        raise ValueError('Unsupported negative API-policy schema/method')
    backends, routes = facts.get('api_backend_restrictions'), facts.get('api_route_restrictions')
    if not isinstance(backends, list) or not isinstance(routes, list) or len(backends) + len(routes) > 10000:
        raise ValueError('Missing or unbounded negative-policy facts')
    for kind, rows in [('backend', backends), ('route', routes)]:
        for row in rows:
            check_row(row, kind)
        key = 'engine' if kind == 'backend' else 'requested_engines'
        if len({(r['endpoint'], r[key]) for r in rows}) != len(rows):
            raise ValueError('Duplicate negative-policy key')
    con = sqlite3.connect(network_db)
    try:
        con.execute('BEGIN IMMEDIATE')
        before = {name: con.execute('SELECT count(*) FROM ' + name).fetchone()[0] for name in EMPTY_TABLES}
        if any(before.values()):
            raise ValueError('Only a new cold cache with no observations/history may receive negative API policy')
        host_before = list(con.execute('SELECT * FROM hosts ORDER BY host'))
        old_backends = {(r[0], r[1]): r for r in con.execute('SELECT * FROM api_backend_restrictions')}
        old_routes = {(r[0], r[1]): r for r in con.execute('SELECT * FROM api_route_restrictions')}
        for row in backends:
            con.execute('''INSERT INTO api_backend_restrictions(endpoint,engine,reason,first_seen,last_seen,pending_review,evidence_sha256)
                VALUES(?,?,?,?,?,1,?) ON CONFLICT(endpoint,engine) DO UPDATE SET
                first_seen=MIN(api_backend_restrictions.first_seen,excluded.first_seen),
                last_seen=MAX(api_backend_restrictions.last_seen,excluded.last_seen),pending_review=1''',
                tuple(row[k] for k in ('endpoint', 'engine', 'reason', 'first_seen', 'last_seen', 'evidence_sha256')))
        for row in routes:
            con.execute('''INSERT INTO api_route_restrictions(endpoint,requested_engines,reason,first_seen,last_seen,evidence_sha256)
                VALUES(?,?,?,?,?,?) ON CONFLICT(endpoint,requested_engines) DO UPDATE SET
                first_seen=MIN(api_route_restrictions.first_seen,excluded.first_seen),
                last_seen=MAX(api_route_restrictions.last_seen,excluded.last_seen)''',
                tuple(row[k] for k in ('endpoint', 'requested_engines', 'reason', 'first_seen', 'last_seen', 'evidence_sha256')))
        after = {name: con.execute('SELECT count(*) FROM ' + name).fetchone()[0] for name in EMPTY_TABLES}
        new_backends = {(r[0], r[1]): r for r in con.execute('SELECT * FROM api_backend_restrictions')}
        new_routes = {(r[0], r[1]): r for r in con.execute('SELECT * FROM api_route_restrictions')}
        if before != after or host_before != list(con.execute('SELECT * FROM hosts ORDER BY host')):
            raise ValueError('Import changed content/history or host/robots/server/client policy')
        for old, new in [(old_backends, new_backends), (old_routes, new_routes)]:
            for key, row in old.items():
                current = new.get(key)
                if current is None or current[2] != row[2] or current[3] > row[3] or current[4] < row[4] or current[-1] != row[-1]:
                    raise ValueError('Import deleted or relaxed existing negative facts or their first evidence')
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()
    return {'schema_version': 1, 'imported_at': datetime.now(timezone.utc).isoformat(),
            'negative_policy_sha256': expected_sha256, 'input_backend_rows': len(backends), 'input_route_rows': len(routes),
            'backend_restrictions_after': len(new_backends), 'route_restrictions_after': len(new_routes),
            'content_counts_before': before, 'content_counts_after': after,
            'host_policy_unchanged': True, 'existing_negative_policy_monotonic': True,
            'no_observations_bodies_queries_source_permissions_imported': True, 'network_requests': 0,
            'pending_review_has_no_invented_operator_expiry': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--network-db', type=Path, required=True)
    parser.add_argument('--facts', type=Path, required=True)
    parser.add_argument('--facts-sha256', required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    args = parser.parse_args()
    if args.receipt.exists():
        raise ValueError('Never overwrite a prior import receipt')
    output = import_policy(args.network_db, args.facts, args.facts_sha256)
    args.receipt.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with args.receipt.open('x', encoding='utf8') as stream:
        json.dump(output, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
    args.receipt.chmod(0o600)
    print(json.dumps(output))


if __name__ == '__main__':
    main()
