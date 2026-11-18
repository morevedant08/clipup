"""Validated streaming storage for user-uploaded source videos.

Uploaded source videos are accepted ONLY after a job passes the Rights Gate,
validated by extension, MIME type, container magic bytes, and byte size cap,
and stored strictly inside STORAGE_ROOT outside any public directory.
File contents are never logged.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path

from fastapi import UploadFile, status

logger = logging.getLogger(__name__)

ALLOWED_VIDEO_EXTENSIONS: frozenset[str] = frozenset({".mp4", ".mov", ".webm"})
ALLOWED_VIDEO_MIME_TYPES: frozenset[str] = frozenset(
    {
        "video/mp4",
        "video/quicktime",
        "video/webm",
        "application/octet-stream",
    }
)
_ISO_BMFF_BOX_TYPES: frozenset[bytes] = frozenset(
    {b"ftyp", b"moov", b"mdat", b"wide", b"free"}
)
_WEBM_EBML_MAGIC = b"\x1a\x45\xdf\xa3"
_CHUNK_SIZE_BYTES = 1024 * 1024  # 1 MiB


class VideoUploadValidationError(Exception):
    """Raised when an uploaded video fails type, magic-byte, or size validation."""

    def __init__(self, message: str, status_code: int = status.HTTP_400_BAD_REQUEST) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class UploadedVideoResult:
    """Metadata for a validated uploaded source video persisted under STORAGE_ROOT."""

    relative_path: str
    original_filename: str
    extension: str
    size_bytes: int
    sha256: str


def _normalize_mime(content_type: str | None) -> str:
    if not content_type:
        return ""
    return content_type.split(";", 1)[0].strip().lower()


def validate_video_header(header: bytes, extension: str) -> bool:
    """Verify container magic bytes match the declared mp4/mov/webm extension."""
    ext = extension.lower()
    if ext in {".mp4", ".mov"}:
        return len(header) >= 8 and header[4:8] in _ISO_BMFF_BOX_TYPES
    if ext == ".webm":
        return header.startswith(_WEBM_EBML_MAGIC)
    return False


async def save_validated_source_upload(
    upload: UploadFile,
    *,
    job_id: uuid.UUID,
    storage_root: Path,
    max_size_bytes: int,
) -> UploadedVideoResult:
    """Validate and stream-save an uploaded source video into STORAGE_ROOT/uploads/<job_id>/.

    Enforces:
      - Allowed extensions (.mp4, .mov, .webm)
      - Allowed MIME types (if provided)
      - Container magic bytes
      - Streaming size limit (cleans up partial files immediately on error)
      - Containment within STORAGE_ROOT (never trusts client filename in disk path)
    """
    original_name = Path(upload.filename or "source.mp4").name
    ext = Path(original_name).suffix.lower()
    if ext not in ALLOWED_VIDEO_EXTENSIONS:
        raise VideoUploadValidationError(
            "Unsupported video file type. Allowed formats: .mp4, .mov, .webm."
        )

    mime = _normalize_mime(upload.content_type)
    if mime and mime not in ALLOWED_VIDEO_MIME_TYPES:
        raise VideoUploadValidationError(
            f"Unsupported video MIME type '{mime}'. Allowed formats: mp4, mov, webm."
        )

    resolved_root = storage_root.resolve()
    upload_dir = (resolved_root / "uploads" / str(job_id)).resolve()
    if not upload_dir.is_relative_to(resolved_root):
        raise VideoUploadValidationError(
            "Invalid upload storage destination.",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    upload_dir.mkdir(parents=True, exist_ok=True)
    target_path = (upload_dir / f"source{ext}").resolve()

    hasher = hashlib.sha256()
    total_bytes = 0
    header_buf = bytearray()
    header_checked = False

    try:
        with target_path.open("wb") as out_fp:
            while True:
                chunk = await upload.read(_CHUNK_SIZE_BYTES)
                if not chunk:
                    break

                if not header_checked:
                    header_buf.extend(chunk[:16])
                    if len(header_buf) >= 8:
                        if not validate_video_header(bytes(header_buf), ext):
                            raise VideoUploadValidationError(
                                f"Uploaded file content does not match a valid {ext.lstrip('.').upper()} video."
                            )
                        header_checked = True

                total_bytes += len(chunk)
                if total_bytes > max_size_bytes:
                    max_mb = max(1, max_size_bytes // (1024 * 1024))
                    raise VideoUploadValidationError(
                        f"Uploaded video exceeds the maximum allowed size of {max_mb} MB.",
                        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    )

                hasher.update(chunk)
                out_fp.write(chunk)

        if total_bytes == 0:
            raise VideoUploadValidationError("Uploaded video file is empty.")
        if not header_checked and not validate_video_header(bytes(header_buf), ext):
            raise VideoUploadValidationError(
                f"Uploaded file content does not match a valid {ext.lstrip('.').upper()} video."
            )
    except Exception:
        target_path.unlink(missing_ok=True)
        try:
            upload_dir.rmdir()
        except OSError:
            pass
        raise
    finally:
        await upload.close()

    relative_path = str(target_path.relative_to(resolved_root))
    digest = hasher.hexdigest()
    logger.info(
        "Stored uploaded source video for job %s (ext=%s, size_bytes=%d, sha256=%s)",
        job_id,
        ext,
        total_bytes,
        digest[:12],
    )
    return UploadedVideoResult(
        relative_path=relative_path,
        original_filename=original_name,
        extension=ext,
        size_bytes=total_bytes,
        sha256=digest,
    )
