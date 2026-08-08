"""
Admin CMS models - User accounts, site settings, and audit logs.
Separate from the main news pipeline models for clarity.
"""
from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Text, DateTime, Boolean, JSON
from models import Base
import hashlib
import secrets


class AdminUser(Base):
    """Admin/editor accounts with role-based permissions."""
    __tablename__ = "admin_users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, nullable=False, index=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(String(128), nullable=False)  # SHA-256 + salt
    role = Column(String(20), default="editor", nullable=False)  # admin, editor, viewer
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    last_login = Column(DateTime(timezone=True), nullable=True)

    @staticmethod
    def hash_password(password: str, salt: str = None) -> tuple[str, str]:
        """Hash password with SHA-256 + random salt. Returns (hash, salt)."""
        if salt is None:
            salt = secrets.token_hex(16)
        pwd_hash = hashlib.sha256((password + salt).encode()).hexdigest()
        return pwd_hash, salt

    def set_password(self, password: str):
        """Hash and store password."""
        pwd_hash, salt = self.hash_password(password)
        self.password_hash = f"{salt}${pwd_hash}"

    def check_password(self, password: str) -> bool:
        """Verify password against stored hash."""
        if not self.password_hash or "$" not in self.password_hash:
            return False
        salt, stored_hash = self.password_hash.split("$", 1)
        pwd_hash, _ = self.hash_password(password, salt)
        return pwd_hash == stored_hash


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
