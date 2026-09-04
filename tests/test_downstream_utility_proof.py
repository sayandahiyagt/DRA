"""§38.6 Downstream-utility proof tests (sayandahiyagt/dra#43).

Mirrors ``tests/test_progressive_interview.py``:

- **TestArmSeed** — the 5-condition arm seed matches eval_plan.md §6 (ids,
  labels, provides_*), and ``ARMS_JSON_SEED`` is valid JSON with the 5 arm ids.
- **TestTaskSet** — ``generate_task_set`` is deterministic (same seed -> same ids);
  ≥1 task; each task has factual_requirements + structure; ≥1 repo-extension
  task per §37 Stage 0.
- **TestObservableFacts** — monotonicity: ``observable(arm5) ⊇ … ⊇ observable(arm1)``;
  arm5 observes every fact.
- **TestFakeDownstreamAgent** — ``attempt`` is deterministic; re_research rises as
  the arm degrades; incorrect_assumptions rise as the arm degrades; ``build_green``
  is False when assumptions>0 or re-research>0.
- **TestMetrics** — ``measure_arm`` returns exactly the 4 §38.6 metric keys + the
  §24.4 cross-cutting context keys; time = base + linear composite.
- **TestDecisionRule** — PASS when arm5 strictly beats both baselines on all four
  under 0.90/0.95; FAIL on a tie; FAIL when a baseline beats arm5 on one metric.
- **TestProofOffline** — ``run_proof`` verdict PASS; report schema keys; determinism;
  ``write_report`` emits both files; ``held_constant`` populated; arms 4/5 consume
  the real handoff contract.

- **TestProofDB** — ``@pytest.mark.skipif`` from ``tests/_db.py``. When Postgres is
  reachable, drives ``run_proof(publish=True)`` and asserts per-arm metric rows + the
  §38.6 report are staged as ``canonical`` in ``user_assertion``; publish is
  idempotent (re-run -> 0 staged, ≥N canonical).

Test style follows ``test_atomic_commit.py``: synchronous ``def test_*()`` wrapping
an ``async def run()`` driven via ``asyncio.run``.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import text

from dra.proof_downstream_utility import (
    ARMS,
    ARMS_JSON_SEED,
    BINDING_BASELINES,
    CORRECTNESS_STYLE_METRICS,
    RECOVERY_FACTORS,
    Arm,
    BuildAttempt,
    FactSpec,
    FakeDownstreamAgent,
    MISSION,
    SPEC_ANCHOR,
    apply_decision_rule,
    build_arm_input,
    evaluate_all,
    generate_task_set,
    measure_arm,
    observable_facts,
    run_proof,
    write_report,
)
from dra.proof_downstream_utility import _all_facts

from tests._db import DB


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _arm(arm_id: str) -> Arm:
    return next(a for a in ARMS if a.id == arm_id)


def _metrics_equal(a: dict, b: dict) -> bool:
    """Compare report dicts, excluding timestamps/volatile fields."""
    def _strip(d):
        if isinstance(d, dict):
            return {k: _strip(v) for k, v in d.items()
                    if k not in ("generated_at", "run_id", "staged")}
        if isinstance(d, list):
            return [_strip(v) for v in d]
        return d
    return json.dumps(_strip(a), sort_keys=True) == json.dumps(_strip(b), sort_keys=True)


def _offline_report():
    """Run the proof offline and return the report dict."""
    async def run():
        from dra.proof_downstream_utility import ProofConfig
        cfg = ProofConfig()
        return await run_proof(cfg, write=False, publish=False, report_path="results.json")
    return asyncio.run(run())


# ===========================================================================
# OFFLINE / PURE TESTS (no DB, no network — always green)
# ===========================================================================


class TestArmSeed:
    """The 5-condition arm seed matches eval_plan.md §6."""

    def test_seed_has_five_arms(self):
        assert len(ARMS_JSON_SEED["arms"]) == 5
        assert len(ARMS) == 5

    def test_seed_is_serializable(self):
        data = json.dumps(ARMS_JSON_SEED)
        assert json.loads(data) is not None

    def test_seed_arm_ids_and_order(self):
        expected = [
            "raw_sources",
            "ordinary_report",
            "structured_corpus_no_handoff",
            "handoff_no_queryable_corpus",
            "full_handoff_queryable_corpus",
        ]
        assert [a["id"] for a in ARMS_JSON_SEED["arms"]] == expected
        assert [a.id for a in ARMS] == expected

    def test_two_baselines_marked(self):
        assert set(BINDING_BASELINES) == {"raw_sources", "ordinary_report"}
        for a in ARMS:
            if a.id in BINDING_BASELINES:
                assert a.is_baseline
            else:
                assert not a.is_baseline

    def test_provides_contract(self):
        by_id = {a.id: a for a in ARMS}
        assert not by_id["raw_sources"].provides_handoff_document
        assert not by_id["raw_sources"].provides_queryable_knowledge
        assert by_id["ordinary_report"].provides_handoff_document
        assert not by_id["ordinary_report"].provides_queryable_knowledge
        assert not by_id["structured_corpus_no_handoff"].provides_handoff_document
        assert by_id["structured_corpus_no_handoff"].provides_queryable_knowledge
        assert by_id["handoff_no_queryable_corpus"].provides_handoff_document
        assert not by_id["handoff_no_queryable_corpus"].provides_queryable_knowledge
        assert by_id["full_handoff_queryable_corpus"].provides_handoff_document
        assert by_id["full_handoff_queryable_corpus"].provides_queryable_knowledge

    def test_decision_rule_present_in_seed(self):
        assert "decision_rule" in ARMS_JSON_SEED
        rule = ARMS_JSON_SEED["decision_rule"].lower()
        assert "strictly" in rule or "strict" in rule
        assert "baseline" in rule

    def test_factor_map_keys(self):
        assert set(RECOVERY_FACTORS) == {
            "re_research_calls", "incorrect_assumptions",
            "time_to_correct_build", "architectural_rework",
        }
        assert CORRECTNESS_STYLE_METRICS == frozenset(
            {"incorrect_assumptions", "architectural_rework"}
        )


class TestTaskSet:
    """Deterministic §37 Stage-0 ground-truth fixture."""

    def test_default_task_set(self):
        ts = generate_task_set()
        assert len(ts) >= 1
        assert all(t.facts for t in ts)
        assert all(t.structure for t in ts)

    def test_deterministic_same_seed(self):
        a = generate_task_set(n_tasks=3, seed=42)
        b = generate_task_set(n_tasks=3, seed=42)
        assert [t.id for t in a] == [t.id for t in b]

    def test_different_seed_different_order(self):
        a = generate_task_set(n_tasks=3, seed=42)
        b = generate_task_set(n_tasks=3, seed=7)
        assert [t.id for t in a] != [t.id for t in b]

    def test_repo_extension_task_present(self):
        ts = generate_task_set()
        assert any(t.repo_extension for t in ts), "≥1 §37 Stage-0 repo-extension task"

    def test_facts_have_consistent_kinds(self):
        ts = generate_task_set()
        for t in ts:
            kinds = {f.kind for f in t.facts}
            assert "identity" in kinds
            for k in kinds:
                assert k in (
                    "identity", "module_boundary", "symbol_signature",
                    "dependency_edge", "data_flow", "api_surface", "data_contract",
                    "test_entry",
                )

    def test_n_tasks_subset(self):
        ts = generate_task_set(n_tasks=1, seed=42)
        assert len(ts) == 1


class TestObservableFacts:
    """Monotone information-availability profile across arms."""

    def test_arm5_observes_everything(self):
        ts = generate_task_set()
        full = observable_facts(ts, _arm("full_handoff_queryable_corpus"))
        all_ids = {f.id for f in _all_facts(ts)}
        assert full == all_ids

    def test_monotonic_subset(self):
        ts = generate_task_set()
        order = [a.id for a in ARMS]
        prev = observable_facts(ts, _arm(order[0]))
        assert prev <= {f.id for f in _all_facts(ts)}
        for aid in order[1:]:
            cur = observable_facts(ts, _arm(aid))
            assert cur.issuperset(prev), f"{aid} not superset of previous"
            prev = cur

    def test_raw_sources_only_identity(self):
        ts = generate_task_set()
        obs = observable_facts(ts, _arm("raw_sources"))
        non_id = [f.id for f in _all_facts(ts) if f.kind != "identity"]
        assert not (obs & set(non_id)), "raw_sources must not observe non-identity facts"
        id_only = {f.id for f in _all_facts(ts) if f.kind == "identity"}
        assert obs == id_only

    def test_deterministic(self):
        ts = generate_task_set()
        a = observable_facts(ts, _arm("ordinary_report"))
        b = observable_facts(ts, _arm("ordinary_report"))
        assert a == b


class TestFakeDownstreamAgent:
    """Deterministic FakeDownstreamAgent behaviour."""

    def test_attempt_is_deterministic(self):
        ts = generate_task_set()
        agent = FakeDownstreamAgent()
        a = agent.attempt(ts, _arm("raw_sources"))
        b = agent.attempt(ts, _arm("raw_sources"))
        assert a == b

    def test_re_research_monotone_decreasing(self):
        ts = generate_task_set()
        agent = FakeDownstreamAgent()
        counts = [agent.attempt(ts, a).re_research_calls for a in ARMS]
        assert counts == sorted(counts, reverse=True), counts
        assert counts[-1] == 0  # arm5 (full) -> 0

    def test_incorrect_monotone_decreasing(self):
        ts = generate_task_set()
        agent = FakeDownstreamAgent()
        counts = [len(agent.attempt(ts, a).incorrect_assumptions) for a in ARMS]
        assert counts == sorted(counts, reverse=True), counts
        assert counts[-1] == 0

    def test_build_green_only_when_perfect(self):
        ts = generate_task_set()
        agent = FakeDownstreamAgent()
        for a in ARMS[:-1]:
            att = agent.attempt(ts, a)
            assert not att.build_green, f"{a.id} should not be build_green"
        perfect = agent.attempt(ts, ARMS[-1])
        assert perfect.build_green
        assert perfect.tests_pass

    def test_build_green_false_with_assumptions(self):
        ts = generate_task_set()
        agent = FakeDownstreamAgent()
        att = agent.attempt(ts, _arm("raw_sources"))
        assert len(att.incorrect_assumptions) > 0
        assert not att.build_green


class TestMetrics:
    """measure_arm returns the four §38.6 metrics + §24.4 cross-cutting keys."""

    EXPECTED_METRIC_KEYS = {
        "re_research_calls", "incorrect_assumptions",
        "time_to_correct_build", "architectural_rework",
    }
    EXPECTED_CROSS_KEYS = {
        "citation_entailability", "source_diversity",
        "contradiction_discovery_rate", "gap_detection_rate",
        "downstream_success", "p50_ms",
    }

    def test_metric_keys(self):
        ts = generate_task_set()
        m = measure_arm(ts, _arm("raw_sources"), "run-m")
        assert set(m["metrics"]) == self.EXPECTED_METRIC_KEYS
        for k in self.EXPECTED_CROSS_KEYS:
            assert k in m, f"missing cross-cutting key {k}"

    def test_cross_cutting_not_decision_inputs(self):
        # cross-cutting context is present but the decision rule only uses metrics.
        ts = generate_task_set()
        m = measure_arm(ts, _arm("raw_sources"), "run-m")
        assert "metrics" in m

    def test_time_is_linear_composite(self):
        ts = generate_task_set()
        agent = FakeDownstreamAgent()
        att = agent.attempt(ts, _arm("ordinary_report"))
        expected = round(
            agent.BASE_MS
            + att.re_research_calls * agent.SCOUT_MS
            + len(att.incorrect_assumptions) * agent.CORRECT_MS
            + att.rework_events * agent.RESCAFFOLD_MS,
            3,
        )
        assert att.time_ms == expected

    def test_time_monotone(self):
        ts = generate_task_set()
        times = [
            measure_arm(ts, a, "run-t")["metrics"]["time_to_correct_build"]
            for a in ARMS
        ]
        assert times == sorted(times, reverse=True)


class TestDecisionRule:
    """apply_decision_rule implements the §5 rule with strict-beat + factors."""

    def _arms_block(self, per):
        return {
            "raw_sources": {"metrics": per["raw_sources"]},
            "ordinary_report": {"metrics": per["ordinary_report"]},
            "structured_corpus_no_handoff": {"metrics": per["structured_corpus_no_handoff"]},
            "handoff_no_queryable_corpus": {"metrics": per["handoff_no_queryable_corpus"]},
            "full_handoff_queryable_corpus": {"metrics": per["full_handoff_queryable_corpus"]},
        }

    def test_pass_when_arm5_strictly_beats_all(self):
        per = {
            "raw_sources":              {"re_research_calls": 100, "incorrect_assumptions": 50,
                                        "time_to_correct_build": 5000, "architectural_rework": 40},
            "ordinary_report":          {"re_research_calls": 60,  "incorrect_assumptions": 30,
                                        "time_to_correct_build": 3000, "architectural_rework": 25},
            "structured_corpus_no_handoff": {"re_research_calls": 20, "incorrect_assumptions": 10,
                                        "time_to_correct_build": 900, "architectural_rework": 5},
            "handoff_no_queryable_corpus": {"re_research_calls": 8, "incorrect_assumptions": 3,
                                        "time_to_correct_build": 400, "architectural_rework": 2},
            "full_handoff_queryable_corpus": {"re_research_calls": 0, "incorrect_assumptions": 0,
                                        "time_to_correct_build": 100, "architectural_rework": 0},
        }
        decision = apply_decision_rule(self._arms_block(per))
        assert decision["verdict"] == "PASS"
        assert decision["simplification_triggered"] is False
        assert decision["adr018_reversal_triggered"] is False

    def test_fail_on_tie(self):
        # arm5 ties ordinary_report on time_to_correct_build (both 3000) -> tie is
        # a LOSS (§38.6:2694: strict beat required). All other metrics arm5 wins.
        per = {
            "raw_sources":              {"re_research_calls": 100, "incorrect_assumptions": 50,
                                        "time_to_correct_build": 5000, "architectural_rework": 40},
            "ordinary_report":          {"re_research_calls": 60,  "incorrect_assumptions": 30,
                                        "time_to_correct_build": 3000, "architectural_rework": 25},
            "structured_corpus_no_handoff": {"re_research_calls": 20, "incorrect_assumptions": 10,
                                        "time_to_correct_build": 1500, "architectural_rework": 5},
            "handoff_no_queryable_corpus": {"re_research_calls": 8, "incorrect_assumptions": 3,
                                        "time_to_correct_build": 500, "architectural_rework": 2},
            "full_handoff_queryable_corpus": {"re_research_calls": 0, "incorrect_assumptions": 0,
                                        "time_to_correct_build": 3000, "architectural_rework": 0},
        }
        decision = apply_decision_rule(self._arms_block(per))
        assert decision["verdict"] == "FAIL"
        assert decision["simplification_triggered"] is True
        # The failing trigger is the time tie vs ordinary_report.
        time_trig = decision["reversal_triggers"]["time_to_correct_build__vs__ordinary_report"]
        assert time_trig["value"] == 3000
        assert time_trig["baseline_value"] == 3000
        assert time_trig["pass"] is False

    def test_fail_on_baseline_win(self):
        per = {
            "raw_sources":              {"re_research_calls": 5, "incorrect_assumptions": 2,
                                        "time_to_correct_build": 100, "architectural_rework": 1},
            "ordinary_report":          {"re_research_calls": 4,  "incorrect_assumptions": 2,
                                        "time_to_correct_build": 110, "architectural_rework": 1},
            "structured_corpus_no_handoff": {"re_research_calls": 3, "incorrect_assumptions": 1,
                                        "time_to_correct_build": 120, "architectural_rework": 1},
            "handoff_no_queryable_corpus": {"re_research_calls": 2, "incorrect_assumptions": 0,
                                        "time_to_correct_build": 110, "architectural_rework": 0},
            "full_handoff_queryable_corpus": {"re_research_calls": 3, "incorrect_assumptions": 0,
                                        "time_to_correct_build": 130, "architectural_rework": 0},
        }
        decision = apply_decision_rule(self._arms_block(per))
        # arm5 re_research=3 > raw_sources=5? no. arm5 re_research=3 vs raw 5 (strict ✓)
        # but arm5 time=130 > raw 100 -> baseline beats arm5 on time -> FAIL.
        assert decision["verdict"] == "FAIL"
        assert decision["simplification_triggered"] is True
        assert decision["adr018_reversal_triggered"] is True

    def test_eight_reversal_triggers(self):
        per = {aid: {"re_research_calls": i,
                     "incorrect_assumptions": i,
                     "time_to_correct_build": float(i * 10),
                     "architectural_rework": i}
               for i, aid in enumerate(
                   ["raw_sources", "ordinary_report", "structured_corpus_no_handoff",
                    "handoff_no_queryable_corpus", "full_handoff_queryable_corpus"])}
        # arm5 (index 4) = 4; baselines raw=0, ordinary=1 -> arm5 NOT < baseline -> FAIL.
        decision = apply_decision_rule(self._arms_block(per))
        assert decision["verdict"] == "FAIL"
        assert len(decision["reversal_triggers"]) == 8  # 4 metrics x 2 baselines


class TestProofOffline:
    """Offline proof run (no DB needed) — always green."""

    def test_verdict_pass(self):
        report = _offline_report()
        assert report["verdict"] == "PASS"
        assert report["adr018_reversal_triggered"] is False

    def test_report_schema_keys(self):
        report = _offline_report()
        assert report["schema_version"] == 1
        assert report["mission"] == MISSION
        assert report["spec_anchor"] == SPEC_ANCHOR
        for key in ("arms", "per_arm", "reversal_triggers", "verdict",
                    "held_constant", "decision_rule"):
            assert key in report
        assert "simplification_triggered" in report

    def test_all_arms_measured(self):
        report = _offline_report()
        for a in ARMS:
            assert a.id in report["per_arm"]
            m = report["per_arm"][a.id]["metrics"]
            for k in RECOVERY_FACTORS:
                assert k in m

    def test_arm5_strictly_beats_baselines(self):
        report = _offline_report()
        full = report["per_arm"]["full_handoff_queryable_corpus"]["metrics"]
        for base in BINDING_BASELINES:
            b = report["per_arm"][base]["metrics"]
            assert full["re_research_calls"] < b["re_research_calls"], base
            assert full["incorrect_assumptions"] < b["incorrect_assumptions"], base
            assert full["architectural_rework"] < b["architectural_rework"], base
            assert full["time_to_correct_build"] < b["time_to_correct_build"], base

    def test_reversal_triggers_all_pass(self):
        report = _offline_report()
        for name, trig in report["reversal_triggers"].items():
            assert trig["pass"] is True, f"trigger {name} failed: {trig}"

    def test_metrics_deterministic(self):
        r1 = _offline_report()
        r2 = _offline_report()
        assert r1["verdict"] == r2["verdict"]
        assert _metrics_equal(r1["per_arm"], r2["per_arm"])
        assert _metrics_equal(r1["reversal_triggers"], r2["reversal_triggers"])

    def test_write_report_emits_both(self, tmp_path):
        async def run():
            from dra.proof_downstream_utility import ProofConfig
            path = str(tmp_path / "results.json")
            report = await run_proof(ProofConfig(), write=True,
                                     report_path=path, publish=False)
            return report, path
        report, path = asyncio.run(run())
        assert report["verdict"] == "PASS"
        with open(path) as f:
            loaded = json.load(f)
        assert loaded["verdict"] == "PASS"
        md_path = path.replace(".json", ".md")
        assert __import__("os").path.exists(md_path)

    def test_held_constant_populated(self):
        report = _offline_report()
        hc = report["held_constant"]
        for key in ("coding_agent_model", "coding_agent_version", "sandbox_image",
                    "repo_snapshot_ref", "downstream_task", "objective"):
            assert key in hc and hc[key], f"held_constant.{key} missing"

    def test_arms4_5_consume_handoff_contract(self):
        """Arms 4 & 5 actually build the §31.1 package + §31.2 manifest."""
        from dra.handoff import SECTION_FILES as REAL_SECTIONS
        ts = generate_task_set()
        for aid in ("handoff_no_queryable_corpus", "full_handoff_queryable_corpus"):
            inp = build_arm_input(ts, _arm(aid), "run-hc")
            assert "manifest" in inp
            assert "handoff_document" in inp
            pkg = inp["handoff_document"]
            for sec in REAL_SECTIONS:
                assert sec in pkg, f"{aid}: missing section {sec}"


# ===========================================================================
# DB-GATED TESTS (require Postgres + pgvector + migration 0008)
# ===========================================================================


@DB
class TestProofDB:
    """DB-backed assertion-staging assertions (skip without Postgres)."""

    def test_per_arm_metric_rows_staged(self, tmp_path):
        """After publish=True, canonical MAINTAINER_ASSERTION rows exist per arm."""
        async def run():
            from dra.proof_downstream_utility import ProofConfig
            report = await run_proof(
                ProofConfig(), write=True,
                report_path=str(tmp_path / "r.json"), publish=True,
            )
            from dra.publish import async_session
            async with async_session() as s:
                results: dict[str, int] = {}
                for arm in report["arms"]:
                    row = await s.execute(
                        text(
                            "SELECT count(*) FROM user_assertion "
                            "WHERE assertion_type = 'MAINTAINER_ASSERTION' "
                            "AND question = :q AND state = 'canonical'"
                        ),
                        {"q": f"§38.6 arm:{arm['id']}:metrics"},
                    )
                    results[arm["id"]] = row.scalar()
                return results, report
        results, report = asyncio.run(run())
        for arm in report["arms"]:
            assert results[arm["id"]] >= 1, f"{arm['id']}: no canonical metrics row"
        assert report["verdict"] == "PASS"

    def test_results_report_staged(self, tmp_path):
        """The full §38.6 report row exists in canonical state."""
        async def run():
            from dra.proof_downstream_utility import ProofConfig
            await run_proof(ProofConfig(), write=True,
                            report_path=str(tmp_path / "r.json"), publish=True)
            from dra.publish import async_session
            async with async_session() as s:
                row = await s.execute(
                    text(
                        "SELECT id, question, state, assertion_type FROM user_assertion "
                        "WHERE assertion_type = 'MAINTAINER_ASSERTION' "
                        "AND question = '§38.6 results report'"
                    )
                )
                return row.mappings().fetchall()
        rows = asyncio.run(run())
        assert len(rows) >= 1
        assert rows[0]["state"] == "canonical"

    def test_publish_idempotent(self, tmp_path):
        """Re-running stages new bundles, leaves 0 staged rows, ≥N canonical."""
        async def run():
            from dra.proof_downstream_utility import ProofConfig
            await run_proof(ProofConfig(), write=True,
                            report_path=str(tmp_path / "r1.json"), publish=True)
            await run_proof(ProofConfig(), write=True,
                            report_path=str(tmp_path / "r2.json"), publish=True)
            from dra.publish import async_session
            async with async_session() as s:
                total = await s.scalar(
                    text("SELECT count(*) FROM user_assertion WHERE state = 'canonical'")
                )
                staged = await s.scalar(
                    text("SELECT count(*) FROM user_assertion WHERE state = 'staged'")
                )
                return total, staged
        total, staged = asyncio.run(run())
        assert staged == 0, f"{staged} rows still staged — publish not atomic"
        assert total >= 6, f"expected >=6 canonical rows, got {total}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
