# Practical Deep Research-to-Implementation System
## Comprehensive Architecture & Design Specification — v3 Final Architecture Audit

**Status:** Final pre-implementation architecture audit. Re-checked against current framework documentation, security/protocol standards, provenance/licensing standards, current agent benchmarks, and recent deep-research reliability literature.  
**Date:** 2026-09-02  
**Scope:** Architecture and dependencies only; no implementation code.  
**Primary objective:** Convert an initially ambiguous build idea into a durable, evidence-grounded, implementation-ready research corpus and handoff package that one or more downstream coding/orchestrator agents can use without repeating the research.

---

# 0. What changed in this audited revision

The first version reached the right broad architectural direction, but several recommendations were stated more confidently than the evidence justified. This revision keeps the core architecture and corrects the weak points.

The most important changes are:

1. **LangGraph remains the recommended orchestration substrate, but this is now explicitly a product architecture decision rather than a proven universal optimum.** The recommendation is based on its current documented persistence/checkpointing, stores, subgraphs, interrupts, fault tolerance, and provider-neutral graph runtime [R1][R2]. DeerFlow 2 and LangChain Deep Agents are treated as serious worker-runtime/reference alternatives rather than minor inspirations [R3][R4].
2. **LangChain Open Deep Research is no longer considered a live implementation dependency.** Its repository was archived by the owner on August 21, 2026 and is read-only. It remains useful only as a historical/reference architecture [R5].
3. **The evidence layer is strengthened substantially.** Recent evaluation of deep-research systems shows that valid-looking citations do not guarantee factual support; source-attribution quality can degrade as tool use scales, misleading evidence can derail research, and repeated retrieval of user-generated content can create concentrated poisoning risk [R6][R7][R8]. Therefore a dedicated Source & Claim Verification Gate is now mandatory rather than relying mainly on a generic critic.
4. **The initial “shallow breadth grilling” idea is retained and formalized.** Research on uncertainty-guided clarification supports asking questions selectively when their expected information value exceeds interruption cost, although applying that finding to implementation-oriented deep research is an architectural transfer rather than a directly validated result [R9].
5. **The search stack is now provider-neutral and task-routed.** “Exa primary” was too arbitrary. Exa, Perplexity Search, Tavily Map/Crawl, Firecrawl, and rendered-browser tools have different strengths and should be benchmarked and routed by task [R10][R11][R12][R13].
6. **The model recommendation is now evaluation-driven.** The first version overcommitted to specific Anthropic/OpenAI roles. Current prices and capabilities justify considering inexpensive models from OpenAI, Anthropic, and Google, and current Anthropic guidance explicitly warns that advisor/executor pairings are workload-dependent [R14][R15][R16][R17]. The previous “<10% frontier tokens” target is removed because it was a heuristic, not a validated threshold.
7. **GROBID and Docling are no longer treated as interchangeable PDF choices.** They are complementary: GROBID is strong for scholarly structure/citations while Docling targets rich layout, tables, formulas, pictures, provenance and structured document representation [R18][R19]. Critical equations/tables/figures still require visual verification because parser errors are possible.
8. **Temporal is removed from the default MVP.** LangGraph already supplies durable execution/checkpoint semantics. Temporal is an optional later operational layer only if the deployment develops workflow requirements that LangGraph alone does not comfortably satisfy [R1][R20].
9. **PostgreSQL + pgvector remains the MVP storage recommendation, but only under explicit scale assumptions.** pgvector supports exact and approximate nearest-neighbor indexes with documented recall/speed tradeoffs, so the design now defines migration triggers rather than assuming it remains sufficient indefinitely [R21].
10. **Fixed numerical scoring weights are removed.** Coverage, support, exact locators, contradiction handling, actionability, freshness and cost remain evaluation dimensions, but their weights must be calibrated on the project’s own task suite.
11. **A formal assumptions register, architecture decision records, reversal triggers, source-specific evidence rules, invalidation semantics, and independent selection matrix are added.** These are specifically intended to let another engineering team reconstruct why this architecture was selected.

12. **The canonical evidence layer now has an explicit transaction boundary.** A worker cannot partially “publish” evidence and then fail. Raw capture, derivation metadata, evidence units, hashes, source identity, and task linkage are staged and committed atomically where practical; retries are content-addressed/idempotent. This closes an implementation gap that otherwise creates duplicate or orphaned evidence during long-running/retried jobs.
13. **Provenance is now specified as a first-class derivation graph, inspired by W3C PROV concepts rather than an ad-hoc collection of citation fields.** The system records entities/artifacts, generation/derivation activities, responsible agent/model/tool versions, and bundles/run boundaries. Full W3C PROV serialization is optional; preserving equivalent semantics is required [R23].
14. **Source access, crawling, licensing, and authorization policy are promoted from “constraints” to an explicit subsystem.** Crawlers must honor deployment policy including RFC 9309 robots controls where applicable; repository/document licensing uses standardized SPDX identifiers/expressions when available; authenticated MCP integrations follow audience-bound authorization rather than token passthrough [R24][R25][R26].
15. **Cross-run memory reuse is now quarantined by provenance and freshness.** Prior synthesized knowledge may seed discovery, but it cannot silently count as fresh evidence in a new run. Reused claims must retain original source/version identity and be revalidated when freshness or implementation version matters.
16. **User corrections and human assertions receive explicit semantics.** They are preserved as user-provided constraints/claims with history, not rewritten into external evidence or used to overwrite contradictory source observations. Supersession is versioned and auditable.
17. **The evaluation plan is strengthened with external benchmark triangulation.** BrowseComp is useful for hard web discovery, RepoProbe for architecture-aware repository comprehension, and SWE-bench/SWE-bench-Live for downstream implementation utility; none alone measures this product, so they complement—not replace—the custom end-to-end handoff benchmark [R27][R28][R29][R30].
18. **MCP/tool security is made concrete.** MCP tools are arbitrary capability surfaces; authorization, consent, audience binding, token isolation, and least-privilege scopes are required. Retrieved tool descriptions remain untrusted input [R24][R31].

---

# 1. Executive architectural decision

Build a **custom Progressive Specification + Evidence Graph Research Harness** on top of a **durable graph/state-machine runtime**, with **LangGraph as the recommended initial orchestration substrate**.

Do **not** adopt a current deep-research product wholesale. Instead combine:

- LangGraph for durable state transitions, checkpoints, interrupts, fan-out/fan-in and resumability [R1][R2];
- Deep Agents and DeerFlow 2 patterns for context offloading, subagent isolation, filesystem-backed work, sandboxing and long-horizon worker ergonomics [R3][R4];
- STORM-like perspective expansion before convergence;
- GPT-Researcher-like recursive breadth/depth exploration as a reference pattern, not as the canonical scheduler;
- AgentDisCo-like separation of exploration, shared evidence, critique and downstream writing;
- source-type-specific investigators for repositories, papers and websites;
- a custom canonical evidence/claim/topic/decision model;
- a dedicated claim-verification and gap-closure subsystem;
- cost-aware, evaluation-driven model routing.

The system repeatedly executes:

> **shallow breadth elicitation → cheap reconnaissance → focused clarification → typed research planning → broad parallel exploration → deep source-specific investigation → evidence normalization → claim verification → synthesis → gap/contradiction detection → targeted re-investigation → implementation handoff audit**

The canonical state is **not** a chat transcript, a single report, or an agent filesystem. It is a structured, versioned research state composed of immutable raw captures plus versioned derived evidence, claims, implementation entities, decisions, gaps and topic relationships.

### Confidence in this architectural decision

**High, but not absolute.** The individual substrate capabilities are directly documented; the combination is a product-specific architectural synthesis. No public benchmark directly compares this exact architecture against DeerFlow 2, Deep Agents, Claude Managed Agents, Magentic-style orchestration, or a custom Temporal-first system on the user’s three implementation-research use cases. Therefore the architecture must include an evaluation plan and reversal triggers rather than treating the selection as dogma.

---

# 2. Evidence and certainty conventions

Every architectural statement in this document should be understood as one of four types:

| Label | Meaning | Example |
|---|---|---|
| **VC — Verified capability** | Directly documented by an official source or strongly supported research finding. | LangGraph checkpointers persist thread state and stores persist application-defined cross-thread data [R1]. |
| **ERI — Evidence-supported inference** | Evidence supports the ingredients, but the product-specific combination is an architectural inference. | Use shallow clarification, then research, then ask higher-value follow-up questions. |
| **PD — Product decision** | A deliberate engineering choice made for this product, not a universal fact. | Use Postgres + pgvector for MVP. |
| **H — Heuristic requiring calibration** | A starting policy that must be tuned with internal evaluation. | Branch novelty thresholds or model escalation cutoffs. |

Implementers should preserve these labels in ADRs so later teams do not mistake a convenience decision for an externally proven fact.

---

# 3. Assumptions register

The recommendation depends on the following assumptions. If several are false, the architectural conclusion should be revisited.

| ID | Assumption | Why it matters | Reversal consequence |
|---|---|---|---|
| A1 | Primary jobs last minutes to hours, occasionally longer, rather than routinely weeks/months. | LangGraph persistence is likely sufficient operationally at first. | Consider Temporal or another workflow engine as an outer durable service layer. |
| A2 | Initial scale is moderate: individual/project research corpora rather than internet-scale indexing. | Makes Postgres + pgvector operationally attractive. | Evaluate Qdrant/Weaviate/Milvus/vector service and partitioned metadata architecture. |
| A3 | Exact provenance, implementation locators, and relational joins matter as much as semantic similarity. | Favors relational canonical state plus vector projection. | A vector-native primary store might become more attractive if similarity dominates. |
| A4 | The system can persist source artifacts and derived research state. | Evidence lineage and reproducibility require durable artifacts. | Stateless deployments require a substantially different architecture. |
| A5 | Repositories/sites/papers are public or the deployment is explicitly authorized to inspect them. | Browser/repo deep inspection must remain lawful and within access policy. | Restrict tools and evidence collection by source permissions. |
| A6 | The system can execute disposable containers/sandboxes. | Safe repo testing and code inspection benefit from isolated execution. | Disable execution or move it to a managed sandbox service. |
| A7 | Downstream coding agents benefit from structured handoff plus queryable evidence, not just prose. | Justifies evidence/claim/topic schema. | If the output is only a human report, the data model can be simplified. |
| A8 | Multi-provider model/search APIs are acceptable. | Enables task-specific routing and cost optimization. | A single-vendor deployment should preserve interfaces but narrow implementations. |
| A9 | Async research is acceptable. | Breadth/depth exploration and verification can be slow. | Real-time-only use requires much tighter budgets and reduced depth. |
| A10 | Initial engineering team is comfortable operating Python services, Postgres, object storage and containers. | Drives practical dependency choices. | A managed-runtime architecture could be preferable. |
| A11 | The agent is research-first and does not autonomously deploy/merge production code. | Allows tighter read-only permissions and safer tool boundaries. | Autonomous build/deploy adds a much stronger approval, secrets and change-control plane. |
| A12 | Product quality is judged by implementation uncertainty removed, evidence quality and downstream actionability—not prose elegance. | Drives verification-heavy architecture. | A report-generation product would optimize different components. |

---

# 4. Why the architecture is evidence-first rather than report-first

Recent research materially strengthens the case for this decision.

A 2026 analysis of deep-research source attribution found that source links can be valid and relevant while still failing to support the factual claim attributed to them, and reported degradation in source factual accuracy as tool calls increased in evaluated systems [R6]. DRNOISE shows that plausible misleading documents can strongly distort deep-research answers and identifies a tendency for agents to stop before fully reconciling evidence chains [R7]. Separate work demonstrates that frequently retrieved user-generated pages can create concentrated poisoning risk across related research queries [R8].

Therefore this product must not equate:

- more sources with more truth;
- more agent calls with more verification;
- citation presence with entailment;
- a synthesized report with a durable research record.

The architecture instead makes the chain explicit:

**Raw Source Capture → Parsed/Normalized Evidence → Claim → Verification State → Topic/Implementation Entity → Decision/Inference → Handoff Statement**

A downstream coding agent should be able to traverse a handoff recommendation backward to the exact evidence that justified it.

---

# 5. Architectural selection matrix

This matrix distinguishes **documented capability** from **fit judgment**. “Fit” is product-specific and should be rechecked during implementation.

