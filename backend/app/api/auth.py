"""Google OAuth login and read-only YouTube channel connection endpoints.

The shared OAuth callback is registered at the application root:
    GET /auth/google/callback  ->  http://localhost:8000/auth/google/callback
and handles both `google_login` and `youtube_connect` flows using a signed,
short-lived, session-cookie-bound `state` token.
"""

from __future__ import annotations

import logging
import uuid
from urllib.parse import urlencode

import jwt
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_youtube_service
from app.config import get_settings
from app.database import get_db
from app.models.user import User
from app.models.youtube_channel import YouTubeChannel
from app.schemas.auth import (
    AuthTokenResponse,
    DevLoginRequest,
    OAuthUrlResponse,
    UserResponse,
    YouTubeChannelResponse,
)
from app.security import (
    OAUTH_SESSION_COOKIE_NAME,
    OAUTH_STATE_TTL_MINUTES,
    TokenCipher,
    create_access_token,
    create_oauth_state,
    generate_oauth_session_id,
    verify_oauth_state,
)
from app.services.audit import record_audit
from app.services.youtube import (
    FORBIDDEN_UPLOAD_SCOPES,
    YOUTUBE_READONLY_SCOPE,
    YOUTUBE_UPLOAD_SCOPE,
    YouTubeAPIError,
    YouTubeService,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])
oauth_callback_router = APIRouter(tags=["auth"])


def _set_oauth_session_cookie(response: Response, session_id: str) -> None:
    """Bind the OAuth flow to the caller's browser via an HttpOnly SameSite=Lax cookie."""
    settings = get_settings()
    response.set_cookie(
        key=OAUTH_SESSION_COOKIE_NAME,
        value=session_id,
        max_age=OAUTH_STATE_TTL_MINUTES * 60,
        httponly=True,
        samesite="lax",
        secure=settings.app_env.lower() == "production",
        path="/",
    )


def _clear_oauth_session_cookie(response: Response) -> None:
    """Clear the one-time OAuth session binding cookie after callback completion."""
    settings = get_settings()
    response.delete_cookie(
        key=OAUTH_SESSION_COOKIE_NAME,
        path="/",
        httponly=True,
        samesite="lax",
        secure=settings.app_env.lower() == "production",
    )


@router.get("/google/login", response_model=OAuthUrlResponse)
def google_login_url(
    response: Response,
    youtube_service: YouTubeService = Depends(get_youtube_service),
) -> OAuthUrlResponse:
    """Return Google OAuth 2.0 login URL with a signed, short-lived, session-bound state."""
    session_id = generate_oauth_session_id()
    state = create_oauth_state("google_login", session_id=session_id)
    try:
        auth_url = youtube_service.build_google_login_url(state)
    except YouTubeAPIError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc

    _set_oauth_session_cookie(response, session_id)
    return OAuthUrlResponse(auth_url=auth_url)


@router.get("/youtube/connect", response_model=OAuthUrlResponse)
def youtube_connect_url(
    response: Response,
    current_user: User = Depends(get_current_user),
    youtube_service: YouTubeService = Depends(get_youtube_service),
) -> OAuthUrlResponse:
    """Return Google OAuth URL requesting read-only YouTube access (no upload scope)."""
    session_id = generate_oauth_session_id()
    state = create_oauth_state(
        "youtube_connect",
        session_id=session_id,
        user_id=current_user.id,
    )
    try:
        auth_url = youtube_service.build_youtube_connect_url(state)
    except YouTubeAPIError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc

    _set_oauth_session_cookie(response, session_id)
    return OAuthUrlResponse(auth_url=auth_url)


@router.get("/youtube/enable-upload", response_model=OAuthUrlResponse)
def youtube_enable_upload_url(
    response: Response,
    current_user: User = Depends(get_current_user),
    youtube_service: YouTubeService = Depends(get_youtube_service),
) -> OAuthUrlResponse:
    """Explicit opt-in OAuth flow requesting YouTube upload scope (youtube.upload).

    Reuses the same signed, short-lived, session-bound state mechanism and the
    single registered callback URI (/auth/google/callback).
    """
    session_id = generate_oauth_session_id()
    state = create_oauth_state(
        "youtube_upload_connect",
        session_id=session_id,
        user_id=current_user.id,
    )
    try:
        auth_url = youtube_service.build_youtube_upload_connect_url(state)
    except YouTubeAPIError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc

    _set_oauth_session_cookie(response, session_id)
    return OAuthUrlResponse(auth_url=auth_url)


