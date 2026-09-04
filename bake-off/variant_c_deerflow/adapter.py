"""Variant C — DeerFlow-derived adapter (non-canonical reference harness).

Vendored DeerFlow 2 reference implementation (gitignored clone under
``bake-off/variant_c_deerflow/deerflow/``). This adapter wires DeerFlow's
``create_deerflow_agent`` onto the identical §2 lifecycle (the shared
``evidence`` tools route every finding through ``dra.publish``).

DeerFlow's native ``ThreadState`` materialises tool results into agent-internal
channels (``thread_data`` via ``ThreadDataMiddleware``, plus schema-defined
``artifacts``/``delegations``/``skill_context``) — so canonical evidence is NOT
held exclusively in ``dra.publish`` once the native path is exercised. This is
the §38.1/§42 disqualifying evidence-integration violation measured by the bake-off.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_DEERFLOW_HARNESS = (Path(__file__).resolve().parent / "deerflow"
                     / "backend" / "packages" / "harness")


def _bootstrap() -> None:
    root = Path(__file__).resolve()
    for p in root.parents:
        if (p / "pyproject.toml").exists():
            root = p
            break
    for p in (str(root / "src"), str(root / "bake-off"), str(_DEERFLOW_HARNESS)):
        if p not in sys.path:
            sys.path.insert(0, p)
    # Auto-point DeerFlow at the vendored config.yaml (gitignored clone) so the
    # bake-off runs without a pre-set DEER_FLOW_CONFIG_PATH env var.
    cfg = _DEERFLOW_HARNESS.parent / "config.yaml"
    if cfg.exists() and not os.environ.get("DEER_FLOW_CONFIG_PATH"):
        os.environ["DEER_FLOW_CONFIG_PATH"] = str(cfg)


def deerflow_available() -> bool:
    """True if the vendored DeerFlow 2 clone is present on disk."""
    return _DEERFLOW_HARNESS.exists()


# ---------------------------------------------------------------------------
# Lockfile-neutral shim: langgraph's ``create_agent`` (which DeerFlow's factory
# wraps) passes ``Runtime(context=None)`` to middlewares. DeerFlow's
# ``ThreadDataMiddleware.before_agent`` guards ``context = runtime.context or {}``
# on line 83 but then reads ``runtime.context.get("run_id")`` unguarded on line
# 110 — a latent DeerFlow bug. The shim wraps the Runtime so ``.context`` is a
# non-None dict (thread_id still resolves from config.configurable via line 87),
# letting the native ``thread_data`` channel populate. It does NOT promote any
# DeerFlow dependency into uv.lock.
# ---------------------------------------------------------------------------
class _ShimRuntime:
    """Minimal Runtime stand-in exposing a non-None ``context`` attribute."""

    def __init__(self, context: dict) -> None:
        self.context = context


def _apply_deerflow_context_shim() -> None:
    from deerflow.agents.middlewares.thread_data_middleware import (
        ThreadDataMiddleware,
    )

    orig = ThreadDataMiddleware.before_agent

    def _patched(self, state, runtime):
        return orig(self, state, _ShimRuntime(runtime.context or {}))

    ThreadDataMiddleware.before_agent = _patched


def create_agent(model, tools, checkpointer=None):
    """Build a DeerFlow agent wired to the §2 lifecycle tools + fake model.

    Uses the default ``RuntimeFeatures(sandbox=True)`` so ``ThreadDataMiddleware``
    is active — this is the native-state vector the bake-off measures (and the
    disqualifying evidence-integration channel).
    """
    _bootstrap()
    _apply_deerflow_context_shim()
    from deerflow.agents.factory import create_deerflow_agent
    from deerflow.agents.features import RuntimeFeatures

    feats = RuntimeFeatures()  # sandbox=True default -> ThreadDataMiddleware on
    return create_deerflow_agent(
        model=model, tools=tools, features=feats, checkpointer=checkpointer,
        name="bakeoff-variant-c",
    )
