"""Job submission (with mandatory Rights Gate), listing, detail, and clip review endpoints."""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session, selectinload

from app.api.deps import (
    JobDispatcher,
    get_current_user,
    get_current_user_for_media,
    get_job_dispatcher,
    get_youtube_service,
)
from app.config import get_settings
from app.database import get_db
from app.models.campaign import Campaign
from app.models.clip import Clip
from app.models.job import Job, JobStatus
from app.models.user import User
from app.schemas.clip import ClipResponse, ClipReviewUpdate
from app.schemas.job import JobCreateRequest, JobResponse
from app.security import TokenCipher
from app.services.audit import record_audit
from app.services.rights import check_rights_for_user
from app.services.youtube import (
    YouTubeAPIError,
    YouTubeService,
    YouTubeURLValidationError,
    extract_youtube_video_id,
    normalize_youtube_video_url,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/jobs", tags=["jobs"])


def _serialize_job(job: Job) -> JobResponse:
    """Serialize Job ORM object and attach clip preview URLs."""
    clips = [
        ClipResponse(
            id=clip.id,
            job_id=clip.job_id,
            file_path=clip.file_path,
            preview_url=f"/api/jobs/{job.id}/clips/{clip.id}/media",
            start_sec=clip.start_sec,
            end_sec=clip.end_sec,
            score=clip.score,
            title=clip.title,
            attribution_text=clip.attribution_text,
            review_status=clip.review_status,
            created_at=clip.created_at,
        )
        for clip in job.clips
    ]
    return JobResponse(
        id=job.id,
        user_id=job.user_id,
        source_url=job.source_url,
        youtube_video_id=job.youtube_video_id,
        source_channel_id=job.source_channel_id,
        source_title=job.source_title,
        source_duration_sec=job.source_duration_sec,
        campaign_id=job.campaign_id,
        rights_basis=job.rights_basis,
        rights_evidence_json=job.rights_evidence_json,
        rejection_reason=job.rejection_reason,
        settings_json=job.settings_json,
        status=job.status,
        error_message=job.error_message,
        created_at=job.created_at,
        updated_at=job.updated_at,
        clips=clips,
    )


def _try_get_user_access_token(
    user: User,
    youtube_service: YouTubeService,
) -> str | None:
    """If YOUTUBE_API_KEY is absent, try refreshing a user's connected YouTube OAuth token."""
    settings = get_settings()
    if settings.youtube_api_key or not user.youtube_channels:
        return None
    try:
        cipher = TokenCipher.from_settings(settings)
        refresh_token = cipher.decrypt(user.youtube_channels[0].encrypted_refresh_token)
        return youtube_service.refresh_access_token(refresh_token)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Unable to refresh YouTube OAuth token for metadata lookup: %s",
            type(exc).__name__,
        )
        return None


