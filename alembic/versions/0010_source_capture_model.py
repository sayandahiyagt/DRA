"""Wave 1a — Source/capture storage model (sayandahiyagt/dra#78, Wave 1 part 1/3).

Decouples content identity from capture provenance (docs/evolution spec.md
§42-44):

- ``content_blob``      — pure content identity (sha256 PK; size/mime/storage_uri/
                          encryption_metadata).  Durable bytes live behind the
  BlobStore abstraction (``dra.storage``); ``storage_uri`` is the provider-
  specific durable location.
- ``source_representation`` — a retriable representation of a source (canonical
  URL + origin/publisher + HTTP/access metadata), linked to its ContentBlob.
- ``source_capture``     — an acquisition *event*: links a source_identity, a
  representation, and a content_blob, with captured_at / final_url /
  redirect_chain / method / provider / HTTP metadata.  Many independent captures
  may reference one identical blob (§43/§155), fixing the §42 provenance collapse
  where raw_capture.content_hash-as-PK erased separate source occurrences.

``source_identity`` gains a ``normalized_key`` column (canonical key over
(kind, locator, version) with a partial UNIQUE index) so acquisition can use a
concurrency-safe ``ON CONFLICT`` get-or-create upsert (§41/§159) instead of the
racy UUID-per-insert path.

``derived_artifact.source_capture_hash`` is repointed from ``raw_capture`` to
``content_blob`` — the content identity is now the blob hash, not the deprecated
raw_capture PK.

``raw_capture`` is left in place (deprecated, not dropped) so existing readers
and the staged->canonical publication machinery continue to function; the new
tables are the authoritative Wave 1a substrate for Parts 2 and 3.

The v2 ``knowledge_schema_version`` row is seeded with the canonical signature
of the v2 object set (V1_EXPECTED_TABLES + the three new tables).
"""

from __future__ import annotations

from alembic import op

revision = "0010_source_capture_model"
down_revision = "0009_knowledge_schema_baseline"
branch_labels = None
depends_on = None

_CANONICAL_SIGNATURE_V2 = (
    "60123f40db96eed5780c84289494c0286dd8d513692f1ce56e1d6a52eebed5ac"
)


def upgrade() -> None:
    # --- content_blob (pure content identity) --------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS content_blob (
            hash              TEXT PRIMARY KEY,
            size              BIGINT,
            mime_type         TEXT,
            storage_uri       TEXT,
            encryption_metadata JSONB,
            created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )

    # --- source_representation (retrievable representation) ------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS source_representation (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            content_blob_hash TEXT REFERENCES content_blob(hash),
            canonical_url   TEXT,
            origin          TEXT,
            publisher       TEXT,
            http_status     INT,
            http_headers    JSONB,
            access_metadata JSONB,
            retrieved_at    TIMESTAMPTZ
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_source_representation_content_blob "
        "ON source_representation (content_blob_hash);"
    )

    # --- source_capture (acquisition event) -----------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS source_capture (
            capture_id          UUID PRIMARY KEY,
            source_identity_id  UUID REFERENCES source_identity(id),
            representation_id   UUID REFERENCES source_representation(id),
            content_blob_hash   TEXT REFERENCES content_blob(hash),
            kind                TEXT,
            state               evidence_state NOT NULL DEFAULT 'staged',
            captured_at         TIMESTAMPTZ,
            final_url           TEXT,
            redirect_chain      JSONB,
            method              TEXT,
            provider            TEXT,
            http_metadata       JSONB,
            size_bytes          BIGINT,
            mime_type           TEXT,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            metadata            JSONB
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_source_capture_source "
        "ON source_capture (source_identity_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_source_capture_representation "
        "ON source_capture (representation_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_source_capture_content_blob "
        "ON source_capture (content_blob_hash);"
    )

    # --- source_identity: add normalized_key for concurrency-safe get-or-create
    #     (§41/§159).  A plain UNIQUE constraint (not a partial index) so that
    #     ON CONFLICT (normalized_key) inference works in stage_source_identity.
    #     PostgreSQL treats NULL as distinct in UNIQUE, so rows with NULL
    #     normalized_key (pre-0010 inserts) are allowed unbounded.
    op.execute(
        "ALTER TABLE source_identity ADD COLUMN IF NOT EXISTS "
        "normalized_key TEXT;"
    )
    op.execute(
        "ALTER TABLE source_identity ADD CONSTRAINT "
        "source_identity_normalized_key_key UNIQUE (normalized_key);"
    )

    # --- Repoint derived_artifact.source_capture_hash FK: raw_capture -> content_blob
    #     (§42/§43 content-capture decoupling).
    op.execute(
        "ALTER TABLE derived_artifact DROP CONSTRAINT IF EXISTS "
        "derived_artifact_source_capture_hash_fkey;"
    )
    op.execute(
        "ALTER TABLE derived_artifact ADD FOREIGN KEY (source_capture_hash) "
        "REFERENCES content_blob (hash);"
    )

    # --- Seed the v2 knowledge_schema_version row (idempotent).
    op.execute(
        f"""
        INSERT INTO knowledge_schema_version (version, label, canonical_signature)
        VALUES (2, 'v2', '{_CANONICAL_SIGNATURE_V2}')
        ON CONFLICT (version) DO NOTHING
        """
    )


def downgrade() -> None:
    # v1 baseline is append-only; downgrade is for local test DBs only.
    op.execute(
        "DELETE FROM knowledge_schema_version WHERE version = 2 "
        "AND label = 'v2';"
    )
    op.execute(
        "ALTER TABLE derived_artifact DROP CONSTRAINT IF EXISTS "
        "derived_artifact_source_capture_hash_fkey;"
    )
    op.execute(
        "ALTER TABLE derived_artifact ADD FOREIGN KEY (source_capture_hash) "
        "REFERENCES raw_capture (content_hash);"
    )
    op.execute(
        "DROP INDEX IF EXISTS ix_source_identity_normalized_key;"
    )
    op.execute(
        "ALTER TABLE source_identity DROP CONSTRAINT IF EXISTS "
        "source_identity_normalized_key_key;"
    )
    op.execute("ALTER TABLE source_identity DROP COLUMN IF EXISTS normalized_key;")
    op.execute(
        "DROP INDEX IF EXISTS ix_source_capture_content_blob, "
        "ix_source_capture_representation, ix_source_capture_source;"
    )
    op.execute("DROP TABLE IF EXISTS source_capture CASCADE;")
    op.execute("DROP TABLE IF EXISTS source_representation CASCADE;")
    op.execute("DROP TABLE IF EXISTS content_blob CASCADE;")
