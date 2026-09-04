"""End-to-end test: control-plane → branch_worker → RepositoryInvestigator → dra.publish.

Exercises the dra#41 prototype-1 path: a repo source in the intent flows through
Phase 3 synthesis (-> repo-source ResearchTask) → Phase 5 branch_worker dispatch
(-> RepositoryInvestigator) → InvestigatorContext.__aexit__ → publish_bundle
(ADR-013 staged→canonical) to emit the canonical provenance chain:

    source_identity(repo) → raw_capture(repo_snapshot) →
    derived_artifact(symbol index) → evidence_unit(repo@commit:path:symbol) →
    implementation_entity → behavioral claim

DB-gated via ``tests._db.DB`` so the no-DB sandbox stays green; the DB-reachable
path is the real run. The repo target is the hermetic fixture
``tests._repo_fixture.build_repo`` (no network).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.pool import NullPool
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from dra.control_plane import (
    B_COMPLETE,
    COMPLETE,
    NUM_PHASES,
    branch_worker,
    build_graph,
    postgres_conninfo,
)
from dra.publish import async_session
from dra.sandbox import SandboxCapability

from tests._db import DB
from tests._evidence import reset
from tests._repo_fixture import build_repo


# ---------------------------------------------------------------------------
# Per-test NullPool engine (mirrors test_control_plane.py / test_repo_investigator.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def _isolated_async_engine(monkeypatch):
    """Per-test NullPool async engine patching every async_session reference.

    Patches dra.publish, dra.investigators, tests._evidence, and dra.db.engine
    so the control plane (can_connect in p0), InvestigatorContext, and reset()
    all share the per-test NullPool engine — avoiding cross-test QueuePool /
    event-loop deadlocks (each @DB test runs in its own asyncio.run).
    """
    from dra.db import DATABASE_URL

    engine = create_async_engine(DATABASE_URL, poolclass=NullPool, future=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    import dra.publish as _publish
    import dra.investigators as _investigators
    import tests._evidence as _evidence
    import dra.db as _dbmod

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


# ---------------------------------------------------------------------------
# SQL templates for provenance verification
# ---------------------------------------------------------------------------

# Source identity + raw capture lineage for a run — source_identity does NOT
# have its own prov_entity row (§21.2), so it joins through raw_capture's
# prov_entity back to prov_bundle.
_RUN_SOURCE_LINEAGE = """
SELECT
    si.locator       AS source_locator,
    si.kind          AS source_kind,
    si.version       AS source_version,
    si.license_spdx  AS license,
    rc.content_hash  AS raw_hash,
    rc.kind          AS raw_kind,
    rc.state         AS raw_state,
    rc.size_bytes    AS raw_size,
    pb.id            AS bundle_id
FROM source_identity si
JOIN raw_capture rc      ON rc.source_id = si.id
JOIN prov_entity pe_raw  ON pe_raw.entity_kind = 'raw_capture'
                        AND pe_raw.content_hash = rc.content_hash
JOIN prov_bundle pb      ON pb.id = pe_raw.bundle_id
WHERE pb.run_id = :run_id
"""

# All derived artifacts for a run, through prov_entity
_RUN_DERIVED = """
SELECT
    da.content_hash  AS derived_hash,
    da.kind          AS derived_kind,
    da.state         AS derived_state,
    da.schema_name   AS derived_schema
FROM derived_artifact da
JOIN prov_entity pe ON pe.entity_kind = 'derived_artifact' AND pe.id = da.id
JOIN prov_bundle pb ON pb.id = pe.bundle_id
WHERE pb.run_id = :run_id
"""

# All evidence units for a run, through prov_entity
_RUN_EVIDENCE = """
SELECT
    eu.id            AS evidence_id,
    eu.locator       AS evidence_locator,
    eu.content_hash  AS evidence_hash,
    eu.state         AS evidence_state,
    eu.metadata      AS evidence_metadata
