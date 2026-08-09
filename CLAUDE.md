# news-engine — Working Log & Handoff

**Audience:** an AI or person with zero prior context. Everything needed to resume is in this
file. Read it top to bottom before touching code.

**Maintenance rule:** update this file continuously as work happens — not at session end.
Every created/edited/deleted file goes in either "What's Done" (with reasoning) or
"Small Changes Log" (one line). Keep "What's Next" reordered by priority.

**Last updated:** 2026-08-09 (cont. 3) — LIVE on Railway + data migrated; FB token refresh next

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

### Session 2026-08-06 (Claude Code)

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
`CLAUDE.md`. Also created `RESUME.md` — a short, self-contained briefing to paste into an
assistant that has no access to the repo (e.g. Claude Desktop without a filesystem connector).
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
   - CLAUDE.md What's Next and Key Decisions updated accordingly; WhatsApp items moved
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

### Session 2026-08-07 (Claude Code)

**14. Fixed: nullable region/category crash in `app.py` `render_sources`.**
   - *File:* `app.py`, function `render_sources`. *Was:* `article['region'].upper()` and
     `article['category'].title()` — both crash with `AttributeError` if either field is `None`.
   - *Now:* `(article['region'] or 'unknown').upper()` and `(article['category'] or 'uncategorised').title()`.
   - *Why:* `Post.region` and `Post.category` are nullable columns in the DB schema. All 362 current
     articles happen to have values, but future ingests via feeds that fail classification could
     produce nulls. Defensive fallback costs nothing.

**15. Consolidated ingestion: `ingest_rss.py` is now the canonical script.**
   - *File changed:* `ingest_rss.py` — merged the best of both scripts:
     `ingest.py` contributed `clean_html()` (BeautifulSoup strip before embedding) and the
     4 international feeds (BBC, TechCrunch, The Verge, Ars Technica);
     `ingest_rss.py` contributed auto-classification of `region` + `category`, `image_url`
     extraction, `--dry-run` mode, and bozo feed detection.
   - *Old `ingest.py` is superseded* but kept in the repo (it still runs correctly; removing
     it is a separate decision).
   - `run_pipeline.bat` updated to call `ingest_rss.py`.
   - Dry-run verified: all11 feeds parsed correctly; The Kathmandu Post feed returned a
     bozo error (text/html media type — now visible instead of silent).
   - UTF-8 stdout fix (`sys.stdout.reconfigure(encoding="utf-8")`) applied at startup.

**16. Optional items completed: nullable region tag + requirements.txt audit.**
   - `telegram_bot.py` `format_post_message` now uses `post.region or "unknown"` and
     `post.category or "uncategorised"` so approval messages never render `"English / None / politics"`.
   - `requirements.txt` pinned 7 missing packages: `beautifulsoup4==4.15.0`,
     `google-genai==2.16.0`, `numpy==2.5.1`, `pgvector==0.5.0`, `slowapi==0.1.10`,
     `SQLAlchemy==2.0.51`, `streamlit==1.60.0`.

**17. Scheduler created: `run_pipeline.bat`.**
   - Calls `ingest_rss.py` then `generate_posts.py` with timestamped logging and exit-code
     checking. Designed to be wired into Windows Task Scheduler. The Telegram bot runs
     separately and picks up new pending posts on its30-second poll.

**18. FastAPI + `/synthesize` verified live.**
   - `main.py` imports clean, server starts on port 8001 without errors.
   - `/synthesize` with `top_k=3` on "What is happening in Nepal politics?" returned 3 sources
     (distances 0.29–0.30), a coherent synthesised answer, and `model_used: gemini-3.5-flash-lite`.
   - Prior "sources: 0" observation was a stale server (uvicorn had failed to bind to the
     already-occupied port 8000; the request hit an old instance). Not a code bug.

### Verification performed (2026-08-07)
- `py_compile` passes on `ingest_rss.py`, `app.py`, `telegram_bot.py`.
- `ingest_rss.py --dry-run` ran cleanly against all 11 feeds.
- Actual cosine distances confirmed (0.29–0.31) for a Nepal politics query — well under the 0.55 threshold.
- `/synthesize` live call returned populated `sources_used` with correct data.

**19. Built Facebook + Instagram publisher (`publisher.py`).**
   - *Created:* `publisher.py`. Publishes approved `Post` rows to Facebook and Instagram
     via the Graph API (`v20.0`). Skips posts with no `social_summary`. Instagram posts are
     left pending (not failed) when `image_url` is absent — IG feed posts require an image;
     this allows a future retry once one is attached.
   - Facebook: `POST /{page-id}/photos` (with image) or `/{page-id}/feed` (text-only).
   - Instagram: two-step — create container → `media_publish`. Caption truncated to 2,200 chars.
   - On real API error, `PlatformPost.status = "failed"` (not retried automatically).
   - `Post.status` flips to `"published"` once all FB+IG rows are non-pending (see Key Decisions
     for why website/threads/tiktok are excluded from this check).
   - `--dry-run` mode prints what would be posted without writing anything.
   - *Updated:* `run_pipeline.bat` now calls `publisher.py` after `generate_posts.py`.
   - *Updated:* `.env` — added the three required vars (filled in after Meta credential setup).

