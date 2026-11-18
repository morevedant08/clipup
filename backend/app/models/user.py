"""User ORM model."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.campaign import Campaign
    from app.models.job import Job
    from app.models.permission import Permission
    from app.models.youtube_channel import YouTubeChannel


class User(TimestampMixin, Base):
    """Registered Clipper user authenticated via Google OAuth."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    google_sub: Mapped[str | None] = mapped_column(
        String(255), unique=True, index=True, nullable=True
    )
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    youtube_channels: Mapped[list[YouTubeChannel]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )
    campaigns: Mapped[list[Campaign]] = relationship(
        back_populates="owner",
        cascade="all, delete-orphan",
    )
    permissions: Mapped[list[Permission]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )
    jobs: Mapped[list[Job]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )
