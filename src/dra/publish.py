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
- :func:`stage_source_capture` / :func:`stage_claim` / :func:`add_prov_edge`
   — ergonomic helpers that insert staged rows within a bundle.
- :func:`stage_source_candidate` — discover a search-engine snippet/returned URL
   as a SourceCandidate (§140) without reifying it as a Claim (§38/§39, dra#79).
- :func:`stage_content_blob` — write durable bytes via a BlobStore and upsert
   a ``content_blob`` row (Wave 1a, dra#78).
- :func:`stage_implementation_entity` — stage a code/entity reference
   discovered by an investigator (dra#23, §13.4).
- :func:`stage_user_assertion` — stage a versioned human/maintainer assertion
   (ADR-017, dra#44), standalone outside ``entity_kind``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dra.db import engine
from dra.storage import BlobStore

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
    async with async_session() as session, session.begin():
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


async def stage_content_blob(
    session: AsyncSession,
    hash: str,
    data: bytes | None,
    mime_type: str | None,
    size_bytes: int | None,
    blob_store: BlobStore | None,
) -> str | None:
    """Stage a content-addressed blob via the BlobStore + ``content_blob`` table.

    Writes *data* through *blob_store* when both are supplied and upserts the
    ``content_blob`` row keyed by *hash* (``ON CONFLICT DO NOTHING`` — the blob
    is immutable, identical bytes never change metadata).  When *data* or
    *blob_store* is absent (e.g. synthetic test fixtures) a ``memory://``
    placeholder URI is written so the FK from ``source_capture`` /
    ``derived_artifact`` remains satisfiable.

    Returns the ``storage_uri`` (or ``None`` if only the DB row was created).
    """
    if data is not None and blob_store is not None:
        storage_uri = await blob_store.put(data, hash, mime_type)
    else:
        storage_uri = f"memory://{hash}"
    await session.execute(
        text(
            "INSERT INTO content_blob (hash, size, mime_type, storage_uri, "
            "encryption_metadata, created_at) "
            "VALUES (:hash, :size, :mime, :uri, :meta, now()) "
            "ON CONFLICT (hash) DO NOTHING"
        ),
        {
            "hash": hash,
            "size": size_bytes,
            "mime": mime_type,
            "uri": storage_uri,
            "meta": _json({}),
        },
    )
    return storage_uri


async def stage_source_capture(
    session: AsyncSession,
    bundle_id: UUID,
    activity_id: UUID,
    source_id: UUID,
    content_hash: str,
    kind: str,
    *,
    blob_store: BlobStore | None = None,
    data: bytes | None = None,
    size_bytes: int | None = None,
    mime_type: str | None = None,
    captured_at: str | None = None,
    final_url: str | None = None,
    redirect_chain: list[dict[str, Any]] | None = None,
    method: str | None = None,
    provider: str | None = None,
    http_metadata: dict[str, Any] | None = None,
    origin: str | None = None,
    publisher: str | None = None,
    metadata: dict[str, Any] | None = None,
    state: str = "staged",
) -> UUID:
    """Stage a content-addressed source capture (§43 model, dra#78 Wave 1a).

    Replaces the deprecated ``stage_raw_capture``: routes bytes through a
    :class:`~dra.storage.BlobStore` and a ``ContentBlob`` (deduped by sha256)
    referenced by a new ``SourceCapture`` row, while *also* writing a backward-
    compatible ``raw_capture`` row so existing readers (knowledge.py,
    verification_gate.py, handoff.py) keep working during the deprecation
    window.

    The provenance ``prov_entity`` is created with ``entity_kind='raw_capture'``
    (no new enum value — preserves the dra#14 byte-stability contract) and its
    UUID id is reused as ``source_capture.capture_id`` so the
    ``_DOMAIN_STATE_TABLES`` mirror join ``pe.id = source_capture.capture_id``
    holds.  Returns the prov_entity id.

    *origin* / *publisher* (§156) are forwarded to the
    ``source_representation`` row so that multiple pages on one site, sharing
    an origin, no longer collapse into a single ``source_identity`` — the
    representation carries the exact ``final_url`` as ``canonical_url`` while
    origin/publisher are recorded as separate metadata columns.
    """
    entity_id = await _insert_prov_entity(
        session, bundle_id, "raw_capture", activity_id, content_hash, None, state, metadata
    )

    # 1. Stage the content blob (durable bytes + content_blob row).
    await stage_content_blob(
        session, content_hash, data, mime_type, size_bytes, blob_store
    )

    # 2. Stage a source_representation (canonical URL + HTTP/access metadata).
    representation_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO source_representation (id, content_blob_hash, "
            "canonical_url, origin, publisher, http_status, http_headers, "
            "access_metadata, retrieved_at) "
            "VALUES (:id, :hash, :url, :origin, :pub, :status, :hdrs, "
            ":am, :ret) "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {
             "id": str(representation_id),
            "hash": content_hash,
            "url": final_url,
            "origin": origin,
            "pub": publisher,
            "status": (http_metadata or {}).get("status"),
            "hdrs": _json(http_metadata),
            "am": _json({}),
            "ret": captured_at,
        },
    )

    # 3. Stage the source_capture (authoritative acquisition event).
    await session.execute(
        text(
            "INSERT INTO source_capture (capture_id, source_identity_id, "
            "representation_id, content_blob_hash, kind, state, captured_at, "
            "final_url, redirect_chain, method, provider, http_metadata, "
            "size_bytes, mime_type, created_at, metadata) "
            "VALUES (:cid, :src, :rep, :hash, :kind, :state, "
            "COALESCE(:capt, now()), :url, :chain, :method, :prov, "
            ":httpm, :sz, :mime, now(), :meta) "
            "ON CONFLICT (capture_id) DO UPDATE SET "
            "state = EXCLUDED.state"
        ),
        {
            "cid": str(entity_id),
            "src": str(source_id),
            "rep": str(representation_id),
            "hash": content_hash,
            "kind": kind,
            "state": state,
            "capt": captured_at,
            "url": final_url,
            "chain": _json(redirect_chain),
            "method": method,
            "prov": provider,
            "httpm": _json(http_metadata),
            "sz": size_bytes,
            "mime": mime_type,
            "meta": _json(metadata),
        },
    )

    # 4. Backward-compatible raw_capture row (deprecated, kept for readers that
    #    still JOIN through raw_capture).  stored_at is marked obsolete — prefer
    #    content_blob.storage_uri for durable location.
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
            "stored": final_url,
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

    Uses a concurrency-safe ``ON CONFLICT (normalized_key)`` upsert (§41/§159):
    identical (kind, locator, version) tuples return the existing id rather than
    inserting a duplicate.  Nullable scalar fields are merged with ``COALESCE``
    so an existing row's populated values are never clobbered by a re-staging
    with a partial attribute set; ``metadata`` is merged via JSONB
    concatenation.
    """
    normalized_key = f"{kind}:{locator}:{version or ''}"
    source_id = uuid.uuid4()
    row = await session.execute(
        text(
            "INSERT INTO source_identity (id, kind, locator, version, "
            "normalized_key, license_spdx, access_basis, crawl_allowed, "
            "auth_scope, redist_allowed, created_at, metadata) "
            "VALUES (:id, :kind, :loc, :ver, :nk, :lic, :ab, :cl, :as_, "
            ":rd, now(), :meta) "
            "ON CONFLICT (normalized_key) DO UPDATE SET "
            "license_spdx = COALESCE(source_identity.license_spdx, EXCLUDED.license_spdx), "
            "access_basis = COALESCE(source_identity.access_basis, EXCLUDED.access_basis), "
            "crawl_allowed = COALESCE(source_identity.crawl_allowed, EXCLUDED.crawl_allowed), "
            "auth_scope = COALESCE(source_identity.auth_scope, EXCLUDED.auth_scope), "
            "redist_allowed = COALESCE(source_identity.redist_allowed, EXCLUDED.redist_allowed), "
            "metadata = source_identity.metadata || EXCLUDED.metadata "
            "RETURNING id"
        ),
        {
            "id": str(source_id),
            "kind": kind,
            "loc": locator,
            "ver": version,
            "nk": normalized_key,
            "lic": license_spdx,
            "ab": access_basis,
            "cl": crawl_allowed,
            "as_": auth_scope,
            "rd": redist_allowed,
            "meta": _json(metadata),
        },
    )
    return row.scalar_one()


async def stage_crawl_manifest_entry(
    session: AsyncSession,
    bundle_id: UUID,
    activity_id: UUID | None,
    *,
    url: str,
    origin: str,
    result: str,
    step: str | None = None,
    reason: str | None = None,
    latency_ms: float | None = None,
    status: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> UUID:
    """Stage a crawl-manifest entry (§11.4 ladder / RFC 9309 bookkeeping).

    ``web_crawl_manifest`` is a supporting log table (dra#26, migration
    0007): each acquisition attempt — attempted / skipped / crawled — is
    recorded here with the step, reason and latency so the investigator's
    crawl surface is auditable without re-running it.  Skipped (e.g.
    RFC 9309 robots exclusion) entries carry the skip reason; crawled entries
    reference the step that succeeded.

    Entries are written in the caller's transaction (typically
    :class:`~dra.investigators.InvestigatorContext`) so a rolled-back publish
    also rolls back the manifest.
    """
    entry_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO web_crawl_manifest (id, bundle_id, activity_id, "
            "url, origin, result, step, reason, latency_ms, status, "
            "attempted_at, metadata) "
            "VALUES (:id, :bundle, :act, :url, :origin, :result, :step, "
            ":reason, :lat, :status, now(), :meta)"
        ),
        {
            "id": str(entry_id),
            "bundle": str(bundle_id),
            "act": str(activity_id) if activity_id is not None else None,
            "url": url,
            "origin": origin,
            "result": result,
             "step": step,
            "reason": reason,
            "lat": latency_ms,
            "status": status,
            "meta": _json(metadata),
        },
    )
    return entry_id


async def stage_source_candidate(
    session: AsyncSession,
    bundle_id: UUID,
    activity_id: UUID | None,
    *,
    query: str,
    purpose: str | None,
    provider: str | None,
    title: str | None,
    returned_url: str,
    snippet: str | None = None,
    rank: int | None = None,
    provider_score: float | None = None,
    origin: str | None = None,
    publisher: str | None = None,
    metadata: dict[str, Any] | None = None,
    state: str = "staged",
) -> UUID:
    """Stage a SourceCandidate discovery result (§140/§141, dra#79 Wave 1b).

    A SourceCandidate records a single search-engine / discovery result
    (``returned_url`` + ``snippet``) **without** reifying it as an
    ``EvidenceUnit``→``Claim`` (§38/§39).  Per §141 it is "not canonical source
    evidence" — it may become a source only after selection and capture.

    The candidate "carries its own ``source_representation``" (D3): a
    ``source_representation`` row is inserted with ``canonical_url = returned_url``
    and ``origin``/``publisher`` recorded as metadata (§156), so that multiple
    pages on one site no longer collapse into a single identity.  The
    ``content_blob_hash`` on the representation is left NULL — the snippet is
    carried directly on the candidate row as a ``snippet`` TEXT column (§140),
    and no full-content capture has been performed yet.

    Delegates to the caller's acquisition ``prov_activity`` (bound via
    ``activity_id``) within the bundle's transaction.  Returns the candidate id.
    """
    candidate_id = uuid.uuid4()

    # 1. Stage the candidate's own source_representation, keyed by the exact
    #    returned_url (§156 — NOT site origin).  origin/publisher are recorded as
    #    separate columns so distinct pages on one site keep separate identities.
    representation_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO source_representation (id, content_blob_hash, "
            "canonical_url, origin, publisher, http_status, http_headers, "
            "access_metadata, retrieved_at) "
            "VALUES (:id, :hash, :url, :origin, :pub, :status, :hdrs, "
            ":am, :ret) "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {
            "id": str(representation_id),
            "hash": None,
            "url": returned_url,
            "origin": origin,
            "pub": publisher,
            "status": None,
            "hdrs": _json({}),
            "am": _json({}),
            "ret": None,
        },
    )

    # 2. Insert the candidate row, linked to the representation.
    await session.execute(
        text(
            "INSERT INTO source_candidate (candidate_id, bundle_id, "
            "representation_id, produced_by_activity, query, purpose, provider, "
            "title, returned_url, snippet, rank, provider_score, discovered_at, "
            "state, metadata) "
             "VALUES (:cid, :bid, :rep, :act, :q, :purpose, :prov, :title, "
             ":url, :snippet, :rank, :score, COALESCE(:disc, now()), :state, :meta)"
        ),
        {
            "cid": str(candidate_id),
            "bid": str(bundle_id),
            "rep": str(representation_id),
            "act": str(activity_id) if activity_id is not None else None,
            "q": query,
            "purpose": purpose,
            "prov": provider,
            "title": title,
            "url": returned_url,
            "snippet": snippet,
            "rank": rank,
            "score": provider_score,
            "disc": None,
            "state": state,
            "meta": _json(metadata),
        },
    )
    return candidate_id


async def stage_user_assertion(
    session: AsyncSession,
    bundle_id: UUID,
    activity_id: UUID,
    assertion_type: str,
    question: str,
    value: Any | None = None,
    *,
    run_id: str | None = None,
    task_id: str | None = None,
    superseded_by: UUID | None = None,
    disputed_claim_id: UUID | None = None,
    disputed_decision_id: UUID | None = None,
    disputed_source_id: UUID | None = None,
    state: str = "staged",
    metadata: dict[str, Any] | None = None,
) -> UUID:
    """Stage a versioned user/maintainer assertion (ADR-017, dra#44).

    Stands ALONE (no prov_entity row): ``user_assertion`` is deliberately kept
    outside ``entity_kind`` so the dra#14 introspection contract stays
    byte-stable.  It is anchored to the provenance graph via
    ``produced_by_activity`` (-> prov_activity) and, through provenance, its
    bundle; its ``state`` is flipped to ``canonical`` by :func:`publish_bundle`
    via a bundle-scoped mirror (see ``_STANDALONE_STATE_TABLES``).
    """
    assertion_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO user_assertion (id, bundle_id, run_id, task_id, "
            "question, value, assertion_type, superseded_by, produced_by_activity, "
            "disputed_claim_id, disputed_decision_id, disputed_source_id, "
            "state, metadata) "
            "VALUES (:id, :b, :run, :task, :q, :val, :typ, :sup, :act, "
            ":c, :d, :s, :state, :meta)"
        ),
        {
            "id": str(assertion_id),
            "b": str(bundle_id),
            "run": run_id,
            "task": task_id,
            "q": question,
            "val": _json(value),
            "typ": assertion_type,
            "sup": str(superseded_by) if superseded_by is not None else None,
            "act": str(activity_id),
            "c": str(disputed_claim_id) if disputed_claim_id is not None else None,
            "d": str(disputed_decision_id) if disputed_decision_id is not None else None,
            "s": str(disputed_source_id) if disputed_source_id is not None else None,
            "state": state,
            "meta": _json(metadata),
        },
    )
    return assertion_id


async def stage_decision(
    session: AsyncSession,
    bundle_id: UUID,
    activity_id: UUID,
    claim_id: UUID | None,
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
    decision_id: UUID | None,
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


async def stage_gap(
    session: AsyncSession,
    bundle_id: UUID,
    activity_id: UUID,
    description: str,
    severity: str = "medium",
    *,
    topic_id: UUID | None = None,
    decision_id: UUID | None = None,
    state: str = "staged",
    metadata: dict[str, Any] | None = None,
) -> UUID:
    """Stage a gap entity (spec §16.2 critical-content verification, §11.9).

    A ``gap`` is a provenance-anchored finding that a parser output could not
    be reconciled against its visual source (or that parsers disagreed on).
    The ``gap`` table is a supporting table — like ``handoff_statement`` it
    carries no ``state`` column of its own; its staged→canonical transition is
    tracked via ``prov_entity.state`` (gap is excluded from
    ``_DOMAIN_STATE_TABLES`` in :func:`publish_bundle`, consistent with
    ``source_identity`` and ``handoff_statement``).

    Returns the new ``prov_entity`` id (the canonical provenance anchor).
    """
    entity_id = await _insert_prov_entity(
        session, bundle_id, "gap", activity_id, None, None, state, metadata,
    )
    await session.execute(
        text(
            "INSERT INTO gap (id, topic_id, description, severity, "
            "decision_id, produced_by_activity, metadata) "
            "VALUES (:id, :topic, :desc, :sev, :dec, :act, :meta)"
        ),
        {
            "id": str(entity_id),
            "topic": str(topic_id) if topic_id is not None else None,
            "desc": description,
            "sev": severity,
            "dec": str(decision_id) if decision_id is not None else None,
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
        # Count staged rows in standalone tables (user_assertion) that carry
        # their own bundle_id + state but NO prov_entity row (ADR-017).  A bundle
        # staging only such tables has no prov_entity rows, so we must not error
        # here — fall through to the standalone flip (step 4b).
        standalone_staged = 0
        for table in _STANDALONE_STATE_TABLES:
            srow = await session.execute(
                text(
                    f"SELECT count(*) FROM {table} "
                    "WHERE bundle_id = :b AND state = 'staged'"
                ),
                {"b": str(bundle_id)},
            )
            standalone_staged += srow.scalar_one()
        if not standalone_staged:
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

    # 4b. Flip standalone supporting tables (user_assertion) that carry their
    #    own ``bundle_id`` + ``state`` but NO prov_entity row — these are kept
    #    outside ``entity_kind`` so the dra#14 byte-stability contract holds
    #    (ADR-017).  A dedicated bundle-scoped UPDATE, NOT the prov_entity join.
    for table in _STANDALONE_STATE_TABLES:
        await session.execute(
            text(
                f"UPDATE {table} SET state = 'canonical' "
                "WHERE bundle_id = :b AND state = 'staged'"
            ),
            {"b": str(bundle_id)},
        )

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
            "JOIN content_blob cb ON cb.hash = da.source_capture_hash "
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
# source_capture reuses the prov_entity UUID as its capture_id (set by
# stage_source_capture), joining on pe.id — the authoritative Wave 1a link.
_DOMAIN_STATE_TABLES = (
    ("raw_capture",       "raw_capture",       "pe.content_hash = raw_capture.content_hash"),
    ("source_capture",    "raw_capture",       "pe.id = source_capture.capture_id"),
    ("derived_artifact",  "derived_artifact",  "pe.id = derived_artifact.id"),
    ("evidence_unit",     "evidence_unit",     "pe.id = evidence_unit.id"),
    ("implementation_entity", "implementation_entity", "pe.id = implementation_entity.id"),
    ("claim",             "claim",             "pe.id = claim.id"),
)

# Standalone supporting tables with their own ``bundle_id`` + ``state`` but NO
# ``prov_entity`` row — kept outside ``entity_kind`` for the dra#14
# byte-stability contract (ADR-017).  Flipped to canonical via a dedicated
# bundle-scoped UPDATE in :func:`_publish_bundle_tx`, not via the
# ``prov_entity`` join used by ``_DOMAIN_STATE_TABLES``.
_STANDALONE_STATE_TABLES = ("user_assertion", "source_candidate")


async def _mirror_state_canonical(session: AsyncSession, bundle_id: UUID) -> None:
    """Mirror prov_entity.state to each domain table's own ``state`` column.

    Each domain table's primary key equals the matching prov_entity column
    (its UUID id, except raw_capture whose PK is ``content_hash``). Joining on
    (entity_kind, bundle_id, state) flips only this bundle's validated rows.
    ``source_capture`` reuses the prov_entity UUID as ``capture_id`` (set by
    :func:`stage_source_capture`), so it joins on ``pe.id`` like the other UUID-
    PK domain tables.
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
