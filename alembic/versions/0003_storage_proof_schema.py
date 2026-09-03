"""Storage proof schema (dra#15, §38.2).

Adds the standalone ``proof_corpus`` table — a deterministic, synthetic corpus
with dense embeddings loaded into a pgvector ``vector(384)`` column for the
§38.2 storage proof: exact-vs-HNSW recall/latency, tenant/project filtering,
and update/delete/staleness workloads.

This is intentionally a STANDALONE table (not a column on the canonical
``derived_artifact``): the dra#14 schema contract is immutable by
``test_schema_introspection``, and ``derived_artifact`` has a ``vector_embedding``
*enum tag* but no embedding column (see ONBOARD discovery). A standalone
table is reversible and keeps the canonical provenance model pristine.
"""

from __future__ import annotations

from alembic import op

revision = "0003_storage_proof_schema"
down_revision = "0002_evidence_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS proof_corpus (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id       TEXT NOT NULL,
            project_id      TEXT NOT NULL,
            doc_id          TEXT NOT NULL,
            chunk_seq       INT NOT NULL,
            content_hash    TEXT,
            text            TEXT,
            embedding       VECTOR(384),
            topic_id        TEXT,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            valid_from      TIMESTAMPTZ,
            valid_to        TIMESTAMPTZ,
            superseded_by   UUID REFERENCES proof_corpus(id),
            state           evidence_state NOT NULL DEFAULT 'canonical',
            staleness_policy JSONB DEFAULT '{}'::jsonb
        )
        """
    )

    # B-tree index for (tenant, project) filter lookups in tenant-scoped ANN.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_proof_corpus_tenant_project "
        "ON proof_corpus (tenant_id, project_id)"
    )

    # Idempotent upsert key: same chunk text must dedupe, not duplicate.
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_proof_corpus_uniq_chunk "
        "ON proof_corpus (content_hash, doc_id, chunk_seq)"
    )

    # HNSW index is NOT created here — it is built/dropped at runtime by the
    # proof harness (CREATE INDEX CONCURRENTLY cannot run inside a migration
    # transaction, and the proof toggles it repeatedly to compare exact-vs-HNSW).

    # A view that exposes only canonical, non-superseded rows — the retrieval
    # query surface the proof filters on (§14.2 / §21.1 state model).
    op.execute(
        """
        CREATE OR REPLACE VIEW proof_corpus_canonical AS
        SELECT * FROM proof_corpus
        WHERE state IN ('canonical', 'verified')
          AND superseded_by IS NULL
          AND (valid_to IS NULL OR valid_to > now())
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS proof_corpus_canonical")
    op.execute("DROP INDEX IF EXISTS ix_proof_corpus_uniq_chunk")
    op.execute("DROP INDEX IF EXISTS ix_proof_corpus_tenant_project")
    op.execute("DROP TABLE IF EXISTS proof_corpus CASCADE")
