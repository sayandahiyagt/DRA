# ADR-009 — Use complementary scholarly parsers plus visual verification

- **Decision type:** PD
- **Confidence:** High
- **Status:** Accepted
- **Spec anchor:** Section 6, line ~203.
- **Evidence:** GROBID specializes in scholarly document structure/reference extraction [R19]; Docling exposes structured layout, formula/picture/table and provenance features [R18].
- **Decision:** Use both selectively; visually inspect implementation-critical equations/tables/figures when parser confidence is low or outputs disagree.
- **Reversal trigger:** Output disagreement or low parser confidence on critical equations/tables/figures reaches a measured failure threshold, or a single parser consistently matches both tools’ coverage.
- **Consequences:** Higher per-document inspection cost; but prevents parser-specific hallucination of numeric/structural content that would poison downstream implementation claims.
