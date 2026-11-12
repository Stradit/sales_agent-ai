"""Platform Catalog Schema, Verified Sharing Infrastructure, and Account Origin.

Revision ID: 0013
Revises: 0012
Created: 2026-11-12
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Create platform_catalog schema
    op.execute("CREATE SCHEMA IF NOT EXISTS platform_catalog")

    # 2. Table: platform_catalog.companies
    op.create_table(
        "companies",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("canonical_key", sa.String(255), nullable=False, unique=True),
        sa.Column("legal_name", sa.String(500), nullable=True),
        sa.Column("display_name", sa.String(500), nullable=True),
        sa.Column("country", sa.String(100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        schema="platform_catalog",
    )
    op.create_index(
        "ix_companies_canonical_key",
        "companies",
        ["canonical_key"],
        schema="platform_catalog",
    )

    # 3. Table: platform_catalog.company_identifiers
    op.create_table(
        "company_identifiers",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "company_id",
            sa.BigInteger(),
            sa.ForeignKey("platform_catalog.companies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("id_type", sa.String(32), nullable=False),
        sa.Column("id_value", sa.String(255), nullable=False),
        sa.UniqueConstraint("id_type", "id_value", name="uq_company_id_type_val"),
        schema="platform_catalog",
    )
    op.create_index(
        "ix_company_identifiers_company_id",
        "company_identifiers",
        ["company_id"],
        schema="platform_catalog",
    )
    op.create_index(
        "ix_company_identifiers_type_val",
        "company_identifiers",
        ["id_type", "id_value"],
        schema="platform_catalog",
    )

    # 4. Table: platform_catalog.snapshots
    op.create_table(
        "snapshots",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "company_id",
            sa.BigInteger(),
            sa.ForeignKey("platform_catalog.companies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="ready"),
        sa.Column(
            "source_client_id",
            sa.Integer(),
            sa.ForeignKey("public.clients.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("source_account_id", sa.Integer(), nullable=True),
        sa.Column("policy_version", sa.String(32), nullable=False, server_default="1.0.0"),
        sa.Column("skeleton_version", sa.String(32), nullable=False, server_default="T0002"),
        sa.Column("quality_score", sa.Numeric(5, 2), nullable=True),
        sa.Column("quality_grade", sa.String(16), nullable=True),
        sa.Column("source_extracted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("taken_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("counts", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("checksum", sa.String(128), nullable=False),
        sa.UniqueConstraint("company_id", "version", name="uq_snapshot_company_version"),
        schema="platform_catalog",
    )
    op.create_index(
        "ix_snapshots_company_status",
        "snapshots",
        ["company_id", "status"],
        schema="platform_catalog",
    )
    op.create_index(
        "ix_snapshots_source_client_id",
        "snapshots",
        ["source_client_id"],
        schema="platform_catalog",
    )

    # 5. Table: platform_catalog.snapshot_blobs
    op.create_table(
        "snapshot_blobs",
        sa.Column(
            "snapshot_id",
            sa.BigInteger(),
            sa.ForeignKey("platform_catalog.snapshots.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        schema="platform_catalog",
    )

    # 6. Table: platform_catalog.share_events
    op.create_table(
        "share_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "snapshot_id",
            sa.BigInteger(),
            sa.ForeignKey("platform_catalog.snapshots.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "target_client_id",
            sa.Integer(),
            sa.ForeignKey("public.clients.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("mode", sa.String(32), nullable=False),  # push | request | refresh_pull
        sa.Column(
            "requested_by",
            sa.Integer(),
            sa.ForeignKey("public.users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "approved_by",
            sa.Integer(),
            sa.ForeignKey("public.users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="previewed"),
        sa.Column("idempotency_key", sa.String(128), unique=True, nullable=False),
        sa.Column("fee_credits", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("counts", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rolled_back_at", sa.DateTime(timezone=True), nullable=True),
        schema="platform_catalog",
    )
    op.create_index(
        "ix_share_events_target_status",
        "share_events",
        ["target_client_id", "status"],
        schema="platform_catalog",
    )
    op.create_index(
        "ix_share_events_snapshot_id",
        "share_events",
        ["snapshot_id"],
        schema="platform_catalog",
    )

    # 7. Add catalog flags to public.clients
    op.add_column("clients", sa.Column("catalog_publish", sa.Boolean(), server_default="false", nullable=False))
    op.add_column("clients", sa.Column("catalog_consume", sa.Boolean(), server_default="false", nullable=False))
    op.add_column("clients", sa.Column("reuse_requires_approval", sa.Boolean(), server_default="true", nullable=False))

    # 8. Create template table public.account_origin
    op.create_table(
        "account_origin",
        sa.Column(
            "account_id",
            sa.Integer(),
            sa.ForeignKey("accounts.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("origin_type", sa.String(32), server_default="fetched", nullable=False),
        sa.Column("canonical_key", sa.String(255), nullable=True),
        sa.Column("snapshot_id", sa.BigInteger(), nullable=True),
        sa.Column("snapshot_version", sa.Integer(), nullable=True),
        sa.Column("hydrated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "hydrated_by",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("local_edits", sa.Boolean(), server_default="false", nullable=False),
    )
    op.create_index("ix_account_origin_canonical_key", "account_origin", ["canonical_key"])
    op.create_index("ix_account_origin_snapshot_id", "account_origin", ["snapshot_id"])


def downgrade() -> None:
    # 1. Drop public.account_origin
    op.drop_index("ix_account_origin_snapshot_id", table_name="account_origin")
    op.drop_index("ix_account_origin_canonical_key", table_name="account_origin")
    op.drop_table("account_origin")

    # 2. Drop catalog columns from public.clients
    op.drop_column("clients", "reuse_requires_approval")
    op.drop_column("clients", "catalog_consume")
    op.drop_column("clients", "catalog_publish")

    # 3. Drop platform_catalog tables
    op.drop_table("share_events", schema="platform_catalog")
    op.drop_table("snapshot_blobs", schema="platform_catalog")
    op.drop_table("snapshots", schema="platform_catalog")
    op.drop_table("company_identifiers", schema="platform_catalog")
    op.drop_table("companies", schema="platform_catalog")

    # 4. Drop platform_catalog schema
    op.execute("DROP SCHEMA IF EXISTS platform_catalog CASCADE")
