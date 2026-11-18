"""Campaign and CampaignSource ORM models."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, utcnow

if TYPE_CHECKING:
    from app.models.job import Job
    from app.models.user import User


class Campaign(TimestampMixin, Base):
    """Paid clipping campaign defining authorized source videos/channels, rules, and deadline."""

    __tablename__ = "campaigns"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    rules_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    owner: Mapped[User] = relationship(back_populates="campaigns")
    sources: Mapped[list[CampaignSource]] = relationship(
        back_populates="campaign",
        cascade="all, delete-orphan",
    )
    jobs: Mapped[list[Job]] = relationship(back_populates="campaign")


class CampaignSource(Base):
    """Authorized source URL (video or channel) belonging to a clipping campaign."""

    __tablename__ = "campaign_sources"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("campaigns.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    source_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    normalized_video_id: Mapped[str | None] = mapped_column(
        String(32), index=True, nullable=True
    )
    source_channel_id: Mapped[str | None] = mapped_column(
        String(64), index=True, nullable=True
    )
    source_type: Mapped[str] = mapped_column(String(20), nullable=False, default="video")
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        nullable=False,
    )

    campaign: Mapped[Campaign] = relationship(back_populates="sources")
