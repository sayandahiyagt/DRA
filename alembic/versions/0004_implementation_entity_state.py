"""Add state column to implementation_entity (pre-existing, not in repo).

This migration was applied to the shared Postgres instance before this branch
was created. The migration source file is absent from the repository's
alembic/versions/, but the ``state`` column (evidence_state enum, default
'staged') already exists in the DB. This stub reproduces it so the Alembic
chain resolves: ... -> 0003 -> 0004_verification_gate_indexes ->
0004_implementation_entity_state -> 0006_model_routing_schema (dra#9).
"""

from __future__ import annotations

from alembic import op

revision = "0004_implementation_entity_state"
down_revision = "0004_verification_gate_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE implementation_entity "
        "ADD COLUMN IF NOT EXISTS state evidence_state "
        "NOT NULL DEFAULT 'staged'"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE implementation_entity DROP COLUMN IF EXISTS state")
