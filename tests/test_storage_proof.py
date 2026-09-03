"""§38.2 Storage proof tests (dra#15).

DB-gated: skips (exit 0) when Postgres+pgvector is unreachable, matching the
skipif convention from ``tests/_db.py``. When the DB is live, these tests
exercise the exact-vs-HNSW recall, p50/p95 latency, tenant/project filtering
isolation, and update/delete/staleness workloads against the
``proof_corpus`` table.

Test style follows ``test_atomic_commit.py``: synchronous ``def test_*()``
wrapping an ``async def run()`` that is driven via ``asyncio.run``.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import text

from dra.proof_corpus import (
    HNSW_INDEX_NAME,
    ProofConfig,
    _vec_str,
    build_query_set,
    compute_recall,
    create_hnsw_index,
    drop_hnsw_index,
    generate_corpus,
    hnsw_explain_plan,
    load_corpus,
    reset_corpus,
    run_exact,
    run_hnsw,
    run_proof,
    workload_delete,
    workload_invalidate,
    workload_mutate,
)
from dra.db import engine
from tests._db import DB, async_session

pytestmark = DB


TEST_CFG = ProofConfig(
    n_vectors=12000,
    dim=384,
    k=10,
    n_queries=64,
    recall_target=0.90,
    latency_hnsw_p95_ms=50.0,
    latency_exact_p95_ms=200.0,
    hnsw_m=16,
    hnsw_ef_construction=200,
    ef_search_sweep=[40, 80, 160, 320],
    reindex_budget_ms=10000.0,
    mutate_count=500,
    delete_count=500,
    invalidate_frac=0.05,
)


def _small_corpus():
    """Generate a small deterministic corpus for speed-sensitive tests."""
    return generate_corpus(
        n_vectors=TEST_CFG.n_vectors,
        dim=TEST_CFG.dim,
        n_tenants=TEST_CFG.n_tenants,
        projects_per_tenant=TEST_CFG.projects_per_tenant,
        topics_per_project=TEST_CFG.topics_per_project,
        seed=TEST_CFG.seed,
    )


def test_corpus_loads_and_exact_recall_is_full():
    """Loaded count matches and exact recall vs self is 1.0."""

    async def run():
        await reset_corpus()
        rows = _small_corpus()
        loaded = await load_corpus(rows)
        assert loaded == TEST_CFG.n_vectors

        query_set = build_query_set(
            rows, k=TEST_CFG.k, n_queries=TEST_CFG.n_queries, seed=TEST_CFG.seed + 1
        )
        exact = await run_exact(query_set, k=TEST_CFG.k)
        assert exact["queries"] == len(query_set)
        assert exact["recall_vs_self"] == 1.0

        async with async_session() as session:
            count = await session.scalar(text("SELECT count(*) FROM proof_corpus"))
            assert count == TEST_CFG.n_vectors
    asyncio.run(run())


def test_hnsw_meets_recall_target():
    """HNSW recall@k vs exact is >= the recall target at some ef_search."""

    async def run():
        await reset_corpus()
        rows = _small_corpus()
        await load_corpus(rows)
        query_set = build_query_set(
            rows, k=TEST_CFG.k, n_queries=TEST_CFG.n_queries, seed=TEST_CFG.seed + 1
        )
        exact = await run_exact(query_set, k=TEST_CFG.k)
        await create_hnsw_index(m=TEST_CFG.hnsw_m, ef_construction=TEST_CFG.hnsw_ef_construction)
        try:
            best_recall = 0.0
            for ef in TEST_CFG.ef_search_sweep:
                result = await run_hnsw(query_set, k=TEST_CFG.k, ef_search=ef, iterative=True)
                recall = compute_recall(exact["ids"], result["ids"], TEST_CFG.k)
                best_recall = max(best_recall, recall)
        finally:
            await drop_hnsw_index()
        assert best_recall >= TEST_CFG.recall_target
    asyncio.run(run())


def test_hnsw_latency_meets_slo():
    """HNSW p95 latency is below the configured SLO."""

    async def run():
        await reset_corpus()
        rows = _small_corpus()
        await load_corpus(rows)
        query_set = build_query_set(
            rows, k=TEST_CFG.k, n_queries=TEST_CFG.n_queries, seed=TEST_CFG.seed + 1
        )
        await run_exact(query_set, k=TEST_CFG.k)
        await create_hnsw_index(m=TEST_CFG.hnsw_m, ef_construction=TEST_CFG.hnsw_ef_construction)
        try:
            result = await run_hnsw(
                query_set, k=TEST_CFG.k, ef_search=160, iterative=True
            )
        finally:
            await drop_hnsw_index()
        assert result["p95_ms"] < TEST_CFG.latency_hnsw_p95_ms
    asyncio.run(run())


def test_hnsw_index_is_engaged():
    """The HNSW index is actually selected by the planner (Defect 4).

    Guards against the operator/opclass mismatch that caused the proof to seq
    scan instead of using HNSW. Two airtight properties, both verified directly
    against the DB:

    1. EXPLAIN at a low ``ef_search`` contains ``idx_proof_corpus_hnsw`` — the
       planner picks the HNSW index, proving the nearest-neighbor operator
       (``<->``) matches the ``vector_l2_ops`` index opclass (R21). With the
       buggy inner-product ``<#>`` the plan is a Bitmap/Heap scan and the
       index name never appears.
    2. recall@10 at low ``ef_search`` is strictly less than 1.0 and strictly
       less than recall at high ``ef_search`` — the signature of real HNSW
       approximation. With the ``<#>`` bug recall is flat 1.0 at every
       ``ef_search`` because retrieval degenerates to exact sequential scan.
    """

    async def run():
        await reset_corpus()
        rows = _small_corpus()
        await load_corpus(rows)
        query_set = build_query_set(
            rows, k=TEST_CFG.k, n_queries=TEST_CFG.n_queries, seed=TEST_CFG.seed + 1
        )
        exact = await run_exact(query_set, k=TEST_CFG.k)
        await create_hnsw_index(m=TEST_CFG.hnsw_m, ef_construction=TEST_CFG.hnsw_ef_construction)
        try:
            # Property 1: the HNSW index is selected at low ef_search.
            qvec, tid, pid, _ = query_set[0]
            plan = await hnsw_explain_plan(
                qvec, tid, pid, k=TEST_CFG.k, ef_search=40, iterative=True
            )
            assert HNSW_INDEX_NAME in plan, (
                f"HNSW index not selected — plan used a non-HNSW path:\n{plan}"
            )

            # Property 2: recall varies with ef_search (genuine approximation).
            recalls = {}
            for ef in TEST_CFG.ef_search_sweep:
                result = await run_hnsw(query_set, k=TEST_CFG.k, ef_search=ef, iterative=True)
                recalls[ef] = compute_recall(exact["ids"], result["ids"], TEST_CFG.k)
            low = recalls[min(TEST_CFG.ef_search_sweep)]
            high = recalls[max(TEST_CFG.ef_search_sweep)]
            assert low < 1.0, (
                f"recall at low ef_search is 1.0 — HNSW not actually approximating: {recalls}"
            )
            assert low < high, (
                f"recall does not vary with ef_search (flat {low}) — HNSW not engaged: {recalls}"
            )
        finally:
            await drop_hnsw_index()
        await reset_corpus()
    asyncio.run(run())


def test_exact_latency_within_slo():
    """Exact (sequential) p95 latency is below the exact SLO."""

    async def run():
        await reset_corpus()
        rows = _small_corpus()
        await load_corpus(rows)
        query_set = build_query_set(
            rows, k=TEST_CFG.k, n_queries=TEST_CFG.n_queries, seed=TEST_CFG.seed + 1
        )
        exact = await run_exact(query_set, k=TEST_CFG.k)
        assert exact["p95_ms"] < TEST_CFG.latency_exact_p95_ms
    asyncio.run(run())


def test_tenant_isolation():
    """Filtered ANN returns no cross-tenant IDs; per-tenant recall >= target."""

    async def run():
        await reset_corpus()
        rows = _small_corpus()
        await load_corpus(rows)
        query_set = build_query_set(
            rows, k=TEST_CFG.k, n_queries=TEST_CFG.n_queries, seed=TEST_CFG.seed + 1
        )
        exact = await run_exact(query_set, k=TEST_CFG.k)
        await create_hnsw_index(m=TEST_CFG.hnsw_m, ef_construction=TEST_CFG.hnsw_ef_construction)
        try:
            result = await run_hnsw(query_set, k=TEST_CFG.k, ef_search=160, iterative=True)
        finally:
            await drop_hnsw_index()

        # Check the query-set rows carry tenant IDs; filtered results should
        # only contain rows matching the query's tenant/project.
        cross_leak = 0
        for q_idx, (qvec, tid, pid, _topic) in enumerate(query_set):
            for rid in result["ids"][q_idx]:
                async with async_session() as session:
                    t = await session.scalar(
                        text("SELECT tenant_id FROM proof_corpus WHERE id = :id"),
                        {"id": rid},
                    )
                    if t is not None and str(t) != tid:
                        cross_leak += 1

        assert cross_leak == 0

        # Per-tenant recall: group exact+hnsw IDs by tenant, compute recall.
        for ti in range(TEST_CFG.n_tenants):
            tid = f"tenant_{ti}"
            indices = [i for i, q in enumerate(query_set) if q[1] == tid]
            if not indices:
                continue
            ex_ids = [exact["ids"][i] for i in indices]
            hn_ids = [result["ids"][i] for i in indices]
            recall = compute_recall(ex_ids, hn_ids, TEST_CFG.k)
            assert recall >= TEST_CFG.recall_target
    asyncio.run(run())


def test_workload_mutation_invalidation():
    """Insert new vectors, REINDEX, verify recall is recoverable within budget."""

    async def run():
        await reset_corpus()
        rows = _small_corpus()
        await load_corpus(rows)
        await create_hnsw_index(m=TEST_CFG.hnsw_m, ef_construction=TEST_CFG.hnsw_ef_construction)
        try:
            result = await workload_mutate(
                n=TEST_CFG.mutate_count, dim=TEST_CFG.dim
            )
        finally:
            await drop_hnsw_index()

        assert result["inserted"] == TEST_CFG.mutate_count
        # pgvector 0.8+ supports incremental HNSW inserts, so staleness may
        # or may not be detected depending on version — both are acceptable.
        assert result["reindex_ms"] <= TEST_CFG.reindex_budget_ms
        assert result["recall_restored"] is True
    asyncio.run(run())


def test_workload_delete_no_ghosts():
    """Deleted row IDs never appear in filtered ANN results."""

    async def run():
        await reset_corpus()
        rows = _small_corpus()
        await load_corpus(rows)
        await create_hnsw_index(m=TEST_CFG.hnsw_m, ef_construction=TEST_CFG.hnsw_ef_construction)
        try:
            result = await workload_delete(n=TEST_CFG.delete_count)
        finally:
            await drop_hnsw_index()

        assert result["deleted"] == TEST_CFG.delete_count
        assert result["ghost_ids_returned"] == 0
    asyncio.run(run())


def test_workload_stale_vector_exclusion():
    """State-machine invalidation removes rows from filtered retrieval."""

    async def run():
        await reset_corpus()
        rows = _small_corpus()
        await load_corpus(rows)
        await create_hnsw_index(m=TEST_CFG.hnsw_m, ef_construction=TEST_CFG.hnsw_ef_construction)
        try:
            result = await workload_invalidate(invalidate_frac=TEST_CFG.invalidate_frac)
        finally:
            await drop_hnsw_index()

        assert result["invalidated"] > 0
        assert result["leaked_stale_ids"] == 0
    asyncio.run(run())


def test_proof_report_pass_fail(tmp_path):
    """End-to-end run_proof emits a machine-checkable report with 5 triggers.

    Writes to a per-test temp path so the canonical ``proof_report.json`` is
    never clobbered by the test run (Defect 3: the 5000-vector TEST_CFG wrote a
    FAIL report to the repo root).
    """

    async def run():
        report = await run_proof(
            cfg=TEST_CFG, write=True, report_path=str(tmp_path / "proof_report.json")
        )
        return report
    report = asyncio.run(run())

    assert report["verdict"] in ("PASS", "FAIL")
    assert len(report["reversal_triggers"]) == 5
    for name, trig in report["reversal_triggers"].items():
        assert trig["pass"] in (True, False), f"trigger {name} has no pass/fail value"
    # Report files exist on the temp path
    assert (tmp_path / "proof_report.json").exists()
    assert (tmp_path / "proof_report.md").exists()

    # Machine-checkable assertion from §10 acceptance criteria
    with open(tmp_path / "proof_report.json") as f:
        r = json.load(f)
    assert r["verdict"] in ("PASS", "FAIL")
    assert len(r["reversal_triggers"]) == 5

    # Leave the DB clean: run_proof leaves the HNSW index built and the corpus
    # loaded; drop the index and reset so subsequent runs start clean.
    asyncio.run(_cleanup())


async def _cleanup():
    """Clean up proof_corpus and drop HNSW index after the end-to-end test."""
    await drop_hnsw_index()
    await reset_corpus()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
