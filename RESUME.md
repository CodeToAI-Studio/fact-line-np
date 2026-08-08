# RESUME.md — paste this into any fresh AI chat

Self-contained. No attachments needed. Everything below the line can be copied as-is into
Claude Desktop (or any assistant) that has **no access to the project files**.

For an assistant that *can* read the repo (e.g. Claude Code in this directory), skip this and
just say: **"Read CLAUDE.md and continue."**

---

I'm resuming work on a personal Python project. You have no file access, so here's the full
context. Assume I've told you nothing else.

**Project:** a RAG news pipeline for Nepali + international news. RSS feeds are ingested and
embedded into Postgres/pgvector, similar articles are clustered across outlets, and stories
corroborated by 2+ independent sources are drafted by Gemini into a full article plus a short
social summary. Each draft becomes a `Post` with `status="pending"` and is sent to me over
WhatsApp with Approve/Reject quick-reply buttons; tapping one hits a webhook that flips the
status. There's also a FastAPI retrieval/synthesis API with a Streamlit frontend.

**Stack:** FastAPI (async routes, *synchronous* SQLAlchemy — all DB calls wrapped in
`run_in_threadpool`), PostgreSQL + pgvector, Gemini for both generation and embeddings,
Streamlit, WhatsApp Business Cloud API.

**Key files:** `main.py` (API + WhatsApp webhook), `models.py`, `app.py` (Streamlit),
`embeddings.py` (the only place embeddings are computed), `llm_models.py` (the only place
Gemini model IDs are declared), `ingest.py`, `generate_posts.py`, `whatsapp_bot.py`
(outbound sends only), `whatsapp_client.py` (shared WhatsApp API client).

**What currently works:** retrieval, synthesis (buffered and SSE-streamed), the Streamlit UI,
ingestion, and the full WhatsApp webhook path (signature verification, status update,
confirmation reply). 240 articles in the DB, split 180 nepal / 60 international.

**The one blocker:** `whatsapp_bot.py` has `TEMPLATE_NAME = "post_approval_request"` and
`whatsapp_client.py` hardcodes `"language": {"code": "en_US"}`. Both must match my approved
WhatsApp Manager template byte-for-byte, and I haven't confirmed either value yet. Note that
if the template was created as plain **English**, the code is `en`, not `en_US` — and that
mismatch fails with **error 132001**, *"template name does not exist in the translation"*,
which misleadingly looks like a wrong-*name* problem.

**Rules this codebase learned the hard way — please don't violate them:**
- Gemini model IDs go in `llm_models.py` only. Currently `gemini-3.6-flash` (primary) and
  `gemini-3.5-flash-lite` (fallback). `gemini-2.0-flash` / `-flash-lite` were retired
  2026-06-01 and 404. **Do not suggest `gemini-2.5-flash`** — it retires 2026-10-16.
- All embeddings go through `embeddings.get_embedding` (`gemini-embedding-001`, 768-dim).
  Use `task_type="RETRIEVAL_DOCUMENT"` for stored text, `"RETRIEVAL_QUERY"` for queries.
  Never introduce a second embedding model — mismatched vectors either fail on dimension or,
  worse, rank badly without erroring.
- The WhatsApp webhook route is `/webhooks/whatsapp` (plural) and its signature check must
  **fail closed** — a missing `WHATSAPP_APP_SECRET` means reject, never allow.
- Never call `db.execute(...)` directly inside an async route.

**What I want help with right now:** <replace this line with your actual question>

Because you can't write to my repo, give me complete file contents or exact find-and-replace
blocks I can apply myself — and if you make any decision worth remembering, summarise it at
the end so I can paste it back into my `CLAUDE.md` log.
