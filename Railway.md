# Deploying Fact Line NP to Railway

Quick deploy guide. The repo already ships `railway.json` (`uvicorn main:app
--host 0.0.0.0 --port $PORT`), and the app creates the `vector` extension,
all tables, the site settings, and the admin user automatically in `lifespan`
on first boot — so an empty database works with zero manual schema steps.

## 1. Push the repo to GitHub (once)
```
git remote add origin https://github.com/<you>/<repo-name>.git
git push -u origin master
```
(Note: the current branch is `master`. Fine either way — just be consistent
when you attach your Railway Project to the repo.)

## 2. Create the Railway project
1. New Project → **Deploy from GitHub repo** → pick your repo.
2. Railway auto-builds (Nixpacks reads `railway.json`, installs
   `requirements.txt`, starts uvicorn).
3. **Add a PostgreSQL** service → Railway injects `DATABASE_URL` automatically
   (that's `postgres://` — the app converts it to `postgresql://`, so no edits
   needed).
4. Go to the Deploy service → **Variables** and add everything below.

> **SMOKE TEST:** watch the deploy log; the first boot must **fail loudly** if
> `ADMIN_*` aren't set yet (the app refuses to start without them). That's
> correct behaviour, not a bug — add the vars, and it starts. This is the
> same safety check that runs locally.

## 3. Required environment variables
| Variable | Value |
|---|---|
| `DATABASE_URL` | auto-injected by the Postgres service |
| `GEMINI_API_KEY` | from your local `.env` |
| `ADMIN_USERNAME` | your admin username (matches `.env`) |
| `ADMIN_PASSWORD` | a **new, strong** random password — do NOT reuse the local one |
| `ADMIN_EMAIL` | from `.env` |

The email/password only matter on first boot (they seed the admin user). If
you use a different password than the local DB's, that's fine — the deployed
site's admin panel is independent of your local `.env`.

## 4. Optional variables (set them if you want these features on the server)
| Variable | Purpose |
|---|---|
| `RATE_LIMIT_SYNTHESIS` | `/synthesize` calls-per-minute/IP (default `10/minute`) |
| `RATE_LIMIT_QUERY` | `/query` calls-per-minute/IP (default `30/minute`) |
| `RATE_LIMIT_ADMIN_LOGIN` | admin login attempts-per-minute/IP (default `10/minute`) |
| `WHATSAPP_*` / `FACEBOOK_*` / `INSTAGRAM_BUSINESS_ACCOUNT_ID` | pending platform features (website is the active destination) |

## 5. Verify
Open your `*.up.railway.app` URL — the news homepage should render with live
posts. Check `/web`, `/admin/login` (log in with the new creds), and the
health endpoint.

## 6. Data
The deployed Postgres starts **empty**. The local Postgres has the real
curated data (articles, posts, platform_posts, settings, admin user). To copy
it over, see the steps below — or just let the pipeline re-ingest from RSS on
the server.

### Copy local data → Railway (optional, keep live posts)
On your local machine with `pg_dump`/`psql`:
```bash
pg_dump -h localhost -p 5433 -U news_user -d news_db -F c -f news_db.dump
$env:DATABASE_URL = "<railway-db-url>"   # or set it for psql once
# restore to Railway (railway-internal URL works when run in a Railway shell)
pg_restore -d <railway-db-url> news_db.dump --no-owner --clean
```
> Tip: add a **temporary Service Variable / allow Railway to connect** or use
> the Railway CLI (`railway run` / `railway connect`) — the internal
> `postgres.railway.internal` URL is only reachable from inside Railway.
> Easiest: use **Railway's PostgreSQL → Connect tab** → "Connect CLI" command,
> then run `pg_restore`.

**Skip this step** and just press deploy if you'd rather start fresh; the
pipeline is very responsive — new articles stream in on the 10-minute
watcher cycle.

> ⚠️ The local **watcher/bot** still runs against the local DB. Once you point
> them at Railway's `DATABASE_URL`, they become extra workers. Leaving them to
> the old DB just keeps the local copy alive — that's fine if you ever want a
> fallback, but for a single source of truth, re-point everything at Railway
> after the deploy and stop the local watcher/bot.