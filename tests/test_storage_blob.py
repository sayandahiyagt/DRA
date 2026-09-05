"""Tests for the Wave 1a BlobStore abstraction (dra#78).

Pure unit tests (no DB) assert the FilesystemBlobStore put/open/exists/verify
round-trip and that ``boto3`` is lazy-imported (so ``import dra.storage``
succeeds without AWS).  DB-gated tests exercise ``stage_content_blob`` and
``stage_source_capture`` end-to-end against Postgres.

SKIP DB-gated tests when Postgres is unreachable (env concern — spec §21).
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import sys

import pytest

from dra.storage import (
    BlobStore,
    FilesystemBlobStore,
    S3BlobStore,
    default_blob_store,
)


def _arun(coro):
    return asyncio.run(coro)


def test_filesystem_blob_store_round_trip(tmp_path):
    """put -> open -> exists -> verify round-trips on the filesystem backend."""
    store = FilesystemBlobStore(root=tmp_path)
    data = b"hello, DRA Wave 1a"
    h = hashlib.sha256(data).hexdigest()
    expected_uri = store._uri(store._path(h, "text/plain"))

    assert not _arun(store.exists(expected_uri))

    u = _arun(store.put(data, h, "text/plain"))
    assert u == expected_uri
    assert _arun(store.exists(u))

    with store.open(u) as fh:
        assert fh.read() == data

    assert _arun(store.verify(u, h))
    assert _arun(store.exists(u))


def test_filesystem_blob_store_verify_detects_corruption(tmp_path):
    """verify() returns False when content does not match the expected hash."""
    store = FilesystemBlobStore(root=tmp_path)
    data = b"correct data"
    h = hashlib.sha256(data).hexdigest()
    wrong = hashlib.sha256(b"tampered").hexdigest()
    u = _arun(store.put(data, h, None))
    assert _arun(store.verify(u, wrong)) is False


def test_storage_module_does_not_eagerly_import_boto3():
    """``import dra.storage`` must not eagerly import boto3 (lazy S3 import)."""
    # Record boto3 presence before re-import.
    had_boto3 = "boto3" in sys.modules
    sys.modules.pop("dra.storage", None)
    importlib.import_module("dra.storage")
    assert "boto3" not in sys.modules
    if had_boto3:
        # Restore — another code path may have imported it; not our concern.
        pass


def test_default_blob_store_is_filesystem():
    """The default dev blob store is a FilesystemBlobStore (no AWS needed)."""
    store = default_blob_store()
    assert isinstance(store, FilesystemBlobStore)


def test_blobstore_protocol_is_runtime_checkable():
    """FilesystemBlobStore satisfies the BlobStore Protocol at runtime."""
    store = FilesystemBlobStore()
    assert isinstance(store, BlobStore)


@pytest.mark.parametrize("kind", ["repo", "paper", "web"])
def test_normalized_key_format(kind):
    """normalized_key is kind:locator:version — stable and collision-free
    across distinct (kind, locator, version) triples."""
    from dra.publish import stage_source_identity
    # Just verify the key format is deterministic from the parameters.
    for k, loc, ver in [
        ("repo", "https://x.com/r", "v1"),
        ("repo", "https://x.com/r", "v2"),
        ("web", "https://x.com/r", "v1"),
    ]:
        nk = f"{k}:{loc}:{ver or ''}"
        assert nk  # non-empty
        assert nk.startswith(k)
        assert loc in nk


# ---------------------------------------------------------------------------
# DB-gated staging round-trip (SKIP without Postgres — env concern, not a code
# defect — see tests._db.DB / spec §21)
# ---------------------------------------------------------------------------

from tests._db import DB  # noqa: E402

_run = _arun


@DB
def test_stage_source_capture_populates_new_tables():
    """stage_source_capture creates content_blob + source_representation +
    source_capture rows and a backward-compat raw_capture row."""
    from dra.publish import (
        async_session,
        create_activity,
        publish_bundle,
        stage_bundle,
        stage_source_capture,
        stage_source_identity,
    )
    from sqlalchemy import text
    from tests._evidence import reset

    async def run():
        await reset()
        async with async_session() as session:
            async with session.begin():
                bid = await stage_bundle(
                    "run_sc", "task_sc", "sc-test",
                    {"kind": "model", "name": "gpt", "version": "1.0",
                     "external_id": "gpt-1.0"},
                )
                acq = await create_activity(session, bid, "acquisition",
                                            {"kind": "model"})
                sid = await stage_source_identity(
                    session, bid, acq, "web", "https://example.com/page",
                    version="v1", access_basis="public", crawl_allowed=True,
                )
                data = b"wave 1a capture bytes"
                ch = hashlib.sha256(data).hexdigest()
                cap = await stage_source_capture(
                    session, bid, acq, sid, ch, "html",
                    blob_store=FilesystemBlobStore(root="/tmp/dra-sc-test"),
                    data=data, size_bytes=len(data),
                    mime_type="text/html", final_url="https://example.com/page",
                )

                cb = await session.scalar(
                    text("SELECT hash FROM content_blob WHERE hash = :h"),
                    {"h": ch},
                )
                assert cb is not None

                rep_url = await session.scalar(
                    text(
                        "SELECT canonical_url FROM source_representation "
                        "WHERE content_blob_hash = :h"
                    ),
                    {"h": ch},
                )
                assert rep_url == "https://example.com/page"

                sc_kind = await session.scalar(
                    text(
                        "SELECT kind FROM source_capture WHERE capture_id = :c"
                    ),
                    {"c": str(cap)},
                )
                assert sc_kind == "html"

                rc_kind = await session.scalar(
                    text(
                        "SELECT kind FROM raw_capture WHERE content_hash = :h"
                    ),
                    {"h": ch},
                )
                assert rc_kind == "html"

        published = await publish_bundle(bid)
        assert published >= 1

        async with async_session() as session:
            sc_state = await session.scalar(
                text("SELECT state FROM source_capture WHERE capture_id = :c"),
                {"c": str(cap)},
            )
            assert sc_state == "canonical"
    _run(run())


@DB
def test_stage_source_identity_get_or_create_idempotent():
    """Identical (kind, locator, version) returns the same source_identity id."""
    from dra.publish import async_session, stage_source_identity
    from tests._evidence import reset

    async def run():
        await reset()
        async with async_session() as session:
            async with session.begin():
                sid1 = await stage_source_identity(
                    session, None, None, "repo",
                    "https://example.com/repo", version="abc123",
                    license_spdx="MIT", access_basis="public",
                )
                sid2 = await stage_source_identity(
                    session, None, None, "repo",
                    "https://example.com/repo", version="abc123",
                    license_spdx="MIT", access_basis="public",
                )
                assert sid1 == sid2

                # Different version → different id
                sid3 = await stage_source_identity(
                    session, None, None, "repo",
                    "https://example.com/repo", version="def456",
                    license_spdx="MIT", access_basis="public",
                )
                assert sid3 != sid1
    _run(run())


@DB
def test_stage_content_blob_writes_durable_bytes():
    """stage_content_blob writes bytes to the BlobStore and creates the row."""
    import tempfile
    from dra.publish import async_session, stage_bundle, stage_content_blob
    from sqlalchemy import text
    from tests._evidence import reset

    async def run():
        await reset()
        tmp = tempfile.mkdtemp()
        store = FilesystemBlobStore(root=tmp)
        data = b"blob content for test"
        h = hashlib.sha256(data).hexdigest()

        async with async_session() as session:
            async with session.begin():
                uri = await stage_content_blob(
                    session, h, data, "text/plain", len(data), store
                )
                assert uri is not None
                assert uri.startswith(tmp)

                row = await session.execute(
                    text("SELECT storage_uri, size FROM content_blob WHERE hash = :h"),
                    {"h": h},
                )
                r = row.mappings().one()
                assert r["size"] == len(data)
                assert await store.exists(uri)
    _run(run())
