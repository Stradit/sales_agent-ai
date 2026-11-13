"""Allow cancelled status in share_events.

Revision ID: 0019
Revises: 0018
Created: 2026-11-13
"""
from alembic import op
import sqlalchemy as sa

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("chk_share_events_status", "share_events", schema="platform_catalog", type_="check")
    op.create_check_constraint(
        "chk_share_events_status",
        "share_events",
        "status IN ('previewed', 'pending_approval', 'running', 'done', 'failed', 'rejected', 'rolled_back', 'cancelled')",
        schema="platform_catalog",
    )


def downgrade() -> None:
    op.drop_constraint("chk_share_events_status", "share_events", schema="platform_catalog", type_="check")
    op.create_check_constraint(
        "chk_share_events_status",
        "share_events",
        "status IN ('previewed', 'pending_approval', 'running', 'done', 'failed', 'rejected', 'rolled_back')",
        schema="platform_catalog",
    )
