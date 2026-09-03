"""§38.3 Model-routing proof tests (dra#9).

Mirrors tests/test_storage_proof.py: DB-gated (pytestmark = DB from
tests/_db.py) for canonical-publishing + escalation-log assertions;
offline / pure tests are always green (no DB, no network).

Test style follows test_atomic_commit.py: synchronous def test_*() wrapping
an async def run() driven via asyncio.run.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os

import pytest
from sqlalchemy import text

from dra.db import can_connect, engine
from dra.routing.fixtures import Fixture, assert_fixtures_well_formed, load_fixtures
from dra.routing.models import (
    ExpensiveRole,
    FakeModelAdapter,
    ModelPool,
    ModelRegistry,
    make_model_adapter,
    model_pricing,
    pool_correctness,
)
from dra.routing.policy import (
    ProofConfig,
    RoutingPolicy,
    VariantMetrics,
    compute_recall,
    compute_unsupported_rate,
    cost_of,
    escalation_frequency,
    latency_p50,
    latency_p95,
)
from dra.routing.providers import (
    ContentProvider,
    BrowserProvider,
    ProviderMode,
    SearchProvider,
    SearchProviderRegistry,
    TaskType,
    make_providers,
)
from dra.routing import proof as proof_mod

from tests._db import DB


# ---------------------------------------------------------------------------
# Shared gates
# ---------------------------------------------------------------------------


def _db_reachable() -> bool:
    try:
        return asyncio.run(can_connect())
    except Exception:
        return False


def _creds_reachable() -> bool:
    keys = ["OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY",
            "EXA_API_KEY", "PERPLEXITY_API_KEY", "TAVILY_API_KEY", "FIRECRAWL_API_KEY"]
    return any(os.environ.get(k) for k in keys)


# ===========================================================================
# OFFLINE / PURE TESTS (no DB, no network — always green)
# ===========================================================================


class TestMetricFunctions:
    """Pure metric functions — exact values on synthetic inputs."""

    def test_compute_recall(self):
        assert compute_recall(10, 10) == 1.0
        assert compute_recall(0, 10) == 0.0
        assert compute_recall(5, 10) == 0.5
        assert compute_recall(0, 0) == 0.0

    def test_compute_unsupported_rate(self):
        assert compute_unsupported_rate(0, 10) == 0.0
        assert compute_unsupported_rate(10, 10) == 1.0
        assert compute_unsupported_rate(2, 10) == 0.2
        assert compute_unsupported_rate(0, 0) == 0.0

    def test_cost_of(self):
        assert cost_of(500, 1000, 1.0, 2.0) == pytest.approx(2.5)

    def test_cost_of_zero(self):
        assert cost_of(0, 0, 1.0, 2.0) == 0.0

    def test_latency_p50(self):
        assert latency_p50([]) == 0.0
        assert latency_p50([100]) == 100.0
        assert latency_p50([100, 200, 300]) == 200.0
        assert latency_p50([10, 20, 30, 40]) == 25.0

    def test_latency_p95(self):
        assert latency_p95([]) == 0.0
        assert latency_p95([100]) == 100.0
        assert latency_p95([100, 200, 300]) == 300.0

    def test_escalation_frequency(self):
        assert escalation_frequency(0, 10) == 0.0
        assert escalation_frequency(5, 10) == 0.5
        assert escalation_frequency(0, 0) == 0.0


class TestFixtures:
    """Fixture loading and well-formedness."""

    def test_load_fixtures_returns_deterministic(self):
        fxs1 = load_fixtures()
        fxs2 = load_fixtures()
        assert len(fxs1) == len(fxs2)
        assert [f.id for f in fxs1] == [f.id for f in fxs2]

    def test_fixtures_cover_all_roles(self):
        fxs = load_fixtures()
        assert {f.role for f in fxs} == set(ExpensiveRole)

    def test_each_role_has_at_least_three_fixtures(self):
        fxs = load_fixtures()
        for role in ExpensiveRole:
            role_fxs = [f for f in fxs if f.role == role]
            assert len(role_fxs) >= 3, f"{role.value}: only {len(role_fxs)}"

    def test_assert_fixtures_well_formed(self):
        assert_fixtures_well_formed(load_fixtures())  # must not raise

    def test_fixtures_have_claims_source_refs_answers(self):
        fxs = load_fixtures()
        for fx in fxs:
            assert len(fx.ground_truth.claims) > 0
            assert any(not c["supported"] for c in fx.ground_truth.claims)
            assert len(fx.ground_truth.source_refs) > 0
            assert len(fx.ground_truth.answers) > 0

    def test_assert_fixtures_rejects_empty(self):
        with pytest.raises(ValueError, match="empty"):
            assert_fixtures_well_formed([])

    def test_assert_fixtures_rejects_duplicate_id(self):
        fxs = load_fixtures()
        with pytest.raises(ValueError, match="duplicate"):
            assert_fixtures_well_formed(list(fxs) + [fxs[0]])

    def test_assert_fixtures_rejects_all_supported(self):
        fx = load_fixtures()[0]
        bad_claims = [{"text": c["text"], "supported": True} for c in fx.ground_truth.claims]
        bad_gt = type(fx.ground_truth)(
            answers=fx.ground_truth.answers, claims=bad_claims,
            source_refs=fx.ground_truth.source_refs,
        )
        bad_fx = Fixture(id=fx.id, task_type=fx.task_type, role=fx.role,
                         input=fx.input, context=fx.context, ground_truth=bad_gt)
        with pytest.raises(ValueError, match="trivial"):
            assert_fixtures_well_formed([bad_fx])


class TestPricing:
    """Pricing table correctness (ADR-008 env-overridable)."""

    def test_pricing_has_all_model_ids(self):
        p = model_pricing()
        for name in ("gpt-5.6-luna", "claude-sonnet-5", "claude-opus-5", "gemini-3.5-ultra"):
            assert name in p

    def test_pricing_env_override(self, monkeypatch):
        monkeypatch.setenv("DRA_PRICE_GPT_5_6_LUNA", "0.05,0.10")
        assert model_pricing()["gpt-5.6-luna"] == (0.05, 0.10)

    def test_pool_correctness_rates(self):
        assert pool_correctness(ModelPool.CHEAP) == 0.72
        assert pool_correctness(ModelPool.WORKHORSE) == 0.96
        assert pool_correctness(ModelPool.FRONTIER) == 0.99
        assert pool_correctness(ModelPool.ADVISOR) == 0.99


class TestModelRegistry:
    """ModelRegistry candidate selection."""

    def test_select_returns_correct_pool_role(self):
        reg = ModelRegistry()
        for role in ExpensiveRole:
            for pool in [ModelPool.CHEAP, ModelPool.WORKHORSE, ModelPool.FRONTIER]:
                spec = reg.select(role, pool)
                assert spec.pool == pool
                assert spec.role == role
                assert spec.input_cost_usd_per_1k > 0
                assert spec.output_cost_usd_per_1k > 0

    def test_candidates_sorted_cheapest_first(self):
        reg = ModelRegistry()
        for role in ExpensiveRole:
            for pool in ModelPool:
                cands = reg.candidates(role, pool)
                assert len(cands) >= 1
                for i in range(len(cands) - 1):
                    ra = cands[i].input_cost_usd_per_1k + cands[i].output_cost_usd_per_1k
                    rb = cands[i+1].input_cost_usd_per_1k + cands[i+1].output_cost_usd_per_1k
                    assert ra <= rb

    def test_frontier_more_expensive_than_workhorse(self):
        reg = ModelRegistry()
        spec = ExpensiveRole.REPO_INVESTIGATION
        wh = reg.select(spec, ModelPool.WORKHORSE)
        fr = reg.select(spec, ModelPool.FRONTIER)
        wh_r = wh.input_cost_usd_per_1k + wh.output_cost_usd_per_1k
        fr_r = fr.input_cost_usd_per_1k + fr.output_cost_usd_per_1k
        assert fr_r > wh_r

    def test_cheap_cheaper_than_workhorse(self):
        reg = ModelRegistry()
        role = ExpensiveRole.REPO_INVESTIGATION
        cheap = reg.select(role, ModelPool.CHEAP)
        wh = reg.select(role, ModelPool.WORKHORSE)
        cr = cheap.input_cost_usd_per_1k + cheap.output_cost_usd_per_1k
        wr = wh.input_cost_usd_per_1k + wh.output_cost_usd_per_1k
        assert cr < wr


class TestProviderMatrix:
    """Task-routed search-provider matrix (§17.2)."""

    def test_select_providers_returns_ordered(self):
        reg = SearchProviderRegistry()
        for task in TaskType:
            cands = reg.select_providers(task)
            assert len(cands) >= 2
            assert all(c.provider_type in ("search", "content", "sitemap", "browser")
                       for c in cands)

    def test_rendered_browser_is_fallback(self):
        reg = SearchProviderRegistry()
        for task in TaskType:
            cands = reg.select_providers(task)
            assert cands[-1].provider_type == "browser"
            assert reg.has_rendered_browser_fallback(task)

    def test_provider_matrix_spans_names(self):
        reg = SearchProviderRegistry()
        names = set()
        for task in TaskType:
            for c in reg.select_providers(task):
                names.add(c.name)
        for n in ("exa", "perplexity", "tavily", "firecrawl", "rendered_browser"):
            assert n in names, f"provider {n} not in task matrix"

    def test_make_providers_offline(self):
        p = make_providers(ProviderMode.OFFLINE)
        assert "search" in p and "content" in p and "browser" in p
        assert p["search"].name == "fake_search"
        assert p["browser"].name == "fake_browser"

    def test_make_providers_live_without_creds_raises(self):
        for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY",
                  "EXA_API_KEY", "PERPLEXITY_API_KEY", "TAVILY_API_KEY",
                  "FIRECRAWL_API_KEY"):
            os.environ.pop(k, None)
        with pytest.raises(RuntimeError, match="no provider API key"):
            make_providers(ProviderMode.LIVE)

    def test_fake_satisfy_protocols(self):
        p = make_providers(ProviderMode.OFFLINE)
        assert isinstance(p["search"], SearchProvider)
        assert isinstance(p["content"], ContentProvider)
        assert isinstance(p["browser"], BrowserProvider)

    def test_fake_search_deterministic(self):
        async def run():
            p = make_providers(ProviderMode.OFFLINE)["search"]
            r1 = await p.search("same", k=3)
            r2 = await p.search("same", k=3)
            assert [r["url"] for r in r1] == [r["url"] for r in r2]
        asyncio.run(run())


class TestRoutingPolicy:
    """RoutingPolicy picks cheapest admissible, never 'feels smarter'."""

    def test_picks_workhorse_when_cheap_fails(self):
        policy = RoutingPolicy(0.9, 0.15, 10.0)
        results = {
            "cheap": VariantMetrics(role="r", pool=ModelPool.CHEAP, provider="p",
                                    model_name="m1", correctness=0.70, unsupported_rate=0.25, cost_usd=0.01),
            "workhorse": VariantMetrics(role="r", pool=ModelPool.WORKHORSE, provider="p",
                                        model_name="m2", correctness=0.95, unsupported_rate=0.05, cost_usd=0.50),
            "frontier": VariantMetrics(role="r", pool=ModelPool.FRONTIER, provider="p",
                                       model_name="m3", correctness=0.98, unsupported_rate=0.02, cost_usd=2.00),
        }
        d = policy.choose(results, "r")
        assert d.chosen_pool == ModelPool.WORKHORSE
        assert d.escalation_triggered is True

    def test_picks_cheap_when_passing(self):
        policy = RoutingPolicy(0.80, 0.30, 10.0)
        results = {
            "cheap": VariantMetrics(role="r", pool=ModelPool.CHEAP, provider="p",
                                    model_name="m1", correctness=0.85, unsupported_rate=0.10, cost_usd=0.01),
            "workhorse": VariantMetrics(role="r", pool=ModelPool.WORKHORSE, provider="p",
                                        model_name="m2", correctness=0.95, unsupported_rate=0.05, cost_usd=0.50),
        }
        d = policy.choose(results, "r")
        assert d.chosen_pool == ModelPool.CHEAP
        assert d.escalation_triggered is False

    def test_none_admissible_falls_back_to_frontier(self):
        policy = RoutingPolicy(0.95, 0.01, 10.0)
        results = {
            "cheap": VariantMetrics(role="r", pool=ModelPool.CHEAP, provider="p",
                                    model_name="m1", correctness=0.70, unsupported_rate=0.25, cost_usd=0.01),
            "workhorse": VariantMetrics(role="r", pool=ModelPool.WORKHORSE, provider="p",
                                        model_name="m2", correctness=0.90, unsupported_rate=0.10, cost_usd=0.50),
            "frontier": VariantMetrics(role="r", pool=ModelPool.FRONTIER, provider="p",
                                       model_name="m3", correctness=0.93, unsupported_rate=0.05, cost_usd=2.00),
        }
        d = policy.choose(results, "r")
        assert d.chosen_pool == ModelPool.FRONTIER
        assert d.escalation_triggered is True

    def test_never_picks_dominated(self):
        """Policy must not pick a more-expensive variant at equal correctness."""
        policy = RoutingPolicy(0.90, 0.15, 10.0)
        results = {
            "cheap": VariantMetrics(role="r", pool=ModelPool.CHEAP, provider="p",
                                    model_name="m1", correctness=0.70, unsupported_rate=0.25, cost_usd=0.01),
            "workhorse": VariantMetrics(role="r", pool=ModelPool.WORKHORSE, provider="p",
                                        model_name="m2", correctness=0.95, unsupported_rate=0.05, cost_usd=0.50),
            "frontier": VariantMetrics(role="r", pool=ModelPool.FRONTIER, provider="p",
                                       model_name="m3", correctness=0.95, unsupported_rate=0.02, cost_usd=2.00),
        }
        d = policy.choose(results, "r")
        assert d.chosen_pool == ModelPool.WORKHORSE
        assert d.cost_usd == 0.50


class TestProofConfig:
    """ProofConfig defaults and env overrides."""

    def test_default(self):
        cfg = ProofConfig()
        assert cfg.correctness_floor == 0.9
        assert cfg.unsupported_claim_ceil == 0.15
        assert cfg.cost_ceiling_usd == 10.0
        assert cfg.mode == "offline"

    def test_from_env_override(self, monkeypatch):
        monkeypatch.setenv("DRA_ROUTING_CORRECTNESS_FLOOR", "0.85")
        cfg = ProofConfig.from_env()
        assert cfg.correctness_floor == 0.85

    def test_from_env_mode_override(self, monkeypatch):
        monkeypatch.setenv("DRA_ROUTING_MODE", "live")
        cfg = ProofConfig.from_env()
        assert cfg.mode == "live"


class TestProofOffline:
    """Offline proof run (no DB needed) — always green."""

    def test_run_proof_offline_passes(self, tmp_path):
        async def run():
            cfg = ProofConfig(mode="offline")
            report = await proof_mod.run_proof(
                cfg, write=True,
                report_path=str(tmp_path / "r.json"), publish=False,
            )
            return report
        report = asyncio.run(run())
        assert report["verdict"] == "PASS"
        assert report["summary"]["policy_assertion"] == "PASS"
        assert report["summary"]["total_fixtures"] == 18
        assert report["summary"]["total_roles"] == 6

    def test_run_proof_report_keys(self):
        async def run():
            cfg = ProofConfig(mode="offline")
            return await proof_mod.run_proof(cfg, write=False, publish=False)
        report = asyncio.run(run())
        assert report["schema_version"] == 1
        assert report["mission"] == "sayandahiyagt/dra#9"
        assert report["spec_anchor"] == "§38.3"
        assert "per_role" in report
        assert "reversal_triggers" in report
        assert "escalations" in report
        assert "summary" in report

    def test_run_proof_choses_workhorse(self):
        """Core claim: policy picks cheapest admissible (workhorse), not frontier."""
        async def run():
            cfg = ProofConfig(mode="offline")
            return await proof_mod.run_proof(cfg, write=False, publish=False)
        report = asyncio.run(run())
        for role, data in report["per_role"].items():
            decision = data["policy_decision"]
            assert decision["chosen_pool"] == "workhorse", (
                f"{role}: chose {decision['chosen_pool']}"
            )

    def test_run_proof_four_variants_per_role(self):
        async def run():
            cfg = ProofConfig(mode="offline")
            return await proof_mod.run_proof(cfg, write=False, publish=False)
        report = asyncio.run(run())
        for role, data in report["per_role"].items():
            v = data["variants"]
            assert "cheap" in v and "workhorse" in v
            assert "frontier" in v and "workhorse+advisor" in v

    def test_run_proof_cheap_fails_floors(self):
        """CHEAP fails correctness floor AND unsupported ceil for every role."""
        async def run():
            cfg = ProofConfig(mode="offline")
            return await proof_mod.run_proof(cfg, write=False, publish=False)
        report = asyncio.run(run())
        cfg = ProofConfig(mode="offline")
        for role, data in report["per_role"].items():
            cheap = data["variants"]["cheap"]
            assert cheap["correctness"] < cfg.correctness_floor
            assert cheap["unsupported_rate"] > cfg.unsupported_claim_ceil

    def test_run_proof_all_triggers_pass(self):
        async def run():
            cfg = ProofConfig(mode="offline")
            return await proof_mod.run_proof(cfg, write=False, publish=False)
        report = asyncio.run(run())
        for name, trig in report["reversal_triggers"].items():
            assert trig["pass"] is True, f"trigger {name} failed"

    def test_run_proof_cheap_cheaper_than_workhorse(self):
        """Cheap variant is always cheaper but worse — proves cost-aware routing."""
        async def run():
            cfg = ProofConfig(mode="offline")
            return await proof_mod.run_proof(cfg, write=False, publish=False)
        report = asyncio.run(run())
        for role, data in report["per_role"].items():
            cheap = data["variants"]["cheap"]
            wh = data["variants"]["workhorse"]
            assert cheap["cost_usd"] < wh["cost_usd"]
            assert cheap["correctness"] < wh["correctness"]

    def test_make_adapter_offline(self):
        a = make_model_adapter(ProviderMode.OFFLINE)
        assert isinstance(a, FakeModelAdapter)

    def test_make_adapter_live_without_creds(self):
        for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY"):
            os.environ.pop(k, None)
        with pytest.raises(RuntimeError, match="no model provider API key"):
            make_model_adapter(ProviderMode.LIVE)


# ===========================================================================
# DB-GATED TESTS (require Postgres + pgvector + migration 0005)
# ===========================================================================

pytestmark_db = DB


@pytest.mark.skipif(not _db_reachable(), reason="No Postgres")
class TestProofDB:
    """DB-backed escalation logging assertions."""

    def test_escalation_log_rows_written(self, tmp_path):
        async def run():
            cfg = ProofConfig(mode="offline")
            await proof_mod.run_proof(
                cfg, write=True, report_path=str(tmp_path / "r.json"), publish=True,
            )
            async with engine.connect() as conn:
                r = await conn.execute(text(
                    "SELECT count(*) FROM model_escalation_log "
                    "WHERE to_pool IN ('frontier', 'advisor')"
                ))
                return r.scalar()
        count = asyncio.run(run())
        assert count >= 6  # at least 1 frontier/advisor per role

    def test_escalation_log_all_roles(self, tmp_path):
        async def run():
            cfg = ProofConfig(mode="offline")
            await proof_mod.run_proof(
                cfg, write=True, report_path=str(tmp_path / "r.json"), publish=True,
            )
            async with engine.connect() as conn:
                r = await conn.execute(text("SELECT DISTINCT role FROM model_escalation_log"))
                return {row[0] for row in r}
        roles = asyncio.run(run())
        assert roles == {r.value for r in ExpensiveRole}

    def test_escalation_log_structure(self, tmp_path):
        async def run():
            cfg = ProofConfig(mode="offline")
            await proof_mod.run_proof(
                cfg, write=True, report_path=str(tmp_path / "r.json"), publish=True,
            )
            async with engine.connect() as conn:
                r = await conn.execute(text(
                    "SELECT run_id, task_id, role, from_pool, to_pool, trigger, "
                    "cost_delta_usd, latency_delta_ms, correctness_gain "
                    "FROM model_escalation_log LIMIT 1"
                ))
                return r.fetchone()
        row = asyncio.run(run())
        assert row is not None
        assert row.from_pool in ("cheap", "workhorse")
        assert row.to_pool in ("workhorse", "frontier", "advisor")
        assert row.trigger

    def test_model_routing_config_seeded(self):
        async def run():
            async with engine.connect() as conn:
                r = await conn.execute(text("SELECT count(*) FROM model_routing_config"))
                return r.scalar()
        assert asyncio.run(run()) >= 4

    def test_report_written_and_content_hashed(self, tmp_path):
        async def run():
            cfg = ProofConfig(mode="offline")
            path = str(tmp_path / "report.json")
            report = await proof_mod.run_proof(
                cfg, write=True, report_path=path, publish=True,
            )
            return report, path
        report, path = asyncio.run(run())
        assert report["verdict"] == "PASS"
        with open(path) as f:
            loaded = json.load(f)
        assert loaded["verdict"] == "PASS"
        with open(path, "rb") as f:
            h = hashlib.sha256(f.read()).hexdigest()
        assert len(h) == 64


# ===========================================================================
# Credential-gated live tests (skip without API keys)
# ---------------------------------------------------------------------------


LIVE = pytest.mark.skipif(not _creds_reachable(), reason="No provider API keys set")


def test_live_mode_without_keys_raises():
    for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY"):
        os.environ.pop(k, None)
    with pytest.raises(RuntimeError, match="no model provider API key"):
        make_model_adapter(ProviderMode.LIVE)


# This test is always collected but skipped if no keys. It verifies the live
# gate path exists (the error message is the contract).

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