| Candidate | Durable graph/run state | Subagent isolation / parallelism | Filesystem/sandbox orientation | Multi-provider openness | Evidence/claim model built in | Fit as canonical control plane | Fit as worker/runtime reference |
|---|---:|---:|---:|---:|---:|---:|---:|
| **LangGraph** | Strong [R1] | Strong via subgraphs [R2] | Requires composition | Strong | No | **High** | High |
| **LangChain Deep Agents** | Uses LangGraph runtime [R3] | Strong [R3] | Strong [R3] | Strong [R3] | No | Medium | **High** |
| **DeerFlow 2** | Long-horizon/persistent runtime [R4] | Strong | Strong | Broad | No explicit product-specific claim graph | Medium | **High** |
| **Claude Managed Agents / Agent SDK style** | Strong managed sessions | Strong | Strong | Provider-bound | No product-specific claim graph | Medium/low for canonical layer | High if Anthropic-centric |
| **Magentic-style orchestrator** | Workflow-specific | Specialist agents | Tool-oriented | Framework-dependent | No | Medium/low | Medium as pattern |
| **GPT Researcher** | Research-oriented | Breadth/depth recursion | Research tools | Broad | Report/research artifacts, not this schema | Low/medium | High as research pattern |
| **STORM** | Research pipeline | Perspective-driven breadth | Not general sandbox harness | Model/retrieval configurable | Knowledge/report pipeline | Low | High as breadth pattern |
| **Open Deep Research** | LangGraph-based | Research supervisor patterns | Tool based | Broad | No product-specific evidence model | **Do not depend on it: archived** [R5] | Historical reference only |
| **Temporal** | Excellent durable workflow semantics [R20] | Distributed activities/workflows | Not an agent harness | Neutral | No | Medium as outer operational layer | Low as research harness |

### Selection conclusion

LangGraph wins as the **initial control-plane substrate** because its state-machine/graph semantics map directly onto the proposed typed research lifecycle while keeping models, search providers, stores and specialist workers replaceable. Deep Agents or DeerFlow-derived components can be used inside worker nodes without giving their internal message/filesystem representation canonical authority.

This is a **PD**, supported by VCs around runtime capabilities. It is not a benchmark-proven universal ranking.

---

# 6. Architecture Decision Records (ADR summary)

## ADR-001 — Use LangGraph as the initial orchestration substrate
**Decision type:** PD  
**Confidence:** High  
**Evidence:** Checkpointers persist thread graph state; stores persist application-defined cross-thread data; documented use cases include interruption recovery, fault tolerance and human-in-the-loop [R1]. Subgraphs support modular/isolated graph execution [R2].  
**Alternatives:** DeerFlow 2, Deep Agents as top-level harness, Claude Managed Agents, Temporal-first custom runtime.  
**Why chosen:** Explicit graph semantics align with typed phases/gates, provider-neutral composition, and research branch fan-out/fan-in.  
**Reversal trigger:** Production evaluation shows unacceptable failure recovery, subgraph scaling, observability, or cross-service workflow management; or another runtime materially reduces complexity without compromising evidence state.

## ADR-002 — Separate orchestration state from canonical research evidence
**Decision type:** ERI/PD  
**Confidence:** Very high  
**Evidence:** LangGraph itself distinguishes thread graph state from application-defined durable stores [R1]. Deep Agents documentation similarly distinguishes context/file backends and emphasizes offloading large outputs rather than carrying them in model context [R3].  
**Decision:** Graph checkpoints store control state and stable IDs, not the full research corpus. Postgres/object storage hold canonical evidence.  
**Reversal trigger:** None expected; implementation may change stores, not the separation principle.

## ADR-003 — Use Postgres + pgvector for MVP retrieval metadata
**Decision type:** PD  
**Confidence:** Medium-high under A2/A3  
**Evidence:** pgvector supports exact nearest-neighbor search and HNSW/IVFFlat approximate indexes with explicit speed/recall tradeoffs [R21].  
**Why chosen:** Provenance/claim joins and vector retrieval can coexist with fewer operational systems.  
**Reversal triggers:** Corpus/vector count, p95 retrieval latency, multi-tenant isolation, filtered ANN recall, or write throughput miss SLO after tuning.

## ADR-004 — Use immutable raw captures and versioned derived artifacts
**Decision type:** ERI/PD  
**Confidence:** High  
**Reason:** Reproducibility and invalidation require preserving what was actually observed. Derived parser/model outputs must be regenerable when tools change.  
**Decision:** Raw captures are content-addressed and immutable. Parsed artifacts/evidence/claims are versioned, supersedable and staleness-aware.

## ADR-005 — Use progressive clarification, not exhaustive up-front grilling
**Decision type:** ERI  
**Confidence:** Medium-high  
**Evidence:** Structured uncertainty/EVPI-oriented clarification research supports asking questions selectively based on information value versus cost [R9].  
**Transfer caveat:** That work is not specifically a benchmark of deep implementation research. The spiral application must be evaluated internally.  
**Reversal trigger:** User studies show repeated clarification harms completion or yields negligible architectural improvement.

## ADR-006 — Use typed specialist worker modes, not an unconstrained swarm
**Decision type:** ERI/PD  
**Confidence:** High  
**Evidence:** Existing multi-agent systems commonly isolate specialized contexts and tools; Deep Agents explicitly supports isolated subagents [R3].  
**Reason:** Repository, paper and DOM evidence have different validity rules; typed worker contracts prevent “one generic researcher” from treating all sources as text.

## ADR-007 — Add a dedicated Source & Claim Verification Gate
**Decision type:** Evidence-driven PD  
**Confidence:** Very high  
**Evidence:** Deep-research citation studies show citation presence/relevance is insufficient for factual support, and misleading evidence can strongly alter results [R6][R7].  
**Decision:** Verification is a separate lifecycle stage with deterministic checks plus calibrated LLM judges where needed.

## ADR-008 — Model routing is evaluation-driven, not brand/role hardcoded
**Decision type:** PD  
**Confidence:** Very high  
**Evidence:** Current providers expose very different price tiers [R14][R15][R16]; Anthropic’s current advisor guidance explicitly says model pairing depends on task and consultation rate [R17].  
**Decision:** Maintain role candidate pools and benchmark them. No permanent “Opus for X” rule.

## ADR-009 — Use complementary scholarly parsers plus visual verification
**Decision type:** PD  
**Confidence:** High  
**Evidence:** GROBID specializes in scholarly document structure/reference extraction [R19]; Docling exposes structured layout, formula/picture/table and provenance features [R18].  
**Decision:** Use both selectively; visually inspect implementation-critical equations/tables/figures when parser confidence is low or outputs disagree.

## ADR-010 — Do not add Temporal in MVP
**Decision type:** PD  
**Confidence:** Medium-high  
**Evidence:** LangGraph already provides checkpoint/persistence/fault-recovery semantics [R1]; Temporal provides durable event-history-based workflow recovery [R20].  
**Reason:** Two overlapping workflow state machines increase operational complexity before a demonstrated need.  
**Adoption triggers:** multi-day/month SLAs, durable timers/schedules, cross-service orchestration, or distributed workflow guarantees beyond the chosen LangGraph deployment.

## ADR-011 — Search is a task-routed provider abstraction
**Decision type:** PD  
**Confidence:** High  
**Evidence:** Exa, Perplexity Search, Tavily, and Firecrawl expose materially different search/extraction/crawl modes [R10][R11][R12][R13].  
**Decision:** No universal primary provider. Benchmark and route.

## ADR-012 — Deep Agents/DeerFlow are worker-harness candidates, not canonical data models
**Decision type:** PD  
**Confidence:** High  
**Evidence:** Both provide long-horizon/context/sandbox/subagent primitives [R3][R4]; neither exposes the product-specific claim/evidence/decision semantics this system requires.  
**Reversal trigger:** A future harness adopts equivalent first-class provenance/claim/versioning contracts and materially reduces custom complexity.


## ADR-013 — Canonical evidence publication is transactional and idempotent
**Decision type:** PD  
**Confidence:** Very high  
**Problem addressed:** Retried workers, partial failures, duplicate fetches, parser crashes, and concurrent branches can otherwise create orphaned artifacts or conflicting evidence rows.  
**Decision:** A branch writes to a staging scope first. Publication requires source identity, raw artifact hash, derivation metadata, evidence locators, task/run IDs, and schema validation. The canonical commit is idempotent on stable source/artifact/content identities. Partial commits never count toward branch completion.  
**Reversal trigger:** None expected; the database implementation may change, but atomic publication semantics remain required.

## ADR-014 — Use provenance semantics compatible with an entity/activity/agent derivation model
**Decision type:** ERI/PD  
**Confidence:** High  
**Evidence:** W3C PROV defines provenance around entities, activities, agents, derivations, responsibility, and bundles, specifically to support quality/trust assessment and interchange [R23].  
**Decision:** Internally represent equivalent semantics for every derived artifact, evidence unit, claim, summary, and handoff statement. Full PROV-O/PROV-N export is optional at MVP.  
**Why not require full W3C serialization:** It would add implementation surface without proving product value; semantic compatibility preserves future exportability.

## ADR-015 — Source-access and licensing policy is a mandatory precondition, not an afterthought
**Decision type:** PD  
**Confidence:** High  
**Evidence:** RFC 9309 standardizes robots exclusion for automated crawlers while explicitly noting it is not authorization [R25]. SPDX provides standardized machine-readable license identifiers/expressions [R26].  
**Decision:** Every source acquisition path records access basis, robots/crawl policy where applicable, license metadata when relevant, authentication scope, and whether artifact redistribution is allowed.  
**Reversal trigger:** Deployment-specific policy may be stricter, never silently looser.

## ADR-016 — Cross-run knowledge is a discovery accelerator, not automatically current evidence
**Decision type:** PD  
**Confidence:** Very high  
**Reason:** Reusing old conclusions without carrying source/version/freshness creates silent staleness and circular corroboration.  
**Decision:** Prior claims/topics may be retrieved as `PRIOR_KNOWLEDGE`, but new runs must distinguish them from `CURRENT_RUN_EVIDENCE`. High-impact/current claims require freshness checks or source revalidation.  
**Reversal trigger:** None expected; cache policy can become more permissive only for explicitly immutable/version-pinned sources.

## ADR-017 — Human corrections are versioned assertions, not destructive edits to evidence
**Decision type:** PD  
**Confidence:** Very high  
**Decision:** User/maintainer corrections create new assertion records linked to what they supersede or dispute. They may change product decisions immediately when they encode preferences/constraints, but they do not rewrite external source evidence.  
**Reason:** This preserves auditability and prevents human preference from masquerading as empirical fact.

## ADR-018 — Evaluate with benchmark triangulation plus a product-specific downstream handoff benchmark
**Decision type:** ERI/PD  
**Confidence:** High  
**Evidence:** BrowseComp measures difficult web discovery/reasoning [R27]; RepoProbe targets open-ended architecture-aware repository comprehension [R28]; SWE-bench evaluates solving real GitHub issues [R29], with SWE-bench-Live adding continuously updated multi-language/multi-OS tasks [R30].  
**Decision:** Use these as component stress tests, but make downstream coding success from the generated handoff the decisive product metric because no public benchmark exactly matches this system.

## ADR-019 — MCP/tool authorization is deny-by-default and capability-scoped
**Decision type:** Evidence-driven PD  
**Confidence:** Very high  
**Evidence:** MCP authorization guidance requires resource/audience binding, secure token handling, and forbids token passthrough; the specification also treats tools as arbitrary code-execution capability surfaces requiring consent and access controls [R24][R31].  
**Decision:** Tool grants are per role/run, read-only by default, audience-bound, non-transferable between MCP servers, and separately logged. Browser/retrieval content cannot elevate tool scope.


---

# 7. System goals and non-goals

## 7.1 Goals

The system must:

1. transform ambiguous intent into an explicit implementation research specification;
2. generate multiple plausible implementation paths before narrowing;
3. inspect repositories, papers, websites, documentation and supporting evidence at source-appropriate depth;
4. retain exact source snapshots and provenance;
5. separate observations from claims, inferences, recommendations and user decisions;
6. create both a hierarchical topic understanding and similarity retrieval index;
7. detect missing, weak, contradictory, derivative, stale or suspicious evidence;
8. validate important implementation conclusions using executable/static/visual checks when practical;
9. produce a handoff that downstream coding agents can use directly;
10. remain resumable and inspectable across long runs;
11. route expensive models only where measured marginal value justifies them;
12. make incomplete research visibly incomplete instead of converting budget exhaustion into apparent completion.

## 7.2 Non-goals

The system is not primarily:

- an encyclopedia/report writer;
- an unrestricted autonomous coder;
- a generic chat swarm;
- a pure vector-RAG application;
- a single-long-context-model solution;
- a web summarizer;
- a proprietary-source reverse-engineering system;
- a replacement for downstream coding/test/merge governance.

---

# 8. Logical architecture and trust planes

The implementation should separate five planes.

## 8.1 Control plane

Owns:
- run/session identity;
- typed task graph;
- phase transitions;
- branch budgets;
- retries;
- human checkpoints;
- policy;
- model/tool routing;
- status and observability.

**Canonical technology:** LangGraph initially.

Control-plane state should contain compact identifiers and decision/status fields, not large evidence blobs.

