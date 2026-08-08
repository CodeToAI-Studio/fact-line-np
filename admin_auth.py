"""
Admin authentication - session-based with secure cookies.
No JWT complexity; sessions stored server-side in DB.
"""
import secrets
from datetime import datetime, timezone, timedelta
from typing import Optional
from fastapi import Request, HTTPException, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from admin_models import AdminUser


# In-memory session store (for simplicity; move to Redis/DB for multi-instance)
# Format: {session_id: {"user_id": int, "username": str, "role": str, "expires": datetime}}
_sessions = {}

SESSION_COOKIE_NAME = "flnp_admin_session"
SESSION_DURATION_HOURS = 24


def create_session(user: AdminUser) -> str:
    """Create a new session for authenticated user. Returns session_id."""
    session_id = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(hours=SESSION_DURATION_HOURS)

    _sessions[session_id] = {
        "user_id": user.id,
        "username": user.username,
        "email": user.email,
        "role": user.role,
        "expires": expires,
    }

    return session_id


def get_session(session_id: str) -> Optional[dict]:
    """Get session data if valid, None otherwise."""
    if not session_id or session_id not in _sessions:
        return None

    session = _sessions[session_id]

    # Check expiry
    if session["expires"] < datetime.now(timezone.utc):
        del _sessions[session_id]
        return None

    return session


def delete_session(session_id: str):
    """Destroy session (logout)."""
    if session_id in _sessions:
        del _sessions[session_id]


def get_current_user(request: Request) -> Optional[dict]:
    """Extract current user from session cookie. Returns session dict or None."""
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if not session_id:
        return None
    return get_session(session_id)


def require_auth(request: Request) -> dict:
    """Require authentication. Raises 401 if not logged in."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated"
        )
    return user


def require_role(request: Request, allowed_roles: list[str]) -> dict:
    """Require specific role(s). Raises 403 if insufficient permissions."""
    user = require_auth(request)
    if user["role"] not in allowed_roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Requires one of: {', '.join(allowed_roles)}"
        )
    return user


def login_redirect() -> RedirectResponse:
    """Redirect to login page."""
    return RedirectResponse(url="/admin/login", status_code=status.HTTP_303_SEE_OTHER)


def authenticate_user(db: Session, username: str, password: str) -> Optional[AdminUser]:
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


def create_admin_user(db: Session, username: str, email: str, password: str, role: str = "editor") -> AdminUser:
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


def log_action(db: Session, user: dict, action: str, entity_type: str = None,
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
