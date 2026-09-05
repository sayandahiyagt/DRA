"""v2 canonical schema baseline (Wave 0 + Wave 1a, sayandahiyagt/dra#59/#78).

Locks the Wave 1a canonical schema as the v2 ``knowledge_schema_version``
baseline so that any later schema wave which alters the canonical object set
without bumping the version fails this gate. SKIP if Postgres is unreachable
(env concern, not a code defect — see ``tests/_db.py`` / spec §21).

The v2 object set below adds the three Wave 1a content/capture tables
(``content_blob``, ``source_representation``, ``source_capture``) to the frozen
v1 set.  The signature assertion additionally locks the DB seed in ``0010``
against the accessor in ``dra.schema_version`` — a future wave that changes an
enum value or adds a table without a version bump changes
``dra.schema_version.canonical_v2_signature()``, which must then match a new
migration seed + version bump or this test fails.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text

from dra.schema_version import (
    canonical_v2_signature,
    V2_EXPECTED_TABLES,
)
from tests._db import DB, async_session

pytestmark = DB


async def _q(sql: str, params: dict | None = None) -> list[tuple]:
    async with async_session() as session:
        result = await session.execute(text(sql), params or {})
        return list(result.fetchall())


def _sync(sql: str, params: dict | None = None) -> list[tuple]:
    return asyncio.run(_q(sql, params))

# Frozen v2 canonical object set (see module docstring).  The three Wave 1a
# tables are added to the frozen v1 enumeration so the v2 baseline is pinned
# independently of later edits to the introspection test.
EXPECTED_TABLES = V2_EXPECTED_TABLES

EXPECTED_ENUMS = {
    "evidence_state": [
        "discovered", "fetched", "staged", "canonical", "verified",
        "superseded", "stale", "rejected",
    ],
    "source_kind": ["repo", "paper", "web", "doc", "pdf"],
    "raw_kind": ["html", "pdf", "repo_snapshot", "text", "image", "xml"],
    "artifact_kind": [
        "parsed", "normalized", "vector_embedding", "summary", "synthesis",
        "grobid_tei", "docling_document",
    ],
    "activity_type": [
        "acquisition", "parsing", "derivation", "verification",
        "publication", "synthesis", "human_correction", "visual_review",
    ],
    "entity_kind": [
        "raw_capture", "derived_artifact", "evidence_unit", "claim",
        "decision", "handoff", "gap", "implementation_entity",
    ],
    "relationship_type": [
        "related", "narrower", "broader", "implies", "contradicts", "synonym",
    ],
    "impl_kind": ["file", "symbol", "algorithm", "interface", "api"],
    "gap_severity": ["low", "medium", "high", "critical"],
    "agent_kind": ["human", "model", "tool", "organization"],
    "assertion_type": [
        "USER_PREFERENCE", "USER_CONSTRAINT", "USER_ASSERTION",
        "MAINTAINER_ASSERTION", "USER_CORRECTION", "USER_ACCEPTED_RISK",
    ],
}


def _run(coro):
    return asyncio.run(coro)


def test_knowledge_schema_version_is_v2():
    """The 0010 seed locks the canonical schema to v2 + its object-set digest."""
    async def run():
        async with async_session() as session:
            from dra.schema_version import current_schema_version

            version, label = await current_schema_version(session)
            assert (version, label) == (2, "v2")

            sig = (
                await session.scalar(
                    text(
                        "SELECT canonical_signature FROM knowledge_schema_version "
                        "WHERE version = 2"
                    )
                )
            )
            assert sig == canonical_v2_signature()
    _run(run())


def test_baseline_canonical_object_set():
    """Every v2 canonical table/enum (with its frozen value set) exists."""
    tables = {r[0] for r in _sync(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
    )}
    missing = [t for t in EXPECTED_TABLES if t not in tables]
    assert not missing, f"missing baseline tables: {missing}"

    enum_names = {r[0] for r in _sync(
        "SELECT typname FROM pg_type WHERE typtype = 'e'"
    )}
    for enum_name, values in EXPECTED_ENUMS.items():
        assert enum_name in enum_names, f"missing enum type: {enum_name}"
        got = [
            r[0]
            for r in _sync(
                f"SELECT enumlabel FROM pg_enum "
                f"WHERE enumtypid = '{enum_name}'::regtype "
                "ORDER BY enumsortorder"
            )
        ]
        assert set(got) == set(values), (
            f"{enum_name}: expected {set(values)}, got {set(got)}"
        )


def test_v1_signature_still_computable():
    """The v1 canonical signature accessor must remain stable for migration audit.

    A future wave that changes an enum value changes
    ``canonical_v1_signature()``; 0010 does not (enums are unchanged), so the
    v1 signature is identical to the v1 seed and remains the audit anchor for
    the v1→v2 transition.
    """
    from dra.schema_version import (
        V1_EXPECTED_TABLES,
        canonical_v1_signature,
        CANONICAL_SIGNATURE_V1,
    )
    assert canonical_v1_signature() == CANONICAL_SIGNATURE_V1
    assert set(V1_EXPECTED_TABLES).issubset(set(EXPECTED_TABLES))
