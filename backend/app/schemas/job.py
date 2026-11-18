"""Pydantic schemas for clipping job creation and retrieval."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.clip import ClipResponse


class JobSettings(BaseModel):
    """User-configurable clipping parameters."""

    clip_count: int = Field(default=3, ge=1, le=20)
    clip_duration_sec: int = Field(default=30, ge=5, le=180)
    aspect_ratio: Literal["9:16"] = "9:16"


class JobCreateRequest(BaseModel):
    """Request payload for POST /api/jobs."""

    source_url: str = Field(..., min_length=10, max_length=1024)
    campaign_id: uuid.UUID | None = None
    settings: JobSettings = Field(default_factory=JobSettings)


class JobResponse(BaseModel):
    """Full job state including rights gate decision, evidence, and generated clips."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    source_url: str
    youtube_video_id: str
    source_channel_id: str | None = None
    source_title: str | None = None
    source_duration_sec: int | None = None
    campaign_id: uuid.UUID | None = None
    rights_basis: str | None = None
    rights_evidence_json: dict[str, Any] = Field(default_factory=dict)
    rejection_reason: str | None = None
    settings_json: dict[str, Any] = Field(default_factory=dict)
    source_mode: Literal["url", "uploaded_file"] = "url"
    uploaded_filename: str | None = None
    status: str
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime
    clips: list[ClipResponse] = Field(default_factory=list)

