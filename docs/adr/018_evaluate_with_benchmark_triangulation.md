# ADR-018 — Evaluate with benchmark triangulation plus a product-specific downstream handoff benchmark

- **Decision type:** ERI/PD
- **Confidence:** High
- **Status:** Accepted
- **Spec anchor:** Section 6, line ~263.
- **Evidence:** BrowseComp measures difficult web discovery/reasoning [R27]; RepoProbe targets open-ended architecture-aware repository comprehension [R28]; SWE-bench evaluates solving real GitHub issues [R29], with SWE-bench-Live adding continuously updated multi-language/multi-OS tasks [R30].
- **Decision:** Use these as component stress tests, but make downstream coding success from the generated handoff the decisive product metric because no public benchmark exactly matches this system.
- **Reversal trigger:** A new public benchmark that exactly matches the downstream coding-from-handoff use case is published and demonstrates superior signal, or downstream success stops correlating with the chosen component stress tests.
- **Consequences:** Evaluation is split between component stress tests and a product-specific handoff benchmark; effort invested in a synthetic product metric that later proves miscalibrated can be retired.