## 8.2 Evidence/data plane

Owns:
- source registry;
- raw captures;
- parsed artifacts;
- evidence units;
- claims;
- implementation entities;
- topic hierarchy;
- relationships;
- decisions;
- gaps;
- embeddings;
- staleness and lineage.

**Canonical technologies:** PostgreSQL + pgvector + object storage initially.

## 8.3 Sandbox plane

Owns untrusted execution and deep inspection:
- cloned repositories;
- builds/tests;
- shell/Python;
- browsers;
- downloaded files;
- parsing utilities.

Properties:
- disposable;
- non-root;
- bounded CPU/memory/time;
- restricted filesystem;
- no ambient credentials;
- network policy;
- explicit artifact export.

## 8.4 Provider plane

Replaceable adapters for:
- LLMs;
- embeddings;
- search;
- crawl/extraction;
- browsers;
- Git hosting;
- scholarly metadata;
- MCP tools.

No provider-specific result should become canonical until normalized into the evidence/data plane.

## 8.5 Observability/evaluation plane

Owns:
- typed events;
- traces;
- model/tool cost accounting;
- branch timing;
- retry/error histories;
- retrieval overlap;
- verification outcomes;
- quality benchmark results;
- model/provider experiment assignments.

---

# 9. Progressive specification / grilling loop

This is a core product behavior, but its exact policy is a heuristic that must be evaluated.

## 9.1 Stage A — initial breadth-oriented interview

Ask for only enough information to conduct useful reconnaissance:

- desired artifact/product;
- implementation target if known;
- intended user and workflow;
- deployment environment;
- major performance/quality constraints;
- privacy/security/licensing constraints;
- preferred examples or reference systems;
- what must be original vs interoperable/compatible;
- success criteria;
- explicit non-goals;
- time/cost boundaries if important.

Do **not** ask the user to decide technical forks they cannot yet know exist.

### Output contract

`IntentSnapshot` contains:
- goal statement;
- acceptance criteria;
- known constraints;
- preferences;
- examples/references;
- unresolved ambiguities;
- initial assumptions;
- user-decision-required questions;
- externally-researchable questions.

## 9.2 Stage B — cheap breadth reconnaissance

Use inexpensive search/retrieval and cheap/medium models to discover:
- terminology;
- neighboring approaches;
- relevant repositories;
- papers;
- documentation sites;
- obvious architecture forks;
- dependencies;
- implementation precedents;
- failure modes;
- licensing/security constraints;
- questions that were invisible during the first interview.

This phase creates **candidates and uncertainties**, not final conclusions.

## 9.3 Stage C — question utility estimation

For each unresolved question estimate:

- **Implementation impact:** Would the answer change architecture, scope, compatibility, cost or acceptance?
- **Current uncertainty:** Are plausible alternatives still materially open?
- **User-only answerability:** Is this preference/constraint information unavailable externally?
- **Research answerability:** Could external evidence resolve it more cheaply than interrupting the user?
- **Interruption cost:** Will the question slow/frustrate the user without enough decision value?

A useful heuristic is:

> **Question utility ≈ implementation impact × expected uncertainty reduction × user-only-answerability − interruption cost**

This is EVPI-inspired rather than a literal calibrated probability model at MVP. Research on structured uncertainty-guided clarification supports the general principle of selecting clarification questions for information value instead of asking indiscriminately [R9].

## 9.4 Stage D — focused second interview

Ask only high-utility questions that reconnaissance has made concrete.

Examples:
- “The repository supports both an extension API and a fork-level internal hook; do you need upstream compatibility?”
- “The paper has two implementation variants with a 3× memory difference; are you optimizing training memory or inference latency?”
- “The reference site’s behavior appears to require a client-side state machine; is matching interaction behavior more important than matching visual layout?”

## 9.5 Stage E — repeat only when the evidence changes the decision surface

After deep research or critic review, reopen user clarification only if:
- a new high-impact product preference becomes necessary;
- two evidence-supported architectures remain viable for different priorities;
- legal/privacy/licensing constraints require a human decision;
- an expensive research fork needs authorization.

Do not repeatedly grill the user for facts the system can research itself.

---

# 10. End-to-end research state machine

Every phase has typed inputs, outputs and gates. The graph should be inspectable and resumable.

## Phase 0 — Bootstrap

Create:
- `run_id`;
- immutable configuration snapshot;
- selected provider/model policy versions;
- exact model identifiers and reasoning/config parameters;
- prompt/template versions and hashes;
- tool/MCP server identities and versions where obtainable;
- parser/indexer/embedding model versions;
- user objective;
- source/access/crawl/licensing policy;
- budget envelope;
- research schema version;
- evaluator/judge versions and calibration set IDs;
- parent/prior-run IDs if this run reuses earlier knowledge.

Gate: configuration valid and persistent stores reachable.

## Phase 1 — Breadth interview

Output `IntentSnapshot`.

Gate: enough information exists to formulate multiple reconnaissance queries without guessing core user preferences.

## Phase 2 — Reconnaissance fan-out

Generate independent perspective/scout nodes. Suggested perspective families:
- implementation mechanisms;
- closest existing systems;
- alternatives/competing approaches;
- empirical evidence/benchmarks;
- failure/security/licensing risks;
- source-of-truth repositories/docs/papers.

Perspective count is **dynamic**; do not hardcode a universal number.

Gate:
- diversity target reached;
- obvious duplicate branches merged;
- key terminology stabilized enough to plan deeper research.

## Phase 3 — Research DAG synthesis

Each `ResearchTask` must define:
- task ID;
- question;
- parent question;
- why it matters to implementation;
- expected artifact/evidence type;
- source types;
- dependencies;
- priority;
- breadth/depth allowance;
- model/tool policy;
- acceptance criteria;
- verification policy;
- stopping conditions;
- retry/escalation rules;
- expected cost envelope.

Gate: every high-impact question is represented or explicitly deferred.

## Phase 4 — Focused clarification

Use question-utility policy. Record answers as `USER_DECISION` or `USER_CONSTRAINT`, never as external evidence.

Gate: no unresolved user-only blocker prevents branch selection.

## Phase 5 — Deep branch execution

Dispatch typed investigators. Each investigator can generate child research questions only through the scheduler contract.

Gate per branch: required artifacts/evidence are staged and branch acceptance conditions are met or the branch is explicitly incomplete/blocked.

## Phase 6 — Evidence normalization and commit

Worker results are **not canonical immediately**.

Commit protocol:
1. stage raw artifacts in a run/task-scoped staging namespace;
2. hash and register source/version plus acquisition policy metadata;
3. record the acquisition activity: tool/provider/version/query/request parameters and time;
4. parse/normalize and record parser/model derivations;
5. validate required provenance and locator fields;
6. detect exact duplicates, near duplicates, mirrors, and derivative sources;
7. create evidence units with stable IDs where possible;
8. execute the canonical publication transaction;
9. update graph node with committed canonical IDs;
10. only then mark worker execution successful.

If any publication step fails, the branch remains `STAGED`/`COMMIT_FAILED`; staged material can be retried or garbage-collected but does not count as verified research.

Retrying a node must be idempotent with respect to source/version/content identity. Concurrent workers must not create logically duplicate canonical evidence merely because they discovered the same source independently.

## Phase 7 — Claim construction

Synthesis workers convert evidence into claims while preserving:
- exact supporting evidence IDs;
- contradictions;
- inference type;
- source independence;
- freshness;
- implementation relevance.

Gate: no major synthesis statement exists only in model prose without a claim record.

## Phase 8 — Source & Claim Verification Gate

Run deterministic and model-assisted validation (Section 20).

Gate: high-impact claims meet source-type-specific support rules, or remain explicitly unresolved/contested.

## Phase 9 — Topic and implementation synthesis

Construct:
- topic hierarchy;
- implementation entity graph;
- component/dependency map;
- architecture alternatives;
- algorithm mappings;
- decision candidates;
- unresolved gaps.

## Phase 10 — Independent gap/contradiction review

Critic receives canonical evidence/claims and current synthesis, not the original researcher's hidden reasoning.

It must ask:
- what important implementation question has no answer?
- what answer rests on a single fragile source?
- which source families are derivative rather than independent?
- what code path was inferred but not actually inspected?
- what equation was parsed but not visually checked?
- what browser observation was upgraded improperly into source-code fact?
- what recommendation conflicts with a user constraint?
- where did the system stop only because the budget expired?

Output: prioritized `ResearchGap[]`.

## Phase 11 — Targeted re-research loop

Only reopen branches linked to high-impact gaps or contradictions.

This loop terminates when:
- blocking gaps are resolved;
- remaining gaps are explicitly documented;
- marginal novelty is low relative to cost;
- budget is exhausted and status is marked **INCOMPLETE**, not “done.”

## Phase 12 — Architecture/implementation decision synthesis

Generate formal decisions with:
- question;
- alternatives;
- evidence;
- user preference dependencies;
- chosen option;
- rationale;
- consequences;
- unresolved uncertainty;
- reversal triggers.

## Phase 13 — Handoff generation

Generate human and machine-readable artifacts from canonical research state.

## Phase 14 — Handoff audit

Final independent review checks:
- every major implementation claim has evidence;
- all exact repository/paper/browser locators are resolvable;
- dependency/version assumptions are explicit;
- known contradictions remain visible;
- inferred behavior is labeled;
- user constraints are carried forward;
- architecture decisions cite their evidence;
- coding agents can query supporting knowledge without rereading the entire run.

---

# 11. Typed worker topology

Workers are **execution modes with contracts**, not anthropomorphic personas.

## 11.1 Scout Worker

Purpose:
- rapid discovery;
- terminology expansion;
- alternative generation;
- source candidate collection.

Default cost tier: cheap model + search provider.

Must not:
- make final architecture decisions;
- treat search-engine synthesis as primary evidence;
- crawl deeply unless promoted to an investigator task.

Output:
- source candidates;
- candidate approaches;
- research questions;
- relevance rationale;
- initial source metadata.

## 11.2 Repository Investigator

Purpose: produce implementation-grade understanding of a repository snapshot.

Required inspection baseline:
- exact repository URL;
- branch/tag and commit SHA;
- submodules;
- dependency/lock manifests;
- repository tree;
- build entry points;
- application/library entry points;
- public APIs;
- core modules/classes/functions;
- data/control flow relevant to the task;
- configuration;
- persistence/state;
- tests;
- examples;
- extension/plugin points;
- CI/build scripts;
- relevant issue/PR/history context when it explains architecture.

Optional tools depending on language/task:
- GitHub API;
- git history/blame;
- ripgrep;
- tree-sitter;
- LSP/reference tools;
- call graphs;
- package/dependency analyzers;
- static type analysis;
- targeted test execution.

Output must include exact `repo@sha:path:symbol` locators wherever possible.

### README-bias rule
A repository branch cannot be considered implementation-ready if its major conclusions come only from README/docs. It must inspect relevant source and tests.

## 11.3 Paper Investigator

Purpose: convert a paper into an implementable model rather than a narrative summary.

Inspect:
- bibliographic identity/version;
- abstract/introduction only for orientation;
- method sections;
- equations;
- pseudocode;
- figures/tables;
- appendices/supplements;
- training/evaluation details;
- hyperparameters;
- complexity claims;
- limitations;
- cited predecessor methods when required;
- official code if available;
- issue/replication notes where relevant.

Output:
- algorithm decomposition;
- variable/state definitions;
- equation → computation mapping;
- data/tensor shapes when inferable;
- boundary/initialization rules;
- hyperparameters;
- computational complexity;
- required dependencies;
- reproduction checklist;
- paper/code discrepancies;
- unresolved ambiguities.

## 11.4 Browser / DOM Investigator

Purpose: gather **observed** implementation clues unavailable from text-only extraction.

Escalation ladder:
1. search metadata/snippet;
2. extracted clean text;
3. raw HTML;
4. rendered DOM;
5. accessibility tree;
6. interactive browser session;
7. screenshots/visual state sequence;
8. network/HAR metadata where authorized and necessary.

Evidence labels must distinguish:
- direct DOM observation;
- visible UI observation;
- accessibility-tree observation;
- network-observed behavior;
- inferred frontend architecture;
- speculation.

**Never equate DOM/network observation with private source-code truth.**

## 11.5 Evidence Curator

Prefer deterministic processing first.

Responsibilities:
- normalization;
- chunk/symbol segmentation;
- content hashing;
- deduplication;
- derivative-source clustering;
- source metadata;
- evidence locators;
- embedding creation;
- parser-version attachment;
- freshness and staleness metadata.

Use LLMs only for semantic normalization/classification that deterministic code cannot handle reliably.

## 11.6 Claim Builder

Turns evidence into minimal canonical claims.

Must separate:
- directly observed fact;
- execution-verified behavior;
- cross-source conclusion;
- inference;
- recommendation.

