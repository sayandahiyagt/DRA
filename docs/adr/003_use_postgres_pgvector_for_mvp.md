# ADR-003 — Use Postgres + pgvector for MVP retrieval metadata

- **Decision type:** PD
- **Confidence:** Medium-high under A2/A3
- **Status:** Accepted
- **Spec anchor:** Section 6, line ~165.
- **Evidence:** pgvector supports exact nearest-neighbor search and HNSW/IVFFlat approximate indexes with explicit speed/recall tradeoffs [R21].
- **Why chosen:** Provenance/claim joins and vector retrieval can coexist with fewer operational systems.
- **Reversal triggers:** Corpus/vector count, p95 retrieval latency, multi-tenant isolation, filtered ANN recall, or write throughput miss SLO after tuning.
- **Consequences:** Relies on Postgres for both relational provenance and vector retrieval at MVP scale; a migration to a dedicated vector store is deferred until an SLO miss is measured, not anticipated.
