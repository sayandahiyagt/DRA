"""Canonical evidence-graph + W3C-PROV provenance schema (ADR-004/013/014).

Implements the §4 lineage chain (raw capture -> parsed/normalized evidence ->
claim -> verification -> topic/implementation entity -> decision -> handoff),
the §21.1 staged->canonical state machine, and the W3C-PROV-inspired entity /
activity / agent / derivation / bundle graph (ADR-014) as first-class tables,
plus ADR-013 transactional staged->canonical publication support.

Schema defined via raw SQL (env.py uses ``target_metadata = None``, matching the
baseline migration's established pattern).
"""

from __future__ import annotations

from alembic import op

revision = "0002_evidence_schema"
down_revision = "0001_enable_pgvector"
branch_labels = None
depends_on = None


def _create_enum(name: str, values: list[str]) -> None:
    """Create a PostgreSQL enum type idempotently.

    PostgreSQL does not support ``CREATE TYPE IF NOT EXISTS ... AS ENUM``,
    so each type is created inside a guarded ``DO`` block that checks
    ``pg_type`` first (idempotent across re-runs).
    """
    vals = ", ".join(f"'{v}'" for v in values)
    op.execute(
        f"DO $$\nBEGIN\n"
        f"    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = '{name}') THEN\n"
        f"        CREATE TYPE {name} AS ENUM ({vals});\n"
        f"    END IF;\n"
        f"END $$;"
    )


