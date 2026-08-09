"""
embeddings.py

Shared embedding helper, used everywhere an embedding needs to be
computed: main.py and search.py (search queries), rag_chat.py (CLI RAG),
ingest.py (new articles), and migrate_switch_to_gemini_embeddings.py
(re-embedding existing rows).

Why this exists as its own module: embeddings now come from the Gemini
API instead of a local sentence-transformers model, specifically to
avoid needing `torch` at all -- torch's native DLLs were getting blocked
by Windows Smart App Control on a local dev machine with no way around
it short of disabling a real security feature. Routing every embedding
call through one function means the whole project can't accidentally
drift into mixing two incompatible embedding spaces again.

EMBEDDING_DIMENSIONS = 768 -- if you ever change this, every existing
embedding becomes incompatible with new ones and needs re-generating
(see the migration script).
"""

import os
import time

from google import genai
from google.genai import types

import gemini_keys

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
EMBEDDING_MODEL = "gemini-embedding-001"
EMBEDDING_DIMENSIONS = 768

MAX_RETRIES = 5
INITIAL_BACKOFF_SECONDS = 20

_client = None


def get_client():
    global _client
    if _client is None:
        _client = gemini_keys.get_client()
    return _client


def get_embedding(text: str, task_type: str = "RETRIEVAL_DOCUMENT") -> list:
    """Returns a 768-dimension embedding for the given text.

    task_type matters for quality: use "RETRIEVAL_DOCUMENT" when embedding
    something that will be *searched for* (articles, posts), and
    "RETRIEVAL_QUERY" when embedding a user's search query itself --
    Gemini optimizes the vector differently depending on which side of a
    search you're on.

    Retries with backoff on rate-limit (429) errors -- the free tier caps
    at 100 embed requests/minute, which a tight loop over many articles
    (ingestion, migrations) can hit easily. Other errors are raised
    immediately, not retried.
    """
    client = get_client()
    backoff = INITIAL_BACKOFF_SECONDS
    last_error = None

    for attempt in range(MAX_RETRIES):
        try:
            gemini_keys.pace()
            response = client.models.embed_content(
                model=EMBEDDING_MODEL,
                contents=text,
                config=types.EmbedContentConfig(
                    output_dimensionality=EMBEDDING_DIMENSIONS,
                    task_type=task_type,
                ),
            )
            return response.embeddings[0].values
        except Exception as e:
            last_error = e
            is_rate_limit = "RESOURCE_EXHAUSTED" in str(e) or "429" in str(e)
            if not is_rate_limit or attempt == MAX_RETRIES - 1:
                raise
            # A 429 means the current project's quota is gone — honor the retry
            # hint, rotate to the next key (if any) so a different account's
            # quota is tried before backing off.
            gemini_keys.sleep_on_429(e)
            gemini_keys.rotate()
            client = get_client()
            print(f"    Rate limited on {gemini_keys.current_key()[-6:]}..., rotating + waiting {backoff}s (attempt {attempt + 1}/{MAX_RETRIES})...")
            time.sleep(backoff)
            backoff *= 2  # back off further each time, in case the limit is tighter than expected

    raise last_error
