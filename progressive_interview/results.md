# §38.5 Progressive-Interview A/B Proof — Results Ledger

Mission: `sayandahiyagt/dra#46`  Spec: `§38.5` (lines 2667–2681)  ADR: `ADR-023`

## Decision (reverses to measured)

ADR-005 (Use progressive clarification, not exhaustive up-front grilling) is
promoted from plausible architecture to **measured product policy**: progressive
clarification consumes fewer user turns than exhaustive while matching handoff
correctness. This converts ADR-005 "from plausible architecture into measured
product policy" (spec §38.5, line 2681).

## Reversal-trigger outcome (ADR-005, measured)

| Trigger | Measured | Threshold | Result |
|---|---|---|---|
| progressive_handoff_not_worse_than_exhaustive | prog=1.0, exh=1.0 | progressive >= exhaustive | PASS |
| progressive_fewer_turns_than_exhaustive | prog=1.0, exh=11.0 | progressive < exhaustive | PASS |
| progressive_saves_at_least_min_turns | 10.0 | >= 1 | PASS |
| progressive_annoyance_below_threshold | 0.0 | < 0.5 | PASS |
| progressive_wasted_research_below_ceil | 0.3333 | < 0.4 | PASS |
| progressive_handoff_correctness_above_floor | 1.0 | >= 0.9 | PASS |

**Overall verdict: PASS** — no ADR-005 reversal trigger fired.

## §38.5 metrics per strategy (12 topics, seed=42)

| Strategy | User turns | Annoyance | Late facts missed | Wasted research | Handoff correctness |
|---|---|---|---|---|---|
| progressive | 1.0 | 0.0 | 0 | 0.3333 | 1.0 |
| exhaustive | 11.0 | 0.7273 | 0 | 0.3333 | 1.0 |
| minimal | 1.0 | 0.0 | 48 | 0.3333 | 0.3333 |

## Decision facts

- Exhaustive asks the full 11-question §9.1 questionnaire; progressive/minimal
  ask 1 (`objective` only). → `user_turns` = 11 / 1 / 1 (question-count, per
  `progressive_interview.py:418`).
- Progressive correctness (1.0) == exhaustive correctness (1.0), and progressive
  turns (1) < exhaustive turns (11) → ADR-005 satisfied, promoted to measured.
- Minimal correctness (0.3333) is the worst — late facts (48) are not
  contextualised without p4, confirming the design lever.
- Exhaustive annoyance 0.7273 (= 8 non-critical / 11) exceeds the 0.5 SLO;
  progressive 0.0 is below.

## Notes

- `user_turns` = per-topic average of `p1_questions + p4_questions` (question-count,
  not interrupt-round count).
- DB-gated canonical staging of `MAINTAINER_ASSERTION` rows + the A/B report is
  via `0008_interview_constraints` → `publish.py` `_STANDALONE_STATE_TABLES`
  (`user_assertion`, standalone, no `prov_entity`, kept outside `entity_kind`).
- This ledger is reproducible: regenerate with
  `run_proof(ProofConfig(), write=False, publish=False)` after stripping
  `run_id` / `generated_at` / `staged` and the output is byte-identical (verified
  against this committed `results.json`).
