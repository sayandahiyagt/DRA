"""§38.6 Downstream-utility proof harness (sayandahiyagt/dra#43).

Holds the coding-agent model and the downstream task constant while varying the
input condition the *downstream coding agent* receives, and measures the four
§38.6 metrics — re-research calls, incorrect/hallucinated assumptions,
time-to-correct-build, and architectural rework — across the five comparison
arms. Applies the ADR-018 / §38.6 decision rule (the full architecture —
handoff + queryable corpus — must *strictly* beat BOTH binding baselines on
all four metrics; a tie or any baseline win triggers simplification) and emits
a machine-checkable ``results.json`` + ``results.md`` ledger mirroring the
dra#37 §38.1 bake-off precedent.

Design follows the §38.x proof template (``proof_corpus`` §38.2,
``progressive_interview`` §38.5): the held-constant "downstream coding agent" is
a deterministic ``FakeDownstreamAgent`` whose metrics are a pure function of arm
input quality — *not* a real LLM invocation (eval_plan.md §7: "grading is not
done by the research agent; deterministic, external ground-truth verification").
The pure simulation is always-green offline; when Postgres is reachable and
``publish=True``, per-arm metric rows + the report are staged as canonical
``MAINTAINER_ASSERTION`` rows in the standalone ``user_assertion`` table (dra#44,
migration ``0008_interview_constraints``), exactly as §38.5 does — **no new
migration**.

Consumes:
  * dra#40 → ``docs/eval_plan.md`` §5 (decision rule), §6 (5-condition seed),
    §4 (four-metric definitions).
  * dra#42 → ``dra.handoff.build_manifest`` / ``build_document_package``
    (§31.2 manifest + §31.1 package, pure) for arms 4 & 5 inputs; and
    ``dra.knowledge.retrieve_context_bundle`` / ``RETRIEVAL_KEY_TYPES``
    (§34 retrieval contract, DB-gated) for arm 5's queryable knowledge.

CLI entry: ``dra-downstream-utility-proof`` (wired in ``pyproject.toml``).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import random
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from dra.db import DATABASE_URL, can_connect
from dra.handoff import SECTION_FILES, build_document_package, build_manifest
from dra.knowledge import RETRIEVAL_KEY_TYPES

__all__ = [
    "MISSION",
    "SPEC_ANCHOR",
    "ARMS_JSON_SEED",
    "ARMS",
    "BINDING_BASELINES",
    "RECOVERY_FACTORS",
    "CORRECTNESS_STYLE_METRICS",
    "Arm",
    "FactSpec",
    "GroundTruthTask",
    "BuildAttempt",
    "ProofConfig",
    "FakeDownstreamAgent",
    "generate_task_set",
    "observable_facts",
    "measure_arm",
    "apply_decision_rule",
    "evaluate_all",
    "run_proof",
    "write_report",
    "main",
]

MISSION = "sayandahiyagt/dra#43"
SPEC_ANCHOR = "§38.6"

#: The standalone table used for DB-gated canonical assertion staging (dra#44,
#: migration ``0008``). No new migration is required — mirrors §38.5.
CHECK_DB_TABLE = "user_assertion"

#: Coding-agent model + downstream task held constant across all five arms
#: (eval_plan.md §3 / §3.9 — filled per-run from the §31.2 manifest; defaults
#: here are the synthetic workhorse, mirroring §38.3's model IDs).
_HELDED_CONSTANT_DEFAULTS = {
    "coding_agent_model": "gpt-5.6-luna@workhorse (synthetic)",
    "coding_agent_version": "1.0-synthetic",
    "sandbox_image": "ghcr.io/sayandahiyagt/dra:synthetic-workhorse",
    "repo_snapshot_ref": "synthetic:repo-comprehension-fixture",
    "downstream_task": "repo-comprehension:build a module+symbol inventory of a README repo",
    "objective": "produce a correct module boundary map + symbol signature inventory of a repository",
}

# ---------------------------------------------------------------------------
# §38.6 decision rule (eval_plan.md §5) — encoded as data
# ---------------------------------------------------------------------------

#: The two binding baselines the full architecture must strictly beat (§5).
BINDING_BASELINES = ("raw_sources", "ordinary_report")

#: Correctness-style metrics relax to a 0.95 factor (low counts make ratios
#: noisy); re-research + time hold to 0.90 (≥10% reduction preferred).
RECOVERY_FACTORS: dict[str, float] = {
    "re_research_calls": 0.90,
    "time_to_correct_build": 0.90,
    "incorrect_assumptions": 0.95,
    "architectural_rework": 0.95,
}
_CORRECTNESS_STYLE_METRICS = frozenset(
    {"incorrect_assumptions", "architectural_rework"}
)
CORRECTNESS_STYLE_METRICS = _CORRECTNESS_STYLE_METRICS


# ---------------------------------------------------------------------------
# §6 mechanical 5-condition seed (verbatim shape from eval_plan.md §6)
# ---------------------------------------------------------------------------

ARMS_JSON_SEED: dict[str, Any] = {
    "held_constant": {
        "coding_agent_model": "<from §38.3 model-routing proof / ADR-022 workhorse; filled per run>",
        "coding_agent_version": "<pinned>",
        "sandbox_image": "<pinned>",
        "repo_snapshot_ref": "<commit-ish of the target repo>",
        "downstream_task": "<objective ID from the seeded task set; exact pass-test path>",
        "objective": "<human-readable objective>",
    },
    "arms": [
        {
            "id": "raw_sources",
            "label": "Raw sources (baseline)",
            "provides": ["source_links_or_snapshots"],
            "provides_handoff_document": False,
            "provides_queryable_knowledge": False,
            "knowledge_access": "none",
            "provenance": "agent re-discovers sources itself",
        },
        {
            "id": "ordinary_report",
            "label": "Ordinary research report (baseline)",
            "provides": ["prose_report"],
            "provides_handoff_document": True,
            "provides_queryable_knowledge": False,
            "knowledge_access": "none (prose only)",
        },
        {
            "id": "structured_corpus_no_handoff",
            "label": "Structured evidence corpus, no handoff",
            "provides": [
                "evidence_claim_graph",
                "entities",
                "relationships",
                "topics",
                "claims",
                "gaps",
                "decisions",
            ],
            "provides_handoff_document": False,
            "provides_queryable_knowledge": True,
            "knowledge_access": "queryable",
            "knowledge_retrieval_contract": "§34 ImplementationContextBundle",
            "handoff_document": None,
        },
        {
            "id": "handoff_no_queryable_corpus",
            "label": "Handoff without queryable corpus",
            "provides": ["handoff_package_00_to_07", "manifest_json"],
            "provides_handoff_document": True,
            "provides_queryable_knowledge": False,
            "knowledge_access": "inline evidence-index only (§31.1 / 07-evidence-index.md)",
        },
        {
            "id": "full_handoff_queryable_corpus",
            "label": "Full architecture: handoff + queryable corpus",
            "provides": [
                "handoff_package_00_to_07",
                "manifest_json",
                "queryable_knowledge",
            ],
            "provides_handoff_document": True,
            "provides_queryable_knowledge": True,
            "knowledge_retrieval_contract": "§34 ImplementationContextBundle by ID/query",
        },
    ],
    "metrics": {
        "re_research_calls": "count of re-scout tool/node calls (fuzzy dup query >= 0.9)",
        "incorrect_assumptions": "ground-truth-contradicted assumptions (imports / paths / API signatures)",
        "time_to_correct_build": "wall-clock to green build + ground-truth tests pass",
        "architectural_rework": "distinct re-scaffolding diff events (moved / renamed modules)",
    },
    "decision_rule": "Full handoff+queryable-corpus architecture (arm 5) must strictly < (<=0.9x / <=0.95x) BOTH binding baselines (raw_sources, ordinary_report) on ALL four metrics; tie or any baseline win -> simplify (ADR-018 / §40).",
}


@dataclass(frozen=True)
class Arm:
    """One of the five §38.6 comparison arms (from the §6 seed)."""

    id: str
    label: str
    provides_handoff_document: bool
    provides_queryable_knowledge: bool
    knowledge_access: str
    provenance: str = ""

    @property
    def is_baseline(self) -> bool:
        return self.id in BINDING_BASELINES


def _build_arms_from_seed() -> list[Arm]:
    return [
        Arm(
            id=a["id"],
            label=a["label"],
            provides_handoff_document=a["provides_handoff_document"],
            provides_queryable_knowledge=a["provides_queryable_knowledge"],
            knowledge_access=a.get(
                "knowledge_access",
                "queryable" if a["provides_queryable_knowledge"] else "none",
            ),
            provenance=a.get("provenance", ""),
        )
        for a in ARMS_JSON_SEED["arms"]
    ]


#: Ordered arm list (arm1..arm5). Ordering is significant for the monotonic
#: observability invariant tested in ``TestObservableFacts``.
ARMS: list[Arm] = _build_arms_from_seed()

_ARM_BY_ID = {a.id: a for a in ARMS}


# ---------------------------------------------------------------------------
# §24.1 / §37 Stage-0 ground-truth fixture (deterministic, in-memory)
# ---------------------------------------------------------------------------

#: Atomic claim kinds a correct repo-comprehension build must surface (§24.1).
#: ``identity`` facts are always observable (the agent knows the repo identity);
#: the remaining six kinds are reconstructed and degrade with arm quality.
FACT_KINDS = (
    "identity",
    "module_boundary",
    "symbol_signature",
    "dependency_edge",
    "data_flow",
    "api_surface",
    "data_contract",
    "test_entry",
)


@dataclass(frozen=True)
class FactSpec:
    """One factual requirement a correct build must surface (§24.1)."""

    id: str
    kind: str
    text: str


@dataclass(frozen=True)
class ModuleNode:
    """One node in a task's structural module graph (§31.3 / §24.1)."""

    name: str
    kind: str  # e.g. "module" | "package" | "component"


