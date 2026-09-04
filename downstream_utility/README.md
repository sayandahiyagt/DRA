# §38.6 Downstream-Utility Proof (ledger)

Committed deterministic measurement summary for the §38.6 downstream-utility
proof (mission `sayandahiyagt/dra#43`, engine
`src/dra/proof_downstream_utility.py`). The per-run runtime report
`results.json` / `results.md` written by the CLI at the repo root is gitignored —
see `.gitignore` §38.6 block — so this directory holds the diff-stable ledger that
survives workspace resets, mirroring the `bake-off/` committed pattern.

## What it measures

The proof holds the coding-agent model and the downstream task **constant** and
varies the input condition the downstream coding agent receives, then measures
the four §38.6 metrics:

- **re-research calls** — distinct re-scout tool/node invocations made because
  input data was missing, ambiguous, or non-queryable (fuzzy-duplicate queries,
  similarity ≥ 0.9, deduped — §4.1);
- **incorrect/hallucinated assumptions** — factually wrong claims contradicted by
  the held-out ground truth (imports / paths / API signatures / structural
  claims, §4.2 / §24.1 false implementation claims);
- **time-to-correct-build** — wall-clock to a green build + ground-truth tests
  passing (§4.3);
- **architectural rework** — distinct re-scaffolding diff events (moved / renamed
  modules after a wrong placement assumption, §4.4).

## The five arms (§38.6 / eval_plan.md §6)

| Arm id | Label | Handoff doc | Queryable §34 | Role |
|---|---|---|---|---|
| `raw_sources` | Raw sources (baseline) | no | no | binding baseline |
| `ordinary_report` | Ordinary research report (baseline) | yes (prose) | no | binding baseline |
| `structured_corpus_no_handoff` | Structured evidence corpus, no handoff | no | yes | diagnostic intermediate |
| `handoff_no_queryable_corpus` | Handoff without queryable corpus | yes | no | diagnostic intermediate |
| `full_handoff_queryable_corpus` | Full architecture: handoff + queryable corpus | yes | yes | preferred target |

The held-constant downstream coding agent is a deterministic `FakeDownstreamAgent`
(§2.4) — like §38.3's `FakeModelAdapter` and §38.5's oracle simulation — so the
proof is always-green offline and free of API keys/network (eval_plan.md §7:
"grading is not done by the research agent; deterministic, external
ground-truth verification"). A *live* run with a real coding agent is the
downstream confirmation step, not this harness.

## Information-availability profile (the simulation core)

Each arm exposes a strictly increasing fraction of the non-identity ground-truth
facts without a retrieval call (identity facts are always known), so better input
strictly reduces every metric — making the §38.6 decision rule *satisfiable*:

| Arm | non-id observable | re-scout × | hallucinate × | structure loss × |
|---|---|---|---|---|
| raw_sources | 0% | 1.6 | 0.55 | 0.45 |
| ordinary_report | 60% (noisy) | 1.1 | 0.40 | 0.30 |
| structured_corpus_no_handoff | 70% | 0.6 | 0.15 | 0.12 |
| handoff_no_queryable_corpus | 85% | 0.3 | 0.08 | 0.08 |
| full_handoff_queryable_corpus | 100% | 0.0 | 0.0 | 0.00 |

Arms 4 & 5 build their handoff input through the shipped pure
`dra.handoff.build_manifest` (§31.2) + `build_document_package` (§31.1) contracts;
arm 5's §34 queryable knowledge is simulated offline and can additionally hit the
real `dra.knowledge.retrieve_context_bundle` against a seeded run when the DB is
up and `publish=True`.

## Decision rule (reverses ADR-018 to measured)

ADR-018 (adopt the handoff + queryable-corpus architecture) is promoted from
plausible design to **measured product policy** when the proof verdict is `PASS`:
the full architecture (arm 5) *strictly* beats both binding baselines
(`raw_sources`, `ordinary_report`) on **all four** metrics under the §5
quantification — ≥10% reduction (factor 0.90) for re-research/time, ≥5% (factor
0.95) for the correctness-style metrics (incorrect / rework), and **strict `<`**
(a tie is a loss, §38.6:2694). A `FAIL` verdict fires the ADR-018 / §40
simplification trigger ("drop the non-value-added layer; choose a simpler
report-first architecture").

## Verdict

**PASS** — ADR-018 reversal NOT fired. The full architecture materially reduces
all four metrics vs both binding baselines. See `docs/adr/024_downstream_utility_proof.md`.

## Run

```bash
# Deterministic ledger (regenerable) — same seed => identical results
.venv/bin/dra-downstream-utility-proof --no-publish --report-path downstream_utility/results.json

# Always-green offline tests (no DB)
.venv/bin/python -m pytest tests/test_downstream_utility_proof.py -q

# DB-gated assertion staging (Postgres at host.docker.internal:5432)
DATABASE_URL="postgresql+psycopg://postgres:postgres@host.docker.internal:5432/postgres" \
  .venv/bin/python -m pytest tests/test_downstream_utility_proof.py::TestProofDB -v
```

## ADR numbering note

This decision is recorded as `docs/adr/024_downstream_utility_proof.md` (not
`023`): `023` is taken by `docs/adr/023_progressive_interview_proof.md`
(dra#47, the §38.5 progressive-interview proof), which landed on `main` after
this mission's onboarding. The deviation is documented in this ledger's ADR.
