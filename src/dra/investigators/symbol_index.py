"""tree-sitter code-structure extraction (spec §11.2 §15.3).

Walks a repository tree with tree-sitter, extracting the structural facts an
investigator needs to emit normalized evidence: modules (files), classes,
functions/methods, imports, and entry points with their line spans (§15.3
structural edges).  The extraction is pure (no DB) and degrades gracefully when
a file cannot be parsed.

The grammars are declared explicitly in ``pyproject.toml`` (dra#24) so the
import does not depend on a transitive docling pull (Discovery #2).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

_LOCKFILES = (
    "uv.lock", "poetry.lock", "requirements.txt", "requirements-dev.txt",
    "pyproject.toml", "package-lock.json", "yarn.lock", "Cargo.lock",
    "go.mod", "pom.xml", "Gemfile.lock", "composer.lock", "mix.lock",
)
_ENTRY_FUNCTION_NAMES = {"main", "app", "run", "cli", "serve", "worker"}


@dataclass
class SymbolSpan:
    """A single structural symbol: kind + name + 1-indexed inclusive span."""

    kind: str  # "module" | "class" | "function" | "method"
    name: str
    line_start: int
    line_end: int
    signature: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "name": self.name,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "signature": self.signature,
        }


@dataclass
class FileSymbols:
    """Structural symbols extracted from one source file."""

    path: str  # repo-relative path
    language: str
    module: SymbolSpan | None = None
    classes: list[SymbolSpan] = field(default_factory=list)
    functions: list[SymbolSpan] = field(default_factory=list)
    methods: list[SymbolSpan] = field(default_factory=list)
    imports: list[tuple[str, str]] = field(default_factory=list)
    entry_points: list[dict[str, Any]] = field(default_factory=list)

    def all_symbols(self) -> list[SymbolSpan]:
        out: list[SymbolSpan] = []
        if self.module is not None:
            out.append(self.module)
        out.extend(self.classes)
        out.extend(self.functions)
        out.extend(self.methods)
        return out


# ---------------------------------------------------------------------------
# tree-sitter bootstrap
# ---------------------------------------------------------------------------

_LANGUAGE_CACHE: dict[str, Any] = {}


def _language_name(relpath: str) -> str:
    ext = relpath.rsplit(".", 1)[-1].lower() if "." in relpath else ""
    return {
        "py": "python", "js": "javascript", "ts": "typescript", "c": "c",
    }.get(ext, "unknown")


def _coerce_language(raw: Any) -> Any:
    """Coerce a grammar's ``language()`` return into a tree-sitter Language.

    Handles both tree-sitter >=0.24 grammars (return a ``Language`` directly)
    and older grammars that return a raw capsule/bytes object.
    """
    try:
        from tree_sitter import Language
    except ImportError as exc:  # pragma: no cover - substrate guarantees import
        raise RuntimeError(
            "tree-sitter is not installed; run `uv sync` (pyproject declares "
            "the grammars explicitly as of dra#24)"
        ) from exc
    if isinstance(raw, Language):
        return raw
    return Language(raw)


def _python_language() -> Any:
    if "python" not in _LANGUAGE_CACHE:
        import tree_sitter_python as grammar
        _LANGUAGE_CACHE["python"] = _coerce_language(grammar.language())
    return _LANGUAGE_CACHE["python"]


_LANGUAGES: list[tuple[str, str]] = [
    ("python", "py"),
    ("javascript", "js"),
    ("typescript", "ts"),
    ("c", "c"),
]


def _language_for(relpath: str) -> Any | None:
    """Return a tree-sitter Language for *relpath* by file extension, or None."""
    ext = relpath.rsplit(".", 1)[-1].lower() if "." in relpath else ""
    for lang_name, suffix in _LANGUAGES:
        if ext == suffix:
            try:
                if lang_name == "python":
                    return _python_language()
                module = __import__(f"tree_sitter_{lang_name}", fromlist=["language"])
                return _coerce_language(module.language())
            except Exception:
                return None
    return None


def _make_parser(language: Any):
    try:
        parser = Parser(language=language)
    except TypeError:
        from tree_sitter import Parser as _Parser
        parser = _Parser()
        try:
            parser.language = language
        except Exception:
            parser.set_language(language)  # legacy bindings
    return parser


# late import so the module loads even if tree-sitter is absent (pure helpers
# still importable for type/LOCATOR_SHAPES consumers)
try:
    from tree_sitter import Parser
except Exception:  # pragma: no cover - exercised only when tree-sitter missing
    Parser = None  # type: ignore[assignment]


def _span(node: Any) -> tuple[int, int]:
    start = node.start_point
    end = node.end_point
    return (start[0] + 1, end[0] + 1)


def _signature(node: Any, source: bytes) -> str | None:
    """Header text of a function/class (everything before the body block)."""
    body = node.child_by_field_name("body")
    if body is None:
        body = node.child_by_field_name("block")
    if body is None:
        for child in node.children:
            if child.type == "block":
                body = child
                break
    end = body.start_byte if (body is not None and body.start_byte > node.start_byte) \
        else node.end_byte
    try:
        return source[node.start_byte:end].decode("utf-8", "replace").strip()
    except Exception:
        return None


def _name(node: Any) -> str | None:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        name_node = node.child_by_field_name("identifier")
    return name_node.text.decode("utf-8", "replace") if name_node is not None else None


def _iter_named(node: Any):
    """Depth-first iteration including ``node`` itself."""
    stack = [node]
    while stack:
        cur = stack.pop()
        yield cur
        # children left-to-right by pushing reversed
        stack.extend(reversed(cur.children))


def extract_file(path: str, source: bytes | str | None = None) -> FileSymbols:
    """Extract structural symbols from a single file.

    ``path`` is the repo-relative path used both for language resolution and as
    the module name.  Returns an empty :class:`FileSymbols` (with the module
    span still populated) when the file cannot be parsed.
    """
    if isinstance(source, str):
        source = source.encode("utf-8")
    if source is None:
        try:
            with open(path, "rb") as fh:
                source = fh.read()
        except OSError:
            source = b""
    relpath = path
    language = _language_for(relpath)
    fs = FileSymbols(
        path=relpath,
        language=_language_name(relpath) if language is not None else "unknown",
    )

    # Module span is always knowable (1..last line) even without a parser.
    last_line = max(1, source.decode("utf-8", "replace").count("\n") or 1)
    module_name = os.path.splitext(os.path.basename(relpath))[0]
    fs.module = SymbolSpan(kind="module", name=module_name,
                           line_start=1, line_end=last_line,
                           signature="module file")

    if language is None or Parser is None:
        return fs

    try:
        parser = _make_parser(language)
        tree = parser.parse(source)
    except Exception:
        return fs

    root = tree.root_node
    _extract_structure(fs, root, source)
    _extract_imports(fs, root, source)
    _extract_entry_points(fs, root, relpath, source)
    return fs


def _extract_structure(fs: FileSymbols, root: Any, source: bytes) -> None:
    """Walk the tree for class/function/method definitions and entry points."""
    for node in _iter_named(root):
        if node.type in ("class_definition", "function_definition") or \
                node.type == "decorated_definition":
            _handle_definition(fs, node, source)


def _nearest_ancestor(node: Any, types: set[str]) -> Any | None:
    """Walk up ``node.parent`` chain returning the first ancestor of *types*."""
    cur = node.parent
    while cur is not None:
        if cur.type in types:
            return cur
        cur = cur.parent
    return None


def _handle_definition(fs: FileSymbols, node: Any, source: bytes) -> None:
    """Record class/function/method nodes, unwrapping decorators."""
    if node.type == "decorated_definition":
        for child in node.children:
            if child.type in ("class_definition", "function_definition"):
                _handle_definition(fs, child, source)
        return

    name = _name(node) or "<anonymous>"
    start, end = _span(node)
    sig = _signature(node, source)
    if node.type == "function_definition":
        # A function is a method if a class_definition is an ancestor.
        if _nearest_ancestor(node, {"class_definition"}) is not None:
            fs.methods.append(SymbolSpan("method", name, start, end, sig))
        else:
            fs.functions.append(SymbolSpan("function", name, start, end, sig))
            if name in _ENTRY_FUNCTION_NAMES:
                fs.entry_points.append({"path": fs.path, "symbol": name,
                                        "kind": "entry_function"})
    elif node.type == "class_definition":
        fs.classes.append(SymbolSpan("class", name, start, end, sig))
        if name in _ENTRY_FUNCTION_NAMES:
            fs.entry_points.append({"path": fs.path, "symbol": name,
                                    "kind": "entry_class"})


def _extract_imports(fs: FileSymbols, root: Any, source: bytes) -> None:
    """Collect (module, imported_name) tuples from import statements."""
    text = source.decode("utf-8", "replace")
    for node in _iter_named(root):
        if node.type == "import_statement":
            _imports_from(node, text, fs.imports, from_import=False)
        elif node.type == "import_from_statement":
            _imports_from(node, text, fs.imports, from_import=True)


def _imports_from(node: Any, text: str, out: list[tuple[str, str]],
                  *, from_import: bool = False) -> None:
    """Parse an import statement's text into (module, imported_name) tuples."""
    raw = " ".join(text[node.start_byte:node.end_byte].split())
    if from_import:
        if not raw.startswith("from "):
            return
        body = raw[len("from "):]
        mod_part, _, rest = body.partition(" import ")
        module = mod_part.strip()
        rest = rest.strip()
        if rest.startswith("("):
            rest = rest[1:]
        if rest.endswith(")"):
            rest = rest[:-1]
        for piece in rest.split(","):
            piece = piece.strip()
            if not piece:
                continue
            name = piece.split(" as ")[-1].strip() if " as " in piece else piece
            name = name.strip()
            if name:
                out.append((module, name))
    else:
        if not raw.startswith("import "):
            return
        body = raw[len("import "):].strip()
        for piece in body.split(","):
            piece = piece.strip()
            if not piece:
                continue
            if piece.startswith("."):
                stripped = piece.lstrip(".")
                if not stripped:
                    continue
                name = stripped.split(" as ")[0].strip()
                out.append(("." + stripped, name))
            else:
                if " as " in piece:
                    module, alias = piece.split(" as ", 1)
                else:
                    module = piece
                    alias = module.split(".")[0].strip()
                out.append((module.strip(), alias.strip()))


