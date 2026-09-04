"""§33 final handoff generation (spec §33 / §31.1-§31.3).

Generates the audited implementation handoff from control state + canonical
evidence:

- :func:`build_manifest` -- pure, assembles the §31.2 machine-readable manifest
  from control state and an optional ``canon`` dict supplied by the DB half
  (canonical IDs / source snapshots / freshness). DB-free so the no-DB
  verification path can render a control-state manifest.
- :func:`build_document_package` -- pure, renders the §31.1 8-section
  human-readable markdown package, each section citing canonical research IDs.
- :func:`build_dependency_graph` -- pure, edges from control-state
  ``research_tasks`` dependencies plus canonical claim->evidence linkage.
- :func:`stage_section_handoff` -- DB half (mirrors
  ``bake-off/evidence.py::synthesize_bundle``): opens its own
  ``async_session`` transaction so a handoff-staging failure rolls back only
  this bundle (ADR-013) without orphaning prior canonical rows; idempotently
  stages control-state decisions (D1: p12 stays pure), builds the §31.2
  manifest + §31.1 package from the canonical IDs it just staged, then
  commits the handoff through :func:`dra.publish.stage_handoff` +
  :func:`dra.publish.publish_bundle`.

On the no-DB path (``live_investigators=False``) p13 calls only the pure
helpers and never reaches ``stage_section_handoff`` (mirroring p12's
control-state-only degradation).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text

from dra.knowledge import RETRIEVAL_KEY_TYPES
from dra.publish import (
    add_prov_edge,
    create_activity,
    publish_bundle,
    stage_decision,
    stage_handoff,
)

__all__ = [
    "RETRIEVAL_KEY_TYPES",
    "SECTION_FILES",
    "build_dependency_graph",
    "build_document_package",
    "build_manifest",
    "canonical_ids_by_run",
    "stage_section_handoff",
]

# §31.1 human-readable package sections (order matters; 03 is directory-shaped).
SECTION_FILES = [
    "00-executive",
    "01-requirements",
    "02-architecture",
    "03-source-system-understanding",
    "04-implementation-plan",
    "05-decisions",
    "06-risks-and-unknowns",
    "07-evidence-index",
]


def _as_str(value: Any) -> str | None:
    """Stringify a UUID (or anything) for JSON-safe manifest IDs."""
    if value is None:
        return None
    if isinstance(value, UUID):
        return str(value)
    return str(value)


def _str_list(values: Any) -> list[str]:
    """Normalize an iterable of UUIDs/strings into a list of str IDs."""
    if not values:
        return []
    if isinstance(values, (str, UUID)):
        return [str(values)]
    return [str(v) for v in values if v is not None]


def _canon_ids(canon: dict[str, Any] | None) -> dict[str, list[str]]:
    """Extract the canonical ID lists from a ``canon`` dict (defensive)."""
    if not canon:
        return {}
    ids = canon.get("canonical_ids") or {}
    return {k: _str_list(v) for k, v in ids.items()}


# ---------------------------------------------------------------------------
# Pure: dependency graph
# ---------------------------------------------------------------------------


def build_dependency_graph(
    state: dict[str, Any], canon: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Assemble §31.2 dependency-graph edges (pure, no DB).

    Edges come from two sources:
    - control-state ``research_tasks`` ``dependencies`` (task -> task), and
    - canonical claim->evidence/derived->implementation_entity lineage
      (supplied via ``canon["lineage"]`` by the DB half).
    """
    edges: list[dict[str, Any]] = []

    tasks_map = state.get("research_tasks") or {}
    for task_id, task in tasks_map.items():
        for dep in task.get("dependencies", []) or []:
            edges.append(
                {
                    "from": _as_str(dep) or dep,
                    "to": _as_str(task_id),
                    "kind": "task_dep",
                }
            )

    for link in (canon or {}).get("lineage", []) or []:
        edges.append(
            {
                "from": _as_str(link.get("source")),
                "to": _as_str(link.get("target")),
                "kind": link.get("kind", "lineage"),
                "relation": link.get("relation"),
            }
        )
    return edges


