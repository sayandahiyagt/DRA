"""Shared deterministic (no-LLM) fake model + lifecycle tools for the §38.1 bake-off.

Both Variant B (DeepAgents) and Variant C (DeerFlow) orchestrate a deterministic
fake chat model that calls the same lifecycle tools, which in turn route every
finding through ``dra.publish`` / ``publish_bundle``. The fake model lets the
bake-off run end-to-end with NO real LLM call and NO network — the comparison is
about the orchestration substrate (checkpoint/resume, parallel isolation,
observability, native agent-internal state), not model quality.

Native-state measurement hook: ``count_native_state_files`` inspects the LangGraph
checkpoint's ``channel_values`` for the DeepAgents ``files`` channel and the
DeerFlow ``ThreadState`` native channels (``thread_data``/``artifacts``/...).
"""
from __future__ import annotations

import asyncio
import json
from typing import Any


def _ensure_bakeoff_on_path() -> None:
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    for p in (str(root / "src"), str(root / "bake-off")):
        if p not in sys.path:
            sys.path.insert(0, p)


_ensure_bakeoff_on_path()


# ---------------------------------------------------------------------------
# Deterministic fake chat model
# ---------------------------------------------------------------------------


def _make_fake_model(canned: list[str] | None = None) -> Any:
    """A BaseChatModel that emits a fixed tool-call sequence, then finishes.

    ``canned`` is the ordered list of tool names to call; the final entry
    ("finish") emits a no-tool-call answer so the agent loop terminates.
    """
    from langchain_core.language_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatResult, ChatGeneration

    if canned is None:
        canned = ["commit_evidence", "verify_evidence", "synthesize_evidence", "finish"]

    class _FakeChatModel(BaseChatModel):
        _canned: list[str] = canned
        _counter: int = 0

        @property
        def _llm_type(self) -> str:
            return "bakeoff-fake-model"

        @property
        def _identifying_params(self) -> dict[str, Any]:
            return {"canned": self._canned}

        def bind_tools(self, tools=None, **kwargs):
            # DeepAgents/DeerFlow call model.bind_tools(...); no-op: the fake
            # model just returns its next canned tool call regardless.
            return self

        def _generate(self, messages, stop=None, **kwargs):
            return self._step()

        async def _agenerate(self, messages, stop=None, **kwargs):
            return self._step()

        def _step(self):
            step = self._counter
            self._counter += 1
            name = self._canned[step % len(self._canned)]
            if name == "finish":
                msg = AIMessage(content="bake-off lifecycle complete; all findings published via dra.publish.")
                return ChatResult(generations=[ChatGeneration(message=msg)])
            msg = AIMessage(content="", tool_calls=[
                {"id": f"call_{step}", "name": name, "args": {}, "type": "tool_call"}
            ])
            return ChatResult(generations=[ChatGeneration(message=msg)])

    return _FakeChatModel()


def _fake_model_for() -> Any:
    """Fresh fake model instance (deterministic sequence)."""
    return _make_fake_model()


# ---------------------------------------------------------------------------
# Lifecycle tools (call evidence.py phases; route findings to dra.publish)
# ---------------------------------------------------------------------------


