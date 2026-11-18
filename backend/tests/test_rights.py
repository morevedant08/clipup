"""Comprehensive unit tests for the non-negotiable Rights Gate (rights.py)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.models.campaign import Campaign, CampaignSource
from app.models.job import Job, JobStatus, RightsBasis
from app.models.permission import Permission
from app.models.youtube_channel import YouTubeChannel
from app.services.rights import (
    RightsGateError,
    assert_job_rights_approved,
    evaluate_rights,
)
from app.services.youtube import VideoMetadata


def _make_video(
    *,
    video_id: str = "dQw4w9WgXcQ",
    channel_id: str = "UC_CREATOR_000000000001",
    channel_title: str = "Creator Studio",
    title: str = "How to Build Rights-Safe Video Tools",
    license_name: str = "youtube",
    duration_sec: int = 300,
) -> VideoMetadata:
    return VideoMetadata(
        video_id=video_id,
        channel_id=channel_id,
        channel_title=channel_title,
        title=title,
        license=license_name,
        duration_sec=duration_sec,
    )


def _make_connected_channel(channel_id: str, title: str = "My Channel") -> YouTubeChannel:
    return YouTubeChannel(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        channel_id=channel_id,
        title=title,
        encrypted_refresh_token="gAAAAAB_encrypted",
        scopes="https://www.googleapis.com/auth/youtube.readonly",
    )


def _make_campaign(
    *,
    name: str = "Paid Podcast Campaign",
    video_ids: list[str] | None = None,
    channel_ids: list[str] | None = None,
    is_active: bool = True,
    deadline: datetime | None = None,
    rules_text: str = "Add campaign hashtag.",
) -> Campaign:
    campaign_id = uuid.uuid4()
    campaign = Campaign(
        id=campaign_id,
        owner_user_id=uuid.uuid4(),
        name=name,
        rules_text=rules_text,
        is_active=is_active,
        deadline=deadline,
    )
    sources: list[CampaignSource] = []
    for vid in video_ids or []:
        sources.append(
            CampaignSource(
                id=uuid.uuid4(),
                campaign_id=campaign_id,
                source_url=f"https://www.youtube.com/watch?v={vid}",
                normalized_video_id=vid,
                source_type="video",
            )
        )
    for ch_id in channel_ids or []:
        sources.append(
            CampaignSource(
                id=uuid.uuid4(),
                campaign_id=campaign_id,
                source_url=f"https://www.youtube.com/channel/{ch_id}",
                source_channel_id=ch_id,
                source_type="channel",
            )
        )
    campaign.sources = sources
    return campaign


def _make_permission(
    channel_id: str,
    *,
    revoked: bool = False,
    expires_at: datetime | None = None,
    document_path: str = "permissions/user/doc.pdf",
    document_sha256: str = "b" * 64,
) -> Permission:
    return Permission(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        channel_id=channel_id,
        channel_name="Partner Channel",
        document_path=document_path,
        document_sha256=document_sha256,
        original_filename="signed_permission.pdf",
        revoked=revoked,
        expires_at=expires_at,
    )


# --- Basis 1: owner ---


def test_rights_pass_owner_when_channel_matches() -> None:
    video = _make_video(channel_id="UC_OWNED_11111111111111")
    channel = _make_connected_channel("UC_OWNED_11111111111111", title="Alice Official")

    decision = evaluate_rights(video=video, connected_channels=[channel])

    assert decision.allowed is True
    assert decision.basis == RightsBasis.OWNER
    assert decision.requires_attribution is False
    assert decision.attribution_text is None
    assert decision.evidence["allowed"] is True
    assert decision.evidence["basis"] == "owner"
    assert decision.evidence["matched_channel_id"] == "UC_OWNED_11111111111111"


def test_rights_reject_when_connected_channel_does_not_match() -> None:
    video = _make_video(channel_id="UC_STRANGER_00000000000")
    channel = _make_connected_channel("UC_OWNED_11111111111111")

    decision = evaluate_rights(video=video, connected_channels=[channel])

    assert decision.allowed is False
    assert decision.basis is None
    assert "UC_STRANGER_00000000000" in decision.reason
    assert decision.evidence["allowed"] is False


# --- Basis 2: campaign ---


def test_rights_pass_campaign_when_video_listed_in_active_campaign() -> None:
    video = _make_video(video_id="9bZkp7q19f0")
    future_deadline = datetime.now(timezone.utc) + timedelta(days=7)
    campaign = _make_campaign(
        video_ids=["dQw4w9WgXcQ", "9bZkp7q19f0"],
        deadline=future_deadline,
    )

    decision = evaluate_rights(video=video, connected_channels=[], campaign=campaign)

    assert decision.allowed is True
    assert decision.basis == RightsBasis.CAMPAIGN
    assert decision.evidence["campaign_id"] == str(campaign.id)
    assert "9bZkp7q19f0" in decision.evidence["matched_source_url"]


def test_rights_pass_campaign_when_channel_source_listed() -> None:
    video = _make_video(channel_id="UC_CAMP_CHANNEL_0000000")
    campaign = _make_campaign(channel_ids=["UC_CAMP_CHANNEL_0000000"])

    decision = evaluate_rights(video=video, connected_channels=[], campaign=campaign)

    assert decision.allowed is True
    assert decision.basis == RightsBasis.CAMPAIGN


def test_rights_reject_campaign_when_video_not_in_campaign_sources() -> None:
    video = _make_video(video_id="NOT_IN_LIST")
    campaign = _make_campaign(video_ids=["dQw4w9WgXcQ"])

    decision = evaluate_rights(video=video, connected_channels=[], campaign=campaign)

    assert decision.allowed is False
    assert decision.basis is None
    assert "not listed in the authorized sources" in decision.reason


def test_rights_reject_campaign_when_deadline_expired() -> None:
    video = _make_video(video_id="dQw4w9WgXcQ")
    past_deadline = datetime.now(timezone.utc) - timedelta(hours=2)
    campaign = _make_campaign(video_ids=["dQw4w9WgXcQ"], deadline=past_deadline)

    decision = evaluate_rights(video=video, connected_channels=[], campaign=campaign)

    assert decision.allowed is False
    assert decision.basis is None
    assert "expired" in decision.reason.lower()


def test_rights_reject_campaign_when_inactive() -> None:
    video = _make_video(video_id="dQw4w9WgXcQ")
    campaign = _make_campaign(video_ids=["dQw4w9WgXcQ"], is_active=False)

    decision = evaluate_rights(video=video, connected_channels=[], campaign=campaign)

    assert decision.allowed is False
    assert "inactive" in decision.reason.lower()


# --- Basis 3: Creative Commons (cc) ---


def test_rights_pass_cc_and_requires_attribution() -> None:
    video = _make_video(
        video_id="ccVideo1234",
        channel_title="Open Science Channel",
        title="Quantum Computing Explained",
        license_name="creativeCommon",
    )

    decision = evaluate_rights(video=video, connected_channels=[])

    assert decision.allowed is True
    assert decision.basis == RightsBasis.CC
    assert decision.requires_attribution is True
    assert decision.attribution_text is not None
    assert "Open Science Channel" in decision.attribution_text
    assert "Creative Commons" in decision.attribution_text
    assert decision.evidence["requires_attribution"] is True
    assert decision.evidence["attribution_text"] == decision.attribution_text


def test_rights_reject_standard_youtube_license_without_other_basis() -> None:
    video = _make_video(license_name="youtube")

    decision = evaluate_rights(video=video, connected_channels=[])

    assert decision.allowed is False
    assert decision.basis is None
    assert "standard YouTube license" in decision.reason


# --- Basis 4: written permission ---


def test_rights_pass_permission_when_valid_document_uploaded() -> None:
    video = _make_video(channel_id="UC_PARTNER_000000000000")
    perm = _make_permission(
        "UC_PARTNER_000000000000",
        expires_at=datetime.now(timezone.utc) + timedelta(days=30),
    )

    decision = evaluate_rights(
        video=video,
        connected_channels=[],
        permissions=[perm],
    )

    assert decision.allowed is True
    assert decision.basis == RightsBasis.PERMISSION
    assert decision.evidence["permission_id"] == str(perm.id)
    assert decision.evidence["document_sha256"] == perm.document_sha256


def test_rights_reject_revoked_permission() -> None:
    video = _make_video(channel_id="UC_PARTNER_000000000000")
    perm = _make_permission("UC_PARTNER_000000000000", revoked=True)

    decision = evaluate_rights(video=video, connected_channels=[], permissions=[perm])

    assert decision.allowed is False
    assert "revoked" in decision.reason.lower()


def test_rights_reject_expired_permission() -> None:
    video = _make_video(channel_id="UC_PARTNER_000000000000")
    perm = _make_permission(
        "UC_PARTNER_000000000000",
        expires_at=datetime.now(timezone.utc) - timedelta(days=1),
    )

    decision = evaluate_rights(video=video, connected_channels=[], permissions=[perm])

    assert decision.allowed is False
    assert "expired" in decision.reason.lower()


def test_rights_reject_permission_for_different_channel() -> None:
    video = _make_video(channel_id="UC_OTHER_CHANNEL_000000")
    perm = _make_permission("UC_PARTNER_000000000000")

    decision = evaluate_rights(video=video, connected_channels=[], permissions=[perm])

    assert decision.allowed is False


# --- Edge cases & defense-in-depth gate ---


def test_rights_reject_incomplete_video_metadata() -> None:
    video = _make_video(channel_id="")
    decision = evaluate_rights(video=video, connected_channels=[])
    assert decision.allowed is False
    assert "incomplete video metadata" in decision.reason.lower()


def test_rights_priority_owner_beats_cc() -> None:
    video = _make_video(
        channel_id="UC_OWNED_11111111111111",
        license_name="creativeCommon",
    )
    channel = _make_connected_channel("UC_OWNED_11111111111111")

    decision = evaluate_rights(video=video, connected_channels=[channel])
    assert decision.allowed is True
    assert decision.basis == RightsBasis.OWNER
    assert decision.requires_attribution is False


def test_assert_job_rights_approved_blocks_unapproved_jobs() -> None:
    rejected_job = Job(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        source_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        youtube_video_id="dQw4w9WgXcQ",
        rights_basis=None,
        rights_evidence_json={"allowed": False},
        status=JobStatus.REJECTED.value,
    )
    with pytest.raises(RightsGateError):
        assert_job_rights_approved(rejected_job)

    tampered_job = Job(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        source_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        youtube_video_id="dQw4w9WgXcQ",
        rights_basis=RightsBasis.CC.value,
        rights_evidence_json={"allowed": True, "basis": "cc"},  # missing attribution_text!
        status=JobStatus.QUEUED.value,
    )
    with pytest.raises(RightsGateError):
        assert_job_rights_approved(tampered_job)

    valid_job = Job(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        source_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        youtube_video_id="dQw4w9WgXcQ",
        rights_basis=RightsBasis.OWNER.value,
        rights_evidence_json={"allowed": True, "basis": "owner"},
        status=JobStatus.QUEUED.value,
    )
    assert_job_rights_approved(valid_job)

