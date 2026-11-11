"""User client access mapping table and user assignments.

Revision ID: 0010
Revises: 0009
Created: 2026-10-29
"""
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. UserClientAccess table in public schema
    op.execute("""
        CREATE TABLE IF NOT EXISTS user_client_access (
            id                  SERIAL PRIMARY KEY,
            user_id             INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            client_id           INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
            role                VARCHAR(50) NOT NULL DEFAULT 'analyst',
            granted_by_id       INTEGER REFERENCES users(id) ON DELETE SET NULL,
            granted_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_user_client_access UNIQUE (user_id, client_id)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_user_client_access_user_id ON user_client_access (user_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_user_client_access_client_id ON user_client_access (client_id)")

    # 2. Assign all active users to Stradit India PVT LTD
    op.execute("""
        INSERT INTO user_client_access (user_id, client_id, role, granted_at)
        SELECT 
            u.id,
            c.id,
            CASE WHEN u.role = 'super_admin' THEN 'client_admin' ELSE 'analyst' END,
            NOW()
        FROM users u
        CROSS JOIN clients c
        WHERE c.slug = 'stradit_india'
          AND u.is_active = TRUE
        ON CONFLICT (user_id, client_id) DO NOTHING
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS user_client_access")