# ---------------------------------------------------------------------------
# Pure: machine-readable manifest (§31.2)
# ---------------------------------------------------------------------------


def build_manifest(
    state: dict[str, Any],
    run_id: str,
    *,
    schema_version: str = "1.0",
    retrieval_endpoint: str | None = None,
    canon: dict[str, Any] | None = None,
    decision_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Assemble the §31.2 machine-readable manifest (pure, no DB).

    Control-state fields (objective, constraints, intent) are always
    available; canonical IDs / source snapshots / freshness come from ``canon``
    (populated by :func:`canonical_ids_by_run` on the DB path, empty on the
    no-DB path so p13 degrades to a control-state manifest).
    """
    intent = state.get("intent") or {}
    decisions_state = state.get("decisions") or []

    ids = _canon_ids(canon)
    snapshots = (canon or {}).get("source_snapshots") or []
    visibility = (canon or {}).get("source_visibility") or {}

    # Decisions staged during this handoff (DB path) override control-state
    # decision IDs; on the no-DB path these are control-state placeholders.
    if decision_ids is None:
        decision_ids = []

    # control-state decisions have no UUID; synthesize a stable placeholder id
    # so the manifest shape is stable on the no-DB path (DB path supplies real
    # canonical IDs via ``decision_ids``).
    control_decision_ids = [
        _as_str(d.get("id")) for d in decisions_state if d.get("id")
    ]

    user_constraints = list(intent.get("constraints") or [])
    user_decisions = state.get("user_decisions") or {}
    if user_decisions:
        user_constraints.extend(
            f"{k}={v}" for k, v in user_decisions.items()
        )

    dependency_graph = build_dependency_graph(state, canon)

    # Freshness / invalidation: any non-fresh canonical evidence under the run
    # invalidates the bundle; the manifest records what was seen.
    invalidated_by: list[str] = []
    if visibility:
        for stale_set in ("stale", "superseded", "rejected"):
            invalidated_by.extend(f"{stale_set}:{i}" for i in range(visibility.get(stale_set, 0)))

    return {
        "schema_version": schema_version,
        "version": schema_version,
        "run_id": run_id,
        "objective": intent.get("objective"),
        "source_snapshots": snapshots,
        "user_constraints": user_constraints,
        "requirement_ids": ids.get("requirement_ids", []),
        "topic_ids": ids.get("topic_ids", []),
        "decision_ids": decision_ids if decision_ids else control_decision_ids,
        "high_impact_claim_ids": ids.get("claim_ids", []),
        "implementation_entity_ids": ids.get("implementation_entity_ids", []),
        "unresolved_gap_ids": ids.get("gap_ids", []),
        "dependency_graph": dependency_graph,
        "document_map": {
            "manifest_version": schema_version,
            "sections": list(SECTION_FILES),
            "files": {f"{i:02d}": name for i, name in enumerate(SECTION_FILES)},
        },
        "freshness": {
            "generated_at": datetime.now(UTC).isoformat(),
            "source_visibility": visibility,
            "invalidated_by": invalidated_by,
            "stale": bool(invalidated_by),
        },
        "retrieval": {
            "contract": "§34",
            "endpoint": retrieval_endpoint,
            "key_types": list(RETRIEVAL_KEY_TYPES),
            "bounded": True,
        },
    }


# ---------------------------------------------------------------------------
# Pure: human-readable package (§31.1)
# ---------------------------------------------------------------------------

# Canonical IDs per §31.1 field, pulled from the manifest so section 03 can
# print a directory-shaped evidence index referencing real provenance IDs.
_SECTION_TITLES = {
    "00-executive": "Executive Summary",
    "01-requirements": "Requirements",
    "02-architecture": "Architecture",
    "03-source-system-understanding": "Source System Understanding",
    "04-implementation-plan": "Implementation Plan",
    "05-decisions": "Decisions",
    "06-risks-and-unknowns": "Risks and Unknowns",
    "07-evidence-index": "Evidence Index",
}


def _cite(manifest: dict[str, Any]) -> dict[str, list[str]]:
    """Collect canonical citation IDs per category from the manifest."""
    return {
        "requirements": manifest.get("requirement_ids", []),
        "topics": manifest.get("topic_ids", []),
        "decisions": manifest.get("decision_ids", []),
        "claims": manifest.get("high_impact_claim_ids", []),
        "entities": manifest.get("implementation_entity_ids", []),
        "gaps": manifest.get("unresolved_gap_ids", []),
        "sources": [s.get("locator") for s in manifest.get("source_snapshots", [])],
    }


def _section_header(idx: int, name: str) -> str:
    title = _SECTION_TITLES.get(name, name)
    return f"## §31.1/{idx:02d} {name} — {title}"


def build_document_package(state: dict[str, Any], manifest: dict[str, Any]) -> str:
    """Render the §31.1 8-section human-readable handoff package (pure).

    Each section cites the canonical research IDs it depends on (decision:<id>,
    evidence_unit:<id>, claim:<id>, implementation_entity:<id>, gap:<id>,
    topic:<id>) so the downstream coding agent can resolve provenance without
    re-reading the run. Section 03 is a directory-shaped block (repo maps /
    entity tables) embedded in the concatenated ``content`` (D4).
    """
    intent = state.get("intent") or {}
    cite = _cite(manifest)
    decisions = state.get("decisions") or []
    gaps = state.get("gaps") or []
    branches = state.get("branches") or {}

    parts: list[str] = []

    # 00-executive
    parts.append(_section_header(0, "00-executive"))
    parts.append(
        f"**Objective:** {manifest.get('objective') or '(unspecified)'}\n"
    )
    parts.append(
        f"**Run:** `{manifest.get('run_id')}`  "
        f"**Schema:** {manifest.get('schema_version')}\n"
    )
    dec_preview = [d.get("question") for d in decisions[:3]] if decisions else []
    if dec_preview:
        parts.append("**Key decisions:**")
        for q in dec_preview:
            parts.append(f"- {q}")
    parts.append(
        f"\n**Confidence/uncertainty:** {len(gaps)} unresolved gaps; "
        f"{len(branches)} branches published.\n"
    )
    if cite["decisions"]:
        parts.append(f"Decision IDs: {', '.join(f'decision:`{d}`' for d in cite['decisions'])}\n")
    if cite["topics"]:
        parts.append(f"Topic IDs: {', '.join(f'topic:`{t}`' for t in cite['topics'])}\n")

    # 01-requirements
    parts.append(_section_header(1, "01-requirements"))
    parts.append("**User intent:**\n")
    parts.append(f"- {manifest.get('objective') or intent.get('objective')}")
    constraints = manifest.get("user_constraints", [])
    if constraints:
        parts.append("\n**Constraints:**\n")
        for c in constraints:
            parts.append(f"- {c}")
    if cite["gaps"]:
        parts.append("\n**Unresolved user decisions / gaps (§31.1 cites):**\n")
        for g in cite["gaps"]:
            parts.append(f"- gap:`{g}`")

    # 02-architecture
    parts.append(_section_header(2, "02-architecture"))
    parts.append("**Components/boundaries:**\n")
    parts.append("- §10 control-plane state machine (`/workspace/repo/DRA/src/dra/control_plane.py`)\n")
    parts.append("- Evidence-graph publisher (`dra.publish`) — ADR-013 staged->canonical commit\n")
    parts.append("- Typed investigators (`dra.investigators.*`) — ADR-002 per-worker isolation\n")
    parts.append("\n**Dependencies / interfaces:**\n")
    sources = manifest.get("source_snapshots", [])
    if sources:
        for s in sources:
            parts.append(
                f"- source `{s.get('locator')}` (kind={s.get('kind')}, "
                f"version={s.get('version')})"
            )
    else:
        parts.append("- (no canonical source snapshots on the no-DB path)")
    if cite["entities"]:
        parts.append("\n**Implementation entities cited:** " + ", ".join(f"implementation_entity:`{e}`" for e in cite["entities"]))

    # 03-source-system-understanding (directory-shaped)
    parts.append(_section_header(3, "03-source-system-understanding"))
    parts.append("### Source map\n")
    if sources:
        parts.append("| locator | kind | version | license |")
        parts.append("|---|---|---|---|")
        for s in sources:
            parts.append(
                f"| `{s.get('locator')}` | {s.get('kind')} | {s.get('version')} | {s.get('license_spdx')} |"
            )
    else:
        parts.append("- (no-DB path: no source snapshots staged)")
    parts.append("\n### Entity tables (canonical)\n")
    parts.append("| category | count | sample IDs |")
    parts.append("|---|---|---|")
    for label, key in (
        ("requirements", "requirements"),
        ("topics", "topics"),
        ("decisions", "decisions"),
        ("high-impact claims", "claims"),
        ("implementation entities", "entities"),
        ("unresolved gaps", "gaps"),
    ):
        ids = cite[key]
        sample = ids[:3] if ids else ["—"]
        parts.append(f"| {label} | {len(ids)} | {', '.join(f'`{i}`' for i in sample)} |")

    # 04-implementation-plan
    parts.append(_section_header(4, "04-implementation-plan"))
    parts.append("**Build order / milestones:** derived from the §31.2 dependency graph.\n")
    edges = manifest.get("dependency_graph", [])
    if edges:
        parts.append("| from | to | kind |")
        parts.append("|---|---|---|")
        for e in edges[:20]:
            parts.append(f"| `{e.get('from')}` | `{e.get('to')}` | {e.get('kind')} |")
    else:
        parts.append("- (dependency graph: no edges on the no-DB path)")
    parts.append("\n**Test gates:** §38.4 verification gate (`dra.verification_gate`)")
    parts.append("\n**Assumptions:** no-DB degradation mirrors p12 (control-state-only)")

    # 05-decisions
    parts.append(_section_header(5, "05-decisions"))
    if decisions:
        for d in decisions:
            parts.append(f"- **{d.get('question')}**")
            parts.append(f"  - chosen: {d.get('chosen')}")
            parts.append(f"  - rationale: {d.get('rationale')}")
            if cite["decisions"]:
                parts.append(f"  - canonical id: `decision:{cite['decisions'][0]}`")
    else:
        parts.append("- (no decisions synthesized this run)")
    parts.append("\nReversal triggers carried from control state (§12).")

    # 06-risks-and-unknowns
    parts.append(_section_header(6, "06-risks-and-unknowns"))
    if gaps:
        for g in gaps:
            sev = g.get("severity", "medium")
            parts.append(f"- **[{sev}]** {g.get('description')} (blocking={g.get('blocking')})")
    else:
        parts.append("- (no gaps flagged)")
    parts.append("\n**Mitigations:** §33.1 dedicated reconnaissance breadth; §33.1 critic check.")
    parts.append("\n**Verification tasks:** §38.4 gate (p8); Phase 14 audit (p14).")
    if cite["gaps"]:
        parts.append("\n**Unresolved gap IDs:** " + ", ".join(f"gap:`{g}`" for g in cite["gaps"]))

    # 07-evidence-index
    parts.append(_section_header(7, "07-evidence-index"))
    parts.append("**Claims -> evidence locators (canonical IDs):**\n")
    claims = state.get("claims") or []
    if claims:
        for c in claims:
            ev = ", ".join(f"evidence_unit:`{e}`" for e in c.get("evidence_ids", []))
            parts.append(f"- `claim:{c.get('claim_id')}` — {c.get('text')[:80]} — {ev or 'no evidence'}")
    else:
        parts.append("- (no control-state claims on the no-DB path)")
    if cite["claims"]:
        parts.append("\n**Canonical claim IDs:** " + ", ".join(f"claim:`{c}`" for c in cite["claims"]))
    if cite["sources"]:
        parts.append("\n**Canonical source locators:** " + ", ".join(f"`{s}`" for s in cite["sources"]))

    return "\n".join(parts) + "\n"


# ---------------------------------------------------------------------------
# DB half: canonical provenance traversal + idempotent staging
# ---------------------------------------------------------------------------


async def canonical_ids_by_run(
    run_id: str, *, session: Any
) -> dict[str, Any]:
    """Fetch canonical IDs + source snapshots + visibility by run_id (DB).

    Mirrors the provenance-join shape used in
    ``tests/test_repo_control_plane_e2e.py``: ``prov_bundle(run_id) ->
    prov_entity(entity_kind, state) -> domain row``.  ``source_identity`` has
    no prov_entity row, so it joins through ``raw_capture``'s entity.
    """
    snapshots = (
        await session.execute(
            text(
                "SELECT si.locator, si.version, si.kind, si.license_spdx, "
                "si.access_basis "
                "FROM source_identity si "
                "JOIN raw_capture rc ON rc.source_id = si.id "
                "JOIN prov_entity pe ON pe.entity_kind='raw_capture' "
                "AND pe.content_hash = rc.content_hash "
                "JOIN prov_bundle pb ON pb.id = pe.bundle_id "
                "WHERE pb.run_id = :r AND pe.state = 'canonical'"
            ),
            {"r": run_id},
        )
    ).mappings().all()

    async def _ids(entity_kind: str, table: str, id_col: str = "id") -> list[str]:
        rows = await session.execute(
            text(
                f"SELECT t.{id_col} FROM {table} t "
                "JOIN prov_entity pe ON pe.entity_kind = :k AND pe.id = t.id "
                "JOIN prov_bundle pb ON pb.id = pe.bundle_id "
                "WHERE pb.run_id = :r AND pe.state = 'canonical'"
            ),
            {"k": entity_kind, "r": run_id},
        )
        return [str(r[0]) for r in rows.fetchall()]

    claim_ids = await _ids("claim", "claim")
    impl_ids = await _ids("implementation_entity", "implementation_entity")
    evidence_ids = await _ids("evidence_unit", "evidence_unit")
    # topics are a supporting table in 0002 (NO prov_entity row, NO run_id, NO
    # state column) — they are not run-scoped provenance entities, so there is
    # no canonical topic set to emit by run_id. gap IS an entity_kind and is
    # provenance-scoped, so it queries like the others.
    topic_ids: list[str] = []
    gap_ids = await _ids("gap", "gap")

    # Requirement IDs: the §14/0002 schema has NO dedicated ``requirement``
    # table — requirements are represented by canonical ``topic`` rows in
    # related missions. We do not silently relabel topics as requirement_ids
    # (kept empty + documented).
    requirement_ids: list[str] = []

    # Stale/superseded/rejected visibility (non-fresh canonical evidence under
    # the run), surfaced in the manifest's freshness/invalidation metadata.
    visibility: dict[str, int] = {}
    for state_val in ("stale", "superseded", "rejected"):
        cnt = await session.scalar(
            text(
                "SELECT count(*) FROM prov_entity pe "
                "JOIN prov_bundle pb ON pb.id = pe.bundle_id "
                "WHERE pb.run_id = :r AND pe.state = :s"
            ),
            {"r": run_id, "s": state_val},
        )
        visibility[state_val] = int(cnt or 0)
    total = await session.scalar(
        text(
            "SELECT count(*) FROM prov_entity pe "
            "JOIN prov_bundle pb ON pb.id = pe.bundle_id "
            "WHERE pb.run_id = :r"
        ),
        {"r": run_id},
    )
    visibility["total"] = int(total or 0)

    # Lineage edges (§21.2 claim->evidence + derivation chain), all scoped to
    # the run via prov_bundle. These back the dependency_graph's claim/entity
    # lineage (the impl->source link is captured separately below).
    evidence_claim_rows = (
        await session.execute(
            text(
                "SELECT eu.id AS evidence_id, cl.id AS claim_id "
                "FROM evidence_unit eu "
                "JOIN prov_entity pe_e ON pe_e.entity_kind='evidence_unit' AND pe_e.id=eu.id "
                "JOIN prov_bundle pb ON pb.id = pe_e.bundle_id "
                "LEFT JOIN claim cl ON cl.evidence_unit_id = eu.id "
                "WHERE pb.run_id = :r AND pe_e.state='canonical' AND cl.id IS NOT NULL"
            ),
            {"r": run_id},
        )
    ).mappings().all()

    lineage: list[dict[str, Any]] = []
    for row in evidence_claim_rows:
        lineage.append(
            {
                "source": str(row["evidence_id"]),
                "target": str(row["claim_id"]),
                "kind": "evidence",
                "relation": "claim-supports",
            }
        )

    # derivation: derived_artifact -> raw_capture (source_capture_hash)
    derivation_rows = (
        await session.execute(
            text(
                "SELECT da.id AS derived_id, rc.content_hash AS raw_hash "
                "FROM derived_artifact da "
                "JOIN prov_entity pe ON pe.entity_kind='derived_artifact' AND pe.id=da.id "
                "JOIN prov_bundle pb ON pb.id=pe.bundle_id "
                "JOIN raw_capture rc ON rc.content_hash = da.source_capture_hash "
                "WHERE pb.run_id = :r AND pe.state='canonical'"
            ),
            {"r": run_id},
        )
    ).mappings().all()
    for row in derivation_rows:
        lineage.append(
            {
                "source": str(row["derived_id"]),
                "target": str(row["raw_hash"]),
                "kind": "derivation",
                "relation": "derived-from",
            }
        )

    return {
        "source_snapshots": [dict(r) for r in snapshots],
        "canonical_ids": {
            "claim_ids": claim_ids,
            "decision_ids": await _ids("decision", "decision"),
            "implementation_entity_ids": impl_ids,
            "evidence_unit_ids": evidence_ids,
            "topic_ids": topic_ids,
            "gap_ids": gap_ids,
            "requirement_ids": requirement_ids,
        },
        "lineage": lineage,
        "source_visibility": visibility,
    }


async def _create_bundle(
    session: Any, run_id: str, task_id: str, label: str, actor: dict[str, Any] | None
) -> UUID:
    """Session-aware prov_bundle insert (stage_bundle opens its own txn)."""
    row = await session.execute(
        text(
            "INSERT INTO prov_bundle (run_id, task_id, label) "
            "VALUES (:run_id, :task_id, :label) RETURNING id"
        ),
        {"run_id": run_id, "task_id": task_id, "label": label},
    )
    return row.scalar_one()


async def _stage_decision_idempotent(
    session: Any,
    bundle_id: UUID,
    activity_id: UUID,
    dec: dict[str, Any],
    run_id: str,
    claim_id: UUID | None,
) -> UUID:
    """Stage a control-state decision, reusing any existing (run_id, text) row.

    Dedupe-by-(run_id, text) survives p13 re-invocation/resume so a re-run
    does not duplicate canonical decisions (D1: p13 owns decision staging
    because p12 is kept pure per the mission scope).
    """
    text_val = dec.get("rationale") or dec.get("question", "")
    existing = await session.execute(
        text(
            "SELECT pe.id FROM prov_entity pe JOIN decision d ON d.id = pe.id "
            "WHERE pe.entity_kind='decision' AND d.run_id = :r "
            "AND d.text = :t LIMIT 1"
        ),
        {"r": run_id, "t": text_val},
    )
    found = existing.scalar_one_or_none()
    if found is not None:
        return UUID(str(found))

    return await stage_decision(
        session,
        bundle_id,
        activity_id,
        claim_id=claim_id,
        decision_text=text_val,
        run_id=run_id,
        state="staged",
        rationale=dec.get("rationale") or dec.get("question"),
        metadata={
            "alternatives": dec.get("alternatives", []),
            "chosen": dec.get("chosen"),
            "consequences": dec.get("consequences", []),
            "reversal_triggers": dec.get("reversal_triggers", []),
            "user_preference_deps": dec.get("user_preference_deps", []),
        },
    )


async def _claim_for_decision(
    session: Any,
    dec: dict[str, Any],
    run_id: str,
    fallback_claim_ids: list[str],
) -> UUID | None:
    """Pick the canonical claim this decision anchors to (async, DB-backed).

    Matches the decision's control-state ``evidence_ids`` (canonical
    evidence_unit UUIDs on the DB path) to a canonical claim whose
    ``evidence_unit_id`` is in that set, scoped to the run. Falls back to the
    first canonical claim for the run.
    """
    ev_ids = [str(e) for e in (dec.get("evidence_ids") or [])]
    if ev_ids:
        matched = await session.execute(
            text(
                "SELECT cl.id FROM claim cl "
                "JOIN prov_entity pe ON pe.entity_kind='claim' AND pe.id = cl.id "
                "JOIN prov_bundle pb ON pb.id = pe.bundle_id "
                "WHERE cl.evidence_unit_id = ANY(:ev) AND pb.run_id = :r "
                "LIMIT 1"
            ),
            {"ev": ev_ids, "r": run_id},
        )
        found = matched.scalar_one_or_none()
        if found is not None:
            return UUID(str(found))
    for cid in fallback_claim_ids:
        try:
            return UUID(cid)
        except (ValueError, TypeError):
            continue
    return None


async def stage_section_handoff(
    state: dict[str, Any],
    run_id: str,
    actor: dict[str, Any] | None = None,
    *,
    task_id: str | None = None,
) -> UUID:
    """Stage the §33 handoff (machine manifest + human package) as canonical.

    Mirrors ``bake-off/evidence.py::synthesize_bundle``: opens its OWN
    ``async_session`` transaction (not the checkpointer's) so a staging failure
    rolls back only this bundle (ADR-013). Idempotently stages control-state
    decisions (D1), then builds the §31.2 manifest + §31.1 package from the
    canonical IDs it just staged, stages the handoff through
    :func:`dra.publish.stage_handoff`, and commits via :func:`publish_bundle`.

    Raises :class:`PublishError` (or DB errors) on failure — caught by p13 so
    the no-DB / degraded path never crashes the control DAG.
    """
    actor = actor or {}
    task_id = task_id or f"handoff:{run_id}"

    # Resolve async_session via the publish module (not a top-level binding) so
    # DB-gated tests that monkeypatch ``dra.publish.async_session`` (per-test
    # NullPool engine) are honored — the staging helpers above all take the
    # session explicitly and need no own binding.
    from dra import publish as _publish

    async with _publish.async_session() as session, session.begin():
        canon = await canonical_ids_by_run(run_id, session=session)
        bundle_id = await _create_bundle(
            session, run_id, task_id, "§33 handoff generation", actor
        )
        synth_act = await create_activity(
            session, bundle_id, "synthesis", actor
        )

        # D1: stage control-state decisions (p12 stays pure/control-state).
        decision_ids: list[str] = []
        claim_ids = canon["canonical_ids"].get("claim_ids", [])
        for dec in state.get("decisions") or []:
            claim_id = await _claim_for_decision(session, dec, run_id, claim_ids)
            did = await _stage_decision_idempotent(
                session, bundle_id, synth_act, dec, run_id, claim_id
            )
            decision_ids.append(str(did))

        # Fallback: a run with no p12 decisions still gets a handoff anchor.
        if not decision_ids and claim_ids:
            objective = (state.get("intent") or {}).get("objective") or "handoff"
            anchor = await _claim_for_decision(
                session, {"evidence_ids": []}, run_id, claim_ids
            )
            lead = await _stage_decision_idempotent(
                session,
                bundle_id,
                synth_act,
                {
                    "question": objective,
                    "rationale": "Auto-derived handoff anchor (no p12 decision).",
                    "evidence_ids": [],
                },
                run_id,
                anchor,
            )
            decision_ids.append(str(lead))

        lead_decision = UUID(decision_ids[0]) if decision_ids else None

        manifest = build_manifest(
            state,
            run_id,
            retrieval_endpoint="/knowledge",
            canon=canon,
            decision_ids=decision_ids,
        )
        content = build_document_package(state, manifest)

        handoff_id = await stage_handoff(
            session,
            bundle_id,
            synth_act,
            decision_id=lead_decision,
            manifest=manifest,
            run_id=run_id,
            content=content,
            state="staged",
            metadata={
                "schema_version": manifest["schema_version"],
                "section_count": len(SECTION_FILES),
                "retrieval_contract": "§34",
                "document_map": manifest["document_map"],
            },
        )
        await add_prov_edge(
            session, generated_entity_id=handoff_id, activity_id=synth_act
        )

        await publish_bundle(bundle_id, session=session)
        return handoff_id
