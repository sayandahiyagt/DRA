"""Tests for the RepositoryInvestigator (dra#24, spec §11.2).

Pure unit tests (no DB) assert tree-sitter symbol extraction, the §13.4 repo
locator contract, and the sandbox capability-detection / static-only degrade.
DB-gated tests (reusing ``tests/_db.py`` + ``tests/_evidence.py`` reset
convention) stage a real local git repo snapshot and assert canonical
publication + an ``evidence_unit -> derived_artifact -> raw_capture ->
source_identity`` provenance traversal (mirroring
``test_provenance_traversal.py``'s ``LINEAGE_CHAIN_QUERY`` shape).
"""

from __future__ import annotations

import asyncio
import sys

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from dra.investigators import InvestigatorContext, content_hash, normalize_locator, validate_locator
from dra.investigators.symbol_index import extract_file
from dra.publish import async_session
from dra.sandbox import Sandbox, SandboxCapability
from tests._db import DB
from tests._evidence import ACTOR, reset
from tests._repo_fixture import build_repo


@pytest.fixture
def _isolated_async_engine(monkeypatch):
    """Give each DB test a fresh ``NullPool`` async engine.

    SQLAlchemy async engines bind pooled connections to the event loop they
    were created on.  Each ``@DB`` test opens its own loop via ``asyncio.run``,
    so reusing the module-level ``dra.db.engine`` (a pooled ``QueuePool``)
    across tests deadlocks when a checkout hands back a connection from a
    closed loop.  A ``NullPool`` engine creates a brand-new connection per
    session and closes it on ``session.close()``, so connections never survive
    across ``asyncio.run`` boundaries.  We patch every ``async_session``
    reference (``dra.publish``, ``dra.investigators``, ``tests._evidence`` and
    this module's local binding) so the investigator, staging helpers and
    assertions all share the per-test engine.

    Applied only to the DB-gated tests (via ``usefixtures``); pure tests never
    touch the DB.
    """
    from dra.db import DATABASE_URL

    engine = create_async_engine(DATABASE_URL, poolclass=NullPool, future=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    import dra.publish as _publish
    import dra.investigators as _investigators
    import tests._evidence as _evidence

    monkeypatch.setattr(_publish, "async_session", session_factory)
    monkeypatch.setattr(_investigators, "async_session", session_factory)
    monkeypatch.setattr(_evidence, "async_session", session_factory)
    monkeypatch.setattr(sys.modules[__name__], "async_session", session_factory)
    yield engine
    try:
        loop = asyncio.new_event_loop()
        loop.run_until_complete(_close_engine(engine))
        loop.close()
    except Exception:
        pass


async def _close_engine(engine):
    await engine.dispose()




# ---------------------------------------------------------------------------
# Pure unit tests (no DB — run even without Postgres)
# ---------------------------------------------------------------------------

SNIPPET = b'''from os import path
import sys

class Foo:
    """a foo"""

    def bar(self):
        return 1

def main():
    print("hi")

if __name__ == "__main__":
    main()
'''


def test_extract_symbols_python():
    """tree-sitter extracts class Foo, function main, method bar, imports."""
    fs = extract_file("snippet.py", source=SNIPPET)
    class_names = [c.name for c in fs.classes]
    func_names = [f.name for f in fs.functions]
    method_names = [m.name for m in fs.methods]
    assert class_names == ["Foo"]
    assert func_names == ["main"]
    assert method_names == ["bar"]
    # imports are (module, imported_name) tuples.
    assert ("os", "path") in fs.imports
    assert ("sys", "sys") in fs.imports
    foo = fs.classes[0]
    assert foo.line_start == 4 and foo.line_end == 8
    bar = fs.methods[0]
    assert bar.line_start == 7 and bar.line_end == 8
    # signatures are the def / class header.
    assert bar.signature == "def bar(self):"
    assert foo.signature == "class Foo:"
    main_fn = fs.functions[0]
    assert main_fn.line_start == 10 and main_fn.line_end == 11
    assert main_fn.signature == "def main():"


def test_extract_symbols_infers_line_spans():
    """Line spans are 1-indexed and inclusive across multi-line bodies."""
    src = b"def f(\n    a,\n    b,\n):\n    return a + b\n"
    fs = extract_file("f.py", source=src)
    assert fs.functions[0].line_start == 1
    assert fs.functions[0].line_end == 5
    assert fs.functions[0].name == "f"


def test_normalize_repo_locator_shape():
    """normalize_locator returns exactly the §13.4 repo fields + source_kind."""
    loc = normalize_locator(
        "repo",
        {"commit": "abc", "path": "x.py", "symbol": "foo",
         "line_start": 1, "line_end": 10},
    )
    assert loc["source_kind"] == "repo"
    assert set(loc) == {"source_kind", "commit", "path", "symbol",
                        "line_start", "line_end"}


def test_validate_locator_rejects_missing_symbol():
    """validate_locator rejects a repo locator missing `symbol`."""
    with pytest.raises(ValueError, match="missing required"):
        validate_locator("repo", {"commit": "abc", "path": "x.py"})


def test_validate_locator_accepts_full_repo_locator():
    validate_locator(
        "repo",
        {"commit": "abc", "path": "x.py", "symbol": "foo",
         "line_start": 1, "line_end": 10},
    )


def test_sandbox_detect_static_when_no_runtime(monkeypatch):
    """With static_only forced and no runtime, detect degrades to STATIC_ONLY."""
    monkeypatch.setenv("DRA_SANDBOX_CAPABILITY", "static_only")
    cap = Sandbox.detect()
    assert cap is SandboxCapability.STATIC_ONLY


def test_sandbox_run_returns_none_in_static_mode():
    """run() degrades to None in static-only mode — never a hard failure."""
    sandbox = Sandbox("/tmp", capability=SandboxCapability.STATIC_ONLY)
    assert sandbox.run(["echo", "hi"]) is None


# ---------------------------------------------------------------------------
# DB-gated integration tests (SKIP without Postgres)
# ---------------------------------------------------------------------------

_LINEAGE_FROM_EVIDENCE = """
SELECT
    si.locator               AS source_locator,
    si.kind                  AS source_kind,
    si.license_spdx          AS license,
    rc.content_hash          AS raw_hash,
    rc.kind                  AS raw_kind,
    da.content_hash          AS derived_hash,
    eu.id                    AS evidence_id,
    eu.locator               AS evidence_locator,
    pa.activity_type         AS acquisition_activity,
    pb.run_id
FROM evidence_unit eu
JOIN derived_artifact      da ON da.id = eu.artifact_id
JOIN raw_capture           rc ON rc.content_hash = da.source_capture_hash
JOIN source_identity       si ON si.id = rc.source_id
JOIN prov_entity re          ON re.entity_kind = 'raw_capture'
                              AND re.content_hash = rc.content_hash
JOIN prov_activity pa       ON pa.id = re.produced_by_activity
JOIN prov_bundle pb         ON pb.id = pa.bundle_id
WHERE eu.id = :ev_id
"""


_IMPL_COUNT_FOR_SOURCE = """
SELECT count(*) FROM implementation_entity
WHERE repo_source_id = (
    SELECT id FROM source_identity WHERE locator = :loc
)
"""


@DB
@pytest.mark.usefixtures("_isolated_async_engine")
def test_repo_investigator_publishes_canonical(tmp_path, monkeypatch):
    """Investigate the fixture repo; everything lands in CANONICAL state."""
    async def run():
        await reset()
        monkeypatch.setenv("DRA_SANDBOX_CAPABILITY", "static_only")
        repo_path, sha, toplevel = build_repo(tmp_path)

        async with InvestigatorContext(
            run_id="run_repo", task_id="task_repo", actor=ACTOR, label="repo",
        ) as ctx:
            from dra.investigators.repo import RepositoryInvestigator
            res = await RepositoryInvestigator(ctx, repo_path).investigate()

        assert ctx.published_count is not None and ctx.published_count >= 4
        # Snapshot is a deterministic function of the tree (idempotent re-runs):
        # re-hashing the working tree yields the same content_hash recorded in
        # the result / raw_capture PK.
        from dra.investigators.repo import _make_snapshot
        recompute_hash = content_hash(_make_snapshot(repo_path)[0])
        assert recompute_hash == res.snapshot_hash
        assert res.symbol_index_hash

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
                text(
                    "SELECT state FROM implementation_entity "
                    "WHERE repo_source_id IN "
                    "(SELECT id FROM source_identity WHERE locator = :loc)"
                ),
                {"loc": repo_path},
            )
            assert impl_state == "canonical"

            # snapshot raw_capture exists and is canonical.
            raw_kind = await s.scalar(
                text(
                    "SELECT kind FROM raw_capture WHERE content_hash = :h"
                ),
                {"h": res.snapshot_hash},
            )
            assert raw_kind == "repo_snapshot"

            # symbol-index derived artifact chains to the snapshot.
            da = await s.execute(
                text(
                    "SELECT source_capture_hash FROM derived_artifact "
                    "WHERE content_hash = :h AND kind = 'parsed'"
                ),
                {"h": res.symbol_index_hash},
            )
            row = da.mappings().one()
            assert row["source_capture_hash"] == res.snapshot_hash
        from dra.db import engine as _eng
        await _eng.dispose()
        return res, repo_path, sha
    res, repo_path, sha = asyncio.run(run())


