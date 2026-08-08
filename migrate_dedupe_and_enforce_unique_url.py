"""
migrate_dedupe_and_enforce_unique_url.py

Fixes two things:
1. Reports (and can remove) duplicate rows in `articles` that share the
   same `url` -- keeps the earliest (lowest id) copy of each, removes the
   rest.
2. Checks whether a real UNIQUE constraint exists on articles.url at the
   DATABASE level (not just in the Python model) and adds one if it
   doesn't. The model has always declared unique=True, but that only gets
   enforced when SQLAlchemy creates a table from scratch -- since this
   table already existed before that declaration mattered, the constraint
   was likely never actually applied, which is why duplicates could slip
   in without any error.

USAGE
-----
    python migrate_dedupe_and_enforce_unique_url.py            # report only
    python migrate_dedupe_and_enforce_unique_url.py --apply    # actually fix it
"""

import argparse

from dotenv import load_dotenv
load_dotenv()

from sqlalchemy import text

from models import engine


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Actually delete duplicates and add the constraint.")
    args = parser.parse_args()

    with engine.connect() as conn:
        # --- Step 1: report duplicates -----------------------------------
        dupes = conn.execute(text("""
            SELECT url, COUNT(*) AS n
            FROM articles
            GROUP BY url
            HAVING COUNT(*) > 1
            ORDER BY n DESC
        """)).fetchall()

        total_rows = conn.execute(text("SELECT COUNT(*) FROM articles")).scalar()
        print(f"Total rows in articles: {total_rows}")
        print(f"Distinct URLs with duplicates: {len(dupes)}")
        if dupes:
            extra_rows = sum(row.n - 1 for row in dupes)
            print(f"Extra (removable) duplicate rows: {extra_rows}")
            print("\nWorst offenders:")
            for row in dupes[:10]:
                print(f"  {row.n}x  {row.url}")

        # --- Step 2: check if a real UNIQUE constraint already exists ----
        constraint_check = conn.execute(text("""
            SELECT con.conname
            FROM pg_constraint con
            JOIN pg_class rel ON rel.oid = con.conrelid
            JOIN pg_attribute att ON att.attrelid = rel.oid AND att.attnum = ANY(con.conkey)
            WHERE rel.relname = 'articles'
              AND att.attname = 'url'
              AND con.contype = 'u'
        """)).fetchall()

        constraint_exists = len(constraint_check) > 0
        print(f"\nReal UNIQUE constraint on articles.url currently exists: {constraint_exists}")

        if not args.apply:
            print("\nReport-only run. Re-run with --apply to remove duplicates and add the constraint.")
            return

        # --- Apply: remove duplicates (keep earliest id per url) ---------
        if dupes:
            result = conn.execute(text("""
                DELETE FROM articles a
                USING articles b
                WHERE a.url = b.url
                  AND a.id > b.id
            """))
            conn.commit()
            print(f"\nRemoved {result.rowcount} duplicate row(s), kept the earliest copy of each URL.")
        else:
            print("\nNo duplicates to remove.")

        # --- Apply: add the real constraint, if missing -------------------
        if not constraint_exists:
            conn.execute(text("ALTER TABLE articles ADD CONSTRAINT articles_url_unique UNIQUE (url)"))
            conn.commit()
            print("Added a real UNIQUE constraint on articles.url. Duplicate inserts will now fail loudly instead of silently succeeding.")
        else:
            print("UNIQUE constraint already exists -- nothing to add.")


if __name__ == "__main__":
    main()
