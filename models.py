import os
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, LargeBinary, create_engine, func
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from pgvector.sqlalchemy import Vector

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://news_user:news_pass@localhost:5432/news_db"
)
# Railway (and older Heroku) emit postgres:// — SQLAlchemy requires postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    # Batch processes (watcher, bot, migrations) and the web service all share
    # this engine. Keep total connections modest so a few workers can't exhaust
    # the deployed Postgres max_connections.
    pool_size=5,
    max_overflow=5,
    connect_args={"connect_timeout": 10},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class Article(Base):
    """Raw ingested RSS items -- working material for clustering/verification.
    Deleted once the resulting post is fully published everywhere, or after
    a retention window if it never gets corroborated into a post at all
    (cleanup logic comes later, not part of this schema change)."""
    __tablename__ = "articles"
    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)
    source = Column(String, nullable=False)
    url = Column(String, unique=True, nullable=False)
    content = Column(Text, nullable=False)
    region = Column(String, default="international", index=True)
    category = Column(String, default="general", index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    embedding = Column(Vector(768))  # Gemini embeddings, was Vector(384) for local sentence-transformers
    image_url = Column(String, nullable=True)  # NEW: was extracted by ingest_rss.py but never stored

    # NEW: links a raw article to the synthesized post it fed into (if any).
    # Null until the clustering/verification step assigns it. This is what
    # makes the deletion rule from earlier executable: once every
    # platform_posts row for this post_id shows status="published", the
    # articles pointing at it are safe to delete.
    post_id = Column(Integer, ForeignKey("posts.id"), nullable=True, index=True)


class Post(Base):
    """A synthesized, verified story -- one row per story, regardless of how
    many platforms it goes out to. Fed by 2+ corroborating Articles."""
    __tablename__ = "posts"
    id = Column(Integer, primary_key=True, index=True)
    full_body = Column(Text, nullable=False)          # website article
    social_summary = Column(String, nullable=False)   # 2-4 line version
    language = Column(String, nullable=True)           # "nepali" or "english" -- which Gemini chose for this story
    image_url = Column(String, nullable=True)
    image_source_credit = Column(String, nullable=True)
    # Normalized rehosted image bytes (JPEG) stored in the DB so the public
    # /post_image/{id}.jpg route can serve them permanently (survives Railway
    # restarts) and IG/FB always have a stable public image_url.
    image_data = Column(LargeBinary, nullable=True)
    region = Column(String, nullable=True, index=True)
    category = Column(String, nullable=True, index=True)

    # pending -> approved/rejected (via Telegram) -> published (once all
    # platform_posts rows below are published)
    status = Column(String, default="pending", index=True)
    telegram_message_id = Column(String, nullable=True)
    whatsapp_message_id = Column(String, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Page views on the public article page (/web/post/{id}). Incremented once per
    # browser session per post (deduped via the flnp_viewed cookie). Drives the
    # /web/popular page and the "Most Read" sidebars; the number itself is shown
    # only to admins, never rendered in public templates.
    view_count = Column(Integer, default=0, server_default="0", nullable=False)

    platform_posts = relationship(
        "PlatformPost", back_populates="post", cascade="all, delete-orphan"
    )


class PlatformPost(Base):
    """One row per (post, platform) pair. Avoids bolting facebook_post_id /
    instagram_post_id / threads_post_id / tiktok_post_id columns onto Post
    directly -- adding a platform later means inserting rows, not altering
    the table."""
    __tablename__ = "platform_posts"
    id = Column(Integer, primary_key=True, index=True)
    post_id = Column(Integer, ForeignKey("posts.id"), nullable=False, index=True)

    platform = Column(String, nullable=False, index=True)  # facebook/instagram/threads/tiktok/website
    platform_post_id = Column(String, nullable=True)        # null until actually published
    status = Column(String, default="pending", index=True)  # pending/published/failed
    published_at = Column(DateTime(timezone=True), nullable=True)

    post = relationship("Post", back_populates="platform_posts")
