DRA Implementation Detail Specification v3
Audited Evidence-to-Implementation Architecture for Deep Research
Status: Governing target architecture
Supersedes: Earlier consolidated specification and v2 audit
Baseline audited: sayandahiyagt/DRA current main at 0deb70b5c571ee43dc5fadd0c86843698449d314
Primary consumer: Autonomous and semi-autonomous coding agents / software-engineering harnesses
Secondary consumer: Engineers and humans reviewing implementation decisions

Part I — Product definition and evidence basis
1. Governing objective
DRA exists to transform an ambiguous implementation objective into a research-backed implementation state from which a coding agent can perform real engineering work accurately.
The quality target is not the report.
The quality target is:
correct implementation decisions and code produced with fewer unsupported assumptions, less repeated research, fewer architecture reversals, and clear evidence for every implementation-significant conclusion.

2. The canonical product
The canonical DRA product is a navigable knowledge state containing:
requirements
constraints
target-system structure
external knowledge
source captures
evidence
claims
implementation entities
implementation relationships
contradictions
gaps
decisions
implementation work packages
tests / acceptance obligations
provenance
version / environment pins
uncertainty


3. The report is a projection
A human-readable report remains useful.
It is not canonical truth.
Canonical Knowledge
        ↓
Knowledge Snapshot
        ↓
Human Handoff Renderer

Changing the report must never silently change researched knowledge.

4. The coding-agent context is also a projection
The coding agent should not receive the whole corpus.
It receives a task-specific:
ImplementationContextBundle

compiled from the canonical knowledge snapshot.

5. Research completeness and implementation-context completeness are separate
DRA must satisfy two different invariants:
Research sufficiency
Does the knowledge store contain everything necessary for the implementation objective?
Consumption sufficiency
Did the coding agent receive everything relevant to the particular work it is about to perform?
R1-Reasoning-RAG's useful/missing-information loop strongly motivates this distinction, although that repository itself is only a small prototype.

6. Source hierarchy for architectural decisions
Not every surveyed repository carries equal evidential weight.
The architecture should weight evidence roughly as:
production experience / external benchmark evidence
        >
implemented open-source architecture
        >
prototype with useful mechanism
        >
survey / roadmap direction
        >
our own architectural intuition


7. Strongest control-plane references
For adaptive control:
Open Deep Research provides a concrete adaptive supervisor/worker model rather than an immutable workflow.
Salesforce EDR provides a living research todo state and explicit knowledge-gap feedback.
Anthropic's research lead explicitly distinguishes breadth-first, depth-first and straightforward questions, dynamically allocates workers, and repeatedly updates the research plan.
The DR survey independently identifies dynamic planning, iterative tools and sequential-execution limitations as central research directions.

8. Strongest epistemic references
HyperResearch contributes the strongest practical ideas around:
source independence;
adversarial checking;
verification;
terminal shipping gates.
Its finish_run() is explicitly the only route to terminal done-state after verification.

9. Strongest acquisition-platform reference
GPT Researcher's mature contribution is its heterogeneous acquisition ecosystem and explicit retriever contracts.
A particularly important lesson is its replacement of heuristic content detection with an explicit requires_scraping declaration.
DRA should generalize that principle.

10. Strongest structured-completeness reference
Deep-Research-Skills demonstrates the value of machine-checkable research fields and fail-loud schema validation; its validator now errors rather than silently accepting a schema that parsed zero fields.
DRA should adopt explicit contracts but not its report policy of hiding uncertain values. That repository explicitly tells its report layer to skip uncertain values, whereas its research layer marks them.
For implementation work, uncertainty must remain visible.

11. Strongest context-engineering references
Skywork, Open Deep Research, GPT Researcher and the long-running-agent article converge on:
external state;
compressed working context;
selective retrieval;
model context as a temporary projection rather than canonical memory.
GPT Researcher's compressor explicitly uses embeddings to filter retrieved chunks into relevant context.
DRA should use semantic similarity for candidate selection, but not as its final truth-selection mechanism.

12. Strongest repository-navigation evidence
The original graph-memory intuition is supported by software-engineering research, not merely by deep-research repositories.
RepoGraph reports improvements when repository-level graph structure is added to four SWE approaches.
CodexGraph specifically identifies weak recall from similarity-only repository retrieval on complex coding tasks and exposes structural code graphs to agents.

13. Hybrid retrieval has independent precedent
Microsoft GraphRAG local search first maps a query to semantically related entities, then expands graph information and related text before ranking everything into a bounded context.
This provides useful independent support for DRA's proposed:
semantic anchors
→ typed graph expansion
→ evidence/context packing

architecture.

14. Explicitly unproven assumptions remain hypotheses
Several major DRA beliefs are still unproven:
structured knowledge will materially help real coding agents;
hybrid graph/vector retrieval will beat simpler retrieval;
sufficiency checking will materially reduce coding mistakes;
adaptive planning will outperform a simpler controller on DRA workloads;
research playbooks will outperform a sufficiently capable generic worker.
These must be benchmarked rather than converted into doctrine.

15. Primary system success metric
The final metric is:
downstream implementation utility.
Not:
RACE score;
report length;
citation count;
source count;
number of agents;
graph density.

Part II — Audit of current DRA
16. Current DRA has a good provenance-oriented foundation
The control plane intentionally keeps canonical evidence out of LangGraph checkpoint state and publishes investigator results through the canonical evidence substrate.
This principle remains correct.

17. The Phase-11 control-flow defect has been fixed
Current main now loops re-research back through the real downstream phases rather than going directly to decisions.
The most recent commit explicitly changed re-research state handling.
Therefore "close the P11 arrow" is no longer Priority Zero.

18. The deeper P11 problem remains
Re-research can still fall back to deterministic synthetic capture bytes when it cannot identify a meaningful source.
A production epistemic gap must not be considered researched merely because some bytes were canonically published.

19. Current branch completion is publication completion, not research completion
A branch can become B_COMPLETE when a bundle published rows successfully.
That proves:
storage worked

not:
the ResearchTask was answered.

Task acceptance must become a separate semantic gate.

20. Current Phase 7 is still control-plane scaffolding
Phase 7 generates claims such as:
Task X produced content-addressed evidence.
That is not a proposition about the researched system.
Production Phase 7 semantics therefore need replacement.

21. Current Phase 9 is also scaffolding
The present synthesis layer largely transforms control state into placeholder topic/decision structures rather than deriving a real implementation knowledge model.
It should eventually disappear as a special synthetic phase.

22. Current Phase 10 creates pseudo-gaps
Generic critic questions are materialized as ResearchGap objects regardless of whether a concrete missing research obligation has actually been detected.
A question is not automatically a gap.

23. Current Phase 12 creates placeholder decisions
Current decision synthesis creates generic alternatives such as:
keep current
adopt alternative

rather than evidence-grounded architectural choices.
This must be replaced by a real decision synthesis contract.

24. Current final audit is weaker than intended
Current completion checks:
claims have evidence IDs;
branch states;
some evidence exists;
no blocking gaps;
budget remains.
But it does not require the actual verification report to have passed.
A run can therefore satisfy the control-plane audit while epistemic verification is inconclusive or failed.

25. Handoff publication is not currently a hard requirement for completion
Phase 13 can fail canonical handoff staging and degrade to a control-state-only manifest, while the later completion predicate does not require successful durable handoff publication.
That is acceptable for smoke tests.
It is unacceptable for a production verified snapshot.

26. Verification is currently globally scoped
The verification gate selects all canonical/verified/stale claims in the database, not just claims belonging to the current run or candidate snapshot.
This is a major correctness defect for multi-run operation.

27. Verification read/write semantics are confusing
run_verification_proof(write=False) controls report-file output, while verification-state DB mutations are independently governed by GateConfig.write_mutations.
This creates too much opportunity for unexpected mutation.
Replace with explicit:
mode=READ_ONLY
mode=APPLY_RESULTS

and explicit claim/snapshot scope.

28. The current deterministic verifier has useful mechanics
Keep:
provenance traversal;
freshness quarantine;
UGC visibility;
contradiction visibility;
anti-citation laundering.
These are meaningful pieces.

29. Token-overlap entailment is insufficient as a universal truth verifier
The current verifier largely determines support from lexical token overlap plus negation handling.
That is useful as a deterministic filter but cannot reliably determine nuanced implementation propositions.
It should become Verification Layer 1/2, not the entire semantic verifier.

30. The current repository investigator is useful but task-agnostic
It already produces:
repository identity
commit SHA
raw snapshot
symbol index
implementation entities
code evidence
optional execution evidence

which is valuable.
But it is not given the actual research question and does not conduct an adaptive investigation around that question.

31. Repository execution claims are currently too broad
The repository investigator emits:
Repository test suite passes under the sandbox execution policy
and degrades the evidence status to INFERENCE when execution was unavailable.
A test suite that was never executed must not produce a proposition saying it passes.
No execution should produce:
UNKNOWN / NOT_EXECUTED

not an inferred pass claim.

32. Execution output hashes are not yet robust enough
The generic repository test claim passes a commit SHA as output_hash.
An execution-evidence record should instead hash the actual:
command
stdout
stderr
exit code
environment manifest

or a canonical execution-result document.

