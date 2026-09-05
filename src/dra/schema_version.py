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
    "SCHEMA_VERSION",
    "SCHEMA_VERSION_LABEL",
    "CANONICAL_SIGNATURE_V1",
    "V1_EXPECTED_TABLES",
    "V1_EXPECTED_ENUMS",
    "canonical_v1_signature",
    "current_schema_version",
]

# The Wave 0 canonical schema version (frozen at HEAD 5272ffdad267):
#   0001_enable_pgvector -> ... -> 0008_interview_constraints (merge of the
#   0007 double-head) -> 0009_knowledge_schema_baseline (this migration).
SCHEMA_VERSION: int = 1
SCHEMA_VERSION_LABEL: str = "v1"

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


def _object_set_payload() -> bytes:
    """Stable JSON encoding of the frozen v1 object set (sorted)."""
    return json.dumps(
        {
            "tables": sorted(V1_EXPECTED_TABLES),
            "enums": {name: sorted(values) for name, values in V1_EXPECTED_ENUMS.items()},
        },
        sort_keys=True,
    ).encode("utf-8")


def canonical_v1_signature() -> str:
    """SHA-256 digest of the frozen v1 canonical object set.

    The migration 0009 seeds ``knowledge_schema_version.canonical_signature``
    with this same digest; ``tests/test_schema_baseline.py`` asserts the DB row
    matches, locking the accessor against the migration seed.
    """
    return hashlib.sha256(_object_set_payload()).hexdigest()


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