FROM evidence_unit eu
JOIN prov_entity pe ON pe.entity_kind = 'evidence_unit' AND pe.id = eu.id
JOIN prov_bundle pb ON pb.id = pe.bundle_id
WHERE pb.run_id = :run_id
"""

# All implementation entities for a run
_RUN_IMPL = """
SELECT
    ie.id           AS impl_id,
    ie.kind         AS impl_kind,
    ie.path         AS impl_path,
    ie.symbol_name  AS impl_symbol,
    ie.commit_sha   AS impl_commit,
    ie.state        AS impl_state
FROM implementation_entity ie
JOIN prov_entity pe ON pe.entity_kind = 'implementation_entity' AND pe.id = ie.id
JOIN prov_bundle pb ON pb.id = pe.bundle_id
WHERE pb.run_id = :run_id
"""

# All claims for a run
_RUN_CLAIMS = """
SELECT
    cl.id              AS claim_id,
    cl.text            AS claim_text,
    cl.state           AS claim_state,
    cl.metadata        AS claim_metadata,
    cl.evidence_unit_id AS evidence_unit_id,
    eu.locator         AS evidence_locator
FROM claim cl
JOIN prov_entity pe ON pe.entity_kind = 'claim' AND pe.id = cl.id
JOIN prov_bundle pb ON pb.id = pe.bundle_id
LEFT JOIN evidence_unit eu ON eu.id = cl.evidence_unit_id
WHERE pb.run_id = :run_id
"""

# Count of staged prov_entity rows left (should be 0 after publish)
_STAGED_COUNT = """
SELECT count(*)
FROM prov_entity
WHERE bundle_id IN (
    SELECT id FROM prov_bundle WHERE run_id = :run_id
) AND state = 'staged'
"""


# ---------------------------------------------------------------------------
# DB-gated e2e test: full control-plane pipeline
# ---------------------------------------------------------------------------

@DB
@pytest.mark.usefixtures("_isolated_async_engine")
def test_repo_control_plane_e2e(tmp_path, monkeypatch):
    """End-to-end: repo source in intent → RepositoryInvestigator → canonical chain.

    Drives the full §10 control-plane pipeline (with live investigators) against
    the hermetic fixture repo, asserting the canonical provenance chain is
    committed and queryable by run_id.
    """
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from dra.db import DATABASE_URL

    monkeypatch.setenv("DRA_SANDBOX_CAPABILITY", "static_only")

    async def run():
        await reset()
        repo_path, commit_sha, _toplevel = build_repo(tmp_path)
        thread_id = f"proto1-{uuid.uuid4().hex[:8]}"

        async with AsyncPostgresSaver.from_conn_string(
            postgres_conninfo(DATABASE_URL)
        ) as checkpointer:
            await checkpointer.setup()
            from langgraph.store.memory import InMemoryStore
            store = InMemoryStore()
            graph = build_graph().compile(checkpointer=checkpointer, store=store)

            initial_state: dict = {
                "require_db": True,
                "live_investigators": True,
                "actor": {
                    "kind": "model",
                    "name": "langgraph-control-plane",
                    "version": "1.0",
                    "external_id": "dra-control-plane#1.0",
                },
                "budget": {
                    "envelope_total": 10.0, "spent": 0.0, "remaining": 10.0,
                    "currency": "USD",
                },
                "intent": {
                    "objective": "README comprehension of the sample fixture repo",
                    "sources": [
                        {"kind": "repo", "ref": repo_path, "version": ""}
                    ],
                    "constraints": ["scope:repo-comprehension"],
                },
                "run_id": thread_id,
                "config_snapshot": {},
            }
            cfg = {"configurable": {"thread_id": thread_id}}

            state = await graph.ainvoke(initial_state, config=cfg)

        # (a) Phase 14 reached, COMPLETE
        assert state["phase"] == NUM_PHASES - 1, state
        assert state["status"] == COMPLETE, (
            f"expected COMPLETE, got {state['status']}; "
            f"audit={state.get('audit')}; branches={state.get('branches')}"
        )

        # (b) branch_results contains B_COMPLETE with non-empty evidence_ids
        branch_results = state.get("branch_results") or []
        assert branch_results, "expected at least one branch_result"
        complete_branches = [b for b in branch_results if b["status"] == B_COMPLETE]
        assert complete_branches, (
            f"no B_COMPLETE branch; results: {branch_results}"
        )
        for br in complete_branches:
            assert br.get("evidence_ids"), "B_COMPLETE branch has no evidence_ids"
            assert br.get("published_count", 0) >= 4, br

        # (c) canonical provenance chain queryable by run_id
        async with async_session() as s:
            # source_identity (kind=repo, version=commit SHA)
            src_rows = (await s.execute(text(_RUN_SOURCE_LINEAGE), {"run_id": thread_id})).mappings().all()
            assert src_rows, f"no source_identity lineage for run_id={thread_id}"
            src = src_rows[0]
            assert src["source_kind"] == "repo"
            assert src["source_version"] == commit_sha
            assert src["source_locator"] == repo_path
            assert src["license"] == "MIT"

            # raw_capture (kind=repo_snapshot, content_hash deterministic)
            raw_rows = (await s.execute(text(_RUN_SOURCE_LINEAGE), {"run_id": thread_id})).mappings().all()
            assert raw_rows[0]["raw_kind"] == "repo_snapshot"
            assert raw_rows[0]["raw_state"] == "canonical"
            snapshot_hash = raw_rows[0]["raw_hash"]

            # derived_artifact (kind=parsed, symbol index, chains to raw_capture)
            da_rows = (await s.execute(text(_RUN_DERIVED), {"run_id": thread_id})).mappings().all()
            assert any(d["derived_kind"] == "parsed" for d in da_rows), da_rows
            symbol_index = next(d for d in da_rows if d["derived_kind"] == "parsed")
            assert symbol_index["derived_state"] == "canonical"

            # evidence_unit rows with repo@commit:path:symbol locators
            ev_rows = (await s.execute(text(_RUN_EVIDENCE), {"run_id": thread_id})).mappings().all()
            assert len(ev_rows) >= 1
            repo_ev_rows = [
                ev for ev in ev_rows
                if isinstance(ev["evidence_locator"], dict)
                and ev["evidence_locator"].get("source_kind") == "repo"
            ]
            assert repo_ev_rows, (
                "no repo-source evidence_units; locators: "
                f"{[e['evidence_locator'] for e in ev_rows]}"
            )
            for ev in repo_ev_rows:
                assert ev["evidence_state"] == "canonical", ev
                loc = ev["evidence_locator"]
                assert loc.get("commit") == commit_sha
                assert "path" in loc and "symbol" in loc

            # implementation_entity rows
            impl_rows = (await s.execute(text(_RUN_IMPL), {"run_id": thread_id})).mappings().all()
            assert len(impl_rows) >= 1, "no implementation_entity rows"
            assert all(r["impl_state"] == "canonical" for r in impl_rows)

            # behavioral claim (INFERENCE under static_only)
            claim_rows = (await s.execute(text(_RUN_CLAIMS), {"run_id": thread_id})).mappings().all()
            assert len(claim_rows) >= 1, "no canonical claim"
            for cl in claim_rows:
                assert cl["claim_state"] == "canonical"
                meta = cl["claim_metadata"] or {}
                assert meta.get("evidence_status") == "INFERENCE", cl

            # all prov_entity rows for this run are canonical (none left staged)
            staged = await s.scalar(text(_STAGED_COUNT), {"run_id": thread_id})
            assert staged == 0, f"{staged} staged prov_entity rows left unreleased"

        # (d) run_id queryability: the §21.2 lineage query shape
        async with async_session() as s:
            lineage = await s.execute(text(_RUN_SOURCE_LINEAGE), {"run_id": thread_id})
            rows = lineage.mappings().all()
            assert len(rows) == 1  # one repo source
            assert rows[0]["source_kind"] == "repo"
            assert rows[0]["raw_kind"] == "repo_snapshot"

        # (e) §33 handoff: p13 staged a canonical handoff_statement (dra#42)
        async with async_session() as s:
            hs = (
                await s.execute(
                    text(
                        "SELECT hs.decision_id, hs.manifest, hs.content "
                        "FROM handoff_statement hs "
                        "JOIN prov_entity pe ON pe.entity_kind='handoff' "
                        "AND pe.id=hs.id JOIN prov_bundle pb ON pb.id=pe.bundle_id "
                        "AND pb.run_id=:r AND pe.state='canonical' LIMIT 1"
                    ),
                    {"r": thread_id},
                )
            ).mappings().first()
            assert hs is not None, "no canonical handoff_statement staged by p13"
            manifest = hs["manifest"]
            assert isinstance(manifest, dict), manifest
            for fld in ("schema_version", "dependency_graph", "document_map", "retrieval"):
                assert fld in manifest, f"handoff manifest missing §31.2 field {fld!r}: {manifest}"
            assert manifest["retrieval"]["contract"] == "§34"
            assert manifest["retrieval"]["bounded"] is True
            assert manifest["document_map"]["sections"] == [
                "00-executive", "01-requirements", "02-architecture",
                "03-source-system-understanding", "04-implementation-plan",
                "05-decisions", "06-risks-and-unknowns", "07-evidence-index",
            ]
            # §31.1 eight-section human-readable package in content TEXT (D4)
            content = hs["content"] or ""
            assert "00-executive" in content
            assert "07-evidence-index" in content

            # decision_id FK points at a real canonical decision (D1: p13 staged it)
            dec_count = await s.scalar(
                text(
                    "SELECT count(*) FROM decision d "
                    "JOIN prov_entity pe ON pe.entity_kind='decision' AND pe.id=d.id "
                    "JOIN prov_bundle pb ON pb.id=pe.bundle_id "
                    "AND pb.run_id=:r AND pe.state='canonical' WHERE d.id=:did"
                ),
                {"r": thread_id, "did": str(hs["decision_id"])},
            )
            assert dec_count == 1, "handoff.decision_id is not a real canonical decision"

            # (f) §34 retrieval contract: bounded bundle keyed by decision id
            from dra.knowledge import retrieve_context_bundle

            bundle = await retrieve_context_bundle(
                session=s, run_id=thread_id,
                by={"decision": str(hs["decision_id"])},
            )
            assert bundle["architecture_decisions"], "§34 decision bundle: empty decisions"
            assert bundle["evidence_locators"], "§34 decision bundle: empty evidence"
            assert bundle["immediate_objective"], "§34 bundle: missing immediate objective"
            # bounded (§34 L2311)
            assert len(bundle["architecture_decisions"]) <= 50
            assert len(bundle["evidence_locators"]) <= 50

            # (g) published_count >= 4 already asserted via branch_results above

        # Emit canonical-ID receipt (optionally to disk)
        async with async_session() as s:
            ev_rows = (await s.execute(text(
                "SELECT eu.id, eu.locator FROM evidence_unit eu "
                "JOIN prov_entity pe ON pe.entity_kind = 'evidence_unit' AND pe.id = eu.id "
                "JOIN prov_bundle pb ON pb.id = pe.bundle_id "
                "WHERE pb.run_id = :r AND pe.state = 'canonical'"
            ), {"r": thread_id})).mappings().all()

            impl_rows = (await s.execute(text(
                "SELECT ie.id, ie.kind, ie.path, ie.symbol_name FROM implementation_entity ie "
                "JOIN prov_entity pe ON pe.entity_kind = 'implementation_entity' AND pe.id = ie.id "
                "JOIN prov_bundle pb ON pb.id = pe.bundle_id "
                "WHERE pb.run_id = :r AND pe.state = 'canonical'"
            ), {"r": thread_id})).mappings().all()

            claim_rows = (await s.execute(text(
                "SELECT cl.id, cl.metadata FROM claim cl "
                "JOIN prov_entity pe ON pe.entity_kind = 'claim' AND pe.id = cl.id "
                "JOIN prov_bundle pb ON pb.id = pe.bundle_id "
                "WHERE pb.run_id = :r AND pe.state = 'canonical'"
            ), {"r": thread_id})).mappings().all()

            receipt = {
                "schema_version": 1,
                "run_id": thread_id,
                "objective": initial_state["intent"]["objective"],
                "repo_ref": repo_path,
                "commit_sha": commit_sha,
                "snapshot_hash": snapshot_hash,
                "evidence_unit_ids": [str(r["id"]) for r in ev_rows],
                "implementation_entity_ids": [str(r["id"]) for r in impl_rows],
                "claim_ids": [str(r["id"]) for r in claim_rows],
                "evidence_inventory": [
                    {"entity_kind": "evidence_unit", "id": str(r["id"]), "locator": r["locator"]}
                    for r in ev_rows
                ] + [
                    {"entity_kind": "implementation_entity", "id": str(r["id"]),
                     "kind": r["kind"], "path": r["path"], "symbol_name": r["symbol_name"]}
                    for r in impl_rows
                ],
            }

        out_dir = os.environ.get("DRA_PROTOTYPE_OUT")
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
            path = os.path.join(out_dir, f"prototype1_{thread_id}.json")
            with open(path, "w") as fh:
                json.dump(receipt, fh, indent=2, default=str)
            print(f"[prototype1] receipt written to {path}")

        from dra.db import engine as _eng
        await _eng.dispose()
        return receipt

    receipt = asyncio.run(run())
    assert receipt["evidence_unit_ids"], "receipt has no evidence_unit_ids"
    assert receipt["implementation_entity_ids"], "receipt has no impl entity ids"
    assert receipt["claim_ids"], "receipt has no claim ids"


# ---------------------------------------------------------------------------
# DB-gated: direct branch_worker dispatch on a repo task (wiring check)
# ---------------------------------------------------------------------------

@DB
@pytest.mark.usefixtures("_isolated_async_engine")
def test_repo_control_plane_e2e_direct_branch_worker(tmp_path, monkeypatch):
    """Direct branch_worker dispatch on a repo task (minimal wiring verification).

    Verifies the repo-source ResearchTask flows through run_branch_worker into
    RepositoryInvestigator without the full graph — a focused check that the
    Phase 3 → Phase 5 wiring is correct.
    """
    monkeypatch.setenv("DRA_SANDBOX_CAPABILITY", "static_only")

    async def run():
        await reset()
        repo_path, sha, _ = build_repo(tmp_path)

        task = {
            "task_id": "task-direct-0",
            "question": "README comprehension: what does this repo do?",
            "source_types": ["repo"],
            "source": {
                "kind": "repo",
                "ref": repo_path,
                "locator": repo_path,
                "version": "",
            },
            "run_id": "run-direct",
            "actor": {
                "kind": "model",
                "name": "test",
                "version": "1.0",
                "external_id": "dra-control-plane#1.0",
            },
        }

        result = await branch_worker(task)
        br = result["branch_results"][0]
        assert br["status"] == B_COMPLETE, br
        assert br["evidence_ids"], "no evidence_ids produced"
        assert br["published_count"] >= 4, br

        # Verify canonical chain in DB (direct domain table queries)
        async with async_session() as s:
            # source_identity: repo with version = commit SHA
            si_row = (await s.execute(text(
                "SELECT si.kind, si.version, si.locator, si.license_spdx "
                "FROM source_identity si "
                "JOIN raw_capture rc ON rc.source_id = si.id "
                "JOIN prov_entity pe ON pe.entity_kind = 'raw_capture' AND pe.content_hash = rc.content_hash "
                "JOIN prov_bundle pb ON pb.id = pe.bundle_id "
                "WHERE pb.run_id = 'run-direct' AND si.locator = :loc"
            ), {"loc": repo_path})).mappings().first()
            assert si_row is not None, "no source_identity for repo"
            assert si_row["kind"] == "repo"
            assert si_row["version"] == sha
            assert si_row["license_spdx"] == "MIT"

            # raw_capture: repo_snapshot, canonical
            rc_row = (await s.execute(text(
                "SELECT rc.kind, rc.state FROM raw_capture rc "
                "JOIN prov_entity pe ON pe.entity_kind = 'raw_capture' AND pe.content_hash = rc.content_hash "
                "JOIN prov_bundle pb ON pb.id = pe.bundle_id "
                "WHERE pb.run_id = 'run-direct'"
            ))).mappings().first()
            assert rc_row["kind"] == "repo_snapshot"
            assert rc_row["state"] == "canonical"

            # derived_artifact: parsed (symbol index), canonical, chains to raw_capture
            da_rows = (await s.execute(text(
                "SELECT da.kind, da.content_hash, da.state, da.source_capture_hash "
                "FROM derived_artifact da "
                "JOIN prov_entity pe ON pe.entity_kind = 'derived_artifact' AND pe.id = da.id "
                "JOIN prov_bundle pb ON pb.id = pe.bundle_id "
                "WHERE pb.run_id = 'run-direct'"
            ))).mappings().all()
            assert any(d["kind"] == "parsed" for d in da_rows), da_rows
            symbol_index = next(d for d in da_rows if d["kind"] == "parsed")
            assert symbol_index["state"] == "canonical"

            # evidence_unit: repo@commit:path:symbol locators, canonical
            eu_rows = (await s.execute(text(
                "SELECT eu.locator, eu.state FROM evidence_unit eu "
                "JOIN prov_entity pe ON pe.entity_kind = 'evidence_unit' AND pe.id = eu.id "
                "JOIN prov_bundle pb ON pb.id = pe.bundle_id "
                "WHERE pb.run_id = 'run-direct'"
            ))).mappings().all()
            assert len(eu_rows) >= 1
            repo_eu_rows = [
                eu for eu in eu_rows
                if isinstance(eu["locator"], dict)
                and eu["locator"].get("source_kind") == "repo"
            ]
            assert repo_eu_rows, (
                "no repo-source evidence_units; locators: "
                f"{[e['locator'] for e in eu_rows]}"
            )
            for eu in repo_eu_rows:
                assert eu["state"] == "canonical"
                loc = eu["locator"]
                assert loc.get("commit") == sha
                assert "path" in loc and "symbol" in loc

            # implementation_entity: rows for the repo source, canonical
            impl_rows = (await s.execute(text(
                "SELECT ie.kind, ie.path, ie.symbol_name, ie.state "
                "FROM implementation_entity ie "
                "WHERE ie.repo_source_id IN ("
                "  SELECT si.id FROM source_identity si "
                "  JOIN raw_capture rc ON rc.source_id = si.id "
                "  JOIN prov_entity pe ON pe.entity_kind = 'raw_capture' AND pe.content_hash = rc.content_hash "
                "  JOIN prov_bundle pb ON pb.id = pe.bundle_id "
                "  WHERE pb.run_id = 'run-direct'"
                ")"
            ))).mappings().all()
            assert len(impl_rows) >= 1, "no implementation_entity rows"
            assert all(r["state"] == "canonical" for r in impl_rows)

            # behavioral claim: INFERENCE under static_only
            claim_rows = (await s.execute(text(
                "SELECT cl.metadata->>'evidence_status' AS status "
                "FROM claim cl "
                "JOIN prov_entity pe ON pe.entity_kind = 'claim' AND pe.id = cl.id "
                "JOIN prov_bundle pb ON pb.id = pe.bundle_id "
                "WHERE pb.run_id = 'run-direct'"
            ))).mappings().all()
            assert len(claim_rows) >= 1, "no canonical claim"
            for c in claim_rows:
                assert c["status"] == "INFERENCE", c

            # no staged prov_entity rows left
            staged = await s.scalar(text(
                "SELECT count(*) FROM prov_entity "
                "WHERE bundle_id IN ("
                "  SELECT id FROM prov_bundle WHERE run_id = 'run-direct'"
                ") AND state = 'staged'"
            ))
            assert staged == 0

    asyncio.run(run())


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
