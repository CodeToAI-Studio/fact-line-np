# news-engine — Working Log & Handoff

**Audience:** an AI or person with zero prior context. Everything needed to resume is in this
file. Read it top to bottom before touching code.

**Maintenance rule:** update this file continuously as work happens — not at session end.
Every created/edited/deleted file goes in either "What's Done" (with reasoning) or
"Small Changes Log" (one line). Keep "What's Next" reordered by priority.

**Last updated:** 2026-08-06

---

## Project Overview

A Python RAG (Retrieval-Augmented Generation) news pipeline for Nepali + international news.

Flow: RSS feeds → `ingest.py` (fetch, clean, embed, store) → `generate_posts.py` (cluster
same-event articles across outlets, enforce a 2+ independent-source rule, ask Gemini to draft
a full article + a short social summary) → `Post` rows with `status="pending"` →
`whatsapp_bot.py` sends each for human approval over WhatsApp → the approver taps
Approve/Reject → `main.py`'s webhook flips `Post.status`. Separately, `main.py` serves a
retrieval + synthesis API consumed by a Streamlit UI (`app.py`).

### Stack
- **Backend:** FastAPI (`main.py`). Routes are `async def` but the DB layer is **synchronous**
  SQLAlchemy, so DB/embedding calls are wrapped in `run_in_threadpool`.
- **DB:** PostgreSQL + `pgvector`. Vector search via `Article.embedding.cosine_distance(...)`.
- **Embeddings:** Gemini `gemini-embedding-001` at 768 dimensions (`embeddings.py`).
- **LLM:** Google Gemini via the `google-genai` SDK.
- **Frontend:** Streamlit (`app.py`).
- **Approval channel:** WhatsApp Business Cloud API. `telegram_bot.py` is the earlier
  approval channel, still in the repo.

### File map
| File | Role |
|---|---|
| `main.py` | FastAPI app: `/articles`, `/query`, `/synthesize`, `/synthesize/stream`, WhatsApp webhook |
| `models.py` | SQLAlchemy models — `Article`, `Post`, `PlatformPost` |
| `app.py` | Streamlit frontend |
| `embeddings.py` | Shared embedding helper — **the only** place embeddings are computed |
| `llm_models.py` | Shared Gemini generation-model list + discovery fallback |
| `ingest.py` | RSS ingestion → `Article` rows |
| `search.py` | CLI vector search (debug tool) |
| `rag_chat.py` | CLI RAG question-answering (debug tool) |
| `generate_posts.py` | Clustering + verification + drafting → `Post` rows |
| `backfill_category.py` | One-off: classify existing articles into categories |
| `whatsapp_bot.py` | Outbound only — sends pending posts for approval |
| `whatsapp_client.py` | Shared WhatsApp Cloud API client |
| `main.py` webhook | Inbound — receives button taps (**not** in `whatsapp_bot.py`) |
| `migrate_*.py` | One-off schema migrations |
| `RESUME.md` | Self-contained prompt to paste into an AI chat with **no** repo access |
| `PROJECT_BRIEFING.md` | Superseded by this file; kept as a pointer |

---

## What's Done

### Earlier sessions (context, not verified line-by-line unless noted)
- **Gemini model migration.** `gemini-2.0-flash` and `gemini-2.0-flash-lite` were retired by
  Google on 2026-06-01 and now return 404. Replaced with `gemini-3.6-flash` (primary) and
  `gemini-3.5-flash-lite` (fallback). This was the root cause of an earlier "Error 3" 404 bug.
- **Dynamic model fallback.** If every hardcoded model name fails, call `client.models.list()`
  and retry against whatever is actually live, preferring flash-tier models.
- **Event-loop fix.** Synchronous `db.execute(...)` calls inside `async` FastAPI routes were
  blocking the event loop; wrapped in `run_in_threadpool`.
