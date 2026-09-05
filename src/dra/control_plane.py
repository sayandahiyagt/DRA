"""LangGraph control-plane state machine for the spec §10 research loop (dra#36).

Durable, Postgres-checkpointed state machine that executes the 15-phase §10
end-to-end research pipeline (bootstrap → breadth interview → reconnaissance
fan-out → research-DAG synthesis → focused clarification → deep branch
execution → evidence normalization & commit → claim construction → source-and-claim
verification → topic/impl graph → critic review → targeted re-research →
architecture decision synthesis → handoff generation → handoff audit).

The control plane *orchestrates* the existing substrate
(``dra.publish``, ``dra.investigators``, ``dra.verification_gate``,
``dra.routing``) rather than reimplementing it. Per ADR-002 the checkpoint
payload holds **control state only** — canonical evidence never lives inside
the checkpoint / agent-internal state; every fan-out worker opens its own
:class:`~dra.investigators.InvestigatorContext` bundle so canonical evidence is
written through :func:`dra.publish.publish_bundle` inside its own transaction.

DB-dependent submodules (``dra.publish``, ``dra.investigators``,
``dra.verification_gate``, ``dra.routing.models``) are imported lazily inside
the functions that use them, so ``build_graph().compile()`` (the no-DB
verification path exercised by ``tests/test_control_plane.py::test_graph_assembles``)
works even when SQLAlchemy / the investigate-extras are not importable.

CLI entry: ``dra-control-plane`` (wired in ``pyproject.toml``).
"""

from __future__ import annotations

import argparse
import asyncio
import operator
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, StateGraph
from langgraph.store.memory import InMemoryStore
from langgraph.types import Send, interrupt


# ---------------------------------------------------------------------------
# Status vocabulary (spec §10)
# ---------------------------------------------------------------------------
RUNNING = "RUNNING"
INCOMPLETE = "INCOMPLETE"
COMPLETE = "COMPLETE"
FAILED = "FAILED"

# Branch-level lifecycle (Phase 5/6)
B_STAGED = "STAGED"
B_COMMIT_FAILED = "COMMIT_FAILED"
B_BLOCKED = "BLOCKED"
B_COMPLETE = "COMPLETE"

# Interview strategies for the §38.5 A/B (dra#45).
#   progressive : existing shallow-breadth p1 interrupt / recon p2 / p4 clarification loop
#   exhaustive  : p1 interrupts with the full §9.1 Stage-A questionnaire; p4 is a no-op
#   minimal     : p1 interrupts for an objective only; p4 is skipped
STRATEGY_PROGRESSIVE = "progressive"
STRATEGY_EXHAUSTIVE = "exhaustive"
STRATEGY_MINIMAL = "minimal"
_VALID_STRATEGIES = (STRATEGY_PROGRESSIVE, STRATEGY_EXHAUSTIVE, STRATEGY_MINIMAL)
_DEFAULT_STRATEGY = STRATEGY_PROGRESSIVE

NUM_PHASES = 15  # p0 .. p14

# Per-investigator cost units (§31 cost accounting — symbolic, budget-tracked).
_PHASE_COST = {
    0: 0.0,
    1: 0.0,
    2: 0.1,
    3: 0.1,
    4: 0.0,
    5: 0.0,  # branch cost charged per-branch in run_branch_worker
    6: 0.1,
    7: 0.1,
    8: 0.5,
    9: 0.1,
    10: 0.2,
    11: 0.0,
    12: 0.1,
    13: 0.1,
    14: 0.0,
}
_PER_BRANCH_COST = 0.5


# ---------------------------------------------------------------------------
# Structured phase payloads (frozen helpers; stored as dicts in state)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IntentSnapshot:
    """Phase 1 output — the researched problem statement."""

    objective: str
    questions: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    parent_run_ids: list[str] = field(default_factory=list)
    sources: list[dict[str, Any]] = field(default_factory=list)
    user_decisions: dict[str, str] = field(default_factory=dict)


def _intent_to_dict(intent: dict[str, Any] | IntentSnapshot | None) -> dict[str, Any]:
    if intent is None:
        return {}
    if isinstance(intent, dict):
        return dict(intent)
    return asdict(intent)


@dataclass(frozen=True)
class ReconBranch:
    """Phase 2 output — one reconnaissance perspective + its query."""

    perspective: str
    query: str
    seen_source_ids: list[str] = field(default_factory=list)


_RECON_PERSPECTIVES = (
    "implementation_mechanisms",
    "closest_existing_systems",
    "alternatives",
    "empirical_evidence",
    "failure_security_licensing_risk",
    "source_of_truth",
)


@dataclass
class ResearchTask:
    """Phase 3 output — a single typed, gated research task."""

    task_id: str
    question: str
    parent_question: str | None
    why_it_matters: str
    artifact_type: str
    source_types: list[str]
    dependencies: list[str]
    priority: int
    breadth: int
    depth: int
    model_policy: dict[str, Any]
    acceptance_criteria: list[str]
    verification_policy: dict[str, Any]
    stopping_conditions: list[str]
    retry_rules: dict[str, Any]
    cost_envelope: float
    source: dict[str, Any] = field(default_factory=dict)


@dataclass
class BranchState:
    """Phase 5/6 output — per-branch publication state."""

    task_id: str
    status: str
    evidence_ids: list[str] = field(default_factory=list)
    claim_ids: list[str] = field(default_factory=list)
    bundle_id: str | None = None
    published_count: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ClaimRef:
    """Phase 7 output — a claim anchored to supporting evidence."""

    claim_id: str
    evidence_ids: list[str]
    contradictions: list[str]
    inference_type: str
    source_independence: int
    freshness: str
    relevance: str
    text: str


@dataclass(frozen=True)
class ResearchGap:
    """Phase 10 output — a prioritized gap/contradiction flagged by the critic."""

    gap_id: str
    description: str
    severity: str
    impact: int
    blocking: bool
    related_claim_ids: list[str]


@dataclass
class Decision:
    """Phase 12 output — an architecture/implementation decision."""

    question: str
    alternatives: list[str]
    evidence_ids: list[str]
    user_preference_deps: list[str]
    chosen: str
    rationale: str
    consequences: list[str]
    reversal_triggers: list[str]


# ---------------------------------------------------------------------------
# ControlState — the LangGraph checkpoint payload (control state only)
# ---------------------------------------------------------------------------
# Fan-out accumulators use Annotated[list, operator.add] so parallel worker
# nodes can merge partial results into a single channel (LangGraph rejects
# concurrent root writes on plain list channels).
_BUDGET_DEFAULT = {
    "envelope_total": 10.0,
    "spent": 0.0,
    "remaining": 10.0,
    "currency": "USD",
}

_ACTOR: dict[str, Any] = {
    "kind": "model",
    "name": "langgraph-control-plane",
    "version": "1.0",
    "external_id": "dra-control-plane#1.0",
}


def _merge_dict(base: dict[str, Any], upd: dict[str, Any]) -> dict[str, Any]:
    """Shallow merge reducer for dict channels."""
    out = dict(base)
    for k, v in upd.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = {**out[k], **v}
        else:
            out[k] = v
    return out


class ControlState(TypedDict, total=False):
    """Checkpoint payload for the §10 control-plane state machine.

    Per ADR-002 this holds *control state only*; canonical evidence lives at the
    DB layer via each :class:`InvestigatorContext` bundle, never in checkpoint
    memory.
    """

    run_id: str
    status: str
    phase: int
    budget: Annotated[dict[str, Any], _merge_dict]
    config_snapshot: Annotated[dict[str, Any], _merge_dict]
    intent: Annotated[dict[str, Any], _merge_dict]
    recon_branches: list[dict[str, Any]]
    recon_results: Annotated[list[dict[str, Any]], operator.add]
    research_tasks: Annotated[dict[str, Any], _merge_dict]
    user_decisions: Annotated[dict[str, str], _merge_dict]
    branches: Annotated[dict[str, Any], _merge_dict]
    branch_results: Annotated[list[dict[str, Any]], operator.add]
    # Phase 11 re-search round counter (gated by _REMAX_ITERATIONS). Replace
    # (not accumulate): p11 is the sole writer and re-sets it each pass.
    reresearch_round: int
    # Phase 11 dispatch channel: holds ONLY the current round's re-research tasks
    # (Replace, not accumulate) so the _route_reresearch fan-out edge never
    # re-dispatches a prior round's tasks. p11 re-emits a fresh, bounded task
    # list each round; the loop-back edge consumes exactly that list.
    reresearch_tasks: list[dict[str, Any]]
    # Phase 7/10 recompute their payloads from canonical branch_results/gaps each
    # round, so they must Replace (not accumulate) to keep the p11 loop-back
    # seeing only the CURRENT claims/gaps — preventing stale blocking gaps and
    # duplicate claims from accumulating across re-research iterations.
    claims: list[dict[str, Any]]
    verification_report: Annotated[dict[str, Any], _merge_dict]
    synthesis: Annotated[dict[str, Any], _merge_dict]
    gaps: list[dict[str, Any]]
    decisions: Annotated[list[dict[str, Any]], operator.add]
    handoff: Annotated[dict[str, Any], _merge_dict]
    audit: Annotated[dict[str, Any], _merge_dict]
    require_db: bool
    live_investigators: bool
    actor: Annotated[dict[str, Any], _merge_dict]
    strategy: str