## 11.7 Verification Worker

Runs the Source & Claim Verification Gate independently from synthesis.

## 11.8 Synthesis Worker

Builds topic/architecture understanding from verified or explicitly qualified claims.

## 11.9 Gap/Critic Worker

Attempts to falsify or identify missing pieces rather than improve prose.

## 11.10 Handoff Writer

Writes from structured state. It must not browse independently unless the graph explicitly reopens a research task.

## 11.11 Handoff Auditor

Checks evidence coverage, exact locators, decision completeness and unresolved gaps. It can reject a polished handoff.

---

# 12. Research-task and branch scheduling model

Adaptive breadth/depth scheduling is **custom architecture**, not established as the uniquely correct strategy by the cited systems. Treat it as a tunable policy.

## 12.1 Branch creation criteria

A child branch must state:
- question;
- evidence that triggered it;
- implementation impact;
- expected source type;
- expected novelty;
- estimated cost;
- parent dependency;
- completion condition.

The supervisor rejects branches that are merely interesting but unlikely to change implementation understanding.

## 12.2 Priority dimensions

Prioritize using structured dimensions rather than one opaque confidence score:
- implementation impact;
- blocker status;
- uncertainty;
- evidence weakness;
- contradiction severity;
- source availability;
- novelty;
- cost;
- dependency centrality.

Weights are a **heuristic** and must be calibrated.

## 12.3 Breadth increase triggers

Increase breadth when:
- solution space is poorly defined;
- multiple ecosystem families exist;
- current results cluster around one vendor/domain;
- sources disagree;
- user asks for alternatives;
- critic detects premature convergence.

## 12.4 Depth increase triggers

Increase depth when:
- implementation directly depends on exact behavior;
- code extension points are unclear;
- algorithm details are ambiguous;
- evidence is weak/contradictory;
- a handoff requires exact interfaces;
- parser/DOM observations are insufficient.

## 12.5 Stopping criteria

A research branch may close only when:
- acceptance questions are answered or explicitly marked unresolved;
- required source-type locators are present;
- high-impact claims pass verification policy;
- contradictions are reconciled or documented;
- incremental search has low novelty/duplicate rate;
- remaining gaps are classified;
- budget status is explicit.

Budget exhaustion alone yields **INCOMPLETE_BUDGET**, not COMPLETE.

---

# 13. Canonical source, artifact, evidence and knowledge model

A vector index is a retrieval feature, not the canonical data model.

## 13.1 `Source`

Fields:
- `source_id`;
- canonical URI/repository/DOI/etc.;
- source type;
- publisher/owner;
- first-party / third-party / UGC;
- primary / secondary / tertiary;
- publication/update date;
- retrieval timestamp;
- repository commit/tag or paper version;
- license/access constraints;
- domain/source family;
- independence cluster ID;
- derivative-of relationships;
- acquisition provider/query;
- checksum where meaningful.

Do **not** collapse these dimensions into one “trust score.”

## 13.2 `RawArtifact`

Raw captures are immutable/content-addressed.

Fields:
- artifact ID;
- source ID;
- hash;
- MIME/type;
- object path;
- capture method/tool version;
- capture timestamp;
- source version/headers where available;
- parent artifact if derived from another artifact;
- access policy.

Examples:
- HTML response;
- rendered DOM snapshot;
- screenshot;
- HAR;
- PDF;
- git bundle/snapshot metadata;
- test output;
- API JSON.

## 13.3 `DerivedArtifact`

Unlike raw captures, derived artifacts are versioned and replaceable.

Fields:
- derived artifact ID;
- parent raw artifact IDs;
- parser/tool/model version;
- transformation configuration;
- created time;
- supersedes/superseded-by;
- validity/staleness status.

Examples:
- parsed Docling document;
- GROBID TEI;
- extracted DOM tree;
- symbol index;
- normalized table;
- OCR output.

## 13.4 `EvidenceUnit`

Smallest independently attributable observation useful to support a claim.

Fields:
- evidence ID;
- source and artifact IDs;
- exact locator;
- normalized statement/observation;
- optional raw excerpt reference;
- evidence status/type;
- parser confidence if applicable;
- freshness;
- embedding;
- related implementation entities;
- verification state.

Locators by source:
- repo: commit + path + symbol/line span;
- paper: version + page/section/equation/figure/table;
- web: canonical URL + captured artifact + DOM/text locator;
- browser: timestamp + page state + selector/accessibility node/screenshot/HAR event;
- execution: environment manifest + command/test + output hash.

## 13.5 Evidence status vocabulary

Use explicit semantics:
- `RAW_CAPTURE`;
- `PARSED_EXTRACTION`;
- `DIRECT_TEXT_OBSERVATION`;
- `DIRECT_CODE_OBSERVATION`;
- `DIRECT_DOM_OBSERVATION`;
- `DIRECT_NETWORK_OBSERVATION`;
- `EXECUTION_VERIFIED`;
- `CROSS_SOURCE_VERIFIED`;
- `INFERENCE`;
- `USER_DECISION`;
- `USER_CONSTRAINT`;
- `RECOMMENDATION`;
- `CONTRADICTED`;
- `UNRESOLVED`.

## 13.6 `Claim`

Fields:
- claim ID;
- canonical minimal statement;
- claim class;
- support evidence IDs;
- contradiction evidence IDs;
- source independence clusters;
- entailment status;
- provenance directness;
- freshness;
- reproducibility status;
- implementation impact;
- verification timestamps/worker versions;
- state: verified / qualified / contested / unsupported / stale / obsolete.

Avoid one model-generated scalar confidence field as the main truth signal.

## 13.7 `ImplementationEntity`

Fields:
- entity ID;
- source/repository/version;
- type: module/class/function/interface/config/schema/endpoint/component/etc.;
- exact locator;
- signature/API shape;
- dependencies;
- relationships;
- tests/examples;
- related evidence/claims;
- implementation significance;
- extension/replaceability notes.

## 13.8 `TopicNode`

Fields:
- topic ID;
- parent/child links;
- title;
- canonical summary generated from current claims;
- semantic embedding;
- related topics;
- implementation entities;
- source coverage;
- open gaps;
- summary version and source claim IDs.

Topic summaries must be regenerable from canonical claims rather than recursively summarizing old summaries.

## 13.9 `DecisionRecord`

Fields:
- decision ID;
- question;
- alternatives;
- evidence/claims;
- user preference dependency;
- selected alternative;
- rationale;
- consequences;
- uncertainties;
- reversal trigger;
- decision owner/time/version.

## 13.10 `ResearchGap`

Fields:
- gap ID;
- question;
- impact;
- blocker status;
- resolvability: research / user / experiment / unavailable;
- linked claims/tasks/decisions;
- current state;
- closure evidence;
- reason if accepted unresolved.

## 13.11 `DerivationActivity`

Every transformation that can affect meaning must be represented explicitly.

Fields:
- `activity_id`;
- activity kind: acquire / parse / OCR / render / extract / normalize / embed / summarize / claim-build / verify / synthesize / handoff-render;
- input entity/artifact IDs;
- output entity/artifact IDs;
- tool/provider/model identity;
- exact model/tool version where available;
- prompt/template hash if an LLM was involved;
- non-secret parameters affecting output;
- start/end timestamps;
- run/task/worker IDs;
- success/failure state;
- deterministic/non-deterministic flag;
- optional reproducibility notes.

This follows the same conceptual separation as W3C PROV's entities, activities and agents [R23] without requiring a specific serialization.

## 13.12 `AgentIdentity`

Here “agent” means the accountable generating actor, not an anthropomorphic role.

Fields:
- `agent_id`;
- type: model / deterministic tool / parser / browser / human / external service;
- provider;
- product/version;
- role;
- authorization scope;
- configuration/prompt revision;
- run/task ownership.

## 13.13 `SourceLineage`

Required to address false source diversity.

Fields/relations:
- source family/domain;
- `derivative_of`;
- `cites`;
- copied/near-duplicate cluster;
- UGC/first-party marker;
- retrieval queries/providers;
- independence cluster ID;
- content similarity/hash cluster.

Multiple pages that derive from one press release should count as one evidentiary lineage for corroboration purposes.

---

# 14. Invalidation and freshness

A research corpus that cannot become stale explicitly will eventually mislead coding agents.

## 14.1 Source identity / freshness signals

Repository:
- exact commit SHA;
- branch/tag;
- dependency lock hashes;
- submodule SHAs.

Paper:
- DOI/arXiv ID;
- version;
- PDF hash;
- supplement/code version.

Web:
- URL;
- retrieval time;
- content hash;
- ETag/Last-Modified when available;
- rendered-artifact hash.

Parser:
- parser version/model;
- configuration;
- derived artifact version.

## 14.2 Dependency-based invalidation

Maintain downstream edges:

`RawArtifact → DerivedArtifact → EvidenceUnit → Claim → TopicSummary/Decision/HandoffSection`

When an upstream artifact changes or parser output is superseded:
- mark downstream objects `STALE_PENDING_REVIEW`;
- do not silently delete historical state;
- regenerate affected summaries/claims selectively;
- preserve the version used by previous handoffs.

---

# 15. Repository analysis subsystem

Repository research is the highest-priority specialist for this product because the main output is intended for coding/orchestrator agents.

## 15.1 Snapshot discipline

Every branch binds findings to:
- repo URL;
- commit SHA;
- branch/tag context;
- submodules;
- lockfiles;
- relevant toolchain/runtime versions.

## 15.2 Layered investigation

1. **Inventory** — languages, frameworks, manifests, directory topology.  
2. **Entry-point discovery** — CLI/server/library/UI/bootstrap locations.  
3. **API/extension surface** — exported interfaces, plugins, adapters, hooks.  
4. **Data/state flow** — persistent stores, message/event state, schemas.  
5. **Target-flow tracing** — exact call/control path relevant to requested extension.  
6. **Tests/examples** — actual expected behavior and supported usage.  
7. **Build/deploy** — package, CI, environment, platform assumptions.  
8. **History/rationale** — issues/PRs/blame only where current code leaves architectural intent unclear.  
9. **Validation** — targeted static/dynamic tests if safe.  
10. **Handoff map** — files/symbols/interfaces to reuse, modify, wrap or avoid.

## 15.3 Indexing strategy

Do not depend on fixed-size text chunks alone.

Use semantic units:
- module;
- class;
- function/method;
- interface/type;
- test case;
- config declaration;
- schema;
- endpoint;
- build definition.

Store structural edges such as:
- contains;
- imports;
- calls;
- implements;
- tests;
- configures;
- instantiates;
- references.

Baseline parser: tree-sitter where suitable. Add language-specific LSP/static tooling when the language/task requires precise references. The use of tree-sitter is a practical baseline, not a claim that it is sufficient for every language.

## 15.4 Behavioral-claim support policy

A claim about repository behavior should generally require:
- exact commit/path/symbol observation;
- plus test/example/runtime evidence when behavior rather than structure is implementation-critical.

If behavior is inferred statically without execution, label it as inference.

---

# 16. Paper / academic subsystem

## 16.1 Complementary parser pipeline

Use:
- **GROBID** for scholarly structure, bibliographic metadata, references/citation contexts and document segmentation [R19];
- **Docling** for rich layout, tables, formulas, pictures and structured document/provenance representation [R18];
- direct PDF text/page rendering as a fallback/reference;
- multimodal review for implementation-critical visual content.

Do not assume either parser is authoritative.

## 16.2 Critical-content verification rule

For an equation/table/figure that materially affects implementation:
- preserve the original page image;
- retain parser outputs;
- compare parser result to visual source;
- if parsers disagree or confidence is low, create a gap;
- use multimodal inspection or human review before treating it as verified.

## 16.3 Implementation mapping contract

A theoretical method is not “implementation ready” until the corpus records:
- inputs/outputs;
- variables/state;
- update equations/algorithm steps;
- initialization/boundary conditions;
- shapes/types when inferable;
- complexity;
- numerical assumptions;
- hyperparameters/defaults;
- preprocessing;
- training/inference distinction;
- evaluation procedure;
- official implementation differences;
- unresolved details.

## 16.4 Paper/code reconciliation

If official code exists:
- bind to commit/version;
- compare pseudocode and equations to implementation;
- inspect defaults/configs;
- inspect preprocessing and evaluation scripts;
- record undocumented behaviors;
- treat discrepancies as explicit claims/gaps, not silent corrections.

---

# 17. Web / browser / DOM subsystem

## 17.1 Retrieval-to-browser escalation

Prefer the cheapest source representation that can answer the research question:

1. ranked search result;
2. extracted text/highlights;
3. full page content;
4. site map/crawl;
5. raw HTML;
6. rendered DOM;
7. accessibility tree;
8. interactive Playwright session;
9. screenshots/state sequence;
10. network/HAR observation where authorized.

## 17.2 Provider routing