33. The paper investigator is strong at extraction but weak at task synthesis
It already:
dual-parses;
identifies critical equations/tables/figures;
creates gaps when parsers disagree;
retains paper locators.
This is useful acquisition infrastructure.

34. Paper claims are currently template-like
The investigator can emit generic claims such as:
the paper presents an algorithm with polynomial computational complexity
independent of the actual research task.
This demonstrates why acquisition/parsing must be separated from task-conditioned claim synthesis.

35. The website investigator has useful escalation machinery
Its ladder supports:
search
extracted text
HTML
rendered DOM
accessibility
interaction
screenshot
network

and enforces crawl/access policy.
Preserve the escalation concept.

36. Website source identity is currently too coarse
The investigator creates a source_identity using the origin rather than the exact page URL.
That can collapse multiple pages on the same site into one logical source identity.
Exact source resource identity and publisher/origin identity must be separate.

37. Search-snippet attribution is unsafe
The search rung obtains the first snippet returned for the query and then records it under the currently investigated target URL path.
A search result must preserve its own returned URL.
Discovery results must not be silently attached to another source identity.

38. Search snippets should not automatically become researched claims
A search-engine snippet is discovery metadata.
It is generally not sufficient to establish a substantive implementation claim.
Use:
SourceCandidate

not:
EvidenceUnit → Claim

unless the provider contract explicitly guarantees authoritative full content.

39. Many website claims are acquisition observations, not research conclusions
Examples such as:
Raw markup captured
Screenshot evidence captured
Network/HAR observed

are useful evidence-production events, but not substantive answers to a ResearchTask.
They belong in acquisition provenance.

40. The live browser provider is not production-ready
The present Playwright adapter has several implementation problems, including asynchronous API misuse and network/HAR handling that does not correspond to actual HAR semantics.
Provider implementations must pass capability-conformance tests before being considered available.

41. Current source identity insertion is not concurrency-safe enough
stage_source_identity() generates a new UUID and performs a plain insert.
Canonical source identities should have normalized keys and safe get-or-create/upsert behavior.

42. Current raw capture identity conflates content identity with capture provenance
raw_capture.content_hash is the primary key and also contains a single source_id.
Identical bytes independently captured from two sources therefore collapse to one content row and cannot faithfully preserve both source occurrences.
This is a genuine provenance flaw.

43. Blob identity and source-capture identity must separate
The corrected model is:
ContentBlob
    hash

SourceCapture
    source identity
    retrieval representation
    captured_at
    content_blob_hash

Many independent captures may reference one identical blob.

44. Current raw captures are not necessarily durably stored
The schema records stored_at, but repository captures may point to a temporary/local repository path rather than to a durable content-addressed blob location.
DRA needs a real BlobStore abstraction.

45. Current transaction boundaries are too broad for future long-running workers
InvestigatorContext holds its staging session across the investigation and publishes on context exit.
A worker researching ten websites over several minutes should not conceptually need one long publication transaction.

46. Publication atomicity should shrink, not disappear
Retain staged→canonical publication, but apply it to:
one source/capture batch;
one parsed-artifact batch;
one claim synthesis batch;
one decision batch;
final snapshot.
A ResearchTask may span many publication bundles.

47. Current claim schema is too one-to-one
claim contains a single evidence_unit_id.
Serious research claims often need:
multiple supporting sources;
contradictory evidence;
execution evidence;
qualifying evidence.
Introduce many-to-many claim/evidence relations.

48. Current decision schema is similarly too narrow
A meaningful architecture decision usually rests on multiple claims.
A decision must not be structurally attached to only one claim.

49. Current DRA has no canonical requirement table
The handoff code explicitly acknowledges this.
For an implementation system, this is a core missing entity.

50. Research dependencies and implementation dependencies are conflated
Current handoff logic derives implementation-plan ordering from research-task dependencies.
These are fundamentally different graphs.
This is a major architectural error.

51. Current handoff contains target-independent DRA-specific prose
The architecture section can describe DRA's own control plane and evidence publisher rather than necessarily describing the researched target system.
The handoff renderer must operate only on target-system knowledge.

52. Current downstream retrieval has a good boundedness principle
It explicitly avoids dumping the whole corpus and retrieves linked claims/evidence/entities.
Retain bounded retrieval.

53. Current "semantic" retrieval is not semantic
The implementation uses ILIKE, not vector similarity.
This should be renamed until genuine semantic retrieval exists.

54. pgvector feasibility is established only operationally
DRA's storage proof showed good HNSW latency and recall on a 25k synthetic corpus, but explicitly warns that its synthetic vectors have clean cluster structure and real overlapping workloads must be tested.
Therefore:
pgvector viability = supported
retrieval quality = not yet demonstrated


55. Alembic history needs cleanup before major schema expansion
Current migration files document a pre-existing double-head situation.
Do not add a large graph-schema wave before reconciling the migration lineage.

Part III — Non-negotiable architectural invariants
56. Canonical knowledge is external to model context
Models receive projections.
They never own research truth.

57. Canonical knowledge is external to LangGraph checkpoint state
Checkpoint state coordinates execution.
It is not the evidence database.

58. Every implementation-significant claim must resolve to evidence
Required path:
Claim
→ ClaimEvidence
→ EvidenceUnit
→ DerivedArtifact
→ SourceCapture
→ SourceIdentity

or explicitly to execution/user-assertion evidence where applicable.

59. Unknown is a valid product result
An unresolved but important question must be surfaced as:
UNKNOWN

not guessed away.

60. Operational failure is not epistemic absence
Could not access source

must never become:
The source does not support the claim.


61. Discovery is not evidence
Search snippets, URLs and titles identify sources worth investigating.
They do not automatically support claims.

62. Evidence is not a claim
A source passage is evidence.
A proposition derived from it is a claim.
Keep both objects.

63. Structural facts are not necessarily claims
A source-defined relationship such as:
file A defines symbol B

can be represented directly as an evidence-backed implementation relation.
Do not generate prose claims unnecessarily.

64. User authority is scoped
Users are authoritative about:
requirements;
preferences;
constraints;
non-goals.
User-provided technical material remains evidence subject to verification.

65. Source authority is claim-dependent
Official documentation may dominate API-contract evidence.
Independent reproduction may dominate performance claims.
A single universal "credibility score" is insufficient.

66. Source independence must be modeled
Five copies of one upstream article are not five corroborations.

67. Version/environment compatibility is part of truth
A claim about:
library 2.1

is not automatically support for:
library 3.0


68. Research planning and research execution are separate
Supervisor:
What knowledge obligation should be resolved?
Research worker:
How should I resolve it?

69. Acquisition adapters and research workers are separate
Current repo/paper/web investigators become lower-level typed capabilities.
The ResearchWorker performs task-conditioned reasoning over those capabilities.

70. Research-task completion requires acceptance, not publication
published_count > 0 can never be sufficient for SATISFIED.

71. Verification occurs inside the research loop
research
→ claims
→ verify
→ gaps
→ research

not only at the end.

72. Final completion is authoritative and deterministic
Only finalize_snapshot() may produce a verified consumable snapshot.

73. The final rendered artifact must itself pass integrity checks
Research correctness before rendering is insufficient if the renderer introduces unsupported wording.

74. Research state and implementation plan are separate
Research answers:
What do we know?
Implementation planning answers:
What should the coding agent change, and in what order?

75. Research task graph and implementation work graph are separate
Never reuse the task DAG as build order.

76. Retrieval indexes are rebuildable
Deleting every embedding must not delete knowledge.

77. Semantic similarity is not a knowledge relation by itself
Do not permanently write:
A RELATED_TO B

solely because cosine similarity exceeds a threshold.

78. Graph relations should be typed
Prefer:
CALLS
IMPLEMENTS
SUPPORTS
SATISFIES
TESTED_BY

to generic:
RELATED_TO


79. Coding agents receive bounded contexts
Even a perfect corpus becomes harmful if dumped wholesale into the implementation agent.

80. Context sufficiency is explicitly checked
A semantically relevant bundle may still omit one essential invariant.

81. Coding agents cannot directly rewrite research truth
They may report observations.
DRA verifies and ingests them through normal evidence pathways.

82. Research behavior and research knowledge are independently versioned
Changing a prompt must not mutate historical evidence.

83. No arbitrary online self-modifying executable code
Skywork's broader self-evolution ambition is useful conceptually, but executable changes require ordinary validation and sandboxing.

84. Synthetic proofs are mechanism tests, not product-performance claims
Current DRA already correctly documents that its downstream utility harness uses a deterministic fake coding agent.
Keep that honesty.

Part IV — Core five-subsystem architecture
85. Subsystem A — Research Contract
Responsible for:
what the user needs
what must be learned
what success means


86. Subsystem B — Research Control
Responsible for:
what to investigate next
dependencies
priority
steering
stopping


87. Subsystem C — Research Execution
Responsible for:
how a ResearchTask is investigated
tools
providers
acquisition
inspection
experiments


88. Subsystem D — Knowledge
Responsible for:
what has actually been established
with what evidence
at what version


89. Subsystem E — Consumption
Responsible for:
what a coding agent needs
how it navigates the knowledge
whether its selected context is sufficient


90. Experience/evolution remains an extension
Do not make a sixth mandatory production subsystem yet.
Add procedural learning only after real runs exist.

