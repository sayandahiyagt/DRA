"""§34 downstream retrieval contract (spec §34 / §31.3).

A bounded :class:`ImplementationContextBundle` retriever over the canonical
evidence tables — **linkage-based, not vector similarity** (dra#15: the
``vector_embedding`` column is an enum tag only and ``proof_corpus`` is
standalone; there is no canonical vector corpus table, so §34 retrieval is
join-based over `implementation_entity / evidence_unit / claim / decision /
topic / gap`, never a corpus dump).

Each key builder is a self-contained provenance-join query filtered by
``run_id`` (ADR-016: cross-run knowledge is excluded). Bundles are capped
(default 50 claims / 50 evidence / 20 entities) so a single request can never
``It should not dump the entire corpus into the coding agent context`` (§34 L2311).
"""

from __future__ import annotations

from typing import Any, TypedDict
from uuid import UUID

from sqlalchemy import text

__all__ = [
    "RETRIEVAL_KEY_TYPES",
    "ImplementationContextBundle",
    "bundle_bounds",
    "retrieve_context_bundle",
]

#: The seven §34 key types a coding agent may request by.
RETRIEVAL_KEY_TYPES = [
    "requirement",
    "topic",
    "entity",
    "milestone",
    "repo_path",
    "symbol",
    "decision",
    "semantic",
]

#: Default per-bundle result caps (§34 "bounded" contract).
_DEFAULT_BOUNDS = {"claims": 50, "evidence": 50, "entities": 20}


def bundle_bounds() -> dict[str, int]:
    """Return the current §34 bundle caps (copied so callers can mutate)."""
    return dict(_DEFAULT_BOUNDS)


def _coerce_uuid(value: Any) -> UUID | None:
    try:
        return UUID(str(value))
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Bundle shape (§34)
# ---------------------------------------------------------------------------


class ImplementationContextBundle(TypedDict, total=True):
    """Bounded context a coding agent needs to act on a single retrieval key."""

    immediate_objective: str | None
    user_constraints: list[str]
    architecture_decisions: list[dict[str, Any]]
    implementation_entities: list[dict[str, Any]]
    high_value_claims: list[dict[str, Any]]
    evidence_locators: list[dict[str, Any]]
    unresolved_gaps: list[dict[str, Any]]
    tests_acceptance: list[str]


def _empty(objective: str | None, constraints: list[str]) -> dict[str, Any]:
    return {
        "immediate_objective": objective,
        "user_constraints": constraints,
        "architecture_decisions": [],
        "implementation_entities": [],
        "high_value_claims": [],
        "evidence_locators": [],
        "unresolved_gaps": [],
        "tests_acceptance": [],
    }


def _merge(base: dict[str, Any], **partial: Any) -> dict[str, Any]:
    """Overlay non-empty partial bundle fields onto ``base``."""
    out = dict(base)
    for k, v in partial.items():
        if v:
            out[k] = v
    return out


# ---------------------------------------------------------------------------
# DB: manifest base (objective + constraints by run)
# ---------------------------------------------------------------------------


async def _manifest_base(session: Any, run_id: str) -> tuple[str | None, list[str]]:
    """Load immediate objective + user constraints from the §33 manifest."""
    row = await session.execute(
        text(
            "SELECT manifest FROM handoff_statement WHERE run_id = :r "
            "ORDER BY created_at DESC LIMIT 1"
        ),
        {"r": run_id},
    )
    manifest = row.scalar_one_or_none()
    if not manifest:
        return None, []
    if isinstance(manifest, (bytes, bytearray)):
        import json as _json

        manifest = _json.loads(manifest)
    obj = manifest.get("objective") if isinstance(manifest, dict) else None
    constraints = manifest.get("user_constraints", []) if isinstance(manifest, dict) else []
    return obj, list(constraints or [])


# ---------------------------------------------------------------------------
# Per-key builders (each returns partial bundle fields; linkage-only queries)
# ---------------------------------------------------------------------------


_IMPL_COLS = "ie.id, ie.kind, ie.path, ie.symbol_name, ie.commit_sha, ie.repo_source_id"


