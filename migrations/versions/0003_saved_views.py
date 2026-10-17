"""Saved list views (#6): a user's named filter/sort combinations for the Deals and Introductions lists.

Revision ID: 0003
Revises: 0002
Created: 2026-10-17

New table only - nothing existing changes.
"""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS crm_saved_views (
          id         bigserial PRIMARY KEY,
          user_id    int NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          page       text NOT NULL CHECK (page IN ('deals', 'introductions')),
          name       text NOT NULL CHECK (length(name) BETWEEN 1 AND 60),
          state      jsonb NOT NULL DEFAULT '{}'::jsonb,
          created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE (user_id, page, name)
        )""")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS crm_saved_views")
