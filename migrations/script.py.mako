"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Created: ${create_date}

Rules (docs/DB_DEPLOYMENT_PLAN.md): backward-compatible with the app version currently running
(expand → migrate → contract); no table rewrites; CREATE INDEX CONCURRENTLY in its own migration
with transactional DDL off; backfills in batches.
"""
from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
