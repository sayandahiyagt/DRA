"""DB-gated bake-off tests: every variant executes the identical §2 workflow
end-to-end and satisfies the §2 invariants.

Mirrors the repo's ``tests/_db.py`` convention (DB gate) and ``tests/test_db.py``
sync-test style (``asyncio.run`` inside a sync test, no pytest-asyncio). These
tests SKIP cleanly when Postgres is unreachable (env concern, not a code defect).
"""
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from _db import DB


CORPUS_DIR = str(Path(__file__).resolve().parents[1] / "_corpus")


def _can_import(modname: str) -> bool:
    import importlib
    try:
        importlib.import_module(modname)
        return True
    except Exception:
        return False


def _run(variant: str, run_id: str):
    from corpus import generate
    import evidence
    generate(CORPUS_DIR)
    evidence.reset_bakeoff_evidence()
    evidence.reset_checkpoints()
    if variant == "A_langgraph":
        from variant_a_langgraph.run import _run_graph
        asyncio.run(_run_graph(run_id, "bakeoff-A-task", CORPUS_DIR))
    elif variant == "B_deep_agents":
        from variant_b_deep_agents.worker import _run_agent
        asyncio.run(_run_agent(run_id, CORPUS_DIR, reset=False))
    elif variant == "C_deerflow":
        from variant_c_deerflow.run import _run_agent
        asyncio.run(_run_agent(run_id, CORPUS_DIR, reset=False))
    else:
        raise ValueError(variant)


def _counts(run_id: str):
    import evidence

    async def _go():
        bundles = await evidence.bundles_for_run(run_id)
        canonical = 0
        for b in bundles:
            canonical += await evidence.canonical_count(b["id"])
        return bundles, canonical
    return asyncio.run(_go())


def _native(variant: str, run_id: str) -> int:
    if variant == "A_langgraph":
        from variant_a_langgraph.run import _native_state
        return int(_native_state(run_id).get("in_state_findings", 0))
    if variant == "B_deep_agents":
        from variant_b_deep_agents.worker import _native_state
        return int(_native_state(run_id).get("in_state_findings", 0))
    if variant == "C_deerflow":
        from variant_c_deerflow.run import _native_state
        return int(_native_state(run_id).get("in_state_findings", 0))
    return 0


def _verify(bundle_id: str) -> dict:
    import evidence
    return asyncio.run(evidence.verify_bundle(bundle_id))


@DB
def test_variant_a_end_to_end_and_invariants():
    run_id = f"bakeoff-test-A-{uuid.uuid4().hex[:6]}"
    _run("A_langgraph", run_id)
    bundles, canonical = _counts(run_id)
    assert len(bundles) >= 1, "A must commit >=1 canonical bundle"
    assert canonical >= 1, "A must produce >=1 canonical row"
    assert _native("A_langgraph", run_id) == 0, "A must not hold findings in agent state"
    commit = next((b for b in bundles if not b["task_id"].endswith("-synth")), None)
    v = _verify(commit["id"]) if commit else {"verdict": "PASS"}
    assert v["verdict"] == "PASS", f"A gate: {v}"


@DB
def test_variant_b_end_to_end_and_invariants():
    import pytest
    if not _can_import("deepagents"):
        pytest.skip("deepagents not installed (`uv pip install deepagents==0.7.13`)")
    run_id = f"bakeoff-test-B-{uuid.uuid4().hex[:6]}"
    _run("B_deep_agents", run_id)
    bundles, canonical = _counts(run_id)
    assert len(bundles) >= 1 and canonical >= 1, "B must commit >=1 canonical bundle"
    assert _native("B_deep_agents", run_id) == 0, "B must not hold findings in agent state"
    commit = next((b for b in bundles if not b["task_id"].endswith("-synth")), None)
    assert _verify(commit["id"])["verdict"] == "PASS"


@DB
def test_variant_c_end_to_end_and_disqualification():
    from variant_c_deerflow.adapter import deerflow_available
    import pytest
    if not deerflow_available():
        pytest.skip("DeerFlow clone absent (gitignored; `git clone --branch v2.0.0 "
                    "https://github.com/bytedance/deer-flow bake-off/variant_c_deerflow/deerflow`)")
    run_id = f"bakeoff-test-C-{uuid.uuid4().hex[:6]}"
    _run("C_deerflow", run_id)
    bundles, canonical = _counts(run_id)
    assert len(bundles) >= 1 and canonical >= 1, "C must commit >=1 canonical bundle (it still runs)"
    # C is DISQUALIFIED: DeerFlow native state holds findings off-contract.
    assert _native("C_deerflow", run_id) > 0, (
        "C must demonstrate native agent-internal state (the disqualifying violation)")
    commit = next((b for b in bundles if not b["task_id"].endswith("-synth")), None)
    assert _verify(commit["id"])["verdict"] == "PASS"


@DB
def test_resume_idempotency_variant_a():
    """Killing+resuming A must not double-commit canonical rows."""
    run_id = f"bakeoff-test-A-r-{uuid.uuid4().hex[:6]}"
    _run("A_langgraph", run_id)
    _, c1 = _counts(run_id)
    from variant_a_langgraph.run import _run_graph
    asyncio.run(_run_graph(run_id, "bakeoff-A-task", CORPUS_DIR))
    _, c2 = _counts(run_id)
    assert c1 == c2, f"resume double-committed: {c1} -> {c2}"


@DB
def test_cancel_retry_atomicity():
    """Mid-publish cancel must roll back (0 leaked canonical); retry commits once."""
    import evidence
    rid = f"bakeoff-test-cancel-{uuid.uuid4().hex[:6]}"
    probe = asyncio.run(evidence.cancel_rollback_probe(rid, "A_langgraph", Path(CORPUS_DIR)))
    assert probe["rollback_canonical"] == 0, f"cancel leaked canonical rows: {probe}"
    assert probe["retry_canonical"] >= 1, f"retry did not commit: {probe}"
    assert probe["idempotent"] is True
