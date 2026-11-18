"""Tests for Google OAuth login, read-only YouTube connect, state hardening, CORS, and token encryption."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import jwt
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import AuditLog, YouTubeChannel
from app.security import OAUTH_SESSION_COOKIE_NAME, TokenCipher
from app.services.youtube import YOUTUBE_READONLY_SCOPE
from tests.conftest import FakeYouTubeService


def test_google_login_uses_exact_root_callback_and_redirects_to_frontend(
    client: TestClient,
) -> None:
    login_resp = client.get(
        "/api/auth/google/login",
        headers={"Origin": "http://localhost:3000"},
    )
    assert login_resp.status_code == 200
    assert login_resp.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert login_resp.headers["access-control-allow-credentials"] == "true"
    assert OAUTH_SESSION_COOKIE_NAME in login_resp.cookies

    auth_url = login_resp.json()["auth_url"]
    qs = parse_qs(urlparse(auth_url).query)
    assert qs["redirect_uri"] == ["http://localhost:8000/auth/google/callback"]
    assert "youtube.upload" not in qs["scope"][0]
    state = qs["state"][0]

    # Default redirect mode sends browser back to frontend callback with JWT in fragment
    redirect_resp = client.get(
        "/auth/google/callback",
        params={"code": "mock-code", "state": state},
        follow_redirects=False,
    )
    assert redirect_resp.status_code == 302
    location = redirect_resp.headers["location"]
    assert location.startswith("http://localhost:3000/auth/callback#token=")
    token = location.split("#token=", 1)[1]

    me_resp = client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert me_resp.status_code == 200
    assert me_resp.json()["email"] == "alice@example.com"


def test_youtube_connect_uses_shared_root_callback_and_encrypts_refresh_token(
    client: TestClient,
    db_session: Session,
    auth_headers: dict[str, str],
    test_settings: Settings,
) -> None:
    connect_resp = client.get(
        "/api/auth/youtube/connect",
        headers={**auth_headers, "Origin": "http://localhost:3000"},
    )
    assert connect_resp.status_code == 200
    assert connect_resp.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert connect_resp.headers["access-control-allow-credentials"] == "true"
    assert OAUTH_SESSION_COOKIE_NAME in connect_resp.cookies

    auth_url = connect_resp.json()["auth_url"]
    qs = parse_qs(urlparse(auth_url).query)
    assert qs["redirect_uri"] == ["http://localhost:8000/auth/google/callback"]
    assert qs["scope"] == [YOUTUBE_READONLY_SCOPE]
    assert "youtube.upload" not in auth_url
    state = qs["state"][0]

    # Verify redirect back to frontend works on default mode
    cb_resp = client.get(
        "/auth/google/callback",
        params={"code": "yt-code", "state": state},
        follow_redirects=False,
    )
    assert cb_resp.status_code == 302
    assert cb_resp.headers["location"] == (
        "http://localhost:3000/?youtube_connected=1&channels=1"
    )

    # API list endpoint must never leak refresh tokens
    list_resp = client.get("/api/auth/youtube/channels", headers=auth_headers)
    assert list_resp.status_code == 200
    channels_json = list_resp.json()
    assert len(channels_json) == 1
    assert channels_json[0]["channel_id"] == "UC1234567890123456789012"
    assert "refresh_token" not in channels_json[0]
    assert "encrypted_refresh_token" not in channels_json[0]

    # DB must store Fernet-encrypted ciphertext (using env TOKEN_ENCRYPTION_KEY), not plaintext
    stored = db_session.query(YouTubeChannel).first()
    assert stored is not None
    assert stored.encrypted_refresh_token != "fake-super-secret-refresh-token"
    assert "fake-super-secret-refresh-token" not in stored.encrypted_refresh_token

    cipher = TokenCipher.from_settings(test_settings)
    assert cipher.decrypt(stored.encrypted_refresh_token) == "fake-super-secret-refresh-token"

    # Audit log must record connection without leaking refresh token
    audit = (
        db_session.query(AuditLog)
        .filter(AuditLog.action == "youtube.channel_connected")
        .first()
    )
    assert audit is not None
    assert "fake-super-secret-refresh-token" not in str(audit.details_json)


def test_oauth_callback_rejects_tampered_state(client: TestClient) -> None:
    login_resp = client.get("/api/auth/google/login")
    state = parse_qs(urlparse(login_resp.json()["auth_url"]).query)["state"][0]
    # Flip last character of signature to tamper with token
    tampered_state = state[:-1] + ("A" if state[-1] != "A" else "B")

    resp = client.get(
        "/auth/google/callback",
        params={"code": "mock-code", "state": tampered_state},
    )
    assert resp.status_code == 400
    assert "invalid" in resp.json()["detail"].lower()


def test_oauth_callback_rejects_expired_state(
    client: TestClient,
    test_settings: Settings,
) -> None:
    session_id = "test-session-expired-123"
    past = datetime.now(timezone.utc) - timedelta(minutes=15)
    expired_state = jwt.encode(
        {
            "type": "oauth_state",
            "purpose": "google_login",
            "sid": session_id,
            "iat": int((past - timedelta(minutes=10)).timestamp()),
            "exp": int(past.timestamp()),
        },
        test_settings.jwt_secret_key,
        algorithm=test_settings.jwt_algorithm,
    )
    client.cookies.set(OAUTH_SESSION_COOKIE_NAME, session_id)
    resp = client.get(
        "/auth/google/callback",
        params={"code": "mock-code", "state": expired_state},
    )
    assert resp.status_code == 400
    assert "expired" in resp.json()["detail"].lower()


def test_oauth_callback_rejects_missing_or_unknown_purpose(
    client: TestClient,
    test_settings: Settings,
) -> None:
    session_id = "test-session-purpose-123"
    now = datetime.now(timezone.utc)
    client.cookies.set(OAUTH_SESSION_COOKIE_NAME, session_id)

    missing_purpose_state = jwt.encode(
        {
            "type": "oauth_state",
            "sid": session_id,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=5)).timestamp()),
        },
        test_settings.jwt_secret_key,
        algorithm=test_settings.jwt_algorithm,
    )
    resp_missing = client.get(
        "/auth/google/callback",
        params={"code": "mock-code", "state": missing_purpose_state},
    )
    assert resp_missing.status_code == 400

    unknown_purpose_state = jwt.encode(
        {
            "type": "oauth_state",
            "purpose": "unexpected_admin_flow",
            "sid": session_id,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=5)).timestamp()),
        },
        test_settings.jwt_secret_key,
        algorithm=test_settings.jwt_algorithm,
    )
    resp_unknown = client.get(
        "/auth/google/callback",
        params={"code": "mock-code", "state": unknown_purpose_state},
    )
    assert resp_unknown.status_code == 400


def test_oauth_callback_rejects_missing_or_mismatched_session_cookie(
    client: TestClient,
) -> None:
    login_resp = client.get("/api/auth/google/login")
    state = parse_qs(urlparse(login_resp.json()["auth_url"]).query)["state"][0]

    # Clear cookies -> missing session binding
    client.cookies.clear()
    resp_no_cookie = client.get(
        "/auth/google/callback",
        params={"code": "mock-code", "state": state},
    )
    assert resp_no_cookie.status_code == 400

    # Wrong session cookie -> CSRF attempt
    client.cookies.set(OAUTH_SESSION_COOKIE_NAME, "attacker-different-session-id")
    resp_wrong_cookie = client.get(
        "/auth/google/callback",
        params={"code": "mock-code", "state": state},
    )
    assert resp_wrong_cookie.status_code == 400


def test_youtube_callback_rejects_upload_scope(
    client: TestClient,
    auth_headers: dict[str, str],
    fake_youtube: FakeYouTubeService,
) -> None:
    connect_resp = client.get("/api/auth/youtube/connect", headers=auth_headers)
    state = parse_qs(urlparse(connect_resp.json()["auth_url"]).query)["state"][0]

    fake_youtube.tokens_response["scope"] = (
        f"{YOUTUBE_READONLY_SCOPE} https://www.googleapis.com/auth/youtube.upload"
    )
    cb_resp = client.get(
        "/auth/google/callback",
        params={"code": "yt-code", "state": state, "mode": "json"},
    )
    assert cb_resp.status_code == 400
    assert "read-only" in cb_resp.json()["detail"].lower()


def test_dev_login_disabled_by_default(client: TestClient) -> None:
    resp = client.post("/api/auth/dev-login", json={"email": "dev@example.com"})
    assert resp.status_code == 404
