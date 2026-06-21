"""Add checkpoint, heartbeat_at, task_id, retry_count to processing_jobs

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-20

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("processing_jobs", sa.Column("checkpoint", JSONB(), nullable=True))
    op.add_column("processing_jobs", sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("processing_jobs", sa.Column("task_id", sa.String(100), nullable=True))
    op.add_column("processing_jobs", sa.Column("retry_count", sa.Integer(), nullable=False, server_default=sa.text("0")))


def downgrade() -> None:
    op.drop_column("processing_jobs", "retry_count")
    op.drop_column("processing_jobs", "task_id")
    op.drop_column("processing_jobs", "heartbeat_at")
    op.drop_column("processing_jobs", "checkpoint")