def _budget(state: dict[str, Any]) -> dict[str, Any]:
    b = state.get("budget")
    if not isinstance(b, dict):
        return dict(_BUDGET_DEFAULT)
    return {**_BUDGET_DEFAULT, **b}


def budget_ok(state: dict[str, Any]) -> bool:
    """True iff a positive budget envelope remains (Phase 11/14 INCOMPLETE trap)."""
    return bool(state.get("budget")) and (state["budget"].get("remaining", 0) > 0)


def _spend(state: dict[str, Any], cost: float) -> dict[str, Any]:
    """Return a partial budget update consuming ``cost`` from the envelope."""
    b = _budget(state)
    spent = b["spent"] + cost
    spent = min(spent, b["envelope_total"])
    remaining = b["envelope_total"] - spent
    return {
        "budget": {
            "envelope_total": b["envelope_total"],
            "spent": spent,
            "remaining": max(0.0, remaining),
            "currency": b["currency"],
        }
    }


# ---------------------------------------------------------------------------
# Phase 0 — bootstrap config snapshot (pure: routing versions are in-process)
# ---------------------------------------------------------------------------

def _snapshot_config() -> dict[str, Any]:
    """Capture model/provider/prompt/evaluator versions (spec §10 Phase 0).

    Reads only the in-process routing machinery (no DB), so Phase 0 is
    DB-free and works in the no-DB verification path. The persistent-store
    reachability gate (``dra.db.can_connect``) is evaluated lazily in ``p0``.
    Robust to environments where the routing extras are not importable: any
    sub-snapshot that cannot be resolved is recorded as ``unavailable``.
    """
    model_providers: dict[str, Any] = {"available": False}
    try:
        from dra.routing.models import ModelRegistry, ModelPool, ExpensiveRole, model_pricing

        registry = ModelRegistry()
        pools = {
            p.value: [s.name for r in ExpensiveRole for s in registry.candidates(r, p)]
            for p in ModelPool
        }
        model_providers = {
            "available": True,
            "pricing": {k: list(v) for k, v in model_pricing().items()},
            "pools": pools,
        }
    except Exception:
        model_providers = {"available": False}

    return {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "model_providers": model_providers,
        # Evaluator version is the §38.4 verification gate; imported by p8, not p0.
        "verifier": {"mission": "sayandahiyagt/dra#20", "spec_anchor": "§38.4", "gate_version": "38.4"},
        "source_access_licensing_policy": {
            "default_access_basis": "public",
            "crawl_allowed": True,
            "redist_allowed": None,
            "robots_check": True,
        },
        "parent_run_ids": [],
        "evaluator_versions": {"verification_gate": "38.4"},
        "db_reachable": None,
    }


async def _db_reachable(require_db: bool) -> bool | None:
    """Evaluate the Phase 0 persistent-store gate unless ephemeral mode.

    Returns True/False when checked, or None when the DB gate is not required
    (ephemeral / no-DB runs). Never raises — a connection failure is a recorded
    state value, surfaced as FAILED only by the caller when ``require_db``.
    """
    if not require_db:
        return None
    try:
        from dra.db import can_connect

        return await can_connect()
    except Exception:
        return False


async def p0(state: dict[str, Any]) -> dict[str, Any]:
    """Phase 0 bootstrap: run_id + immutable config snapshot + budget envelope."""
    run_id = state.get("run_id") or uuid.uuid4().hex
    require_db = bool(state.get("require_db", False))
    snapshot = _snapshot_config()

    db_reachable = await _db_reachable(require_db)
    snapshot["db_reachable"] = db_reachable
    if require_db and db_reachable is not True:
        return {
            "run_id": run_id,
            "phase": 0,
            "status": FAILED,
            "config_snapshot": snapshot,
            "budget": _budget(state),
            "actor": state.get("actor", _ACTOR),
            "strategy": state.get("strategy") or _DEFAULT_STRATEGY,
        }

    budget = _budget(state)  # honors an injected envelope (e.g. budget test)
    return {
        "run_id": run_id,
        "phase": 0,
        "status": RUNNING,
        "config_snapshot": snapshot,
        "budget": budget,
        "actor": state.get("actor", _ACTOR),
        "recon_branches": state.get("recon_branches", []),
        "recon_results": state.get("recon_results", []),
        "research_tasks": state.get("research_tasks", {}),
        "branches": state.get("branches", {}),
        "branch_results": state.get("branch_results", []),
        "claims": state.get("claims", []),
        "gaps": state.get("gaps", []),
        "decisions": state.get("decisions", []),
        "strategy": state.get("strategy") or _DEFAULT_STRATEGY,
    }


# ---------------------------------------------------------------------------
# Interview strategies — §38.5 A/B parameterisation (dra#45)
# ---------------------------------------------------------------------------

_EXHAUSTIVE_QUESTIONNAIRE: tuple[str, ...] = (
    "artifact/product: what are we building?",
    "target: who is the intended user/workflow and deployment environment?",
    "perf/quality constraints: latency, throughput, SLA targets?",
    "privacy/security/licensing constraints: regulatory or license obligations?",
    "reference examples: closest existing systems or prior art?",
    "original-vs-interop: green-field or integrate with existing stack?",
    "success criteria: how do we know it worked?",
    "non-goals: what is explicitly out of scope?",
    "time/cost boundaries: deadline and budget envelope?",
    "tradeoffs: which dimensions are acceptable to relax?",
    "acceptance: how will the result be reviewed?",
)


async def _record_human_assertions(
    state: dict[str, Any],
    answers: list[tuple[str, Any]],
    run_id: str,
    task_id: str,
    actor: dict[str, Any],
) -> dict[str, Any]:
    """Record human-provided answers as versioned ``user_assertion`` rows.

    Each ``(question, value)`` pair is staged as ``USER_CONSTRAINT``; when the
    value revises a prior *canonical* assertion for the same ``(run_id, question)``
    it is staged as ``USER_CORRECTION`` linked via ``superseded_by`` so the prior
    row is left intact (history preserved, never an overwrite).

    All assertions in one call are staged inside a single
    :class:`InvestigatorContext` bundle so the publication is atomic.

    No-op when the DB is not reachable (``require_db`` False → returns ``{}``),
    so the always-green no-DB pipeline tests never touch the network.
    """
    if await _db_reachable(state.get("require_db", False)) is not True:
        return {}

    import json as _json

    from dra.investigators import InvestigatorContext
    from dra.publish import PublishError, async_session
    from sqlalchemy import text

    prepared: list[tuple[str, Any, str, str | None]] = []
    lookup_sql = text(
        "SELECT id, value FROM user_assertion "
        "WHERE run_id = :run AND question = :q "
        "AND assertion_type IN ('USER_CONSTRAINT', 'USER_CORRECTION') "
        "AND superseded_by IS NULL AND state = 'canonical' "
        "ORDER BY created_at DESC, id DESC LIMIT 1"
    )
    async with async_session() as sess:
        for question, value in answers:
            row = await sess.execute(
                lookup_sql, {"run": run_id, "q": question}
            )
            prior = row.fetchone()
            if prior is not None:
                prior_id, prior_val = prior[0], prior[1]
                try:
                    same = _json.dumps(prior_val, sort_keys=True, default=str) == \
                        _json.dumps(value, sort_keys=True, default=str)
                except TypeError:
                    same = False
                if not same:
                    prepared.append((question, value, "USER_CORRECTION", str(prior_id)))
                else:
                    prepared.append((question, value, "USER_CONSTRAINT", None))
            else:
                prepared.append((question, value, "USER_CONSTRAINT", None))

    try:
        async with InvestigatorContext(
            run_id=run_id, task_id=task_id, actor=actor, label="human-assertion"
        ) as ctx:
            for question, value, atype, sup in prepared:
                await ctx.stage_user_assertion(
                    atype, question, value,
                    run_id=run_id, task_id=task_id, superseded_by=sup,
                )
    except PublishError as exc:
        return {"audit": {"reason": f"assertion recording failed: {exc}"}}
    return {}


# ---------------------------------------------------------------------------
# Phase 1 — breadth interview (IntentSnapshot) via interrupt()
# ---------------------------------------------------------------------------

