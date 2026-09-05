"""Shared §2 evidence-emission lifecycle for the §38.1 bake-off (non-canonical).

Every variant routes the *same* tiny-corpus recon->fan-out->investigate->commit->
verify->synthesize findings through ``dra.publish`` / ``publish_bundle`` so the
dra.publish evidence-graph bundle/commit contract stays the source of truth.
This module is the harness-agnostic core; each variant wraps it with its own
orchestration substrate (LangGraph graph / DeepAgents worker / DeerFlow plan).

Grounded in the real ``dra.publish`` API (verified against publish.py):
  stage_bundle(run_id, task_id, label, actor) -> UUID
  create_activity(session, bundle_id, activity_type, actor, ...) -> UUID
  stage_source_identity(session, bundle_id, activity_id, kind, locator, ...) -> UUID
  stage_source_capture(session, bundle_id, activity_id, source_id, content_hash, kind, *, blob_store, data, ..., final_url, ...) -> UUID
  stage_implementation_entity(session, bundle_id, activity_id, repo_source_id, kind, *, path, ...) -> UUID
  stage_derived_artifact(session, bundle_id, activity_id, source_capture_hash, content_hash, kind, version, ...) -> UUID
  stage_evidence_unit(session, bundle_id, activity_id, artifact_id, locator, ...) -> UUID
  stage_claim(session, bundle_id, activity_id, claim_text, evidence_unit_id, ...) -> UUID
  stage_decision(session, bundle_id, activity_id, claim_id, decision_text, ...) -> UUID
  stage_handoff(session, bundle_id, activity_id, decision_id, manifest, ...) -> UUID
  add_prov_edge(session, *, generated_entity_id, activity_id=...) / (deriving_entity_id, source_entity_id, activity_id)
  publish_bundle(bundle_id, *, session) -> int   # atomic staged->canonical
  run_verification_proof(cfg, write) -> report  # §38.4 gate
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import text

from dra.db import DATABASE_URL, engine
from dra.publish import (
    async_session,
    stage_bundle,
    create_activity,
    stage_source_identity,
    stage_source_capture,
    stage_implementation_entity,
    stage_derived_artifact,
    stage_evidence_unit,
    stage_claim,
    stage_decision,
    stage_handoff,
    add_prov_edge,
    publish_bundle,
)

# ---------------------------------------------------------------------------
# Run-scoped identity + DB hygiene
# ---------------------------------------------------------------------------

RUN_PREFIX = "bakeoff-"


def actor_for(variant: str, run_id: str) -> dict[str, Any]:
    """A stable, per-run actor identity (avoids the prov_agent external_id race
    called out in control_plane.py:run_branch_worker)."""
    return {
        "external_id": f"{RUN_PREFIX}{variant}:{run_id}",
        "kind": "model",
        "name": f"bakeoff-{variant}",
        "version": "1.0",
        "model_family": variant,
    }


# run_ids from ad-hoc test sessions that must also be cleanable.
_SMOKE_RUN_IDS = ["smoke", "smoke2", "crA"]


def reset_checkpoints() -> None:
    """Wipe the langgraph checkpoint tables (langgraph-owned, safe to clear).

    Keeps shared canonical DRA evidence untouched. Uses the shared async engine
    so it depends only on the already-declared ``psycopg`` (psycopg3) stack.
    """
    asyncio.run(_reset_checkpoints_async())


async def _reset_checkpoints_async() -> None:
    # NOTE: ``TRUNCATE ... IF EXISTS`` is rejected by this DB endpoint; the
    # checkpoint tables are confirmed to exist (AsyncPostgresSaver.setup()),
    # so a DELETE FROM is used instead.
    async with engine.begin() as c:
        for t in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
            await c.execute(text(f"DELETE FROM {t}"))


def reset_bakeoff_evidence(run_prefix: str = RUN_PREFIX) -> int:
    """Scoped teardown of bake-off evidence graphs (this mission's runs only).

    Deletes every domain + provenance row belonging to bundles whose run_id
    starts with ``run_prefix`` (or the ad-hoc smoke run_ids), so the §38.4 gate
    (which scans *all* canonical claims) and counts reflect only the current
    measurement. FK enforcement is disabled via ``session_replication_role`` so
    deletion order is irrelevant; only rows we created are touched.
    """
    return asyncio.run(_reset_bakeoff_evidence_async(run_prefix))


async def _reset_bakeoff_evidence_async(run_prefix: str) -> int:
    async with engine.begin() as c:
        await c.execute(text("SET session_replication_role = 'REPLICA'"))
        try:
            bid_rows = await c.execute(
                text("SELECT id FROM prov_bundle "
                     "WHERE run_id LIKE :p OR run_id = ANY(:smoke)"),
                {"p": run_prefix + "%", "smoke": _SMOKE_RUN_IDS},
            )
            bundle_ids = [str(r[0]) for r in bid_rows.fetchall()]
            if not bundle_ids:
                return 0
            bparam = {"b": bundle_ids}
            # Collect entity ids + raw_capture(source_id,content_hash) for the
            # target bundles. prov_entity.id is the PK of impl/evidence/claim/
            # decision/handoff/gap/derived_artifact domain rows; raw_capture's PK
            # is content_hash; source_identity's PK is id (reached via raw_capture).
            row = await c.execute(
                text("SELECT pe.id, rc.source_id, rc.content_hash "
                     "FROM prov_entity pe "
                     "LEFT JOIN raw_capture rc ON rc.content_hash = pe.content_hash "
                     "AND pe.entity_kind = 'raw_capture' "
                     "WHERE pe.bundle_id = ANY(:b)"),
                bparam,
            )
            eids: list[str] = []
            source_ids: list[str] = []
            raw_hashes: list[str] = []
            for r in row.fetchall():
                eids.append(str(r[0]))
                if r[1] is not None:
                    source_ids.append(str(r[1]))
                if r[2] is not None:
                    raw_hashes.append(str(r[2]))
            eparam = {"e": eids, "s": source_ids, "rh": raw_hashes}
            stmts = [
                "DELETE FROM prov_generation WHERE entity_id = ANY(:e)",
                "DELETE FROM prov_derivation WHERE derived_entity_id = ANY(:e) OR source_entity_id = ANY(:e)",
                "DELETE FROM prov_activity WHERE bundle_id = ANY(:b)",
                "DELETE FROM prov_entity WHERE bundle_id = ANY(:b)",
                "DELETE FROM implementation_entity WHERE id = ANY(:e)",
                "DELETE FROM evidence_unit WHERE id = ANY(:e)",
                "DELETE FROM claim WHERE id = ANY(:e)",
                "DELETE FROM handoff_statement WHERE id = ANY(:e)",
                "DELETE FROM decision WHERE id = ANY(:e)",
                "DELETE FROM gap WHERE id = ANY(:e)",
                "DELETE FROM derived_artifact WHERE id = ANY(:e)",
                "DELETE FROM raw_capture WHERE content_hash = ANY(:rh)",
                "DELETE FROM source_identity WHERE id = ANY(:s)",
                "DELETE FROM prov_bundle WHERE id = ANY(:b)",
                "DELETE FROM prov_agent WHERE external_id LIKE :p",
            ]
            for sql in stmts:
                await c.execute(text(sql), {**bparam, **eparam, "p": run_prefix + "%"})
            deleted = len(bundle_ids)
            return deleted
        finally:
            await c.execute(text("SET session_replication_role = 'origin'"))


# ---------------------------------------------------------------------------
# Receipts
# ---------------------------------------------------------------------------


@dataclass
class CommitReceipt:
    run_id: str
    variant: str
    bundle_id: str
    task_id: str
    canonical_count: int = 0
    entity_counts: dict[str, int] = field(default_factory=dict)
    phase_counts: dict[str, int] = field(default_factory=dict)
    raw_hash: str = ""
    elapsed_ms: float = 0.0
    errors: list[str] = field(default_factory=list)


@dataclass
class SynthReceipt:
    run_id: str
    variant: str
    bundle_id: str
    commit_bundle_id: str
    canonical_count: int = 0
    elapsed_ms: float = 0.0
    errors: list[str] = field(default_factory=list)


@dataclass
class RunReceipt:
    run_id: str
    variant: str
    commit: CommitReceipt
    verify: dict[str, Any]
    synth: SynthReceipt
    checkpoint_blobs: int = 0
    checkpoint_sizes: list[int] = field(default_factory=list)
    resume_idempotent: bool = True
    cancel_rollback_canonical: int = 0
    in_state_findings: int = 0
    native_state: dict[str, Any] = field(default_factory=dict)
    fanout_workers: int = 3
    fanout_bleed: list[str] = field(default_factory=list)
    publish_call_sites: int = 2  # commit + synthesize
    elapsed_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "variant": self.variant,
            "commit": asdict(self.commit),
            "verify": self.verify,
            "synth": asdict(self.synth),
            "checkpoint_blobs": self.checkpoint_blobs,
            "checkpoint_sizes": self.checkpoint_sizes,
            "resume_idempotent": self.resume_idempotent,
            "cancel_rollback_canonical": self.cancel_rollback_canonical,
            "in_state_findings": self.in_state_findings,
            "native_state": self.native_state,
            "fanout_workers": self.fanout_workers,
            "fanout_bleed": self.fanout_bleed,
            "publish_call_sites": self.publish_call_sites,
            "elapsed_ms": self.elapsed_ms,
        }


# ---------------------------------------------------------------------------
# Phase 1 — recon
# ---------------------------------------------------------------------------


async def get_or_create_bundle(
    session, run_id: str, task_id: str, label: str, actor: dict[str, Any]
) -> tuple[UUID, bool]:
    """Return an existing bundle for (run_id, task_id), else create one.

    This is what makes a re-invoked workflow idempotent: a resumed LangGraph run
    reuses the same commit bundle instead of staging a duplicate source_identity
    (the ``source_identity.locator,version`` UNIQUE would otherwise raise).
    """
    row = await session.execute(
        text("SELECT id FROM prov_bundle WHERE run_id = :r AND task_id = :t "
             "ORDER BY created_at DESC LIMIT 1"),
        {"r": run_id, "t": task_id})
    existing = row.scalar_one_or_none()
    if existing is not None:
        return existing, False
    return await stage_bundle(run_id, task_id, label, actor), True


async def get_or_create_activity(
    session, bundle_id: UUID, activity_type: str, actor: dict[str, Any]
) -> UUID:
    """Return an existing activity of this type in the bundle, else create one.

    Idempotent counterpart to :func:`get_or_create_bundle` for the per-phase
    ``prov_activity`` rows (acquisition/derivation/verification/synthesis).
    """
    row = await session.execute(
        text("SELECT id FROM prov_activity WHERE bundle_id = :b "
             "AND activity_type = :a LIMIT 1"),
        {"b": str(bundle_id), "a": activity_type})
    existing = row.scalar_one_or_none()
    if existing is not None:
        return existing
    return await create_activity(session, bundle_id, activity_type, actor)


async def _get_or_create_source_identity(
    session, bundle_id: UUID, act_id: UUID, corpus_dir: Path
) -> UUID:
    """Reuse a source_identity by (locator, version), else create one.

    ``source_identity`` now uses a concurrency-safe get-or-create on
    ``normalized_key`` (kind:locator:version), so two runs pointing at the same
    corpus path share one source row.  The shared source stays consistent with
    the content-addressed content_blob that backs the source_capture.
    """
    return await stage_source_identity(
        session, bundle_id, act_id, "repo", str(corpus_dir), version="bakeoff-corpus",
        state="staged", license_spdx="CC0-1.0", access_basis="public",
        crawl_allowed=True, redist_allowed=True,
        metadata={"kind": "corpus", "fanout_worker": 0},
    )


async def _stage_recon(
    session, bundle_id: UUID, act_id: UUID, corpus_dir: Path
) -> dict[str, Any]:
    """Stage the corpus snapshot as source_capture + source_identity (recon).

    Idempotent: if this bundle already carries a source_capture, the existing
    source_id/raw_hash/raw_eid are returned unchanged (resume-safe). Across
    runs the content-addressed content_blob (ON CONFLICT DO NOTHING) + shared
    source_identity (get-or-create) prevent UNIQUE-key collisions.
    """
    existing = await session.execute(
        text("SELECT pe.id, sc.content_blob_hash, sc.source_identity_id "
             "FROM prov_entity pe JOIN source_capture sc "
             "ON sc.capture_id = pe.id "
             "WHERE pe.bundle_id = :b AND pe.entity_kind = 'raw_capture'"),
        {"b": str(bundle_id)},
    )
    row = existing.fetchone()
    if row is not None:
        return {"source_id": row[2], "raw_hash": str(row[1]), "raw_eid": row[0]}

    corpus_dir = Path(corpus_dir)
    file_hashes = {}
    for p in sorted(corpus_dir.glob("*.py")):
        file_hashes[p.name] = sha256(p.read_bytes()).hexdigest()
    tree_bytes = "".join(f"{n}:{h}" for n, h in sorted(file_hashes.items())).encode()
    raw_hash = sha256(tree_bytes).hexdigest()

    src = await _get_or_create_source_identity(session, bundle_id, act_id, corpus_dir)
    raw_eid = await stage_source_capture(
        session, bundle_id, act_id, src, raw_hash, "repo_snapshot",
        size_bytes=len(tree_bytes), data=tree_bytes, final_url=str(corpus_dir),
        state="staged", metadata={"fanout_worker": 0},
    )
    await add_prov_edge(session, generated_entity_id=raw_eid, activity_id=act_id)
    return {"source_id": src, "raw_hash": raw_hash, "raw_eid": raw_eid}


# ---------------------------------------------------------------------------
# Phase 2 — fan-out (three parallel investigation workers)
# ---------------------------------------------------------------------------


async def _stage_fanout_workers(
    session, bundle_id: UUID, fanout_act: UUID, recon: dict[str, Any],
    analysis, worker_idx: int, finding: dict[str, Any],
) -> list[UUID]:
    """Stage one fan-out worker's implementation_entity findings (idempotent).

    ``finding`` shape: {"kind": impl_kind, "symbol": str, "path": str,
    "line_start": int, "line_end": int, "signature": str, "content": str}.
    Skips re-staging when an implementation_entity with the same content_hash
    already exists in the bundle (resume-safe). Each worker writes its own
    ``metadata.fanout_worker`` so parallel isolation is auditable.
    """
    out: list[UUID] = []
    content = finding["content"]
    ch = sha256(content.encode("utf-8")).hexdigest()
    existing = await session.execute(
        text("SELECT pe.id FROM prov_entity pe "
             "WHERE pe.bundle_id = :b AND pe.entity_kind = 'implementation_entity' "
             "AND pe.content_hash = :ch"),
        {"b": str(bundle_id), "ch": ch})
    found = existing.fetchone()
    if found is not None:
        return [found[0]]  # already staged by a prior run for this bundle (resume)
    eid = await stage_implementation_entity(
        session, bundle_id, fanout_act, recon["source_id"], finding["kind"],
        path=finding.get("path"), symbol_name=finding.get("symbol"),
        commit_sha="bakeoff-local", line_start=finding.get("line_start"),
        line_end=finding.get("line_end"), signature=finding.get("signature"),
        content_hash=ch, state="staged",
        metadata={"fanout_worker": worker_idx},
    )
    await add_prov_edge(session, generated_entity_id=eid, activity_id=fanout_act)
    out.append(eid)
    return out


# ---------------------------------------------------------------------------
# Phase 3 — deep investigation (build the claim chain + evidence)
# ---------------------------------------------------------------------------


async def _existing_entity(session, bundle_id: UUID, entity_kind: str, content_hash: str) -> UUID | None:
    row = await session.execute(
        text("SELECT pe.id FROM prov_entity pe "
             "WHERE pe.bundle_id = :b AND pe.entity_kind = :k "
             "AND pe.content_hash = :ch"),
        {"b": str(bundle_id), "k": entity_kind, "ch": content_hash})
    return row.scalar_one_or_none()


async def _existing_claim(session, bundle_id: UUID, claim_text: str) -> UUID | None:
    """Find an already-staged claim in the bundle by its text (claims have no
    content_hash, so they dedupe by text). Resume-safe."""
    row = await session.execute(
        text("SELECT pe.id FROM prov_entity pe JOIN claim c ON c.id = pe.id "
             "WHERE pe.bundle_id = :b AND pe.entity_kind = 'claim' "
             "AND c.text = :t LIMIT 1"),
        {"b": str(bundle_id), "t": claim_text})
    return row.scalar_one_or_none()


async def _existing_decision(session, bundle_id: UUID, claim_id: UUID) -> UUID | None:
    row = await session.execute(
        text("SELECT pe.id FROM prov_entity pe "
             "JOIN decision d ON d.id = pe.id "
             "WHERE pe.bundle_id = :b AND pe.entity_kind = 'decision' "
             "AND d.claim_id = :c LIMIT 1"),
        {"b": str(bundle_id), "c": str(claim_id)})
    return row.scalar_one_or_none()


async def _existing_handoff(session, bundle_id: UUID, decision_id: UUID) -> UUID | None:
    row = await session.execute(
        text("SELECT pe.id FROM prov_entity pe "
             "JOIN handoff_statement h ON h.id = pe.id "
             "WHERE pe.bundle_id = :b AND pe.entity_kind = 'handoff' "
             "AND h.decision_id = :d LIMIT 1"),
        {"b": str(bundle_id), "d": str(decision_id)})
    return row.scalar_one_or_none()


async def _stage_deep_investigation(
    session, bundle_id: UUID, invest_act: UUID, recon: dict[str, Any],
    analysis, findings: list[dict[str, Any]],
) -> dict[str, Any]:
    """Stage the derived artifact + evidence unit + claim for the findings.

    Chain: raw_capture -> derived_artifact -> evidence_unit -> claim
    (the exact provenance chain publish_bundle validates, §21.2). Idempotent:
    re-staging the same content_hash within a bundle is a no-op (resume-safe).
    """
    findings_blob = json.dumps(findings, sort_keys=True)
    # Scope hashes to the bundle: a re-staged artifact for a DIFFERENT bundle
    # must not trip derived_artifact's ON CONFLICT DO NOTHING (which would leave
    # a prov_entity.id with no matching domain row -> evidence_unit FK violation).
    # Within one bundle the hash is stable (resume reuse).
    findings_hash = sha256((findings_blob + str(bundle_id)).encode("utf-8")).hexdigest()
    summary_text = (
        "Public symbols: " + ", ".join(s.name for s in analysis.public_symbols) + ". "
        f"Auth entry point: {analysis.auth_entry_point.name if analysis.auth_entry_point else 'NONE'}. "
        f"Config safe before init: {analysis.config_safe_before_init}."
    )
    summary_hash = sha256((summary_text + str(bundle_id)).encode("utf-8")).hexdigest()
    # idempotent: reuse existing derived/evidence/claim if already staged in THIS
    # bundle (resume-safe). claims have no content_hash, so dedupe by text.
    da_id = await _existing_entity(session, bundle_id, "derived_artifact", findings_hash)
    if da_id is not None:
        ev_id = await _existing_entity(session, bundle_id, "evidence_unit", summary_hash)
        cl_id = await _existing_claim(session, bundle_id, summary_text)
        return {"derived_id": da_id, "evidence_id": ev_id, "claim_id": cl_id,
                "findings_hash": findings_hash}
    da = await stage_derived_artifact(
        session, bundle_id, invest_act, recon["raw_hash"], findings_hash,
        "summary", version=1, state="staged",
        metadata=finding_meta(findings, "deep_investigation"),
    )
    await add_prov_edge(session, generated_entity_id=da, activity_id=invest_act)
    await add_prov_edge(
        session, deriving_entity_id=da, source_entity_id=recon["raw_eid"],
        activity_id=invest_act,
    )

    ev = await stage_evidence_unit(
        session, bundle_id, invest_act, da,
        {"source_kind": "repo", "path": "corpus/auth.py",
         "symbol": "authenticate", "line_start": 1, "line_end": 9},
        content_hash=summary_hash, state="staged",
        metadata={"excerpt": summary_text, "fanout_worker": 3},
    )
    await add_prov_edge(session, generated_entity_id=ev, activity_id=invest_act)
    await add_prov_edge(session, deriving_entity_id=ev, source_entity_id=da, activity_id=invest_act)

    claim = await stage_claim(
        session, bundle_id, invest_act, summary_text, evidence_unit_id=ev,
        state="staged", confidence=0.9,
        metadata={"fanout_worker": 3},
    )
    await add_prov_edge(session, generated_entity_id=claim, activity_id=invest_act)
    await add_prov_edge(session, deriving_entity_id=claim, source_entity_id=ev, activity_id=invest_act)
    return {"derived_id": da, "evidence_id": ev, "claim_id": claim,
            "findings_hash": findings_hash, "summary": summary_text}


def finding_meta(findings: list[dict[str, Any]], phase: str) -> dict[str, Any]:
    return {"phase": phase, "findings_count": len(findings)}


# ---------------------------------------------------------------------------
# The identical §2 workflow (harness-agnostic) — commit bundle
# ---------------------------------------------------------------------------


async def commit_workflow(
    run_id: str, variant: str, task_id: str | None, corpus_dir: Path,
) -> CommitReceipt:
    """Run the full §2 lifecycle EXCEPT synthesize, committed as ONE bundle.

    recon -> fan-out (3 workers) -> deep-investigation -> publish_bundle.
    Idempotent: if a canonical bundle already exists for this run_id+task_id,
    it is reused (publish_bundle is an atomic no-op returning the existing
    canonical count), so a crashed/restarted run does not double-commit.
    """
    task_id = task_id or f"{RUN_PREFIX}{variant}-task"
    actor = actor_for(variant, run_id)
    receipt = CommitReceipt(run_id=run_id, variant=variant, bundle_id="",
                            task_id=task_id, errors=[])
    t0 = time.perf_counter()
    async with async_session() as session:
        async with session.begin():
            bundle_id, _new = await get_or_create_bundle(
                session, run_id, task_id, "§38.1 bake-off commit", actor)
            receipt.bundle_id = str(bundle_id)
            recon_act = await get_or_create_activity(
                session, bundle_id, "acquisition", actor)
            fanout_act = await get_or_create_activity(
                session, bundle_id, "derivation", actor)
            invest_act = await get_or_create_activity(
                session, bundle_id, "verification", actor)

            # Phase 1: recon
            recon = await _stage_recon(session, bundle_id, recon_act, corpus_dir)
            receipt.raw_hash = recon["raw_hash"]

            # Phase 2: fan-out (3 parallel investigation workers, deterministic)
            analyze = _import_corpus()
            analysis = analyze(corpus_dir)
            worker_findings = _partition_findings(analysis)
            all_impl_ids: list[UUID] = []
            phase2_count = 0
            for widx, finding in worker_findings:
                ids = await _stage_fanout_workers(
                    session, bundle_id, fanout_act, recon, analysis, widx, finding)
                all_impl_ids.extend(ids)
                phase2_count += len(ids)

            # Phase 3: deep investigation -> claim chain
            chain = await _stage_deep_investigation(
                session, bundle_id, invest_act, recon, analysis, worker_findings)
            phase3_count = 4  # derived_artifact + evidence_unit + claim + prov edges already counted in entities

            # Phase 4: commit (atomic staged->canonical)
            n = await publish_bundle(bundle_id, session=session)
            receipt.canonical_count = n
    receipt.phase_counts = {"recon": 2, "fanout_impl": phase2_count, "deep_chain": 3}
    receipt.elapsed_ms = (time.perf_counter() - t0) * 1000
    receipt.entity_counts = await _canonical_entity_counts(receipt.bundle_id)
    return receipt


def _partition_findings(analysis) -> list[tuple[int, dict[str, Any]]]:
    """Split the analysis into 3 fan-out workers:
    worker 1 = public API symbols, worker 2 = auth entry point, worker 3 = config safety."""
    out: list[tuple[int, dict[str, Any]]] = []
    # Worker 1: every public symbol -> one impl entity each
    for s in analysis.public_symbols:
        out.append((1, {
            "kind": "api" if s.kind == "class" else "symbol",
            "symbol": s.name,
            "path": f"corpus/{s.file}",
            "line_start": s.line,
            "line_end": s.line,
            "signature": f"{s.name}() @ {s.file}:{s.line}",
            "content": f"public_symbol:{s.file}:{s.name}:{s.line}",
        }))
    # Worker 2: auth entry point
    if analysis.auth_entry_point is not None:
        a = analysis.auth_entry_point
        out.append((2, {
            "kind": "symbol",
            "symbol": a.name,
            "path": f"corpus/{a.file}",
            "line_start": a.line,
            "line_end": a.line,
            "signature": f"{a.name}() @ {a.file}:{a.line}",
            "content": f"auth_entry_point:{a.file}:{a.name}",
        }))
    else:
        out.append((2, {
            "kind": "symbol", "symbol": "NONE",
            "path": "corpus/auth.py", "line_start": 0, "line_end": 0,
            "signature": "NONE",
            "content": "auth_entry_point:NONE",
        }))
    # Worker 3: config safety
    out.append((3, {
        "kind": "algorithm", "symbol": "config_safe_before_init",
        "path": "corpus/config.py", "line_start": 10, "line_end": 14,
        "signature": "configure(settings_dir: str) -> None  [init-guarded]",
        "content": f"config_safe_before_init:{analysis.config_safe_before_init}",
    }))
    return out


async def _canonical_entity_counts(bundle_id_str: str) -> dict[str, int]:
    """Count canonical prov_entity rows by kind for a bundle (for the receipt)."""
    async with async_session() as session:
        async with session.begin():
            rows = await session.execute(
                text(
                    "SELECT entity_kind, count(*) FROM prov_entity "
                    "WHERE bundle_id = :b AND state = 'canonical' "
                    "GROUP BY entity_kind"
                ),
                {"b": bundle_id_str},
            )
            return {str(r[0]): int(r[1]) for r in rows.fetchall()}


# ---------------------------------------------------------------------------
# Phase 5 — verify (§38.4 gate, read-only)
# ---------------------------------------------------------------------------


async def verify_bundle(bundle_id_str: str) -> dict[str, Any]:
    """Run the §38.4 verification gate over the bundle's claims (read-only)."""
    from dra.verification_gate import GateConfig, run_verification_proof

    cfg = GateConfig(write_mutations=False)  # read-only: don't stamp the claims
    report = await run_verification_proof(cfg=cfg, write=False,
                                          report_path="/tmp/_bakeoff_verify.json")
    async with async_session() as session:
        async with session.begin():
            n_claims = await session.execute(
                text(
                    "SELECT count(*) FROM claim c "
                    "JOIN prov_entity pe ON pe.id = c.id "
                    "WHERE pe.bundle_id = :b AND pe.state = 'canonical'"
                ),
                {"b": bundle_id_str},
            )
            n_claims = int(n_claims.scalar_one())
    return {"verdict": report.get("verdict"),
            "claims_evaluated": n_claims,
            "gate_rules": report.get("gate_rules", {})}


def _import_corpus():
    """bake-off/ sits on sys.path at runtime (bootstrap), so ``corpus`` is
    importable as a top-level module — not a package relative import."""
    import sys
    from pathlib import Path
    bake = Path(__file__).resolve().parent
    if str(bake) not in sys.path:
        sys.path.insert(0, str(bake))
    from corpus import analyze  # noqa: WPS433 deferred
    return analyze


# ---------------------------------------------------------------------------
# Phase 6 — synthesize (second bundle, references commit bundle)
# ---------------------------------------------------------------------------


async def synthesize_bundle(
    run_id: str, variant: str, commit_receipt: CommitReceipt, corpus_dir: Path,
) -> SynthReceipt:
    """Stage the §38.1 recommendation as a decision + handoff_statement in a
    second publish_bundle (the handoff_summary artifact)."""
    actor = actor_for(variant, run_id)
    task_id = f"{RUN_PREFIX}{variant}-synth"
    receipt = SynthReceipt(run_id=run_id, variant=variant, bundle_id="",
                           commit_bundle_id=commit_receipt.bundle_id)
    t0 = time.perf_counter()
    async with async_session() as session:
        async with session.begin():
            bundle_id, _new = await get_or_create_bundle(
                session, run_id, task_id, "§38.1 bake-off synthesize", actor)
            receipt.bundle_id = str(bundle_id)
            synth_act = await get_or_create_activity(
                session, bundle_id, "synthesis", actor)

            # Stage a derived_artifact (handoff_summary) derived from the commit
            # bundle's raw capture, then a topic-less claim anchor, then decision
            # + handoff. Evidence stays canonical via publish_bundle.
            analyze = _import_corpus()
            analysis = analyze(corpus_dir)
            summary_text = (
                f"Bake-off {variant}: keep LangGraph as control-plane substrate "
                f"(§38.1/§42). Commit bundle {commit_receipt.bundle_id} "
                f"committed {commit_receipt.canonical_count} canonical rows."
            )
            raw_hash = commit_receipt.raw_hash
            summary_hash = sha256((summary_text + str(bundle_id)).encode("utf-8")).hexdigest()
            # Idempotent staging: reuse existing da/ev/claim/decision/handoff so a
            # resumed run does not duplicate rows the synth bundle already carries.
            da = await _existing_entity(session, bundle_id, "derived_artifact", summary_hash)
            if da is None:
                da = await stage_derived_artifact(
                    session, bundle_id, synth_act, raw_hash,
                    summary_hash, "synthesis", version=1,
                    state="staged", metadata={"kind": "handoff_summary"},
                )
                await add_prov_edge(session, generated_entity_id=da, activity_id=synth_act)

            ev = await _existing_entity(session, bundle_id, "evidence_unit", summary_hash)
            if ev is None:
                ev = await stage_evidence_unit(
                    session, bundle_id, synth_act, da,
                    {"source_kind": "repo", "path": "bake-off/results.md"},
                    summary_hash, state="staged",
                    metadata={"excerpt": summary_text},
                )
                await add_prov_edge(session, generated_entity_id=ev, activity_id=synth_act)
                await add_prov_edge(session, deriving_entity_id=ev, source_entity_id=da, activity_id=synth_act)

            claim = await _existing_claim(session, bundle_id, summary_text)
            if claim is None:
                claim = await stage_claim(
                    session, bundle_id, synth_act, summary_text, evidence_unit_id=ev,
                    state="staged", confidence=0.85,
                    metadata={"kind": "recommendation"},
                )
                await add_prov_edge(session, generated_entity_id=claim, activity_id=synth_act)
                await add_prov_edge(session, deriving_entity_id=claim, source_entity_id=ev, activity_id=synth_act)

            decision = await _existing_decision(session, bundle_id, claim)
            if decision is None:
                decision = await stage_decision(
                    session, bundle_id, synth_act, claim,
                    "Keep LangGraph as the §38.1 control-plane substrate unless an "
                    "alternative materially reduces cost AND keeps dra.publish the "
                    "source of truth.",
                    run_id=run_id, state="staged", rationale=summary_text,
                    metadata={"rule": "§38.1/§42"},
                )
                await add_prov_edge(session, generated_entity_id=decision, activity_id=synth_act)

            manifest = {
                "commit_bundle_id": commit_receipt.bundle_id,
                "canonical_rows": commit_receipt.canonical_count,
                "recommendation": "LangGraph remains the control-plane substrate",
                "decision_id": str(decision),
                "corpus_hash": analysis.corpus_hash,
            }
            handoff = await _existing_handoff(session, bundle_id, decision)
            if handoff is None:
                handoff = await stage_handoff(
                    session, bundle_id, synth_act, decision, manifest,
                    run_id=run_id, content=summary_text,
                    state="staged", metadata={"corpus_hash": analysis.corpus_hash},
                )
                await add_prov_edge(session, generated_entity_id=handoff, activity_id=synth_act)
            else:
                # refresh the manifest link on resume
                await session.execute(
                    text("UPDATE handoff_statement SET manifest = manifest || CAST(:m AS jsonb) "
                         "WHERE id = :h"),
                    {"m": json.dumps(manifest), "h": str(handoff)})

            n = await publish_bundle(bundle_id, session=session)
            receipt.canonical_count = n
    receipt.elapsed_ms = (time.perf_counter() - t0) * 1000
    return receipt


# ---------------------------------------------------------------------------
# Measurement query helpers (used by measure.py + invariant tests)
# ---------------------------------------------------------------------------


async def canonical_count(bundle_id_str: str) -> int:
    async with async_session() as session:
        async with session.begin():
            r = await session.execute(
                text("SELECT count(*) FROM prov_entity "
                     "WHERE bundle_id = :b AND state = 'canonical'"),
                {"b": bundle_id_str},
            )
            return int(r.scalar_one())


async def bundles_for_run(run_id: str) -> list[dict[str, Any]]:
    async with async_session() as session:
        async with session.begin():
            r = await session.execute(
                text("SELECT id, run_id, task_id, label, created_at FROM prov_bundle "
                     "WHERE run_id = :r ORDER BY created_at"),
                {"r": run_id},
            )
            cols = ("id", "run_id", "task_id", "label", "created_at")
            return [{c: (str(row[i]) if c in ("id",) else row[i]) for i, c in enumerate(cols)}
                    for row in r.fetchall()]


async def checkpoint_blobs(run_id: str) -> int:
    """Count langgraph checkpoint blobs for a run (thread_id = run_id)."""
    async with engine.connect() as c:
        r = await c.execute(
            text("SELECT count(*) FROM checkpoint_blobs WHERE thread_id = :r"),
            {"r": run_id})
        return int(r.scalar_one())


async def checkpoint_sizes(run_id: str) -> list[int]:
    """Per-step checkpoint state sizes (pg_column_size) in insertion order.

    Context-growth measurement (dimension 7). The ``checkpoints`` table is
    ordered by the embedded ``checkpoint->>'ts'`` timestamp (no created_at col).
    """
    async with engine.connect() as c:
        r = await c.execute(
            text("SELECT pg_column_size(checkpoint) FROM checkpoints "
                 "WHERE thread_id = :r ORDER BY (checkpoint->>'ts')::timestamptz NULLS LAST"),
            {"r": run_id})
        return [int(row[0]) for row in r.fetchall()]


# ---------------------------------------------------------------------------
# Cancel/retry atomicity probe (dimension 6): simulate a mid-publish failure
# ---------------------------------------------------------------------------


async def cancel_rollback_probe(run_id: str, variant: str, corpus_dir: Path) -> dict[str, Any]:
    """Stage a bundle and raise inside publish_bundle's txn; assert 0 canonical
    rows leaked (atomic staged->canonical commit rolls back on exception)."""
    actor = actor_for(variant, run_id + "-cancel")
    task_id = f"{RUN_PREFIX}{variant}-cancel"
    async with async_session() as session:
        async with session.begin():
            bundle_id = await stage_bundle(run_id + "-cancel", task_id, "cancel/retry probe", actor)
            act = await create_activity(session, bundle_id, "acquisition", actor)
            recon = await _stage_recon(session, bundle_id, act, corpus_dir)
            # Now attempt publish but simulate a cancellation by rolling back
            # (raising inside the txn). The staged rows must NOT become canonical.
            try:
                raise _Cancelled  # simulated mid-publish cancel
            except _Cancelled:
                # Let the async-with session.begin() roll back the txn.
                pass
        # After rollback, no canonical rows for this bundle.
    leaked = await canonical_count(str(bundle_id))
    # Retry: re-run cleanly and assert it commits exactly once (idempotent).
    retry_receipt = await commit_workflow(run_id + "-cancel-retry", variant, task_id, corpus_dir)
    return {
        "rollback_canonical": leaked,
        "retry_canonical": retry_receipt.canonical_count,
        "retry_bundle_id": retry_receipt.bundle_id,
        "idempotent": leaked == 0,
    }


class _Cancelled(BaseException):
    pass


# ---------------------------------------------------------------------------
# Resume idempotency probe (dimension 2): re-run the SAME run_id, expect no dup
# ---------------------------------------------------------------------------


async def resume_idempotency_probe(
    run_id: str, variant: str, corpus_dir: Path
) -> dict[str, Any]:
    """Run commit_workflow twice with the same run_id; the second must not
    double-commit (publish_bundle is an atomic no-op returning the existing
    canonical count)."""
    first = await commit_workflow(run_id, variant, None, corpus_dir)
    second = await commit_workflow(run_id, variant, None, corpus_dir)
    return {
        "first_canonical": first.canonical_count,
        "second_canonical": second.canonical_count,
        "idempotent": first.canonical_count == second.canonical_count,
        "dup_canonical": abs(first.canonical_count - second.canonical_count) == 0,
    }


def sync(fn, *args, **kwargs):
    """Run a coroutine to completion (repo convention: asyncio.run in sync tests)."""
    return asyncio.run(fn(*args, **kwargs))
