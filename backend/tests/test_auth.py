"""Tests for Google OAuth login, read-only YouTube connection, and token encryption."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import AuditLog, YouTubeChannel
from app.security import TokenCipher
from app.services.youtube import YOUTUBE_READONLY_SCOPE
from tests.conftest import FakeYouTubeService


def test_google_login_and_callback_issues_jwt(client: TestClient) -> None:
    login_resp = client.get("/api/auth/google/login")
    assert login_resp.status_code == 200
    auth_url = login_resp.json()["auth_url"]
    parsed = urlparse(auth_url)
    qs = parse_qs(parsed.query)
    state = qs["state"][0]
    assert "youtube.upload" not in qs["scope"][0]

    cb_resp = client.get(
        "/api/auth/google/callback",
        params={"code": "mock-code", "state": state, "mode": "json"},
    )
    assert cb_resp.status_code == 200
    body = cb_resp.json()
    assert body["access_token"]
    assert body["user"]["email"] == "alice@example.com"

    me_resp = client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {body['access_token']}"},
    )
    assert me_resp.status_code == 200
    assert me_resp.json()["email"] == "alice@example.com"


def test_youtube_connect_requests_readonly_scope_only_and_encrypts_refresh_token(
    client: TestClient,
    db_session: Session,
    auth_headers: dict[str, str],
    test_settings: Settings,
) -> None:
    connect_resp = client.get("/api/auth/youtube/connect", headers=auth_headers)
    assert connect_resp.status_code == 200
    auth_url = connect_resp.json()["auth_url"]
    qs = parse_qs(urlparse(auth_url).query)
    assert qs["scope"] == [YOUTUBE_READONLY_SCOPE]
    assert "youtube.upload" not in auth_url
    state = qs["state"][0]

    cb_resp = client.get(
        "/api/auth/youtube/callback",
        params={"code": "yt-code", "state": state, "mode": "json"},
    )
    assert cb_resp.status_code == 200
    channels_json = cb_resp.json()
    assert len(channels_json) == 1
    assert channels_json[0]["channel_id"] == "UC1234567890123456789012"
    # API response must never leak refresh token
    assert "refresh_token" not in channels_json[0]
    assert "encrypted_refresh_token" not in channels_json[0]

    # DB must store encrypted ciphertext, not plaintext
    stored = db_session.query(YouTubeChannel).first()
    assert stored is not None
    assert stored.encrypted_refresh_token != "fake-super-secret-refresh-token"
    assert "fake-super-secret-refresh-token" not in stored.encrypted_refresh_token

    cipher = TokenCipher.from_settings(test_settings)
    assert cipher.decrypt(stored.encrypted_refresh_token) == "fake-super-secret-refresh-token"

    # Audit log must record connection without any token secrets
    audit = db_session.query(AuditLog).filter(AuditLog.action == "youtube.channel_connected").first()
    assert audit is not None
    assert "fake-super-secret-refresh-token" not in str(audit.details_json)


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
        "/api/auth/youtube/callback",
        params={"code": "yt-code", "state": state, "mode": "json"},
    )
    assert cb_resp.status_code == 400
    assert "read-only" in cb_resp.json()["detail"].lower()


def test_dev_login_disabled_by_default(client: TestClient) -> None:
    resp = client.post("/api/auth/dev-login", json={"email": "dev@example.com"})
    assert resp.status_code == 404

