"""Shared DB gate for the evidence-graph test suite.

Reused verbatim from the scaffold's established convention
(``tests/test_db_connect.py``): when Postgres is unreachable at
``DATABASE_URL`` the DB-gated tests SKIP instead of FAIL, so the suite is
green-in-sandbox. Provisioning a live Postgres+pgvector is an environment
concern, not a code defect — see spec §21 and dra#14 PLAN §4.
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
    reason="No reachable Postgres at DATABASE_URL (skipped — env concern, not a code defect)",
)
