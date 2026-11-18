"""Phase 3 tests: Enable-uploading OAuth, approval gate, rights re-check, CC/campaign metadata, token refresh, quota, and scheduling."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import (
    AuditLog,
    Campaign,
    CampaignSource,
    Clip,
    Job,
    JobStatus,
    Permission,
    ReviewStatus,
    RightsBasis,
    Upload,
    UploadStatus,
    User,
    YouTubeChannel,
)
from app.security import TokenCipher
from app.services.youtube import (
    YOUTUBE_READONLY_SCOPE,
    YOUTUBE_UPLOAD_SCOPE,
    VideoMetadata,
    YouTubeAuthRevokedError,
    YouTubeQuotaExceededError,
)
from app.workers.tasks import dispatch_due_uploads, execute_clip_upload
from tests.conftest import FakeYouTubeService


def _create_upload_enabled_channel(
    db_session: Session,
    user: User,
    settings: Settings,
    *,
    channel_id: str = "UC_MY_SHORTS_CHANNEL_01",
    with_upload_scope: bool = True,
) -> YouTubeChannel:
    cipher = TokenCipher.from_settings(settings)
    scopes = (
        f"{YOUTUBE_READONLY_SCOPE} {YOUTUBE_UPLOAD_SCOPE}"
        if with_upload_scope
        else YOUTUBE_READONLY_SCOPE
    )
    ch = YouTubeChannel(
        user_id=user.id,
        channel_id=channel_id,
        title="Creator Shorts",
        encrypted_refresh_token=cipher.encrypt("real-secret-refresh-token"),
        scopes=scopes,
    )
    db_session.add(ch)
    db_session.commit()
    db_session.refresh(ch)
    return ch


def _create_job_and_clip(
    db_session: Session,
    user: User,
    settings: Settings,
    *,
    video_id: str = "vidUpload01",
    channel_id: str = "UC_SRC_CHANNEL_00000001",
    rights_basis: RightsBasis = RightsBasis.CC,
    review_status: ReviewStatus = ReviewStatus.APPROVED,
    campaign: Campaign | None = None,
    attribution_text: str | None = None,
) -> tuple[Job, Clip]:
    evidence = {
        "allowed": True,
        "basis": rights_basis.value,
        "requires_attribution": rights_basis == RightsBasis.CC,
    }
    if attribution_text:
        evidence["attribution_text"] = attribution_text

    job = Job(
        user_id=user.id,
        source_url=f"https://www.youtube.com/watch?v={video_id}",
        youtube_video_id=video_id,
        source_channel_id=channel_id,
        source_title="Original Source Video",
        source_duration_sec=120,
        campaign_id=campaign.id if campaign else None,
        rights_basis=rights_basis.value,
        rights_evidence_json=evidence,
        status=JobStatus.READY.value,
    )
    db_session.add(job)
    db_session.flush()

    clip_rel = f"jobs/{job.id}/clips/clip_01.mp4"
    clip_abs = settings.storage_path / clip_rel
    clip_abs.parent.mkdir(parents=True, exist_ok=True)
    clip_abs.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 512)

    clip = Clip(
        job_id=job.id,
        file_path=clip_rel,
        start_sec=0.0,
        end_sec=25.0,
        score=0.92,
        title="Highlight Clip 1",
        attribution_text=attribution_text,
        review_status=review_status.value,
    )
    db_session.add(clip)
    db_session.commit()
    db_session.refresh(job)
    db_session.refresh(clip)
    return job, clip


def test_enable_uploading_oauth_flow_uses_shared_callback_and_encrypts_token(
    client: TestClient,
    db_session: Session,
    auth_headers: dict[str, str],
    fake_youtube: FakeYouTubeService,
    test_settings: Settings,
) -> None:
    resp = client.get("/api/auth/youtube/enable-upload", headers=auth_headers)
    assert resp.status_code == 200
    auth_url = resp.json()["auth_url"]
    qs = parse_qs(urlparse(auth_url).query)
    assert qs["redirect_uri"] == ["http://localhost:8000/auth/google/callback"]
    assert YOUTUBE_UPLOAD_SCOPE in qs["scope"][0]
    assert YOUTUBE_READONLY_SCOPE in qs["scope"][0]
    state = qs["state"][0]

    fake_youtube.tokens_response["scope"] = (
        f"{YOUTUBE_READONLY_SCOPE} {YOUTUBE_UPLOAD_SCOPE}"
    )
    cb_resp = client.get(
        "/auth/google/callback",
        params={"code": "upload-code", "state": state},
        follow_redirects=False,
    )
    assert cb_resp.status_code == 302
    assert cb_resp.headers["location"] == (
        "http://localhost:3000/?youtube_upload_enabled=1&channels=1"
    )

    channels_resp = client.get("/api/auth/youtube/channels", headers=auth_headers)
    assert channels_resp.status_code == 200
    assert channels_resp.json()[0]["upload_enabled"] is True

    stored = db_session.query(YouTubeChannel).first()
    assert stored is not None
    assert stored.encrypted_refresh_token != "fake-super-secret-refresh-token"
    assert TokenCipher.from_settings(test_settings).decrypt(
        stored.encrypted_refresh_token
    ) == "fake-super-secret-refresh-token"


def test_upload_blocked_when_clip_is_not_approved(
    client: TestClient,
    db_session: Session,
    test_user: User,
    auth_headers: dict[str, str],
    fake_youtube: FakeYouTubeService,
    dispatched_uploads: list[str],
    test_settings: Settings,
) -> None:
    channel = _create_upload_enabled_channel(db_session, test_user, test_settings)
    job, clip = _create_job_and_clip(
        db_session,
        test_user,
        test_settings,
        review_status=ReviewStatus.PENDING,
    )

    # API blocks unapproved clip
    resp = client.post(
        f"/api/jobs/{job.id}/clips/{clip.id}/upload",
        headers=auth_headers,
        json={"title": "Unapproved Short"},
    )
    assert resp.status_code == 400
    assert "approved" in resp.json()["detail"].lower()
    assert dispatched_uploads == []

    # Worker defense-in-depth also blocks if an Upload row exists for a non-approved clip
    sneaky_upload = Upload(
        user_id=test_user.id,
        job_id=job.id,
        clip_id=clip.id,
        youtube_channel_id=channel.id,
        title="Sneaky",
        description="#Shorts",
        tags_json=["Shorts"],
        privacy="private",
        status=UploadStatus.QUEUED.value,
    )
    db_session.add(sneaky_upload)
    db_session.commit()

    result = execute_clip_upload(
        db_session,
        sneaky_upload.id,
        youtube_service=fake_youtube,
    )
    assert result.status == UploadStatus.FAILED.value
    assert "not approved" in (result.error or "").lower()
    assert fake_youtube.uploaded_calls == []


def test_upload_blocked_when_rights_basis_is_revoked_before_upload(
    client: TestClient,
    db_session: Session,
    test_user: User,
    auth_headers: dict[str, str],
    fake_youtube: FakeYouTubeService,
    dispatched_uploads: list[str],
    test_settings: Settings,
) -> None:
    channel = _create_upload_enabled_channel(db_session, test_user, test_settings)
    partner_channel = "UC_PARTNER_REVOKED_0001"
    perm = Permission(
        user_id=test_user.id,
        channel_id=partner_channel,
        document_path="permissions/test/doc.pdf",
        document_sha256="c" * 64,
        original_filename="signed.pdf",
        revoked=False,
    )
    db_session.add(perm)
    db_session.commit()

    job, clip = _create_job_and_clip(
        db_session,
        test_user,
        test_settings,
        video_id="permRevoke1",
        channel_id=partner_channel,
        rights_basis=RightsBasis.PERMISSION,
        review_status=ReviewStatus.APPROVED,
    )
    fake_youtube.videos_by_id["permRevoke1"] = VideoMetadata(
        video_id="permRevoke1",
        channel_id=partner_channel,
        channel_title="Partner Show",
        title="Interview",
        license="youtube",
        duration_sec=120,
    )

    # Queue upload while permission is valid
    queue_resp = client.post(
        f"/api/jobs/{job.id}/clips/{clip.id}/upload",
        headers=auth_headers,
        json={"title": "Partner Short"},
    )
    assert queue_resp.status_code == 201
    upload_id = uuid.UUID(queue_resp.json()["id"])

    # Now REVOKE the written permission before the worker executes!
    perm.revoked = True
    db_session.commit()

    # Worker must re-run live rights check and BLOCK the upload
    worker_result = execute_clip_upload(
        db_session,
        upload_id,
        youtube_service=fake_youtube,
    )
    assert worker_result.status == UploadStatus.FAILED.value
    assert "rights gate" in (worker_result.error or "").lower()
    assert fake_youtube.uploaded_calls == []

    # Subsequent API call is also rejected immediately with 403
    retry_api = client.post(
        f"/api/jobs/{job.id}/clips/{clip.id}/upload",
        headers=auth_headers,
        json={"title": "Partner Short Retry"},
    )
    assert retry_api.status_code == 403
    assert "revoked" in retry_api.json()["detail"].lower()

    audit = (
        db_session.query(AuditLog)
        .filter(AuditLog.job_id == job.id, AuditLog.action == "upload.rights_blocked")
        .first()
    )
    assert audit is not None


def test_cc_upload_automatically_includes_attribution_and_shorts_tag(
    client: TestClient,
    db_session: Session,
    test_user: User,
    auth_headers: dict[str, str],
    fake_youtube: FakeYouTubeService,
    dispatched_uploads: list[str],
    test_settings: Settings,
) -> None:
    _create_upload_enabled_channel(db_session, test_user, test_settings)
    attribution = (
        'Source: "Open Physics" by Open Lab '
        "(https://www.youtube.com/watch?v=ccShort1234) — "
        "Licensed under Creative Commons Attribution (CC BY)."
    )
    job, clip = _create_job_and_clip(
        db_session,
        test_user,
        test_settings,
        video_id="ccShort1234",
        channel_id="UC_OPEN_LAB_00000000001",
        rights_basis=RightsBasis.CC,
        review_status=ReviewStatus.APPROVED,
        attribution_text=attribution,
    )
    fake_youtube.videos_by_id["ccShort1234"] = VideoMetadata(
        video_id="ccShort1234",
        channel_id="UC_OPEN_LAB_00000000001",
        channel_title="Open Lab",
        title="Open Physics",
        license="creativeCommon",
        duration_sec=120,
    )

    resp = client.post(
        f"/api/jobs/{job.id}/clips/{clip.id}/upload",
        headers=auth_headers,
        json={
            "title": "Cool Physics Moment",
            "description": "Check out this experiment!",
            "tags": ["physics"],
        },
    )
    assert resp.status_code == 201
    created = resp.json()
    assert created["privacy"] == "private"  # default privacy is private
    assert "#Shorts" in created["description"]
    assert "Creative Commons Attribution (CC BY)" in created["description"]
    assert "Open Lab" in created["description"]
    assert "Shorts" in created["tags"]

    completed = execute_clip_upload(
        db_session,
        uuid.UUID(created["id"]),
        youtube_service=fake_youtube,
    )
    assert completed.status == UploadStatus.UPLOADED.value
    assert completed.youtube_video_id == "SHORT_ID_001"
    assert completed.posted_at is not None
    assert len(fake_youtube.uploaded_calls) == 1
    sent = fake_youtube.uploaded_calls[0]
    assert "#Shorts" in sent["description"]
    assert "Creative Commons Attribution (CC BY)" in sent["description"]
    assert sent["privacy"] == "private"


def test_campaign_upload_includes_required_campaign_hashtags(
    client: TestClient,
    db_session: Session,
    test_user: User,
    auth_headers: dict[str, str],
    fake_youtube: FakeYouTubeService,
    test_settings: Settings,
) -> None:
    _create_upload_enabled_channel(db_session, test_user, test_settings)
    campaign = Campaign(
        owner_user_id=test_user.id,
        name="Podcast Push",
        rules_text="Must include #PodcastClips and #CreatorLaunch in description.",
        deadline=datetime.now(timezone.utc) + timedelta(days=5),
        is_active=True,
    )
    db_session.add(campaign)
    db_session.flush()
    db_session.add(
        CampaignSource(
            campaign_id=campaign.id,
            source_url="https://www.youtube.com/watch?v=campShort01",
            normalized_video_id="campShort01",
            source_type="video",
        )
    )
    db_session.commit()

    job, clip = _create_job_and_clip(
        db_session,
        test_user,
        test_settings,
        video_id="campShort01",
        rights_basis=RightsBasis.CAMPAIGN,
        review_status=ReviewStatus.APPROVED,
        campaign=campaign,
    )
    fake_youtube.videos_by_id["campShort01"] = VideoMetadata(
        video_id="campShort01",
        channel_id="UC_BRAND_00000000000001",
        channel_title="Brand Pod",
        title="Episode 1",
        license="youtube",
        duration_sec=300,
    )

    resp = client.post(
        f"/api/jobs/{job.id}/clips/{clip.id}/upload",
        headers=auth_headers,
        json={
            "title": "Best Moment",
            "description": "Wild takeaway.",
            "privacy": "unlisted",
        },
    )
    assert resp.status_code == 201
    desc = resp.json()["description"]
    assert "#Shorts" in desc
    assert "#PodcastClips" in desc
    assert "#CreatorLaunch" in desc
    assert resp.json()["privacy"] == "unlisted"


def test_token_refresh_failure_revokes_upload_scope_and_marks_failed(
    db_session: Session,
    test_user: User,
    fake_youtube: FakeYouTubeService,
    test_settings: Settings,
) -> None:
    channel = _create_upload_enabled_channel(db_session, test_user, test_settings)
    job, clip = _create_job_and_clip(
        db_session,
        test_user,
        test_settings,
        video_id="refreshFail",
        rights_basis=RightsBasis.CC,
        review_status=ReviewStatus.APPROVED,
        attribution_text="CC BY Attribution",
    )
    upload = Upload(
        user_id=test_user.id,
        job_id=job.id,
        clip_id=clip.id,
        youtube_channel_id=channel.id,
        title="Refresh Fail Test",
        description="Test #Shorts",
        tags_json=["Shorts"],
        privacy="private",
        status=UploadStatus.QUEUED.value,
    )
    db_session.add(upload)
    db_session.commit()

    fake_youtube.refresh_error = YouTubeAuthRevokedError(
        "Your YouTube authorization has expired or was revoked. "
        "Click 'Enable uploading' to reconnect your channel."
    )

    result = execute_clip_upload(
        db_session,
        upload.id,
        youtube_service=fake_youtube,
    )
    assert result.status == UploadStatus.FAILED.value
    assert "revoked" in (result.error or "").lower()
    assert fake_youtube.uploaded_calls == []

    db_session.refresh(channel)
    assert channel.upload_enabled is False

    audit = (
        db_session.query(AuditLog)
        .filter(AuditLog.job_id == job.id, AuditLog.action == "upload.auth_revoked")
        .first()
    )
    assert audit is not None


def test_quota_exceeded_marks_upload_failed_with_clear_message(
    db_session: Session,
    test_user: User,
    fake_youtube: FakeYouTubeService,
    test_settings: Settings,
) -> None:
    channel = _create_upload_enabled_channel(db_session, test_user, test_settings)
    job, clip = _create_job_and_clip(
        db_session,
        test_user,
        test_settings,
        video_id="quotaVid001",
        rights_basis=RightsBasis.CC,
        review_status=ReviewStatus.APPROVED,
        attribution_text="CC BY",
    )
    fake_youtube.videos_by_id["quotaVid001"] = VideoMetadata(
        video_id="quotaVid001",
        channel_id="UC_OPEN_LAB_00000000001",
        channel_title="Open Lab",
        title="Quota Test",
        license="creativeCommon",
        duration_sec=90,
    )
    upload = Upload(
        user_id=test_user.id,
        job_id=job.id,
        clip_id=clip.id,
        youtube_channel_id=channel.id,
        title="Quota Short",
        description="Test #Shorts",
        tags_json=["Shorts"],
        privacy="private",
        status=UploadStatus.QUEUED.value,
    )
    db_session.add(upload)
    db_session.commit()

    fake_youtube.upload_error = YouTubeQuotaExceededError(
        "YouTube API daily upload quota exceeded. Please try again after the quota resets."
    )

    result = execute_clip_upload(
        db_session,
        upload.id,
        youtube_service=fake_youtube,
    )
    assert result.status == UploadStatus.FAILED.value
    assert "quota" in (result.error or "").lower()

    audit = (
        db_session.query(AuditLog)
        .filter(AuditLog.job_id == job.id, AuditLog.action == "upload.quota_exceeded")
        .first()
    )
    assert audit is not None


def test_scheduled_upload_dispatched_only_when_due(
    client: TestClient,
    db_session: Session,
    test_user: User,
    auth_headers: dict[str, str],
    fake_youtube: FakeYouTubeService,
    dispatched_uploads: list[str],
    test_settings: Settings,
) -> None:
    _create_upload_enabled_channel(db_session, test_user, test_settings)
    job, clip = _create_job_and_clip(
        db_session,
        test_user,
        test_settings,
        video_id="schedVid001",
        rights_basis=RightsBasis.CC,
        review_status=ReviewStatus.APPROVED,
        attribution_text="CC BY",
    )
    fake_youtube.videos_by_id["schedVid001"] = VideoMetadata(
        video_id="schedVid001",
        channel_id="UC_OPEN_LAB_00000000001",
        channel_title="Open Lab",
        title="Scheduled Video",
        license="creativeCommon",
        duration_sec=90,
    )

    future_time = datetime.now(timezone.utc) + timedelta(hours=2)
    resp = client.post(
        f"/api/jobs/{job.id}/clips/{clip.id}/upload",
        headers=auth_headers,
        json={
            "title": "Scheduled Short",
            "scheduled_at": future_time.isoformat(),
        },
    )
    assert resp.status_code == 201
    upload_id = uuid.UUID(resp.json()["id"])
    # Future scheduled uploads MUST NOT be dispatched immediately
    assert dispatched_uploads == []

    # Before scheduled_at: beat dispatches nothing
    beat_calls: list[uuid.UUID] = []
    dispatched_early = dispatch_due_uploads(
        db_session,
        upload_dispatcher=lambda uid: (beat_calls.append(uid) or "task-1"),
        now=future_time - timedelta(minutes=10),
    )
    assert dispatched_early == []
    assert beat_calls == []

    # Once due: beat dispatches exactly once
    dispatched_due = dispatch_due_uploads(
        db_session,
        upload_dispatcher=lambda uid: (beat_calls.append(uid) or "task-2"),
        now=future_time + timedelta(minutes=1),
    )
    assert dispatched_due == [upload_id]
    assert beat_calls == [upload_id]

    # Subsequent beat tick must NOT double-dispatch
    dispatched_again = dispatch_due_uploads(
        db_session,
        upload_dispatcher=lambda uid: (beat_calls.append(uid) or "task-3"),
        now=future_time + timedelta(minutes=2),
    )
    assert dispatched_again == []
