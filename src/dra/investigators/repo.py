"""RepositoryInvestigator (spec §11.2 Repository Investigator).

Ingests a repository source — a local on-disk path (tests) or a remote URL
(production: shallow ``git clone --depth 1`` into a temp dir, never credentialed)
— and emits normalized evidence units with ``repo@commit:path:symbol`` locators.

Staging flow (single atomic :class:`InvestigatorContext` bundle):
source_identity(repo) -> source_capture(repo_snapshot) ->
derived_artifact(symbol_index) -> evidence_unit + implementation_entity rows
-> behavioral claim.  :class:`InvestigatorContext.__aexit__` publishes the
bundle (staged->canonical, ADR-013) on clean exit.

Design decisions that deviate from PLAN_1 §5 (sound, smallest change):
the ``implementation_entity.kind`` column is the ``impl_kind`` enum
(``file | symbol | algorithm | interface | api`` — see
``alembic/versions/0002_evidence_schema.py``) which has no ``module``/``class``
/``function`` members, so module/class/function are emitted as
``file``/``symbol`` with the finer granularity carried in
``metadata.symbol_kind``.  This avoids a schema migration (out of scope) while
preserving the spec's intent.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tarfile
import tempfile
import urllib.parse
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from dra.investigators import (
    InvestigatorContext,
    content_hash,
    normalize_locator,
    validate_locator,
)
from dra.investigators.claims import (
    detect_capability,
    stage_behavioral_claim,
)
from dra.investigators.symbol_index import index_repo
from dra.sandbox import Sandbox, SandboxCapability, _sanitized_env

_REPO_TREE_SKIP_DIRS = {".git", "__pycache__", ".tox", ".mypy_cache",
                        "node_modules", ".venv", ".git"}


@dataclass
class InvestigationResult:
    """Normalized result of investigating a single repository."""

    source_id: UUID
    snapshot_hash: str
    symbol_index_hash: str
    evidence_unit_ids: list[UUID] = field(default_factory=list)
    implementation_entity_ids: list[UUID] = field(default_factory=list)
    sandbox: SandboxCapability = SandboxCapability.STATIC_ONLY
    claim_evidence_status: str = "INFERENCE"


_REPO_SCHEMES = {"http", "https", "ssh", "git", "ftp", "ftps"}


def _is_remote_url(repo_ref: str) -> bool:
    parsed = urllib.parse.urlparse(repo_ref)
    if parsed.scheme in _REPO_SCHEMES:
        return True
    # git@host:owner/repo.git (scp-like)
    if ":" in repo_ref and not os.path.isabs(repo_ref) and not repo_ref.startswith(
        "."
    ):
        first = repo_ref.split(":", 1)[0]
        if "@" in first:
            return True
    return False


def _git(repo_path: str, args: list[str]) -> str:
    proc = subprocess.run(
        ["git", "-C", repo_path, *args],
        capture_output=True, text=True, timeout=60, env=_sanitized_env(),
    )
    return proc.stdout.strip()


def _git_ok(repo_path: str, args: list[str]) -> bool:
    proc = subprocess.run(
        ["git", "-C", repo_path, *args],
        capture_output=True, text=True, timeout=60, env=_sanitized_env(),
    )
    return proc.returncode == 0


def _submodules(repo_path: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    text_out = _git(repo_path, ["submodule", "status"])
    if not text_out:
        return out
    for line in text_out.splitlines():
        # <status char><40-hex sha> <path> [<commit-ish>]
        if not line.strip():
            continue
        marker = line[0]
        rest = line[1:].strip()
        parts = rest.split(None, 1)
        sha = parts[0] if parts else ""
        path = parts[1].split()[0] if len(parts) > 1 else ""
        out.append({"path": path, "commit_sha": sha, "status": marker})
    return out


def _lockfiles(repo_path: str) -> list[str]:
    known = (
        "uv.lock", "poetry.lock", "pyproject.toml", "requirements.txt",
        "requirements-dev.txt", "package-lock.json", "yarn.lock", "Cargo.lock",
        "go.mod", "pom.xml", "Gemfile.lock", "composer.lock", "mix.lock",
    )
    found: list[str] = []
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = sorted([d for d in dirs if d not in _REPO_TREE_SKIP_DIRS])
        for fname in sorted(files):
            if fname in known:
                found.append(os.path.relpath(os.path.join(root, fname), repo_path))
    return sorted(found)


_LICENSE_PATTERNS: list[tuple[str, str]] = [
    ("The MIT License", "MIT"),
    ("MIT License", "MIT"),
    ("Apache License", "Apache-2.0"),
    ("GNU GENERAL PUBLIC LICENSE", "GPL-3.0-or-later"),
    ("GNU GENERAL PUBLIC LICENSE Version 2", "GPL-2.0-or-later"),
    ("BSD 3-Clause", "BSD-3-Clause"),
    ("BSD 2-Clause", "BSD-2-Clause"),
    ("Mozilla Public License Version 2.0", "MPL-2.0"),
    ("ISC", "ISC"),
]


def _detect_license(repo_path: str) -> str | None:
    candidates: list[str] = []
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = sorted([d for d in dirs if d not in _REPO_TREE_SKIP_DIRS])
        for fname in sorted(files):
            if fname.upper().startswith("LICENSE") or fname == "COPYING":
                candidates.append(os.path.join(root, fname))
    for path in candidates:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        lower = path.lower()
        for needle, spdx in _LICENSE_PATTERNS:
            if needle in text:
                return spdx
        if "LICENSE-MIT" in lower or "MIT" in text.upper().split():
            return "MIT"
    return None


def _walk_repo_files(repo_path: str):
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = sorted([d for d in dirs if d not in _REPO_TREE_SKIP_DIRS])
        for fname in sorted(files):
            abs_path = os.path.join(root, fname)
            rel = os.path.relpath(abs_path, repo_path)
            yield rel, abs_path


def _make_snapshot(repo_path: str) -> tuple[bytes, str]:
    """tar.gz the working tree (excluding .git); return (bytes, sha256).

    The tar is built uncompressed then gzip-wrapped with a fixed mtime (``0``)
    and a sorted file walk, so the content hash is a deterministic function of
    the tree — re-running on an unchanged repo yields an identical
    ``content_hash`` (idempotent re-runs per the substrate's ``ON CONFLICT``
    content_blob.hash PK).
    """
    tar_stream = _build_tar_bytes(repo_path)
    gz_stream = _gzip(tar_stream)
    return gz_stream, content_hash(gz_stream)


def _build_tar_bytes(repo_path: str) -> bytes:
    """Build an uncompressed POSIX tar of the working tree (sorted, no .git)."""
    import io
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for rel, abs_path in _walk_repo_files(repo_path):
            try:
                tar.add(abs_path, arcname=rel, recursive=False)
            except OSError:
                continue
    return buf.getvalue()


def _gzip(data: bytes) -> bytes:
    """Gzip *data* with a fixed mtime (reproducible content hashing)."""
    import gzip
    import io
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", mtime=0) as gz:
        gz.write(data)
    return out.getvalue()


class RepositoryInvestigator:
    """Investigate a repository and emit normalized, content-addressed evidence.

    ``repo_ref`` is a local on-disk git path (tests) or a remote URL (production
    shallow clone).  ``sandbox`` overrides capability detection (useful for
    deterministic tests); when ``None`` the sandbox auto-detects its capability
    and degrades to static-only rather than hard-failing (spec §8.3).
    """

    def __init__(
        self,
        ctx: InvestigatorContext,
        repo_ref: str,
        *,
        name: str | None = None,
        actor: dict[str, Any] | None = None,
        sandbox: Sandbox | None = None,
    ) -> None:
        self.ctx = ctx
        self.repo_ref = repo_ref
        self.name = name
        self.actor = actor
        self.sandbox = sandbox

    async def investigate(self) -> InvestigationResult:
        repo_path = self._resolve_to_local(self.repo_ref)
        toplevel = _git(repo_path, ["rev-parse", "--show-toplevel"]) or repo_path
        commit_sha = _git(toplevel, ["rev-parse", "HEAD"])
        if not commit_sha:
            raise ValueError(f"{toplevel} is not a git repository or has no commits")
        submodules = _submodules(toplevel)
        lockfiles = _lockfiles(toplevel)
        license_spdx = _detect_license(toplevel)

        source_id = await self.ctx.stage_source_identity(
            "repo",
            locator=self.repo_ref,
            version=commit_sha,
            license_spdx=license_spdx,
            access_basis="public",
            crawl_allowed=True,
            redist_allowed=None,
            metadata={
                "submodules": submodules,
                "lockfiles": lockfiles,
                "name": self.name,
                "toplevel": toplevel,
            },
        )

        snapshot_bytes, snapshot_hash = _make_snapshot(toplevel)
        await self.ctx.stage_source_capture(
            source_id,
            snapshot_hash,
            kind="repo_snapshot",
            mime_type="application/x-tar+gzip",
            size_bytes=len(snapshot_bytes),
            data=snapshot_bytes,
            final_url=toplevel,
            metadata={
                "submodules": submodules,
                "lockfiles": lockfiles,
                "commit_sha": commit_sha,
            },
        )

        symbol_index = index_repo(toplevel)
        symbol_index_hash = symbol_index.pop("content_hash")
        symbol_index["commit_sha"] = commit_sha
        idx_artifact_id = await self.ctx.stage_derived_artifact(
            snapshot_hash,
            symbol_index_hash,
            kind="parsed",
            version=1,
            metadata={
                "schema_name": "symbol_index",
                "parser": "tree-sitter@0.26",
                "symbol_count": len(symbol_index.get("modules", [])),
            },
        )

        # Execution-output artifact: a stable, content-addressed handle for the
        # test-run result the behavioral claim is tethered to.  In static mode
        # the hash is derived from the (immutable) snapshot, so re-runs are
        # idempotent.
        exec_hash = content_hash(f"repo_test_execution::{snapshot_hash}")
        exec_artifact_id = await self.ctx.stage_derived_artifact(
            snapshot_hash,
            exec_hash,
            kind="summary",
            version=1,
            metadata={"schema_name": "repo_test_execution"},
        )

        evidence_ids: list[UUID] = []
        impl_ids: list[UUID] = []
        for symbol in symbol_index.get("modules", []):
            await self._emit_symbol(
                source_id, toplevel, commit_sha,
                idx_artifact_id, evidence_ids, impl_ids,
                symbol_kind="module", kind_impl="file",
                name=symbol["symbol"], path=symbol["path"],
                line_start=symbol["line_start"], line_end=symbol["line_end"],
                signature=None,
            )
        for symbol in symbol_index.get("classes", []):
            await self._emit_symbol(
                source_id, toplevel, commit_sha,
                idx_artifact_id, evidence_ids, impl_ids,
                symbol_kind="class", kind_impl="symbol",
                name=symbol["name"], path=symbol["path"],
                line_start=symbol["line_start"], line_end=symbol["line_end"],
                signature=symbol.get("signature"),
            )
        for symbol in symbol_index.get("functions", []):
            await self._emit_symbol(
                source_id, toplevel, commit_sha,
                idx_artifact_id, evidence_ids, impl_ids,
                symbol_kind="function", kind_impl="symbol",
                name=symbol["name"], path=symbol["path"],
                line_start=symbol["line_start"], line_end=symbol["line_end"],
                signature=symbol.get("signature"),
            )

        sandbox = self.sandbox or Sandbox(toplevel, capability=detect_capability())
        ran_test = await self._try_run_tests(sandbox, toplevel)
        claim_status, _ev = await self._stage_behavioral_claim(
            sandbox, exec_artifact_id, ran_test, commit_sha,
        )

        return InvestigationResult(
            source_id=source_id,
            snapshot_hash=snapshot_hash,
            symbol_index_hash=symbol_index_hash,
            evidence_unit_ids=evidence_ids,
            implementation_entity_ids=impl_ids,
            sandbox=sandbox.capability,
            claim_evidence_status=claim_status,
        )

    async def _emit_symbol(
        self, source_id, toplevel, commit_sha, idx_artifact_id,
        evidence_ids, impl_ids, *,
        symbol_kind, kind_impl, name, path, line_start, line_end, signature,
    ) -> None:
        relpath = path
        src_text = _read_source(toplevel, relpath)
        locator = normalize_locator(
            "repo",
            {
                "commit": commit_sha,
                "path": relpath,
                "symbol": name,
                "line_start": line_start,
                "line_end": line_end,
            },
        )
        validate_locator("repo", locator)
        ev_id = await self.ctx.stage_evidence_unit(
            idx_artifact_id,
            locator,
            content_hash=content_hash(src_text) if src_text is not None else None,
            metadata={
                "evidence_status": "DIRECT_CODE_OBSERVATION",
                "spec_section": "§13.5",
                "source_symbol_kind": symbol_kind,
            },
        )
        impl_id = await self.ctx.stage_implementation_entity(
            source_id,
            kind_impl,
            path=relpath,
            symbol_name=name,
            commit_sha=commit_sha,
            line_start=line_start,
            line_end=line_end,
            signature=signature,
            content_hash=content_hash(src_text) if src_text is not None else None,
            metadata={"symbol_kind": symbol_kind},
        )
        # Both rows are staged through ctx, which links each to the bundle's
        # ``parsing`` prov_activity (§21.2 generation edges) — no extra edge
        # call is needed here.
        evidence_ids.append(ev_id)
        impl_ids.append(impl_id)

    async def _stage_behavioral_claim(
        self, sandbox: Sandbox, exec_artifact_id, ran_test: bool, commit_sha: str
    ) -> tuple[str, UUID]:
        from dra.investigators.claims import stage_behavioral_claim
        return await stage_behavioral_claim(
            self.ctx,
            sandbox,
            exec_artifact_id,
            "Repository test suite passes under the sandbox execution policy",
            sandbox_ran_test=ran_test,
            command=["pytest", "-q"],
            output_hash=commit_sha,
        )

    async def _try_run_tests(self, sandbox: Sandbox, repo_path: str) -> bool:
        """Run the repo's tests in the sandbox; return True iff they passed.

        Returns False when the sandbox is static-only (no execution runtime) —
        the investigator degrades to static inspection rather than hard-failing
        (spec §8.3).  Network is disabled and the FS is read-only + restricted.
        """
        if sandbox.capability is SandboxCapability.STATIC_ONLY:
            return False
        if not _has_tests(repo_path):
            return False
        result = sandbox.run(
            ["python", "-m", "pytest", "-q"], cwd=repo_path
        )
        if result is None:
            return False
        return result.returncode == 0

    def _resolve_to_local(self, repo_ref: str) -> str:
        if _is_remote_url(str(repo_ref)):
            return self._shallow_clone(str(repo_ref))
        path = os.path.abspath(os.fspath(repo_ref))
        if not _git_ok(path, ["rev-parse", "--is-inside-work-tree"]):
            raise ValueError(
                f"repo_ref {path!r} is not inside a git work tree"
            )
        return path

    def _shallow_clone(self, url: str) -> str:
        dest = tempfile.mkdtemp(prefix="dra-repo-")
        env = _sanitized_env()
        proc = subprocess.run(
            ["git", "clone", "--depth", "1", "--separate-git-dir",
             os.path.join(dest, ".git"), url, dest],
            capture_output=True, text=True, timeout=180, env=env,
        )
        if proc.returncode != 0:
            shutil.rmtree(dest, ignore_errors=True)
            raise ValueError(f"shallow clone of {url!r} failed: {proc.stderr}")
        return dest


def _has_tests(repo_path: str) -> bool:
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = sorted([d for d in dirs if d not in _REPO_TREE_SKIP_DIRS])
        for fname in sorted(files):
            if fname.startswith("test_") and fname.endswith(".py"):
                return True
            if fname == "pytest.ini" or fname == "tox.ini":
                return True
    pp = os.path.join(repo_path, "pyproject.toml")
    if os.path.isfile(pp):
        try:
            import tomllib
            with open(pp, "rb") as fh:
                data = tomllib.load(fh)
            if data.get("tool", {}).get("pytest") or \
               "pytest" in str(data.get("project", {}).get("dev-dependencies", [])):
                return True
        except Exception:
            pass
    return False


def _read_source(repo_path: str, relpath: str) -> str | None:
    try:
        with open(os.path.join(repo_path, relpath), "r",
                  encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None
