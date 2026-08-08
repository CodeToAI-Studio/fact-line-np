import sys

from dotenv import load_dotenv
load_dotenv()  # must run before importing embeddings, which reads GEMINI_API_KEY

from sqlalchemy import select
from models import Article, SessionLocal
from embeddings import get_embedding

# Was the local 384-dim sentence-transformers model; the corpus is now
# Gemini 768-dim (see migrate_switch_to_gemini_embeddings.py), so those
# query vectors could no longer be compared against the column at all.


def search_articles(query_text: str, top_k: int = 3):
    db = SessionLocal()
    try:
        # RETRIEVAL_QUERY, not RETRIEVAL_DOCUMENT -- Gemini embeds the two
        # sides of a search differently, and mixing them degrades ranking.
        query_vector = get_embedding(query_text, task_type="RETRIEVAL_QUERY")

        # Execute cosine similarity search against pgvector HNSW index
        stmt = (
            select(
                Article,
                Article.embedding.cosine_distance(query_vector).label(
                    "distance"
                ),
            )
            .order_by("distance")
            .limit(top_k)
        )

        results = db.execute(stmt).all()

        print(f"\n--- Top Results for Query: '{query_text}' ---")
        for rank, (article, distance) in enumerate(results, start=1):
            similarity_score = 1 - distance
            print(f"\n[{rank}] {article.title}")
            print(f"    Source: {article.source}")
            print(f"    URL: {article.url}")
            print(f"    Similarity Score: {similarity_score:.4f}")
            print(
                f"    Snippet: {article.content[:150]}..."
                if article.content
                else "    No snippet."
            )

    finally:
        db.close()


if __name__ == "__main__":
    query = sys.argv[1] if len(sys.argv) > 1 else "technology and AI"
    search_articles(query)