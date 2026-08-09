"""
railway_worker.py — run watch_pipeline / telegram_bot under Railway as a
long-running worker without an HTTP dependency.

Railway background services monitor process liveness (container stay-alive /
exit code), NOT HTTP health pings. Set the service's **Health Check Path to
EMPTY** in the Railway dashboard for these two services; do not point it at a
hypothetical `/health` endpoint. These pipeline scripts are pure long-running
loops with no web server, so there is nothing to bind $PORT for, and adding a
fake HTTP listener here would only reintroduce a failure point (a wedged loop
that appears "healthy" or an HTTP thread that dies).

Usage (set as the service's **Start Command**):
    python railway_worker.py watch      # run watch_pipeline.main()
    python railway_worker.py bot        # run telegram_bot.main()

The wrapper pre-checks the required env vars and exits NON-ZERO (so Railway
logs a clear reason and restarts) instead of reporting a missing secret as a
successful run.
"""
import os
import sys

from dotenv import load_dotenv

load_dotenv()  # no-op on Railway (no .env file); loads local .env during dev


def _require(*names: str) -> None:
    missing = [n for n in names if not (os.getenv(n) or "").strip()]
    if missing:
        print(
            f"[railway_worker] Missing required env var(s): {', '.join(missing)}",
            file=sys.stderr,
        )
        sys.exit(1)


def run_worker(kind: str) -> None:
    if kind == "watch":
        _require(
            "DATABASE_URL",
            "GEMINI_API_KEY",
            "FACEBOOK_PAGE_ID",
            "FACEBOOK_PAGE_ACCESS_TOKEN",
            "INSTAGRAM_BUSINESS_ACCOUNT_ID",
        )
        from watch_pipeline import main
    elif kind == "bot":
        _require("DATABASE_URL", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
        from telegram_bot import main
    else:
        print(
            f"railway_worker: unknown worker kind {kind!r} (expected 'watch' or 'bot')",
            file=sys.stderr,
        )
        sys.exit(2)

    print(f"[railway_worker] starting {kind} ...", flush=True)
    main()  # blocks forever (loops in the target script)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python railway_worker.py <watch|bot>", file=sys.stderr)
        sys.exit(2)
    run_worker(sys.argv[1])