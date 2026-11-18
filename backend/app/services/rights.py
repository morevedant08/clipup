"""NON-NEGOTIABLE RIGHTS GATE: verifies clipping rights across the 4 allowed bases.

Allowed rights bases:
1. owner      — video.channel_id matches one of the user's connected YouTube channels
2. campaign   — video URL/ID (or channel) is listed in the selected active campaign's sources
3. cc         — YouTube videos.list (part=status) returns license == "creativeCommon"
                (requires attribution text on every clip)
4. permission — user uploaded a valid, non-revoked, non-expired written permission document
                covering video.channel_id

No clipping job may be queued, downloaded, or uploaded unless it passes this gate.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session, selectinload

from app.models.campaign import Campaign
from app.models.job import Job, RightsBasis
from app.models.permission import Permission
from app.models.user import User
from app.models.youtube_channel import YouTubeChannel
from app.services.metadata import build_cc_attribution
from app.services.rules import evaluate_campaign_eligibility
from app.services.youtube import VideoMetadata, YouTubeService

ALLOWED_RIGHTS_BASES: frozenset[str] = frozenset(b.value for b in RightsBasis)


class RightsGateError(PermissionError):
    """Raised when a job attempts to download/process/upload without valid rights."""


@dataclass(frozen=True, slots=True)
class RightsDecision:
    """Structured outcome of a Rights Gate check."""

    allowed: bool
    basis: RightsBasis | None
    reason: str
    requires_attribution: bool
    attribution_text: str | None
    evidence: dict[str, Any]


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def evaluate_rights(
    *,
    video: VideoMetadata,
    connected_channels: Sequence[YouTubeChannel],
    campaign: Campaign | None = None,
    permissions: Sequence[Permission] = (),
    now: datetime | None = None,
) -> RightsDecision:
    """Evaluate the 4 legal rights bases for clipping a YouTube video.

    Returns a RightsDecision containing the winning basis (if allowed),
    attribution requirements (always True for Creative Commons), a clear
    human-readable reason, and structured audit evidence.
    """
    checked_at = _ensure_utc(now or datetime.now(timezone.utc))
    checks_evaluated: dict[str, Any] = {}

    if not video.video_id or not video.channel_id:
        reason = (
            "Rights check failed: incomplete video metadata returned by YouTube "
            "(missing video ID or channel ID)."
        )
        return RightsDecision(
            allowed=False,
            basis=None,
            reason=reason,
            requires_attribution=False,
            attribution_text=None,
            evidence={
                "allowed": False,
                "basis": None,
                "checked_at": checked_at.isoformat(),
                "video_id": video.video_id,
                "channel_id": video.channel_id,
                "license": video.license,
                "reason": reason,
                "checks_evaluated": checks_evaluated,
            },
        )

    base_evidence: dict[str, Any] = {
        "checked_at": checked_at.isoformat(),
        "video_id": video.video_id,
        "video_title": video.title,
        "channel_id": video.channel_id,
        "channel_title": video.channel_title,
        "license": video.license,
        "privacy_status": video.privacy_status,
    }

    # 1. OWNER: video's channelId matches one of the user's connected YouTube channels
    matched_channel = next(
        (ch for ch in connected_channels if ch.channel_id == video.channel_id),
        None,
    )
    if matched_channel is not None:
        reason = (
            f"Verified channel ownership: video belongs to your connected YouTube "
            f"channel '{matched_channel.title}' ({matched_channel.channel_id})."
        )
        checks_evaluated["owner"] = {
            "passed": True,
            "connected_channel_id": matched_channel.channel_id,
            "connected_channel_title": matched_channel.title,
        }
        return RightsDecision(
            allowed=True,
            basis=RightsBasis.OWNER,
            reason=reason,
            requires_attribution=False,
            attribution_text=None,
            evidence={
                **base_evidence,
                "allowed": True,
                "basis": RightsBasis.OWNER.value,
                "requires_attribution": False,
                "matched_channel_record_id": str(matched_channel.id),
                "matched_channel_id": matched_channel.channel_id,
                "matched_channel_title": matched_channel.title,
                "reason": reason,
                "checks_evaluated": checks_evaluated,
            },
        )
    checks_evaluated["owner"] = {
        "passed": False,
        "detail": (
            f"Video channel '{video.channel_id}' is not among your "
            f"{len(connected_channels)} connected YouTube channel(s)."
        ),
    }

    # 2. CAMPAIGN: URL/video ID (or authorized channel) is listed in the selected campaign
    if campaign is not None:
        rule_eval = evaluate_campaign_eligibility(campaign, now=checked_at)
        if not rule_eval.is_eligible:
            checks_evaluated["campaign"] = {
                "passed": False,
                "campaign_id": str(campaign.id),
                "campaign_name": campaign.name,
                "detail": rule_eval.reason,
            }
        else:
            matched_source = next(
                (
                    src
                    for src in campaign.sources
                    if (
                        src.normalized_video_id
                        and src.normalized_video_id == video.video_id
                    )
                    or (
                        src.source_type == "channel"
                        and src.source_channel_id
                        and src.source_channel_id == video.channel_id
                    )
                ),
                None,
            )
            if matched_source is not None:
                reason = (
                    f"Authorized under campaign '{campaign.name}': video matches "
                    f"listed campaign source ({matched_source.source_url})."
                )
                checks_evaluated["campaign"] = {
                    "passed": True,
                    "campaign_id": str(campaign.id),
                    "campaign_name": campaign.name,
                    "matched_source_id": str(matched_source.id),
                    "matched_source_url": matched_source.source_url,
                }
                return RightsDecision(
                    allowed=True,
                    basis=RightsBasis.CAMPAIGN,
                    reason=reason,
                    requires_attribution=False,
                    attribution_text=None,
                    evidence={
                        **base_evidence,
                        "allowed": True,
                        "basis": RightsBasis.CAMPAIGN.value,
                        "requires_attribution": False,
                        "campaign_id": str(campaign.id),
                        "campaign_name": campaign.name,
                        "campaign_rules_text": campaign.rules_text,
                        "campaign_deadline": (
                            rule_eval.deadline.isoformat() if rule_eval.deadline else None
                        ),
                        "matched_source_id": str(matched_source.id),
                        "matched_source_url": matched_source.source_url,
                        "reason": reason,
                        "checks_evaluated": checks_evaluated,
                    },
                )
            checks_evaluated["campaign"] = {
                "passed": False,
                "campaign_id": str(campaign.id),
                "campaign_name": campaign.name,
                "detail": (
                    f"Video '{video.video_id}' is not listed in the authorized sources "
                    f"of campaign '{campaign.name}'."
                ),
            }
    else:
        checks_evaluated["campaign"] = {
            "passed": False,
            "detail": "No campaign selected.",
        }

    # 3. PERMISSION: user uploaded a valid written permission document for that channel
    valid_permission: Permission | None = None
    permission_failure_detail = (
        f"No active written permission document found for channel '{video.channel_id}'."
    )
    for perm in permissions:
        if perm.channel_id != video.channel_id:
            continue
        if perm.revoked:
            permission_failure_detail = (
                f"Written permission document '{perm.original_filename}' for channel "
                f"'{video.channel_id}' has been revoked."
            )
            continue
        if perm.expires_at is not None and checked_at > _ensure_utc(perm.expires_at):
            permission_failure_detail = (
                f"Written permission document '{perm.original_filename}' for channel "
                f"'{video.channel_id}' expired on "
                f"{_ensure_utc(perm.expires_at).strftime('%Y-%m-%d %H:%M UTC')}."
            )
            continue
        if not perm.document_path or not perm.document_sha256:
            permission_failure_detail = (
                f"Permission record for channel '{video.channel_id}' is missing "
                "document evidence."
            )
            continue
        valid_permission = perm
        break

    if valid_permission is not None:
        reason = (
            f"Verified written permission document '{valid_permission.original_filename}' "
            f"for channel '{video.channel_id}'."
        )
        checks_evaluated["permission"] = {
            "passed": True,
            "permission_id": str(valid_permission.id),
            "document_sha256": valid_permission.document_sha256,
            "original_filename": valid_permission.original_filename,
        }
        return RightsDecision(
            allowed=True,
            basis=RightsBasis.PERMISSION,
            reason=reason,
            requires_attribution=False,
            attribution_text=None,
            evidence={
                **base_evidence,
                "allowed": True,
                "basis": RightsBasis.PERMISSION.value,
                "requires_attribution": False,
                "permission_id": str(valid_permission.id),
                "document_sha256": valid_permission.document_sha256,
                "original_filename": valid_permission.original_filename,
                "expires_at": (
                    _ensure_utc(valid_permission.expires_at).isoformat()
                    if valid_permission.expires_at
                    else None
                ),
                "reason": reason,
                "checks_evaluated": checks_evaluated,
            },
        )
    checks_evaluated["permission"] = {
        "passed": False,
        "detail": permission_failure_detail,
    }

    # 4. CC: YouTube videos.list (part=status) returns license == "creativeCommon"
    if video.license == "creativeCommon":
        attribution = build_cc_attribution(video)
        reason = (
            "Verified Creative Commons license (license == 'creativeCommon'). "
            "Attribution text is required and will be attached to every clip."
        )
        checks_evaluated["cc"] = {
            "passed": True,
            "license": video.license,
            "requires_attribution": True,
        }
        return RightsDecision(
            allowed=True,
            basis=RightsBasis.CC,
            reason=reason,
            requires_attribution=True,
            attribution_text=attribution,
            evidence={
                **base_evidence,
                "allowed": True,
                "basis": RightsBasis.CC.value,
                "requires_attribution": True,
                "attribution_text": attribution,
                "reason": reason,
                "checks_evaluated": checks_evaluated,
            },
        )
    checks_evaluated["cc"] = {
        "passed": False,
        "license": video.license,
        "detail": (
            f"Video has standard YouTube license ('{video.license}'), "
            "not Creative Commons ('creativeCommon')."
        ),
    }

    # NONE MATCHED -> REJECT with a clear, actionable human-readable explanation
    rejection_parts = [
        f"Rights check rejected for video '{video.title or video.video_id}' "
        f"(channel {video.channel_id}):"
    ]
    if campaign is not None:
        rejection_parts.append(f"• Campaign: {checks_evaluated['campaign']['detail']}")
    rejection_parts.append(f"• Channel ownership: {checks_evaluated['owner']['detail']}")
    rejection_parts.append(f"• License: {checks_evaluated['cc']['detail']}")
    rejection_parts.append(f"• Written permission: {checks_evaluated['permission']['detail']}")
    rejection_parts.append(
        "Connect the owning YouTube channel, select an active campaign listing this URL, "
        "use a Creative Commons video, or upload written permission for this channel."
    )
    reason = " ".join(rejection_parts)

    return RightsDecision(
        allowed=False,
        basis=None,
        reason=reason,
        requires_attribution=False,
        attribution_text=None,
        evidence={
            **base_evidence,
            "allowed": False,
            "basis": None,
            "requires_attribution": False,
            "reason": reason,
            "checks_evaluated": checks_evaluated,
        },
    )


def check_rights_for_user(
    db: Session,
    *,
    user: User,
    video: VideoMetadata,
    campaign_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> RightsDecision:
    """Load the user's connected channels, permissions, and optional campaign, then evaluate rights."""
    connected_channels = (
        db.query(YouTubeChannel)
        .filter(YouTubeChannel.user_id == user.id)
        .all()
    )
    permissions = (
        db.query(Permission)
        .filter(
            Permission.user_id == user.id,
            Permission.channel_id == video.channel_id,
        )
        .order_by(Permission.created_at.desc())
        .all()
    )
    campaign: Campaign | None = None
    if campaign_id is not None:
        campaign = (
            db.query(Campaign)
            .options(selectinload(Campaign.sources))
            .filter(Campaign.id == campaign_id)
            .first()
        )

    return evaluate_rights(
        video=video,
        connected_channels=connected_channels,
        campaign=campaign,
        permissions=permissions,
        now=now,
    )