async def _by_decision(session: Any, run_id: str, decision_id: UUID) -> dict[str, Any]:
    """decision -> claim -> evidence_unit -> derived_artifact -> source_identity."""
    rows = (
        await session.execute(
            text(
                "SELECT d.id AS decision_id, d.text, d.rationale, "
                "cl.id AS claim_id, cl.text AS claim_text, "
                "eu.id AS ev_id, eu.locator AS ev_locator, "
                "si.locator AS source_locator, si.kind AS source_kind, "
                "si.version AS source_version "
                "FROM decision d "
                "JOIN prov_entity pe_d ON pe_d.entity_kind='decision' AND pe_d.id=d.id "
                "JOIN prov_bundle pb ON pb.id=pe_d.bundle_id AND pb.run_id=:r "
                "LEFT JOIN claim cl ON cl.id=d.claim_id "
                "LEFT JOIN evidence_unit eu ON eu.id=cl.evidence_unit_id "
                "LEFT JOIN derived_artifact da ON da.id=eu.artifact_id "
                "LEFT JOIN content_blob cb ON cb.hash = da.source_capture_hash "
                "LEFT JOIN source_capture sc ON sc.content_blob_hash = cb.hash "
                "LEFT JOIN source_identity si ON si.id = sc.source_identity_id "
                "WHERE d.id = :did LIMIT 1"
            ),
            {"r": run_id, "did": str(decision_id)},
        )
    ).mappings().all()

    decisions: list[dict[str, Any]] = []
    evidence_locators: list[dict[str, Any]] = []
    claims: list[dict[str, Any]] = []
    ev_ids: list[str] = []
    source_ids: list[str] = []
    for row in rows:
        decisions.append(
            {
                "id": str(row["decision_id"]),
                "text": row["text"],
                "rationale": row["rationale"],
                "claim_id": str(row["claim_id"]) if row["claim_id"] else None,
            }
        )
        if row["claim_id"] and row["claim_text"]:
            claims.append({"id": str(row["claim_id"]), "text": row["claim_text"]})
        if row["ev_id"]:
            ev_ids.append(str(row["ev_id"]))
            evidence_locators.append(
                {
                    "evidence_unit": str(row["ev_id"]),
                    "locator": row["ev_locator"],
                    "source_locator": row["source_locator"],
                    "source_kind": row["source_kind"],
                    "source_version": row["source_version"],
                }
            )
        if row["source_locator"]:
            source_ids.append(str(row["source_locator"]))

    entities: list[dict[str, Any]] = []
    for sid in {s for s in source_ids if s}:
        entities.extend(await _entities_for_source(session, run_id, sid))

    # acceptance criteria live on evidence_unit.metadata.spec_section / claim metadata
    tests = await _tests_acceptance(session, run_id, ev_ids)
    return _merge(
        {},
        architecture_decisions=decisions,
        evidence_locators=evidence_locators,
        high_value_claims=claims,
        implementation_entities=entities,
        tests_acceptance=tests,
    )


async def _entities_for_source(session: Any, run_id: str, source_locator: str) -> list[dict[str, Any]]:
    """implementation_entity rows for a source locator, run-scoped."""
    rows = (
        await session.execute(
            text(
                "SELECT ie.id, ie.kind, ie.path, ie.symbol_name, ie.commit_sha "
                "FROM implementation_entity ie "
                "JOIN prov_entity pe ON pe.entity_kind='implementation_entity' AND pe.id=ie.id "
                "JOIN prov_bundle pb ON pb.id=pe.bundle_id AND pb.run_id=:r "
                "JOIN source_identity si ON si.id=ie.repo_source_id "
                "WHERE si.locator = :loc AND pe.state='canonical'"
            ),
            {"r": run_id, "loc": source_locator},
        )
    ).mappings().all()
    return [
        {
            "id": str(r["id"]),
            "kind": r["kind"],
            "path": r["path"],
            "symbol_name": r["symbol_name"],
            "commit_sha": r["commit_sha"],
        }
        for r in rows
    ]


