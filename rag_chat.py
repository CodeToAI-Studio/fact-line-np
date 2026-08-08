import os
import sys
import time

from dotenv import load_dotenv

# Load environment variables from .env before importing modules that read them
load_dotenv()

from google import genai
from sqlalchemy import select

from models import Article, SessionLocal
from embeddings import get_embedding
from llm_models import PRIMARY_MODELS, LATEST_FLASH_ALIAS

# This used to embed queries locally with SentenceTransformer
# ("all-MiniLM-L6-v2", 384-dim). The corpus has since been migrated to
# gemini-embedding-001 at 768-dim (see migrate_switch_to_gemini_embeddings.py),
# so those query vectors no longer matched the column and every search here
# failed on a dimension mismatch. Now uses the same embedding path as the
# API, which is the only way the two stay comparable.
ai_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))


def retrieve_context(query_text: str, top_k: int = 3) -> str:
    db = SessionLocal()
    try:
        # Generate vector embedding for user query. RETRIEVAL_QUERY (not
        # RETRIEVAL_DOCUMENT) -- Gemini embeds queries and documents into
        # different spaces, and mismatching them quietly degrades ranking.
        query_vector = get_embedding(query_text, task_type="RETRIEVAL_QUERY")

        # Perform cosine distance vector search against PostgreSQL pgvector
        stmt = (
            select(Article, Article.embedding.cosine_distance(query_vector).label("distance"))
            .order_by("distance")
            .limit(top_k)
        )
        results = db.execute(stmt).all()

        context_blocks = []
        for rank, (article, distance) in enumerate(results, start=1):
            context_blocks.append(
                f"--- Article {rank} ---\n"
                f"Title: {article.title}\n"
                f"Source: {article.source}\n"
                f"URL: {article.url}\n"
                f"Content: {article.content}\n"
            )

        return "\n".join(context_blocks)
    finally:
        db.close()


def generate_rag_response(user_query: str):
    print(f"\n🔍 Searching database for: '{user_query}'...")
    context = retrieve_context(user_query, top_k=3)

    prompt = f"""You are a professional AI news assistant. Answer the user's question using ONLY the provided news context. 
If the context doesn't contain enough information to fully answer, state what is known from the context and mention what is missing.
Always cite your source and URL at the end of key statements.

[RETRIEVED NEWS CONTEXT]:
{context}

[USER QUESTION]:
{user_query}
"""

    print("🤖 Synthesizing answer with Gemini...\n")

    # Shared list, plus the alias as a last resort. This file previously
    # carried its own copy and drifted onto the retired 2.0 models -- the
    # "404 -> continue" branch below then swallowed the deprecation and
    # reported it as a rate limit.
    candidate_models = [*PRIMARY_MODELS, LATEST_FLASH_ALIAS]

    for model_name in candidate_models:
        try:
            response = ai_client.models.generate_content(
                model=model_name,
                contents=prompt,
            )
            print(f"=== RAG Answer (Using: {model_name}) ===")
            print(response.text)
            return
        except Exception as e:
            error_str = str(e)
            if "429" in error_str:
                print(f"⚠️ Quota hit on '{model_name}'. Trying next available model...")
                time.sleep(1)
            elif "404" in error_str:
                # Silently skip endpoint if unavailable
                continue
            else:
                print(f"⚠️ Model '{model_name}' failed: {e}")

    print("❌ All candidate models exceeded rate limits. Please wait 60 seconds and try again.")


if __name__ == "__main__":
    user_query = sys.argv[1] if len(sys.argv) > 1 else "What is going on with OpenAI and AI agents?"
    generate_rag_response(user_query)