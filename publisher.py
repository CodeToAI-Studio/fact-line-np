"""
publisher.py

Publishes approved Post rows to Facebook and Instagram via the Graph API.

For each Post with status="approved" that has PlatformPost rows still
status="pending" for facebook or instagram:
  1. Post to Facebook — text post or photo post if image_url is set.
  2. Post to Instagram — two-step container → publish (image required;
     posts without image_url are skipped and left pending for a retry
     once an image is available).
  3. Update PlatformPost.status / platform_post_id / published_at.
  4. If ALL platform_posts for that Post are now non-pending,
     flip Post.status to "published".

Required .env variables
-----------------------
  FACEBOOK_PAGE_ID               — numeric Page ID (not username)
  FACEBOOK_PAGE_ACCESS_TOKEN     — long-lived Page access token with
                                   pages_manage_posts and
                                   instagram_content_publish permissions
  INSTAGRAM_BUSINESS_ACCOUNT_ID  — numeric IG Business Account ID linked
                                   to the Facebook Page above

How to get these
----------------
  1. Go to https://developers.facebook.com → create or open your App.
  2. Add the Facebook Page to the App under Permissions.
  3. Use Graph API Explorer to generate a Page access token; then exchange
     it for a long-lived token via:
     GET /oauth/access_token?grant_type=fb_exchange_token&...
  4. Find your IG Business Account ID:
     GET /{page-id}?fields=instagram_business_account&access_token={token}

USAGE
-----
    python publisher.py            # publish all pending approved posts
    python publisher.py --dry-run  # preview only, nothing posted or written
"""
import sys
import argparse
import os
from datetime import datetime, timezone

import requests as http_requests
from dotenv import load_dotenv

import images

load_dotenv()

# Windows cp1252 console can't encode emoji or non-ASCII content.
sys.stdout.reconfigure(encoding="utf-8")

from models import Post, PlatformPost, SessionLocal
from sqlalchemy import select

GRAPH_API_VERSION = "v20.0"
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

FACEBOOK_PAGE_ID = os.getenv("FACEBOOK_PAGE_ID")
FACEBOOK_PAGE_ACCESS_TOKEN = os.getenv("FACEBOOK_PAGE_ACCESS_TOKEN")
INSTAGRAM_BUSINESS_ACCOUNT_ID = os.getenv("INSTAGRAM_BUSINESS_ACCOUNT_ID")

# Instagram caption limit is 2,200 chars; truncate gracefully if needed.
IG_CAPTION_LIMIT = 2200


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def post_to_facebook(post: Post, dry_run: bool = False) -> tuple[bool, str, str]:
    """
    Returns (success, platform_post_id, log_message).
    On failure, platform_post_id is an empty string.
    """
    if not FACEBOOK_PAGE_ID or not FACEBOOK_PAGE_ACCESS_TOKEN:
        return False, "", "Missing FACEBOOK_PAGE_ID or FACEBOOK_PAGE_ACCESS_TOKEN in .env"

    caption = (post.social_summary or "").strip()
    if not caption:
        return False, "", "Post has no social_summary — cannot publish"

    if dry_run:
        has_image = "with image" if post.image_url else "text only"
        print(f"    [FB DRY RUN] {has_image}: {caption[:80]!r}")
        return True, "dry-run-fb", "dry run"

    # Decide whether the image can actually be used. Facebook downloads the
    # image server-side, so it must be PUBLICLY reachable (no hotlink
    # protection, not expired). Many sources (Ratopati CDN returns 403,
    # BBC images 404 after a while) are not — which is why photo posts to FB
    # were failing wholesale. If the image can't be fetched, fall back to a
    # text-only post rather than failing the whole publish.
    image_url = None
    if post.image_url:
        try:
            probe = http_requests.get(post.image_url, timeout=15)
            if probe.status_code == 200 and (probe.headers.get("Content-Type", "").startswith("image")):
                image_url = post.image_url
            else:
                print(f"    [FB] image not fetchable ({probe.status_code}) — falling back to text-only")
        except Exception:
            print("    [FB] image fetch failed — falling back to text-only")

    if image_url:
        # Photo post: the image is attached and the caption goes with it.
        url = f"{GRAPH_BASE}/{FACEBOOK_PAGE_ID}/photos"
        payload = {
            "url": image_url,
            "caption": caption,
            "access_token": FACEBOOK_PAGE_ACCESS_TOKEN,
            "published": "true",
        }
    else:
        # Text-only post.
        url = f"{GRAPH_BASE}/{FACEBOOK_PAGE_ID}/feed"
        payload = {
            "message": caption,
            "access_token": FACEBOOK_PAGE_ACCESS_TOKEN,
        }

    try:
        resp = http_requests.post(url, data=payload, timeout=30)
        data = resp.json()
    except Exception as e:
        return False, "", f"HTTP error posting to Facebook: {e}"

    if resp.status_code == 200 and ("id" in data or "post_id" in data):
        fb_id = data.get("post_id") or data.get("id")
        return True, str(fb_id), f"✅ Facebook: {fb_id}"
    else:
        err = data.get("error", {})
        msg = err.get("message", resp.text[:200])
        return False, "", f"❌ Facebook error {err.get('code', '?')}: {msg}"


