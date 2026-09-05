"""Alembic single-head gate (Wave 0 + Wave 1a + Wave 1b, dra#59/#78/#79).

Pure-Python, NO Postgres required — always runs in CI. Embodies the Wave 0 gate
"no ambiguous migration heads may ship before any later schema wave" (docs/evolution
spec.md §389 / §384): the Alembic revision graph must have exactly one head, and the
historical double-head of 0007_*/0008 must remain reconciled as a single linear
trunk so that no future PR reintroduces a second head.

The lineage at repository HEAD (Wave 1b, dra#79) is:

    0001_enable_pgvector
      └─ 0002_evidence_schema
          └─ 0003_storage_proof_schema
              └─ 0004_verification_gate_indexes
                  ├─┬─ 0004_implementation_entity_state ─ 0006_model_routing_schema ─ 0007_web_crawl_manifest ─┐
                  │ └─ 0005_implementation_entity_state ─ 0007_paper_investigator ─────────────────────────────── 0008 (merge)
                  └─── 0008_interview_constraints
                      └─ 0009_knowledge_schema_baseline (Wave 0 v1 baseline)
                          └─ 0010_source_capture_model (Wave 1a — ContentBlob/SourceCapture model)
                              └─ 0011_source_candidate (Wave 1b — SourceCandidate discovery table)

0008_interview_constraints is the merge node whose ``down_revision`` is the pair
('0007_paper_investigator', '0007_web_crawl_manifest') with ``branch_labels == set()``
— a structural merge, not a live branch label. 0009 chains linearly off 0008,
0010 chains linearly off 0009, and 0011 chains linearly off 0010, so the single
head is now the Wave 1b ``0011_source_candidate`` sentinel. This test locks that
invariant so a future PR that adds a new migration off the 0007 trunk without a
merge fails the gate before it ships.
"""

from __future__ import annotations

import os

from alembic.config import Config
from alembic.script import ScriptDirectory

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _revision_graph() -> ScriptDirectory:
    cfg = Config(os.path.join(_REPO_ROOT, "alembic.ini"))
    return ScriptDirectory.from_config(cfg)


def test_single_alembic_head():
    """Exactly one migration head — no ambiguous parallel heads."""
    sd = _revision_graph()
    heads = sd.get_heads()
    assert len(heads) == 1, f"ambiguous migration heads: {heads}"


def test_head_is_wave1b_sentinel():
    """The sole head is the 0011 Wave 1b sentinel, chained off the 0010 Wave 1a baseline."""
    sd = _revision_graph()
    head = sd.get_revision(sd.get_heads()[0])
    assert head.revision == "0011_source_candidate"
    down = head.down_revision
    down_list = down if isinstance(down, tuple) else (down,)
    assert down_list == ("0010_source_capture_model",)


def test_head_merges_double_head_lineage():
    """The 0008 merge node still resolves the historical 0007 double-head.

    Locks the reconciliation finding from the Wave 0 ONBOARD: 0008 carries
    down_revision = ('0007_paper_investigator', '0007_web_crawl_manifest') and
    no live branch labels, so rewriting shipped migration history is unnecessary
    and would fail this gate.
    """
    sd = _revision_graph()
    merge = sd.get_revision("0008_interview_constraints")
    assert merge is not None, "0008_interview_constraints merge node is missing"
    assert merge.down_revision == (
        "0007_paper_investigator",
        "0007_web_crawl_manifest",
    )
    assert merge.branch_labels == set()
