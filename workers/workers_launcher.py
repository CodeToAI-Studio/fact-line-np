"""
workers/workers_launcher.py — run the watcher or bot worker.

Imported by workers/main.py (the FastAPI app RAILPACK starts). Reuses the same
logic as the repo-root railway_worker.py: env pre-checks, then run the target's
main() (which loops forever). The health HTTP server is provided by FastAPI
itself (main.py's /health), so we don't need a separate threaded server here.
"""
import os
import sys

from dotenv import load_dotenv

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(1, _REPO_ROOT)

load_dotenv()


def _require(*names: str) -> None:
    missing = [n for n in names if not (os.getenv(n) or "").strip()]
    if missing:
        raise RuntimeError(f"Missing required env var(s): {', '.join(missing)}")


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
        raise RuntimeError(f"Unknown worker kind {kind!r} (expected 'watch' or 'bot')")

    print(f"[workers_launcher] starting {kind} ...", flush=True)
    main()  # blocks forever (loops in the target script)
