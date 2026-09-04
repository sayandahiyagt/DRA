"""Deterministic tiny corpus + AST analysis for the §38.1 control-plane bake-off.

Non-canonical prototype code. Lives outside ``src/`` so it is never packaged by
``[tool.setuptools.packages.find] where = ["src"]``. No network, no LLM — the
single user goal this corpus exists to answer:

    "For this package, list every public top-level symbol, locate the auth
    entry point, and assert whether the config function is safe to call
    before init."

The analysis is pure (stdlib ``ast`` only) so it is unit-testable without a DB.
"""
from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# The fixed corpus (4 files, seed-pinned, deterministic, ASCII-safe).
# A sibling of the `proof_corpus` deterministic generator style.
# ---------------------------------------------------------------------------

_AUTH_PY = '''"""Authentication entry point for the bake-off package.

This module is intentionally tiny and deterministic: it exists only so the
§38.1 bake-off has a fixed, reproducible artifact to investigate.
"""


def authenticate(token: str, *, realm: str = "default") -> bool:
    """Validate *token* against the configured *realm*.

    Returns True for any non-empty token (deterministic stub — no network).
    """
    if not token:
        return False
    return True


def _private_helper(x: int) -> int:
    """Module-private helper; must NOT appear in the public symbol list."""
    return x + 1


def login(username: str, password: str) -> str:
    """Thin wrapper around authenticate (also an auth entry point)."""
    return "ok" if authenticate(password) else "denied"
'''

_CONFIG_PY = '''"""Configuration with an init-guard (safety-before-init check).

``configure`` is safe to call before init because it is a no-op when already
initialised — the ``_INIT_DONE`` flag guards the real work.
"""

_INIT_DONE = False


def configure(settings_dir: str) -> None:
    """Load configuration from *settings_dir*.

    Safe to call before init: idempotent no-op when ``_INIT_DONE`` is set.
    """
    global _INIT_DONE
    if _INIT_DONE:
        return
    _INIT_DONE = True


def is_configured() -> bool:
    """Return whether configure() has run."""
    return _INIT_DONE
'''

_DATA_PY = '''"""Data-access function (deterministic stub)."""


def fetch_record(record_id: int) -> dict | None:
    """Fetch one record by id (deterministic stub, no I/O)."""
    return {"id": record_id, "name": "record"}


class Repository:
    """Tiny data repository (public class symbol)."""

    def __init__(self, name: str) -> None:
        self.name = name

    def get(self, record_id: int) -> dict | None:
        return fetch_record(record_id)
'''

_README = """# bake-off-corpus

A tiny deterministic package for the §38.1 control-plane bake-off.

Goal: list every public top-level symbol, locate the auth entry point, and
assert whether the config function is safe to call before init.
"""

PKG_FILES: dict[str, str] = {
    "auth.py": _AUTH_PY,
    "config.py": _CONFIG_PY,
    "data.py": _DATA_PY,
    "README.md": _README,
}


@dataclass
class SymbolRef:
    file: str
    name: str
    kind: str  # "function" | "class"
    line: int


@dataclass
class Analysis:
    """Structured findings from the tiny-corpus investigation."""

    files: list[str] = field(default_factory=list)
    public_symbols: list[SymbolRef] = field(default_factory=list)
    auth_entry_point: SymbolRef | None = None
    config_safe_before_init: bool = False
    corpus_hash: str = ""


def generate(dest_dir: str | Path) -> dict[str, str]:
    """Write the deterministic corpus into *dest_dir*; return {name: content_hash}.

    Idempotent: re-running overwrites with identical bytes (sha256 stable).
    """
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    out: dict[str, str] = {}
    for name, content in PKG_FILES.items():
        p = dest / name
        p.write_text(content, encoding="utf-8")
        out[name] = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return out


def _signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    args = [a.arg for a in node.args.args]
    if node.args.vararg:
        args.append("*" + node.args.vararg.arg)
    for a in node.args.kwonlyargs:
        args.append(a.arg)
    if node.args.kwarg:
        args.append("**" + node.args.kwarg.arg)
    return f"{node.name}({', '.join(args)})"


def _find_init_guard(module: ast.Module) -> bool:
    """True if the module defines a global flag + a function that reads it.

    A config function is "safe to call before init" when it short-circuits on an
    init flag rather than unconditionally executing privileged setup.
    """
    flag_names: set[str] = set()
    for node in module.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and isinstance(node.value, ast.Constant) and node.value.value is False:
                    flag_names.add(t.id)
    if not flag_names:
        return False
    for node in module.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Name) and sub.id in flag_names:
                    return True
    return False


def analyze(pkg_dir: str | Path) -> Analysis:
    """AST-analyze the corpus package at *pkg_dir*.

    Returns the structured findings used by every variant's investigation step.
    Pure — no DB, no network, deterministic for the generated corpus.
    """
    pkg = Path(pkg_dir)
    module_files = sorted(p for p in pkg.glob("*.py") if not p.name.startswith("_"))
    files = [p.name for p in module_files]

    public_symbols: list[SymbolRef] = []
    auth_entry: SymbolRef | None = None
    config_safe = False

    for f in module_files:
        src = f.read_text(encoding="utf-8")
        tree = ast.parse(src, filename=str(f))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith("_"):
                    continue
                sym = SymbolRef(file=f.name, name=node.name, kind="function", line=node.lineno)
                public_symbols.append(sym)
                low = node.name.lower()
                if "auth" in low or low in ("login",):
                    auth_entry = auth_entry or sym
            elif isinstance(node, ast.ClassDef):
                if node.name.startswith("_"):
                    continue
                public_symbols.append(SymbolRef(file=f.name, name=node.name, kind="class", line=node.lineno))

        # config safety: look for a function whose body references a global False flag.
        tree = ast.parse(src, filename=str(f))  # parsed above too; keep explicit
        try:
            cfg_tree = ast.parse(src)
        except SyntaxError:
            continue
        if _find_init_guard(cfg_tree):
            # Only treat as the config function if a *configure*-like name exists.
            has_cfg_fn = any(
                isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and ("config" in n.name.lower() or n.name == "configure")
                for n in cfg_tree.body
            )
            if has_cfg_fn:
                config_safe = True

    if auth_entry is None:
        # fall back to any symbol containing 'token' or 'login'
        for s in public_symbols:
            if "login" in s.name.lower() or "token" in s.name.lower():
                auth_entry = s
                break

    all_bytes = "".join(PKG_FILES[n] for n in files if n in PKG_FILES)
    return Analysis(
        files=files,
        public_symbols=public_symbols,
        auth_entry_point=auth_entry,
        config_safe_before_init=config_safe,
        corpus_hash=hashlib.sha256(all_bytes.encode("utf-8")).hexdigest(),
    )