def assert_job_rights_approved(job: Job) -> None:
    """Defense-in-depth gate called before downloading or processing any job.

    Raises RightsGateError if the job lacks a verified rights basis or evidence.
    """
    if not job.rights_basis or job.rights_basis not in ALLOWED_RIGHTS_BASES:
        raise RightsGateError(
            f"Job {job.id} blocked by Rights Gate: missing or invalid rights_basis."
        )
    evidence = job.rights_evidence_json or {}
    if evidence.get("allowed") is not True or evidence.get("basis") != job.rights_basis:
        raise RightsGateError(
            f"Job {job.id} blocked by Rights Gate: rights_evidence_json does not confirm approval."
        )
    if job.rights_basis == RightsBasis.CC.value and not evidence.get("attribution_text"):
        raise RightsGateError(
            f"Job {job.id} blocked by Rights Gate: Creative Commons job missing attribution text."
        )


def reverify_job_rights(
    db: Session,
    *,
    job: Job,
    youtube_service: YouTubeService,
    access_token: str | None = None,
    now: datetime | None = None,
) -> RightsDecision:
    """Re-run the live Rights Gate for an existing job (required before any future upload)."""
    user = db.get(User, job.user_id)
    if user is None:
        raise RightsGateError(f"Job {job.id} owner user no longer exists.")

    video = youtube_service.fetch_video_metadata(
        job.youtube_video_id,
        access_token=access_token,
    )
    return check_rights_for_user(
        db,
        user=user,
        video=video,
        campaign_id=job.campaign_id,
        now=now,
    )

