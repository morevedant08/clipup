"""Pydantic schemas for uploaded written permission documents."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class PermissionResponse(BaseModel):
    """Metadata for a user-uploaded written permission document."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    channel_id: str
    channel_name: str | None = None
    original_filename: str
    document_sha256: str
    notes: str | None = None
    expires_at: datetime | None = None
    revoked: bool
    created_at: datetime
