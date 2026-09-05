"""Provenance-traversal tests (§21.2 reconstruction chain, ADR-014).

Builds the full lineage chain end-to-end (source capture -> derived artifact ->
evidence unit -> claim -> decision -> handoff_statement) plus the W3C-PROV
entity/activity/agent/bundle edges, then asserts two backward traversals
reconstruct the complete chain back to the source identity and the run/bundle.

SKIP if no DB is reachable (env concern).
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text

from dra.publish import async_session, publish_bundle
from tests._db import DB
from tests._evidence import build_lineage_bundle, reset

pytestmark = DB

LINEAGE_CHAIN_QUERY = """
SELECT
    si.locator               AS source_locator,
    si.kind                 AS source_kind,
    si.license_spdx         AS license,
    cb.hash                 AS raw_hash,
    sc.kind                 AS raw_kind,
    da.content_hash         AS derived_hash,
    eu.content_hash         AS evidence_hash,
    c.text                  AS claim_text,
    d.text                  AS decision_text,
    h.content               AS handoff_content,
    pa.activity_type        AS acquisition_activity,
    pa.bundle_id            AS bundle_id,
    pb.run_id               AS run_id,
    pb.task_id              AS task_id,
    ag.kind                 AS agent_kind,
    ag.name                 AS agent_name,
    ag.version              AS agent_version
FROM handoff_statement h
JOIN decision              d  ON d.id = h.decision_id
JOIN claim                 c  ON c.id = d.claim_id
JOIN evidence_unit         eu ON eu.id = c.evidence_unit_id
JOIN derived_artifact      da ON da.id = eu.artifact_id
JOIN content_blob       cb ON cb.hash = da.source_capture_hash
JOIN source_capture     sc ON sc.content_blob_hash = cb.hash
JOIN source_identity    si ON si.id = sc.source_identity_id
JOIN prov_entity        re ON re.entity_kind = 'raw_capture' AND re.id = sc.capture_id
JOIN prov_activity         pa ON pa.id = re.produced_by_activity
JOIN prov_bundle           pb ON pb.id = pa.bundle_id
JOIN prov_agent            ag ON ag.id = pa.agent_id
WHERE h.id = :handoff_id
"""

DERIVATION_BACKTRAVERSE_QUERY = """
WITH RECURSIVE deriv AS (
    SELECT derived_entity_id AS cur, source_entity_id AS src, 1 AS depth
    FROM prov_derivation WHERE derived_entity_id = :start_id
    UNION ALL
    SELECT d.derived_entity_id, d.source_entity_id, deriv.depth + 1
    FROM prov_derivation d JOIN deriv ON deriv.src = d.derived_entity_id
    WHERE deriv.depth < 20
),
chain AS (
    SELECT cur AS entity_id, depth FROM deriv
    UNION ALL
    SELECT src, depth FROM deriv WHERE depth = (SELECT max(depth) FROM deriv)
)
SELECT pe.entity_kind, pe.id, pe.content_hash
FROM chain c JOIN prov_entity pe ON pe.id = c.entity_id
ORDER BY depth DESC
"""


def test_backward_domain_chain_reconstruction():
    """handoff -> ... -> source -> acquisition activity -> agent -> run/bundle."""
    async def run():
        await reset()
        bundle_id, ids = await build_lineage_bundle()
        await publish_bundle(bundle_id)

        handoff_id = ids["handoff"]
        async with async_session() as session:
            row = await session.execute(text(LINEAGE_CHAIN_QUERY), {"handoff_id": str(handoff_id)})
            rec = row.mappings().one()

        assert rec["source_locator"] == "https://example.com/src"
        assert rec["license"] == "MIT"
        assert rec["raw_hash"] == "a" * 64
        assert rec["raw_kind"] == "repo_snapshot"
        assert rec["derived_hash"] == "b" * 64
        assert rec["evidence_hash"] == "c" * 64
        assert rec["claim_text"] == "claims something"
        assert rec["decision_text"] == "Decide X"
        assert rec["handoff_content"] == "handoff body"
        assert rec["acquisition_activity"] == "acquisition"
        assert rec["run_id"] == "run_lin"
        assert rec["task_id"] == "task_lin"
        assert rec["agent_kind"] == "model"
        assert rec["agent_name"] == "gpt"
        assert rec["agent_version"] == "1.0"
    asyncio.run(run())


def test_derivation_graph_traversal():
    """Recursive prov_derivation walk reaches the source capture (prov_entity
    entity_kind='raw_capture') from the handoff entity."""
    async def run():
        await reset()
        bundle_id, ids = await build_lineage_bundle()
        await publish_bundle(bundle_id)

        handoff_eid = ids["handoff"]
        async with async_session() as session:
            rows = await session.execute(
                text(DERIVATION_BACKTRAVERSE_QUERY),
                {"start_id": str(handoff_eid)},
            )
            kinds = [r[0] for r in rows.fetchall()]

        # The derivation chain from handoff must reach back through decision,
        # claim, evidence_unit, derived_artifact, source capture (entity_kind
        #='raw_capture').
        for expected in (
            "handoff", "decision", "claim", "evidence_unit",
            "derived_artifact", "raw_capture",
        ):
            assert expected in kinds, f"derivation chain missing {expected}: {kinds}"
    asyncio.run(run())
