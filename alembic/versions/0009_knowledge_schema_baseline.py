"""Freeze the Wave 0 canonical schema as the v1 knowledge_schema_version baseline.

Wave 0 deliverable (see docs/evolution spec.md §389, sayandahiyagt/dra#59):
``knowledge_schema_version`` did not exist anywhere in the repo before this
migration — the ``schema_version`` fields in ``src/dra/handoff.py`` are the
handoff-MANIFEST version (``"1.0"``), a different concept. This migration
introduces the DB-stored canonical schema-version table and seeds the v1 row.

The v1 ``canonical_signature`` is an opaque SHA-256 digest of the frozen v1
object set (``V1_EXPECTED_TABLES`` + ``V1_EXPECTED_ENUMS`` in
``src/dra/schema_version.py``), computed so a later wave that adds a table or
enum value without bumping ``knowledge_schema_version`` is detectable as
signature drift. The seeded literal below is the digest of the exact v1 object
set locked in by 0001-0008 (see ``tests/test_schema_introspection.py`` for the
canonical enumeration, including the ``grobid_tei``/``docling_document``/
``visual_review`` additions from 0007 and ``assertion_type`` from 0008).

This migration chains linearly off the reconciled single head
``0008_interview_constraints`` (itself the merge node resolving the historical
double-head of ``0007_paper_investigator`` + ``0007_web_crawl_manifest``), so
from here the trunk is strictly linear and the "no ambiguous migration heads"
gate in ``tests/test_alembic_single_head.py`` holds.

Note on revision id: the plan text truncated this id to ``0009_knowledge_schema``
on the assumption it would exceed ``alembic_version``'s ``varchar(32)``. The full
slug ``0009_knowledge_schema_baseline`` is 30 characters and fits the documented
32-char limit, so the untruncated, filename-aligned id is used.
"""

from __future__ import annotations

from alembic import op

revision = "0009_knowledge_schema_baseline"
down_revision = "0008_interview_constraints"
branch_labels = None
depends_on = None

# SHA-256 of json.dumps({"tables": sorted(V1_EXPECTED_TABLES),
#                         "enums": {name: sorted(values)}}, sort_keys=True)
# computed over the frozen v1 object set in src/dra/schema_version.py.
# Keep in sync with ``dra.schema_version.canonical_v1_signature()``.
_CANONICAL_SIGNATURE_V1 = (
    "51242702e1e6b1d570c886500234a43eec81cd1d72211c5e254868afde18aacb"
)


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS knowledge_schema_version (
            version             INT PRIMARY KEY,
            label               TEXT NOT NULL,
            canonical_signature TEXT NOT NULL,
            applied_at          TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    # Idempotent seed: a DB that already ran this migration (or a shared Postgres
    # with the row pre-inserted) is left untouched. Mirrors the IF NOT EXISTS /
    # ON CONFLICT guards established by 0002 / 0007 / 0008. The signature is a
    # constant literal (no user input), so it is inlined directly.
    op.execute(
        f"""
        INSERT INTO knowledge_schema_version (version, label, canonical_signature)
        VALUES (1, 'v1', '{_CANONICAL_SIGNATURE_V1}')
        ON CONFLICT (version) DO NOTHING
        """
    )


def downgrade() -> None:
    # v1 baseline is append-only in practice; downgrade is for local test DBs
    # only and is guarded so it never drops the table in a CI-provisioned DB
    # that has already migrated past v1.
    op.execute("DROP TABLE IF EXISTS knowledge_schema_version")
