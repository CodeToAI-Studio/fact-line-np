import os
import json
import asyncio
import threading
import logging
import hmac
import hashlib
from contextlib import asynccontextmanager
from typing import List, Optional, AsyncGenerator

from dotenv import load_dotenv
load_dotenv()

from google import genai
from google.genai.errors import APIError
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, func, text
from sqlalchemy.orm import Session
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from models import Article, Post, Base, SessionLocal, engine
from admin_models import AdminUser, SiteSetting, AuditLog, DEFAULT_SETTINGS
from admin_auth import (
    SESSION_COOKIE_NAME, create_session, delete_session,
    get_current_user, authenticate_user, create_admin_user, log_action
)
from embeddings import get_embedding
from llm_models import PRIMARY_MODELS, list_available_models
import gemini_keys
from whatsapp_client import send_text_message

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
WHATSAPP_WEBHOOK_VERIFY_TOKEN = os.getenv("WHATSAPP_WEBHOOK_VERIFY_TOKEN")
WHATSAPP_APP_SECRET = os.getenv("WHATSAPP_APP_SECRET")  # same App Secret already used for Instagram -- same parent Meta app

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("factlinenp")

# --- Rate limiting -----------------------------------------------------------
# No login/auth exists anywhere in this project, so this isn't about
# protecting an auth flow -- it's about protecting the endpoints that spend
# real Gemini API money per call (/synthesize, /synthesize/stream) from
# being hammered once this has a public IP. /query and /articles are cheap
# DB reads, so they get a looser limit. All thresholds are env-configurable,
# not hardcoded, so they can be tuned per-deployment without a code change.
RATE_LIMIT_SYNTHESIS = os.getenv("RATE_LIMIT_SYNTHESIS", "10/minute")
RATE_LIMIT_QUERY = os.getenv("RATE_LIMIT_QUERY", "30/minute")
# Login is the brute-force target (the door to editing the whole site); tighten
# it. This mirrors the existing per-IP model used for the Gemini endpoints.
RATE_LIMIT_ADMIN_LOGIN = os.getenv("RATE_LIMIT_ADMIN_LOGIN", "10/minute")

limiter = Limiter(key_func=get_remote_address)

# --- Model selection -------------------------------------------------------
# PRIMARY_MODELS and list_available_models() live in llm_models.py so every
# entry point shares one list — see the note there on why.

llm_client = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global llm_client
    # Enable pgvector extension before table creation (required on fresh databases
    # including Railway PostgreSQL — safe to re-run, IF NOT EXISTS is idempotent).
    # Boot is deliberately fail-fast: if the DB is unreachable or the migration
    # fails, raising here crashes the process so the platform (Railway) logs the
    # reason and restarts once the DB comes up — instead of the app starting with
    # a silently dead database and timing out the healthcheck.
    try:
        with engine.connect() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            conn.commit()
        Base.metadata.create_all(bind=engine)
    except Exception as exc:
        logger.error("Database bootstrap FAILED: %s", exc)
        raise

    if GEMINI_API_KEY or gemini_keys.key_count():
        llm_client = gemini_keys.get_client()

    # Seed default site settings and admin user on first run.
    # Admin credentials are REQUIRED environment variables — there is no
    # default. A hardcoded fallback password would ship with the source code
    # and be trivially known to any attacker reading the repo.
    admin_u = os.getenv("ADMIN_USERNAME", "").strip()
    admin_p = os.getenv("ADMIN_PASSWORD", "").strip()
    admin_e = os.getenv("ADMIN_EMAIL", "").strip()
    if not (admin_u and admin_p and admin_e):
        raise RuntimeError(
            "ADMIN_USERNAME, ADMIN_PASSWORD and ADMIN_EMAIL must be set in the "
            "environment. Refusing to start with a default admin password."
        )
    if len(admin_p) < 8:
        raise RuntimeError("ADMIN_PASSWORD must be at least 8 characters long.")

    db = SessionLocal()
    try:
        for key, (value, value_type, desc) in DEFAULT_SETTINGS.items():
            if not db.get(SiteSetting, key):
                db.add(SiteSetting(key=key, value=value, value_type=value_type, description=desc))

        admin_user = db.query(AdminUser).filter(AdminUser.username == admin_u).first()
        if not admin_user:
            u = AdminUser(username=admin_u, email=admin_e, role="admin", is_active=True)
            u.set_password(admin_p)
            db.add(u)
            logger.info("Created admin user: %s", admin_u)
        elif not admin_user.check_password(admin_p):
            # Exists but hash doesn't verify (e.g. migrated from the old
            # unsalted sha256 scheme, or the env password changed). Re-issue
            # the hash so the configured password works again.
            admin_user.set_password(admin_p)
            admin_user.email = admin_e
            admin_user.role = admin_user.role or "admin"
            logger.warning("Admin user %s password re-issued from env.", admin_u)
        db.commit()
    finally:
        db.close()

    yield


app = FastAPI(
    title="News RAG Engine API",
    description="Vector search & synthesis API over news articles.",
    version="1.2.0",
    lifespan=lifespan,
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# --- Pydantic Schemas --------------------------------------------------------
class ArticleResponse(BaseModel):
    id: int
    title: str
    source: str
    url: str
    content: str
    region: str
    category: str
    model_config = ConfigDict(from_attributes=True)


class ArticleSearchResult(ArticleResponse):
    distance: float


class QueryRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=500)
    top_k: Optional[int] = Field(default=5, ge=1, le=20)
    region: Optional[str] = None
    category: Optional[str] = None


class SynthesisRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=500)
    top_k: Optional[int] = Field(default=5, ge=1, le=20)
    max_distance_threshold: Optional[float] = Field(default=0.55, ge=0.0, le=2.0)
    region: Optional[str] = None
    category: Optional[str] = None