Part V — Research Contract
91. Replace overlapping brief/contract objects with one authority
Canonical object:
ResearchContract

Views such as ResearchBriefView may be generated for models.

92. ResearchContract schema
contract_id
project_id
version

objective

target_consumer
target_system
target_repository

constraints[]
non_goals[]

requirement_ids[]

success_criteria[]

pinned_context

source_preferences[]
source_restrictions[]

research_mode


93. PinnedContext
May contain:
repo_sha
branch
package_versions
api_versions
runtime
platform
hardware
configuration
date_scope
deployment_target


94. Requirement becomes canonical
Requirement

id
project_id

kind
statement

origin

importance
blocking

source_assertion_ids[]

pinned_context

verification_policy

state


95. Requirement origins
USER_REQUIRED
DISCOVERED_CONSTRAINT
ACCEPTANCE
COMPATIBILITY
EXTERNAL_NORM
SECURITY
NON_GOAL


96. Requirement states
UNASSESSED
RESEARCHING
VERIFIED
QUALIFIED
CONFLICTED
UNKNOWN
ACCEPTED_UNKNOWN
NOT_APPLICABLE


97. Requirements can emerge during research
Research may reveal an implementation invariant that the user did not know to specify.
Example:
Existing persistence protocol requires ordered replay.

DRA can propose:
Preserve ordered replay semantics.

with provenance to the discovery.

98. Discovered requirements require provenance
Store:
discovered_by_task
discovered_by_claim
discovery_reason


99. Progressive interview remains the default interaction policy
Initial questioning should remain broad but shallow.
DRA's existing interview experiment is synthetic and should not be treated as proof of optimal UX, but it remains a reasonable hypothesis.

100. Reconnaissance may trigger focused clarification
Ask a second question only if the answer changes:
architecture;
source strategy;
required version;
decision tradeoff;
implementation outcome.

101. Later clarification becomes a DecisionGate
Human questions are allowed at any supervisor boundary when unresolved preference—not missing factual research—blocks implementation.

102. DecisionGate conditions
Ask the user only if:
multiple defensible choices remain;
choice materially changes implementation;
external research cannot determine the correct preference;
existing assertions do not resolve it.

103. Optional ResearchSchema
For comparative/entity research only:
items × fields

may provide a completeness contract.
Do not force matrix schemas onto open-ended repository research.

104. ResearchSchema and Requirement share one canonical type system
Producer schema, serialization and validation should derive from common definitions.
This avoids the class of validator/schema drift that Deep-Research-Skills had to correct.

Part VI — Research control
105. Replace static Phase-3 planning with a durable task ledger
Persist:
research_task
task_dependency
plan_event
steering_event


106. Do not create a second authoritative ResearchPlan object
Generate:
ResearchPlanView

from the task ledger.
History comes from plan_event.

107. ResearchTask is the universal unit of investigation
Initial research, re-research, contradiction resolution, falsification and refresh all use the same task type.

108. ResearchTask schema
task_id
contract_version

question
purpose
reason

affected_requirement_ids[]
affected_claim_ids[]
affected_gap_ids[]
affected_decision_ids[]
affected_entity_ids[]

dependencies[]

mode
shape

playbook

required_source_scopes[]
pinned_context

acceptance_criteria

verification_policy

budget

status


109. Task reasons
INITIAL
DISCOVERY
GAP
CONTRADICTION
FALSIFICATION
REFRESH
STEERING
EXECUTION_VALIDATION
CONSUMER_GAP


110. Research modes
Initial useful modes:
OPEN_ENDED
REPOSITORY_ANALYSIS
DEBUGGING
LITERATURE_REVIEW
COMPARATIVE
PERFORMANCE
SECURITY
MIGRATION
DUE_DILIGENCE


111. Research shape is independent of domain
STRAIGHTFORWARD
BREADTH_FIRST
DEPTH_FIRST
MIXED
DEPENDENCY_CHAIN

Anthropic's lead prompt offers strong practical precedent for this distinction.

112. Fixed reconnaissance perspectives become heuristics
Current six perspectives can remain useful suggestions.
They should not automatically produce six tasks for every objective.

113. Supervisor operates in rounds initially
Do not begin with fully asynchronous continuous DAG mutation.
Initial execution:
snapshot state
→ choose ready tasks
→ parallel batch
→ barrier
→ publish/verify
→ reassess


114. Logical task graph remains dynamic
The graph may add/cancel/reopen tasks each round.
Execution synchronization does not make planning static.

115. Why not full live asynchrony yet
The DR survey identifies sequential execution limitations but presents asynchronous DAG scheduling as a future direction, not proof of a universally optimal architecture.
Batch barriers simplify:
consistency;
steering;
recovery;
reproducibility;
verification.

116. Parallelize only independent work
Parallel candidates:
different technologies;
different papers;
independent repository subsystems;
independent source families.

117. Serialize dependent work
Examples:
identify API
→ inspect implementation

reproduce failure
→ inspect implicated code

discover version
→ research version-specific behavior


118. Do not hard-code worker counts
Anthropic's own prompt contains fuzzy and partly contradictory subagent-count guidance.
Budgets belong in structured task policy, not prose instructions.

119. Initial priority classes
Use interpretable classes rather than an invented weighted formula.
P0 blocking requirement unknown
P1 contradiction affecting major decision
P2 critical claim inadequately supported
P3 required target/primary evidence missing
P4 important nonblocking uncertainty
P5 breadth / alternatives


120. Within-class ranking
Consider:
expected information gain
dependency centrality
source availability
expected cost
expected latency

These remain heuristics until calibrated.

121. Steering events are queued
Do not mutate an active task underneath a worker.

122. Steering is applied at supervisor boundaries
user message
→ SteeringEvent
→ next round
→ task-plan change

This follows the useful Salesforce pattern.

123. Steering remains auditable
Record:
created tasks
cancelled tasks
reprioritized tasks
changed constraints
affected decisions


124. Task lifecycle
PENDING
READY
RUNNING
WAITING
PUBLISHED
VERIFYING
SATISFIED

BLOCKED_OPERATIONAL
BLOCKED_USER
FAILED_RETRYABLE

SUPERSEDED
CANCELLED


125. SATISFIED has semantic meaning
Required:
task acceptance criteria passed
+
required evidence exists
+
affected claims verified appropriately


126. Stop reasons are explicit
SATISFIED
DIMINISHING_RETURNS
SOURCE_EXHAUSTED
BLOCKED
BUDGET_EXHAUSTED
TIME_EXHAUSTED
SUPERSEDED
CANCELLED
FAILED


127. Diminishing returns must be measured
Track:
new claims
new source families
new evidence
gaps resolved
decisions changed
duplicate rate
cost since last novel information


Part VII — ResearchWorker: the missing middle layer
128. Introduce ResearchWorker
This is one of the largest architectural changes.
The worker sits above source-specific investigators.

129. ResearchWorker input
ResearchTask

relevant ResearchContract subset

relevant prior claims
relevant gaps
relevant packets

playbook

available capabilities

budget


130. ResearchWorker responsibility
It must actually answer:
the ResearchTask question.
Not merely ingest one source type.

131. Existing investigators become capabilities
Conceptually:
RepositoryCapability
PaperCapability
WebCapability
SearchCapability
BrowserCapability
ExecutionCapability

Preserve their useful implementation.
Change their role.

132. Worker local loop
OBSERVE
What is known?

ORIENT
What evidence is missing?

DECIDE
What is the highest-value next source/action?

ACT
Acquire / inspect / execute.

ASSESS
Did this resolve the obligation?

repeat


133. Global/local autonomy boundary
Supervisor chooses:
WHAT question

Worker chooses:
HOW to answer it

This boundary is supported by both ODR and Anthropic-style research delegation.

134. Worker must support source discovery
A task about a paper should not require the control plane to already possess raw PDF bytes.
The worker may:
search literature
→ identify work
→ acquire legal representation
→ invoke PaperCapability


135. Worker must support repository follow-up
Example:
Task:
How does retry ownership work?

Worker:
search symbols
→ inspect caller/callee
→ inspect tests
→ inspect config
→ execute focused test
→ synthesize claim


136. Worker must support heterogeneous evidence
A single task may need:
repository source
+
official docs
+
execution result

It must not be restricted to one investigator class.

137. Worker emits a ResearchPacket
Not raw conversation history.

138. ResearchPacket schema
packet_id
task_id

answer_summary

claim_ids[]
evidence_ids[]
entity_ids[]
relation_ids[]
requirement_ids[]

new_gap_ids[]
resolved_gap_ids[]

contradiction_ids[]

unresolved_questions[]

suggested_followups[]

stop_reason
metrics


139. ResearchPacket is derived
Deleting a packet must not delete canonical knowledge.
It can be rebuilt.

Part VIII — Acquisition architecture
140. Introduce SourceCandidate
A discovery result contains:
candidate_id

query
purpose

provider

title
returned_url
snippet

rank
provider_score

discovered_at


141. Candidate is not canonical source evidence
It may become a source after selection and capture.

142. Preserve query purpose
Every search/action should record why it occurred.
This later enables procedural learning and debugging.

143. Progressive acquisition levels
DISCOVER
METADATA
CAPTURE
FOCUSED_EXTRACT
DEEP_INSPECT
EXECUTE