- **`model_used`** added to the synthesis response so you can see which model actually answered.
- **SSE streaming.** `/synthesize/stream` streams tokens over Server-Sent Events.
  `_stream_gemini_sync` (`main.py`) runs the SDK's blocking stream iterator on a background
  thread and hands chunks to the event loop through an `asyncio.Queue`. Streamlit consumes it
  via `stream_synthesis` (`app.py`). Both `/synthesize` and `/synthesize/stream` share
  `_retrieve_and_build_prompt` so their grounding logic can't drift apart.
- **Embedding migration.** Moved from local `sentence-transformers` (`all-MiniLM-L6-v2`,
  384-dim) to Gemini (768-dim). Motivation was avoiding `torch`, whose native DLLs were being
  blocked by Windows Smart App Control (see `embeddings.py` docstring). Vectors from two
  models are not convertible, so `migrate_switch_to_gemini_embeddings.py` dropped the column
  and re-embedded all rows from scratch.

### Session 2026-08-06 (Codex)

**1. Verified — the WhatsApp webhook in place is the correct version.** An earlier session
flagged that a tool-generated replacement had three defects. Confirmed the *good* version is
what's on disk: route is `@app.post("/webhooks/whatsapp")` (plural, must match Meta's console
exactly); `_verify_whatsapp_signature` returns `False` when either the signature header or
`WHATSAPP_APP_SECRET` is missing (fails **closed** — never treat a missing secret as verified);
and the handler actually parses the button payload, updates `Post.status` to
`"approved"`/`"rejected"`, and sends a confirmation reply. No changes needed.

**2. Corrected two stale claims in the prior handoff.** Both were listed as outstanding work
but were already done:
   - *`KeyError: 'region'`* — the previous diagnosis was that the backend never defined or
     populated `region`. That is wrong. `Article.region` exists in `models.py`
     (`default="international"`, indexed), `region: str` is declared on `ArticleResponse` in
     `main.py`, it's populated when building `ArticleSearchResult`, filtered
     case-insensitively in `_vector_search`, and consumed by `app.py`'s `render_sources`.
     Nothing to patch.
   - *SSE streaming* — listed as "deferred", but already implemented (see above).

