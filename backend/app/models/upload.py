"""YouTube Short upload record ORM model."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, JSON, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.clip import Clip
    from app.models.job import Job
    from app.models.user import User
    from app.models.youtube_channel import YouTubeChannel


class UploadStatus(StrEnum):
    """Lifecycle states for uploading an approved clip to YouTube."""

    QUEUED = "queued"
    UPLOADING = "uploading"
    UPLOADED = "uploaded"
    FAILED = "failed"


class UploadPrivacy(StrEnum):
    """Allowed YouTube privacy settings per upload."""

    PRIVATE = "private"
    UNLISTED = "unlisted"
    PUBLIC = "public"


class Upload(TimestampMixin, Base):
    """Tracks a user-approved upload (or scheduled upload) of a Clip to YouTube Shorts."""

    __tablename__ = "uploads"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    clip_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("clips.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    youtube_channel_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("youtube_channels.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    title: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tags_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    privacy: Mapped[str] = mapped_column(
        String(20),
        default=UploadPrivacy.PRIVATE.value,
        nullable=False,
    )
    youtube_video_id: Mapped[str | None] = mapped_column(
        String(32),
        index=True,
        nullable=True,
    )
    scheduled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        index=True,
        nullable=True,
    )
    posted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        default=UploadStatus.QUEUED.value,
        index=True,
        nullable=False,
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    user: Mapped[User] = relationship()
    job: Mapped[Job] = relationship()
    clip: Mapped[Clip] = relationship(back_populates="uploads")
    youtube_channel: Mapped[YouTubeChannel | None] = relationship()
