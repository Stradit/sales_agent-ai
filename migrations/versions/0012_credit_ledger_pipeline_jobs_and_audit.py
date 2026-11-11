"""Add credit ledger, persistent pipeline jobs table, and client audit logging.

Revision ID: 0012
Revises: 0011
Created: 2026-11-11
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Create credit_ledger table for atomic quota and credit audit trail
    op.create_table(
        "credit_ledger",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("client_id", sa.Integer(), sa.ForeignKey("clients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=True),
        sa.Column("run_id", sa.String(128), nullable=True),
        sa.Column("delta", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_credit_ledger_client_created", "credit_ledger", ["client_id", "created_at"])
    op.create_index("ix_credit_ledger_run_id", "credit_ledger", ["run_id"])

    # 2. Create pipeline_jobs table for restart-durable background job tracking
    op.create_table(
        "pipeline_jobs",
        sa.Column("job_id", sa.String(64), primary_key=True),
        sa.Column("client_slug", sa.String(64), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=True),
        sa.Column("job_type", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), server_default="queued", nullable=False),
        sa.Column("progress_pct", sa.Integer(), server_default="0", nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("credits_reserved", sa.Integer(), server_default="0", nullable=False),
        sa.Column("credits_used", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("meta", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_pipeline_jobs_client_status", "pipeline_jobs", ["client_slug", "status"])
    op.create_index("ix_pipeline_jobs_created_at", "pipeline_jobs", ["created_at"])

    # 3. Add client_id to audit_log table
    op.add_column("audit_log", sa.Column("client_id", sa.Integer(), sa.ForeignKey("clients.id", ondelete="SET NULL"), nullable=True))
    op.create_index("ix_audit_log_client_id", "audit_log", ["client_id"])


def downgrade() -> None:
    op.drop_index("ix_audit_log_client_id", table_name="audit_log")
    op.drop_column("audit_log", "client_id")
    op.drop_index("ix_pipeline_jobs_created_at", table_name="pipeline_jobs")
    op.drop_index("ix_pipeline_jobs_client_status", table_name="pipeline_jobs")
    op.drop_table("pipeline_jobs")
    op.drop_index("ix_credit_ledger_run_id", table_name="credit_ledger")
    op.drop_index("ix_credit_ledger_client_created", table_name="credit_ledger")
    op.drop_table("credit_ledger")
