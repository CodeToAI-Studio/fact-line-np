"""
backfill_category.py

One-off maintenance script: infers `category` for existing Article rows,
same dry-run-by-default pattern as backfill_region.py.

Unlike region, category can't be inferred from `source` alone (one outlet
covers many topics), so this uses two signals in priority order:

  1. URL PATH SECTION — many news sites encode their own section in the
     URL (e.g. kathmandupost.com/opinion/..., kathmandupost.com/columns/...,
     kathmandupost.com/art-culture/...). This is the *site's own*
     categorization, so it's more trustworthy than guessing from keywords.
  2. KEYWORD MATCHING — for sources/URLs that don't expose a clear section
     (e.g. TechCrunch, The Verge), scans title + content for topic
     keywords and picks whichever category has the most hits.

Articles that match neither signal are left untouched and reported under
UNMAPPED, same as backfill_region.py — nothing is guessed blindly.

USAGE
-----
    python backfill_category.py            # dry run — report only
    python backfill_category.py --apply    # commit the changes
"""

import argparse
import os
import re
import threading
import time
from collections import Counter
from urllib.parse import urlparse

from dotenv import load_dotenv
load_dotenv()  # must run before importing models, so DATABASE_URL is set

from google import genai

from models import Article, SessionLocal
from llm_models import CHEAP_MODEL
import gemini_keys

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = CHEAP_MODEL  # single-word classification, not synthesis -- cost dominates
_client = None


def get_client():
    global _client
    if _client is None:
        _client = gemini_keys.get_client()
    return _client


# --- Signal 1: URL path section --------------------------------------------
# Maps the first path segment of a URL to a category. Edit/extend this for
# whatever sections your actual sources use — check the UNMAPPED output
# after a dry run to see what's not being caught.
URL_SECTION_MAP = {
    "opinion": "opinion",
    "columns": "opinion",
    "editorial": "opinion",
    "politics": "politics",
    "national": "politics",
    "world": "world",
    "business": "business",
    "money": "business",
    "economy": "business",
    "sports": "sports",
    "art-culture": "culture",
    "culture": "culture",
    "entertainment": "culture",
    "science-technology": "technology",
    "technology": "technology",
    "tech": "technology",
    "health": "health",
    "science": "science",
    "environment": "environment",
}

# --- Signal 2 (weak prior): single-topic outlets ----------------------------
# For sources that are essentially always one category regardless of URL
# structure. Only add outlets here if they're genuinely single-topic —
# general-purpose papers (Kathmandu Post, Reuters, etc.) should NOT be here,
# since they cover everything and would get miscategorized.
SOURCE_CATEGORY_HINTS = {
    "techcrunch": "technology",
    "the verge": "technology",
}

# --- Signal 3: keyword matching on title + content ---------------------------
# Substring match, case-insensitive. Category with the most keyword hits
# wins; ties or zero hits fall through to UNMAPPED.
CATEGORY_KEYWORDS = {
    "politics": [
        "parliament", "minister", "prime minister", "election", "government",
        "cabinet", "political party", "president", "lawmaker", "mp ",
        "speaker of", "vote", "policy",
    ],
    "business": [
        "market", "economy", "stock", "trade", "investment", "inflation",
        "gdp", "company", "ceo", "revenue", "bank", "startup funding",
    ],
    "technology": [
        "artificial intelligence", " ai ", "startup", "software", " app ",
        "smartphone", "silicon valley", "chip", "robot", "gadget",
    ],
    "sports": [
        "match", "tournament", "cricket", "football", "olympic",
        "championship", "player", "coach", "score", "league",
    ],
    "culture": [
        "poet", "literature", "film", "music", "festival", "novel",
        "author", "cultural", "heritage", "tradition",
    ],
    "society": [
        "communal", "violence", "protest", "riot", "misinformation",
        "policing", "community", "caste", "ethnic",
    ],
    "health": [
        "hospital", "disease", "vaccine", "medical", "doctor", "pandemic", "virus",
    ],
    "science": [
        "research", "study finds", "scientist", "discovery", "space", "nasa",
    ],
    "environment": [
        "climate", "pollution", "wildlife", "deforestation", "emissions",
    ],
    "world": [
        "united nations", "foreign minister", "diplomat", "treaty", "summit",
    ],
}


def classify_from_url(url: str) -> str | None:
    try:
        path_parts = [p for p in urlparse(url).path.split("/") if p]
    except Exception:
        return None
    if not path_parts:
        return None
    first_segment = path_parts[0].lower()
    return URL_SECTION_MAP.get(first_segment)


def classify_from_source_hint(source: str) -> str | None:
    source_lc = source.lower()
    for key, category in SOURCE_CATEGORY_HINTS.items():
        if key in source_lc:
            return category
    return None


def classify_from_keywords(title: str, content: str) -> str | None:
    text = f"{title} {content}".lower()
    scores = Counter()
    for category, keywords in CATEGORY_KEYWORDS.items():
        for kw in keywords:
            if kw in text:
                scores[category] += 1
    if not scores:
        return None
    top = scores.most_common()
    # Require a clear winner — a tie at the top means "not confident enough."
    if len(top) > 1 and top[0][1] == top[1][1]:
        return None
    return top[0][0]


# --- Signal 4: Gemini fallback -----------------------------------------------
# URL sections and keyword matching are both effectively English/
# Kathmandu-Post-URL-structure specific. Nepali-language sources (Ratopati,
# Nagarik Dainik, etc.) fall through both almost entirely. Gemini handles
# any language natively, so it's the fallback of last resort rather than
# the first choice -- it costs an API call, the free signals above don't.
GEMINI_CATEGORY_OPTIONS = list(CATEGORY_KEYWORDS.keys())
MAX_RETRIES = 3
INITIAL_BACKOFF_SECONDS = 15