def _make_tools(run_id: str, corpus_dir: str, variant: str):
    """Return {name: tool_fn} closures bound to a run.

    Each tool invokes the shared §2 evidence lifecycle; findings are staged
    through ``dra.publish`` — the tools return short summaries, so the
    DeepAgents/DeerFlow native ``files``/``thread_data`` channels never hold a
    finding off-contract.
    """
    import evidence

    def commit_evidence():
        cr = asyncio.run(evidence.commit_workflow(run_id, variant, None, corpus_dir))
        return f"committed bundle {cr.bundle_id[:8]}: {cr.canonical_count} canonical rows"

    def verify_evidence():
        # find this run's commit bundle
        bundles = asyncio.run(evidence.bundles_for_run(run_id))
        commit = next((b for b in bundles if b["task_id"].endswith("-task")
                       or "commit" in b["task_id"]), bundles[0] if bundles else None)
        if commit is None:
            return "no commit bundle found"
        rep = asyncio.run(evidence.verify_bundle(commit["id"]))
        return json.dumps({"verdict": rep["verdict"],
                           "rules": rep["gate_rules"]})

    def synthesize_evidence():
        from corpus import generate
        from pathlib import Path
        import hashlib
        corpus_hash = evidence._import_corpus()(Path(corpus_dir)).corpus_hash
        # rebuild a CommitReceipt from DB for the commit bundle
        bundles = asyncio.run(evidence.bundles_for_run(run_id))
        commit = next((b for b in bundles if "synth" not in b["task_id"]), None)
        cr = evidence.CommitReceipt(
            run_id=run_id, variant=variant, bundle_id=commit["id"],
            task_id=commit["task_id"], canonical_count=asyncio.run(evidence.canonical_count(commit["id"])),
            raw_hash=_raw_hash_for(run_id), errors=[])
        syn = asyncio.run(evidence.synthesize_bundle(run_id, variant, cr, Path(corpus_dir)))
        return f"synthesized bundle {syn.bundle_id[:8]}: {syn.canonical_count} canonical rows"

    return {"commit_evidence": commit_evidence,
            "verify_evidence": verify_evidence,
            "synthesize_evidence": synthesize_evidence}


def _raw_hash_for(run_id: str) -> str:
    """Compute the corpus raw_hash (sha256 of the per-file-hash tree list).

    Must match ``evidence._stage_recon`` so the synth bundle's
    derived_artifact.source_capture_hash FK resolves to the commit bundle's
    raw_capture row. Re-derived deterministically from the corpus on disk.
    """
    from hashlib import sha256
    from pathlib import Path
    import evidence
    # re-use the commit bundle's raw_capture content_hash if present (authoritative)
    async def _from_db():
        async with evidence.async_session() as s:
            async with s.begin():
                from sqlalchemy import text
                r = await s.execute(
                    text("SELECT rc.content_hash FROM prov_bundle b "
                         "JOIN prov_entity pe ON pe.bundle_id=b.id "
                         "JOIN raw_capture rc ON rc.content_hash=pe.content_hash "
                         "WHERE b.run_id=:r AND pe.entity_kind='raw_capture' "
                         "ORDER BY b.created_at DESC LIMIT 1"),
                    {"r": run_id})
                row = r.fetchone()
                return str(row[0]) if row else None
    val = asyncio.run(_from_db())
    if val:
        return val
    # fall back to deterministic recompute
    import glob
    file_hashes = {}
    for p in sorted(glob.glob(str(Path(_corpus_dir_cache) / "*.py"))):
        file_hashes[Path(p).name] = sha256(Path(p).read_bytes()).hexdigest()
    tree = "".join(f"{n}:{h}" for n, h in sorted(file_hashes.items())).encode()
    return sha256(tree).hexdigest()


# corpus dir cache for the recompute fallback
_corpus_dir_cache = None


def set_corpus_dir(path: str) -> None:
    global _corpus_dir_cache
    _corpus_dir_cache = path

# ---------------------------------------------------------------------------
# Native-state measurement (dimension 5 disqualifier: in-state findings)
# ---------------------------------------------------------------------------
# langgraph 1.2.11's AsyncPostgresSaver spills large channel values to
# ``checkpoint_blobs`` (keyed by ``channel``); small ones live inline in
# ``checkpoints.checkpoint -> channel_values``. Both must be inspected to detect
# evidence held in agent-internal state (the §38.1/§42 disqualifier).

from dra.db import engine  # noqa: E402


async def _channels_for(run_id: str) -> set[str]:
    """Set of channel names present for a run (inline + blob)."""
    from sqlalchemy import text
    async with engine.connect() as c:
        row = await c.execute(
            text("SELECT checkpoint FROM checkpoints WHERE thread_id = :r "
                 "ORDER BY (checkpoint->>'ts')::timestamptz DESC NULLS LAST LIMIT 1"),
            {"r": run_id})
        rec = row.fetchone()
        cv = ((rec[0] or {}).get("channel_values", {})) if rec and rec[0] else {}
        inline = set(cv.keys()) if isinstance(cv, dict) else set()
        rb = await c.execute(
            text("SELECT channel FROM checkpoint_blobs WHERE thread_id = :r"),
            {"r": run_id})
        blobs = {r[0] for r in rb.fetchall()}
    return inline | blobs


