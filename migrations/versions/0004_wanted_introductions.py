"""Wanted introductions (#8): the team's wishlist of accounts / roles partners can claim.

Revision ID: 0004
Revises: 0003
Created: 2026-10-17

New table + one nullable column on introductions (which wishlist item a submission answers).
"""
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS crm_wanted_intros (
          id               bigserial PRIMARY KEY,
          account_id       int NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
          role_wanted      text NOT NULL CHECK (length(role_wanted) BETWEEN 2 AND 200),
          note             text CHECK (length(note) <= 1000),
          business_line_id int REFERENCES business_lines(id) ON DELETE SET NULL,
          status           text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
          created_by       int REFERENCES users(id) ON DELETE SET NULL,
          created_at       timestamptz NOT NULL DEFAULT now(),
          closed_at        timestamptz
        )""")
    op.execute("CREATE INDEX IF NOT EXISTS crm_wanted_intros_open ON crm_wanted_intros (status, created_at DESC)")
    op.execute("ALTER TABLE introductions ADD COLUMN IF NOT EXISTS wanted_id bigint "
               "REFERENCES crm_wanted_intros(id) ON DELETE SET NULL")


def downgrade() -> None:
    op.execute("ALTER TABLE introductions DROP COLUMN IF EXISTS wanted_id")
    op.execute("DROP TABLE IF EXISTS crm_wanted_intros")
