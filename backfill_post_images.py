"""
backfill_post_images.py

One-off: for every post whose Instagram/Facebook platform row is stuck
('failed' or 'pending' because it lacked a usable image), acquire a
guaranteed image (source downloaded + normalized, or a branded placeholder),
store the bytes on Post.image_data, set the public /post_image URL, and reset
the IG/FB rows to 'pending' so the publisher retries and they publish.

Run AFTER the /post_image route is live on Railway (the public URL must be
reachable by Instagram), and after migrate_post_image_data.py.

USAGE
-----
    python backfill_post_images.py
"""

from dotenv import load_dotenv
load_dotenv()

import os

import images
from models import Post, SessionLocal
from sqlalchemy import select, or_


def main():
    site_base = os.getenv("SITE_BASE_URL", "").strip()
    if not site_base:
        print("WARNING: SITE_BASE_URL not set — public image URLs will be relative.")
    db = SessionLocal()
    try:
        # Posts with an IG or FB platform row that is failed/pending and that
        # have no stored image yet (or whose image_url is still an external URL).
        stuck = db.execute(
            select(Post).where(
                Post.status.in_(["approved", "published"]),
                or_(Post.image_data.is_(None), Post.image_url.like("http%")),
            )
        ).scalars().all()
        print(f"{len(stuck)} post(s) to backfill.\n")

        for post in stuck:
            b = images.acquire_post_image(
                post.image_url, post.social_summary or post.full_body or "", post.id
            )
            if not b:
                print(f"  post {post.id}: could not acquire image — skipping")
                continue
            post.image_data = b
            post.image_url = images.public_image_url(post.id, site_base or None)
            if not post.image_source_credit:
                post.image_source_credit = "Fact Line NP"
            db.flush()
            print(f"  post {post.id}: image stored ({len(b)} bytes) -> {post.image_url}")

            # Reset stuck IG/FB platform rows so the publisher retries them.
            for pp in post.platform_posts:
                if pp.platform in ("facebook", "instagram") and pp.status in ("failed",):
                    pp.status = "pending"
                    print(f"    reset {pp.platform} row id={pp.id} to pending")

        db.commit()
        print("\nBackfill done. Run publisher.py to publish the reset rows.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
