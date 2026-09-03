# ADR-014 — Use provenance semantics compatible with an entity/activity/agent derivation model

- **Decision type:** ERI/PD
- **Confidence:** High
- **Status:** Accepted
- **Spec anchor:** Section 6, line ~236; §21.2 provenance graph; [R23] W3C PROV-DM.
- **Evidence:** W3C PROV defines provenance around entities, activities, agents, derivations, responsibility, and bundles, specifically to support quality/trust assessment and interchange [R23].
- **Decision:** Internally represent equivalent semantics for every derived artifact, evidence unit, claim, summary, and handoff statement. Full PROV-O/PROV-N export is optional at MVP.
- **Why not require full W3C serialization:** It would add implementation surface without proving product value; semantic compatibility preserves future exportability.
- **Reversal trigger:** The product needs to interoperate with external PROV consumers in a way that the simplified relational model cannot satisfy, or a downstream consumer demonstrates concrete value loss from the lossy internal representation.
- **Consequences:** The canonical schema carries first-class entity/activity/agent/bundle tables (see `alembic/versions/0002_evidence_schema.py`, `src/dra/publish.py`) rather than opaque provenance text; every handoff item traces backward to exact evidence.
