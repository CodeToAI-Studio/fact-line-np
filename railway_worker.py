"""
railway_worker.py — run watch_pipeline / telegram_bot under Railway with a
real HTTP health endpoint.

Railway health-checks a service over HTTP on its injected PORT. These pipeline
scripts are long-running loops with no web server, so we give them a small
threaded HTTP server on that PORT returning 200 OK on `/`, `/health` and any
other path Railway probes. The worker loop runs in the main thread; the HTTP
server runs in a background thread, so a wedged loop never looks falsely
"healthy" (if the loop dies, the process exits and Railway restarts it).

Usage (set as the service's **Start Command**):
    python railway_worker.py watch      # run watch_pipeline.main()
    python railway_worker.py bot        # run telegram_bot.main()

Env:
    PORT (Railway-injected) — HTTP port to serve health on. Defaults to 8080.
"""
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from dotenv import load_dotenv

load_dotenv()  # no-op on Railway (no .env file); loads local .env during dev

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

    def log_message(self, *args):  # keep logs clean for the health endpoint
        pass


def _serve_health() -> None:
    """Serve 200 OK health responses from a background thread.

    Binds the configured PORT (Railway-injected for web-type services) and
    falls back to 80 (the default web port Railway probes) so the healthcheck
    passes whether or not Railway injects $PORT.
    """
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
    # passes Railway's healthcheck and stays alive even if a later step fails,
    # keeping the real error visible in logs instead of a silent crash-loop.
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