async def _count_channel(run_id: str, channel: str) -> int:
    """Count rows (or inline length) for a single channel.

    ``channel_values`` is a key inside the ``checkpoints.checkpoint`` JSONB
    (not a column), so inline values are read from the JSONB, and large ones
    live in ``checkpoint_blobs`` (keyed by ``channel``).
    """
    from sqlalchemy import text
    from dra.db import engine
    inline_n = 0
    async with engine.connect() as c:
        r = await c.execute(
            text("SELECT checkpoint FROM checkpoints WHERE thread_id = :r "
                 "ORDER BY (checkpoint->>'ts')::timestamptz DESC NULLS LAST LIMIT 1"),
            {"r": run_id})
        rec = r.fetchone()
        if rec and rec[0]:
            cv = (rec[0].get("channel_values") or {}) if isinstance(rec[0], dict) else {}
            v = cv.get(channel)
            if isinstance(v, list):
                inline_n = len(v)
            elif isinstance(v, dict):
                inline_n = len(v)
            elif v is not None:
                inline_n = 1
        r = await c.execute(
            text("SELECT count(*) FROM checkpoint_blobs "
                 "WHERE thread_id = :r AND channel = :ch"),
            {"r": run_id, "ch": channel})
        blob_n = int(r.scalar_one())
    return max(inline_n, blob_n)


def count_native_state_files(run_id: str) -> dict:
    """Variant B (DeepAgents) native state: the ``files`` filesystem channel.

    ``in_state_findings`` = entries in ``files`` holding finding/evidence content
    that bypassed ``dra.publish``. If ``files`` is empty/absent (findings routed
    only through ``publish_bundle``), this is 0 — the clean case.
    """
    channels = asyncio.run(_channels_for(run_id))
    files_n = asyncio.run(_count_channel(run_id, "files")) if "files" in channels else 0
    return {"kind": "deepagents_filesystem", "files_channel": "files" in channels,
            "files_entries": files_n, "in_state_findings": files_n,
            "channels": sorted(channels)}


def count_in_state_findings(run_id: str) -> int:
    """Generic disqualifier metric: findings held ONLY in agent-native state.

    Sums the evidence-sensitive native channels (``files``/``thread_data``/
    ``artifacts``/``delegations``/``skill_context``) that hold content outside
    ``dra.publish``. 0 for the clean baseline (Variant A) and DeepAgents (Variant
    B); >0 for DeerFlow (Variant C — disqualified).
    """
    channels = asyncio.run(_channels_for(run_id))
    total = 0
    for ch in ("files", "thread_data", "artifacts", "delegations", "skill_context"):
        if ch in channels:
            total += asyncio.run(_count_channel(run_id, ch))
    return total


def count_native_state_deerflow(run_id: str) -> dict:
    """Variant C (DeerFlow) native state: ThreadState evidence channels.

    DeerFlow's ``ThreadState`` schema defines ``thread_data``/``artifacts``/
    ``delegations``/``skill_context`` as native agent-internal state, and
    ``ThreadDataMiddleware`` (``sandbox=True``) materialises tool results into
    ``thread_data``. Any non-zero count of these native evidence channels means
    canonical evidence is NOT held exclusively in ``dra.publish`` — the
    §38.1/§42 disqualifying evidence-integration violation.
    """
    channels = asyncio.run(_channels_for(run_id))
    native_evidence_channels = ("thread_data", "artifacts", "delegations", "skill_context")
    counts = {}
    in_state = 0
    for ch in native_evidence_channels:
        n = asyncio.run(_count_channel(run_id, ch)) if ch in channels else 0
        counts[ch] = n
        in_state += n
    counts["channels_present"] = sorted(channels)
    counts["in_state_findings"] = in_state
    return counts
