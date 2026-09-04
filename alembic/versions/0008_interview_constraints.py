"""Add versioned user-provided constraint model (§1.3.3, ADR-017, dra#44).

This is a MERGE migration: the repository ships a bifurcated Alembic history with
two heads and no existing merge node:

  - ``0007_paper_investigator`` (down_revision = ``0005_implementation_entity_state``)
  - ``0007_web_crawl_manifest``  (down_revision = ``0006_model_routing_schema``)

````0008_progressive_interview_constraints`` (the mission's literal migration name,
38 chars) exceeds ``alembic_version``'s ``varchar(32)`` — repo-documented in
``0007_paper_investigator_extensions.py:19-22`` and corroborated by the
exactly-32-char ``0004`` revisions — so the revision id is shortened to
``0008_interview_constraints`` (26 chars; the fuller semantic name is retained in
this docstring for readability).  The shortened slug is the canonical revision
id that Parts 2/3 will reference.

Adds:

1. ``assertion_type`` enum — human/maintainer corrections are versioned
   assertions, never destructive overwrites of external evidence (ADR-017):

       USER_PREFERENCE, USER_CONSTRAINT, USER_ASSERTION,
       MAINTAINER_ASSERTION, USER_CORRECTION, USER_ACCEPTED_RISK

2. ``user_assertion`` table — canonical, versioned storage for
   user/maintainer assertions so they are never rewritten into external
   evidence or overwritten.  Stands ALONE (no ``prov_entity`` row; NOT added to
   the ``entity_kind`` enum) so the dra#14 schema contract asserted by
   ``test_schema_introspection`` stays byte-stable.  Instead it references the
   provenance graph via ``produced_by_activity`` (-> prov_activity) and is
   staged+published atomically through the ADR-013 ``publish_bundle``
   transaction via a bundle-scoped ``staged -> canonical`` mirror
   (``_STANDALONE_STATE_TABLES`` in ``src/dra/publish.py``).
"""

from __future__ import annotations

from alembic import op

revision = "0008_interview_constraints"
down_revision = ("0007_paper_investigator", "0007_web_crawl_manifest")
branch_labels = None
depends_on = None


def upgrade() -> None:
    _create_enum(
        "assertion_type",
        [
            "USER_PREFERENCE",
            "USER_CONSTRAINT",
            "USER_ASSERTION",
            "MAINTAINER_ASSERTION",
            "USER_CORRECTION",
            "USER_ACCEPTED_RISK",
        ],
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS user_assertion (
            id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            bundle_id             UUID NOT NULL REFERENCES prov_bundle(id),
            run_id                TEXT,
            task_id               TEXT,
            question              TEXT NOT NULL,
            value                 JSONB NOT NULL DEFAULT '{}'::jsonb,
            assertion_type        assertion_type NOT NULL,
            superseded_by         UUID REFERENCES user_assertion(id),
            produced_by_activity  UUID NOT NULL REFERENCES prov_activity(id),
            disputed_claim_id     UUID REFERENCES claim(id),
            disputed_decision_id  UUID REFERENCES decision(id),
            disputed_source_id    UUID REFERENCES source_identity(id),
            state                 evidence_state NOT NULL DEFAULT 'staged',
            created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
            metadata              JSONB DEFAULT '{}'::jsonb
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_user_assertion_bundle "
        "ON user_assertion (bundle_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_user_assertion_activity "
        "ON user_assertion (produced_by_activity)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_user_assertion_superseded_by "
        "ON user_assertion (superseded_by)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_user_assertion_target "
        "ON user_assertion (disputed_claim_id, disputed_decision_id, disputed_source_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_user_assertion_target")
    op.execute("DROP INDEX IF EXISTS ix_user_assertion_superseded_by")
    op.execute("DROP INDEX IF EXISTS ix_user_assertion_activity")
    op.execute("DROP INDEX IF EXISTS ix_user_assertion_bundle")
    op.execute("DROP TABLE IF EXISTS user_assertion")
    op.execute("DROP TYPE IF EXISTS assertion_type CASCADE")


def _create_enum(name: str, values: list[str]) -> None:
    """Create a PostgreSQL enum type idempotently.

    PostgreSQL does not support ``CREATE TYPE IF NOT EXISTS ... AS ENUM``,
    so each type is created inside a guarded ``DO`` block that checks
    ``pg_type`` first (idempotent across re-runs), mirroring the
    ``0002_evidence_schema`` enum-creation pattern.
    """
    vals = ", ".join(f"'{v}'" for v in values)
    op.execute(
        f"DO $$\nBEGIN\n"
        f"    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = '{name}') THEN\n"
        f"        CREATE TYPE {name} AS ENUM ({vals});\n"
        f"    END IF;\n"
        f"END $$;"
    )
