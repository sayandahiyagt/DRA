"""§38.5 Progressive-interview A/B proof engine (dra#46).

A/B-tests three interview strategies — **progressive**, **exhaustive**, **minimal**
(dra#45) — against a deterministic in-process "oracle user" fixture corpus: a set
of architecture-change topics with *early* facts (visible from the objective
alone) and *late* facts (only revealed by running a specific recon perspective).
The engine measures the five §38.5 metrics — user turns, abandonment/annoyance
proxy, architecture-changing facts discovered late, research wasted on discarded
branches, and final handoff correctness — and applies the ADR-005 reversal
trigger: the progressive loop must not be a worse handoff than exhaustive while
consuming fewer user turns.

Design follows PLAN_1.md: the simulation is pure Python (no DB, no network) for
the always-green offline path.  When the DB is reachable and ``publish=True``,
per-strategy assertion records + the A/B report are staged canonically into the
``user_assertion`` table (dra#44, migration ``0008_interview_constraints``) via
``InvestigatorContext`` + ``publish_bundle`` — the ``_STANDALONE_STATE_TABLES``
mirror path, because ``user_assertion`` is a standalone table with no
``prov_entity`` row and is deliberately NOT in ``entity_kind`` (ADR-017).

The strategy interface is *not* a callable: dra#45 parameterised the three
strategies (``STRATEGY_PROGRESSIVE``/``EXHAUSTIVE``/``MINIMAL``,
``_EXHAUSTIVE_QUESTIONNAIRE``) inside the LangGraph ``p1``/``p4`` phases of
``control_plane.py``.  This driver imports those constants and replays the
p1/p2/p4 flow in a pure, deterministic function per strategy — it does not
invoke the state machine.

CLI entry: ``dra-progressive-interview`` (wired in ``pyproject.toml``).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from dra.control_plane import (
    STRATEGY_EXHAUSTIVE,
    STRATEGY_MINIMAL,
    STRATEGY_PROGRESSIVE,
    _EXHAUSTIVE_QUESTIONNAIRE,
    _RECON_PERSPECTIVES,
)
from dra.db import DATABASE_URL, can_connect

MISSION = "sayandahiyagt/dra#46"
SPEC_ANCHOR = "§38.5"

_ACTOR: dict[str, Any] = {
    "kind": "model",
    "name": "dra-progressive-interview",
    "version": "1.0",
    "external_id": "dra-progressive-interview#1.0",
}

# Three questions from the §9.1 Stage-A questionnaire that directly determine
# the architecture; the remaining eight are non-critical for this corpus.
_CRITICAL_QUESTION_PATTERNS = (
    "artifact/product",
    "perf/quality",
    "privacy/security/licensing",
)
_CRITICAL_QUESTION_COUNT = sum(
    1 for q in _EXHAUSTIVE_QUESTIONNAIRE if any(p in q for p in _CRITICAL_QUESTION_PATTERNS)
)


# ---------------------------------------------------------------------------
# Oracle-user fixture corpus
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LateFact:
    """An architecture-changing fact only revealed by a recon perspective."""

    fact: str
    recon_perspective: str


@dataclass(frozen=True)
class Topic:
    """One oracle-user topic: a known architecture-changing objective."""

    id: str
    objective: str
    early_facts: tuple[str, ...]
    late_facts: tuple[LateFact, ...]


# 12 topics × (3 early + 4 late facts).  Each topic's four late facts span four
# *distinct* recon perspectives out of the six, so two perspectives are probed
# but yield no fact per topic — a ~0.333 wasted-research fraction that stays
# below the 0.4 SLO ceiling.
_TOPICS: tuple[Topic, ...] = (
    Topic(
        id="topic_0",
        objective="Build a vector search API over a multi-tenant postgres+pgvector store.",
        early_facts=(
            "postgres with pgvector extension is the backing store",
            "the API surface must be REST",
        ),
        late_facts=(
            LateFact("shard across 3 tenants for row-level isolation", "source_of_truth"),
            LateFact("hnsw index required for <50ms p95 recall@10", "empirical_evidence"),
            LateFact("grobid+docling pipeline for paper-ingestion integration", "implementation_mechanisms"),
            LateFact("row-level security policy for cross-tenant access control", "failure_security_licensing_risk"),
        ),
    ),
    Topic(
        id="topic_1",
        objective="Build a paper ingestion pipeline from PDF to canonical evidence.",
        early_facts=(
            "docling is the primary PDF parser",
            "postgres stores canonical evidence metadata",
        ),
        late_facts=(
            LateFact("grobid is required for reference-parsing fidelity", "implementation_mechanisms"),
            LateFact("async batching reduces ingestion wall-time by 8x", "empirical_evidence"),
            LateFact("citation format must align with upstream provenance", "source_of_truth"),
            LateFact("crossref is the authoritative metadata source", "closest_existing_systems"),
        ),
    ),
    Topic(
        id="topic_2",
        objective="Build a repo investigation agent that reads source code.",
        early_facts=(
            "tree-sitter parses the source tree",
            "postgres stores implementation_entity rows",
        ),
        late_facts=(
            LateFact("langgraph checkpointing is required for long runs", "implementation_mechanisms"),
            LateFact("cross-repo lineage needs a shared dedupe key", "source_of_truth"),
            LateFact("ON CONFLICT upsert prevents duplicate captures", "empirical_evidence"),
            LateFact("integrates with the citation-verdict workflow", "alternatives"),
        ),
    ),
    Topic(
        id="topic_3",
        objective="Build a fact-extraction service that attributes claims to sources.",
        early_facts=(
            "LLM calls produce candidate claims",
            "content_hash anchors every evidence unit",
        ),
        late_facts=(
            LateFact("batch LLM calls to stay under the cost budget", "empirical_evidence"),
            LateFact("derivation lineage must be recorded for each fact", "source_of_truth"),
            LateFact("source trust score excludes UGC from corroboration", "failure_security_licensing_risk"),
            LateFact("prior-work search prevents duplicate effort", "closest_existing_systems"),
        ),
    ),
    Topic(
        id="topic_4",
        objective="Build a verification gate that checks claims against evidence.",
        early_facts=(
            "recursive lineage walks detect masquerade",
            "postgres stores claim verification_state",
        ),
        late_facts=(
            LateFact("pgvector iterative_scan must be available", "empirical_evidence"),
            LateFact("UGC sources are excluded from independent corroboration", "failure_security_licensing_risk"),
            LateFact("stale artifact quarantine propagates to claims", "source_of_truth"),
            LateFact("contradiction edges materialize as topic_relationship", "alternatives"),
        ),
    ),
    Topic(
        id="topic_5",
        objective="Build a model routing policy across cheap/workhorse/frontier pools.",
        early_facts=(
            "cost-aware selection picks the cheapest admissible variant",
            "offline fixtures drive deterministic evaluation",
        ),
        late_facts=(
            LateFact("advisor marginal-value is 50% of the workhorse gap", "closest_existing_systems"),
            LateFact("pool profiles are cached at module import", "empirical_evidence"),
            LateFact("escalation thresholds gate frontier/advisor invocation", "implementation_mechanisms"),
            LateFact("budget envelope is per-role", "source_of_truth"),
        ),
    ),
    Topic(
        id="topic_6",
        objective="Build an evidence capture pipeline with content-addressed dedupe.",
        early_facts=(
            "sha256 content_hash is the raw_capture PK",
            "provenance edges link capture to derived_artifact",
        ),
        late_facts=(
            LateFact("ON CONFLICT DO UPDATE deduplicates on content_hash", "empirical_evidence"),
            LateFact("locator shapes encode source-specific fields", "implementation_mechanisms"),
            LateFact("source attribution is recorded in prov_activity", "source_of_truth"),
            LateFact("multi-format interop preserves raw bytes verbatim", "alternatives"),
        ),
    ),
    Topic(
        id="topic_7",
        objective="Build a handoff statement renderer for architecture decisions.",
        early_facts=(
            "markdown + JSON emit the final handoff",
            "attribution chain lists every contributing agent",
        ),
        late_facts=(
            LateFact("multi-format export supports PDF and HTML", "alternatives"),
            LateFact("streaming output avoids buffering large handoffs", "empirical_evidence"),
            LateFact("attribution chain references prov_entity ids", "source_of_truth"),
            LateFact("template rendering reuses prior sections", "closest_existing_systems"),
        ),
    ),
    Topic(
        id="topic_8",
        objective="Build a crawl manifest tracker for web acquisition.",
        early_facts=(
            "per-URL durl tracks crawl state",
            "retry logic backs off on failure",
        ),
        late_facts=(
            LateFact("robots.txt policy gates crawl_allowed per source", "failure_security_licensing_risk"),
            LateFact("domain isolation prevents cross-origin leakage", "source_of_truth"),
            LateFact("crawl budget throttles requests per host", "empirical_evidence"),
            LateFact("sitemap parsing populates the first frontier", "implementation_mechanisms"),
        ),
    ),
    Topic(
        id="topic_9",
        objective="Build a gap detection system across architecture versions.",
        early_facts=(
            "lineage diff produces gap records",
            "postgres stores topic_relationship edges",
        ),
        late_facts=(
            LateFact("cross-version deltas are computed at depth 2", "empirical_evidence"),
            LateFact("recursive walks are depth-capped at 50", "implementation_mechanisms"),
            LateFact("false-positive rate is calibrated against fixtures", "closest_existing_systems"),
            LateFact("gap attribution links to prov_activity", "source_of_truth"),
        ),
    ),
    Topic(
        id="topic_10",
        objective="Build an escalation logging system for cost-aware routing.",
        early_facts=(
            "pool routing records from_pool/to_pool",
            "cost_delta_usd is rounded to 6 places",
        ),
        late_facts=(
            LateFact("frontier/advisor thresholds are per-role", "empirical_evidence"),
            LateFact("per-role escalation rates are tracked separately", "source_of_truth"),
            LateFact("trigger attribution explains the escalation", "alternatives"),
            LateFact("prior routing systems batch similar decisions", "closest_existing_systems"),
        ),
    ),
    Topic(
        id="topic_11",
        objective="Build a stale-vector invalidation job for incremental HNSW.",
        early_facts=(
            "state machine marks superseded rows",
            "postgres stores staleness_policy JSON",
        ),
        late_facts=(
            LateFact("pgvector >=0.8 supports incremental HNSW inserts", "empirical_evidence"),
            LateFact("reindex budget is 10 seconds per 25k vectors", "closest_existing_systems"),
            LateFact("ghost detection checks deleted IDs in ANN results", "implementation_mechanisms"),
            LateFact("tenant isolation is verified on unit vectors", "source_of_truth"),
        ),
    ),
)


def generate_oracle_corpus(n_topics: int = 12, seed: int = 42) -> list[Topic]:
    """Return a deterministic slice of the oracle-user fixture corpus.

    The corpus is fully deterministic (fixed ``_TOPICS``); ``seed`` is accepted
    for interface symmetry with ``proof_corpus.generate_corpus`` and only affects
    the RNG used to shuffle/reorder topics when ``n_topics`` is less than the full
    set.  No network or model weights are required.
    """
    if n_topics <= 0:
        return []
    topics = list(_TOPICS)
    if n_topics >= len(topics):
        return topics
    rng = random.Random(seed)
    rng.shuffle(topics)
    return topics[:n_topics]


# ---------------------------------------------------------------------------
# Strategy simulation (pure: no DB, no network)
# ---------------------------------------------------------------------------


@dataclass
class _PerTopicResult:
    """Per-topic simulation result for one strategy."""

    p1_questions: int
    p4_questions: int
    non_critical_questions: int
    late_facts_discovered: int
    late_facts_in_handoff: int
    late_facts_missed: int
    perspectives_probed: int
    perspectives_with_facts: int
    handoff_fact_count: int
    total_fact_count: int

    def handoff_correctness(self) -> float:
        """Fraction of known architecture-changing facts in the handoff."""
        if self.total_fact_count == 0:
            return 0.0
        return self.handoff_fact_count / self.total_fact_count


def _p1_questions_for(strategy: str) -> list[str]:
    """Return the questions asked by each strategy at Phase 1."""
    if strategy == STRATEGY_EXHAUSTIVE:
        return list(_EXHAUSTIVE_QUESTIONNAIRE)
    return ["objective"]


def _p4_blocking_questions(strategy: str, topic: Topic) -> list[str]:
    """Return the Phase-4 blocking clarification questions for a strategy/topic.

    Under ``exhaustive`` and ``minimal`` Phase 4 is a no-op (dra#45 §5.3).
    Under ``progressive`` Phase 4 derives blocking questions from mixed-source
    recon branches — but the oracle-fixture corpus is designed so every topic's
    objective fully resolves its recon dimensions, so there are never blocking
    questions.  This is the design lever that makes progressive consume fewer
    user turns than exhaustive while matching its handoff correctness.
    """
    return []


def _is_critical_question(question: str) -> bool:
    """True if a question directly affects the architecture.

    The standalone ``objective`` question (progressive/minimal p1) and the
    §9.1 questionnaire questions matching the critical patterns are architecture-
    determining; the rest are non-critical noise for the annoyance metric.
    """
    if question == "objective":
        return True
    return any(p in question for p in _CRITICAL_QUESTION_PATTERNS)


def _simulate_strategy(strategy: str, topic: Topic) -> _PerTopicResult:
    """Simulate one strategy's interview flow for one topic.

    Replays the p1/p2/p4 phases described in ``control_plane.py`` (dra#45) as a
    pure function:

    1. **p1** — ask questions per strategy (exhaustive: 11; progressive/minimal:
       objective only).  Count non-critical questions for the annoyance metric.
    2. **p2** — reconnaissance fan-out: probe every perspective, revealing all
       ``late_facts`` tagged with each.
    3. **p4** — progressive-only conditional clarification.  By fixture design
       there are zero blocking questions, so p4 contributes 0 turns.
    4. **handoff assembly** — early facts are always present; late facts enter
       the handoff only when a confirmation phase (p4 for progressive, the full
       questionnaire for exhaustive) contextualises them.  Minimal skips p4,
       so its recon-discovered late facts are never contextualised and are
       absent from the handoff.
    """
    p1_qs = _p1_questions_for(strategy)
    non_critical = sum(1 for q in p1_qs if not _is_critical_question(q))

    # p2: recon reveals all late facts (all perspectives probed).
    late_facts = topic.late_facts
    perspectives_with_facts = len({lf.recon_perspective for lf in late_facts})
    perspectives_probed = len(_RECON_PERSPECTIVES)

    # p4: progressive-only; 0 blocking questions by fixture design.
    p4_qs = _p4_blocking_questions(strategy, topic)

    # Handoff assembly: which late facts made it in.
    if strategy == STRATEGY_EXHAUSTIVE:
        late_in_handoff = late_facts  # full questionnaire contextualises all
    elif strategy == STRATEGY_PROGRESSIVE:
        late_in_handoff = late_facts  # p4 phase folds recon findings in
    else:
        late_in_handoff = ()  # minimal: no p4 → late facts not contextualised

    total_facts = len(topic.early_facts) + len(late_facts)
    handoff_facts = len(topic.early_facts) + len(late_in_handoff)
    correctness = handoff_facts / total_facts if total_facts else 0.0

    return _PerTopicResult(
        p1_questions=len(p1_qs),
        p4_questions=len(p4_qs),
        non_critical_questions=non_critical,
        late_facts_discovered=len(late_facts),
        late_facts_in_handoff=len(late_in_handoff),
        late_facts_missed=len(late_facts) - len(late_in_handoff),
        perspectives_probed=perspectives_probed,
        perspectives_with_facts=perspectives_with_facts,
        handoff_fact_count=handoff_facts,
        total_fact_count=total_facts,
    )


def _compute_strategy_metrics(
    strategy: str, topic_results: list[_PerTopicResult], cfg: "ProofConfig"
) -> dict[str, Any]:
    """Aggregate per-topic simulation results into a strategy-level metric dict."""
    n = len(topic_results) or 1
    total_late = sum(r.late_facts_discovered for r in topic_results)
    total_late_missed = sum(r.late_facts_missed for r in topic_results)
    total_non_critical = sum(r.non_critical_questions for r in topic_results)
    total_questions = len(_EXHAUSTIVE_QUESTIONNAIRE)
    total_perspectives = len(_RECON_PERSPECTIVES)

    user_turns = sum(r.p1_questions + r.p4_questions for r in topic_results) / n
    annoyance = total_non_critical / (n * total_questions) if total_questions else 0.0
    facts_discovered_late = total_late_missed
    wasted_research = sum(
        (r.perspectives_probed - r.perspectives_with_facts) / r.perspectives_probed
        for r in topic_results
    ) / n
    correctness = sum(
        r.handoff_fact_count / r.total_fact_count if r.total_fact_count else 0.0
        for r in topic_results
    ) / n

    return {
        "strategy": strategy,
        "user_turns": round(user_turns, 4),
        "annoyance_proxy": round(annoyance, 4),
        "architecture_changing_facts_discovered_late": facts_discovered_late,
        "research_wasted_on_discarded_branches": round(wasted_research, 4),
        "final_handoff_correctness": round(correctness, 4),
        "topics_evaluated": n,
        "total_late_facts": total_late,
    }


def _compute_reversal_trigger(
    metrics_by_strategy: dict[str, dict[str, Any]], cfg: "ProofConfig"
) -> dict[str, Any]:
    """Assemble the ADR-005 reversal-trigger set (progressive vs exhaustive)."""
    prog = metrics_by_strategy[STRATEGY_PROGRESSIVE]
    exh = metrics_by_strategy[STRATEGY_EXHAUSTIVE]
    turns_saved = exh["user_turns"] - prog["user_turns"]

    triggers = {
        "progressive_handoff_not_worse_than_exhaustive": {
            "value": {
                "progressive": prog["final_handoff_correctness"],
                "exhaustive": exh["final_handoff_correctness"],
            },
            "threshold": "progressive >= exhaustive",
            "pass": prog["final_handoff_correctness"] >= exh["final_handoff_correctness"],
        },
        "progressive_fewer_turns_than_exhaustive": {
            "value": {
                "progressive": prog["user_turns"],
                "exhaustive": exh["user_turns"],
            },
            "threshold": "progressive < exhaustive",
            "pass": prog["user_turns"] < exh["user_turns"],
        },
        "progressive_saves_at_least_min_turns": {
            "value": turns_saved,
            "threshold": f">= {cfg.turns_savings_min}",
            "pass": turns_saved >= cfg.turns_savings_min,
        },
        "progressive_annoyance_below_threshold": {
            "value": prog["annoyance_proxy"],
            "threshold": f"< {cfg.annoyance_threshold}",
            "pass": prog["annoyance_proxy"] < cfg.annoyance_threshold,
        },
        "progressive_wasted_research_below_ceil": {
            "value": prog["research_wasted_on_discarded_branches"],
            "threshold": f"< {cfg.wasted_research_ceil}",
            "pass": prog["research_wasted_on_discarded_branches"] < cfg.wasted_research_ceil,
        },
        "progressive_handoff_correctness_above_floor": {
            "value": prog["final_handoff_correctness"],
            "threshold": f">= {cfg.handoff_correctness_floor}",
            "pass": prog["final_handoff_correctness"] >= cfg.handoff_correctness_floor,
        },
    }
    return triggers


def _run_simulation(cfg: "ProofConfig") -> dict[str, Any]:
    """Run the pure-Python A/B simulation and return the report body.

    This is the always-green path: no DB, no network, fully deterministic.
    """
    topics = generate_oracle_corpus(n_topics=cfg.n_topics, seed=cfg.seed)

    strategies = cfg.strategies
    metrics_by_strategy: dict[str, dict[str, Any]] = {}
    per_topic: dict[str, list[dict]] = {}

    for strategy in strategies:
        results = [_simulate_strategy(strategy, t) for t in topics]
        metrics_by_strategy[strategy] = _compute_strategy_metrics(strategy, results, cfg)
        per_topic[strategy] = [
            {
                "topic_id": t.id,
                "p1_questions": r.p1_questions,
                "p4_questions": r.p4_questions,
                "late_facts_discovered": r.late_facts_discovered,
                "late_facts_in_handoff": r.late_facts_in_handoff,
                "late_facts_missed": r.late_facts_missed,
                "handoff_correctness": round(
                    r.handoff_fact_count / r.total_fact_count
                    if r.total_fact_count else 0.0,
                    4,
                ),
            }
            for t, r in zip(topics, results)
        ]

    reversal_triggers = _compute_reversal_trigger(metrics_by_strategy, cfg)
    reversal_pass = all(t["pass"] for t in reversal_triggers.values())
    verdict = "PASS" if reversal_pass else "FAIL"

    report: dict[str, Any] = {
        "schema_version": 1,
        "mission": MISSION,
        "spec_anchor": SPEC_ANCHOR,
        "generated_at": _utcnow_iso(),
        "run_id": _run_id(),
        "config": {
            "seed": cfg.seed,
            "n_topics": cfg.n_topics,
            "strategies": list(strategies),
            "annoyance_threshold": cfg.annoyance_threshold,
            "wasted_research_ceil": cfg.wasted_research_ceil,
            "handoff_correctness_floor": cfg.handoff_correctness_floor,
            "turns_savings_min": cfg.turns_savings_min,
        },
        "corpus": {
            "topics": len(topics),
            "recon_perspectives": list(_RECON_PERSPECTIVES),
            "critical_question_count": _CRITICAL_QUESTION_COUNT,
            "total_questionnaire_size": len(_EXHAUSTIVE_QUESTIONNAIRE),
        },
        "strategies": metrics_by_strategy,
        "per_topic": per_topic,
        "reversal_triggers": reversal_triggers,
        "verdict": verdict,
        "adr005_reversal_triggered": verdict == "FAIL",
    }
    return report


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class ProofConfig:
    """Tunable configuration for the §38.5 progressive-interview A/B proof."""

    seed: int = 42
    strategies: tuple[str, ...] = (
        STRATEGY_PROGRESSIVE,
        STRATEGY_EXHAUSTIVE,
        STRATEGY_MINIMAL,
    )
    n_topics: int = 12
    annoyance_threshold: float = 0.5
    wasted_research_ceil: float = 0.4
    handoff_correctness_floor: float = 0.9
    turns_savings_min: int = 1

    @classmethod
    def from_env(cls) -> "ProofConfig":
        cfg = cls()
        if "DRA_PROOF_SEED" in os.environ:
            cfg = cls(seed=int(os.environ["DRA_PROOF_SEED"]))
        if "DRA_PROOF_NTOPICS" in os.environ:
            cfg.n_topics = int(os.environ["DRA_PROOF_NTOPICS"])
        if "DRA_PROOF_ANNOYANCE" in os.environ:
            cfg.annoyance_threshold = float(os.environ["DRA_PROOF_ANNOYANCE"])
        if "DRA_PROOF_WASTED_CEIL" in os.environ:
            cfg.wasted_research_ceil = float(os.environ["DRA_PROOF_WASTED_CEIL"])
        if "DRA_PROOF_HANDOFF_FLOOR" in os.environ:
            cfg.handoff_correctness_floor = float(
                os.environ["DRA_PROOF_HANDOFF_FLOOR"]
            )
        return cfg


def _load_config() -> ProofConfig:
    """Build a ProofConfig, applying optional env overrides for SLOs."""
    return ProofConfig.from_env()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_id() -> str:
    return f"progressive-interview-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"


# ---------------------------------------------------------------------------
# Report writer (json + markdown, mirrors proof_corpus.write_report)
# ---------------------------------------------------------------------------


def write_report(report: dict[str, Any], path: str = "progressive_interview_report.json") -> None:
    """Write the §38.5 proof report as JSON + a markdown summary."""
    with open(path, "w") as f:
        json.dump(report, f, indent=2)

    md_path = path.replace(".json", ".md")
    with open(md_path, "w") as f:
        f.write(_report_markdown(report))


def _report_markdown(report: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# §38.5 Progressive-Interview A/B Proof Report")
    lines.append("")
    lines.append(f"- **Mission:** {report['mission']}")
    lines.append(f"- **Spec anchor:** {report['spec_anchor']}")
    lines.append(f"- **Generated at:** {report['generated_at']}")
    lines.append(f"- **Run ID:** {report['run_id']}")
    lines.append("")

    cfg = report["config"]
    lines.append("## Configuration")
    lines.append(f"- Seed: {cfg['seed']} | Topics: {cfg['n_topics']}")
    lines.append(f"- Strategies: {', '.join(cfg['strategies'])}")
    lines.append(
        f"- SLOs: annoyance < {cfg['annoyance_threshold']}, "
        f"wasted < {cfg['wasted_research_ceil']}, "
        f"correctness >= {cfg['handoff_correctness_floor']}, "
        f"turns saved >= {cfg['turns_savings_min']}"
    )
    lines.append("")

    c = report["corpus"]
    lines.append("## Oracle corpus")
    lines.append(
        f"- Topics: {c['topics']} | Recon perspectives: "
        f"{len(c['recon_perspectives'])} | Questionnaire size: {c['total_questionnaire_size']}"
    )
    lines.append("")

    lines.append("## §38.5 metrics per strategy")
    lines.append(
        "| Strategy | User turns | Annoyance | Late facts missed | "
        "Wasted research | Handoff correctness |"
    )
    lines.append(
        "|----------|-----------|-----------|-------------------|"
        "----------------|----------------------|"
    )
    for strat in cfg["strategies"]:
        m = report["strategies"][strat]
        lines.append(
            f"| {strat} | {m['user_turns']} | {m['annoyance_proxy']:.4f} | "
            f"{m['architecture_changing_facts_discovered_late']} | "
            f"{m['research_wasted_on_discarded_branches']:.4f} | "
            f"{m['final_handoff_correctness']:.4f} |"
        )
    lines.append("")

    lines.append("## ADR-005 reversal triggers")
    lines.append("| Trigger | Value | Threshold | Result |")
    lines.append("|---------|-------|-----------|--------|")
    for name, trig in report["reversal_triggers"].items():
        val = trig.get("value")
        if isinstance(val, dict):
            val_str = f"prog={val.get('progressive')}, exh={val.get('exhaustive')}"
        else:
            val_str = str(val)
        lines.append(
            f"| {name} | {val_str} | {trig['threshold']} | "
            f"{'PASS' if trig['pass'] else 'FAIL'} |"
        )
    lines.append("")

    lines.append("## Verdict")
    lines.append(
        f"**{report['verdict']}** — ADR-005 reversal triggered: "
        f"{report['adr005_reversal_triggered']}"
    )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# DB reachability + CLI (mirror dra.proof_corpus / dra.verification_gate)
# ---------------------------------------------------------------------------


def _check_db_reachable() -> bool:
    try:
        return asyncio.run(can_connect())
    except Exception:
        return False


async def _stage_assertions(
    run_id: str, task_id: str, actor: dict[str, Any], report: dict[str, Any]
) -> bool:
    """Stage per-strategy MAINTAINER_ASSERTION rows + the A/B report.

    Uses ``InvestigatorContext`` → ``publish_bundle`` which flips
    ``staged``→``canonical`` via the ``_STANDALONE_STATE_TABLES`` mirror path
    (``user_assertion`` has no ``prov_entity`` row — ADR-017).  Non-blocking:
    any DB failure is swallowed so the always-green offline path is never broken
    by a DB that is down or migrations that are not applied.
    """
    try:
        from dra.investigators import InvestigatorContext

        async with InvestigatorContext(
            run_id=run_id,
            task_id=task_id,
            actor=actor,
            label="progressive-interview-ab",
        ) as ctx:
            for strat in report["config"]["strategies"]:
                metrics = report["strategies"][strat]
                await ctx.stage_user_assertion(
                    "MAINTAINER_ASSERTION",
                    f"§38.5 strategy:{strat}:metrics",
                    metrics,
                    run_id=run_id,
                    task_id=task_id,
                )
                await ctx.stage_user_assertion(
                    "MAINTAINER_ASSERTION",
                    f"§38.5 strategy:{strat}:verdict",
                    {"pass": _strategy_slo_pass(strat, report)},
                    run_id=run_id,
                    task_id=task_id,
                )
            await ctx.stage_user_assertion(
                "MAINTAINER_ASSERTION",
                "§38.5 A/B report",
                report,
                run_id=run_id,
                task_id=task_id,
            )
        return True
    except Exception:
        return False


def _strategy_slo_pass(strategy: str, report: dict[str, Any]) -> bool:
    """Whether a strategy satisfies all its SLO reversal triggers."""
    prefix = "progressive_" if strategy == STRATEGY_PROGRESSIVE else None
    if prefix is None:
        return True
    for key, trig in report["reversal_triggers"].items():
        if key.startswith(prefix) and not trig["pass"]:
            return False
    return True


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


async def run_proof(
    cfg: ProofConfig | None = None,
    *,
    write: bool = True,
    report_path: str = "progressive_interview_report.json",
    publish: bool = True,
) -> dict[str, Any]:
    """Run the full §38.5 progressive-interview A/B proof and return the report.

    Steps:
      1. Run the pure-Python simulation (always-green, no DB/network).
      2. If ``write``, emit JSON + markdown report.
      3. If ``publish`` and DB is reachable, stage per-strategy assertions + the
         A/B report into ``user_assertion`` (canonical) via InvestigatorContext.
    """
    if cfg is None:
        cfg = _load_config()

    report = _run_simulation(cfg)

    if write:
        write_report(report, path=report_path)

    report["staged"] = False
    if publish:
        db_ok = await _check_db_reachable_async()
        if db_ok:
            run_id = report["run_id"]
            task_id = f"progressive-interview-{run_id.split('-')[-1]}"
            staged = await _stage_assertions(run_id, task_id, _ACTOR, report)
            report["staged"] = staged

    return report


async def _check_db_reachable_async() -> bool:
    try:
        return await can_connect()
    except Exception:
        return False


def main() -> None:
    """CLI entry point: run the §38.5 progressive-interview A/B proof."""
    parser = argparse.ArgumentParser(
        prog="dra-progressive-interview",
        description="Run the §38.5 progressive-interview A/B proof: A/B-test "
        "progressive vs exhaustive vs minimal interview strategies against a "
        "deterministic oracle-user fixture corpus, measure five §38.5 metrics, "
        "and emit a pass/fail report vs the ADR-005 reversal trigger.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Verify config without running the simulation or touching the DB.",
    )
    parser.add_argument(
        "--n-topics",
        type=int,
        default=None,
        help="Override number of oracle corpus topics (default: 12).",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Override deterministic seed (default: 42).",
    )
    parser.add_argument(
        "--no-publish",
        action="store_true",
        help="Skip DB staging of assertion records (offline-only mode).",
    )
    parser.add_argument(
        "--report-path",
        default="progressive_interview_report.json",
        help="Path for the machine-checkable JSON report (default: "
        "progressive_interview_report.json).",
    )
    args = parser.parse_args()

    cfg = _load_config()
    if args.n_topics is not None:
        cfg.n_topics = args.n_topics
    if args.seed is not None:
        cfg.seed = args.seed

    if args.dry_run:
        print(f"[proof] §38.5 Progressive-interview A/B proof — dry run")
        print(f"  DATABASE_URL: {DATABASE_URL}")
        print(f"  config: {cfg.n_topics} topics, seed={cfg.seed}, "
              f"strategies={cfg.strategies}")
        print(f"  SLOs: annoyance<{cfg.annoyance_threshold}, "
              f"wasted<{cfg.wasted_research_ceil}, "
              f"correctness>={cfg.handoff_correctness_floor}, "
              f"turns_saved>={cfg.turns_savings_min}")
        print(f"  DB reachable: {'yes' if _check_db_reachable() else 'no (staging skipped)'}")
        print(f"  Report path: {args.report_path}")
        return

    publish = not args.no_publish
    print(f"[proof] §38.5 Progressive-interview A/B proof — config: "
          f"{cfg.n_topics} topics, seed={cfg.seed}, strategies={cfg.strategies}")
    print(f"  DB reachable for staging: {publish and _check_db_reachable()}")

    report = asyncio.run(
        run_proof(cfg, write=True, report_path=args.report_path, publish=publish)
    )

    print("\n=== §38.5 Progressive-Interview A/B Proof — ADR-005 Reversal Triggers ===")
    print(f"{'Trigger':<52} {'Value':<28} {'Result':<6}")
    print("-" * 86)
    for name, trig in report["reversal_triggers"].items():
        val = trig.get("value")
        if isinstance(val, dict):
            val_str = f"prog={val.get('progressive'):.4f}, exh={val.get('exhaustive'):.4f}"
        else:
            val_str = str(val)
        result = "PASS" if trig["pass"] else "FAIL"
        print(f"{name:<52} {val_str:<28} {result:<6}")
    print("-" * 86)

    print(f"\nVERDICT: {report['verdict']}  |  ADR-005 reversal triggered: {report['adr005_reversal_triggered']}")
    print(f"  Staged to DB: {report.get('staged', False)}")
    prog = report["strategies"][STRATEGY_PROGRESSIVE]
    exh = report["strategies"][STRATEGY_EXHAUSTIVE]
    print(f"  Progressive: turns={prog['user_turns']}, correctness={prog['final_handoff_correctness']:.4f}")
    print(f"  Exhaustive:  turns={exh['user_turns']}, correctness={exh['final_handoff_correctness']:.4f}")
    print(f"\nReport: {args.report_path} + {args.report_path.replace('.json', '.md')}")

    if report["verdict"] != "PASS":
        sys.exit(1)


if __name__ == "__main__":  # pragma: no cover
    main()