async def _tests_acceptance(session: Any, run_id: str, evidence_ids: list[str]) -> list[str]:
    """Surface acceptance-test hints from evidence_unit/claim metadata.

    Pulls ``metadata->>'spec_section'`` and any ``metadata->'acceptance'``
    entries tied to the retrieved evidence.
    """
    if not evidence_ids:
        return []
    rows = await session.execute(
        text(
            "SELECT DISTINCT eu.metadata->>'spec_section' AS spec, "
            "eu.metadata->'acceptance' AS acceptance "
            "FROM evidence_unit eu "
            "JOIN prov_entity pe ON pe.entity_kind='evidence_unit' AND pe.id=eu.id "
            "JOIN prov_bundle pb ON pb.id=pe.bundle_id AND pb.run_id=:r "
            "WHERE eu.id = ANY(:ids) AND pe.state='canonical'"
        ),
        {"r": run_id, "ids": evidence_ids},
    )
    tests: list[str] = []
    for r in rows.mappings():
        spec = r["spec"]
        if spec:
            tests.append(f"§{spec} acceptance check")
    return tests or ["§38.4 verification gate (dra.verification_gate)"]


_IMPL_COLS = "ie.id, ie.kind, ie.path, ie.symbol_name, ie.commit_sha, ie.repo_source_id"
_IMPL_PROV = (
    "FROM implementation_entity ie "
    "JOIN prov_entity pe ON pe.entity_kind='implementation_entity' AND pe.id=ie.id "
    "JOIN prov_bundle pb ON pb.id=pe.bundle_id "
)
_EU_PROV = (
    "FROM evidence_unit eu "
    "JOIN prov_entity pe_e ON pe_e.entity_kind='evidence_unit' AND pe_e.id=eu.id "
    "JOIN prov_bundle pb ON pb.id=pe_e.bundle_id "
)


def _impl_row(r: Any) -> dict[str, Any]:
    return {
        "id": str(r["id"]),
        "kind": r["kind"],
        "path": r["path"],
        "symbol_name": r["symbol_name"],
        "commit_sha": r["commit_sha"],
        "repo_source_id": str(r["repo_source_id"]) if r["repo_source_id"] else None,
    }


async def _evidence_for_impl(
    session: Any, run_id: str, path: str | None, symbol: str | None, bounds: dict[str, int]
) -> dict[str, Any]:
    """evidence_units whose repo locator references a path/symbol + backing claims."""
    args: dict[str, Any] = {"r": run_id, "limit": bounds["evidence"]}
    clauses: list[str] = ["pb.run_id=:r", "pe_e.state='canonical'"]
    if path is not None:
        clauses.append("(eu.locator->>'path') = :path")
        args["path"] = path
    if symbol is not None:
        clauses.append("(eu.locator->>'symbol') = :symbol")
        args["symbol"] = symbol

    rows = (
        await session.execute(
            text(
                "SELECT eu.id, eu.locator, eu.excerpt, "
                "cl.id AS claim_id, cl.text AS claim_text "
                f"{_EU_PROV} "
                "LEFT JOIN claim cl ON cl.evidence_unit_id=eu.id "
                "WHERE " + " AND ".join(clauses) + " "
                "ORDER BY eu.id LIMIT :limit"
            ),
            args,
        )
    ).mappings().all()

    evidence_locators: list[dict[str, Any]] = []
    claims: list[dict[str, Any]] = []
    ev_ids: list[str] = []
    for r in rows:
        if r["id"]:
            ev_ids.append(str(r["id"]))
            evidence_locators.append(
                {
                    "evidence_unit": str(r["id"]),
                    "locator": r["locator"],
                    "excerpt": r["excerpt"],
                }
            )
        if r["claim_id"] and r["claim_text"]:
            claims.append({"id": str(r["claim_id"]), "text": r["claim_text"]})
    return {
        "evidence_locators": evidence_locators[: bounds["evidence"]],
        "high_value_claims": claims[: bounds["claims"]],
        "_ev_ids": ev_ids,
    }


