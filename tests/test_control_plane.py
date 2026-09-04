"""Tests for the §10 control-plane state machine (dra#36).

Structural/convention mirrors ``tests/test_atomic_commit.py`` and
``tests/test_verification_gate.py``:

- Pure / in-memory tests run **always** (graph assembly, budget-exhaustion guard,
  phase advancement, helper logic) — they need no Postgres and give fast
  in-sandbox verification of the state-machine contracts.
- DB-gated scenarios (``@DB`` skipif from ``tests._db``) exercise the
  Postgres-backed ``PostgresSaver`` round-trip and the per-worker
  ``InvestigatorContext`` isolation/dedupe guarantee against a provisioned
  Postgres+pgvector at ``DATABASE_URL``.

Budget-exhaustion is asserted on the **no-DB** path (the guard is pure in-graph
state logic, independent of storage) so it is always green; this is a sound
widening of PLAN_1.md §5 test #3 (which listed it DB-gated).
"""

from __future__ import annotations

import asyncio
import sys
import uuid

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from dra.control_plane import (
    B_COMPLETE,
    COMPLETE,
    INCOMPLETE,
    NUM_PHASES,
    build_graph,
    run_branch_worker,
)
from dra.control_plane import ControlState


# ---------------------------------------------------------------------------
# Shared DB gate — robust to environments where SQLAlchemy is not importable.
# Mirrors tests/_db.py: DB-gated tests SKIP (env concern) instead of failing.
# ---------------------------------------------------------------------------


def _db_reachable() -> bool:
    try:
        import asyncio as _asyncio

        from dra.db import can_connect

        return _asyncio.run(can_connect())
    except Exception:
        return False


DB = pytest.mark.skipif(
    not _db_reachable(),
    reason="No reachable Postgres at DATABASE_URL (skipped — env concern, not a code defect)",
)


