# ADR-004 — Use immutable raw captures and versioned derived artifacts

- **Decision type:** ERI/PD
- **Confidence:** High
- **Status:** Accepted
- **Spec anchor:** Section 6, line ~172; §3 A4; §21.1.
- **Reason:** Reproducibility and invalidation require preserving what was actually observed. Derived parser/model outputs must be regenerable when tools change.
- **Decision:** Raw captures are content-addressed and immutable. Parsed artifacts/evidence/claims are versioned, supersedable and staleness-aware.
- **Reversal trigger:** Derived-artifact versioning invalidates on tool/model change — when the parsing or generation model/tooling changes, previously derived artifacts are regenerated and the prior version is marked `superseded` (never overwritten). If tool-version invalidation proves impractical to track at scale, this decision is revisited (a signal that the versioning granularity is too fine or too coarse).
- **Consequences:** Content-addressed raw captures (PK = sha256) give exact de-duplication and immutability; versioned derived artifacts enable staleness propagation (§21.4) but require a supersession policy on every tool/version boundary.
