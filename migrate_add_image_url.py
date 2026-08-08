"""
migrate_add_image_url.py

One-time migration: adds the `image_url` column to the existing `articles`
table. ingest_rss.py already extracts each item's image from the RSS feed
(extract_image_url()) but had nowhere to store it -- this fixes that gap.

Safe to re-run: checks whether the column already exists first.

USAGE
-----
    python migrate_add_image_url.py
"""

from dotenv import load_dotenv
load_dotenv()

from sqlalchemy import text

from models import engine


def main():
    with engine.connect() as conn:
        result = conn.execute(text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'articles' AND column_name = 'image_url'
        """))
        already_exists = result.first() is not None

        if already_exists:
            print("articles.image_url already exists -- nothing to do.")
            return

        conn.execute(text("ALTER TABLE articles ADD COLUMN image_url VARCHAR"))
        conn.commit()
        print("Added articles.image_url column.")


if __name__ == "__main__":
    main()
