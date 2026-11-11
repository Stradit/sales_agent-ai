"""Add client lifecycle state machine and skeleton version tracking.

Revision ID: 0011
Revises: 0010
Created: 2026-11-11
"""
from alembic import op
import sqlalchemy as sa

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Add lifecycle state machine & skeleton tracking columns to clients table
    op.add_column("clients", sa.Column("status", sa.String(50), server_default="active", nullable=False))
    op.add_column("clients", sa.Column("skeleton_version", sa.String(32), server_default="T0001", nullable=False))
    op.add_column("clients", sa.Column("provisioned_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("clients", sa.Column("suspended_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("clients", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("clients", sa.Column("refresh_cron", sa.String(64), server_default="0 0 * * *", nullable=False))
    op.add_column("clients", sa.Column("max_concurrent_jobs", sa.Integer(), server_default="3", nullable=False))
    op.add_column("clients", sa.Column("last_error", sa.Text(), nullable=True))

    op.create_index("ix_clients_status", "clients", ["status"])

    # 2. Backfill existing active clients: set provisioned_at to created_at or NOW()
    op.execute("""
        UPDATE clients
        SET status = CASE WHEN is_active = TRUE THEN 'active' ELSE 'suspended' END,
            skeleton_version = 'T0001',
            provisioned_at = COALESCE(created_at, NOW()),
            refresh_cron = '0 0 * * *',
            max_concurrent_jobs = 3
        WHERE provisioned_at IS NULL
    """)


def downgrade() -> None:
    op.drop_index("ix_clients_status", table_name="clients")
    op.drop_column("clients", "last_error")
    op.drop_column("clients", "max_concurrent_jobs")
    op.drop_column("clients", "refresh_cron")
    op.drop_column("clients", "deleted_at")
    op.drop_column("clients", "suspended_at")
    op.drop_column("clients", "provisioned_at")
    op.drop_column("clients", "skeleton_version")
    op.drop_column("clients", "status")
