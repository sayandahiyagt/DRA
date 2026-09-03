"""Selection policy + pure metric functions (§38.3 D6/D7, §31, §31.2).

Pure, dependency-free functions for the §38.3 model-routing proof:

  - ``compute_recall``           — |correct| / |items| (per-fixture correctness).
  - ``compute_unsupported_rate`` — |claims not backed by source| / |claims| (§38.3).
  - ``cost_of``                  — USD from prompt/completion tokens + pricing.
  - ``latency_p50`` / ``latency_p95`` — percentile latencies from raw samples.
  - ``escalation_frequency``     — #escalations / #runs that reached the role.

:class:`RoutingPolicy` consumes the per-role, per-variant metric bundle and
picks the cheapest variant meeting the correctness floor + unsupported-claim
ceiling + cost ceiling (D7). The policy is deterministic and overrides
"feels smarter".
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import Any, Sequence

from dra.routing.models import ModelPool


# ---------------------------------------------------------------------------
# Pure metric functions (§38.3 D6 — unit-testable without DB)
# ---------------------------------------------------------------------------


def compute_recall(correct: int, total: int) -> float:
    """Correctness = |correct_items| / |items| (§38.3).

    ``correct`` / ``total`` are counts of items (answers or claims) the model
    got right out of the total evaluated.
    """
    if total <= 0:
        return 0.0
    return correct / total


def compute_unsupported_rate(unsupported: int, total_claims: int) -> float:
    """Unsupported-claim rate = |claims not backed by source| / |claims| (§38.3).

    A claim is unsupported if no source_ref actually backs it (i.e. the model
    asserted something without source evidence).
    """
    if total_claims <= 0:
        return 0.0
    return unsupported / total_claims


def cost_of(input_tokens: int, output_tokens: int,
            input_cost_per_1k: float, output_cost_per_1k: float) -> float:
    """Compute USD cost from token counts and per-1k-token rates (§31)."""
    return (
        input_cost_per_1k * input_tokens / 1000.0
        + output_cost_per_1k * output_tokens / 1000.0
    )


def cost_of_spec(
    input_tokens: int,
    output_tokens: int,
    input_cost_per_1k: float,
    output_cost_per_1k: float,
) -> float:
    """Alias of :func:`cost_of` (§31 cost accounting)."""
    return cost_of(input_tokens, output_tokens, input_cost_per_1k, output_cost_per_1k)


def latency_p50(latencies_ms: Sequence[float]) -> float:
    """Return the p50 (median) latency in ms.

    For even-length samples the median is the average of the two middle values,
    matching dra#15's ``proof_corpus._pct`` convention.
    """
    if not latencies_ms:
        return 0.0
    s = sorted(latencies_ms)
    n = len(s)
    if n == 1:
        return s[0]
    if n % 2 == 1:
        return s[n // 2]
    return (s[n // 2 - 1] + s[n // 2]) / 2.0


def latency_p95(latencies_ms: Sequence[float]) -> float:
    """Return the p95 latency in ms (nearest-rank, matching §38.2)."""
    if not latencies_ms:
        return 0.0
    s = sorted(latencies_ms)
    n = len(s)
    if n == 1:
        return s[0]
    idx = int(math.ceil(n * 0.95)) - 1
    if idx < 0:
        idx = 0
    if idx >= n:
        idx = n - 1
    return s[idx]


def escalation_frequency(
    escalations: int, runs_that_reached_role: int
) -> float:
    """Escalation frequency = #escalations / #runs that reached the role (§38.3)."""
    if runs_that_reached_role <= 0:
        return 0.0
    return escalations / runs_that_reached_role


