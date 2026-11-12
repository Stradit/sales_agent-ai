"""Rollback audit columns: rolled_back_by and rollback_reason.

Revision ID: 0014
Revises: 0013
Created: 2026-11-12
"""
from alembic import op
import sqlalchemy as sa

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "share_events",
        sa.Column(
            "rolled_back_by",
            sa.Integer(),
            sa.ForeignKey("public.users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        schema="platform_catalog",
    )
    op.add_column(
        "share_events",
        sa.Column("rollback_reason", sa.Text(), nullable=True),
        schema="platform_catalog",
    )


def downgrade() -> None:
    op.drop_column("share_events", "rollback_reason", schema="platform_catalog")
    op.drop_column("share_events", "rolled_back_by", schema="platform_catalog")
