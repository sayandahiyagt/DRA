# ADR-012 — Deep Agents/DeerFlow are worker-harness candidates, not canonical data models

- **Decision type:** PD
- **Confidence:** High
- **Status:** Accepted
- **Spec anchor:** Section 6, line ~222.
- **Evidence:** Both provide long-horizon/context/sandbox/subagent primitives [R3][R4]; neither exposes the product-specific claim/evidence/decision semantics this system requires.
- **Reversal trigger:** A future harness adopts equivalent first-class provenance/claim/versioning contracts and materially reduces custom complexity.
- **Consequences:** Custom evidence/claim/topic schema remains canonical; worker harnesses are substitutable without migrating canonical evidence, but the product bears the cost of owning that schema.
