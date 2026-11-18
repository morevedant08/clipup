"""Pydantic schemas for Campaign and CampaignSource CRUD endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CampaignSourceCreate(BaseModel):
    """Payload to attach an authorized YouTube video or channel source to a campaign."""

    source_url: str = Field(..., min_length=10, max_length=1024)
    title: str | None = Field(default=None, max_length=255)
    source_type: Literal["video", "channel"] | None = None


class CampaignSourceResponse(BaseModel):
    """Authorized campaign source returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    campaign_id: uuid.UUID
    source_url: str
    normalized_video_id: str | None = None
    source_channel_id: str | None = None
    source_type: str
    title: str | None = None
    created_at: datetime


class CampaignCreate(BaseModel):
    """Payload to create a new paid clipping campaign with sources, rules, and deadline."""

    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=4000)
    rules_text: str = Field(default="", max_length=10000)
    deadline: datetime | None = None
    is_active: bool = True
    sources: list[CampaignSourceCreate] = Field(default_factory=list)


class CampaignUpdate(BaseModel):
    """Partial update payload for an existing campaign."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=4000)
    rules_text: str | None = Field(default=None, max_length=10000)
    deadline: datetime | None = None
    is_active: bool | None = None
    sources: list[CampaignSourceCreate] | None = None


class CampaignResponse(BaseModel):
    """Campaign detail response including its authorized sources."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    owner_user_id: uuid.UUID
    name: str
    description: str | None = None
    rules_text: str
    deadline: datetime | None = None
    is_active: bool
    created_at: datetime
    updated_at: datetime
    sources: list[CampaignSourceResponse] = Field(default_factory=list)