# ---------------------------------------------------------------------------
# Policy configuration + result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProofConfig:
    """Tunable configuration for the §38.3 model-routing proof.

    Mirrors ``dra.proof_corpus.ProofConfig`` in shape and intent.
    """

    correctness_floor: float = 0.9
    unsupported_claim_ceil: float = 0.15
    cost_ceiling_usd: float = 10.0
    roles: tuple = (
        "repo_investigation",
        "paper_reconciliation",
        "dom_reasoning",
        "fact_extraction",
        "final_audit",
        "citation_verdict",
    )
    pools: tuple = (
        ModelPool.CHEAP.value,
        ModelPool.WORKHORSE.value,
        ModelPool.FRONTIER.value,
    )
    advisor_consult_rates: tuple = (0.25, 0.5)
    fixture_subset: list[str] | None = None
    mode: str = "offline"  # "offline" or "live"

    @classmethod
    def from_env(cls) -> "ProofConfig":
        """Build a ProofConfig, applying optional env overrides for SLOs."""
        cfg = cls()
        floor = float(os.environ.get("DRA_ROUTING_CORRECTNESS_FLOOR", cfg.correctness_floor))
        ceil_val = float(os.environ.get("DRA_ROUTING_UNSUPPORTED_CEIL", cfg.unsupported_claim_ceil))
        cost_max = float(os.environ.get("DRA_ROUTING_COST_CEILING_USD", cfg.cost_ceiling_usd))
        mode_env = os.environ.get("DRA_ROUTING_MODE", cfg.mode)
        if (
            floor != cfg.correctness_floor
            or ceil_val != cfg.unsupported_claim_ceil
            or cost_max != cfg.cost_ceiling_usd
            or mode_env != cfg.mode
        ):
            cfg = cls(
                correctness_floor=floor,
                unsupported_claim_ceil=ceil_val,
                cost_ceiling_usd=cost_max,
                mode=mode_env,
            )
        return cfg


@dataclass
class VariantMetrics:
    """Measured metrics for a (role, pool) variant (§38.3)."""

    role: str
    pool: ModelPool
    provider: str
    model_name: str
    is_advisor: bool = False
    correctness: float = 0.0
    unsupported_rate: float = 0.0
    cost_usd: float = 0.0
    p50_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    num_calls: int = 0
    num_items: int = 0
    escalations: int = 0
    raw_latencies: list[float] = field(default_factory=list)
    report_ids: list[str] = field(default_factory=list)


@dataclass
class AdvisorMetrics:
    """Measured metrics for the workhorse+advisor variant (§23.4)."""

    workhorse: VariantMetrics
    advisor: VariantMetrics
    combined_correctness: float
    combined_unsupported_rate: float
    combined_cost_usd: float
    combined_p50_ms: float
    combined_p95_ms: float
    num_items: int
    escalations: int
    report_ids: list[str] = field(default_factory=list)


# Type alias for the variant → metrics bundle the policy consumes.
VariantResult = VariantMetrics


# ---------------------------------------------------------------------------
# RoutingPolicy — argmin cost s.t. constraints (D7)
# ---------------------------------------------------------------------------


@dataclass
class PolicyDecision:
    """A routing policy decision per role."""

    role: str
    chosen_pool: ModelPool
    chosen_model: str
    reason: str
    cost_usd: float
    correctness: float
    unsupported_rate: float
    p95_latency_ms: float
    admissible_pools: list[str]
    escalation_triggered: bool


