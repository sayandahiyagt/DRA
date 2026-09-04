# §38.6 Downstream-Utility Proof Report

- **Mission:** `sayandahiyagt/dra#43`
- **Spec anchor:** §38.6
- **Generated at:** 2026-09-04T16:03:26.671050+00:00
- **Run ID:** `downstream-utility-20260904T160326`

## Held constant
- coding_agent_model: gpt-5.6-luna@workhorse (synthetic)
- coding_agent_version: 1.0-synthetic
- sandbox_image: `ghcr.io/sayandahiyagt/dra:synthetic-workhorse`
- repo_snapshot_ref: `synthetic:repo-comprehension-fixture`
- downstream_task: repo-comprehension:build a module+symbol inventory of a README repo
- objective: produce a correct module boundary map + symbol signature inventory of a repository

## Ground-truth task set (§37 Stage 0 fixture)
- Tasks: 3 | total facts: 24 | repo-extension tasks: 2 | seed: 42

## §38.6 metrics per arm
| Arm | re-research | incorrect | time(ms) | rework | build_green |
|---|---|---|---|---|---|
| raw_sources | 34 | 12 | 1440.000 | 6 | False |
| ordinary_report | 10 | 5 | 650.000 | 4 | False |
| structured_corpus_no_handoff | 4 | 1 | 310.000 | 2 | False |
| handoff_no_queryable_corpus | 1 | 0 | 170.000 | 1 | False |
| full_handoff_queryable_corpus | 0 | 0 | 100.000 | 0 | True |

## §24.4 cross-cutting context (recorded, not decision inputs)
| Arm | citation_entavailability | source_diversity | contradiction | gap_detect | downstream_success | p50_ms |
|---|---|---|---|---|---|---|
| raw_sources | 0.1250 | 0.1250 | 0.5000 | 0.8750 | False | 1440.000 |
| ordinary_report | 0.6250 | 0.6250 | 0.2083 | 0.3750 | False | 650.000 |
| structured_corpus_no_handoff | 0.7500 | 0.7500 | 0.0417 | 0.2500 | False | 310.000 |
| handoff_no_queryable_corpus | 0.8750 | 0.8750 | 0.0000 | 0.1250 | False | 170.000 |
| full_handoff_queryable_corpus | 1.0000 | 1.0000 | 0.0000 | 0.0000 | True | 100.000 |

## ADR-018 / §38.6 reversal triggers (binding baselines)
Full handoff + queryable corpus (arm 5) must strictly beat BOTH binding baselines on all four metrics.

| Metric | Arm5 value | Baseline | Baseline value | Factor | Pass |
|---|---|---|---|---|---|
| re_research_calls__vs__raw_sources | 0 | raw_sources | 34 | 0.9 | PASS |
| time_to_correct_build__vs__raw_sources | 100.0 | raw_sources | 1440.0 | 0.9 | PASS |
| incorrect_assumptions__vs__raw_sources | 0 | raw_sources | 12 | 0.95 | PASS |
| architectural_rework__vs__raw_sources | 0 | raw_sources | 6 | 0.95 | PASS |
| re_research_calls__vs__ordinary_report | 0 | ordinary_report | 10 | 0.9 | PASS |
| time_to_correct_build__vs__ordinary_report | 100.0 | ordinary_report | 650.0 | 0.9 | PASS |
| incorrect_assumptions__vs__ordinary_report | 0 | ordinary_report | 5 | 0.95 | PASS |
| architectural_rework__vs__ordinary_report | 0 | ordinary_report | 4 | 0.95 | PASS |

## Diagnostic ranking (arms 3 & 4 are intermediates, secondary read)
- Ranking (best-first): full_handoff_queryable_corpus, handoff_no_queryable_corpus, structured_corpus_no_handoff, ordinary_report, raw_sources
- Binding baselines: raw_sources, ordinary_report
- Intermediate arms: structured_corpus_no_handoff, handoff_no_queryable_corpus

## Verdict
**PASS** — simplification triggered: False (ADR-018 reversal: False)
- Staged to DB: False