144. Avoid maximum-depth acquisition by default
A snippet may be enough to discard a candidate.
A primary doc may justify full capture.
A contradiction may justify execution.

145. ResearchCapability contract
capability_id
version

kind

input_schema
output_schema

output_fidelity

identity_semantics

requires_followup_capture

freshness_semantics

supported_source_scopes

side_effect_class

failure_semantics

cost_profile
latency_profile
health


146. Output-fidelity vocabulary
REFERENCE
SNIPPET
PARTIAL_CONTENT
FULL_CONTENT
STRUCTURED_CAPTURE
EXECUTION_RESULT


147. Do not infer fidelity from payload length
GPT Researcher's requires_scraping change exists precisely because heuristic length inference failed.
DRA should enforce fidelity contractually.

148. Structured ToolResult
status

data

capability_id
provider

failure_kind
retryable
retry_after

source_candidates[]
source_capture_ids[]

cost
latency


149. Never represent operational failure as []
Distinguish:
SUCCESS_NO_RESULTS

from:
RATE_LIMITED
PROVIDER_FAILED
AUTH_FAILED


Part IX — Source identity and durable capture
150. Split logical resource from capture occurrence
Introduce:
SourceIdentity
SourceRepresentation
SourceCapture
ContentBlob


151. SourceIdentity means the intellectual/technical source
Examples:
Git repository
specific documentation page
scholarly work
API reference resource


152. SourceRepresentation means a retrievable representation
Examples:
publisher PDF
arXiv PDF
HTML page
GitHub mirror
PMC copy


153. SourceCapture means an acquisition event
capture_id
source_identity_id
representation_id

content_blob_hash

captured_at

final_url
redirect_chain

method
provider

http metadata
access metadata


154. ContentBlob is pure content identity
sha256
size
mime
storage_uri
encryption metadata


155. Identical content does not erase independent provenance
Two independent source captures may point to the same blob.
This fixes the current raw_capture.content_hash provenance collapse.

156. Canonical page identity must use exact canonical resource URL
Do not use site origin as the source identity.
Origin/publisher becomes separate metadata.

157. Scholarly WorkIdentity
When DOI or equivalent stable work identity exists:
WorkIdentity

should connect multiple representations of the same work.

158. Repository identity
Use:
canonical repo identity
+
exact commit SHA

as the meaningful pinned representation.

159. Safe source get-or-create
Source identities require canonical normalized keys and concurrency-safe insert/upsert semantics.

160. BlobStore becomes a real component
Interface:
put(bytes)
open(hash)
exists(hash)
verify(hash)
delete_if_unreferenced(hash)


161. Development BlobStore
Filesystem content-addressed backend is sufficient.

162. Production BlobStore
S3-compatible/object-store implementation is appropriate.

163. Canonical capture requires resolvable content
A verified claim should not depend on a raw capture whose body no longer exists unless policy explicitly declares a nonpersistent source and finalization revalidates it.

164. Derived artifacts may also use blob storage
Large:
parser output;
AST index;
screenshots;
HAR;
execution logs;
should not be forced into Postgres JSON.

Part X — Publication and atomicity
165. Retain staged→canonical publication
The concept is good.
Change the scope.

166. Acquisition publication bundle
Publishes:
source representation
capture
blob reference
parsed artifact
basic evidence units

using a short DB transaction.

167. Claim publication bundle
May reference evidence across many previously canonical acquisition bundles.

168. Decision publication bundle
May reference claims across many tasks.

169. Snapshot finalization bundle
Atomically publishes the consumable snapshot and its derived artifacts.

170. ResearchTask may span many publication bundles
Task atomicity is logical.
Database atomicity remains short-lived.

Part XI — Canonical knowledge schema
171. Preserve W3C-PROV-inspired lineage
The existing provenance graph is worth retaining.

172. Add canonical requirement
This is a mandatory schema change.

173. Add claim_evidence
claim_id
evidence_id

relation:
  SUPPORTS
  CONTRADICTS
  QUALIFIES

support_type:
  DIRECT
  CORROBORATING
  EXECUTION
  DERIVED

created_by_activity


174. Gradually deprecate claim.evidence_unit_id
Keep compatibility while migrating.
Eventually the many-to-many table becomes authoritative.

175. Add decision_basis
decision_id
claim_id

role:
  SUPPORTS
  OPPOSES
  CONSTRAINT


176. Add requirement_link
Possible targets:
claim
decision
gap
work package
test


177. Add claim_relation
SUPPORTS
CONTRADICTS
QUALIFIES
SUPERSEDES

between claims.

178. Add gap_link
Connect gaps to:
requirements;
claims;
decisions;
entities;
work packages.

179. Expand implementation entity types
Current enum is too narrow.
Target types:
repository
component
package
file
module
class
function
method
interface
api
schema
config
test
algorithm
service
database
queue
endpoint

Do not add every type immediately if a general kind/subkind representation is easier to migrate.

180. Add implementation_relation
from_entity
to_entity
relation_type

evidence_id
activity_id

confidence/status


181. Structural relation vocabulary
Initial:
CONTAINS
DEFINES
IMPORTS
CALLS
IMPLEMENTS
INHERITS
DEPENDS_ON
READS
WRITES
CONFIGURES
TESTED_BY
EXPOSES
USES
MIGRATES_FROM


182. Prefer deterministic relation extraction
AST/static analysis edges should be preferred over LLM inference where possible.

183. Inferred structural edges are allowed
But they must be labeled:
INFERRED

and have backing evidence.

184. Topics remain organizational aids
Do not treat topic hierarchy as the implementation graph.

185. Do not build one giant weak semantic-edge table
Strongly typed relation tables remain authoritative.
A union view can provide generic graph traversal.

186. KnowledgeEdgeView
Derived interface:
from_kind
from_id

relation

to_kind
to_id

evidence_id
state


Part XII — Claim synthesis
187. Introduce ClaimSynthesisService
This is a core missing layer.

188. Inputs
ResearchTask
ResearchContract subset
selected EvidenceUnits
existing Claims
pinned context


189. Output is structured ClaimCandidate
subject
predicate
value
units

canonical_statement

scope
version
environment

evidence_links[]

inference_type


190. Claims must answer the task
A worker researching retry semantics should produce propositions about retry semantics.
Not generic source observations.

191. Claim synthesis can abstain
If evidence does not justify a proposition:
NO_CLAIM
+
ResearchGap

is correct.

192. Acquisition observations remain evidence metadata
Examples:
page captured
DOM observed
test command executed

do not need to become substantive research claims.

193. Direct structural facts can remain structured entities/relations
Avoid prose inflation.

194. Behavioral claims distinguish observation quality
Suggested evidence status:
DIRECT_CODE_OBSERVATION
EXECUTION_VERIFIED
DOCUMENTED_BEHAVIOR
INFERRED_BEHAVIOR
USER_ASSERTION


195. A test that was not run produces no pass claim
Represent:
execution_status=NOT_RUN


196. Failed execution is evidence
A failing test can itself support important claims.
Do not treat failure merely as an operational problem if execution completed correctly.

Part XIII — Verification
197. Verification is scoped
Every verification call receives:
claim_ids
or snapshot_id

Never implicitly "all claims in the database."

198. Separate verification report generation from mutation
Explicit APIs:
evaluate_claims(..., apply=False)
apply_verification(result)


199. Verification Layer 1 — provenance integrity
Check:
evidence exists;
blob exists;
locator resolves;
lineage exists;
state canonical;
access scope allowed.

200. Verification Layer 2 — deterministic content checks
Check:
quoted span;
numerical equality;
symbol existence;
commit match;
version;
execution hash.

201. Verification Layer 3 — semantic support
Where deterministic logic is insufficient:
SUPPORTS
PARTIALLY_SUPPORTS
CONTRADICTS
NEUTRAL

using structured verifier output.

202. Verifier sees evidence, not researcher hidden reasoning
Keep critic independence.

203. Verification Layer 4 — source independence
Use provenance and derived duplicate clustering.

204. Explicit lineage remains authoritative
If evidence B derives from source A, it cannot count independently.

205. Add near-duplicate independence clustering
HyperResearch's implemented independence clustering supports this practical need.
Signals:
same canonical URL
same blob hash
mirror relation
syndication marker
near-duplicate fingerprint
wire attribution


206. Independence cluster is derived metadata
Do not make a scalar credibility weight the source of truth.

207. Verification Layer 5 — scope match
Check:
version
commit
environment
date
hardware
configuration


208. Verification Layer 6 — execution verification
When factual behavior is critical and testable:
execute
→ capture
→ compare


209. Verification state
VERIFIED
QUALIFIED
CONTESTED
UNSUPPORTED
STALE
OBSOLETE


210. Do not store one mystical confidence number as truth
Confidence may exist as UI/model metadata.
Verification state and explicit evidence are authoritative.

Part XIV — Contradictions, gaps and falsification
211. Contradictions are first-class relationships
Use claim_relation(CONTRADICTS).

212. Contradiction grouping is a view
A ContradictionView may group related contradictory claims.
No need for a huge separate subsystem initially.

