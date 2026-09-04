"""Variant A — bare LangGraph (the stack this repo already depends on).

A real ``langgraph.graph.StateGraph`` over the §2 lifecycle with genuine
parallel ``Send`` fan-out for the three investigation workers, checkpointed via
``AsyncPostgresSaver`` (thread_id = run_id). Evidence is staged through the
shared ``evidence._stage_*`` helpers (which call ``dra.publish``), so the
dra.publish evidence-graph bundle/commit contract stays the source of truth —
checkpoint state holds only control data (ADR-002).
"""
from __future__ import annotations

import asyncio
import operator
import time
import uuid
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, StateGraph
from langgraph.types import Send

import evidence


ACTOR = {
    "kind": "model",
    "name": "langgraph-variant-a",
    "version": "1.0",
    "external_id": "bakeoff:A_langgraph",
}


def _merge(d: dict[str, Any], u: dict[str, Any]) -> dict[str, Any]:
    out = dict(d)
    for k, v in u.items():
        out[k] = v
    return out


class VariantAState(TypedDict, total=False):
    run_id: str
    variant: str
    corpus_dir: str
    actor: Annotated[dict[str, Any], _merge]
    task_id: str
    bundle_id: str
    activities: Annotated[dict[str, Any], _merge]
    recon: dict[str, Any]
    analysis: dict[str, Any]
    worker_findings: list[dict[str, Any]]
    fanout_results: Annotated[list[dict[str, Any]], operator.add]
    impl_ids: Annotated[list[str], operator.add]
    chain: dict[str, Any]
    verify: dict[str, Any]
    synth_bundle_id: str
    commit_canonical: int
    synth_canonical: int
    errors: list[str]
    t0: float


def build_graph() -> StateGraph:
    sg = StateGraph(VariantAState)
    sg.add_node("recon", recon)
    sg.add_node("fanout_fork", fanout_fork)
    sg.add_node("worker", worker)
    sg.add_node("investigate", investigate)
    sg.add_node("commit", commit)
    sg.add_node("verify", verify)
    sg.add_node("synthesize", synthesize)

    sg.add_edge("__start__", "recon")
    sg.add_conditional_edges("recon", lambda s: "fanout_fork")
    sg.add_conditional_edges("fanout_fork", _route_workers)
    sg.add_edge("worker", "investigate")
    sg.add_edge("investigate", "commit")
    sg.add_edge("commit", "verify")
    sg.add_edge("verify", "synthesize")
    sg.add_edge("synthesize", END)
    return sg


def _route_workers(state: dict[str, Any]) -> list[Send] | str:
    findings = state.get("worker_findings") or []
    if not findings:
        return "investigate"
    ctx = {"bundle_id": state["bundle_id"], "activities": state["activities"],
           "recon": state["recon"], "analysis": state["analysis"]}
    return [Send("worker", {**ctx, "run_id": state["run_id"], "worker": w["worker"],
                            "finding": w["finding"]}) for w in findings]


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


async def recon(state: dict[str, Any]) -> dict[str, Any]:
    from pathlib import Path
    from dra.publish import async_session

    actor = actor_for_state(state)
    async with async_session() as session:
        async with session.begin():
            # get_or_create_bundle reuses the existing commit bundle on resume
            # (idempotent), so _stage_recon's dedupe guard finds the prior rows.
            bundle_id, _new = await evidence.get_or_create_bundle(
                session, state["run_id"], state["task_id"],
                "§38.1 Variant A commit", actor)
            acts = {
                "recon": await evidence.get_or_create_activity(
                    session, bundle_id, "acquisition", actor),
                "fanout": await evidence.get_or_create_activity(
                    session, bundle_id, "derivation", actor),
                "invest": await evidence.get_or_create_activity(
                    session, bundle_id, "verification", actor),
                "synth": await evidence.get_or_create_activity(
                    session, bundle_id, "synthesis", actor),
            }
            recon_data = await evidence._stage_recon(
                session, bundle_id, acts["recon"], Path(state["corpus_dir"]))
            analyze = evidence._import_corpus()
            analysis = analyze(Path(state["corpus_dir"]))
    return {
        "bundle_id": str(bundle_id),
        "activities": {str(k): str(v) for k, v in acts.items()},
        "recon": {**recon_data, "source_id": str(recon_data["source_id"]),
                  "raw_eid": str(recon_data["raw_eid"]),
                  "raw_hash": recon_data["raw_hash"]},
        "analysis": _analysis_to_dict(analysis),
        "worker_findings": [{"worker": w, "finding": dict(f)}
                            for w, f in evidence._partition_findings(analysis)],
    }


