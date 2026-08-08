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
from embeddings import get_embedding
from llm_models import PRIMARY_MODELS, list_available_models
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
    with engine.connect() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        conn.commit()
    Base.metadata.create_all(bind=engine)

    if GEMINI_API_KEY:
        llm_client = genai.Client(api_key=GEMINI_API_KEY)

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
            response = await run_in_threadpool(
                lambda mn=model_name: llm_client.models.generate_content(
                    model=mn,
                    contents=prompt,
                )
            )
            if response and response.text:
                answer_text = response.text
                model_used = model_name
                break
        except APIError as e:
            last_error = e
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
                response = await run_in_threadpool(
                    lambda mn=model_name: llm_client.models.generate_content(
                        model=mn,
                        contents=prompt,
                    )
                )
                if response and response.text:
                    answer_text = response.text
                    model_used = model_name
                    break
            except Exception as e:
                last_error = e
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
    }


def _all_categories(db):
    rows = db.execute(
        select(Post.category).where(Post.status == "published")
        .distinct().order_by(Post.category)
    ).scalars().all()
    return [c for c in rows if c]


def _most_read(db, limit: int = 5):
    """No view-count column yet — recency as proxy."""
    rows = db.execute(
        select(Post).where(Post.status == "published")
        .order_by(Post.created_at.desc()).limit(limit)
    ).scalars().all()
    return [_p(p) for p in rows]


@app.get("/web", include_in_schema=False)
def web_index(request: Request, db: Session = Depends(get_db)):
    all_posts = db.execute(
        select(Post).where(Post.status == "published")
        .order_by(Post.created_at.desc()).limit(30)
    ).scalars().all()
    post_dicts = [_p(p) for p in all_posts]

    hero_lead = next((p for p in post_dicts if p["image_url"]), post_dicts[0] if post_dicts else None)
    remaining = [p for p in post_dicts if p is not hero_lead]
    hero_subs = remaining[:4]

    cats = _all_categories(db)
    cat_sections = []
    for cat in cats:
        if cat.lower() == "opinion":
            continue
        rows = db.execute(
            select(Post).where(Post.status == "published",
                               func.lower(Post.category) == cat.lower())
            .order_by(Post.created_at.desc()).limit(3)
        ).scalars().all()
        if rows:
            cat_sections.append({"name": cat, "slug": cat.lower(), "posts": [_p(r) for r in rows]})

    op_rows = db.execute(
        select(Post).where(Post.status == "published",
                           func.lower(Post.category) == "opinion")
        .order_by(Post.created_at.desc()).limit(3)
    ).scalars().all()

    return templates.TemplateResponse("index.html", {
        "request": request, "current_page": "home",
        "hero_lead": hero_lead, "hero_subs": hero_subs,
        "latest_posts": post_dicts[:10],
        "most_read": _most_read(db),
        "category_sections": cat_sections,
        "opinion_posts": [_p(r) for r in op_rows],
        "all_categories": cats,
    })




@app.get("/web/post/{post_id}", include_in_schema=False)
def web_post(request: Request, post_id: int, db: Session = Depends(get_db)):
    post = db.get(Post, post_id)
    if not post or post.status not in ("published", "approved"):
        return templates.TemplateResponse("404.html", {"request": request}, status_code=404)
    words = len((post.full_body or "").split())
    related_rows = db.execute(
        select(Post).where(Post.status == "published", Post.id != post_id,
                           func.lower(Post.category) == (post.category or "").lower())
        .order_by(Post.created_at.desc()).limit(3)
    ).scalars().all()
    return templates.TemplateResponse("post.html", {
        "request": request, "post": _p(post),
        "related": [_p(r) for r in related_rows],
        "most_read": _most_read(db),
        "reading_time": max(1, round(words / 200)),
    })


@app.get("/web/category/{category}", include_in_schema=False)
def web_category(request: Request, category: str, db: Session = Depends(get_db)):
    import re as _re
    safe_cat = _re.sub(r"[^a-zA-Z0-9\-]", "", category)[:50]
    rows = db.execute(
        select(Post).where(Post.status == "published",
                           func.lower(Post.category) == safe_cat.lower())
        .order_by(Post.created_at.desc()).limit(40)
    ).scalars().all()
    return templates.TemplateResponse("category.html", {
        "request": request, "current_cat": safe_cat.lower(),
        "category": safe_cat, "posts": [_p(r) for r in rows],
        "all_categories": _all_categories(db),
    })


@app.get("/web/latest", include_in_schema=False)
def web_latest(request: Request, db: Session = Depends(get_db)):
    rows = db.execute(
        select(Post).where(Post.status == "published")
        .order_by(Post.created_at.desc()).limit(50)
    ).scalars().all()
    return templates.TemplateResponse("latest.html", {
        "request": request, "current_page": "latest",
        "posts": [_p(r) for r in rows], "all_categories": _all_categories(db),
    })


@app.get("/web/popular", include_in_schema=False)
def web_popular(request: Request, db: Session = Depends(get_db)):
    rows = db.execute(
        select(Post).where(Post.status == "published")
        .order_by(Post.created_at.desc()).limit(30)
    ).scalars().all()
    return templates.TemplateResponse("latest.html", {
        "request": request, "current_page": "popular",
        "posts": [_p(r) for r in rows], "all_categories": _all_categories(db),
    })


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
    return templates.TemplateResponse("search.html", {
        "request": request, "query": query,
        "results": results, "all_categories": _all_categories(db),
    })


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
    return templates.TemplateResponse(tpl, {
        "request": request, "all_categories": _all_categories(db),
    })


@app.exception_handler(404)
async def not_found_handler(request: Request, exc):
    if request.url.path.startswith("/web"):
        return templates.TemplateResponse("404.html", {"request": request}, status_code=404)
    from fastapi.responses import JSONResponse
    return JSONResponse({"detail": "Not found"}, status_code=404)


@app.exception_handler(500)
async def server_error_handler(request: Request, exc):
    logger.error("500 on %s: %s", request.url.path, exc)
    if request.url.path.startswith("/web"):
        return templates.TemplateResponse("404.html", {"request": request}, status_code=500)
    from fastapi.responses import JSONResponse
    return JSONResponse({"detail": "Internal server error"}, status_code=500)