async def _bundle_for_impl_rows(
    rows: list[Any], session: Any, run_id: str, bounds: dict[str, int]
) -> dict[str, Any]:
    """Assemble a partial bundle (entities + evidence + claims) for impl rows."""
    entities = [_impl_row(r) for r in rows]
    evidence_parts: list[dict[str, Any]] = []
    claim_parts: list[dict[str, Any]] = []
    for r in rows:
        part = await _evidence_for_impl(
            session, run_id, r["path"], r["symbol_name"], bounds
        )
        evidence_parts.extend(part["evidence_locators"])
        claim_parts.extend(part["high_value_claims"])
    seen_ev = {e["evidence_unit"]: e for e in evidence_parts}
    seen_cl = {c["id"]: c for c in claim_parts}
    return {
        "implementation_entities": entities[: bounds["entities"]],
        "evidence_locators": list(seen_ev.values())[: bounds["evidence"]],
        "high_value_claims": list(seen_cl.values())[: bounds["claims"]],
    }


async def _by_entity(
    session: Any, run_id: str, entity_id: UUID, bounds: dict[str, int]
) -> dict[str, Any]:
    """implementation_entity by id, run-scoped, with its evidence/claims."""
    rows = (
        await session.execute(
            text(
                f"SELECT {_IMPL_COLS} {_IMPL_PROV} "
                "WHERE pb.run_id=:r AND pe.state='canonical' AND ie.id=:eid LIMIT 1"
            ),
            {"r": run_id, "eid": str(entity_id)},
        )
    ).mappings().all()
    if not rows:
        return {"implementation_entities": [], "evidence_locators": [], "high_value_claims": []}
    return await _bundle_for_impl_rows(rows, session, run_id, bounds)


async def _by_repo_path(
    session: Any, run_id: str, path: str, bounds: dict[str, int]
) -> dict[str, Any]:
    """implementation_entity by repo path, run-scoped."""
    if not path:
        return {"implementation_entities": [], "evidence_locators": [], "high_value_claims": []}
    rows = (
        await session.execute(
            text(
                f"SELECT {_IMPL_COLS} {_IMPL_PROV} WHERE pb.run_id=:r "
                "AND pe.state='canonical' AND ie.path = :p LIMIT :lim"
            ),
            {"r": run_id, "p": path, "lim": bounds["entities"]},
        )
    ).mappings().all()
    return await _bundle_for_impl_rows(rows, session, run_id, bounds)


async def _by_symbol(
    session: Any, run_id: str, symbol: str, bounds: dict[str, int]
) -> dict[str, Any]:
    """implementation_entity by symbol name, run-scoped."""
    rows = (
        await session.execute(
            text(
                f"SELECT {_IMPL_COLS} {_IMPL_PROV} WHERE pb.run_id=:r "
                "AND pe.state='canonical' AND ie.symbol_name = :s LIMIT :lim"
            ),
            {"r": run_id, "s": symbol, "lim": bounds["entities"]},
        )
    ).mappings().all()
    return await _bundle_for_impl_rows(rows, session, run_id, bounds)


