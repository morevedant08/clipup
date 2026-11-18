"""Job submission (with mandatory Rights Gate & optional source file upload), listing, detail, and clip review."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import FileResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session, selectinload
from starlette.datastructures import UploadFile as StarletteUploadFile

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
from app.services.uploads import (
    VideoUploadValidationError,
    save_validated_source_upload,
)
from app.services.youtube import (
    YouTubeAPIError,
    YouTubeService,
    YouTubeURLValidationError,
    extract_youtube_video_id,
    normalize_youtube_video_url,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/jobs", tags=["jobs"])

_INTERNAL_SETTINGS_KEYS = frozenset({"uploaded_source_path"})


def _serialize_job(job: Job) -> JobResponse:
    """Serialize Job ORM object, attach clip preview URLs, and hide internal paths."""
    raw_settings = dict(job.settings_json or {})
    source_mode = (
        "uploaded_file"
        if raw_settings.get("source_mode") == "uploaded_file"
        else "url"
    )
    uploaded_filename = (
        str(raw_settings["uploaded_source_filename"])
        if raw_settings.get("uploaded_source_filename")
        else None
    )
    public_settings = {
        k: v for k, v in raw_settings.items() if k not in _INTERNAL_SETTINGS_KEYS
    }

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
        settings_json=public_settings,
        source_mode=source_mode,
        uploaded_filename=uploaded_filename,
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


async def _parse_job_create_request(
    request: Request,
) -> tuple[JobCreateRequest, StarletteUploadFile | None]:
    """Parse POST /api/jobs from either application/json or multipart/form-data."""
    content_type = (request.headers.get("content-type") or "").lower()
    if "multipart/form-data" in content_type:
        form = await request.form()
        raw_source_url = form.get("source_url")
        if not isinstance(raw_source_url, str) or not raw_source_url.strip():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="source_url is required.",
            )

        raw_campaign_id = form.get("campaign_id")
        campaign_id = (
            raw_campaign_id.strip()
            if isinstance(raw_campaign_id, str) and raw_campaign_id.strip()
            else None
        )

        settings_data: dict[str, Any] = {}
        raw_settings = form.get("settings")
        if isinstance(raw_settings, str) and raw_settings.strip():
            try:
                parsed_settings = json.loads(raw_settings)
                if not isinstance(parsed_settings, dict):
                    raise ValueError("settings must be a JSON object")
                settings_data.update(parsed_settings)
            except (json.JSONDecodeError, ValueError) as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="Invalid JSON in 'settings' field.",
                ) from exc

        for field_name in ("clip_count", "clip_duration_sec", "aspect_ratio"):
            raw_val = form.get(field_name)
            if isinstance(raw_val, str) and raw_val.strip():
                settings_data[field_name] = raw_val.strip()

        raw_file = form.get("source_file") or form.get("file")
        upload_file: StarletteUploadFile | None = None
        if isinstance(raw_file, StarletteUploadFile) and raw_file.filename:
            upload_file = raw_file

        try:
            payload = JobCreateRequest.model_validate(
                {
                    "source_url": raw_source_url,
                    "campaign_id": campaign_id,
                    "settings": settings_data,
                }
            )
        except ValidationError as exc:
            if upload_file is not None:
                await upload_file.close()
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=exc.errors(),
            ) from exc

        return payload, upload_file

    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Request body must be valid JSON or multipart/form-data.",
        ) from exc

    try:
        payload = JobCreateRequest.model_validate(body)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=exc.errors(),
        ) from exc
    return payload, None


@router.post("", response_model=JobResponse, status_code=status.HTTP_201_CREATED)
async def create_job(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    youtube_service: YouTubeService = Depends(get_youtube_service),
    job_dispatcher: JobDispatcher = Depends(get_job_dispatcher),
) -> JobResponse:
    """Validate YouTube URL, fetch metadata, enforce Rights Gate, accept optional upload, and queue.

    Accepts either JSON (`JobCreateRequest`) or `multipart/form-data` with an optional
    `source_file` (mp4/mov/webm). The Rights Gate ALWAYS runs first on the YouTube URL;
    uploaded files are discarded and never stored if the rights check fails.
    """
    payload, source_file = await _parse_job_create_request(request)

    try:
        video_id = extract_youtube_video_id(payload.source_url)
        canonical_url = normalize_youtube_video_url(video_id)
    except YouTubeURLValidationError as exc:
        if source_file is not None:
            await source_file.close()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    if payload.campaign_id is not None:
        campaign_exists = (
            db.query(Campaign.id).filter(Campaign.id == payload.campaign_id).first()
        )
        if campaign_exists is None:
            if source_file is not None:
                await source_file.close()
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
        if source_file is not None:
            await source_file.close()
        logger.warning("Metadata lookup failed for video %s: %s", video_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc

    # Start in pending_rights state before recording the Rights Gate decision
    base_settings: dict[str, Any] = {
        **payload.settings.model_dump(),
        "source_mode": "url",
    }
    job = Job(
        user_id=current_user.id,
        source_url=canonical_url,
        youtube_video_id=video_id,
        source_channel_id=video_meta.channel_id,
        source_title=video_meta.title,
        source_duration_sec=video_meta.duration_sec,
        campaign_id=payload.campaign_id,
        settings_json=base_settings,
        status=JobStatus.PENDING_RIGHTS.value,
        rights_evidence_json={},
    )
    db.add(job)
    db.flush()

    # NON-NEGOTIABLE: Run Rights Gate FIRST on the URL before accepting any uploaded file
    decision = check_rights_for_user(
        db,
        user=current_user,
        video=video_meta,
        campaign_id=payload.campaign_id,
    )

    job.rights_basis = decision.basis.value if decision.basis else None
    job.rights_evidence_json = decision.evidence

    if not decision.allowed:
        had_upload = source_file is not None
        if source_file is not None:
            await source_file.close()

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
                "source_upload_discarded": had_upload,
            },
        )
        db.commit()
        db.refresh(job)
        return _serialize_job(job)

    # Rights passed -> if a source file was uploaded, validate & store under STORAGE_ROOT
    source_mode = "url"
    if source_file is not None:
        settings = get_settings()
        try:
            uploaded = await save_validated_source_upload(
                source_file,
                job_id=job.id,
                storage_root=settings.storage_path,
                max_size_bytes=settings.max_upload_size_bytes,
            )
        except VideoUploadValidationError as exc:
            db.rollback()
            raise HTTPException(
                status_code=exc.status_code,
                detail=exc.message,
            ) from exc

        source_mode = "uploaded_file"
        job.settings_json = {
            **base_settings,
            "source_mode": "uploaded_file",
            "uploaded_source_path": uploaded.relative_path,
            "uploaded_source_filename": uploaded.original_filename,
            "uploaded_source_sha256": uploaded.sha256,
            "uploaded_source_size_bytes": uploaded.size_bytes,
        }
        record_audit(
            db,
            "job.source_uploaded",
            user_id=current_user.id,
            job_id=job.id,
            details={
                "source_mode": "uploaded_file",
                "original_filename": uploaded.original_filename,
                "extension": uploaded.extension,
                "size_bytes": uploaded.size_bytes,
                "sha256": uploaded.sha256,
            },
        )

    # Transition to queued and enqueue background worker
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
            "source_mode": source_mode,
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
            details={
                "task_id": task_id,
                "rights_basis": job.rights_basis,
                "source_mode": source_mode,
            },
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
    if not resolved.is_relative_to(storage_root) or not resolved.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Clip media file not found on disk.",
        )

    return FileResponse(path=resolved, media_type="video/mp4")
