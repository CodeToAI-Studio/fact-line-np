"""
migrate_add_whatsapp_message_id.py

One-time migration: adds whatsapp_message_id to the existing posts table,
alongside telegram_message_id. Safe to re-run.

USAGE
-----
    python migrate_add_whatsapp_message_id.py
"""

from dotenv import load_dotenv
load_dotenv()

from sqlalchemy import text

from models import engine


def main():
    with engine.connect() as conn:
        result = conn.execute(text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'posts' AND column_name = 'whatsapp_message_id'
        """))
        if result.first() is not None:
            print("posts.whatsapp_message_id already exists -- nothing to do.")
            return

        conn.execute(text("ALTER TABLE posts ADD COLUMN whatsapp_message_id VARCHAR"))
        conn.commit()
        print("Added posts.whatsapp_message_id column.")


if __name__ == "__main__":
    main()