213. Distinguish true contradictions from scope differences
Possible explanation:
TRUE_CONFLICT
DIFFERENT_VERSION
DIFFERENT_ENVIRONMENT
DIFFERENT_DATE
DIFFERENT_MEASUREMENT
DIFFERENT_SCOPE
SUPERSESSION
UNKNOWN


214. Gap creation must identify a concrete obligation
A gap requires:
what is unknown
why it matters
what it blocks
how it may be resolved


215. Deterministic gaps
Examples:
blocking requirement has no verified claim;
required primary evidence missing;
claim uses wrong version;
expected test behavior unknown;
unresolved contradiction affects decision;
source unavailable.

216. Semantic critic can propose gaps
LLM critic output enters as:
GapCandidate

until grounded to a concrete obligation.

217. Gap resolvability
RESEARCH
EXECUTION
USER
EXTERNAL
UNRESOLVABLE


218. Gap closure is explicit
A gap closes only with:
closure evidence
or user decision
or accepted-unknown record


219. Falsification tasks are selective
Create them for:
major architecture decisions;
security-sensitive changes;
irreversible migrations;
critical performance assumptions.

220. Falsification question
What evidence would make the current favored decision wrong?

221. Absence of counter-evidence does not automatically prove correctness
Record search coverage and limitations.

Part XV — Decisions
222. DecisionRecord
question
alternatives[]

supporting claims[]
opposing claims[]
constraints[]

user preference dependencies[]

chosen

rationale
consequences

remaining uncertainty

reversal triggers


223. Decisions are versioned
New evidence can supersede a decision.
Do not mutate historical decisions silently.

224. Decisions distinguish fact from product choice
DRA should preserve the current useful conceptual distinction between:
fact
reasoning
engineering inference
product decision
heuristic

The current assumptions register already encodes this discipline.

Part XVI — Separate ImplementationPlan
225. Add ImplementationPlan
This is a major addition missing from both previous specifications.
The coding agent needs more than a knowledge graph.
It needs an executable plan derived from that graph.

226. ResearchPlan and ImplementationPlan are different
ResearchPlan
How DRA learns.

ImplementationPlan
How the target system should be changed.


227. ImplementationWorkPackage
work_id

objective

dependencies[]

requirement_ids[]

decision_ids[]

target_entity_ids[]
target_relation_ids[]

invariants[]

expected_change

acceptance_tests[]

evidence_handles[]

known_risks[]
blocking_gaps[]

readiness


228. Work packages should be coding-agent sized
Examples:
add persistence adapter
migrate old token format
wire dependency injection
add rollback tests

Not:
implement entire system


229. WorkPackage dependencies form implementation build order
This replaces the current misuse of ResearchTask dependencies.

230. WorkPackage readiness is independently computed
A research run may be generally complete while one particular work package remains blocked.

231. Work packages preserve provenance
For every expected change, the coding agent can navigate:
WorkPackage
→ Requirement
→ Decision
→ Claim
→ Evidence


232. WorkPackage can contain explicit DO_NOT_ASSUME
Example:
DO NOT ASSUME token writes are cross-process safe.


233. ImplementationPlan is derived, not external truth
It is allowed to change as decisions change.

Part XVII — Knowledge snapshots
234. Introduce KnowledgeSnapshot
A coding wave must have stable research under it.

235. Snapshot schema
snapshot_id
project_id

created_by_run

contract_version

source pins

requirement versions
claim versions
decision versions
entity versions
relation versions
gap versions

implementation_plan_version

freshness state

created_at
supersedes_snapshot


236. Active coding agents pin a snapshot
Example:
Implementation Wave 4
uses DRA snapshot S42


237. New research creates a new snapshot
Do not silently alter S42 under a running coding agent.

238. Snapshot works like a research lockfile
It provides reproducibility for implementation and later debugging.

Part XVIII — Retrieval architecture
239. Remove literal "3D memory" from the core design
No reviewed source demonstrates benefit from arranging research knowledge in literal 3D coordinates.
Use 3D visualization only if helpful for human exploration.

240. Do not choose pure vector RAG
Similarity alone is weak for exact dependencies and multi-hop repository structure.
CodexGraph explicitly identifies low recall from similarity-only retrieval on complex repository tasks.

241. Do not choose pure graph retrieval either
Natural-language coding questions still require semantic entry into the graph and source-text retrieval.

242. Adopt Hybrid Implementation Knowledge Navigation
exact
+
lexical
+
vector
+
typed graph traversal
+
evidence/verification filters
+
sufficiency


243. Postgres remains canonical for MVP
Current DRA ADRs already make the reasonable decision to colocate relational provenance and vector capability, with reversal triggers if scale/SLOs fail.
No Neo4j migration is justified yet.

244. Graph relations live in typed relational tables
Use:
normal joins;
recursive CTEs;
indexes;
materialized derived views.

245. Dedicated graph DB reversal triggers
Consider one only if real benchmarks show:
traversal latency unacceptable;
graph operations dominate complexity;
graph size exceeds Postgres practicality;
development/query ergonomics materially hurt product velocity.

246. Add rebuildable retrieval_index
entity_id
entity_kind

retrieval_text

tsvector

embedding
embedding_model
embedding_version

text_hash

state


247. What gets embedded
Good candidates:
requirement;
claim;
decision;
implementation entity;
component summary;
normalized evidence span;
work package.

248. Do not embed raw captures as the only semantic representation
Raw captures can be huge and contain unrelated material.
Use normalized semantic units.

249. Embedding version is explicit
Embedding upgrades rebuild the retrieval index.
They do not alter canonical knowledge.

250. Query pipeline
coding/research query
        ↓
exact anchor extraction
        ↓
lexical search
        +
vector search
        ↓
candidate fusion
        ↓
state/version/scope filtering
        ↓
typed graph expansion
        ↓
reranking
        ↓
context packing
        ↓
sufficiency check


251. Exact anchors outrank fuzzy retrieval
Recognize:
requirement ID;
work ID;
symbol;
path;
component;
decision;
claim;
commit;
version.

252. Lexical search remains important
Exact API/symbol names are often better served by lexical search than embeddings.

253. Vector search is candidate generation
It answers:
What canonical entities are semantically near this task?
It does not answer:
What is true?

254. Graph expansion supplies structural neighborhood
Example:
WorkPackage
→ target component
→ called symbols
→ lifecycle owner
→ related tests
→ supporting decision
→ evidence


255. Query-specific relation policies
Debugging favors:
CALLS
READS
WRITES
TESTED_BY
DEPENDS_ON

Architecture favors:
CONTAINS
IMPLEMENTS
DEPENDS_ON
CONSTRAINS

Migration favors:
MIGRATES_FROM
DEPENDS_ON
TESTED_BY
SUPERSEDES


256. Bound graph expansion
Parameters:
max_hops
allowed_relations
node_budget
token_budget


257. Never permanently create all vector-similar semantic edges
That would create graph noise and costly embedding-version invalidation.

258. GraphRAG-style hybrid retrieval is the useful analogy
Semantic entities can act as access points into structured relations and linked source text.
DRA should adapt the principle to implementation knowledge rather than copy GraphRAG's community-report architecture.

Part XIX — Context compilation
259. Model context is a compiled working set
Database = memory
Prompt context = cache


260. ResearchFocusView
For investigator/supervisor attention:
objective
active task
why it matters

recently resolved

current blockers
contradictions

next candidate tasks

do_not_repeat

budget
stop condition


261. ResearchFocusView is generated
Never maintain a competing todo file.

262. Context budgets are token-based
Existing row caps remain defensive.
Primary budget:
max_tokens


263. Category budgets
Optionally reserve soft portions for:
requirements
decisions
entities
claims
evidence
gaps


264. Progressive disclosure
Initial bundle contains high-value state.
Coding agent can request deeper evidence/graph neighborhoods on demand.

Part XX — Consumption sufficiency
265. Introduce ContextRequirement
Compile the coding task into obligations.

266. Example obligations
For:
implement retry cleanup
the context may require:
owning component;
public interface;
lifecycle;
error classification;
retry semantics;
synchronization behavior;
affected tests.

267. Sufficiency Stage A — deterministic coverage
Check whether required linked objects are present.

268. Sufficiency Stage B — semantic missing-obligation audit
A model may identify a missing implementation-significant concern.
It cannot verify facts.

269. SufficiencyAssessment
status

covered_requirements[]

selected_claims[]
selected_entities[]
selected_decisions[]

missing_obligations[]

contradictions[]
wrong_version_items[]
stale_items[]

do_not_assume[]

suggested_expansions[]

research_required


270. Sufficiency statuses
SATISFIED
INCOMPLETE
CONFLICTED
BLOCKED


271. Retrieval loop
retrieve
→ assess
→ identify missing
→ retrieve specifically for missing
→ reassess

This is the useful R1-derived primitive.

272. Search existing DRA knowledge before researching externally
Escalation order:
exact graph
→ broader graph
→ lexical
→ vector
→ alternative snapshot/project evidence
→ new ResearchTask

subject to scope/freshness policy.

273. Missing corpus knowledge may reopen research
If the coding task needs something DRA never established:
Consumer Gap
→ ResearchTask
→ new Snapshot


Part XXI — ImplementationContextBundle
274. Bundle schema
snapshot_id
work_package_id
consumer_task

readiness

objective

requirements
constraints
invariants

decisions

implementation_entities
critical_relations