@dataclass(frozen=True)
class StructureEdge:
    """One directed dependency edge in the module graph (§24.1 dependency id)."""

    src: str
    dst: str


@dataclass(frozen=True)
class GroundTruthTask:
    """A deterministic §37 Stage-0 repo-comprehension task fixture.

    Mirrors §38.3/§38.5's bundled offline fixtures: a self-contained, fixed
    objective with a structural skeleton + factual requirements that the held-
    constant downstream coding agent must reconstruct. ``n_repo_extension`` is
    a §37 Stage-0 repo-extension problem (≥1 per §37 Stage 0:2511-2516).
    """

    id: str
    objective: str
    facts: tuple[FactSpec, ...]
    structure: tuple[ModuleNode, ...]
    edges: tuple[StructureEdge, ...]
    repo_extension: bool


# A fixed, license-safe, deterministic pool of tasks. The default n_tasks=3
# selects a deterministic subset (shuffled by seed) like §38.5's
# ``generate_oracle_corpus``.
_TASK_POOL: tuple[GroundTruthTask, ...] = (
    GroundTruthTask(
        id="task_readme_inventory",
        objective=(
            "Produce a module-boundary map + symbol signature inventory of the "
            "bake-off package, and assert the auth entry point."
        ),
        facts=(
            FactSpec("f:readme_inventory:identity", "identity",
                     "repo: sayandahiyagt/DRA, bake-off subproject, Python package."),
            FactSpec("f:readme_inventory:module_boundary", "module_boundary",
                     "bake-off is a standalone subproject; modules: corpus, measure, "
                     "evidence, lifecycle_tools, workflow_def, checkpointer."),
            FactSpec("f:readme_inventory:symbol_signature", "symbol_signature",
                     "bakeoff.corpus.generate_corpus(n_vectors, dim, seed); "
                     "bakeoff.measure.compute_recall(exact_ids, hnsw_ids, k)."),
            FactSpec("f:readme_inventory:dependency_edge", "dependency_edge",
                     "measure imports corpus.generate_corpus; evidence imports measure."),
            FactSpec("f:readme_inventory:data_flow", "data_flow",
                     "generate_corpus -> proof_corpus table -> run_exact/run_hnsw "
                     "-> recall vs exact."),
            FactSpec("f:readme_inventory:api_surface", "api_surface",
                     "bakeoff.corpus exposes generate_corpus; bakeoff.measure exposes "
                     "run_exact, run_hnsw, compute_recall."),
            FactSpec("f:readme_inventory:data_contract", "data_contract",
                     "rows carry content_hash (sha256), tenant_id, project_id, "
                     "embedding (vector), topic_id; ON CONFLICT upserts on content_hash."),
            FactSpec("f:readme_inventory:test_entry", "test_entry",
                     "pytest tests/test_storage_proof.py exercises proof_corpus."),
        ),
        structure=(
            ModuleNode("bakeoff/corpus", "module"),
            ModuleNode("bakeoff/measure", "module"),
            ModuleNode("bakeoff/lifecycle_tools", "module"),
            ModuleNode("bakeoff/evidence", "component"),
        ),
        edges=(
            StructureEdge("bakeoff/corpus", "bakeoff/evidence"),
            StructureEdge("bakeoff/measure", "bakeoff/evidence"),
            StructureEdge("bakeoff/lifecycle_tools", "bakeoff/measure"),
        ),
        repo_extension=True,
    ),
    GroundTruthTask(
        id="task_handoff_package",
        objective=(
            "Implement the §31.1 8-section handoff package from a control state "
            "and verify the §31.2 manifest shape."
        ),
        facts=(
            FactSpec("f:handoff:identity", "identity",
                     "repo: sayandahiyagt/DRA; handoff module: src/dra/handoff.py."),
            FactSpec("f:handoff:module_boundary", "module_boundary",
                     "handoff.py exposes build_manifest, build_document_package, "
                     "SECTION_FILES (8 sections 00..07), stage_section_handoff."),
            FactSpec("f:handoff:symbol_signature", "symbol_signature",
                     "build_manifest(state, run_id, *, schema_version, "
                     "retrieval_endpoint, canon, decision_ids) -> dict."),
            FactSpec("f:handoff:dependency_edge", "dependency_edge",
                     "build_document_package depends on build_manifest output; "
                     "stage_section_handoff depends on both."),
            FactSpec("f:handoff:data_flow", "data_flow",
                     "control_state -> build_manifest -> §31.2 manifest -> "
                     "build_document_package -> §31.1 package -> publish_bundle."),
            FactSpec("f:handoff:api_surface", "api_surface",
                     "build_dependency_graph(state, canon) -> list[dict]; "
                     "canonical_ids_by_run(run_id, session) -> dict."),
            FactSpec("f:handoff:data_contract", "data_contract",
                     "Stage 05-decisions cites canonical decision IDs; "
                     "manifest.freshness.source_visibility surfaces stale/superseded/rejected."),
            FactSpec("f:handoff:test_entry", "test_entry",
                     "tests/test_handoff.py exercises the pure builders (no DB)."),
        ),
        structure=(
            ModuleNode("src/dra/handoff.py", "module"),
            ModuleNode("section_render", "component"),
            ModuleNode("manifest", "component"),
            ModuleNode("dependency_graph", "component"),
        ),
        edges=(
            StructureEdge("manifest", "section_render"),
            StructureEdge("section_render", "src/dra/handoff.py"),
            StructureEdge("dependency_graph", "manifest"),
        ),
        repo_extension=False,
    ),
    GroundTruthTask(
        id="task_knowledge_retrieval",
        objective=(
            "Implement the §34 ImplementationContextBundle retrieval over the "
            "canonical evidence tables by key type, bounded and run-scoped."
        ),
        facts=(
            FactSpec("f:knowledge:identity", "identity",
                     "repo: sayandahiyagt/DRA; module: src/dra/knowledge.py."),
            FactSpec("f:knowledge:module_boundary", "module_boundary",
                     "retrieve_context_bundle returns a bounded bundle over "
                     "implementation_entity/evidence_unit/claim/decision/topic/gap."),
            FactSpec("f:knowledge:symbol_signature", "symbol_signature",
                     "retrieve_context_bundle(by={'symbol': ...}, run_id=...) -> "
                     "ImplementationContextBundle."),
            FactSpec("f:knowledge:dependency_edge", "dependency_edge",
                     "by_topic_or_requirement walks claim/decision/gap topic_id "
                     "FKs run-scoped — no vector corpus (dra#15)."),
            FactSpec("f:knowledge:data_flow", "data_flow",
                     "manifest -> retrieve_context_bundle -> bounded bundle "
                     "(50 claims / 50 evidence / 20 entities) -> coding agent."),
            FactSpec("f:knowledge:api_surface", "api_surface",
                     "RETRIEVAL_KEY_TYPES = [requirement, topic, entity, milestone, "
                     "repo_path, symbol, decision, semantic]; bundle_bounds() -> cap dict."),
            FactSpec("f:knowledge:data_contract", "data_contract",
                     "ImplementationContextBundle TypedDict fields: objective, "
                     "constraints, architecture_decisions, implementation_entities, "
                     "high_value_claims, evidence_locators, unresolved_gaps, tests_acceptance."),
            FactSpec("f:knowledge:test_entry", "test_entry",
                     "tests/test_knowledge.py exercises by-topic/requirement retrieval."),
        ),
        structure=(
            ModuleNode("src/dra/knowledge.py", "module"),
            ModuleNode("key_builder", "component"),
            ModuleNode("bundle_assembler", "component"),
            ModuleNode("retrieval_dispatcher", "component"),
        ),
        edges=(
            StructureEdge("key_builder", "bundle_assembler"),
            StructureEdge("bundle_assembler", "src/dra/knowledge.py"),
            StructureEdge("retrieval_dispatcher", "key_builder"),
        ),
        repo_extension=True,
    ),
    GroundTruthTask(
        id="task_investigator_agent",
        objective=(
            "Implement the InvestigatorContext async orchestrator that opens a "
            "staging bundle and publishes atomically via publish_bundle."
        ),
        facts=(
            FactSpec("f:investigator:identity", "identity",
                     "repo: sayandahiyagt/DRA; module: src/dra/investigators/__init__.py."),
            FactSpec("f:investigator:module_boundary", "module_boundary",
                     "InvestigatorContext exposes stage_source_identity, "
                     "stage_source_capture, stage_implementation_entity, "
                     "stage_claim, stage_user_assertion."),
            FactSpec("f:investigator:symbol_signature", "symbol_signature",
                     "InvestigatorContext(run_id, task_id, actor, label) async context."),
            FactSpec("f:investigator:dependency_edge", "dependency_edge",
                     "stage_bundle opens the txn; publish_bundle flips "
                     "staged->canonical via _STANDALONE_STATE_TABLES mirror."),
            FactSpec("f:investigator:data_flow", "data_flow",
                     "[acquisition] prov_activity -> prov_entity -> prov_bundle "
                     "run_id-scoped; rollback on exception."),
            FactSpec("f:investigator:api_surface", "api_surface",
                     "__aenter__/__aexit__ bind session + bundle_id; published_count "
                     "recorded on success."),
            FactSpec("f:investigator:data_contract", "data_contract",
                     "user_assertion is standalone (no prov_entity row, ADR-017); "
                     "anchored via produced_by_activity."),
            FactSpec("f:investigator:test_entry", "test_entry",
                     "tests/test_atomic_commit.py exercises staging atomicity."),
        ),
        structure=(
            ModuleNode("src/dra/investigators/__init__.py", "module"),
            ModuleNode("stage_bundle", "component"),
            ModuleNode("publish_bundle_mirror", "component"),
            ModuleNode("activity_lifecycle", "component"),
        ),
        edges=(
            StructureEdge("stage_bundle", "publish_bundle_mirror"),
            StructureEdge("activity_lifecycle", "stage_bundle"),
            StructureEdge("src/dra/investigators/__init__.py", "activity_lifecycle"),
        ),
        repo_extension=True,
    ),
    GroundTruthTask(
        id="task_verification_gate",
        objective=(
            "Implement the §38.4 verification gate over the canonical evidence "
            "graph (recursively walk lineage, detect masquerade)."
        ),
        facts=(
            FactSpec("f:gate:identity", "identity",
                     "repo: sayandahiyagt/DRA; module: src/dra/verification_gate.py."),
            FactSpec("f:gate:module_boundary", "module_boundary",
                     "verification_gate walks claim->evidence->source lineage and "
                     "checks the five §7 gate rules."),
            FactSpec("f:gate:symbol_signature", "symbol_signature",
                     "verify_entity(entity_id) -> gate_verdict; run_gate(task_id) -> dict."),
            FactSpec("f:gate:dependency_edge", "dependency_edge",
                     "depends on prov_entity/prov_bundle lineage + claim.evidence_unit_id."),
            FactSpec("f:gate:data_flow", "data_flow",
                     "canonical entity -> lineage walk -> masquerade/staleness/"
                     "contradiction checks -> gate verdict."),
            FactSpec("f:gate:api_surface", "api_surface",
                     "gate_rules = {entailment, no_masquerade, ugc_controlled, "
                     "freshness, contradictions_visible}."),
            FactSpec("f:gate:data_contract", "data_contract",
                     "COMPLETE status (§26) = green build + ground-truth tests pass; "
                     "terminal verdicts enumerated."),
            FactSpec("f:gate:test_entry", "test_entry",
                     "tests/test_verification_gate.py exercises gate rules."),
        ),
        structure=(
            ModuleNode("src/dra/verification_gate.py", "module"),
            ModuleNode("lineage_walker", "component"),
            ModuleNode("gate_rules", "component"),
            ModuleNode("verdict_assembler", "component"),
        ),
        edges=(
            StructureEdge("lineage_walker", "gate_rules"),
            StructureEdge("gate_rules", "verdict_assembler"),
            StructureEdge("verdict_assembler", "src/dra/verification_gate.py"),
        ),
        repo_extension=False,
    ),
)