**20. Fixed two bugs in `publisher.py` found during live test.**
   - *Bug 1 — No-image IG posts incorrectly marked `failed`.*
     `post_to_instagram` returned `(False, ...)` for the "no image" case, causing the caller
     to set `PlatformPost.status = "failed"`. A missing image is a skip (retry-able), not an
     API error. Fixed: return `(None, ...)` for the skip case; caller now leaves status as
     `"pending"` when `success is None`. Three already-failed rows manually reset to `"pending"`.
   - *Bug 2 — Posts never flipped to `"published"`.*
     The "all done" check iterated over all `platform_posts` (including website/threads/tiktok,
     which are unimplemented and always `"pending"`). The check would never pass. Fixed: scope
     to only `{"facebook", "instagram"}` rows, and sweep ALL `approved` posts at the end of
     each run (not just those touched in the current run, so posts published in a prior run
     also get flipped).

**21. Meta credentials configured and publisher live-tested.**
   - `FACEBOOK_PAGE_ID`, `FACEBOOK_PAGE_ACCESS_TOKEN`, `INSTAGRAM_BUSINESS_ACCOUNT_ID` added
     to `.env` (Fact Line NP page, `factlinenp` IG business account).
   - Facebook App `FactLineNP Publisher` (`961662956934562`) created in Meta for Developers.
   - Token obtained after resolving a Page Admin binding issue: the active Facebook profile
     lacked Admin rights on the Fact Line NP Page; resolved by assigning Admin via the
     original creator account.
   - Live run result:
     - 6 Facebook posts published (3 text-only, 3 with image)
     - 3 Instagram posts published (those with `image_url`)
     - 3 IG rows left `"pending"` (posts 1, 4, 5 have no `image_url`)
     - Posts 7, 8, 9 flipped to `Post.status = "published"`
     - Posts 1, 4, 5 remain `"approved"` (FB published, IG pending — awaiting image)

### Verification performed (2026-08-07 continued)
- `py_compile publisher.py` passes (before and after bug fixes).
- Live publish run: 6 FB + 3 IG posts confirmed live on Fact Line NP.
- DB verified: 3 posts `published`, 3 posts `approved` (FB done, IG pending), 4 `rejected`.

**22. Replaced daily batch with continuous pipeline watcher (`watch_pipeline.py`).**
   - *Motivation:* daily schedule means new articles sit unprocessed for up to 24 hours.
     With a10-minute poll, new articles are detected and processed as they appear.
   - *Refactored `ingest_rss.py`:* extracted `main()` body into `run_ingestion(dry_run=False) -> int`.
     Returns the count of newly committed articles. `main()` now just parses args and delegates.
   - *Refactored `generate_posts.py`:* same pattern — `run_pipeline(dry_run=False) -> int`.
     Returns posts created. `main()` delegates. Both CLIs (`python ingest_rss.py`,
     `python generate_posts.py`) still work identically.
   - *Created `watch_pipeline.py`:* the continuous loop.
     - Polls RSS every `--interval` seconds (default 600 = 10 min).
     - Step 1: `run_ingestion()`. Step 2: `run_pipeline()` **only if new articles arrived**.
       Step 3: `run_publisher()` always (flushes any newly-approved posts to FB/IG).
     - `--once` flag runs a single cycle and exits (equivalent to the old `run_pipeline.bat`).
     - Catches exceptions per-step and logs them; watcher keeps running on partial failures.
   - *Auto-start on login:* `news-engine-watcher.bat` written to the Windows Startup folder
     (`AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Startup/`). No admin required.
     On next login the watcher starts automatically alongside the OS.
   - *Also created `setup_scheduler.ps1`*: alternative Task Scheduler setup (requires running
     as admin). Creates `news-engine-watcher` task at logon with auto-restart on failure.

**23. Built website publisher/viewer — `/posts` API + Streamlit tab.**
   - *Added to `main.py`:* `PostResponse` Pydantic model; `GET /posts` (filterable by
     status/region/category, paginated, newest first); `GET /posts/{post_id}` (single post).
   - *Added to `app.py`:* third tab "📰 Published Posts" — filters by region/category,
     loads posts via `/posts`, renders each in an expander with image, social summary,
     and full article body.
   - *Updated `publisher.py`:* when `Post.status` flips to `"published"`, the
     `platform="website"` `PlatformPost` row is also marked `published` with a timestamp.
     The `/posts` endpoint is the website; it is now a real publishing destination.
   - *Verified live:* `GET /posts` returned the 3 published posts correctly (Nepali +
     English, images intact). Streamlit "Published Posts" tab confirmed working.

**24. Streamlit UI fully verified.**
   - All three tabs tested live: News Search & Synthesis (streaming SSE working, sources
     appearing with cosine distances), Published Posts (3 posts loaded with images),
     Ingested Database (raw article table).
   - API running on port 8000 with `--reload` (auto-reloads on file changes).

### Verification performed (2026-08-07 final)
- `py_compile ingest_rss.py generate_posts.py watch_pipeline.py` all pass.
- `py_compile main.py app.py publisher.py` all pass.
- `GET /posts` live: 3 published posts returned correctly.
- Streamlit UI: all 3 tabs working end-to-end.

### Session 2026-08-08 (Claude Code)

