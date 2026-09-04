"""§38.5 Progressive-interview A/B proof tests (dra#46).

Mirrors ``tests/test_model_routing_proof.py`` and ``tests/test_verification_gate.py``:

- **TestProofOffline** — always green, no DB/network (pure-Python simulation).
  Verifies the A/B fixture corpus, the five §38.5 metrics, the ADR-005 reversal
  trigger, determinism, and config env overrides.
- **TestProofDB** — ``@pytest.mark.skipif`` from ``tests/_db.py``.  When Postgres
  is reachable, drives ``run_proof(publish=True)`` and asserts per-strategy
  ``MAINTAINER_ASSERTION`` rows + the full A/B report are staged as ``canonical``
  in the standalone ``user_assertion`` table (dra#44, ``0008_interview_constraints``).

Test style follows ``test_atomic_commit.py``: synchronous ``def test_*()``
wrapping an ``async def run()`` driven via ``asyncio.run``.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import text

from dra.control_plane import (
    STRATEGY_EXHAUSTIVE,
    STRATEGY_MINIMAL,
    STRATEGY_PROGRESSIVE,
    _EXHAUSTIVE_QUESTIONNAIRE,
    _RECON_PERSPECTIVES,
)
from dra.db import can_connect
from dra.progressive_interview import (
    MISSION,
    SPEC_ANCHOR,
    ProofConfig,
    _simulate_strategy,
    _run_simulation,
    generate_oracle_corpus,
    run_proof,
)

from tests._db import DB


# ---------------------------------------------------------------------------
# Shared DB gate (mirrors tests/test_model_routing_proof.py)
# ---------------------------------------------------------------------------


def _db_reachable() -> bool:
    try:
        return asyncio.run(can_connect())
    except Exception:
        return False


def _metrics_equal(a: dict, b: dict) -> bool:
    """Compare metric dicts, excluding timestamps/volatile fields."""
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
        cfg = ProofConfig()
        return await run_proof(cfg, write=False, publish=False)
    return asyncio.run(run())


# ===========================================================================
# OFFLINE / PURE TESTS (no DB, no network — always green)
# ===========================================================================


class TestOracleCorpus:
    """Deterministic fixture corpus properties."""

    def test_corpus_is_deterministic(self):
        c1 = generate_oracle_corpus(n_topics=12, seed=42)
        c2 = generate_oracle_corpus(n_topics=12, seed=42)
        assert [t.id for t in c1] == [t.id for t in c2]
        assert all(t.early_facts for t in c1)
        assert all(len(t.late_facts) >= 1 for t in c1)

    def test_corpus_has_twelve_topics(self):
        topics = generate_oracle_corpus()
        assert len(topics) == 12

    def test_topics_have_early_and_late_facts(self):
        topics = generate_oracle_corpus()
        for t in topics:
            assert len(t.early_facts) >= 2, f"{t.id}: needs >=2 early facts"
            assert len(t.late_facts) >= 2, f"{t.id}: needs >=2 late facts"

    def test_late_facts_tagged_with_known_perspective(self):
        topics = generate_oracle_corpus()
        valid = set(_RECON_PERSPECTIVES)
        for t in topics:
            for lf in t.late_facts:
                assert lf.recon_perspective in valid, (
                    f"{t.id}: unknown perspective {lf.recon_perspective!r}"
                )

    def test_n_topics_subset_is_deterministic(self):
        sub1 = generate_oracle_corpus(n_topics=5, seed=42)
        sub2 = generate_oracle_corpus(n_topics=5, seed=42)
        assert [t.id for t in sub1] == [t.id for t in sub2]


class TestProofConfig:
    """ProofConfig defaults and env overrides."""

    def test_default(self):
        cfg = ProofConfig()
        assert cfg.seed == 42
        assert cfg.n_topics == 12
        assert cfg.annoyance_threshold == 0.5
        assert cfg.wasted_research_ceil == 0.4
        assert cfg.handoff_correctness_floor == 0.9
        assert cfg.turns_savings_min == 1

    def test_strategies_are_valid(self):
        from dra.control_plane import _VALID_STRATEGIES

        cfg = ProofConfig()
        for s in cfg.strategies:
            assert s in _VALID_STRATEGIES

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("DRA_PROOF_SEED", "99")
        monkeypatch.setenv("DRA_PROOF_NTOPICS", "5")
        monkeypatch.setenv("DRA_PROOF_ANNOYANCE", "0.3")
        monkeypatch.setenv("DRA_PROOF_WASTED_CEIL", "0.6")
        monkeypatch.setenv("DRA_PROOF_HANDOFF_FLOOR", "0.85")
        cfg = ProofConfig.from_env()
        assert cfg.seed == 99
        assert cfg.n_topics == 5
        assert cfg.annoyance_threshold == 0.3
        assert cfg.wasted_research_ceil == 0.6
        assert cfg.handoff_correctness_floor == 0.85


class TestSimulateStrategy:
    """Unit tests for the pure _simulate_strategy function."""

    def test_exhaustive_asks_all_questions(self):
        topic = generate_oracle_corpus(n_topics=1, seed=42)[0]
        r = _simulate_strategy(STRATEGY_EXHAUSTIVE, topic)
        assert r.p1_questions == len(_EXHAUSTIVE_QUESTIONNAIRE)
        assert r.p4_questions == 0

    def test_progressive_asks_one_question_no_p4(self):
        topic = generate_oracle_corpus(n_topics=1, seed=42)[0]
        r = _simulate_strategy(STRATEGY_PROGRESSIVE, topic)
        assert r.p1_questions == 1
        assert r.p4_questions == 0

    def test_minimal_asks_one_question_no_p4(self):
        topic = generate_oracle_corpus(n_topics=1, seed=42)[0]
        r = _simulate_strategy(STRATEGY_MINIMAL, topic)
        assert r.p1_questions == 1
        assert r.p4_questions == 0

    def test_exhaustive_includes_all_late_facts(self):
        topic = generate_oracle_corpus(n_topics=1, seed=42)[0]
        r = _simulate_strategy(STRATEGY_EXHAUSTIVE, topic)
        assert r.late_facts_missed == 0
        assert r.handoff_correctness() == 1.0

    def test_minimal_misses_all_late_facts(self):
        topic = generate_oracle_corpus(n_topics=1, seed=42)[0]
        r = _simulate_strategy(STRATEGY_MINIMAL, topic)
        assert r.late_facts_missed == len(topic.late_facts)
        assert r.late_facts_in_handoff == 0

    def test_progressive_matches_exhaustive_correctness(self):
        topic = generate_oracle_corpus(n_topics=1, seed=42)[0]
        prog = _simulate_strategy(STRATEGY_PROGRESSIVE, topic)
        exh = _simulate_strategy(STRATEGY_EXHAUSTIVE, topic)
        assert prog.handoff_correctness() == pytest.approx(exh.handoff_correctness())

    def test_minimal_correctness_below_progressive(self):
        topic = generate_oracle_corpus(n_topics=1, seed=42)[0]
        prog = _simulate_strategy(STRATEGY_PROGRESSIVE, topic)
        mini = _simulate_strategy(STRATEGY_MINIMAL, topic)
        assert mini.handoff_correctness() < prog.handoff_correctness()


class TestProofOffline:
    """Offline proof run (no DB needed) — always green."""

    def test_run_proof_offline_passes(self, tmp_path):
        report = _offline_report()
        assert report["verdict"] == "PASS"
        assert report["adr005_reversal_triggered"] is False

    def test_run_proof_report_keys(self):
        report = _offline_report()
        assert report["schema_version"] == 1
        assert report["mission"] == MISSION
        assert report["spec_anchor"] == SPEC_ANCHOR
        assert "strategies" in report
        assert "reversal_triggers" in report
        assert "verdict" in report
        assert "per_topic" in report

    def test_all_strategies_evaluated(self):
        cfg = ProofConfig()
        report = _offline_report()
        for s in cfg.strategies:
            assert s in report["strategies"], f"strategy {s} missing from report"

    def test_progressive_fewer_turns_than_exhaustive(self):
        """Core claim: progressive consumes fewer user turns than exhaustive."""
        report = _offline_report()
        prog = report["strategies"][STRATEGY_PROGRESSIVE]
        exh = report["strategies"][STRATEGY_EXHAUSTIVE]
        assert prog["user_turns"] < exh["user_turns"]

    def test_progressive_not_worse_handoff(self):
        """Progressive handoff correctness is not worse than exhaustive."""
        report = _offline_report()
        prog = report["strategies"][STRATEGY_PROGRESSIVE]
        exh = report["strategies"][STRATEGY_EXHAUSTIVE]
        assert prog["final_handoff_correctness"] >= exh["final_handoff_correctness"]

    def test_progressive_annoyance_below_threshold(self):
        """Progressive annoyance proxy is below the SLO ceiling (0.5)."""
        report = _offline_report()
        prog = report["strategies"][STRATEGY_PROGRESSIVE]
        assert prog["annoyance_proxy"] < 0.5

    def test_exhaustive_annoyance_above_threshold(self):
        """Exhaustive asks 11 questions, 8 non-critical → annoyance >= 0.5."""
        report = _offline_report()
        exh = report["strategies"][STRATEGY_EXHAUSTIVE]
        assert exh["annoyance_proxy"] >= 0.5

    def test_minimal_misses_late_facts(self):
        """Minimal (no p4) misses late-discovered facts but has low turns."""
        report = _offline_report()
        mini = report["strategies"][STRATEGY_MINIMAL]
        exh = report["strategies"][STRATEGY_EXHAUSTIVE]
        assert mini["architecture_changing_facts_discovered_late"] > 0
        assert mini["final_handoff_correctness"] < exh["final_handoff_correctness"]

    def test_reversal_triggers_all_pass(self):
        report = _offline_report()
        for name, trig in report["reversal_triggers"].items():
            assert trig["pass"] is True, f"trigger {name} failed: {trig}"

    def test_metrics_deterministic(self):
        """Two runs produce identical metric values (seed-pinned corpus)."""
        r1 = _offline_report()
        r2 = _offline_report()
        assert _metrics_equal(r1["strategies"], r2["strategies"])
        assert _metrics_equal(r1["reversal_triggers"], r2["reversal_triggers"])
        assert r1["verdict"] == r2["verdict"]

    def test_run_proof_writes_report(self, tmp_path):
        """write=True emits JSON + markdown."""
        async def run():
            cfg = ProofConfig()
            path = str(tmp_path / "report.json")
            report = await run_proof(cfg, write=True, report_path=path, publish=False)
            return report, path
        report, path = asyncio.run(run())
        assert report["verdict"] == "PASS"
        with open(path) as f:
            loaded = json.load(f)
        assert loaded["verdict"] == "PASS"
        md_path = path.replace(".json", ".md")
        assert __import__("os").path.exists(md_path)

    def test_config_env_override(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DRA_PROOF_SEED", "7")
        monkeypatch.setenv("DRA_PROOF_NTOPICS", "6")
        cfg = ProofConfig.from_env()
        assert cfg.seed == 7
        assert cfg.n_topics == 6
        report = _run_simulation(cfg)
        assert len(report["per_topic"][STRATEGY_PROGRESSIVE]) == 6
        assert report["verdict"] == "PASS"


# ===========================================================================
# DB-GATED TESTS (require Postgres + pgvector + migration 0008)
# ===========================================================================


@pytest.mark.skipif(not _db_reachable(), reason="No Postgres")
class TestProofDB:
    """DB-backed assertion-staging assertions (skip without Postgres)."""

    def test_assertion_records_staged(self, tmp_path):
        """After publish=True, user_assertion has canonical MAINTAINER_ASSERTION rows."""

        async def run():
            cfg = ProofConfig()
            report = await run_proof(
                cfg, write=True,
                report_path=str(tmp_path / "r.json"), publish=True,
            )
            from dra.publish import async_session

            async with async_session() as s:
                cnt = await s.scalar(
                    text(
                        "SELECT count(*) FROM user_assertion "
                        "WHERE assertion_type = 'MAINTAINER_ASSERTION' AND state = 'canonical'"
                    )
                )
                return cnt, report
        cnt, report = asyncio.run(run())
        assert cnt >= 1, f"expected >=1 canonical MAINTAINER_ASSERTION row, got {cnt}"
        assert report["verdict"] == "PASS"

    def test_ab_report_staged(self, tmp_path):
        """The full A/B report row exists in canonical state."""

        async def run():
            cfg = ProofConfig()
            await run_proof(cfg, write=True, report_path=str(tmp_path / "r.json"), publish=True)
            from dra.publish import async_session

            async with async_session() as s:
                row = await s.execute(
                    text(
                        "SELECT id, question, state, assertion_type FROM user_assertion "
                        "WHERE assertion_type = 'MAINTAINER_ASSERTION' "
                        "AND question = '§38.5 A/B report'"
                    )
                )
                return row.mappings().fetchall()
        rows = asyncio.run(run())
        assert len(rows) >= 1
        assert rows[0]["state"] == "canonical"

    def test_per_strategy_assertions_canonical(self, tmp_path):
        """Each strategy has MAINTAINER_ASSERTION rows in canonical state."""

        async def run():
            cfg = ProofConfig()
            await run_proof(cfg, write=True, report_path=str(tmp_path / "r.json"), publish=True)
            from dra.publish import async_session

            async with async_session() as s:
                results = {}
                for strat in cfg.strategies:
                    row = await s.execute(
                        text(
                            "SELECT count(*) FROM user_assertion "
                            "WHERE assertion_type = 'MAINTAINER_ASSERTION' "
                            "AND question = :q AND state = 'canonical'"
                        ),
                        {"q": f"§38.5 strategy:{strat}:metrics"},
                    )
                    results[strat] = row.scalar()
                return results
        results = asyncio.run(run())
        for strat in (STRATEGY_PROGRESSIVE, STRATEGY_EXHAUSTIVE, STRATEGY_MINIMAL):
            assert results[strat] >= 1, f"strategy {strat}: no canonical metrics row"

    def test_publish_idempotent(self, tmp_path):
        """Re-running stages new bundles, doesn't corrupt prior canonical rows."""

        async def run():
            cfg = ProofConfig()
            await run_proof(cfg, write=True, report_path=str(tmp_path / "r1.json"), publish=True)
            await run_proof(cfg, write=True, report_path=str(tmp_path / "r2.json"), publish=True)
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