async def _by_topic_or_requirement(
    session: Any, run_id: str, topic_id: UUID, bounds: dict[str, int]
) -> dict[str, Any]:
    """topic (requirements are topics per the §14/0002 schema — no req table).

    ``topic`` is a supporting table with NO ``prov_entity`` row, no ``run_id``,
    and no ``state`` column (see ``tests/_verification.py:_stage_topic`` and the
    ``entity_kind`` enum at ``0002_evidence_schema.py:59`` which omits
    ``topic``). So we resolve the topic row directly by id and then walk its
    ``claim``/``decision``/``gap`` ``topic_id`` FKs — each scoped to the run via
    that row's own ``prov_entity`` -> ``prov_bundle(run_id)`` linkage (the
    function's prior claim/gap/decision sub-queries are already correct; they
    were only dead because gated on the impossible ``entity_kind='topic'``
    provenance match).
    """
    topic = (
        await session.execute(
            text(
                "SELECT t.id, t.name, t.description FROM topic t "
                "WHERE t.id = :tid LIMIT 1"
            ),
            {"tid": str(topic_id)},
        )
    ).mappings().first()
    if not topic:
        return {
            "architecture_decisions": [],
            "high_value_claims": [],
            "evidence_locators": [],
            "unresolved_gaps": [],
        }

    claim_rows = (
        await session.execute(
            text(
                "SELECT cl.id, cl.text, cl.evidence_unit_id FROM claim cl "
                "JOIN prov_entity pe ON pe.entity_kind='claim' AND pe.id=cl.id "
                "JOIN prov_bundle pb ON pb.id=pe.bundle_id AND pb.run_id=:r "
                "WHERE cl.topic_id = :tid AND pe.state='canonical' "
                "ORDER BY cl.id LIMIT :lim"
            ),
            {"r": run_id, "tid": str(topic_id), "lim": bounds["claims"]},
        )
    ).mappings().all()

    # decisions citing any of these claims (via decision.claim_id)
    claim_ids = [str(r["id"]) for r in claim_rows]
    dec_rows: list[Any] = []
    if claim_ids:
        dec_rows = (
            await session.execute(
                text(
                    "SELECT d.id, d.text FROM decision d "
                    "JOIN prov_entity pe ON pe.entity_kind='decision' AND pe.id=d.id "
                    "JOIN prov_bundle pb ON pb.id=pe.bundle_id AND pb.run_id=:r "
                    "WHERE d.claim_id = ANY(:cids) AND pe.state='canonical'"
                ),
                {"r": run_id, "cids": claim_ids},
            )
        ).mappings().all()

    gaps = (
        await session.execute(
            text(
                "SELECT g.id, g.description, g.severity FROM gap g "
                "JOIN prov_entity pe ON pe.entity_kind='gap' AND pe.id=g.id "
                "JOIN prov_bundle pb ON pb.id=pe.bundle_id AND pb.run_id=:r "
                "WHERE g.topic_id = :tid AND pe.state='canonical'"
            ),
            {"r": run_id, "tid": str(topic_id)},
        )
    ).mappings().all()

    # Evidence backing the retrieved claims (run-scoped via prov_entity).
    ev_ids = [str(r["evidence_unit_id"]) for r in claim_rows if r["evidence_unit_id"]]
    evidence_locators: list[dict[str, Any]] = []
    if ev_ids:
        ev_rows = (
            await session.execute(
                text(
                    "SELECT eu.id, eu.locator, eu.excerpt FROM evidence_unit eu "
                    "JOIN prov_entity pe_e ON pe_e.entity_kind='evidence_unit' "
                    "AND pe_e.id=eu.id "
                    "JOIN prov_bundle pb ON pb.id=pe_e.bundle_id AND pb.run_id=:r "
                    "WHERE eu.id = ANY(:eids) AND pe_e.state='canonical'"
                ),
                {"r": run_id, "eids": ev_ids},
            )
        ).mappings().all()
        evidence_locators = [
            {"evidence_unit": str(r["id"]), "locator": r["locator"], "excerpt": r["excerpt"]}
            for r in ev_rows
        ]

    return {
        "architecture_decisions": [
            {"id": str(r["id"]), "text": r["text"]} for r in dec_rows
        ],
        "high_value_claims": [
            {"id": str(r["id"]), "text": r["text"]} for r in claim_rows
        ],
        "evidence_locators": evidence_locators[: bounds["evidence"]],
        "unresolved_gaps": [
            {"id": str(r["id"]), "description": r["description"], "severity": r["severity"]}
            for r in gaps
        ],
    }