def _handle_google_login_callback(
    *,
    code: str,
    mode: str,
    db: Session,
    youtube_service: YouTubeService,
) -> Response:
    settings = get_settings()
    try:
        tokens = youtube_service.exchange_code_for_tokens(
            code=code,
            redirect_uri=settings.google_oauth_redirect_uri,
        )
        access_token = tokens.get("access_token")
        if not access_token:
            raise YouTubeAPIError("Google OAuth did not return an access token.")
        profile = youtube_service.fetch_google_userinfo(str(access_token))
    except YouTubeAPIError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc

    email = profile.get("email")
    google_sub = profile.get("sub")
    if not email or not google_sub:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Google profile missing required email or subject identifier.",
        )

    user = (
        db.query(User)
        .filter((User.google_sub == google_sub) | (User.email == email))
        .first()
    )
    if user is None:
        user = User(
            email=email,
            google_sub=google_sub,
            name=profile.get("name"),
            avatar_url=profile.get("picture"),
        )
        db.add(user)
        db.flush()
    else:
        user.google_sub = google_sub
        user.name = profile.get("name") or user.name
        user.avatar_url = profile.get("picture") or user.avatar_url

    record_audit(
        db,
        "auth.google_login",
        user_id=user.id,
        details={"email": user.email},
    )
    db.commit()
    db.refresh(user)

    jwt_token = create_access_token(user.id, user.email)
    if mode == "json":
        payload = AuthTokenResponse(
            access_token=jwt_token,
            user=UserResponse.model_validate(user),
        )
        return JSONResponse(content=payload.model_dump(mode="json"))

    # Pass token in URL fragment so it is never sent in HTTP Referer or proxy logs
    redirect_target = (
        f"{settings.frontend_url.rstrip('/')}/auth/callback#token={jwt_token}"
    )
    return RedirectResponse(url=redirect_target, status_code=status.HTTP_302_FOUND)


def _handle_youtube_connect_callback(
    *,
    code: str,
    user_id: uuid.UUID,
    mode: str,
    db: Session,
    youtube_service: YouTubeService,
) -> Response:
    settings = get_settings()
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found.",
        )

    try:
        tokens = youtube_service.exchange_code_for_tokens(
            code=code,
            redirect_uri=settings.google_oauth_redirect_uri,
        )
    except YouTubeAPIError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc

    granted_scopes_raw = str(tokens.get("scope", YOUTUBE_READONLY_SCOPE))
    granted_scopes = set(granted_scopes_raw.split())
    if not FORBIDDEN_UPLOAD_SCOPES.isdisjoint(granted_scopes):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Unexpected write/upload YouTube scope granted; "
                "only read-only scope is permitted."
            ),
        )

    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")
    if not access_token:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Google OAuth response did not include an access token.",
        )
    if not refresh_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Google OAuth did not return a refresh token. "
                "Please reconnect and grant consent."
            ),
        )

    try:
        owned_channels = youtube_service.fetch_owned_channels(str(access_token))
    except YouTubeAPIError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc

    if not owned_channels:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No YouTube channel found on the authenticated Google account.",
        )

    cipher = TokenCipher.from_settings(settings)
    encrypted_refresh = cipher.encrypt(str(refresh_token))

    saved_channels: list[YouTubeChannel] = []
    for ch in owned_channels:
        existing = (
            db.query(YouTubeChannel)
            .filter(
                YouTubeChannel.user_id == user.id,
                YouTubeChannel.channel_id == ch["channel_id"],
            )
            .first()
        )
        if existing is None:
            record = YouTubeChannel(
                user_id=user.id,
                channel_id=ch["channel_id"],
                title=ch["title"],
                handle=ch.get("handle"),
                thumbnail_url=ch.get("thumbnail_url"),
                encrypted_refresh_token=encrypted_refresh,
                scopes=granted_scopes_raw,
            )
            db.add(record)
            saved_channels.append(record)
        else:
            existing.title = ch["title"]
            existing.handle = ch.get("handle")
            existing.thumbnail_url = ch.get("thumbnail_url")
            existing.encrypted_refresh_token = encrypted_refresh
            existing.scopes = granted_scopes_raw
            saved_channels.append(existing)

    record_audit(
        db,
        "youtube.channel_connected",
        user_id=user.id,
        details={
            "channel_ids": [ch["channel_id"] for ch in owned_channels],
            "scopes": granted_scopes_raw,
        },
    )
    db.commit()
    db.expire(user, ["youtube_channels"])
    for rec in saved_channels:
        db.refresh(rec)

    if mode == "json":
        payload = [
            YouTubeChannelResponse.model_validate(rec).model_dump(mode="json")
            for rec in saved_channels
        ]
        return JSONResponse(content=payload)

    params = urlencode({"youtube_connected": "1", "channels": len(saved_channels)})
    return RedirectResponse(
        url=f"{settings.frontend_url.rstrip('/')}/?{params}",
        status_code=status.HTTP_302_FOUND,
    )


