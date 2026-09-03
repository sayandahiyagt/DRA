# ADR-007 — Add a dedicated Source & Claim Verification Gate

- **Decision type:** Evidence-driven PD
- **Confidence:** Very high
- **Status:** Accepted
- **Spec anchor:** Section 6, line ~191; §21.1 (VERIFIED), §20.
- **Evidence:** Deep-research citation studies show citation presence/relevance is insufficient for factual support, and misleading evidence can strongly alter results [R6][R7].
- **Decision:** Verification is a separate lifecycle stage with deterministic checks plus calibrated LLM judges where needed.
- **Reversal trigger:** Calibration study shows the chosen LLM judge thresholds produce unacceptable false-positive or false-negative rates for high-impact claims, or deterministic checks cannot cover a source type that turns out to dominate.
- **Consequences:** Adds latency and cost to every high-impact claim; but prevents citation-presence from being mistaken for entailment (§4 non-functional equation).
