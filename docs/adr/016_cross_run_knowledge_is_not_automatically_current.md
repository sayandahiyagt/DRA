# ADR-016 — Cross-run knowledge is a discovery accelerator, not automatically current evidence

- **Decision type:** PD
- **Confidence:** Very high
- **Status:** Accepted
- **Spec anchor:** Section 6, line ~250; §21.4 cross-run reuse.
- **Reason:** Reusing old conclusions without carrying source/version/freshness creates silent staleness and circular corroboration.
- **Decision:** Prior claims/topics may be retrieved as `PRIOR_KNOWLEDGE`, but new runs must distinguish them from `CURRENT_RUN_EVIDENCE`. High-impact/current claims require freshness checks or source revalidation.
- **Reversal trigger:** None expected; cache policy can become more permissive only for explicitly immutable/version-pinned sources (e.g. a pinned Git SHA whose hash is verified). The freshness/distinction mechanism is retained regardless.
- **Consequences:** Evidence artifacts carry a run_id/bundle lineage and staleness_policy; prior summaries are never counted as independent corroboration.