def _handle_youtube_upload_connect_callback(
    *,
    code: str,
    user_id: uuid.UUID,
    mode: str,
    db: Session,
    youtube_service: YouTubeService,
) -> Response:
    """Complete explicit 'Enable uploading' OAuth flow and store encrypted upload tokens."""
    settings = get_settings()
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found.",
        )

    try:
        tokens = youtube_service.exchange_code_for_tokens(
            code=code,
            redirect_uri=settings.google_oauth_redirect_uri,
        )
    except YouTubeAPIError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc

    granted_scopes_raw = str(tokens.get("scope", ""))
    granted_scopes = set(granted_scopes_raw.split())
    if YOUTUBE_UPLOAD_SCOPE not in granted_scopes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "YouTube upload permission was not granted. "
                "Please approve the YouTube upload scope to enable publishing."
            ),
        )

    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")
    if not access_token:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Google OAuth response did not include an access token.",
        )
    if not refresh_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Google OAuth did not return a refresh token. "
                "Please click 'Enable uploading' again and grant consent."
            ),
        )

    try:
        owned_channels = youtube_service.fetch_owned_channels(str(access_token))
    except YouTubeAPIError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc

    if not owned_channels:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No YouTube channel found on the authenticated Google account.",
        )

    cipher = TokenCipher.from_settings(settings)
    encrypted_refresh = cipher.encrypt(str(refresh_token))

    saved_channels: list[YouTubeChannel] = []
    for ch in owned_channels:
        existing = (
            db.query(YouTubeChannel)
            .filter(
                YouTubeChannel.user_id == user.id,
                YouTubeChannel.channel_id == ch["channel_id"],
            )
            .first()
        )
        if existing is None:
            record = YouTubeChannel(
                user_id=user.id,
                channel_id=ch["channel_id"],
                title=ch["title"],
                handle=ch.get("handle"),
                thumbnail_url=ch.get("thumbnail_url"),
                encrypted_refresh_token=encrypted_refresh,
                scopes=granted_scopes_raw,
            )
            db.add(record)
            saved_channels.append(record)
        else:
            existing.title = ch["title"]
            existing.handle = ch.get("handle")
            existing.thumbnail_url = ch.get("thumbnail_url")
            existing.encrypted_refresh_token = encrypted_refresh
            existing.scopes = granted_scopes_raw
            saved_channels.append(existing)

    record_audit(
        db,
        "youtube.upload_enabled",
        user_id=user.id,
        details={
            "channel_ids": [ch["channel_id"] for ch in owned_channels],
            "scopes": granted_scopes_raw,
        },
    )
    db.commit()
    db.expire(user, ["youtube_channels"])
    for rec in saved_channels:
        db.refresh(rec)

    if mode == "json":
        payload = [
            YouTubeChannelResponse.model_validate(rec).model_dump(mode="json")
            for rec in saved_channels
        ]
        return JSONResponse(content=payload)

    params = urlencode({"youtube_upload_enabled": "1", "channels": len(saved_channels)})
    return RedirectResponse(
        url=f"{settings.frontend_url.rstrip('/')}/?{params}",
        status_code=status.HTTP_302_FOUND,
    )