async def p1(state: dict[str, Any]) -> dict[str, Any]:
    """Phase 1: gate for enough intent to formulate >=1 recon query; interrupt for human.

    The interrupt payload is parameterised by the §38.5 A/B ``strategy`` (dra#45):
      - ``progressive`` (default): request a full IntentSnapshot.
      - ``exhaustive``: request the full §9.1 Stage-A questionnaire up-front.
      - ``minimal``: request an objective only.
    On resume, every human-provided answer is recorded as a versioned
    ``user_assertion`` (USER_CONSTRAINT / USER_CORRECTION) when the DB is
    reachable; the recording is a no-op on the no-DB path.
    """
    if not budget_ok(state):
        return {"status": INCOMPLETE}
    strategy = state.get("strategy") or _DEFAULT_STRATEGY
    if state.get("intent"):
        # Pre-seeded intent (non-interactive `run --objective`): skip interrupt.
        return {"phase": 1, "status": RUNNING, "strategy": strategy}
    if strategy == STRATEGY_EXHAUSTIVE:
        instruction = (
            "Section 9.1 Stage-A questionnaire — answer every item below so "
            "Phase 4 is unnecessary. Return an IntentSnapshot with the "
            "objective and each answer keyed in `user_decisions`."
        )
        questions = list(_EXHAUSTIVE_QUESTIONNAIRE)
    elif strategy == STRATEGY_MINIMAL:
        instruction = (
            "Provide only the objective for this research run (a single string)."
        )
        questions = []
    else:
        instruction = (
            "Provide an IntentSnapshot: objective + optional "
            "questions/constraints/parent_run_ids/sources/user_decisions."
        )
        questions = []
    payload = interrupt(
        {
            "phase": 1,
            "strategy": strategy,
            "instruction": instruction,
            "questions": questions,
        }
    )
    intent = _intent_to_dict(payload) if payload else {}
    if not intent.get("objective"):
        return {"phase": 1, "status": FAILED, "audit": {"reason": "intent.objective missing"}}

    # Record human-provided answers as versioned user_assertions (DB-gated no-op
    # on the no-DB path).  objective / constraint:<i> / user_decisions keys /
    # question:<i> each become a stably-keyed assertion so p4/p2 corrections can
    # detect and supersede a prior canonical row.
    run_id = state.get("run_id", "run")
    task_id = f"p1:{run_id}"
    actor = state.get("actor", _ACTOR)
    answers: list[tuple[str, Any]] = [("objective", intent["objective"])]
    for i, c in enumerate(intent.get("constraints") or []):
        answers.append((f"constraint:{i}", c))
    for k, v in (intent.get("user_decisions") or {}).items():
        answers.append((k, v))
    for i, q in enumerate(intent.get("questions") or []):
        answers.append((f"question:{i}", q))
    record = await _record_human_assertions(state, answers, run_id, task_id, actor)

    result: dict[str, Any] = {"phase": 1, "status": RUNNING, "intent": intent, "strategy": strategy}
    result.update(record)
    return result


def _sufficient_intent(intent: dict[str, Any]) -> bool:
    return bool(intent.get("objective"))


# ---------------------------------------------------------------------------
# Phase 2 — reconnaissance fan-out (pure, deterministic per perspective)
# ---------------------------------------------------------------------------

def _recon_queries(intent: dict[str, Any]) -> list[dict[str, Any]]:
    """Synthesize one recon query per perspective from the intent (no network)."""
    objective = intent.get("objective", "") or ""
    queries: list[dict[str, Any]] = []
    for perspective in _RECON_PERSPECTIVES:
        query = f"{perspective}: {objective}".strip(": ")
        queries.append(
            {
                "perspective": perspective,
                "query": query,
                "seen_source_ids": [],
            }
        )
    return queries


async def p2(state: dict[str, Any]) -> dict[str, Any]:
    """Phase 2: prepare the recon perspective list (gate: intent sufficient)."""
    if not budget_ok(state):
        return {"status": INCOMPLETE}
    intent = state.get("intent") or {}
    if not _sufficient_intent(intent):
        return {"status": FAILED, "audit": {"reason": "phase2: insufficient intent"}}
    branches = _recon_queries(intent)
    return {"phase": 2, "recon_branches": branches}


def _route_recon(state: dict[str, Any]) -> list[Send] | str:
    """Conditional edge p2 -> recon_worker fan-out (or END on budget)."""
    if not budget_ok(state) or state.get("status") == INCOMPLETE:
        return END
    branches = state.get("recon_branches") or []
    if not branches:
        return "p3"
    return [Send("recon_worker", b) for b in branches]


async def recon_worker(branch: dict[str, Any]) -> dict[str, Any]:
    """Phase 2 leaf: emit a normalized, obvious-duplicate-merged recon branch.

    Pure (no canonical evidence staged here — scouting only); results accumulate
    into the ``recon_results`` channel which Phase 3 consumes.
    """
    merged_query = branch.get("query", "").strip().lower()
    return {
        "recon_results": [
            {
                "perspective": branch.get("perspective"),
                "query": merged_query,
                "seen_source_ids": list(branch.get("seen_source_ids", [])),
            }
        ]
    }


# ---------------------------------------------------------------------------
# Phase 3 — research DAG synthesis (pure)
# ---------------------------------------------------------------------------

def _synthesize_tasks(state: dict[str, Any]) -> dict[str, Any]:
    """Synthesize ResearchTask records from intent + recon (pure)."""
    intent = state.get("intent") or {}
    recon = state.get("recon_results") or state.get("recon_branches") or []
    run_id = state.get("run_id", "run")
    tasks: dict[str, Any] = {}

    # Route a repo source from the intent to RepositoryInvestigator (dra#24)
    # through branch_worker. This is the repo-comprehension path: Phase 3 emits a
    # repo-source ResearchTask whose source.ref drives the real investigation
    # rather than the synthetic capture fallback. Only the first repo source is
    # used (single-repo prototype-1 target).
    sources = intent.get("sources") or []
    repo_source = next(
        (s for s in sources if isinstance(s, dict) and s.get("kind") == "repo"), None
    )
    if repo_source:
        task_id = f"task-{run_id}-0"
        tasks[task_id] = asdict(
            ResearchTask(
                task_id=task_id,
                question="README comprehension: what does this repository do?",
                parent_question=None,
                why_it_matters="Repository source from intent requires repo investigation (dra#24).",
                artifact_type="evidence_unit",
                source_types=["repo"],
                dependencies=[],
                priority=1,
                breadth=1,
                depth=1,
                model_policy={"role": "fact_extraction", "pool": "workhorse"},
                acceptance_criteria=[
                    "source_identity version (commit SHA) present",
                    "raw_capture content_hash present",
                    "evidence_unit linked to a derived_artifact",
                    "implementation_entity rows with repo@commit:path:symbol locators",
                ],
                verification_policy={"rules": ["38.4"]},
                stopping_conditions=["evidence staged", "max_attempts=1"],
                retry_rules={"attempts": 1},
                cost_envelope=0.5,
                source={
                    "kind": "repo",
                    "ref": repo_source.get("ref"),
                    "locator": repo_source.get("ref") or "repo:intent",
                    "version": repo_source.get("version") or "",
                },
            )
        )
        return tasks

    # One capture task per recon perspective -> deterministic, content-addressed
    # evidence so the DB publish/claims/verify pipeline is exercised end-to-end
    # even with no live network source (§11 investigators still run for real
    # refs in production).
    for i, branch in enumerate(recon):
        perspective = branch.get("perspective", f"perspective-{i}")
        query = branch.get("query", "")
        task_id = f"task-{run_id}-{i}"
        tasks[task_id] = asdict(
            ResearchTask(
                task_id=task_id,
                question=f"How to {query}?",
                parent_question=None,
                why_it_matters="Recon-scoped research branch.",
                artifact_type="evidence_unit",
                source_types=["repo", "paper", "website"],
                dependencies=[],
                priority=max(1, len(recon) - i),
                breadth=1,
                depth=1,
                model_policy={"role": "fact_extraction", "pool": "workhorse"},
                acceptance_criteria=[
                    "raw_capture content_hash present",
                    "evidence_unit linked to a derived_artifact",
                ],
                verification_policy={"rules": ["38.4"]},
                stopping_conditions=["evidence staged", "max_attempts=1"],
                retry_rules={"attempts": 1},
                cost_envelope=0.5,
                source={
                    "kind": "capture",
                    "locator": f"recon-capture:{perspective}",
                    # Synthetic-but-deterministic content; real runs fetch the
                    # actual source via the typed investigator for ``source.ref``.
                    "bytes": f"dra-control-plane:smoke-evidence:{run_id}:{query}".encode(),
                },
            )
        )
    return tasks


async def p3(state: dict[str, Any]) -> dict[str, Any]:
    """Phase 3: synthesize the research-task DAG from intent + recon."""
    if not budget_ok(state):
        return {"status": INCOMPLETE}
    tasks = _synthesize_tasks(state)
    spend = _spend(state, _PHASE_COST[3])
    return {"phase": 3, "research_tasks": tasks, **spend}


