"""Pydantic schemas for YouTube Shorts upload requests and responses."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class UploadCreateRequest(BaseModel):
    """User-editable metadata and publishing options for uploading an approved clip."""

    title: str | None = Field(default=None, max_length=100)
    description: str = Field(default="", max_length=4500)
    tags: list[str] = Field(default_factory=list, max_length=30)
    privacy: Literal["private", "unlisted", "public"] = "private"
    channel_id: uuid.UUID | None = None
    scheduled_at: datetime | None = None


class UploadResponse(BaseModel):
    """Upload status, YouTube link, and scheduling details."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    job_id: uuid.UUID
    clip_id: uuid.UUID
    youtube_channel_id: uuid.UUID | None = None
    title: str
    description: str
    tags: list[str] = Field(default_factory=list)
    privacy: str
    youtube_video_id: str | None = None
    youtube_url: str | None = None
    scheduled_at: datetime | None = None
    posted_at: datetime | None = None
    status: str
    error: str | None = None
    created_at: datetime
    updated_at: datetime