@oauth_callback_router.get("/auth/google/callback")
def google_oauth_callback(
    request: Request,
    code: str = Query(..., min_length=1),
    state: str = Query(..., min_length=1),
    mode: str = Query(default="redirect", pattern="^(redirect|json)$"),
    db: Session = Depends(get_db),
    youtube_service: YouTubeService = Depends(get_youtube_service),
) -> Response:
    """Shared root-level Google OAuth callback (http://localhost:8000/auth/google/callback).

    Validates the signed, short-lived, session-bound `state` token and dispatches
    based on its verified `purpose` (`google_login` or `youtube_connect`).
    """
    session_cookie = request.cookies.get(OAUTH_SESSION_COOKIE_NAME)
    try:
        state_payload = verify_oauth_state(state, session_id=session_cookie)
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="OAuth state token has expired. Please try signing in again.",
        ) from exc
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or session-mismatched OAuth state parameter.",
        ) from exc

    purpose = state_payload["purpose"]
    if purpose == "google_login":
        resp = _handle_google_login_callback(
            code=code,
            mode=mode,
            db=db,
            youtube_service=youtube_service,
        )
    elif purpose == "youtube_connect":
        try:
            user_id = uuid.UUID(str(state_payload["uid"]))
        except (KeyError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid user identifier in OAuth state.",
            ) from exc
        resp = _handle_youtube_connect_callback(
            code=code,
            user_id=user_id,
            mode=mode,
            db=db,
            youtube_service=youtube_service,
        )
    elif purpose == "youtube_upload_connect":
        try:
            user_id = uuid.UUID(str(state_payload["uid"]))
        except (KeyError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid user identifier in OAuth state.",
            ) from exc
        resp = _handle_youtube_upload_connect_callback(
            code=code,
            user_id=user_id,
            mode=mode,
            db=db,
            youtube_service=youtube_service,
        )
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unsupported OAuth state purpose.",
        )

    _clear_oauth_session_cookie(resp)
    return resp


@router.post("/dev-login", response_model=AuthTokenResponse)
def dev_login(
    body: DevLoginRequest,
    db: Session = Depends(get_db),
) -> AuthTokenResponse:
    """Local development-only login endpoint (disabled unless ENABLE_DEV_AUTH=true)."""
    settings = get_settings()
    if not settings.enable_dev_auth or settings.app_env.lower() == "production":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Not found.",
        )

    user = db.query(User).filter(User.email == str(body.email)).first()
    if user is None:
        user = User(email=str(body.email), name=body.name)
        db.add(user)
        db.flush()

    record_audit(
        db,
        "auth.dev_login",
        user_id=user.id,
        details={"email": user.email},
    )
    db.commit()
    db.refresh(user)

    token = create_access_token(user.id, user.email)
    return AuthTokenResponse(
        access_token=token,
        user=UserResponse.model_validate(user),
    )


@router.get("/me", response_model=UserResponse)
def get_me(current_user: User = Depends(get_current_user)) -> UserResponse:
    """Return the authenticated user's profile and connected YouTube channels."""
    return UserResponse.model_validate(current_user)


@router.get("/youtube/channels", response_model=list[YouTubeChannelResponse])
def list_connected_channels(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[YouTubeChannelResponse]:
    """List the current user's connected YouTube channels."""
    channels = (
        db.query(YouTubeChannel)
        .filter(YouTubeChannel.user_id == current_user.id)
        .order_by(YouTubeChannel.connected_at.desc())
        .all()
    )
    return [YouTubeChannelResponse.model_validate(channel) for channel in channels]


@router.delete("/youtube/channels/{channel_id}", status_code=status.HTTP_204_NO_CONTENT)
def disconnect_channel(
    channel_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    """Disconnect a YouTube channel from the current user's account."""
    record = (
        db.query(YouTubeChannel)
        .filter(
            YouTubeChannel.user_id == current_user.id,
            YouTubeChannel.channel_id == channel_id,
        )
        .first()
    )
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Connected YouTube channel not found.",
        )

    db.delete(record)
    record_audit(
        db,
        "youtube.channel_disconnected",
        user_id=current_user.id,
        details={"channel_id": channel_id},
    )
    db.commit()
