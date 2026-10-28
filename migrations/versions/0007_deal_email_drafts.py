"""Deal email drafts table for tracking generated AI email history per deal.

Revision ID: 0007
Revises: 0006
Created: 2026-10-23
"""
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS deal_email_drafts (
          id              bigserial PRIMARY KEY,
          deal_id         bigint NOT NULL REFERENCES deals(id) ON DELETE CASCADE,
          user_id         int REFERENCES users(id) ON DELETE SET NULL,
          action_code     text,
          stage           text,
          recipient_name  text,
          recipient_email text,
          subject         text NOT NULL,
          body            text NOT NULL,
          model           text,
          created_at      timestamptz NOT NULL DEFAULT now()
        )""")
    op.execute("CREATE INDEX IF NOT EXISTS deal_email_drafts_deal ON deal_email_drafts (deal_id, created_at DESC)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS deal_email_drafts")
