"""Written permission document upload and management endpoints."""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.config import get_settings
from app.database import get_db
from app.models.permission import Permission
from app.models.user import User
from app.schemas.permission import PermissionResponse
from app.services.audit import record_audit

router = APIRouter(prefix="/api/permissions", tags=["permissions"])

ALLOWED_PERMISSION_EXTENSIONS = frozenset({".pdf", ".png", ".jpg", ".jpeg", ".txt", ".docx"})
MAX_PERMISSION_FILE_BYTES = 10 * 1024 * 1024  # 10 MB


@router.post("", response_model=PermissionResponse, status_code=status.HTTP_201_CREATED)
async def upload_permission_document(
    channel_id: str = Form(..., min_length=3, max_length=64),
    channel_name: str | None = Form(default=None),
    notes: str | None = Form(default=None),
    expires_at: datetime | None = Form(default=None),
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PermissionResponse:
    """Upload a written permission document granting clipping rights for a YouTube channel."""
    original_name = Path(file.filename or "permission.pdf").name
    ext = Path(original_name).suffix.lower()
    if ext not in ALLOWED_PERMISSION_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Unsupported document format. Allowed extensions: "
                + ", ".join(sorted(ALLOWED_PERMISSION_EXTENSIONS))
            ),
        )

    content = await file.read()
    if not content:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded permission document is empty.",
        )
    if len(content) > MAX_PERMISSION_FILE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Permission document exceeds the 10 MB maximum size.",
        )

    digest = hashlib.sha256(content).hexdigest()
    settings = get_settings()
    perm_dir = settings.storage_path / "permissions" / str(current_user.id)
    perm_dir.mkdir(parents=True, exist_ok=True)

    stored_filename = f"{uuid.uuid4().hex}{ext}"
    stored_path = perm_dir / stored_filename
    stored_path.write_bytes(content)

    relative_path = str(Path("permissions") / str(current_user.id) / stored_filename)
    permission = Permission(
        user_id=current_user.id,
        channel_id=channel_id.strip(),
        channel_name=channel_name.strip() if channel_name else None,
        document_path=relative_path,
        document_sha256=digest,
        original_filename=original_name,
        notes=notes,
        expires_at=expires_at,
        revoked=False,
    )
    db.add(permission)
    db.flush()

    record_audit(
        db,
        "permission.uploaded",
        user_id=current_user.id,
        details={
            "permission_id": str(permission.id),
            "channel_id": permission.channel_id,
            "document_sha256": digest,
        },
    )
    db.commit()
    db.refresh(permission)
    return PermissionResponse.model_validate(permission)


@router.get("", response_model=list[PermissionResponse])
def list_permissions(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[PermissionResponse]:
    """List written permission documents uploaded by the authenticated user."""
    records = (
        db.query(Permission)
        .filter(Permission.user_id == current_user.id)
        .order_by(Permission.created_at.desc())
        .all()
    )
    return [PermissionResponse.model_validate(item) for item in records]


@router.post("/{permission_id}/revoke", response_model=PermissionResponse)
def revoke_permission(
    permission_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PermissionResponse:
    """Revoke a previously uploaded permission document."""
    record = (
        db.query(Permission)
        .filter(
            Permission.id == permission_id,
            Permission.user_id == current_user.id,
        )
        .first()
    )
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Permission document not found.",
        )

    record.revoked = True
    record_audit(
        db,
        "permission.revoked",
        user_id=current_user.id,
        details={"permission_id": str(record.id), "channel_id": record.channel_id},
    )
    db.commit()
    db.refresh(record)
    return PermissionResponse.model_validate(record)

