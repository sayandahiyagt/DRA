"""Bootstrap for the bake-off test suite: make dra + bake-off packages
importable when pytest runs from the repo root.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]  # repo root (tests -> bake-off -> root)
for p in (str(_ROOT / "src"), str(_ROOT / "bake-off"),
          str(_ROOT / "bake-off" / "variant_c_deerflow" / "deerflow"
              / "backend" / "packages" / "harness")):
    if p not in sys.path:
        sys.path.insert(0, p)

_cfg = _ROOT / "bake-off" / "variant_c_deerflow" / "deerflow" / "config.yaml"
if _cfg.exists() and not os.environ.get("DEER_FLOW_CONFIG_PATH"):
    os.environ["DEER_FLOW_CONFIG_PATH"] = str(_cfg)

# Reuse the repo's DB gate.
from _db import DB, _db_reachable  # noqa: E402
