"""Variant A runner: LangGraph StateGraph + AsyncPostgresSaver.

Non-canonical prototype. Run::

    python bake-off/variant_a_langgraph/run.py            # one run, print receipt
    python bake-off/variant_a_langgraph/run.py --probe    # + resume/cancel probes

Routes all findings through ``dra.publish``; the checkpoint holds control state
only (ADR-002), so the native-state measurement counts finding text absent from
the checkpoint blob (``in_state_findings`` must be 0 for the clean baseline).
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
import uuid
from pathlib import Path


def _bootstrap() -> Path:
    root = Path(__file__).resolve()
    for p in root.parents:
        if (p / "pyproject.toml").exists():
            root = p
            break
    for p in (str(root / "src"), str(root / "bake-off")):
        if p not in sys.path:
            sys.path.insert(0, p)
    return root


def _native_state(run_id: str) -> dict:
    """Variant A checkpoints control state only — no finding text in native
    evidence channels (files/thread_data/artifacts/...). This is the §38.1
    'evidence-integration' invariant for the clean baseline."""
    from lifecycle_tools import count_in_state_findings
    return {"kind": "langgraph_control_state",
            "in_state_findings": count_in_state_findings(run_id),
            "files": 0, "thread_data": 0, "artifacts": 0}


async def _run_graph(run_id, task_id, corpus_dir):
    from variant_a_langgraph.graph import build_graph
    from checkpointer import postgres_conninfo
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    initial = {"run_id": run_id, "variant": "A_langgraph",
               "corpus_dir": corpus_dir, "task_id": task_id}
    async with AsyncPostgresSaver.from_conn_string(postgres_conninfo()) as ckpt:
        await ckpt.setup()
        graph = build_graph().compile(checkpointer=ckpt)
        await graph.ainvoke(initial, config={"configurable": {"thread_id": run_id}})


def run_workflow(run_id: str | None = None, corpus_dir: str | None = None) -> dict:
    _bootstrap()
    import evidence
    from corpus import generate

    root = _bootstrap()
    run_id = run_id or f"bakeoff-A-{uuid.uuid4().hex[:8]}"
    task_id = f"bakeoff-A-task"
    if corpus_dir is None:
        corpus_dir = str(root / "bake-off" / "_corpus")
    generate(corpus_dir)

    evidence.reset_bakeoff_evidence()
    evidence.reset_checkpoints()
    t0 = time.perf_counter()
    asyncio.run(_run_graph(run_id, task_id, corpus_dir))
    native = _native_state(run_id)
    bundles = asyncio.run(evidence.bundles_for_run(run_id))
    commit_canon = sum(asyncio.run(evidence.canonical_count(b["id"])) for b in bundles)
    return {
        "run_id": run_id, "variant": "A_langgraph",
        "native_state": native,
        "canonical_total": commit_canon,
        "bundles": [b["id"] for b in bundles],
        "elapsed_ms": (time.perf_counter() - t0) * 1000,
    }


if __name__ == "__main__":  # pragma: no cover
    print(json.dumps(run_workflow(), indent=2, default=str))
