"""Scheduled jobs: settings per job and a run log (services/scheduler.py).

scheduled_jobs       one row per job: on/off, how often, job options (e.g. which source channels)
scheduled_job_runs   every run, scheduled or "Run now": when, how long, result counts, error

Revision ID: 0008
Revises: 0007
Created: 2026-10-30
"""
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS scheduled_jobs (
          name            text PRIMARY KEY,
          enabled         boolean NOT NULL DEFAULT true,
          interval_hours  numeric(6, 2) NOT NULL DEFAULT 6 CHECK (interval_hours >= 1 AND interval_hours <= 168),
          options         jsonb NOT NULL DEFAULT '{}',
          updated_at      timestamptz NOT NULL DEFAULT now(),
          updated_by      int REFERENCES users(id) ON DELETE SET NULL
        )""")
    op.execute("""
        CREATE TABLE IF NOT EXISTS scheduled_job_runs (
          id            bigserial PRIMARY KEY,
          job           text NOT NULL,
          trigger       text NOT NULL DEFAULT 'schedule' CHECK (trigger IN ('schedule', 'manual')),
          triggered_by  int REFERENCES users(id) ON DELETE SET NULL,
          started_at    timestamptz NOT NULL DEFAULT now(),
          finished_at   timestamptz,
          status        text NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'ok', 'failed', 'skipped')),
          summary       jsonb NOT NULL DEFAULT '{}',
          error         text
        )""")
    op.execute("CREATE INDEX IF NOT EXISTS scheduled_job_runs_job ON scheduled_job_runs (job, started_at DESC)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS scheduled_job_runs")
    op.execute("DROP TABLE IF EXISTS scheduled_jobs")