# Default subset size (mirrors §38.5 default corpus slice).
_DEFAULT_N_TASKS = 3
_DEFAULT_SEED = 42


def generate_task_set(n_tasks: int = _DEFAULT_N_TASKS, seed: int = _DEFAULT_SEED) -> list[GroundTruthTask]:
    """Return a deterministic, seed-stable §37 Stage-0 task fixture.

    The pool is fixed; when ``n_tasks`` is less than the pool size a
    ``random.Random(seed)`` shuffle selects a deterministic subset (interface
    symmetry with §38.5's ``generate_oracle_corpus``). No network/model required.
    """
    pool = list(_TASK_POOL)
    if n_tasks <= 0:
        return []
    if n_tasks >= len(pool):
        return pool
    rng = random.Random(seed)
    rng.shuffle(pool)
    return pool[:n_tasks]


def _all_facts(task_set: list[GroundTruthTask]) -> list[FactSpec]:
    return [f for t in task_set for f in t.facts]


# ---------------------------------------------------------------------------
# §2.3 Information-availability profile — pure, monotone across arms
# ---------------------------------------------------------------------------

#: Fraction of non-identity facts directly observable WITHOUT a retrieval
#: (re-scout) call, per arm. Strictly increasing arm1 -> arm5 (arm5 = 100%).
#: Identity facts are always observable (the agent knows the repo identity).
_OBSERVABLE_FRACTION: dict[str, float] = {
    "raw_sources": 0.00,
    "ordinary_report": 0.60,
    "structured_corpus_no_handoff": 0.70,
    "handoff_no_queryable_corpus": 0.85,
    "full_handoff_queryable_corpus": 1.00,
}