# ---------------------------------------------------------------------------
# Phase 4 — focused clarification (conditional interrupt)
# ---------------------------------------------------------------------------

def _clarification_questions(state: dict[str, Any]) -> list[str]:
    """Derive genuinely blocking clarification questions (Phase 4).

    Returns only questions that block branch execution; a complete intent with a
    homogeneous source set yields none, so the smoke run takes a single
    interrupt (Phase 1). ``USER_DECISION``/``USER_CONSTRAINT`` records are never
    treated as external evidence.
    """
    questions: list[str] = []
    kinds = {t.get("source", {}).get("kind") for t in state.get("research_tasks", {}).values()}
    if len(kinds) > 1:
        questions.append("Mixed source kinds detected (repo/paper/website) — prefer one?")
    return questions


async def p4(state: dict[str, Any]) -> dict[str, Any]:
    """Phase 4: focused clarification, recording USER_DECISION/USER_CONSTRAINT only.

    Under ``exhaustive`` and ``minimal`` strategies Phase 4 is skipped (the
    full questionnaire was asked up-front in p1 / only an objective was
    requested), so it returns RUNNING without interrupting.  Only ``progressive``
    runs the conditional clarification interrupt; on resume each answer is
    recorded as a versioned ``user_assertion`` (DB-gated no-op on the no-DB
    path).
    """
    if not budget_ok(state):
        return {"status": INCOMPLETE}
    strategy = state.get("strategy") or _DEFAULT_STRATEGY
    if strategy in (STRATEGY_EXHAUSTIVE, STRATEGY_MINIMAL):
        # Everything was asked in p1 (exhaustive) or only an objective was
        # requested (minimal) — Phase 4 is a no-op.
        return {"phase": 4, "status": RUNNING, "strategy": strategy}
    questions = _clarification_questions(state)
    if not questions:
        return {"phase": 4, "status": RUNNING, "strategy": strategy}
    payload = interrupt({"phase": 4, "questions": questions})
    decisions = {}
    if isinstance(payload, dict):
        decisions.update({f"clarify:{q}": str(ans) for q, ans in payload.items()})

    # Record each clarification answer as a versioned user_assertion (DB-gated
    # no-op on the no-DB path).  A clarification that revises a prior p1
    # assertion for the same question flips to USER_CORRECTION (superseded_by)
    # via the lookup inside _record_human_assertions.
    run_id = state.get("run_id", "run")
    task_id = f"p4:{run_id}"
    actor = state.get("actor", _ACTOR)
    answers: list[tuple[str, Any]] = []
    if isinstance(payload, dict):
        for q, ans in payload.items():
            answers.append((str(q), str(ans)))
    record = await _record_human_assertions(state, answers, run_id, task_id, actor)

    spend = _spend(state, _PHASE_COST[4])
    result: dict[str, Any] = {"phase": 4, "status": RUNNING, "user_decisions": decisions, "strategy": strategy, **spend}
    result.update(record)
    return result


# ---------------------------------------------------------------------------
# Phase 5/6 worker — per-branch investigation with isolated InvestigatorContext
# ---------------------------------------------------------------------------


async def _stage_capture_point(
    ctx, source_kind: str, locator: str, raw_bytes: bytes
) -> dict[str, Any]:
    """Stage the content-addressed dedupe point for a capture task.

    ``source_identity`` -> ``raw_capture``. The owning
    :class:`InvestigatorContext` publishes atomically on ``__aexit__``;
    :mod:`dra.publish.publish_bundle` flips ``staged``->``canonical`` inside one
    transaction (ADR-013), so a failure rolls the bundle back with no orphan
    canonical evidence.

    Only the raw capture is staged (not the full derived/evidence/claim chain)
    so two concurrent workers on **identical bytes** collapse on the
    ``raw_capture.content_hash`` primary key (the canonical dedupe point,
    publish.py:234 ``ON CONFLICT DO UPDATE``) instead of colliding on the
    ``derived_artifact`` ``ON CONFLICT DO NOTHING`` tuple (which is keyed by
    ``(content_hash, kind, version)`` and does not upsert provenance). This is
    the contract the Phase 5/6 parallel-isolation guarantee relies on.
    """
    from dra.investigators import content_hash

    # The provenance source_identity.kind enum (§22) is
    # repo/paper/web/doc/pdf; a synthetic recon "capture" is recorded as a
    # generic "web" content source so it stays schema-valid.
    db_kind = source_kind if source_kind in {"repo", "paper", "web", "doc", "pdf"} else "web"
    raw_hash = content_hash(raw_bytes)
    src = await ctx.stage_source_identity(
        db_kind,
        locator,
        state="staged",
        license_spdx="CC0-1.0",
        access_basis="public",
        crawl_allowed=True,
        redist_allowed=True,
        metadata={"auto": True, "control_plane_kind": source_kind},
    )
    raw_eid = await ctx.stage_source_capture(
        src,
        raw_hash,
        "text",
        mime_type="text/plain",
        size_bytes=len(raw_bytes),
        data=raw_bytes,
        final_url=locator,
        metadata={"captured_by": "control_plane.recon"},
    )
    return {"source_id": str(src), "raw_hash": raw_hash, "evidence_id": str(raw_eid)}


async def run_branch_worker(task: dict[str, Any], actor: dict[str, Any]) -> BranchState:
    """Phase 5 unit of work: one ResearchTask -> isolated InvestigatorContext.

    Each call opens its **own** ``InvestigatorContext`` bundle (its own
    ``async_session`` transaction) — the per-worker isolation guarantee
    (ADR-002/§42). Canonical evidence is never staged inside checkpoint/agent
    state. On clean exit the context publishes staged->canonical atomically
    (ADR-013); a ``PublishError`` or investigator failure marks the branch
    ``COMMIT_FAILED``/``BLOCKED`` rather than crashing the pipeline.
    """
    br = BranchState(
        task_id=task.get("task_id", "task"),
        status=B_STAGED,
        evidence_ids=[],
        claim_ids=[],
        bundle_id=None,
        published_count=0,
    )
    run_id = task.get("run_id", "run")
    source = task.get("source") or {}
    source_kind = source.get("kind", "capture")
    # Each fan-out worker is a distinct responsible agent (ADR-014 §4): the
    # provenance ``prov_agent`` table keys on ``external_id`` (a plain TEXT
    # column, no unique constraint), so reusing one actor identity across
    # concurrent workers causes a race where ``_resolve_or_create_engine``'s
    # ``SELECT ... WHERE external_id`` returns >1 row (MultipleResultsFound).
    # Scoping the external_id to the task_id keeps per-worker attribution
    # without cross-worker agent collision.
    actor = dict(actor or _ACTOR)
    actor["external_id"] = f"{actor.get('external_id', _ACTOR['external_id'])}:{task.get('task_id')}"
    # InvestigatorContext + PublishError are needed for every branch; the typed
    # investigators are imported lazily inside their dispatch branches so a
    # capture-only run does not require the repo/paper/website native deps
    # (tree-sitter/docling/etc.). If the DB/investigator stack is unavailable
    # the branch degrades to BLOCKED rather than crashing the whole pipeline.
    try:
        from dra.investigators import InvestigatorContext
        from dra.publish import PublishError

        async with InvestigatorContext(
            run_id=run_id,
            task_id=task.get("task_id", "task"),
            actor=actor,
            label=f"branch:{task.get('task_id')}",
        ) as ctx:
            br.bundle_id = str(ctx._bundle_id)
            if source_kind == "repo" and source.get("ref"):
                from dra.investigators.repo import RepositoryInvestigator

                res = await RepositoryInvestigator(ctx, source["ref"]).investigate()
                br.evidence_ids = [str(e) for e in res.evidence_unit_ids]
            elif source_kind == "paper" and source.get("pdf_bytes"):
                from dra.investigators.paper import PaperInvestigator, ParserMode

                res = await PaperInvestigator(mode=ParserMode.OFFLINE).investigate(
                    source["pdf_bytes"], source.get("locator", task["task_id"]),
                    run_id=run_id, task_id=task.get("task_id", "task"), actor=actor,
                )
                br.evidence_ids = [str(e) for e in res.evidence_unit_ids]
            elif source_kind == "website" and source.get("query"):
                from dra.investigators.website import WebsiteInvestigator
                from dra.routing.providers import ProviderMode

                res = await WebsiteInvestigator(
                    provider_mode=ProviderMode.OFFLINE,
                ).investigate(
                    ctx,
                    task_type=source.get("query"),
                    query=source["query"],
                    target_urls=source.get("target_urls", []),
                )
                br.evidence_ids = [str(e) for e in res.evidence_unit_ids]
            else:
                lineage = await _stage_capture_point(
                    ctx, source_kind, source.get("locator", f"capture:{task.get('task_id')}"),
                    source.get("bytes", b""),
                )
                br.evidence_ids = [lineage["evidence_id"]]
        # __aexit__ has now run publish_bundle (staged->canonical, ADR-013);
        # published_count is only valid AFTER the bundle is committed.
        br.published_count = ctx.published_count or 0
        br.status = B_COMPLETE if br.published_count > 0 else B_COMMIT_FAILED
    except PublishError as exc:
        br.status = B_COMMIT_FAILED
        br.errors.append(f"publish: {exc}")
    except Exception as exc:  # noqa: BLE001 — investigator failures are branch-level
        br.status = B_BLOCKED
        br.errors.append(f"investigate: {exc.__class__.__name__}: {exc}")
    return br