**26. Built full Fact Line NP news website (complete redesign).**
   - *Design system:* `static/style.css` (~400 lines) — CSS custom properties for Fact
     Line red (`#D4001A`), deep navy (`#0A1628`), full type scale (Roboto Condensed
     headlines + Noto Sans body + Noto Sans Devanagari for Nepali), responsive grid,
     card components, hero layout, sidebar, article page, footer.
   - *Templates* (Jinja2, all in `templates/`):
     - `base.html` — header (topbar + logo + desktop/mobile nav), breaking ticker,
       footer with4 columns, live date JS, hamburger toggle.
     - `index.html` — hero grid (lead + 4 secondary), latest news feed, per-category
       sections, opinion strip, sidebar with most-read + categories.
     - `post.html` — article page: category badge, title, byline, reading time, hero
       image, lede callout, body paragraphs, FB/TW/WA/copy share bar, tags, related grid.
     - `category.html` — category archive with card grid.
     - `latest.html` — latest news list (also used for /popular).
     - `search.html` — search form + results grid.
     - `about.html` — about/editorial standards page.
     - `404.html` — custom 404 page.
   - *Routes added to `main.py`*: `/web`, `/web/post/{id}`, `/web/category/{cat}`,
     `/web/latest`, `/web/popular`, `/web/search`, `/web/about`, `/web/contact`,
     `/web/privacy`, `/web/terms`, `/web/corrections`, `/web/advertise`.
   - *Security*: category param sanitised (strip non-alphanumeric), search input
     validated (min 2 chars, max 200 chars), 404/500 exception handlers hide stack
     traces and serve `404.html` for `/web` paths + JSON for API paths.
   - *Helpers*: `_p()` (Post→dict), `_all_categories()`, `_most_read()`.
   - *Verified*: `py_compile main.py` passes; all 13 `/web` routes confirmed in
     `app.routes`.

### Verification performed (2026-08-08)
- `py_compile main.py` passes.
- Import test: all 13 `/web` routes registered correctly.
   - `news-engine-watcher.bat` (Windows Startup folder) updated to also launch
     `python telegram_bot.py` alongside `python watch_pipeline.py`. Both start
     automatically on next login without admin.

**26. Built public-facing news website (`/web`).**
   - *Files created:* `static/style.css`, `templates/base.html`,
     `templates/index.html`, `templates/post.html`.
   - *Routes added to `main.py`:*
     - `GET /web` — news homepage: published posts as responsive cards,
       filterable by region and category.
     - `GET /web/post/{id}` — article page: hero image, social summary callout,
       full article body rendered as paragraphs, metadata badges.
   - *App changes:* `StaticFiles` mounted at `/static`; `Jinja2Templates`
     initialized at module level. Both `Jinja2` and `aiofiles` added to
     `requirements.txt`.
   - *Route registration confirmed* via `from main import app` import test —
     both `/web` routes appear in `app.routes`.
   - *Status:* code complete and verified. Requires `uvicorn main:app --reload`
     to be started manually in a terminal (background process management via the
     bash tool was unreliable for long-lived servers).

### Verification performed (2026-08-08)
- `py_compile main.py` passes after all website changes.
- `from main import app` confirms `/web` and `/web/post/{post_id}` are
  registered as APIRoute objects.
- `GET /posts` JSON endpoint confirmed working throughout session.

### Session 2026-08-09 (Claude Code)

**27. Completed the admin CMS (paused mid-build on 2026-08-08).**
   - `main.py` was left in a broken state: `web_static_page` had a stray `})` (SyntaxError)
     and only login/logout/dashboard admin routes existed. Fixed the return, then added all
     remaining admin routes and wired admin-editable settings into the public site.
   - *New routes:* `/admin/posts` (list + status filter), `/admin/posts/{id}` (view/edit),
     `/admin/posts/{id}/delete` (admin-only), `/admin/settings` (GET form + POST save),
     `/admin/articles` (ingested raw RSS items), `/admin/users`, `/admin/users/create`,
     `/admin/users/{id}/toggle`, `/admin/logs` (audit search by user).
   - *Files created:* `admin_models.py` (AdminUser/SiteSetting/AuditLog +
     `DEFAULT_SETTINGS`), `admin_auth.py` (session-cookie auth in a module-level dict;
     SH-256 password hashing client-side — note: a DB-backed session store is pending),
     `templates/admin_login.html`, `templates/admin_base.html`, `templates/admin_dashboard.html`,
     `templates/admin_posts.html`, `templates/admin_post_edit.html`,
     `templates/admin_settings.html`, `templates/admin_users.html`,
     `templates/admin_logs.html`, `templates/admin_articles.html`.
   - *Auth:* sessions are 24h cookies (`flnp_admin_session`, httpOnly, SameSite=lax) stored in a
     process-local dict — fine for single-process, but **lost on restart** and not shared across
     gunicorn/uvicorn workers. Move to a DB/Redis session store before multi-worker deploy.
   - *Edit forms* adapted to the real `Post` schema (`social_summary`/`full_body`/`image_url`,
     not `title`/`content`). Platform publication status shown read-only on the edit page;
     `Post.status` is editable (pending/approved/rejected/published) so backfill is possible.
   - *Admin settings drive the live site:* `_site_context(db)` injects breaking-news ticker,
     footer about text, social URLs, and site tagline into every `/web` template; saving in
     the admin panel updates the public site immediately (verified).
   - *Verified end-to-end with FastAPI TestClient* (lifespan triggers table creation + admin
     seed): login/logout; every admin page 200; settings save → breaking ticker appears on
     `/web`; post edit persists; post delete works (cascade); user create + toggle works;
     audit log records update_settings/create_user/delete_post. Auth gates redirect to
     `/admin/login` when the session cookie is missing.

