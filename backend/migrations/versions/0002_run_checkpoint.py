"""批次断点续跑：archive_runs 增加 resumed_from_id / checkpoint_seq / checkpoint。

Revision ID: 0002_run_checkpoint
Revises: 0001_initial
Create Date: 2026-11-12
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002_run_checkpoint"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

SCHEMA = "docflow"


def upgrade() -> None:
    op.add_column(
        "archive_runs",
        sa.Column(
            "resumed_from_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.archive_runs.id"),
            nullable=True,
        ),
        schema=SCHEMA,
    )
    op.add_column(
        "archive_runs",
        sa.Column("checkpoint_seq", sa.Integer(), nullable=False, server_default="0"),
        schema=SCHEMA,
    )
    op.add_column(
        "archive_runs",
        sa.Column(
            "checkpoint",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_archive_runs_resumed_from_id",
        "archive_runs",
        ["resumed_from_id"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_index("ix_archive_runs_resumed_from_id", table_name="archive_runs", schema=SCHEMA)
    op.drop_column("archive_runs", "checkpoint", schema=SCHEMA)
    op.drop_column("archive_runs", "checkpoint_seq", schema=SCHEMA)
    op.drop_column("archive_runs", "resumed_from_id", schema=SCHEMA)
