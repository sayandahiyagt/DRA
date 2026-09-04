"""Variant C runner: DeerFlow-derived agent + fake model + AsyncPostgresSaver.

Non-canonical prototype. Run::

    python bake-off/variant_c_deerflow/run.py

The DeepFlow agent runs the §2 lifecycle via the shared tools; DeerFlow's native
``ThreadState`` materialises tool results into ``thread_data``/``artifacts``
(enabled by the default ``sandbox=True`` feature). Any non-empty native channel
is the §38.1 disqualifying evidence-integration violation.
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
    from lifecycle_tools import count_native_state_deerflow
    return count_native_state_deerflow(run_id)


async def _run_agent(run_id: str, corpus_dir: str, reset: bool = True):
    from variant_c_deerflow.adapter import create_agent, deerflow_available
    from langchain_core.messages import HumanMessage
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from checkpointer import postgres_conninfo
    from lifecycle_tools import _fake_model_for, _make_tools, set_corpus_dir
    import evidence

    if not deerflow_available():
        raise RuntimeError(
            "DeerFlow clone not present: git clone --depth 1 --branch v2.0.0 "
            "https://github.com/bytedance/deer-flow "
            "bake-off/variant_c_deerflow/deerflow")

    set_corpus_dir(corpus_dir)
    if reset:
        await evidence._reset_checkpoints_async()
        await evidence._reset_bakeoff_evidence_async(evidence.RUN_PREFIX)
    model = _fake_model_for()
    tools = _make_tools(run_id, corpus_dir, "C_deerflow")
    tool_names = ["commit_evidence", "verify_evidence", "synthesize_evidence"]

    from langchain_core.tools import StructuredTool
    tool_objs = [StructuredTool.from_function(func=tools[n], name=n,
                                              description=f"bake-off §2 step: {n}")
                 for n in tool_names]

    checkpointer = AsyncPostgresSaver.from_conn_string(postgres_conninfo())
    async with checkpointer as ckpt:
        await ckpt.setup()
        agent = create_agent(model=model, tools=tool_objs, checkpointer=ckpt)
        input_state = {"messages": [HumanMessage(content="Run the §38.1 bake-off workflow.")]}
        # ThreadDataMiddleware reads runtime.context (langgraph derives it from
        # config.configurable); pass thread_id + run_id so before_agent does not
        # crash on a None context (DEERFLOW line 110 uses runtime.context.get).
        cfg = {"configurable": {"thread_id": run_id, "run_id": run_id}}
        await agent.ainvoke(input_state, config=cfg)


def run_workflow(run_id: str | None = None, corpus_dir: str | None = None) -> dict:
    _bootstrap()
    import evidence
    from corpus import generate

    root = _bootstrap()
    run_id = run_id or f"bakeoff-C-{uuid.uuid4().hex[:8]}"
    if corpus_dir is None:
        corpus_dir = str(root / "bake-off" / "_corpus")
    generate(corpus_dir)
    t0 = time.perf_counter()
    try:
        asyncio.run(_run_agent(run_id, corpus_dir, reset=True))
        native = _native_state(run_id)
        bundles = asyncio.run(evidence.bundles_for_run(run_id))
        canonical = sum(asyncio.run(evidence.canonical_count(b["id"])) for b in bundles)
        in_state = sum(v for k, v in native.items() if k in
                       ("thread_data", "artifacts", "delegations", "skill_context"))
        ok = True
    except Exception as exc:
        native = {"error": f"{exc.__class__.__name__}: {exc}", "has_files_channel": False}
        canonical, in_state, bundles = 0, 0, []
        ok = False
    return {"run_id": run_id, "variant": "C_deerflow", "native_state": native,
            "in_state_findings": in_state, "canonical_total": canonical,
            "bundles": [b["id"] for b in bundles], "ok": ok,
            "elapsed_ms": (time.perf_counter() - t0) * 1000}


if __name__ == "__main__":  # pragma: no cover
    print(json.dumps(run_workflow(), indent=2, default=str))