class RoutingPolicy:
    """Metric-driven model selection (D7).

    Selects ``argmin cost | correctness >= floor `` and ``unsupported <= ceil``
    and ``cost <= ceiling`` — never on "feels smarter". Escalation is recorded
    when the cheapest admissible pool is not the global cheapest pool.
    """

    def __init__(
        self,
        correctness_floor: float = 0.9,
        unsupported_claim_ceil: float = 0.15,
        cost_ceiling_usd: float = 0.10,
    ) -> None:
        self.correctness_floor = correctness_floor
        self.unsupported_claim_ceil = unsupported_claim_ceil
        self.cost_ceiling_usd = cost_ceiling_usd

    def _metrics(self, m: VariantMetrics | AdvisorMetrics) -> tuple[float, float, float]:
        """Extract (correctness, unsupported_rate, cost_usd) from either type."""
        if isinstance(m, AdvisorMetrics):
            return m.combined_correctness, m.combined_unsupported_rate, m.combined_cost_usd
        return m.correctness, m.unsupported_rate, m.cost_usd

    def _is_admissible(self, m: VariantMetrics | AdvisorMetrics) -> bool:
        """True if a variant meets all three constraint floors."""
        correctness, unsupported_rate, cost = self._metrics(m)
        return (
            correctness >= self.correctness_floor
            and unsupported_rate <= self.unsupported_claim_ceil
            and cost <= self.cost_ceiling_usd
        )

    def choose(
        self, results: dict[str, VariantMetrics | AdvisorMetrics], role: str
    ) -> PolicyDecision:
        """Pick the cheapest *admissible* variant for ``role``.

        ``results`` maps variant-name -> metrics. The cheapest admissible pool
        wins; if the cheapest pool is inadmissible the policy escalates to the
        next cheapest admissible pool. If no pool is admissible, frontier is
        chosen as best-effort fallback.
        """
        pool_order = [
            ModelPool.CHEAP, ModelPool.WORKHORSE, ModelPool.FRONTIER, ModelPool.ADVISOR,
        ]
        pool_rank = {p: i for i, p in enumerate(pool_order)}

        admissible: list[tuple[int, VariantMetrics | AdvisorMetrics]] = []
        all_by_pool: dict[ModelPool, VariantMetrics | AdvisorMetrics] = {}
        for name, m in results.items():
            pool = _extract_pool(m)
            all_by_pool[pool] = m
            if self._is_admissible(m):
                admissible.append((pool_rank.get(pool, 99), m))

        if admissible:
            admissible.sort(key=lambda t: (t[0], self._metrics(t[1])[2]))
            _, chosen = admissible[0]
            chosen_pool = _extract_pool(chosen)
            escalation = (
                chosen_pool != ModelPool.CHEAP and ModelPool.CHEAP in all_by_pool
            )
        else:
            frontier = all_by_pool.get(ModelPool.FRONTIER)
            if frontier is not None:
                chosen = frontier
            else:
                _, chosen = min(
                    (
                        (pool_rank.get(_extract_pool(m), 99), m)
                        for m in results.values()
                    ),
                    key=lambda t: (t[0], self._metrics(t[1])[2]),
                )
            chosen_pool = _extract_pool(chosen)
            escalation = True

        admissible_names = sorted({
            _extract_pool(m).value for _, m in admissible
        })

        _correctness, _unsupported, _cost = self._metrics(chosen)
        reason = _decision_reason(chosen, escalation, admissible_names, _correctness, _cost, _unsupported)
        _correctness, _unsupported, _cost = self._metrics(chosen)
        return PolicyDecision(
            role=role,
            chosen_pool=chosen_pool,
            chosen_model=(
                chosen.model_name
                if isinstance(chosen, VariantMetrics)
                else chosen.workhorse.model_name
            ),
            reason=reason,
            cost_usd=_cost,
            correctness=_correctness,
            unsupported_rate=_unsupported,
            p95_latency_ms=(
                chosen.p95_latency_ms
                if isinstance(chosen, VariantMetrics)
                else chosen.combined_p95_ms
            ),
            admissible_pools=admissible_names,
            escalation_triggered=escalation,
        )


def _extract_pool(m: VariantMetrics | AdvisorMetrics) -> ModelPool:
    if isinstance(m, AdvisorMetrics):
        return ModelPool.ADVISOR
    return m.pool


def _decision_reason(
    chosen: VariantMetrics | AdvisorMetrics,
    escalation: bool,
    admissible_names: list[str],
    correctness: float,
    cost: float,
    unsupported: float,
) -> str:
    if not admissible_names:
        return (
            "no variant meets correctness/unsupported/cost floors; "
            "escalated to frontier as best-effort fallback"
        )
    if escalation:
        return (
            f"cheapest admissible variant is {cost} USD at "
            f"correctness={correctness:.4f}; cheaper pools failed floors"
        )
    return (
        f"cheapest admissible variant at {cost} USD "
        f"correctness={correctness:.4f} "
        f"unsupported={unsupported:.4f}"
    )
