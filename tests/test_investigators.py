"""Integration tests for the shared investigator substrate (dra#23).

Pure unit tests (no DB) assert the ``content_hash`` helper and the §13.4
locator-shape schema contract.  These run even without Postgres.

DB-gated tests (reusing ``tests/_db.py`` + ``tests/_evidence.py`` conventions)
stage a minimal repo source identity + raw capture + derived artifact +
evidence unit + implementation entity and publish them, asserting the
canonical transition and provenance traversal (implementation_entity ->
repo_source_id -> source_identity).  DB-gated tests SKIP when Postgres is
unreachable (env concern, not a code defect).
"""

from __future__ import annotations

import asyncio
import hashlib

import pytest
from sqlalchemy import text

from dra.investigators import (
    LOCATOR_SHAPES,
    InvestigatorContext,
    content_hash,
    normalize_locator,
    validate_locator,
)
from dra.publish import (
    PublishError,
    async_session,
    create_activity,
    publish_bundle,
    stage_implementation_entity,
)
from tests._db import DB
from tests._evidence import (
    ACTOR,
    IMPL_HASH,
    build_implementation_bundle,
    reset,
)

# ---------------------------------------------------------------------------
# Pure unit tests (no DB — run even without Postgres)
# ---------------------------------------------------------------------------


def test_content_hash_is_sha256_hex():
    """content_hash returns 64-char lowercase sha256 hex for str and bytes."""
    assert content_hash("hello") == hashlib.sha256(b"hello").hexdigest()
    assert content_hash(b"hello") == content_hash("hello")
    assert len(content_hash("hello")) == 64
    assert content_hash("hello") == content_hash("hello").lower()
    assert content_hash("") == hashlib.sha256(b"").hexdigest()


def test_content_hash_unique():
    """Different inputs produce different hashes."""
    assert content_hash("a") != content_hash("b")


def test_locator_shapes_repo_paper_web():
    """LOCATOR_SHAPES constantizes the spec §13.4 locator shapes."""
    assert set(LOCATOR_SHAPES) == {
        "repo", "paper", "web", "browser", "execution",
    }
    assert LOCATOR_SHAPES["repo"] == (
        "commit", "path", "symbol", "line_start", "line_end",
    )
    assert LOCATOR_SHAPES["paper"] == (
        "version", "page", "section", "equation", "figure", "table",
    )
    assert LOCATOR_SHAPES["web"] == (
        "canonical_url", "captured_artifact", "dom_locator", "text_locator",
    )


def test_normalize_locator_drops_unknowns():
    """normalize_locator keeps only known shape fields + source_kind."""
    repo_loc = normalize_locator(
        "repo",
        {"commit": "abc", "path": "x", "symbol": "foo",
         "line_start": 1, "line_end": 10, "extra": "dropped"},
    )
    assert repo_loc["source_kind"] == "repo"
    assert "extra" not in repo_loc
    assert repo_loc["commit"] == "abc"


def test_normalize_locator_omits_absent_fields():
    """Fields absent from the input are omitted, not set to None."""
    web_loc = normalize_locator(
        "web", {"canonical_url": "https://x", "captured_artifact": "art"},
    )
    assert web_loc["source_kind"] == "web"
    assert web_loc["canonical_url"] == "https://x"
    assert "dom_locator" not in web_loc


def test_validate_locator_missing_raises():
    """validate_locator raises ValueError when a required field is absent."""
    with pytest.raises(ValueError, match="missing required fields"):
        validate_locator("repo", {"commit": "abc", "path": "x"})


def test_validate_locator_ok():
    """A fully-populated repo locator validates."""
    validate_locator(
        "repo",
        {"commit": "abc", "path": "x", "symbol": "foo",
         "line_start": 1, "line_end": 10},
    )


def test_unknown_source_kind_raises():
    """normalize_locator and validate_locator reject unknown source kinds."""
    with pytest.raises(ValueError, match="unknown source_kind"):
        normalize_locator("blob", {"a": 1})
    with pytest.raises(ValueError, match="unknown source_kind"):
        validate_locator("blob", {"a": 1})


# ---------------------------------------------------------------------------
# DB-gated integration tests (SKIP without Postgres)
# ---------------------------------------------------------------------------