interfaces
dependencies
lifecycle_and_ownership
state_and_concurrency
failure_semantics

tests_and_acceptance

claims

contradictions
unresolved_gaps

do_not_assume

evidence_handles
graph_handles
next_queries

sufficiency


275. Readiness statuses
READY
READY_WITH_QUALIFICATIONS
NOT_READY


276. DO_NOT_ASSUME is mandatory when appropriate
This is an important safety interface for coding agents.

277. Evidence handles are lazy
A bundle need not inline every source excerpt.
It should provide exact retrievable IDs.

Part XXII — Coding-agent API / MCP
278. DRA should expose a standard machine interface
MCP is a strong candidate because the DR survey identifies MCP as an extensibility mechanism and GPT Researcher demonstrates heterogeneous integration patterns.
Keep a plain HTTP/library API underneath it.

279. dra.context
Input:
snapshot
work package / task
token budget

Output:
ImplementationContextBundle.

280. dra.search
Hybrid search over canonical entities.

281. dra.expand
Typed graph traversal.

282. dra.claim
Return claim, state, scope and related evidence.

283. dra.evidence
Return exact evidence locator and provenance chain.

284. dra.entity
Return target-system implementation entity and structural neighbors.

285. dra.decision
Return alternatives, rationale, supporting/opposing claims and reversal triggers.

286. dra.readiness
Evaluate coding-task readiness without returning full context.

287. dra.report_gap
Coding agents can report newly observed discrepancies.

288. Consumer observations are not automatically facts
Create:
ConsumerObservation

for review/reproduction.

289. Reproducible coding-agent execution can become evidence
If the harness supplies:
pinned snapshot;
repository SHA;
command;
environment;
output;
DRA can ingest it through the normal execution-evidence path.

Part XXIII — Implementation handoff
290. Retain the eight-section human package concept
The current structure is useful:
executive
requirements
architecture
source-system understanding
implementation plan
decisions
risks
evidence index


291. Replace target-independent hardcoded prose
Every section must be generated from the researched target's canonical state.

292. Architecture section comes from implementation graph
Not from DRA's internal architecture unless DRA itself is the research target.

293. Implementation-plan section comes from ImplementationPlan
Never from ResearchTask.dependencies.

294. Requirement section references canonical Requirement IDs
No topic-as-requirement workaround.

295. Evidence index resolves through snapshot IDs
All IDs must remain dereferenceable through the consumption API.

Part XXIV — Finalization
296. Correct finalization sequence
research readiness
      ↓
candidate KnowledgeSnapshot
      ↓
candidate ImplementationPlan
      ↓
manifest
      ↓
human handoff
      ↓
HandoffIntegrityAudit
      ↓
selective freshness/scope audit
      ↓
FINALIZATION TRANSACTION
      ↓
COMPLETE


297. A run is not complete before rendered artifacts pass
This fixes the subtle ordering problem in the earlier spec.

298. Finalization requirement — Research Contract
Every blocking requirement must be:
VERIFIED
QUALIFIED with permitted qualification
ACCEPTED_UNKNOWN explicitly


299. Finalization requirement — Claims
Critical implementation claims require acceptable verification.

300. Finalization requirement — Verification scope
Verification must explicitly cover the candidate snapshot.

301. Finalization requirement — Contradictions
No unresolved blocking contradiction.

302. Finalization requirement — Sources
Required target-system / primary-source policies must be satisfied.

303. Finalization requirement — Version pins
Required exact:
commit;
API;
package;
platform;
must match.

304. Finalization requirement — Artifact durability
Every required evidence locator and blob must resolve.

305. Finalization requirement — ImplementationPlan
No supposedly READY work package may contain a blocking gap.

306. Finalization requirement — Handoff integrity
Rendered prose may not overstate canonical claims.

307. Finalization requirement — Publication
Candidate snapshot and handoff must successfully persist.

308. No LLM override
HyperResearch's hard finish_run() pattern is the useful reference here.

Part XXV — Freshness and lifecycle
309. Freshness belongs to claims/evidence policy
Not every source needs refetching every run.

310. Immutable evidence
Examples:
pinned commit
content-addressed capture
published paper version

does not need routine recapture.

311. Mutable evidence
Examples:
current docs
current API
security advisory
current product behavior
web page

may require refresh.

312. Retraction/deprecation sweep
Apply where relevant:
scholarly claims;
security information;
APIs;
package compatibility.

313. Lifecycle states
Useful canonical states:
ACTIVE
SUPERSEDED
STALE
REJECTED
ARCHIVED


Part XXVI — Execution failures and operations
314. Typed task outcomes
SATISFIED

EVIDENCE_GAP
CONTRADICTION
SOURCE_EXHAUSTED

BLOCKED_PROVIDER
BLOCKED_RATE_LIMIT
BLOCKED_AUTH
BLOCKED_POLICY
BLOCKED_SOURCE
BLOCKED_EXTRACTION
BLOCKED_SANDBOX
BLOCKED_TOOL

BUDGET_EXHAUSTED
TIME_EXHAUSTED

SUPERSEDED
CANCELLED

FAILED_INTERNAL


315. Provider health
Eventually track provider/capability state:
HEALTHY
DEGRADED
OPEN
HALF_OPEN


316. Circuit breakers
tarun7r provides useful concrete precedent for treating external calls as failure-prone services.
Implement only after core semantic correctness is fixed.

317. Health-aware routing
Consider:
capability fit
source quality
cost
latency
health
authorization


318. Retry policy is failure-specific
A 429 differs from an invalid URL.

319. Task metrics
wall time

model calls
tokens
model cost

search calls
fetch calls
browser calls
repo calls
execution calls

sources discovered
captures made
evidence created
claims created
gaps resolved

retries
failures


320. Replace symbolic phase cost with measured cost
Current phase-cost units can remain test scaffolding.
Production budgets use actual task/call costs.

321. Budget hierarchy
run
task
capability


322. Budget exhaustion is an incomplete condition
Never a quiet success.

323. Durable event stream
Events such as:
TASK_CREATED
TASK_STARTED
SOURCE_DISCOVERED
CAPTURE_PUBLISHED
CLAIM_VERIFIED
GAP_OPENED
GAP_RESOLVED
STEERING_APPLIED
SNAPSHOT_FINALIZED

improve observability and later procedural learning.

Part XXVII — Recovery
324. Research control and knowledge recover independently
A checkpoint is not enough.

325. Recovery reconciler
Compare:
workflow checkpoint
task ledger
canonical publication state


326. Published evidence wins over stale checkpoint state
If a worker published successfully but crashed before checkpointing:
do not repeat the full investigation blindly.

327. Checkpoint saying complete cannot override missing canonical output
Knowledge-store truth remains authoritative.

Part XXVIII — Caching
328. Capture cache
Key by:
source identity
representation
version/freshness


329. Derived artifact cache
Key by:
input blob hash
transformer version
configuration


330. Task-result reuse
Only when:
research contract compatible;
pins match;
evidence fresh;
required source scopes unchanged.

331. Do not cache a whole research answer solely by topic string
Too much hidden context affects implementation research.

Part XXIX — Security
332. External content is untrusted data
It can inform research.
It cannot issue system instructions.

333. Prompt-injection boundary
Fetched text is explicitly wrapped/tagged as source content.

334. Least privilege
Capabilities get only the permissions they require.

335. Discovery tools
Network read only.

336. Repo inspection
Target repository read only.

337. Repo execution
Isolated sandbox.

338. Verification
Canonical read plus narrowly scoped verification writes.

339. Renderer
Snapshot read plus artifact output.

340. Private/internal sources
Must retain project/tenant authorization in:
canonical state;
retrieval index;
context compiler.

341. Never place private evidence into a globally queryable embedding namespace
Retrieval isolation is mandatory.

342. SSRF protections
Block cloud metadata/private network destinations unless explicitly authorized.

343. Browser/network capture authorization
HAR or authenticated network observation requires explicit access policy.

344. Sandbox manifest
Every execution captures:
repo SHA
image
dependency versions
OS
hardware
network policy
command
resource limits


Part XXX — ResearchPlaybooks
345. Start with a small set
REPO_ARCHITECTURE
REPO_DEBUGGING
API_SOURCE_OF_TRUTH
IMPLEMENTATION_MECHANISM
TECH_COMPARISON
ACADEMIC_LITERATURE
PERFORMANCE_BENCHMARK
SECURITY_REVIEW
MIGRATION_ANALYSIS


346. Playbooks are guidance
They cannot override:
task acceptance;
source policy;
verification;
budget.

347. Do not create a playbook for every source type
Source type answers:
where?
Playbook answers:
how?

Part XXXI — Model routing
348. Route by role
Possible roles:
SUPERVISOR
RESEARCH_WORKER
CLAIM_SYNTHESIS
VERIFIER
CRITIC
SUFFICIENCY
DECISION_SYNTHESIS
RENDERER


349. Do not assume the most expensive model belongs everywhere
Benchmark each role.

350. Structured-output reliability matters
Models producing canonical candidates require strict schema adherence.

351. Config snapshot records exact models and policy versions
Every snapshot should be reproducible enough to audit its production path.

