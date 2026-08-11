"""
migrate_platform_posts_cascade.py

Idempotent: adds ON DELETE CASCADE to the platform_posts.post_id FK.

Why: retention.py deletes Post rows (stale pending / rejected) with a core
bulk delete(), which bypasses SQLAlchemy ORM relationship cascades
(cascade="all, delete-orphan" only fires for ORM-level object deletion). The
database-level FK had no ON DELETE CASCADE, so deleting a post that still had
platform_posts rows raised an IntegrityError. This migration rebuilds the FK
with ON DELETE CASCADE so platform rows are removed by the database itself.

Match the repo's established migrate_*.py pattern: probe pg_constraint, then
apply. Re-running is a no-op.

Usage: venv/Scripts/python.exe migrate_platform_posts_cascade.py
"""
import sys
from dotenv import load_dotenv
load_dotenv()

sys.stdout.reconfigure(encoding="utf-8")

from sqlalchemy import text
from models import SessionLocal

FK_NAME = "platform_posts_post_id_fkey"


def run():
    db = SessionLocal()
    try:
        # Does the constraint already have ON DELETE CASCADE?
        rows = db.execute(text("""
            SELECT pg_get_constraintdef(oid) AS cdef
            FROM pg_constraint
            WHERE conname = :name AND conrelid = 'platform_posts'::regclass
        """), {"name": FK_NAME}).fetchall()
        if rows:
            cdef = rows[0].cdef
            if "ON DELETE CASCADE" in cdef:
                print(f"[migrate] {FK_NAME} already has ON DELETE CASCADE — nothing to do.")
                return
            print(f"[migrate] Rebuilding {FK_NAME}: {cdef}")
            db.execute(text(f"ALTER TABLE platform_posts DROP CONSTRAINT {FK_NAME}"))
        else:
            print(f"[migrate] {FK_NAME} not found — adding it.")

        db.execute(text(f"""
            ALTER TABLE platform_posts
            ADD CONSTRAINT {FK_NAME}
            FOREIGN KEY (post_id) REFERENCES posts(id) ON DELETE CASCADE
        """))
        db.commit()

        # Verify
        check = db.execute(text("""
            SELECT pg_get_constraintdef(oid) AS cdef
            FROM pg_constraint
            WHERE conname = :name AND conrelid = 'platform_posts'::regclass
        """), {"name": FK_NAME}).fetchall()
        print(f"[migrate] Verified: {check[0].cdef if check else 'MISSING'}")
        print("[migrate] Done.")
    finally:
        db.close()


if __name__ == "__main__":
    run()
