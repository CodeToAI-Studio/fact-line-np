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

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

from models import Post, SessionLocal

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
CHECK_INTERVAL_SECONDS = 30


def format_post_message(post: Post) -> str:
    lang_tag = "🇳🇵 Nepali" if post.language == "nepali" else "🌐 English"
    region = post.region or "unknown"
    category = post.category or "uncategorised"
    return (
        f"<b>New post pending approval</b>  ({lang_tag})\n"
        f"Region: {region} | Category: {category}\n\n"
        f"{post.social_summary}\n\n"
        f"<i>Post ID: {post.id} -- full article: view_pending_posts.py</i>"
    )


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


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()  # acknowledge immediately so the tap doesn't show a stuck loading spinner

    action, post_id_str = query.data.split(":")
    post_id = int(post_id_str)

    db = SessionLocal()
    try:
        post = db.query(Post).filter(Post.id == post_id).first()
        if not post:
            return

        if post.status != "pending":
            # Already actioned (e.g. a double-tap before the edit landed) -- ignore.
            return

        post.status = "approved" if action == "approve" else "rejected"
        db.commit()

        outcome_label = "✅ APPROVED" if action == "approve" else "❌ REJECTED"
        new_text = f"{format_post_message(post)}\n\n<b>{outcome_label}</b>"

        if query.message.photo:
            await query.edit_message_caption(caption=new_text, parse_mode="HTML")
        else:
            await query.edit_message_text(text=new_text, parse_mode="HTML")

        print(f"Post id={post.id} -> {post.status}")
    finally:
        db.close()


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


def main():
    if "--get-chat-id" in sys.argv:
        if not TELEGRAM_BOT_TOKEN:
            print("Set TELEGRAM_BOT_TOKEN in your .env first.")
            return
        asyncio.run(get_chat_id_mode())
        return

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in your .env first.")
        return

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CallbackQueryHandler(handle_button))
    app.job_queue.run_repeating(send_pending_posts, interval=CHECK_INTERVAL_SECONDS, first=0)

    print(f"Bot running -- checking for new pending posts every {CHECK_INTERVAL_SECONDS}s. Ctrl+C to stop.")
    app.run_polling()


if __name__ == "__main__":
    main()
