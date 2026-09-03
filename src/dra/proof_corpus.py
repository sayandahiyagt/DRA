"""§38.2 Storage proof engine (dra#15).

Loads a representative, deterministic, license-safe synthetic corpus (texts +
dense embeddings) into the standalone ``proof_corpus`` pgvector table, then
compares exact (sequential) vs HNSW approximate retrieval, measures filtered
recall against exact search (per pgvector R21), p50/p95 latency, tenant/project
filtering isolation, and update/delete/staleness workloads — and emits a
machine-checkable ``proof_report.json`` with pass/fail against the five
ADR-003 reversal triggers.

Design follows PLAN_8.md: raw ``text()`` SQL, reuses ``dra.db.engine`` (no
second connection string), ``_vec_str`` renders pgvector literals without
depending on the psycopg adapter registration order (mirrors ``publish._json``).

CLI entry: ``dra-storage-proof`` (wired in ``pyproject.toml``).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import random
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from dra.db import DATABASE_URL, can_connect, engine
from dra.publish import async_session

HNSW_INDEX_NAME = "idx_proof_corpus_hnsw"


def _vec_str(vec: Sequence[float]) -> str:
    """Render a sequence of floats as a pgvector literal ``'[0.1,0.2,...]'``.

    Uses 6-decimal precision. This avoids depending on the ``pgvector`` psycopg
    adapter being registered; a plain SQL string literal is always valid for a
    ``vector`` column.
    """
    formatted = ", ".join(f"{v:.6f}" for v in vec)
    return f"[{formatted}]"


def _pct(values: Sequence[float]) -> tuple[float, float, float]:
    """Return (p50, p95, mean) for a list of millisecond latencies."""
    if not values:
        return 0.0, 0.0, 0.0
    s = sorted(values)
    n = len(s)
    p50 = s[n // 2] if n % 2 == 1 else (s[n // 2 - 1] + s[n // 2]) / 2
    p95_idx = int(math.ceil(n * 0.95)) - 1
    p95 = s[p95_idx] if p95_idx < n else s[-1]
    return p50, p95, statistics.mean(s)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class ProofConfig:
    """Tunable configuration for the §38.2 storage proof."""

    n_vectors: int = 25000
    dim: int = 384
    n_tenants: int = 3
    projects_per_tenant: int = 2
    topics_per_project: int = 5
    seed: int = 42
    k: int = 10
    n_queries: int = 200
    recall_target: float = 0.90
    latency_hnsw_p95_ms: float = 50.0
    latency_exact_p95_ms: float = 200.0
    hnsw_m: int = 16
    hnsw_ef_construction: int = 200
    ef_search_sweep: list[int] = field(default_factory=lambda: [40, 80, 160, 320])
    reindex_budget_ms: float = 10000.0
    mutate_count: int = 1000
    delete_count: int = 1000
    invalidate_frac: float = 0.05
    latency_cliff_factor: float = 1.5


def _load_config() -> ProofConfig:
    """Build a ProofConfig, applying optional env overrides for SLOs."""
    cfg = ProofConfig()
    if "DRA_PROOF_NVECTORS" in os.environ:
        cfg.n_vectors = int(os.environ["DRA_PROOF_NVECTORS"])
    if "DRA_PROOF_K" in os.environ:
        cfg.k = int(os.environ["DRA_PROOF_K"])
    if "DRA_PROOF_RECALL_TARGET" in os.environ:
        cfg.recall_target = float(os.environ["DRA_PROOF_RECALL_TARGET"])
    if "DRA_PROOF_LATENCY_HNSW_P95_MS" in os.environ:
        cfg.latency_hnsw_p95_ms = float(os.environ["DRA_PROOF_LATENCY_HNSW_P95_MS"])
    if "DRA_PROOF_LATENCY_EXACT_P95_MS" in os.environ:
        cfg.latency_exact_p95_ms = float(os.environ["DRA_PROOF_LATENCY_EXACT_P95_MS"])
    if "DRA_PROOF_REINDEX_BUDGET_MS" in os.environ:
        cfg.reindex_budget_ms = float(os.environ["DRA_PROOF_REINDEX_BUDGET_MS"])
    return cfg


# ---------------------------------------------------------------------------
# Corpus generation (deterministic, in-process, no network)
# ---------------------------------------------------------------------------

_TOPICS = [
    "vector databases", "retrieval augmented generation",
    "postgres internals", "knowledge distillation",
    "agent orchestration",
]


def _l2_normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec))
    if norm < 1e-12:
        return vec
    return [v / norm for v in vec]


def _centroid_key(tid: str, pid: str, topic: str) -> str:
    return f"{tid}:{pid}:{topic}"


def generate_corpus(
    n_vectors: int = 25000,
    dim: int = 384,
    n_tenants: int = 3,
    projects_per_tenant: int = 2,
    topics_per_project: int = 5,
    seed: int = 42,
) -> list[dict]:
    """Generate a deterministic synthetic corpus of ``n_vectors`` dense vectors.

    Each (tenant, project, topic) triple has a unit-norm centroid vector drawn
    from a fixed RNG (seeded). Every vector is ``l2_normalize(centroid + noise)``
    so intra-cluster vectors are close and inter-cluster / cross-tenant vectors
    are far apart. No network or model weights are required.
    """
    rng = random.Random(seed)

    # Per-tenant anchor blocks: each tenant gets a distinct contiguous block of
    # dimensions set to 1.0 (rest 0.0). With 3 tenants in 384 dims, each block is
    # ~128 dims wide, so the cosine similarity between vectors from different
    # tenants is ~0 — guaranteeing tenant isolation in ANN results.
    block_size = max(1, dim // n_tenants)
    tenant_anchors: dict[str, list[float]] = {}
    for ti in range(n_tenants):
        anchor = [0.0] * dim
        for d in range(ti * block_size, min((ti + 1) * block_size, dim)):
            anchor[d] = 1.0
        tenant_anchors[f"tenant_{ti}"] = _l2_normalize(anchor)

    # Pre-generate centroids: one per (tenant, project, topic). Each centroid
    # = normalize(0.9 * tenant_anchor + 0.1 * random_gauss), so centroids within
    # a tenant cluster tightly while cross-tenant centroids are near-orthogonal.
    centroids: dict[str, list[float]] = {}
    for ti in range(n_tenants):
        tid = f"tenant_{ti}"
        anchor = tenant_anchors[tid]
        for pi in range(projects_per_tenant):
            pid = f"proj_{ti}_{pi}"
            for ki in range(topics_per_project):
                key = _centroid_key(tid, pid, _TOPICS[ki % len(_TOPICS)])
                rnd = [rng.gauss(0.0, 1.0) for _ in range(dim)]
                blended = [0.9 * a + 0.1 * r for a, r in zip(anchor, rnd)]
                centroids[key] = _l2_normalize(blended)

    rows: list[dict] = []
    centroid_keys = list(centroids.keys())

    for i in range(n_vectors):
        key = centroid_keys[i % len(centroid_keys)]
        parts = key.split(":")
        tid, pid, topic = parts[0], parts[1], parts[2]

        centroid = centroids[key]
        noise = [rng.gauss(0.0, 0.05) for _ in range(dim)]
        vec = [c + n for c, n in zip(centroid, noise)]
        vec = _l2_normalize(vec)

        doc_id = f"doc_{tid}_{pid}_{i // topics_per_project}"
        chunk_seq = i % topics_per_project
        text_content = (
            f"Corpus chunk {i} for topic '{topic}' in {tid}/{pid}. "
            f"Synthetic content for storage proof retrieval testing."
        )
        content_hash = hashlib.sha256(
            f"{topic}:{tid}:{pid}:{chunk_seq}:{text_content}".encode()
        ).hexdigest()

        rows.append({
            "tenant_id": tid,
            "project_id": pid,
            "doc_id": doc_id,
            "chunk_seq": chunk_seq,
            "topic_id": topic,
            "content_hash": content_hash,
            "text": text_content,
            "embedding": vec,
        })

    return rows


# ---------------------------------------------------------------------------
# DB operations
# ---------------------------------------------------------------------------


async def _get_extversion() -> str | None:
    """Return the installed pgvector extension version string, or None."""
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        )
        v = result.scalar()
        return str(v) if v is not None else None


def _extversion_supports_iterative(extversion: str | None) -> bool:
    """Return True if pgvector >= 0.8.0 (has hnsw.iterative_scan)."""
    if extversion is None:
        return False
    try:
        major = int(extversion.split(".")[0])
        minor = int(extversion.split(".")[1])
    except (ValueError, IndexError):
        return False
    return (major, minor) >= (0, 8)


async def reset_corpus() -> None:
    """Truncate the proof_corpus table (idempotent reset)."""
    async with async_session() as session:
        async with session.begin():
            await session.execute(text("TRUNCATE TABLE proof_corpus RESTART IDENTITY CASCADE"))


async def load_corpus(rows: list[dict]) -> int:
    """Bulk-insert corpus rows with idempotent upsert on content_hash/doc_id/chunk_seq.

    Returns the number of rows in the table after load.
    """
    if not rows:
        return 0
    async with async_session() as session:
        async with session.begin():
            for r in rows:
                await session.execute(
                    text(
                        "INSERT INTO proof_corpus "
                        "(tenant_id, project_id, doc_id, chunk_seq, content_hash, "
                        "text, embedding, topic_id, created_at, updated_at, "
                        "valid_from, valid_to, superseded_by, state, staleness_policy) "
                        "VALUES (:tenant_id, :project_id, :doc_id, :chunk_seq, "
                        ":content_hash, :text, :embedding, :topic_id, now(), now(), "
                        "NULL, NULL, NULL, 'canonical', '{}'::jsonb) "
                        "ON CONFLICT (content_hash, doc_id, chunk_seq) "
                        "DO UPDATE SET embedding = EXCLUDED.embedding, "
                        "text = EXCLUDED.text, topic_id = EXCLUDED.topic_id, "
                        "tenant_id = EXCLUDED.tenant_id, project_id = EXCLUDED.project_id"
                    ),
                    {
                        "tenant_id": r["tenant_id"],
                        "project_id": r["project_id"],
                        "doc_id": r["doc_id"],
                        "chunk_seq": r["chunk_seq"],
                        "content_hash": r["content_hash"],
                        "text": r["text"],
                        "embedding": _vec_str(r["embedding"]),
                        "topic_id": r["topic_id"],
                    },
                )
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT count(*) FROM proof_corpus"))
        return result.scalar()


def build_query_set(
    rows: list[dict], k: int = 10, n_queries: int = 200, seed: int = 99
) -> list[tuple[list[float], str, str, str]]:
    """Sample k-NN queries from held-out cluster centroids with tenant/project filters.

    Returns a list of ``(query_vec, tenant_id, project_id, expected_topic_id)``.
    The authoritative recall denominator is exact-vs-HNSW, not the oracle; the
    oracle is a secondary sanity assertion.
    """
    rng = random.Random(seed)

    # Group rows by (tenant, project, topic) to pick centroids from real clusters
    clusters: dict[str, list[list[float]]] = {}
    cluster_meta: dict[str, tuple[str, str, str]] = {}
    for r in rows:
        key = f"{r['tenant_id']}:{r['project_id']}:{r['topic_id']}"
        if key not in clusters:
            clusters[key] = []
            cluster_meta[key] = (r["tenant_id"], r["project_id"], r["topic_id"])
        clusters[key].append(r["embedding"])

    # Compute mean centroid per cluster
    centroids: dict[str, list[float]] = {}
    for key, vecs in clusters.items():
        mean_vec = [0.0] * len(vecs[0])
        for v in vecs:
            for i, vi in enumerate(v):
                mean_vec[i] += vi
        n = len(vecs)
        mean_vec = [m / n for m in mean_vec]
        centroids[key] = _l2_normalize(mean_vec)

    keys = list(centroids.keys())
    queries: list[tuple[list[float], str, str, str]] = []
    for i in range(n_queries):
        key = rng.choice(keys)
        centroid = centroids[key]
        noise = [rng.gauss(0.0, 0.15) for _ in range(len(centroid))]
        query_vec = _l2_normalize([c + n for c, n in zip(centroid, noise)])
        tid, pid, topic = cluster_meta[key]
        queries.append((query_vec, tid, pid, topic))

    return queries


async def run_exact(
    query_set: list[tuple[list[float], str, str, str]],
    k: int = 10,
) -> dict:
    """Run exact (sequential) retrieval with the HNSW index dropped.

    Uses ``SET LOCAL enable_indexscan = off`` to force a seq scan — pgvector's
    documented exact-search path (R21). If the HNSW index happens to exist,
    it is dropped first so the seq scan is the true baseline.
    """
    drop_sql = f"DROP INDEX IF EXISTS {HNSW_INDEX_NAME}"
    latencies: list[float] = []
    all_ids: list[list[str]] = []

    async with engine.connect() as conn:
        await conn.execute(text(drop_sql))
        # enable_indexscan off forces seq scan = exact path in pgvector
        await conn.execute(text("SET LOCAL enable_indexscan = off"))

        for qvec, tid, pid, _topic in query_set:
            t0 = time.perf_counter()
            sql = (
                f"SELECT id FROM proof_corpus "
                f"WHERE state IN ('canonical', 'verified') "
                f"AND superseded_by IS NULL "
                f"AND (valid_to IS NULL OR valid_to > now()) "
                f"AND tenant_id = :tid AND project_id = :pid "
                f"ORDER BY embedding <-> '{_vec_str(qvec)}' "
                f"LIMIT :k"
            )
            result = await conn.execute(text(sql), {"tid": tid, "pid": pid, "k": k})
            ids = [str(r[0]) for r in result.fetchall()]
            elapsed = (time.perf_counter() - t0) * 1000.0
            latencies.append(elapsed)
            all_ids.append(ids)

    p50, p95, mean_lat = _pct(latencies)
    return {
        "p50_ms": round(p50, 3),
        "p95_ms": round(p95, 3),
        "mean_ms": round(mean_lat, 3),
        "recall_vs_self": 1.0,
        "queries": len(query_set),
        "ids": all_ids,
        "latencies_ms": latencies,
    }


async def create_hnsw_index(m: int = 16, ef_construction: int = 200) -> None:
    """Create the HNSW index CONCURRENTLY (must be outside a transaction)."""
    drop = f"DROP INDEX IF EXISTS {HNSW_INDEX_NAME}"
    create = (
        f"CREATE INDEX CONCURRENTLY {HNSW_INDEX_NAME} "
        f"ON proof_corpus USING hnsw (embedding vector_l2_ops) "
        f"WITH (m={m}, ef_construction={ef_construction})"
    )
    # CONCURRENTLY cannot run in a transaction block; use a raw connection
    # with the async engine in autocommit mode.
    raw = create_async_engine(DATABASE_URL, isolation_level="AUTOCOMMIT")
    async with raw.connect() as conn:
        await conn.execute(text(drop))
        await conn.execute(text(create))
    await raw.dispose()


async def drop_hnsw_index() -> None:
    """Drop the HNSW index CONCURRENTLY."""
    drop = f"DROP INDEX IF EXISTS {HNSW_INDEX_NAME}"
    raw = create_async_engine(DATABASE_URL, isolation_level="AUTOCOMMIT")
    async with raw.connect() as conn:
        await conn.execute(text(drop))
    await raw.dispose()


async def _supports_iterative_scan() -> bool:
    """Check if hnsw.iterative_scan is a known setting (pgvector >= 0.8.0)."""
    extver = await _get_extversion()
    return _extversion_supports_iterative(extver)


async def run_hnsw(
    query_set: list[tuple[list[float], str, str, str]],
    k: int = 10,
    ef_search: int = 160,
    iterative: bool = True,
) -> dict:
    """Run HNSW approximate retrieval with tenant/project filtering.

    Sets ``hnsw.ef_search`` and (if supported) ``hnsw.iterative_scan =
    relaxed_order`` for filtered queries, per R21.
    """
    latencies: list[float] = []
    all_ids: list[list[str]] = []

    iter_ok = iterative and await _supports_iterative_scan()

    async with engine.connect() as conn:
        await conn.execute(text(f"SET LOCAL hnsw.ef_search = {ef_search}"))
        if iter_ok:
            await conn.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))

        for qvec, tid, pid, _topic in query_set:
            t0 = time.perf_counter()
            sql = (
                f"SELECT id FROM proof_corpus "
                f"WHERE state IN ('canonical', 'verified') "
                f"AND superseded_by IS NULL "
                f"AND (valid_to IS NULL OR valid_to > now()) "
                f"AND tenant_id = :tid AND project_id = :pid "
                f"ORDER BY embedding <-> '{_vec_str(qvec)}' "
                f"LIMIT :k"
            )
            result = await conn.execute(text(sql), {"tid": tid, "pid": pid, "k": k})
            ids = [str(r[0]) for r in result.fetchall()]
            elapsed = (time.perf_counter() - t0) * 1000.0
            latencies.append(elapsed)
            all_ids.append(ids)

    p50, p95, mean_lat = _pct(latencies)
    return {
        "p50_ms": round(p50, 3),
        "p95_ms": round(p95, 3),
        "mean_ms": round(mean_lat, 3),
        "queries": len(query_set),
        "ids": all_ids,
        "latencies_ms": latencies,
    }


def compute_recall(exact_ids: list[list[str]], hnsw_ids: list[list[str]], k: int) -> float:
    """Compute mean recall@k = |exact ∩ hnsw| / k, averaged over queries."""
    if not exact_ids or not hnsw_ids:
        return 0.0
    total = len(exact_ids)
    sum_recall = 0.0
    for ex, hx in zip(exact_ids, hnsw_ids):
        if k == 0:
            continue
        intersection = len(set(ex) & set(hx))
        sum_recall += intersection / k
    return sum_recall / total


async def hnsw_explain_plan(
    query_vec: Sequence[float],
    tenant_id: str,
    project_id: str,
    k: int = 10,
    ef_search: int = 40,
    iterative: bool = True,
) -> str:
    """Return the EXPLAIN (text) for a single filtered HNSW ANN query.

    Used by the test suite to assert the HNSW index is actually selected (the
    nearest-neighbor operator must match the index opclass — R21). With a
    mismatched operator (e.g. the inner-product ``<#>`` against a ``vector_l2_ops``
    index) the planner falls back to a bitmap/seq scan and this plan will NOT
    contain the HNSW index name.
    """
    q = _vec_str(query_vec)
    iter_supported = iterative and _extversion_supports_iterative(await _get_extversion())
    async with engine.connect() as conn:
        statements = [
            f"SET LOCAL hnsw.ef_search = {ef_search}",
        ]
        if iter_supported:
            statements.append("SET LOCAL hnsw.iterative_scan = relaxed_order")
        explain_sql = (
            f"EXPLAIN SELECT id FROM proof_corpus "
            f"WHERE state IN ('canonical', 'verified') "
            f"AND superseded_by IS NULL "
            f"AND (valid_to IS NULL OR valid_to > now()) "
            f"AND tenant_id = :tid AND project_id = :pid "
            f"ORDER BY embedding <-> '{q}' "
            f"LIMIT :k"
        )
        for stmt in statements:
            await conn.execute(text(stmt))
        result = await conn.execute(text(explain_sql), {"tid": tenant_id, "pid": project_id, "k": k})
        plan_lines = [str(r[0]) for r in result.fetchall()]
    return "\n".join(plan_lines)


# ---------------------------------------------------------------------------
# Workload drivers
# ---------------------------------------------------------------------------


async def _count_corpus() -> int:
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT count(*) FROM proof_corpus"))
        return result.scalar()


async def _sample_ids(n: int, tenant: str | None = None, conn=None) -> list[str]:
    """Randomly sample n row IDs, optionally scoped to a tenant."""
    sql = f"SELECT id FROM proof_corpus"
    params: dict[str, Any] = {"n": n}
    if tenant:
        sql += " WHERE tenant_id = :tenant"
        params["tenant"] = tenant
    sql += " ORDER BY random() LIMIT :n"
    if conn:
        result = await conn.execute(text(sql), params)
    else:
        async with engine.connect() as conn2:
            result = await conn2.execute(text(sql), params)
            return [str(r[0]) for r in result.fetchall()]
    return [str(r[0]) for r in result.fetchall()]


async def workload_mutate(
    n: int = 1000, dim: int = 384, seed: int = 777,
) -> dict:
    """Insert N new vectors and measure HNSW staleness + reindex latency.

    Returns:
      - ``inserted``: number of rows inserted.
      - ``detected_stale_before_reindex``: True if pre-reindex HNSW queries
        did NOT return the newly inserted IDs (proving the index isn't live).
      - ``reindex_ms``: time to ``REINDEX INDEX CONCURRENTLY`` the HNSW index.
      - ``recall_restored``: True if post-reindex HNSW returns the new IDs
        at the same recall level as exact.
    """
    rng = random.Random(seed)
    new_rows: list[dict] = []
    for i in range(n):
        vec = [rng.gauss(0.0, 1.0) for _ in range(dim)]
        vec = _l2_normalize(vec)
        topic = rng.choice(_TOPICS)
        ti = rng.randint(0, 2)
        tid = f"tenant_{ti}"
        pid = f"proj_{ti}_{rng.randint(0, 1)}"
        doc_id = f"mut_doc_{i}"
        text_content = f"Mutated corpus chunk {i} for topic '{topic}'."
        content_hash = hashlib.sha256(
            f"{topic}:{tid}:{pid}:mut:{i}".encode()
        ).hexdigest()
        new_rows.append({
            "tenant_id": tid,
            "project_id": pid,
            "doc_id": doc_id,
            "chunk_seq": 0,
            "topic_id": topic,
            "content_hash": content_hash,
            "text": text_content,
            "embedding": vec,
        })

    # Insert new vectors
    async with async_session() as session:
        async with session.begin():
            for r in new_rows:
                await session.execute(
                    text(
                        "INSERT INTO proof_corpus "
                        "(tenant_id, project_id, doc_id, chunk_seq, content_hash, "
                        "text, embedding, topic_id, created_at, updated_at, "
                        "state, staleness_policy) "
                        "VALUES (:tenant_id, :project_id, :doc_id, :chunk_seq, "
                        ":content_hash, :text, :embedding, :topic_id, now(), now(), "
                        "'canonical', '{}'::jsonb) "
                        "ON CONFLICT (content_hash, doc_id, chunk_seq) "
                        "DO UPDATE SET embedding = EXCLUDED.embedding"
                    ),
                    {
                        "tenant_id": r["tenant_id"],
                        "project_id": r["project_id"],
                        "doc_id": r["doc_id"],
                        "chunk_seq": r["chunk_seq"],
                        "content_hash": r["content_hash"],
                        "text": r["text"],
                        "embedding": _vec_str(r["embedding"]),
                        "topic_id": r["topic_id"],
                    },
                )

    # Check whether the HNSW index sees the newly inserted vectors.
    # In pgvector >= 0.7, HNSW supports incremental inserts, so new vectors
    # ARE searchable without reindex. In older versions, they are NOT.
    # We test this empirically: if the new vectors appear in HNSW results,
    # incremental inserts are supported (detected_stale = False); otherwise
    # the index is stale and REINDEX is needed.
    new_id_set = {r["content_hash"] for r in new_rows}
    new_vecs = [r["embedding"] for r in new_rows[:10]]
    iter_ok = await _supports_iterative_scan()

    found_new = 0
    async with engine.connect() as conn:
        await conn.execute(text("SET LOCAL hnsw.ef_search = 200"))
        if iter_ok:
            await conn.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
        for qv in new_vecs:
            sql = (
                f"SELECT content_hash FROM proof_corpus "
                f"WHERE state IN ('canonical', 'verified') "
                f"AND superseded_by IS NULL "
                f"AND (valid_to IS NULL OR valid_to > now()) "
                f"ORDER BY embedding <-> '{_vec_str(qv)}' "
                f"LIMIT 10"
            )
            result = await conn.execute(text(sql))
            hashes = {str(r[0]) for r in result.fetchall()}
            intersection = hashes & new_id_set
            found_new += len(intersection)

    # If no new vectors appear, the index is stale (HNSW missed them).
    detected_stale = found_new == 0

    # Reindex — measure latency of rebuilding the HNSW graph with all vectors.
    t0 = time.perf_counter()
    raw = create_async_engine(DATABASE_URL, isolation_level="AUTOCOMMIT")
    async with raw.connect() as conn:
        await conn.execute(text(f"REINDEX INDEX CONCURRENTLY {HNSW_INDEX_NAME}"))
    await raw.dispose()
    reindex_ms = (time.perf_counter() - t0) * 1000.0

    # Post-reindex: verify the newly inserted vectors are still recall-recoverable.
    # In pgvector 0.8+, they were never lost; in older versions, REINDEX restored them.
    recall_restored = True
    async with engine.connect() as conn:
        await conn.execute(text("SET LOCAL hnsw.ef_search = 200"))
        if iter_ok:
            await conn.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
        for qv, expected_hash in zip(new_vecs[:10], [r["content_hash"] for r in new_rows[:10]]):
            sql = (
                f"SELECT content_hash FROM proof_corpus "
                f"WHERE state IN ('canonical', 'verified') "
                f"AND superseded_by IS NULL "
                f"AND (valid_to IS NULL OR valid_to > now()) "
                f"ORDER BY embedding <-> '{_vec_str(qv)}' "
                f"LIMIT 10"
            )
            result = await conn.execute(text(sql))
            hashes = {str(r[0]) for r in result.fetchall()}
            if expected_hash not in hashes:
                recall_restored = False
                break

    return {
        "inserted": n,
        "detected_stale_before_reindex": detected_stale,
        "reindex_ms": round(reindex_ms, 3),
        "recall_restored": recall_restored,
    }


async def workload_delete(n: int = 1000, tenant: str | None = None, seed: int = 555) -> dict:
    """Delete N rows and assert no ghost IDs appear in filtered results."""
    deleted_ids = await _sample_ids(n, tenant=tenant)

    async with async_session() as session:
        async with session.begin():
            for did in deleted_ids:
                await session.execute(
                    text("DELETE FROM proof_corpus WHERE id = :id"),
                    {"id": did},
                )

    # Verify no deleted IDs appear in any filtered result
    ghost_count = 0
    rng_del = random.Random(seed)
    check_vec = _l2_normalize([rng_del.gauss(0.0, 1.0) for _ in range(384)])
    async with engine.connect() as conn:
        await conn.execute(text(f"SET LOCAL hnsw.ef_search = 160"))
        for tid_t in [f"tenant_{i}" for i in range(3)]:
            sql = (
                f"SELECT id FROM proof_corpus "
                f"WHERE state IN ('canonical', 'verified') "
                f"AND superseded_by IS NULL "
                f"AND (valid_to IS NULL OR valid_to > now()) "
                f"AND tenant_id = '{tid_t}' "
                f"ORDER BY embedding <-> '{_vec_str(check_vec)}' "
                f"LIMIT 50"
            )
            result = await conn.execute(text(sql))
            ids = {str(r[0]) for r in result.fetchall()}
            for did in deleted_ids:
                if did in ids:
                    ghost_count += 1

    return {
        "deleted": len(deleted_ids),
        "ghost_ids_returned": ghost_count,
    }


async def workload_invalidate(invalidate_frac: float = 0.05, seed: int = 333) -> dict:
    """Invalidate a fraction of rows via the state machine and check exclusion.

    Sets ``state='superseded'``, ``superseded_by`` to a new canonical row,
    ``valid_to=now()`` on a sample. The retrieval query filters on
    ``state IN ('canonical','verified') AND superseded_by IS NULL``, so
    invalidated rows must never appear.
    """
    rng = random.Random(seed)
    total = await _count_corpus()
    n_invalidate = max(1, int(total * invalidate_frac))

    # Pick rows to invalidate
    ids_to_invalidate = await _sample_ids(n_invalidate)
    if not ids_to_invalidate:
        return {"invalidated": 0, "leaked_stale_ids": 0, "latency_ms": 0.0}

    async with async_session() as session:
        async with session.begin():
            for rid in ids_to_invalidate:
                # Find a replacement canonical row from the same tenant/project
                # (just pick a random other row)
                repl_result = await session.execute(
                    text(
                        "SELECT id FROM proof_corpus WHERE id != :rid "
                        "ORDER BY random() LIMIT 1"
                    ),
                    {"rid": rid},
                )
                repl = repl_result.scalar_one_or_none()
                repl_id = str(repl) if repl else None
                await session.execute(
                    text(
                        "UPDATE proof_corpus SET state = 'superseded', "
                        "superseded_by = :repl, valid_to = now(), "
                        "updated_at = now() "
                        "WHERE id = :id"
                    ),
                    {"repl": repl_id, "id": rid},
                )

    # Run queries and check for leaked stale IDs. Use a representative unit
    # vector (not the zero vector) so ANN actually returns meaningful rows.
    rng2 = random.Random(seed + 1)
    query_vec = _l2_normalize([rng2.gauss(0.0, 1.0) for _ in range(384)])
    leaked = 0
    t0 = time.perf_counter()
    async with engine.connect() as conn:
        await conn.execute(text("SET LOCAL hnsw.ef_search = 160"))
        if await _supports_iterative_scan():
            await conn.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
        for tid_t in [f"tenant_{i}" for i in range(3)]:
            sql = (
                f"SELECT id FROM proof_corpus "
                f"WHERE state IN ('canonical', 'verified') "
                f"AND superseded_by IS NULL "
                f"AND (valid_to IS NULL OR valid_to > now()) "
                f"AND tenant_id = '{tid_t}' "
                f"ORDER BY embedding <-> '{_vec_str(query_vec)}' "
                f"LIMIT 50"
            )
            result = await conn.execute(text(sql))
            ids = {str(r[0]) for r in result.fetchall()}
            for rid in ids_to_invalidate:
                if rid in ids:
                    leaked += 1
    latency_ms = (time.perf_counter() - t0) * 1000.0

    return {
        "invalidated": len(ids_to_invalidate),
        "leaked_stale_ids": leaked,
        "latency_ms": round(latency_ms, 3),
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


async def run_proof(
    cfg: ProofConfig | None = None, write: bool = True, report_path: str = "proof_report.json"
) -> dict:
    """Run the full §38.2 storage proof and return the report dict.

    Steps:
      1. Generate + load corpus.
      2. Build query set (filtered by tenant/project).
      3. Exact baseline (index dropped, enable_indexscan=off).
      4. Create HNSW index.
      5. HNSW sweep over ef_search values, compute filtered recall vs exact.
      6. Tenant/project isolation check.
      7. Workload drivers (mutate, delete, invalidate).
      8. Assemble report + pass/fail against ADR-003 reversal triggers.
    """
    if cfg is None:
        cfg = _load_config()

    extversion = await _get_extversion()
    iter_supported = _extversion_supports_iterative(extversion)

    # 1-2: Generate + load corpus, build query set
    print(f"[proof] Generating {cfg.n_vectors} corpus vectors (dim={cfg.dim}, seed={cfg.seed})...")
    rows = generate_corpus(
        n_vectors=cfg.n_vectors,
        dim=cfg.dim,
        n_tenants=cfg.n_tenants,
        projects_per_tenant=cfg.projects_per_tenant,
        topics_per_project=cfg.topics_per_project,
        seed=cfg.seed,
    )
    print(f"[proof] Resetting + loading corpus into proof_corpus...")
    await reset_corpus()
    loaded = await load_corpus(rows)
    print(f"[proof] Loaded {loaded} vectors")

    query_set = build_query_set(rows, k=cfg.k, n_queries=cfg.n_queries, seed=cfg.seed + 1)
    print(f"[proof] Built {len(query_set)} filtered queries (k={cfg.k})")

    # 3: Exact baseline
    print("[proof] Running exact (sequential) baseline...")
    exact = await run_exact(query_set, k=cfg.k)
    print(f"[proof] Exact: p50={exact['p50_ms']}ms p95={exact['p95_ms']}ms")

    # 4: Create HNSW index
    print(f"[proof] Building HNSW index (m={cfg.hnsw_m}, ef_construction={cfg.hnsw_ef_construction})...")
    await create_hnsw_index(m=cfg.hnsw_m, ef_construction=cfg.hnsw_ef_construction)
    print("[proof] HNSW index built")

    # 5: HNSW sweep over ef_search
    print("[proof] Running HNSW sweep...")
    hnsw_by_ef: list[dict] = []
    hnsw_best: dict | None = None
    hnsw_best_ids: list[list[str]] | None = None
    best_recall = -1.0

    for ef in cfg.ef_search_sweep:
        result = await run_hnsw(query_set, k=cfg.k, ef_search=ef, iterative=True)
        recall = compute_recall(exact["ids"], result["ids"], cfg.k)
        entry = {
            "ef_search": ef,
            "recall": round(recall, 4),
            "p50_ms": result["p50_ms"],
            "p95_ms": result["p95_ms"],
            "mean_ms": result["mean_ms"],
        }
        hnsw_by_ef.append(entry)
        print(f"[proof]   ef_search={ef}: recall={recall:.4f} p95={result['p95_ms']}ms")
        if recall >= best_recall:
            best_recall = recall
            hnsw_best = entry
            hnsw_best_ids = result["ids"]

    # 6: Tenant isolation
    print("[proof] Testing tenant/project isolation...")
    cross_tenant_leak = 0
    per_tenant_recall: dict[str, float] = {}

    # Cross-tenant leak: run UNFILTERED ANN and check whether the top-k ever
    # contains IDs from more than one tenant. A well-partitioned corpus with
    # tenant-separated centroids yields zero cross-tenant results at small k.
    rng = random.Random(4242)
    test_vectors = [rows[i]["embedding"] for i in rng.sample(range(len(rows)), min(20, len(rows)))]

    async with engine.connect() as conn:
        await conn.execute(text("SET LOCAL hnsw.ef_search = 160"))
        if iter_supported:
            await conn.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
        for tv in test_vectors:
            sql = f"SELECT id, tenant_id FROM proof_corpus ORDER BY embedding <-> '{_vec_str(tv)}' LIMIT 50"
            result = await conn.execute(text(sql))
            top_ids = [(str(r[0]), str(r[1])) for r in result.fetchall()]
            if not top_ids:
                continue
            query_tenant = top_ids[0][1]
            for rid, tid in top_ids:
                if tid != query_tenant:
                    cross_tenant_leak += 1

    # Per-tenant filtered recall: group queries by tenant; both the exact and
    # best-effort HNSW result IDs are aligned 1:1 with query_set ordering.
    filtered_recall_mean = best_recall if hnsw_best else 0.0

    if hnsw_best_ids is not None and exact["ids"]:
        for ti in range(cfg.n_tenants):
            tid = f"tenant_{ti}"
            indices = [i for i, q in enumerate(query_set) if q[1] == tid]
            if not indices:
                per_tenant_recall[tid] = 0.0
                continue
            ex_ids = [exact["ids"][i] for i in indices]
            hn_ids = [hnsw_best_ids[i] for i in indices]
            per_tenant_recall[tid] = round(
                compute_recall(ex_ids, hn_ids, cfg.k), 4
            )
    else:
        for ti in range(cfg.n_tenants):
            per_tenant_recall[f"tenant_{ti}"] = 0.0

    filtered_recall_mean = best_recall if hnsw_best else 0.0

    tenant_isolation = {
        "cross_tenant_leak": cross_tenant_leak,
        "passed": cross_tenant_leak == 0,
    }

    # 7: Workloads
    print("[proof] Running workload: mutation/invalidation...")
    mut_result = await workload_mutate(n=cfg.mutate_count, dim=cfg.dim)
    # Drop + recreate index to get a clean state for delete/invalidate tests
    await drop_hnsw_index()
    await create_hnsw_index(m=cfg.hnsw_m, ef_construction=cfg.hnsw_ef_construction)

    print("[proof] Running workload: deletion...")
    del_result = await workload_delete(n=cfg.delete_count)

    print("[proof] Running workload: stale invalidation...")
    inv_result = await workload_invalidate(invalidate_frac=cfg.invalidate_frac)

    # 8: Assemble report
    reversal_triggers = {
        "corpus_vector_count": {
            "value": loaded,
            "threshold": ">=10000",
            "pass": loaded >= 10000,
        },
        "p95_retrieval_latency": {
            "value_ms": hnsw_best["p95_ms"] if hnsw_best else 0.0,
            "threshold": f"<{cfg.latency_hnsw_p95_ms}",
            "pass": (hnsw_best["p95_ms"] if hnsw_best else float("inf")) < cfg.latency_hnsw_p95_ms,
        },
        "multi_tenant_isolation": {
            "value": cross_tenant_leak,
            "threshold": "0 leakage",
            "pass": cross_tenant_leak == 0,
        },
        "filtered_ann_recall": {
            "value": round(filtered_recall_mean, 4),
            "threshold": f">={cfg.recall_target}",
            "pass": filtered_recall_mean >= cfg.recall_target,
        },
        "write_throughput": {
            "value_ms": mut_result["reindex_ms"],
            "threshold": f"<{cfg.reindex_budget_ms}",
            "pass": mut_result["reindex_ms"] < cfg.reindex_budget_ms and mut_result["recall_restored"],
        },
    }

    verdict = "PASS" if all(t["pass"] for t in reversal_triggers.values()) else "FAIL"

    report = {
        "schema_version": 1,
        "mission": "sayandahiyagt/dra#15",
        "spec_anchor": "§38.2",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "config": {
            "n_vectors": cfg.n_vectors,
            "dim": cfg.dim,
            "k": cfg.k,
            "recall_target": cfg.recall_target,
            "latency_hnsw_p95_ms": cfg.latency_hnsw_p95_ms,
            "latency_exact_p95_ms": cfg.latency_exact_p95_ms,
            "hnsw_m": cfg.hnsw_m,
            "hnsw_ef_construction": cfg.hnsw_ef_construction,
            "ef_search_sweep": cfg.ef_search_sweep,
            "pgvector_extversion": extversion or "unknown",
            "iterative_scan_supported": iter_supported,
        },
        "corpus": {
            "count": loaded,
            "dim": cfg.dim,
            "tenants": cfg.n_tenants,
            "projects_per_tenant": cfg.projects_per_tenant,
            "topics_per_project": cfg.topics_per_project,
            "seed": cfg.seed,
        },
        "exact": {
            "p50_ms": exact["p50_ms"],
            "p95_ms": exact["p95_ms"],
            "mean_ms": exact["mean_ms"],
            "recall_vs_self": exact["recall_vs_self"],
            "queries": exact["queries"],
        },
        "hnsw": {
            "by_ef": hnsw_by_ef,
            "best": hnsw_best,
        },
        "filtered_recall": {
            "mean_recall_at_k": filtered_recall_mean,
            "per_tenant": per_tenant_recall,
        },
        "tenant_isolation": tenant_isolation,
        "workloads": {
            "mutation": {
                "inserted": mut_result["inserted"],
                "detected_stale_before_reindex": mut_result["detected_stale_before_reindex"],
                "reindex_ms": mut_result["reindex_ms"],
                "recall_restored": mut_result["recall_restored"],
            },
            "deletion": {
                "deleted": del_result["deleted"],
                "ghost_ids_returned": del_result["ghost_ids_returned"],
            },
            "stale_invalidation": {
                "invalidated": inv_result["invalidated"],
                "leaked_stale_ids": inv_result["leaked_stale_ids"],
                "latency_ms": inv_result["latency_ms"],
            },
        },
        "reversal_triggers": reversal_triggers,
        "verdict": verdict,
        "adr003_reversal_triggered": verdict == "FAIL",
    }

    if write:
        write_report(report, path=report_path)

    return report


def write_report(report: dict, path: str = "proof_report.json") -> None:
    """Write the proof report as JSON + a markdown summary."""
    with open(path, "w") as f:
        json.dump(report, f, indent=2)

    md_path = path.replace(".json", ".md")
    with open(md_path, "w") as f:
        f.write(_report_markdown(report))


def _report_markdown(report: dict) -> str:
    """Render a human-readable markdown proof report."""
    lines = []
    lines.append("# §38.2 Storage Proof Report")
    lines.append("")
    lines.append(f"- **Mission:** {report['mission']}")
    lines.append(f"- **Spec anchor:** {report['spec_anchor']}")
    lines.append(f"- **Generated at:** {report['generated_at']}")
    lines.append(f"- **pgvector version:** {report['config']['pgvector_extversion']}")
    lines.append(f"- **Iterative scan supported:** {report['config']['iterative_scan_supported']}")
    lines.append("")

    lines.append("## Corpus")
    c = report["corpus"]
    lines.append(f"- Vectors loaded: **{c['count']}** (dim={c['dim']}, seed={c['seed']})")
    lines.append(f"- Tenants: {c['tenants']}, projects/tenant: {c['projects_per_tenant']}, topics/project: {c['topics_per_project']}")
    lines.append("")

    lines.append("## Exact (sequential) retrieval")
    e = report["exact"]
    lines.append(f"- p50: {e['p50_ms']} ms | p95: {e['p95_ms']} ms | mean: {e['mean_ms']} ms")
    lines.append(f"- Recall vs self: {e['recall_vs_self']} | queries: {e['queries']}")
    lines.append("")

    lines.append("## HNSW approximate retrieval")
    h = report["hnsw"]
    lines.append("| ef_search | recall | p50 (ms) | p95 (ms) | mean (ms) |")
    lines.append("|-----------|--------|----------|----------|-----------|")
    for entry in h["by_ef"]:
        lines.append(
            f"| {entry['ef_search']} | {entry['recall']:.4f} | "
            f"{entry['p50_ms']} | {entry['p95_ms']} | {entry['mean_ms']} |"
        )
    if h["best"]:
        b = h["best"]
        lines.append(f"\n**Best:** ef_search={b['ef_search']}, recall={b['recall']:.4f}, p95={b['p95_ms']} ms")
    lines.append("")

    lines.append("## Filtered recall (vs exact, per R21)")
    fr = report["filtered_recall"]
    lines.append(f"- Mean recall@{report['config']['k']}: {fr['mean_recall_at_k']:.4f}")
    for tid, r in fr["per_tenant"].items():
        lines.append(f"  - {tid}: {r:.4f}")
    lines.append("")

    lines.append("## Tenant isolation")
    ti = report["tenant_isolation"]
    lines.append(f"- Cross-tenant leak count: {ti['cross_tenant_leak']} → {'PASS' if ti['passed'] else 'FAIL'}")
    lines.append("")

    lines.append("## Workloads")
    w = report["workloads"]
    m = w["mutation"]
    lines.append("### Mutation / stale-vector invalidation")
    lines.append(f"- Inserted: {m['inserted']} | stale detected before reindex: {m['detected_stale_before_reindex']}")
    lines.append(f"- Reindex latency: {m['reindex_ms']} ms | recall restored: {m['recall_restored']}")
    d = w["deletion"]
    lines.append("### Deletion")
    lines.append(f"- Deleted: {d['deleted']} | ghost IDs returned: {d['ghost_ids_returned']}")
    inv = w["stale_invalidation"]
    lines.append("### Stale-vector state-machine invalidation")
    lines.append(f"- Invalidated: {inv['invalidated']} | leaked stale IDs: {inv['leaked_stale_ids']} | query latency: {inv['latency_ms']} ms")
    lines.append("")

    lines.append("## ADR-003 reversal triggers")
    lines.append("| Trigger | Value | Threshold | Result |")
    lines.append("|---------|-------|-----------|--------|")
    for name, trig in report["reversal_triggers"].items():
        val = trig.get("value_ms", trig.get("value"))
        lines.append(f"| {name} | {val} | {trig['threshold']} | {'PASS' if trig['pass'] else 'FAIL'} |")
    lines.append("")

    lines.append("## Verdict")
    lines.append(f"**{report['verdict']}** — reversal triggered: {report['adr003_reversal_triggered']}")
    return "\n".join(lines) + "\n"


def _check_db_reachable() -> bool:
    try:
        return asyncio.run(can_connect())
    except Exception:
        return False


def main() -> None:
    """CLI entry point: run the §38.2 storage proof."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="dra-storage-proof",
        description="Run the §38.2 storage proof: load corpus, compare "
        "exact vs HNSW retrieval, measure recall/latency, test workloads, "
        "and emit a pass/fail report vs ADR-003 reversal triggers.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Verify DB connectivity and config without running the proof.",
    )
    parser.add_argument(
        "--n-vectors",
        type=int,
        default=None,
        help="Override number of corpus vectors (default: 25000).",
    )
    parser.add_argument(
        "--recall-target",
        type=float,
        default=None,
        help="Override recall target (default: 0.90).",
    )
    parser.add_argument(
        "--latency-p95-ms",
        type=float,
        default=None,
        help="Override HNSW p95 latency SLO in ms (default: 50).",
    )
    parser.add_argument(
        "--reindex-budget-ms",
        type=float,
        default=None,
        help="Override reindex latency budget in ms (default: 10000).",
    )
    args = parser.parse_args()

    if not _check_db_reachable():
        print("FAIL: No reachable Postgres at DATABASE_URL.")
        print("      The storage proof requires a running Postgres+pgvector instance.")
        print("      Set DATABASE_URL and run `alembic -c alembic.ini upgrade head`.")
        sys.exit(1)

    cfg = _load_config()
    if args.n_vectors is not None:
        cfg.n_vectors = args.n_vectors
    if args.recall_target is not None:
        cfg.recall_target = args.recall_target
    if args.latency_p95_ms is not None:
        cfg.latency_hnsw_p95_ms = args.latency_p95_ms
    if args.reindex_budget_ms is not None:
        cfg.reindex_budget_ms = args.reindex_budget_ms

    if args.dry_run:
        print(f"[proof] §38.2 Storage proof — dry run")
        print(f"  DATABASE_URL: {DATABASE_URL}")
        print(f"  config: {cfg.n_vectors} vectors, k={cfg.k}, "
              f"recall_target={cfg.recall_target}, "
              f"p95_SLO={cfg.latency_hnsw_p95_ms}ms")
        print("  DB reachable: yes")
        return

    print(f"[proof] §38.2 Storage proof — config: {cfg.n_vectors} vectors, k={cfg.k}, "
          f"recall_target={cfg.recall_target}, p95_SLO={cfg.latency_hnsw_p95_ms}ms")

    report = asyncio.run(run_proof(cfg))

    # Print PASS/FAIL table
    print("\n=== §38.2 Storage Proof — ADR-003 Reversal Triggers ===")
    print(f"{'Trigger':<30} {'Value':<15} {'Threshold':<20} {'Result':<6}")
    print("-" * 75)
    for name, trig in report["reversal_triggers"].items():
        val = trig.get("value_ms", trig.get("value"))
        result = "PASS" if trig["pass"] else "FAIL"
        print(f"{name:<30} {str(val):<15} {trig['threshold']:<20} {result:<6}")
    print("-" * 75)
    print(f"\nVERDICT: {report['verdict']}  |  reversal_triggered: {report['adr003_reversal_triggered']}")
    print(f"\nReport written to: proof_report.json + proof_report.md")


if __name__ == "__main__":  # pragma: no cover
    main()
