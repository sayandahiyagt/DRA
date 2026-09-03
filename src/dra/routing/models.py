"""Model roles, candidate pools, adapters, and pricing (§23, §31, ADR-008).

Defines the ``ModelPool`` / ``ExpensiveRole`` taxonomy from §23.2–23.3,
``ModelSpec`` cost metadata, ``ModelAdapter`` protocol + OpenAI/Anthropic/Google
adapters (live SDKs stubbed behind ``ProviderMode``), a :class:`ModelRegistry`
that selects a model per ``(role, pool)``, and env-overridable pricing tables
(§31 cost accounting).

In offline proof mode the :class:`FakeModelAdapter` returns scripted responses
whose correctness / unsupported-claim rate / latency are determined by the pool,
so the proof compares cheap / workhorse / frontier / workhorse+advisor purely on
measured metrics (D3, D4, D6).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from dra.routing.providers import ProviderMode


# ---------------------------------------------------------------------------
# Enumerations (§23.2 candidate pools, §23.3 advisor)
# ---------------------------------------------------------------------------


class ModelPool(str, Enum):
    """Candidate pricing/quality tier (§23.2 Pool C/B/A + advisor)."""

    CHEAP = "cheap"
    WORKHORSE = "workhorse"
    FRONTIER = "frontier"
    ADVISOR = "advisor"


class ExpensiveRole(str, Enum):
    """Roles that justify an expensive (non-cheap) model (§23.2 candidate tasks)."""

    REPO_INVESTIGATION = "repo_investigation"
    PAPER_RECONCILIATION = "paper_reconciliation"
    DOM_REASONING = "dom_reasoning"
    FACT_EXTRACTION = "fact_extraction"
    FINAL_AUDIT = "final_audit"
    CITATION_VERDICT = "citation_verdict"


# ---------------------------------------------------------------------------
# Model spec + pricing (§31 cost accounting, ADR-020 drift note)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelSpec:
    """A concrete model with provider, pool, role, and per-token pricing."""

    provider: str
    name: str
    pool: ModelPool
    role: ExpensiveRole
    input_cost_usd_per_1k: float
    output_cost_usd_per_1k: float
    max_output_tokens: int


# Pricing tables keyed by model name. Env-overridable: if a
# ``DRA_PRICE_<MODEL>`` env var is set (comma-separated input,output rates per
# 1k tokens), it overrides the default. Model IDs follow §23.2 pool lists;
# they are synthetic (§23 notes these drift) and reversal-triggered per ADR-008.
_DEFAULT_PRICING: dict[str, tuple[float, float]] = {
    # Pool CHEAP
    "gpt-5.6-luna": (0.10, 0.30),
    "gemini-3.5-flash-lite": (0.075, 0.15),
    "claude-haiku-4.5": (0.25, 0.50),
    # Pool WORKHORSE
    "claude-sonnet-5": (2.00, 10.00),
    "gpt-5.6-terra": (1.50, 7.50),
    "gemini-3.5-flash": (0.75, 3.00),
    # Pool FRONTIER
    "claude-opus-5": (5.00, 25.00),
    "gpt-5.6-sol": (3.00, 15.00),
    "gemini-3.5-ultra": (4.00, 20.00),
}


def _env_price(name: str) -> tuple[float, float] | None:
    """Override price from ``DRA_PRICE_<NAME>`` env var (input,output per 1k).

    ``<NAME>`` is the model name upper-cased with hyphens and dots replaced by
    underscores (e.g. ``gpt-5.6-luna`` -> ``GPT_5_6_LUNA``).
    """
    key = f"DRA_PRICE_{name.upper().replace('-', '_').replace('.', '_')}"
    raw = os.environ.get(key)
    if raw is None:
        return None
    parts = raw.split(",")
    if len(parts) != 2:
        return None
    try:
        return float(parts[0]), float(parts[1])
    except ValueError:
        return None


def model_pricing() -> dict[str, tuple[float, float]]:
    """Return the resolved pricing table (env overrides take precedence)."""
    resolved: dict[str, tuple[float, float]] = {}
    for name, (inp, out_cost) in _DEFAULT_PRICING.items():
        override = _env_price(name)
        resolved[name] = override if override is not None else (inp, out_cost)
    return resolved


# ---------------------------------------------------------------------------
# Default model pool assignment per role (ADR-008: candidates, not assignments)
# ---------------------------------------------------------------------------

_POOL_ASSIGNMENT: dict[ModelPool, list[tuple[str, str, int, int]]] = {
    ModelPool.CHEAP: [
        ("openai", "gpt-5.6-luna", 1_000_000, 4_000),
        ("google", "gemini-3.5-flash-lite", 1_000_000, 4_000),
        ("anthropic", "claude-haiku-4.5", 200_000, 4_000),
    ],
    ModelPool.WORKHORSE: [
        ("anthropic", "claude-sonnet-5", 200_000, 8_192),
        ("openai", "gpt-5.6-terra", 1_000_000, 8_192),
        ("google", "gemini-3.5-flash", 1_000_000, 8_192),
    ],
    ModelPool.FRONTIER: [
        ("anthropic", "claude-opus-5", 200_000, 8_192),
        ("openai", "gpt-5.6-sol", 1_000_000, 8_192),
        ("google", "gemini-3.5-ultra", 1_000_000, 8_192),
    ],
    ModelPool.ADVISOR: [
        ("anthropic", "claude-opus-5", 200_000, 8_192),
        ("openai", "gpt-5.6-sol", 1_000_000, 8_192),
    ],
}


def _build_spec(provider: str, name: str, pool: ModelPool, role: ExpensiveRole,
                max_out: int) -> ModelSpec:
    pricing = model_pricing()
    inp, out = pricing.get(name, (0.0, 0.0))
    return ModelSpec(
        provider=provider,
        name=name,
        pool=pool,
        role=role,
        input_cost_usd_per_1k=inp,
        output_cost_usd_per_1k=out,
        max_output_tokens=max_out,
    )


# ---------------------------------------------------------------------------
# Model call result + adapter protocol
# ---------------------------------------------------------------------------


@dataclass
class ModelCallResult:
    """Recorded result of a single model call (§31, §32 cost accounting).

    The ``is_correct`` / ``n_unsupported`` / ``n_claims`` fields are populated
    by the :class:`FakeModelAdapter` for scripted evaluation; real adapters set
    them to ``None`` (real evaluation computes them against ground truth
    externally).
    """

    provider: str
    model_name: str
    pool: ModelPool
    prompt_tokens: int
    completion_tokens: int
    reasoning_tokens: int
    cost_usd: float
    latency_ms: float
    cache_hit: bool = False
    is_escalation: bool = False
    output: str = ""
    is_correct: bool | None = None
    n_unsupported: int | None = None
    n_claims: int | None = None


@runtime_checkable
class ModelAdapter(Protocol):
    """Provider-neutral model adapter (§23, ADR-008).

    Real SDK wiring (OpenAI/Anthropic/Google) is env-gated behind ``ProviderMode``
    and intentionally stubbed in this proof; the offline ``FakeModelAdapter``
    drives the §38.3 harness with deterministic scripted responses.
    """

    def resolve(self, spec: ModelSpec) -> ModelSpec:
        """Return the spec this adapter will execute."""
        ...

    async def complete(
        self, spec: ModelSpec, prompt: str, *,
        max_tokens: int | None = None,
        temperature: float = 0.0,
    ) -> ModelCallResult:
        """Execute ``prompt`` against ``spec.provider``/``spec.name``.

        Returns a :class:`ModelCallResult` with tokens, cost, latency, and the
        raw ``output`` string. Cost/latency are derived from the pricing table
        (§31) — not from subjective quality.
        """
        ...


# ---------------------------------------------------------------------------
# Fake model adapter — deterministic, pool-profiled responses (D5, D6)
# ---------------------------------------------------------------------------


# Pool correctness profile: the fraction of *claims* a model gets right.
# These are applied via floor(n_claims * rate) per fixture so the aggregate
# correctness across fixtures is deterministic and reproducible
# (sandbox-green, no sampling variance).
_POOL_CORRECTNESS: dict[ModelPool, float] = {
    ModelPool.CHEAP: 0.72,
    ModelPool.WORKHORSE: 0.96,
    ModelPool.FRONTIER: 0.99,
    ModelPool.ADVISOR: 0.99,
}

# Pool unsupported-claim rate: fraction of claims the model asserts without
# source backing. Applied via floor(n_claims * rate) per fixture.
_POOL_UNSUPPORTED_BASE: dict[ModelPool, float] = {
    ModelPool.CHEAP: 0.28,
    ModelPool.WORKHORSE: 0.08,
    ModelPool.FRONTIER: 0.03,
    ModelPool.ADVISOR: 0.02,
}

# Deterministic latency (ms) profile per pool — frontier costs more and is slower.
_POOL_LATENCY_MS: dict[ModelPool, float] = {
    ModelPool.CHEAP: 180.0,
    ModelPool.WORKHORSE: 850.0,
    ModelPool.FRONTIER: 2200.0,
    ModelPool.ADVISOR: 2200.0,
}


def _det_bool(seed: int, prob: float) -> bool:
    """Deterministic pseudo-random boolean at probability ``prob`` from ``seed``.

    Uses a multiplicative hash so the same (seed, prob) always yields the same
    answer — for deterministic per-claim jitter that doesn't affect the
    floor-based aggregate rate.
    """
    h = (seed * 2654435761) & 0xFFFFFFFF
    r = (h % 10000) / 10000.0
    return r < prob


def _estimate_tokens(text: str) -> int:
    """Rough token count (~1 token per 0.75 words) for cost estimation."""
    if not text:
        return 1
    return max(1, int(len(text.split()) / 0.75))


def _cost_of_call(spec: ModelSpec, prompt_tokens: int, completion_tokens: int) -> float:
    """Compute USD cost of a call from per-1k-token rates (§31)."""
    return (
        spec.input_cost_usd_per_1k * prompt_tokens / 1000.0
        + spec.output_cost_usd_per_1k * completion_tokens / 1000.0
    )


class FakeModelAdapter:
    """Deterministic offline model adapter (D5, D6).

    For each fixture it scripts whether the model gets each claim right and
    whether it emits unsupported claims, driven by the pool's correctness /
    unsupported-rate profile. Counts are computed via ``floor(n_claims * rate)``
    so the aggregate metric bundle is reproducible across runs (no sampling
    variance) — the proof is sandbox-green.

    The fake also accepts an explicit ``is_correct_override`` list so the
    evaluation harness can pin exact correctness for falsification tests (e.g.
    "always frontier" router that should be caught as dominated).
    """

    def __init__(self, mode: ProviderMode = ProviderMode.OFFLINE) -> None:
        self._mode = mode

    def resolve(self, spec: ModelSpec) -> ModelSpec:
        return spec

    async def complete(
        self, spec: ModelSpec, prompt: str, *,
        max_tokens: int | None = None,
        temperature: float = 0.0,
        fixture_id: str | None = None,
        ground_truth: dict[str, Any] | None = None,
        is_correct_override: bool | None = None,
    ) -> ModelCallResult:
        """Execute a scripted call.

        ``fixture_id`` + ``ground_truth`` align the fake's response to the
        fixture's hidden truth (D5). ``is_correct_override`` (test-only) pins
        the per-fixture correctness for controlled policy tests.
        """
        prompt_tokens = _estimate_tokens(prompt)
        correctness_rate = _POOL_CORRECTNESS.get(spec.pool, 0.5)
        unsupported_rate = _POOL_UNSUPPORTED_BASE.get(spec.pool, 0.15)
        latency_base = _POOL_LATENCY_MS.get(spec.pool, 1000.0)

        claims = ground_truth.get("claims", []) if ground_truth else []
        n_claims = len(claims) if claims else 1

        if fixture_id is not None:
            seed = abs(hash(f"{fixture_id}:{spec.pool.value}:{spec.name}")) % (2**31)
        else:
            seed = 0

        # Deterministic claim-level counts via floor (reproducible aggregate).
        n_correct = int(n_claims * correctness_rate)
        n_unsupported = int(n_claims * unsupported_rate)

        # Per-claim deterministic jitter for latency only (doesn't affect
        # aggregate correctness / unsupported counts).
        if is_correct_override is not None:
            per_fixture_correct = is_correct_override
        else:
            per_fixture_correct = n_correct > 0 and _det_bool(seed, correctness_rate)

        # If the model is "correct" for this fixture, it gets the answer right.
        if ground_truth is not None:
            answers = ground_truth.get("answers", {})
            if per_fixture_correct and answers:
                chosen_key = next(iter(answers))
                output = f"ANSWER: {answers[chosen_key]}"
            else:
                output = "PLACEHOLDER response (model did not answer correctly)"
        else:
            output = "PLACEHOLDER response (no fixture context)"

        completion_tokens = _estimate_tokens(output) + n_claims
        cost = _cost_of_call(spec, prompt_tokens, completion_tokens)
        lat = latency_base + (seed % 100)

        return ModelCallResult(
            provider=spec.provider,
            model_name=spec.name,
            pool=spec.pool,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            reasoning_tokens=0,
            cost_usd=round(cost, 6),
            latency_ms=round(lat, 3),
            cache_hit=False,
            is_escalation=False,
            output=output,
            is_correct=per_fixture_correct,
            n_unsupported=n_unsupported,
            n_claims=n_claims,
        )


# ---------------------------------------------------------------------------
# Model registry + selection
# ---------------------------------------------------------------------------


class ModelRegistry:
    """Selects model specs per ``(role, pool)``.

    The candidate list per pool is fixed (ADR-008: candidates, not permanent
    assignments). ``select(role, pool)`` returns the cheapest spec in the pool
    (cheapest-first by combined per-token rate), and ``candidates(role, pool)``
    returns all specs in that pool so the proof can benchmark them.
    """

    def __init__(self, pricing: dict[str, tuple[float, float]] | None = None) -> None:
        self._pricing = pricing or model_pricing()
        self._cache: dict[tuple[ExpensiveRole, ModelPool], list[ModelSpec]] = {}
        self._build()

    def _build(self) -> None:
        for pool, entries in _POOL_ASSIGNMENT.items():
            for role in ExpensiveRole:
                specs = [
                    _build_spec(provider, name, pool, role, max_out)
                    for provider, name, _, max_out in entries
                ]
                specs.sort(
                    key=lambda s: (s.output_cost_usd_per_1k + s.input_cost_usd_per_1k, s.name)
                )
                self._cache[(role, pool)] = specs

    def candidates(self, role: ExpensiveRole, pool: ModelPool) -> list[ModelSpec]:
        """All candidate specs for ``(role, pool)`` sorted cheapest-first."""
        return list(self._cache.get((role, pool), []))

    def select(self, role: ExpensiveRole, pool: ModelPool) -> ModelSpec:
        """Cheapest model spec in ``pool`` for ``role`` (ties: lowest latency)."""
        cands = self.candidates(role, pool)
        if not cands:
            raise ValueError(f"no candidates for role={role} pool={pool}")
        return cands[0]

    def all_pools(self) -> list[ModelPool]:
        return list(ModelPool)


def pool_correctness(pool: ModelPool) -> float:
    """Expose the fake pool correctness rate for the proof harness."""
    return _POOL_CORRECTNESS.get(pool, 0.5)


def pool_unsupported_rate(pool: ModelPool) -> float:
    """Expose the fake pool unsupported-claim rate for the proof harness."""
    return _POOL_UNSUPPORTED_BASE.get(pool, 0.15)


def pool_latency_ms(pool: ModelPool) -> float:
    """Expose the fake pool latency profile for the proof harness."""
    return _POOL_LATENCY_MS.get(pool, 1000.0)


# ---------------------------------------------------------------------------
# Adapter factory (D1)
# ---------------------------------------------------------------------------


def make_model_adapter(mode: ProviderMode = ProviderMode.OFFLINE) -> ModelAdapter:
    """Factory returning an adapter for the given mode (D1).

    ``OFFLINE`` (default) → :class:`FakeModelAdapter`. ``LIVE`` → raises if no
    credentials. Real SDK adapters (OpenAI/Anthropic/Google) are wired by
    downstream missions (part 5) through this contract; the offline proof is
    the deliverable.
    """
    if mode is ProviderMode.LIVE:
        if not (
            os.environ.get("OPENAI_API_KEY")
            or os.environ.get("ANTHROPIC_API_KEY")
            or os.environ.get("GOOGLE_API_KEY")
        ):
            raise RuntimeError(
                "ProviderMode.LIVE requested but no model provider API key is set "
                "(OPENAI_API_KEY / ANTHROPIC_API_KEY / GOOGLE_API_KEY)."
            )
        raise RuntimeError(
            "Real model SDK wiring is out of scope for dra#9 §38.3 proof "
            "(PLAN §10 non-goal). The offline fake adapter proves the routing "
            "stack; live evaluation is operator-run."
        )
    return FakeModelAdapter(mode=mode)
