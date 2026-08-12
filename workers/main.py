"""
workers/main.py — a FastAPI app that RAILPACK will start with uvicorn, but whose
lifespan actually runs the worker (watcher or bot) in the background.

RAILPACK auto-detects a FastAPI `app` and force-starts `uvicorn main:app`. For a
background worker we don't want uvicorn to serve a web app — we want the pipeline
loop. This module gives RAILPACK exactly what it detects (a FastAPI app) but the
app's startup runs the real worker, and it also serves a health endpoint so
Railway's healthcheck passes.

Which worker runs is chosen by the WORKER_KIND env var ('watch' or 'bot',
default 'watch').
"""
import os
import sys
import threading

from dotenv import load_dotenv

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(1, _REPO_ROOT)

load_dotenv()

from fastapi import FastAPI  # noqa: E402

WORKER_KIND = os.getenv("WORKER_KIND", "watch").strip().lower()

app = FastAPI(title="Fact Line NP worker")

_worker_thread = None


@app.on_event("startup")
def _start_worker():
    """Run the requested worker in a daemon thread so uvicorn (and thus the
    container) stays alive and the healthcheck passes."""
    global _worker_thread

    def _run():
        try:
            from workers_launcher import run_worker  # relative to this folder
        except ImportError:
            # workers_launcher sits in the same folder as this module.
            import importlib
            run_worker = importlib.import_module("workers_launcher").run_worker
        run_worker(WORKER_KIND)

    if _worker_thread is None or not _worker_thread.is_alive():
        _worker_thread = threading.Thread(target=_run, daemon=True)
        _worker_thread.start()


@app.get("/health")
@app.get("/")
def health():
    return {"status": "ok", "worker": WORKER_KIND}
