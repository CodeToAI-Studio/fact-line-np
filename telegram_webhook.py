"""
telegram_webhook.py

Shared Telegram-approval logic used by BOTH the live FastAPI webhook
(telegram_bot's real-time path, 24/7 on Railway) and the GitHub Actions
send-only bot job.

Why this exists: taps on the Approve/Reject buttons are handled by a
Telegram webhook pointing at the always-on Railway app — so an approval is
applied the moment the button is tapped (no 5-minute polling lag, no dead
spinner). The GitHub Actions bot job only SENDS new pending posts to the
chat; it must NOT call getUpdates (Telegram returns 409 CONFLICT on
getUpdates while a webhook is set).

The webhook's Telegram secret token is NOT the bot token. It is a separate
shared secret we generate and configure on BOTH the Railway side and in the
Telegram webhook URL (as ?secret=...). Telegram signs every webhook request
with it via the X-Telegram-Bot-Api-Secret-Token header, and we verify it
fails-closed (reject when missing/mismatched) — otherwise anyone who finds
the public webhook URL could approve/reject arbitrary posts.
"""

import hmac
import os

from dotenv import load_dotenv
load_dotenv()

from models import Post

# URL that Telegram will push button taps to. Computed from SITE_BASE_URL so
# it survives a custom domain / Railway service rename without a code change.
SITE_BASE_URL = (os.getenv("SITE_BASE_URL") or "").rstrip("/")
WEBHOOK_URL = f"{SITE_BASE_URL}/webhooks/telegram"
WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")


def webhook_secret() -> str:
    """The shared secret configured on Railway AND in the Telegram webhook URL.

    No default on purpose — a hardcoded fallback would ship in the public
    repo and anyone could forge Approve taps."""
    return WEBHOOK_SECRET.strip()


def _constant_time_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def verify_secret(header: str | None) -> bool:
    """Fails closed: returns True only when the header exactly matches our
    shared secret token. A missing/blank configured secret also fails."""
    secret = webhook_secret()
    if not secret or not header:
        return False
    return _constant_time_eq(header, secret)


def format_post_message(post: Post) -> str:
    """The approval message shown for a pending post. Keep in sync with the
    send path — both the webhook and the send-only bot render the same text."""
    lang_tag = "🇳🇵 Nepali" if post.language == "nepali" else "🌐 English"
    region = post.region or "unknown"
    category = post.category or "uncategorised"
    return (
        f"<b>New post pending approval</b>  ({lang_tag})\n"
        f"Region: {region} | Category: {category}\n\n"
        f"{post.social_summary}\n\n"
        f"<i>Post ID: {post.id} -- full article: view_pending_posts.py</i>"
    )


def apply_action(post: Post, action: str) -> str | None:
    """Apply an approve/reject tap to a Post row.

    Returns a short human-readable outcome label for the edited message, or
    None when the post is already actioned (double-tap / late tap — ignored).
    Mutates + commits the DB row; the caller owns session lifecycle."""
    if post.status != "pending":
        return None

    post.status = "approved" if action == "approve" else "rejected"
    return "✅ APPROVED" if action == "approve" else "❌ REJECTED"
