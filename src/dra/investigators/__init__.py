"""Shared investigator substrate: evidence emission contract (dra#23).

Provides the single way all investigators capture evidence:

- :func:`content_hash` — sha256 hex helper (raw_capture PK and
  evidence_unit.content_hash, ADR-004).
- :data:`LOCATOR_SHAPES` — constantized spec §13.4 locator field sets per
  source kind so every investigator emits consistent locators.
- :func:`normalize_locator` / :func:`validate_locator` — pure helpers that
  normalize and validate locator dicts against ``LOCATOR_SHAPES``.
- :class:`InvestigatorContext` — async orchestrator that opens a bundle,
    records the acquisition + parsing ``prov_activity`` rows with the
    responsible agent, batches staged domain rows, and commits them via
    :func:`dra.publish.publish_bundle`, raising :class:`PublishError` on
    failure.

The package is DB-free except for :class:`InvestigatorContext` (which delegates
to :mod:`dra.publish`). The pure helpers can be unit-tested without Postgres.

A default :class:`~dra.storage.FilesystemBlobStore` is bound to each
:class:`InvestigatorContext` so investigators do not need to construct one
explicitly; pass ``blob_store=...`` to override (e.g. with an
:class:`~dra.storage.S3BlobStore` for production).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Mapping
from uuid import UUID

from dra.publish import (
    PublishError,
    add_prov_edge,
    async_session,
    create_activity,
    stage_bundle,
    stage_claim,
    stage_crawl_manifest_entry,
    stage_derived_artifact,
    stage_evidence_unit,
    stage_gap,
    stage_implementation_entity,
    stage_source_capture,
    stage_source_identity,
    stage_user_assertion,
    publish_bundle,
)
from dra.storage import default_blob_store
__all__ = [
    "content_hash",
    "LOCATOR_SHAPES",
    "normalize_locator",
    "validate_locator",
    "InvestigatorContext",
    "create_activity",
    "stage_gap",
]


def content_hash(data: str | bytes) -> str:
    """Return the sha256 hex digest of *data*.

    Used as the ``content_blob`` primary key and as
    ``evidence_unit.content_hash`` (ADR-004).  Accepts ``str`` or ``bytes``
    and always returns 64 lowercase hex characters — the format validated by
    :func:`dra.publish.publish_bundle` (publish.py:620-625).
    """
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


LOCATOR_SHAPES: Mapping[str, tuple[str, ...]] = {
    "repo": ("commit", "path", "symbol", "line_start", "line_end"),
    "paper": ("version", "page", "section", "equation", "figure", "table"),
    "web": ("canonical_url", "captured_artifact", "dom_locator", "text_locator"),
    "browser": (
        "timestamp",
        "page_state",
        "selector",
        "accessibility_node",
        "screenshot",
        "har_event",
    ),
    "execution": ("environment_manifest", "command", "output_hash"),
}


def normalize_locator(source_kind: str, locator: Mapping[str, Any]) -> dict[str, Any]:
    """Project *locator* onto the spec §13.4 shape for *source_kind*.

    Returns a dict containing ``source_kind`` plus only the fields declared in
    :data:`LOCATOR_SHAPES` for that kind — unknown keys are dropped so every
    investigator emits a consistent locator schema.  Fields absent from
    *locator* are omitted (not set to ``None``).
    """
    if source_kind not in LOCATOR_SHAPES:
        raise ValueError(
            f"unknown source_kind {source_kind!r}; "
            f"expected one of {sorted(LOCATOR_SHAPES)}"
        )
    shape = LOCATOR_SHAPES[source_kind]
    normalized: dict[str, Any] = {"source_kind": source_kind}
    for field_name in shape:
        if field_name in locator:
            normalized[field_name] = locator[field_name]
    return normalized


def validate_locator(source_kind: str, locator: Mapping[str, Any]) -> None:
    """Raise :class:`ValueError` if *locator* is missing a required shape field.

    A "required" field is any field in :data:`LOCATOR_SHAPES[source_kind]`.
    This enforces the evidence-emission contract: every investigator must emit
    all declared fields for its source kind before publication.
    """
    if source_kind not in LOCATOR_SHAPES:
        raise ValueError(
            f"unknown source_kind {source_kind!r}; "
            f"expected one of {sorted(LOCATOR_SHAPES)}"
        )
    missing = [f for f in LOCATOR_SHAPES[source_kind] if f not in locator]
    if missing:
        raise ValueError(
            f"locator for source_kind {source_kind!r} missing required "
            f"fields: {missing}"
        )


@dataclass
class InvestigatorContext:
    """Async orchestrator for a single investigation run.

    Opens a staging bundle (via :func:`dra.publish.stage_bundle`), records the
    ``acquisition`` and ``parsing`` ``prov_activity`` rows attributed to
    ``actor``, then exposes bound batchers that stage domain rows within the
    bundle's transaction.  On normal exit the bundle is published
    (staged→canonical, ADR-013) and the transaction is committed; on any
    exception the staging transaction is rolled back without publishing and
    :class:`PublishError` (or the original exception) propagates.

    Example::

        async with InvestigatorContext(
            run_id="r1", task_id="t1", actor={"external_id": "investigator-1"}
        ) as ctx:
            src = await ctx.stage_source_identity("repo", "https://example.com/r")
            cap = await ctx.stage_source_capture(src, content_hash(b"snap"), "repo_snapshot")
            ei = await ctx.stage_implementation_entity(src, "symbol", path="m.py", symbol_name="f")
    """

    run_id: str
    task_id: str
    actor: dict[str, Any]
    label: str | None = None
    blob_store: Any = field(
        default_factory=default_blob_store
    )  # BlobStore abstraction (dra#78 Wave 1a); FilesystemBlobStore by default.
    published_count: int | None = field(default=None, init=False)
    _bundle_id: UUID | None = field(default=None, init=False)
    _session: Any = field(default=None, init=False)
    _acquisition_activity: UUID | None = field(default=None, init=False)
    _parsing_activity: UUID | None = field(default=None, init=False)
    _human_correction_activity_id: UUID | None = field(default=None, init=False)

    async def __aenter__(self) -> "InvestigatorContext":
        self._bundle_id = await stage_bundle(
            self.run_id, self.task_id, self.label, self.actor
        )
        self._session = async_session()
        self._acquisition_activity = await create_activity(
            self._session, self._bundle_id, "acquisition", self.actor
        )
        self._parsing_activity = await create_activity(
            self._session, self._bundle_id, "parsing", self.actor
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._session is not None:
            try:
                if exc_type is None:
                    count = await publish_bundle(
                        self._bundle_id, session=self._session
                    )
                    await self._session.commit()
                    self.published_count = count
                else:
                    await self._session.rollback()
            except Exception:
                if self._session.is_active:
                    await self._session.rollback()
                raise
            finally:
                await self._session.close()

    # -- bound batchers ----------------------------------------------------
    # Each delegates to the matching publish.py helper, binding the context's
    # session, bundle_id, and responsible activity.

    async def stage_source_identity(
        self,
        kind: str,
        locator: str,
        *,
        version: str | None = None,
        state: str = "staged",
        license_spdx: str | None = None,
        access_basis: str | None = None,
        crawl_allowed: bool | None = None,
        auth_scope: str | None = None,
        redist_allowed: bool | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> UUID:
        return await stage_source_identity(
            self._session,
            self._bundle_id,
            self._acquisition_activity,
            kind,
            locator,
            version=version,
            state=state,
            license_spdx=license_spdx,
            access_basis=access_basis,
            crawl_allowed=crawl_allowed,
            auth_scope=auth_scope,
            redist_allowed=redist_allowed,
            metadata=metadata,
        )

    async def stage_source_capture(
        self,
        source_id: UUID,
        content_hash: str,
        kind: str,
        *,
        blob_store: Any | None = None,
        data: bytes | None = None,
        state: str = "staged",
        size_bytes: int | None = None,
        mime_type: str | None = None,
        captured_at: str | None = None,
        final_url: str | None = None,
        redirect_chain: list[dict[str, Any]] | None = None,
        method: str | None = None,
        provider: str | None = None,
        http_metadata: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> UUID:
        store = blob_store if blob_store is not None else self.blob_store
        entity_id = await stage_source_capture(
            self._session,
            self._bundle_id,
            self._acquisition_activity,
            source_id,
            content_hash,
            kind,
            blob_store=store,
            data=data,
            state=state,
            size_bytes=size_bytes,
            mime_type=mime_type,
            captured_at=captured_at,
            final_url=final_url,
            redirect_chain=redirect_chain,
            method=method,
            provider=provider,
            http_metadata=http_metadata,
            metadata=metadata,
        )
        await add_prov_edge(
            self._session,
            generated_entity_id=entity_id,
            activity_id=self._acquisition_activity,
        )
        return entity_id

    async def stage_derived_artifact(
        self,
        source_capture_hash: str,
        content_hash: str,
        kind: str,
        version: int,
        *,
        state: str = "staged",
        metadata: dict[str, Any] | None = None,
    ) -> UUID:
        entity_id = await stage_derived_artifact(
            self._session,
            self._bundle_id,
            self._parsing_activity,
            source_capture_hash,
            content_hash,
            kind,
            version,
            state=state,
            metadata=metadata,
        )
        await add_prov_edge(
            self._session,
            generated_entity_id=entity_id,
            activity_id=self._parsing_activity,
        )
        return entity_id

    async def stage_evidence_unit(
        self,
        artifact_id: UUID,
        locator: dict[str, Any],
        *,
        content_hash: str | None = None,
        state: str = "staged",
        metadata: dict[str, Any] | None = None,
    ) -> UUID:
        entity_id = await stage_evidence_unit(
            self._session,
            self._bundle_id,
            self._parsing_activity,
            artifact_id,
            locator,
            content_hash=content_hash,
            state=state,
            metadata=metadata,
        )
        await add_prov_edge(
            self._session,
            generated_entity_id=entity_id,
            activity_id=self._parsing_activity,
        )
        return entity_id

    async def stage_implementation_entity(
        self,
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
        entity_id = await stage_implementation_entity(
            self._session,
            self._bundle_id,
            self._parsing_activity,
            repo_source_id,
            kind,
            path=path,
            symbol_name=symbol_name,
            commit_sha=commit_sha,
            line_start=line_start,
            line_end=line_end,
            signature=signature,
            content_hash=content_hash,
            state=state,
            metadata=metadata,
        )
        await add_prov_edge(
            self._session,
            generated_entity_id=entity_id,
            activity_id=self._parsing_activity,
        )
        return entity_id

    async def stage_claim(
        self,
        claim_text: str,
        evidence_unit_id: UUID | None = None,
        topic_id: UUID | None = None,
        *,
        state: str = "staged",
        confidence: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> UUID:
        entity_id = await stage_claim(
            self._session,
            self._bundle_id,
            self._parsing_activity,
            claim_text,
            evidence_unit_id=evidence_unit_id,
            topic_id=topic_id,
            state=state,
            confidence=confidence,
            metadata=metadata,
        )
        await add_prov_edge(
            self._session,
            generated_entity_id=entity_id,
            activity_id=self._parsing_activity,
        )
        return entity_id

    async def stage_crawl_manifest_entry(
        self,
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
        """Record one acquisition attempt in the crawl manifest (dra#26).

        Delegates to :func:`dra.publish.stage_crawl_manifest_entry` on the
        context's acquisition activity, within the bundle's transaction.
        """
        return await stage_crawl_manifest_entry(
            self._session,
            self._bundle_id,
            self._acquisition_activity,
            url=url,
            origin=origin,
            result=result,
            step=step,
            reason=reason,
            latency_ms=latency_ms,
            status=status,
            metadata=metadata,
        )

    async def _human_correction_activity(self) -> UUID:
        """Return the bundle's single ``human_correction`` prov_activity.

        Lazily creates (and caches) one ``human_correction`` activity on first
        use so we don't emit a separate activity row per assertion — the
        assertion_type enum (ADR-017) implies human correction provenance.
        """
        if self._human_correction_activity_id is None:
            self._human_correction_activity_id = await create_activity(
                self._session, self._bundle_id, "human_correction", self.actor
            )
        return self._human_correction_activity_id

    async def stage_user_assertion(
        self,
        assertion_type: str,
        question: str,
        value: Any | None = None,
        *,
        activity_id: UUID | None = None,
        run_id: str | None = None,
        task_id: str | None = None,
        superseded_by: UUID | None = None,
        disputed_claim_id: UUID | None = None,
        disputed_decision_id: UUID | None = None,
        disputed_source_id: UUID | None = None,
        state: str = "staged",
        metadata: dict[str, Any] | None = None,
    ) -> UUID:
        """Stage a versioned human/maintainer assertion (ADR-017, dra#44).

        Attributes the assertion to a ``human_correction`` activity by default
        (the activity_type the ``assertion_type`` enum and ADR-017 imply); pass
        an explicit ``activity_id`` to attribute it differently.  Delegates to
        :func:`dra.publish.stage_user_assertion` within the bundle's transaction.
        """
        if activity_id is None:
            activity_id = await self._human_correction_activity()
        return await stage_user_assertion(
            self._session,
            self._bundle_id,
            activity_id,
            assertion_type,
            question,
            value,
            run_id=run_id,
            task_id=task_id,
            superseded_by=superseded_by,
            disputed_claim_id=disputed_claim_id,
            disputed_decision_id=disputed_decision_id,
            disputed_source_id=disputed_source_id,
            state=state,
            metadata=metadata,
        )

    async def create_activity(
        self,
        activity_type: str,
        *,
        input_ids: list[str] | None = None,
        output_ids: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> UUID:
        """Create a ``prov_activity`` within this bundle, attributed to ``actor``.

        Delegates to :func:`dra.publish.create_activity` with the context's
        bound session, bundle_id, and actor. Used for activities beyond the
        default ``acquisition``/``parsing`` (e.g. the §16.2 ``visual_review``
        activity).
        """
        return await create_activity(
            self._session,
            self._bundle_id,
            activity_type,
            self.actor,
            input_ids=input_ids,
            output_ids=output_ids,
            metadata=metadata,
        )

    async def stage_gap(
        self,
        description: str,
        severity: str = "medium",
        *,
        activity_id: UUID | None = None,
        topic_id: UUID | None = None,
        state: str = "staged",
        metadata: dict[str, Any] | None = None,
    ) -> UUID:
        """Stage a gap entity (spec §16.2, §11.9) within this bundle.

        Delegates to :func:`dra.publish.stage_gap` with the context's bound
        session/bundle_id. Defaults to the parsing activity but accepts an
        ``activity_id`` override (e.g. the ``visual_review`` activity).
        """
        aid = activity_id or self._parsing_activity
        entity_id = await stage_gap(
            self._session,
            self._bundle_id,
            aid,
            description,
            severity,
            topic_id=topic_id,
            state=state,
            metadata=metadata,
        )
        await add_prov_edge(
            self._session,
            generated_entity_id=entity_id,
            activity_id=aid,
        )
        return entity_id

    async def publish(self) -> int:
        """Commit staged rows to canonical (ADR-013).

        Returns the number of prov_entity rows transitioned.  Raises
        :class:`PublishError` on validation failure.
        """
        return await publish_bundle(self._bundle_id, session=self._session)


# Late-bound re-export (imported at the bottom to avoid a circular import:
# dra.investigators.website imports InvestigatorContext from this package).
from dra.investigators.website import WebsiteInvestigator  # noqa: E402

__all__ = [
    "content_hash",
    "LOCATOR_SHAPES",
    "normalize_locator",
    "validate_locator",
    "InvestigatorContext",
    "WebsiteInvestigator",
    "stage_crawl_manifest_entry",
    "stage_user_assertion",
    "create_activity",
    "stage_gap",
]
