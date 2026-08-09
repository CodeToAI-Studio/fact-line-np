"""
railway_worker.py — run watch_pipeline / telegram_bot under Railway as a robust
long-running worker.

Railway kills processes that don't bind its `PORT` (the healthcheck has
nowhere to report healthy). These pipeline scripts are pure long-running
loops with no HTTP endpoint, so we start the target loop AND a tiny HTTP
server on the injected PORT that always returns 200. That keeps Railway's
healthcheck satisfied while the worker runs forever.

Usage:
    python railway_worker.py watch      # run watch_pipeline.py
    python railway_worker.py bot        # run telegram_bot.py

Env:
    PORT (Railway-injected) — the health port. Falls back to 8080 if unset
    actually rely on (it is the sheet).

Never import this file's module-level socket server as a dependency — it is
a thin launcher meant to be called directly.
"""
import os
import sys
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.getenv("PORT", "8080"))


class _Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):  # silence request logging
        pass


def _serve_health():
    try:
        srv = ThreadingHTTPServer(("0.0.0.0", PORT), _Health)
        srv.serve_forever()
    except Exception as exc:
        print(f"[railway_worker] health server error: {exc}", flush=True)


def run_worker(module_name: str, func_name: str):
    import importlib

    mod = importlib.import_module(module_name)
    target = getattr(mod, func_name)

    # Start the health HTTP server in a daemon thread so it never blocks exit.
    t = threading.Thread(target=_serve_health, daemon=True)
    t.start()
    print(f"[railway_worker] health server on :{PORT}", flush=True)

    # Run the target's main() in the main thread (forever).
    target()