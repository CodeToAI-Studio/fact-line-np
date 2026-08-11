"""
watch_pipeline.py

Continuously watches RSS feeds and runs the full pipeline the moment new
articles arrive. Meant to run as a long-lived background process alongside
telegram_bot.py.

Loop logic:
  1. Poll all RSS feeds (ingest_rss.run_ingestion).
  2. If new articles arrived → cluster + verify + draft posts
     (generate_posts.run_pipeline).
  3. Always run the publisher at the end of each cycle to push any
     newly-approved posts to FB + IG (publisher.run_publisher).
  4. Run the retention sweep (retention.run_retention) to delete consumed /
     stale Article rows, unapproved/rejected posts, and admin clutter.
  5. Sleep POLL_INTERVAL seconds, then repeat.

Steps 2 and 3 are skipped only if step 1 added nothing AND there are approved
pending posts (the publisher exits immediately in that case).

USAGE
-----
    python watch_pipeline.py              # default 10-minute poll interval
    python watch_pipeline.py --interval 5 # poll every 5 minutes
    python watch_pipeline.py --once       # single run then exit (same as run_pipeline.bat)
"""
import sys
import os
import argparse
import time
from datetime import datetime

from dotenv import load_dotenv
load_dotenv()

sys.stdout.reconfigure(encoding="utf-8")

from ingest_rss import run_ingestion
from generate_posts import run_pipeline
from publisher import run_publisher
from retention import run_retention

DEFAULT_POLL_INTERVAL = 600  # 10 minutes


def _timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def run_once():
    """Run one full ingest → generate → publish cycle."""
    print(f"\n{'='*60}")
    print(f"[{_timestamp()}] Pipeline cycle start")
    print(f"{'='*60}\n")

    # Step 1 — ingest
    print(f"[{_timestamp()}] --- Ingesting RSS feeds ---")
    try:
        new_articles = run_ingestion()
    except Exception as e:
        print(f"[{_timestamp()}] ERROR in ingest_rss: {e}")
        new_articles = 0

    # Step 2 — generate posts (only if new articles arrived)
    if new_articles > 0:
        print(f"\n[{_timestamp()}] --- {new_articles} new article(s) → running generate_posts ---")
        try:
            run_pipeline()
        except Exception as e:
            print(f"[{_timestamp()}] ERROR in generate_posts: {e}")
    else:
        print(f"\n[{_timestamp()}] No new articles — skipping generate_posts.")

    # Step 3 — publish approved posts
    print(f"\n[{_timestamp()}] --- Running publisher ---")
    try:
        run_publisher()
    except Exception as e:
        print(f"[{_timestamp()}] ERROR in publisher: {e}")

    # Step 4 — retention sweep (delete consumed/stale articles + stale
    # pending/rejected posts + expired sessions + old audit logs)
    print(f"\n[{_timestamp()}] --- Running retention sweep ---")
    try:
        run_retention()
    except Exception as e:
        print(f"[{_timestamp()}] ERROR in retention: {e}")

    # Step 5 — daily DB backup (once per day, tracked by a state file)
    _maybe_daily_backup()

    print(f"\n[{_timestamp()}] Pipeline cycle complete.")


def _maybe_daily_backup():
    """Run backup_db once per day. Uses a small state file so we don't dump
    the whole DB on every 10-minute cycle."""
    state_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".last_backup")
    from datetime import date
    today = date.today().isoformat()
    try:
        if os.path.exists(state_path):
            with open(state_path) as f:
                if f.read().strip() == today:
                    return  # already backed up today
        print(f"\n[{_timestamp()}] --- Daily DB backup ---")
        # Import lazily so watch_pipeline works even if backup deps are missing.
        import backup_db
        backup_db.run_backup()
        with open(state_path, "w") as f:
            f.write(today)
    except Exception as e:
        print(f"[{_timestamp()}] ERROR in daily backup: {e}")


def main():
    parser = argparse.ArgumentParser(
        description="Continuously poll RSS feeds and run the pipeline when new articles arrive."
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_POLL_INTERVAL,
        metavar="SECONDS",
        help=f"Seconds between RSS polls (default: {DEFAULT_POLL_INTERVAL})",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single cycle and exit instead of looping.",
    )
    args = parser.parse_args()

    if args.once:
        run_once()
        return

    print(f"[{_timestamp()}] Watcher started — polling every {args.interval}s. Ctrl+C to stop.")
    while True:
        try:
            run_once()
        except KeyboardInterrupt:
            print(f"\n[{_timestamp()}] Stopped by user.")
            break
        except Exception as e:
            # Unexpected error — log and keep running rather than crashing the watcher.
            print(f"[{_timestamp()}] UNEXPECTED ERROR: {e}")

        print(f"\n[{_timestamp()}] Sleeping {args.interval}s until next poll...\n")
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            print(f"\n[{_timestamp()}] Stopped by user.")
            break


if __name__ == "__main__":
    main()
