"""Add uploads table for Phase 3 YouTube Shorts publishing and scheduling.

Revision ID: 0002_add_uploads_table
Revises: 0001_initial_schema
Create Date: 2026-11-18 21:30:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_add_uploads_table"
down_revision: str | Sequence[str] | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "uploads",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "user_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "job_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "clip_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("clips.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "youtube_channel_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("youtube_channels.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("title", sa.String(length=100), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("tags_json", sa.JSON(), nullable=False),
        sa.Column(
            "privacy",
            sa.String(length=20),
            nullable=False,
            server_default="private",
        ),
        sa.Column("youtube_video_id", sa.String(length=32), nullable=True),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="queued",
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_uploads_user_id", "uploads", ["user_id"])
    op.create_index("ix_uploads_job_id", "uploads", ["job_id"])
    op.create_index("ix_uploads_clip_id", "uploads", ["clip_id"])
    op.create_index("ix_uploads_youtube_channel_id", "uploads", ["youtube_channel_id"])
    op.create_index("ix_uploads_youtube_video_id", "uploads", ["youtube_video_id"])
    op.create_index("ix_uploads_scheduled_at", "uploads", ["scheduled_at"])
    op.create_index("ix_uploads_status", "uploads", ["status"])


def downgrade() -> None:
    op.drop_table("uploads")
