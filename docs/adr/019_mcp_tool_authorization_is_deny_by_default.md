# ADR-019 — MCP/tool authorization is deny-by-default and capability-scoped

- **Decision type:** Evidence-driven PD
- **Confidence:** Very high
- **Status:** Accepted
- **Spec anchor:** Section 6, line ~269; §22.4 authenticated tools and MCP.
- **Evidence:** MCP authorization guidance requires resource/audience binding, secure token handling, and forbids token passthrough; the specification also treats tools as arbitrary code-execution capability surfaces requiring consent and access controls [R24][R31].
- **Decision:** Tool grants are per role/run, read-only by default, audience-bound, non-transferable between MCP servers, and separately logged. Browser/retrieval content cannot elevate tool scope.
- **Reversal trigger:** A required integration cannot satisfy deny-by-default/capability scoping while remaining usable, and no capability-isolating wrapper is available — at which point the policy engine (not this decision) is the integration point.
- **Consequences:** Write/side-effect tools require a separate policy path; all tool invocations are auditable; credentials never travel in retrieved content.
