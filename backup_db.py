"""
backup_db.py — routine backup of the Railway Postgres (the single source of
truth) to local JSON snapshot files.

The live data lives on Railway Postgres. If Railway lost the volume there is
no other copy (the git repo holds only code). This script connects via
psycopg2 (alreadinstalled, works at any server version) and writes a complete
JSON snapshot of every table under backups/, pruning old ones.

NOTE: a `pg_dump`-based approach was abandoned because Railway's server is
PostgreSQL 18 while the local pg_dump is 16 (version mismatch). The JSON
snapshot is portable and can be restored/re-imported with a small script.

USAGE
-----
    python backup_db.py                 # take one snapshot, keep last N
    python backup_db.py --dry-run       # print what it would do, write nothing

Safe to re-run anytime.
"""

import argparse
import json
import os
import sys
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

sys.stdout.reconfigure(encoding="utf-8")

from urllib.parse import urlparse, unquote

import psycopg2

# How many snapshots to keep (oldest pruned beyond this).
KEEP_SNAPSHOTS = 7

BACKUP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backups")

# All user tables to snapshot, in dependency-safe order (children first is fine
# for a flat JSON restore; order only affects readability).
TABLES = [
    "articles",
    "posts",
    "platform_posts",
    "site_settings",
    "admin_users",
    "admin_sessions",
    "audit_logs",
]


def _connect():
    url = os.getenv("DATABASE_URL", "")
    p = urlparse(url)
    return psycopg2.connect(
        host=p.hostname,
        port=p.port,
        user=unquote(p.username or ""),
        password=unquote(p.password or ""),
        dbname=p.path[1:],
        connect_timeout=20,
    )


def run_backup(dry_run: bool = False) -> str | None:
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(BACKUP_DIR, f"news_db_{stamp}.json")

    if dry_run:
        print(f"[DRY RUN] would snapshot tables to {dest}")
        print(f"          tables: {', '.join(TABLES)}")
        print(f"          keep last {KEEP_SNAPSHOTS} snapshots, prune older")
        return None

    conn = _connect()
    snapshot = {"saved_at": stamp, "tables": {}}
    try:
        with conn.cursor() as cur:
            for table in TABLES:
                try:
                    cur.execute(f'SELECT * FROM {table}')
                    cols = [d[0] for d in cur.description]
                    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
                    snapshot["tables"][table] = rows
                    print(f"  {table}: {len(rows)} rows")
                except Exception as e:
                    print(f"  {table}: SKIPPED ({type(e).__name__}: {e})")
                    snapshot["tables"][table] = []
    finally:
        conn.close()

    with open(dest, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, default=str)
    size = os.path.getsize(dest)
    print(f"✅ Backup saved: {dest} ({size / 1024 / 1024:.1f} MB)")

    # Prune old snapshots, keep newest KEEP_SNAPSHOTS.
    snapshots = sorted(
        f for f in os.listdir(BACKUP_DIR)
        if f.startswith("news_db_") and f.endswith(".json")
    )
    to_prune = snapshots[:-KEEP_SNAPSHOTS] if len(snapshots) > KEEP_SNAPSHOTS else []
    for name in to_prune:
        os.remove(os.path.join(BACKUP_DIR, name))
        print(f"  pruned old snapshot: {name}")

    return dest


def main():
    parser = argparse.ArgumentParser(description="Snapshot the Railway Postgres to a local JSON file.")
    parser.add_argument("--dry-run", action="store_true", help="Print what would happen, write nothing.")
    args = parser.parse_args()
    run_backup(dry_run=args.dry_run)


if __name__ == "__main__":
    main()