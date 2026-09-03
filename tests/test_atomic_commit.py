"""Transactional staged->canonical publication tests (ADR-013).

Positive path: a well-formed §21.2 lineage bundle commits entirely to
CANONICAL and is traversable.

Negative path: a bundle with a deliberately broken provenance link (a claim
with no evidence_unit) or a malformed content_hash raises ``PublishError`` and
leaves NO canonical rows for that bundle — proving the single transaction
rolls back the whole publish (no partial publish can orphan evidence).

SKIP if no DB is reachable (env concern); the rollback unit test is the
verification required by dra#14.
"""

from __future__ import annotations

import asyncio

import pytest

from dra.publish import PublishError, async_session, publish_bundle
from sqlalchemy import text

from tests._db import DB
from tests._evidence import build_lineage_bundle, reset

pytestmark = DB


def test_atomic_publish_positive():
    """A well-formed bundle commits entirely to CANONICAL."""
    async def run():
        await reset()
        bundle_id, ids = await build_lineage_bundle()
        published = await publish_bundle(bundle_id)
        # One staged prov_entity per domain lineage node (raw, derived,
        # evidence, claim, decision, handoff). Activities are not prov_entities.
        domain_entities = [
            "raw", "derived", "evidence", "claim", "decision", "handoff",
        ]
        assert published == len(domain_entities)

        async with async_session() as session:
            staged = await session.scalar(
                text("SELECT count(*) FROM prov_entity WHERE bundle_id = :b AND state = 'staged'"),
                {"b": str(bundle_id)},
            )
            assert staged == 0
            canonical = await session.scalar(
                text("SELECT count(*) FROM prov_entity WHERE bundle_id = :b AND state = 'canonical'"),
                {"b": str(bundle_id)},
            )
            assert canonical == published
    asyncio.run(run())


def test_atomic_publish_rollback_on_broken_link():
    """A claim with no evidence_unit raises and the whole bundle rolls back."""
    async def run():
        await reset()
        bundle_id, _ = await build_lineage_bundle(broken_claim=True)
        with pytest.raises(PublishError):
            await publish_bundle(bundle_id)

        async with async_session() as session:
            canon = await session.scalar(
                text("SELECT count(*) FROM prov_entity WHERE bundle_id = :b AND state = 'canonical'"),
                {"b": str(bundle_id)},
            )
            assert canon == 0
    asyncio.run(run())


def test_atomic_publish_rollback_on_hash_mismatch():
    """A malformed content_hash rolls back the entire publish."""
    async def run():
        await reset()
        bundle_id, _ = await build_lineage_bundle(bad_hash="tooshort")
        with pytest.raises(PublishError):
            await publish_bundle(bundle_id)

        async with async_session() as session:
            canon = await session.scalar(
                text("SELECT count(*) FROM prov_entity WHERE bundle_id = :b AND state = 'canonical'"),
                {"b": str(bundle_id)},
            )
            assert canon == 0
    asyncio.run(run())


def test_publish_idempotent():
    """Re-publishing an already-canonical bundle is a no-op (ADR-013)."""
    async def run():
        await reset()
        bundle_id, _ = await build_lineage_bundle()
        first = await publish_bundle(bundle_id)
        second = await publish_bundle(bundle_id)
        assert first == second
    asyncio.run(run())
