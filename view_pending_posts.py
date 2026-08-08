"""
view_pending_posts.py

Read-only peek at whatever's sitting in `posts` right now. Exists because
the Telegram approval bot isn't built yet -- this is the only way to
actually see what generate_posts.py has drafted.

USAGE
-----
    python view_pending_posts.py
"""

from dotenv import load_dotenv
load_dotenv()

from models import Post, SessionLocal


def main():
    db = SessionLocal()
    try:
        posts = db.query(Post).order_by(Post.id).all()
        if not posts:
            print("No posts yet.")
            return

        for post in posts:
            print("=" * 70)
            print(f"Post id={post.id}  status={post.status}  language={post.language}  region={post.region}  category={post.category}")
            print(f"Image: {post.image_url or '(none)'}")
            if post.image_source_credit:
                print(f"Image credit: {post.image_source_credit}")
            print(f"\nSOCIAL SUMMARY:\n{post.social_summary}")
            print(f"\nFULL ARTICLE:\n{post.full_body}")
            print()

    finally:
        db.close()


if __name__ == "__main__":
    main()
