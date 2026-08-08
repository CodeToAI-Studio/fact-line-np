"""
migrate_add_posts_schema.py

One-time migration for the Post / PlatformPost schema addition.

Why this is needed at all: SQLAlchemy's Base.metadata.create_all() (called
on every app startup in main.py) creates tables that don't exist yet, but
it does NOT alter existing tables. `posts` and `platform_posts` are brand
new, so create_all() handles those fine on its own next time you start the
app. But `articles.post_id` is a new COLUMN on a table that already
exists — that needs an explicit ALTER TABLE, which is what this script does.

Safe to re-run: checks whether the column already exists before adding it,
so running this twice does nothing the second time rather than erroring.

USAGE
-----
    python migrate_add_posts_schema.py
"""

from dotenv import load_dotenv
load_dotenv()

from sqlalchemy import text

from models import Base, engine


def main():
    # Create the two brand-new tables (posts, platform_posts). Harmless if
    # they already exist -- create_all() skips tables that are already there.
    Base.metadata.create_all(bind=engine)
    print("Ensured posts / platform_posts tables exist.")

    with engine.connect() as conn:
        result = conn.execute(text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'articles' AND column_name = 'post_id'
        """))
        already_exists = result.first() is not None

        if already_exists:
            print("articles.post_id already exists -- nothing to do.")
            return

        conn.execute(text("""
            ALTER TABLE articles
            ADD COLUMN post_id INTEGER REFERENCES posts(id)
        """))
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_articles_post_id ON articles (post_id)
        """))
        conn.commit()
        print("Added articles.post_id column and index.")


if __name__ == "__main__":
    main()
