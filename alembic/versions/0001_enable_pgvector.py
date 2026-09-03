"""Baseline migration: enable the pgvector extension.

Per mission scope (#13), this scaffold owns only the project foundation and the
pgvector extension baseline — no domain tables (sources/claims/evidence) are
defined here. The first domain-table migration is a downstream mission's job.
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "0001_enable_pgvector"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector;")


def downgrade() -> None:
    op.execute("DROP EXTENSION IF EXISTS vector;")
