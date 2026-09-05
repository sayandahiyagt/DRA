"""Staged->canonical publication-path regression (Wave 0, sayandahiyagt/dra#59).

Locks the *current* staged->canonical contract as a regression anchor so a future
schema wave cannot silently mutate it. Mirrors the DB-gate convention in
``tests/_db.py`` (SKIP when Postgres is unreachable — env concern, not a code
defect, spec §21). Reuses the proven ``build_lineage_bundle`` + ``publish_bundle``
path exercised by ``tests/test_atomic_commit.py``, but asserts the *count*
contract and the state-flip invariant explicitly rather than relying on a
coincidental equality.

Contract under test (src/dra/publish.py):
  - ``_DOMAIN_STATE_TABLES`` (raw_capture, source_capture, derived_artifact,
    evidence_unit, implementation_entity, claim) flip their own ``state`` column
    to ``'canonical'`` via ``_mirror_state_canonical``.
  - ``_STANDALONE_STATE_TABLES`` (``user_assertion``) is flipped separately and
    is untouched by a non-standalone (lineage) bundle.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text

from dra.publish import (
    PublishError,
    async_session,
    create_activity,
    publish_bundle,
    stage_bundle,
    stage_user_assertion,
)
from tests._db import DB
from tests._evidence import build_implementation_bundle, build_lineage_bundle, reset

pytestmark = DB

_DOMAIN_STATE_TABLES = (
    "raw_capture",
    "source_capture",
    "derived_artifact",
    "evidence_unit",
    "implementation_entity",
    "claim",
)
_STANDALONE_STATE_TABLES = ("user_assertion",)


def _run(coro):
    return asyncio.run(coro)


def test_publishes_exactly_canonical_domain_entities():
    """A well-formed lineage bundle publishes exactly its 6 domain entities and
    leaves zero staged prov_entity rows behind."""
    async def run():
        await reset()
        bundle_id, _ids = await build_lineage_bundle()
        published = await publish_bundle(bundle_id)

        # raw/derived/evidence/claim/decision/handoff — one prov_entity each.
        assert published == 6

        async with async_session() as session:
            staged = await session.scalar(
                text(
                    "SELECT count(*) FROM prov_entity "
                    "WHERE bundle_id = :b AND state = 'staged'"
                ),
                {"b": str(bundle_id)},
            )
            assert staged == 0
            canonical = await session.scalar(
                text(
                    "SELECT count(*) FROM prov_entity "
                    "WHERE bundle_id = :b AND state = 'canonical'"
                ),
                {"b": str(bundle_id)},
            )
            assert canonical == published == 6
    _run(run())


def test_domain_rows_flip_to_canonical():
    """Domain tables staged in a lineage bundle flip their own ``state`` column
    to 'canonical' (the ``_mirror_state_canonical`` contract), and the standalone
    ``user_assertion`` path is untouched by a non-standalone bundle.

    ``build_lineage_bundle`` stages raw_capture/source_capture, derived_artifact,
    evidence_unit and claim — the four domains reachable through the §21.2
    provenance chain.  ``implementation_entity`` is covered separately by
    :func:`test_implementation_entity_flips_to_canonical` via
    ``build_implementation_bundle`` (it is not part of a plain lineage bundle).
    """
    staged_domains = (
        "raw_capture",
        "source_capture",
        "derived_artifact",
        "evidence_unit",
        "claim",
    )
    async def run():
        await reset()
        bundle_id, _ids = await build_lineage_bundle()
        await publish_bundle(bundle_id)

        async with async_session() as session:
            for table in staged_domains:
                if table == "raw_capture":
                    join = "pe.content_hash = t.content_hash"
                elif table == "source_capture":
                    join = "pe.id = t.capture_id"
                else:
                    join = "pe.id = t.id"
                n = await session.scalar(
                    text(
                        f"SELECT count(*) FROM {table} t "
                        "JOIN prov_entity pe "
                        "ON pe.entity_kind = :kind "
                        "AND pe.bundle_id = :b "
                        "AND pe.state = 'canonical' "
                        f"AND {join} AND t.state = 'canonical'"
                    ),
                    {"kind": "raw_capture" if table in ("raw_capture", "source_capture") else table,
                     "b": str(bundle_id)},
                )
                assert n >= 1, f"{table} did not flip to canonical"

            # The standalone user_assertion table must be untouched by a lineage
            # (non-standalone) bundle: no rows, no state mutation.
            ua = await session.scalar(
                text("SELECT count(*) FROM user_assertion WHERE bundle_id = :b"),
                {"b": str(bundle_id)},
            )
            assert ua == 0
    _run(run())


def test_implementation_entity_flips_to_canonical():
    """``implementation_entity`` (a ``_DOMAIN_STATE_TABLES`` member staged by
    ``build_implementation_bundle``) flips its own ``state`` to canonical.
    Exercises the 0004_implementation_entity_state column on the publish path.
    """
    async def run():
        await reset()
        bundle_id, _ids = await build_implementation_bundle()
        await publish_bundle(bundle_id)

        async with async_session() as session:
            n = await session.scalar(
                text(
                    "SELECT count(*) FROM implementation_entity t "
                    "JOIN prov_entity pe "
                    "ON pe.entity_kind = 'implementation_entity' "
                    "AND pe.bundle_id = :b "
                    "AND pe.state = 'canonical' "
                    "AND pe.id = t.id AND t.state = 'canonical'"
                ),
                {"b": str(bundle_id)},
            )
            assert n >= 1, "implementation_entity did not flip to canonical"
    _run(run())


def test_standalone_user_assertion_publishes_via_mirror_path():
    """A bundle containing only a standalone ``user_assertion`` publishes
    through the ``_STANDALONE_STATE_TABLES`` mirror path (no prov_entity row)."""
    async def run():
        await reset()
        async with async_session() as session, session.begin():
            bundle_id = await stage_bundle(
                "run_ua", "task_ua", "standalone", {"kind": "human", "name": "tester"}
            )
            act = await create_activity(
                session, bundle_id, "publication", {"kind": "human", "name": "tester"}
            )
            ua_id = await stage_user_assertion(
                session, bundle_id, act, "USER_PREFERENCE",
                question="prefer X", value={"x": True},
            )
        published = await publish_bundle(bundle_id)
        # Standalone-only bundles report 0 prov_entity transitions (they carry
        # no prov_entity rows); the standalone mirror is the publication side
        # effect we assert below.
        assert published == 0

        async with async_session() as session:
            state = await session.scalar(
                text(
                    "SELECT state FROM user_assertion WHERE id = :id"
                ),
                {"id": str(ua_id)},
            )
            assert state == "canonical"
            # And it left no staged prov_entity rows.
            staged = await session.scalar(
                text(
                    "SELECT count(*) FROM prov_entity "
                    "WHERE bundle_id = :b AND state = 'staged'"
                ),
                {"b": str(bundle_id)},
            )
            assert staged == 0
    _run(run())


@pytest.mark.parametrize(
    "stage_kwargs",
    [
        pytest.param({"broken_claim": True}, id="broken-provenance-link"),
        pytest.param({"bad_hash": "tooshort"}, id="malformed-content-hash"),
    ],
)
def test_rejected_bundle_leaves_zero_canonical(stage_kwargs):
    """A rejected bundle raises PublishError and leaves zero canonical rows."""
    async def run():
        await reset()
        bundle_id, _ids = await build_lineage_bundle(**stage_kwargs)
        with pytest.raises(PublishError):
            await publish_bundle(bundle_id)

        async with async_session() as session:
            canon = await session.scalar(
                text(
                    "SELECT count(*) FROM prov_entity "
                    "WHERE bundle_id = :b AND state = 'canonical'"
                ),
                {"b": str(bundle_id)},
            )
            assert canon == 0
    _run(run())
