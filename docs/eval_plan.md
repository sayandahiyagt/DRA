# Evaluation plan — §38.6 downstream utility proof (ADR-018 / spec §24-§26, §38.6)

- **Decision type:** ERI/PD (product-scoped evaluation design)
- **Confidence:** Medium (the architecture under test is fixed by spec §38.6; metric operationalization — thresholds, measurement procedures, mechanical input shapes — is the part this plan exists to define and the proof step later validates)
- **Status:** Planned (set Accepted once the §38.6 proof passes; see §3.9)
- **Spec anchors:** §24.1 (1881-1892), §24.5 (1929-1944), §26.0 (1869-1878), §38.6 (2683-2694), §38.1 (2610-2614); §31 final handoff contract (2198-2286), §34 downstream retrieval contract (2290-2313), §33.13 (2397-2402), §37 Stage 0 (2509-2516), §39 acceptance criteria (2728-2731). ADR-018 (docs/adr/018).
- **Reversal trigger (ADR-018 / §38.6):** A §38.6 proof run fails to show the full handoff + queryable-corpus architecture *materially* reducing all four metrics versus all four baselines, OR the component stress tests (BrowseComp, RepoProbe, SWE-bench/SWE-bench-Live) cease correlating with downstream handoff utility. Either triggers simplification of the handoff/corpus architecture (§40 "Choose a simpler report-first architecture" / ADR-018 reversal trigger).

## 1. Purpose and scope

This plan decides *only* the measurement side of the §38.6 downstream-utility proof. It specifies the component-stress-test triangulation, the decisive end-to-end handoff benchmark, the §38.6 measurement model (the four metrics, operationally defined), the five comparison arms with their mechanical input shapes, and the §38.6 decision rule with a concrete quantification of "materially reduce." It is a design/spec artifact; no DB is required to author it (per the mission brief) and verification is a static checklist against the spec anchors + ADR-018 (§5).

It does **not** decide:
- which coding-agent model is the held-constant workhorse (that comes from the §38.3 model-routing proof / ADR-022 and is recorded per run in the manifest);
- which specific downstream task is held constant (selected by the run from the seeded task set per §24.5 / §37 Stage 0, and recorded in the manifest);
- the proof-harness code itself, or any actual §38.6 measurement.

Keeping those two choices out of scope is what makes the plan reusable across model-routing and task-selection decisions, and is what the spec mandates — §38.6 says only "hold coding-agent model and task constant," not what each is.

## 2. Component stress tests (triangulation, §26.0 + ADR-018)

Public benchmarks stress components; they must not become the product objective. Per §26.0: "Do not optimize the research harness directly against any single benchmark. A benchmark can improve while the handoff becomes worse through overfitting or task mismatch." Each stress test below is therefore a component probe, **not** the decisive product metric.

| Benchmark | Ref | Component skill exercised | §24/§26 measures it maps to | Stated limitation (§26.0) |
|---|---|---|---|---|
| **BrowseComp** [R27] | §26.0:1873 | Hard-to-find web information requiring strategic browsing / scout + search-path quality | §24.3 (DOM/component/state observation, dynamic-page success, observation-vs-inference, prompt-injection resilience); §24.4 (citation entailment, source independence/diversity, contradiction discovery) | Does not test implementation handoff quality |
| **RepoProbe** [R28] | §26.0:1874 | Architecture-aware repository comprehension via real GitHub Discussions + atomic checklist verification | §24.1 (exact file/symbol recall, architecture-map correctness, dependency identification, false implementation claims, task completion cost/latency) | Open-ended comprehension; not downstream coding-agent utility from a handoff |
| **SWE-bench** [R29] | §26.0:1876 | Real issue resolution (downstream coding-agent utility component) | §24.5 downstream-utility component (issue resolution) | Tests issue resolution, not research quality; pre-research tasks, not handoff-from-research |
| **SWE-bench-Live** [R30] | §26.0:1876 | Continuously-updated multi-language/multi-OS issue resolution | §24.5 downstream-utility component | Adds currency/language/OS breadth but not handoff quality |

**Anti-overfitting note (§26.0:1878):** a stress test improving while the handoff worsens is the overfitting risk §26.0 warns about. That is exactly why §38.6 (the product metric) governs the decision, not these. If any stress-test arm diverges from the handoff benchmark, §38.6's pass/fail — not this component set — resolves the conflict.

## 3. Decisive product metric — custom end-to-end handoff benchmark (§24.5, §38.6)

Most important end-to-end metric (§24.5:1931): give coding agents one of the five inputs below, then measure. **Hold the coding-agent model and the downstream task constant** (§38.6:2687). Concretely:

