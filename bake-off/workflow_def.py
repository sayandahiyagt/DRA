"""§38.1 bake-off shared workflow definition (non-canonical prototype).

The *identical* §2 lifecycle — recon->fan-out->investigate->commit->verify->
synthesize over a tiny deterministic local corpus — reused verbatim by all three
harness variants. Every finding is staged through ``dra.publish`` /
``publish_bundle`` so the dra.publish evidence-graph bundle/commit contract
stays the source of truth (§38.1/§42).

Invariants asserted by ``bake-off/tests/test_all_variants.py`` for every variant:
  - exactly one canonical ``prov_bundle`` per logical commit (idempotent retry);
  - no finding lives only in agent-internal state (every finding has a
    ``prov_entity`` row in ``canonical`` state linked to a ``prov_activity``);
  - retry-after-crash resumes from the last checkpoint and does NOT double-commit
    canonical rows;
  - mid-publish cancel rolls back atomically (0 leaked canonical rows).

This module documents those invariants and re-exports the shared corpus +
evidence-emission lifecycle so each variant's harness wraps the same contract.
"""
from __future__ import annotations

from pathlib import Path

from corpus import generate, analyze, Analysis, SymbolRef  # noqa: F401
import evidence  # noqa: F401  (the §2 evidence-emission lifecycle)

CORPUS_DIR = str(Path(__file__).resolve().parent / "_corpus")

# The eight dimensions measured per variant (see bake-off/measure.py).
DIMS = [
    "implementation_effort",
    "checkpoint_resume",
    "parallel_isolation",
    "observability",
    "artifact_evidence_friction",
    "cancellation_retry",
    "context_growth",
    "operational_complexity",
]

INVARIANTS = [
    "exactly one canonical prov_bundle per logical commit (idempotent retry)",
    "no finding lives only in agent-internal state (every finding has a prov_entity row)",
    "retry-after-crash resumes from last checkpoint without double-committing",
    "mid-publish cancel rolls back atomically (0 leaked canonical rows)",
]
