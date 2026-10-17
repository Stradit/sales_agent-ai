"""Buyer rooms (#11): a read-only link a rep shares with the customer, showing the close plan.

Revision ID: 0006
Revises: 0005
Created: 2026-10-17

The link token is never stored: token_hash (sha256) is used to look it up, token_enc (sealed with
the capture-token key) lets the rep copy the same link again. One active room per deal.
"""
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS deal_buyer_rooms (
          id               bigserial PRIMARY KEY,
          deal_id          int NOT NULL REFERENCES deals(id) ON DELETE CASCADE,
          token_hash       text NOT NULL UNIQUE,
          token_enc        bytea NOT NULL,
          title            text NOT NULL CHECK (length(title) BETWEEN 1 AND 160),
          welcome_note     text CHECK (length(welcome_note) <= 2000),
          resources        jsonb NOT NULL DEFAULT '[]'::jsonb,
          created_by       int REFERENCES users(id) ON DELETE SET NULL,
          created_at       timestamptz NOT NULL DEFAULT now(),
          expires_at       timestamptz NOT NULL,
          revoked_at       timestamptz,
          view_count       int NOT NULL DEFAULT 0,
          first_viewed_at  timestamptz,
          last_viewed_at   timestamptz
        )""")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS deal_buyer_rooms_one_active ON deal_buyer_rooms (deal_id) WHERE revoked_at IS NULL")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS deal_buyer_rooms")