async def fanout_fork(state: dict[str, Any]) -> dict[str, Any]:
    # Marker node; the real fan-out happens via Send -> worker(_route_workers).
    return {}


async def worker(payload: dict[str, Any]) -> dict[str, Any]:
    from dra.publish import async_session
    from uuid import UUID

    finding = payload["finding"]
    worker_idx = payload["worker"]
    async with async_session() as session:
        async with session.begin():
            eids = await evidence._stage_fanout_workers(
                session, UUID(payload["bundle_id"]),
                UUID(payload["activities"]["fanout"]),
                payload["recon"], payload["analysis"], worker_idx, finding)
            eid = eids[0]
    return {"fanout_results": [{"worker": worker_idx, "impl_id": str(eid)}],
            "impl_ids": [str(eid)]}


async def investigate(state: dict[str, Any]) -> dict[str, Any]:
    from dra.publish import async_session
    from uuid import UUID

    async with async_session() as session:
        async with session.begin():
            analysis = _analysis_from_dict(state["analysis"])
            chain = await evidence._stage_deep_investigation(
                session, UUID(state["bundle_id"]),
                UUID(state["activities"]["invest"]),
                state["recon"], analysis,
                [f["finding"] for f in (state.get("worker_findings") or [])])
    return {"chain": {**chain, "derived_id": str(chain["derived_id"]),
                      "evidence_id": str(chain["evidence_id"]),
                      "claim_id": str(chain["claim_id"])}}


async def commit(state: dict[str, Any]) -> dict[str, Any]:
    from dra.publish import async_session, publish_bundle
    from uuid import UUID

    async with async_session() as session:
        async with session.begin():
            n = await publish_bundle(UUID(state["bundle_id"]), session=session)
    return {"commit_canonical": n}


async def verify(state: dict[str, Any]) -> dict[str, Any]:
    report = await evidence.verify_bundle(state["bundle_id"])
    return {"verify": report}


async def synthesize(state: dict[str, Any]) -> dict[str, Any]:
    from pathlib import Path

    cr = evidence.CommitReceipt(
        run_id=state["run_id"], variant="A_langgraph",
        bundle_id=state["bundle_id"], task_id=state["task_id"],
        canonical_count=state["commit_canonical"], raw_hash=state["recon"]["raw_hash"])
    syn = await evidence.synthesize_bundle(
        state["run_id"], "A_langgraph", cr, Path(state["corpus_dir"]))
    return {"synth_bundle_id": syn.bundle_id, "synth_canonical": syn.canonical_count,
            "errors": list(state.get("errors", []))}


def _analysis_to_dict(a) -> dict[str, Any]:
    return {
        "files": a.files,
        "public_symbols": [{"file": s.file, "name": s.name, "kind": s.kind, "line": s.line}
                             for s in a.public_symbols],
        "auth_entry_point": None if a.auth_entry_point is None else
        {"file": a.auth_entry_point.file, "name": a.auth_entry_point.name,
         "kind": a.auth_entry_point.kind, "line": a.auth_entry_point.line},
        "config_safe_before_init": a.config_safe_before_init,
        "corpus_hash": a.corpus_hash,
    }


def _analysis_from_dict(d: dict[str, Any]):
    from corpus import Analysis, SymbolRef
    a = Analysis()
    a.files = d.get("files", [])
    a.public_symbols = [SymbolRef(file=s["file"], name=s["name"], kind=s["kind"], line=s["line"])
                        for s in d.get("public_symbols", [])]
    aep = d.get("auth_entry_point")
    a.auth_entry_point = SymbolRef(**aep) if aep else None
    a.config_safe_before_init = d.get("config_safe_before_init", False)
    a.corpus_hash = d.get("corpus_hash", "")
    return a


def actor_for_state(state: dict[str, Any]) -> dict[str, Any]:
    a = dict(ACTOR)
    a["external_id"] = f"bakeoff:A_langgraph:{state.get('run_id')}"
    return a