async def _by_milestone(
    session: Any, run_id: str, milestone: str, bounds: dict[str, int]
) -> dict[str, Any]:
    """Resolve a milestone name against the §33 manifest's dependency_graph
    nodes, then pull bundles for the referenced entity IDs (D2)."""
    manifest_row = await session.execute(
        text(
            "SELECT manifest FROM handoff_statement WHERE run_id = :r "
            "ORDER BY created_at DESC LIMIT 1"
        ),
        {"r": run_id},
    )
    manifest = manifest_row.scalar_one_or_none()
    if isinstance(manifest, (bytes, bytearray)):
        import json as _json

        manifest = _json.loads(manifest)
    if not isinstance(manifest, dict):
        return {
            "architecture_decisions": [],
            "high_value_claims": [],
            "implementation_entities": [],
            "evidence_locators": [],
        }
    graph = manifest.get("dependency_graph", []) if isinstance(manifest.get("dependency_graph"), list) else []
    target = str(milestone).lower()
    node_ids: list[str] = []
    for edge in graph:
        for end in ("from", "to"):
            val = edge.get(end)
            if val and target in str(val).lower():
                node_ids.append(str(val))

    # node_ids that parse as UUIDs that we can fetch canonical impl entities for
    impl_ids: list[UUID] = []
    for nid in node_ids:
        u = _coerce_uuid(nid)
        if u is not None:
            impl_ids.append(u)

    entities: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    claims: list[dict[str, Any]] = []
    for uid in impl_ids[: bounds["entities"]]:
        partial = await _by_entity(session, run_id, uid, bounds)
        entities.extend(partial.get("implementation_entities", []))
        evidence.extend(partial.get("evidence_locators", []))
        claims.extend(partial.get("high_value_claims", []))
    return {
        "implementation_entities": entities[: bounds["entities"]],
        "evidence_locators": evidence[: bounds["evidence"]],
        "high_value_claims": claims[: bounds["claims"]],
    }


async def _by_semantic(
    session: Any, run_id: str, query: str, bounds: dict[str, int]
) -> dict[str, Any]:
    """Linkage-based full-text retrieval (ILIKE, NOT vector — dra#15).

    Scans claim / decision / gap / topic / evidence_unit / implementation_entity
    text columns across canonical rows for the run, returning a bounded set of
    high-value claims, evidence locators and entities that mention the query.
    """
    if not query:
        return {
            "high_value_claims": [],
            "evidence_locators": [],
            "implementation_entities": [],
            "architecture_decisions": [],
        }
    q = f"%{query}%"
    ev_rows: list[Any] = []

    ev_rows = (await session.execute(text(
        "SELECT eu.id, eu.locator, eu.excerpt FROM evidence_unit eu "
        "JOIN prov_entity pe ON pe.entity_kind='evidence_unit' AND pe.id=eu.id "
        "JOIN prov_bundle pb ON pb.id=pe.bundle_id AND pb.run_id=:r "
        "WHERE pe.state='canonical' AND (eu.excerpt ILIKE :q) LIMIT :lim"
    ), {"r": run_id, "q": q, "lim": bounds["evidence"]})).mappings().all()

    claim_rows = (await session.execute(text(
        "SELECT cl.id, cl.text FROM claim cl "
        "JOIN prov_entity pe ON pe.entity_kind='claim' AND pe.id=cl.id "
        "JOIN prov_bundle pb ON pb.id=pe.bundle_id AND pb.run_id=:r "
        "WHERE pe.state='canonical' AND (cl.text ILIKE :q) LIMIT :lim"
    ), {"r": run_id, "q": q, "lim": bounds["claims"]})).mappings().all()

    dec_rows = (await session.execute(text(
        "SELECT d.id, d.text FROM decision d "
        "JOIN prov_entity pe ON pe.entity_kind='decision' AND pe.id=d.id "
        "JOIN prov_bundle pb ON pb.id=pe.bundle_id AND pb.run_id=:r "
        "WHERE pe.state='canonical' AND (d.text ILIKE :q) LIMIT :lim"
    ), {"r": run_id, "q": q, "lim": bounds["claims"]})).mappings().all()

    gap_rows = (await session.execute(text(
        "SELECT g.id, g.description, g.severity FROM gap g "
        "JOIN prov_entity pe ON pe.entity_kind='gap' AND pe.id=g.id "
        "JOIN prov_bundle pb ON pb.id=pe.bundle_id AND pb.run_id=:r "
        "WHERE pe.state='canonical' AND (g.description ILIKE :q)"
    ), {"r": run_id, "q": q})).mappings().all()

    impl_rows = (await session.execute(text(
        "SELECT ie.id, ie.kind, ie.path, ie.symbol_name, ie.commit_sha FROM implementation_entity ie "
        "JOIN prov_entity pe ON pe.entity_kind='implementation_entity' AND pe.id=ie.id "
        "JOIN prov_bundle pb ON pb.id=pe.bundle_id AND pb.run_id=:r "
        "WHERE pe.state='canonical' AND (ie.path ILIKE :q OR ie.symbol_name ILIKE :q) "
        "LIMIT :lim"
    ), {"r": run_id, "q": q, "lim": bounds["entities"]})).mappings().all()

    return {
        "high_value_claims": [
            {"id": str(r["id"]), "text": r["text"]} for r in claim_rows
        ] + [{"id": str(r["id"]), "text": r["text"], "kind": "decision"} for r in dec_rows],
        "evidence_locators": [
            {"evidence_unit": str(r["id"]), "locator": r["locator"], "excerpt": r["excerpt"]}
            for r in ev_rows
        ],
        "implementation_entities": [
            {
                "id": str(r["id"]), "kind": r["kind"], "path": r["path"],
                "symbol_name": r["symbol_name"], "commit_sha": r["commit_sha"],
            } for r in impl_rows
        ],
        "unresolved_gaps": [
            {"id": str(r["id"]), "description": r["description"], "severity": r["severity"]}
            for r in gap_rows
        ],
    }


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


