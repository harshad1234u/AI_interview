"""Request dependencies shared by the routers."""

from typing import Optional

from fastapi import Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.session import get_db
from app.models import Candidate
from app.services.auth import SESSION_SLIDING_TTL, session_candidate

SESSION_COOKIE = "coach_session"
NOT_AUTHENTICATED = "Please log in."


def current_candidate(request: Request, db: Session = Depends(get_db)) -> Candidate:
    """
    The signed-in candidate, extracted from the HttpOnly session cookie:
    the only source of identity for every protected route.
    Missing cookie, unknown token, revoked or expired session is a 401.
    """
    token = request.cookies.get(SESSION_COOKIE)
    candidate = session_candidate(db, token) if token else None
    if candidate is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, NOT_AUTHENTICATED)
    return candidate


def set_session_cookie(
    response: Response,
    token: str,
    settings: Settings,
    max_age: Optional[int] = None,
) -> None:
    """
    Sets the HttpOnly session cookie.
    JavaScript cannot access this cookie. SameSite prevents cross-site POST attacks.
    """
    cookie_max_age = max_age if max_age is not None else int(SESSION_SLIDING_TTL.total_seconds())
    kwargs: dict[str, object] = {
        "key": SESSION_COOKIE,
        "value": token,
        "max_age": cookie_max_age,
        "path": "/",
        "httponly": True,
        "samesite": settings.cookie_samesite,
        "secure": settings.auth_cookie_secure,
    }
    if settings.cookie_domain:
        kwargs["domain"] = settings.cookie_domain
    response.set_cookie(**kwargs)


def clear_session_cookie(response: Response, settings: Settings) -> None:
    """Clears the session cookie."""
    kwargs: dict[str, object] = {
        "key": SESSION_COOKIE,
        "path": "/",
        "httponly": True,
        "samesite": settings.cookie_samesite,
        "secure": settings.auth_cookie_secure,
    }
    if settings.cookie_domain:
        kwargs["domain"] = settings.cookie_domain
    response.delete_cookie(**kwargs)