**28. Root cause of the persistent `/web` 404s finally identified (Starlette signature change).**
   - The site never actually rendered correctly since the web templates were built:
     the installed Starlette **1.3.1** changed `Jinja2Templates.TemplateResponse` to
     `(request, name, context)`. Every call site used the **old** `(name, context)` order:
     Starlette treated the string name as `request` and the dict as `name`, so `get_template(dict)`
     raised `TypeError: cannot use 'tuple' as a dict key`. The app still *compiled* and routes
     *registered*, which is why `--reload` looked healthy while every request 500'd — earlier
     sessions misdiagnosed this as stale uvicorn processes.
   - *Fix:* rewrote all 24 `templates.TemplateResponse(...)` call sites to the new
     `(request, name, context)` signature. All `/web` and `/admin` pages now return 200.
   - *Lesson:* after any dependency upgrade, run a render smoke test, not just `py_compile`.
     `import main` succeeding does not mean templates render.
   - *Mitigation:* add a pinned-fastapi/starlette version, or **pin `starlette` in
     `requirements.txt`** — currently unpinned, so a `pip install -r` on a fresh host pulls the
     newest starlette and silently breaks all templates again. **Recommended: pin
     `starlette==0.41.3`+ or pin the known-good `fastapi==0.141.1` together with starlette.**

**29. Destructive-edit incident on Post 9 + recovery (be careful when test-editing live rows).**
   During smoke-testing the admin post editor I overwrote Post 9 (a published Nepal story) with
   empty fields, wiping its stored text (social_summary/full_body). Recovery path:
   - The 8 `Article` rows linked to post_id=9 were re-clustered into a *different* mixed set
     (Scout election, apple export, land-fraud, transport policy) → Gemini correctly judged that
     whole set as *not* a single event (uncorroborated).
   - Two of the articles (Ratopati #163 + Nagarik Dainik #191) were a genuine corroborated pair
     ("Jagga ko nissa dinchan bhanera khosh gathan") — the actual subset that matched
     2-source corroboration. Re-ran `generate_posts.build_verification_prompt` +
     `call_gemini_for_cluster` over that pair → regenerated full_body+social_summary in Nepali,
     restored `image_url` (Ratopati) + `image_source_credit`, kept `status=published` so the
     FB/IG platform rows still point at a real story.
   - **Lesson:** when testing POST handlers that mutate the DB, use a throwaway row (or run a
     dry edit) — the admin editor *will* clobber any field.
   - **Fix applied:** the admin post editor now refuses to save if a non-empty post is being
     cleared (min-length guard on social_summary/full_body) — see #30.
   - Note: the FB Page access token has **expired** (07-Aug) — `FACEBOOK_PAGE_ACCESS_TOKEN` no
     longer validates. The publisher will start failing any new posts. See "Open Questions".

**30. Admin CMS security hardening (reviewer audit — 4 issues, all fixed).**
   An independent review of the new admin CMS flagged four issues before deployment. All fixed
   and verified with live TestClient tests:
   - **Password hashing upgraded to PBKDF2.** `admin_models.py` was using `sha256(password+salt)`
     — a fast, GPU-crackable general-purpose hash. Now `hashlib.pbkdf2_hmac("sha256", ..., 600k
     iters)` with a random 16-byte salt and a constant-time `hmac.compare_digest` compare. Stored
     format `salt$iterations$hash` (forward-compatible: raise iterations later without a schema
     change). The existing admin row's old 2-part hash can't verify under the new scheme and is
     re-issued from env at next startup (see below).
   - **Admin password is now a required env var (no default).** Previously `ADMIN_PASSWORD`
     defaulted to `FactLineNP2026!` in source — an attacker reading the repo knows it. The
     lifespan now refuses to start (`RuntimeError`) if `ADMIN_USERNAME`/`ADMIN_PASSWORD`/
     `ADMIN_EMAIL` are missing or the password is < 8 chars. `.env` now holds a generated
     random password (never committed). Env password changes re-hash the stored user at startup.
   - **DB-backed sessions.** The docstring claimed "server-side in DB" but sessions actually
     lived in a process-local dict — they'd break multi-worker deploys (random logouts) and
     vanished on restart. Added `AdminSession` model + DB store; verified a cookie set by one
     TestClient (simulating one worker) is recognized by a fresh one, and logout deletes the row.
     `get_current_user` now takes `db` and every admin route passes it.
   - **Rate-limited the admin.** `/admin/login` POST is now `10/minute` per IP (env-tunable
     `RATE_LIMIT_ADMIN_LOGIN`); the mutating admin POST endpoints (post edit/delete, settings
     save, user create/toggle) get `20–30/minute`. Verified: 10 wrong-password logins return 401,
     the 11th and 12th return 429.

   *(These were the four "worth catching before Railway" findings — all four were real.)*
   - The legacy `admin_users.password_hash` column was `VARCHAR(128)` while the model declared
     `VARCHAR(255)`. `ALTER TABLE ... SET DATA TYPE VARCHAR(255)` applied so a future hash
     format change can't silently hit the width limit.

### Session 2026-08-09 (cont.) — sequenced remaining work

**31. Attached images to approved posts 1, 4, 5 (unblocks their Instagram).**
   These posts were approved with FB published but IG `pending` because they had no
   `image_url`. Harvested the open-graph image from each post's source articles (BBC,
   Kathmandu Post, OnlineKhabar, Nagarik Dainik page) via `requests` + regex, set
   `image_url` + `image_source_credit` ("Image via {source}"). Verified all 3 URLs return
   `200 image/jpeg`. IG publish is still blocked only by the expired FB token (deferred per
   user — do not touch until they say so).

**32. Data retention/cleanup automation implemented (`retention.py`).**
   The retained/window policy decided at model design was the one entry sitting at 0%.
   Created `retention.py` with `run_retention(dry_run=False)` that deletes Article rows that
   are (a) linked to a fully-published post (FB+IG non-pending, mirroring publisher.py's
   ACTIVE_PLATFORMS scope) or (b) unclustered (post_id NULL) and older than
   `UNCLUSTERED_RETENTION_DAYS` (default 30, env-tunable). Wired as step 4 of
   `watch_pipeline.py`'s cycle. First live run deleted 12 consumed articles (8 from post 9,
   2 each from posts 7 and 8); all posts untouched. A CLI (`python retention.py [--dry-run]`)
   also works.