class QueryResponse(BaseModel):
    query: str
    results_count: int
    articles: List[ArticleSearchResult]


class SynthesisResponse(BaseModel):
    query: str
    answer: str
    sources_used: List[ArticleSearchResult]
    model_used: Optional[str] = None


class PostResponse(BaseModel):
    id: int
    full_body: str
    social_summary: str
    language: Optional[str] = None
    region: Optional[str] = None
    category: Optional[str] = None
    image_url: Optional[str] = None
    image_source_credit: Optional[str] = None
    status: str
    created_at: Optional[str] = None   # ISO string; datetime not JSON-serialisable by default
    model_config = ConfigDict(from_attributes=True)


@app.get("/")
def read_root():
    return {"status": "online", "message": "News RAG Engine API is running."}


@app.get("/articles", response_model=List[ArticleResponse])
def list_articles(
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    articles = db.execute(select(Article).offset(skip).limit(limit)).scalars().all()
    return articles


@app.get("/categories", response_model=List[str])
def list_categories(db: Session = Depends(get_db)):
    """Distinct category values currently in the DB, so the frontend can
    populate a dropdown without hardcoding a taxonomy it doesn't know."""
    rows = db.execute(select(Article.category).distinct().order_by(Article.category)).scalars().all()
    return [r for r in rows if r]


def _vector_search(
    db: Session,
    query_vector: list,
    top_k: int,
    region: Optional[str] = None,
    category: Optional[str] = None,
):
    """Synchronous helper — run inside run_in_threadpool so it never blocks
    the event loop."""
    stmt = select(
        Article,
        Article.embedding.cosine_distance(query_vector).label("distance"),
    )
    if region:
        # Case-insensitive so "Nepal" from the UI matches a lowercase
        # "nepal"/"international" value stored in the DB.
        stmt = stmt.where(func.lower(Article.region) == region.lower())
    if category:
        stmt = stmt.where(func.lower(Article.category) == category.lower())
    stmt = stmt.order_by("distance").limit(top_k)
    return db.execute(stmt).all()


@app.post("/query", response_model=QueryResponse)
@limiter.limit(RATE_LIMIT_QUERY)
async def query_articles(request: Request, payload: QueryRequest, db: Session = Depends(get_db)):
    user_query = payload.query.strip()
    if not user_query:
        raise HTTPException(status_code=400, detail="Query string cannot be empty.")

    query_vector = await run_in_threadpool(
        lambda: get_embedding(user_query, task_type="RETRIEVAL_QUERY")
    )

    results = await run_in_threadpool(
        _vector_search, db, query_vector, payload.top_k, payload.region, payload.category
    )

    search_results = [
        ArticleSearchResult(
            id=article.id,
            title=article.title,
            source=article.source,
            url=article.url,
            content=article.content,
            region=article.region,
            category=article.category,
            distance=float(distance),
        )
        for article, distance in results
    ]

    return QueryResponse(
        query=user_query,
        results_count=len(search_results),
        articles=search_results,
    )


async def _retrieve_and_build_prompt(payload: SynthesisRequest, db: Session):
    """Shared retrieval + prompt-construction logic used by both the
    blocking /synthesize endpoint and the streaming /synthesize/stream
    endpoint, so they can't silently drift apart from each other.
    Returns (user_query, filtered_sources, prompt). prompt is None when
    no sources cleared the distance threshold — callers should treat that
    as the "no relevant articles" case rather than calling the LLM."""
    user_query = payload.query.strip()
    if not user_query:
        raise HTTPException(status_code=400, detail="Query string cannot be empty.")

    query_vector = await run_in_threadpool(
        lambda: get_embedding(user_query, task_type="RETRIEVAL_QUERY")
    )

    results = await run_in_threadpool(
        _vector_search, db, query_vector, payload.top_k, payload.region, payload.category
    )

    filtered_sources = [
        ArticleSearchResult(
            id=article.id,
            title=article.title,
            source=article.source,
            url=article.url,
            content=article.content,
            region=article.region,
            category=article.category,
            distance=float(distance),
        )
        for article, distance in results
        if float(distance) <= payload.max_distance_threshold
    ]

    if not filtered_sources:
        return user_query, filtered_sources, None

    context_text = "\n\n".join(
        [
            f"--- Article {i+1} ---\nTitle: {art.title}\nSource: {art.source}\nContent: {art.content}"
            for i, art in enumerate(filtered_sources)
        ]
    )

    prompt = f"""You are a news synthesis assistant. Use ONLY the provided context below to answer the user's question.
If the context does not contain enough information to answer, state clearly that you do not have sufficient information. Do not make up facts.

Context:
{context_text}

User Question: {user_query}

Answer:"""

    return user_query, filtered_sources, prompt


@app.post("/synthesize", response_model=SynthesisResponse)
@limiter.limit(RATE_LIMIT_SYNTHESIS)
async def synthesize_answer(request: Request, payload: SynthesisRequest, db: Session = Depends(get_db)):
    if not llm_client:
        raise HTTPException(
            status_code=500, detail="GEMINI_API_KEY environment variable is unconfigured."
        )

    user_query, filtered_sources, prompt = await _retrieve_and_build_prompt(payload, db)

    if prompt is None:
        return SynthesisResponse(
            query=user_query,
            answer="No sufficiently relevant articles were found to answer this query accurately.",
            sources_used=[],
        )

    answer_text = None
    model_used = None
    last_error = None

    # Fast path: try the known-good models first.
    for model_name in PRIMARY_MODELS:
        try:
            def _call(mn=model_name):
                gemini_keys.pace()
                return llm_client.models.generate_content(model=mn, contents=prompt)
            response = await run_in_threadpool(_call)
            if response and response.text:
                answer_text = response.text
                model_used = model_name
                break
        except APIError as e:
            last_error = e
            if gemini_keys.is_rate_limit(e):
                gemini_keys.rotate()
                llm_client = gemini_keys.get_client()
                logger.warning("Gemini rate-limited, rotated to key ...%s", gemini_keys.current_key()[-6:])
            continue
        except Exception as e:
            last_error = e
            continue

    # Slow path: the hardcoded names above are stale (Google retired them) —
    # discover what's actually live and retry once against the first hit.
    if answer_text is None:
        discovered = await run_in_threadpool(list_available_models, llm_client)
        # Prefer flash-tier models for latency/cost; fall back to anything.
        discovered_ordered = sorted(
            discovered, key=lambda n: ("flash" not in n, n)
        )
        for model_name in discovered_ordered:
            if model_name in PRIMARY_MODELS:
                continue  # already tried
            try:
                def _call(mn=model_name):
                    gemini_keys.pace()
                    return llm_client.models.generate_content(model=mn, contents=prompt)
                response = await run_in_threadpool(_call)
                if response and response.text:
                    answer_text = response.text
                    model_used = model_name
                    break
            except Exception as e:
                last_error = e
                if gemini_keys.is_rate_limit(e):
                    gemini_keys.rotate()
                    llm_client = gemini_keys.get_client()
                    logger.warning("Gemini rate-limited, rotated to key ...%s", gemini_keys.current_key()[-6:])
                continue

    if answer_text is None:
        logger.error("Gemini generation failed for query %r: %s", user_query, last_error)
        raise HTTPException(
            status_code=502,
            detail="Synthesis is temporarily unavailable. Please try again shortly.",
        )

    return SynthesisResponse(
        query=user_query,
        answer=answer_text,
        sources_used=filtered_sources,
        model_used=model_used,
    )


# --- Streaming (SSE) ---------------------------------------------------------
def _sse(event: str, data: dict) -> str:
    """Format one Server-Sent Event frame."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _stream_gemini_sync(model_name: str, prompt: str, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue):
    """Runs in a background thread — google-genai's streaming call
    (generate_content_stream) is a blocking/synchronous iterator, so it
    can't be awaited directly without stalling FastAPI's event loop. This
    pulls chunks off it and hands them to the event loop via the queue."""
    try:
        gemini_keys.pace()
        stream = llm_client.models.generate_content_stream(model=model_name, contents=prompt)
        for chunk in stream:
            text = getattr(chunk, "text", None)
            if text:
                loop.call_soon_threadsafe(queue.put_nowait, ("chunk", text))
    except Exception as e:
        loop.call_soon_threadsafe(queue.put_nowait, ("error", e))
    finally:
        loop.call_soon_threadsafe(queue.put_nowait, ("done", None))


async def _stream_from_model(model_name: str, prompt: str) -> AsyncGenerator[str, None]:
    """Async generator wrapping _stream_gemini_sync. Yields text chunks as
    they arrive; raises whatever exception the background thread hit once
    all buffered chunks are drained."""
    loop = asyncio.get_event_loop()
    queue: asyncio.Queue = asyncio.Queue()

    thread = threading.Thread(
        target=_stream_gemini_sync, args=(model_name, prompt, loop, queue), daemon=True
    )
    thread.start()

    while True:
        kind, item = await queue.get()
        if kind == "chunk":
            yield item
        elif kind == "error":
            raise item
        elif kind == "done":
            break


@app.post("/synthesize/stream")
@limiter.limit(RATE_LIMIT_SYNTHESIS)
async def synthesize_stream(request: Request, payload: SynthesisRequest, db: Session = Depends(get_db)):
    """Same retrieval + grounding as /synthesize, but streams the answer
    token-by-token over Server-Sent Events instead of waiting for the full
    Gemini response. Event types sent, in order:
      - "sources": {"sources_used": [...]}  (sent once, immediately)
      - "token":   {"text": "..."}           (repeated, as text arrives)
      - "done":    {"model_used": "..."}     (sent once, stream complete)
      - "error":   {"detail": "..."}         (sent instead of "done" on failure)
    """
    if not llm_client:
        raise HTTPException(
            status_code=500, detail="GEMINI_API_KEY environment variable is unconfigured."
        )

    user_query, filtered_sources, prompt = await _retrieve_and_build_prompt(payload, db)

    async def event_generator():
        yield _sse("sources", {"sources_used": [s.model_dump() for s in filtered_sources]})

        if prompt is None:
            yield _sse("token", {"text": "No sufficiently relevant articles were found to answer this query accurately."})
            yield _sse("done", {"model_used": None})
            return

        last_error = None
        models_to_try = list(PRIMARY_MODELS)
        tried_discovery = False

        while True:
            for model_name in models_to_try:
                started = False
                try:
                    async for text_chunk in _stream_from_model(model_name, prompt):
                        started = True
                        yield _sse("token", {"text": text_chunk})
                    yield _sse("done", {"model_used": model_name})
                    return
                except Exception as e:
                    last_error = e
                    if started:
                        # Already streamed partial text under this model —
                        # switching models now would just splice two
                        # different answers together. Stop and surface it.
                        logger.error("Gemini streaming failed mid-response for query %r: %s", user_query, e)
                        yield _sse("error", {"detail": "Synthesis was interrupted. Please try again."})
                        return
                    continue  # nothing sent yet, safe to try the next model

            if tried_discovery:
                break
            tried_discovery = True
            discovered = await run_in_threadpool(list_available_models, llm_client)
            models_to_try = [
                m for m in sorted(discovered, key=lambda n: ("flash" not in n, n))
                if m not in PRIMARY_MODELS
            ]
            if not models_to_try:
                break

        logger.error("Gemini streaming failed for query %r after trying all models: %s", user_query, last_error)
        yield _sse(
            "error",
            {"detail": "Synthesis is temporarily unavailable. Please try again shortly."},
        )

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --- WhatsApp webhook ---------------------------------------------------------
# Unlike Telegram, WhatsApp's Cloud API only works via webhooks -- Meta
# pushes events here, there's no polling alternative. This needs to be
# publicly reachable (via ngrok locally, the real domain once deployed).

@app.get("/webhooks/whatsapp")
async def whatsapp_webhook_verify(request: Request):
    """Meta's one-time handshake when you save the webhook URL in the
    console: echoes back hub.challenge if the verify token matches."""
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token == WHATSAPP_WEBHOOK_VERIFY_TOKEN:
        return PlainTextResponse(challenge or "")

    logger.warning("WhatsApp webhook verification failed (bad token or mode)")
    raise HTTPException(status_code=403, detail="Verification failed")


def _verify_whatsapp_signature(payload_body: bytes, signature_header: Optional[str]) -> bool:
    """Confirms a webhook POST actually came from Meta, not someone who
    found the public ngrok URL. Uses the same App Secret as Instagram --
    same parent Meta app, one secret for the whole app."""
    if not signature_header or not WHATSAPP_APP_SECRET:
        return False
    expected = "sha256=" + hmac.new(
        WHATSAPP_APP_SECRET.encode(), payload_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)


@app.post("/webhooks/whatsapp")
async def whatsapp_webhook_receive(request: Request, db: Session = Depends(get_db)):
    body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")

    if not _verify_whatsapp_signature(body, signature):
        logger.warning("WhatsApp webhook signature verification failed -- rejecting")
        raise HTTPException(status_code=403, detail="Invalid signature")

    try:
        payload = json.loads(body)
        entry = payload["entry"][0]
        change = entry["changes"][0]
        messages = change["value"].get("messages", [])

        for msg in messages:
            if msg.get("type") != "button":
                continue  # not a quick-reply button tap -- ignore (e.g. a plain text reply)

            button_payload = msg.get("button", {}).get("payload", "")
            sender = msg.get("from")

            if ":" not in button_payload:
                logger.warning("Malformed WhatsApp button payload: %r", button_payload)
                continue

            action, post_id_str = button_payload.split(":", 1)
            try:
                post_id = int(post_id_str)
            except ValueError:
                logger.warning("Non-integer post id in WhatsApp button payload: %r", button_payload)
                continue

            post = db.query(Post).filter(Post.id == post_id).first()
            if not post:
                logger.warning("WhatsApp button tap for unknown post id=%s", post_id)
                continue
            if post.status != "pending":
                continue  # already actioned -- ignore a duplicate/late tap

            post.status = "approved" if action == "approve" else "rejected"
            db.commit()
            logger.info("Post id=%s -> %s via WhatsApp", post.id, post.status)

            if sender:
                outcome = "✅ Approved" if action == "approve" else "❌ Rejected"
                send_text_message(sender, f"{outcome} — Post ID {post.id}")

    except (KeyError, IndexError, json.JSONDecodeError) as e:
        # Malformed/unexpected payload shape -- log it, but still return 200
        # below. Meta disables webhooks that repeatedly return errors.
        logger.error("Malformed WhatsApp webhook payload: %s", e)

    return {"status": "ok"}


# --- Published posts (website) -----------------------------------------------

@app.get("/posts", response_model=List[PostResponse])
def list_posts(
    status: str = Query("published", description="Filter by status: published, approved, pending"),
    region: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """Return posts for the website. Defaults to published posts, newest first.
    The website frontend (or a future CMS) consumes this endpoint."""
    stmt = select(Post).where(Post.status == status)
    if region:
        stmt = stmt.where(func.lower(Post.region) == region.lower())
    if category:
        stmt = stmt.where(func.lower(Post.category) == category.lower())
    stmt = stmt.order_by(Post.created_at.desc()).offset(skip).limit(limit)
    posts = db.execute(stmt).scalars().all()
    return [
        PostResponse(
            id=p.id,
            full_body=p.full_body,
            social_summary=p.social_summary,
            language=p.language,
            region=p.region,
            category=p.category,
            image_url=p.image_url,
            image_source_credit=p.image_source_credit,
            status=p.status,
            created_at=p.created_at.isoformat() if p.created_at else None,
        )
        for p in posts
    ]


@app.get("/posts/{post_id}", response_model=PostResponse)
def get_post(post_id: int, db: Session = Depends(get_db)):
    """Return a single post by ID."""
    post = db.get(Post, post_id)
    if not post:
        raise HTTPException(status_code=404, detail=f"Post {post_id} not found")
    return PostResponse(
        id=post.id,
        full_body=post.full_body,
        social_summary=post.social_summary,
        language=post.language,
        region=post.region,
        category=post.category,
        image_url=post.image_url,
        image_source_credit=post.image_source_credit,
        status=post.status,
        created_at=post.created_at.isoformat() if post.created_at else None,
    )


# --- Website HTML pages -------------------------------------------------------
# Routes at /web serve the public-facing Fact Line NP news website (Jinja2).
# Security: inputs sanitised, no stack traces exposed to visitors.

def _p(post):
    """Post ORM row -> plain dict safe for template rendering."""
    return {
        "id": post.id,
        "social_summary": post.social_summary or "",
        "full_body": post.full_body or "",
        "language": post.language,
        "region": post.region,
        "category": post.category,
        "image_url": post.image_url,
        "image_source_credit": post.image_source_credit,
        "status": post.status,
        "created_at": post.created_at.isoformat() if post.created_at else None,
        "view_count": getattr(post, "view_count", 0) or 0,
    }


def _all_categories(db):
    rows = db.execute(
        select(Post.category).where(Post.status == "published")
        .distinct().order_by(Post.category)
    ).scalars().all()
    return [c for c in rows if c]


# Category display order for the homepage. Unknown/other categories fall back to
# alphabetical order after these. "opinion" is deliberately excluded here — it is
# surfaced as its own strip rather than a card row.
CATEGORY_ORDER = [
    "politics", "economy", "business", "society",
    "world", "sports", "technology", "entertainment", "nepal",
]


def _most_read(db, limit: int = 5):
    """The 5 published posts with the most page views for the "Most Read"
    sidebars. Numbers are admin-only; readers just see the ranked list."""
    rows = db.execute(
        select(Post).where(Post.status == "published")
        .order_by(Post.view_count.desc(), Post.created_at.desc()).limit(limit)
    ).scalars().all()
    return [_p(p) for p in rows]


def _site_context(db) -> dict:
    """Admin-editable site settings (breaking ticker, social links, tagline,
    ad slots) merged into a flat dict for every /web template. Undefined
    settings fall back to the default Fact Line NP values."""
    rows = db.query(SiteSetting).all()
    s = {r.key: r.value for r in rows}

    def _g(key, default):
        val = s.get(key)
        return val if val else default

    return {
        "site_title": _g("site_title", "Fact Line NP"),
        "site_tagline": _g("site_tagline", "Nepal News. Verified. Explained."),
        "breaking_news": _g("breaking_news_text", ""),
        "breaking_news_url": _g("breaking_news_url", ""),
        "footer_about": _g("footer_about", "Fact Line NP delivers verified, contextualized news from Nepal and around the world."),
        "contact_email": _g("contact_email", "contact@factlinenp.com"),
        "facebook_url": _g("facebook_url", "https://facebook.com/factlinenp"),
        "instagram_url": _g("instagram_url", "https://instagram.com/factlinenp"),
        "youtube_url": _g("youtube_url", "https://youtube.com/@factlinenp"),
        "twitter_url": _g("twitter_url", "https://twitter.com/factlinenp"),
        "ad_header_code": _g("ad_header_code", ""),
        "ad_sidebar_code": _g("ad_sidebar_code", ""),
        "ad_article_code": _g("ad_article_code", ""),
    }


@app.get("/web", include_in_schema=False)
def web_index(request: Request, db: Session = Depends(get_db)):
    all_posts = db.execute(
        select(Post).where(Post.status == "published")
        .order_by(Post.created_at.desc()).limit(30)
    ).scalars().all()
    post_dicts = [_p(p) for p in all_posts]

    # Hero: 1 dominant lead (newest with an image) + up to 3 stacked secondaries.
    hero_lead = next((p for p in post_dicts if p["image_url"]), post_dicts[0] if post_dicts else None)
    remaining = [p for p in post_dicts if p is not hero_lead]
    hero_subs = [p for p in remaining if p["image_url"]][:3]

    # Category rows in the editorial order; 3-4 cards per row, narrower grid if sparse.
    cat_sections = []
    seen_cats = set()
    for cat in CATEGORY_ORDER:
        if cat.lower() == "opinion":
            continue
        rows = db.execute(
            select(Post).where(Post.status == "published",
                               func.lower(Post.category) == cat.lower())
            .order_by(Post.created_at.desc()).limit(4)
        ).scalars().all()
        if not rows:
            continue
        seen_cats.add(cat.lower())
        cat_sections.append({
            "name": cat, "slug": cat.lower(),
            "posts": [_p(r) for r in rows],
            "display": "card-grid-4" if len(rows) >= 3 else "card-grid-2",
        })
    # Any categories the pipeline produced that aren't in CATEGORY_ORDER.
    for cat in _all_categories(db):
        if cat.lower() == "opinion" or cat.lower() in seen_cats:
            continue
        rows = db.execute(
            select(Post).where(Post.status == "published",
                               func.lower(Post.category) == cat.lower())
            .order_by(Post.created_at.desc()).limit(4)
        ).scalars().all()
        if rows:
            seen_cats.add(cat.lower())
            cat_sections.append({
                "name": cat, "slug": cat.lower(),
                "posts": [_p(r) for r in rows],
                "display": "card-grid-4" if len(rows) >= 3 else "card-grid-2",
            })

    op_rows = db.execute(
        select(Post).where(Post.status == "published",
                           func.lower(Post.category) == "opinion")
        .order_by(Post.created_at.desc()).limit(3)
    ).scalars().all()

    ctx = {"request": request, "current_page": "home",
           "hero_lead": hero_lead, "hero_subs": hero_subs,
           "latest_posts": post_dicts[:10],
           "most_read": _most_read(db),
           "category_sections": cat_sections,
           "opinion_posts": [_p(r) for r in op_rows],
           "all_categories": _all_categories(db)}
    ctx.update(_site_context(db))
    return templates.TemplateResponse(request, "index.html", ctx)




@app.get("/web/post/{post_id}", include_in_schema=False)
def web_post(request: Request, post_id: int, db: Session = Depends(get_db)):
    post = db.get(Post, post_id)
    if not post or post.status not in ("published", "approved"):
        return templates.TemplateResponse(request, "404.html", {"request": request}, status_code=404)
    words = len((post.full_body or "").split())
    related_rows = db.execute(
        select(Post).where(Post.status == "published", Post.id != post_id,
                           func.lower(Post.category) == (post.category or "").lower())
        .order_by(Post.created_at.desc()).limit(3)
    ).scalars().all()
    ctx = {"request": request, "post": _p(post),
           "related": [_p(r) for r in related_rows],
           "most_read": _most_read(db),
           "reading_time": max(1, round(words / 200))}
    ctx.update(_site_context(db))
    resp = templates.TemplateResponse(request, "post.html", ctx)

    # Count a view once per browser session per published post. The flnp_viewed
    # cookie holds comma-separated post ids already seen, so refreshes and bots
    # within one session don't inflate the number. The count is admin-only; it
    # drives /web/popular and the "Most Read" sidebars but is never rendered.
    viewed = request.cookies.get("flnp_viewed", "")
    seen_ids = viewed.split(",") if viewed else []
    if post.status == "published" and str(post_id) not in seen_ids:
        post.view_count = (post.view_count or 0) + 1
        db.commit()
        seen_ids.append(str(post_id))
        seen_ids = seen_ids[-200:]  # cap the cookie so it can't grow without bound
        resp.set_cookie("flnp_viewed", ",".join(seen_ids),
                        max_age=60 * 60 * 24 * 30, httponly=True)
    return resp


@app.get("/web/category/{category}", include_in_schema=False)
def web_category(request: Request, category: str, db: Session = Depends(get_db)):
    import re as _re
    safe_cat = _re.sub(r"[^a-zA-Z0-9\-]", "", category)[:50]
    rows = db.execute(
        select(Post).where(Post.status == "published",
                           func.lower(Post.category) == safe_cat.lower())
        .order_by(Post.created_at.desc()).limit(40)
    ).scalars().all()
    ctx = {"request": request, "current_cat": safe_cat.lower(),
            "category": safe_cat, "posts": [_p(r) for r in rows],
            "all_categories": _all_categories(db)}
    ctx.update(_site_context(db))
    return templates.TemplateResponse(request, "category.html", ctx)


@app.get("/web/latest", include_in_schema=False)
def web_latest(request: Request, db: Session = Depends(get_db)):
    rows = db.execute(
        select(Post).where(Post.status == "published")
        .order_by(Post.created_at.desc()).limit(50)
    ).scalars().all()
    ctx = {"request": request, "current_page": "latest",
            "page_title": "Latest News", "page_sub": "Fresh from the newsroom",
            "posts": [_p(r) for r in rows], "all_categories": _all_categories(db)}
    ctx.update(_site_context(db))
    return templates.TemplateResponse(request, "latest.html", ctx)


@app.get("/web/popular", include_in_schema=False)
def web_popular(request: Request, db: Session = Depends(get_db)):
    rows = db.execute(
        select(Post).where(Post.status == "published")
        .order_by(Post.view_count.desc(), Post.created_at.desc()).limit(30)
    ).scalars().all()
    ctx = {"request": request, "current_page": "popular",
            "page_title": "Most Popular", "page_sub": "Most-read stories right now",
            "posts": [_p(r) for r in rows], "all_categories": _all_categories(db)}
    ctx.update(_site_context(db))
    return templates.TemplateResponse(request, "latest.html", ctx)


@app.get("/web/search", include_in_schema=False)
def web_search(request: Request, q: Optional[str] = None, db: Session = Depends(get_db)):
    query = (q or "").strip()[:200]
    results = []
    if len(query) >= 2:
        pattern = f"%{query}%"
        rows = db.execute(
            select(Post).where(
                Post.status == "published",
                Post.social_summary.ilike(pattern) | Post.full_body.ilike(pattern)
            ).order_by(Post.created_at.desc()).limit(30)
        ).scalars().all()
        results = [_p(r) for r in rows]
    ctx = {"request": request, "query": query,
            "results": results, "all_categories": _all_categories(db)}
    ctx.update(_site_context(db))
    return templates.TemplateResponse(request, "search.html", ctx)


@app.get("/web/about", include_in_schema=False)
@app.get("/web/contact", include_in_schema=False)
@app.get("/web/privacy", include_in_schema=False)
@app.get("/web/terms", include_in_schema=False)
@app.get("/web/advertise", include_in_schema=False)
@app.get("/web/corrections", include_in_schema=False)
def web_static_page(request: Request, db: Session = Depends(get_db)):
    import os as _os
    page = request.url.path.rstrip("/").split("/")[-1]
    tpl = f"{page}.html"
    if not _os.path.exists(f"templates/{tpl}"):
        tpl = "about.html"
    ctx = {"request": request, "all_categories": _all_categories(db)}
    ctx.update(_site_context(db))
    return templates.TemplateResponse(request, tpl, ctx)


# ── Admin helpers ──────────────────────────────────────────────────────────────
from fastapi.responses import JSONResponse, RedirectResponse

def _admin_redirect_login():
    return RedirectResponse(url="/admin/login", status_code=303)

def _get_settings(db) -> dict:
    rows = db.query(SiteSetting).all()
    return {r.key: r.value for r in rows}

def _save_setting(db, key: str, value: str, user: dict):
    row = db.get(SiteSetting, key)
    if row:
        row.value = value
        row.updated_by = user["username"]
    else:
        db.add(SiteSetting(key=key, value=value, updated_by=user["username"]))


# ── Admin: Login / Logout ─────────────────────────────────────────────────────
@app.get("/admin/login", include_in_schema=False)
def admin_login_page(request: Request, db: Session = Depends(get_db)):
    if get_current_user(request, db):
        return RedirectResponse(url="/admin", status_code=303)
    return templates.TemplateResponse(request, "admin_login.html", {"request": request})

@app.post("/admin/login", include_in_schema=False)
@limiter.limit(RATE_LIMIT_ADMIN_LOGIN)
async def admin_login(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    username = (form.get("username") or "").strip()[:50]
    password = form.get("password") or ""
    user = authenticate_user(db, username, password)
    if not user:
        log_action(db, {"username": username}, "login_failed", ip=request.client.host if request.client else None)
        return templates.TemplateResponse(request, "admin_login.html", {
            "request": request, "error": "Invalid username or password."
        }, status_code=401)
    session_id = create_session(db, user)
    log_action(db, {"username": user.username}, "login_success", ip=request.client.host if request.client else None)
    resp = RedirectResponse(url="/admin", status_code=303)
    # Secure flag only when served over HTTPS (Railway/prod); plain HTTP in
    # local dev must still work, so it's keyed to the request scheme.
    resp.set_cookie(
        SESSION_COOKIE_NAME, session_id,
        httponly=True, samesite="lax", max_age=86400,
        secure=request.url.scheme == "https",
    )
    return resp

@app.post("/admin/logout", include_in_schema=False)
def admin_logout(request: Request, db: Session = Depends(get_db)):
    sid = request.cookies.get(SESSION_COOKIE_NAME)
    if sid:
        delete_session(db, sid)
    resp = RedirectResponse(url="/admin/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE_NAME, secure=request.url.scheme == "https")
    return resp


# ── Admin: Dashboard ──────────────────────────────────────────────────────────
@app.get("/admin", include_in_schema=False)
def admin_dashboard(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return _admin_redirect_login()
    from datetime import datetime, timezone, timedelta
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    stats = {
        "total_posts": db.query(Post).count(),
        "published_posts": db.query(Post).filter(Post.status == "published").count(),
        "pending_posts": db.query(Post).filter(Post.status == "pending").count(),
        "rejected_posts": db.query(Post).filter(Post.status == "rejected").count(),
        "total_articles": db.query(Article).count(),
        "unclustered_articles": db.query(Article).filter(Article.post_id.is_(None)).count(),
        "published_today": db.query(Post).filter(Post.status == "published", Post.created_at >= today).count(),
        "fb_published": 0, "ig_published": 0,
    }
    from models import PlatformPost
    stats["fb_published"] = db.query(PlatformPost).filter(PlatformPost.platform == "facebook", PlatformPost.status == "published").count()
    stats["ig_published"] = db.query(PlatformPost).filter(PlatformPost.platform == "instagram", PlatformPost.status == "published").count()
    recent_posts = [{"id": p.id, "title": p.social_summary or "", "language": p.language or "?",
                     "region": p.region, "status": p.status, "created_at": p.created_at,
                     "view_count": p.view_count or 0,
                     "platform_posts": p.platform_posts} for p in
                    db.query(Post).order_by(Post.created_at.desc()).limit(10).all()]
    recent_logs = db.query(AuditLog).order_by(AuditLog.timestamp.desc()).limit(10).all()
    return templates.TemplateResponse(request, "admin_dashboard.html", {
        "request": request, "user": user, "stats": stats,
        "recent_posts": recent_posts, "recent_logs": recent_logs,
    })


# ── Admin: Posts (list / edit / delete) ───────────────────────────────────────
@app.get("/admin/posts", include_in_schema=False)
def admin_posts(request: Request, status_filter: str = "", db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return _admin_redirect_login()
    q = db.query(Post)
    if status_filter:
        q = q.filter(Post.status == status_filter)
    posts = q.order_by(Post.created_at.desc()).limit(100).all()
    data = []
    for p in posts:
        d = _p(p)
        d["platform_posts"] = [
            {"platform": pp.platform, "status": pp.status,
             "published_at": pp.published_at.isoformat() if pp.published_at else None}
            for pp in p.platform_posts
        ]
        data.append(d)
    return templates.TemplateResponse(request, "admin_posts.html", {
        "request": request, "user": user, "posts": data,
        "total": len(posts), "status_filter": status_filter,
    })


@app.get("/admin/posts/{post_id}", include_in_schema=False)
def admin_post_edit(request: Request, post_id: int, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return _admin_redirect_login()
    post = db.get(Post, post_id)
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    pp = [{"id": pp.id, "platform": pp.platform, "status": pp.status,
           "published_at": pp.published_at.isoformat() if pp.published_at else None}
          for pp in post.platform_posts]
    return templates.TemplateResponse(request, "admin_post_edit.html", {
        "request": request, "user": user, "post": _p(post) | {"platform_posts": pp},
        "error": None, "success": None,
    })


@app.post("/admin/posts/{post_id}", include_in_schema=False)
@limiter.limit("30/minute")
async def admin_post_update(request: Request, post_id: int, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return _admin_redirect_login()
    post = db.get(Post, post_id)
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    form = await request.form()
    from datetime import datetime, timezone
    new_summary = (form.get("social_summary") or "").strip()
    new_body = (form.get("full_body") or "").strip()
    # Guard against accidentally wiping a post (happened 2026-08-09):
    # never let the editor clear content that already exists.
    if len(new_summary) < 5 and (post.social_summary or "").strip():
        return templates.TemplateResponse(request, "admin_post_edit.html", {
            "request": request, "user": user, "post": _p(post) | {"platform_posts": [
                {"id": pp.id, "platform": pp.platform, "status": pp.status,
                 "published_at": pp.published_at.isoformat() if pp.published_at else None}
                for pp in post.platform_posts]},
            "error": "Headline is too short — refusing to wipe the existing post.",
            "success": None,
        })
    if len(new_body) < 20 and (post.full_body or "").strip():
        return templates.TemplateResponse(request, "admin_post_edit.html", {
            "request": request, "user": user, "post": _p(post) | {"platform_posts": [
                {"id": pp.id, "platform": pp.platform, "status": pp.status,
                 "published_at": pp.published_at.isoformat() if pp.published_at else None}
                for pp in post.platform_posts]},
            "error": "Article body is too short — refusing to wipe existing content.",
            "success": None,
        })
    post.social_summary = new_summary
    post.full_body = new_body
    post.language = (form.get("language") or "").strip() or None
    post.region = (form.get("region") or "").strip() or None
    post.category = (form.get("category") or "").strip() or None
    post.image_url = (form.get("image_url") or "").strip() or None
    post.image_source_credit = (form.get("image_source_credit") or "").strip() or None
    new_status = (form.get("status") or "").strip()
    if new_status in ("pending", "approved", "rejected", "published"):
        post.status = new_status
    db.commit()
    log_action(db, user, "edit_post", "Post", post.id)
    pp = [{"id": pp.id, "platform": pp.platform, "status": pp.status,
           "published_at": pp.published_at.isoformat() if pp.published_at else None}
          for pp in post.platform_posts]
    return templates.TemplateResponse(request, "admin_post_edit.html", {
        "request": request, "user": user, "post": _p(post) | {"platform_posts": pp},
        "error": None, "success": f"Post #{post.id} updated.",
    })


@app.post("/admin/posts/{post_id}/delete", include_in_schema=False)
@limiter.limit("20/minute")
async def admin_post_delete(request: Request, post_id: int, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return _admin_redirect_login()
    if user["role"] != "admin":
        return templates.TemplateResponse(request, "admin_posts.html", {
            "request": request, "user": user, "posts": [], "total": 0,
            "status_filter": "", "error": "Only admins can delete posts.",
        }, status_code=403)
    post = db.get(Post, post_id)
    if post:
        log_action(db, user, "delete_post", "Post", post_id)
        db.delete(post)
        db.commit()
    return RedirectResponse(url="/admin/posts", status_code=303)


# ── Admin: Articles (ingested raw feed items) ─────────────────────────────────
@app.get("/admin/articles", include_in_schema=False)
def admin_articles(request: Request, limit: int = 100, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return _admin_redirect_login()
    articles = db.query(Article).order_by(Article.created_at.desc()).limit(min(limit, 500)).all()
    rows = [{
        "id": a.id, "title": a.title, "source": a.source, "url": a.url,
        "region": a.region, "category": a.category,
        "has_image": bool(a.image_url), "post_id": a.post_id,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    } for a in articles]
    return templates.TemplateResponse(request, "admin_articles.html", {
        "request": request, "user": user, "articles": rows,
    })


# ── Admin: Settings ───────────────────────────────────────────────────────────
@app.get("/admin/settings", include_in_schema=False)
def admin_settings(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return _admin_redirect_login()
    return templates.TemplateResponse(request, "admin_settings.html", {
        "request": request, "user": user, "settings": _get_settings(db),
        "error": None, "success": None,
    })


@app.post("/admin/settings", include_in_schema=False)
@limiter.limit("20/minute")
async def admin_settings_save(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return _admin_redirect_login()
    if user["role"] not in ("admin", "editor"):
        return templates.TemplateResponse(request, "admin_settings.html", {
            "request": request, "user": user, "settings": _get_settings(db),
            "error": "You don't have permission to edit settings.", "success": None,
        }, status_code=403)
    form = await request.form()
    for key in DEFAULT_SETTINGS:
        if key in form:
            _save_setting(db, key, (form.get(key) or "").strip(), user)
    db.commit()
    log_action(db, user, "update_settings", "SiteSetting", None)
    return templates.TemplateResponse(request, "admin_settings.html", {
        "request": request, "user": user, "settings": _get_settings(db),
        "error": None, "success": "Site settings saved.",
    })


# ── Admin: Users ──────────────────────────────────────────────────────────────
@app.get("/admin/users", include_in_schema=False)
def admin_users(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return _admin_redirect_login()
    if user["role"] != "admin":
        return templates.TemplateResponse(request, "admin_users.html", {
            "request": request, "user": user, "users": [], "error": None, "success": None,
        }, status_code=403)
    users = db.query(AdminUser).order_by(AdminUser.id).all()
    return templates.TemplateResponse(request, "admin_users.html", {
        "request": request, "user": user, "users": users,
        "error": None, "success": None,
    })


@app.post("/admin/users/create", include_in_schema=False)
@limiter.limit("20/minute")
async def admin_user_create(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user or user["role"] != "admin":
        return _admin_redirect_login()
    form = await request.form()
    username = (form.get("username") or "").strip()[:50]
    email = (form.get("email") or "").strip()[:255]
    password = form.get("password") or ""
    role = (form.get("role") or "editor").strip()[:20]
    if not username or not email or len(password) < 6:
        return templates.TemplateResponse(request, "admin_users.html", {
            "request": request, "user": user,
            "users": db.query(AdminUser).order_by(AdminUser.id).all(),
            "error": "Username, email and a 6+ char password are required.", "success": None,
        })
    try:
        create_admin_user(db, username, email, password, role=role if role in ("admin", "editor", "viewer") else "editor")
        log_action(db, user, "create_user", "AdminUser", None)
    except Exception as e:
        return templates.TemplateResponse(request, "admin_users.html", {
            "request": request, "user": user,
            "users": db.query(AdminUser).order_by(AdminUser.id).all(),
            "error": f"Could not create user: {e}", "success": None,
        })
    return RedirectResponse(url="/admin/users", status_code=303)


@app.post("/admin/users/{user_id}/toggle", include_in_schema=False)
@limiter.limit("20/minute")
def admin_user_toggle(request: Request, user_id: int, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user or user["role"] != "admin":
        return _admin_redirect_login()
    target = db.get(AdminUser, user_id)
    if target and target.username != user["username"]:
        target.is_active = not target.is_active
        db.commit()
        log_action(db, user, "toggle_user", "User", user_id)
    return RedirectResponse(url="/admin/users", status_code=303)


# ── Admin: Audit logs ─────────────────────────────────────────────────────────
@app.get("/admin/logs", include_in_schema=False)
def admin_logs(request: Request, user_filter: str = "", db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return _admin_redirect_login()
    q = db.query(AuditLog)
    if user_filter:
        q = q.filter(AuditLog.user == user_filter)
    logs = q.order_by(AuditLog.timestamp.desc()).limit(100).all()
    all_users = db.query(AdminUser).order_by(AdminUser.username).all()
    return templates.TemplateResponse(request, "admin_logs.html", {
        "request": request, "user": user, "logs": logs,
        "all_users": all_users, "user_filter": user_filter,
    })
