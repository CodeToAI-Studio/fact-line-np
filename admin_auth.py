"""
Admin authentication - session-based with secure cookies.

Sessions are persisted in the database (admin_sessions table) so they survive
process restarts and are shared across every worker/replica. A session created
on one process is recognized by any other, which is required once this deploys
behind a multi-worker WSGI/ASGI server (Railway, gunicorn, uvicorn --workers).
"""
import secrets
from datetime import datetime, timezone, timedelta
from typing import Optional
from fastapi import Request, HTTPException, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session as DbSession
from admin_models import AdminUser, AdminSession

SESSION_COOKIE_NAME = "flnp_admin_session"
SESSION_DURATION_HOURS = 24
# Sweep expired rows occasionally; every get does it cheaply via index.
_SWEEP_PROBABILITY = 0.05


def create_session(db: DbSession, user: AdminUser) -> str:
    """Persist a new session for authenticated user. Returns session_id."""
    session_id = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(hours=SESSION_DURATION_HOURS)
    row = AdminSession(
        session_id=session_id,
        user_id=user.id,
        username=user.username,
        role=user.role,
        email=user.email,
        expires_at=expires,
    )
    db.add(row)
    db.commit()
    return session_id


def get_session(db: DbSession, session_id: str) -> Optional[dict]:
    """Return session data if valid, None otherwise. Expired rows are removed."""
    if not session_id:
        return None
    row = db.get(AdminSession, session_id)
    if not row:
        return None
    now = datetime.now(timezone.utc)
    if row.expires_at < now:
        db.delete(row)
        db.commit()
        return None
    return {
        "user_id": row.user_id,
        "username": row.username,
        "email": row.email,
        "role": row.role,
        "expires": row.expires_at.isoformat(),
    }


def delete_session(db: DbSession, session_id: str):
    """Destroy session (logout)."""
    if not session_id:
        return
    row = db.get(AdminSession, session_id)
    if row:
        db.delete(row)
        db.commit()


def _sweep_expired(db: DbSession):
    """Best-effort cleanup of expired sessions (called ~5% of reads)."""
    try:
        expired = db.query(AdminSession).filter(
            AdminSession.expires_at < datetime.now(timezone.utc)
        ).delete()
        if expired:
            db.commit()
    except Exception:
        db.rollback()


def get_current_user(request: Request, db: DbSession) -> Optional[dict]:
    """Extract current user from session cookie. Returns session dict or None."""
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if not session_id:
        return None
    import random
    if random.random() < _SWEEP_PROBABILITY:
        _sweep_expired(db)
    return get_session(db, session_id)


def require_auth(request: Request, db: DbSession) -> dict:
    """Require authentication. Raises 401 if not logged in."""
    user = get_current_user(request, db)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated"
        )
    return user


def require_role(request: Request, db: DbSession, allowed_roles: list[str]) -> dict:
    """Require specific role(s). Raises 403 if insufficient permissions."""
    user = require_auth(request, db)
    if user["role"] not in allowed_roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Requires one of: {', '.join(allowed_roles)}"
        )
    return user


def login_redirect() -> RedirectResponse:
    """Redirect to login page."""
    return RedirectResponse(url="/admin/login", status_code=status.HTTP_303_SEE_OTHER)


def authenticate_user(db: DbSession, username: str, password: str) -> Optional[AdminUser]:
    """Verify credentials. Returns user object if valid, None otherwise."""
    user = db.query(AdminUser).filter(
        AdminUser.username == username,
        AdminUser.is_active == True
    ).first()

    if not user or not user.check_password(password):
        return None

    # Update last login
    user.last_login = datetime.now(timezone.utc)
    db.commit()

    return user


def create_admin_user(db: DbSession, username: str, email: str, password: str, role: str = "editor") -> AdminUser:
    """Create a new admin user. Used for initial setup."""
    user = AdminUser(
        username=username,
        email=email,
        role=role,
        is_active=True
    )
    user.set_password(password)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def log_action(db: DbSession, user: dict, action: str, entity_type: str = None,
               entity_id: int = None, details: dict = None, ip: str = None):
    """Write to audit log."""
    from admin_models import AuditLog

    log = AuditLog(
        user=user["username"],
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        details=details,
        ip_address=ip
    )
    db.add(log)
    db.commit()