"""Integration tests for POST /api/jobs, GET /api/jobs, GET /api/jobs/{id}, and clip review."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import AuditLog, Clip, User, YouTubeChannel
from app.security import create_access_token
from app.services.youtube import YOUTUBE_READONLY_SCOPE, VideoMetadata
from tests.conftest import FakeYouTubeService


def test_post_jobs_rejects_invalid_url_and_never_dispatches(
    client: TestClient,
    auth_headers: dict[str, str],
    dispatched_jobs: list[str],
) -> None:
    resp = client.post(
        "/api/jobs",
        headers=auth_headers,
        json={"source_url": "https://vimeo.com/123456789"},
    )
    assert resp.status_code == 422
    assert dispatched_jobs == []


def test_post_jobs_rejects_when_no_rights_basis_matches_and_never_queues(
    client: TestClient,
    db_session: Session,
    auth_headers: dict[str, str],
    fake_youtube: FakeYouTubeService,
    dispatched_jobs: list[str],
) -> None:
    fake_youtube.videos_by_id["abc12345678"] = VideoMetadata(
        video_id="abc12345678",
        channel_id="UC_STRANGER_99999999999",
        channel_title="Stranger Media",
        title="Copyrighted Music Video",
        license="youtube",
        duration_sec=240,
    )

    resp = client.post(
        "/api/jobs",
        headers=auth_headers,
        json={"source_url": "https://www.youtube.com/watch?v=abc12345678"},
    )
    assert resp.status_code == 201
    job = resp.json()
    assert job["status"] == "rejected"
    assert job["rights_basis"] is None
    assert job["rejection_reason"]
    assert "standard YouTube license" in job["rejection_reason"]
    assert job["rights_evidence_json"]["allowed"] is False

    # Critical invariant: rejected job MUST NEVER be enqueued
    assert dispatched_jobs == []

    audit = db_session.query(AuditLog).filter(AuditLog.action == "rights.rejected").first()
    assert audit is not None


def test_post_jobs_queues_on_owner_basis(
    client: TestClient,
    db_session: Session,
    test_user: User,
    auth_headers: dict[str, str],
    fake_youtube: FakeYouTubeService,
    dispatched_jobs: list[str],
) -> None:
    db_session.add(
        YouTubeChannel(
            user_id=test_user.id,
            channel_id="UC_MY_OWN_CHANNEL_12345",
            title="Creator Main",
            encrypted_refresh_token="gAAAAAB_enc",
            scopes=YOUTUBE_READONLY_SCOPE,
        )
    )
    db_session.commit()

    fake_youtube.videos_by_id["ownVideo123"] = VideoMetadata(
        video_id="ownVideo123",
        channel_id="UC_MY_OWN_CHANNEL_12345",
        channel_title="Creator Main",
        title="My Own Tutorial",
        license="youtube",
        duration_sec=600,
    )

    resp = client.post(
        "/api/jobs",
        headers=auth_headers,
        json={"source_url": "https://youtu.be/ownVideo123"},
    )
    assert resp.status_code == 201
    job = resp.json()
    assert job["status"] == "queued"
    assert job["rights_basis"] == "owner"
    assert job["source_url"] == "https://www.youtube.com/watch?v=ownVideo123"
    assert dispatched_jobs == [job["id"]]


def test_post_jobs_queues_on_campaign_basis(
    client: TestClient,
    auth_headers: dict[str, str],
    fake_youtube: FakeYouTubeService,
    dispatched_jobs: list[str],
) -> None:
    campaign_resp = client.post(
        "/api/campaigns",
        headers=auth_headers,
        json={
            "name": "Creator Launch",
            "rules_text": "Include #Launch",
            "deadline": (datetime.now(timezone.utc) + timedelta(days=5)).isoformat(),
            "sources": [{"source_url": "https://youtu.be/campVid1234"}],
        },
    )
    campaign_id = campaign_resp.json()["id"]

    fake_youtube.videos_by_id["campVid1234"] = VideoMetadata(
        video_id="campVid1234",
        channel_id="UC_BRAND_CHANNEL_123456",
        channel_title="Brand Podcast",
        title="Launch Keynote",
        license="youtube",
        duration_sec=900,
    )

    resp = client.post(
        "/api/jobs",
        headers=auth_headers,
        json={
            "source_url": "https://www.youtube.com/watch?v=campVid1234",
            "campaign_id": campaign_id,
            "settings": {"clip_count": 4, "clip_duration_sec": 25},
        },
    )
    assert resp.status_code == 201
    job = resp.json()
    assert job["status"] == "queued"
    assert job["rights_basis"] == "campaign"
    assert job["campaign_id"] == campaign_id
    assert job["settings_json"]["clip_count"] == 4
    assert dispatched_jobs == [job["id"]]


def test_post_jobs_queues_on_cc_basis_with_attribution(
    client: TestClient,
    auth_headers: dict[str, str],
    fake_youtube: FakeYouTubeService,
    dispatched_jobs: list[str],
) -> None:
    fake_youtube.videos_by_id["ccVid123456"] = VideoMetadata(
        video_id="ccVid123456",
        channel_id="UC_OPEN_EDU_12345678901",
        channel_title="Open Lectures",
        title="Intro to Distributed Systems",
        license="creativeCommon",
        duration_sec=1200,
    )

    resp = client.post(
        "/api/jobs",
        headers=auth_headers,
        json={"source_url": "https://www.youtube.com/watch?v=ccVid123456"},
    )
    assert resp.status_code == 201
    job = resp.json()
    assert job["status"] == "queued"
    assert job["rights_basis"] == "cc"
    assert job["rights_evidence_json"]["requires_attribution"] is True
    assert "Open Lectures" in job["rights_evidence_json"]["attribution_text"]
    assert dispatched_jobs == [job["id"]]


def test_post_jobs_queues_on_permission_basis_and_lists_and_fetches_job(
    client: TestClient,
    db_session: Session,
    auth_headers: dict[str, str],
    fake_youtube: FakeYouTubeService,
    dispatched_jobs: list[str],
    test_settings: Settings,
) -> None:
    upload_resp = client.post(
        "/api/permissions",
        headers=auth_headers,
        data={"channel_id": "UC_SIGNED_PARTNER_12345"},
        files={"file": ("agreement.pdf", b"%PDF-1.4 permission", "application/pdf")},
    )
    assert upload_resp.status_code == 201

    fake_youtube.videos_by_id["permVid1234"] = VideoMetadata(
        video_id="permVid1234",
        channel_id="UC_SIGNED_PARTNER_12345",
        channel_title="Partner Show",
        title="Interview Ep 42",
        license="youtube",
        duration_sec=400,
    )

    create_resp = client.post(
        "/api/jobs",
        headers=auth_headers,
        json={"source_url": "https://www.youtube.com/watch?v=permVid1234"},
    )
    assert create_resp.status_code == 201
    job_id = create_resp.json()["id"]
    assert create_resp.json()["rights_basis"] == "permission"
    assert dispatched_jobs == [job_id]

    # GET /api/jobs
    list_resp = client.get("/api/jobs", headers=auth_headers)
    assert list_resp.status_code == 200
    assert len(list_resp.json()) == 1

    # GET /api/jobs/{id}
    get_resp = client.get(f"/api/jobs/{job_id}", headers=auth_headers)
    assert get_resp.status_code == 200
    assert get_resp.json()["id"] == job_id

    # Another user cannot read this job
    other_user = User(email="other@example.com", name="Other")
    db_session.add(other_user)
    db_session.commit()
    other_token = create_access_token(other_user.id, other_user.email, settings=test_settings)
    forbidden_resp = client.get(
        f"/api/jobs/{job_id}",
        headers={"Authorization": f"Bearer {other_token}"},
    )
    assert forbidden_resp.status_code == 404

    # Clip review status update works
    import uuid

    clip = Clip(
        job_id=uuid.UUID(job_id),
        file_path="clips/test/clip_1.mp4",
        start_sec=0.0,
        end_sec=30.0,
        score=0.85,
        title="Clip 1",
    )
    db_session.add(clip)
    db_session.commit()
    db_session.refresh(clip)

    review_resp = client.patch(
        f"/api/jobs/{job_id}/clips/{clip.id}/review",
        headers=auth_headers,
        json={"review_status": "approved"},
    )
    assert review_resp.status_code == 200
    assert review_resp.json()["review_status"] == "approved"
