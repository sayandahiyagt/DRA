"""Crawl manifest for the §11.4 Browser/DOM investigator (dra#26).

Adds a standalone ``web_crawl_manifest`` log table that records every
acquisition attempt — attempted / skipped / crawled — with its §11.4 ladder
step, RFC 9309 skip reason, latency and status.  This is the per-page crawl
manifest the dra#26 spec requires ("keep a crawl manifest of attempted/skipped
pages"); it lets the investigator's crawl surface be audited without
re-running it and distinguishes skipped (robots-excluded / unauthorized) pages
from crawled ones.

This table is standalone (like ``proof_corpus`` in 0003 and the 0006
``model_routing_*`` tables): it carries its own ``result``/``step`` columns
rather than overloading ``prov_entity``/``raw_capture`` so the canonical
evidence-graph introspection contract (``test_schema_introspection``) is
unaffected.

Migration placement note (dra#23 handoff / dra#26 discovery): the repository
ships a *bifurcated* Alembic history — ``0004_implementation_entity_state`` and
``0005_implementation_entity_state`` both branch off
``0004_verification_gate_indexes`` (the latter is dra#26's lineage), while
``0006_model_routing_schema`` chains after ``0004_implementation_entity_state``.
This produces two alembic heads.  dra#23's handoff states that collision is
intended to be reconciled at merge to origin/main; dra#26 must NOT fix it here
but must place its new migration carefully.  This migration therefore chains
*off the ``0006_model_routing_schema`` head* (the longer, complete branch that
already carries the ``implementation_entity.state`` column + the model-routing
schema) rather than introducing a third state-column migration or a new
branchpoint.  It references no column added by 0005, so it is correct on either
head; the surviving two heads (0005 and this 0007) remain to be merged upstream
by the maintainer (see ``alembic merge``).
"""

from __future__ import annotations

from alembic import op

revision = "0007_web_crawl_manifest"
down_revision = "0006_model_routing_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS web_crawl_manifest (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            bundle_id       UUID REFERENCES prov_bundle(id),
            activity_id     UUID REFERENCES prov_activity(id),
            url             TEXT NOT NULL,
            origin          TEXT NOT NULL,
            result          TEXT NOT NULL CHECK (result IN ('attempted', 'skipped', 'crawled')),
            step            TEXT,
            reason          TEXT,
            latency_ms      NUMERIC(12,3),
            status          INT,
            content_hash    TEXT,
            attempted_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            metadata        JSONB DEFAULT '{}'::jsonb
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_web_crawl_manifest_bundle "
        "ON web_crawl_manifest (bundle_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_web_crawl_manifest_url "
        "ON web_crawl_manifest (url)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_web_crawl_manifest_origin_result "
        "ON web_crawl_manifest (origin, result)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_web_crawl_manifest_origin_result")
    op.execute("DROP INDEX IF EXISTS ix_web_crawl_manifest_url")
    op.execute("DROP INDEX IF EXISTS ix_web_crawl_manifest_bundle")
    op.execute("DROP TABLE IF EXISTS web_crawl_manifest")