- **Downstream task set:** a small, seeded, deterministic set of repo/code targets with a single held-out comparison task — mirroring §37 Stage 0 "gold/reference tasks" (2511-2516: one repo extension problem, one paper-to-code problem, one website/DOM reconstruction problem) and the hidden-ground-truth fixture pattern in ADR-022 D5. Each task is a concrete, bounded implementation goal with an objective pass/fail (a specific function/API implemented and passing a known-good test), matching §39 acceptance gates #27/#31 (2728-2731).
- **Coding agent + environment:** one fixed coding-agent model + one fixed sandbox/repo snapshot, identical across all five arms, so differences are attributable to the *input given to the agent*, not the agent itself (§38.6:2687).
- **The five input arms** (the "5 conditions" the harness encodes — see §6 for the mechanical shape):
  1. `raw_sources` — the coding agent is given only the raw source links/snapshots it would otherwise have discovered (baseline; §24.5:1934 "baseline raw sources").
  2. `ordinary_report` — a human-style, prose-only research report (no structured evidence / no queryable corpus; §24.5:1935).
  3. `structured_corpus_no_handoff` — the structured evidence/claim/topic/decision graph (§31.3 `/knowledge` schema: topics, claims, evidence, entities, relationships, gaps, decisions) presented as raw data, with **no** synthesized handoff document.
  4. `handoff_no_queryable_corpus` — the human-readable handoff package (§31.1 / files `00-executive.md` … `07-evidence-index.md` + `manifest.json` in §31.2) **without** a live queryable `/knowledge` endpoint (§31.3) — evidence must be carried inline or be unavailable on demand.
  5. `full_handoff_queryable_corpus` — the full proposed architecture: handoff package **and** a queryable `/knowledge` package (§31.3) consumable via the §34 retrieval contract (bounded `ImplementationContextBundle` by requirement/topic/entity/milestone/repo-path/decision/semantic-query; no full-corpus dumps — §34:2311).
- **Held constant** (recorded per run in the manifest, §31.2): coding-agent model, model version/config, sandbox image, repo snapshot ref, downstream task objective + pass test path.

## 4. §38.6 measurement model (the four metrics)

Grounded in spec §38.6 (2683-2694) and §24.5 (1938-1944). Each metric is named verbatim from §38.6 and operationally defined so the proof harness can compute it from a §38.6 run.

### 4.1 re_research_calls
Count of additional research/scout calls the coding agent (or its orchestrator) must make because input data was missing, ambiguous, or non-queryable (§24.4 "number of re-research calls"). **Definition:** distinct tool/LangGraph-node invocations whose stated purpose is re-acquiring a fact already in scope but absent from the arm's input. A re-scout is any call whose query is a near-duplicate (fuzzy similarity ≥ 0.9) of a prior call in the same arm. Counted per arm.

### 4.2 incorrect_assumptions
Number of factually wrong or hallucinated assumptions about the source repository/paper/site the coding agent commits during the task (§24.5 "hallucinated API/file assumptions" + §24.1 "false implementation claims"). **Definition:** assumptions contradicted by the held-out ground truth, verified by a deterministic checker — an imported name that does not exist, a file path that is wrong, an API/signature mismatch, or a structural claim contradicted by the repo map. Each distinct assumption counted once per arm.

