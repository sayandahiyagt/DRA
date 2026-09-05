"""Schema introspection assertions for dra#14.

When Postgres+pgvector is provisioned, asserts the canonical evidence-graph
schema (enums, provenance graph tables, and the §4 lineage-chain domain tables)
exists in ``pg_catalog``/``information_schema`` with the expected primary keys
and key foreign-key constraints.

SKIP if no DB is reachable (env concern); FAIL fast if reachable but the
schema is missing — which means ``alembic upgrade head`` was not run.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text

from tests._db import DB, async_session

pytestmark = DB

EXPECTED_TABLES = [
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
    "content_blob",
    "source_representation",
    "source_capture",
]

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
        "USER_PREFERENCE",
        "USER_CONSTRAINT",
        "USER_ASSERTION",
        "MAINTAINER_ASSERTION",
        "USER_CORRECTION",
        "USER_ACCEPTED_RISK",
    ],
}

# table -> a column that must appear in its primary key
EXPECTED_PKS = {
    "raw_capture": "content_hash",       # content-addressed PK (ADR-004)
    "content_blob": "hash",               # content-addressed PK (Wave 1a)
    "source_capture": "capture_id",       # capture event PK (Wave 1a)
    "source_representation": "id",        # representation PK (Wave 1a)
    "prov_entity": "id",
    "prov_generation": "entity_id",     # composite PK
    "prov_derivation": "derived_entity_id",  # composite PK

    "derived_artifact": "id",
    "evidence_unit": "id",
    "implementation_entity": "id",
    "claim": "id",
    "decision": "id",
    "gap": "id",
    "handoff_statement": "id",
    "topic": "id",
    "topic_relationship": "id",
    "prov_agent": "id",
    "prov_bundle": "id",
    "prov_activity": "id",
    "source_identity": "id",
    "user_assertion": "id",
}

# (src_table, src_col, tgt_table, tgt_col) key foreign keys that must exist
KEY_FKS = [
    ("prov_activity", "bundle_id", "prov_bundle"),
    ("prov_activity", "agent_id", "prov_agent"),
    ("prov_entity", "bundle_id", "prov_bundle"),
    ("prov_entity", "produced_by_activity", "prov_activity"),
    ("prov_generation", "entity_id", "prov_entity"),
    ("prov_generation", "activity_id", "prov_activity"),
    ("prov_derivation", "derived_entity_id", "prov_entity"),
    ("prov_derivation", "source_entity_id", "prov_entity"),
    ("prov_derivation", "activity_id", "prov_activity"),
    ("raw_capture", "source_id", "source_identity"),
    ("derived_artifact", "source_capture_hash", "content_blob"),
    ("derived_artifact", "produced_by_activity", "prov_activity"),
    ("derived_artifact", "superseded_by", "derived_artifact"),
    ("evidence_unit", "artifact_id", "derived_artifact"),
    ("evidence_unit", "produced_by_activity", "prov_activity"),
    ("claim", "evidence_unit_id", "evidence_unit"),
    ("claim", "topic_id", "topic"),
    ("claim", "produced_by_activity", "prov_activity"),
    ("claim", "superseded_by", "claim"),
    ("topic", "parent_topic_id", "topic"),
    ("topic_relationship", "source_topic_id", "topic"),
    ("topic_relationship", "target_topic_id", "topic"),
    ("implementation_entity", "repo_source_id", "source_identity"),
    ("implementation_entity", "produced_by_activity", "prov_activity"),
    ("decision", "claim_id", "claim"),
    ("decision", "topic_id", "topic"),
    ("decision", "produced_by_activity", "prov_activity"),
    ("gap", "topic_id", "topic"),
    ("gap", "decision_id", "decision"),
    ("gap", "produced_by_activity", "prov_activity"),
    ("handoff_statement", "decision_id", "decision"),
    ("handoff_statement", "produced_by_activity", "prov_activity"),
    ("user_assertion", "bundle_id", "prov_bundle"),
    ("user_assertion", "produced_by_activity", "prov_activity"),
    ("user_assertion", "superseded_by", "user_assertion"),
    ("user_assertion", "disputed_claim_id", "claim"),
    ("user_assertion", "disputed_decision_id", "decision"),
    ("user_assertion", "disputed_source_id", "source_identity"),
    # Wave 1a content/capture model FKs
    ("source_representation", "content_blob_hash", "content_blob"),
    ("source_capture", "source_identity_id", "source_identity"),
    ("source_capture", "representation_id", "source_representation"),
    ("source_capture", "content_blob_hash", "content_blob"),
]


async def _q(sql: str, params: dict | None = None) -> list[tuple]:
    async with async_session() as session:
        result = await session.execute(text(sql), params or {})
        return list(result.fetchall())


def _sync(sql: str, params: dict | None = None) -> list[tuple]:
    return asyncio.run(_q(sql, params))


def test_all_tables_exist():
    names = {r[0] for r in _sync(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
    )}
    missing = [t for t in EXPECTED_TABLES if t not in names]
    assert not missing, f"missing tables: {missing}"


def test_enum_types_and_values():
    names = {r[0] for r in _sync(
        "SELECT typname FROM pg_type WHERE typtype = 'e'"
    )}
    for enum_name, values in EXPECTED_ENUMS.items():
        assert enum_name in names, f"missing enum type: {enum_name}"
        got = [r[0] for r in _sync(
            f"SELECT enumlabel FROM pg_enum "
            f"WHERE enumtypid = '{enum_name}'::regtype "
            "ORDER BY enumsortorder"
        )]
        assert set(got) == set(values), f"{enum_name}: expected {set(values)}, got {set(got)}"


def test_primary_keys():
    for table, pk_col in EXPECTED_PKS.items():
        cols = [r[0] for r in _sync(
            f"SELECT a.attname FROM pg_index i "
            f"JOIN pg_attribute a ON a.attrelid = i.indrelid "
            f"AND a.attnum = ANY(i.indkey) "
            f"WHERE i.indrelid = '{table}'::regclass AND i.indisprimary"
        )]
        assert pk_col in cols, f"{table} PK expected to include {pk_col}; got {cols}"


def test_foreign_keys():
    rows = _sync(
        "SELECT con.conrelid::regclass::text, con.confrelid::regclass::text "
        "FROM pg_constraint con "
        "WHERE con.contype = 'f' AND con.connamespace = 'public'::regnamespace"
    )
    present = {(r[0], r[1]) for r in rows}
    missing = [fk for fk in KEY_FKS if (fk[0], fk[2]) not in present]
    assert not missing, f"missing FK constraints: {missing}"


@pytest.mark.parametrize("tbl", EXPECTED_TABLES)
def test_table_selectable(tbl):
    # Proves the table compiled to a queryable relation.
    _sync(f"SELECT 1 FROM {tbl} WHERE 1=0")