def post_to_instagram(post: Post, dry_run: bool = False) -> tuple[bool | None, str, str]:
    """
    Every post is guaranteed an image (see images.acquire_post_image): the
    source image is downloaded + normalized to an IG-ready square and saved
    locally, or a branded placeholder is generated when none exists. The
    local JPEG is uploaded as a multipart file, so IG never depends on an
    external URL being reachable and the aspect-ratio error (36003) cannot
    recur.

    Returns (success, platform_post_id, log_message).
      True  — published successfully
      False — API error; caller marks PlatformPost as "failed"
    """
    if not INSTAGRAM_BUSINESS_ACCOUNT_ID or not FACEBOOK_PAGE_ACCESS_TOKEN:
        return False, "", "Missing INSTAGRAM_BUSINESS_ACCOUNT_ID or FACEBOOK_PAGE_ACCESS_TOKEN in .env"

    caption = _truncate((post.social_summary or "").strip(), IG_CAPTION_LIMIT)
    if not caption:
        return False, "", "Post has no social_summary — cannot publish"

    if dry_run:
        print(f"    [IG DRY RUN] with image: {caption[:80]!r}")
        return True, "dry-run-ig", "dry run"

    # Acquire a guaranteed image (source downloaded+normalized, or branded
    # placeholder). Store the bytes on the post and use the absolute public
    # /post_image/{id}.jpg URL — IG requires a reachable public image_url.
    site_base = os.getenv("SITE_BASE_URL", "").strip()
    if not site_base:
        print("    [IG] WARNING: SITE_BASE_URL not set — IG image_url will be relative and may fail")
    acquired = images.acquire_post_image(
        post.image_url, post.social_summary or post.full_body or "", post.id
    )
    if not acquired:
        return False, "", "Could not acquire an image for this post"
    post.image_data = acquired
    post.image_url = images.public_image_url(post.id, site_base or None)
    if not post.image_source_credit:
        post.image_source_credit = "Fact Line NP"

    # Step 1 — create media container using the public image URL.
    try:
        r1 = http_requests.post(
            f"{GRAPH_BASE}/{INSTAGRAM_BUSINESS_ACCOUNT_ID}/media",
            data={
                "image_url": images.public_image_url(post.id, site_base or None),
                "caption": caption,
                "access_token": FACEBOOK_PAGE_ACCESS_TOKEN,
            },
            timeout=60,
        )
        d1 = r1.json()
    except Exception as e:
        return False, "", f"HTTP error creating IG container: {e}"

    if r1.status_code != 200 or "id" not in d1:
        err = d1.get("error", {})
        msg = err.get("message", r1.text[:200])
        return False, "", f"❌ Instagram container error {err.get('code', '?')}: {msg}"

    creation_id = d1["id"]

    # Step 2 — publish the container
    try:
        r2 = http_requests.post(
            f"{GRAPH_BASE}/{INSTAGRAM_BUSINESS_ACCOUNT_ID}/media_publish",
            data={
                "creation_id": creation_id,
                "access_token": FACEBOOK_PAGE_ACCESS_TOKEN,
            },
            timeout=30,
        )
        d2 = r2.json()
    except Exception as e:
        return False, "", f"HTTP error publishing IG container: {e}"

    if r2.status_code == 200 and "id" in d2:
        return True, str(d2["id"]), f"✅ Instagram: {d2['id']}"
    else:
        err = d2.get("error", {})
        msg = err.get("message", r2.text[:200])
        return False, "", f"❌ Instagram publish error {err.get('code', '?')}: {msg}"