Part XXXII — Behavioral resource versioning
352. Later introduce ResearchResource
Possible kinds:
PLAYBOOK
PROMPT
MODEL_POLICY
PROVIDER_POLICY
ROUTING_POLICY
VERIFICATION_POLICY
CONTEXT_POLICY


353. Resource lifecycle
CANDIDATE
VALIDATED
ACTIVE
RETIRED
REJECTED


354. Every research run records active resource versions
This is the safe useful lesson from Skywork.

Part XXXIII — Procedural memory
355. Epistemic memory and procedural memory are separate
Epistemic:
what is true about the researched system?
Procedural:
what research strategy tends to work?

356. ResearchExperience
Later schema:
task characteristics
strategy

playbook
capabilities

outcome

cost
latency

gaps resolved
failures

downstream utility

lesson


357. Do not build procedural memory before real runs exist
Synthetic trajectories are insufficient training evidence.

Part XXXIV — Offline evolution
358. Safe evolution pipeline
real run evidence
→ candidate resource
→ static validation
→ isolated test
→ research benchmark
→ downstream coding benchmark
→ promote/reject


359. No direct production code exec
Explicitly reject unsafe self-modification patterns.

Part XXXV — Evaluation architecture
360. Component correctness tests
Test:
source identity;
blob durability;
claim/evidence linkage;
scope matching;
verification;
graph extraction;
finalization;
retrieval isolation.

361. Research-quality metrics
requirement coverage
primary-source coverage
target-system coverage

claim support
claim verification

contradiction detection
gap detection

source independence

freshness

unknown calibration

cost
latency


362. Retrieval-quality metrics
required-knowledge recall
irrelevant-context rate

wrong-version retrieval
wrong-scope retrieval

token count
latency

follow-up retrieval count


363. Downstream coding metrics
acceptance-test pass rate

wrong assumptions
missed constraints

re-research calls
implementation revisions
architectural rework

human intervention

tokens
cost
wall time
time-to-green


364. Real coding-agent evaluation is mandatory
The current fake-agent downstream harness is useful only as a deterministic mechanism test.
Do not market its current numbers as DRA performance.

365. Preserve the current five-arm idea
It is actually a good experiment design:
raw sources
ordinary report
structured corpus
handoff
handoff + corpus


366. Extend it for the revised architecture
Add:
linkage/lexical baseline
vector top-k
lexical + vector
hybrid graph navigation
hybrid + sufficiency
hybrid + sufficiency + on-demand research


367. Hybrid retrieval must earn its complexity
If simpler lexical/vector retrieval performs equally well downstream, simplify.

368. Sufficiency checking must also earn its complexity
Compare:
static bundle
vs
sufficiency-checked bundle


369. Adaptive control must earn its complexity
Compare adaptive supervisor against a simpler fixed controller on real tasks.

370. Playbooks must earn their complexity
Compare generic worker vs specialized playbook.

371. External report benchmarks are secondary
RACE/DeepResearchBench/LiveResearchBench are useful for general research competence.
They do not measure DRA's full product thesis.

Part XXXVI — Assumptions register revision
372. Keep A1 — jobs usually minutes/hours
LangGraph remains reasonable initially.
If jobs routinely last days/weeks, reconsider Temporal/durable workflow infrastructure.

373. Keep A2 — moderate corpus scale
Postgres + pgvector remains sensible until measured SLO reversal.

374. Strengthen A3 — relational provenance matters alongside similarity
Repository-graph research provides stronger external support for this than we had originally.

375. Keep A4 — durable artifact storage is possible
But the implementation must now actually provide it.

376. Keep A5/A6 — authorized inspection and sandboxing
Still central.

377. A7 remains unproven
downstream coding agents benefit from structured handoff + queryable knowledge
is still a product hypothesis, despite strong rationale.
It requires real coding-agent evaluation.

378. Keep A8/A9/A10
Multi-provider, asynchronous-enough research and Python/Postgres/container operations remain reasonable product assumptions.

379. Keep A11
DRA remains research-first rather than autonomous production deployer.

380. Keep A12
Quality is implementation uncertainty removed, not prose elegance.
This remains the central product choice.

381. New A13 — coding agents can productively use structured retrieval APIs
Verify via real agent integrations.
Reversal:
simplify toward richer inline handoffs if tool use is consistently poor.

382. New A14 — implementation obligations can be inferred reliably enough for sufficiency checking
Verify against human-labeled implementation tasks.
Reversal:
make explicit WorkPackage requirements more important and semantic sufficiency more advisory.

383. New A15 — repository structural extraction has adequate coverage
Initially language/tool coverage will be incomplete.
Unsupported languages must degrade gracefully rather than fabricate graph edges.

Part XXXVII — Migration safety
384. Reconcile Alembic heads before schema v2
No large relation schema lands before migration history is clean.

385. Introduce explicit schema version
knowledge_schema_version


386. Preserve compatibility readers during migration
Old v1 evidence remains readable.

387. Do not rewrite historical provenance gratuitously
Migration may construct new relation records from old evidence but must preserve original rows.

388. Feature flags
Suggested:
CONTROL_MODE=legacy|adaptive
RETRIEVAL_MODE=linkage|hybrid
CLAIM_MODE=legacy|task_conditioned

for staged rollouts.

Part XXXVIII — Revised implementation waves
389. Wave 0 — Migration and safety baseline
Implement
reconcile Alembic history;
freeze current schema baseline;
add explicit schema version;
add regression tests around current publication.
Gate
No ambiguous migration heads.

390. Wave 1 — Source/capture correctness
Implement
ContentBlob
SourceCapture
normalized SourceIdentity
safe source get-or-create
BlobStore

Fix
website page identity;
snippet URL binding;
discovery vs evidence;
durable raw bytes.

391. Wave 2 — ResearchWorker and capability contracts
Implement
ResearchWorker
ResearchCapability
ToolResult
task-conditioned context

Demote repo/paper/web investigators to capabilities.

392. Wave 3 — Task-conditioned epistemic output
Replace
P7 placeholder claims;
paper template claims;
website acquisition claims;
generic repository pass inference.
Add
ClaimSynthesisService
task acceptance
real ResearchPacket


393. Wave 4 — Canonical implementation reasoning schema
Add
requirement
claim_evidence
decision_basis
requirement_link
claim_relation
gap_link
implementation_relation


394. Wave 5 — Verification v2
Implement
run/snapshot scope;
explicit read/apply modes;
layered semantic verification;
scope/version checks;
correct execution evidence;
incremental affected-claim verification.

395. Wave 6 — Real gaps and decisions
Replace
critic-question fake gaps;
placeholder Phase-12 decisions.
Add
deterministic obligation gaps;
semantic GapCandidates;
DecisionRecord v2;
explicit closure evidence.

396. Wave 7 — Separate ImplementationPlan
Implement
ImplementationPlan
ImplementationWorkPackage
implementation dependency graph
readiness

Rewrite handoff generation around target knowledge.

397. Wave 8 — Hybrid retrieval
Implement
retrieval_index
FTS
pgvector embeddings
KnowledgeNavigator
typed graph expansion
token budgets


398. Wave 9 — Retrieval bake-off
Before deeper integration, compare:
linkage
vector
lexical+vector
hybrid graph

on real implementation queries.
Kill unnecessary complexity if it loses.

399. Wave 10 — Adaptive Research Contract/control
Implement
ResearchContract
Requirement lifecycle
research_task ledger
task dependencies
plan events
steering
adaptive supervisor rounds

Retire mandatory fixed recon fan-out gradually.

400. Wave 11 — Research context engineering
Add
ResearchFocusView
ResearchPacket compiler
bounded investigator context
bounded supervisor context


401. Wave 12 — Contradictions / falsification / independence
Implement
claim relations;
duplicate-source clustering;
selective falsification tasks;
scope conflict classification.

402. Wave 13 — KnowledgeSnapshot and finalization
Implement
KnowledgeSnapshot
candidate snapshot
HandoffIntegrityAudit
selective freshness
hard atomic finalization

Retire current permissive P14 semantics.

403. Wave 14 — Coding-agent consumption surface
Implement
dra.context
dra.search
dra.expand
dra.evidence
dra.entity
dra.decision
dra.readiness
dra.report_gap

via HTTP/library + MCP.

404. Wave 15 — Sufficiency loop
Implement:
ContextRequirement
deterministic coverage
semantic missing-obligation audit
ImplementationContextBundle v3


405. Wave 16 — Execution hardening
Only now prioritize:
circuit breakers;
provider health;
caching;
actual cost telemetry;
durable events;
recovery reconciliation;
security hardening.

406. Wave 17 — Real end-to-end evaluation
Run real:
repositories;
coding agents;
implementation tasks;
acceptance tests;
multiple models/seeds.
This is the primary architecture judgment point.

407. Wave 18 — Project-scoped cross-run reuse
Add only after snapshots and freshness work reliably.

408. Wave 19 — Procedural memory
Persist ResearchExperience only once enough real runs exist.

409. Wave 20 — Offline behavioral optimization
Evaluate versioned playbooks/prompts/policies.

410. Wave 21 — Optional deeper asynchrony
Only pursue continuously asynchronous task scheduling if batch barriers become a measured bottleneck.

Part XXXIX — Explicit near-term non-goals
411. No 3D retrieval geometry
Visualization only.

412. No dedicated graph DB yet
Measure first.

