"""Tests for the §34 retrieval contract (``dra.knowledge``).

- No-DB tests run always (key validation + constant surface).
- DB-gated tests (``@DB``) stage a canonical §21.2 lineage bundle via
  ``tests._evidence`` and assert :func:`retrieve_context_bundle` returns a
  bounded bundle per §34 key. They require Postgres; they SKIP (env concern,
  not a code defect) in the no-DB sandbox — matching the repo's established
  convention (``tests/_db.py``).
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from dra.knowledge import (
    RETRIEVAL_KEY_TYPES,
    retrieve_context_bundle,
)


def _check_bundle(bundle: dict) -> None:
    """Assert a retrieval result carries all §34 bundle fields."""
    for key in (
        "immediate_objective",
        "user_constraints",
        "architecture_decisions",
        "implementation_entities",
        "high_value_claims",
        "evidence_locators",
        "unresolved_gaps",
        "tests_acceptance",
    ):
        assert key in bundle, f"bundle missing field {key!r}"


def _db_reachable() -> bool:
    try:
        from dra.db import can_connect

        return asyncio.run(can_connect())
    except Exception:
        return False


DB = pytest.mark.skipif(
    not _db_reachable(),
    reason="No reachable Postgres at DATABASE_URL (skipped — env concern, not a code defect)",
)


@pytest.fixture
def _isolated_async_engine(monkeypatch):
    """Per-test NullPool async engine patching every async_session reference."""
    from dra.db import DATABASE_URL

    engine = create_async_engine(DATABASE_URL, poolclass=NullPool, future=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    import sys

    import dra.db as _dbmod
    import dra.investigators as _investigators
    import dra.publish as _publish
    from tests import _evidence

    monkeypatch.setattr(_publish, "async_session", session_factory)
    monkeypatch.setattr(_investigators, "async_session", session_factory)
    monkeypatch.setattr(_evidence, "async_session", session_factory)
    monkeypatch.setattr(_dbmod, "engine", engine)
    monkeypatch.setattr(sys.modules[__name__], "async_session", session_factory)
    yield engine
    try:
        loop = asyncio.new_event_loop()
        loop.run_until_complete(engine.dispose())
        loop.close()
    except Exception:
        pass


def async_session():
    from dra.publish import async_session as _as

    return _as()


# ---------------------------------------------------------------------------
# 1. No-DB: constant + key validation (always green)
# ---------------------------------------------------------------------------


def test_retrieval_key_types_constant():
    # §34 lists 7 spec keys ("repo path/symbol" is one bullet). We expose 8
    # concrete key types (repo_path + symbol split) — assert the 7 spec keys are
    # all supported plus the symbol sub-form.
    spec_seven = {
        "requirement",
        "topic",
        "entity",
        "milestone",
        "repo_path",
        "decision",
        "semantic",
    }
    assert spec_seven.issubset(set(RETRIEVAL_KEY_TYPES))
    assert "symbol" in RETRIEVAL_KEY_TYPES
    assert len(RETRIEVAL_KEY_TYPES) == 8


def test_retrieve_rejects_multiple_keys():
    with pytest.raises(ValueError, match="exactly one"):
        asyncio.run(
            retrieve_context_bundle(
                run_id="run-x",
                by={"decision": str(uuid.uuid4()), "topic": str(uuid.uuid4())},
            )
        )


def test_retrieve_rejects_no_keys():
    with pytest.raises(ValueError, match="exactly one"):
        asyncio.run(retrieve_context_bundle(run_id="run-x", by={}))


def test_retrieve_rejects_unknown_key():
    with pytest.raises(ValueError, match="unknown retrieval key"):
        asyncio.run(
            retrieve_context_bundle(
                run_id="run-x", by={"bogus_key": "x"}
            )
        )


def test_bundle_bounds_constant():
    from dra.knowledge import bundle_bounds

    b = bundle_bounds()
    assert b["claims"] == 50
    assert b["evidence"] == 50
    assert b["entities"] == 20


# ---------------------------------------------------------------------------
# 2. DB-gated: §34 retrieval over a canonical lineage bundle
# ---------------------------------------------------------------------------


@DB
@pytest.mark.usefixtures("_isolated_async_engine")
def test_retrieve_context_bundle_by_decision():
    """by=decision returns a bounded bundle with the decision + evidence."""
    from tests._evidence import build_lineage_bundle, reset

    async def run():
        await reset()
        run_id = "run-know-dec"
        bundle_id, ids = await build_lineage_bundle(run_id=run_id)
        from dra.publish import publish_bundle

        await publish_bundle(str(bundle_id))  # staged -> canonical

        async with async_session() as s:
            bundle = await retrieve_context_bundle(
                session=s, run_id=run_id, by={"decision": str(ids["decision"])}
            )
        assert bundle["architecture_decisions"], "no architecture_decisions for decision"
        assert bundle["evidence_locators"], "no evidence_locators for decision"
        assert bundle["architecture_decisions"][0]["id"] == str(ids["decision"])
        # bounded
        assert len(bundle["architecture_decisions"]) <= 50
        return bundle

    bundle = asyncio.run(run())
    _check_bundle(bundle)


@DB
@pytest.mark.usefixtures("_isolated_async_engine")
def test_retrieve_context_bundle_by_repo_path():
    """by=repo_path returns matching implementation_entities (bounded)."""
    from tests._evidence import build_implementation_bundle, reset

    async def run():
        await reset()
        run_id = "run-know-repo"
        bundle_id, _ids = await build_implementation_bundle(run_id=run_id)
        from dra.publish import publish_bundle

        await publish_bundle(str(bundle_id))

        async with async_session() as s:
            path = (
                await s.execute(
                    text(
                        "SELECT ie.path FROM implementation_entity ie "
                        "JOIN prov_entity pe ON pe.entity_kind='implementation_entity' "
                        "AND pe.id=ie.id JOIN prov_bundle pb ON pb.id=pe.bundle_id "
                        "WHERE pb.run_id=:r AND pe.state='canonical' LIMIT 1"
                    ),
                    {"r": run_id},
                )
            ).scalar_one()

        async with async_session() as s:
            bundle = await retrieve_context_bundle(
                session=s, run_id=run_id, by={"repo_path": str(path)}
            )
        assert bundle["implementation_entities"], "no implementation_entities for repo_path"
        assert all(e["path"] == str(path) for e in bundle["implementation_entities"])
        assert len(bundle["implementation_entities"]) <= 20
        return bundle

    bundle = asyncio.run(run())
    _check_bundle(bundle)


@DB
@pytest.mark.usefixtures("_isolated_async_engine")
def test_retrieve_context_bundle_by_semantic():
    """by=semantic returns a bounded set of matching claims/evidence (no vector)."""
    from tests._evidence import build_lineage_bundle, reset

    async def run():
        await reset()
        run_id = "run-know-sem"
        bundle_id, _ids = await build_lineage_bundle(run_id=run_id)
        from dra.publish import publish_bundle

        await publish_bundle(str(bundle_id))

        async with async_session() as s:
            bundle = await retrieve_context_bundle(
                session=s, run_id=run_id, by={"semantic": "claims something"}
            )
        assert bundle["high_value_claims"], "semantic query matched no claims"
        assert len(bundle["high_value_claims"]) <= 50
        return bundle

    bundle = asyncio.run(run())
    _check_bundle(bundle)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
