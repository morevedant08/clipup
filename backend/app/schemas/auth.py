"""Pydantic schemas for authentication and connected YouTube channels."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class YouTubeChannelResponse(BaseModel):
    """Public representation of a connected YouTube channel (never exposes tokens)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    channel_id: str
    title: str
    handle: str | None = None
    thumbnail_url: str | None = None
    scopes: str
    upload_enabled: bool = False
    connected_at: datetime


class UserResponse(BaseModel):
    """Authenticated user profile with connected YouTube channels."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    name: str | None = None
    avatar_url: str | None = None
    created_at: datetime
    youtube_channels: list[YouTubeChannelResponse] = Field(default_factory=list)


class AuthTokenResponse(BaseModel):
    """JWT access token paired with user profile."""

    access_token: str
    token_type: str = "bearer"
    user: UserResponse


class OAuthUrlResponse(BaseModel):
    """OAuth authorization redirect URL."""

    auth_url: str


class DevLoginRequest(BaseModel):
    """Request payload for optional development-only quick login."""

    email: EmailStr
    name: str | None = "Dev Creator"

