"""Clipping job ORM model and status / rights enums."""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from sqlalchemy import ForeignKey, Integer, JSON, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.audit_log import AuditLog
    from app.models.campaign import Campaign
    from app.models.clip import Clip
    from app.models.user import User


class JobStatus(StrEnum):
    """Allowed job lifecycle states."""

    PENDING_RIGHTS = "pending_rights"
    REJECTED = "rejected"
    QUEUED = "queued"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"


class RightsBasis(StrEnum):
    """Allowed legal rights bases for clipping a source video."""

    OWNER = "owner"
    CAMPAIGN = "campaign"
    CC = "cc"
    PERMISSION = "permission"


class Job(TimestampMixin, Base):
    """Represents a user request to clip a YouTube video after passing the Rights Gate."""

    __tablename__ = "jobs"

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
    source_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    youtube_video_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    source_channel_id: Mapped[str | None] = mapped_column(
        String(64), index=True, nullable=True
    )
    source_title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    source_duration_sec: Mapped[int | None] = mapped_column(Integer, nullable=True)
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("campaigns.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    rights_basis: Mapped[str | None] = mapped_column(String(32), nullable=True)
    rights_evidence_json: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        default=dict,
        nullable=False,
    )
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    settings_json: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        default=dict,
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(32),
        default=JobStatus.PENDING_RIGHTS.value,
        index=True,
        nullable=False,
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    user: Mapped[User] = relationship(back_populates="jobs")
    campaign: Mapped[Campaign | None] = relationship(back_populates="jobs")
    clips: Mapped[list[Clip]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        order_by="Clip.start_sec",
    )
    audit_logs: Mapped[list[AuditLog]] = relationship(back_populates="job")