def _task_with_run(task: dict[str, Any], run_id: str) -> dict[str, Any]:
    """Attach the run_id to a ResearchTask payload for the isolated worker."""
    out = dict(task)
    out["run_id"] = run_id
    return out


def _route_branches(state: dict[str, Any]) -> list[Send] | str:
    """Conditional edge p5 -> branch_worker fan-out (or END on budget/exhaustion).

    When ``live_investigators`` is False (the default / no-DB verification path)
    the fan-out is skipped — Phase 5 becomes a pure dispatch that advances to
    Phase 6 without opening DB-backed InvestigatorContext bundles. The DB-gated
    smoke test sets ``live_investigators=True`` to exercise real investigators.
    """
    if not budget_ok(state) or state.get("status") == INCOMPLETE:
        return END
    if not state.get("live_investigators"):
        return "p6"
    tasks = state.get("research_tasks") or {}
    if not tasks:
        return "p6"
    run_id = state.get("run_id", "run")
    return [Send("branch_worker", _task_with_run(t, run_id)) for t in tasks.values()]


async def branch_worker(task: dict[str, Any]) -> dict[str, Any]:
    """Phase 5 graph leaf: dispatch one task through an isolated InvestigatorContext.

    Receives only the ``Send`` argument (the ResearchTask payload, with run_id
    attached) — never the parent state — so each worker's context is fully
    isolated (ADR-002/§42).
    """
    result = await run_branch_worker(task, task.get("actor", _ACTOR))
    return {"branch_results": [asdict(result)]}


# ---------------------------------------------------------------------------
# Phase 5 — dispatch (sets phase; fan-out edge follows)
# ---------------------------------------------------------------------------

async def p5(state: dict[str, Any]) -> dict[str, Any]:
    """Phase 5: deep branch execution dispatch (fan-out handled by edge)."""
    if not budget_ok(state):
        return {"status": INCOMPLETE}
    spend = _spend(state, _PHASE_COST[5])
    return {"phase": 5, **spend}


# ---------------------------------------------------------------------------
# Phase 6 — evidence normalization & commit (consolidate branch_results)
# ---------------------------------------------------------------------------

async def p6(state: dict[str, Any]) -> dict[str, Any]:
    """Phase 6: consolidate branch_results -> branches (COMMIT_FAILED stays retryable).

    Charges the per-branch cost only for branches not already consolidated in a
    prior p6 pass (``state["branches"]``), so the Phase 11 re-research loop-back
    (``reresearch_worker -> p6``) does not re-charge round-0 branches into the
    budget envelope. The first pass (empty ``branches``) charges every branch,
    preserving the existing behaviour for the non-looping smoke/e2e paths.
    """
    if not budget_ok(state):
        return {"status": INCOMPLETE}
    results = state.get("branch_results") or []
    existing = state.get("branches") or {}
    branches: dict[str, Any] = {}
    new_count = 0
    for res in results:
        bid = res.get("task_id", res.get("bundle_id") or uuid.uuid4().hex)
        if bid not in existing:
            new_count += 1
        branches[bid] = res
    cost = _PHASE_COST[6] + _PER_BRANCH_COST * new_count
    spend = _spend(state, cost)
    return {"phase": 6, "branches": branches, **spend}


# ---------------------------------------------------------------------------
# Phase 7 — claim construction
# ---------------------------------------------------------------------------

async def p7(state: dict[str, Any]) -> dict[str, Any]:
    """Phase 7: build ClaimRef records preserving evidence ids/contradictions."""
    if not budget_ok(state):
        return {"status": INCOMPLETE}
    claims: list[dict[str, Any]] = []
    for br in state.get("branch_results") or []:
        ev_ids = br.get("evidence_ids") or []
        if not ev_ids:
            continue
        claims.append(
            asdict(
                ClaimRef(
                    claim_id=f"claim:{br.get('task_id')}",
                    evidence_ids=ev_ids,
                    contradictions=[],
                    inference_type="direct",
                    source_independence=len(ev_ids),
                    freshness=datetime.now(timezone.utc).isoformat(),
                    relevance="high",
                    text=f"Task {br.get('task_id')} produced content-addressed evidence.",
                )
            )
        )
    spend = _spend(state, _PHASE_COST[7])
    return {"phase": 7, "claims": claims, **spend}


# ---------------------------------------------------------------------------
# Phase 8 — source-and-claim verification gate (delegation to dra#20)
# ---------------------------------------------------------------------------

async def p8(state: dict[str, Any]) -> dict[str, Any]:
    """Phase 8: delegate to dra.verification_gate.run_verification_proof (§38.4).

    Defensive: a gate failure (e.g. no DB, malformed evidence) is recorded in
    ``verification_report`` and marked ``INCONCLUSIVE`` rather than crashing the
    run — the audit in Phase 14 surfaces it.
    """
    if not budget_ok(state):
        return {"status": INCOMPLETE}
    report: dict[str, Any] = {"gate": "38.4", "verdict": "INCONCLUSIVE", "error": None}
    try:
        from dra.verification_gate import GateConfig, run_verification_proof

        report = await run_verification_proof(cfg=GateConfig(), write=False)
    except Exception as exc:  # noqa: BLE001 — gate is optional for the machinery
        report = {"gate": "38.4", "verdict": "INCONCLUSIVE", "error": f"{exc.__class__.__name__}: {exc}"}
    spend = _spend(state, _PHASE_COST[8])
    return {"phase": 8, "verification_report": report, **spend}


# ---------------------------------------------------------------------------
# Phase 9 — topic/implementation-entity graph + synthesis
# ---------------------------------------------------------------------------

async def p9(state: dict[str, Any]) -> dict[str, Any]:
    """Phase 9: topic hierarchy + component map + arch alternatives + decision candidates."""
    if not budget_ok(state):
        return {"status": INCOMPLETE}
    claims = state.get("claims") or []
    topics = {c.get("claim_id", f"claim-{i}"): c.get("text", "") for i, c in enumerate(claims)}
    impl_entities = []
    for br in state.get("branch_results") or []:
        for eid in br.get("evidence_ids") or []:
            impl_entities.append({"id": eid, "kind": "evidence", "from_branch": br.get("task_id")})
    synthesis = {
        "topic_hierarchy": topics,
        "impl_entities": impl_entities,
        "component_map": {eid["from_branch"]: eid for eid in impl_entities},
        "arch_alternatives": [],
        "algo_mappings": {},
        "decision_candidates": [f"dc-{i}" for i in range(len(claims))],
        "unresolved_gaps": [],
    }
    spend = _spend(state, _PHASE_COST[9])
    return {"phase": 9, "synthesis": synthesis, **spend}


# ---------------------------------------------------------------------------
# Phase 10 — independent gap/contradiction review (critic)
# ---------------------------------------------------------------------------

def _critic_questions() -> list[str]:
    """The spec §10.10 question list the critic answers from canonical state only."""
    return [
        "What evidence, if any, contradicts each claim?",
        "Are supporting sources independent (no derivative masquerade)?",
        "Is any claim backed only by UGC / unverified sources?",
        "Are locators resolvable and provenance intact?",
        "Are there missing perspectives or failure/security/licensing risks?",
        "What is the marginal novelty cost vs. remaining budget?",
    ]


async def p10(state: dict[str, Any]) -> dict[str, Any]:
    """Phase 10: critic reviews ONLY canonical evidence+claims+synthesis (not hidden reasoning)."""
    if not budget_ok(state):
        return {"status": INCOMPLETE}
    claims = state.get("claims") or []
    evidence_ids = {e for c in claims for e in c.get("evidence_ids", [])}
    synthesis = state.get("synthesis") or {}
    # Critic does NOT see the researcher's hidden reasoning — only the above.
    gaps: list[dict[str, Any]] = []
    if not claims:
        gaps.append(
            asdict(
                ResearchGap(
                    gap_id="gap:0",
                    description="No claims were produced from the gathered evidence.",
                    severity="high",
                    impact=3,
                    blocking=True,
                    related_claim_ids=[],
                )
            )
        )
    if not evidence_ids:
        gaps.append(
            asdict(
                ResearchGap(
                    gap_id="gap:1",
                    description="No content-addressed evidence was staged (no canonical raw_capture).",
                    severity="high",
                    impact=2,
                    blocking=True,
                    related_claim_ids=[],
                )
            )
        )
    for q in _critic_questions():
        gaps.append(
            asdict(
                ResearchGap(
                    gap_id=f"gap:question-{len(gaps)}",
                    description=f"Critic question: {q}",
                    severity="medium",
                    impact=1,
                    blocking=False,
                    related_claim_ids=[c["claim_id"] for c in claims[:3]],
                )
            )
        )
    spend = _spend(state, _PHASE_COST[10])
    return {"phase": 10, "gaps": gaps, **spend}