Provider capabilities justify task-based routing rather than a universal “primary search engine”:

- **Exa:** semantic/conceptual search, multiple search depth modes, content/highlight retrieval; good candidate for concept neighbors, papers and technical discovery [R10].
- **Perplexity Search API:** raw ranked web results suitable for custom pipelines where this harness owns synthesis [R11].
- **Tavily:** Map/Crawl/Extract-style site-structure and targeted content workflows; useful for documentation sites [R12].
- **Firecrawl:** scrape/crawl/map/search/extract capabilities and difficult page extraction [R13].
- **Playwright/local Chromium:** canonical rendered-interaction interface when content APIs are insufficient.
- **Managed browser providers:** optional for remote/session reliability; not a canonical dependency.

## 17.3 Managed deep-research services

Perplexity/Sonar-style deep research, Exa deep reasoning, Tavily research agents, etc. may be used as:
- reconnaissance accelerators;
- alternate hypothesis generators;
- adversarial second opinions.

They should **not** become canonical evidence objects by themselves because they collapse retrieval and synthesis. Important claims should ingest and verify their underlying cited sources.

## 17.4 Site-observation semantics

A site investigation can legitimately document:
- rendered DOM hierarchy;
- accessible roles/names;
- visible interaction states;
- public network request patterns;
- public asset metadata;
- timing/state observations.

It must not claim private implementation details that are not observable.

---

# 18. Search and extraction provider interface

The architecture requires a provider abstraction.

## `SearchProvider`
Capabilities:
- ranked search;
- query filters;
- date/domain/source filters;
- multi-query;
- semantic/keyword modes;
- source metadata;
- optional highlights/full text.

## `SiteMapProvider`
Capabilities:
- discover site/page topology;
- respect scope/domain rules;
- provide candidate URLs without necessarily extracting all pages.

## `ContentProvider`
Capabilities:
- fetch/extract;
- raw artifact preservation;
- full text/HTML;
- content metadata;
- dynamic-rendering indicator;
- page sub-resource discovery.

## `BrowserProvider`
Capabilities:
- session create/close;
- navigate;
- DOM snapshot;
- accessibility snapshot;
- screenshot;
- controlled interaction;
- network capture;
- artifact export.

## Provider selection benchmark

Before locking a deployment default, benchmark providers on the product’s own corpus for:
- relevant-source recall;
- source diversity;
- primary-source rate;
- duplicate/derivative rate;
- extraction fidelity;
- dynamic-page success;
- citation locator quality;
- latency;
- cost;
- failure rate.

---

# 19. Source-specific evidence rules

One universal “two sources per claim” rule is wrong. Evidence requirements depend on claim type.

## 19.1 Repository structural claim

Minimum:
- exact repository commit;
- path/symbol or manifest locator.

## 19.2 Repository behavioral claim

Prefer:
- exact source locator;
- relevant test/example;
- execution verification if safe and material.

Otherwise label as static inference.

## 19.3 Paper algorithm claim

Minimum:
- paper version;
- exact section/equation/algorithm locator.

When implementation-critical:
- appendix/official code corroboration if available;
- visual verification for critical parsed equations/figures.

## 19.4 Current web/product fact

For high-impact facts:
- prefer one authoritative first-party source;
- otherwise use multiple independent evidence lineages;
- do not count derivative reposts as independent corroboration.

## 19.5 UGC / forum / issue claim

UGC may be valuable for:
- bug symptoms;
- undocumented edge cases;
- practitioner experience.

But high-impact general claims should be corroborated. Repeated retrieval of one UGC source should not increase its evidentiary weight [R8].

## 19.6 DOM/UI behavior claim

Require:
- capture timestamp;
- page state/URL;
- DOM/accessibility/screenshot/network artifact locator as appropriate;
- label observation vs inference.

---

# 20. Source & Claim Verification Gate

This is one of the most important v2 additions.

## 20.1 Why a separate gate is required

Deep-research evaluation shows that citation links can be valid/relevant without providing factual support and that misleading evidence can induce large errors [R6][R7]. Therefore verification cannot be an optional “critic prompt.”

## 20.2 Verification layers

### Layer 1 — deterministic source integrity
Check:
- source/artifact exists;
- captured version/hash resolves;
- locator resolves;
- repository SHA/path/symbol exists;
- URL/artifact mapping is intact;
- PDF page/equation locator exists;
- parser provenance exists.

### Layer 2 — relevance
Does the cited evidence address the claim subject?

### Layer 3 — factual entailment/support
Does the evidence actually support the canonical claim as written?

Use calibrated LLM judges for scale, but retain benchmarked false-positive/false-negative rates. Recent work suggests frontier models are not automatically necessary for citation-verification tasks and cheaper rubric models can be competitive, while evaluator biases still require calibration [R22].

### Layer 4 — provenance directness
Is the evidence:
- primary/direct;
- secondary;
- derivative;
- UGC;
- inferred from observed behavior?

### Layer 5 — independence
Do multiple supporting sources come from distinct evidence lineages?

### Layer 6 — contradiction/reconciliation
Search for and attach contradictory evidence. Do not “majority vote” blindly; resolve by source authority, version, directness and experimental reproduction.

### Layer 7 — execution/visual verification
For high-impact implementation claims:
- run test/reproduction where safe;
- visually validate equation/table/DOM claims where parser/extraction uncertainty is material.

## 20.3 Verification outcome

Possible states:
- `VERIFIED_DIRECT`;
- `VERIFIED_EXECUTION`;
- `VERIFIED_CROSS_SOURCE`;
- `QUALIFIED_INFERENCE`;
- `CONTESTED`;
- `UNSUPPORTED`;
- `STALE`;
- `PARSER_UNCERTAIN`;
- `BLOCKED_VERIFICATION`.

## 20.4 Anti-citation-laundering behavior

If source B merely restates source A, the claim provenance should point through that lineage rather than treating A and B as independent confirmations.

---

# 21. Canonical publication, provenance, and correction semantics

This section is an implementation-critical addition from the final audit.

## 21.1 Staged vs canonical state

Research workers never write directly into “truth.”

States:
- `DISCOVERED`: source candidate exists only as a pointer.
- `FETCHED`: raw capture exists but has not passed schema/provenance validation.
- `STAGED`: normalized evidence has been produced inside a task-scoped staging transaction.
- `CANONICAL`: publication transaction succeeded.
- `VERIFIED`: required source/claim checks passed.
- `SUPERSEDED`: newer source/version or correction exists.
- `STALE`: freshness/invalidation policy says revalidation is required.
- `REJECTED`: invalid, corrupted, unsupported, policy-disallowed, or proven misleading.

Only `CANONICAL` or stronger records may support synthesis. High-impact handoff claims require `VERIFIED` evidence under their source-specific policy.

## 21.2 Provenance graph

For every handoff statement, the system should be able to reconstruct:

`handoff statement`
→ `decision/claim`
→ `verification result`
→ `evidence unit`
→ `derived artifact`
→ `raw artifact`
→ `source identity/version`
→ `acquisition activity`
→ `tool/model/human agent`
→ `run/task`

This is not decorative metadata. It enables:
- reproducibility;
- stale-source invalidation;
- forensic debugging;
- judge recalibration;
- source licensing checks;
- detection of recursive summary drift;
- downstream agent trust decisions.

W3C PROV is strong evidence that entity/activity/agent/derivation semantics are a mature general model for provenance [R23]. The product can use a simpler relational schema while retaining these semantics.

## 21.3 Human correction semantics

Human inputs are typed:

- `USER_PREFERENCE`;
- `USER_CONSTRAINT`;
- `USER_ASSERTION`;
- `MAINTAINER_ASSERTION`;
- `USER_CORRECTION`;
- `USER_ACCEPTED_RISK`.

Rules:
- preferences/constraints can directly shape decisions;
- factual assertions do not silently become externally verified facts;
- corrections supersede earlier human assertions by link, never destructive overwrite;
- if a human correction conflicts with source evidence, both remain visible and the contradiction is routed to resolution;
- the final handoff states which conclusions are evidence-backed versus user-directed.

## 21.4 Cross-run reuse

Previous research can reduce cost, but it creates a contamination risk.

On reuse:
1. preserve original `run_id`, source/version, and derivation lineage;
2. label retrieved prior claims `PRIOR_KNOWLEDGE`;
3. do not count prior summaries as independent corroboration;
4. re-fetch/revalidate source material when freshness matters;
5. permit exact version-pinned immutable artifacts (for example a Git SHA) to be reused without unnecessary network retrieval if hashes and artifact integrity match;
6. propagate staleness to any new decisions that depend on stale prior claims.

This prevents a system from repeatedly citing its own prior synthesis until it appears independently corroborated.

---

# 22. Source access, compliance, and acquisition policy

This architecture is designed for practical implementation research, so source access rules must be executable policy.

## 22.1 Access-basis record

Each source records:
- public / authenticated / user-provided / organization-internal;
- acquisition mechanism;
- authorization scope;
- robots/crawl policy result when automated crawling applies;
- license/SPDX expression when relevant;
- redistribution/storage constraints;
- retention classification;
- whether downstream agents may receive raw content or only derived references.

## 22.2 Crawling policy

RFC 9309 standardizes the Robots Exclusion Protocol for automated crawlers and explicitly distinguishes robots controls from authorization [R25].

Therefore:
- obey deployment-configured robots policy for crawler-style traversal;
- do not treat robots permission as authentication or legal authorization;
- obey rate limits and bounded concurrency;
- use sitemap/map endpoints where available before blind crawling;
- keep a crawl manifest so the system can explain which pages were attempted/skipped and why.

## 22.3 Repository/license policy

Use SPDX identifiers/expressions when licenses can be resolved [R26].

Record separately:
- license detected;
- source of license metadata;
- file-level overrides if relevant;
- dependency license notes;
- whether code/text may be copied into persistent artifacts;
- whether final handoff should describe a pattern rather than reproduce protected source material.

This is important for the stated use case of building forks/extensions/inspired implementations.

## 22.4 Authenticated tools and MCP

MCP authorization guidance requires resource/audience binding and explicitly forbids token passthrough between services [R24]. MCP's broader security guidance treats tools as potentially arbitrary code execution and calls for consent/access controls [R31].

Policy:
- credentials are owned by the control plane/secret manager, never by retrieved content;
- each MCP server receives only its own audience-bound credentials;
- workers receive capability handles rather than reusable raw tokens where possible;
- read/write scopes are explicit;
- tool descriptions and server-returned text remain untrusted;
- write/side-effect tools require a separate policy path from research-only tools;
- every invocation is auditable.

---

# 23. Knowledge organization and retrieval

The desired “RAG-like similarity grouped and organized hierarchical understanding” should be implemented as **multiple projections over canonical evidence**, not one monolithic vector corpus.

## 21.1 Retrieval layers

1. **Exact/structural retrieval** — source ID, commit, path, symbol, DOI, URL, decision/gap ID.  
2. **Lexical retrieval** — full-text search for identifiers/phrases.  
3. **Vector retrieval** — semantic similarity over evidence, claims, topics and implementation entities.  
4. **Hierarchy traversal** — parent/child topic context.  
5. **Relationship traversal** — calls/imports/supports/contradicts/depends-on.  
6. **Filtered retrieval** — source version, status, freshness, evidence type, task/use case.  
7. **Handoff-first retrieval** — start from curated implementation docs, then drill to knowledge/evidence.

## 21.2 Topic hierarchy construction

Topic hierarchy should combine:
- semantic clustering;
- explicit source structure;
- implementation architecture;
- research-task structure;
- manual/user concepts.

Do not let embedding clusters alone define hierarchy.

## 21.3 Similarity deduplication

Use content hashes where exact duplicate; embeddings/minhash/text similarity for near duplicates; source lineage to avoid false corroboration.

## 21.4 Summary regeneration

Topic summaries record their input claim IDs and generation version. When claims become stale/contested, affected summaries are regenerated rather than edited invisibly.

---

# 24. Context management

## 22.1 Context window as a working cache

Model context should contain only what the current task requires:
- task objective;
- parent objective;
- user constraints relevant to this task;
- acceptance criteria;
- retrieved claims/evidence;
- related implementation entities;
- current gaps;
- allowed tools;
- output schema;
- budget/status.

Everything else lives in durable storage.

## 22.2 Context packet contract

Every substantive worker invocation receives a versioned `ContextPacket` with canonical IDs rather than free-form copied summaries.

## 22.3 Never compress away

Preserve exactly:
- file paths;
- symbol names/signatures;
- commit/version identifiers;
- dependency versions;
- equations/variables;
- measured values;
- source locators;
- contradictions;
- user constraints;
- explicit assumptions;
- decision/reversal conditions.

## 22.4 Safe compression targets

Compress:
- repetitive browsing history;
- low-value tool chatter;
- duplicate source descriptions;
- already normalized raw prose;
- verbose intermediate explanations.

