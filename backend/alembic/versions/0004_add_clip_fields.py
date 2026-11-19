"""Add clip reason, transcript excerpt, and metadata_json fields.

Revision ID: 0004_add_clip_fields
Revises: 0003_harden_rights_gate
Create Date: 2026-11-19 15:30:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_add_clip_fields"
down_revision: str | Sequence[str] | None = "0003_harden_rights_gate"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "clips",
        sa.Column("reason", sa.Text(), nullable=True),
    )
    op.add_column(
        "clips",
        sa.Column("transcript_excerpt", sa.Text(), nullable=True),
    )
    op.add_column(
        "clips",
        sa.Column(
            "metadata_json",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("clips", "metadata_json")
    op.drop_column("clips", "transcript_excerpt")
    op.drop_column("clips", "reason")
