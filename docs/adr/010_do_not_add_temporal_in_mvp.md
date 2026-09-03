# ADR-010 — Do not add Temporal in MVP

- **Decision type:** PD
- **Confidence:** Medium-high
- **Status:** Accepted
- **Spec anchor:** Section 6, line ~209.
- **Evidence:** LangGraph already provides checkpoint/persistence/fault-recovery semantics [R1]; Temporal provides durable event-history-based workflow recovery [R20].
- **Reason:** Two overlapping workflow state machines increase operational complexity before a demonstrated need.
- **Adoption triggers:** multi-day/month SLAs, durable timers/schedules, cross-service orchestration, or distributed workflow guarantees beyond the chosen LangGraph deployment.
- **Reversal trigger:** A measured need arises for any of the adoption triggers (multi-day SLAs, durable timers, cross-service orchestration) AND LangGraph is shown unable to satisfy it within operational tolerance.
- **Consequences:** MVP runs on LangGraph persistence only; adding Temporal later is a layered change because control and evidence state are separated (ADR-002).
