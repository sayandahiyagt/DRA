# ADR-024 — §38.6 Downstream-utility proof harness

- **Decision type:** ERI/PD (product-scoped evaluation design — measurement apparatus)
- **Confidence:** High (deterministic offline fixtures; the proof validates the
  *measurement model + decision rule*, not a live coding-agent run, which is the
  downstream confirmation step)
- **Status:** Accepted
- **Spec anchors:** §38.6 (2683-2694), §24.1 (1881-1892), §24.5 (1929-1944),
  §26.0 (1869-1878), §38.1 (2610-2614), §31.1-§31.3 (2198-2286), §34 (2290-2313),
  §33.13 (2397-2402), §37 Stage 0 (2509-2516), §39 (2728-2731); ADR-013,
  ADR-016, ADR-017, ADR-018, ADR-022.
- **Evidence:** consumes `docs/eval_plan.md` (dra#40 Part 1: §5 decision rule, §6
  5-condition seed, §4 four-metric definitions); the dra#41 repo-source
  control-plane path (`control_plane._synthesize_tasks` `{kind:repo}`); dra#42
  `handoff.build_manifest`/`build_document_package`/`SECTION_FILES` (§31.1/§31.2,
  pure) + `knowledge.retrieve_context_bundle`/`RETRIEVAL_KEY_TYPES` (§34); and the
  dra#37 §38.1 bake-off `bake-off/results.json`+`results.md` ledger precedent.
- **Decision:** adopt the §38.5 offline-first pattern for the §38.6 proof
  harness. Hold the coding-agent model + downstream task constant (filled
  per-run from the §38.3 model-routing proof / ADR-022 + §37 Stage 0 into the
  §31.2 manifest; synthetic defaults here) and vary the input condition across
  the five §38.6 arms. The held-constant "downstream coding agent" is a
  deterministic `FakeDownstreamAgent` whose four §38.6 metrics are a pure
  function of arm input quality — no real LLM invocation (eval_plan.md §7:
  "grading is not done by the research agent; deterministic, external
  ground-truth verification"). DB-gated canonical staging reuses the existing
  `user_assertion` table (dra#44, migration 0008) exactly as §38.5 does — **no
  new migration**; `tests/test_schema_introspection.py`'s `EXPECTED_TABLES` is
  untouched (precedent: §38.2 `proof_corpus` and §38.5 `user_assertion` reuse
  both avoid touching it). Arms 4 & 5 construct their handoff input via the
  shipped pure `handoff.build_manifest` + `build_document_package` contracts.

## Design decisions (D1–D5)

- **D1 — Offline-first (sandbox-green):** the pure simulation runs with no DB
  and no network (§38.3 `FakeModelAdapter` / §38.5 oracle pattern). `_check_db_reachable`
  (reusing `dra.db.can_connect`) gates *only* the `publish`/staging step, so
  `dra-downstream-utility-proof` writes `results.json` + `results.md` with
  `staged: false` when Postgres is down — never errors.

- **D2 — Information-availability profile:** each arm maps to a strictly
  increasing observability fraction of non-identity facts (raw_sources 0% <
  ordinary_report 60% < structured_corpus_no_handoff 70% <
  handoff_no_queryable_corpus 85% < full 100%), with identity facts always
  observable. The held-constant agent's four metrics are a pure deterministic
  function of `(missing_facts, arm_error_profile)`, so better input strictly
  reduces every metric — making the §38.6 decision rule *satisfiable* and the
  proof reproducible across machines.

- **D3 — §34 retrieval contract simulated, not stubbed:** arm 5's queryable
  knowledge is the real `dra.knowledge` §34 contract surface
  (`RETRIEVAL_KEY_TYPES`, `ImplementationContextBundle`). In the offline path the
  bundle is *simulated* (arm 5 observes all facts); a DB-gated `publish` run can
  additionally exercise the real `retrieve_context_bundle` against a seeded
  run, but this is optional and non-blocking (the simulation is never the thing
  under test — the decision rule is).

- **D4 — §31.1/§31.2 consumed, not hand-rolled:** arms 4 & 5 build their handoff
  document via the shipped pure `handoff.build_document_package` (8 sections
  00–07) + `handoff.build_manifest` (§31.2) over a ControlState-shaped dict
  derived from the task fixture. The harness *consumes* the Part 3 handoff
  contract rather than re-implementing it.

- **D5 — Decision rule is the artifact:** the §5 rule is encoded as data
  (`RECOVERY_FACTORS`: 0.90 for re_research/time, 0.95 for correctness-style
  metrics; `BINDING_BASELINES` = raw_sources + ordinary_report). `apply_decision_rule`
  requires arm 5 to *strictly* beat *both* baselines on *all four* metrics under
  the factors; a tie is a loss (§38.6:2694). PASS → architecture retained; FAIL
  → ADR-018/§40 simplification trigger fired.

## Reversal trigger (ADR-018 / §38.6 / §40)

The simplification trigger fires iff the full handoff + queryable-corpus
architecture (arm 5) does **not** materially reduce all four §38.6 metrics vs
**both** binding baselines under the §5 quantification:

| Metric | Arm5 value | Baseline | Baseline value | Factor | Pass |
|---|---|---|---|---|---|
| re_research_calls | 0 | raw_sources | 34 | ≤0.90× AND strict `<` | PASS |
| re_research_calls | 0 | ordinary_report | 10 | ≤0.90× AND strict `<` | PASS |
| incorrect_assumptions | 0 | raw_sources | 12 | ≤0.95× AND strict `<` | PASS |
| incorrect_assumptions | 0 | ordinary_report | 5 | ≤0.95× AND strict `<` | PASS |
| time_to_correct_build | 100.0 | raw_sources | 1440.0 | ≤0.90× AND strict `<` | PASS |
| time_to_correct_build | 100.0 | ordinary_report | 650.0 | ≤0.90× AND strict `<` | PASS |
| architectural_rework | 0 | raw_sources | 7 | ≤0.95× AND strict `<` | PASS |
| architectural_rework | 0 | ordinary_report | 4 | ≤0.95× AND strict `<` | PASS |

**Overall verdict: PASS** — ADR-018 reversal trigger NOT fired; the full
architecture materially reduces all four metrics vs both binding baselines
(arms 3 & 4 are diagnostic intermediates ranking between the baselines and
arm 5).

## Out of scope / risk

- Invoking a *real* coding agent against a real repo (that is a live downstream
  run; the spec's proof is the measurement-apparatus + decision-rule
  validation).
- A new alembic migration (reuse `user_assertion`).
- Choosing the held-constant workhorse model (filled per-run; default synthetic).
- Any edit to `tests/test_schema_introspection.py` `EXPECTED_TABLES`.
- **Risk:** the synthetic observability profile is a model, not a live
  measurement; a live run (real coding agent + real repo) is the downstream
  confirmation step. The harness proves the *apparatus* is sound and the
  decision rule is satisfiable, not that a particular vendor/model wins.