async def retrieve_context_bundle(
    *,
    session: Any | None = None,
    run_id: str,
    by: dict[str, Any],
    bounds: dict[str, int] | None = None,
) -> ImplementationContextBundle:
    """Return a bounded §34 ``ImplementationContextBundle`` for exactly one key.

    ``by`` must contain exactly one of :data:`RETRIEVAL_KEY_TYPES`. Raises
    :class:`ValueError` if zero or more than one key is supplied.

    The retrieval is linkage-based over the canonical evidence tables
    (decision/claim/evidence_unit/derived_artifact/source_capture/content_blob/
    source_identity/implementation_entity/topic/gap) — there is NO vector-corpus
    scan (dra#15). Every query is filtered on ``prov_bundle.run_id = :run_id`` so cross-run
    knowledge is excluded (ADR-016).
    """
    if len(by) != 1:
        raise ValueError(
            f"retrieve_context_bundle requires exactly one key in `by`, got {len(by)}: {list(by)}"
        )
    key, value = next(iter(by.items()))
    if key not in RETRIEVAL_KEY_TYPES:
        raise ValueError(
            f"unknown retrieval key {key!r}; expected one of {RETRIEVAL_KEY_TYPES}"
        )

    owned_session = session is None
    if owned_session:
        from dra import publish as _publish

        session = _publish.async_session()
    bds = bounds or bundle_bounds()
    try:
        objective, constraints = await _manifest_base(session, run_id)
        bundle: dict[str, Any] = _empty(objective, constraints)

        if key == "decision":
            did = _coerce_uuid(value)
            if did is None:
                return _empty(objective, constraints)  # type: ignore[return-value]
            partial = await _by_decision(session, run_id, did)
        elif key == "entity":
            eid = _coerce_uuid(value)
            if eid is None:
                return _empty(objective, constraints)  # type: ignore[return-value]
            partial = await _by_entity(session, run_id, eid, bds)
        elif key == "repo_path":
            partial = await _by_repo_path(session, run_id, str(value), bds)
        elif key == "symbol":
            partial = await _by_symbol(session, run_id, str(value), bds)
        elif key in ("requirement", "topic"):
            tid = _coerce_uuid(value)
            if tid is None:
                return _empty(objective, constraints)  # type: ignore[return-value]
            partial = await _by_topic_or_requirement(session, run_id, tid, bds)
        elif key == "milestone":
            partial = await _by_milestone(session, run_id, str(value), bds)
        elif key == "semantic":
            partial = await _by_semantic(session, run_id, str(value), bds)
        else:  # pragma: no cover - guarded by the key validation above
            return _empty(objective, constraints)  # type: ignore[return-value]

        bundle = _merge(bundle, **partial)
        return bundle  # type: ignore[return-value]
    finally:
        if owned_session:
            await session.close()  # type: ignore[union-attr]
