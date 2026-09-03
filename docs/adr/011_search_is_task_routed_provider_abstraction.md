# ADR-011 — Search is a task-routed provider abstraction

- **Decision type:** PD
- **Confidence:** High
- **Status:** Accepted
- **Spec anchor:** Section 6, line ~216.
- **Evidence:** Exa, Perplexity Search, Tavily, and Firecrawl expose materially different search/extraction/crawl modes [R10][R11][R12][R13].
- **Decision:** No universal primary provider. Benchmark and route.
- **Reversal trigger:** A single search provider demonstrably dominates on all task types after benchmarking, making per-provider routing overhead unjustified.
- **Consequences:** Requires a provider abstraction and a routing/benchmark harness; avoids premature commit to one vendor’s retrieval characteristics.
