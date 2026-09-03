# ADR-008 — Model routing is evaluation-driven, not brand/role hardcoded

- **Decision type:** PD
- **Confidence:** Very high
- **Status:** Accepted
- **Spec anchor:** Section 6, line ~197.
- **Evidence:** Current providers expose very different price tiers [R14][R15][R16]; Anthropic’s current advisor guidance explicitly says model pairing depends on task and consultation rate [R17].
- **Decision:** Maintain role candidate pools and benchmark them. No permanent “Opus for X” rule.
- **Reversal trigger:** Provider pricing/capabilities converge to a single dominant option for a role, or the benchmark harness becomes prohibitively expensive relative to research value.
- **Consequences:** Avoids provider lock-in by design; requires an ongoing evaluation harness and explicit routing policy rather than a static config map.
