"""Variant B — LangGraph + Deep-Agents worker.

``create_deep_agent`` with a deterministic no-LLM fake model that calls the
shared lifecycle tools (commit/verify/synthesize). Each tool routes findings
through ``dra.publish`` and returns a short summary, so the DeepAgents
``files`` filesystem channel never holds a finding off-contract
(``in_state_findings`` must be 0 — B is the clean alternative to A).

deep-agents import is LAZY (gated): ``uv pip install deepagents`` populates the
sandbox venv but is NOT promoted to ``uv.lock``, so a default ``uv sync`` is
unaffected by Variant B.
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
    from lifecycle_tools import count_native_state_files
    return count_native_state_files(run_id)


async def _run_agent(run_id: str, corpus_dir: str, reset: bool = True):
    from deepagents import create_deep_agent  # LAZY: gated optional dep
    from langchain_core.messages import HumanMessage
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from checkpointer import postgres_conninfo
    from lifecycle_tools import _fake_model_for, _make_tools, set_corpus_dir
    import evidence

    set_corpus_dir(corpus_dir)
    if reset:
        await evidence._reset_checkpoints_async()
        await evidence._reset_bakeoff_evidence_async(evidence.RUN_PREFIX)
    model = _fake_model_for()
    tools = _make_tools(run_id, corpus_dir, "B_deep_agents")
    tool_names = ["commit_evidence", "verify_evidence", "synthesize_evidence"]

    async def _make_callable(name):
        return tools[name]

    # build a list of BaseTool-wrapped callables by name
    from langchain_core.tools import StructuredTool
    tool_objs = [StructuredTool.from_function(func=tools[n], name=n,
                                          description=f"bake-off §2 step: {n}")
                 for n in tool_names]

    prompt = (
        "You are the §38.1 bake-off Variant B (DeepAgents) worker. Run the "
        "recon->fan-out->investigate->commit->verify->synthesize workflow for the "
        "tiny corpus by calling the lifecycle tools in order: commit_evidence, "
        "then verify_evidence, then synthesize_evidence. Route all findings "
        "through dra.publish."
    )
    checkpointer = AsyncPostgresSaver.from_conn_string(postgres_conninfo())
    async with checkpointer as ckpt:
        await ckpt.setup()
        agent = create_deep_agent(
            model=model, tools=tool_objs, system_prompt=prompt, checkpointer=ckpt,
        )
        input_state = {"messages": [HumanMessage(content="Run the §38.1 bake-off workflow.")]}
        await agent.ainvoke(input_state, config={"configurable": {"thread_id": run_id}})


def run_workflow(run_id: str | None = None, corpus_dir: str | None = None) -> dict:
    _bootstrap()
    import evidence
    from corpus import generate

    root = _bootstrap()
    run_id = run_id or f"bakeoff-B-{uuid.uuid4().hex[:8]}"
    if corpus_dir is None:
        corpus_dir = str(root / "bake-off" / "_corpus")
    generate(corpus_dir)
    t0 = time.perf_counter()
    asyncio.run(_run_agent(run_id, corpus_dir, reset=True))
    native = _native_state(run_id)
    bundles = asyncio.run(evidence.bundles_for_run(run_id))
    commit_canon = sum(asyncio.run(evidence.canonical_count(b["id"])) for b in bundles)
    return {
        "run_id": run_id, "variant": "B_deep_agents",
        "native_state": native,
        "in_state_findings": native.get("files_entries", 0),
        "canonical_total": commit_canon,
        "bundles": [b["id"] for b in bundles],
        "elapsed_ms": (time.perf_counter() - t0) * 1000,
    }


if __name__ == "__main__":  # pragma: no cover
    print(json.dumps(run_workflow(), indent=2, default=str))
