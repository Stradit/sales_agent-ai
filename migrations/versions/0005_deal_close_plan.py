"""Close plan (#10): dated milestones both sides agree on, per deal (a "mutual action plan").

Revision ID: 0005
Revises: 0004
Created: 2026-10-17

New table only.
"""
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS deal_close_plan (
          id          bigserial PRIMARY KEY,
          deal_id     int NOT NULL REFERENCES deals(id) ON DELETE CASCADE,
          title       text NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
          side        text NOT NULL DEFAULT 'us' CHECK (side IN ('us', 'customer', 'both')),
          owner_name  text CHECK (length(owner_name) <= 120),
          due_date    date,
          status      text NOT NULL DEFAULT 'todo' CHECK (status IN ('todo', 'done', 'blocked')),
          note        text CHECK (length(note) <= 1000),
          ordinal     int NOT NULL DEFAULT 0,
          done_at     timestamptz,
          created_by  int REFERENCES users(id) ON DELETE SET NULL,
          created_at  timestamptz NOT NULL DEFAULT now(),
          updated_at  timestamptz NOT NULL DEFAULT now()
        )""")
    op.execute("CREATE INDEX IF NOT EXISTS deal_close_plan_deal ON deal_close_plan (deal_id, ordinal)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS deal_close_plan")
