"""No-DB tests for the §33 handoff generator (``dra.handoff``).

Pure helpers are exercised without Postgres so they are always green in the
sandbox (the DB half — ``stage_section_handoff`` / ``canonical_ids_by_run`` —
is covered by the DB-gated suite in ``tests/test_repo_control_plane_e2e.py``).
"""

from __future__ import annotations

import json
import uuid

from dra.handoff import (
    RETRIEVAL_KEY_TYPES,
    SECTION_FILES,
    build_dependency_graph,
    build_document_package,
    build_manifest,
)


def _base_state(**overrides) -> dict:
    """Minimal ControlState-shaped dict for pure handoff tests (no DB)."""
    state = {
        "run_id": "run-test",
        "require_db": False,
        "live_investigators": False,
        "actor": {"kind": "model", "name": "test", "version": "1.0"},
        "budget": {"envelope_total": 10.0, "spent": 0.0, "remaining": 10.0, "currency": "USD"},
        "config_snapshot": {},
        "intent": {},
        "recon_branches": [],
        "recon_results": [],
        "research_tasks": {},
        "user_decisions": {},
        "branches": {},
        "branch_results": [],
        "claims": [],
        "verification_report": {},
        "synthesis": {},
        "gaps": [],
        "decisions": [],
        "handoff": {},
        "audit": {},
    }
    state.update(overrides)
    return state


def _canon() -> dict:
    """A fake ``canon`` dict (shape produced by canonical_ids_by_run)."""
    c1 = str(uuid.uuid4())
    c2 = str(uuid.uuid4())
    d1 = str(uuid.uuid4())
    e1 = str(uuid.uuid4())
    ie1 = str(uuid.uuid4())
    t1 = str(uuid.uuid4())
    g1 = str(uuid.uuid4())
    return {
        "source_snapshots": [
            {
                "locator": "https://example.com/repo",
                "version": "abc123",
                "kind": "repo",
                "license_spdx": "MIT",
                "access_basis": "public",
            }
        ],
        "canonical_ids": {
            "claim_ids": [c1, c2],
            "decision_ids": [d1],
            "implementation_entity_ids": [ie1],
            "evidence_unit_ids": [e1],
            "topic_ids": [t1],
            "gap_ids": [g1],
            "requirement_ids": [],
        },
        "lineage": [
            {"source": e1, "target": c1, "kind": "evidence", "relation": "claim-supports"}
        ],
        "source_visibility": {"stale": 0, "superseded": 0, "rejected": 0, "total": 5},
    }


def _seed_state() -> dict:
    return _base_state(
        run_id="run-handoff-test",
        intent={"objective": "build a handoff generator", "constraints": ["scope:handoff"]},
        claims=[
            {
                "claim_id": "claim:t-0",
                "evidence_ids": [str(uuid.uuid4())],
                "text": "The handoff generator synthesizes a manifest.",
                "relevance": "high",
            }
        ],
        decisions=[
            {
                "question": "Which manifest schema version?",
                "alternatives": ["1.0", "2.0"],
                "evidence_ids": [],
                "chosen": "1.0",
                "rationale": "Matches §31.2.",
                "consequences": ["stable"],
                "reversal_triggers": ["spec drift"],
            }
        ],
        gaps=[
            {
                "gap_id": "gap:0",
                "description": "No vector corpus table exists (dra#15).",
                "severity": "high",
                "impact": 1,
                "blocking": False,
            }
        ],
        research_tasks={
            "task-run-0": {
                "task_id": "task-run-0",
                "question": "what is the repo",
                "dependencies": [],
            },
            "task-run-1": {
                "task_id": "task-run-1",
                "question": "what is the architecture",
                "dependencies": ["task-run-0"],
            },
        },
    )


# ---------------------------------------------------------------------------
# 1. §31.2 manifest has every required field
# ---------------------------------------------------------------------------


def test_build_manifest_has_all_manifest_fields():
    state = _seed_state()
    canon = _canon()
    manifest = build_manifest(
        state, "run-handoff-test",
        retrieval_endpoint="/knowledge", canon=canon,
        decision_ids=canon["canonical_ids"]["decision_ids"],
    )
    required = [
        "schema_version",
        "version",
        "run_id",
        "objective",
        "source_snapshots",
        "user_constraints",
        "requirement_ids",
        "topic_ids",
        "decision_ids",
        "high_impact_claim_ids",
        "implementation_entity_ids",
        "unresolved_gap_ids",
        "dependency_graph",
        "document_map",
        "freshness",
        "retrieval",
    ]
    for key in required:
        assert key in manifest, f"manifest missing §31.2 field {key!r}"

    # §31.2 content checks
    assert manifest["schema_version"] == "1.0"
    assert manifest["run_id"] == "run-handoff-test"
    assert manifest["objective"] == "build a handoff generator"
    assert manifest["retrieval"]["contract"] == "§34"
    assert manifest["retrieval"]["bounded"] is True
    assert manifest["retrieval"]["endpoint"] == "/knowledge"
    assert set(manifest["retrieval"]["key_types"]) == set(RETRIEVAL_KEY_TYPES)
    # canonical IDs flow from the injected canon dict
    assert manifest["high_impact_claim_ids"] == canon["canonical_ids"]["claim_ids"]
    assert manifest["implementation_entity_ids"] == canon["canonical_ids"]["implementation_entity_ids"]
    assert manifest["unresolved_gap_ids"] == canon["canonical_ids"]["gap_ids"]
    assert manifest["source_snapshots"] == canon["source_snapshots"]
    assert manifest["decision_ids"] == canon["canonical_ids"]["decision_ids"]
    # user constraints carry intent constraints + (empty) user_decisions
    assert "scope:handoff" in manifest["user_constraints"]