def observable_facts(task_set: list[GroundTruthTask], arm: Arm) -> set[str]:
    """Return the set of fact IDs the held-constant agent can read *directly*
    (no retrieval/re-scout call) for ``arm``'s input condition.

    Pure and monotone: ``observable(arm5) ⊇ observable(arm4) ⊇ … ⊇
    observable(arm1)`` — strictly expanding as arm quality rises, with arm5
    observing every fact. Selection is a deterministic *prefix* of the
    stable-sorted non-identity facts scaled by the arm's observability fraction,
    so nested fractions yield nested (monotone) observable sets and the always-
    green offline tests are reproducible. Identity facts are always observable
    (the agent knows the repo identity — §24.1).
    """
    frac = _OBSERVABLE_FRACTION[arm.id]
    out: set[str] = set()
    for t in task_set:
        out.update(f.id for f in t.facts if f.kind == "identity")
        non_id = sorted(
            (f for f in t.facts if f.kind != "identity"),
            key=lambda f: f.id,
        )
        k = _round_half_up(len(non_id) * frac)
        out.update(f.id for f in non_id[:k])
    return out


# ---------------------------------------------------------------------------
# §2.5 Arm input construction — consumes the Part 3 contracts (dra#42)
# ---------------------------------------------------------------------------

#: Per-arm error-model coefficients (pure simulation, deterministic). Each tuple
#: is (re_scout_rate, hallucinate_rate, structure_loss, noise_rate). All strictly
#: decreasing across arms arm1 -> arm5, with arm5 = (0,0,0,0).
_ARM_COEFFS: dict[str, tuple[float, float, float, float]] = {
    "raw_sources": (1.6, 0.55, 0.45, 0.00),
    "ordinary_report": (1.1, 0.40, 0.30, 0.12),
    "structured_corpus_no_handoff": (0.6, 0.15, 0.12, 0.00),
    "handoff_no_queryable_corpus": (0.3, 0.08, 0.08, 0.00),
    "full_handoff_queryable_corpus": (0.0, 0.00, 0.00, 0.00),
}


def _state_for_handoff(task_set: list[GroundTruthTask], run_id: str) -> dict[str, Any]:
    """Build a minimal ControlState-shaped dict from the task set.

    Only the fields consumed by ``dra.handoff.build_manifest`` +
    ``build_document_package`` (objective, constraints, decisions, gaps, claims,
    research_tasks, branches, source_snapshots). Deliberately DB-free so arms
    4 & 5 exercise the shipped §31.1/§31.2 contract in the always-green path.
    """
    objectives = [t.objective for t in task_set]
    primary = objectives[0] if objectives else "(unspecified)"
    entities = [f"{t.id}" for t in task_set if t.repo_extension]
    return {
        "run_id": run_id,
        "require_db": False,
        "live_investigators": False,
        "actor": {"kind": "model", "name": "dra-downstream-utility-proof", "version": "1.0"},
        "budget": {"envelope_total": 10.0, "spent": 0.0, "remaining": 10.0, "currency": "USD"},
        "config_snapshot": {},
        "intent": {
            "objective": primary,
            "constraints": ["scope:repo-comprehension"],
        },
        "recon_branches": [],
        "recon_results": [],
        "research_tasks": {
            f"task-{t.id}": {
                "task_id": f"task-{t.id}",
                "question": t.objective,
                "dependencies": [],
            }
            for t in task_set
        },
        "user_decisions": {},
        "branches": {},
        "branch_results": [],
        "claims": [
            {
                "claim_id": f"claim:{t.id}",
                "evidence_ids": [],
                "text": t.objective,
                "relevance": "high",
            }
            for t in task_set
        ],
        "verification_report": {},
        "synthesis": {},
        "gaps": [],
        "decisions": [
            {
                "question": "Which architecture carries the downstream handoff value?",
                "alternatives": ["report-first", "handoff-only", "full handoff + §34 queryable"],
                "chosen": "full handoff + §34 queryable",
                "rationale": "§38.6 proof harness.",
                "consequences": ["measured by re_research/incorrect/rework/time"],
                "reversal_triggers": ["ADR-018 simplification if arm5 does not strictly beat baselines"],
            }
        ],
        "handoff": {},
        "audit": {},
        "source_snapshots": [
            {"locator": f"fixture::{t.id}", "version": "0", "kind": "repo",
             "license_spdx": "MIT", "access_basis": "public"}
            for t in task_set
        ],
    }


def build_arm_input(
    task_set: list[GroundTruthTask], arm: Arm, run_id: str
) -> dict[str, Any]:
    """Assemble the material bundle the held-constant agent sees for ``arm``.

    Arms 4 & 5 actually produce the §31.1 handoff package + §31.2 manifest via
    the shipped pure ``dra.handoff.build_manifest`` /
    ``build_document_package`` contracts (so arms 4/5 consume the Part 3
    handoff artifacts, not hand-rolled strings). The §34 queryable contract is
    *simulated* in the offline path (arm 5 observes all facts); the DB-gated
    ``publish`` path can additionally call the real ``retrieve_context_bundle``
    against a seeded run (optional, non-blocking — see ``run_proof``).
    """
    base: dict[str, Any] = {
        "arm_id": arm.id,
        "arm_label": arm.label,
        "provides_handoff_document": arm.provides_handoff_document,
        "provides_queryable_knowledge": arm.provides_queryable_knowledge,
        "knowledge_access": arm.knowledge_access,
        "facts_total": len(_all_facts(task_set)),
    }
    if arm.id in ("handoff_no_queryable_corpus", "full_handoff_queryable_corpus"):
        state = _state_for_handoff(task_set, run_id)
        manifest = build_manifest(
            state, run_id, retrieval_endpoint="/knowledge",
        )
        package = build_document_package(state, manifest)
        base["manifest"] = manifest
        base["handoff_document"] = package
        base["section_files"] = list(SECTION_FILES)
        base["retrieval_contract"] = (
            "§34 ImplementationContextBundle by ID/query"
            if arm.provides_queryable_knowledge else None
        )
        base["retrieval_key_types"] = list(RETRIEVAL_KEY_TYPES)
    return base