def _extract_entry_points(fs: FileSymbols, root: Any, relpath: str, source: bytes) -> None:
    """Flag ``__main__`` guards as entry points."""
    text = source.decode("utf-8", "replace")
    if "__name__" in text and "__main__" in text:
        fs.entry_points.append({
            "path": fs.path, "symbol": "__main__", "kind": "main_guard",
        })


# ---------------------------------------------------------------------------
# Repo-wide indexing
# ---------------------------------------------------------------------------

def _walk_source_files(repo_path: str):
    """Yield (relpath, abs_path) for source files under repo_path (no .git)."""
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = sorted([d for d in dirs if d not in (".git", "__pycache__",
                                               ".tox", ".mypy_cache", "node_modules")])
        for fname in sorted(files):
            ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
            if ext not in {"py", "js", "ts", "c"}:
                continue
            abs_path = os.path.join(root, fname)
            rel = os.path.relpath(abs_path, repo_path)
            yield rel, abs_path


def _entry_points_from_pyproject(repo_path: str) -> list[dict[str, Any]]:
    """Pull entry points from pyproject.toml [project.scripts] / dev scripts."""
    pts: list[dict[str, Any]] = []
    pp = os.path.join(repo_path, "pyproject.toml")
    if not os.path.isfile(pp):
        return pts
    try:
        import tomllib
    except ImportError:  # pragma: no cover
        try:
            import tomli as tomllib  # type: ignore
        except ImportError:
            return pts
    try:
        with open(pp, "rb") as fh:
            data = tomllib.load(fh)
    except Exception:
        return pts
    scripts = data.get("project", {}).get("scripts", {})
    if isinstance(scripts, dict):
        for name in scripts:
            pts.append({"path": "pyproject.toml", "symbol": name,
                        "kind": "console_script"})
    dev_scripts = data.get("tool", {}).get("dev", {}).get("scripts", {})
    if isinstance(dev_scripts, dict):
        for name in dev_scripts:
            pts.append({"path": "pyproject.toml", "symbol": name,
                        "kind": "dev_script"})
    return pts