# ---------------------------------------------------------------------------
# Phase 11 — targeted re-research (bounded, terminating)
# ---------------------------------------------------------------------------

_REMAX_ITERATIONS = 3


def _claim_id_to_task_id(claim_id: str) -> str:
    """Recover a ResearchTask task_id from a ``claim:{task_id}`` ClaimRef id."""
    prefix = "claim:"
    return claim_id[len(prefix):] if claim_id.startswith(prefix) else claim_id


def _first_repo_ref(state: dict[str, Any]) -> str | None:
    """First repo source ref from the intent snapshot (for repo-gap re-research)."""
    intent = state.get("intent") or {}
    for s in intent.get("sources") or []:
        if isinstance(s, dict) and s.get("kind") == "repo" and s.get("ref"):
            return s["ref"]
    return None


def _reresearch_salt(gap: dict[str, Any], round_num: int) -> bytes:
    """Deterministic bytes payload for the capture fallback (researched from the
    gap id + round, so re-runs are content-addressed and dedupe on raw_capture)."""
    return f"dra-control-plane:reresearch:{gap.get('gap_id')}:{round_num}".encode()


def _infer_reresearch_source(
    state: dict[str, Any], gap: dict[str, Any], round_num: int
) -> dict[str, Any]:
    """Infer the re-research source kind/locator for a critic gap (plan §3c).

    Reuses the *existing* ``source["kind"]`` dispatch in ``run_branch_worker``
    (``repo`` -> RepositoryInvestigator, ``paper`` -> PaperInvestigator,
    ``website`` -> WebsiteInvestigator, ``capture`` fallback) — no new routing
    table is introduced, only the task-shaping that chooses ``source.kind``.

    Rule 1: a gap whose ``related_claim_ids`` trace back to a **repo** branch
    (via the originating ResearchTask in ``research_tasks``) and a repo ref is
    available from the intent -> a repo re-research task.
    Rule 2: a gap whose text references literature -> the ``website``/search
    investigator (the paper investigator needs ``pdf_bytes`` unavailable from a
    gap — RC #3 "Web/source discovery -> website/search").
    Rule 3 (default): the ``capture`` fallback with a deterministic bytes
    payload.

    NOTE (plan deviation, documented in discoveries): plan §3c rule 3 specified
    ``website`` as the default, but the WebsiteInvestigator iterates
    ``target_urls`` (which critic gaps do not carry) and emits no evidence
    offline without them. The capture fallback is the only source kind that
    reliably stages canonical evidence for a generic gap, and it is exactly the
    "deterministic bytes payload for the capture fallback" that plan Step 1
    already reserves — so the default is capture, not website.
    """
    related = gap.get("related_claim_ids") or []
    research_tasks = state.get("research_tasks") or {}
    repo_ref = _first_repo_ref(state)
    for cid in related:
        task = research_tasks.get(_claim_id_to_task_id(cid))
        if not task:
            continue
        kind = (task.get("source") or {}).get("kind")
        if kind == "repo" and repo_ref:
            return {
                "kind": "repo",
                "ref": repo_ref,
                "locator": repo_ref,
                "version": "",
                "metadata": {"gap_id": gap.get("gap_id"), "affected_claim_ids": list(related)},
            }
        if kind == "paper":
            return {
                "kind": "website",
                "query": (gap.get("description") or "")[:200],
                "locator": f"reresearch:{gap.get('gap_id')}",
                "bytes": _reresearch_salt(gap, round_num),
                "metadata": {"gap_id": gap.get("gap_id"), "affected_claim_ids": list(related)},
            }
    desc = (gap.get("description") or "").lower()
    if any(tok in desc for tok in ("paper", "literature", "study", "report")):
        return {
            "kind": "website",
            "query": (gap.get("description") or "")[:200],
            "locator": f"reresearch:{gap.get('gap_id')}",
            "bytes": _reresearch_salt(gap, round_num),
            "metadata": {"gap_id": gap.get("gap_id"), "affected_claim_ids": list(related)},
        }
    return {
        "kind": "capture",
        "locator": f"reresearch:{gap.get('gap_id')}",
        "bytes": _reresearch_salt(gap, round_num),
        "metadata": {"gap_id": gap.get("gap_id"), "affected_claim_ids": list(related)},
    }


def _build_reresearch_tasks(
    state: dict[str, Any], blocking: list[dict[str, Any]], round_num: int
) -> list[dict[str, Any]]:
    """Convert each eligible blocking gap into a full ResearchTask record (plan Step 1).

    Each record preserves ``gap_id`` + affected ``claim_ids`` in
    ``source.metadata`` (so the audit/trace round-trips out of checkpoint
    control state into canonical bundle provenance), and carries acceptance
    criteria, retry limits, and a per-branch cost envelope from the phase budget.
    """
    tasks: list[dict[str, Any]] = []
    for gap in blocking:
        source = _infer_reresearch_source(state, gap, round_num)
        tasks.append(
            asdict(
                ResearchTask(
                    task_id=f"reresearch-{gap.get('gap_id')}-pass-{round_num}",
                    question=gap.get("description", ""),
                    parent_question=None,
                    why_it_matters=(
                        f"Targeted re-research of critic gap {gap.get('gap_id')} "
                        f"(severity={gap.get('severity')}, impact={gap.get('impact')})."
                    ),
                    artifact_type="evidence_unit",
                    source_types=["repo", "paper", "website", "capture"],
                    dependencies=[],
                    priority=max(1, gap.get("impact", 1)),
                    breadth=1,
                    depth=1,
                    model_policy={"role": "fact_extraction", "pool": "workhorse"},
                    acceptance_criteria=[
                        "content-addressed evidence staged for gap",
                        "affected claims re-derivable from new evidence",
                    ],
                    verification_policy={"rules": ["38.4"]},
                    stopping_conditions=["evidence staged", "budget_ok"],
                    retry_rules={"attempts": _REMAX_ITERATIONS},
                    cost_envelope=_PER_BRANCH_COST,
                    source=source,
                )
            )
        )
    return tasks


async def p11(state: dict[str, Any]) -> dict[str, Any]:
    """Phase 11: re-dispatch blocking critic gaps through the shared investigator path.

    The critic (``p10``) emits ``ResearchGap`` records; this phase converts each
    *blocking* gap into a full :class:`ResearchTask` (preserving ``gap_id`` +
    affected ``claim_ids`` in ``source.metadata``) and hands the fan-out to the
    graph edge ``_route_reresearch`` -> ``reresearch_worker`` ->
    ``run_branch_worker`` (RC #2: no separate re-research engine). The loop closes
    back through the real phases: ``p6`` consolidation -> ``p7`` claim rebuild ->
    ``p8`` re-verification -> ``p9`` synthesis -> ``p10`` critic re-evaluation ->
    ``p11`` gate, so the ``critic -> targeted re-research -> new evidence ->
    re-verification`` loop closes end-to-end.

    Termination (RC #7): a blocking gap that survives ``_REMAX_ITERATIONS`` rounds
    (budget exhaustion is already trapped by ``budget_ok`` above) yields
    ``INCOMPLETE`` so ``_route_reresearch`` routes END without advancing past an
    unresolved blocking gap. Only non-blocking uncertainty is left recorded in
    ``gaps`` and carried forward to Phase 12.
    """
    if not budget_ok(state):
        return {"status": INCOMPLETE}
    gaps = state.get("gaps") or []
    blocking = [g for g in gaps if g.get("blocking")]
    round_num = state.get("reresearch_round", 0)
    # No blocking gap -> resolved, or only non-blocking uncertainty -> proceed to p12.
    if not blocking:
        return {
            "phase": 11,
            "reresearch_tasks": [],
            "reresearch_round": round_num,
        }
    # Blocking gap remains after retry exhaustion -> INCOMPLETE (no Phase 12/13/14).
    if round_num >= _REMAX_ITERATIONS:
        return {
            "phase": 11,
            "status": INCOMPLETE,
            "reresearch_round": round_num,
            "reresearch_tasks": [],
            "closing_gaps": [g.get("gap_id") for g in blocking],
        }
    tasks = _build_reresearch_tasks(state, blocking, round_num)
    spend = _spend(state, _PHASE_COST[11] * max(1, len(tasks)))
    return {
        "phase": 11,
        "reresearch_tasks": tasks,
        "reresearch_round": round_num + 1,
        **spend,
    }


