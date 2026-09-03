"""Checkpoint test for mission #13 bootstrap.

Gate: when Postgres+pgvector is provisioned (per the verification checkpoint in
PLAN_1.md), the suite must prove (a) a DB connection opens and (b) the
``vector`` extension is registered (i.e. ``alembic upgrade head`` ran).

If no database is reachable at DATABASE_URL, the test is SKIPPED rather than
failed — provisioning the DB is an environment concern, not a code defect. When
the database *is* reachable but the extension is missing, the test FAILS, which
means the migration baseline was not applied.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from dra.db import DATABASE_URL, can_connect, has_vector_extension


def _db_reachable() -> bool:
    try:
        return asyncio.run(can_connect())
    except Exception:
        return False


def test_db_config_default():
    """The shared DATABASE_URL defaults to the psycopg3 pgvector driver."""
    assert DATABASE_URL.startswith("postgresql+psycopg://")


@pytest.mark.skipif(not _db_reachable(), reason="No reachable Postgres at DATABASE_URL")
def test_can_open_connection():
    """A connection to Postgres can be opened."""
    assert asyncio.run(can_connect()) is True


@pytest.mark.skipif(not _db_reachable(), reason="No reachable Postgres at DATABASE_URL")
def test_vector_extension_present():
    """The pgvector baseline migration has been applied."""
    assert asyncio.run(has_vector_extension()) is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