@DB
@pytest.mark.usefixtures("_isolated_async_engine")
def test_repo_investigator_provenance_traversal(tmp_path, monkeypatch):
    """An evidence_unit traverses back to source_identity + raw_capture.

    Exercises the same §21.2 lineage chain shape as
    ``test_provenance_traversal.py``::LINEAGE_CHAIN_QUERY but anchored on an
    evidence_unit emitted by the investigator.
    """
    async def run():
        await reset()
        monkeypatch.setenv("DRA_SANDBOX_CAPABILITY", "static_only")
        repo_path, sha, toplevel = build_repo(tmp_path)

        async with InvestigatorContext(
            run_id="run_prov", task_id="task_prov", actor=ACTOR, label="repo",
        ) as ctx:
            from dra.investigators.repo import RepositoryInvestigator
            res = await RepositoryInvestigator(ctx, repo_path).investigate()

        ev_id = res.evidence_unit_ids[0]
        async with async_session() as s:
            res_row = await s.execute(
                text(_LINEAGE_FROM_EVIDENCE), {"ev_id": str(ev_id)}
            )
            row = res_row.mappings().one()
        assert row["source_kind"] == "repo"
        assert row["source_locator"] == repo_path
        assert row["license"] == "MIT"
        assert row["raw_kind"] == "repo_snapshot"
        assert row["raw_hash"] == res.snapshot_hash
        assert row["derived_hash"] == res.symbol_index_hash
        assert row["acquisition_activity"] == "acquisition"
        assert row["run_id"] == "run_prov"
        # locator carries the repo@commit:path:symbol shape (§13.4).
        loc = row["evidence_locator"]
        assert loc["source_kind"] == "repo"
        assert loc["commit"] == sha
        assert "path" in loc and "symbol" in loc
        # An implementation_entity links back to the repo source_identity.
        impl_count = await s.scalar(
            text(_IMPL_COUNT_FOR_SOURCE), {"loc": repo_path}
        )
        assert impl_count > 0
        return res
    asyncio.run(run())


