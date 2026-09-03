"""Extend artifact_kind and activity_type enums for PaperInvestigator (§11.3, §16.2).

Adds ``grobid_tei`` and ``docling_document`` to ``artifact_kind`` (required by
the §16.1 dual-parser pipeline — staging these without this migration hits a
Postgres ``23514`` enum violation, per the dra#23 ONBOARD discovery), and
``visual_review`` to ``activity_type`` (for §16.2 critical-content provenance
attribution).

PostgreSQL does not support ``ALTER TYPE ... ADD VALUE IF NOT EXISTS``, so
each value is added inside a guarded ``DO`` block that checks ``pg_enum``
first (idempotent across re-runs and across the pre-existing double-head
reconciliation, ADR-007).

Down Revision: chains off ``0005_implementation_entity_state`` (the dra#23 head
at branch HEAD). The pre-existing double-head (``0006_model_routing_schema``
branching off the ``0004_implementation_entity_state`` stub) is NOT introduced
here — see §8 of the PLAN for reconciliation notes at merge to ``origin/main``.

NOTE: ``alembic_version`` is ``varchar(32)``, so the revision id is truncated
from the plan's ``0007_paper_investigator_extensions`` (34 chars) to
``0007_paper_investigator`` (23 chars); the migration docstring retains the
fuller semantic name for readability.
"""

from __future__ import annotations

from alembic import op

revision = "0007_paper_investigator"
down_revision = "0005_implementation_entity_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _add_enum_value("artifact_kind", "grobid_tei")
    _add_enum_value("artifact_kind", "docling_document")
    _add_enum_value("activity_type", "visual_review")


def downgrade() -> None:
    # PostgreSQL does not support dropping enum values in-place within the same
    # transaction or via ALTER TYPE. A full type rebuild would be required
    # (CREATE TYPE ... AS ENUM, UPDATE, DROP, RENAME) — deliberately omitted as
    # an anti-pattern for production evidence-schema enums. This migration is
    # effectively append-only, consistent with the repo's enum-adding precedent.
    pass


def _add_enum_value(enum_name: str, value: str) -> None:
    """Add *value* to the PostgreSQL enum *enum_name* if absent (idempotent)."""
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_type t
                JOIN pg_enum e ON t.oid = e.enumtypid
                WHERE t.typname = '{enum_name}'
                  AND e.enumlabel = '{value}'
            ) THEN
                ALTER TYPE {enum_name} ADD VALUE '{value}';
            END IF;
        END $$;
        """
    )
