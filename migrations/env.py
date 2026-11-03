"""Alembic environment for multi-tenant database architecture.
Runs migrations for the public platform schema and provides multi-schema
iteration across per-client schemas (e.g. client_stradit_india).

Supported options:
- Default: runs against public schema: `alembic upgrade head`
- Target specific client schema: `alembic -x schema=client_stradit_india upgrade head`
- Run across all active client schemas: `alembic -x all_clients=true upgrade head`
"""
from alembic import context
from sqlalchemy import create_engine, pool, text

import config

# Tables owned by apps/content_pipeline, which shares this database — never touched by these migrations
OTHER_APP_TABLES = {"targets", "run_history"}

LOCK_TIMEOUT = "10s"
STATEMENT_TIMEOUT = "30min"


def include_object(obj, name, type_, reflected, compare_to):
    return not (type_ == "table" and name in OTHER_APP_TABLES)


def run_migrations_offline() -> None:
    x_args = context.get_x_argument(as_dictionary=True)
    target_schema = x_args.get("schema", "public")

    context.configure(
        url=config.DATABASE_URL,
        target_metadata=None,
        literal_binds=True,
        include_object=include_object,
        transaction_per_migration=True,
        version_table="alembic_version",
        version_table_schema=target_schema,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(config.DATABASE_URL, poolclass=pool.NullPool)
    with engine.connect() as connection:
        connection.execute(text(f"SET lock_timeout = '{LOCK_TIMEOUT}'"))
        connection.execute(text(f"SET statement_timeout = '{STATEMENT_TIMEOUT}'"))
        connection.commit()

        x_args = context.get_x_argument(as_dictionary=True)
        target_schema = x_args.get("schema", "public")
        run_all_clients = x_args.get("all_clients", "false").lower() in ("true", "1")

        if run_all_clients:
            # Multi-schema mode: run on public first, then iterate all active client schemas
            schemas = ["public"]
            try:
                client_schemas = connection.execute(
                    text("SELECT schema_name FROM public.clients WHERE is_active = TRUE")
                ).scalars().all()
                schemas.extend(client_schemas)
            except Exception as e:
                print(f"[Alembic Multi-Schema] Note: could not query active clients: {e}")

            for sc in schemas:
                print(f"[Alembic Multi-Schema] Running migrations on schema: {sc}")
                connection.execute(text(f'SET search_path TO "{sc}", public'))
                connection.commit()
                context.configure(
                    connection=connection,
                    target_metadata=None,
                    include_object=include_object,
                    transaction_per_migration=True,
                    version_table="alembic_version",
                    version_table_schema=sc,
                )
                with context.begin_transaction():
                    context.run_migrations()
        else:
            # Targeted single-schema mode (public or designated client schema)
            if target_schema != "public":
                connection.execute(text(f'SET search_path TO "{target_schema}", public'))
                connection.commit()

            context.configure(
                connection=connection,
                target_metadata=None,
                include_object=include_object,
                transaction_per_migration=True,
                version_table="alembic_version",
                version_table_schema=target_schema,
            )
            with context.begin_transaction():
                context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
