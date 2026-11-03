"""Clients table and multi-tenant credit foundation.

Revision ID: 0008
Revises: 0007
Created: 2026-10-29
"""
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Clients table in public schema
    op.execute("""
        CREATE TABLE IF NOT EXISTS clients (
            id                  SERIAL PRIMARY KEY,
            name                VARCHAR(500) NOT NULL,
            slug                VARCHAR(100) UNIQUE NOT NULL,
            legal_name          VARCHAR(500),
            schema_name         VARCHAR(120) UNIQUE NOT NULL,
            plan_tier           VARCHAR(50) NOT NULL DEFAULT 'standard',
            is_active           BOOLEAN NOT NULL DEFAULT TRUE,
            max_accounts        INTEGER NOT NULL DEFAULT 20,
            api_credits_total   INTEGER NOT NULL DEFAULT 0,
            api_credits_used    INTEGER NOT NULL DEFAULT 0,
            created_by_id       INTEGER REFERENCES users(id) ON DELETE SET NULL,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_clients_slug ON clients (slug)")

    # 2. Add credit tracking columns to accounts if missing
    op.execute("ALTER TABLE accounts ADD COLUMN IF NOT EXISTS api_credits_total INT NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE accounts ADD COLUMN IF NOT EXISTS api_credits_used INT NOT NULL DEFAULT 0")

    # 3. Add account_id FK to pipeline_runs if missing
    op.execute("ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS account_id INT REFERENCES accounts(id) ON DELETE SET NULL")
    op.execute("CREATE INDEX IF NOT EXISTS ix_pipeline_runs_account_id ON pipeline_runs (account_id)")

    # 4. Seed default client: Stradit India PVT LTD
    op.execute("""
        INSERT INTO clients (name, slug, legal_name, schema_name, plan_tier, is_active, max_accounts, api_credits_total, api_credits_used)
        VALUES ('Stradit India PVT LTD', 'stradit_india', 'Stradit India Private Limited', 'client_stradit_india', 'enterprise', TRUE, 50, 100000, 0)
        ON CONFLICT (slug) DO NOTHING
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS clients")
    op.execute("ALTER TABLE pipeline_runs DROP COLUMN IF EXISTS account_id")
    op.execute("ALTER TABLE accounts DROP COLUMN IF EXISTS api_credits_total")
    op.execute("ALTER TABLE accounts DROP COLUMN IF EXISTS api_credits_used")