**3. Fixed: WhatsApp template sends would have failed 100% of the time.**
   - *File:* `whatsapp_client.py`. *Added:* `import re`, module constant `_WHITESPACE_RUN`,
     and function `_sanitize_param(value)`. *Changed:* `send_template_message` now builds body
     parameters as `_sanitize_param(p)` instead of the raw value.
   - *Why:* WhatsApp rejects template body parameters containing newlines, tabs, or 4+
     consecutive spaces (error 132000, *"Parameter format does not match format in the created
     template"*), and rejects empty parameters. `whatsapp_bot.py` passes `post.social_summary`,
     which `generate_posts.py` explicitly prompts Gemini to produce as "a punchy 2-4 line
     summary" — i.e. it contains newlines — and which is an empty string when a story wasn't
     corroborated. Both failure modes were live.
   - `_sanitize_param` collapses all whitespace runs to single spaces and substitutes
     `"(no summary)"` when the result is blank.

**4. Fixed: `rag_chat.py` was never migrated off the retired models.**
   - *File:* `rag_chat.py`, function `generate_rag_response`. *Was:* a local
     `candidate_models = ["gemini-flash-latest", "gemini-2.0-flash", "gemini-2.0-flash-lite"]`.
     *Now:* `candidate_models = [*PRIMARY_MODELS, LATEST_FLASH_ALIAS]`.
   - *Why it hid:* the loop's exception handler does `continue` on any error containing "404",
     so the retirement was swallowed silently and execution always fell through to the final
     `"All candidate models exceeded rate limits"` message — pointing a debugger at quota when
     the real cause was deprecation.

**5. Deduplicated the model list into a new shared module.**
   - *Created:* `llm_models.py`, exporting `PRIMARY_MODELS`, `CHEAP_MODEL`,
     `LATEST_FLASH_ALIAS`, and `list_available_models(client)`.
   - *Changed:* `main.py` now imports `PRIMARY_MODELS, list_available_models` from it; its
     local `PRIMARY_MODELS` constant and its private `_list_available_models` function were
     deleted, and both call sites (the `/synthesize` slow path and the `/synthesize/stream`
     discovery retry) updated to the imported name. `generate_posts.py` imports
     `PRIMARY_MODELS` and its local copy was deleted. `backfill_category.py` now sets
     `GEMINI_MODEL = CHEAP_MODEL` instead of a hardcoded `"gemini-3.5-flash-lite"`.
     `rag_chat.py` imports `PRIMARY_MODELS, LATEST_FLASH_ALIAS`.
   - *Why:* the list was copy-pasted into four files. Three were updated during the model
     migration and `rag_chat.py` was missed — item 4 above is the direct consequence.

**6. Fixed: `ingest.py` and `search.py` were still using the deleted 384-dim embedder.**
   - *Files:* `ingest.py`, `search.py`, and the query side of `rag_chat.py`.
   - *Was:* all three built query/document vectors with
     `SentenceTransformer("all-MiniLM-L6-v2")` (384-dim) and compared them against the
     `vector(768)` column. **`ingest.py` is the pipeline's entry point, so every new article
     insert would have failed** on a pgvector dimension mismatch; `search.py` could not run at
     all.
   - *Now:* all three call `embeddings.get_embedding` — `task_type="RETRIEVAL_DOCUMENT"` in
     `ingest.py` (matching exactly what `migrate_switch_to_gemini_embeddings.py` used, so new
     rows land in the same vector space as re-embedded old ones) and
     `task_type="RETRIEVAL_QUERY"` in `search.py` and `rag_chat.py`.
   - *Side effects:* no project code imports `sentence_transformers` any more, which completes
     the original point of the embedding migration (dropping `torch`). Also,
     `sentence-transformers` was never listed in `requirements.txt`, so all three scripts
     would have failed on a fresh install regardless of the dimension issue.

**7. Fixed: `ingest.py` never set `region`, so the Nepal filter would silently rot.**
   - *File:* `ingest.py`. *Changed:* every entry in `RSS_FEEDS` gained a `"region"` key
     (`"nepal"` for OnlineKhabar English, The Kathmandu Post, The Himalayan Times;
     `"international"` for BBC News, TechCrunch, The Verge, Ars Technica).
     `run_ingestion` reads `feed_region = feed_info["region"]` and passes `region=feed_region`
     when constructing the `Article`.
   - *Why:* `Article.region` defaults to `"international"`, and `ingest.py` never set the field,
     so every newly ingested Nepali-source article would have been filed as international and
     disappeared from the "Nepal" filter in `app.py`. Verified against the live DB: the current
     240 rows are correctly split 180 `nepal` / 60 `international`, meaning they were populated
     by something outside this script. So this was not a visible error — it was gradual,
     silent data-quality decay affecting only new ingests.
   - Values are lowercase to match what's already stored (`main.py` filters case-insensitively
     either way).

**8. Documentation.** Created `PROJECT_BRIEFING.md` (now superseded by this file) and this
`AGENTS.md`. Also created `RESUME.md` — a short, self-contained briefing to paste into an
assistant that has no access to the repo (e.g. Codex Desktop without a filesystem connector).
It duplicates a small amount of context from this file by design: it has to stand alone. If
the blocker below is resolved or the hard rules change, update `RESUME.md` too.

### Verification performed this session (earlier)
- `python -m py_compile` passes on every touched file.
- `import llm_models`, `import ingest`, `import search` all succeed.
- Live DB query confirmed the region distribution quoted above (240 rows).

### Session 2026-08-06 continuation — Telegram + end-to-end pipeline test

