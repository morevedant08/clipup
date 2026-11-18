"""Celery background tasks for rights-verified video downloading and clipping."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.database import SessionLocal
from app.models.clip import Clip, ReviewStatus
from app.models.job import Job, JobStatus, RightsBasis
from app.services.audit import record_audit
from app.services.engines.base import (
    ClipEngine,
    ClipEngineError,
    ClipSettings,
    get_clip_engine,
)
from app.services.rights import RightsGateError, assert_job_rights_approved
from app.services.youtube import (
    VideoDownloadError,
    YouTubeAPIError,
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
            aspect_ratio=str(job_cfg.get("aspect_ratio", "9:16")),
            source_title=job.source_title,
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

