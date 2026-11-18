"""Shared pytest fixtures for Clipper backend tests."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.api.deps import get_job_dispatcher, get_youtube_service
from app.config import Settings, get_settings
from app.database import get_db
from app.main import create_app
from app.models import Base, User
from app.security import create_access_token
from app.services.youtube import YOUTUBE_READONLY_SCOPE, VideoMetadata, YouTubeService


class FakeYouTubeService(YouTubeService):
    """Deterministic test double for Google OAuth and YouTube Data API v3."""

    def __init__(self, settings: Settings | None = None) -> None:
        super().__init__(settings=settings)
        self.tokens_response: dict[str, Any] = {
            "access_token": "fake-access-token",
            "refresh_token": "fake-super-secret-refresh-token",
            "scope": YOUTUBE_READONLY_SCOPE,
        }
        self.userinfo_response: dict[str, Any] = {
            "sub": "google-sub-12345",
            "email": "alice@example.com",
            "name": "Alice Creator",
            "picture": "https://example.com/alice.png",
        }
        self.owned_channels_response: list[dict[str, Any]] = [
            {
                "channel_id": "UC1234567890123456789012",
                "title": "Alice Official",
                "handle": "@aliceofficial",
                "thumbnail_url": "https://example.com/ch.png",
            }
        ]
        self.videos_by_id: dict[str, VideoMetadata] = {}

    def exchange_code_for_tokens(self, code: str, redirect_uri: str) -> dict[str, Any]:
        return dict(self.tokens_response)

    def fetch_google_userinfo(self, access_token: str) -> dict[str, Any]:
        return dict(self.userinfo_response)

    def fetch_owned_channels(self, access_token: str) -> list[dict[str, Any]]:
        return list(self.owned_channels_response)

    def fetch_video_metadata(
        self,
        video_id: str,
        *,
        access_token: str | None = None,
    ) -> VideoMetadata:
        if video_id in self.videos_by_id:
            return self.videos_by_id[video_id]
        return VideoMetadata(
            video_id=video_id,
            channel_id="UC_UNKNOWN_CHANNEL_00001",
            channel_title="Unknown Channel",
            title="Sample Video",
            license="youtube",
            duration_sec=180,
        )


@pytest.fixture()
def test_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    fernet_key = Fernet.generate_key().decode("utf-8")
    storage_dir = tmp_path / "storage"
    storage_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-key-for-clipper-suite-123456")
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", fernet_key)
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-google-client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "test-google-client-secret")
    monkeypatch.setenv("YOUTUBE_API_KEY", "test-youtube-api-key")
    monkeypatch.setenv("STORAGE_ROOT", str(storage_dir))
    monkeypatch.setenv("ENABLE_DEV_AUTH", "false")

    get_settings.cache_clear()
    settings = get_settings()
    yield settings
    get_settings.cache_clear()


@pytest.fixture()
def db_session(tmp_path: Path) -> Generator[Session, None, None]:
    db_file = tmp_path / "test_clipper.db"
    engine = create_engine(
        f"sqlite:///{db_file}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def fake_youtube(test_settings: Settings) -> FakeYouTubeService:
    return FakeYouTubeService(settings=test_settings)


@pytest.fixture()
def dispatched_jobs() -> list[str]:
    return []


@pytest.fixture()
def client(
    db_session: Session,
    fake_youtube: FakeYouTubeService,
    dispatched_jobs: list[str],
    test_settings: Settings,
) -> Generator[TestClient, None, None]:
    app = create_app()

    def _override_get_db() -> Generator[Session, None, None]:
        yield db_session

    def _fake_dispatcher(job_id) -> str:
        dispatched_jobs.append(str(job_id))
        return f"fake-task-{job_id}"

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_youtube_service] = lambda: fake_youtube
    app.dependency_overrides[get_job_dispatcher] = lambda: _fake_dispatcher

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def test_user(db_session: Session) -> User:
    user = User(
        email="creator@example.com",
        google_sub="sub-creator-1",
        name="Test Creator",
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def auth_headers(test_user: User, test_settings: Settings) -> dict[str, str]:
    token = create_access_token(test_user.id, test_user.email, settings=test_settings)
    return {"Authorization": f"Bearer {token}"}
