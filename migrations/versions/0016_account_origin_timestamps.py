"""Add source_extracted_at and snapshot_taken_at to public.account_origin template table.

Revision ID: 0016
Revises: 0015
Created: 2026-11-13
"""
from alembic import op
import sqlalchemy as sa

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("account_origin", sa.Column("source_extracted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("account_origin", sa.Column("snapshot_taken_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("account_origin", "snapshot_taken_at")
    op.drop_column("account_origin", "source_extracted_at")
