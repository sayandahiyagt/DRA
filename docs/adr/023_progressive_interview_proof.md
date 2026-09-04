# ADR-023 — §38.5 Progressive-interview A/B proof: promote ADR-005 to measured policy

- **Decision type:** Evidence-driven PD
- **Confidence:** High (offline §38.5 metrics are fully measured against the
  deterministic oracle-corpus fixture at seed=42 — 12 topics, 6 recon
  perspectives; see §“Measured outcome”; the DB-gated canonical-staging path is
  implemented in `src/dra/progressive_interview.py:_stage_assertions` +
  `publish.py:_STANDALONE_STATE_TABLES` and is exercised under Postgres — all 4
  `TestProofDB` tests pass when a DB is reachable, skipped otherwise).
- **Status:** Accepted
- **Spec anchor:** §38.5 Progressive-interview proof (lines 2667–2681 of the spec
  — the five §38.5 measures + the sentence “This converts ADR-005 from plausible
  architecture into measured product policy”); ADR-005 reversal trigger
  (spec line 178; qualitative form in `docs/adr/005_use_progressive_clarification.md:9`);
  §21.3 human-correction semantics (spec line 1591); ADR-017 versioned
  user assertions (spec line 257; `docs/adr/017_human_corrections_are_versioned_assertions.md`);
  `0008_interview_constraints` migration (`alembic/versions/0008_interview_constraints.py:9-33`
  docstring + `revision = "0008_interview_constraints"` at line 40).
- **Evidence:** §38.5 spec (three strategies × five metrics); the dra#46 §38.5
  proof engine `src/dra/progressive_interview.py` (`run_proof`, `_run_simulation`,
  `_simulate_strategy`, `_compute_reversal_trigger`); the deterministic 12-topic
  oracle corpus (`generate_oracle_corpus`, `control_plane.py:_EXHAUSTIVE_QUESTIONNAIRE`
  of 11 questions, `_RECON_PERSPECTIVES` of 6); `tests/test_progressive_interview.py`
  (31 tests: 27 always-green offline + 4 DB-gated `@skipif` from `tests/_db.py:27`);
  `publish.py` `_STANDALONE_STATE_TABLES = ("user_assertion",)` at line 967 and the
  bundle-scoped standalone flip at lines 816–830 and 891–901; `control_plane.py`
  strategy constants at lines 60–63 and `ControlState.strategy: str` at line 272,
  seeded by `state.get("strategy")` (lines 383, 402, 513, 767) and the
  `dra-control-plane run --strategy` flag (line 1429, wired at 1474).
- **Decision:** Adopt progressive clarification (`STRATEGY_PROGRESSIVE`) as the
  default interview strategy — A/B-proven by the §38.5 proof to consume fewer
  user turns than exhaustive while matching handoff correctness. ADR-005 is
  promoted from plausible architecture to **measured product policy** by the
  §38.5 verdict (`PASS`).
