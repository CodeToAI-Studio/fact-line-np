"""
backfill_region.py

One-off maintenance script: infers `region` for existing Article rows from
their `source` field and updates the DB accordingly.

Why this exists: models.py defaults region="international" on the column,
so every article ingested before region-awareness existed (or ingested by
code that never set it explicitly) silently landed in "international" even
when it's clearly Nepal-domestic content (e.g. Kathmandu Post articles about
Nepali parliament).

USAGE
-----
    # 1. See what's actually in your DB and how it WOULD be reclassified —
    #    makes no changes.
    python backfill_region.py

    # 2. Once the mapping below looks right for your sources, apply it.
    python backfill_region.py --apply

If you see sources in the "UNMAPPED" section of the dry-run output, add
them to SOURCE_REGION_MAP below before running with --apply — anything not
in the map is left untouched, not guessed at.
"""

import argparse
from collections import Counter

from dotenv import load_dotenv
load_dotenv()  # must run before importing models, so DATABASE_URL is set

from sqlalchemy import func

from models import Article, SessionLocal

# --- Edit this mapping to match your actual sources -------------------------
# Keys are matched case-insensitively as SUBSTRINGS of the stored `source`
# value, so "The Kathmandu Post" matches the "kathmandu post" key below.
# Order doesn't matter; first match wins if a source could match more than
# one key (unlikely, but be specific if you add ambiguous entries).
SOURCE_REGION_MAP = {
    # Nepal-domestic outlets
    "kathmandu post": "nepal",
    "the himalayan times": "nepal",
    "himalayan times": "nepal",
    "republica": "nepal",
    "myrepublica": "nepal",
    "setopati": "nepal",
    "onlinekhabar": "nepal",
    "nepali times": "nepal",
    "ratopati": "nepal",
    "annapurna post": "nepal",
    "nagarik": "nepal",  # was "nagarik news" -- didn't match actual source name "Nagarik Dainik"
    "techmandu": "nepal",  # was missing entirely -- Nepal-based tech outlet
    # International wire services / outlets
    "reuters": "international",
    "associated press": "international",
    "ap news": "international",
    "bbc": "international",
    "al jazeera": "international",
    "afp": "international",
    "cnn": "international",
    "the guardian": "international",
    "new york times": "international",
    "washington post": "international",
    "techcrunch": "international",
    "the verge": "international",
}


def classify(source: str) -> str | None:
    source_lc = source.lower()
    for key, region in SOURCE_REGION_MAP.items():
        if key in source_lc:
            return region
    return None  # unmapped — left untouched


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
        rows = db.query(Article.id, Article.source, Article.region).all()

        if not rows:
            print("No articles found in the database.")
            return

        current_counts = Counter(r.region for r in rows)
        print(f"Total articles: {len(rows)}")
        print(f"Current region distribution: {dict(current_counts)}\n")

        to_update = []  # (id, old_region, new_region, source)
        unmapped_sources = Counter()

        for row in rows:
            new_region = classify(row.source)
            if new_region is None:
                unmapped_sources[row.source] += 1
                continue
            if new_region != row.region:
                to_update.append((row.id, row.region, new_region, row.source))

        print(f"Would change: {len(to_update)} article(s)")
        if to_update:
            preview_counts = Counter((u[1], u[3]) for u in to_update)
            by_source_change = Counter(
                (u[3], u[1], u[2]) for u in to_update
            )
            print("\nChanges by source:")
            for (source, old, new), count in sorted(by_source_change.items()):
                print(f"  {count:>4}x  {source!r}: {old!r} -> {new!r}")

        if unmapped_sources:
            print(f"\nUNMAPPED sources (left untouched, {sum(unmapped_sources.values())} article(s)):")
            for source, count in unmapped_sources.most_common():
                print(f"  {count:>4}x  {source!r}")
            print(
                "\nAdd these to SOURCE_REGION_MAP in this script if they should be classified."
            )

        if not args.apply:
            print("\nDry run only — no changes written. Re-run with --apply to commit these updates.")
            return

        if not to_update:
            print("\nNothing to apply.")
            return

        for article_id, old_region, new_region, source in to_update:
            db.query(Article).filter(Article.id == article_id).update(
                {"region": new_region}
            )
        db.commit()
        print(f"\nApplied. Updated {len(to_update)} article(s).")

    finally:
        db.close()


if __name__ == "__main__":
    main()
