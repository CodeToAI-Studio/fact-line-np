"""
migrate_post_image_data.py

One-time migration: adds the `image_data` column to the existing `posts`
table, so the pipeline can store the normalized rehosted image bytes (JPEG)
in the DB and serve them publicly from the /post_image/{id}.jpg route. This
is the permanent home for post images -- survives Railway restarts/redeploys,
and gives Instagram/Facebook a stable public image_url.

Safe to re-run: checks whether the column already exists first.

USAGE
-----
    python migrate_post_image_data.py
"""

from dotenv import load_dotenv
load_dotenv()

from sqlalchemy import text

from models import engine


def main():
    with engine.connect() as conn:
        result = conn.execute(text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'posts' AND column_name = 'image_data'
        """))
        if result.first() is not None:
            print("posts.image_data already exists -- nothing to do.")
            return

        conn.execute(text("ALTER TABLE posts ADD COLUMN image_data BYTEA"))
        conn.commit()
        print("Added posts.image_data column (bytea).")


if __name__ == "__main__":
    main()
