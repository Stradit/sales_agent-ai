"""Sync local_edited_at, account_origin timestamps, and backups table to all existing tenant schemas.

Revision ID: 0020
Revises: 0019
Created: 2026-11-13
"""
from alembic import op
import sqlalchemy as sa

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    schemas = conn.execute(sa.text(
        "SELECT schema_name FROM information_schema.schemata WHERE schema_name LIKE 'client_%'"
    )).fetchall()

    for (schema,) in schemas:
        conn.execute(sa.text(f'ALTER TABLE "{schema}"."account_origin" ADD COLUMN IF NOT EXISTS "source_extracted_at" timestamptz'))
        conn.execute(sa.text(f'ALTER TABLE "{schema}"."account_origin" ADD COLUMN IF NOT EXISTS "snapshot_taken_at" timestamptz'))
        conn.execute(sa.text(f'ALTER TABLE "{schema}"."lobs" ADD COLUMN IF NOT EXISTS "local_edited_at" timestamptz'))
        conn.execute(sa.text(f'ALTER TABLE "{schema}"."sub_lobs" ADD COLUMN IF NOT EXISTS "local_edited_at" timestamptz'))
        conn.execute(sa.text(f'ALTER TABLE "{schema}"."personas" ADD COLUMN IF NOT EXISTS "local_edited_at" timestamptz'))
        conn.execute(sa.text(
            f'CREATE TABLE IF NOT EXISTS "{schema}"."account_origin_backups" '
            f'(LIKE public."account_origin_backups" INCLUDING ALL)'
        ))


def downgrade() -> None:
    pass
