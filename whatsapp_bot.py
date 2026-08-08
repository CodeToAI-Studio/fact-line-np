"""
whatsapp_bot.py

Sends pending posts via WhatsApp template message for approval. This is
ONLY the outbound side -- the actual button-tap handling (updating post
status) lives in main.py's POST /webhooks/whatsapp endpoint, since that's
what needs to be publicly reachable, not this script.

Run this periodically (manually while testing, a scheduled task later)
to check for and send new pending posts.

USAGE
-----
    python whatsapp_bot.py
"""

import os

from dotenv import load_dotenv
load_dotenv()

from models import Post, SessionLocal
from whatsapp_client import send_template_message

WHATSAPP_TO_NUMBER = os.getenv("WHATSAPP_TO_NUMBER")
TEMPLATE_NAME = "post_approval_request"  # must match the name approved in WhatsApp Manager


def main():
    if not WHATSAPP_TO_NUMBER:
        print("Set WHATSAPP_TO_NUMBER in your .env first.")
        return

    db = SessionLocal()
    try:
        pending = (
            db.query(Post)
            .filter(Post.status == "pending", Post.whatsapp_message_id.is_(None))
            .all()
        )
        print(f"{len(pending)} pending post(s) to send.")

        for post in pending:
            lang_tag = "Nepali" if post.language == "nepali" else "English"
            tag = f"{lang_tag} / {post.region} / {post.category}"

            message_id = send_template_message(
                to_number=WHATSAPP_TO_NUMBER,
                template_name=TEMPLATE_NAME,
                body_params=[tag, post.social_summary[:1000]],
                button_payloads=[f"approve:{post.id}", f"reject:{post.id}"],
            )

            if message_id:
                post.whatsapp_message_id = message_id
                db.commit()
                print(f"  Sent Post id={post.id} (WhatsApp message id={message_id})")
            else:
                print(f"  Post id={post.id}: send failed, will retry next run")

    finally:
        db.close()


if __name__ == "__main__":
    main()
