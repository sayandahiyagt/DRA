"""Pytest fixture: build a real local git repo in ``tmp_path``.

The fixture repo mirrors a minimal but realistic Python package: a class with a
method, a top-level function, a ``main`` entry point, a ``__main__`` guard, an
import, a ``pyproject.toml`` with a console-script entry point, a ``LICENSE``
(MIT), and a submodule-bearing lockfile (``uv.lock``).  It returns
``(repo_path, commit_sha, toplevel)`` so DB-gated tests can assert canonical
publication and a provenance traversal back to the committed raw_capture and
source_identity.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

MAIN_PY = """\
\"\"\"Sample package module for the repo investigator fixture.\"\"\"
import os
from os import path


class Calculator:
    \"\"\"A trivial calculator.\"\"\"

    def add(self, a, b):
        \"\"\"Return the sum of a and b.\"\"\"
        return a + b

    def _private(self):
        return 0


def multiply(a, b):
    \"\"\"Return the product of a and b.\"\"\"
    return a * b


def main():
    \"\"\"Entry point.\"\"\"
    print(multiply(2, 3))


if __name__ == "__main__":
    main()
"""

README_MD = """\
# sample-pkg

A trivial sample package for the DRA repo-investigator fixture.

## Usage

.. code-block:: python

   from sample_pkg import multiply
   print(multiply(2, 3))

## License

MIT (see ``LICENSE``).
"""

PYPROJECT_TOML = """\
[project]
name = "sample_pkg"
version = "0.1.0"
description = "fixture repo for dra#24"
requires-python = ">=3.12"
dependencies = []

[project.scripts]
sample-cli = "sample_pkg:main"

[tool.dev.scripts]
dev-tool = "sample_pkg:multiply"
"""

UV_LOCK = """\
# mock uv.lock for the fixture repo
version = "1"

[[package]]
name = "sample_pkg"
version = "0.1.0"
"""

LICENSE_MIT = """\
MIT License

Copyright (c) 2026 fixture

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software").
"""


def _git(path: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True, text=True, timeout=60,
    )
    return proc.stdout.strip()


def build_repo(tmp_path: Path) -> tuple[str, str, str]:
    """Create a committed git repo under ``tmp_path/sample_repo``.

    Returns ``(repo_path, commit_sha, toplevel)`` where ``toplevel`` equals
    ``repo_path``.  The repo is a single commit so the snapshot is stable.
    """
    repo = tmp_path / "sample_repo"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "sample_pkg.py").write_text(MAIN_PY)
    (repo / "README.md").write_text(README_MD)
    (repo / "pyproject.toml").write_text(PYPROJECT_TOML)
    (repo / "uv.lock").write_text(UV_LOCK)
    (repo / "LICENSE").write_text(LICENSE_MIT)

    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "fixture@example.com")
    _git(repo, "config", "user.name", "fixture")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "fixture: initial commit")

    sha = _git(repo, "rev-parse", "HEAD")
    toplevel = _git(repo, "rev-parse", "--show-toplevel")
    return str(repo), sha, toplevel