def index_repo(repo_path: str) -> dict[str, Any]:
    """Build the symbol-index dict for a repo (spec §11.2 §15.3).

    Returns ``{modules, classes, functions, imports, edges, entry_points}``
    with a deterministic ``content_hash`` of the JSON blob.
    """
    modules: list[dict[str, Any]] = []
    classes: list[dict[str, Any]] = []
    functions: list[dict[str, Any]] = []
    imports: list[dict[str, Any]] = []
    entry_points: list[dict[str, Any]] = []

    for rel, abs_path in _walk_source_files(repo_path):
        try:
            with open(abs_path, "rb") as fh:
                data = fh.read()
            fs = extract_file(rel, source=data)
        except OSError:
            continue
        if fs.module is not None:
            modules.append({
                "path": rel, "language": fs.language,
                "symbol": fs.module.name,
                "line_start": fs.module.line_start,
                "line_end": fs.module.line_end,
            })
        classes.extend(_to_dicts(fs.classes, rel))
        functions.extend(_to_dicts(fs.functions, rel))
        functions.extend(_to_dicts(fs.methods, rel))
        for module, imported in fs.imports:
            imports.append({"module": module, "name": imported, "path": rel})
        entry_points.extend(fs.entry_points)

    entry_points.extend(_entry_points_from_pyproject(repo_path))

    edges: list[dict[str, Any]] = []
    # §15.3 structural edges: module -> imports.
    for imp in imports:
        edges.append({"kind": "imports", "from_path": imp["path"],
                      "to_module": imp["module"]})

    index = {
        "modules": modules,
        "classes": classes,
        "functions": functions,
        "imports": imports,
        "edges": edges,
        "entry_points": entry_points,
    }
    from dra.investigators import content_hash
    index["content_hash"] = content_hash(json.dumps(index, sort_keys=True))
    return index


def _to_dicts(spans: list[SymbolSpan], relpath: str) -> list[dict[str, Any]]:
    out = []
    for s in spans:
        d = s.to_dict()
        d["path"] = relpath
        out.append(d)
    return out