@DB
def test_emission_contract_end_to_end():
    """Stage impl bundle, publish, assert canonical transition + provenance.

    Asserts:
    - All prov_entity rows flip staged->canonical (``staged == 0``).
    - ``implementation_entity.state`` is ``canonical`` (domain-level mirror).
    - Provenance traversal: implementation_entity -> repo_source_id ->
      source_identity yields the repo locator.
    """
    async def run():
        await reset()
        bundle_id, ids = await build_implementation_bundle()
        published = await publish_bundle(bundle_id)
        assert published >= 4  # raw, derived, evidence, impl_entity

        impl_id = ids["implementation_entity"]
        async with async_session() as s:
            staged = await s.scalar(
                text(
                    "SELECT count(*) FROM prov_entity "
                    "WHERE bundle_id = :b AND state = 'staged'"
                ),
                {"b": str(bundle_id)},
            )
            assert staged == 0

            impl_state = await s.scalar(
                text(
                    "SELECT state FROM implementation_entity WHERE id = :e"
                ),
                {"e": str(impl_id)},
            )
            assert impl_state == "canonical"

            src = await s.execute(
                text(
                    "SELECT si.locator FROM implementation_entity ie "
                    "JOIN source_identity si ON si.id = ie.repo_source_id "
                    "WHERE ie.id = :e"
                ),
                {"e": str(impl_id)},
            )
            row = src.mappings().one()
            assert row["locator"] == "https://example.com/src"

            impl_hash = await s.scalar(
                text(
                    "SELECT content_hash FROM implementation_entity "
                    "WHERE id = :e"
                ),
                {"e": str(impl_id)},
            )
            assert impl_hash == IMPL_HASH

            acq_act = await s.scalar(
                text(
                    "SELECT activity_type FROM prov_entity pe "
                    "JOIN prov_activity pa ON pa.id = pe.produced_by_activity "
                    "WHERE pe.entity_kind = 'implementation_entity' "
                    "AND pe.bundle_id = :b"
                ),
                {"b": str(bundle_id)},
            )
            assert acq_act == "parsing"
    asyncio.run(run())


@DB
def test_investigator_context_auto_publishes():
    """InvestigatorContext auto-publishes on clean exit and sets domain state."""
    async def run():
        await reset()
        async with InvestigatorContext(
            run_id="run_ctx",
            task_id="task_ctx",
            actor=ACTOR,
            label="ctx-test",
        ) as ctx:
            source_id = await ctx.stage_source_identity(
                "repo", "https://example.com/ctx",
                license_spdx="MIT", access_basis="public",
                crawl_allowed=True, redist_allowed=True,
            )
            raw_hash = content_hash("snapshot-bytes")
            await ctx.stage_raw_capture(
                raw_hash, source_id, "repo_snapshot",
                mime_type="text/plain", stored_at="/store/ctx",
            )
            da_eid = await ctx.stage_derived_artifact(
                raw_hash, content_hash("derived"), "parsed", version=1,
            )
            ev_eid = await ctx.stage_evidence_unit(
                da_eid, {"file": "y.md", "range": [0, 5]},
                content_hash=content_hash("ev"),
            )
            impl_eid = await ctx.stage_implementation_entity(
                source_id, "symbol",
                path="src/app.py", symbol_name="bar",
                commit_sha="def456", line_start=1, line_end=5,
                content_hash=content_hash("impl"),
            )

        assert ctx.published_count >= 4

        async with async_session() as s:
            staged = await s.scalar(
                text(
                    "SELECT count(*) FROM prov_entity "
                    "WHERE bundle_id = :b AND state = 'staged'"
                ),
                {"b": str(ctx._bundle_id)},
            )
            assert staged == 0

            impl_state = await s.scalar(
                text("SELECT state FROM implementation_entity WHERE id = :e"),
                {"e": str(impl_eid)},
            )
            assert impl_state == "canonical"

            prov = await s.execute(
                text(
                    "SELECT pa.activity_type FROM prov_entity pe "
                    "JOIN prov_generation g ON g.entity_id = pe.id "
                    "JOIN prov_activity pa ON pa.id = g.activity_id "
                    "WHERE pe.entity_kind = 'implementation_entity' "
                    "AND pe.bundle_id = :b"
                ),
                {"b": str(ctx._bundle_id)},
            )
            types = [r[0] for r in prov.fetchall()]
            assert "parsing" in types
    asyncio.run(run())


@DB
def test_investigator_context_rollback_on_error():
    """PublishError during publish rolls back staging — no canonical rows."""
    async def run():
        await reset()
        ctx = InvestigatorContext(
            run_id="run_err",
            task_id="task_err",
            actor=ACTOR,
        )
        raised = False
        try:
            async with ctx:
                source_id = await ctx.stage_source_identity(
                    "repo", "https://example.com/err",
                    license_spdx="MIT", access_basis="public",
                )
                await ctx.stage_raw_capture(
                    content_hash("snap"), source_id, "repo_snapshot",
                )
                # Bypass the context wrapper to inject a malformed
                # content_hash so publish_bundle raises PublishError.
                parse = await create_activity(
                    ctx._session, ctx._bundle_id, "parsing", ACTOR,
                )
                await stage_implementation_entity(
                    ctx._session, ctx._bundle_id, parse, source_id, "file",
                    content_hash="too-short",
                )
        except PublishError:
            raised = True

        assert raised, "expected PublishError to propagate"
        assert ctx.published_count is None

        async with async_session() as s:
            canon = await s.scalar(
                text(
                    "SELECT count(*) FROM prov_entity "
                    "WHERE bundle_id = :b AND state = 'canonical'"
                ),
                {"b": str(ctx._bundle_id)},
            )
            assert canon == 0
    asyncio.run(run())
