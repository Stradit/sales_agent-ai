"""Phase C Foundations: share_events metrics and reuse_request_notifications outbox.

Revision ID: 0017
Revises: 0016
Created: 2026-11-13
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Extend platform_catalog.share_events with metrics & decision metadata
    op.add_column(
        "share_events",
        sa.Column("estimated_credits_saved", sa.Integer(), nullable=True),
        schema="platform_catalog",
    )
    op.add_column(
        "share_events",
        sa.Column("quality_score_at_event", sa.Numeric(5, 2), nullable=True),
        schema="platform_catalog",
    )
    op.add_column(
        "share_events",
        sa.Column("freshness_at_event", sa.String(16), nullable=True),
        schema="platform_catalog",
    )
    op.add_column(
        "share_events",
        sa.Column("fetch_fresh_credits_est", sa.Integer(), nullable=True),
        schema="platform_catalog",
    )
    op.add_column(
        "share_events",
        sa.Column("decided_by", sa.Integer(), sa.ForeignKey("public.users.id", ondelete="SET NULL"), nullable=True),
        schema="platform_catalog",
    )
    op.add_column(
        "share_events",
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        schema="platform_catalog",
    )
    op.add_column(
        "share_events",
        sa.Column("decision_note", sa.Text(), nullable=True),
        schema="platform_catalog",
    )

    # Migrate existing approved rows if present
    op.execute(
        sa.text("""
            UPDATE platform_catalog.share_events
            SET decided_by = approved_by,
                decided_at = completed_at
            WHERE approved_by IS NOT NULL AND decided_by IS NULL
        """)
    )

    # 2. Create platform_catalog.reuse_request_notifications
    op.create_table(
        "reuse_request_notifications",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.BigInteger(), sa.ForeignKey("platform_catalog.share_events.id", ondelete="CASCADE"), nullable=True),
        sa.Column("recipient_user_id", sa.Integer(), sa.ForeignKey("public.users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("recipient_scope", sa.String(32), nullable=False),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("target_client_id", sa.Integer(), sa.ForeignKey("public.clients.id", ondelete="CASCADE"), nullable=True),
        sa.Column("payload", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("status", sa.String(16), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "recipient_scope IN ('super_admin', 'client_admin')",
            name="ck_rrn_recipient_scope",
        ),
        sa.CheckConstraint(
            "channel IN ('in_app', 'email')",
            name="ck_rrn_channel",
        ),
        sa.CheckConstraint(
            "kind IN ('request_created', 'approved', 'rejected', 'failed', 'refresh_available')",
            name="ck_rrn_kind",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'sent', 'failed', 'read')",
            name="ck_rrn_status",
        ),
        schema="platform_catalog",
    )

    op.create_index(
        "idx_rrn_recipient_status",
        "reuse_request_notifications",
        ["recipient_user_id", "status"],
        schema="platform_catalog",
    )
    op.create_index(
        "idx_rrn_status_created",
        "reuse_request_notifications",
        ["status", "created_at"],
        schema="platform_catalog",
    )
    op.create_index(
        "idx_rrn_client_status",
        "reuse_request_notifications",
        ["target_client_id", "status"],
        schema="platform_catalog",
    )

    # 3. Credit ledger index for cursor pagination / transparency
    op.create_index(
        "idx_credit_ledger_client_created",
        "credit_ledger",
        ["client_id", sa.text("created_at DESC")],
        schema="public",
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index("idx_credit_ledger_client_created", table_name="credit_ledger", schema="public", if_exists=True)
    op.drop_table("reuse_request_notifications", schema="platform_catalog")
    op.drop_column("share_events", "decision_note", schema="platform_catalog")
    op.drop_column("share_events", "decided_at", schema="platform_catalog")
    op.drop_column("share_events", "decided_by", schema="platform_catalog")
    op.drop_column("share_events", "fetch_fresh_credits_est", schema="platform_catalog")
    op.drop_column("share_events", "freshness_at_event", schema="platform_catalog")
    op.drop_column("share_events", "quality_score_at_event", schema="platform_catalog")
    op.drop_column("share_events", "estimated_credits_saved", schema="platform_catalog")
