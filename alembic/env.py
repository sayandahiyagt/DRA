"""Alembic migration environment.

Reads the shared ``DATABASE_URL`` (the same default the app uses in
``src/dra/db.py``) so migrations target the same Postgres+pgvector instance the
LangGraph control plane checkpoints against.

The engine is async (psycopg3), so migrations are driven via
``connection.run_sync`` per Alembic's supported async pattern. The baseline
migration is extension-only (``CREATE EXTENSION IF NOT EXISTS vector``) — no
domain tables are defined in this scaffold.
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg://postgres:postgres@localhost:5432/postgres",
)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (no live connection needed)."""
    context.configure(
        url=DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online_async() -> None:
    """Run migrations in 'online' mode against the async engine."""
    connectable: AsyncEngine = create_async_engine(
        DATABASE_URL,
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as conn:
        await conn.run_sync(do_run_migrations)


def do_run_migrations(connection):
    """Sync callback executed on the async connection."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    asyncio.run(run_migrations_online_async())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
