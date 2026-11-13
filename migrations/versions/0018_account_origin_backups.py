"""Create public.account_origin_backups template table and local_edited_at columns.

Revision ID: 0018
Revises: 0017
Created: 2026-11-13
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Add local_edited_at tracking columns to tenant entity templates
    op.add_column("lobs", sa.Column("local_edited_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("sub_lobs", sa.Column("local_edited_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("personas", sa.Column("local_edited_at", sa.DateTime(timezone=True), nullable=True))

    # 2. Create public.account_origin_backups template table
    op.create_table(
        "account_origin_backups",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "account_id",
            sa.Integer(),
            sa.ForeignKey("accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("snapshot_version_before", sa.Integer(), nullable=True),
        sa.Column("snapshot_version_after", sa.Integer(), nullable=True),
        sa.Column("strategy", sa.String(32), nullable=False),
        sa.Column("backup_payload", JSONB(), nullable=False),
        sa.Column(
            "performed_by",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_account_origin_backups_account_created",
        "account_origin_backups",
        ["account_id", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_account_origin_backups_account_created", table_name="account_origin_backups")
    op.drop_table("account_origin_backups")
    op.drop_column("personas", "local_edited_at")
    op.drop_column("sub_lobs", "local_edited_at")
    op.drop_column("lobs", "local_edited_at")