@DB
@pytest.mark.usefixtures("_isolated_async_engine")
def test_repo_investigator_static_fallback_inference(tmp_path, monkeypatch):
    """Static-only sandbox: behavioral claim is INFERENCE and bundle still publishes."""
    async def run():
        await reset()
        monkeypatch.setenv("DRA_SANDBOX_CAPABILITY", "static_only")
        repo_path, sha, toplevel = build_repo(tmp_path)

        async with InvestigatorContext(
            run_id="run_inf", task_id="task_inf", actor=ACTOR, label="repo",
        ) as ctx:
            from dra.investigators.repo import RepositoryInvestigator
            res = await RepositoryInvestigator(ctx, repo_path).investigate()

        assert res.sandbox is SandboxCapability.STATIC_ONLY
        assert res.claim_evidence_status == "INFERENCE"

        async with async_session() as s:
            # claim + its backing evidence_unit both carry INFERENCE.
            rows = await s.execute(
                text(
                    "SELECT c.metadata->>'evidence_status' AS claim_status, "
                    "eu.metadata->>'evidence_status' AS ev_status "
                    "FROM claim c "
                    "JOIN evidence_unit eu ON eu.id = c.evidence_unit_id "
                    "JOIN prov_entity pe ON pe.id = c.id "
                    "WHERE pe.bundle_id = :b"
                ),
                {"b": str(ctx._bundle_id)},
            )
            recs = rows.mappings().all()
            assert recs, "expected at least one behavioral claim"
            for rec in recs:
                assert rec["claim_status"] == "INFERENCE"
                assert rec["ev_status"] == "INFERENCE"
            # static-only still published (no hard-fail).
            staged = await s.scalar(
                text(
                    "SELECT count(*) FROM prov_entity "
                    "WHERE bundle_id = :b AND state = 'staged'"
                ),
                {"b": str(ctx._bundle_id)},
            )
            assert staged == 0
    asyncio.run(run())
