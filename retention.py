"""
retention.py

Data retention/cleanup automation for the news pipeline.

Implements the policy decided when the Article model was designed (see
models.py docstring): raw ingested items are working material, not a permanent
archive. Two classes of Article rows get cleaned:

  1. **Consumed** Articles whose Post is fully published everywhere
     (FB + IG non-pending). The story it contributed to is live, so the raw
     source text has done its job.

  2. **Retention-window** Articles that were never corroborated into a Post at
     all (post_id is NULL) after N days. They keep getting re-embedded and
     re-clustered every run, so leaving them forever is pure waste.

Deliberately *not* imported into main.py — it belongs in the long-lived
watcher / clock path (retention.run_retention()), not the request handlers.
"""
import os
import sys
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv
load_dotenv()

sys.stdout.reconfigure(encoding="utf-8")

from sqlalchemy import select
from models import Article, Post, PlatformPost, SessionLocal

ACTIVE_PLATFORMS = {"facebook", "instagram"}          # same scope as publisher.py
UNCLUSTERED_RETENTION_DAYS = int(os.getenv("UNCLUSTERED_RETENTION_DAYS", "30"))


def _fully_published(post: Post) -> bool:
    """True if the post is done on every platform the publisher actually handles."""
    active = [pp for pp in post.platform_posts if pp.platform in ACTIVE_PLATFORMS]
    return bool(active) and all(pp.status != "pending" for pp in active)


def run_retention(dry_run: bool = False) -> dict:
    """Delete stale Article rows. Returns a summary of what was removed.

    Returns
    -------
    dict with keys: consumed, unclustered (row counts).
    """
    db = SessionLocal()
    try:
        # --- 1. Consumed articles: post fully published everywhere ----------
        consumed_ids = []
        # Only rows linked to a published post whose FB+IG are both non-pending.
        stmt = (
            select(Article)
            .join(Post, Article.post_id == Post.id)
            .where(Post.status == "published", Article.post_id.isnot(None))
        )
        for art in db.execute(stmt).scalars():
            post = db.get(Post, art.post_id)
            if post and _fully_published(post):
                consumed_ids.append(art.id)

        # --- 2. Unclustered articles past the retention window -------------
        cutoff = datetime.now(timezone.utc) - timedelta(days=UNCLUSTERED_RETENTION_DAYS)
        # select(Article.id) yields scalar ints — use each value directly.
        unclustered_ids = [
            a for a in db.execute(
                select(Article.id).where(
                    Article.post_id.is_(None),
                    Article.created_at < cutoff,
                )
            ).scalars()
        ]

        all_ids = list(dict.fromkeys(consumed_ids + unclustered_ids))  # no double-count
        if all_ids and not dry_run:
            db.query(Article).filter(Article.id.in_(all_ids)).delete(synchronize_session=False)
            db.commit()

        summary = {
            "consumed": len(consumed_ids),
            "unclustered": len(unclustered_ids),
            "total": len(all_ids),
        }
        if dry_run:
            print(f"[retention] DRY RUN — would delete {len(all_ids)} article(s) "
                  f"({len(consumed_ids)} consumed, {len(unclustered_ids)} past "
                  f"{UNCLUSTERED_RETENTION_DAYS}d window).")
        elif all_ids:
            print(f"[retention] Deleted {len(all_ids)} article(s) "
                  f"({len(consumed_ids)} consumed, {len(unclustered_ids)} past "
                  f"{UNCLUSTERED_RETENTION_DAYS}d).")
        else:
            print("[retention] Nothing to clean.")
        return summary
    finally:
        db.close()


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="One-off retention sweep.")
    p.add_argument("--dry-run", action="store_true", help="Report without deleting.")
    a = p.parse_args()
    run_retention(dry_run=a.dry_run)