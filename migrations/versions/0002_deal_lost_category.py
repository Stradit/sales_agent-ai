"""Structured lost reasons: a category and the competitor that won, next to the free-text note.

Revision ID: 0002
Revises: 0001
Created: 2026-10-17

Additive only: two nullable columns, no rewrite. Deals lost before this keep their text reason
and show as "Not categorised" until someone picks a category.
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

# Keep in step with LOST_CATEGORIES in apps/sales_deals/api.py
CATEGORIES = ("price", "timing", "competitor", "no_decision", "no_budget", "product_fit", "relationship", "other")


def upgrade() -> None:
    op.execute("ALTER TABLE deals ADD COLUMN IF NOT EXISTS lost_category text")
    op.execute("ALTER TABLE deals ADD COLUMN IF NOT EXISTS lost_competitor text")
    allowed = ", ".join(f"'{c}'" for c in CATEGORIES)
    # NOT VALID: no scan of existing rows under the lock; then VALIDATE with a weaker lock
    op.execute(f"ALTER TABLE deals ADD CONSTRAINT deals_lost_category_check "
               f"CHECK (lost_category IS NULL OR lost_category IN ({allowed})) NOT VALID")
    op.execute("ALTER TABLE deals VALIDATE CONSTRAINT deals_lost_category_check")


def downgrade() -> None:
    op.execute("ALTER TABLE deals DROP CONSTRAINT IF EXISTS deals_lost_category_check")
    op.execute("ALTER TABLE deals DROP COLUMN IF EXISTS lost_competitor")
    op.execute("ALTER TABLE deals DROP COLUMN IF EXISTS lost_category")
