# ADR-002 — Separate orchestration state from canonical research evidence

- **Decision type:** ERI/PD
- **Confidence:** Very high
- **Status:** Accepted
- **Spec anchor:** Section 6, line ~158.
- **Evidence:** LangGraph itself distinguishes thread graph state from application-defined durable stores [R1]. Deep Agents documentation similarly distinguishes context/file backends and emphasizes offloading large outputs rather than carrying them in model context [R3].
- **Decision:** Graph checkpoints store control state and stable IDs, not the full research corpus. Postgres/object storage hold canonical evidence.
- **Reversal trigger:** None expected; implementation may change stores, but the separation principle between control-plane checkpoints and canonical evidence is retained.
- **Consequences:** Control plane stays small and resumable; evidence survives harness migrations; large blobs are never carried in checkpoints, bounding checkpoint size and recovery cost.