## 22.5 Summary-drift defense

Do not recursively summarize summaries when canonical evidence/claims are available. Periodically regenerate current summaries from canonical records.

---

# 25. Model architecture and economics

## 23.1 Corrected principle

The architecture should **not** assume that high-frontier models occupy most roles.

Current provider pricing varies substantially. Anthropic currently lists Claude Sonnet 5 at $2/MTok input and $10/MTok output, Opus 5 at $5/$25, and Haiku 4.5 at $1/$5 [R15]. OpenAI explicitly positions lower-cost GPT-5.6 tiers for high-volume workloads and frontier Sol for premium cases [R14]. Google provides lower-cost Gemini tiers and search grounding options [R16]. These price differences make routing economically meaningful.

Anthropic’s own cost/intelligence guidance describes an executor/advisor pattern but warns that the benefit is workload- and consultation-rate-dependent; therefore it should be benchmarked, not assumed [R17].

## 23.2 Candidate pools, not permanent assignments

### Pool C — high-volume/cheap candidates
Evaluate:
- GPT-5.6 Luna;
- Gemini 3.5 Flash-Lite or current equivalent;
- Claude Haiku-class/current inexpensive Anthropic model;
- locally hosted model where privacy/cost makes sense.

Candidate tasks:
- query expansion;
- source triage;
- tagging;
- routine extraction normalization;
- low-risk clarification turns;
- duplicate classification;
- routine citation relevance/entailment if benchmarked.

### Pool B — technical workhorse candidates
Evaluate:
- Claude Sonnet 5;
- GPT-5.6 Terra;
- Gemini 3.5 Flash/current capable Gemini equivalent;
- other provider model that scores well on internal tasks.

Candidate tasks:
- repository investigation;
- paper interpretation;
- browser/DOM reasoning;
- branch planning;
- bounded synthesis;
- gap detection;
- handoff drafting.

### Pool A — sparse frontier/advisor candidates
Evaluate:
- Claude Opus 5;
- GPT-5.6 Sol;
- current top-performing model from any supported provider.

Candidate triggers:
- high-impact architecture fork;
- unresolved disagreement between workhorse investigators;
- hard mathematics/code reasoning;
- repeated branch failure;
- final adversarial audit;
- a measured quality jump large enough to justify cost.

## 23.3 Remove the “<10% frontier token” rule

The prior version proposed a numeric frontier-token target. That was not research-backed and is removed.

Replace it with **marginal-value routing**:
- measure quality gain from escalation;
- measure incremental cost/latency;
- learn per-task escalation thresholds;
- cap consultation only when the marginal gain no longer justifies cost.

## 23.4 Advisor pattern

A useful experiment:
1. Pool B worker executes task.
2. Pool A advisor receives task state plus concise canonical evidence/claims.
3. Advisor returns strategy/corrections/gaps only.
4. Pool B worker continues.

Benchmark against:
- Pool B alone;
- Pool A alone at lower reasoning effort;
- Pool B + Pool A advisor at several consult rates.

Do not assume advisor mode wins; [R17] explicitly motivates task-specific evaluation.

## 23.5 Search-specialized systems vs reasoning models

Use search systems aggressively for discovery and extraction. They reduce the need to spend general-model tokens on retrieval mechanics [R10][R11][R12][R13].

They do **not** remove the need for technical reasoning over:
- repository control/data flow;
- paper/code reconciliation;
- architecture consequences;
- cross-source contradictions;
- source-type-specific implementation decisions.

---

# 26. Model/provider evaluation suite

Do not finalize provider/model assignments before this exists.


## 26.0 External benchmark triangulation

Public benchmarks should stress components, but must not become the product objective.

- **BrowseComp**: hard-to-find web information requiring strategic browsing; useful for scout/search-path quality, but it does not test implementation handoff quality [R27].
- **RepoProbe**: open-ended architecture-aware repository comprehension based on real GitHub Discussions and atomic checklist verification; particularly aligned with the repository-understanding use case [R28].
- **RepoBench** may be retained as a narrower retrieval/cross-file context benchmark, but it is less directly aligned than RepoProbe because its core tasks center on repository-level code completion/retrieval rather than architectural explanation.
- **SWE-bench / SWE-bench-Live**: useful as downstream coding-agent utility tests after research handoff; they test issue resolution, not research quality itself [R29][R30].

Do not optimize the research harness directly against any single benchmark. A benchmark can improve while the handoff becomes worse through overfitting or task mismatch.


## 24.1 Repository benchmark set

Representative tasks should measure:
- exact relevant file/symbol recall;
- extension-point correctness;
- architecture-map correctness;
- dependency identification;
- test/example use;
- false implementation claims;
- task completion cost/latency.

Ground truth should be reviewed by engineers or extracted from maintainers/tests where possible.

## 24.2 Paper benchmark set

Measure:
- equation/pseudocode mapping;
- algorithm-step completeness;
- hyperparameter extraction;
- paper/code discrepancy detection;
- reproduction-plan correctness;
- parser-error handling;
- citation support.

## 24.3 Website/DOM benchmark set

Measure:
- DOM/component/state observation accuracy;
- accessibility structure capture;
- dynamic-page success;
- distinction between observation and inference;
- network observation correctness;
- prompt-injection resilience.

## 24.4 Cross-cutting benchmark

Measure:
- citation entailment;
- source independence/diversity;
- contradiction discovery;
- gap detection;
- duplicate work rate;
- branch novelty;
- downstream coding-agent success;
- end-to-end cost;
- p50/p95 latency;
- retry/failure rate.

## 24.5 Downstream utility benchmark

Most important end-to-end metric:

Give coding agents either:
- baseline raw sources;
- ordinary report;
- this system’s handoff + queryable evidence.

Measure:
- implementation correctness;
- number of re-research calls;
- time to first correct patch/prototype;
- architectural rework;
- hallucinated API/file assumptions;
- test success.

This directly tests whether the research architecture serves its actual purpose.

---

# 27. Storage architecture

## 25.1 MVP

### PostgreSQL
Canonical structured metadata:
- runs/tasks;
- sources;
- artifacts;
- evidence;
- claims;
- topics;
- implementation entities;
- decisions;
- gaps;
- lineage;
- validation results;
- model/tool metadata.

### pgvector
Semantic indexes over selected normalized objects.

pgvector provides exact search by default and HNSW/IVFFlat approximate indexes with explicit recall/speed tradeoffs [R21]. Exact search should be retained on sampled queries to monitor approximate-index recall.

### Object storage
S3-compatible or equivalent:
- raw source captures;
- PDFs;
- screenshots;
- DOM/HAR;
- repository bundles/snapshots as appropriate;
- parser outputs;
- test/reproduction logs;
- large generated artifacts.

## 25.2 Why no graph database initially

This is a **PD**, not a research fact.

The initial relationship model can be represented using typed relational edge tables. Add a graph DB only if real workloads demonstrate that multi-hop graph traversal/query complexity justifies another operational system.

## 25.3 Vector-store migration triggers

Evaluate a vector-native/distributed store when:
- vector count or growth exceeds Postgres operating target;
- filtered ANN recall cannot meet requirements;
- p95 latency misses SLO under realistic filters;
- multi-tenant isolation becomes painful;
- independent vector scaling is required;
- operational testing shows lower total complexity elsewhere.

---

# 28. Durable execution, retries and idempotency

## 26.1 LangGraph state boundary

Use checkpoint state for:
- node statuses;
- branch/task IDs;
- control decisions;
- compact context manifests;
- artifact/evidence IDs;
- pending human interrupts;
- retry metadata.

Do not store cloned repos/PDFs/large evidence payloads in graph checkpoints.

LangGraph explicitly distinguishes checkpointed thread state from durable application data stores [R1].

## 26.2 Idempotent nodes

Each node defines:
- deterministic task ID;
- input manifest hash;
- side-effect policy;
- staged output location;
- commit marker;
- retry-safe behavior.

Canonical evidence writes should deduplicate by content/source identity and be transactionally committed where practical.

## 26.3 Failure semantics

Use explicit statuses:
- `PENDING`;
- `RUNNING`;
- `WAITING_HUMAN`;
- `RETRYABLE_FAILURE`;
- `NONRETRYABLE_FAILURE`;
- `BLOCKED_SOURCE`;
- `BLOCKED_PERMISSION`;
- `INCOMPLETE_BUDGET`;
- `COMPLETE_UNVERIFIED`;
- `COMPLETE_VERIFIED`.

## 26.4 When Temporal becomes justified

Temporal provides durable workflow execution/event history designed to survive process failures [R20], but should be added only if operational requirements justify an outer workflow layer.

Candidates:
- scheduled/recurrent research over months;
- strict durable timers;
- workflows spanning independent services/teams;
- very long wait states;
- organization-wide workflow guarantees;
- large distributed activity fleets.

---

# 29. Security and prompt-injection architecture

All retrieved content is untrusted data. This is a hard trust boundary, not merely a prompting guideline. OpenAI's current prompt-injection guidance describes third-party content as a social-engineering surface and recommends layered defenses that constrain impact even when detection fails [R32].

## 27.1 Trust boundaries

Separate:
- privileged controller prompts/policy;
- untrusted web/repo/PDF content;
- model-generated interpretation;
- sandbox execution;
- credentials;
- external tool side effects.

## 27.2 Browser/retrieval policy

Retrieved text must never be allowed to:
- redefine system instructions;
- grant tool permissions;
- request credentials;
- alter network policy;
- silently cause writes or external side effects.

## 27.3 Sandbox policy

Browser and repository execution environments:
- no ambient secrets;
- least-privilege filesystem;
- restricted network by default;
- resource/time limits;
- disposable lifecycle;
- explicit artifact export;
- quarantine unknown downloads.

Deep Agents itself documents optional sandbox execution and filesystem permission surfaces, which supports the feasibility of this pattern [R3].

## 27.4 MCP policy

Each MCP/tool integration declares:
- read/write scope;
- external side effects;
- credentials required;
- allowed agent roles;
- domain/source restrictions;
- audit behavior.

Research workers should default to read-only tools.

## 27.5 UGC poisoning defenses

Because repeated retrieval of the same user-generated source can create disproportionate influence [R8]:
- track source lineage and domain frequency;
- cap duplicate/derivative evidentiary weight;
- require corroboration for high-impact UGC-derived claims;
- deliberately search for independent counterevidence;
- expose source concentration in verification reports.

---

# 30. Human-in-the-loop checkpoints

Human interruption should be sparse and high-value.

Recommended checkpoints:

1. **After initial reconnaissance** if discovered alternatives require preference decisions.  
2. **Before expensive or sensitive deep inspection** if scope/cost/legal boundaries change.  
3. **Before architecture commitment** if high-impact user preferences remain unresolved.  
4. **Before any write/side-effect capability** outside the research workspace.  
5. **Optional final review** before handoff is frozen.

Do not interrupt the user merely because a worker encounters a researchable factual uncertainty.

---

# 31. Cost accounting and routing

Track costs separately:
- model input/output/reasoning tokens;
- cache writes/hits;
- search requests;
- extraction pages;
- crawl pages;
- browser minutes;
- sandbox compute;
- embeddings;
- storage;
- egress;
- human interruptions if modeling operational cost.

Each `ResearchTask` receives:
- expected-value estimate;
- soft budget;
- hard budget;
- allowed escalation tier;
- provider fallback policy.

Before expensive escalation, ask:
1. can deterministic tooling answer it?
2. can a search/extraction provider answer it?
3. can a cheap model answer it?
4. can a technical workhorse answer it?
5. what evidence shows a frontier advisor is likely to improve the decision?

---

# 32. Observability

Emit typed events for:
- task created/started/completed;
- search request/results accepted/rejected;
- source captured;
- artifact parsed;
- evidence committed;
- claim created/verified/contested;
- branch spawned/pruned;
- model invoked;
- tool invoked;
- prompt-injection/security event;
- budget warning/exhaustion;
- human interrupt;
- stale/invalidation event;
- handoff generation/audit.

Dashboards should expose:
- branch tree;
- unresolved blocker gaps;
- source-domain concentration;
- derivative-source clusters;
- verified vs unverified high-impact claims;
- model/provider cost by role;
- retrieval duplicates/novelty;
- retry rates;
- p50/p95 task latency;
- handoff audit failures.

---

# 33. Final handoff contract

The handoff must serve both humans and coding agents.

## 31.1 Human-readable package

`/handoff/00-executive.md`
- what to build;
- target scope;
- key decisions;
- confidence/uncertainty;
- major risks.

`/handoff/01-requirements.md`
- user intent;
- constraints;
- success criteria;
- non-goals;
- unresolved user decisions.

`/handoff/02-architecture.md`
- components/boundaries;
- control/data flows;
- interfaces;
- dependencies;
- alternatives;
- decision references.

