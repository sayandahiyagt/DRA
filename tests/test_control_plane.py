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
from langgraph.graph import END
from langgraph.types import Command

from dra.control_plane import (
    B_BLOCKED,
    B_COMPLETE,
    COMPLETE,
    INCOMPLETE,
    NUM_PHASES,
    STRATEGY_EXHAUSTIVE,
    STRATEGY_MINIMAL,
    STRATEGY_PROGRESSIVE,
    _PER_BRANCH_COST,
    _REMAX_ITERATIONS,
    _build_reresearch_tasks,
    _route_reresearch,
    build_graph,
    p11,
    reresearch_worker,
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
    # All 15 phase nodes + the three fan-out leaves.
    for i in range(NUM_PHASES):
        assert f"p{i}" in node_names, f"missing phase node p{i}"
    assert "recon_worker" in node_names
    assert "branch_worker" in node_names
    assert "reresearch_worker" in node_names, "Phase 11 re-research fan-out leaf missing"
    # ControlState carries the budget envelope (Phase 11/14 INCOMPLETE trap).
    assert "budget" in ControlState.__annotations__
    # Phase 11 round counter (loop-back termination gate, RC #7).
    assert "reresearch_round" in ControlState.__annotations__
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
    makes the guarantee hold is ``content_blob.hash`` PK with
    ``ON CONFLICT DO UPDATE`` (publish.py stage_content_blob). The assertion is on
    ``content_blob`` (the content-addressed dedupe root), NOT
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
        # Both committed; the content-addressed bytes dedupe to a single
        # content_blob (content-addressed dedupe root).
        from sqlalchemy import text

        async with async_session() as s:
            cnt = await s.scalar(
                text(
                    "SELECT count(*) FROM content_blob WHERE hash = :h"
                ),
                {"h": raw_hash},
            )
        assert cnt == 1, {"content_blob count": cnt, "hash": raw_hash}
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


# ---------------------------------------------------------------------------
# 7. No-DB: Phase 11 targeted re-research gate/dispatch logic (always green)
# ---------------------------------------------------------------------------


def test_p11_emits_full_research_tasks():
    """Phase 11 converts each blocking gap into a full ResearchTask (RC #1).

    Pure in-graph logic (no DB): p11 must emit full ``asdict(ResearchTask)``
    records carrying ``gap_id`` + affected ``claim_ids`` in ``source.metadata``,
    a deterministic capture-bytes payload, and a round counter.
    """
    from dataclasses import fields

    from dra.control_plane import ResearchTask

    gap = {
        "gap_id": "gap:0",
        "description": "No content-addressed evidence was staged (no canonical source_capture).",
        "severity": "high",
        "impact": 2,
        "blocking": True,
        "related_claim_ids": ["claim:task-x"],
    }
    result = asyncio.run(p11(_base_state(gaps=[gap])))
    assert result.get("status") != INCOMPLETE, result  # round 0 < _REMAX_ITERATIONS
    tasks = result["reresearch_tasks"]
    assert len(tasks) == 1, tasks
    task = tasks[0]
    # Full ResearchTask-shaped record (asdict(ResearchTask(...))).
    expected = {f.name for f in fields(ResearchTask)}
    assert set(task) == expected, set(task) ^ expected
    assert task["task_id"] == "reresearch-gap:0-pass-0"
    assert task["question"] == gap["description"]
    assert task["retry_rules"] == {"attempts": _REMAX_ITERATIONS}
    assert task["cost_envelope"] == _PER_BRANCH_COST
    # gap_id + affected claim_ids preserved for canonical traceability.
    meta = task["source"]["metadata"]
    assert meta["gap_id"] == "gap:0"
    assert meta["affected_claim_ids"] == ["claim:task-x"]
    # Deterministic capture bytes (the synthetic-evidence fallback).
    assert task["source"]["bytes"] == b"dra-control-plane:reresearch:gap:0:0"
    assert task["source"]["locator"] == "reresearch:gap:0"
    # Round counter advanced for the gate.
    assert result["reresearch_round"] == 1


def test_p11_returns_incomplete_when_blocking_gaps_remain_after_retries():
    """A blocking gap surviving _REMAX_ITERATIONS rounds -> INCOMPLETE (RC #7).

    Two independent traps: round exhaustion and zero-budget. Both must yield
    INCOMPLETE so the graph edge routes END without advancing past the gap.
    """
    gap = {
        "gap_id": "gap:1",
        "description": "still unresolved blocking gap",
        "severity": "high",
        "impact": 3,
        "blocking": True,
        "related_claim_ids": [],
    }
    # Round already exhausted -> INCOMPLETE (no further dispatch).
    exhausted = asyncio.run(
        p11(_base_state(gaps=[gap], reresearch_round=_REMAX_ITERATIONS))
    )
    assert exhausted["status"] == INCOMPLETE, exhausted
    assert exhausted["reresearch_round"] == _REMAX_ITERATIONS
    assert exhausted["reresearch_tasks"] == [], exhausted
    # Zero budget also traps INCOMPLETE (existing budget guard, p11-level).
    broke = asyncio.run(
        p11(
            _base_state(
                gaps=[gap],
                budget={"envelope_total": 0.0, "spent": 0.0, "remaining": 0.0, "currency": "USD"},
            )
        )
    )
    assert broke["status"] == INCOMPLETE, broke


def test_p11_creates_no_tasks_when_no_blocking_gaps():
    """Non-blocking gaps (or none) -> no re-research tasks, proceeds to p12."""
    from dra.control_plane import p11

    nonblocking = {
        "gap_id": "g1", "description": "critic question", "severity": "medium",
        "impact": 1, "blocking": False, "related_claim_ids": [],
    }
    result = asyncio.run(p11(_base_state(gaps=[nonblocking])))
    assert result.get("status") != INCOMPLETE
    assert result["reresearch_tasks"] == [], result
    assert result["reresearch_round"] == 0  # round not advanced when nothing dispatched


def test_route_reresearch_fanout_dispatches_workers():
    """DB path (live_investigators True) with a blocking gap fans out Send tasks."""
    gap = {
        "gap_id": "gap:db", "description": "d", "severity": "high",
        "impact": 2, "blocking": True, "related_claim_ids": [],
    }
    tasks = _build_reresearch_tasks(_base_state(), [gap], 0)
    state = _base_state(gaps=[gap], live_investigators=True, reresearch_tasks=tasks)
    routed = _route_reresearch(state)
    assert isinstance(routed, list), routed
    assert routed, "expected a fan-out Send list for a blocking DB gap"
    assert routed[0].node == "reresearch_worker"
    assert all(s.arg.get("task_id", "").startswith("reresearch-") for s in routed)


def test_route_reresearch_no_db_skips_fanout():
    """live_investigators=False -> p12 even with blocking gaps + tasks queued."""
    gap = {
        "gap_id": "g", "description": "d", "severity": "high",
        "impact": 1, "blocking": True, "related_claim_ids": [],
    }
    tasks = _build_reresearch_tasks(_base_state(), [gap], 0)
    state = _base_state(gaps=[gap], live_investigators=False, reresearch_tasks=tasks)
    assert _route_reresearch(state) == "p12"


def test_route_reresearch_incomplete_routes_end():
    """INCOMPLETE (round/budget exhausted) -> END, never advances to p12."""
    gap = {
        "gap_id": "g", "description": "d", "severity": "high",
        "impact": 1, "blocking": True, "related_claim_ids": [],
    }
    state = _base_state(
        status=INCOMPLETE,
        gaps=[gap],
        live_investigators=True,
        reresearch_round=_REMAX_ITERATIONS,
        reresearch_tasks=[],
    )
    assert _route_reresearch(state) == END


# ---------------------------------------------------------------------------
# 8. DB-gated: re-research worker dispatch + full closed loop (Postgres)
# ---------------------------------------------------------------------------


@DB
@pytest.mark.usefixtures("_isolated_async_engine")
def test_reresearch_worker_dispatches_via_run_branch_worker(tmp_path):
    """reresearch_worker is a thin wrapper over run_branch_worker (RC #2).

    NOTE (plan deviation): PLAN_1.md §5a listed this no-DB, but run_branch_worker
    opens an InvestigatorContext (DB-backed), so it is DB-gated here — the no-DB
    path is covered by test_route_reresearch_no_db_skips_fanout + test_graph_assembles.
    """
    from dra.investigators import content_hash
    from dra.publish import async_session
    from sqlalchemy import text

    task_id = "reresearch-direct-pass-0"
    raw_bytes = b"dra-control-plane:reresearch:direct:0"
    task = {
        "task_id": task_id,
        "question": "re-research a blocking gap",
        "run_id": "run-reroute-direct",
        "actor": {
            "kind": "model", "name": "test", "version": "1.0",
            "external_id": "dra-control-plane#1.0",
        },
        "source": {
            "kind": "capture",
            "locator": "reresearch:gap:direct",
            "bytes": raw_bytes,
            "metadata": {"gap_id": "gap:direct", "affected_claim_ids": []},
        },
    }

    async def run():
        from tests._evidence import reset

        await reset()
        out = await reresearch_worker(task)
        br = out["branch_results"][0]
        assert br["status"] == B_COMPLETE, br
        assert br["evidence_ids"], br
        # Canonical source_capture staged for the deterministic bytes
        # (content-addressed dedupe lives on content_blob; the capture is
        # promoted to canonical via source_capture.state, mirrored by
        # _DOMAIN_STATE_TABLES).
        raw_hash = content_hash(raw_bytes)
        async with async_session() as s:
            cnt = await s.scalar(
                text(
                    "SELECT count(*) FROM source_capture sc "
                    "JOIN content_blob cb ON sc.content_blob_hash = cb.hash "
                    "JOIN prov_entity pe ON pe.entity_kind='raw_capture' "
                    "AND pe.id = sc.capture_id "
                    "JOIN prov_bundle pb ON pb.id = pe.bundle_id "
                    "WHERE pb.run_id=:r AND sc.state='canonical' "
                    "AND cb.hash=:h"
                ),
                {"r": "run-reroute-direct", "h": raw_hash},
            )
        assert cnt == 1, {"canonical source_capture": cnt}

    asyncio.run(run())


@DB
@pytest.mark.usefixtures("_isolated_async_engine")
def test_reresearch_loop_closes_end_to_end(tmp_path, monkeypatch):
    """End-to-end re-research loop (RC #8 resolve path): critic blocking gap ->
    p11 targets a task -> dispatched to an investigator -> canonical evidence
    persisted -> claims rebuilt (p7) -> p8 re-verifies -> p10 re-evaluates ->
    proceed to Phase 12/14 COMPLETE.

    Round 0 uses a repo source whose ref is an invalid local path, so
    RepositoryInvestigator fails (B_BLOCKED, no evidence) -> p10 emits blocking
    gap:0/gap:1 -> p11 dispatches capture re-research tasks -> the capture
    fallback stages canonical source_capture -> p7 rebuilds claims -> p10 re-runs
    with evidence+claims -> no blocking gaps -> p11 -> p12 -> COMPLETE.
    """
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from langgraph.store.memory import InMemoryStore

    from dra.control_plane import postgres_conninfo
    from dra.db import DATABASE_URL
    from dra.publish import async_session
    from sqlalchemy import text
    from tests._evidence import reset

    monkeypatch.setenv("DRA_SANDBOX_CAPABILITY", "static_only")

    async def run():
        await reset()
        thread_id = f"reresearch-e2e-{uuid.uuid4().hex[:8]}"
        # Invalid repo ref: _git_ok fails -> RepositoryInvestigator raises
        # ValueError -> B_BLOCKED (no evidence) -> p10 blocking gap -> re-research.
        repo_ref = str(tmp_path / "not-a-real-repo")
        async with AsyncPostgresSaver.from_conn_string(
            postgres_conninfo(DATABASE_URL)
        ) as checkpointer:
            await checkpointer.setup()
            store = InMemoryStore()
            graph = build_graph().compile(checkpointer=checkpointer, store=store)
            cfg = {"configurable": {"thread_id": thread_id}}
            initial = _base_state(
                require_db=True,
                live_investigators=True,
                run_id=thread_id,
                intent={
                    "objective": "README comprehension of the sample fixture repo",
                    "sources": [{"kind": "repo", "ref": repo_ref, "version": ""}],
                    "constraints": ["scope:repo-comprehension"],
                },
            )
            state = await graph.ainvoke(initial, config=cfg)

        # Phase 14 reached and the blocking gap resolved -> COMPLETE.
        assert state["phase"] == NUM_PHASES - 1, state
        assert state["status"] == COMPLETE, (
            f"expected COMPLETE, got {state['status']}; "
            f"audit={state.get('audit')}; reresearch_round={state.get('reresearch_round')}; "
            f"gaps={state.get('gaps')}"
        )

        # (AC3) Phase 11 actually dispatched re-research workers -> canonical
        # branch_results exist with reresearch-* task ids. Two blocking gaps
        # (gap:0 "no claims" + gap:1 "no evidence") each become one targeted task.
        reresearch_results = [
            b for b in (state.get("branch_results") or [])
            if b.get("task_id", "").startswith("reresearch-")
        ]
        assert len(reresearch_results) == 2, (
            f"expected 2 reresearch branches (one per blocking gap), got {reresearch_results}"
        )
        assert all(b["status"] == B_COMPLETE for b in reresearch_results), reresearch_results
        # The round counter advanced (p11 ran a dispatch round, then gated to p12).
        assert state.get("reresearch_round") >= 1, state.get("reresearch_round")

        # (AC3/4) New canonical evidence persisted for the re-research tasks
        # (source_capture staged through publish_bundle — outside checkpoint
        # state; source_capture.state is flipped to canonical by
        # _mirror_state_canonical).
        async with async_session() as s:
            cnt = await s.scalar(
                text(
                    "SELECT count(*) FROM source_identity si "
                    "JOIN source_capture sc ON sc.source_identity_id = si.id "
                    "JOIN prov_entity pe ON pe.entity_kind='raw_capture' "
                    "AND pe.id = sc.capture_id "
                    "JOIN prov_bundle pb ON pb.id = pe.bundle_id "
                    "WHERE pb.run_id=:r AND sc.state='canonical' "
                    "AND si.locator LIKE 'reresearch:%'"
                ),
                {"r": thread_id},
            )
        assert cnt >= 1, f"no canonical source_capture evidence for re-research (run {thread_id})"

        # (AC5) Relevant claims rebuilt from the new evidence.
        claims = state.get("claims") or []
        assert claims, "no claims rebuilt after re-research"
        assert all(c.get("evidence_ids") for c in claims), claims

        # (AC6) Re-verification ran (p8 produced a §38.4 verification report).
        # The loop topology (reresearch_worker -> p6 -> p7 -> p8 -> ...) guarantees
        # p8 re-executed after the re-research iteration; the merged report
        # carries the gate_rules verdict structure from run_verification_proof.
        vreport = state.get("verification_report") or {}
        assert vreport, "no verification_report from p8"
        assert "gate_rules" in vreport or "verdict" in vreport, vreport

        # (AC7) Critic re-evaluated: the final gaps contain NO blocking gap.
        final_gaps = state.get("gaps") or []
        assert not any(g.get("blocking") for g in final_gaps), final_gaps

    asyncio.run(run())


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
