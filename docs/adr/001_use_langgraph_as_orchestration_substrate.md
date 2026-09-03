# ADR-001 — Use LangGraph as the initial orchestration substrate

- **Decision type:** PD
- **Confidence:** High
- **Status:** Accepted
- **Spec anchor:** Section 6, line ~150.
- **Evidence:** Checkpointers persist thread graph state; stores persist application-defined cross-thread data; documented use cases include interruption recovery, fault tolerance and human-in-the-loop [R1]. Subgraphs support modular/isolated graph execution [R2].
- **Alternatives:** DeerFlow 2, Deep Agents as top-level harness, Claude Managed Agents, Temporal-first custom runtime.
- **Why chosen:** Explicit graph semantics align with typed phases/gates, provider-neutral composition, and research branch fan-out/fan-in.
- **Reversal trigger:** Production evaluation shows unacceptable failure recovery, subgraph scaling, observability, or cross-service workflow management; or another runtime materially reduces complexity without compromising evidence state.
- **Consequences:** Locks orchestration to the LangGraph checkpoint model (control state only) while keeping the evidence/claim/topic layer separate; provider and store swaps remain possible because canonical evidence does not live in graph state (see ADR-002).
