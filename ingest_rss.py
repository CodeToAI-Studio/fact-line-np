"""
ingest_rss.py  (canonical ingestion script — supersedes ingest.py)

Polls configured RSS feeds, extracts new articles, auto-classifies region
and category (reusing backfill_region.py / backfill_category.py logic so
there is one source of truth for classification rules), embeds them via
Gemini (768-dim), and inserts into the `articles` table.

Improvements over the old ingest.py:
  - Sets both `region` AND `category` on every article.
  - Extracts `image_url` from RSS enclosures / media tags.
  - Has --dry-run mode (parse feeds, show what would be added, write nothing).
  - Reports bozo feed parse errors rather than silently returning 0 entries.
  - HTML is stripped from content before embedding (via BeautifulSoup).

Dedup strategy: skip any URL that already exists in the DB (cheap exact match).
Cross-outlet "same event" detection is handled downstream in generate_posts.py.

USAGE
-----
    python ingest_rss.py             # normal run
    python ingest_rss.py --dry-run   # preview only, nothing written
"""

import sys
import argparse
import re
from collections import namedtuple

import feedparser
from bs4 import BeautifulSoup
from dotenv import load_dotenv
load_dotenv()  # must run before importing models so DATABASE_URL is set

# Windows cp1252 console can't encode emoji or non-ASCII article titles.
sys.stdout.reconfigure(encoding="utf-8")

from models import Article, SessionLocal
from embeddings import get_embedding
from backfill_region import classify as classify_region
from backfill_category import classify as classify_category

# ---------------------------------------------------------------------------
# RSS feed list
# (display_name, feed_url, verified)
# "verified" = confirmed the URL returns valid XML. Set to True after checking.
# A broken feed silently returns 0 entries (feedparser doesn't crash) — watch
# the per-feed counts in the output to catch silent failures.
# ---------------------------------------------------------------------------
RSS_FEEDS = [
    # Nepal sources
    ("The Kathmandu Post",    "https://kathmandupost.com/rss",                    True),
    ("The Himalayan Times",   "https://thehimalayantimes.com/feed",               True),
    ("OnlineKhabar English",  "https://english.onlinekhabar.com/feed",            True),
    ("Ratopati",              "https://www.ratopati.com/feed",                    True),
    ("Nagarik Dainik",        "https://nagariknews.nagariknetwork.com/feed",      True),
    ("Nepali Times",          "https://www.nepalitimes.com/feed/",                True),
    ("Techmandu",             "https://techmandu.com/feed/",                      True),
    # International sources
    ("BBC News",              "http://feeds.bbci.co.uk/news/rss.xml",            True),
    ("TechCrunch",            "https://techcrunch.com/feed/",                     True),
    ("The Verge",             "https://www.theverge.com/rss/index.xml",           True),
    ("Ars Technica",          "https://feeds.feedburner.com/ArsTechnica",         True),
]

# Lightweight stand-in so we can pass a freshly-parsed RSS entry (not yet an
# Article ORM row) to classify_category(), which expects .url/.source/.title/.content
ClassifyInput = namedtuple("ClassifyInput", ["url", "source", "title", "content"])


def clean_html(raw: str) -> str:
    """Strip HTML tags and collapse whitespace.
    RSS content fields often contain full markup; plain text is needed for
    embedding quality and readable display."""
    if not raw:
        return ""
    text = BeautifulSoup(raw, "html.parser").get_text(separator=" ")
    return re.sub(r"\s+", " ", text).strip()


def extract_image_url(entry) -> str | None:
    """RSS entries expose images in several non-standardised ways.
    Check the common ones in order of reliability."""
    media_content = entry.get("media_content")
    if media_content:
        url = media_content[0].get("url")
        if url:
            return url
    media_thumbnail = entry.get("media_thumbnail")
    if media_thumbnail:
        url = media_thumbnail[0].get("url")
        if url:
            return url
    for link in entry.get("links", []):
        if link.get("type", "").startswith("image/"):
            return link.get("href")
    for enclosure in entry.get("enclosures", []):
        if enclosure.get("type", "").startswith("image/"):
            return enclosure.get("href")
    return None


def extract_content(entry) -> str:
    """Prefer full content (content:encoded) over the short summary."""
    if "content" in entry and entry["content"]:
        return entry["content"][0].get("value", "")
    return entry.get("summary", "") or entry.get("description", "")


def run_ingestion(dry_run: bool = False) -> int:
    """Poll all RSS feeds, embed and store new articles.

    Returns the number of new articles committed (0 if nothing new or dry run).
    Callers (e.g. watch_pipeline.py) use the return value to decide whether
    to trigger the downstream clustering + publishing steps.
    """
    unverified = [name for name, _, verified in RSS_FEEDS if not verified]
    if unverified:
        print(
            f"WARNING: {len(unverified)} feed(s) marked unverified: {', '.join(unverified)}. "
            "A broken feed returns 0 entries silently — check per-feed counts below.\n"
        )

    db = SessionLocal()
    try:
        existing_urls = {row.url for row in db.query(Article.url).all()}
        print(f"Found {len(existing_urls)} existing articles in DB.\n")

        total_new = 0
        for source_name, feed_url, _ in RSS_FEEDS:
            parsed = feedparser.parse(feed_url)

            if parsed.bozo:
                print(f"[{source_name}] WARNING: feed parse error — {parsed.bozo_exception}")

            entries = parsed.entries
            print(f"[{source_name}] {len(entries)} entries in feed")

            new_count = 0
            for entry in entries:
                url = entry.get("link")
                if not url or url in existing_urls:
                    continue

                title = clean_html(entry.get("title", ""))
                content = clean_html(extract_content(entry))

                if not title or len(content) < 30:
                    continue  # too sparse to be useful

                image_url = extract_image_url(entry)
                region = classify_region(source_name) or "international"
                classify_input = ClassifyInput(url=url, source=source_name, title=title, content=content)
                category, _ = classify_category(classify_input)
                category = category or "general"

                if dry_run:
                    print(f"  WOULD ADD: {title[:70]!r} (region={region}, category={category}, image={'yes' if image_url else 'no'})")
                    new_count += 1
                    continue

                embedding = get_embedding(f"{title}. {content}", task_type="RETRIEVAL_DOCUMENT")

                article = Article(
                    title=title,
                    source=source_name,
                    url=url,
                    content=content,
                    region=region,
                    category=category,
                    embedding=embedding,
                    image_url=image_url,
                )
                db.add(article)
                existing_urls.add(url)
                new_count += 1
                print(f"  + {title[:70]!r}")

            total_new += new_count
            print(f"[{source_name}] {new_count} new article(s){' (dry run)' if dry_run else ''}\n")

        if not dry_run and total_new > 0:
            db.commit()
            print(f"Committed {total_new} new article(s) total.")
        elif dry_run:
            print(f"Dry run — {total_new} article(s) would be added. Nothing written.")
        else:
            print("No new articles found.")

        return 0 if dry_run else total_new

    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse feeds and report what would be ingested, but write nothing to the DB.",
    )
    args = parser.parse_args()
    run_ingestion(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
