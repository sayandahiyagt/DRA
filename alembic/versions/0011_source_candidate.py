"""Wave 1b — SourceCandidate discovery table (sayandahiyagt/dra#79).

Introduces the ``source_candidate`` table (evolution spec §140/§141) to separate
*discovery* emissions (search-engine snippets, returned URLs) from
``EvidenceUnit``→``Claim`` research conclusions (§37/§38/§39).

A SourceCandidate is a discovery-level entity: it records a search result's
returned_url + snippet without reifying a researched claim.  Per §141 it "may
become a source after selection and capture" — i.e. only once a candidate is
selected and a full SourceCapture is performed does it graduate to a
``source_identity`` + ``source_capture`` row.

The candidate carries its own ``source_representation`` keyed by the snippet's
``returned_url`` (§156: exact canonical resource URL, not site origin), with
origin/publisher recorded on the representation so multiple pages on one site
no longer collapse into one identity (§36/§156 fix).

``source_candidate`` is a *canonical table* — adding it to the canonical object
set mechanically bumps ``knowledge_schema_version`` from v2 to v3, seeded here
with ``canonical_v3_signature()`` (src/dra/schema_version.py).

Chains linearly off ``0010_source_capture_model`` (the Wave 1a sentinel),
preserving the single-head invariant locked by ``test_alembic_single_head.py``.
"""

from __future__ import annotations

from alembic import op

revision = "0011_source_candidate"
down_revision = "0010_source_capture_model"
branch_labels = None
depends_on = None

_CANONICAL_SIGNATURE_V3 = (
    "dd099e1c80609ab226f43354ae2640167802990e55b4a3d136fe08dad220a554"
)


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS source_candidate (
            candidate_id        UUID PRIMARY KEY,
            bundle_id           UUID REFERENCES prov_bundle(id),
            representation_id   UUID REFERENCES source_representation(id),
            produced_by_activity UUID REFERENCES prov_activity(id),
            query               TEXT,
            purpose             TEXT,
            provider            TEXT,
            title               TEXT,
            returned_url        TEXT,
            snippet             TEXT,
            rank                INT,
            provider_score      REAL,
            discovered_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            state               evidence_state NOT NULL DEFAULT 'staged',
            metadata            JSONB DEFAULT '{}'::jsonb
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_source_candidate_bundle "
        "ON source_candidate (bundle_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_source_candidate_representation "
        "ON source_candidate (representation_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_source_candidate_returned_url "
        "ON source_candidate (returned_url)"
    )

    # Seed the v3 knowledge_schema_version row (idempotent upsert).  ON CONFLICT
    # DO UPDATE keeps the label/signature in sync if a v2 row already exists and
    # the DB is being migrated forward from the Wave 1a baseline.
    op.execute(
        f"""
        INSERT INTO knowledge_schema_version (version, label, canonical_signature)
        VALUES (3, 'v3', '{_CANONICAL_SIGNATURE_V3}')
        ON CONFLICT (version) DO UPDATE SET
            label = EXCLUDED.label,
            canonical_signature = EXCLUDED.canonical_signature
        """
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM knowledge_schema_version WHERE version = 3 "
        "AND label = 'v3';"
    )
    op.execute(
        "DROP INDEX IF EXISTS ix_source_candidate_returned_url, "
        "ix_source_candidate_representation, ix_source_candidate_bundle;"
    )
    op.execute("DROP TABLE IF EXISTS source_candidate CASCADE;")
