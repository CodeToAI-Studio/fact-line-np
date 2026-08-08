import sys
from dotenv import load_dotenv

# Load environment variables FIRST before importing database models
load_dotenv()

# Windows cp1252 console can't encode emoji; reconfigure stdout to UTF-8.
sys.stdout.reconfigure(encoding='utf-8')

import re
import feedparser
from bs4 import BeautifulSoup
from sqlalchemy import select
from models import Article, SessionLocal
from embeddings import get_embedding

# RSS feeds to ingest (Global + Nepal sources)
# region is set per-feed rather than left to the Article column default.
# The default is "international", so without this every newly ingested
# Kathmandu Post / OnlineKhabar / Himalayan Times article would be filed as
# international and silently vanish from the "Nepal" filter in app.py --
# the existing 180 nepal rows were populated outside this script, so the
# gap only shows up on new ingests. Values are lowercase to match what's
# already stored (main.py filters case-insensitively regardless).
RSS_FEEDS = [
    {"source": "BBC News", "url": "http://feeds.bbci.co.uk/news/rss.xml", "region": "international"},
    {"source": "TechCrunch", "url": "https://techcrunch.com/feed/", "region": "international"},
    {"source": "The Verge", "url": "https://www.theverge.com/rss/index.xml", "region": "international"},
    {"source": "Ars Technica", "url": "https://feeds.feedburner.com/ArsTechnica", "region": "international"},
    {"source": "OnlineKhabar English", "url": "https://english.onlinekhabar.com/feed", "region": "nepal"},
    {"source": "The Kathmandu Post", "url": "https://kathmandupost.com/rss", "region": "nepal"},
    {"source": "The Himalayan Times", "url": "https://thehimalayantimes.com/feed", "region": "nepal"},
]

# Embeddings come from the Gemini API (768-dim), not the old local
# 384-dim sentence-transformers model. This file was missed when the corpus
# was migrated (see migrate_switch_to_gemini_embeddings.py), so it was still
# writing 384-dim vectors into a vector(768) column -- every insert of a new
# article would have failed on a dimension mismatch.


def clean_html(raw_html: str) -> str:
    """Strips HTML tags and extra whitespace from RSS content."""
    if not raw_html:
        return ""
    soup = BeautifulSoup(raw_html, "html.parser")
    text = soup.get_text(separator=" ")
    return re.sub(r"\s+", " ", text).strip()


def run_ingestion():
    db = SessionLocal()
    try:
        # Step 2.2: Batch fetch existing URLs from DB to prevent N+1 query bottlenecks
        existing_urls_result = db.execute(select(Article.url)).scalars().all()
        existing_urls = set(existing_urls_result)

        print(f"📦 Found {len(existing_urls)} existing articles in database.")
        new_articles_count = 0

        for feed_info in RSS_FEEDS:
            source_name = feed_info["source"]
            feed_url = feed_info["url"]
            feed_region = feed_info["region"]
            print(f"\n📡 Parsing RSS feed: {source_name} ({feed_url})...")
            
            parsed_feed = feedparser.parse(feed_url)

            for entry in parsed_feed.entries:
                article_url = entry.get("link", "")

                # Deduplication check
                if not article_url or article_url in existing_urls:
                    continue

                title = clean_html(entry.get("title", ""))

                # Content fallback hierarchy (content -> summary -> description)
                content_raw = ""
                if "content" in entry and len(entry.content) > 0:
                    content_raw = entry.content[0].get("value", "")
                elif "summary" in entry:
                    content_raw = entry.get("summary", "")
                elif "description" in entry:
                    content_raw = entry.get("description", "")

                content = clean_html(content_raw) or title

                # Skip invalid or empty entries
                if not title or len(content) < 30:
                    continue

                # Step 2.3: Generate embedding vector. Text format and
                # task_type must match what the migration used when it
                # re-embedded existing rows, or new articles land in a
                # different space than the old ones and ranking goes wrong.
                text_to_embed = f"{title}. {content}"
                vector_embedding = get_embedding(
                    text_to_embed, task_type="RETRIEVAL_DOCUMENT"
                )

                # Step 2.4: Queue record for bulk database commit
                new_article = Article(
                    title=title,
                    source=source_name,
                    url=article_url,
                    content=content,
                    region=feed_region,
                    embedding=vector_embedding,
                )
                db.add(new_article)
                existing_urls.add(article_url)
                new_articles_count += 1
                print(f"  └─ ➕ Ingested: '{title[:60]}...'")

        db.commit()
        print(f"\n✅ Ingestion complete! Added {new_articles_count} new articles.")

    except Exception as e:
        db.rollback()
        print(f"❌ Error during ingestion: {e}")
    finally:
        db.close()


if __name__ == "__main__":
    run_ingestion()