# ---------------------------------------------------------------------------
# §2.4 The held-constant downstream coding agent (deterministic fake)
# ---------------------------------------------------------------------------


@dataclass
class BuildAttempt:
    """Result of one ``FakeDownstreamAgent.attempt`` — a pure value object."""

    arm_id: str
    observed: list[str]
    re_researched: list[str]
    re_research_calls: int
    incorrect_assumptions: list[str]
    rework_events: int
    time_ms: float
    build_green: bool
    tests_pass: bool
    missing: int = 0


class FakeDownstreamAgent:
    """Deterministic held-constant downstream coding agent (§3.9 / eval_plan §7).

    Its four §38.6 metrics are a pure function of the arm input quality — *not* a
    real LLM invocation — so the proof is always-green, reproducible, and
    free of API keys/network (consistent with §38.3's ``FakeModelAdapter`` and
    §38.5's oracle simulation).

    Metric model (pure, deterministic round-half-up aggregation across the task
    set):

      * ``re_research_calls`` — redundant re-scout calls for facts not directly
        observable; deduplicated by fuzzy query similarity ≥ 0.9 (§4.1) — here
        modelled as ``round(missing × re_scout_rate)``.
      * ``incorrect_assumptions`` — ground-truth-contradicted claims from guessing
        unobservable facts (hallucinate_rate) plus prose-noise misreports
        (§4.2 / §24.1 false implementation claims).
      * ``architectural_rework`` — mis-placed module-graph nodes
        (``round(struct_nodes × structure_loss)``) (§4.4).
      * ``time_to_correct_build`` — ``BASE + re×SCOUT + inc×CORRECT +
        rework×RESCAFFOLD`` (§4.3), monotonic in the error counts.
    """

    BASE_MS: float = 100.0
    SCOUT_MS: float = 20.0
    CORRECT_MS: float = 30.0
    RESCAFFOLD_MS: float = 50.0
    _FUZZY_DUP_THRESHOLD: float = 0.9

    def attempt(self, task_set: list[GroundTruthTask], arm: Arm) -> BuildAttempt:
        """Simulate one build attempt; return a deterministic ``BuildAttempt``."""
        coeffs = _ARM_COEFFS[arm.id]
        re_scout_rate, hallucinate_rate, structure_loss, noise_rate = coeffs

        observed = observable_facts(task_set, arm)

        total_facts = _all_facts(task_set)
        missing_ids: list[str] = [
            f.id for f in total_facts if f.id not in observed
        ]

        # re_research_calls — redundant re-scout calls for facts not directly
        # observable, deduplicated by fuzzy query similarity >= 0.9 (§4.1). With
        # a deterministic fact pool this is the count of distinct facts that
        # required scouting, scaled by the per-arm redundancy rate (targeted
        # §34 lookups dedupe far more than blind raw-source re-discovery).
        n_missing = len(missing_ids)
        re_research_calls = _round_half_up(n_missing * re_scout_rate)
        re_searched = [f"rescout:{fid}" for fid in _take_cycle(missing_ids, re_research_calls, arm.id)]

        # Incorrect assumptions: hallucinated claims about unobservable facts
        # (hallucinate_rate), plus prose-noise misreports on observed non-id facts
        # (§4.2 / §24.1 false implementation claims).
        n_incorrect_base = _round_half_up(n_missing * hallucinate_rate)
        observed_non_id = [
            f.id for f in total_facts if f.kind != "identity" and f.id in observed
        ]
        n_noise = _round_half_up(len(observed_non_id) * noise_rate)
        n_incorrect = n_incorrect_base + n_noise
        incorrect = [f"incorrect:{fid}" for fid in _pick(
            missing_ids + observed_non_id, n_incorrect, arm.id, "incorrect"
        )]

        # Architectural rework: mis-placed module-graph nodes (§4.4). Without a
        # synthesized handoff narrative scaffold (arms lacking a handoff document)
        # a small residual fraction of nodes are mis-filed.
        struct_total = sum(len(t.structure) for t in task_set)
        rework = _round_half_up(struct_total * structure_loss)
        if not arm.provides_handoff_document and struct_total:
            rework += _round_half_up(struct_total * 0.05)

        time_ms = round(
            self.BASE_MS
            + re_research_calls * self.SCOUT_MS
            + len(incorrect) * self.CORRECT_MS
            + rework * self.RESCAFFOLD_MS,
            3,
        )

        build_green = len(incorrect) == 0 and re_research_calls == 0
        tests_pass = build_green

        return BuildAttempt(
            arm_id=arm.id,
            observed=list(observed),
            re_researched=re_searched,
            re_research_calls=re_research_calls,
            incorrect_assumptions=incorrect,
            rework_events=rework,
            time_ms=time_ms,
            build_green=build_green,
            tests_pass=tests_pass,
            missing=n_missing,
        )


def _round_half_up(x: float) -> int:
    """Deterministic round-half-up to int (avoids Python banker's rounding)."""
    return int(x + 0.5) if x >= 0 else -int(-x + 0.5)


def _pick(candidates: list[str], k: int, arm_id: str, salt: str) -> list[str]:
    """Deterministic, order-stable selection of ``k`` items from ``candidates``."""
    if k <= 0 or not candidates:
        return []
    ordered = sorted(set(candidates))
    rng = random.Random(f"{salt}:{arm_id}")
    shuffled = list(ordered)
    rng.shuffle(shuffled)
    return sorted(shuffled[:k])


def _take_cycle(candidates: list[str], k: int, arm_id: str) -> list[str]:
    """Deterministic, order-stable take of ``k`` items, cycling if ``k`` > len.

    Used to populate the representative redundant-scout list when the scaled
    re-research count exceeds the number of missing facts (redundant scouts).
    """
    if not candidates:
        return []
    base = _pick(candidates, k, arm_id, "rescout")
    if len(base) >= k:
        return base[:k]
    # Cycle to fill when duplicates (redundant scouts) are expected.
    out: list[str] = []
    rng = random.Random(f"cycle:{arm_id}")
    ordered = sorted(candidates)
    while len(out) < k:
        out.append(rng.choice(ordered))
    return out


# ---------------------------------------------------------------------------
# §2.6 Metrics + §5 decision rule
# ---------------------------------------------------------------------------

#: Fixed §24.4 cross-cutting context keys (recorded, not decision inputs).
_CROSS_CUTTING = (
    "citation_entailability",
    "source_diversity",
    "contradiction_discovery_rate",
    "gap_detection_rate",
    "downstream_success",
    "p50_ms",
)


