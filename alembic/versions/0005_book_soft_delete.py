"""book soft delete

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-23
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("books", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_books_deleted_at", "books", ["deleted_at"])


def downgrade() -> None:
    op.drop_index("ix_books_deleted_at", table_name="books")
    op.drop_column("books", "deleted_at")
