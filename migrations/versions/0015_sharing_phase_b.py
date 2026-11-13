"""Phase B: Platform catalog deduplication, merge candidates, checksum versioning, and credit ledger references.

Revision ID: 0015
Revises: 0014
Created: 2026-11-13
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Add merged_into_id and needs_review to platform_catalog.companies
    op.add_column(
        "companies",
        sa.Column(
            "merged_into_id",
            sa.BigInteger(),
            sa.ForeignKey("platform_catalog.companies.id", ondelete="SET NULL"),
            nullable=True,
        ),
        schema="platform_catalog",
    )
    op.add_column(
        "companies",
        sa.Column(
            "needs_review",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        schema="platform_catalog",
    )

    # 2. Create platform_catalog.merge_candidates
    op.create_table(
        "merge_candidates",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "company_a_id",
            sa.BigInteger(),
            sa.ForeignKey("platform_catalog.companies.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "company_b_id",
            sa.BigInteger(),
            sa.ForeignKey("platform_catalog.companies.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column("reason", sa.String(255), nullable=False),
        sa.Column("evidence", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("status", sa.String(32), server_default="pending", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
        sa.Column(
            "resolved_by",
            sa.Integer(),
            sa.ForeignKey("public.users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        schema="platform_catalog",
    )

    # Partial index on merge_candidates(status) WHERE status = 'pending'
    op.create_index(
        "ix_merge_candidates_pending",
        "merge_candidates",
        ["status"],
        postgresql_where=sa.text("status = 'pending'"),
        schema="platform_catalog",
    )

    # 3. Add checksum_version and policy_hash to platform_catalog.snapshots
    op.add_column(
        "snapshots",
        sa.Column("checksum_version", sa.Integer(), server_default="1", nullable=False),
        schema="platform_catalog",
    )
    op.add_column(
        "snapshots",
        sa.Column("policy_hash", sa.String(64), nullable=True),
        schema="platform_catalog",
    )

    # 4. Add ref_type, ref_id, and balance_after to public.credit_ledger
    op.add_column("credit_ledger", sa.Column("ref_type", sa.String(64), nullable=True))
    op.add_column("credit_ledger", sa.Column("ref_id", sa.BigInteger(), nullable=True))
    op.add_column("credit_ledger", sa.Column("balance_after", sa.Integer(), nullable=True))
    op.create_index("ix_credit_ledger_ref", "credit_ledger", ["ref_type", "ref_id"])

    # 5. Add check constraint on share_events.status
    # Verify no unknown status values exist first
    conn = op.get_bind()
    conn.execute(sa.text("""
        UPDATE platform_catalog.share_events
        SET status = 'failed'
        WHERE status NOT IN ('previewed', 'pending_approval', 'running', 'done', 'failed', 'rejected', 'rolled_back')
    """))
    op.create_check_constraint(
        "chk_share_events_status",
        "share_events",
        "status IN ('previewed', 'pending_approval', 'running', 'done', 'failed', 'rejected', 'rolled_back')",
        schema="platform_catalog",
    )


def downgrade() -> None:
    # 5. Drop check constraint on share_events.status
    op.drop_constraint("chk_share_events_status", "share_events", schema="platform_catalog", type_="check")

    # 4. Drop columns from credit_ledger
    op.drop_index("ix_credit_ledger_ref", table_name="credit_ledger")
    op.drop_column("credit_ledger", "balance_after")
    op.drop_column("credit_ledger", "ref_id")
    op.drop_column("credit_ledger", "ref_type")

    # 3. Drop columns from snapshots
    op.drop_column("snapshots", "policy_hash", schema="platform_catalog")
    op.drop_column("snapshots", "checksum_version", schema="platform_catalog")

    # 2. Drop merge_candidates table
    op.drop_index("ix_merge_candidates_pending", table_name="merge_candidates", schema="platform_catalog")
    op.drop_table("merge_candidates", schema="platform_catalog")

    # 1. Drop columns from companies
    op.drop_column("companies", "needs_review", schema="platform_catalog")
    op.drop_column("companies", "merged_into_id", schema="platform_catalog")
