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


# --- Source file upload path tests ---

_VALID_MP4_BYTES = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2mp41" + b"\x00" * 64
_VALID_WEBM_BYTES = b"\x1a\x45\xdf\xa3\x9f\x42\x86\x81\x01" + b"\x00" * 64


def test_source_upload_discarded_when_rights_gate_rejects_url(
    client: TestClient,
    db_session: Session,
    auth_headers: dict[str, str],
    fake_youtube: FakeYouTubeService,
    dispatched_jobs: list[str],
    test_settings: Settings,
) -> None:
    fake_youtube.videos_by_id["noRights123"] = VideoMetadata(
        video_id="noRights123",
        channel_id="UC_STRANGER_88888888888",
        channel_title="Stranger",
        title="Copyrighted Video",
        license="youtube",
        duration_sec=120,
    )

    resp = client.post(
        "/api/jobs",
        headers=auth_headers,
        data={"source_url": "https://www.youtube.com/watch?v=noRights123"},
        files={"source_file": ("source.mp4", _VALID_MP4_BYTES, "video/mp4")},
    )
    assert resp.status_code == 201
    job = resp.json()
    assert job["status"] == "rejected"
    assert job["rights_basis"] is None
    assert job["source_mode"] == "url"
    assert dispatched_jobs == []

    # Uploaded file must NOT be persisted when rights fail
    uploads_dir = test_settings.storage_path / "uploads"
    assert not uploads_dir.exists() or list(uploads_dir.rglob("*")) == []

    audit = (
        db_session.query(AuditLog)
        .filter(AuditLog.action == "rights.rejected")
        .order_by(AuditLog.created_at.desc())
        .first()
    )
    assert audit is not None
    assert audit.details_json["source_upload_discarded"] is True


def test_source_upload_accepted_after_rights_pass_and_audited(
    client: TestClient,
    db_session: Session,
    auth_headers: dict[str, str],
    fake_youtube: FakeYouTubeService,
    dispatched_jobs: list[str],
    test_settings: Settings,
) -> None:
    fake_youtube.videos_by_id["ccUpload123"] = VideoMetadata(
        video_id="ccUpload123",
        channel_id="UC_OPEN_EDU_12345678901",
        channel_title="Open Lectures",
        title="CC Lecture",
        license="creativeCommon",
        duration_sec=300,
    )

    resp = client.post(
        "/api/jobs",
        headers=auth_headers,
        data={
            "source_url": "https://www.youtube.com/watch?v=ccUpload123",
            "clip_count": "2",
            "clip_duration_sec": "20",
        },
        files={"source_file": ("lecture.webm", _VALID_WEBM_BYTES, "video/webm")},
    )
    assert resp.status_code == 201
    job = resp.json()
    assert job["status"] == "queued"
    assert job["rights_basis"] == "cc"
    assert job["source_mode"] == "uploaded_file"
    assert job["uploaded_filename"] == "lecture.webm"
    # Internal filesystem path must not be exposed in API response
    assert "uploaded_source_path" not in job["settings_json"]
    assert dispatched_jobs == [job["id"]]

    stored_file = test_settings.storage_path / "uploads" / job["id"] / "source.webm"
    assert stored_file.is_file()
    assert stored_file.read_bytes() == _VALID_WEBM_BYTES

    upload_audit = (
        db_session.query(AuditLog)
        .filter(AuditLog.action == "job.source_uploaded")
        .first()
    )
    assert upload_audit is not None
    assert upload_audit.details_json["source_mode"] == "uploaded_file"
    assert upload_audit.details_json["original_filename"] == "lecture.webm"
    assert upload_audit.details_json["size_bytes"] == len(_VALID_WEBM_BYTES)
    assert len(upload_audit.details_json["sha256"]) == 64


def test_source_upload_rejects_invalid_extension_fake_header_and_oversized_file(
    client: TestClient,
    auth_headers: dict[str, str],
    fake_youtube: FakeYouTubeService,
    dispatched_jobs: list[str],
    test_settings: Settings,
    monkeypatch,
) -> None:
    from app.config import get_settings

    fake_youtube.videos_by_id["ccVal123456"] = VideoMetadata(
        video_id="ccVal123456",
        channel_id="UC_OPEN_EDU_12345678901",
        channel_title="Open Lectures",
        title="Validation Target",
        license="creativeCommon",
        duration_sec=120,
    )

    # 1. Unsupported extension
    bad_ext = client.post(
        "/api/jobs",
        headers=auth_headers,
        data={"source_url": "https://www.youtube.com/watch?v=ccVal123456"},
        files={"source_file": ("notes.txt", b"hello world", "text/plain")},
    )
    assert bad_ext.status_code == 400
    assert dispatched_jobs == []

    # 2. Disguised file with .mp4 extension but non-MP4 header bytes
    fake_mp4 = client.post(
        "/api/jobs",
        headers=auth_headers,
        data={"source_url": "https://www.youtube.com/watch?v=ccVal123456"},
        files={"source_file": ("fake.mp4", b"<html>not a video</html>", "video/mp4")},
    )
    assert fake_mp4.status_code == 400
    assert dispatched_jobs == []

    # 3. Empty file
    empty_mp4 = client.post(
        "/api/jobs",
        headers=auth_headers,
        data={"source_url": "https://www.youtube.com/watch?v=ccVal123456"},
        files={"source_file": ("empty.mp4", b"", "video/mp4")},
    )
    assert empty_mp4.status_code == 400

    # 4. Exceeds MAX_UPLOAD_SIZE_MB (set cap to 1 MB and upload > 1 MB)
    monkeypatch.setenv("MAX_UPLOAD_SIZE_MB", "1")
    get_settings.cache_clear()
    oversized_bytes = _VALID_MP4_BYTES + (b"\x00" * (1024 * 1024 + 128))
    too_big = client.post(
        "/api/jobs",
        headers=auth_headers,
        data={"source_url": "https://www.youtube.com/watch?v=ccVal123456"},
        files={"source_file": ("huge.mp4", oversized_bytes, "video/mp4")},
    )
    assert too_big.status_code == 413
    assert dispatched_jobs == []

    # Ensure no partial/stale uploaded files remain on disk
    uploads_dir = test_settings.storage_path / "uploads"
    assert not uploads_dir.exists() or [p for p in uploads_dir.rglob("*") if p.is_file()] == []