@router.post("", response_model=JobResponse, status_code=status.HTTP_201_CREATED)
def create_job(
    payload: JobCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    youtube_service: YouTubeService = Depends(get_youtube_service),
    job_dispatcher: JobDispatcher = Depends(get_job_dispatcher),
) -> JobResponse:
    """Validate YouTube URL, fetch metadata, enforce Rights Gate, and reject or queue the job."""
    try:
        video_id = extract_youtube_video_id(payload.source_url)
        canonical_url = normalize_youtube_video_url(video_id)
    except YouTubeURLValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    if payload.campaign_id is not None:
        campaign_exists = (
            db.query(Campaign.id).filter(Campaign.id == payload.campaign_id).first()
        )
        if campaign_exists is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Selected campaign was not found.",
            )

    access_token = _try_get_user_access_token(current_user, youtube_service)
    try:
        video_meta = youtube_service.fetch_video_metadata(
            video_id,
            access_token=access_token,
        )
    except YouTubeAPIError as exc:
        logger.warning("Metadata lookup failed for video %s: %s", video_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc

    # Start in pending_rights state before recording the Rights Gate decision
    job = Job(
        user_id=current_user.id,
        source_url=canonical_url,
        youtube_video_id=video_id,
        source_channel_id=video_meta.channel_id,
        source_title=video_meta.title,
        source_duration_sec=video_meta.duration_sec,
        campaign_id=payload.campaign_id,
        settings_json=payload.settings.model_dump(),
        status=JobStatus.PENDING_RIGHTS.value,
        rights_evidence_json={},
    )
    db.add(job)
    db.flush()

    decision = check_rights_for_user(
        db,
        user=current_user,
        video=video_meta,
        campaign_id=payload.campaign_id,
    )

    job.rights_basis = decision.basis.value if decision.basis else None
    job.rights_evidence_json = decision.evidence

    if not decision.allowed:
        job.status = JobStatus.REJECTED.value
        job.rejection_reason = decision.reason
        record_audit(
            db,
            "rights.rejected",
            user_id=current_user.id,
            job_id=job.id,
            details={
                "video_id": video_id,
                "channel_id": video_meta.channel_id,
                "reason": decision.reason,
            },
        )
        db.commit()
        db.refresh(job)
        return _serialize_job(job)

    # Rights passed -> transition to queued and enqueue background worker
    job.status = JobStatus.QUEUED.value
    job.rejection_reason = None
    record_audit(
        db,
        "rights.passed",
        user_id=current_user.id,
        job_id=job.id,
        details={
            "video_id": video_id,
            "channel_id": video_meta.channel_id,
            "rights_basis": job.rights_basis,
            "requires_attribution": decision.requires_attribution,
        },
    )
    db.commit()

    try:
        task_id = job_dispatcher(job.id)
        record_audit(
            db,
            "job.queued",
            user_id=current_user.id,
            job_id=job.id,
            details={"task_id": task_id, "rights_basis": job.rights_basis},
        )
        db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to dispatch queued job %s: %s", job.id, type(exc).__name__)
        job.status = JobStatus.FAILED.value
        job.error_message = "Failed to enqueue clipping job on background worker."
        record_audit(
            db,
            "job.enqueue_failed",
            user_id=current_user.id,
            job_id=job.id,
            details={"error": type(exc).__name__},
        )
        db.commit()

    reloaded = (
        db.query(Job)
        .options(selectinload(Job.clips))
        .filter(Job.id == job.id)
        .one()
    )
    return _serialize_job(reloaded)


@router.get("", response_model=list[JobResponse])
def list_jobs(
    status_filter: str | None = Query(default=None, alias="status"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[JobResponse]:
    """List the authenticated user's clipping jobs (most recent first)."""
    query = (
        db.query(Job)
        .options(selectinload(Job.clips))
        .filter(Job.user_id == current_user.id)
        .order_by(Job.created_at.desc())
    )
    if status_filter:
        query = query.filter(Job.status == status_filter)
    jobs = query.all()
    return [_serialize_job(job) for job in jobs]


@router.get("/{job_id}", response_model=JobResponse)
def get_job(
    job_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JobResponse:
    """Retrieve a single job and its generated clips."""
    job = (
        db.query(Job)
        .options(selectinload(Job.clips))
        .filter(Job.id == job_id, Job.user_id == current_user.id)
        .first()
    )
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found.",
        )
    return _serialize_job(job)


@router.patch("/{job_id}/clips/{clip_id}/review", response_model=ClipResponse)
def update_clip_review_status(
    job_id: uuid.UUID,
    clip_id: uuid.UUID,
    payload: ClipReviewUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ClipResponse:
    """Approve or reject a generated clip during human review."""
    clip = (
        db.query(Clip)
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

    clip.review_status = payload.review_status
    record_audit(
        db,
        "clip.reviewed",
        user_id=current_user.id,
        job_id=job_id,
        details={"clip_id": str(clip_id), "review_status": payload.review_status},
    )
    db.commit()
    db.refresh(clip)
    return ClipResponse(
        id=clip.id,
        job_id=clip.job_id,
        file_path=clip.file_path,
        preview_url=f"/api/jobs/{job_id}/clips/{clip.id}/media",
        start_sec=clip.start_sec,
        end_sec=clip.end_sec,
        score=clip.score,
        title=clip.title,
        attribution_text=clip.attribution_text,
        review_status=clip.review_status,
        created_at=clip.created_at,
    )


@router.get("/{job_id}/clips/{clip_id}/media")
def stream_clip_media(
    job_id: uuid.UUID,
    clip_id: uuid.UUID,
    current_user: User = Depends(get_current_user_for_media),
    db: Session = Depends(get_db),
) -> FileResponse:
    """Serve a generated clip MP4 file safely from STORAGE_ROOT."""
    clip = (
        db.query(Clip)
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

    storage_root = get_settings().storage_path
    resolved = (storage_root / clip.file_path).resolve()
    if not str(resolved).startswith(str(storage_root)) or not resolved.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Clip media file not found on disk.",
        )

    return FileResponse(path=resolved, media_type="video/mp4")