def test_build_manifest_degrades_without_canon():
    """No-DB path: canon={} still yields every key (empty lists)."""
    state = _seed_state()
    manifest = build_manifest(state, "run-handoff-test", retrieval_endpoint="/knowledge")
    for key in (
        "schema_version", "version", "run_id", "objective", "source_snapshots",
        "user_constraints", "requirement_ids", "topic_ids", "decision_ids",
        "high_impact_claim_ids", "implementation_entity_ids", "unresolved_gap_ids",
        "dependency_graph", "document_map", "freshness", "retrieval",
    ):
        assert key in manifest, f"no-DB manifest missing {key!r}"
    assert manifest["source_snapshots"] == []
    assert manifest["decision_ids"] == []


# ---------------------------------------------------------------------------
# 2. §31.1 human-readable package: 8 sections, each cites a canonical ID
# ---------------------------------------------------------------------------


def test_build_document_package_has_8_sections():
    state = _seed_state()
    manifest = build_manifest(
        state, "run-handoff-test",
        retrieval_endpoint="/knowledge", canon=_canon(),
        decision_ids=[str(uuid.uuid4())],
    )
    package = build_document_package(state, manifest)
    for section in SECTION_FILES:
        assert section in package, f"package missing section {section!r}"
    assert len(SECTION_FILES) == 8


def test_each_section_cites_canonical_ids():
    state = _seed_state()
    canon = _canon()
    manifest = build_manifest(
        state, "run-handoff-test",
        retrieval_endpoint="/knowledge", canon=canon,
        decision_ids=canon["canonical_ids"]["decision_ids"],
    )
    package = build_document_package(state, manifest)
    # Split into sections by the §31.1/<nn> header.
    sections = {}
    current = None
    for line in package.splitlines():
        if line.startswith("## §31.1/"):
            # header looks like: ## §31.1/02 architecture — 02-architecture — Architecture
            tag = line.split("§31.1/")[1].split(" ")[0]
            current = tag
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    # seed a known canonical UUID to assert presence in at least one section
    known = canon["canonical_ids"]["decision_ids"][0]
    assert any(known in " ".join(lines) for lines in sections.values())
    # Each populated section must cite at least one backtick-quoted reference.
    for tag, lines in sections.items():
        joined = " ".join(lines)
        assert "`" in joined, f"section {tag} cites no canonical ID (no backticks)"


# ---------------------------------------------------------------------------
# 3. dependency graph edges
# ---------------------------------------------------------------------------


def test_build_dependency_graph_edges():
    state = _seed_state()
    canon = _canon()
    graph = build_dependency_graph(state, canon)
    # task dependency edge: task-run-1 depends on task-run-0
    task_edges = [e for e in graph if e["kind"] == "task_dep"]
    assert any(e["from"] == "task-run-0" and e["to"] == "task-run-1" for e in task_edges)
    # claim -> evidence lineage edge from canon (graph edges use from/to)
    lineage_edges = [e for e in graph if e["kind"] == "evidence"]
    assert len(lineage_edges) == 1
    assert lineage_edges[0]["from"] == canon["canonical_ids"]["evidence_unit_ids"][0]
    assert lineage_edges[0]["to"] == canon["canonical_ids"]["claim_ids"][0]


def test_dependency_graph_pure_does_not_require_db():
    state = _seed_state()
    graph = build_dependency_graph(state, None)
    assert isinstance(graph, list)
    # task edges still present with no canon
    assert any(e["kind"] == "task_dep" for e in graph)


# ---------------------------------------------------------------------------
# 4. manifest is JSON-serializable (no UUID serialization errors)
# ---------------------------------------------------------------------------


def test_manifest_round_trips_json():
    state = _seed_state()
    manifest = build_manifest(
        state, "run-handoff-test",
        retrieval_endpoint="/knowledge", canon=_canon(),
        decision_ids=[str(uuid.uuid4())],
    )
    serialized = json.dumps(manifest)
    restored = json.loads(serialized)
    assert restored["run_id"] == "run-handoff-test"
    assert restored["retrieval"]["contract"] == "§34"
    assert isinstance(restored["dependency_graph"], list)


def test_document_package_round_trips_json():
    state = _seed_state()
    manifest = build_manifest(
        state, "run-handoff-test",
        retrieval_endpoint="/knowledge", canon=_canon(),
        decision_ids=[str(uuid.uuid4())],
    )
    package = build_document_package(state, manifest)
    assert isinstance(package, str) and package
    # the manifest it was built from must round-trip without UUID serialization errors
    assert json.dumps(manifest)


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
