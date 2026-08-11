"""
retention.py

Data retention/cleanup automation for the news pipeline.

Implements the policy decided when the Article model was designed (see
models.py docstring): raw ingested items are working material, not a permanent
archive. Five classes of rows get cleaned:

  1. **Consumed** Articles whose Post is fully published everywhere
     (FB + IG non-pending). The story it contributed to is live, so the raw
     source text has done its job.

  2. **Retention-window** Articles that were never corroborated into a Post at
     all (post_id is NULL) after N days. They keep getting re-embedded and
     re-clustered every run, so leaving them forever is pure waste.

  3. **Stale pending Posts** (status="pending") that nobody approved within
     PENDING_POST_HOURS. An unapproved post is stale news by then; deleting it
     keeps the approval queue fresh and the DB clean. Its PlatformPost rows
     cascade away, and its linked Articles are released (post_id → NULL) so a
     future ingestion pass can re-corroborate the story if it resurfaces.

  4. **Rejected Posts** (status="rejected") are a dead end and are deleted on
     the same sweep — nobody will approve them later, and their platform rows
     never publish. Same article-release semantics as stale pending posts.

  5. **Admin clutter**: expired AdminSession rows (plus defensive leftovers
     past SESSION_RETENTION_DAYS) and AuditLog rows older than
     AUDIT_RETENTION_DAYS are purged so the admin tables don't grow unbounded.

Deliberately *not* imported into main.py — it belongs in the long-lived
watcher / clock path (retention.run_retention()), not the request handlers.
"""
import os
import sys
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv
load_dotenv()

sys.stdout.reconfigure(encoding="utf-8")

from sqlalchemy import select, delete
from models import Article, Post, PlatformPost, SessionLocal
from admin_models import AdminSession, AuditLog

ACTIVE_PLATFORMS = {"facebook", "instagram"}          # same scope as publisher.py
UNCLUSTERED_RETENTION_DAYS = int(os.getenv("UNCLUSTERED_RETENTION_DAYS", "30"))
PENDING_POST_HOURS = int(os.getenv("PENDING_POST_HOURS", "24"))
SESSION_RETENTION_DAYS = int(os.getenv("SESSION_RETENTION_DAYS", "7"))
AUDIT_RETENTION_DAYS = int(os.getenv("AUDIT_RETENTION_DAYS", "90"))


def _fully_published(post: Post) -> bool:
    """True if the post is done on every platform the publisher actually handles."""
    active = [pp for pp in post.platform_posts if pp.platform in ACTIVE_PLATFORMS]
    return bool(active) and all(pp.status != "pending" for pp in active)


