"""Alembic environment. Migrations are hand-written (no autogenerate): the schema mixes ORM models and
raw SQL, so autogenerate would propose wrong changes. See docs/DB_DEPLOYMENT_PLAN.md."""
from alembic import context
from sqlalchemy import create_engine, pool, text

import config

# Tables owned by apps/content_pipeline, which shares this database — never touched by these migrations
OTHER_APP_TABLES = {"targets", "run_history"}

# Fail fast instead of queueing behind live traffic: a migration that can't get its lock within
# LOCK_TIMEOUT stops (and is retried by the next deploy) rather than freezing the app's queries.
LOCK_TIMEOUT = "10s"
STATEMENT_TIMEOUT = "30min"


def include_object(obj, name, type_, reflected, compare_to):
    return not (type_ == "table" and name in OTHER_APP_TABLES)


def run_migrations_offline() -> None:
    context.configure(url=config.DATABASE_URL, target_metadata=None, literal_binds=True,
                      include_object=include_object, transaction_per_migration=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(config.DATABASE_URL, poolclass=pool.NullPool)
    with engine.connect() as connection:
        connection.execute(text(f"SET lock_timeout = '{LOCK_TIMEOUT}'"))
        connection.execute(text(f"SET statement_timeout = '{STATEMENT_TIMEOUT}'"))
        connection.commit()
        context.configure(connection=connection, target_metadata=None, include_object=include_object,
                          transaction_per_migration=True)   # each migration commits on its own
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
