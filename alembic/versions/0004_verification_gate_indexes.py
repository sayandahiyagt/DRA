"""§38.4 verification-gate supporting indexes (dra#19, ADR-021).

Indexes ONLY — no new canonical table or enum. ADR-021 records that the
§38.4 verification gate is expressible as pure queries over the existing
``0002`` schema (claim.verification_state JSONB, prov_derivation lineage
walks, derived_artifact staleness_policy / valid_from / valid_to,
source_identity.access_basis), so this migration adds only the two
indexes that accelerate the gate's hot predicates and leaves
``tests/test_schema_introspection.py`` untouched (mirroring the 0003
standalone-table precedent where ``proof_corpus`` was deliberately kept
out of the canonical introspection contract).

``prov_derivation`` lineage-walk indexes are ALREADY present in ``0002``
(``ix_prov_derivation_source``, ``ix_prov_derivation_derived``,
``ix_prov_derivation_activity``); no new index is added for the
derivative-masquerade recursive walk.

Like 0003, this migration uses raw SQL (``op.execute``) with ``IF NOT
EXISTS``/``IF EXISTS`` guards so it is idempotent across re-runs, and a
``downgrade()`` that reverses cleanly.
"""

from __future__ import annotations

from alembic import op

revision = "0004_verification_gate_indexes"
down_revision = "0003_storage_proof_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # GIN index on claim.verification_state: accelerates the gate's JSONB
    # predicates for unsupported-confidence detection and contradiction
    # visibility (claim.verification_state->'contradictions'[] and the
    # verification outcome recorded per claim).
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_claim_verification_state "
        "ON claim USING GIN (verification_state)"
    )

    # B-tree index on derived_artifact(state, valid_to): accelerates the
    # freshness/quarantine propagation scans — finding stale/rejected
    # artifacts and those whose valid_to window has elapsed, so the gate
    # can quarantine claims backed by a stale/rejected derived_artifact.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_derived_artifact_staleness "
        "ON derived_artifact (state, valid_to)"
    )

    # NOTE: prov_derivation lineage-walk indexes already exist in 0002:
    #   ix_prov_derivation_source   (source_entity_id)
    #   ix_prov_derivation_derived  (derived_entity_id)
    #   ix_prov_derivation_activity (activity_id)
    # The derivative-masquerade recursive CTE in test_provenance_traversal.py
    # already proves the walk reaches raw_capture; no new index is needed.


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_derived_artifact_staleness")
    op.execute("DROP INDEX IF EXISTS ix_claim_verification_state")
    # prov_derivation indexes are left intact (they are 0002 canonical).