def _delete_stale_pending_posts(db, dry_run: bool) -> list[int]:
    """Delete Post rows still awaiting approval past the auto-expiry window.

    Posts created for approval (status="pending") that nobody acted on within
    PENDING_POST_HOURS are stale — the story is no longer fresh, and keeping
    them means they either sit forever or get approved weeks late (publishing
    stale news). Delete the row entirely: its PlatformPost rows cascade, and
    its linked Articles are released (post_id → NULL) so the next clustering
    pass can re-use them if the story resurfaces.

    Returns the list of deleted post ids (empty on dry run).
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=PENDING_POST_HOURS)
    stale_ids = [
        p_id for p_id in db.execute(
            select(Post.id).where(
                Post.status == "pending",
                Post.created_at < cutoff,
            )
        ).scalars()
    ]

    if not stale_ids:
        return []

    if not dry_run:
        # Release the working material so a future ingestion/clustering pass
        # can re-corroborate the story — do not delete the raw articles here.
        db.execute(
            Article.__table__.update()
            .where(Article.post_id.in_(stale_ids))
            .values(post_id=None)
        )
        # platform_posts cascade at the DB level (ON DELETE CASCADE on the FK)
        # — the ORM delete-orphan cascade does NOT fire for core bulk deletes.
        db.execute(delete(Post).where(Post.id.in_(stale_ids)))
        db.commit()

    print(
        f"[retention] Deleted {len(stale_ids)} post(s) not approved within "
        f"{PENDING_POST_HOURS}h."
        if not dry_run else
        f"[retention] DRY RUN — would delete {len(stale_ids)} post(s) not "
        f"approved within {PENDING_POST_HOURS}h."
    )
    return stale_ids


def _delete_rejected_posts(db, dry_run: bool) -> int:
    """Delete Post rows that were explicitly REJECTED (status="rejected").

    A rejected post is a dead end — nobody is going to approve it later, and
    its platform_posts rows (all pending) will never publish. Unlike pending
    posts it isn't waiting on a human decision, so there's no reason to keep
    it around for the approval window; clean it immediately.

    Same article-release semantics as stale pending posts: linked Articles get
    post_id → NULL (kept as unclustered working material, re-clusterable if the
    story resurfaces), then the post is deleted and its platform_posts rows
    cascade at the DB level (ON DELETE CASCADE on the FK).

    Returns the number of rejected posts deleted (0 on dry run).
    """
    rejected_ids = [
        p_id for p_id in db.execute(
            select(Post.id).where(Post.status == "rejected")
        ).scalars()
    ]
    if not rejected_ids:
        return 0

    if not dry_run:
        db.execute(
            Article.__table__.update()
            .where(Article.post_id.in_(rejected_ids))
            .values(post_id=None)
        )
        db.execute(delete(Post).where(Post.id.in_(rejected_ids)))
        db.commit()

    print(
        f"[retention] Deleted {len(rejected_ids)} rejected post(s)."
        if not dry_run else
        f"[retention] DRY RUN — would delete {len(rejected_ids)} rejected post(s)."
    )
    return len(rejected_ids)


def _purge_expired_sessions(db, dry_run: bool) -> int:
    """Delete AdminSession rows past their expiry, plus any stale leftovers
    older than SESSION_RETENTION_DAYS (defensive). Admin sessions are created
    on login and deleted on logout, but aborted logins or crashed processes
    leave orphaned rows that would otherwise accumulate forever."""
    now = datetime.now(timezone.utc)
    stale = db.execute(
        select(AdminSession.session_id).where(
            AdminSession.expires_at < now - timedelta(days=SESSION_RETENTION_DAYS)
        )
    ).scalars().all()
    if stale and not dry_run:
        db.execute(delete(AdminSession).where(AdminSession.session_id.in_(stale)))
        db.commit()
    if stale:
        print(
            f"[retention] Purged {len(stale)} expired admin session(s)."
            if not dry_run else
            f"[retention] DRY RUN — would purge {len(stale)} expired admin session(s)."
        )
    return len(stale)


def _purge_old_audit_logs(db, dry_run: bool) -> int:
    """Delete AuditLog rows older than AUDIT_RETENTION_DAYS. Admin actions are
    logged for a while for security/debugging, but keeping them forever is
    unbounded growth for little value — 90 days is plenty."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=AUDIT_RETENTION_DAYS)
    old = db.execute(
        select(AuditLog.id).where(AuditLog.timestamp < cutoff)
    ).scalars().all()
    if old and not dry_run:
        db.execute(delete(AuditLog).where(AuditLog.id.in_(old)))
        db.commit()
    if old:
        print(
            f"[retention] Purged {len(old)} audit log(s) older than "
            f"{AUDIT_RETENTION_DAYS}d."
            if not dry_run else
            f"[retention] DRY RUN — would purge {len(old)} audit log(s) older than "
            f"{AUDIT_RETENTION_DAYS}d."
        )
    return len(old)


def run_retention(dry_run: bool = False) -> dict:
    """Delete stale Article and Post rows. Returns a summary of what was removed.

    Returns
    -------
    dict with keys: consumed, unclustered, stale_pending_posts, rejected_posts,
    expired_sessions, old_audit_logs (row counts).
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

        # --- 3. Pending posts past the approval window ----------------------
        stale_post_ids = _delete_stale_pending_posts(db, dry_run=dry_run)

        # --- 4. Rejected posts (dead end, clean immediately) ----------------
        rejected_ids = _delete_rejected_posts(db, dry_run=dry_run)

        # --- 5. Expired admin sessions + old audit logs ----------------------
        expired_sessions = _purge_expired_sessions(db, dry_run=dry_run)
        old_audit_logs = _purge_old_audit_logs(db, dry_run=dry_run)

        summary = {
            "consumed": len(consumed_ids),
            "unclustered": len(unclustered_ids),
            "stale_pending_posts": len(stale_post_ids),
            "rejected_posts": rejected_ids,
            "expired_sessions": expired_sessions,
            "old_audit_logs": old_audit_logs,
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