### 4.3 time_to_correct_build
Wall-clock from "coding agent starts the task" to "first successful build + known-good test pass" (§24.5 "time to first correct patch/prototype" + §39 #31 "end-to-end benchmark shows the handoff reduces re-research and implementation errors"). **Definition:** `t(complete_verified)` where `complete_verified` = build is green **and** the ground-truth tests pass (§26 terminal statuses). Excludes time the agent spends waiting on human input (none expected in a benchmark). Recorded per arm.

### 4.4 architectural_rework
Downstream structural rework the coding agent must perform after an initial implementation attempt (§24.5 "architectural rework"). **Definition:** distinct re-scaffolding events — moving/renaming a module after a wrong placement assumption, changing a component boundary, or re-doing a design because the research input mis-specified an interface/dependency (§31.3 source-system-understanding, 2226-2230). Counted as a set of re-scaffolding diffs (each exceeding a fixed LOC-moved/renamed threshold to avoid counting typos); 0 for arms that get it right first time.

### 4.5 Cross-cutting context (§24.4 — recorded, not decision inputs)
For every arm also record the §24.4:1917-1927 cross-cutting measures as context, **not** decision inputs: citation entailment, source independence/diversity, contradiction discovery, gap detection, duplicate-work rate, branch novelty, downstream coding-agent success, end-to-end cost, p50/p95 latency, retry/failure rate. These explain *why* an arm lost but do not decide it (§26.0 anti-overfitting).

## 5. §38.6 decision rule (the pass/fail on the architecture)

Hold coding-agent model + downstream task constant. Compute the four metrics for all five arms (§38.6:2687-2692). The full architecture (`full_handoff_queryable_corpus`, arm 5) **must** materially reduce **all four** metrics versus **every** baseline — `raw_sources` (arm 1) and `ordinary_report` (arm 2) are the binding baselines; arms 3 and 4 are diagnostic intermediates.

**"Materially reduce," quantified for this plan:** for each of the four metrics M and each binding baseline B, the preferred target is `M(full) <= M(B) * 0.90` (i.e. ≥ 10% reduction). Because a single small task set can make a hard 0.90 infeasible on a correctness-style metric (low counts make ratios noisy), the threshold relaxes to `0.95` for the correctness-style metrics (`incorrect_assumptions`, `architectural_rework`) **but** the rule still requires *all four* metrics to improve over *all* binding baselines. Tie/equal is a **loss** — the architecture must strictly beat baselines (§38.6:2694 "must materially reduce … otherwise simplify the architecture").

**Outcomes:**
- **Pass:** full architecture strictly beats both binding baselines on all four metrics under the stated thresholds → architecture retained as-is (ADR-018 reversal trigger NOT fired).
- **Fail (simplification trigger):** if any binding baseline beats or ties the full architecture on any of the four metrics → simplify the architecture (drop the non-value-added layer; §40 "Choose a simpler report-first architecture" / ADR-018 reversal trigger). E.g., if `structured_corpus_no_handoff` never beats `ordinary_report`, the structured graph has no downstream value and is simplified.
- **Secondary read (diagnostic, not pass/fail):** intermediate arms 3 and 4 diagnose *which* layer carries the value (corpus vs. handoff vs. queryability); their ranking is reported to inform the simplification decision but is not itself pass/fail.

## 6. Mechanical 5-condition input definitions (the "seed" for the proof harness)

Per the mission: "seed the 5-condition input definitions so the proof harness can encode them mechanically." Each arm is a JSON-shape the harness can load verbatim, with the held-constant set read from the §31.2 manifest. Restate in prose immediately after.

```jsonc
{
  // Held constant across all arms — filled per run from the §31.2 manifest.
  "held_constant": {
    "coding_agent_model": "<from §38.3 model-routing proof / ADR-022 workhorse; filled per run>",
    "coding_agent_version": "<pinned>",
    "sandbox_image": "<pinned>",
    "repo_snapshot_ref": "<commit-ish of the target repo>",
    "downstream_task": "<objective ID from the seeded task set; exact pass-test path>",
    "objective": "<human-readable objective>"
  },
  "arms": [
    {
      "id": "raw_sources",
      "label": "Raw sources (baseline)",
      "provides": ["source_links_or_snapshots"],
      "provides_handoff_document": false,
      "provides_queryable_knowledge": false,
      "knowledge_access": "none",
      "provenance": "agent re-discovers sources itself"
    },
    {
      "id": "ordinary_report",
      "label": "Ordinary research report (baseline)",
      "provides": ["prose_report"],
      "provides_handoff_document": true,
      "provides_queryable_knowledge": false,
      "knowledge_access": "none (prose only)"
    },
    {
      "id": "structured_corpus_no_handoff",
      "label": "Structured evidence corpus, no handoff",
      "provides": ["evidence_claim_graph", "entities", "relationships", "topics", "claims", "gaps", "decisions"],
      "provides_handoff_document": false,
      "provides_queryable_knowledge": true,
      "knowledge_retrieval_contract": "§34 ImplementationContextBundle",
      "handoff_document": null
    },
    {
      "id": "handoff_no_queryable_corpus",
      "label": "Handoff without queryable corpus",
      "provides": ["handoff_package_00_to_07", "manifest_json"],
      "provides_handoff_document": true,
      "provides_queryable_knowledge": false,
      "knowledge_access": "inline evidence-index only (§31.1 / 07-evidence-index.md)"
    },
    {
      "id": "full_handoff_queryable_corpus",
      "label": "Full architecture: handoff + queryable corpus",
      "provides": ["handoff_package_00_to_07", "manifest_json", "queryable_knowledge"],
      "provides_handoff_document": true,
      "provides_queryable_knowledge": true,
      "knowledge_retrieval_contract": "§34 ImplementationContextBundle by ID/query"
    }
  ],
  "metrics": {
    "re_research_calls":    "count of re-scout tool/node calls (fuzzy dup query >= 0.9)",
    "incorrect_assumptions": "ground-truth-contradicted assumptions (imports / paths / API signatures)",
    "time_to_correct_build": "wall-clock to green build + ground-truth tests pass",
    "architectural_rework": "distinct re-scaffolding diff events (moved / renamed modules)"
  }
}
```

Prose restatement (arm id → label → key provision / what it provides to the agent):

| Arm | Label | Key provision to the coding agent |
|---|---|---|
| `raw_sources` | Raw sources (baseline) | Only raw source links/snapshots the agent re-discovers itself; no report, no queryable corpus |
| `ordinary_report` | Ordinary research report (baseline) | A prose-only handoff document; no structured/queryable evidence |
| `structured_corpus_no_handoff` | Structured evidence corpus, no handoff | The §31.3 `/knowledge` graph (topics/claims/evidence/entities/relationships/gaps/decisions) queryable via §34, but **no** synthesized handoff document |
| `handoff_no_queryable_corpus` | Handoff without queryable corpus | The §31.1 `00`–`07` handoff package + §31.2 `manifest.json`; evidence carried inline via `07-evidence-index.md`, **no** live `/knowledge` endpoint |
| `full_handoff_queryable_corpus` | Full architecture: handoff + queryable corpus | The §31.1 handoff package + §31.2 manifest **and** a queryable §31.3 `/knowledge` package reachable through the §34 `ImplementationContextBundle` contract |

## 7. Proof-harness wiring note

This connects the plan to the rest of the §38 proof family so the proof author can wire it:

- **Inputs reused from sibling proofs:** the deterministic fake-backend ground-truth fixture pattern of ADR-022 (the coding-agent model is held constant by reading the §38.3 model-routing proof's chosen workhorse — ADR-022 picks `argmin cost | correctness>=floor AND unsupported<=ceil AND cost<=ceiling`, never on "feels smarter" grounds); the `dra.publish` evidence-graph bundle/commit contract (ADR-013) so evidence fed to arm 5 is canonical and idempotent; the §31.2 manifest schema as the carrier of `held_constant`; the §37 Stage 0 gold/reference task set as the seeded downstream task sources.
- **Outputs:** a `downstream_utility_report.json` per arm with the four metrics + the §24.4 cross-cutting context, and a single pass/fail verdict keyed to the §38.6 decision rule (§5). Stored alongside the §38.1 `results.json`/`results.md` bake-off artifacts (same directory conventions) but kept mechanically separable.
- **Deterministic, external ground-truth verification:** grading is **not** done by the research agent (§33.13 "same agent researches and grades itself" — 2397-2402 mitigation: independent verification/critic task, separate context/prompt, optionally a different model for high-risk audits). The checker is deterministic and external: run `pytest <ground-truth-test>` + a static import/path linter against the agent's produced code, in the pinned sandbox; COMPLETE requires green build + ground-truth tests pass (§26/§39).

## 8. Acceptance / verification checklist (static; no DB)

The document is complete and correct when it satisfies all of:

- [x] §26.0 triangulation named: BrowseComp + RepoProbe + SWE-bench/SWE-bench-Live, each with its component-skill mapping + stated limitation re: handoff quality.
- [x] Decisive product metric framed as a custom end-to-end handoff benchmark (§24.5:1929-1944, §38.6:2683-2694).
- [x] "Hold coding-agent model and downstream task constant" stated explicitly.
- [x] The five arms named and distinguished exactly as in §38.6 (2687-2692): raw sources / ordinary report / structured evidence corpus without handoff / handoff without queryable corpus / full handoff + queryable corpus.
- [x] The four metrics named verbatim from §38.6: re-research calls, incorrect/hallucinated assumptions, time-to-correct-build, architectural rework.
- [x] The decision rule stated: "full architecture must materially reduce the four metrics vs. baselines, otherwise simplify" — with this plan's concrete quantification of "materially" (≥10% preferred; ≥5% relaxable for correctness-style metrics; strict-beat, no ties) and the pass/fail/simplify outcomes.
- [x] The 5-condition input definitions present in mechanical JSON shape (seed) plus a prose restatement.
- [x] ADR-018 reversal trigger referenced (and §40 "simpler report-first architecture" alternative).
- [x] Spec anchor line numbers cited (§24.1, §24.5, §26.0, §38.6, §38.1, §31, §34, §33.13, §37 Stage 0, §39) + ADR-018, ADR-013, ADR-022.

## 9. Out of scope (explicitly NOT done here)

- Authoring the proof-harness code or running any §38.6 measurement.
- Choosing the held-constant coding-agent model (filled per run from the §38.3/ADR-022 workhorse) or the downstream task (filled per run from the seeded §37 Stage 0 task set). Per §24.5: "Give coding agents either: baseline raw sources, ordinary report, or this system's handoff + queryable evidence" — the *inputs* are the plan's job; the specific selection is the run manifest's job.
- Numbering this as an ADR. This is an eval-plan artifact (consistent with `docs/assumptions.md` and the `docs/adr/NNN_*.md` set living side by side in `docs/`). An ADR recording the decision to *adopt* the plan may follow, but is out of scope for this issue.
