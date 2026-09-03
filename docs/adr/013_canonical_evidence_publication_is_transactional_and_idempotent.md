# ADR-013 — Canonical evidence publication is transactional and idempotent

- **Decision type:** PD
- **Confidence:** Very high
- **Status:** Accepted
- **Spec anchor:** Section 6, line ~229; §21.1 staged->canonical; §23 commit steps 8-10.
- **Problem addressed:** Retried workers, partial failures, duplicate fetches, parser crashes, and concurrent branches can otherwise create orphaned artifacts or conflicting evidence rows.
- **Decision:** A branch writes to a staging scope first. Publication requires source identity, raw artifact hash, derivation metadata, evidence locators, task/run IDs, and schema validation. The canonical commit is idempotent on stable source/artifact/content identities. Partial commits never count toward branch completion.
- **Reversal trigger:** None expected; the database implementation may change, but atomic staged->canonical publication semantics remain required.
- **Consequences:** Staged rows must be garbage-collected/replayed if publication fails (branch remains STAGED/COMMIT_FAILED); concurrent workers cannot create duplicate canonical evidence because stable content/source identities are the conflict key.