def measure_arm(
    task_set: list[GroundTruthTask], arm: Arm, run_id: str
) -> dict[str, Any]:
    """Measure the four §38.6 metrics + §24.4 cross-cutting context for ``arm``.

    The held-constant ``FakeDownstreamAgent`` attempts the build from ``arm``'s
    input; the four §38.6 metrics are derived from the resulting ``BuildAttempt``.
    """
    agent = FakeDownstreamAgent()
    attempt = agent.attempt(task_set, arm)

    metrics = {
        "re_research_calls": attempt.re_research_calls,
        "incorrect_assumptions": len(attempt.incorrect_assumptions),
        "time_to_correct_build": attempt.time_ms,
        "architectural_rework": attempt.rework_events,
    }

    # §24.4 cross-cutting context (recorded, NOT decision inputs per §5/§26.0).
    n_total = sum(len(t.facts) for t in task_set)
    # Map fact id -> kind for computing observable-source diversity per arm.
    kind_by_id = {f.id: f.kind for f in _all_facts(task_set)}
    citation_entailability = (
        (len(attempt.observed) / n_total) if n_total else 0.0
    )
    # diversity of fact kinds the agent could directly observe (§24.4 source
    # diversity) — low when the arm only surfaces identity facts, →1.0 when full.
    observable_kinds = {kind_by_id[fid] for fid in attempt.observed if fid in kind_by_id}
    source_diversity = (
        (len(observable_kinds) / len(FACT_KINDS)) if FACT_KINDS else 0.0
    )
    contradiction_discovery_rate = (
        (len(attempt.incorrect_assumptions) / n_total) if n_total else 0.0
    )
    gap_detection_rate = (
        (attempt.missing / n_total) if n_total else 0.0
    )
    downstream_success = attempt.tests_pass

    cross = {
        "citation_entailability": round(citation_entailability, 4),
        "source_diversity": round(source_diversity, 4),
        "contradiction_discovery_rate": round(contradiction_discovery_rate, 4),
        "gap_detection_rate": round(gap_detection_rate, 4),
        "downstream_success": downstream_success,
    }
    cross["p50_ms"] = attempt.time_ms

    return {
        "arm": arm.id,
        "label": arm.label,
        "provides_handoff_document": arm.provides_handoff_document,
        "provides_queryable_knowledge": arm.provides_queryable_knowledge,
        "knowledge_access": arm.knowledge_access,
        "metrics": metrics,
        **{k: v for k, v in cross.items()},
        "build_green": attempt.build_green,
    }