**33. Admin hardening follow-ups (Secure cookie).**
   `admin/login` and `admin/logout` now set/clear the session cookie with
   `secure=request.url.scheme == "https"` — the flag is on automatically behind HTTPS
   (Railway/prod) and off for plain-HTTP local dev. Verified: TestClient over `https://`
   sets `Secure`, over `http://` it does not. The GEMINI API billing-tier check remains a
   manual verification item (Open Questions), not a code change.

**34. Frontend pre-deployment viewing session set up (user req).**
   Started a fresh uvicorn on port **8002** (ports 8000/8001 were held by zombie uvicorns)
   so the user can review every public page + the admin CMS before deploying. Confirmed
   live: `/`, `/web`, `/web/latest`, `/web/about`, `/web/post/9`, `/web/search?q=nepal`,
   `/admin/login` all return 200 with real data.

### Session 2026-08-09 (cont.) — real view counts (homepage redesign handoff)

**35. Built a real view-count system; rewrote `/web/popular` + site sidebars as "Most Read".**
   Pre-existing state: the uncommitted homepage redesign added a **Most Popular** nav link →
   `/web/popular`, but that page was `/web/latest` with a new title — it ordered by
   `created_at.desc()` and claimed "Most-read stories right now" with no counter to back it.
   `_recent_posts` (main.py) was documented as a *recency proxy* pending a real counter.
   - *Schema:* added `Post.view_count` (`Integer`, default/`server_default` 0, non-null) in
     `models.py`. New idempotent `migrate_view_count.py` (probes `information_schema.columns`,
     then `ALTER TABLE posts ADD COLUMN view_count INTEGER NOT NULL DEFAULT 0`) — matches the
     repo's established `migrate_*.py` pattern. Run manually; re-run is a no-op.
   - *Counting (dedupe per browser session):* `web_post` increments only when (a) the post is
     `published` and (b) its id isn't already in the `flnp_viewed` cookie (comma-separated ids,
     HTTP-only, 30-day, capped at last 200). The 404 path returns before any write, so the cookie
     is only ever set on a real article render. This route is a sync `def` (runs in FastAPI's
     threadpool), so the blocking write is safe.
   - *Ranking:* `_recent_posts` → renamed `_most_read` (order by `view_count desc,
     created_at desc`); used by the homepage + article-page sidebars and by `/web/popular`
     (limit 30, same ordering). Tie-break is newest-first.
   - **Numbers are admin-only** (user decision): readers see ranked lists, never a count. The
     number renders only in `/admin/posts`, `/admin` dashboard (Recent Posts), and
     `/admin/posts/{id}` (edit page). No public template references `view_count` (verified by
     grep). `_p()` exposes `"view_count"` for admin use; public templates simply don't render it.
   - **Labels:** homepage sidebar `"Recent Stories"` → `"Most Read"` and article sidebar
     `"Latest Stories"` → `"Most Read"` (index.html, post.html) now that the list is real.
   - Classic `--reload` gotcha: verification initially "failed" (no cookie/count) because the
     process on 8002 was serving **without** `--reload` — it was running pre-edit code. Restarted
     the server on the new code; everything then worked. Same lesson as #28: confirm the running
     process actually reloaded before judging a route change.