# ---------------------------------------------------------------------------
# Shared per-test DB engine (copied from tests/test_repo_investigator.py so the
# DB-gated scenarios use a fresh NullPool engine per test, avoiding cross-test
# QueuePool/event-loop deadlocks — each @DB test runs in its own asyncio.run).
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Shared per-test DB engine (copied from tests/test_repo_investigator.py so the
# DB-gated scenarios use a fresh NullPool engine per test, avoiding cross-test
# QueuePool/event-loop deadlocks — each @DB test runs in its own asyncio.run).
# ---------------------------------------------------------------------------


@pytest.fixture
def _isolated_async_engine(monkeypatch):
    """Per-test NullPool async engine, patching every ``async_session`` reference."""
    from dra.db import DATABASE_URL
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    engine = create_async_engine(DATABASE_URL, poolclass=NullPool, future=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    import dra.publish as _publish
    import dra.investigators as _investigators
    import tests._evidence as _evidence

    monkeypatch.setattr(_publish, "async_session", session_factory)
    monkeypatch.setattr(_investigators, "async_session", session_factory)
    monkeypatch.setattr(_evidence, "async_session", session_factory)
    # Phase 0's can_connect gate reads dra.db.engine at call time; point it at
    # the per-test NullPool engine so it (and the investigators) share one loop.
    import dra.db as _dbmod

    monkeypatch.setattr(_dbmod, "engine", engine)
    yield engine
    try:
        loop = asyncio.new_event_loop()
        loop.run_until_complete(engine.dispose())
        loop.close()
    except Exception:
        pass


def _base_state(**overrides) -> dict:
    """A fresh ControlState with neutral defaults for in-graph logic tests."""
    state = {
        "run_id": "run-test",
        "require_db": False,
        "actor": {"kind": "model", "name": "test", "version": "1.0"},
        "budget": {"envelope_total": 10.0, "spent": 0.0, "remaining": 10.0, "currency": "USD"},
        "config_snapshot": {},
        "intent": {},
        "recon_branches": [],
        "recon_results": [],
        "research_tasks": {},
        "user_decisions": {},
        "branches": {},
        "branch_results": [],
        "claims": [],
        "verification_report": {},
        "synthesis": {},
        "gaps": [],
        "decisions": [],
        "handoff": {},
        "audit": {},
    }
    state.update(overrides)
    return state


# ---------------------------------------------------------------------------
# 1. No-DB: graph assembles (the required in-sandbox verification path)
# ---------------------------------------------------------------------------


def test_graph_assembles():
    """The compiled StateGraph must assemble without error (no DB needed)."""
    graph = build_graph().compile()
    node_names = set(graph.nodes)
    # All 15 phase nodes + the two fan-out leaves.
    for i in range(NUM_PHASES):
        assert f"p{i}" in node_names, f"missing phase node p{i}"
    assert "recon_worker" in node_names
    assert "branch_worker" in node_names
    # ControlState carries the budget envelope (Phase 11/14 INCOMPLETE trap).
    assert "budget" in ControlState.__annotations__
    # Entry/exit wiring present.
    assert "__start__" in node_names


def test_module_entrypoints():
    """Public API surface required by the mission brief."""
    import dra.control_plane as cp

    assert callable(cp.build_graph)
    assert callable(cp.main)
    assert cp.ControlState is ControlState


# ---------------------------------------------------------------------------
# 2. No-DB: budget exhaustion -> INCOMPLETE (pure in-graph guard)
# ---------------------------------------------------------------------------


def test_budget_exhaustion_marks_incomplete():
    """Forcing the envelope to zero must yield status == INCOMPLETE, never COMPLETE."""
    graph = build_graph().compile(checkpointer=InMemorySaver())
    state = asyncio.run(
        graph.ainvoke(
            _base_state(budget={"envelope_total": 0.0, "spent": 0.0, "remaining": 0.0, "currency": "USD"}),
            config={"configurable": {"thread_id": "budget-exhaust"}},
        )
    )
    assert state["status"] == INCOMPLETE, state
    assert state["status"] != COMPLETE
    # Must terminate before any interrupt/DB phase.
    assert state["phase"] <= 1


# ---------------------------------------------------------------------------
# 3. No-DB: phase advancement through the whole pipeline (defensive path)
# ---------------------------------------------------------------------------


def test_phase_advancement_no_db():
    """Pre-seeded intent advances the full 15-phase pipeline without a DB.

    Branch workers degrade to BLOCKED (sqlalchemy/investigator extras absent)
    and the audit reports INCOMPLETE — proving the DAG and per-phase routers
    are wired end-to-end. With Postgres provisioned the same run reaches
    COMPLETE/INCOMPLETE with real canonical evidence.
    """
    graph = build_graph().compile(checkpointer=InMemorySaver())
    state = asyncio.run(
        graph.ainvoke(
            _base_state(intent={
                "objective": "build the langgraph control-plane state machine",
                "constraints": ["scope:control-plane"],
            }),
            config={"configurable": {"thread_id": "smoke-nodb"}},
        )
    )
    assert state["phase"] == NUM_PHASES - 1, state
    assert state["status"] in (COMPLETE, INCOMPLETE), state
    # Phase 5 dispatched fan-out workers that were isolated per-task.
    assert state.get("branches"), state


def test_interrupt_resume_roundtrip_inmemory():
    """interrupt() -> resume via Command round-trips through the checkpointer."""
    graph = build_graph().compile(checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": "interrupt-resume"}}
    first = asyncio.run(
        graph.ainvoke(_base_state(), config=cfg)
    )
    assert first.get("__interrupt__"), "expected a Phase-1 interrupt"
    # Resume with an intent snapshot.
    resumed = asyncio.run(
        graph.ainvoke(
            Command(resume={"objective": "build the state machine", "constraints": ["s"]}),
            config=cfg,
        )
    )
    assert resumed["phase"] == NUM_PHASES - 1, resumed
    assert resumed["status"] in (COMPLETE, INCOMPLETE), resumed


# ---------------------------------------------------------------------------
# 4. No-DB: pure helper logic
# ---------------------------------------------------------------------------


def test_recon_synthesizes_one_query_per_perspective():
    from dra.control_plane import _RECON_PERSPECTIVES, _recon_queries

    qs = _recon_queries({"objective": "demo"})
    assert len(qs) == len(_RECON_PERSPECTIVES)
    assert all(q["query"] for q in qs)


def test_synthesize_tasks_is_deterministic():
    from dra.control_plane import _synthesize_tasks

    state = _base_state(
        run_id="r1",
        recon_results=[{"perspective": "p", "query": "q1: demo", "seen_source_ids": []}],
        intent={"objective": "demo"},
    )
    tasks = _synthesize_tasks(state)
    assert len(tasks) == 1
    (tid, task) = next(iter(tasks.items()))
    assert task["task_id"] == tid
    assert task["source"]["kind"] == "capture"
    assert task["source"]["bytes"]


def test_budget_spend_decreases_remaining():
    from dra.control_plane import _spend, budget_ok

    s = _base_state()
    s = {**s, **_spend(s, 3.0)}
    assert budget_ok(s)
    assert s["budget"]["spent"] == 3.0
    assert s["budget"]["remaining"] == 7.0
    s = {**s, **_spend(s, 7.0)}
    assert not budget_ok(s)
    assert s["budget"]["remaining"] == 0.0


def test_critic_questions_cover_section10_10():
    from dra.control_plane import _critic_questions

    qs = _critic_questions()
    assert len(qs) >= 5
    assert any("contradict" in q.lower() for q in qs)


def test_audit_complete_only_when_clean():
    from dra.control_plane import _audit

    clean = _audit(_base_state(
        claims=[{"claim_id": "c1", "evidence_ids": ["e1"], "contradictions": []}],
        branches={"b1": {"status": B_COMPLETE}},
    ), budget_exhausted=False)
    # Has evidence + complete branch + evidenced claim -> passes (no blocking gaps)
    assert clean["passes"] is True

    dirty = _audit(_base_state(claims=[], branches={}), budget_exhausted=False)
    assert dirty["passes"] is False


# ---------------------------------------------------------------------------
# 5. DB-gated: smoke round-trip with Postgres-backed checkpointer
# ---------------------------------------------------------------------------


@DB
@pytest.mark.usefixtures("_isolated_async_engine")
def test_smoke_round_trip(tmp_path):
    """Postgres checkpointer round-trips checkpoint -> interrupt -> resume."""
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from langgraph.store.memory import InMemoryStore
    from dra.control_plane import postgres_conninfo
    from dra.db import DATABASE_URL
    from dra.publish import async_session
    from tests._evidence import reset

    async def run():
        await reset()
        thread_id = f"smoke-pg-{uuid.uuid4().hex[:8]}"
        async with AsyncPostgresSaver.from_conn_string(postgres_conninfo(DATABASE_URL)) as checkpointer:
            await checkpointer.setup()
            store = InMemoryStore()
            graph = build_graph(require_db=True).compile(
                checkpointer=checkpointer, store=store,
            )
            cfg = {"configurable": {"thread_id": thread_id}}

            # Phase 1: invoke to the human-in-the-loop interrupt.
            first = await graph.ainvoke(_base_state(), config=cfg)
            assert first.get("__interrupt__"), first

            # Resume with an intent snapshot; the graph runs the pipeline to end.
            resumed = await graph.ainvoke(
                Command(resume={"objective": "build the state machine", "constraints": ["s"]}),
                config=cfg,
            )
            assert resumed["phase"] == NUM_PHASES - 1, resumed
            assert resumed["status"] in (COMPLETE, INCOMPLETE), resumed

            # Checkpointer persisted a checkpoint row for the thread (round-tripped).
            from sqlalchemy import text

            async with async_session() as s:
                cnt = await s.scalar(
                    text("SELECT count(*) FROM checkpoints WHERE thread_id = :t"),
                    {"t": thread_id},
                )
            assert cnt and cnt >= 1, cnt
    asyncio.run(run())


# ---------------------------------------------------------------------------
# 6. DB-gated: parallel-isolation -> no logical duplicate canonical evidence
# ---------------------------------------------------------------------------


@DB
@pytest.mark.usefixtures("_isolated_async_engine")
def test_parallel_isolation_no_duplicate_canonical(tmp_path):
    """Two concurrent fan-out workers on identical bytes must yield ONE canonical row.

    Each worker opens its own ``InvestigatorContext`` bundle; the dedupe that
    makes the guarantee hold is ``raw_capture.content_hash`` PK with
    ``ON CONFLICT DO UPDATE`` (publish.py:234). The assertion is on
    ``raw_capture`` (content-addressed canonical dedupe point), NOT
    ``prov_entity`` (which is a per-bundle UUID PK and would NOT dedupe).
    """
    from dra.investigators import content_hash

    identical = b"identical-evidence-bytes-for-parallel-isolation"
    raw_hash = content_hash(identical)
    task = {
        "task_id": "parallel-A",
        "question": "researched via identical capture",
        "source": {"kind": "capture", "locator": "parallel-source", "bytes": identical},
        "run_id": "run-parallel",
    }

    async def run():
        from dra.publish import async_session
        from tests._evidence import reset

        await reset()
        # One per worker (each opens its own InvestigatorContext bundle).
        t1 = {**task, "task_id": "parallel-A"}
        t2 = {**task, "task_id": "parallel-B"}
        r1, r2 = await asyncio.gather(
            run_branch_worker(t1, {"kind": "model", "name": "w1", "version": "1.0",
                                   "external_id": "w1"}),
            run_branch_worker(t2, {"kind": "model", "name": "w2", "version": "1.0",
                                   "external_id": "w2"}),
        )
        assert r1.status == B_COMPLETE, r1.errors
        assert r2.status == B_COMPLETE, r2.errors
        # Both committed; the single content_hash row is canonical exactly once.
        from sqlalchemy import text

        async with async_session() as s:
            cnt = await s.scalar(
                text(
                    "SELECT count(*) FROM raw_capture "
                    "WHERE content_hash = :h AND state = 'canonical'"
                ),
                {"h": raw_hash},
            )
        assert cnt == 1, {"raw_capture canonical count": cnt, "hash": raw_hash}
    asyncio.run(run())


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
