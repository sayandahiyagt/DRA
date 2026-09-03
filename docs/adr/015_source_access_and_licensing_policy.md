# ADR-015 — Source-access and licensing policy is a mandatory precondition, not an afterthought

- **Decision type:** PD
- **Confidence:** High
- **Status:** Accepted
- **Spec anchor:** Section 6, line ~243; §22 source access, compliance & acquisition policy.
- **Evidence:** RFC 9309 standardizes robots exclusion for automated crawlers while explicitly noting it is not authorization [R25]. SPDX provides standardized machine-readable license identifiers/expressions [R26].
- **Decision:** Every source acquisition path records access basis, robots/crawl policy where applicable, license metadata when relevant, authentication scope, and whether artifact redistribution is allowed.
- **Reversal trigger:** Deployment-specific policy may be stricter, never silently looser. (If the deployment is authorized to be stricter and the policy engine cannot express the needed constraints, the policy model — not this decision — is revisited.)
- **Consequences:** Source records carry access_basis/license_spdx/crawl_allowed/auth_scope/redist_allowed fields (see `source_identity` in the schema); unauthorized sources are rejected at acquisition rather than downstreamed.
