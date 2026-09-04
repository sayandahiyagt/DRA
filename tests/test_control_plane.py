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
    STRATEGY_EXHAUSTIVE,
    STRATEGY_MINIMAL,
    STRATEGY_PROGRESSIVE,
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
        "live_investigators": False,
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
        "strategy": "progressive",
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
    # Phase 13 (handoff) is gated behind budget_ok; zero budget never reaches it
    # (guards the new §33 generation gate).
    assert not state.get("handoff"), state


# ---------------------------------------------------------------------------
# 3. No-DB: phase advancement through the whole pipeline (defensive path)
# ---------------------------------------------------------------------------


def test_phase_advancement_no_db():
    """Pre-seeded intent advances the full 15-phase pipeline without a DB.

    With ``live_investigators=False`` (the no-DB default) Phase 5 skips the
    DB-backed fan-out, so the run reaches Phase 14 INCOMPLETE without touching
    Postgres — proving the DAG and per-phase routers are wired end-to-end. The
    DB-gated smoke test exercises real investigators (``live_investigators=True``)
    and reaches COMPLETE with canonical evidence.
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
    # No-DB mode skipped the Phase 5 fan-out (no InvestigatorContext dispatches).
    assert not state.get("branch_results"), state
    # Phase 13 replaced the stub: a §33 manifest + 8-section package must be
    # present in control state even on the no-DB path (degraded, DB staging
    # skipped via the live_investigators gate).
    handoff = state["handoff"]
    assert handoff["phase"] == 13, handoff
    assert handoff["section_count"] == 8, handoff
    assert handoff["schema_version"] == "1.0", handoff
    assert handoff["retrieval_contract"] == "§34", handoff
    assert not handoff["db_staged"], handoff
    assert handoff["manifest"]["schema_version"] == "1.0", handoff["manifest"]
    from dra.handoff import SECTION_FILES

    assert handoff["manifest"]["document_map"]["sections"] == SECTION_FILES, handoff


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


def test_synthesize_tasks_routes_repo_sources():
    """A repo source in intent yields a repo-source ResearchTask (no DB)."""
    from dra.control_plane import _synthesize_tasks

    state = _base_state(
        run_id="r1",
        recon_results=[{"perspective": "p", "query": "q: demo", "seen_source_ids": []}],
        intent={
            "objective": "comprehend the repo",
            "sources": [{"kind": "repo", "ref": "/path/to/repo"}],
        },
    )
    tasks = _synthesize_tasks(state)
    assert len(tasks) == 1
    (tid, task) = next(iter(tasks.items()))
    assert task["task_id"] == tid
    assert task["source"]["kind"] == "repo"
    assert task["source"]["ref"] == "/path/to/repo"
    assert task["source_types"] == ["repo"]
    assert "readme" in task["question"].lower()


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
# 4b. No-DB: strategy field + §38.5 A/B routing (always green, no Postgres)
# ---------------------------------------------------------------------------


def test_strategy_field_present_and_default():
    """ControlState carries a ``strategy`` field defaulting to ``progressive``."""
    assert "strategy" in ControlState.__annotations__
    assert STRATEGY_PROGRESSIVE == "progressive"
    # p0 seeds strategy into state from the default when not overridden.
    graph = build_graph().compile(checkpointer=InMemorySaver())
    state = asyncio.run(
        graph.ainvoke(
            _base_state(),
            config={"configurable": {"thread_id": "strategy-default"}},
        )
    )
    assert state["strategy"] == "progressive", state


@pytest.mark.parametrize("strategy", [STRATEGY_PROGRESSIVE, STRATEGY_EXHAUSTIVE, STRATEGY_MINIMAL])
def test_strategy_p1_payload(strategy):
    """Each strategy yields a distinct p1 interrupt payload (no DB, always green)."""
    graph = build_graph().compile(checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": f"payload-{strategy}"}}
    first = asyncio.run(
        graph.ainvoke(_base_state(strategy=strategy), config=cfg)
    )
    interrupt = first.get("__interrupt__")
    assert interrupt, f"expected a Phase-1 interrupt for {strategy}"
    payload = interrupt[0].value
    assert payload["strategy"] == strategy, payload
    assert payload["phase"] == 1
    questions = payload.get("questions", [])
    if strategy == STRATEGY_EXHAUSTIVE:
        assert len(questions) == 11, "exhaustive p1 must carry the §9.1 questionnaire"
        assert "questionnaire" in payload["instruction"].lower()
    elif strategy == STRATEGY_MINIMAL:
        assert questions == [], "minimal p1 must have no questions"
        assert "objective" in payload["instruction"].lower()
    else:
        assert questions == [], "progressive p1 must have no questions"
        assert "IntentSnapshot" in payload["instruction"]


def test_minimal_strategy_skips_p1_interrupt():
    """Minimal strategy with pre-seeded intent runs to p14 without interrupt."""
    graph = build_graph().compile(checkpointer=InMemorySaver())
    state = asyncio.run(
        graph.ainvoke(
            _base_state(
                strategy=STRATEGY_MINIMAL,
                intent={"objective": "build the state machine", "constraints": ["s"]},
            ),
            config={"configurable": {"thread_id": "minimal-seeded"}},
        )
    )
    assert state["phase"] == NUM_PHASES - 1, state
    assert state["status"] in (COMPLETE, INCOMPLETE), state
    assert not state.get("__interrupt__"), "no interrupt when intent is pre-seeded"


def test_exhaustive_strategy_skips_p1_interrupt():
    """Exhaustive strategy with pre-seeded intent runs to p14 without interrupt."""
    graph = build_graph().compile(checkpointer=InMemorySaver())
    state = asyncio.run(
        graph.ainvoke(
            _base_state(
                strategy=STRATEGY_EXHAUSTIVE,
                intent={"objective": "build the state machine", "constraints": ["s"]},
            ),
            config={"configurable": {"thread_id": "exhaustive-seeded"}},
        )
    )
    assert state["phase"] == NUM_PHASES - 1, state
    assert state["status"] in (COMPLETE, INCOMPLETE), state
    assert not state.get("__interrupt__"), "no interrupt when intent is pre-seeded"
    # p4 is a no-op under exhaustive/minimal — user_decisions untouched.
    assert state.get("user_decisions") == {}


def test_p4_noop_under_non_progressive_strategies():
    """p4 returns RUNNING without interrupt under exhaustive and minimal."""
    import dra.control_plane as cp

    for strategy in (STRATEGY_EXHAUSTIVE, STRATEGY_MINIMAL):
        result = asyncio.run(
            cp.p4(_base_state(strategy=strategy, live_investigators=False))
        )
        assert result["phase"] == 4, result
        assert result["status"] == "RUNNING", result
        assert "questions" not in result or result.get("strategy") == strategy


def test_delete_duplicate_branch_worker_is_gone():
    """Exactly one ``async def branch_worker`` definition remains in the module."""
    import dra.control_plane as cp
    from pathlib import Path

    src = Path(cp.__file__).read_text()
    assert src.count("async def branch_worker") == 1, "duplicate branch_worker still present"
    graph = build_graph().compile()
    assert "branch_worker" in set(graph.nodes)


def test_compile_reports_strategy():
    """``compile`` still succeeds and reports the assembled graph."""
    from dra.control_plane import main

    rc = main(["compile"])
    assert rc == 0


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
            first = await graph.ainvoke(_base_state(live_investigators=True), config=cfg)
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


# ---------------------------------------------------------------------------
# 6. DB-gated: versioned user_assertion recording on p1/p4 resumes (dra#45)
# ---------------------------------------------------------------------------


@DB
@pytest.mark.usefixtures("_isolated_async_engine")
def test_user_assertion_recorded_on_p1_resume(tmp_path):
    """Resuming a p1 interrupt records canonical USER_CONSTRAINT rows.

    Mirrors the ``test_smoke_round_trip`` DB-gated pattern (AsyncPostgresSaver
    checkpointer + ``_isolated_async_engine`` NullPool fixture +
    ``tests._evidence.reset``).  ``require_db=True`` enables the assertion
    recording path; the no-DB always-green tests stay green without Postgres.
    """
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from langgraph.store.memory import InMemoryStore
    from sqlalchemy import text

    from dra.control_plane import postgres_conninfo
    from dra.db import DATABASE_URL
    from dra.publish import async_session
    from tests._evidence import reset

    async def run():
        await reset()
        # reset() does not truncate user_assertion (standalone table); clean it
        # explicitly so prior test runs don't leak into this one.
        async with async_session() as s:
            await s.execute(text("TRUNCATE TABLE user_assertion RESTART IDENTITY CASCADE"))
            await s.commit()

        thread_id = f"ua-p1-{uuid.uuid4().hex[:8]}"
        run_id = f"run-ua-p1-{thread_id}"
        async with AsyncPostgresSaver.from_conn_string(postgres_conninfo(DATABASE_URL)) as checkpointer:
            await checkpointer.setup()
            store = InMemoryStore()
            graph = build_graph().compile(checkpointer=checkpointer, store=store)
            cfg = {"configurable": {"thread_id": thread_id}}

            first = await graph.ainvoke(
                _base_state(require_db=True, live_investigators=False, run_id=run_id),
                config=cfg,
            )
            assert first.get("__interrupt__"), first

            resumed = await graph.ainvoke(
                Command(resume={
                    "objective": "build the state machine",
                    "constraints": ["scope:test"],
                    "user_decisions": {"arch": "langgraph"},
                }),
                config=cfg,
            )
            assert resumed["phase"] == NUM_PHASES - 1, resumed
            assert resumed["status"] in (COMPLETE, INCOMPLETE), resumed

        # Verify the assertions landed as canonical USER_CONSTRAINT rows.
        async with async_session() as s:
            rows = await s.execute(
                text(
                    "SELECT question, value, assertion_type, superseded_by "
                    "FROM user_assertion "
                    "WHERE run_id = :r "
                    "AND assertion_type IN ('USER_CONSTRAINT', 'USER_CORRECTION') "
                    "AND state = 'canonical'"
                ),
                {"r": run_id},
            )
            results = rows.fetchall()
        assert len(results) >= 1, results
        questions = {r[0] for r in results}
        assert "objective" in questions, questions
        # At least one canonical row with superseded_by IS NULL (the original).
        assert any(r[3] is None for r in results), results
    asyncio.run(run())


@DB
@pytest.mark.usefixtures("_isolated_async_engine")
def test_user_correction_supersedes_prior(tmp_path):
    """A second p1 resume with a different objective becomes USER_CORRECTION.

    Two separate runs share the same ``run_id``.  The second p1 resume finds the
    first row (USER_CONSTRAINT, value "objective A") via the
    ``(run_id, question)`` lookup and stages a USER_CORRECTION that links
    ``superseded_by`` to the first row — the first row stays canonical with
    ``superseded_by IS NULL`` (history preserved, never overwritten).
    """
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from langgraph.store.memory import InMemoryStore
    from sqlalchemy import text

    from dra.control_plane import postgres_conninfo
    from dra.db import DATABASE_URL
    from dra.publish import async_session
    from tests._evidence import reset

    async def run():
        await reset()
        async with async_session() as s:
            await s.execute(text("TRUNCATE TABLE user_assertion RESTART IDENTITY CASCADE"))
            await s.commit()

        run_id = f"run-correction-{uuid.uuid4().hex[:8]}"

        def _state() -> dict:
            return _base_state(
                require_db=True,
                live_investigators=False,
                run_id=run_id,
            )

        # --- First run: objective "A" → USER_CONSTRAINT ---
        cfg1 = {"configurable": {"thread_id": f"c1-{uuid.uuid4().hex[:8]}"}}
        async with AsyncPostgresSaver.from_conn_string(postgres_conninfo(DATABASE_URL)) as checkpointer:
            await checkpointer.setup()
            graph = build_graph().compile(
                checkpointer=checkpointer, store=InMemoryStore()
            )
            first = await graph.ainvoke(_state(), config=cfg1)
            assert first.get("__interrupt__"), first
            await graph.ainvoke(
                Command(resume={"objective": "objective A", "constraints": []}),
                config=cfg1,
            )

        # --- Second run: objective "B" → should find prior, record USER_CORRECTION ---
        cfg2 = {"configurable": {"thread_id": f"c2-{uuid.uuid4().hex[:8]}"}}
        async with AsyncPostgresSaver.from_conn_string(postgres_conninfo(DATABASE_URL)) as checkpointer:
            await checkpointer.setup()
            graph = build_graph().compile(
                checkpointer=checkpointer, store=InMemoryStore()
            )
            first2 = await graph.ainvoke(_state(), config=cfg2)
            assert first2.get("__interrupt__"), first2
            await graph.ainvoke(
                Command(resume={"objective": "objective B", "constraints": []}),
                config=cfg2,
            )

        # --- Assertions ---
        async with async_session() as s:
            rows = await s.execute(
                text(
                    "SELECT id, question, value, assertion_type, superseded_by "
                    "FROM user_assertion "
                    "WHERE run_id = :r AND question = 'objective' "
                    "ORDER BY created_at ASC, id ASC"
                ),
                {"r": run_id},
            )
            results = rows.fetchall()
        assert len(results) == 2, results
        first_row, second_row = results
        first_id, _first_q, first_val, first_type, first_sup = first_row
        second_id, _second_q, second_val, second_type, second_sup = second_row
        assert first_type == "USER_CONSTRAINT", first_row
        assert first_sup is None, first_row  # original stays canonical, unsuperseded
        assert "objective A" in str(first_val), first_row
        assert second_type == "USER_CORRECTION", second_row
        assert str(second_sup) == str(first_id), second_row  # points at the first
        assert "objective B" in str(second_val), second_row
    asyncio.run(run())


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
