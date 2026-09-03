"""Postgres + pgvector connectivity for DRA.

ADR-001: LangGraph is the orchestration substrate; its Postgres checkpointer
lands control-plane state here.
ADR-003: PostgreSQL + pgvector is the MVP store for retrieval metadata.

This module provides a single shared ``DATABASE_URL`` (the same one Alembic
targets in ``alembic/env.py``), an async SQLAlchemy engine, helpers to ensure
the ``vector`` extension exists, and a CLI (`dra-db-check`) that proves a
connection can be opened. No domain tables are defined here.
"""

from __future__ import annotations

import asyncio
import os
import sys

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg://postgres:postgres@localhost:5432/postgres",
)

engine: AsyncEngine = create_async_engine(
    DATABASE_URL,
    echo=False,
    future=True,
)


async def can_connect() -> bool:
    """Open a connection and run ``SELECT 1``. Returns True on success."""
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


async def ensure_pgvector_extension() -> bool:
    """Create the ``vector`` extension if it does not exist."""
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector;"))
    return True


async def has_vector_extension() -> bool:
    """Return True if the ``vector`` extension is installed in the DB."""
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT 1 FROM pg_extension WHERE extname = 'vector';"
            )
        )
        return result.scalar() is not None


def main() -> None:
    """CLI entry point: verify connectivity and the pgvector extension."""
    ok = asyncio.run(can_connect())
    if not ok:
        print("FAIL: could not open a database connection to", DATABASE_URL)
        sys.exit(1)

    try:
        has_ext = asyncio.run(has_vector_extension())
    except Exception as exc:  # pragma: no cover - defensive
        print(f"FAIL: error checking pgvector extension: {exc}")
        sys.exit(1)

    status = "present" if has_ext else "MISSING"
    print(f"OK: database connection open at {DATABASE_URL}; pgvector extension: {status}")
    if not has_ext:
        sys.exit(2)


if __name__ == "__main__":  # pragma: no cover
    main()