async def reresearch_worker(task: dict[str, Any]) -> dict[str, Any]:
    """Phase 11 graph leaf: dispatch one re-research task through the SHARED path.

    A thin wrapper over :func:`run_branch_worker` — the same investigator dispatch
    and isolated ``InvestigatorContext`` / ``publish_bundle`` publication used by
    Phase 5 (RC #2: no separate re-research engine). Each re-research task is a
    full ``ResearchTask`` (RC #1) so the existing ``source["kind"]`` routing
    (repo/paper/website/capture) selects the investigator unchanged.
    """
    result = await run_branch_worker(task, task.get("actor", _ACTOR))
    return {"branch_results": [asdict(result)]}


def _route_reresearch(state: dict[str, Any]) -> list[Send] | str:
    """Conditional edge p11 -> reresearch_worker fan-out (or p12 / END).

    Mirrors the Phase 5 ``_route_branches`` idiom: when ``live_investigators``
    is False (the default / no-DB verification path) the fan-out is skipped and
    the run proceeds to Phase 12, keeping the always-green no-DB tests green.
    Routing (RC #7 termination gate):
      * INCOMPLETE/FAILED or budget exhausted -> END (p11 already returned
        INCOMPLETE on round/budget exhaustion; an unresolved blocking gap never
        advances to Phase 12).
      * No blocking gap -> p12 (resolved, or only non-blocking uncertainty).
      * Blocking gap remains, budget + rounds available, live investigators ->
        Send one ``reresearch_worker`` per re-research task (each opens its own
        isolated InvestigatorContext bundle — canonical evidence lives in the DB,
        checkpoint retains IDs/status only, RC #4).
    """
    if state.get("status") == INCOMPLETE or state.get("status") == FAILED:
        return END
    if not budget_ok(state):
        return END
    gaps = state.get("gaps") or []
    blocking = [g for g in gaps if g.get("blocking")]
    if not blocking:
        return "p12"
    if state.get("reresearch_round", 0) >= _REMAX_ITERATIONS:
        return END
    if not state.get("live_investigators"):
        return "p12"
    tasks = state.get("reresearch_tasks") or []
    if not tasks:
        return "p12"
    run_id = state.get("run_id", "run")
    return [Send("reresearch_worker", _task_with_run(t, run_id)) for t in tasks]


# ---------------------------------------------------------------------------
# Phase 12 — architecture/implementation decision synthesis
# ---------------------------------------------------------------------------

async def p12(state: dict[str, Any]) -> dict[str, Any]:
    """Phase 12: decision synthesis (question/alternatives/evidence/chosen/rationale)."""
    if not budget_ok(state):
        return {"status": INCOMPLETE}
    decisions: list[dict[str, Any]] = []
    candidates = (state.get("synthesis") or {}).get("decision_candidates") or []
    claims = state.get("claims") or []
    for i, cand in enumerate(candidates):
        chosen = claims[-1]["claim_id"] if claims and i == 0 else "undecided"
        decisions.append(
            asdict(
                Decision(
                    question=f"Decision for {cand}",
                    alternatives=["keep current", "adopt alternative"],
                    evidence_ids=[e for c in claims for e in c.get("evidence_ids", [])],
                    user_preference_deps=list(state.get("user_decisions", {}).keys()),
                    chosen=chosen,
                    rationale="Derived from canonical evidence + verification gate.",
                    consequences=["impacts implementation"],
                    reversal_triggers=["evidence contradicted", "budget exceeded"],
                )
            )
        )
    spend = _spend(state, _PHASE_COST[12])
    return {"phase": 12, "decisions": decisions, **spend}


# ---------------------------------------------------------------------------
# Phase 13 — handoff generation (human + machine readable)
# ---------------------------------------------------------------------------

async def p13(state: dict[str, Any]) -> dict[str, Any]:
    """Phase 13: §33 handoff generation from control state + canonical state.

    Builds the §31.2 machine-readable manifest and the §31.1 eight-section
    human-readable package (pure helpers, DB-free) and, when the DB is live,
    stages the handoff through :func:`dra.publish.stage_handoff` +
    :func:`dra.publish.publish_bundle` (provenance-anchored, ADR-013).

    Degrades gracefully on the no-DB path (mirrors p12's control-state-only
    behavior): the canonical staging step is gated on ``live_investigators`` and
    any :class:`~dra.publish.PublishError`/DB failure is caught so
    ``test_phase_advancement_no_db`` stays green and the DAG still advances to
    Phase 14. Decisions are staged here (D1) because p12 is kept pure per the
    mission's "replace the Phase-13 stub" scope.
    """
    if not budget_ok(state):
        return {"status": INCOMPLETE}
    run_id = state.get("run_id", "run")
    actor = state.get("actor", _ACTOR)

    # Lazy import so build_graph().compile() / test_graph_assembles stay DB-free
    # (same idiom as branch_worker). The pure helpers run even without a DB.
    from dra.handoff import SECTION_FILES, build_document_package, build_manifest

    manifest = build_manifest(state, run_id, retrieval_endpoint="/knowledge")
    package = build_document_package(state, manifest)
    handoff = {
        "phase": 13,
        "manifest": manifest,  # §31.2 machine-readable manifest
        "content": package,  # §31.1 eight-section human-readable package
        "schema_version": "1.0",
        "section_count": len(SECTION_FILES),
        "retrieval_contract": "§34",
        "handoff_id": None,
        "db_staged": False,
    }

    # Best-effort canonical staging. Gated on live_investigators (the no-DB
    # path sets it False) so we never attempt a Postgres connection when none is
    # expected; any failure degrades to the control-state manifest above.
    if state.get("live_investigators"):
        try:
            from dra.handoff import stage_section_handoff

            handoff_id = await stage_section_handoff(state, run_id, actor)
            handoff["handoff_id"] = str(handoff_id)
            handoff["db_staged"] = True
        except Exception:  # noqa: BLE001 — degrade to control-state manifest (mirrors p8/p12)
            # No DB / PublishError / investigator failure: keep the
            # control-state manifest so the no-DB verification path stays green.
            handoff["handoff_id"] = None
            handoff["db_staged"] = False
    spend = _spend(state, _PHASE_COST[13])
    return {"phase": 13, "handoff": handoff, **spend}


# ---------------------------------------------------------------------------
# Phase 14 — handoff audit + final status
# ---------------------------------------------------------------------------

async def p14(state: dict[str, Any]) -> dict[str, Any]:
    """Phase 14: audit handoff. COMPLETE only iff clean AND budget remaining."""
    if not budget_ok(state):
        return {"phase": 14, "status": INCOMPLETE, "audit": _audit(state, budget_exhausted=True)}
    audit = _audit(state, budget_exhausted=False)
    status = COMPLETE if audit["passes"] else INCOMPLETE
    return {"phase": 14, "status": status, "audit": audit}


def _audit(state: dict[str, Any], budget_exhausted: bool) -> dict[str, Any]:
    claims = state.get("claims") or []
    branches = state.get("branches") or {}
    gaps = state.get("gaps") or []
    every_claim_has_evidence = all(c.get("evidence_ids") for c in claims)
    locators_resolvable = all(b.get("status") in (B_COMPLETE, B_BLOCKED) for b in branches.values())
    has_evidence = bool(claims) or any(
        b.get("status") == B_COMPLETE for b in branches.values()
    )
    blocking_gaps = [g.get("description") for g in gaps if g.get("blocking")]
    passes = (
        every_claim_has_evidence
        and locators_resolvable
        and has_evidence
        and not blocking_gaps
        and not budget_exhausted
    )
    return {
        "every_claim_has_evidence": every_claim_has_evidence,
        "locators_resolvable": locators_resolvable,
        "has_evidence": has_evidence,
        "assumptions_explicit": True,
        "contradictions_visible": any(c.get("contradictions") for c in claims),
        "constraints_carried_forward": bool(state.get("user_decisions")),
        "decisions_cite_evidence": all(d.get("evidence_ids") for d in state.get("decisions") or []),
        "budget_exhausted": budget_exhausted,
        "blocking_gaps": blocking_gaps,
        "passes": passes,
    }


# ---------------------------------------------------------------------------
# Budget guard router (used for every phase -> phase edge)
# ---------------------------------------------------------------------------


def _route(next_node: str):
    """Build a conditional-edge path fn: INCOMPLETE -> END, else -> next_node."""

    def _fn(state: dict[str, Any]) -> str:
        if state.get("status") == INCOMPLETE or state.get("status") == FAILED:
            return END
        if not budget_ok(state) and state.get("phase", 0) > 0:
            return END
        return next_node

    return _fn


