"""Harden Rights Gate: campaign proof & attestation, permission scope.

Revision ID: 0003_harden_rights_gate
Revises: 0002_add_uploads_table
Create Date: 2026-11-19 14:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_harden_rights_gate"
down_revision: str | Sequence[str] | None = "0002_add_uploads_table"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "campaigns",
        sa.Column("proof_url", sa.String(length=1024), nullable=True),
    )
    op.add_column(
        "campaigns",
        sa.Column("proof_document_path", sa.String(length=1024), nullable=True),
    )
    op.add_column(
        "campaigns",
        sa.Column("proof_document_sha256", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "campaigns",
        sa.Column("attested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "permissions",
        sa.Column("scope", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("permissions", "scope")
    op.drop_column("campaigns", "attested_at")
    op.drop_column("campaigns", "proof_document_sha256")
    op.drop_column("campaigns", "proof_document_path")
    op.drop_column("campaigns", "proof_url")