`/handoff/03-source-system-understanding/`
- repo maps;
- paper algorithm maps;
- site/DOM observations;
- source-specific details.

`/handoff/04-implementation-plan.md`
- build order;
- milestones;
- interfaces;
- dependency order;
- test gates;
- risky spikes;
- assumptions.

`/handoff/05-decisions.md`
- ADR-style decisions and reversal triggers.

`/handoff/06-risks-and-unknowns.md`
- open gaps;
- blockers;
- accepted assumptions;
- verification tasks.

`/handoff/07-evidence-index.md`
- major claims → evidence/source locators.

## 31.2 Machine-readable manifest

A required `/handoff/manifest.json` or equivalent structured artifact contains:
- schema/version;
- run ID;
- objective;
- source snapshots/versions;
- user constraints;
- requirements IDs;
- topic IDs;
- decision IDs;
- high-impact claim IDs;
- implementation entity IDs;
- unresolved gap IDs;
- dependency graph;
- handoff document map;
- freshness/invalidation metadata;
- retrieval endpoint/config metadata.

The downstream orchestrator should load the manifest first, then retrieve only relevant topic/evidence bundles.

## 31.3 Queryable knowledge package

`/knowledge` logically exposes:
- topics;
- claims;
- evidence;
- entities;
- relationships;
- gaps;
- decisions;
- embeddings/retrieval metadata.

Raw files remain under artifact storage rather than being duplicated into every handoff.

---

# 34. Downstream coding-agent retrieval contract

A coding agent should request context by one of:
- requirement ID;
- topic ID;
- implementation entity;
- planned milestone;
- repo path/symbol;
- decision ID;
- semantic query.

The retrieval service returns a bounded `ImplementationContextBundle`:
- immediate objective;
- relevant user constraints;
- selected architecture decisions;
- exact implementation entities;
- high-value verified claims;
- supporting evidence locators;
- unresolved gaps/risks;
- relevant tests/acceptance criteria.

It should not dump the entire corpus into the coding agent context.

---

# 35. Failure modes and required mitigations

## 33.1 Premature convergence
Mitigation:
- dedicated reconnaissance breadth phase;
- perspective diversity;
- alternative registry;
- critic check for prematurely discarded paths.

## 33.2 Infinite research branching
Mitigation:
- explicit implementation impact;
- expected novelty;
- branch budgets;
- dependency-aware pruning;
- low-novelty stopping rule.

## 33.3 Search monoculture / fake consensus
Mitigation:
- source lineage;
- domain diversity;
- multi-query/provider evaluation;
- derivative clustering;
- primary-source preference.

## 33.4 UGC poisoning
Mitigation:
- UGC tagging;
- repeated-source influence controls;
- independent corroboration;
- adversarial countersearch [R8].

## 33.5 Prompt injection
Mitigation:
- untrusted-data boundary;
- tool permission separation;
- no secrets in sandbox;
- controlled network;
- no content-derived policy changes.

## 33.6 Citation presence mistaken for factual support
Mitigation:
- dedicated claim entailment verification;
- exact evidence locators;
- calibrated citation judges;
- contradiction search [R6][R22].

## 33.7 Misleading evidence / verification inertia
Mitigation:
- contradiction stage;
- explicit reconciliation gate;
- do not stop merely because sufficient-looking evidence has been found [R7].

## 33.8 Context rot
Mitigation:
- canonical database;
- bounded context packets;
- stable IDs;
- regeneration from evidence.

## 33.9 Summary drift
Mitigation:
- summaries reference claim IDs;
- regenerate from canonical claims;
- avoid recursive summary-of-summary chains.

## 33.10 README bias
Mitigation:
- mandatory relevant source/test inspection.

## 33.11 Arbitrary code chunking destroys semantics
Mitigation:
- symbol-aware indexing;
- language-specific relationship analysis when needed.

## 33.12 PDF parser error
Mitigation:
- complementary parsers;
- parser provenance;
- visual verification;
- uncertainty status.

## 33.13 Same agent researches and grades itself
Mitigation:
- independent verification/critic task;
- separate context/prompt;
- optionally different model/provider for high-risk audits.

## 33.14 Multi-agent duplicate work
Mitigation:
- central task/source registry;
- ownership/leases;
- overlap detection;
- shared source hashes;
- fan-in merge rules.

## 33.15 Stale repository understanding
Mitigation:
- commit binding;
- source-version metadata;
- invalidation graph.

## 33.16 Browser inference becomes fake source knowledge
Mitigation:
- direct-observation evidence classes;
- inference labels;
- no claims about private implementation without evidence.

## 33.17 Tool/provider volatility
Mitigation:
- raw response capture;
- provider/version/query metadata;
- provider abstraction;
- reproducibility status.

## 33.18 Dependency/license blindness
Mitigation:
- dependency and license extraction as required branch output;
- architecture gate considers compatibility/security/maintenance.

## 33.19 Expensive model masks shallow retrieval
Mitigation:
- evaluate source coverage and exact locators separately from prose quality;
- model escalation cannot substitute for missing evidence.

## 33.20 Budget exhaustion disguised as success
Mitigation:
- `INCOMPLETE_BUDGET` status;
- unresolved gaps in final handoff.

---

# 36. Required dependencies and optional dependencies

This is an implementation-oriented dependency map, not a frozen package lockfile.

## 34.1 Required MVP classes

### Application/runtime
- Python 3.12+ **or** another supported language runtime; Python is recommended initially because the selected agent/document ecosystem is strong there. This is a PD, not an architectural invariant.
- LangGraph.
- schema/validation layer (e.g. Pydantic or equivalent).
- async HTTP client.

### Persistence
- PostgreSQL;
- pgvector;
- S3-compatible object storage or equivalent.

### Repository tooling
- git;
- ripgrep;
- tree-sitter or equivalent parser baseline;
- container runtime/sandbox.

### Browser
- Playwright;
- Chromium.

### Models/search
- at least one model provider;
- at least one search provider;
- embedding provider/model.

### Observability
- structured logging;
- metrics/traces, preferably OpenTelemetry-compatible.

## 34.2 Recommended additions

- Deep Agents **or** selected DeerFlow-derived worker patterns for agent ergonomics;
- GitHub API/connector;
- Exa and/or Perplexity Search;
- Tavily for site maps/crawls;
- GROBID;
- Docling;
- language-specific LSP/static analysis;
- Redis only if queue/cache needs justify it.

## 34.3 Optional / trigger-based

- Firecrawl for difficult extraction/crawls;
- Browserbase or another managed browser;
- Temporal for outer durable workflows;
- vector-native DB if pgvector SLOs fail;
- graph DB if traversal requirements justify it;
- managed agent runtime such as Claude Managed Agents;
- self-hosted/local models for privacy/cost;
- specialized scholarly search APIs.

---

# 37. Implementation sequence

## Stage 0 — Evaluation fixtures first

Before broad autonomy, create small gold/reference tasks covering:
- one repo extension problem;
- one paper-to-code problem;
- one website/DOM reconstruction problem.

These become regression fixtures for provider/model/harness decisions.

## Stage 1 — Control/data foundation

Implement:
- run/task schema;
- LangGraph control graph;
- Postgres/object store;
- artifact hashing;
- source/evidence IDs;
- observability;
- provider interfaces.

No recursive swarm yet.

## Stage 2 — Progressive interview + reconnaissance

Implement:
- initial breadth interview;
- search provider adapter;
- source registry;
- question-utility heuristic;
- focused follow-up loop.

Validate whether reconnaissance actually improves follow-up questions.

## Stage 3 — Evidence/verification backbone

Implement:
- raw/derived artifacts;
- evidence units;
- claims;
- lineage;
- exact locators;
- deterministic verification;
- first citation-entailment judge benchmark.

Do this before sophisticated multi-agent orchestration.

## Stage 4 — Repository specialist

Implement the deepest repo workflow first.

Success gate:
- can produce correct exact file/symbol/dependency/extension maps on benchmark repos;
- downstream coding agent reduces re-research relative to baseline.

## Stage 5 — Paper specialist

Add GROBID/Docling/visual verification and paper→implementation mapping.

## Stage 6 — Browser/DOM specialist

Add text→DOM→interactive escalation plus observed-vs-inferred evidence typing.

## Stage 7 — Topic/retrieval layer

Add:
- topic hierarchy;
- vector/lexical retrieval;
- structural links;
- downstream context bundles.

## Stage 8 — Gap/contradiction loop

Add:
- critic;
- branch reopen;
- contradiction reconciliation;
- stopping/novelty logic.

## Stage 9 — Model/provider routing

Run A/B experiments across candidate pools and search providers.

Only now establish default role routing.

## Stage 10 — Handoff generator/auditor

Generate human docs + machine manifest and evaluate downstream coding-agent performance.

## Stage 11 — Production hardening

Add:
- sandbox/network policy;
- prompt-injection defenses;
- retries/idempotency;
- stale-source invalidation;
- multi-tenant concerns if needed;
- operational SLOs;
- optional Temporal/vector DB/managed browser only if triggered.

---

# 38. Pre-implementation architecture proof obligations

Before implementation starts, the engineering team should be able to falsify—not merely explain—the major decisions.

## 38.1 Control-plane bake-off

Prototype the same small research workflow in:
1. bare LangGraph;
2. LangGraph + Deep Agents worker;
3. DeerFlow-derived/DeerFlow-native equivalent where feasible.

Measure:
- implementation effort;
- checkpoint/resume behavior;
- parallel isolation;
- observability;
- artifact/evidence integration friction;
- cancellation/retry semantics;
- context growth;
- operational complexity.

The architectural recommendation remains LangGraph unless another option wins materially without forcing canonical evidence into its internal agent state.

## 38.2 Storage proof

Before locking pgvector:
- load a representative projected corpus;
- test exact vs HNSW retrieval;
- measure filtered recall against exact search;
- measure p50/p95 latency;
- test tenant/project filtering;
- test update/delete/staleness workloads.

pgvector itself recommends monitoring approximate recall by comparison with exact search and documents filter/recall tradeoffs [R21].

## 38.3 Model-routing proof

For every expensive role compare at least:
- cheap model;
- workhorse model;
- frontier model;
- workhorse + advisor.

Use the same hidden ground-truth fixtures. Measure correctness, unsupported claims, cost, latency, and escalation frequency. Do not choose a model because it “felt smarter” in exploratory use.

## 38.4 Verification-gate proof

Construct adversarial fixtures:
- correct primary source + misleading secondary source;
- two derivative sources presenting the same false claim;
- stale docs vs current repository behavior;
- parser-corrupted equation;
- prompt injection embedded in a page;
- issue/forum claim contradicted by code/tests.

The gate passes only if unsupported confidence falls and contradictions remain visible. DRNOISE and current source-attribution work justify treating this as a core benchmark rather than an edge-case test [R6][R7].

## 38.5 Progressive-interview proof

A/B:
- exhaustive up-front questionnaire;
- minimal interview only;
- proposed shallow→recon→focused loop.

Measure:
- user turns;
- user abandonment/annoyance;
- number of architecture-changing facts discovered late;
- research wasted on discarded branches;
- final handoff correctness.

This converts ADR-005 from plausible architecture into measured product policy.

## 38.6 Downstream utility proof

This is the decisive proof.

Hold coding-agent model and task constant. Compare:
- raw sources;
- ordinary research report;
- structured evidence corpus without handoff;
- handoff without queryable corpus;
- full proposed handoff + queryable corpus.

If the full architecture does not materially reduce re-research, incorrect assumptions, time-to-correct-build, or architectural rework, simplify it.

---

# 39. Acceptance criteria for end-to-end implementation

The system is not complete until it demonstrates:

- [ ] vague user goal can bootstrap a run;
- [ ] first interview is breadth-oriented rather than exhaustively technical;
- [ ] reconnaissance discovers materially new questions/paths;
- [ ] focused clarification asks only high-impact user-resolvable questions;
- [ ] typed research DAG is persisted/resumable;
- [ ] branches have budgets and explicit acceptance criteria;
- [ ] parallel workers have isolated contexts;
- [ ] repo claims bind to commit/path/symbol;
- [ ] paper claims bind to version/page/equation/figure and critical parser outputs can be visually checked;
- [ ] web research can escalate to rendered DOM/browser/network capture;
- [ ] raw artifacts are immutable/content-addressed;
- [ ] derived artifacts are versioned/invalidateable;
- [ ] evidence is distinct from claims/inference/recommendation;
- [ ] source lineage prevents derivative sources from masquerading as independent corroboration;
- [ ] high-impact claims pass source-specific verification;
- [ ] citation entailment is independently checked;
- [ ] misleading/contradictory evidence triggers reconciliation;
- [ ] UGC concentration is visible and controlled;
- [ ] similarity retrieval and explicit hierarchy coexist;
- [ ] context packets retrieve bounded relevant information;
- [ ] summaries are regenerable from canonical claims;
- [ ] expensive model escalation is logged and benchmark-driven;
- [ ] model/provider defaults come from internal evals;
- [ ] code execution occurs in disposable restricted sandboxes;
- [ ] prompt-injection boundaries are enforced;
- [ ] budget exhaustion cannot produce a false COMPLETE status;
- [ ] final handoff includes exact dependencies/interfaces/files/symbols/algorithms/risks/tests/gaps;
- [ ] machine-readable manifest links handoff sections to canonical research IDs;
- [ ] downstream coding agents can retrieve evidence without loading the full corpus;
- [ ] end-to-end benchmark shows the handoff reduces re-research and implementation errors versus a normal report/raw-source baseline.

