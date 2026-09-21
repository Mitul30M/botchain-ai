from logging.config import fileConfig

from sqlalchemy import create_engine, pool

import app.models  # noqa: F401
from alembic import context
from app.db import postgres_uri
from app.models.base import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


_PRISMA_TABLES = frozenset({"_prisma_migrations"})

_LANGGRAPH_TABLES = frozenset({
    "checkpoint_migrations",
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
})

_EXCLUDED_TABLES = _PRISMA_TABLES | _LANGGRAPH_TABLES


def include_object(object, name, type_, reflected, compare_to):
    if getattr(object, "schema", None) not in (None, "public"):
        return False
    if type_ == "table" and name in _EXCLUDED_TABLES:
        return False
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=postgres_uri("psycopg"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(postgres_uri("psycopg"), poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()