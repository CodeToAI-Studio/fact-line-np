"""
llm_models.py

Single source of truth for which Gemini *generation* models this project
uses, plus the runtime discovery fallback.

Why this file exists: the model list used to be copy-pasted into every entry
point. When the 2.0 Flash retirement landed, main.py / generate_posts.py /
backfill_category.py were updated and rag_chat.py was missed -- and because
its error handler skipped 404s silently, the breakage surfaced as a bogus
"rate limit" message instead. One list, one place.

Embedding model config deliberately stays in embeddings.py: swapping the
embedding model requires re-embedding the entire corpus (the stored vectors
and the column dimension have to match), whereas these can be changed freely
between runs.
"""

from typing import List

# Google retires Gemini model IDs on a rolling basis -- 2.0 Flash and
# 2.0 Flash-Lite were shut down 2026-06-01, and 2.5 Flash is scheduled to
# follow on 2026-10-16 (already failing early for some callers, so don't
# adopt it). Keep this list to currently-GA models and expect to revisit it;
# list_available_models() below is the safety net for when it goes stale
# anyway, which it will.
PRIMARY_MODELS = ["gemini-3.6-flash", "gemini-3.5-flash-lite"]

# For bulk single-token work (classification, tagging) where synthesis
# quality is irrelevant and per-call cost dominates.
CHEAP_MODEL = "gemini-3.5-flash-lite"

# Google-maintained alias tracking whatever the current flash model is.
# Fine as a last-resort fallback; deliberately not a primary, since it can
# shift under you between runs and makes behaviour hard to reproduce.
LATEST_FLASH_ALIAS = "gemini-flash-latest"


def list_available_models(client) -> List[str]:
    """Ask Google what's actually live right now. Filters to models that
    support generateContent, since list() also returns embedding and image
    models. Returns [] on any failure -- callers should treat an empty list
    as "discovery unavailable" and fall through to their own error path
    rather than assuming there are no models."""
    try:
        models = client.models.list()
        names = []
        for m in models:
            supported = getattr(m, "supported_actions", None) or getattr(
                m, "supported_generation_methods", None
            )
            if not supported or "generateContent" in supported:
                # model.name is usually "models/gemini-x" -- strip the prefix
                name = m.name.split("/")[-1] if "/" in m.name else m.name
                names.append(name)
        return names
    except Exception:
        return []
