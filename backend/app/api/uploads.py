"""Endpoints for uploading approved clips to YouTube as Shorts (immediate or scheduled)."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session, selectinload

from app.api.deps import (
    UploadDispatcher,
    get_current_user,
    get_upload_dispatcher,
    get_youtube_service,
)
from app.config import get_settings
from app.database import get_db
from app.models.clip import Clip, ReviewStatus
from app.models.job import Job, RightsBasis
from app.models.upload import Upload, UploadStatus
from app.models.user import User
from app.models.youtube_channel import YouTubeChannel
from app.schemas.upload import UploadCreateRequest, UploadResponse
from app.security import TokenCipher
from app.services.audit import record_audit
from app.services.metadata import compose_upload_metadata
from app.services.rights import RightsGateError, reverify_job_rights
from app.services.youtube import (
    YOUTUBE_UPLOAD_SCOPE,
    YouTubeAPIError,
    YouTubeAuthRevokedError,
    YouTubeService,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["uploads"])


def serialize_upload(upload: Upload) -> UploadResponse:
    """Convert an Upload ORM model into its public API response."""
    youtube_url = (
        f"https://www.youtube.com/shorts/{upload.youtube_video_id}"
        if upload.youtube_video_id
        else None
    )
    return UploadResponse(
        id=upload.id,
        user_id=upload.user_id,
        job_id=upload.job_id,
        clip_id=upload.clip_id,
        youtube_channel_id=upload.youtube_channel_id,
        title=upload.title,
        description=upload.description,
        tags=list(upload.tags_json or []),
        privacy=upload.privacy,
        youtube_video_id=upload.youtube_video_id,
        youtube_url=youtube_url,
        scheduled_at=upload.scheduled_at,
        posted_at=upload.posted_at,
        status=upload.status,
        error=upload.error,
        created_at=upload.created_at,
        updated_at=upload.updated_at,
    )


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _strip_upload_scope(channel: YouTubeChannel) -> None:
    remaining = [s for s in (channel.scopes or "").split() if s != YOUTUBE_UPLOAD_SCOPE]
    channel.scopes = " ".join(remaining)


def _resolve_upload_channel(
    db: Session,
    user: User,
    requested_channel_id: uuid.UUID | None,
) -> YouTubeChannel:
    """Locate the target connected YouTube channel and verify youtube.upload scope."""
    channels = (
        db.query(YouTubeChannel)
        .filter(YouTubeChannel.user_id == user.id)
        .order_by(YouTubeChannel.updated_at.desc())
        .all()
    )
    if requested_channel_id is not None:
        channel = next((c for c in channels if c.id == requested_channel_id), None)
        if channel is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Selected YouTube channel was not found on your account.",
            )
        if not channel.upload_enabled:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "Uploading is not enabled for this channel. "
                    "Click 'Enable uploading' first."
                ),
            )
        return channel

    enabled = next((c for c in channels if c.upload_enabled), None)
    if enabled is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "YouTube uploading is not enabled yet. "
                "Click 'Enable uploading' in the top bar to grant upload permission."
            ),
        )
    return enabled


@router.post(
    "/api/jobs/{job_id}/clips/{clip_id}/upload",
    response_model=UploadResponse,
    status_code=status.HTTP_201_CREATED,
)
def queue_clip_upload(
    job_id: uuid.UUID,
    clip_id: uuid.UUID,
    payload: UploadCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    youtube_service: YouTubeService = Depends(get_youtube_service),
    upload_dispatcher: UploadDispatcher = Depends(get_upload_dispatcher),
) -> UploadResponse:
    """Queue (or schedule) a YouTube Shorts upload for an approved clip after re-checking rights."""
    clip = (
        db.query(Clip)
        .options(selectinload(Clip.job).selectinload(Job.campaign))
        .join(Job, Clip.job_id == Job.id)
        .filter(
            Clip.id == clip_id,
            Clip.job_id == job_id,
            Job.user_id == current_user.id,
        )
        .first()
    )
    if clip is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Clip not found.",
        )

    # NON-NEGOTIABLE: Only clips explicitly approved by the user may be uploaded
    if clip.review_status != ReviewStatus.APPROVED.value:
        record_audit(
            db,
            "upload.blocked_unapproved",
            user_id=current_user.id,
            job_id=job_id,
            details={
                "clip_id": str(clip.id),
                "review_status": clip.review_status,
            },
        )
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Only clips with review_status 'approved' can be uploaded to YouTube. "
                "Approve this clip before uploading."
            ),
        )

    channel = _resolve_upload_channel(db, current_user, payload.channel_id)
    job = clip.job

    # Require explicit confirmation checkbox for self-attested jobs
    job_verification_level = (job.rights_evidence_json or {}).get("verification_level")
    if job_verification_level == "self_attested" and not payload.self_attested_confirmed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "This clip relies on a self-attested rights basis. You must explicitly "
                "confirm rights attestation before uploading."
            ),
        )

    # Obtain an access token if needed for live YouTube metadata lookup (when API key is absent)
    settings = get_settings()
    access_token: str | None = None
    if not settings.youtube_api_key:
        try:
            cipher = TokenCipher.from_settings(settings)
            refresh_token = cipher.decrypt(channel.encrypted_refresh_token)
            access_token = youtube_service.refresh_access_token(refresh_token)
        except YouTubeAuthRevokedError as exc:
            _strip_upload_scope(channel)
            record_audit(
                db,
                "upload.auth_revoked",
                user_id=current_user.id,
                job_id=job.id,
                details={"clip_id": str(clip.id), "channel_id": channel.channel_id},
            )
            db.commit()
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=str(exc),
            ) from exc
        except (ValueError, YouTubeAPIError) as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Unable to refresh YouTube OAuth token for pre-upload check.",
            ) from exc

    # NON-NEGOTIABLE: Re-run the live Rights Gate right before queueing the upload (fail-closed)
    try:
        decision = reverify_job_rights(
            db,
            job=job,
            youtube_service=youtube_service,
            access_token=access_token,
        )
        if not decision.allowed or decision.basis is None:
            raise RightsGateError(f"Rights Gate rejected: {decision.reason}")
    except Exception as exc:
        record_audit(
            db,
            "upload.rights_blocked",
            user_id=current_user.id,
            job_id=job.id,
            details={
                "clip_id": str(clip.id),
                "reason": str(exc),
            },
        )
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Upload blocked by Rights Gate: {exc}",
        ) from exc

    if decision.verification_level == "self_attested" and not payload.self_attested_confirmed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "This clip relies on a self-attested rights basis. You must explicitly "
                "confirm rights attestation before uploading."
            ),
        )

    cc_attribution = (
        decision.attribution_text
        or clip.attribution_text
        or (job.rights_evidence_json or {}).get("attribution_text")
    )
    campaign_rules = job.campaign.rules_text if job.campaign is not None else None
    composed = compose_upload_metadata(
        title=payload.title,
        description=payload.description,
        tags=payload.tags,
        rights_basis=decision.basis,
        fallback_title=clip.title or job.source_title,
        cc_attribution_text=str(cc_attribution) if cc_attribution else None,
        campaign_rules_text=campaign_rules,
    )

    now = datetime.now(timezone.utc)
    scheduled_utc = _ensure_utc(payload.scheduled_at) if payload.scheduled_at else None
    is_future_schedule = scheduled_utc is not None and scheduled_utc > now

    upload = Upload(
        user_id=current_user.id,
        job_id=job.id,
        clip_id=clip.id,
        youtube_channel_id=channel.id,
        title=composed["title"],
        description=composed["description"],
        tags_json=composed["tags"],
        privacy=payload.privacy,
        scheduled_at=scheduled_utc if is_future_schedule else None,
        status=UploadStatus.QUEUED.value,
    )
    db.add(upload)
    db.flush()

    record_audit(
        db,
        "upload.scheduled" if is_future_schedule else "upload.queued",
        user_id=current_user.id,
        job_id=job.id,
        details={
            "upload_id": str(upload.id),
            "clip_id": str(clip.id),
            "channel_id": channel.channel_id,
            "privacy": upload.privacy,
            "rights_basis": decision.basis.value,
            "requires_attribution": decision.basis == RightsBasis.CC,
            "scheduled_at": scheduled_utc.isoformat() if is_future_schedule else None,
        },
    )
    db.commit()

    if not is_future_schedule:
        try:
            task_id = upload_dispatcher(upload.id)
            record_audit(
                db,
                "upload.dispatched",
                user_id=current_user.id,
                job_id=job.id,
                details={"upload_id": str(upload.id), "task_id": task_id},
            )
            db.commit()
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Failed to dispatch YouTube upload %s: %s",
                upload.id,
                type(exc).__name__,
            )
            upload.status = UploadStatus.FAILED.value
            upload.error = "Failed to enqueue upload task on background worker."
            record_audit(
                db,
                "upload.enqueue_failed",
                user_id=current_user.id,
                job_id=job.id,
                details={"upload_id": str(upload.id), "error": type(exc).__name__},
            )
            db.commit()

    db.refresh(upload)
    return serialize_upload(upload)


@router.get("/api/uploads", response_model=list[UploadResponse])
def list_uploads(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[UploadResponse]:
    """List the authenticated user's YouTube Shorts upload records."""
    uploads = (
        db.query(Upload)
        .filter(Upload.user_id == current_user.id)
        .order_by(Upload.created_at.desc())
        .all()
    )
    return [serialize_upload(item) for item in uploads]


@router.get("/api/uploads/{upload_id}", response_model=UploadResponse)
def get_upload(
    upload_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UploadResponse:
    """Fetch a single upload record owned by the current user."""
    upload = (
        db.query(Upload)
        .filter(Upload.id == upload_id, Upload.user_id == current_user.id)
        .first()
    )
    if upload is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Upload record not found.",
        )
    return serialize_upload(upload)
