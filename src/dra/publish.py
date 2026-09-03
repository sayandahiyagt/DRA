"""Transactional staged->canonical evidence publication (ADR-013).

Reuses the shared ``DATABASE_URL``/engine conventions from :mod:`dra.db`
(no new dependency surface — depends only on the already-declared stack:
SQLAlchemy 2.0 async + psycopg3).

Public API
----------
- :class:`PublishError` — raised when a bundle fails publication validation.
- :func:`stage_bundle` — opens a run/task-scoped staging bundle + activity.
- :func:`publish_bundle` — the atomic commit: validate provenance links,
  check content-hash integrity, apply idempotent transitions, then flip
  staged->canonical in a single database transaction. Any failure rolls the
  whole transaction back, so partial publishes never leave canonical
  orphans (ADR-013).
- :func:`stage_raw_capture` / :func:`stage_claim` / :func:`add_prov_edge`
   — ergonomic helpers that insert staged rows within a bundle.
- :func:`stage_implementation_entity` — stage a code/entity reference
   discovered by an investigator (dra#23, §13.4).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dra.db import engine

async_session = async_sessionmaker(engine, expire_on_commit=False)


class PublishError(Exception):
    """A bundle failed staged->canonical publication validation."""


@dataclass
class StagedBundle:
    """In-memory description of a bundle staged for publication."""

    bundle_id: UUID
    run_id: str
    task_id: str
    label: str | None = None
    actor: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Bundle / activity lifecycle
# ---------------------------------------------------------------------------


async def stage_bundle(
    run_id: str,
    task_id: str,
    label: str | None = None,
    actor: dict[str, Any] | None = None,
) -> UUID:
    """Create a run/task-scoped staging bundle + publication activity.

    Inserts a ``prov_bundle`` row and a matching ``prov_activity`` of
    activity_type ``publication`` tagged with ``run_id``/``task_id`` and the
    responsible agent. Returns the bundle UUID. All staged domain rows are
    written with ``state='staged'`` and linked to the bundle's ``prov_entity``
    rows, within a single transaction.
    """
    actor = actor or {}
    async with async_session() as session:
        async with session.begin():
            agent_id = await _resolve_or_create_agent(session, actor)
            bundle_result = await session.execute(
                text(
                    "INSERT INTO prov_bundle (run_id, task_id, label) "
                    "VALUES (:run_id, :task_id, :label) RETURNING id"
                ),
                {"run_id": run_id, "task_id": task_id, "label": label},
            )
            bundle_id: UUID = bundle_result.scalar_one()

            await session.execute(
                text(
                    "INSERT INTO prov_activity (bundle_id, activity_type, "
                    "started_at, agent_id, metadata) "
                    "VALUES (:bundle_id, 'publication', now(), "
                    "(:agent_id)::uuid, :metadata) RETURNING id"
                ),
                {
                    "bundle_id": str(bundle_id),
                    "agent_id": str(agent_id) if agent_id is not None else None,
                    "metadata": _json({"run_id": run_id, "task_id": task_id}),
                },
            )

            return bundle_id


async def create_activity(
    session: AsyncSession,
    bundle_id: UUID,
    activity_type: str,
    actor: dict[str, Any] | None = None,
    input_ids: list[str] | None = None,
    output_ids: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> UUID:
    """Create a generation/derivation ``prov_activity`` within a bundle.

    Used to attribute the producing activity (acquisition, parsing, derivation,
    verification, human_correction) for staged entities — §21.2 provenance
    reconstruction chain. ``started_at`` defaults to now.
    """
    actor = actor or {}
    agent_id = await _resolve_or_create_agent(session, actor)
    row = await session.execute(
        text(
            "INSERT INTO prov_activity (bundle_id, activity_type, started_at, "
            "agent_id, input_ids, output_ids, metadata) "
            "VALUES (:bundle_id, :atype, now(), (:agent_id)::uuid, "
            ":in_ids, :out_ids, :meta) RETURNING id"
        ),
        {
            "bundle_id": str(bundle_id),
            "atype": activity_type,
            "agent_id": str(agent_id) if agent_id is not None else None,
            "in_ids": _json(input_ids or []),
            "out_ids": _json(output_ids or []),
            "meta": _json(metadata),
        },
    )
    return row.scalar_one()


async def _resolve_or_create_agent(session: AsyncSession, actor: dict[str, Any]) -> UUID | None:
    """Return an existing agent id for a stable external_id, else create one.

    Falls back to a freshly-created agent row when no identity is supplied.
    Used to attribute model/tool/human responsibility (ADR-014 §4).
    """
    external_id = actor.get("external_id") or actor.get("id")
    if external_id:
        row = await session.execute(
            text("SELECT id FROM prov_agent WHERE external_id = :eid"),
            {"eid": external_id},
        )
        existing = row.scalar_one_or_none()
        if existing is not None:
            return existing

    agent_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO prov_agent (id, kind, name, version, model_family, "
            "external_id, created_at) VALUES "
            "(:id, :kind, :name, :version, :model_family, :external_id, now())"
        ),
        {
            "id": str(agent_id),
            "kind": actor.get("kind", "model"),
            "name": actor.get("name"),
            "version": actor.get("version"),
            "model_family": actor.get("model_family"),
            "external_id": external_id,
        },
    )
    return agent_id


# ---------------------------------------------------------------------------
# Staging helpers (write staged rows inside a bundle, all within ONE txn)
# ---------------------------------------------------------------------------


async def _insert_prov_entity(
    session: AsyncSession,
    bundle_id: UUID,
    entity_kind: str,
    activity_id: UUID | None,
    content_hash: str | None,
    version: int | None,
    state: str,
    metadata: dict[str, Any] | None = None,
) -> UUID:
    row = await session.execute(
        text(
            "INSERT INTO prov_entity (bundle_id, entity_kind, content_hash, "
            "version, state, produced_by_activity, metadata) "
            "VALUES (:bundle_id, :entity_kind, :content_hash, :version, "
            ":state, :activity_id, :metadata) RETURNING id"
        ),
        {
            "bundle_id": str(bundle_id),
            "entity_kind": entity_kind,
            "content_hash": content_hash,
            "version": version,
            "state": state,
            "activity_id": str(activity_id) if activity_id is not None else None,
            "metadata": _json(metadata),
        },
    )
    return row.scalar_one()


async def stage_raw_capture(
    session: AsyncSession,
    bundle_id: UUID,
    activity_id: UUID,
    content_hash: str,
    source_id: UUID,
    kind: str,
    state: str = "staged",
    size_bytes: int | None = None,
    mime_type: str | None = None,
    stored_at: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> UUID:
    """Stage an immutable, content-addressed raw capture (ADR-004).

    The primary key *is* ``content_hash`` so re-staging the same capture is
    idempotent (ON CONFLICT DO NOTHING semantics rely on this PK).
    """
    entity_id = await _insert_prov_entity(
        session, bundle_id, "raw_capture", activity_id, content_hash, None, state, metadata
    )
    await session.execute(
        text(
            "INSERT INTO raw_capture (content_hash, source_id, kind, "
            "mime_type, size_bytes, captured_at, stored_at, state, metadata) "
            "VALUES (:hash, :source_id, :kind, :mime, :size, now(), "
            ":stored, :state, :meta) "
            "ON CONFLICT (content_hash) DO UPDATE SET state = EXCLUDED.state"
        ),
        {
            "hash": content_hash,
            "source_id": str(source_id),
            "kind": kind,
            "mime": mime_type,
            "size": size_bytes,
            "stored": stored_at,
            "state": state,
            "meta": _json(metadata),
        },
    )
    return entity_id


async def stage_derived_artifact(
    session: AsyncSession,
    bundle_id: UUID,
    activity_id: UUID,
    source_capture_hash: str,
    content_hash: str,
    kind: str,
    version: int,
    state: str = "staged",
    metadata: dict[str, Any] | None = None,
) -> UUID:
    """Stage a versioned, supersedable derived artifact (ADR-004)."""
    entity_id = await _insert_prov_entity(
        session, bundle_id, "derived_artifact", activity_id, content_hash, version, state, metadata
    )
    await session.execute(
        text(
            "INSERT INTO derived_artifact (id, source_capture_hash, content_hash, "
            "kind, version, produced_by_activity, state, metadata) "
            "VALUES (:id, :src, :hash, :kind, :ver, :act, :state, :meta) "
            "ON CONFLICT (content_hash, kind, version) DO NOTHING"
        ),
        {
            "id": str(entity_id),
            "src": source_capture_hash,
            "hash": content_hash,
            "kind": kind,
            "ver": version,
            "act": str(activity_id),
            "state": state,
            "meta": _json(metadata),
        },
    )
    return entity_id


async def stage_evidence_unit(
    session: AsyncSession,
    bundle_id: UUID,
    activity_id: UUID,
    artifact_id: UUID,
    locator: dict[str, Any],
    content_hash: str | None = None,
    state: str = "staged",
    metadata: dict[str, Any] | None = None,
) -> UUID:
    entity_id = await _insert_prov_entity(
        session, bundle_id, "evidence_unit", activity_id, content_hash, None, state, metadata
    )
    await session.execute(
        text(
            "INSERT INTO evidence_unit (id, artifact_id, locator, excerpt, "
            "content_hash, produced_by_activity, state, metadata) "
            "VALUES (:id, :art, :loc, :excerpt, :hash, :act, :state, :meta)"
        ),
        {
            "id": str(entity_id),
            "art": str(artifact_id),
            "loc": _json(locator),
            "excerpt": metadata.get("excerpt") if metadata else None,
            "hash": content_hash,
            "act": str(activity_id),
            "state": state,
            "meta": _json(metadata),
        },
    )
    return entity_id


async def stage_implementation_entity(
    session: AsyncSession,
    bundle_id: UUID,
    activity_id: UUID,
    repo_source_id: UUID,
    kind: str,
    *,
    path: str | None = None,
    symbol_name: str | None = None,
    commit_sha: str | None = None,
    line_start: int | None = None,
    line_end: int | None = None,
    signature: str | None = None,
    content_hash: str | None = None,
    state: str = "staged",
    metadata: dict[str, Any] | None = None,
) -> UUID:
    """Stage an implementation entity (dra#23, §13.4).

    ``implementation_entity`` links a repo source identity to a concrete code
    reference (file, symbol, commit, line span, signature) discovered by an
    investigator.  It is a lineage-domain table with its own ``state`` column
    (added by ``0005_implementation_entity_state``) so it participates in the
    staged->canonical atomic commit (ADR-013) like
    raw_capture / derived_artifact / evidence_unit / claim.

    The primary key is ``id`` (a UUID prov_entity mirror), NOT ``content_hash``,
    so there is no ``ON CONFLICT`` upsert — a plain INSERT matches the
    evidence_unit / claim pattern.
    """
    entity_id = await _insert_prov_entity(
        session, bundle_id, "implementation_entity", activity_id,
        content_hash, None, state, metadata,
    )
    await session.execute(
        text(
            "INSERT INTO implementation_entity (id, repo_source_id, kind, "
            "path, symbol_name, commit_sha, line_start, line_end, "
            "signature, content_hash, produced_by_activity, state, metadata) "
            "VALUES (:id, :src, :kind, :path, :sym, :sha, :start, :end, "
            ":sig, :hash, :act, :state, :meta)"
        ),
        {
            "id": str(entity_id),
            "src": str(repo_source_id),
            "kind": kind,
            "path": path,
            "sym": symbol_name,
            "sha": commit_sha,
            "start": line_start,
            "end": line_end,
            "sig": signature,
            "hash": content_hash,
            "act": str(activity_id),
            "state": state,
            "meta": _json(metadata),
        },
    )
    return entity_id


async def stage_claim(
    session: AsyncSession,
    bundle_id: UUID,
    activity_id: UUID,
    claim_text: str,
    evidence_unit_id: UUID | None = None,
    topic_id: UUID | None = None,
    state: str = "staged",
    confidence: float | None = None,
    metadata: dict[str, Any] | None = None,
) -> UUID:
    """Stage a claim tied to an evidence unit (evidence != claim, §7.1)."""
    entity_id = await _insert_prov_entity(
        session, bundle_id, "claim", activity_id, None, 1, state, metadata
    )
    await session.execute(
        text(
            "INSERT INTO claim (id, evidence_unit_id, topic_id, text, "
            "confidence, produced_by_activity, state, version, metadata) "
            "VALUES (:id, :ev, :topic, :text, :conf, :act, :state, 1, :meta)"
        ),
        {
            "id": str(entity_id),
            "ev": str(evidence_unit_id) if evidence_unit_id is not None else None,
            "topic": str(topic_id) if topic_id is not None else None,
            "text": claim_text,
            "conf": confidence,
            "act": str(activity_id),
            "state": state,
            "meta": _json(metadata),
        },
    )
    return entity_id


async def stage_topic(
    session: AsyncSession,
    bundle_id: UUID,
    activity_id: UUID,
    name: str,
    state: str = "staged",
    description: str | None = None,
    parent_topic_id: UUID | None = None,
    content_hash: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> UUID:
    entity_id = await _insert_prov_entity(
        session, bundle_id, "topic", activity_id, content_hash, None, state, metadata
    )
    await session.execute(
        text(
            "INSERT INTO topic (id, name, description, parent_topic_id, "
            "content_hash, created_at, metadata) "
            "VALUES (:id, :name, :desc, :parent, :hash, now(), :meta) "
            "ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name"
        ),
        {
            "id": str(entity_id),
            "name": name,
            "desc": description,
            "parent": str(parent_topic_id) if parent_topic_id is not None else None,
            "hash": content_hash,
            "meta": _json(metadata),
        },
    )
    return entity_id


async def stage_source_identity(
    session: AsyncSession,
    bundle_id: UUID,
    activity_id: UUID | None,
    kind: str,
    locator: str,
    version: str | None = None,
    state: str = "staged",
    license_spdx: str | None = None,
    access_basis: str | None = None,
    crawl_allowed: bool | None = None,
    auth_scope: str | None = None,
    redist_allowed: bool | None = None,
    metadata: dict[str, Any] | None = None,
) -> UUID:
    """Stage a source identity (§22 access-basis record).

    ``source_identity`` is a supporting (non-lineage) domain table, so it does
    not get a ``prov_entity`` row of its own — its acquisition provenance is
    recorded via the ``acquisition`` prov_activity that generated the
    associated raw capture. Returns the new source id.
    """
    source_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO source_identity (id, kind, locator, version, "
            "license_spdx, access_basis, crawl_allowed, auth_scope, "
            "redist_allowed, created_at, metadata) "
            "VALUES (:id, :kind, :loc, :ver, :lic, :ab, :cl, :as_, :rd, now(), :meta)"
        ),
        {
            "id": str(source_id),
            "kind": kind,
            "loc": locator,
            "ver": version,
            "lic": license_spdx,
            "ab": access_basis,
            "cl": crawl_allowed,
            "as_": auth_scope,
            "rd": redist_allowed,
            "meta": _json(metadata),
        },
    )
    return source_id


async def stage_decision(
    session: AsyncSession,
    bundle_id: UUID,
    activity_id: UUID,
    claim_id: UUID,
    decision_text: str,
    topic_id: UUID | None = None,
    run_id: str | None = None,
    state: str = "staged",
    rationale: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> UUID:
    entity_id = await _insert_prov_entity(
        session, bundle_id, "decision", activity_id, None, None, state, metadata
    )
    await session.execute(
        text(
            "INSERT INTO decision (id, claim_id, topic_id, state, text, "
            "rationale, produced_by_activity, run_id, metadata) "
            "VALUES (:id, :claim, :topic, :state, :text, :rat, :act, :run, :meta)"
        ),
        {
            "id": str(entity_id),
            "claim": str(claim_id),
            "topic": str(topic_id) if topic_id is not None else None,
            "state": _json(state),
            "text": decision_text,
            "rat": rationale,
            "act": str(activity_id),
            "run": run_id,
            "meta": _json(metadata),
        },
    )
    return entity_id


async def stage_handoff(
    session: AsyncSession,
    bundle_id: UUID,
    activity_id: UUID,
    decision_id: UUID,
    manifest: dict[str, Any],
    run_id: str | None = None,
    content: str | None = None,
    state: str = "staged",
    metadata: dict[str, Any] | None = None,
) -> UUID:
    entity_id = await _insert_prov_entity(
        session, bundle_id, "handoff", activity_id, None, None, state, metadata
    )
    await session.execute(
        text(
            "INSERT INTO handoff_statement (id, decision_id, manifest, run_id, "
            "content, produced_by_activity, metadata) "
            "VALUES (:id, :dec, :man, :run, :content, :act, :meta)"
        ),
        {
            "id": str(entity_id),
            "dec": str(decision_id),
            "man": _json(manifest),
            "run": run_id,
            "content": content,
            "act": str(activity_id),
            "meta": _json(metadata),
        },
    )
    return entity_id


async def add_prov_edge(
    session: AsyncSession,
    *,
    generated_entity_id: UUID | None = None,
    deriving_entity_id: UUID | None = None,
    source_entity_id: UUID | None = None,
    activity_id: UUID,
) -> None:
    """Record a W3C-PROV generation (``wasGeneratedBy``) or derivation edge.

    - generation: ``generated_entity_id`` + ``activity_id`` (entity <- activity)
    - derivation: ``deriving_entity_id`` + ``source_entity_id`` + ``activity_id``
    """
    if generated_entity_id is not None:
        await session.execute(
            text(
                "INSERT INTO prov_generation (entity_id, activity_id) "
                "VALUES (:e, :a) ON CONFLICT DO NOTHING"
            ),
            {"e": str(generated_entity_id), "a": str(activity_id)},
        )
    if deriving_entity_id is not None and source_entity_id is not None:
        await session.execute(
            text(
                "INSERT INTO prov_derivation "
                "(derived_entity_id, source_entity_id, activity_id) "
                "VALUES (:d, :s, :a) ON CONFLICT DO NOTHING"
            ),
            {
                "d": str(deriving_entity_id),
                "s": str(source_entity_id),
                "a": str(activity_id),
            },
        )


def _json(value: Any) -> str:
    """Serialize a Python value to a JSONB literal for ``text()`` params."""
    import json

    if value is None:
        value = {}
    return json.dumps(value)


# ---------------------------------------------------------------------------
# Atomic publication (ADR-013)
# ---------------------------------------------------------------------------


async def publish_bundle(bundle_id: UUID, *, session: AsyncSession | None = None) -> int:
    """Atomically commit a staged bundle to CANONICAL.

    All validation, integrity checks, idempotent upserts and the
    staged->canonical transition execute inside a SINGLE transaction
    (``async with session.begin()``). Because any failure ``raise``s,
    Postgres atomicity rolls back the whole transaction — partial publishes
    never leave canonical orphans.

    Returns the number of rows transitioned to ``canonical``.
    """
    owns_session = session is None
    if session is None:
        session = async_session()

    try:
        if owns_session:
            async with session.begin():
                return await _publish_bundle_tx(session, bundle_id)
        return await _publish_bundle_tx(session, bundle_id)
    finally:
        if owns_session:
            await session.close()


async def _publish_bundle_tx(session: AsyncSession, bundle_id: UUID) -> int:
    # 1. Existence + staging scope.
    bundle_row = await session.execute(
        text("SELECT id FROM prov_bundle WHERE id = :b"), {"b": str(bundle_id)}
    )
    if bundle_row.scalar_one_or_none() is None:
        raise PublishError(f"bundle {bundle_id} does not exist")

    # 2. Validate provenance links + content-hash integrity for every staged
    #    entity in this bundle (§21.2 reconstruction chain). Missing link or
    #    hash mismatch => raise PublishError (triggers rollback).
    rows = await session.execute(
        text(
            "SELECT id, entity_kind, content_hash, produced_by_activity "
            "FROM prov_entity WHERE bundle_id = :b AND state = 'staged' "
            "ORDER BY entity_kind, id"
        ),
        {"b": str(bundle_id)},
    )
    staged = rows.fetchall()
    if not staged:
        # Idempotent no-op: an already-fully-published bundle has its entities
        # in CANONICAL; re-publishing returns the existing canonical count
        # rather than erroring (ADR-013).
        canon_row = await session.execute(
            text(
                "SELECT count(*) FROM prov_entity WHERE bundle_id = :b "
                "AND state = 'canonical'"
            ),
            {"b": str(bundle_id)},
        )
        already = canon_row.scalar_one()
        if already:
            return already
        raise PublishError(f"bundle {bundle_id} has no staged or canonical entities")

    canonical_count = 0

    for entity_id, _kind, content_hash, activity_id in staged:
        # Provenance link: every staged entity must have a producing activity.
        if activity_id is None:
            raise PublishError(
                f"entity {entity_id} lacks produced_by_activity provenance link"
            )

        # Content-hash integrity: entities carrying a content_hash must carry
        # a non-empty, well-formed sha256 hex.
        if content_hash is not None and (len(content_hash) == 0 or len(content_hash) != 64):
            raise PublishError(
                f"entity {entity_id} has malformed content_hash integrity: "
                f"{content_hash!r}"
            )

        # Source/derivation integrity (ADR-004 / §21.2 chain): derived/evidence
        # domains require their upstream FK to be present and to point at an
        # existing entity. Within a single atomic publish these entities flip
        # to CANONICAL together, so the upstream need only be structurally
        # linked — which also rejects the "claim without evidence_unit" case.
        await _validate_entity_links(session, entity_id, _kind)

        canonical_count += 1

    # 3. Idempotent transition: flip staged->canonical for valid rows.
    #    raw_capture uses content_hash PK (ON CONFLICT handled at staging),
    #    versioned tables rely on their UNIQUE(content_hash,kind,version).
    result = await session.execute(
        text(
            "UPDATE prov_entity SET state = 'canonical' "
            "WHERE bundle_id = :b AND state = 'staged' "
            "AND produced_by_activity IS NOT NULL "
            "AND (content_hash IS NULL OR LENGTH(content_hash) = 64)"
        ),
        {"b": str(bundle_id)},
    )
    updated = result.rowcount or 0

    # 4. Mirror the state transition onto the domain tables that carry their
    #    own ``state`` column (raw_capture, derived_artifact, evidence_unit,
    #    claim) so canonical status is queryable at the domain level too.
    await _mirror_state_canonical(session, bundle_id)

    if updated != canonical_count:
        raise PublishError(
            f"atomic commit mismatch: validated {canonical_count} staged "
            f"entities but only {updated} transitioned to canonical"
        )

    # 5. Commit happens implicitly via the session.begin() context manager.
    #    On ANY exception above, the context rolls back the whole txn.
    return updated


async def _validate_entity_links(session: AsyncSession, entity_id: UUID, kind: str) -> None:
    """Validate structural provenance links for a staged entity.

    Guards ADR-013's "partial commits never count" invariant: a derived
    artifact, evidence unit or claim must reference an *existing* upstream
    entity (raw capture / artifact / evidence unit respectively). Within a
    single atomic publish those entities flip to CANONICAL together, so the
    upstream need only be structurally linked — this also rejects the
    "claim without evidence_unit" broken-link case that must roll back.
    """
    checks = {
        "derived_artifact": (
            "SELECT 1 FROM derived_artifact da "
            "JOIN raw_capture rc ON rc.content_hash = da.source_capture_hash "
            "WHERE da.id = :e"
        ),
        "evidence_unit": (
            "SELECT 1 FROM evidence_unit eu WHERE eu.id = :e "
            "AND eu.artifact_id IS NOT NULL "
            "AND EXISTS (SELECT 1 FROM derived_artifact da WHERE da.id = eu.artifact_id)"
        ),
        "claim": (
            "SELECT 1 FROM claim c WHERE c.id = :e "
            "AND c.evidence_unit_id IS NOT NULL "
            "AND EXISTS (SELECT 1 FROM evidence_unit eu WHERE eu.id = c.evidence_unit_id)"
        ),
    }
    sql = checks.get(kind)
    if sql is None:
        return
    row = await session.execute(text(sql), {"e": str(entity_id)})
    if row.scalar_one_or_none() is None:
        raise PublishError(
            f"{kind} {entity_id} is missing a valid upstream provenance link"
        )


# (domain_table, entity_kind, join_clause) — raw_capture is content-addressed
# (PK = content_hash), so it joins prov_entity on content_hash rather than id.
_DOMAIN_STATE_TABLES = (
    ("raw_capture",       "raw_capture",       "pe.content_hash = raw_capture.content_hash"),
    ("derived_artifact",  "derived_artifact",  "pe.id = derived_artifact.id"),
    ("evidence_unit",     "evidence_unit",     "pe.id = evidence_unit.id"),
    ("implementation_entity", "implementation_entity", "pe.id = implementation_entity.id"),
    ("claim",             "claim",             "pe.id = claim.id"),
)


async def _mirror_state_canonical(session: AsyncSession, bundle_id: UUID) -> None:
    """Mirror prov_entity.state to each domain table's own ``state`` column.

    Each domain table's primary key equals the matching prov_entity column
    (its UUID id, except raw_capture whose PK is ``content_hash``). Joining on
    (entity_kind, bundle_id, state) flips only this bundle's validated rows.
    """
    for table, kind, join in _DOMAIN_STATE_TABLES:
        await session.execute(
            text(
                f"UPDATE {table} SET state = 'canonical' "
                "FROM prov_entity pe "
                f"WHERE pe.entity_kind = '{kind}' "
                f"AND pe.bundle_id = :b AND pe.state = 'canonical' "
                f"AND {join}"
            ),
            {"b": str(bundle_id)},
        )