def run_publisher(dry_run: bool = False):
    """
    Find all approved Post rows with pending facebook/instagram PlatformPost
    rows, publish them, update the PlatformPost status, and flip Post.status
    to "published" once all its platform_posts are done.
    """
    db = SessionLocal()
    try:
        # Find all PlatformPost rows that are pending for fb/ig under approved
        # OR already-published posts. The "published" case covers legacy posts
        # that flipped to published before their FB/IG rows completed (e.g. the
        # image-URL failures); their pending rows still need a final push.
        stmt = (
            select(PlatformPost)
            .join(Post)
            .where(
                Post.status.in_(["approved", "published"]),
                PlatformPost.status == "pending",
                PlatformPost.platform.in_(["facebook", "instagram"]),
            )
            .order_by(Post.id, PlatformPost.platform)
        )
        pending = db.execute(stmt).scalars().all()

        if not pending:
            print("✅ No approved posts with pending Facebook/Instagram platform_posts.")
            return

        print(f"📤 Found {len(pending)} pending platform post(s) to publish.\n")

        processed_post_ids = set()

        for pp in pending:
            post = pp.post
            print(
                f"Post id={post.id} ({post.language or '?'} / {post.region or '?'} / {post.category or '?'})"
            )
            print(f"  Platform: {pp.platform}")

            if pp.platform == "facebook":
                success, platform_id, msg = post_to_facebook(post, dry_run=dry_run)
            elif pp.platform == "instagram":
                success, platform_id, msg = post_to_instagram(post, dry_run=dry_run)
            else:
                continue

            print(f"  {msg}")

            if not dry_run:
                if success is True:
                    pp.status = "published"
                    pp.platform_post_id = platform_id
                    pp.published_at = datetime.now(timezone.utc)
                elif success is False:
                    # Real API error — mark failed so it doesn't retry every run.
                    # Fix the underlying cause, then reset status to "pending" manually.
                    pp.status = "failed"
                # success is None → skipped (e.g. no image for IG); leave as "pending"

            processed_post_ids.add(post.id)

        if not dry_run:
            # Flip Post.status to "published" if all fb/ig PlatformPost rows
            # are now non-pending. Deliberately excludes website/threads/tiktok —
            # those platforms are not yet implemented and their rows stay pending
            # indefinitely; they must not block the parent Post from being marked done.
            # Sweep ALL approved posts (not just the ones touched this run) so that
            # a post whose FB+IG were published in a prior run still gets flipped.
            ACTIVE_PLATFORMS = {"facebook", "instagram"}
            approved_posts = db.execute(
                select(Post).where(Post.status == "approved")
            ).scalars().all()
            for post in approved_posts:
                active_pps = [pp for pp in post.platform_posts if pp.platform in ACTIVE_PLATFORMS]
                all_done = active_pps and all(pp.status != "pending" for pp in active_pps)
                if all_done:
                    post.status = "published"
                    print(f"\n✅ Post id={post.id}: FB+IG done → Post.status=published")
                    # Mark the website PlatformPost as published too — the /posts API
                    # endpoint is now serving this post, so it is effectively "live".
                    for pp in post.platform_posts:
                        if pp.platform == "website" and pp.status == "pending":
                            pp.status = "published"
                            pp.published_at = datetime.now(timezone.utc)

            db.commit()
            print("\n💾 Committed.")
        else:
            print("\n🔍 Dry run — nothing written to the database.")

    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Publish approved posts to Facebook and Instagram."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be published without actually posting or updating the DB.",
    )
    args = parser.parse_args()
    run_publisher(dry_run=args.dry_run)
