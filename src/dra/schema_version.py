"""Canonical knowledge-schema version accessor (Wave 0, sayandahiyagt/dra#59).

``knowledge_schema_version`` is the DB-stored *canonical* schema version —
distinct from the handoff-MANIFEST ``schema_version`` (``"1.0"``) in
``src/dra/handoff.py``. Future waves read :func:`current_schema_version` to gate
"is the DB at the expected schema version before writing new tables."

The frozen v1 object set (``V1_EXPECTED_TABLES`` / ``V1_EXPECTED_ENUMS``) is the
single source of truth for :func:`canonical_v1_signature`, which must match the
literal seeded by ``alembic/versions/0009_knowledge_schema_baseline.py``. The
regression tests in ``tests/test_schema_baseline.py`` lock that contract.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import text

__all__ = [
    "CANONICAL_SIGNATURE_V1",
    "CANONICAL_SIGNATURE_V2",
    "CANONICAL_SIGNATURE_V3",
    "SCHEMA_VERSION",
    "SCHEMA_VERSION_LABEL",
    "V1_EXPECTED_ENUMS",
    "V1_EXPECTED_TABLES",
    "V2_EXPECTED_TABLES",
    "V3_EXPECTED_TABLES",
    "canonical_v1_signature",
    "canonical_v2_signature",
    "canonical_v3_signature",
    "current_schema_version",
]

# The Wave 0 canonical schema version (frozen at HEAD 5272ffdad267):
#   0001_enable_pgvector -> ... -> 0008_interview_constraints (merge of the
#   0007 double-head) -> 0009_knowledge_schema_baseline (this migration).
SCHEMA_VERSION: int = 3
SCHEMA_VERSION_LABEL: str = "v3"

# Frozen v1 canonical object set. This is the exact enumeration asserted by
# ``tests/test_schema_introspection.py`` (including the 0007
# ``grobid_tei``/``docling_document``/``visual_review`` additions and the 0008
# ``assertion_type`` enum). A later wave that changes the canonical object set
# without bumping ``knowledge_schema_version`` produces signature drift, which
# ``tests/test_schema_baseline.py`` detects.
V1_EXPECTED_TABLES: list[str] = [
    "prov_agent",
    "prov_bundle",
    "prov_activity",
    "prov_entity",
    "prov_generation",
    "prov_derivation",
    "source_identity",
    "raw_capture",
    "derived_artifact",
    "evidence_unit",
    "topic",
    "topic_relationship",
    "claim",
    "implementation_entity",
    "decision",
    "gap",
    "handoff_statement",
    "user_assertion",
]

# Wave 1a (dra#78): the v2 canonical object set adds the content/capture model
# tables on top of the frozen v1 set.  Enums are unchanged (no new enum values),
# so V1_EXPECTED_ENUMS is reused for the v2 signature computation.
V2_EXPECTED_TABLES: list[str] = V1_EXPECTED_TABLES + [
    "content_blob",
    "source_representation",
    "source_capture",
]

# Wave 1b (dra#79): the v3 canonical object set adds ``source_candidate`` — the
# §140 discovery-result table that separates search-snippet/discovery emissions
# from EvidenceUnit→Claim.  A new canonical table mechanically bumps the
# ``knowledge_schema_version`` anchor to v3 (see schema_version.py:44-46).
V3_EXPECTED_TABLES: list[str] = V2_EXPECTED_TABLES + [
    "source_candidate",
]

V1_EXPECTED_ENUMS: dict[str, list[str]] = {
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

# Precomputed digest of the v1 object set (mirrors the migration seed). The
# accessor recomputes it from ``_object_set_payload``; this constant is the
# frozen witness the migration stored at 0009.
CANONICAL_SIGNATURE_V1: str = (
    "51242702e1e6b1d570c886500234a43eec81cd1d72211c5e254868afde18aacb"
)

# Precomputed digest of the v2 object set (V1 tables + the three Wave 1a
# content/capture tables).  Mirrors the seed inserted by ``0010_
# source_capture_model``; ``tests/test_schema_baseline.py`` asserts the DB row
# matches.
CANONICAL_SIGNATURE_V2: str = (
    "60123f40db96eed5780c84289494c0286dd8d513692f1ce56e1d6a52eebed5ac"
)

# Precomputed digest of the v3 object set (V2 tables + ``source_candidate`` from
# Wave 1b / dra#79).  Mirrors the seed inserted by ``0011_source_candidate``;
# ``tests/test_schema_baseline.py`` asserts the DB row matches.
CANONICAL_SIGNATURE_V3: str = (
    "dd099e1c80609ab226f43354ae2640167802990e55b4a3d136fe08dad220a554"
)


def _object_set_payload(tables: list[str]) -> bytes:
    """Stable JSON encoding of a canonical object set (sorted)."""
    return json.dumps(
        {
            "tables": sorted(tables),
            "enums": {
                name: sorted(values) for name, values in V1_EXPECTED_ENUMS.items()
            },
        },
        sort_keys=True,
    ).encode("utf-8")


def canonical_v1_signature() -> str:
    """SHA-256 digest of the frozen v1 canonical object set.

    The migration 0009 seeds ``knowledge_schema_version.canonical_signature``
    with this same digest; ``tests/test_schema_baseline.py`` asserts the DB row
    matches, locking the accessor against the migration seed.
    """
    return hashlib.sha256(_object_set_payload(V1_EXPECTED_TABLES)).hexdigest()


def canonical_v2_signature() -> str:
    """SHA-256 digest of the v2 canonical object set.

    The v2 set is ``V1_EXPECTED_TABLES`` plus the three Wave 1a tables
    (``content_blob``, ``source_representation``, ``source_capture``).
    Migration 0010 seeds ``knowledge_schema_version`` with this digest;
    ``tests/test_schema_baseline.py`` asserts the DB row matches.
    """
    return hashlib.sha256(_object_set_payload(V2_EXPECTED_TABLES)).hexdigest()


def canonical_v3_signature() -> str:
    """SHA-256 digest of the v3 canonical object set.

    The v3 set is ``V2_EXPECTED_TABLES`` plus ``source_candidate`` (Wave 1b,
    dra#79).  Migration 0011 seeds ``knowledge_schema_version`` with this digest;
    ``tests/test_schema_baseline.py`` asserts the DB row matches.
    """
    return hashlib.sha256(_object_set_payload(V3_EXPECTED_TABLES)).hexdigest()


async def current_schema_version(session: Any) -> tuple[int, str]:
    """Read the current canonical schema version from the DB.

    Returns ``(version, label)``. Raises :class:`LookupError` if the
    ``knowledge_schema_version`` table is empty (i.e. 0009 has not seeded it).
    Async-session compatible — call inside an ``async_session()`` context, the
    same pattern :mod:`dra.publish` and its test suite use.
    """
    row = await session.execute(
        text(
            "SELECT version, label FROM knowledge_schema_version "
            "ORDER BY version DESC LIMIT 1"
        )
    )
    rec = row.fetchone()
    if rec is None:
        raise LookupError("knowledge_schema_version table has no rows")
    return int(rec[0]), str(rec[1])