---

# 40. When another architect should reach a different conclusion

The purpose of the new assumptions/ADR sections is not to force consensus. A competent architect should reasonably choose a different architecture if evidence shows one of these conditions:

## Choose DeerFlow/Deep Agents as the top-level harness if
- speed-to-prototype matters more than custom research-state semantics;
- its built-in context/filesystem/subagent behavior covers most requirements;
- the team accepts adapting its state model;
- benchmarked downstream utility matches the custom evidence approach.

## Choose Claude Managed Agents / a vendor-managed runtime if
- the organization is intentionally Anthropic-centric;
- managed sandboxes/session infrastructure materially reduce operations;
- provider lock-in is acceptable;
- custom evidence/data storage remains external.

## Choose Temporal + custom workers earlier if
- workflows are routinely multi-day/month;
- durable timers/events/cross-service orchestration dominate complexity;
- agent-graph semantics are secondary.

## Choose a vector-native database earlier if
- vector scale/filtering/tenant isolation already exceeds the pgvector assumptions.

## Choose a simpler report-first architecture if
- downstream coding agents do not need queryable provenance/evidence;
- jobs are shallow/one-shot;
- reproducibility and source-version tracking are not product requirements.

Under the assumptions in Section 3, however, the recommended LangGraph control plane + separate evidence/knowledge layer remains the most coherent starting architecture.

---

# 41. Open uncertainties that must remain explicit

Even after this audit, the following are **not yet evidence-settled** and should not be hidden:

1. **Best workhorse model by role.** Provider benchmarks and pricing change quickly; internal repo/paper/DOM tasks must decide this.
2. **Best search-provider routing policy.** Exa/Perplexity/Tavily/Firecrawl have complementary capabilities, but the optimal mix depends on target sources and cost.
3. **Whether Deep Agents or custom LangGraph workers reduce implementation effort most.** Prototype both on one repository benchmark.
4. **Whether DeerFlow 2 components should be reused directly.** Current capabilities make it a credible alternative/reference; integration cost must be measured.
5. **Optimal clarification cadence.** The EVPI-inspired approach is supported conceptually but must be validated with product users.
6. **Adaptive branch scheduler weights.** These are heuristics requiring task-level calibration.
7. **Citation-judge model and thresholds.** Research suggests cheap models can work, but each chosen judge needs local calibration [R22].
8. **How much execution verification is worth the cost.** Different tasks need different thresholds.
9. **When pgvector stops being sufficient.** Define SLOs and measure rather than predicting by intuition.
10. **Whether graph-database projection adds useful downstream retrieval.** Defer until real query patterns exist.
11. **How much autonomous site crawling is appropriate per deployment.** Legal, robots, authorization and security policy are environment-specific.
12. **How often stale sources should be refreshed.** Driven by source type and downstream risk.

These uncertainties are not flaws in the design; pretending they are already solved would be.

---

# 42. Final recommendation after the audit

The original central architecture survives the audit, but in a more defensible form:

> **Use LangGraph as the initial durable control plane; build the product’s competitive core as a separate typed evidence/claim/topic/decision system; use Deep Agents/DeerFlow-style worker harness patterns inside that control plane; route search and models by evaluated task capability; and make source/claim verification a mandatory lifecycle gate.**

The research loop should be:

> **broad shallow elicitation → reconnaissance → high-value clarification → broad exploration → deep source-specific investigation → canonical evidence commit → claim verification → synthesis → independent gap/contradiction review → targeted re-research → audited implementation handoff**

The most important economic conclusion also survives, but without an arbitrary token quota:

> **A better harness should absorb most mechanical work. Expensive frontier models should be escalation resources whose value is demonstrated on high-impact tasks, not default engines for every research step.**

The most important reliability conclusion is stronger than in v1:

> **More search and more agent calls do not automatically produce more reliable research. The architecture must explicitly verify source entailment, lineage, contradictions, parser fidelity and implementation observations before they become canonical handoff knowledge.** [R6][R7][R8]

---

# 43. Research references

The following references are the evidence base for claims labeled VC or for the external findings used to justify design decisions. Product-specific combinations remain ERI/PD as marked.

**[R1] LangChain — LangGraph Persistence.** Checkpointers, stores, fault tolerance, cross-thread memory.  
https://docs.langchain.com/oss/python/langgraph/persistence

**[R2] LangChain — LangGraph Subgraphs.** Modular subgraphs, persistence/isolation considerations.  
https://docs.langchain.com/oss/python/langgraph/use-subgraphs

**[R3] LangChain — Deep Agents Overview.** Filesystems, subagents, context offload/summarization, code execution, human-in-the-loop, LangGraph runtime.  
https://docs.langchain.com/oss/python/deepagents/overview

**[R4] ByteDance — DeerFlow 2 documentation/repository.** Long-horizon/super-agent harness, subagents, sandbox/context patterns.  
https://github.com/bytedance/deer-flow

**[R5] LangChain — Open Deep Research repository.** Repository archived August 21, 2026; historical/reference only.  
https://github.com/langchain-ai/open_deep_research

**[R6] “Cited but Not Verified: Parsing and Evaluating Source Attribution in LLM Deep Research Agents.”** 2026.  
https://arxiv.org/abs/2605.06635

**[R7] “DRNOISE: Benchmarking Deep Research Agents in Misleading Evidence Environments.”** 2026.  
https://arxiv.org/abs/2607.17291

**[R8] “Deep-Research Agents Can Be Poisoned via User-Generated Content.”** 2026.  
https://arxiv.org/abs/2605.24245

**[R9] Suri et al. — “Structured Uncertainty guided Clarification for LLM Agents.” Findings of ACL 2026.**  
https://aclanthology.org/2026.findings-acl.2028/

**[R10] Exa — Search API documentation.** Search modes/content/highlights.  
https://exa.ai/docs/reference/search

**[R11] Perplexity — Search API.** Raw ranked web search suitable for custom pipelines.  
https://docs.perplexity.ai/

**[R12] Tavily — Website crawling/content extraction documentation.** Map/crawl/extract workflows.  
https://docs.tavily.com/examples/quick-tutorials/crawl-api

**[R13] Firecrawl — API Reference / introduction.** Scrape/crawl/map/search/extract capabilities.  
https://docs.firecrawl.dev/api-reference/v2-introduction

**[R14] OpenAI — Advancing the price-performance frontier with GPT-5.6.** Workload-to-tier positioning and current price-performance direction.  
https://openai.com/index/advancing-the-price-performance-frontier-with-gpt-5-6/

**[R15] Anthropic — Claude Platform Pricing.** Current model/token pricing.  
https://platform.claude.com/docs/en/about-claude/pricing

**[R16] Google — Gemini Developer API Pricing.** Current model and search-grounding pricing.  
https://ai.google.dev/gemini-api/docs/pricing

**[R17] Anthropic — Optimizing for cost and intelligence.** Executor/advisor strategy and workload-dependent tradeoffs.  
https://platform.claude.com/docs/en/about-claude/models/optimizing-for-cost-and-intelligence

**[R18] Docling Documentation.** Structured document model, layout/table/formula/picture enrichment and provenance-oriented representations.  
https://docling-project.github.io/docling/

**[R19] GROBID Documentation.** Scholarly document parsing, metadata/references/citation structure.  
https://github.com/grobidOrg/grobid

**[R20] Temporal Documentation.** Durable workflow/event-history semantics.  
https://docs.temporal.io/

**[R21] pgvector.** Exact and approximate vector search; HNSW/IVFFlat tradeoffs.  
https://github.com/pgvector/pgvector

**[R22] “Do You Need a Frontier Model as a Citation Verifier? Benchmarking Rubric LLMs for Deep-Research Source Attribution.”** 2026.  
https://arxiv.org/abs/2607.08700

**[R23] W3C — PROV-DM: The PROV Data Model.** Entity/activity/agent/derivation/bundle semantics for provenance and trust assessment.  
https://www.w3.org/TR/prov-dm/

**[R24] Model Context Protocol — Authorization specification.** OAuth/resource audience binding, token validation, secure token handling, token-passthrough prohibition.  
https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization

**[R25] IETF RFC 9309 — Robots Exclusion Protocol.** Standardized crawler access-control signaling; explicitly not an authorization mechanism.  
https://www.rfc-editor.org/rfc/rfc9309.html

**[R26] SPDX — License identifiers/specification.** Standard machine-readable license IDs and expressions.  
https://spdx.dev/learn/handling-license-info/

**[R27] OpenAI — BrowseComp: a benchmark for browsing agents.** 1,266 difficult web-retrieval problems requiring strategic search and reasoning.  
https://openai.com/index/browsecomp/

**[R28] Tencent Hunyuan — RepoProbe.** Architecture-aware repository-comprehension benchmark using open-ended GitHub Discussions and checklist-based verification.  
https://github.com/Tencent-Hunyuan/RepoProbe

**[R29] SWE-bench.** Real-world GitHub issue-resolution benchmark for software-engineering agents.  
https://www.swebench.com/original.html

**[R30] Microsoft — SWE-bench-Live.** Continuously updated multi-language/multi-OS SWE benchmark with executable environments.  
https://github.com/microsoft/SWE-bench-Live

**[R31] Model Context Protocol — Specification security principles.** Consent, privacy, access controls, and treatment of tools as arbitrary capability/code-execution surfaces.  
https://modelcontextprotocol.io/specification/2025-11-25

**[R32] OpenAI — Designing AI agents to resist prompt injection / Understanding prompt injections.** Layered defenses, social-engineering framing, sandbox/link/security controls.  
https://openai.com/index/designing-agents-to-resist-prompt-injection/  
https://openai.com/safety/prompt-injections/

## Starter landscape references supplied for this project

- User-provided concise survey of deep-research architectures/harnesses.
- User-provided *Deep Research Agent Architectures and Harnesses: A Landscape Survey*.

Those starter reports motivated the candidate set and architectural patterns; the current decisions above were rechecked against current primary documentation and recent research rather than copied from the surveys.

---

# 44. Independent implementation/review checklist

Before an external engineering team accepts this design, require them to answer each question from the document/evidence rather than from author intent:

1. Why is LangGraph recommended, and what would make you replace it?  
2. Why is graph state not the canonical research corpus?  
3. Why are Deep Agents/DeerFlow worker references rather than the source of truth?  
4. Which assumptions make Postgres+pgvector reasonable, and what are the migration triggers?  
5. Why is Temporal deferred?  
6. How does the shallow-grilling loop decide whether to ask the user or research externally?  
7. How does a repo claim become verified?  
8. How does a paper equation become implementation-ready?  
9. How is a DOM observation prevented from becoming a fabricated source-code claim?  
10. How does the system detect derivative sources and fake corroboration?  
11. How does it prevent citation presence from being confused with factual support?  
12. What happens when budget expires before a branch is adequately supported?  
13. Which model does each role use today, and what benchmark—not preference—selected it?  
14. Which search provider handles a task, and what benchmark selected that routing?  
15. What exactly gets placed in model context vs durable storage?  
16. How are changed repositories/pages/papers propagated into stale claims/handoffs?  
17. What security boundary prevents webpage/repository content from changing agent policy?  
18. How can a coding agent trace a recommendation back to exact evidence?  
19. How is downstream implementation utility measured?  
20. Which unresolved decisions are still heuristics rather than validated design facts?  
21. What is the atomic publication boundary for evidence, and what happens after a partial worker failure?  
22. Can every handoff claim identify the exact model/tool/parser activity that derived it from raw evidence?  
23. How are prior-run claims prevented from becoming circular corroboration in later runs?  
24. How are user corrections recorded without rewriting external evidence history?  
25. What access/robots/license policy governed each collected artifact?  
26. Which MCP credentials/capabilities can each worker use, and are tokens audience-bound?  
27. What benchmark evidence would falsify the LangGraph choice before the implementation becomes entrenched?  
28. Does the full structured handoff actually outperform an ordinary report for downstream coding agents?  

If another competent team can answer these from the spec and references—and can state the reversal conditions without talking to the original architect—the design is sufficiently reconstructable for implementation.