413. No RL scheduling yet
No training evidence exists.

414. No online self-evolution
Evaluation infrastructure comes first.

415. No dozens of agents by default
Parallelism follows task shape.

416. No source-count target
More sources can simply mean more duplicated noise.

417. No citation-density target
Evidence quality matters more.

418. No one scalar credibility score
Use claim-specific evidence policy.

419. No global automatic corpus mixing
Project and snapshot scopes remain explicit.

420. No report-section research ownership
Parallelize knowledge acquisition, centralize canonical knowledge.

Part XL — Target end-to-end architecture
421. Full flow
USER
 │
 ▼
Progressive Intent Discovery
 │
 ▼
ResearchContract
 │
 ├── Requirements
 ├── Constraints
 ├── Pins
 └── Success Criteria
 │
 ▼
Adaptive Supervisor
 │
 ▼
Research Task Ledger
 │
 ▼
Ready Task Batch
 │
 ▼
ResearchWorker(s)
 │
 ├── Search / Discovery
 ├── Repo inspection
 ├── Web acquisition
 ├── Paper acquisition/parsing
 ├── Browser inspection
 └── Execution
 │
 ▼
SourceCandidate
 │
 ▼
SourceIdentity / Representation
 │
 ▼
SourceCapture
 │
 ▼
ContentBlob
 │
 ▼
DerivedArtifact
 │
 ▼
EvidenceUnit
 │
 ▼
ClaimSynthesis
 │
 ▼
Claims / Implementation Relations
 │
 ▼
Verification
 │
 ├── provenance
 ├── deterministic integrity
 ├── semantic support
 ├── source independence
 ├── scope/version
 └── execution
 │
 ▼
Requirements / Gaps / Contradictions
 │
 ▼
Supervisor Reassessment
 │
 ├── more research
 ├── falsify
 ├── ask user
 ├── accept unknown
 └── decide
 │
 └───────────────────────────────┐
                                 │
                     repeat until ready
                                 │
                                 ▼
                          Decision Records
                                 │
                                 ▼
                       ImplementationPlan
                                 │
                                 ▼
                      KnowledgeSnapshot Candidate
                                 │
                    ┌────────────┴────────────┐
                    ▼                         ▼
              Machine Manifest          Human Handoff
                    │                         │
                    └────────────┬────────────┘
                                 ▼
                       HandoffIntegrityAudit
                                 │
                                 ▼
                         FinalizationGate
                          │            │
                         FAIL         PASS
                          │            │
                       INCOMPLETE      ▼
                              Verified Snapshot
                                      │
                                      ▼
                             KnowledgeNavigator
                         exact + FTS + vector + graph
                                      │
                                      ▼
                              ContextRequirement
                                      │
                                      ▼
                              SufficiencyEvaluator
                              │                 │
                        SATISFIED          INCOMPLETE
                              │                 │
                              ▼                 ▼
                    ImplementationContext   retrieve more /
                         Bundle            reopen research
                              │
                              ▼
                         CODING AGENT
                              │
                              ▼
                   Consumer observations/tests
                              │
                              ▼
                       dra.report_gap
                              │
                              └──► future research/snapshot


Part XLI — Core API contracts
422. run_research_task
async def run_research_task(
    task_id: str,
) -> ResearchExecutionResult


423. ResearchExecutionResult
task_id
attempt_id

outcome

packet_id

capture_ids[]
evidence_ids[]
claim_ids[]
entity_ids[]
relation_ids[]

gaps_opened[]
gaps_closed[]

operational_failures[]

cost
tokens
wall_time

stop_reason


424. verify_claims
async def verify_claims(
    *,
    claim_ids: list[str],
    mode: VerificationMode,
) -> VerificationBatch

No implicit global scope.

425. navigate_knowledge
async def navigate_knowledge(
    *,
    snapshot_id: str,
    query: str,
    relation_policy: str,
    token_budget: int,
) -> KnowledgeNavigationResult


426. compile_context
async def compile_context(
    *,
    snapshot_id: str,
    consumer_task: str,
    work_package_id: str | None,
    token_budget: int,
    allow_research: bool = False,
) -> ImplementationContextBundle


427. finalize_snapshot
async def finalize_snapshot(
    run_id: str,
) -> FinalizationResult

Only this creates verified consumable snapshots.

Part XLII — Definition of READY
428. A coding work package is READY only if
relevant blocking requirements are verified/accepted;
required target-system entities are known;
important interfaces/dependencies are known;
ownership/lifecycle is known where relevant;
failure behavior is known where relevant;
required version pins match;
acceptance tests are known;
no blocking contradiction remains;
no blocking gap remains.

429. READY_WITH_QUALIFICATIONS
Allowed only when remaining uncertainty is explicitly nonblocking.

430. NOT_READY
Must be returned rather than fabricating context.

Part XLIII — Example final coding context
431. Example
SNAPSHOT: S42
WORK PACKAGE: WP-17
READINESS: READY

Objective
--------
Add encrypted v3 credential persistence while retaining v2 migration.

Requirements
------------
R12 — Preserve v2 compatibility.
R18 — Atomic persistence.
R24 — No plaintext token at rest.

Decisions
---------
D4 — Reuse AuthStore ownership boundary.
D7 — Migrate on successful v2 read rather than startup scan.

Target entities
---------------
AuthStore.save()
AuthStore.load()
V2TokenParser
TokenRecord
token_migration_test

Critical relations
------------------
AuthService → CALLS → AuthStore.save()
AuthStore.load() → USES → V2TokenParser
token_migration_test → TESTS → AuthStore.load()

Lifecycle
---------
AuthStore owns persistence.
Callers must not write credential files directly.

Failure semantics
-----------------
Pre-rename write errors are retryable.
Post-rename fsync failure is reported but must not rewrite the old token.

Acceptance
----------
- existing v2 migration tests remain green
- v3 encrypted storage test passes
- malformed-v2 behavior remains unchanged

DO NOT ASSUME
-------------
None.

Claims
------
C44 VERIFIED
C51 VERIFIED
C63 EXECUTION_VERIFIED

Evidence
--------
E91 repo@9ad72:src/auth/store.py:AuthStore.save
E102 official migration documentation
E130 execution:test_token_migration

Remaining nonblocking uncertainty
---------------------------------
G90 — behavior above 100k credentials is unbenchmarked.


Part XLIV — Final architectural decisions
432. Original vector-database idea
Keep partially.
Use pgvector as a rebuildable semantic index.
Do not make the vector index canonical memory.

433. Original graph-memory idea
Keep and strengthen.
The graph should be:
typed;
provenance-backed;
implementation-oriented;
requirement-aware.
Repository-level graph research provides meaningful independent support for this choice.

434. Original "semantic edges" idea
Modify substantially.
Persist only meaningful stable typed relationships.
Compute loose semantic similarity at retrieval time.

435. Original "3D memory" idea
Reject as a core architecture.
No evidence found that literal 3D spatial placement improves agent reasoning or implementation retrieval.
It may later become a visualization/debugging feature.

436. Current fixed 15-phase graph
Retain temporarily as migration scaffolding, not final architecture.
Its durable lifecycle and existing tests are useful.
Its semantic phase boundaries should gradually collapse into:
contract
→ supervisor
→ worker batch
→ knowledge/verification
→ reassess
→ finalize


437. Current typed investigators
Preserve heavily, but reposition.
They become specialized acquisition/inspection capabilities beneath a task-conditioned ResearchWorker.
This is a bigger architectural correction than simply changing retrieval.

438. Current evidence substrate
Preserve the provenance philosophy, not every schema decision.
Required corrections include:
real blob storage;
capture/content separation;
M:N claim evidence;
M:N decision bases;
canonical requirements;
implementation relations;
scoped verification.

439. Current handoff architecture
Preserve as a human projection but rebuild its semantics.
The machine snapshot + ImplementationPlan + query API become more important than prose.

440. Current downstream retrieval
Replace rather than merely extend.
The current linkage/ILIKE implementation becomes one baseline arm in the retrieval bake-off.
The target is hybrid navigation + sufficiency.

Part XLV — Governing thesis
441. Research
DRA researches until implementation-significant uncertainty is either:
resolved
explicitly contradicted
explicitly accepted
or explicitly blocked


442. Knowledge
DRA stores what it learns in a form where:
claim
requirement
decision
code entity
test
evidence

can be traversed without reconstructing relationships from prose.

443. Implementation
DRA converts knowledge into explicit WorkPackages rather than expecting the coding agent to infer the whole implementation plan from a report.

444. Consumption
DRA does not merely retrieve relevant information.
It checks whether the selected information is sufficient for the exact implementation task.

445. Evidence
Every high-impact implementation decision can be walked backwards to exact researched evidence.

446. Uncertainty
Anything the research failed to establish remains visible to the coding agent.

447. Evaluation
Any architectural feature that does not improve real implementation outcomes is removable.

448. Final target
The finished DRA should be understood as:
an adaptive research system that acquires and verifies evidence, constructs a version-pinned Implementation Knowledge Graph and ImplementationPlan, and exposes both through sufficiency-checked navigation interfaces so real coding agents can implement concrete work while remaining able to trace every important technical assumption back to researched evidence.
That—not the report, the vector store, the graph itself, or the number of agents—is the product.

