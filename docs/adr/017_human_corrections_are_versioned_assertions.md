# ADR-017 — Human corrections are versioned assertions, not destructive edits to evidence

- **Decision type:** PD
- **Confidence:** Very high
- **Status:** Accepted
- **Spec anchor:** Section 6, line ~257; §21.3 human correction semantics.
- **Decision:** User/maintainer corrections create new assertion records linked to what they supersede or dispute. They may change product decisions immediately when they encode preferences/constraints, but they do not rewrite external source evidence.
- **Reason:** This preserves auditability and prevents human preference from masquerading as empirical fact.
- **Reversal trigger:** If the cost of versioned corrections (rather than overwrites) materially outweighs auditability for a deployment, only after a measured auditability impact is observed — auditability remains the default.
- **Consequences:** Corrections are typed (USER_PREFERENCE/USER_CONSTRAINT/USER_ASSERTION/MAINTAINER_ASSERTION/USER_CORRECTION/USER_ACCEPTED_RISK); prior assertions remain visible and contradictions are routed, never silently overwritten.
