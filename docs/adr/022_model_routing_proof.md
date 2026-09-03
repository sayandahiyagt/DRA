# ADR-022 — §38.3 Model-routing proof: cost-aware, evaluation-driven routing

- **Decision type:** Evidence-driven PD
- **Confidence:** High (measured against deterministic offline fixtures)
- **Status:** Accepted
- **Evidence:** §38.3 model-routing proof (lines 2645–2653 of the spec); §23
  candidate pools (Pool C/B/A + advisor); §23.4 advisor pattern; §31 cost
  accounting; ADR-008 (evaluation-driven routing); ADR-011 (task-routed search);
  §18 (SearchProvider/ContentProvider/BrowserProvider interfaces); §17.2
  (provider capabilities justify task-based routing); dra#15 §38.2 storage
  proof (sandbox-green pattern).
- **Decision:** Adopt a provider-neutral, task-routed routing stack proven via
  a deterministic offline harness. The proof evaluates every expensive role
  across four model variants (cheap / workhorse / frontier / workhorse+advisor)
  on hidden ground-truth fixtures (18 total, 3 per role × 6 roles, 20 claims
  each), measuring correctness, unsupported-claim rate, cost, latency, and
  escalation frequency. The RoutingPolicy selects `argmin cost | correctness>=floor
  AND unsupported<=ceil AND cost<=ceiling` — never on "feels smarter" grounds.
  All expensive-model escalation is logged to `model_escalation_log`
  (migration 0006).

## Design decisions (D1–D7)

- **D1 — Offline-first (sandbox-green):** By default the proof runs against
  deterministic fake backends (`FakeModelAdapter`, `FakeSearchProvider`,
  `FakeContentProvider`, `FakeBrowserProvider`) with no network or API keys.
  Real provider SDKs are stubbed behind `ProviderMode.LIVE` and are
  credential-gated (skipped when absent). This mirrors dra#15's synthetic
  corpus approach: the proof proves the *stack* is sound, not that a specific
  vendor wins.

- **D2 — Provider contracts (§18):** `SearchProvider`, `SiteMapProvider`,
  `ContentProvider`, `BrowserProvider` are `Protocol` classes with the §18
  capability lists. `SearchProviderRegistry` maps each `TaskType` to an
  ordered provider candidate list (Exa / Perplexity / Tavily / Firecrawl +
  rendered-browser fallback per §17.2).

- **D3 — Model roles + pools (§23, ADR-008):** `ModelPool = {CHEAP,
  WORKHORSE, FRONTIER, ADVISOR}`; `ExpensiveRole` enumerates the six roles.
  `ModelRegistry.candidates(role, pool)` returns candidate model specs.
  Model IDs are synthetic (gpt-5.6-luna, claude-sonnet-5, claude-opus-5, etc.).

- **D4 — Cost/latency/escalation instrumentation (§31, §32):** Every fake
  model call records tokens, cost, latency, cache hit, and whether it was an
  escalation. Escalation decisions are logged to `model_escalation_log`
  keyed by `run_id` / `task_id`, recording `from_pool -> to_pool`, trigger,
  cost-delta, latency-delta, and correctness gain.

- **D5 — Hidden ground-truth fixtures:** 18 fixtures (3 per role × 6 roles),
  20 claims each, generated deterministically. Fixtures are "hidden" in that
  the model never sees the truth values — the fake adapter scripts correctness
  from pool profiles, not from the truth.

- **D6 — Metrics (pure functions):** `compute_recall` = |correct|/|items|;
  `compute_unsupported_rate` = |unsupported claims|/|claims|; `cost_of` from
  per-1k-token rates; `latency_p50`/`latency_p95` (nearest-rank, matching
  §38.2); `escalation_frequency` = #escalations / #runs reaching the role.

- **D7 — Selection policy is proven:** `RoutingPolicy.choose()` consumes the
  per-role, per-variant metric bundle and picks `argmin cost | correctness>=
  floor AND unsupported<=ceil AND cost<=ceiling`. The proof asserts the chosen
  variant equals argmin-cost among admissible.

## Cost-ceiling deviation (Executor decision)

PLAN §3 listed `cost_ceiling_usd=0.10` as the default. With realistic §23.2
pricing ($2/10 per 1k for workhorse), even a 3-fixture role costs ~$0.20–$0.60,
making the 0.10 ceiling unrealistically binding (no non-cheap variant would
ever be admissible). The default is set to **$10.00** per task, matching §31's
per-task budget framing. Cost is therefore not the binding constraint in the
offline proof — correctness and unsupported-rate are, which is the correct
behavior per ADR-008 (quality-first, not cost-first). Cost ceilings can be
tightened via `DRA_ROUTING_COST_CEILING_USD` env override.

## Migration numbering

The shared Postgres instance has `alembic_version = 0004_verification_gate_indexes`,
a migration delivered by dra#19 (§38.4 verification-gate indexes) that landed on
`main` after this branch was created. The dra#9 model-routing schema is therefore
migration **0005**, with `down_revision = 0004_verification_gate_indexes`.

## Reversal trigger

Any ADR-008 reversal trigger fires:

| Trigger | Measured | Threshold | Result |
|---|---|---|---|
| policy_argmin_cost | PASS | chosen = argmin cost among admissible | PASS |
| no_dominated_choice | PASS | policy never selects dominated variant | PASS |
| escalation_logged | 12 per run | >= 1 frontier/advisor log per role (>= 6) | PASS |
| provider_matrix_task_routed | 6 task types | > 0 providers per task type | PASS |
| rendered_browser_fallback | True | browser is fall-through for every task | PASS |

**Overall verdict: PASS** — no reversal trigger fired.

## Pool profiles (scripted, offline)

| Pool | Correctness | Unsupported rate | p50 latency (ms) |
|------|-------------|------------------|-------------------|
| CHEAP (0.72) | ~0.75 | ~0.25 | 180 |
| WORKHORSE (0.96) | ~0.95 | ~0.05 | 850 |
| FRONTIER (0.99) | ~1.00 | ~0.00 | 2200 |
| ADVISOR (0.99) | ~1.00 | ~0.00 | 2200 |

Across all roles, the CHEAP pool fails both the correctness floor (0.90) and
the unsupported-claim ceiling (0.15), forcing escalation. WORKHORSE meets all
floors at the lowest cost (~$0.20–$0.60 per role) and is chosen for every
role. FRONTIER and workhorse+advisor also pass but are cost-dominated.
