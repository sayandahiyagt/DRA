"""Model-routing proof schema (dra#9, §38.3).

Adds standalone tables for the §38.3 model-routing proof: a per-task/per-role
model candidate evaluation log (``model_routing_eval``) and an escalation log
(``model_escalation_log``) that records every expensive-model escalation decision
(§38 acceptance criteria: "expensive model escalation must be logged"), plus a
persisted policy-knobs table (``model_routing_config``) seeded from env.

Stands alone from the dra#14 canonical evidence schema — mirroring the dra#15
``proof_corpus`` pattern: the approved schema contract is immutable by
``test_schema_introspection``, so the proof's tables are standalone and
reversible.

Migration 0006 chains after ``0004_implementation_entity_state`` (a pre-existing
migration present in the shared DB but not in the repo — see
``0004_implementation_entity_state.py`` stub).
"""

from __future__ import annotations

from alembic import op

revision = "0006_model_routing_schema"
down_revision = "0004_implementation_entity_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- model_routing_config (persisted policy knobs, env-seeded) -----------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS model_routing_config (
            key            TEXT PRIMARY KEY,
            value_text     TEXT,
            value_json     JSONB,
            updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )

    # Seed sensible defaults (idempotent via the upsert pattern).
    _seed_config()

    # --- model_escalation_log (§38 acceptance criteria: log all escalations) --
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS model_escalation_log (
            id                BIGSERIAL PRIMARY KEY,
            run_id            TEXT NOT NULL,
            task_id           TEXT NOT NULL,
            role              TEXT NOT NULL,
            fixture_id        TEXT,
            from_pool         TEXT NOT NULL,
            to_pool           TEXT NOT NULL,
            trigger           TEXT NOT NULL,
            cost_delta_usd    NUMERIC(12,6),
            latency_delta_ms  NUMERIC(12,3),
            correctness_gain  NUMERIC(6,4),
            escalated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )

    # --- model_routing_eval (per-variant evaluation rows, §38.3 metrics) ------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS model_routing_eval (
            id                BIGSERIAL PRIMARY KEY,
            run_id            TEXT NOT NULL,
            task_id           TEXT NOT NULL,
            role              TEXT NOT NULL,
            pool              TEXT NOT NULL,
            provider          TEXT NOT NULL,
            model_name        TEXT NOT NULL,
            is_advisor          BOOLEAN NOT NULL DEFAULT FALSE,
            correctness         NUMERIC(6,4) NOT NULL,
            unsupported_rate    NUMERIC(6,4) NOT NULL,
            cost_usd           NUMERIC(12,6) NOT NULL,
            p50_latency_ms      NUMERIC(12,3) NOT NULL,
            p95_latency_ms      NUMERIC(12,3) NOT NULL,
            num_calls           INT NOT NULL,
            num_items           INT NOT NULL,
            evaluated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (run_id, task_id, role, pool, is_advisor)
        )
        """
    )

    # Indexes for §32 dashboards (model/provider cost by role, retry/escalation rates).
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_escalation_run_task "
        "ON model_escalation_log (run_id, task_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_escalation_role "
        "ON model_escalation_log (role)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_escalation_from_to "
        "ON model_escalation_log (from_pool, to_pool)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_eval_run_role "
        "ON model_routing_eval (run_id, role)"
    )


def _seed_config() -> None:
    """Seed idempotent default policy knobs."""
    # alembic op.execute in the async env does not accept a params dict, so we
    # interpolate the JSON literals directly. value_text is always NULL for
    # these JSON-backed config rows.
    rows = [
        ("correctness_floor",
         '{"value": 0.90, "description": "minimum correctness for a variant to be considered admissible"}'),
        ("unsupported_claim_ceil",
         '{"value": 0.15, "description": "maximum unsupported-claim rate"}'),
        ("cost_ceiling_usd",
         '{"value": 10.0, "description": "per-task cost ceiling in USD"}'),
        ("eval_mode_default",
         '{"value": "offline", "description": "offline | live"}'),
    ]
    for key, vj in rows:
        op.execute(
            "INSERT INTO model_routing_config (key, value_text, value_json) "
            f"VALUES ('{key}', NULL, '{vj}'::jsonb) "
            "ON CONFLICT (key) DO UPDATE SET value_json = EXCLUDED.value_json"
        )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_eval_run_role")
    op.execute("DROP INDEX IF EXISTS ix_escalation_from_to")
    op.execute("DROP INDEX IF EXISTS ix_escalation_role")
    op.execute("DROP INDEX IF EXISTS ix_escalation_run_task")
    op.execute("DROP TABLE IF EXISTS model_routing_eval")
    op.execute("DROP TABLE IF EXISTS model_escalation_log")
    op.execute("DROP TABLE IF EXISTS model_routing_config")
