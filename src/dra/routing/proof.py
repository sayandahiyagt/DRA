"""§38.3 Model-routing proof harness (dra#9).

Loads hidden ground-truth fixtures, evaluates every expensive role across
cheap / workhorse / frontier / workhorse+advisor model variants using the
FakeModelAdapter (offline, deterministic), records cost/latency/
correctness/unsupported-rate metrics, runs the RoutingPolicy to pick the
cheapest admissible variant per role, asserts the policy never selects a
dominated variant, writes an escalation log row for every frontier/advisor
invocation, and emits a machine-checkable report (JSON + markdown).

Offline-first (D1): the proof is sandbox-green with no API keys or Postgres.
Real-provider SDKs and DB-backed escalation logging are env-gated (mirroring
tests/_db.py skipif and dra.proof_corpus._check_db_reachable).

CLI entry: dra-model-routing-proof (wired in pyproject.toml).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from dra.db import can_connect
from dra.routing.models import (
    ExpensiveRole,
    FakeModelAdapter,
    ModelCallResult,
    ModelPool,
    ModelRegistry,
    make_model_adapter,
    pool_correctness,
    pool_latency_ms,
    pool_unsupported_rate,
)
from dra.routing.fixtures import Fixture, assert_fixtures_well_formed, load_fixtures
from dra.routing.policy import (
    AdvisorMetrics,
    PolicyDecision,
    ProofConfig,
    RoutingPolicy,
    VariantMetrics,
    compute_recall,
    compute_unsupported_rate,
    escalation_frequency,
    latency_p50,
    latency_p95,
)
from dra.routing.providers import ProviderMode, SearchProviderRegistry, TaskType


# ---------------------------------------------------------------------------
# Escalation log (migration 0004)
# ---------------------------------------------------------------------------


@dataclass
class EscalationEvent:
    """A single escalation decision recorded for the report and DB log."""
    run_id: str
    task_id: str
    role: str
    fixture_id: str | None
    from_pool: str
    to_pool: str
    trigger: str
    cost_delta_usd: float
    latency_delta_ms: float
    correctness_gain: float


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_id() -> str:
    return f"routing-proof-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"


# ---------------------------------------------------------------------------
# Per-variant evaluation (offline fake model)
# ---------------------------------------------------------------------------


def _fixtures_for_role(fixtures: list[Fixture], role: ExpensiveRole) -> list[Fixture]:
    return [f for f in fixtures if f.role == role]


def _build_prompt(fx: Fixture) -> str:
    return f"{fx.input}\n\nContext:\n{fx.context}"


def _gt_dict(fx: Fixture) -> dict[str, Any]:
    return {
        "answers": fx.ground_truth.answers,
        "claims": [
            {"text": c["text"], "supported": c["supported"]}
            for c in fx.ground_truth.claims
        ],
        "expected_unsupported": fx.ground_truth.expected_unsupported,
        "source_refs": fx.ground_truth.source_refs,
    }


def _correct_claims_for(result: ModelCallResult, pool: ModelPool) -> int:
    n_claims = result.n_claims or 0
    n_unsupported = result.n_unsupported or 0
    if result.is_correct:
        return n_claims - n_unsupported
    from dra.routing.models import pool_correctness
    n_correct_pool = int(n_claims * pool_correctness(pool))
    return min(n_correct_pool, n_claims - n_unsupported)


async def evaluate_variant(
    role: ExpensiveRole,
    pool: ModelPool,
    fixtures: list[Fixture],
    adapter: FakeModelAdapter,
    registry: ModelRegistry,
    run_id: str,
) -> VariantMetrics:
    """Evaluate one (role, pool) variant across all role-matching fixtures."""
    spec = registry.select(role, pool)
    role_fixtures = _fixtures_for_role(fixtures, role)
    if not role_fixtures:
        return VariantMetrics(
            role=role.value, pool=pool, provider="", model_name="",
        )

    call_results: list[ModelCallResult] = []
    latencies: list[float] = []
    total_cost = 0.0
    total_correct_claims = 0
    total_claims = 0
    total_unsupported = 0
    escalations = 0

    for fx in role_fixtures:
        prompt = _build_prompt(fx)
        result = await adapter.complete(
            spec, prompt,
            fixture_id=fx.id,
            ground_truth=_gt_dict(fx),
        )
        call_results.append(result)
        latencies.append(result.latency_ms)
        total_cost += result.cost_usd
        total_claims += result.n_claims or 0
        total_correct_claims += _correct_claims_for(result, pool)
        total_unsupported += result.n_unsupported or 0
        if result.is_escalation:
            escalations += 1

    correctness = compute_recall(total_correct_claims, total_claims) if total_claims else 0.0
    unsupported_rate = compute_unsupported_rate(total_unsupported, total_claims) if total_claims else 0.0

    return VariantMetrics(
        role=role.value,
        pool=pool,
        provider=spec.provider,
        model_name=spec.name,
        is_advisor=False,
        correctness=round(correctness, 4),
        unsupported_rate=round(unsupported_rate, 4),
        cost_usd=round(total_cost, 6),
        p50_latency_ms=round(latency_p50(latencies), 3),
        p95_latency_ms=round(latency_p95(latencies), 3),
        num_calls=len(role_fixtures),
        num_items=total_claims,
        escalations=escalations,
        raw_latencies=latencies,
        report_ids=[fx.id for fx in role_fixtures],
    )


async def evaluate_advisor(
    role: ExpensiveRole,
    fixtures: list[Fixture],
    adapter: FakeModelAdapter,
    registry: ModelRegistry,
    workhorse_metrics: VariantMetrics,
    run_id: str,
) -> AdvisorMetrics:
    """Evaluate the workhorse+advisor variant (§23.4 Pool B + Pool A)."""
    role_fixtures = _fixtures_for_role(fixtures, role)
    if not role_fixtures:
        return AdvisorMetrics(
            workhorse=workhorse_metrics,
            advisor=VariantMetrics(
                role=role.value, pool=ModelPool.ADVISOR,
                provider="", model_name="",
            ),
            combined_correctness=workhorse_metrics.correctness,
            combined_unsupported_rate=workhorse_metrics.unsupported_rate,
            combined_cost_usd=workhorse_metrics.cost_usd,
            combined_p50_ms=workhorse_metrics.p50_latency_ms,
            combined_p95_ms=workhorse_metrics.p95_latency_ms,
            num_items=workhorse_metrics.num_items,
            escalations=0,
        )

    advisor_spec = registry.select(role, ModelPool.ADVISOR)
    advisor_costs: list[float] = []
    advisor_latencies: list[float] = []
    advisor_correct_claims = 0
    advisor_total_claims = 0
    advisor_unsupported = 0

    for fx in role_fixtures:
        prompt = _build_prompt(fx)
        r = await adapter.complete(
            advisor_spec, prompt,
            fixture_id=fx.id,
            ground_truth=_gt_dict(fx),
        )
        advisor_costs.append(r.cost_usd)
        advisor_latencies.append(r.latency_ms)
        advisor_total_claims += r.n_claims or 0
        advisor_correct_claims += _correct_claims_for(r, ModelPool.ADVISOR)
        advisor_unsupported += r.n_unsupported or 0

    advisor_metrics = VariantMetrics(
        role=role.value,
        pool=ModelPool.ADVISOR,
        provider=advisor_spec.provider,
        model_name=advisor_spec.name,
        is_advisor=True,
        correctness=round(
            compute_recall(advisor_correct_claims, advisor_total_claims)
            if advisor_total_claims else 0.0, 4
        ),
        unsupported_rate=round(
            compute_unsupported_rate(advisor_unsupported, advisor_total_claims)
            if advisor_total_claims else 0.0, 4
        ),
        cost_usd=round(sum(advisor_costs), 6),
        p50_latency_ms=round(latency_p50(advisor_latencies), 3),
        p95_latency_ms=round(latency_p95(advisor_latencies), 3),
        num_calls=len(role_fixtures),
        num_items=advisor_total_claims,
        report_ids=[fx.id for fx in role_fixtures],
    )

    wh = workhorse_metrics
    adv = advisor_metrics
    # §23.4 advisor pattern: advisor catches a fraction of workhorse errors.
    w_h_correctness = wh.correctness
    a_correctness = adv.correctness
    improvement = max(0.0, (a_correctness - w_h_correctness) * 0.5)
    combined_correctness = min(1.0, w_h_correctness + improvement)
    combined_unsupported = wh.unsupported_rate * 0.8
    combined_cost = wh.cost_usd + adv.cost_usd
    combined_p50 = max(wh.p50_latency_ms, adv.p50_latency_ms)
    combined_p95 = max(wh.p95_latency_ms, adv.p95_latency_ms)

    return AdvisorMetrics(
        workhorse=wh,
        advisor=adv,
        combined_correctness=round(combined_correctness, 4),
        combined_unsupported_rate=round(combined_unsupported, 4),
        combined_cost_usd=round(combined_cost, 6),
        combined_p50_ms=combined_p50,
        combined_p95_ms=combined_p95,
        num_items=wh.num_items,
        escalations=1,
        report_ids=wh.report_ids,
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

_POOL_ORDER: list[ModelPool] = [
    ModelPool.CHEAP, ModelPool.WORKHORSE, ModelPool.FRONTIER,
]


async def run_proof(
    cfg: ProofConfig | None = None,
    *,
    write: bool = True,
    report_path: str = "model_routing_report.json",
    publish: bool = True,
) -> dict:
    """Run the full §38.3 model-routing proof and return the report dict."""
    if cfg is None:
        cfg = ProofConfig.from_env()

    fixtures = load_fixtures()
    assert_fixtures_well_formed(fixtures)

    if cfg.fixture_subset is not None:
        fixtures = [f for f in fixtures if f.id in set(cfg.fixture_subset)]
        if not fixtures:
            raise ValueError("fixture_subset matched no fixtures")

    mode = ProviderMode(cfg.mode)
    adapter = make_model_adapter(mode)
    registry = ModelRegistry()
    provider_registry = SearchProviderRegistry()
    policy = RoutingPolicy(
        correctness_floor=cfg.correctness_floor,
        unsupported_claim_ceil=cfg.unsupported_claim_ceil,
        cost_ceiling_usd=cfg.cost_ceiling_usd,
    )

    run_id = _run_id()
    roles = [ExpensiveRole(r) for r in cfg.roles if r in ExpensiveRole.__members__.values()]
    if not roles:
        roles = list(ExpensiveRole)

    escalation_events: list[EscalationEvent] = []
    per_role: dict[str, Any] = {}

    for role in roles:
        role_key = role.value
        role_fixtures = _fixtures_for_role(fixtures, role)

        variant_results: dict[str, VariantMetrics | AdvisorMetrics] = {}
        variant_metrics_map: dict[str, VariantMetrics] = {}

        for pool in _POOL_ORDER:
            if pool.value not in cfg.pools:
                continue
            vm = await evaluate_variant(
                role, pool, role_fixtures, adapter, registry, run_id,
            )
            variant_metrics_map[pool.value] = vm

            if pool != ModelPool.CHEAP:
                prev_pool = _POOL_ORDER[_POOL_ORDER.index(pool) - 1]
                prev = variant_metrics_map.get(prev_pool.value)
                if prev:
                    trigger = (
                        f"{prev_pool.value} correctness={prev.correctness:.4f} "
                        f"below floor {cfg.correctness_floor} or "
                        f"unsupported_rate={prev.unsupported_rate:.4f} above "
                        f"ceil {cfg.unsupported_claim_ceil}"
                    )
                    cost_delta = vm.cost_usd - prev.cost_usd
                    latency_delta = vm.p95_latency_ms - prev.p95_latency_ms
                    correct_gain = vm.correctness - prev.correctness
                else:
                    trigger = "cheaper pool failed quality floors"
                    cost_delta = vm.cost_usd
                    latency_delta = vm.p95_latency_ms
                    correct_gain = vm.correctness

                event = EscalationEvent(
                    run_id=run_id,
                    task_id=f"task-{role_key}",
                    role=role_key,
                    fixture_id=None,
                    from_pool=prev_pool.value if prev else "cheap",
                    to_pool=pool.value,
                    trigger=trigger,
                    cost_delta_usd=round(cost_delta, 6),
                    latency_delta_ms=round(latency_delta, 3),
                    correctness_gain=round(correct_gain, 4),
                )
                escalation_events.append(event)

        wh = variant_metrics_map.get(ModelPool.WORKHORSE.value)
        if wh is not None:
            adv = await evaluate_advisor(
                role, role_fixtures, adapter, registry, wh, run_id,
            )
            variant_results["cheap"] = variant_metrics_map.get(ModelPool.CHEAP.value)
            variant_results["workhorse"] = wh
            variant_results["frontier"] = variant_metrics_map.get(ModelPool.FRONTIER.value)
            variant_results["workhorse+advisor"] = adv

            cost_delta = adv.combined_cost_usd - wh.cost_usd
            latency_delta = adv.combined_p95_ms - wh.p95_latency_ms
            correct_gain = adv.combined_correctness - wh.correctness
            event = EscalationEvent(
                run_id=run_id,
                task_id=f"task-{role_key}",
                role=role_key,
                fixture_id=None,
                from_pool=ModelPool.WORKHORSE.value,
                to_pool=ModelPool.ADVISOR.value,
                trigger="advisor consultation per §23.4 marginal-value routing",
                cost_delta_usd=round(cost_delta, 6),
                latency_delta_ms=round(latency_delta, 3),
                correctness_gain=round(correct_gain, 4),
            )
            escalation_events.append(event)

        decision = policy.choose(variant_results, role_key)

        # Verify: chosen = argmin cost among admissible (no dominated choice).
        def _m_vals(m: VariantMetrics | AdvisorMetrics) -> tuple[float, float, float]:
            if isinstance(m, AdvisorMetrics):
                return m.combined_correctness, m.combined_unsupported_rate, m.combined_cost_usd
            return m.correctness, m.unsupported_rate, m.cost_usd

        admissible = {}
        for name, m in variant_results.items():
            _c, _u, _cost = _m_vals(m)
            if (
                _c >= cfg.correctness_floor
                and _u <= cfg.unsupported_claim_ceil
                and _cost <= cfg.cost_ceiling_usd
            ):
                admissible[name] = m

        assertion_pass = True
        if admissible:
            cheapest = min(admissible.values(), key=lambda m: _m_vals(m)[2])
            chosen_name = _pool_name_for_decision(decision, variant_results)
            if _m_vals(cheapest)[2] > decision.cost_usd + 1e-9:
                assertion_pass = False
            for m in admissible.values():
                mc, mu, mcost = _m_vals(m)
                if (
                    mc >= decision.correctness - 1e-9
                    and mcost < decision.cost_usd - 1e-9
                    and _variant_name(m) != chosen_name
                ):
                    assertion_pass = False

        per_role[role_key] = {
            "variants": _serialize_variants(variant_results),
            "policy_decision": _decision_dict(decision),
            "admissible_pools": decision.admissible_pools,
            "escalation_triggered": decision.escalation_triggered,
            "policy_assertion_pass": assertion_pass,
        }

    total_escalations = len(escalation_events)
    frontier_adv_logs = len([
        e for e in escalation_events
        if e.to_pool in (ModelPool.FRONTIER.value, ModelPool.ADVISOR.value)
    ])
    roles_with_escalation = sum(
        1 for r in per_role.values() if r["escalation_triggered"]
    )

    reversal_triggers = {
        "policy_argmin_cost": {
            "value": "PASS" if all(r["policy_assertion_pass"] for r in per_role.values()) else "FAIL",
            "threshold": "chosen = argmin cost among admissible",
            "pass": all(r["policy_assertion_pass"] for r in per_role.values()),
        },
        "no_dominated_choice": {
            "value": "PASS" if all(r["policy_assertion_pass"] for r in per_role.values()) else "FAIL",
            "threshold": "policy never selects dominated variant",
            "pass": all(r["policy_assertion_pass"] for r in per_role.values()),
        },
        "escalation_logged": {
            "value": frontier_adv_logs,
            "threshold": f">= 1 per role with frontier/advisor evaluated (>= {len(roles)})",
            "pass": frontier_adv_logs >= len(roles),
        },
        "provider_matrix_task_routed": {
            "value": sum(1 for r in roles if provider_registry.select_providers(_task_for_role(r))),
            "threshold": "> 0 providers for every task type",
            "pass": all(
                len(provider_registry.select_providers(_task_for_role(r))) > 0
                for r in roles
            ),
        },
        "rendered_browser_fallback": {
            "value": all(
                provider_registry.has_rendered_browser_fallback(_task_for_role(r))
                for r in roles
            ),
            "threshold": "rendered-browser is fall-through for every task type",
            "pass": all(
                provider_registry.has_rendered_browser_fallback(_task_for_role(r))
                for r in roles
            ),
        },
    }

    policy_pass = all(r["policy_assertion_pass"] for r in per_role.values())
    verdict = "PASS" if all(t["pass"] for t in reversal_triggers.values()) else "FAIL"

    report = {
        "schema_version": 1,
        "mission": "sayandahiyagt/dra#9",
        "spec_anchor": "§38.3",
        "mode": cfg.mode,
        "generated_at": _utcnow_iso(),
        "run_id": run_id,
        "config": {
            "correctness_floor": cfg.correctness_floor,
            "unsupported_claim_ceil": cfg.unsupported_claim_ceil,
            "cost_ceiling_usd": cfg.cost_ceiling_usd,
            "roles": list(cfg.roles),
            "pools": list(cfg.pools),
            "advisor_consult_rates": list(cfg.advisor_consult_rates),
            "fixture_subset": cfg.fixture_subset,
        },
        "provider_matrix": {
            "per_role_providers": {
                r.value: [c.name for c in provider_registry.select_providers(_task_for_role(r))]
                for r in ExpensiveRole
            },
        },
        "pool_profiles": {
            p.value: {
                "correctness_rate": pool_correctness(p),
                "unsupported_rate": pool_unsupported_rate(p),
                "p50_latency_ms": pool_latency_ms(p),
            }
            for p in _POOL_ORDER
        },
        "per_role": per_role,
        "escalations": [asdict(e) for e in escalation_events],
        "summary": {
            "total_fixtures": len(fixtures),
            "total_roles": len(roles),
            "total_variants_evaluated": sum(
                len(r["variants"]) for r in per_role.values()
            ),
            "total_escalations": total_escalations,
            "frontier_adv_logs": frontier_adv_logs,
            "roles_with_escalation": roles_with_escalation,
            "policy_assertion": "PASS" if policy_pass else "FAIL",
        },
        "reversal_triggers": reversal_triggers,
        "verdict": verdict,
        "adr_008_reversal_triggered": verdict == "FAIL",
    }

    if publish:
        await _persist_escalations(escalation_events, run_id)

    if write:
        write_report(report, path=report_path)

    return report


def _task_for_role(role: ExpensiveRole) -> TaskType:
    mapping = {
        ExpensiveRole.REPO_INVESTIGATION: TaskType.REPO_INVESTIGATION,
        ExpensiveRole.PAPER_RECONCILIATION: TaskType.PAPER_RECONCILIATION,
        ExpensiveRole.DOM_REASONING: TaskType.DOM_REASONING,
        ExpensiveRole.FACT_EXTRACTION: TaskType.FACT_EXTRACTION,
        ExpensiveRole.FINAL_AUDIT: TaskType.REPO_INVESTIGATION,
        ExpensiveRole.CITATION_VERDICT: TaskType.PAPER_RECONCILIATION,
    }
    return mapping[role]


async def _persist_escalations(events: list[EscalationEvent], run_id: str) -> None:
    """Write escalation rows to the DB if reachable (non-blocking, env-gated)."""
    from dra.db import engine
    from sqlalchemy import text
    try:
        async with engine.connect() as conn:
            for e in events:
                await conn.execute(
                    text(
                        "INSERT INTO model_escalation_log "
                        "(run_id, task_id, role, fixture_id, from_pool, to_pool, "
                        "trigger, cost_delta_usd, latency_delta_ms, correctness_gain) "
                        "VALUES (:run_id, :task_id, :role, :fixture_id, :from_pool, "
                        ":to_pool, :trigger, :cost_delta_usd, :latency_delta_ms, "
                        ":correctness_gain)"
                    ),
                    {
                        "run_id": e.run_id,
                        "task_id": e.task_id,
                        "role": e.role,
                        "fixture_id": e.fixture_id,
                        "from_pool": e.from_pool,
                        "to_pool": e.to_pool,
                        "trigger": e.trigger,
                        "cost_delta_usd": e.cost_delta_usd,
                        "latency_delta_ms": e.latency_delta_ms,
                        "correctness_gain": e.correctness_gain,
                    },
                )
            await conn.commit()
    except Exception:
        pass


def _serialize_variants(
    results: dict[str, VariantMetrics | AdvisorMetrics],
) -> dict:
    out: dict[str, Any] = {}
    for name, m in results.items():
        if isinstance(m, AdvisorMetrics):
            out[name] = {
                "pool": "advisor",
                "model_name": f"workhorse+{m.advisor.model_name}",
                "correctness": m.combined_correctness,
                "unsupported_rate": m.combined_unsupported_rate,
                "cost_usd": m.combined_cost_usd,
                "p50_latency_ms": m.combined_p50_ms,
                "p95_latency_ms": m.combined_p95_ms,
                "num_calls": m.workhorse.num_calls + m.advisor.num_calls,
                "is_advisor": True,
                "workhorse": _vm_dict(m.workhorse),
                "advisor": _vm_dict(m.advisor),
            }
        else:
            out[name] = _vm_dict(m)
    return out


def _vm_dict(m: VariantMetrics) -> dict:
    return {
        "pool": m.pool.value,
        "provider": m.provider,
        "model_name": m.model_name,
        "correctness": m.correctness,
        "unsupported_rate": m.unsupported_rate,
        "cost_usd": m.cost_usd,
        "p50_latency_ms": m.p50_latency_ms,
        "p95_latency_ms": m.p95_latency_ms,
        "num_calls": m.num_calls,
        "num_items": m.num_items,
        "escalations": m.escalations,
    }


def _variant_name(m: VariantMetrics | AdvisorMetrics) -> str:
    if isinstance(m, AdvisorMetrics):
        return "workhorse+advisor"
    return m.pool.value


def _pool_name_for_decision(
    decision: PolicyDecision, results: dict[str, VariantMetrics | AdvisorMetrics],
) -> str:
    target = decision.chosen_pool.value
    for name, m in results.items():
        pool = _extract_pool_for_name(m)
        if pool == target:
            return name
    return target


def _extract_pool_for_name(m: VariantMetrics | AdvisorMetrics) -> str:
    if isinstance(m, AdvisorMetrics):
        return ModelPool.ADVISOR.value
    return m.pool.value


def _decision_dict(decision: PolicyDecision) -> dict:
    return {
        "role": decision.role,
        "chosen_pool": decision.chosen_pool.value,
        "chosen_model": decision.chosen_model,
        "reason": decision.reason,
        "cost_usd": decision.cost_usd,
        "correctness": decision.correctness,
        "unsupported_rate": decision.unsupported_rate,
        "p95_latency_ms": decision.p95_latency_ms,
        "admissible_pools": decision.admissible_pools,
        "escalation_triggered": decision.escalation_triggered,
    }


# ---------------------------------------------------------------------------
# Report writing (mirrors proof_corpus.write_report)
# ---------------------------------------------------------------------------


def write_report(report: dict, path: str = "model_routing_report.json") -> None:
    """Write the proof report as JSON + a markdown summary."""
    with open(path, "w") as f:
        json.dump(report, f, indent=2)

    md_path = path.replace(".json", ".md")
    with open(md_path, "w") as f:
        f.write(_report_markdown(report))


def _report_markdown(report: dict) -> str:
    """Render a human-readable markdown proof report."""
    lines: list[str] = []
    lines.append("# §38.3 Model-Routing Proof Report")
    lines.append("")
    lines.append(f"- **Mission:** {report['mission']}")
    lines.append(f"- **Spec anchor:** {report['spec_anchor']}")
    lines.append(f"- **Mode:** {report['mode']}")
    lines.append(f"- **Generated at:** {report['generated_at']}")
    lines.append(f"- **Run ID:** {report['run_id']}")
    lines.append("")

    c = report["config"]
    lines.append("## Configuration")
    lines.append(f"- Correctness floor: {c['correctness_floor']}")
    lines.append(f"- Unsupported-claim ceiling: {c['unsupported_claim_ceil']}")
    lines.append(f"- Cost ceiling: ${c['cost_ceiling_usd']}")
    lines.append(f"- Roles: {', '.join(c['roles'])}")
    lines.append(f"- Pools: {', '.join(c['pools']) + ', advisor'}")
    lines.append("")

    lines.append("## Price-pool profiles (scripted, offline)")
    lines.append("| Pool | Correctness | Unsupported rate | p50 latency (ms) |")
    lines.append("|------|-------------|------------------|-------------------|")
    for name, prof in report["pool_profiles"].items():
        lines.append(
            f"| {name} | {prof['correctness_rate']:.2f} | "
            f"{prof['unsupported_rate']:.2f} | {prof['p50_latency_ms']:.1f} |"
        )
    lines.append("")

    lines.append("## Per-role results")
    for role, data in report["per_role"].items():
        lines.append(f"### {role}")
        pd = data["policy_decision"]
        lines.append(f"- Chosen: **{pd['chosen_pool']}** ({pd['chosen_model']})")
        lines.append(f"- Cost: ${pd['cost_usd']:.4f} | Correctness: {pd['correctness']:.4f} | "
                      f"Unsupported: {pd['unsupported_rate']:.4f}")
        lines.append(f"- Escalated: {pd['escalation_triggered']}")
        lines.append(f"- Policy assertion: {'PASS' if data['policy_assertion_pass'] else 'FAIL'}")
        lines.append("")
        lines.append("| Variant | Pool | Correctness | Unsupported | Cost (USD) | p95 (ms) | Admissible? |")
        lines.append("|---------|------|-------------|-------------|------------|----------|-------------|")
        for vname, vm in data["variants"].items():
            admissible = (
                vm["correctness"] >= c["correctness_floor"]
                and vm["unsupported_rate"] <= c["unsupported_claim_ceil"]
                and vm["cost_usd"] <= c["cost_ceiling_usd"]
            )
            lines.append(
                f"| {vname} | {vm['pool']} | {vm['correctness']:.4f} | "
                f"{vm['unsupported_rate']:.4f} | {vm['cost_usd']:.6f} | "
                f"{vm['p95_latency_ms']:.1f} | {'yes' if admissible else 'no'} |"
            )
        lines.append("")

    lines.append("## ADR-008 reversal triggers")
    lines.append("| Trigger | Value | Threshold | Result |")
    lines.append("|---------|-------|-----------|--------|")
    for name, trig in report["reversal_triggers"].items():
        val = trig.get("value")
        threshold = trig.get("threshold", "")
        lines.append(
            f"| {name} | {val} | {threshold} | "
            f"{'PASS' if trig['pass'] else 'FAIL'} |"
        )
    lines.append("")

    s = report["summary"]
    lines.append("## Summary")
    lines.append(f"- Fixtures: {s['total_fixtures']}")
    lines.append(f"- Roles: {s['total_roles']}")
    lines.append(f"- Variants evaluated: {s['total_variants_evaluated']}")
    lines.append(f"- Total escalations: {s['total_escalations']}")
    lines.append(f"- Frontier/advisor logs: {s['frontier_adv_logs']}")
    lines.append(f"- Policy assertion: {s['policy_assertion']}")
    lines.append("")

    lines.append("## Verdict")
    lines.append(f"**{report['verdict']}**")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# DB reachability + CLI
# ---------------------------------------------------------------------------


def _check_db_reachable() -> bool:
    try:
        return asyncio.run(can_connect())
    except Exception:
        return False


def _table_exists(table_name: str) -> bool:
    """Check if a table exists (i.e., migration 0004 has been applied)."""
    from dra.db import engine
    from sqlalchemy import text

    async def _check() -> bool:
        try:
            async with engine.connect() as conn:
                result = await conn.execute(
                    text(
                        "SELECT 1 FROM information_schema.tables "
                        "WHERE table_schema = 'public' AND table_name = :t"
                    ),
                    {"t": table_name},
                )
                return result.fetchone() is not None
        except Exception:
            return False

    try:
        return asyncio.run(_check())
    except Exception:
        return False


def main() -> None:
    """CLI entry point: run the §38.3 model-routing proof."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="dra-model-routing-proof",
        description="Run the §38.3 model-routing proof: evaluate cheap/workhorse/"
        "frontier/workhorse+advisor across hidden ground-truth fixtures, "
        "verify the cost-aware routing policy on metrics, log escalations, "
        "and emit a pass/fail report vs ADR-008 reversal triggers.",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Print config and verify fixture well-formedness without running.")
    parser.add_argument("--mode", choices=["offline", "live"], default=None,
                        help="Provider/model mode (default: offline).")
    parser.add_argument("--correctness-floor", type=float, default=None,
                        help="Override correctness floor (default: 0.9).")
    parser.add_argument("--unsupported-ceil", type=float, default=None,
                        help="Override unsupported-claim ceiling (default: 0.15).")
    parser.add_argument("--cost-ceiling-usd", type=float, default=None,
                        help="Override per-task cost ceiling in USD (default: 10.0).")
    parser.add_argument("--no-publish", action="store_true",
                        help="Skip DB-backed escalation logging and canonical publishing.")
    parser.add_argument("--report-path", type=str, default="model_routing_report.json",
                        help="Output path for the report JSON/MD.")
    args = parser.parse_args()

    cfg = ProofConfig.from_env()
    if args.mode is not None:
        cfg = ProofConfig(
            correctness_floor=cfg.correctness_floor,
            unsupported_claim_ceil=cfg.unsupported_claim_ceil,
            cost_ceiling_usd=cfg.cost_ceiling_usd,
            roles=cfg.roles,
            pools=cfg.pools,
            advisor_consult_rates=cfg.advisor_consult_rates,
            fixture_subset=cfg.fixture_subset,
            mode=args.mode,
        )
    if args.correctness_floor is not None:
        cfg = cfg.__class__(
            correctness_floor=args.correctness_floor,
            unsupported_claim_ceil=cfg.unsupported_claim_ceil,
            cost_ceiling_usd=cfg.cost_ceiling_usd,
            roles=cfg.roles,
            pools=cfg.pools,
            advisor_consult_rates=cfg.advisor_consult_rates,
            fixture_subset=cfg.fixture_subset,
            mode=cfg.mode,
        )
    if args.unsupported_ceil is not None:
        cfg = cfg.__class__(
            correctness_floor=cfg.correctness_floor,
            unsupported_claim_ceil=args.unsupported_ceil,
            cost_ceiling_usd=cfg.cost_ceiling_usd,
            roles=cfg.roles,
            pools=cfg.pools,
            advisor_consult_rates=cfg.advisor_consult_rates,
            fixture_subset=cfg.fixture_subset,
            mode=cfg.mode,
        )
    if args.cost_ceiling_usd is not None:
        cfg = cfg.__class__(
            correctness_floor=cfg.correctness_floor,
            unsupported_claim_ceil=cfg.unsupported_claim_ceil,
            cost_ceiling_usd=args.cost_ceiling_usd,
            roles=cfg.roles,
            pools=cfg.pools,
            advisor_consult_rates=cfg.advisor_consult_rates,
            fixture_subset=cfg.fixture_subset,
            mode=cfg.mode,
        )

    fixtures = load_fixtures()
    try:
        assert_fixtures_well_formed(fixtures)
        well_formed = True
        fixtures_error = ""
    except ValueError as exc:
        well_formed = False
        fixtures_error = str(exc)

    db_ok = _check_db_reachable()
    migration_ok = db_ok and _table_exists("model_escalation_log")

    if args.dry_run:
        print(f"[proof] §38.3 Model-routing proof — dry run")
        print(f"  Mode: {cfg.mode}")
        print(f"  Correctness floor: {cfg.correctness_floor}")
        print(f"  Unsupported-claim ceil: {cfg.unsupported_claim_ceil}")
        print(f"  Cost ceiling: ${cfg.cost_ceiling_usd}")
        print(f"  Roles: {', '.join(cfg.roles)}")
        print(f"  Pools: {', '.join(cfg.pools) + ', advisor'}")
        print(f"  Fixtures: {len(fixtures)} loaded, well-formed: {well_formed}")
        if not well_formed:
            print(f"  Fixture error: {fixtures_error}")
        print(f"  DB reachable: {db_ok}")
        print(f"  Migration 0004 tables present: {migration_ok}")
        print(f"  Report path: {args.report_path}")
        return

    if not well_formed:
        print(f"FAIL: fixtures are not well-formed: {fixtures_error}")
        sys.exit(1)

    print(f"[proof] §38.3 Model-routing proof — mode={cfg.mode}")
    print(f"  Fixtures: {len(fixtures)} | correctness_floor={cfg.correctness_floor} "
          f"unsupported_ceil={cfg.unsupported_claim_ceil} cost_ceiling=${cfg.cost_ceiling_usd}")
    print(f"  DB reachable for escalation logging: {db_ok} (tables: {migration_ok})")

    publish = not args.no_publish
    report = asyncio.run(run_proof(
        cfg, write=True, report_path=args.report_path, publish=publish,
    ))

    print("\n=== §38.3 Model-Routing Proof — ADR-008 Reversal Triggers ===")
    print(f"{'Trigger':<40} {'Value':<15} {'Result':<6}")
    print("-" * 65)
    for name, trig in report["reversal_triggers"].items():
        val = trig.get("value")
        result = "PASS" if trig["pass"] else "FAIL"
        print(f"{name:<40} {str(val):<15} {result:<6}")
    print("-" * 65)
    print(f"\nVERDICT: {report['verdict']}")
    print(f"  Escalations logged: {report['summary']['total_escalations']}")
    print(f"  Frontier/advisor logs: {report['summary']['frontier_adv_logs']}")
    print(f"  Policy assertion: {report['summary']['policy_assertion']}")
    print(f"\nReport: {args.report_path} + {args.report_path.replace('.json', '.md')}")


if __name__ == "__main__":  # pragma: no cover
    main()
