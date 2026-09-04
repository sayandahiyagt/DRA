"""Shared DB gate for the §38.1 bake-off test suite (mirrors tests/_db.py).

When Postgres is unreachable at DATABASE_URL the DB-gated tests SKIP instead of
FAIL — provisioning Postgres+pgvector is an environment concern, not a code
defect (spec §21).
"""
from __future__ import annotations

import asyncio

import pytest

from dra.db import can_connect
from dra.publish import async_session


def _db_reachable() -> bool:
    try:
        return asyncio.run(can_connect())
    except Exception:
        return False


DB = pytest.mark.skipif(
    not _db_reachable(),
    reason="No reachable Postgres at DATABASE_URL (skipped - env concern, not a code defect)",
)
