# Assumptions register (Section 3 of the spec)

Source of truth: `docs/practical_deep_research_system_design_spec_v3_final_audited.md`,
Section 3. These assumptions are ported verbatim; if several are false, the
architectural conclusion should be revisited. This register is the living copy
kept in-source; the spec text remains authoritative.

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

## Decision-label key (from the spec preamble)

| Label | Meaning |
|---|---|
| F | Fact — externally verifiable, not product-specific. |
| R | Reasoning — a defensible inference, not yet benchmark-proven. |
| ERI | Engineering reasoning — product-scoped inference. |
| PD | Product decision — a deliberate engineering choice made for this product, not a universal fact. |
| H | Heuristic requiring calibration — a starting policy that must be tuned with internal evaluation. |

Implementers should preserve these labels in ADRs so later teams do not mistake a convenience decision for an externally proven fact.