# google-genai exposes no request-level timeout, and a hung network call can
# block classify() — and therefore run_ingestion() — forever (this actually
# wedged the pipeline during the Railway migration). We run the Gemini call in
# a daemon thread and bound it to this many seconds instead.
GEMINI_CALL_TIMEOUT_SECONDS = 12


def _classify_via_gemini_once(title: str, content: str) -> str | None:
    """Run the Gemini classification call (must be called in a worker thread)."""
    prompt = f"""Classify this news article into exactly ONE of these categories:
{", ".join(GEMINI_CATEGORY_OPTIONS)}

Respond with ONLY the single category word from that list, lowercase, nothing else -- no punctuation, no explanation.

Title: {title}
Content: {content[:600]}
"""
    client = get_client()
    response = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
    answer = (response.text or "").strip().lower()
    if answer in GEMINI_CATEGORY_OPTIONS:
        return answer
    return None  # Gemini answered something outside the list -- don't guess


def classify_via_gemini(title: str, content: str) -> str | None:
    backoff = INITIAL_BACKOFF_SECONDS

    for attempt in range(MAX_RETRIES):
        result: dict = {}

        def _target():
            try:
                result["answer"] = _classify_via_gemini_once(title, content)
            except Exception as exc:
                result["error"] = exc

        thread = threading.Thread(target=_target, daemon=True)
        thread.start()
        thread.join(timeout=GEMINI_CALL_TIMEOUT_SECONDS)

        if thread.is_alive():
            # Call is still hung after the timeout — abandon it and move on.
            # The daemon thread is left to die with the process.
            print(f"  [classify] Gemini call timed out after {GEMINI_CALL_TIMEOUT_SECONDS}s — skipping classification for this article")
            return None
        if "answer" in result:
            return result["answer"]

        error = result.get("error")
        is_rate_limit = error is not None and gemini_keys.is_rate_limit(error)
        if is_rate_limit:
            # The current project's quota is exhausted. If there's another
            # key configured, switch to it and retry — a different account
            # has its own fresh quota. If all keys are exhausted, fail soft
            # now; the next pipeline cycle retries naturally.
            if gemini_keys.key_count() > 1 and attempt < MAX_RETRIES - 1:
                gemini_keys.rotate()
                print(f"  [classify] Gemini rate-limited on key ...{gemini_keys.current_key()[-6:]}, rotating")
                continue
            print(f"  [classify] all Gemini keys rate-limited (429) — skipping classification for this article")
            return None
        if attempt == MAX_RETRIES - 1:
            return None  # non-rate-limit errors: no point retrying
        time.sleep(backoff)
        backoff *= 2

    return None


def classify(article) -> tuple[str | None, str]:
    """Returns (category_or_None, signal_used_for_reporting)."""
    by_url = classify_from_url(article.url)
    if by_url:
        return by_url, "url"
    by_source = classify_from_source_hint(article.source)
    if by_source:
        return by_source, "source-hint"
    by_keywords = classify_from_keywords(article.title, article.content)
    if by_keywords:
        return by_keywords, "keywords"
    by_gemini = classify_via_gemini(article.title, article.content)
    if by_gemini:
        return by_gemini, "gemini"
    return None, "none"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write changes. Without this flag, the script only reports what it would do.",
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        rows = db.query(Article.id, Article.title, Article.content, Article.url, Article.source, Article.category).all()

        if not rows:
            print("No articles found in the database.")
            return

        current_counts = Counter(r.category for r in rows)
        print(f"Total articles: {len(rows)}")
        print(f"Current category distribution: {dict(current_counts)}\n")

        to_update = []  # (id, old_category, new_category, signal)
        signal_counts = Counter()
        unmapped = []

        for row in rows:
            new_category, signal = classify(row)
            if new_category is None:
                unmapped.append(row)
                continue
            signal_counts[signal] += 1
            if new_category != row.category:
                to_update.append((row.id, row.category, new_category, signal))

        print(f"Would change: {len(to_update)} article(s)")
        if to_update:
            by_change = Counter((u[1], u[2], u[3]) for u in to_update)
            print("\nChanges by (old -> new, signal used):")
            for (old, new, signal), count in sorted(by_change.items()):
                print(f"  {count:>4}x  {old!r} -> {new!r}  (via {signal})")

        if unmapped:
            print(f"\nUNMAPPED (left untouched, {len(unmapped)} article(s)):")
            for row in unmapped[:20]:
                print(f"  id={row.id}  source={row.source!r}  title={row.title[:60]!r}")
            if len(unmapped) > 20:
                print(f"  ... and {len(unmapped) - 20} more")
            print(
                "\nThese didn't match a known URL section, source hint, or keyword. "
                "Extend URL_SECTION_MAP / SOURCE_CATEGORY_HINTS / CATEGORY_KEYWORDS above if they should be classified."
            )

        if not args.apply:
            print("\nDry run only — no changes written. Re-run with --apply to commit these updates.")
            return

        if not to_update:
            print("\nNothing to apply.")
            return

        for article_id, old_category, new_category, signal in to_update:
            db.query(Article).filter(Article.id == article_id).update(
                {"category": new_category}
            )
        db.commit()
        print(f"\nApplied. Updated {len(to_update)} article(s).")

    finally:
        db.close()


if __name__ == "__main__":
    main()
