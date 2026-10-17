"""Future runs only: import negative host policy into an otherwise empty cache.

No Engine, fetcher or network operations. Never copy HTTP/robots/search content,
request history, failure counters, aliases, URLs, credentials or gold labels.
This helper refuses the active V1 bank before opening it.
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

ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN_V1 = ROOT / "runtime/recovery-redesign-2026-10-16/bank-cold-v1/network.sqlite3"
EMPTY_TABLES = ("http_cache", "robots", "robots_refresh", "redirects", "request_history",
                "http_error_diagnostics", "api_backend_observations")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--network-db", type=Path, required=True)
    p.add_argument("--registry", type=Path, required=True)
    p.add_argument("--registry-sha256", required=True)
    p.add_argument("--receipt", type=Path, required=True)
    args = p.parse_args()
    if args.network_db.resolve() == FORBIDDEN_V1.resolve():
        raise ValueError("Active V1 is read-only for this task; never import into it")
    if not args.network_db.is_file() or args.receipt.exists():
        raise ValueError("Require an initialized future network database and a new receipt path")
    raw = args.registry.read_bytes()
    if (not re.fullmatch(r"[a-f0-9]{64}", args.registry_sha256)
            or hashlib.sha256(raw).hexdigest() != args.registry_sha256):
        raise ValueError("Registry differs from its frozen prelaunch hash")
    registry = json.loads(raw.decode('utf-8'))
    if registry.get("schema_version") != 1:
        raise ValueError("Unsupported host policy registry schema")
    now = datetime.now(timezone.utc)
    epoch = now.timestamp()
    active, seen = [], set()
    for row in registry["hosts"]:
        if set(row) != {"host", "blocked_until", "next_at", "reason"}:
            raise ValueError("Only four policy fields may cross into the cold run")
        host = row["host"]
        if (not isinstance(host, str) or not re.fullmatch(r"[a-z0-9.-]+", host)
                or host in seen or not isinstance(row["reason"], str) or len(row["reason"]) > 160
                or any(not isinstance(row[k], (int, float)) or isinstance(row[k], bool)
                       or not math.isfinite(row[k]) or row[k] < 0 for k in ("blocked_until", "next_at"))):
            raise ValueError("Malformed negative host policy")
        seen.add(host)
        if max(row["blocked_until"], row["next_at"]) > epoch:
            active.append(row)
    con = sqlite3.connect(args.network_db)
    try:
        con.execute("BEGIN IMMEDIATE")
        before = {name: con.execute("SELECT count(*) FROM " + name).fetchone()[0] for name in EMPTY_TABLES}
        if any(before.values()):
            raise ValueError("Only empty HTTP/robots/redirect/history state may receive a cold-run policy import")
        old = {r[0]: r[1:] for r in con.execute("SELECT host,blocked_until,next_at FROM hosts")}
        for row in active:
            con.execute("""INSERT INTO hosts(host,blocked_until,next_at,reason) VALUES(?,?,?,?)
                ON CONFLICT(host) DO UPDATE SET
                reason=CASE WHEN excluded.blocked_until>hosts.blocked_until OR excluded.next_at>hosts.next_at
                            THEN excluded.reason ELSE hosts.reason END,
                blocked_until=MAX(hosts.blocked_until,excluded.blocked_until),
                next_at=MAX(hosts.next_at,excluded.next_at)""",
                (row["host"], row["blocked_until"], row["next_at"], row["reason"]))
        after = {name: con.execute("SELECT count(*) FROM " + name).fetchone()[0] for name in EMPTY_TABLES}
        new = {r[0]: r[1:] for r in con.execute("SELECT host,blocked_until,next_at FROM hosts")}
        if before != after or any(new[h][0] < deadlines[0] or new[h][1] < deadlines[1] for h, deadlines in old.items()):
            raise ValueError("Import changed content state or relaxed an existing restriction")
        con.commit()
    finally:
        con.close()
    args.receipt.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    output = {"schema_version": 1, "at": now.isoformat(), "registry_sha256": args.registry_sha256,
              "imported_host_rows": len(active), "expired_registry_rows_not_needed": len(seen) - len(active),
              "content_table_counts_before": before, "content_table_counts_after": after,
              "content_counts_remain_zero": before == after and not any(after.values()),
              "existing_restrictions_only_strengthened": True, "network_requests": 0,
              "policy_only_fields": ["host", "blocked_until", "next_at", "reason"],
              "operator_or_conservative_mixed_floor_only": True,
              "client_only_timeout_policy_not_admitted_by_registry_builder": True,
              "meaning": "Negative permission state only. No evidence, query, candidate, source list, robots document, cache or request receipt was imported."}
    with args.receipt.open("x", encoding="utf-8") as stream:
        json.dump(output, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    args.receipt.chmod(0o600)
    print(json.dumps(output))


if __name__ == "__main__":
    main()
