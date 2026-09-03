# Architecture Decision Records (ADRs)

This directory holds the architecture decision records for the Deep Research
Agent (DRA), ported verbatim from **Section 6** of the canonical design spec
`docs/practical_deep_research_system_design_spec_v3_final_audited.md`.

## Format and lifecycle

Each ADR file (`0XX_<slug>.md`) follows the template below and records a
single decision. Decisions are immutable historical records: to *change* a
decision, record a new ADR that reverses or supersedes the prior one — never
edit the past.

## ADR template

```
# ADR-XXX — <Title>

- **Decision type:** <one of: PD | ERI | ERI/PD | Evidence-driven PD | Evidence-driven PD>
- **Confidence:** <High | Medium-high | Very high | ...>
- **Status:** Accepted / Proposed / Superseded-by-ADR-XXX
- **Evidence:** <the evidence base; spec anchors [R..] / citations>
- **Decision:** <what was decided>
- **Why chosen / Alternatives:** <why this over the alternatives>
- **Reversal trigger:** <the concrete condition under which this decision
  should be reversed. This field is MANDATORY for every ADR — a decision
  without an explicit reversal trigger cannot be confidently retired.>
- **Consequences:** <positive and negative implications of living with it>
```

## Numbering

| Range | Scope |
|---|---|
| ADR-001 – ADR-019 | Decisions carried into this implementation (Section 6 of the spec). |
| ADR-020+ | Decisions made during implementation that were not in the original spec. |

## Reversal semantics

A reversal trigger is a falsifiable condition (a measured SLO miss, a new
evidence threshold, a policy change). When triggered, a new ADR is written that
either reverses the original or marks it `Superseded-by-ADR-XXX`. Reversals
are reflected in the implementation by the smallest sound change to the
code/data model (see `src/dra/publish.py` for the transactional canonical
commit that makes reversions auditable rather than destructive).

> **ADR-004 reversal trigger:** derived-artifact versioning invalidates on
> tool/model change (parsed artifacts are regenerated and the prior version
> is marked `superseded`, not overwritten).
>
> **ADR-013 reversal trigger:** none expected; the *database* implementation
> may change, but atomic staged→canonical publication semantics remain
> required.
>
> **ADR-015 reversal trigger:** deployment-specific policy may be stricter,
> never silently looser.

## Spec anchors

- ADR-004: §3 A4, §6 line ~172, §21.1 (content-addressed raw captures +
  versioned derived artifacts).
- ADR-013: §6 line ~229, §21.1 staged→canonical, §23 commit protocol steps 8–10.
- ADR-014: §6 line ~236, §21.2 provenance graph (entity/activity/agent/
  derivation/bundle), [R23] W3C PROV-DM.