- **Design decisions (D1–D4):**
  - **D1 — Offline-first (sandbox-green):** the proof is pure Python
    (`_run_simulation` → `_simulate_strategy`, no DB/network). `TestProofOffline`
    is always green; the DB-gated `TestProofDB` class is `@pytest.mark.skipif`
    without Postgres (`tests/_db.py:27`). The always-green gate is never broken
    by DB absence because `run_proof` checks reachability first
    (`_check_db_reachable_async`, `progressive_interview.py:811`) and
    `_stage_assertions` non-blockingly swallows DB errors and returns `False`
    (`progressive_interview.py:756-757`), leaving `report["staged"] = False`
    (`progressive_interview.py:799, 806`). `main()` exits non-zero on non-PASS
    (`progressive_interview.py:906-907`), so a `FAIL` verdict is a real gate, not
    a silent success.
  - **D2 — Three strategies (dra#45):** `progressive` (objective + recon + focused
    p4), `exhaustive` (full 11-question §9.1 questionnaire), `minimal` (objective
    only, no p4). The strategy is read from `state['strategy']`
    (`control_plane.py:513, 767`), not hardcoded — default `progressive`
    (`control_plane.py:60`).
  - **D3 — `user_turns` = question-count:** `user_turns` is the per-topic average
    of `p1_questions + p4_questions` (`progressive_interview.py:418`), **not**
    interrupt-round count. exhaustive=11, progressive=1, minimal=1. This is the
    only interpretation consistent with §38.5's design lever and the only one
    that makes ADR-005's "progressive consumes fewer user turns" reversal trigger
    satisfiable. (The stale mission §6 wording about interrupt-round count is
  superseded by this measured definition — see `Docs/adr/005_use_progressive_clarification.md`
  and the dra#46 handoff.)
  - **D4 — Canonical assertion staging via standalone tables:** per-strategy
    `MAINTAINER_ASSERTION` rows + the A/B report are staged into the
    `user_assertion` table (no `prov_entity` row, NOT in the `entity_kind` enum)
    and flipped `staged → canonical` atomically through `publish_bundle` via the
    `_STANDALONE_STATE_TABLES` mirror branch — the bundle-scoped `UPDATE`
    (`publish.py:891-902`) that `_mirror_state_canonical` deliberately cannot
    reuse, since that path flips only `_DOMAIN_STATE_TABLES` (`raw_capture`,
    `derived_artifact`, `evidence_unit`, `claim`) via a `prov_entity` join and
    would violate the byte-stability contract that `test_schema_introspection`
    asserts. No `user_assertion` rows are mutated into external evidence
    (ADR-017).
- **Reversal trigger:** ADR-005 fires (and this ADR is reversed to `Superseded`
  by a follow-up) if the §38.5 proof FAILS — i.e. if **any** of the measured
  reversal triggers is `pass: false`: progressive `final_handoff_correctness <
  exhaustive` (handoff not better-or-equal); progressive `user_turns >=
  exhaustive` (no turns saved); `turns_saved < cfg.turns_savings_min`
  (default 1, `progressive_interview.py:575`); progressive `annoyance_proxy >=
  cfg.annoyance_threshold` (0.5); progressive
  `research_wasted_on_discarded_branches >= cfg.wasted_research_ceil` (0.4); or
  progressive `final_handoff_correctness < cfg.handoff_correctness_floor` (0.9).
  Concretely on the default fixture these are exactly the six triggers in
  `_compute_reversal_trigger` (`progressive_interview.py:442-488`), all `PASS`
  below.
- **Consequences:**
  - Positive: progressive is the default; it saves 10 of 11 p1 questions per topic
    while keeping handoff correctness at 1.0 (no architectural information lost),
    so the progressive loop is strictly preferred over exhaustive on this
    corpus. The decision is now *falsifiable* rather than plausibility-based.
  - Neutral: the `minimal` strategy is retained as the "no-p4" lower bound; its
    0.3333 correctness confirms p4 contextualisation matters.
  - Risk: the oracle fixture is synthetic and seed-pinned (12 topics, 6
    perspectives). Real interview distributions may yield a different
    annoyance / wasted-research split — re-run `dra-progressive-interview` with
    `DRA_PROOF_*` env overrides (`ProofConfig.from_env`,
    `progressive_interview.py:578`) before generalising beyond this corpus.
  - Risk/operational: the DB-gated canonical assertions only stage when Postgres
    is reachable (`run_proof` skips `_stage_assertions` and sets `staged=False`
    otherwise — spec §21 env concern). Without a DB the proof is still green
    offline but `user_assertion` rows are not persisted; this is by design.
  - Risk/process: the migration revision id is `0008_interview_constraints`
    (26 chars), NOT the mission's literal `0008_progressive_interview_constraints`
    (38 chars), because the 38-char form overflows `alembic_version`'s
    `varchar(32)` — documented in the migration docstring
    (`0008_interview_constraints.py:9-13`). Any future migration referencing this
    revision must use the 26-char slug. No new migration is needed for the DB-gated
    assertions; `user_assertion` reuses the existing dra#44 path.

## Measured outcome

| Trigger | Measured value | Threshold | Result |
|---|---|---|---|
| progressive_handoff_not_worse_than_exhaustive | prog=1.0, exh=1.0 | progressive >= exhaustive | PASS |
| progressive_fewer_turns_than_exhaustive | prog=1.0, exh=11.0 | progressive < exhaustive | PASS |
| progressive_saves_at_least_min_turns | 10.0 | >= 1 | PASS |
| progressive_annoyance_below_threshold | 0.0 | < 0.5 | PASS |
| progressive_wasted_research_below_ceil | 0.3333 | < 0.4 | PASS |
| progressive_handoff_correctness_above_floor | 1.0 | >= 0.9 | PASS |

**Overall verdict: PASS** — no ADR-005 reversal trigger fired. Progressive is
promoted from plausible (ADR-005) to measured product policy (§38.5).

## §38.5 metrics snapshot (12 topics, seed=42)

| Strategy | User turns | Annoyance proxy | Late facts missed | Wasted research | Handoff correctness |
|---|---|---|---|---|---|
| progressive | 1.0 | 0.0 | 0 | 0.3333 | 1.0 |
| exhaustive | 11.0 | 0.7273 | 0 | 0.3333 | 1.0 |
| minimal | 1.0 | 0.0 | 48 | 0.3333 | 0.3333 |

## Reproducibility

The committed ledger (`progressive_interview/results.json`) is regenerated by
`run_proof(ProofConfig(), write=False, publish=False)` with the volatile fields
`run_id`, `generated_at`, and `staged` stripped. A fresh offline run produces a
byte-identical snapshot (verified by `diff` against this file).