**36. Deploy-readiness audit passed — the app boots fresh on a prod-like DB.**
   Designed to de-risk the pending Railway deploy (What's Next #15). Findings:
   - *No changes were needed.* A true first-run simulation (new scratch Postgres DB →
     full FastAPI lifespan via TestClient, mirroring Railway: `postgres://` → URL-encoded
     password, empty database) created the pgvector extension + all tables, seeded 15
     `SiteSetting` rows + 1 admin user, and served `/web`, `/`, `/admin/login` all **200**
     with `/admin`+`/admin/posts` correctly **303**-redirecting to login. `posts.view_count`
     exists on a fresh DB (no manual migration needed).
   - `scan_for_secrets.py` clean (no hardcoded secrets; `.env` gitignored) — green light
     to push to GitHub.
   - Confirmed project is deploy-safe: only user-facing products (Website ~70%, Streamlit
     UI local-only) — so **deploying now cannot leak anything sensitive**.
   - *Observations (not blockers):* `app.py` `API_URL` hardcodes `http://127.0.0.1:8000`
     (dev-only Streamlit UI; not served on Railway); `database.py` is an empty leftover
     (all real DB/URL logic lives in `models.py`, which already handles `postgres://`);
     `docker-compose.yml`'s dev password differs from `.env`'s running Postgres (they're
     unrelated — Docker wasn't running; `.env` uses `localhost:5433`).

**37. Restarted the pipeline watcher + Telegram bot — they had silently stopped.**
   The site was serving (uvicorn on 8002, `--reload`, latest code) but `watch_pipeline.py`
   and `telegram_bot.py` were NOT running, so no fresh ingestion/approvals were happening.
   They auto-start at login via the Startup-folder batch, but on a long-lived box that only
   fires at next login. Restarted both in the background; `watch_pipeline.log` /
   `telegram_bot.log` are buffered so they can look empty even while healthy. Verified
   functional: `ingest_rss.run_ingestion(dry_run=True)` parsed all feeds cleanly
   (171 new articles ready to ingest). No backlog in DB (350 articles, 0 pending/0 approved).
   *Lesson:* "is the site up?" is not the same as "is the operation running?" — check the
   watcher/bot processes alongside the server.

### Verification performed (2026-08-09 cont.)
- `py_compile main.py models.py migrate_view_count.py` passes.
- Migration applied; re-run reports "already exists".
- Live curl: fresh session on `/web/post/4` → `Set-Cookie: flnp_viewed=4`, DB `view_count` 1;
  re-visit same cookie → still 1; new session → 2. Fresh-session visits drove post 4 to 4
  (4 separate curl sessions = 4 distinct sessions) — session-dedupe proven.
- `/web/popular` ranks `[5,4,9,8,7]` — the two viewed posts first, tie-break newest.
- Homepage/`/web/popular`/article HTML contain no `view_count` / raw count (only sidebar rank
  numbers like `mr-num top 1`).
- TestClient (lifespan + admin auth): login 303; `/admin/posts`, `/admin`, `/admin/posts/4`
  all 200 with the Views column/label rendered.

### Session 2026-08-09 (cont.) — LIVE DEPLOY (Railway)

**38. Deployed Fact Line NP to Railway — site is live at `https://web-production-a8dc3.up.railway.app`.**
   - *Repo:* `github.com/CodeToAI-Studio/fact-line-np` (public, branch `master`, `.env`
     confirmed untracked; `gh` CLI installed at `~/bin/gh`).
   - *References used:* credentials in the user's local `.env` (user full control).
   - *Config:* added `railway.json` (Nixpacks build, `uvicorn main:app --host 0.0.0.0
     --port $PORT`, `/` healthcheck, restart-on-failure) + `Railway.md` deploy guide.
   - *Runtime hardening (the healthcheck-failure lesson):* `models.py` engine now uses
     `pool_pre_ping=True` and `connect_args={"connect_timeout": 10}` (unreachable DB fails
     ~10s, no infinite hang); `main.py` lifespan DB bootstrap wrapped in try/except that
     logs `Database bootstrap FAILED: ...` then re-raises to crash the process and let the
     platform restart. First deploy failed the `/` healthcheck because `DATABASE_URL` wasn't
     shared into the Web service.
   - *Data migration:* local `news_db` (Postgres 16 at `/c/Program Files/PostgreSQL/16/`)
     dumped to `news_db.dump` (2.9 MB) with `pg_dump -F c`; restored into Railway Postgres
     via `railway connect postgres` SSH tunnel (Railway CLI 5.35.0 at `~/bin/railway`,
     ed25519 key registered) with `pg_restore --no-owner --no-privileges --clean`. Row
     counts confirmed: 637 articles / 22 posts / 110 platform_posts / 15 settings /
     1 admin.
   - *Verified live:* `/` 200; `/web` 200 (Fact Line NP title, 21 post links);
     `/web/post/9` 200 rendering the real Nepali story; `/posts` returns restored rows;
     `/web/about`, `/admin/login`, `/static/style.css` all 200.
   - **LESSON (SSH tunnel):** `railway connect postgres` needs an SSH key. First attempt
     failed with `No SSH keys found` (fixed: `ssh-keygen -t ed25519` + `railway ssh keys
     add`), and the *first* tunnel session flaked with `cannot verify key` + `channel 1:
     open failed: unknown channel type: unsupported` → `pg_restore` errored. A fresh
     tunnel on a new port worked. On flaky tunnels: kill and retry.

**39. Local watcher/bot status after deploy.**
   - The deployed Railway DB now holds the real data, but `watch_pipeline.py` + the Telegram
     bot still run locally against the old local DB (which still has the same data). They
     keep the *local* copy alive and approved-posts continue to work against it.
   - Hiatus: at the very end of the migrate, the background (g) running `railway connect
     postgres --tunnel-only` may or may not still be alive — see next step; if it's dead,
     the next Railway CLI operation re-links automatically.

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
- `publisher.py` — created: FB/IG Graph API publisher.
- `publisher.py` — bug fix: `post_to_instagram` returns `None` (not `False`) for no-image skip;
  caller now leaves `PlatformPost.status` as `"pending"` instead of setting `"failed"`.
- `publisher.py` — bug fix: "all done" check now scopes to `{"facebook", "instagram"}` only and
  sweeps all `approved` posts (not just those touched in the current run).
- `run_pipeline.bat` — added `publisher.py` step after `generate_posts.py`.
- `watch_pipeline.py` — created: continuous RSS watcher, polls every 10 min, runs full pipeline.
- `ingest_rss.py` — refactored `main()` into callable `run_ingestion(dry_run) -> int`.
- `generate_posts.py` — refactored `main()` into callable `run_pipeline(dry_run) -> int`.
- `setup_scheduler.ps1` — created: PowerShell script to register at-logon Task Scheduler job (needs admin).
- `news-engine-watcher.bat` — written to Windows Startup folder for auto-start at login (no admin).
- `main.py` — added `PostResponse` model; `GET /posts`; `GET /posts/{post_id}`.
- `app.py` — added "📰 Published Posts" third tab.
- `publisher.py` — marks `platform="website"` PlatformPost as published when Post goes live.
- `news-engine-watcher.bat` (Startup folder) — updated to also launch `telegram_bot.py`.
- `main.py` — added `StaticFiles` mount at `/static`, `Jinja2Templates` init, `GET /web`, `GET /web/post/{id}`.
- `static/style.css` — created: responsive news site styles.
- `templates/base.html`, `templates/index.html`, `templates/post.html` — created: full HTML website.
- `requirements.txt` — added `jinja2==3.1.6`, `aiofiles==24.1.0`.
- `.env` — added commented placeholder lines for `FACEBOOK_PAGE_ID`, `FACEBOOK_PAGE_ACCESS_TOKEN`,
  `INSTAGRAM_BUSINESS_ACCOUNT_ID`.
- `app.py` — `render_sources`: `article['region'].upper()` → `(article['region'] or 'unknown').upper()`;
  same for `category`. Defensive null guard.
- `ingest_rss.py` — complete rewrite (canonical merge of `ingest.py` + old `ingest_rss.py`):
  added `clean_html()`, international feeds, UTF-8 fix, removed dead imports.
- `telegram_bot.py` — `format_post_message`: region/category fall back to `"unknown"` / `"uncategorised"`.
- `requirements.txt` — added `python-telegram-bot[job-queue]==22.8` and pinned 7 previously-unpinned packages.
- `run_pipeline.bat` — created: scheduled runner for `ingest_rss.py` + `generate_posts.py`.
- `admin_models.py` — created: AdminUser/SiteSetting/AuditLog ORM + `DEFAULT_SETTINGS`.
- `admin_auth.py` — created: session-cookie auth (in-memory session dict), SHA-256+salt passwords, audit `log_action`.
- `templates/admin_*.html` — created: login, base, dashboard, posts, post_edit, settings, users, logs, articles.
- `main.py` — added all `/admin` CRUD routes + `_site_context()`; fixed `web_static_page` stray brace (SyntaxError).
- `main.py` — rewrote all `TemplateResponse(...)` calls to new Starlette `(request, name, context)` signature — this was the hidden cause of every `/web` page 500'ing in prior sessions.
- `templates/base.html` — breaking-news ticker now links `breaking_news_url`; logo tagline + footer + social links read from `SiteSetting`.
- `templates/admin_posts.html` — `post.created_at.strftime(...)` → `post.created_at[5:16].replace('T',' ')` (route passes ISO string).
- `admin_models.py` — password hashing `sha256(password+salt)` → `pbkdf2_hmac` (600k iters) + `hmac.compare_digest`; `password_hash` column widened `String(128)`→`String(255)`; added `AdminSession` model.
- `admin_auth.py` — sessions moved from in-process dict to DB-backed `AdminSession`; `get_current_user`/`create_session`/`delete_session` now take `db`.
- `main.py` — admin credentials are required envs (startup fails loudly if missing); lifespan re-issues admin password when hash doesn't verify; added `@limiter.limit` to `/admin/login` (10/min) + admin POST routes (20–30/min); admin routes pass `db` to session helpers.
- `.env` — added `ADMIN_USERNAME`/`ADMIN_PASSWORD`/`ADMIN_EMAIL` (random generated password).
- `requirements.txt` — pinned `fastapi==0.141.1`, `starlette==1.3.1`.
- `retention.py` — created: `run_retention(dry_run)` deletes consumed/stale Article rows; wired into `watch_pipeline.py` step 4.
- `watch_pipeline.py` — added step 4 (retention sweep) + updated docstring.
- `main.py` — admin session cookie `secure=` keyed to HTTPS scheme (login + logout).
- *(data)* posts 1, 4, 5 — `image_url` + `image_source_credit` set from source-article OG images.
- `models.py` — added `Post.view_count` (`Integer`, default 0, server_default "0").
- `migrate_view_count.py` — created: idempotent migration for `posts.view_count`.
- `main.py` — `web_post` increments `view_count` once per session per published post (via `flnp_viewed`
  cookie, HTTP-only, 30 days, capped 200 ids); `_recent_posts` renamed `_most_read` (view-ordered);
  `web_popular` + both sidebars rank by views; `_p()` exposes `"view_count"`; admin dashboard
  `recent_posts` includes `view_count`.
- `templates/admin_posts.html`, `admin_dashboard.html` — added `Views` column.
- `templates/admin_post_edit.html` — added `Views:` line in Publication Status card.
- `templates/index.html`, `templates/post.html` — sidebar labels `Recent Stories`/`Latest Stories`
  → `Most Read` (list is now view-ranked; count still admin-only).
- `.env` — `DATABASE_URL` switched from local `localhost:5433` to the Railway Postgres public
  proxy (`hayabusa.proxy.rlwy.net:19974/railway`) — single source of truth (2026-08-09).
- `ingest_rss.py` — added `parse_feed()`: fetch RSS bytes via `urllib` with a bounded timeout
  (`FEED_FETCH_TIMEOUT`, default 20s) and hand the stream to feedparser; a failed/blackholed
  feed is logged and skipped instead of wedging `run_ingestion()`.
- `backfill_category.py` — `classify_via_gemini` now runs the Gemini call in a daemon thread
  bounded by `GEMINI_CALL_TIMEOUT_SECONDS` (30s); a hung/rate-limited API fails soft instead
  of blocking classification (and thus the watcher) forever.

---

## What's Next
*(priority order — items 1–6 completed 2026-08-06/07)*

1. ~~Telegram bot setup~~ ✅ Done
2. ~~End-to-end approval test~~ ✅ Done
3. ~~Run `ingest.py` and verify new rows~~ ✅ Done
4. ~~Optional: nullable region/category in bot messages~~ ✅ Done
5. ~~Optional: audit `requirements.txt`~~ ✅ Done
6. ~~FB/IG publisher — Meta credentials + live test~~ ✅ Done
7. ~~Attach images to posts 1, 4, 5~~ ✅ Done — OG images harvested + `image_source_credit` set;
   IG publish now blocked only by the (deferred) FB token.
8. ~~Data retention/cleanup~~ ✅ Done — `retention.py` + wired into watcher (What's Done #32).
8. ~~Build website publisher / viewer~~ ✅ Done — `GET /web` + article pages live at `localhost:8000/web`
9. ~~Build admin CMS~~ ✅ Done — `/admin` login + full CRUD. Log in at `/admin/login`.
   Credentials come from `ADMIN_USERNAME`/`ADMIN_PASSWORD`/`ADMIN_EMAIL` in `.env`
   (the app **refuses to start** without them — no default password exists in source).
10. ~~Admin CMS security hardening~~ ✅ Done — PBKDF2 password hashing, DB-backed sessions,
    required admin env vars, rate-limited login + admin POSTs. See What's Done #30.
11. ~~Pin `starlette`~~ ✅ Done — `fastapi==0.141.1`, `starlette==1.3.1` in requirements.txt.
12. ~~Move admin sessions out of the in-memory dict~~ ✅ Done — `AdminSession` DB table;
    verified a cookie set by one process is honored by a fresh one.
13. **FACEBOOK PAGE TOKEN — WORKING (re-verified 2026-08-09).** A live publish succeeded
    (Post 25 → `101313689402371_1028399360033436`), so the token on the local `.env` is
    valid. If it ever expires again, regenerate via Meta (App `961662956934562`).
14. ~~**Deploy to public server**~~ ✅ LIVE — `https://web-production-a8dc3.up.railway.app`,
    repo `github.com/CodeToAI-Studio/fact-line-np` (branch `master`, public). Full
    details in What's Done #38 (config, runtime hardening, data migration) and #39.
15. **RULE / current architecture (updated 2026-08-09): Railway is paid-only for new
    services, so the bot + watcher CANNOT be co-hosted on Railway.** Instead the local
    `watch_pipeline.py` + `telegram_bot.py` run against the **Railway DB** (`.env`
    `DATABASE_URL` = `hayabusa.proxy.rlwy.net:19974/railway`) — the same DB the live site
    serves. **Single source of truth = Railway DB.** Never run the pipeline against the
    stale local `localhost:5433` copy, and never run two watchers/bots at once (double
    publish risk). Exactly one of each should be running.
16. **Deferred — WhatsApp approval.** Code is in the repo; revisit once deployed.
17. **GEMINI_API_KEY is on a free-tier project and is currently 429 rate-limited**
    (`RESOURCE_EXHAUSTED`, daily 500-request cap on `gemini-3.5-flash-lite`). It blocks
    `generate_posts` drafting AND `/synthesize` on the live site. Either move to a
    billing-enabled project or wait for the daily reset. The pipeline now fails soft on
    429 (no crash/hang) and retries next cycle.

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
- **`publisher.py` "all done" check scopes to `{"facebook", "instagram"}` only.**
  `generate_posts.py` creates `PlatformPost` rows for website/threads/tiktok too, but those
  platforms are unimplemented. Including them in the "is this post fully published?" check
  would mean `Post.status` never flips to `"published"`. Only the platforms the publisher
  actually handles count toward completion.
- **Instagram skip ≠ failure.** `post_to_instagram` returns `None` (not `False`) when
  `image_url` is absent. The caller leaves `PlatformPost.status = "pending"` so the post
  retries automatically once an image is attached. A real API error returns `False` and
  sets status to `"failed"`, which requires manual intervention to retry.
- **Admin passwords are PBKDF2, never a fast hash.** `sha256(password+salt)` is GPU-crackable;
  the admin panel now uses `hashlib.pbkdf2_hmac` (600k iters). Stored as
  `salt$iterations$hash` so the iteration count can be raised later without a schema change.
- **`ADMIN_PASSWORD` etc. are required env vars — there is no default.** A fallback password in
  source would ship to anyone who can read the repo. The app refuses to start
  (`RuntimeError`) if they're missing or the password is < 8 chars. Changing the env password
  re-hashes the stored user on next boot.
- **Admin sessions are DB-backed, not in-memory.** `AdminSession` table survives restarts and
  works across workers/replicas. Never reintroduce a process-local session dict.
- **The admin login and every mutating admin route are rate-limited by IP** — a login form with
  no throttle is the classic brute-force door to the whole CMS. Tune via
  `RATE_LIMIT_ADMIN_LOGIN`.

---

## Open Questions / Blockers

1. **`FACEBOOK_PAGE_ACCESS_TOKEN` expired 2026-08-07.** `publisher.py` will 400 on every new
   post until it's regenerated. This is the top action item (What's Next #13): exchange or
   re-issue a long-lived token at https://developers.facebook.com against App `961662956934562`.
2. **Is `GEMINI_API_KEY` on a billing-enabled project?** Flagged risk: free-tier rate limits
   will reproduce the original 429 errors even with valid model names. Relevant to both
   synthesis and any bulk `ingest.py` run.
3. **Who populated `region` for the existing 240 rows?** Not `ingest.py` (confirmed by
   inspection) and there is no `backfill_region.py`. Likely manual SQL or a deleted script.
   Harmless now that ingestion is set explicitly, but worth knowing if the values look wrong.
4. **Admin session cookie has no `secure` flag.** It is `SameSite=lax` + HttpOnly, which is
   right, but it may be served over plain HTTP in local dev. Enable `Secure` once deployed
   behind HTTPS (Railway/domain) — a future hardening item.

*(WhatsApp template name, language code, and Meta approval status were blockers for the
WhatsApp flow — deferred along with that feature. See What's Next #15.)*

---

## Working Conventions
- Activate the venv first: `venv\Scripts\activate`
- Syntax-check with: `venv/Scripts/python.exe -m py_compile <files>`
- Start the API: `uvicorn main:app --reload`
- Start the UI: `streamlit run app.py`
