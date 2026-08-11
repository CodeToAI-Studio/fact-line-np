"""
telegram_bot.py

Sends pending posts to your Telegram chat for approval before anything
publishes. Runs continuously via POLLING -- the bot checks Telegram for
updates itself, so no public URL/webhook is needed. This means the exact
same code works on your local machine right now and on Oracle Cloud later
without any changes.

Flow:
  - Every CHECK_INTERVAL_SECONDS, scans for Post rows with status="pending"
    that haven't been sent yet, and sends each one with Approve/Reject
    buttons.
  - Tapping a button updates that Post's status and edits the message to
    show the outcome, so the buttons can't be tapped twice.
  - Pending posts wait indefinitely -- no auto-expiry, by design.

Note on what you'll actually see: Telegram photo captions are capped at
1024 characters, so this shows the (short, 2-4 line) social_summary plus
metadata -- NOT the full 250-400 word article. For anything that needs a
closer look before approving (e.g. checking whether identifying details
in a crime story are appropriate to publish), use view_pending_posts.py
to read the full article first.

SETUP (one-time)
-----------------
1. Message @BotFather on Telegram, use /newbot, follow the prompts, get
   your bot token.
2. Send your new bot any message (e.g. "hi"), then run:
       python telegram_bot.py --get-chat-id
   to find your own chat ID.
3. Add both to your .env:
       TELEGRAM_BOT_TOKEN=...
       TELEGRAM_CHAT_ID=...

USAGE
-----
    python telegram_bot.py
"""

import asyncio
import os
import sys

from dotenv import load_dotenv
load_dotenv()

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Bot
from telegram.ext import Application, ContextTypes

from models import Post, SessionLocal
from telegram_webhook import format_post_message, WEBHOOK_URL

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
CHECK_INTERVAL_SECONDS = 30


async def send_pending_posts(context: ContextTypes.DEFAULT_TYPE):
    db = SessionLocal()
    try:
        pending = (
            db.query(Post)
            .filter(Post.status == "pending", Post.telegram_message_id.is_(None))
            .all()
        )
        for post in pending:
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Approve", callback_data=f"approve:{post.id}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"reject:{post.id}"),
            ]])
            text = format_post_message(post)

            try:
                if post.image_url:
                    message = await context.bot.send_photo(
                        chat_id=TELEGRAM_CHAT_ID,
                        photo=post.image_url,
                        caption=text,
                        parse_mode="HTML",
                        reply_markup=keyboard,
                    )
                else:
                    message = await context.bot.send_message(
                        chat_id=TELEGRAM_CHAT_ID,
                        text=text,
                        parse_mode="HTML",
                        reply_markup=keyboard,
                    )
            except Exception as e:
                # If the image URL is broken/unreachable, fall back to a
                # text-only message rather than losing this post's review
                # entirely.
                print(f"  Post id={post.id}: failed to send with image ({e}), retrying as text-only")
                message = await context.bot.send_message(
                    chat_id=TELEGRAM_CHAT_ID,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                )

            post.telegram_message_id = str(message.message_id)
            db.commit()
            print(f"Sent Post id={post.id} for approval.")
    finally:
        db.close()


# Button taps are handled by the FastAPI webhook in main.py
# (POST /webhooks/telegram) — that is the real-time 24/7 path, and the reason
# this script no longer defines a CallbackQueryHandler. Once a webhook is
# registered for a bot token, Telegram refuses getUpdates (409 CONFLICT), so
# a polling loop here could not receive taps anyway.


async def get_chat_id_mode():
    """One-off helper for setup: prints your chat ID so you can add it to .env."""
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    updates = await bot.get_updates()
    if not updates:
        print("No messages found yet. Send your bot any message on Telegram first, then re-run this.")
        return
    chat_id = updates[-1].message.chat_id
    print(f"Your chat ID is: {chat_id}")
    print(f"Add this to your .env as: TELEGRAM_CHAT_ID={chat_id}")


# Single-instance lock: the venv python.exe spawns a base-python child, and
# BOTH run this script — two getUpdates pollers on one token conflict and
# Telegram kills one. A PID-file lock means only the first process to grab it
# polls; the second exits immediately. (The lock is released on clean exit.)
def _acquire_single_instance_lock() -> bool:
    lock_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "telegram_bot.lock")
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        return False


async def main_once():
    """One-shot bot run for GitHub Actions cron — SEND-ONLY.

    Sends any new pending posts to Telegram for approval, then exits.
    Approve/Reject taps are handled in real time by the FastAPI webhook
    (main.py, POST /webhooks/telegram) on Railway — they must NOT be polled
    here, because getUpdates returns 409 CONFLICT while a webhook is set.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env first.")
        return

    if WEBHOOK_URL and not WEBHOOK_URL.startswith("https://"):
        print(f"WARNING: SITE_BASE_URL does not start with https:// — webhook URL {WEBHOOK_URL!r} is not reachable by Telegram; the bot may not work.")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    await app.initialize()

    # Send any new pending posts (this is the only thing the bot job does now).
    await send_pending_posts(app)

    await app.shutdown()
    print("Bot one-shot pass complete.")


def main():
    if "--get-chat-id" in sys.argv:
        if not TELEGRAM_BOT_TOKEN:
            print("Set TELEGRAM_BOT_TOKEN in your .env first.")
            return
        asyncio.run(get_chat_id_mode())
        return

    if "--once" in sys.argv:
        asyncio.run(main_once())
        return

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in your .env first.")
        return

    if not _acquire_single_instance_lock():
        print("Another telegram_bot instance is already running (lock held) — exiting.", file=sys.stderr)
        return

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.job_queue.run_repeating(send_pending_posts, interval=CHECK_INTERVAL_SECONDS, first=0)

    print(f"Bot running -- checking for new pending posts every {CHECK_INTERVAL_SECONDS}s. Ctrl+C to stop.")
    try:
        # A webhook is the real-time path for taps; a local polling loop can
        # only send. If a webhook is registered (Railway is the always-on
        # host), refuse to poll and exit so we never fight Telegram.
        from telegram import Bot
        info = Bot(token=TELEGRAM_BOT_TOKEN).get_webhook_info()
        if info and info.url:
            print(
                "Webhook already registered (taps handled by the always-on server). "
                "This polling mode would conflict — exiting. Use --once for send-only."
            )
            return
        app.run_polling()
    finally:
        try:
            os.remove(os.path.join(os.path.dirname(os.path.abspath(__file__)), "telegram_bot.lock"))
        except OSError:
            pass


if __name__ == "__main__":
    main()
