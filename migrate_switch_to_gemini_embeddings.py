"""
migrate_switch_to_gemini_embeddings.py

One-time migration for switching from local sentence-transformers
embeddings (384 dimensions) to Gemini API embeddings (768 dimensions).

Why a full re-embed is necessary, not just a dimension resize: embeddings
from two different models aren't just a different SIZE, they're a
different coordinate system entirely. A 384-dim local-model vector and a
768-dim Gemini vector for the exact same text are not comparable, so
there's no way to "convert" old embeddings -- every existing row needs a
brand new embedding computed from scratch.

This drops the old embedding column, adds the new 768-dim one, then loops
over every existing article and re-embeds it via the Gemini API. At
typical project scale (low hundreds of articles) this costs a few cents
and takes well under a minute.

USAGE
-----
    python migrate_switch_to_gemini_embeddings.py
"""

from dotenv import load_dotenv
load_dotenv()

from sqlalchemy import text

from models import Article, SessionLocal, engine
from embeddings import get_embedding, EMBEDDING_DIMENSIONS


def main():
    with engine.connect() as conn:
        result = conn.execute(text("""
            SELECT character_maximum_length FROM information_schema.columns
            WHERE table_name = 'articles' AND column_name = 'embedding'
        """))
        # We don't actually use the result value (pgvector dimension isn't
        # exposed cleanly through information_schema) -- this query is just
        # confirming the column exists before we touch it.
        exists = result.first() is not None

        print("Dropping old embedding column (384-dim, local model)..." if exists
              else "No existing embedding column found -- creating fresh.")

        conn.execute(text("ALTER TABLE articles DROP COLUMN IF EXISTS embedding"))
        conn.execute(text(f"ALTER TABLE articles ADD COLUMN embedding vector({EMBEDDING_DIMENSIONS})"))
        conn.commit()
        print(f"Added embedding column at {EMBEDDING_DIMENSIONS} dimensions.")

    db = SessionLocal()
    try:
        articles = db.query(Article).all()
        print(f"\nRe-embedding {len(articles)} existing article(s)...")

        for i, article in enumerate(articles, start=1):
            new_embedding = get_embedding(
                f"{article.title}. {article.content}", task_type="RETRIEVAL_DOCUMENT"
            )
            article.embedding = new_embedding
            if i % 10 == 0 or i == len(articles):
                print(f"  {i}/{len(articles)} re-embedded")

        db.commit()
        print("\nDone. All articles now have Gemini (768-dim) embeddings.")

    finally:
        db.close()


if __name__ == "__main__":
    main()
