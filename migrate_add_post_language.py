"""
migrate_add_post_language.py

One-time migration: adds the `language` column to the existing `posts`
table, so the language decision Gemini makes per-story is tracked as
data, not just embedded in the text itself.

Safe to re-run: checks whether the column already exists first.

USAGE
-----
    python migrate_add_post_language.py
"""

from dotenv import load_dotenv
load_dotenv()

from sqlalchemy import text

from models import engine


def main():
    with engine.connect() as conn:
        result = conn.execute(text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'posts' AND column_name = 'language'
        """))
        if result.first() is not None:
            print("posts.language already exists -- nothing to do.")
            return

        conn.execute(text("ALTER TABLE posts ADD COLUMN language VARCHAR"))
        conn.commit()
        print("Added posts.language column.")


if __name__ == "__main__":
    main()
