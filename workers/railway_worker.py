"""
workers/railway_worker.py — Railway worker launcher for the news pipeline.

Runs the watcher (continuous pipeline) or bot (Telegram send-only) as a Railway
background service. Lives in a `workers/` subfolder so RAILPACK/Nixpacks, when
building this service, does NOT see the repo-root `main.py` (FastAPI app) and
does not auto-generate a `uvicorn --port $PORT` start command — the bug that
kept crashing the workers on Railway.

Imports the real modules from the parent directory (one level up), so the worker
uses the same code as the GH Actions / local path.
"""
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from dotenv import load_dotenv

# Make the parent dir importable (repo root has the real modules: watch_pipeline,
# telegram_bot, models, ...). Insert at position 1 so stdlib stays first.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(1, _REPO_ROOT)

load_dotenv()  # no-op on Railway (no .env); loads local .env during dev

PORT = int(os.getenv("PORT", "8080"))


def _require(*names: str) -> None:
    missing = [n for n in names if not (os.getenv(n) or "").strip()]
    if missing:
        print(
            f"[railway_worker] Missing required env var(s): {', '.join(missing)}",
            file=sys.stderr,
        )
        sys.exit(1)


class _Health(BaseHTTPRequestHandler):
    def _respond(self):
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        self._respond()

    def do_HEAD(self):
        self._respond()

    def log_message(self, *args):
        pass


def _serve_health() -> None:
    """Serve 200 OK health responses. Binds $PORT (if injected) and port 80 so
    Railway's healthcheck passes regardless of which port it probes."""
    bound_any = False
    for port in sorted({PORT, 80}):
        try:
            server = ThreadingHTTPServer(("0.0.0.0", port), _Health)
        except OSError as exc:
            print(f"[railway_worker] could not bind health server on :{port}: {exc}", file=sys.stderr)
            continue
        bound_any = True
        print(f"[railway_worker] health endpoint on http://0.0.0.0:{port}/", flush=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
    if not bound_any:
        print("[railway_worker] WARNING: no health server bound — Railway may fail its healthcheck", file=sys.stderr)


def run_worker(kind: str) -> None:
    # Health endpoint FIRST — before env checks / imports — so the container
    # passes Railway's healthcheck even if a later step fails.
    threading.Thread(target=_serve_health, daemon=True).start()

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
