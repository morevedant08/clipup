"""Shared FastAPI dependencies (database session, current user, services, dispatcher)."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable

import jwt
from fastapi import Depends, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session, selectinload

from app.database import get_db
from app.models.user import User
from app.security import decode_access_token
from app.services.youtube import YouTubeService

logger = logging.getLogger(__name__)
bearer_scheme = HTTPBearer(auto_error=False)

JobDispatcher = Callable[[uuid.UUID], str | None]
UploadDispatcher = Callable[[uuid.UUID], str | None]


def get_youtube_service() -> YouTubeService:
    """Provide the YouTubeService instance (overridable in tests)."""
    return YouTubeService()


def _default_dispatch_job(job_id: uuid.UUID) -> str | None:
    """Enqueue a rights-approved clipping job onto Celery."""
    try:
        from app.workers.tasks import process_clipping_job
    except ImportError:
        logger.warning(
            "Celery worker module not yet configured; job %s remains queued.", job_id
        )
        return None
    async_result = process_clipping_job.delay(str(job_id))
    return str(async_result.id)


def get_job_dispatcher() -> JobDispatcher:
    """Provide the background job dispatcher (overridable in tests)."""
    return _default_dispatch_job


def _default_dispatch_upload(upload_id: uuid.UUID) -> str | None:
    """Enqueue a YouTube Shorts upload onto Celery."""
    from app.workers.tasks import upload_clip_to_youtube

    async_result = upload_clip_to_youtube.delay(str(upload_id))
    return str(async_result.id)


def get_upload_dispatcher() -> UploadDispatcher:
    """Provide the background upload dispatcher (overridable in tests)."""
    return _default_dispatch_upload


def _resolve_user_from_token(token: str, db: Session) -> User:
    try:
        payload = decode_access_token(token)
        user_id = uuid.UUID(str(payload["sub"]))
    except (jwt.PyJWTError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired authentication token.",
        ) from exc

    user = (
        db.query(User)
        .options(selectinload(User.youtube_channels))
        .filter(User.id == user_id)
        .first()
    )
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authenticated user no longer exists.",
        )
    return user


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    """Validate Bearer JWT and return the authenticated User with connected channels."""
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
        )
    return _resolve_user_from_token(credentials.credentials, db)


def get_current_user_for_media(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    token: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> User:
    """Authenticate media playback via Bearer header or ?token= query parameter."""
    raw_token = (
        credentials.credentials
        if credentials is not None and credentials.scheme.lower() == "bearer"
        else token
    )
    if not raw_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required to view clip media.",
        )
    return _resolve_user_from_token(raw_token, db)
