"""
migrate_view_count.py

One-time migration: adds the `view_count` column to the existing `posts`
table, so the public article page can track per-post page views that drive
the /web/popular ranking and the "Most Read" sidebars.

Safe to re-run: checks whether the column already exists first.

USAGE
-----
    python migrate_view_count.py
"""

from dotenv import load_dotenv
load_dotenv()

from sqlalchemy import text

from models import engine


def main():
    with engine.connect() as conn:
        result = conn.execute(text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'posts' AND column_name = 'view_count'
        """))
        if result.first() is not None:
            print("posts.view_count already exists -- nothing to do.")
            return

        conn.execute(text(
            "ALTER TABLE posts ADD COLUMN view_count INTEGER NOT NULL DEFAULT 0"
        ))
        conn.commit()
        print("Added posts.view_count column.")


if __name__ == "__main__":
    main()