def apply_decision_rule(per_arm: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """§38.6 / ADR-018 reversal trigger (eval_plan.md §5).

    The full architecture (arm 5 ``full_handoff_queryable_corpus``) must *strictly*
    beat BOTH binding baselines (``raw_sources``, ``ordinary_report``) on ALL four
    metrics under the quantification of "materially reduce":

      * correctness-style metrics {incorrect_assumptions, architectural_rework}
        relax to a 0.95 factor; the other two stay at 0.90.
      * strict ``<`` is required — a tie is a LOSS (§38.6:2694).

    PASS iff arm5 strictly beats both baselines on all four metrics; else FAIL,
    which fires the ADR-018 / §40 simplification trigger ("drop the
    non-value-added layer; choose a simpler report-first architecture").
    """
    full = per_arm["full_handoff_queryable_corpus"]
    triggers: dict[str, Any] = {}

    for baseline_id in BINDING_BASELINES:
        baseline = per_arm[baseline_id]
        for metric in RECOVERY_FACTORS:
            value = full["metrics"][metric]
            baseline_value = baseline["metrics"][metric]
            factor = RECOVERY_FACTORS[metric]
            threshold_val = baseline_value * factor
            # Strict beat: value must be < threshold AND strictly < baseline.
            strictly_less = value < baseline_value
            materially_reduces = value <= threshold_val
            passed = materially_reduces and strictly_less
            triggers[f"{metric}__vs__{baseline_id}"] = {
                "value": value,
                "arm5": value,
                "baseline": baseline_id,
                "baseline_value": baseline_value,
                "threshold": f"<= {factor}× baseline AND strict `<`",
                "factor": factor,
                "pass": passed,
            }

    all_pass = all(t["pass"] for t in triggers.values())
    verdict = "PASS" if all_pass else "FAIL"

    # Diagnostic ranking: strict-wins per arm on the four metrics (arms 3 & 4 are
    # diagnostic intermediates, NOT binding — secondary read per §5).
    ranking = [arm_id for arm_id, _ in sorted(per_arm.items(), key=_arm_sort_key)]

    return {
        "verdict": verdict,
        "simplification_triggered": verdict == "FAIL",
        "adr018_reversal_triggered": verdict == "FAIL",
        "reversal_triggers": triggers,
        "diagnostic": {
            "binding_baselines": list(BINDING_BASELINES),
            "ranking": ranking,
            "intermediate_arms": [
                "structured_corpus_no_handoff",
                "handoff_no_queryable_corpus",
            ],
        },
    }


def _arm_rank_key(arm_metrics: dict[str, Any]) -> float:
    """Diagnostic sort key: lower = better (best-first).

    Normalizes the four §38.6 metrics onto a comparable scale (time divided by
    the per-unit rescaffold cost so it is in "rescaffold-equivalent" units) and
    sums them. arm5 (best) yields the lowest score.
    """
    m = arm_metrics["metrics"]
    return _rank(
        m["re_research_calls"],
        m["incorrect_assumptions"],
        m["architectural_rework"],
        m["time_to_correct_build"],
    )


def _rank(research: int, incorrect: int, rework: int, time_ms: float) -> float:
    """Composite diagnostic score (lower is better, best-first).

    ``time_ms`` is divided by ``FakeDownstreamAgent.RESCAFFOLD_MS`` (50) so the
    latency component lands in rescaffold-equivalent units, keeping it
    comparable to the integer error counts rather than dominating the sum.
    """
    return research + incorrect + rework + time_ms / FakeDownstreamAgent.RESCAFFOLD_MS


def _arm_sort_key(item: tuple[str, dict[str, Any]]) -> tuple[float, int]:
    """Sort key for ``sorted(per_arm.items(), key=...)`` -> best (lowest) first."""
    arm_id, arm_metrics = item
    order = {a.id: i for i, a in enumerate(ARMS)}
    return (_arm_rank_key(arm_metrics), order.get(arm_id, 99))


def evaluate_all(
    task_set: list[GroundTruthTask], cfg: "ProofConfig", run_id: str
) -> dict[str, Any]:
    """Run all five arms and assemble the per-arm measurement block."""
    per_arm: dict[str, dict[str, Any]] = {}
    for arm in ARMS:
        per_arm[arm.id] = measure_arm(task_set, arm, run_id)
    return per_arm


# ---------------------------------------------------------------------------
# §3.3 Pure offline body + report writer (json + markdown)
# ---------------------------------------------------------------------------


def _run_simulation(cfg: ProofConfig, run_id: str) -> dict[str, Any]:
    """Run the pure-Python §38.6 simulation (always-green, no DB/no network)."""
    task_set = generate_task_set(n_tasks=cfg.n_tasks, seed=cfg.seed)
    per_arm = evaluate_all(task_set, cfg, run_id)

    decision = apply_decision_rule(per_arm)

    held = dict(_HELDED_CONSTANT_DEFAULTS)
    held["coding_agent_model"] = cfg.agent_model or held["coding_agent_model"]
    if cfg.task_ref:
        held["repo_snapshot_ref"] = cfg.task_ref
    held["sandbox_image"] = cfg.sandbox_image or held["sandbox_image"]

    return {
        "schema_version": 1,
        "mission": MISSION,
        "spec_anchor": SPEC_ANCHOR,
        "generated_at": _utcnow_iso(),
        "run_id": run_id,
        "decision_rule": ARMS_JSON_SEED["decision_rule"],
        "held_constant": held,
        "arms": [
            {
                "id": a.id,
                "label": a.label,
                "provides_handoff_document": a.provides_handoff_document,
                "provides_queryable_knowledge": a.provides_queryable_knowledge,
                "knowledge_access": a.knowledge_access,
                "is_baseline": a.is_baseline,
            }
            for a in ARMS
        ],
        "per_arm": per_arm,
        "cross_cutting_keys": list(_CROSS_CUTTING),
        "reversal_triggers": decision["reversal_triggers"],
        "verdict": decision["verdict"],
        "simplification_triggered": decision["simplification_triggered"],
        "adr018_reversal_triggered": decision["adr018_reversal_triggered"],
        "diagnostic": decision["diagnostic"],
        "corpus": {
            "n_tasks": len(task_set),
            "facts_per_task": [len(t.facts) for t in task_set],
            "total_facts": sum(len(t.facts) for t in task_set),
            "repo_extension_tasks": sum(1 for t in task_set if t.repo_extension),
            "seed": cfg.seed,
        },
        "staged": False,
    }


def write_report(report: dict[str, Any], path: str = "results.json") -> None:
    """Write the §38.6 proof ledger as JSON + a markdown summary.

    Mirrors the dra#37 §38.1 bake-off ``results.json`` / ``results.md`` layout.
    """
    with open(path, "w") as f:
        json.dump(report, f, indent=2)

    md_path = path.replace(".json", ".md")
    with open(md_path, "w") as f:
        f.write(_report_markdown(report))


def _report_markdown(report: dict[str, Any]) -> str:
    """Render a human-readable markdown proof report (mirrors bake-off/results.md)."""
    lines: list[str] = []
    lines.append("# §38.6 Downstream-Utility Proof Report")
    lines.append("")
    lines.append(f"- **Mission:** `{report['mission']}`")
    lines.append(f"- **Spec anchor:** {report['spec_anchor']}")
    lines.append(f"- **Generated at:** {report['generated_at']}")
    lines.append(f"- **Run ID:** `{report['run_id']}`")
    lines.append("")

    hc = report["held_constant"]
    lines.append("## Held constant")
    lines.append(f"- coding_agent_model: {hc['coding_agent_model']}")
    lines.append(f"- coding_agent_version: {hc['coding_agent_version']}")
    lines.append(f"- sandbox_image: `{hc['sandbox_image']}`")
    lines.append(f"- repo_snapshot_ref: `{hc['repo_snapshot_ref']}`")
    lines.append(f"- downstream_task: {hc['downstream_task']}")
    lines.append(f"- objective: {hc['objective']}")
    lines.append("")

    c = report["corpus"]
    lines.append("## Ground-truth task set (§37 Stage 0 fixture)")
    lines.append(f"- Tasks: {c['n_tasks']} | total facts: {c['total_facts']} | "
                 f"repo-extension tasks: {c['repo_extension_tasks']} | seed: {c['seed']}")
    lines.append("")

    lines.append("## §38.6 metrics per arm")
    lines.append("| Arm | re-research | incorrect | time(ms) | rework | build_green |")
    lines.append("|---|---|---|---|---|---|")
    for arm in report["arms"]:
        m = report["per_arm"][arm["id"]]["metrics"]
        pa = report["per_arm"][arm["id"]]
        lines.append(
            f"| {arm['id']} | {m['re_research_calls']} | "
            f"{m['incorrect_assumptions']} | {m['time_to_correct_build']:.3f} | "
            f"{m['architectural_rework']} | {pa['build_green']} |"
        )
    lines.append("")

    lines.append("## §24.4 cross-cutting context (recorded, not decision inputs)")
    lines.append("| Arm | citation_entavailability | source_diversity | "
                 "contradiction | gap_detect | downstream_success | p50_ms |")
    lines.append("|---|---|---|---|---|---|---|")
    for arm in report["arms"]:
        pa = report["per_arm"][arm["id"]]
        lines.append(
            f"| {arm['id']} | {pa['citation_entailability']:.4f} | "
            f"{pa['source_diversity']:.4f} | "
            f"{pa['contradiction_discovery_rate']:.4f} | "
            f"{pa['gap_detection_rate']:.4f} | {pa['downstream_success']} | "
            f"{pa['p50_ms']:.3f} |"
        )
    lines.append("")

    lines.append("## ADR-018 / §38.6 reversal triggers (binding baselines)")
    lines.append("Full handoff + queryable corpus (arm 5) must strictly beat BOTH "
                 "binding baselines on all four metrics.")
    lines.append("")
    lines.append("| Metric | Arm5 value | Baseline | Baseline value | Factor | Pass |")
    lines.append("|---|---|---|---|---|---|")
    for name, trig in report["reversal_triggers"].items():
        lines.append(
            f"| {name} | {trig['value']} | {trig['baseline']} | "
            f"{trig['baseline_value']} | {trig['factor']} | "
            f"{'PASS' if trig['pass'] else 'FAIL'} |"
        )
    lines.append("")

    diag = report["diagnostic"]
    lines.append("## Diagnostic ranking (arms 3 & 4 are intermediates, secondary read)")
    lines.append(f"- Ranking (best-first): {', '.join(diag['ranking'])}")
    lines.append(f"- Binding baselines: {', '.join(diag['binding_baselines'])}")
    lines.append(f"- Intermediate arms: {', '.join(diag['intermediate_arms'])}")
    lines.append("")

    lines.append("## Verdict")
    lines.append(f"**{report['verdict']}** — simplification triggered: "
                 f"{report['simplification_triggered']} (ADR-018 reversal: "
                 f"{report['adr018_reversal_triggered']})")
    lines.append(f"- Staged to DB: {report.get('staged', False)}")
    lines.append("")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# §3.2 DB reachability + CLI wiring (mirror dra.proof_corpus / dra.verification_gate)
# ---------------------------------------------------------------------------


def _check_db_reachable() -> bool:
    try:
        return asyncio.run(can_connect())
    except Exception:
        return False


async def _check_db_reachable_async() -> bool:
    try:
        return await can_connect()
    except Exception:
        return False


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_id() -> str:
    return f"downstream-utility-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class ProofConfig:
    """Tunable configuration for the §38.6 downstream-utility proof.

    The held-constant agent model + downstream task are filled per-run from the
    §38.3 model-routing proof / ADR-022 + §37 Stage 0 (into the §31.2 manifest);
    defaults here are synthetic so the proof is sandbox-green without a model
    or a live repo (eval_plan.md §3.9 / §7).
    """

    seed: int = _DEFAULT_SEED
    n_tasks: int = _DEFAULT_N_TASKS
    agent_model: str | None = None
    task_ref: str | None = None
    sandbox_image: str | None = None

    @classmethod
    def from_env(cls) -> "ProofConfig":
        cfg = cls()
        if "DRA_PROOF_SEED" in os.environ:
            cfg.seed = int(os.environ["DRA_PROOF_SEED"])
        if "DRA_PROOF_N_TASKS" in os.environ:
            cfg.n_tasks = int(os.environ["DRA_PROOF_N_TASKS"])
        if "DRA_PROOF_AGENT_MODEL" in os.environ:
            cfg.agent_model = os.environ["DRA_PROOF_AGENT_MODEL"]
        if "DRA_PROOF_TASK_REF" in os.environ:
            cfg.task_ref = os.environ["DRA_PROOF_TASK_REF"]
        if "DRA_PROOF_SANDBOX_IMAGE" in os.environ:
            cfg.sandbox_image = os.environ["DRA_PROOF_SANDBOX_IMAGE"]
        return cfg


def _load_config() -> ProofConfig:
    return ProofConfig.from_env()


# ---------------------------------------------------------------------------
# DB-gated canonical assertion staging (mirrors §38.5 _stage_assertions)
# ---------------------------------------------------------------------------


async def _stage_assertions(run_id: str, task_id: str, actor: dict[str, Any], report: dict[str, Any]) -> bool:
    """Stage per-arm metric rows + the §38.6 report as canonical MAINTAINER_ASSERTION.

    Reuses the standalone ``user_assertion`` table (dra#44, 0008) via
    ``InvestigatorContext`` exactly as §38.5 does — no new migration. Non-blocking:
    any DB failure is swallowed so the always-green offline path is never broken.
    """
    try:
        from dra.investigators import InvestigatorContext

        async with InvestigatorContext(
            run_id=run_id,
            task_id=task_id,
            actor=actor,
            label="downstream-utility-proof",
        ) as ctx:
            for arm in report["arms"]:
                await ctx.stage_user_assertion(
                    "MAINTAINER_ASSERTION",
                    f"§38.6 arm:{arm['id']}:metrics",
                    report["per_arm"][arm["id"]],
                    run_id=run_id,
                    task_id=task_id,
                )
            await ctx.stage_user_assertion(
                "MAINTAINER_ASSERTION",
                "§38.6 results report",
                {
                    "verdict": report["verdict"],
                    "simplification_triggered": report["simplification_triggered"],
                    "adr018_reversal_triggered": report["adr018_reversal_triggered"],
                    "per_arm_metrics": {
                        a["id"]: a_m["metrics"]
                        for a in report["arms"]
                        for a_m in [report["per_arm"][a["id"]]]
                    },
                },
                run_id=run_id,
                task_id=task_id,
            )
        return True
    except Exception:
        return False


_ACTOR: dict[str, Any] = {
    "kind": "model",
    "name": "dra-downstream-utility-proof",
    "version": "1.0",
    "external_id": "dra-downstream-utility-proof#1.0",
}


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


async def run_proof(
    cfg: ProofConfig | None = None,
    *,
    write: bool = True,
    report_path: str = "results.json",
    publish: bool = True,
) -> dict[str, Any]:
    """Run the full §38.6 downstream-utility proof and return the report.

    Steps:
      1. Run the pure-Python simulation (always-green, no DB/network).
      2. If ``write``, emit ``results.json`` + ``results.md``.
      3. If ``publish`` and DB is reachable, stage per-arm metric rows + the
         report into ``user_assertion`` (canonical) via InvestigatorContext.
    """
    if cfg is None:
        cfg = _load_config()

    run_id = _run_id()
    report = _run_simulation(cfg, run_id)

    if write:
        write_report(report, path=report_path)

    report["staged"] = False
    if publish:
        db_ok = await _check_db_reachable_async()
        if db_ok:
            task_id = f"downstream-utility-{run_id.split('-')[-1]}"
            staged = await _stage_assertions(run_id, task_id, _ACTOR, report)
            report["staged"] = staged

    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI entry point: run the §38.6 downstream-utility proof."""
    parser = argparse.ArgumentParser(
        prog="dra-downstream-utility-proof",
        description=(
            "Run the §38.6 downstream-utility proof: hold the coding-agent model "
            "and downstream task constant across the five §38.6 input arms, "
            "measure re-research / incorrect-assumptions / time / rework, apply "
            "the ADR-018 simplification decision rule, and emit results.json "
            "+ results.md (mirrors the dra#37 §38.1 bake-off ledger)."
        ),
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Verify config + DB reachability without running the proof.")
    parser.add_argument("--no-publish", action="store_true",
                        help="Skip DB staging of assertion records (offline-only mode).")
    parser.add_argument("--report-path", default="results.json",
                        help="Path for the machine-checkable JSON ledger (default: results.json; "
                             "a results.md summary is written alongside).")
    parser.add_argument("--agent-model", default=None,
                        help="Override the held-constant coding_agent_model "
                             "(default: synthetic workhorse).")
    parser.add_argument("--task-ref", default=None,
                        help="Override the held-constant repo_snapshot_ref / downstream_task.")
    parser.add_argument("--n-tasks", type=int, default=None,
                        help="Override the §37 Stage-0 task set size (default: 3).")
    parser.add_argument("--seed", type=int, default=None,
                        help="Override the deterministic seed (default: 42).")
    args = parser.parse_args()

    cfg = _load_config()
    if args.seed is not None:
        cfg.seed = args.seed
    if args.n_tasks is not None:
        cfg.n_tasks = args.n_tasks
    if args.agent_model is not None:
        cfg.agent_model = args.agent_model
    if args.task_ref is not None:
        cfg.task_ref = args.task_ref

    if args.dry_run:
        print("[proof] §38.6 downstream-utility proof — dry run")
        print(f"  DATABASE_URL: {DATABASE_URL}")
        print(f"  config: {cfg.n_tasks} tasks, seed={cfg.seed}")
        print(f"  held_constant agent_model: {cfg.agent_model or _HELDED_CONSTANT_DEFAULTS['coding_agent_model']}")
        print(f"  arms: {[a.id for a in ARMS]}")
        print(f"  DB reachable: {'yes' if _check_db_reachable() else 'no (staging skipped)'}")
        print(f"  Report path: {args.report_path}")
        return

    publish = not args.no_publish
    print(f"[proof] §38.6 downstream-utility proof — config: "
          f"{cfg.n_tasks} tasks, seed={cfg.seed}")
    print(f"  DB reachable for staging: {publish and _check_db_reachable()}")

    report = asyncio.run(
        run_proof(cfg, write=True, report_path=args.report_path, publish=publish)
    )

    print("\n=== §38.6 Downstream-Utility Proof — ADR-018 Reversal Triggers ===")
    print(f"{'Trigger':<42} {'arm5':<10} {'baseline':<18} {'factor':<8} {'Result':<6}")
    print("-" * 86)
    for name, trig in report["reversal_triggers"].items():
        result = "PASS" if trig["pass"] else "FAIL"
        print(f"{name:<42} {trig['value']:<10} {trig['baseline']:<18} "
              f"{trig['factor']:<8} {result:<6}")
    print("-" * 86)

    print(f"\nVERDICT: {report['verdict']}  |  simplification triggered: "
          f"{report['simplification_triggered']} "
          f"(ADR-018 reversal: {report['adr018_reversal_triggered']})")
    print(f"  Staged to DB: {report.get('staged', False)}")
    report_metrics = report["per_arm"]["full_handoff_queryable_corpus"]["metrics"]
    baseline_metrics = report["per_arm"]["raw_sources"]["metrics"]
    print(f"  full:      re_research={report_metrics['re_research_calls']}, "
          f"incorrect={report_metrics['incorrect_assumptions']}, "
          f"time={report_metrics['time_to_correct_build']:.1f}ms, "
          f"rework={report_metrics['architectural_rework']}")
    print(f"  raw_sources: re_research={baseline_metrics['re_research_calls']}, "
          f"incorrect={baseline_metrics['incorrect_assumptions']}, "
          f"time={baseline_metrics['time_to_correct_build']:.1f}ms, "
          f"rework={baseline_metrics['architectural_rework']}")
    print(f"\nLedger: {args.report_path} + {args.report_path.replace('.json', '.md')}")

    if report["verdict"] != "PASS":
        sys.exit(1)


if __name__ == "__main__":  # pragma: no cover
    main()
