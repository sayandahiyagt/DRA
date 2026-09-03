"""Add ``state`` column to ``implementation_entity`` (dra#23, §13.4).

The ``#14`` schema (``0002_evidence_schema``) defines ``implementation_entity``
WITHOUT a ``state`` column, unlike ``raw_capture`` / ``derived_artifact`` /
``evidence_unit`` / ``claim`` which all carry ``state evidence_state NOT NULL
DEFAULT 'staged'``. The ``#23`` evidence-emission contract requires
``implementation_entity`` to flow through the same staged->canonical atomic
commit as those four tables (ADR-013), so it needs its own ``state`` column to
be mirrored by ``publish._mirror_state_canonical``.

This migration:
- adds ``state evidence_state NOT NULL DEFAULT 'staged'`` to
  ``implementation_entity`` (existing rows are back-filled with ``'staged'``;
  the default ensures the column is never null);
- adds ``ix_implementation_entity_state`` on ``(state)`` for fast lookup of
  staged/canonical rows during publication, mirroring the indexes on the other
  domain tables.

``evidence_state`` is created by ``0002``; on a fresh upgrade chain this
migration runs after ``0004`` (verification-gate indexes) and the enum
already exists. The ``ALTER TABLE`` is guarded with a column-existence check
inside a ``DO`` block so re-runs are safe.
"""

from __future__ import annotations

from alembic import op

revision = "0005_implementation_entity_state"
down_revision = "0004_verification_gate_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'implementation_entity'
                  AND column_name = 'state'
            ) THEN
                ALTER TABLE implementation_entity
                ADD COLUMN state evidence_state NOT NULL DEFAULT 'staged';
            END IF;
        END
        $$;
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_implementation_entity_state "
        "ON implementation_entity (state);"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_implementation_entity_state;")
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'implementation_entity'
                  AND column_name = 'state'
            ) THEN
                ALTER TABLE implementation_entity DROP COLUMN state;
            END IF;
        END
        $$;
        """
    )