def upgrade() -> None:
    # --- Shared enum types -------------------------------------------------
    _create_enum(
        "evidence_state",
        ["discovered", "fetched", "staged", "canonical", "verified",
         "superseded", "stale", "rejected"],
    )
    _create_enum("source_kind", ["repo", "paper", "web", "doc", "pdf"])
    _create_enum("raw_kind", ["html", "pdf", "repo_snapshot", "text", "image", "xml"])
    _create_enum(
        "artifact_kind",
        ["parsed", "normalized", "vector_embedding", "summary", "synthesis"],
    )
    _create_enum(
        "activity_type",
        ["acquisition", "parsing", "derivation", "verification",
         "publication", "synthesis", "human_correction"],
    )
    _create_enum(
        "entity_kind",
        ["raw_capture", "derived_artifact", "evidence_unit", "claim",
         "decision", "handoff", "gap", "implementation_entity"],
    )
    _create_enum(
        "relationship_type",
        ["related", "narrower", "broader", "implies", "contradicts", "synonym"],
    )
    _create_enum("impl_kind", ["file", "symbol", "algorithm", "interface", "api"])
    _create_enum("gap_severity", ["low", "medium", "high", "critical"])
    _create_enum("agent_kind", ["human", "model", "tool", "organization"])

    # --- Provenance graph (ADR-014) ----------------------------------------
    op.execute(
        """
        CREATE TABLE prov_agent (
            id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            kind          agent_kind               NOT NULL,
            name          TEXT,
            version       TEXT,
            model_family  TEXT,
            external_id   TEXT,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    op.execute(
        """
        CREATE TABLE prov_bundle (
            id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            run_id        TEXT NOT NULL,
            task_id       TEXT NOT NULL,
            label         TEXT,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    op.execute(
        """
        CREATE TABLE prov_activity (
            id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            bundle_id     UUID REFERENCES prov_bundle(id),
            activity_type activity_type          NOT NULL,
            started_at    TIMESTAMPTZ,
            ended_at      TIMESTAMPTZ,
            agent_id      UUID REFERENCES prov_agent(id),
            input_ids     JSONB                  DEFAULT '[]'::jsonb,
            output_ids    JSONB                  DEFAULT '[]'::jsonb,
            metadata      JSONB                  DEFAULT '{}'::jsonb
        );
        """
    )
    op.execute(
        """
        CREATE TABLE prov_entity (
            id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            bundle_id             UUID REFERENCES prov_bundle(id),
            entity_kind           entity_kind            NOT NULL,
            content_hash          TEXT,
            version               INT,
            state                 evidence_state         NOT NULL DEFAULT 'staged',
            produced_by_activity  UUID REFERENCES prov_activity(id),
            created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
            metadata              JSONB                  DEFAULT '{}'::jsonb
        );
        """
    )
    op.execute(
        """
        CREATE TABLE prov_generation (
            entity_id    UUID REFERENCES prov_entity(id)     NOT NULL,
            activity_id  UUID REFERENCES prov_activity(id)   NOT NULL,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (entity_id, activity_id)
        );
        """
    )
    op.execute(
        """
        CREATE TABLE prov_derivation (
            derived_entity_id  UUID REFERENCES prov_entity(id)  NOT NULL,
            source_entity_id   UUID REFERENCES prov_entity(id)  NOT NULL,
            activity_id        UUID REFERENCES prov_activity(id)  NOT NULL,
            created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (derived_entity_id, source_entity_id, activity_id)
        );
        """
    )

    # --- Canonical domain tables (§4 lineage chain) ------------------------
    op.execute(
        """
        CREATE TABLE source_identity (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            kind            source_kind       NOT NULL,
            locator         TEXT              NOT NULL,
            version         TEXT,
            license_spdx    TEXT,
            access_basis    TEXT,
            crawl_allowed   BOOLEAN,
            auth_scope      TEXT,
            redist_allowed  BOOLEAN,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            metadata        JSONB           DEFAULT '{}'::jsonb,
            UNIQUE (locator, version)
        );
        """
    )
    op.execute(
        """
        CREATE TABLE raw_capture (
            content_hash   TEXT PRIMARY KEY,
            source_id      UUID REFERENCES source_identity(id),
            kind           raw_kind            NOT NULL,
            mime_type      TEXT,
            size_bytes     BIGINT,
            captured_at    TIMESTAMPTZ,
            stored_at      TEXT,
            state          evidence_state      NOT NULL DEFAULT 'discovered',
            created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            metadata       JSONB               DEFAULT '{}'::jsonb
        );
        """
    )
    op.execute(
        """
        CREATE TABLE derived_artifact (
            id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            source_capture_hash   TEXT REFERENCES raw_capture(content_hash),
            content_hash          TEXT,
            kind                  artifact_kind           NOT NULL,
            schema_name           TEXT,
            version               INT,
            produced_by_activity  UUID REFERENCES prov_activity(id),
            superseded_by         UUID REFERENCES derived_artifact(id),
            valid_from            TIMESTAMPTZ,
            valid_to              TIMESTAMPTZ,
            staleness_policy      JSONB                   DEFAULT '{}'::jsonb,
            state                 evidence_state          NOT NULL DEFAULT 'staged',
            created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
            metadata              JSONB                   DEFAULT '{}'::jsonb,
            UNIQUE (content_hash, kind, version)
        );
        """
    )
    op.execute(
        """
        CREATE TABLE evidence_unit (
            id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            artifact_id           UUID REFERENCES derived_artifact(id),
            locator               JSONB                   NOT NULL,
            excerpt               TEXT,
            content_hash          TEXT,
            produced_by_activity  UUID REFERENCES prov_activity(id),
            verification_policy   JSONB                   DEFAULT '{}'::jsonb,
            state                 evidence_state          NOT NULL DEFAULT 'staged',
            version               INT,
            created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
            metadata              JSONB                   DEFAULT '{}'::jsonb
        );
        """
    )
    op.execute(
        """
        CREATE TABLE topic (
            id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            name           TEXT              NOT NULL,
            description    TEXT,
            parent_topic_id UUID REFERENCES topic(id),
            content_hash   TEXT,
            created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            metadata       JSONB           DEFAULT '{}'::jsonb,
            UNIQUE (name)
        );
        """
    )
    op.execute(
        """
        CREATE TABLE claim (
            id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            evidence_unit_id      UUID REFERENCES evidence_unit(id),
            topic_id              UUID REFERENCES topic(id),
            text                  TEXT                  NOT NULL,
            confidence          NUMERIC(3,2),
            verification_state   JSONB                   DEFAULT '{}'::jsonb,
            produced_by_activity UUID REFERENCES prov_activity(id),
            state                 evidence_state          NOT NULL DEFAULT 'staged',
            version               INT,
            superseded_by         UUID REFERENCES claim(id),
            created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
            metadata              JSONB                   DEFAULT '{}'::jsonb
        );
        """
    )
    op.execute(
        """
        CREATE TABLE topic_relationship (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            source_topic_id UUID REFERENCES topic(id),
            target_topic_id UUID REFERENCES topic(id),
            relationship_type relationship_type NOT NULL,
            provenance      JSONB               DEFAULT '{}'::jsonb,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    op.execute(
        """
        CREATE TABLE implementation_entity (
            id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            repo_source_id        UUID REFERENCES source_identity(id),
            kind                  impl_kind          NOT NULL,
            path                  TEXT,
            symbol_name           TEXT,
            commit_sha            TEXT,
            line_start            INT,
            line_end              INT,
            signature             TEXT,
            content_hash          TEXT,
            produced_by_activity  UUID REFERENCES prov_activity(id),
            created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
            metadata              JSONB               DEFAULT '{}'::jsonb
        );
        """
    )
    op.execute(
        """
        CREATE TABLE decision (
            id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            claim_id              UUID REFERENCES claim(id),
            topic_id              UUID REFERENCES topic(id),
            state                 JSONB                  DEFAULT '{}'::jsonb,
            text                  TEXT                   NOT NULL,
            rationale             TEXT,
            produced_by_activity  UUID REFERENCES prov_activity(id),
            run_id                TEXT,
            superseded_by         UUID REFERENCES decision(id),
            created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
            metadata              JSONB                  DEFAULT '{}'::jsonb
        );
        """
    )
    op.execute(
        """
        CREATE TABLE gap (
            id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            topic_id              UUID REFERENCES topic(id),
            description           TEXT                  NOT NULL,
            severity              gap_severity          NOT NULL,
            decision_id           UUID REFERENCES decision(id),
            produced_by_activity  UUID REFERENCES prov_activity(id),
            created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
            metadata              JSONB                 DEFAULT '{}'::jsonb
        );
        """
    )
    op.execute(
        """
        CREATE TABLE handoff_statement (
            id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            decision_id           UUID REFERENCES decision(id),
            manifest              JSONB                 DEFAULT '{}'::jsonb,
            run_id                TEXT,
            content               TEXT,
            produced_by_activity  UUID REFERENCES prov_activity(id),
            created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
            metadata              JSONB                 DEFAULT '{}'::jsonb
        );
        """
    )

    # --- Indexes for provenance traversal ----------------------------------
    op.execute("CREATE INDEX IF NOT EXISTS ix_prov_entity_produced_by ON prov_entity(produced_by_activity);")
    op.execute("CREATE INDEX IF NOT EXISTS ix_prov_entity_content_hash ON prov_entity(content_hash);")
    op.execute("CREATE INDEX IF NOT EXISTS ix_prov_entity_bundle ON prov_entity(bundle_id);")
    op.execute("CREATE INDEX IF NOT EXISTS ix_prov_derivation_source ON prov_derivation(source_entity_id);")
    op.execute("CREATE INDEX IF NOT EXISTS ix_prov_derivation_derived ON prov_derivation(derived_entity_id);")
    op.execute("CREATE INDEX IF NOT EXISTS ix_prov_derivation_activity ON prov_derivation(activity_id);")
    op.execute("CREATE INDEX IF NOT EXISTS ix_prov_generation_activity ON prov_generation(activity_id);")
    op.execute("CREATE INDEX IF NOT EXISTS ix_prov_generation_entity ON prov_generation(entity_id);")
    op.execute("CREATE INDEX IF NOT EXISTS ix_prov_activity_bundle ON prov_activity(bundle_id);")
    op.execute("CREATE INDEX IF NOT EXISTS ix_prov_activity_agent ON prov_activity(agent_id);")
    op.execute("CREATE INDEX IF NOT EXISTS ix_prov_agent_kind ON prov_agent(kind);")
    op.execute("CREATE INDEX IF NOT EXISTS ix_derived_artifact_source_capture ON derived_artifact(source_capture_hash);")
    op.execute("CREATE INDEX IF NOT EXISTS ix_evidence_unit_artifact ON evidence_unit(artifact_id);")
    op.execute("CREATE INDEX IF NOT EXISTS ix_claim_evidence_unit ON claim(evidence_unit_id);")
    op.execute("CREATE INDEX IF NOT EXISTS ix_claim_version ON claim(version);")
    op.execute("CREATE INDEX IF NOT EXISTS ix_handoff_decision ON handoff_statement(decision_id);")

    # Staged→canonical publication marker (ADR-013): fast lookup of the
    # bundle's staged rows.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_prov_entity_staged_bundle "
        "ON prov_entity(bundle_id) WHERE state = 'staged';"
    )


def downgrade() -> None:
    op.execute(
        "DROP INDEX IF EXISTS ix_prov_entity_staged_bundle, "
        "ix_handoff_decision, ix_claim_version, ix_claim_evidence_unit, "
        "ix_evidence_unit_artifact, ix_derived_artifact_source_capture, "
        "ix_prov_activity_agent, ix_prov_activity_bundle, "
        "ix_prov_generation_entity, ix_prov_generation_activity, "
        "ix_prov_derivation_activity, ix_prov_derivation_derived, "
        "ix_prov_derivation_source, ix_prov_entity_bundle, "
        "ix_prov_entity_content_hash, ix_prov_entity_produced_by, "
        "ix_prov_agent_kind;"
    )
    op.execute(
        "DROP TABLE IF EXISTS handoff_statement, gap, decision, implementation_entity, "
        "claim, evidence_unit, derived_artifact, raw_capture, source_identity, "
        "topic_relationship, topic, prov_derivation, prov_generation, "
        "prov_entity, prov_activity, prov_bundle, prov_agent CASCADE;"
    )
    op.execute("DROP TYPE IF EXISTS agent_kind CASCADE;")
    op.execute("DROP TYPE IF EXISTS gap_severity CASCADE;")
    op.execute("DROP TYPE IF EXISTS impl_kind CASCADE;")
    op.execute("DROP TYPE IF EXISTS relationship_type CASCADE;")
    op.execute("DROP TYPE IF EXISTS entity_kind CASCADE;")
    op.execute("DROP TYPE IF EXISTS activity_type CASCADE;")
    op.execute("DROP TYPE IF EXISTS artifact_kind CASCADE;")
    op.execute("DROP TYPE IF EXISTS raw_kind CASCADE;")
    op.execute("DROP TYPE IF EXISTS source_kind CASCADE;")
    op.execute("DROP TYPE IF EXISTS evidence_state CASCADE;")
