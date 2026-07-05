"""Add admin_settings table for storing app configuration.

Revision ID: 0004
Revises: 0003
Create Date: 2026-06-21
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "admin_settings",
        sa.Column("id", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("system_prompt", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("product_name", sa.String(255), nullable=False, server_default=sa.text("'Al-Mawsuat al-Deobandiyyah'")),
        sa.Column("primary_color", sa.String(7), nullable=False, server_default=sa.text("'#1D9E75'")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("id = 1", name="ck_admin_settings_single_row"),
    )

    op.execute(
        sa.text(
            "INSERT INTO admin_settings (id, system_prompt, product_name, primary_color) "
            "VALUES (1, '', 'Al-Mawsuat al-Deobandiyyah', '#1D9E75')"
        )
    )


def downgrade() -> None:
    op.drop_table("admin_settings")