**9. Switched approval channel from WhatsApp to Telegram (deferred WhatsApp).**
   - WhatsApp requires a Meta-approved template, a public webhook URL, and has
     a complex setup that isn't worth tackling until the project is proven useful.
   - `telegram_bot.py` was already in the repo (the earlier approval channel) and uses
     polling — no public URL or ngrok needed. Switched to it as the active channel.
   - AGENTS.md What's Next and Key Decisions updated accordingly; WhatsApp items moved
     to a "Deferred" entry.

**10. Fixed: `telegram_bot.py` crashed on startup — missing `job-queue` extra.**
   - *Error:* `AttributeError: 'NoneType' object has no attribute 'run_repeating'` because
     `python-telegram-bot` was installed without `APScheduler`.
   - *Fix:* `pip install "python-telegram-bot[job-queue]==22.8"`. `requirements.txt` updated
     to pin `python-telegram-bot[job-queue]==22.8`.

**11. Telegram bot credentials set up and tested.**
   - `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` added to `.env`.
   - Direct send test confirmed message delivery. All 7 previously-pending posts sent
     to Telegram manually (bot wasn't running when they were created). Bot then started
     and Approve/Reject button taps confirmed `Post.status` flips in the DB.

**12. Fixed: Windows cp1252 encoding crash in `ingest.py` and `generate_posts.py`.**
   - *Error:* `UnicodeEncodeError: 'charmap' codec can't encode character` — Windows
     console defaults to cp1252, which can't encode emoji (📦, 📡, ✅, ❌) or non-ASCII
     article titles printed to stdout.
   - *Fix:* added `import sys` + `sys.stdout.reconfigure(encoding='utf-8')` near the top
     of both files (after `load_dotenv()`).

**13. Full end-to-end pipeline verified live.**
   - `ingest.py` ran successfully: 122 new articles added, region split confirmed correct
     (240 nepal / 122 international — new Nepali-source rows tagged `"nepal"` as expected).
   - `generate_posts.py` ran against 340 unclustered articles, formed 174 clusters, 3 passed
     Gemini's corroboration check → 3 new `Post` rows created (all nepali/nepal).
   - Telegram bot delivered all 3 posts; 2 approved, 1 rejected via button taps.
     `Post.status` flipped correctly in DB for all 3.

### Verification performed (continuation)
- `ingest.py` live run: 122 rows inserted, region distribution verified in DB.
- `generate_posts.py` live run: 3 posts created, Gemini model used logged per post.
- Telegram approve/reject: DB status confirmed flipped for posts 8, 9, 10.

---

## Small Changes Log
- `whatsapp_client.py` — added `import re`.
- `rag_chat.py` — removed the three `os.environ[...]` HuggingFace warning-suppression lines
  (`HF_HUB_DISABLE_SYMLINKS_WARNING`, `HF_HUB_VERBOSITY`, `TOKENIZERS_PARALLELISM`); no longer
  needed once `sentence_transformers` was dropped.
- `rag_chat.py` — moved `load_dotenv()` above the imports that read env vars at import time.
- `ingest.py` — removed the same three HuggingFace `os.environ` lines, and removed
  `import os` entirely (it had no other use in the file).
- `ingest.py` — removed `from sentence_transformers import SentenceTransformer` and the
  module-level `embedding_model = SentenceTransformer(...)` initialization.
- `search.py` — added `from dotenv import load_dotenv` + `load_dotenv()`; the file previously
  had none and now needs it, since `embeddings.py` reads `GEMINI_API_KEY` at import time.
- `backfill_category.py` — `GEMINI_MODEL` is now assigned from `CHEAP_MODEL` rather than a
  string literal; trailing comment reworded.
- `embeddings.py` — docstring only: the list of consumers said `ingest_rss.py`, a file that
  doesn't exist. Corrected to `main.py`, `search.py`, `rag_chat.py`, `ingest.py`,
  `migrate_switch_to_gemini_embeddings.py`.
- `main.py` — deleted the now-redundant `# --- Model selection ---` comment block, replaced
  with a two-line pointer to `llm_models.py`.

---

## What's Next
*(priority order — items 1–3 completed 2026-08-06)*

1. ~~Telegram bot setup~~ ✅ Done
2. ~~End-to-end approval test~~ ✅ Done
3. ~~Run `ingest.py` and verify new rows~~ ✅ Done
4. Optional: `Post.region` is nullable, so the approval message tag can render as
   `"English / None / politics"`. Cosmetic only.
5. Optional: audit `requirements.txt` — it doesn't pin everything the code imports.
6. **Deferred — WhatsApp approval.** `whatsapp_bot.py` and `whatsapp_client.py` stay in the
   repo but are not the active approval channel. Revisit once the project is stable and
   successfully tested end-to-end. Remaining work: confirm exact template name/language code
   in WhatsApp Manager, confirm Meta approval status, wire up ngrok + webhook callback.

---

## Key Decisions

- **Model IDs live in `llm_models.py` and nowhere else.** Google retires Gemini IDs on a
  rolling basis; per-file copies already drifted once and produced a silent failure.
- **Do not adopt `gemini-2.5-flash`.** Scheduled for retirement 2026-10-16 and already failing
  early for some callers. Migrating to it would mean redoing this work within weeks.
  *(Provenance: retirement dates come from an earlier session's research; not independently
  re-verified on 2026-08-06.)*
- **Embedding config stays in `embeddings.py`, separate from `llm_models.py`.** These have
  very different change costs: swapping a generation model is free, swapping the embedding
  model invalidates the entire stored corpus and requires a full re-embed.
- **One embedding function, always.** Mixing embedding models is the failure this codebase has
  already hit twice. `get_embedding`'s `task_type` matters: `RETRIEVAL_DOCUMENT` for stored
  text, `RETRIEVAL_QUERY` for user queries — Gemini optimizes the two sides differently, and
  mismatching them degrades ranking *without* raising an error.
- **`LATEST_FLASH_ALIAS` (`gemini-flash-latest`) is a last-resort fallback, never a primary.**
  It can shift under you between runs, which makes behaviour non-reproducible.
- **Parameter sanitization belongs in `whatsapp_client.py`, not the callers.** It's a
  constraint of the WhatsApp API itself, so every caller needs it.
- **Region values are lowercase** (`"nepal"`, `"international"`) to match existing stored data.
- **WhatsApp webhook route is `/webhooks/whatsapp`** (plural) and signature verification
  **fails closed**. Do not "simplify" either — both have been broken before.
- **FastAPI routes are async, SQLAlchemy is sync.** Always wrap DB and embedding calls in
  `run_in_threadpool`. Never call `db.execute(...)` directly inside an async route.
- **`PROJECT_BRIEFING.md` was not deleted**, only reduced to a pointer — in case it's
  referenced externally.

---

## Open Questions / Blockers

1. **Is `GEMINI_API_KEY` on a billing-enabled project?** Flagged risk: free-tier rate limits
   will reproduce the original 429 errors even with valid model names. Relevant to both
   synthesis and any bulk `ingest.py` run.
2. **Who populated `region` for the existing 240 rows?** Not `ingest.py` (confirmed by
   inspection) and there is no `backfill_region.py`. Likely manual SQL or a deleted script.
   Harmless now that ingestion sets it explicitly, but worth knowing if the values look wrong.

*(WhatsApp template name, language code, and Meta approval status were blockers for the
WhatsApp flow — deferred along with that feature. See What's Next #6.)*

---

## Working Conventions
- Activate the venv first: `venv\Scripts\activate`
- Syntax-check with: `venv/Scripts/python.exe -m py_compile <files>`
- Start the API: `uvicorn main:app --reload`
- Start the UI: `streamlit run app.py`