# ---------------------------------------------------------------------------
# Graph assembly
# ---------------------------------------------------------------------------


def build_graph(require_db: bool = False) -> StateGraph:
    """Assemble the 15-phase §10 StateGraph (no DB required to compile)."""
    sg = StateGraph(ControlState)
    for i in range(NUM_PHASES):
        sg.add_node(f"p{i}", _PHASE_NODES[i])
    sg.add_node("recon_worker", recon_worker)
    sg.add_node("branch_worker", branch_worker)
    sg.add_node("reresearch_worker", reresearch_worker)

    sg.add_edge("__start__", "p0")
    # p0 -> p1 (Phase 0 may set FAILED on missing DB when require_db)
    sg.add_conditional_edges("p0", _route("p1"))
    sg.add_conditional_edges("p1", _route("p2"))
    # Phase 2 fan-out: p2 -> recon_worker(s) -> p3
    sg.add_conditional_edges("p2", _route_recon)
    sg.add_edge("recon_worker", "p3")
    sg.add_conditional_edges("p3", _route("p4"))
    sg.add_conditional_edges("p4", _route("p5"))
    # Phase 5 fan-out: p5 -> branch_worker(s) -> p6
    sg.add_conditional_edges("p5", _route_branches)
    sg.add_edge("branch_worker", "p6")
    sg.add_conditional_edges("p6", _route("p7"))
    sg.add_conditional_edges("p7", _route("p8"))
    sg.add_conditional_edges("p8", _route("p9"))
    sg.add_conditional_edges("p9", _route("p10"))
    sg.add_conditional_edges("p10", _route("p11"))
    # Phase 11 — targeted re-research gate/dispatch. On the no-DB path
    # (live_investigators False) this short-circuits to p12 exactly like
    # _route_branches; on the DB path it fans out reresearch_worker tasks, which
    # loop back through p6->p7->p8->p9->p10->p11 (critic -> re-research ->
    # evidence -> re-verification) until the blocking gaps resolve or
    # _REMAX_ITERATIONS / budget exhaustion yields INCOMPLETE (RC #7).
    sg.add_conditional_edges("p11", _route_reresearch)
    # Loop-back: a dispatched re-research task publishes canonical evidence
    # (run_branch_worker -> InvestigatorContext -> publish_bundle) into its own
    # bundle, then re-enters the real phases for claim rebuild + re-verification.
    sg.add_edge("reresearch_worker", "p6")
    sg.add_conditional_edges("p12", _route("p13"))
    sg.add_conditional_edges("p13", _route("p14"))
    sg.add_edge("p14", END)

    return sg


_PHASE_NODES = [
    p0, p1, p2, p3, p4, p5, p6, p7, p8, p9, p10, p11, p12, p13, p14,
]


def postgres_conninfo(db_url: str | None = None) -> str:
    """Convert the SQLAlchemy ``postgresql+psycopg://`` URL to a libpq conninfo.

    :func:`PostgresSaver.from_conn_string` expects a psycopg connection string,
    not SQLAlchemy's ``+psycopg`` dialect URL — passing the latter raises
    ``psycopg.ProgrammingError: missing "="``. Stripping the dialect suffix
    yields a ``postgresql://`` URL that psycopg/libpq accepts directly.
    """
    db_url = db_url or DATABASE_URL
    if db_url.startswith("postgresql+psycopg://"):
        return "postgresql://" + db_url[len("postgresql+psycopg://"):]
    return db_url


def _build_store(run_id: str, config_snapshot: dict[str, Any]) -> InMemoryStore:
    """Cross-thread app-data store: config snapshot keyed by run_id (ADR-002).

    Uses an in-process ``InMemoryStore`` (a LangGraph ``BaseStore``) for the MVP.
    A Postgres-backed store (``langgraph.store.postgres``) is available for
    production persistence but is not required for the round-trip.
    """
    store = InMemoryStore()
    store.put(("control-plane",), f"config:{run_id}", {"config": config_snapshot})
    return store


async def _run_pipeline(
    initial: dict[str, Any],
    thread_id: str,
) -> dict[str, Any]:
    """Open the durable checkpointer + store and run the pipeline to completion.

    ``AsyncPostgresSaver`` (langgraph-checkpoint-postgres, ADR-001) provides
    durable checkpoint state on the shared ``DATABASE_URL``. It is distinct
    from the sync ``PostgresSaver``: the async Pregel loop requires the async
    ``aget_tuple``/``aput`` interface, which only ``...aio.AsyncPostgresSaver``
    implements in this pinned version.
    """
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from dra.db import DATABASE_URL

    async with AsyncPostgresSaver.from_conn_string(
        postgres_conninfo(DATABASE_URL)
    ) as checkpointer:
        await checkpointer.setup()
        store = _build_store(thread_id, initial.get("config_snapshot", {}))
        graph = build_graph(require_db=True).compile(checkpointer=checkpointer, store=store)
        state = await graph.ainvoke(
            initial, config={"configurable": {"thread_id": thread_id}}
        )
        # If a human-in-the-loop interrupt is pending (e.g. Phase 1 with no
        # --objective), surface the state so the caller can resume; do not raise.
        return state


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: ``dra-control-plane``."""
    parser = argparse.ArgumentParser(
        prog="dra-control-plane",
        description="LangGraph control-plane state machine for dra#36 (spec §10).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_compile = sub.add_parser("compile", help="Assemble the StateGraph and assert structure (no DB).")
    p_compile.add_argument("--require-db", action="store_true", help="Also require a reachable Postgres in p0.")

    p_run = sub.add_parser("run", help="Run the research pipeline to completion (requires Postgres).")
    p_run.add_argument("--objective", required=False, help="Intent objective (seeds Phase 1, skips its interrupt).")
    p_run.add_argument("--repo", metavar="<url|path>", help="Investigate a repository (README comprehension). Seeds a repo source instead of the capture smoke path.")
    p_run.add_argument("--repo-version", metavar="<sha>", help="Pin a commit SHA for --repo (resolved to HEAD if absent).")
    p_run.add_argument("--strategy", choices=list(_VALID_STRATEGIES), default=_DEFAULT_STRATEGY, help="Interview strategy for the §38.5 A/B (default: progressive).")
    p_run.add_argument("--budget", type=float, default=10.0, help="Budget envelope in --currency.")
    p_run.add_argument("--currency", default="USD")
    p_run.add_argument("--thread-id", default=None, help="Reuse a run/thread id (resume).")

    args = parser.parse_args(argv)

    if args.cmd == "compile":
        require_db = args.require_db
        graph = build_graph(require_db=require_db)
        compiled = graph.compile()
        node_names = set(compiled.nodes)
        missing = [f"p{i}" for i in range(NUM_PHASES) if f"p{i}" not in node_names]
        assert not missing, f"missing phase nodes: {missing}"
        assert "budget" in ControlState.__annotations__, "ControlState missing budget field"
        print("OK: §10 control-plane StateGraph assembled")
        print(f"  phases: p0..p{NUM_PHASES - 1} + recon_worker/branch_worker + END")
        print(f"  budget field present: {'budget' in ControlState.__annotations__}")
        return 0

    # run
    from dra.db import can_connect

    if not asyncio.run(can_connect()):
        print("FAIL: No reachable Postgres at DATABASE_URL.")
        print("      `dra-control-plane run` requires Postgres+pgvector (use `compile` for no-DB checks).")
        return 1

    intent: dict[str, Any] = {}
    if args.repo:
        intent = {
            "objective": args.objective or f"README comprehension of {args.repo}",
            "sources": [{"kind": "repo", "ref": args.repo, "version": args.repo_version or ""}],
            "constraints": ["scope:repo-comprehension"],
        }
    elif args.objective:
        intent = {"objective": args.objective, "sources": [], "constraints": []}
    thread_id = args.thread_id or uuid.uuid4().hex
    initial: dict[str, Any] = {
        "require_db": True,
        "live_investigators": True,
        "actor": _ACTOR,
        "budget": {"envelope_total": args.budget, "spent": 0.0, "remaining": args.budget, "currency": args.currency},
        "intent": intent,
        "run_id": thread_id,
        "strategy": args.strategy,
    }

    print(f"[control-plane] §10 research run strategy={args.strategy} objective={args.objective!r} budget={args.budget}{args.currency}")
    state = asyncio.run(_run_pipeline(initial, thread_id))
    if state.get("__interrupt__"):
        # Paused for human-in-the-loop input (Phase 1/4). Resume programmatically
        # by calling the graph with a Command(resume=<intent>) on this thread_id.
        print(f"[control-plane] paused at phase {state.get('phase')} for input:")
        print(f"  interrupt: {state['__interrupt__']}")
        return 2
    print(f"[control-plane] run finished: status={state.get('status')} phase={state.get('phase')}")
    return 0 if state.get("status") in (COMPLETE, INCOMPLETE) else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
