# ADR-006 — Use typed specialist worker modes, not an unconstrained swarm

- **Decision type:** ERI/PD
- **Confidence:** High
- **Status:** Accepted
- **Spec anchor:** Section 6, line ~185.
- **Evidence:** Existing multi-agent systems commonly isolate specialized contexts and tools; Deep Agents explicitly supports isolated subagents [R3].
- **Reason:** Repository, paper and DOM evidence have different validity rules; typed worker contracts prevent “one generic researcher” from treating all sources as text.
- **Reversal trigger:** A future harness adopts equivalent first-class provenance/claim/versioning contracts and materially reduces custom complexity — at which point generic worker contracts may be preferred over bespoke types.
- **Consequences:** More worker implementations to maintain, but stronger per-source validity guarantees and cleaner substitution per evidence type.
