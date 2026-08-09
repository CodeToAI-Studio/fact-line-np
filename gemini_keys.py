"""
gemini_keys.py — shared Gemini API-key rotation.

Google's Gemini quota is per **project**, not per key. A single free-tier
project runs out (~500 requests/day) and every key in it dies together —
which previously stalled the whole pipeline at midday. The zero-cost way to
get more daily throughput is to spread requests across multiple Google
accounts (each account = its own project = its own quota).

This module is the single owner of the key list + rotation, mirroring how
llm_models.py owns the model list. Every client-creation site in the project
delegates here, so rotation behaviour can't drift between files.

Usage:
    GEMINI_API_KEYS="key_a,key_b,key_c"   # comma-separated, first is default
    # fallback for back-compat: GEMINI_API_KEY="key_a"

    client = gemini_keys.get_client()     # cached client for the current key
    ... on 429 RESOURCE_EXHAUSTED ...
    gemini_keys.rotate()                  # move to the next key, return fresh client

rotate() wraps around and stays on the last key once exhausted (idempotent,
never raises) — callers just log that they switched.
"""

import os

from google import genai

# Back-compat: if the old single-key var is set but the new list isn't, use it.
_raw = os.getenv("GEMINI_API_KEYS") or os.getenv("GEMINI_API_KEY") or ""
KEYS = [k.strip() for k in _raw.split(",") if k.strip()]

_index = 0
_clients = {}  # key -> genai.Client


def key_count() -> int:
    """How many API keys are configured (0 if none)."""
    return len(KEYS)


def current_key() -> str | None:
    """The API key in active use (None if no keys configured)."""
    if not KEYS:
        return None
    return KEYS[_index % len(KEYS)]


def get_client():
    """Return a cached genai.Client for the current key."""
    if not KEYS:
        raise RuntimeError(
            "No Gemini API key configured: set GEMINI_API_KEYS (comma-separated) "
            "or GEMINI_API_KEY in .env"
        )
    key = current_key()
    if key not in _clients:
        _clients[key] = genai.Client(api_key=key)
    return _clients[key]


def rotate() -> None:
    """Advance to the next configured key. Wraps around; if all keys are
    exhausted it stays on the last one (retrying a dead key is harmless — it
    just 429s, and callers fail soft)."""
    global _index
    if not KEYS:
        return
    if len(KEYS) == 1:
        return
    _index = (_index + 1) % len(KEYS)


def is_rate_limit(exc: Exception) -> bool:
    """True if the exception is a Gemini 429 / RESOURCE_EXHAUSTED quota error
    (the only case worth rotating on)."""
    s = str(exc)
    return "RESOURCE_EXHAUSTED" in s or "429" in s
