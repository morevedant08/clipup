"""Pydantic schemas for generated video clips."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.schemas.upload import UploadResponse


class ClipResponse(BaseModel):
    """Clip metadata returned within a Job response."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    job_id: uuid.UUID
    file_path: str
    preview_url: str | None = None
    start_sec: float
    end_sec: float
    score: float | None = None
    title: str | None = None
    attribution_text: str | None = None
    review_status: str
    latest_upload: UploadResponse | None = None
    created_at: datetime


class ClipReviewUpdate(BaseModel):
    """Payload to update a clip's human review decision."""

    review_status: Literal["pending", "approved", "rejected"]

