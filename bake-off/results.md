# §38.1 Control-Plane Bake-Off — Results Ledger

Mission: `sayandahiyagt/dra#37`  Spec: `§38.1`

## Decision rule (§38.1/§42)
> LangGraph remains the control-plane substrate unless an alternative MATERIALLY reduces measured cost (>=20% lower composite) AND does not force canonical evidence into agent-internal state (in_state_findings==0). Any variant with in_state_findings>0 is DISQUALIFIED regardless of raw score.

## Measurement table (8 dimensions, real DB-backed numbers)

| Variant | Effort(LOC) | Checkpoint rows | Parallel workers | in-state findings | Cancel rollback→retry | Context growth (max bytes) | Ops deps | Verify |
|---|---|---|---|---|---|---|---|---|
| A_langgraph | 252 | 12 | 3 | 0 | 0->12 | 1650 | 0 | PASS |
| B_deep_agents | 89 | 7 | 3 | 0 | 0->12 | 1043 | 2 | PASS |
| C_deerflow | 145 | 17 | 3 | 1 | 0->12 | 1651 | 0 | PASS |

## §38.1/§42 Recommendation

**Chosen: A_langgraph** — LangGraph REMAINS the control-plane substrate.

Composite scores (lower is better; effort weighted 2×, ops 1×, context growth + 999 penalty for in-state findings):
- `A_langgraph`: 509
- `B_deep_agents`: 843
- `C_deerflow`: 2004

- B_deep_agents: composite 843 (65.6% vs A); in_state=0.
- C_deerflow: composite 2004 (293.7% vs A); in_state=1.

## Disqualifications
- **C_deerflow** DISQUALIFIED: findings forced into agent-internal state (bypass dra.publish). Native state: {'thread_data': 1, 'artifacts': 0, 'delegations': 0, 'skill_context': 0, 'channels_present': ['__pregel_tasks', '__start__', 'messages', 'thread_data'], 'in_state_findings': 1}.

## Evidence note (dra.publish canonical-evidence commit contract)

All findings committed via dra.publish/publish_bundle (bundle_id receipt per variant). A & B: in_state_findings=0 (LangGraph control state / DeepAgents files channel empty — evidence stays on dra.publish). C: DeerFlow native ThreadState materialises tool results into agent-internal thread_data (ThreadDataMiddleware, sandbox=True) — findings NOT held exclusively on dra.publish -> DISQUALIFIED.

Per-variant commit receipts (bundle UUIDs):
- `A_langgraph`: commit bundle(s) ['498d71d1-0edd-47db-8338-da3f4eb99f39']
- `B_deep_agents`: commit bundle(s) ['29cf1879-df68-411b-88cd-f975021f6f49']
- `C_deerflow`: commit bundle(s) ['f13f5029-7847-4ef7-9626-a548388bf40c']
