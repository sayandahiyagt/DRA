# ADR-020 — §38.2 Storage proof: verify pgvector HNSW meets ADR-003 reversal triggers

- **Decision type:** Evidence-driven PD
- **Confidence:** High (measured against live pgvector 0.8.6)
- **Status:** Accepted
- **Evidence:** §38.2 storage proof (lines 2633-2643 of the spec); §25.3
  vector-store migration triggers (lines 1991-1999); ADR-003 reversal triggers;
  pgvector README [R21] (HNSW monitoring + `iterative_scan` guidance).
- **Decision:** Adopt Postgres + pgvector with HNSW as the MVP vector store
  (confirm ADR-003). The §38.2 storage proof loads a deterministic 25,000-vector
  synthetic corpus with controlled tenant/project/topic cluster structure into a
  standalone `proof_corpus` table (not mutating the committed `derived_artifact`
  schema). It compares exact (sequential, `enable_indexscan=off`) vs HNSW
  approximate retrieval, measures filtered recall vs exact (R21-compliant),
  p50/p95 latency, tenant/project filtering isolation, and update/delete/
  staleness workloads — then emits a machine-checkable `proof_report.json`
  with pass/fail against all five ADR-003 reversal triggers.
- **Why chosen / Alternatives:** The ADR-003 reversal triggers (§25.3) are
  qualitative; the proof fixes concrete numeric SLOs so the decision can be
  *falsified*, not merely explained (§38.1 framing). A standalone `proof_corpus`
  table avoids mutating the approved dra#14 schema contract (which
  `test_schema_introspection` asserts). Synthetic corpus avoids dataset
  licensing and network dependency (ACTIVITY.md #13 step2 / #14 step3 sandbox
  constraints).
- **Reversal trigger:** Any ADR-003 reversal trigger fires (see outcome below)
  — i.e., corpus/vector count < 10,000, p95 latency >= SLO after `ef_search`
  sweep, cross-tenant isolation leak >= 1, filtered ANN recall < 0.90, or
  reindex latency >= budget.
- **Consequences:**
  - Positive: pgvector HNSW with `iterative_scan = relaxed_order` (pgvector
    0.8.6) achieves recall = 1.0 and p95 < 4 ms at `ef_search=320` on 25k
    vectors, with recall rising from ~0.90 (`ef_search=40`) to 1.0
    (`ef_search=320`) — the expected HNSW accuracy/latency tradeoff. REINDEX
    of the HNSW index after a 1,000-vector mutation batch completes in ~4.2 s
    (well within the 10 s budget).
  - Neutral: pgvector 0.8.x supports incremental HNSW inserts, so newly
    inserted vectors are searchable immediately (no staleness without reindex
    for single inserts). This is a behavioral note, not a code defect.
  - Risk: the synthetic corpus has near-perfect cluster separation (block-
    anchored tenant directions); real workloads with overlapping embeddings
    should be re-validated before deployment.

## Reversal trigger outcome

| Trigger | Measured value | Threshold | Result | Reversal triggered? |
|---|---|---|---|---|
| corpus_vector_count | 25,000 | >= 10,000 | PASS | no |
| p95_retrieval_latency | 3.696 ms | < 50 ms | PASS | no |
| multi_tenant_isolation | 0 | 0 leakage | PASS | no |
| filtered_ann_recall | 1.0 | >= 0.90 | PASS | no |
| write_throughput | 4,171.3 ms | < 10,000 ms | PASS | no |

**Overall verdict: PASS** — no ADR-003 reversal trigger fired.

## Measured details

- **pgvector version:** 0.8.6 (`hnsw.iterative_scan` supported).
- **Corpus:** 25,000 vectors, dim=384, 3 tenants, 2 projects/tenant,
  5 topics/project, seed=42. Tenant directions use 128-dim block anchors
  (dims 0–127, 128–255, 256–383).
- **Exact (sequential) retrieval:** p50=3.539 ms, p95=3.913 ms (200 filtered
  queries, k=10, `SET LOCAL enable_indexscan = off` per R21).
- **HNSW (m=16, ef_construction=200):** recall rises with `ef_search` — the
  signature of a genuinely engaged approximate index (the operator `<->`
  matches the `vector_l2_ops` index opclass; R21).

  | ef_search | recall@10 | p50 (ms) | p95 (ms) |
  |-----------|-----------|----------|----------|
  | 40        | 0.8960    | 1.263    | 2.064    |
  | 80        | 0.9740    | 1.470    | 2.131    |
  | 160       | 0.9965    | 1.588    | 2.353    |
  | 320       | 1.0000    | 3.293    | 3.696    |

  Best: ef_search=320, recall=1.0000, p95=3.696 ms. At ef_search=40 the index
  is genuinely approximate (recall 0.896 < 1.0); HNSW p95 (~1.3–3.7 ms) is
  consistently below exact p95 (3.913 ms).
- **Filtered recall (per tenant):** tenant_0=1.0, tenant_1=1.0, tenant_2=1.0.
- **Tenant isolation (unfiltered ANN, top-50):** 0 cross-tenant leaks.
- **Workloads:**
  - Mutation: 1,000 vectors inserted; pgvector 0.8.x supports incremental HNSW
    inserts, so `detected_stale_before_reindex = false` (new vectors were
    immediately searchable); REINDEX latency=4,171.3 ms; recall restored
    post-reindex.
  - Deletion: 1,000 rows deleted; 0 ghost IDs returned.
  - Stale invalidation: 1,250 rows invalidated to `state='superseded'` +
    `superseded_by` set; 0 leaked stale IDs; query latency=27.6 ms.
