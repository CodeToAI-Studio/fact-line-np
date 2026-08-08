"""
Admin CMS models - User accounts, site settings, and audit logs.
Separate from the main news pipeline models for clarity.
"""
from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Text, DateTime, Boolean, JSON
from models import Base
import hashlib
import hmac
import secrets


class AdminUser(Base):
    """Admin/editor accounts with role-based permissions."""
    __tablename__ = "admin_users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, nullable=False, index=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)  # format: salt$iterations$hash (PBKDF2-SHA256)
    role = Column(String(20), default="editor", nullable=False)  # admin, editor, viewer
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    last_login = Column(DateTime(timezone=True), nullable=True)

    # PBKDF2-HMAC-SHA256 parameters (OWASP recommended ~600k iterations, 2023).
    # Deliberately slow: a fast general-purpose hash (plain sha256) would be
    # crackable in bulk on GPU. Format stored: "salt$iterations$hash" so the
    # iteration count can be raised later without a schema change.
    PBKDF2_ITERATIONS = 600_000
    PBKDF2_SALT_BYTES = 16
    PBKDF2_HASH_BYTES = 32

    @classmethod
    def _derive(cls, password: str, salt: bytes, iterations: int) -> str:
        return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations).hex()

    def set_password(self, password: str):
        """Hash and store password using PBKDF2-HMAC-SHA256 + random salt."""
        salt = secrets.token_bytes(self.PBKDF2_SALT_BYTES)
        digest = self._derive(password, salt, self.PBKDF2_ITERATIONS)
        self.password_hash = f"{salt.hex()}${self.PBKDF2_ITERATIONS}${digest}"

    def check_password(self, password: str) -> bool:
        """Verify password against stored hash (constant-time compare)."""
        if not self.password_hash or "$" not in self.password_hash:
            return False
        parts = self.password_hash.split("$")
        if len(parts) != 3:
            return False
        salt_hex, iterations_str, stored_hash = parts
        try:
            salt = bytes.fromhex(salt_hex)
            iterations = int(iterations_str)
        except ValueError:
            return False
        digest = self._derive(password, salt, iterations)
        # hmac.compare_digest prevents timing side-channels on the compare.
        return hmac.compare_digest(digest, stored_hash)


class SiteSetting(Base):
    """Key-value store for site configuration - editable from admin panel."""
    __tablename__ = "site_settings"

    key = Column(String(100), primary_key=True)
    value = Column(Text, nullable=True)
    value_type = Column(String(20), default="string")  # string, int, bool, json
    description = Column(String(500), nullable=True)
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    updated_by = Column(String(50), nullable=True)  # username


class AuditLog(Base):
    """Track all admin actions for security and debugging."""
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    user = Column(String(50), nullable=False, index=True)
    action = Column(String(100), nullable=False)  # e.g. "edit_post", "delete_article", "change_setting"
    entity_type = Column(String(50), nullable=True)  # "Post", "Article", "SiteSetting"
    entity_id = Column(Integer, nullable=True)
    details = Column(JSON, nullable=True)  # store before/after values, etc.
    ip_address = Column(String(45), nullable=True)
    timestamp = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)


class AdminSession(Base):
    """Auth session persisted in the DB so it survives restarts and is shared
    across any number of app processes/workers. One row per active session."""
    __tablename__ = "admin_sessions"

    session_id = Column(String(64), primary_key=True)               # secrets.token_urlsafe(32)
    user_id = Column(Integer, nullable=False, index=True)           # AdminUser.id
    username = Column(String(50), nullable=False)                   # denormalized for speed
    role = Column(String(20), nullable=False)
    email = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)


# Default site settings to seed on first run
DEFAULT_SETTINGS = {
    "site_title": ("Fact Line NP", "string", "Site name shown in header and meta tags"),
    "site_tagline": ("Nepal News. Verified. Explained.", "string", "Tagline under logo"),
    "breaking_news_text": ("", "string", "Text for breaking news ticker (leave empty to hide)"),
    "breaking_news_url": ("", "string", "Link for breaking news ticker"),
    "footer_about": ("Fact Line NP delivers verified, contextualized news from Nepal and around the world.", "string", "Footer about text"),
    "contact_email": ("contact@factlinenp.com", "string", "Public contact email"),
    "facebook_url": ("https://facebook.com/factlinenp", "string", "Facebook page URL"),
    "instagram_url": ("https://instagram.com/factlinenp", "string", "Instagram account URL"),
    "youtube_url": ("https://youtube.com/@factlinenp", "string", "YouTube channel URL"),
    "twitter_url": ("https://twitter.com/factlinenp", "string", "Twitter/X account URL"),
    "ad_header_code": ("", "string", "HTML/JS for header ad slot"),
    "ad_sidebar_code": ("", "string", "HTML/JS for sidebar ad slot"),
    "ad_article_code": ("", "string", "HTML/JS for in-article ad slot"),
    "enable_analytics": ("false", "bool", "Enable Google Analytics"),
    "analytics_id": ("", "string", "Google Analytics tracking ID"),
}
