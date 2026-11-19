"""Celery background tasks for rights-verified video downloading and clipping."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.database import SessionLocal
from app.models.base import utcnow
from app.models.clip import Clip, ReviewStatus
from app.models.job import Job, JobStatus, RightsBasis
from app.models.upload import Upload, UploadStatus
from app.models.youtube_channel import YouTubeChannel
from app.security import TokenCipher
from app.services.audit import record_audit
from app.services.engines.base import (
    ClipEngine,
    ClipEngineError,
    ClipSettings,
    get_clip_engine,
)
from app.services.metadata import compose_upload_metadata
from app.services.rights import (
    RightsGateError,
    assert_job_rights_approved,
    reverify_job_rights,
)
from app.services.youtube import (
    YOUTUBE_UPLOAD_SCOPE,
    VideoDownloadError,
    YouTubeAPIError,
    YouTubeAuthRevokedError,
    YouTubeQuotaExceededError,
    YouTubeService,
    YouTubeTransientUploadError,
    download_rights_verified_video,
)
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

VideoDownloader = Callable[[Job, Path], Path]


def _format_user_error_message(exc: Exception) -> str:
    """Return a short, clear failure message for the UI while keeping raw details in logs."""
    if isinstance(exc, VideoDownloadError):
        return exc.user_message
    if isinstance(exc, ClipEngineError):
        return "Video downloaded, but clip generation failed. Check worker logs for details."
    if isinstance(exc, YouTubeAPIError):
        first_line = str(exc).splitlines()[0].strip() if str(exc) else ""
        return first_line[:200] or "YouTube video processing failed."
    return "Job processing failed unexpectedly. Check worker logs for details."


def _relative_storage_path(file_path: Path, storage_root: Path) -> str:
    """Convert an absolute clip path to a path relative to STORAGE_ROOT."""
    resolved_file = file_path.resolve()
    resolved_root = storage_root.resolve()
    return str(resolved_file.relative_to(resolved_root))


def execute_clipping_job(
    db: Session,
    job_id: uuid.UUID,
    *,
    downloader: VideoDownloader | None = None,
    engine: ClipEngine | None = None,
) -> Job:
    """Run the full clipping pipeline for a single rights-verified Job.

    Steps:
      1. Load job and enforce Rights Gate before ANY download.
      2. Transition status to `processing` and write audit_log.
      3. Download source video via yt-dlp (only after rights pass).
      4. Run configured ClipEngine (default: StubEngine).
      5. Persist generated clips (attaching mandatory CC attribution if basis == cc).
      6. Transition status to `ready` (or `failed` on error) and write audit_log.
    """
    job = (
        db.query(Job)
        .options(selectinload(Job.clips))
        .filter(Job.id == job_id)
        .first()
    )
    if job is None:
        raise ValueError(f"Job {job_id} not found.")

    # Defense-in-depth: block any rejected or unverified job BEFORE download
    if job.status == JobStatus.REJECTED.value:
        record_audit(
            db,
            "worker.rights_blocked",
            user_id=job.user_id,
            job_id=job.id,
            details={"reason": "Attempted to process a rejected job."},
        )
        db.commit()
        raise RightsGateError(f"Job {job.id} is rejected and cannot be downloaded.")

    try:
        assert_job_rights_approved(job)
    except RightsGateError as exc:
        job.status = JobStatus.REJECTED.value
        job.rejection_reason = str(exc)
        record_audit(
            db,
            "worker.rights_blocked",
            user_id=job.user_id,
            job_id=job.id,
            details={"reason": str(exc)},
        )
        db.commit()
        raise

    settings = get_settings()
    storage_root = settings.storage_path
    job_root = storage_root / "jobs" / str(job.id)
    download_dir = job_root / "source"
    clips_dir = job_root / "clips"

    # Transition to processing
    job.status = JobStatus.PROCESSING.value
    job.error_message = None
    record_audit(
        db,
        "job.processing",
        user_id=job.user_id,
        job_id=job.id,
        details={"rights_basis": job.rights_basis},
    )
    db.commit()

    download_fn = downloader or download_rights_verified_video
    clip_engine = engine or get_clip_engine(settings.clip_engine)

    try:
        job_cfg = job.settings_json or {}
        if job_cfg.get("source_mode") == "uploaded_file":
            uploaded_rel = str(job_cfg.get("uploaded_source_path") or "")
            if not uploaded_rel:
                raise VideoDownloadError(
                    "Uploaded source file reference is missing for this job."
                )
            resolved_upload = (storage_root / uploaded_rel).resolve()
            if not resolved_upload.is_relative_to(storage_root) or not resolved_upload.is_file():
                raise VideoDownloadError(
                    "Uploaded source file could not be found in storage."
                )
            source_path = resolved_upload
            record_audit(
                db,
                "job.source_ready",
                user_id=job.user_id,
                job_id=job.id,
                details={
                    "source_mode": "uploaded_file",
                    "rights_basis": job.rights_basis,
                    "source_filename": source_path.name,
                },
            )
            db.commit()
        else:
            source_path = download_fn(job, download_dir)
            record_audit(
                db,
                "job.downloaded",
                user_id=job.user_id,
                job_id=job.id,
                details={
                    "source_mode": "url",
                    "video_id": job.youtube_video_id,
                    "rights_basis": job.rights_basis,
                    "source_filename": source_path.name,
                },
            )
            db.commit()

        clip_settings = ClipSettings(
            output_dir=clips_dir,
            clip_count=int(job_cfg.get("clip_count", settings.default_clip_count)),
            clip_duration_sec=int(
                job_cfg.get("clip_duration_sec", settings.default_clip_duration_sec)
            ),
            min_clip_duration_sec=int(job_cfg.get("min_clip_duration_sec", 20)),
            max_clip_duration_sec=int(job_cfg.get("max_clip_duration_sec", 60)),
            aspect_ratio=str(job_cfg.get("aspect_ratio", "9:16")),
            blurred_background=bool(job_cfg.get("blurred_background", False)),
            source_title=job.source_title,
            job_id=str(job.id),
            extra={"rights_basis": job.rights_basis},
        )

        results = clip_engine.create_clips(source_path, clip_settings)
        if not results:
            raise RuntimeError("Clipping engine returned zero clips.")

        cc_attribution: str | None = None
        if job.rights_basis == RightsBasis.CC.value:
            cc_attribution = str(
                (job.rights_evidence_json or {}).get("attribution_text", "")
            ) or None

        # Clear any stale clips from a previous failed attempt
        for existing in list(job.clips):
            db.delete(existing)
        db.flush()

        for result in results:
            clip = Clip(
                job_id=job.id,
                file_path=_relative_storage_path(result.file_path, storage_root),
                start_sec=result.start_sec,
                end_sec=result.end_sec,
                score=result.score,
                reason=result.reason,
                transcript_excerpt=result.transcript_excerpt,
                metadata_json=result.metadata_json or {},
                title=result.title,
                attribution_text=cc_attribution,
                review_status=ReviewStatus.PENDING.value,
            )
            db.add(clip)

        job.status = JobStatus.READY.value
        record_audit(
            db,
            "job.ready",
            user_id=job.user_id,
            job_id=job.id,
            details={
                "engine": clip_engine.name,
                "clip_count": len(results),
                "rights_basis": job.rights_basis,
            },
        )
        db.commit()
        db.refresh(job)
        return job

    except Exception as exc:  # noqa: BLE001
        if isinstance(exc, VideoDownloadError):
            logger.error(
                "Clipping job %s download failed: %s\nFull detail:\n%s",
                job.id,
                exc.user_message,
                exc.detail,
            )
        else:
            logger.exception("Clipping job %s failed", job.id)

        user_message = _format_user_error_message(exc)
        db.rollback()
        failed_job = db.get(Job, job.id)
        if failed_job is not None:
            failed_job.status = JobStatus.FAILED.value
            failed_job.error_message = user_message
            record_audit(
                db,
                "job.failed",
                user_id=failed_job.user_id,
                job_id=failed_job.id,
                details={
                    "error_type": type(exc).__name__,
                    "message": user_message,
                },
            )
            db.commit()
            db.refresh(failed_job)
            return failed_job
        raise


@celery_app.task(name="clipper.process_clipping_job", bind=True)
def process_clipping_job(self, job_id: str) -> dict[str, Any]:
    """Celery task entry point for processing a rights-approved clipping job."""
    parsed_id = uuid.UUID(job_id)
    db = SessionLocal()
    try:
        job = execute_clipping_job(db, parsed_id)
        return {
            "job_id": str(job.id),
            "status": job.status,
            "clip_count": len(job.clips),
        }
    finally:
        db.close()


def _strip_upload_scope(channel: YouTubeChannel) -> None:
    remaining = [s for s in (channel.scopes or "").split() if s != YOUTUBE_UPLOAD_SCOPE]
    channel.scopes = " ".join(remaining)


def execute_clip_upload(
    db: Session,
    upload_id: uuid.UUID,
    *,
    youtube_service: YouTubeService | None = None,
    now: datetime | None = None,
) -> Upload:
    """Execute a YouTube Shorts resumable upload with mandatory approval and live rights re-check.

    Raises YouTubeTransientUploadError for retryable network/5xx errors so Celery can retry
    with backoff. Permanent failures (unapproved clip, revoked rights, revoked OAuth token,
    quota exceeded) mark the Upload row `failed` and write an `audit_log` entry without retrying.
    """
    upload = (
        db.query(Upload)
        .options(
            selectinload(Upload.clip),
            selectinload(Upload.job).selectinload(Job.campaign),
            selectinload(Upload.youtube_channel),
        )
        .filter(Upload.id == upload_id)
        .first()
    )
    if upload is None:
        raise ValueError(f"Upload {upload_id} not found.")

    if upload.status == UploadStatus.UPLOADED.value:
        return upload

    clip = upload.clip
    job = upload.job
    channel = upload.youtube_channel

    # 1. NON-NEGOTIABLE: Clip must still be explicitly approved
    if clip is None or clip.review_status != ReviewStatus.APPROVED.value:
        upload.status = UploadStatus.FAILED.value
        upload.error = "Upload blocked: clip is not approved for publishing."
        record_audit(
            db,
            "upload.blocked_unapproved",
            user_id=upload.user_id,
            job_id=upload.job_id,
            details={
                "upload_id": str(upload.id),
                "clip_id": str(upload.clip_id),
                "review_status": clip.review_status if clip else "missing",
            },
        )
        db.commit()
        db.refresh(upload)
        return upload

    # 2. Verify connected channel still exists and has youtube.upload scope
    if channel is None or not channel.upload_enabled:
        upload.status = UploadStatus.FAILED.value
        upload.error = (
            "YouTube upload permission is missing or disconnected. "
            "Click 'Enable uploading' to reconnect your channel."
        )
        record_audit(
            db,
            "upload.auth_missing",
            user_id=upload.user_id,
            job_id=upload.job_id,
            details={"upload_id": str(upload.id), "clip_id": str(upload.clip_id)},
        )
        db.commit()
        db.refresh(upload)
        return upload

    settings = get_settings()
    yt = youtube_service or YouTubeService(settings=settings)

    # 3. Decrypt refresh token and obtain fresh access token
    try:
        cipher = TokenCipher.from_settings(settings)
        refresh_token = cipher.decrypt(channel.encrypted_refresh_token)
    except ValueError as exc:
        logger.error("Failed to decrypt refresh token for channel %s", channel.channel_id)
        _strip_upload_scope(channel)
        upload.status = UploadStatus.FAILED.value
        upload.error = (
            "Stored YouTube credentials could not be decrypted. "
            "Click 'Enable uploading' to reconnect your channel."
        )
        record_audit(
            db,
            "upload.auth_revoked",
            user_id=upload.user_id,
            job_id=upload.job_id,
            details={"upload_id": str(upload.id), "reason": "token_decrypt_failed"},
        )
        db.commit()
        db.refresh(upload)
        return upload

    try:
        access_token = yt.refresh_access_token(refresh_token)
    except YouTubeAuthRevokedError as exc:
        _strip_upload_scope(channel)
        upload.status = UploadStatus.FAILED.value
        upload.error = str(exc)
        record_audit(
            db,
            "upload.auth_revoked",
            user_id=upload.user_id,
            job_id=upload.job_id,
            details={"upload_id": str(upload.id), "channel_id": channel.channel_id},
        )
        db.commit()
        db.refresh(upload)
        return upload

    # 4. NON-NEGOTIABLE: Re-run live Rights Gate IMMEDIATELY before uploading (fail-closed)
    try:
        decision = reverify_job_rights(
            db,
            job=job,
            youtube_service=yt,
            access_token=access_token,
            now=now,
        )
        if not decision.allowed or decision.basis is None:
            raise RightsGateError(f"Rights Gate rejected: {decision.reason}")
    except Exception as exc:
        upload.status = UploadStatus.FAILED.value
        upload.error = f"Upload blocked by Rights Gate: {exc}"
        record_audit(
            db,
            "upload.rights_blocked",
            user_id=upload.user_id,
            job_id=upload.job_id,
            details={
                "upload_id": str(upload.id),
                "clip_id": str(upload.clip_id),
                "reason": str(exc),
            },
        )
        db.commit()
        db.refresh(upload)
        return upload

    # 5. Defense-in-depth: guarantee #Shorts, campaign hashtags, and CC attribution
    cc_attribution = (
        decision.attribution_text
        or clip.attribution_text
        or (job.rights_evidence_json or {}).get("attribution_text")
    )
    campaign_rules = job.campaign.rules_text if job.campaign is not None else None
    composed = compose_upload_metadata(
        title=upload.title,
        description=upload.description,
        tags=list(upload.tags_json or []),
        rights_basis=decision.basis,
        fallback_title=clip.title or job.source_title,
        cc_attribution_text=str(cc_attribution) if cc_attribution else None,
        campaign_rules_text=campaign_rules,
    )
    upload.title = composed["title"]
    upload.description = composed["description"]
    upload.tags_json = composed["tags"]

    # 6. Locate generated clip file safely inside STORAGE_ROOT
    storage_root = settings.storage_path
    clip_path = (storage_root / clip.file_path).resolve()
    if not clip_path.is_relative_to(storage_root) or not clip_path.is_file():
        upload.status = UploadStatus.FAILED.value
        upload.error = "Clip video file is missing from storage."
        record_audit(
            db,
            "upload.failed",
            user_id=upload.user_id,
            job_id=upload.job_id,
            details={"upload_id": str(upload.id), "reason": "clip_file_missing"},
        )
        db.commit()
        db.refresh(upload)
        return upload

    # 7. Perform resumable upload to YouTube Data API v3
    upload.status = UploadStatus.UPLOADING.value
    upload.error = None
    record_audit(
        db,
        "upload.started",
        user_id=upload.user_id,
        job_id=upload.job_id,
        details={
            "upload_id": str(upload.id),
            "clip_id": str(upload.clip_id),
            "privacy": upload.privacy,
            "rights_basis": decision.basis.value,
        },
    )
    db.commit()

    try:
        youtube_video_id = yt.upload_video_resumable(
            clip_path,
            access_token=access_token,
            title=upload.title,
            description=upload.description,
            tags=list(upload.tags_json or []),
            privacy=upload.privacy,
        )
    except YouTubeQuotaExceededError as exc:
        upload.status = UploadStatus.FAILED.value
        upload.error = str(exc)
        record_audit(
            db,
            "upload.quota_exceeded",
            user_id=upload.user_id,
            job_id=upload.job_id,
            details={"upload_id": str(upload.id), "message": str(exc)},
        )
        db.commit()
        db.refresh(upload)
        return upload
    except YouTubeAuthRevokedError as exc:
        _strip_upload_scope(channel)
        upload.status = UploadStatus.FAILED.value
        upload.error = str(exc)
        record_audit(
            db,
            "upload.auth_revoked",
            user_id=upload.user_id,
            job_id=upload.job_id,
            details={"upload_id": str(upload.id), "channel_id": channel.channel_id},
        )
        db.commit()
        db.refresh(upload)
        return upload
    except YouTubeTransientUploadError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("YouTube upload %s failed permanently", upload.id)
        upload.status = UploadStatus.FAILED.value
        upload.error = str(exc)[:300]
        record_audit(
            db,
            "upload.failed",
            user_id=upload.user_id,
            job_id=upload.job_id,
            details={
                "upload_id": str(upload.id),
                "error_type": type(exc).__name__,
                "message": str(exc)[:300],
            },
        )
        db.commit()
        db.refresh(upload)
        return upload

    upload.youtube_video_id = youtube_video_id
    upload.posted_at = utcnow()
    upload.status = UploadStatus.UPLOADED.value
    upload.error = None
    record_audit(
        db,
        "upload.completed",
        user_id=upload.user_id,
        job_id=upload.job_id,
        details={
            "upload_id": str(upload.id),
            "clip_id": str(upload.clip_id),
            "youtube_video_id": youtube_video_id,
            "privacy": upload.privacy,
            "rights_basis": decision.basis.value,
        },
    )
    db.commit()
    db.refresh(upload)
    return upload


@celery_app.task(name="clipper.upload_clip_to_youtube", bind=True, max_retries=3)
def upload_clip_to_youtube(self, upload_id: str) -> dict[str, Any]:
    """Celery task for uploading an approved clip to YouTube with retries on transient errors."""
    parsed_id = uuid.UUID(upload_id)
    db = SessionLocal()
    try:
        upload = execute_clip_upload(db, parsed_id)
        return {
            "upload_id": str(upload.id),
            "status": upload.status,
            "youtube_video_id": upload.youtube_video_id,
        }
    except YouTubeTransientUploadError as exc:
        if self.request.retries < self.max_retries:
            countdown = 60 * (2 ** self.request.retries)
            logger.warning(
                "Transient upload error for %s (attempt %s/%s); retrying in %ss: %s",
                upload_id,
                self.request.retries + 1,
                self.max_retries,
                countdown,
                exc,
            )
            raise self.retry(exc=exc, countdown=countdown)

        failed = db.get(Upload, parsed_id)
        if failed is not None:
            failed.status = UploadStatus.FAILED.value
            failed.error = f"Upload failed after retries: {exc}"
            record_audit(
                db,
                "upload.failed",
                user_id=failed.user_id,
                job_id=failed.job_id,
                details={
                    "upload_id": str(failed.id),
                    "error_type": type(exc).__name__,
                    "message": str(exc)[:300],
                },
            )
            db.commit()
        return {"upload_id": upload_id, "status": UploadStatus.FAILED.value}
    finally:
        db.close()


def dispatch_due_uploads(
    db: Session,
    *,
    upload_dispatcher: Callable[[uuid.UUID], str | None],
    now: datetime | None = None,
) -> list[uuid.UUID]:
    """Find due scheduled uploads (scheduled_at <= now) and dispatch them once."""
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)

    due_candidates = (
        db.query(Upload)
        .filter(
            Upload.status == UploadStatus.QUEUED.value,
            Upload.scheduled_at.is_not(None),
        )
        .order_by(Upload.scheduled_at.asc())
        .all()
    )

    dispatched: list[uuid.UUID] = []
    for item in due_candidates:
        if item.scheduled_at is None:
            continue
        sched_utc = (
            item.scheduled_at.replace(tzinfo=timezone.utc)
            if item.scheduled_at.tzinfo is None
            else item.scheduled_at.astimezone(timezone.utc)
        )
        if sched_utc > current_time:
            continue

        # Claim the scheduled upload before enqueuing so subsequent beat ticks don't duplicate
        item.status = UploadStatus.UPLOADING.value
        record_audit(
            db,
            "upload.schedule_triggered",
            user_id=item.user_id,
            job_id=item.job_id,
            details={
                "upload_id": str(item.id),
                "scheduled_at": sched_utc.isoformat(),
            },
        )
        db.commit()
        upload_dispatcher(item.id)
        dispatched.append(item.id)

    return dispatched


@celery_app.task(name="clipper.dispatch_due_scheduled_uploads")
def dispatch_due_scheduled_uploads() -> dict[str, Any]:
    """Celery Beat periodic task dispatching scheduled YouTube uploads that are due."""
    db = SessionLocal()
    try:
        dispatched = dispatch_due_uploads(
            db,
            upload_dispatcher=lambda uid: str(upload_clip_to_youtube.delay(str(uid)).id),
        )
        return {"dispatched_count": len(dispatched)}
    finally:
        db.close()

