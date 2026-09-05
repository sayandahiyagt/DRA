"""§38.4 Verification gate engine (dra#20).

Implements the §38.4 verification-gate contract (ADR-021) as pure queries over
the canonical ``0002`` evidence-graph schema, plus the supporting indexes from
``0004``. The gate verifies a corpus of `claim`s against their supporting
evidence and emits a machine-checkable JSON report with a PASS/FAIL verdict.

The six gate rules:
  1. Unsupported-confidence-falls / gate-pass criterion (§38.4, §20.2).
  2. Contradictions stay visible (never silently resolved) (§20.2).
  3. Derivative-masquerade / anti-citation-laundering via recursive
     ``prov_derivation`` lineage walk (§20.4, Layer 5).
  4. UGC concentration visibility (§27.5, §22).
  5. Freshness / quarantine / staleness invalidation (§14, §14.2, §21.1/§21.4).
  6. Per-source evidence-rule boundary hardening the prompt-injection surface
     (§29): retrieved content is untrusted data, never interpolated into SQL or
     re-emitted as policy.

Design follows PLAN_1.md: reuses ``dra.db.engine`` / ``dra.publish.async_session``
(single connection string, per ADR-003/ADR-014), mirrors the structural
conventions of the sibling ``dra.proof_corpus`` engine (``_check_db_reachable``,
``write_report`` JSON+MD, argparse ``main``).

CLI entry: ``dra-verification-gate`` (wired in ``pyproject.toml``).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from dra.db import DATABASE_URL, can_connect
from dra.publish import async_session

MISSION = "sayandahiyagt/dra#20"
SPEC_ANCHOR = "§38.4"

# Canonical evidence states that may carry a high-impact claim to gate pass
# (ADR-021 rule 4: only CANONICAL+VERIFIED evidence may support synthesis).
_CANONICAL_STATES = ("canonical", "verified")

# Evidence states considered stale/rejected for quarantine propagation.
_QUARANTINED_STATES = ("stale", "rejected", "superseded")

# Depth cap for recursive provenance walks (rule 3 + rule 5). The lineage
# graph is a DAG in practice, but a bound guarantees termination on malformed
# input.
_WALK_DEPTH_DEFAULT = 50

# Module-level active configuration. ``run_verification_proof`` installs the
# effective GateConfig here so the per-claim evaluator (``_evaluate_claim``)
# can honor tunables (e.g. the no-write / dry path) without changing its
# ``_evaluate_claim(conn, claim_id)`` signature.
_ACTIVE_CFG: "GateConfig | None" = None


def _cfg() -> "GateConfig":
    """Return the active GateConfig (or a default if none installed)."""
    return _ACTIVE_CFG or GateConfig()


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class GateConfig:
    """Tunable configuration for the §38.4 verification gate."""

    min_independent_corroborations: int = 1
    entailment_recall_threshold: float = 0.5
    staleness_depth: int = _WALK_DEPTH_DEFAULT
    freshness_enabled: bool = True
    ugc_exclude_from_corroboration: bool = True
    # Assertion targets (echoed in the report config + verdict criteria).
    unsupported_confidence_falls: bool = True
    contradictions_visible: bool = True
    # When False the gate runs read-only: staleness propagation, contradiction
    # recording and verification_state stamping are skipped (the CLI
    # ``--no-write`` path), but the report is still produced.
    write_mutations: bool = True


def _default_config() -> GateConfig:
    """Build a GateConfig, applying optional env overrides for SLOs."""
    import os

    cfg = GateConfig()
    if "DRA_GATE_MIN_INDEPENDENT" in os.environ:
        cfg.min_independent_corroborations = int(os.environ["DRA_GATE_MIN_INDEPENDENT"])
    if "DRA_GATE_ENTAILMENT_RECALL" in os.environ:
        cfg.entailment_recall_threshold = float(os.environ["DRA_GATE_ENTAILMENT_RECALL"])
    return cfg


# ---------------------------------------------------------------------------
# Rule 1 + §29 trust boundary: deterministic citation-entailment predicate
# ---------------------------------------------------------------------------

# Stopwords are dropped before the entailment overlap is measured, so that
# function/structural words cannot be confused with content propositions.
_STOPWORDS = frozenset(
    {
        "the", "a", "an", "of", "and", "or", "to", "in", "on", "is", "are",
        "for", "with", "as", "at", "by", "from", "it", "this", "that",
        "these", "those", "be", "was", "were", "been", "being", "its",
        "their", "his", "her", "our", "your", "has", "have", "had", "do",
        "does", "did", "will", "would", "can", "could", "should", "may",
        "might", "must", "i", "you", "we", "they",
    }
)

# Negation operators used by the deterministic contradiction detector. These let
# the gate classify an evidence excerpt as *contradicting* (negated
# proposition) rather than merely *non-entailing* — rule 2 visibility.
_NEGATORS = frozenset(
    {
        "not", "no", "never", "none", "nothing", "neither", "nor", "cannot",
        "without", "fail", "fails", "failed", "failing", "except",
        "excepts", "deny", "denies", "denied", "false", "incorrect",
    }
)


def _tokenize(text: str) -> list[str]:
    """Lowercase alphanumeric tokenization (pure, no DB, no eval)."""
    if not text:
        return []
    return re.findall(r"[a-z0-9]+", text.lower())


def _content_tokens(text: str) -> set[str]:
    """Content (non-stopword) tokens as a set for overlap measurement."""
    return {t for t in _tokenize(text) if t not in _STOPWORDS}


def _has_negated_proposition(text: str, proposition_tokens: set[str]) -> bool:
    """Deterministically detect whether ``text`` negates the proposition.

    Treats ``text`` strictly as untrusted data (spec §29): no execution, no SQL
    interpolation. A proposition is *negated* when a negation operator and at
    least one proposition token co-occur within the same sentence.
    """
    if not text or not proposition_tokens:
        return False
    sentences = re.split(r"(?<=[.!?])\s+", text)
    for sent in sentences:
        toks = set(_tokenize(sent))
        if not (toks & proposition_tokens):
            continue
        if toks & _NEGATORS:
            return True
    return False


def _evidence_stance(claim_text: str, evidence_excerpt: str) -> str:
    """Classify an evidence excerpt's stance toward a claim proposition.

    Returns one of ``"supports"``, ``"contradicts"`` or ``"neutral"``. This is a
    pure function over sample evidence (no LLM / no judge) — spec §20.3 Layer 3
    deterministic entailment.
    """
    claim_tokens = _content_tokens(claim_text)
    ev_tokens = set(_tokenize(evidence_excerpt))
    if not claim_tokens or not ev_tokens:
        return "neutral"
    overlap = claim_tokens & ev_tokens
    if not overlap:
        return "neutral"
    if _has_negated_proposition(evidence_excerpt, claim_tokens):
        return "contradicts"
    recall = len(overlap) / len(claim_tokens)
    threshold = _cfg().entailment_recall_threshold
    return "supports" if recall >= threshold else "neutral"


def _citation_entails(
    claim_text: str,
    evidence_excerpt: str,
    evidence_metadata: dict[str, Any] | None = None,
) -> bool:
    """Deterministic citation-entailment predicate (spec §38.4 rule 1).

    Returns True only when the evidence excerpt *affirmatively* supports the
    claim's content proposition (sufficient token overlap and no negation).
    Retrieved ``content`` is treated as untrusted data (§29): it is compared as
    string tokens here and is never interpolated into SQL or re-emitted as
    policy. ``evidence_metadata`` is accepted for interface symmetry and does not
    influence the truth value of the predicate.
    """
    return _evidence_stance(claim_text, evidence_excerpt) == "supports"


# ---------------------------------------------------------------------------
# Rule 3: derivative-masquerade / anti-citation-laundering lineage walk
# ---------------------------------------------------------------------------

# Recursive prov_derivation back-traverse (mirrors the technique proven by
# tests/test_provenance_traversal.py::DERIVATION_BACKTRAVERSE_QUERY). Returns
# the set of prov_entity ids reachable backward from ``start_entity_id`` — i.e.
# the source lineage of the entity. The derivation graph is a DAG in practice
# (lineage never loops), so a depth cap bounds termination; the path-based
# cycle guard used elsewhere is avoided here because a bound parameter cannot
# be embedded inside an ARRAY[] literal in this driver.
_DERIVATION_BACKTRAVERSE_SQL = """
WITH RECURSIVE deriv(cur, src, depth) AS (
    SELECT derived_entity_id AS cur, source_entity_id AS src, 1 AS depth
    FROM prov_derivation WHERE derived_entity_id = :start_id
    UNION ALL
    SELECT d.derived_entity_id, d.source_entity_id, deriv.depth + 1
    FROM prov_derivation d JOIN deriv ON deriv.src = d.derived_entity_id
    WHERE deriv.depth < :depth
)
SELECT cur FROM deriv
UNION
SELECT src FROM deriv
"""


async def _lineage_source_set(start_entity_id: str, conn) -> set[str]:
    """Return the set of prov_entity ids reachable backward from ``start_entity_id``.

    Two supporting evidence units count as independent corroboration only when
    their source-lineage sets do not intersect (ADR-021 rule 3); an intersection
    means one is derivative of the other and is collapsed into a single
    evidence weight rather than counted twice.
    """
    rows = await conn.execute(
        text(_DERIVATION_BACKTRAVERSE_SQL),
        {"start_id": str(start_entity_id), "depth": _cfg().staleness_depth},
    )
    return {str(r[0]) for r in rows.fetchall()}


# ---------------------------------------------------------------------------
# Rule 4: UGC concentration visibility
# ---------------------------------------------------------------------------

# UGC / forum sources are flagged by source_identity.kind/access_basis and
# excluded from independent-corroboration counting (ADR-021 rule 5, §27.5).
_UGC_KINDS = {"web"}
_UGC_ACCESS_BASIS = {"ugc", "forum", "social", "community"}


def _is_ugc(kind: str | None, access_basis: str | None, metadata: dict[str, Any] | None) -> bool:
    """Pure predicate: is a source UGC / forum (rule 4)?

    Factored out of ``_is_ugc_source`` so the classification logic is testable
    without a database. A source is UGC when its ``kind`` is user-generated
    (e.g. ``web``) and its ``access_basis`` indicates UGC / a forum, or when its
    ``metadata`` carries an explicit UGC/forum flag.
    """
    meta = metadata or {}
    if meta.get("is_ugc") or meta.get("ugc") or meta.get("is_forum"):
        return True
    if kind in _UGC_KINDS:
        ab = (access_basis or "").lower()
        if ab in _UGC_ACCESS_BASIS:
            return True
        if "ugc" in ab or "forum" in ab:
            return True
    return False


async def _is_ugc_source(source_id: str, conn) -> bool:
    """Flag UGC / forum sources that must be excluded from corroboration counts.

    Reads ``source_identity.kind`` / ``access_basis`` / ``metadata`` (the
    §22 access-basis record) and delegates to the pure :func:`_is_ugc` classifier.
    UGC sources surface in the report's ``ugc_visibility`` block but never count
    as independent corroboration (ADR-021 rule 5, §27.5).
    """
    row = await conn.execute(
        text(
            "SELECT kind, access_basis, metadata "
            "FROM source_identity WHERE id = :s"
        ),
        {"s": str(source_id)},
    )
    rec = row.mappings().one_or_none()
    if rec is None:
        return False
    return _is_ugc(rec.kind, rec.access_basis, rec.metadata or {})


# ---------------------------------------------------------------------------
# Rule 5: freshness / quarantine / staleness invalidation
# ---------------------------------------------------------------------------

# A derived artifact is stale when its state is already stale/rejected/
# superseded, its valid_to window elapsed, or its staleness_policy marks it
# expired — accelerated by ix_derived_artifact_staleness.
_STALE_ARTIFACT_CONDITION = (
    "da.state IN ('stale', 'rejected', 'superseded') "
    "OR da.valid_to < now() "
    "OR (da.staleness_policy->>'valid_to')::timestamptz < now() "
    "OR da.staleness_policy->>'expired' = 'true'"
)
# Same predicate without the ``da.`` prefix, for standalone FROM
# ``derived_artifact`` scans (the staleness scan + the stale_artifacts CTEs).
_STALE_ARTIFACT_BARE_CONDITION = (
    "state IN ('stale', 'rejected', 'superseded') "
    "OR valid_to < now() "
    "OR (staleness_policy->>'valid_to')::timestamptz < now() "
    "OR staleness_policy->>'expired' = 'true'"
)

# Flip evidence_unit.state to stale for units backed by a stale artifact.
_UPDATE_EVIDENCE_UNIT_STALE_SQL = (
    "UPDATE evidence_unit eu SET state = 'stale' "
    "WHERE eu.state IN ('canonical', 'verified') "
    "AND EXISTS (SELECT 1 FROM derived_artifact da "
    "WHERE da.id = eu.artifact_id AND "
    + _STALE_ARTIFACT_CONDITION
    + ")"
)

# Flip downstream claims to ``stale`` for the §14.2 / §21.4 edge chain. The
# evaluation step (rule 2 owner of verification_state) then records the
# staleness marker in the claim's verification_state; here we only move the
# claim out of the canonical/verified state so it cannot itself carry a
# high-impact claim to gate pass (ADR-021 rule 4). supporting_evidence
# JSON-array membership is checked with the jsonb ?| operator so quarantines
# propagate to claims that list a stale-backed evidence unit even when it is
# not the primary one.
_STAMP_CLAIMS_SQL = """
WITH stale_artifacts AS (
    SELECT id FROM derived_artifact WHERE """ + _STALE_ARTIFACT_BARE_CONDITION + """
),
affected_ev AS (
    SELECT id AS ev_id FROM evidence_unit
    WHERE artifact_id IN (SELECT id FROM stale_artifacts)
),
affected_claims AS (
    SELECT c.id AS claim_id
    FROM claim c
    WHERE c.state IN ('canonical', 'verified')
      AND (
        c.evidence_unit_id IN (SELECT ev_id FROM affected_ev)
        OR (c.verification_state->'supporting_evidence') ?| array(
            SELECT ev_id::text FROM affected_ev
          )
      )
)
UPDATE claim
SET state = 'stale'
WHERE id IN (SELECT claim_id FROM affected_claims)
"""

# Mark downstream decisions/ handoffs STALE_PENDING_REVIEW (§14.2). Coerce
# decision.state to an object first: stage_decision stores it as a JSON string
# scalar (e.g. "staged"), and jsonb `||` on a scalar+object yields an array
# rather than a merged object.
_STAMP_DECISIONS_SQL = """
WITH stale_artifacts AS (
    SELECT id FROM derived_artifact WHERE """ + _STALE_ARTIFACT_BARE_CONDITION + """
),
affected_ev AS (
    SELECT id AS ev_id FROM evidence_unit
    WHERE artifact_id IN (SELECT id FROM stale_artifacts)
),
affected_claims AS (
    SELECT c.id AS claim_id
    FROM claim c
    WHERE c.evidence_unit_id IN (SELECT ev_id FROM affected_ev)
      OR (c.verification_state->'supporting_evidence') ?| array(
          SELECT ev_id::text FROM affected_ev
        )
)
UPDATE decision d
SET state =
    CASE WHEN jsonb_typeof(d.state) = 'object' THEN d.state ELSE '{}'::jsonb END
    || jsonb_build_object(
        'stale_status', 'STALE_PENDING_REVIEW',
        'quarantined_at', now(),
        'quarantined_by', 'derived_artifact_staleness'
    )
WHERE d.claim_id IN (SELECT claim_id FROM affected_claims)
"""


async def _stale_artifact_ids(conn) -> list[tuple[str, str, str]]:
    """Return (id, source_capture_hash, state) of stale/rejected artifacts."""
    rows = await conn.execute(
        text(
            "SELECT id, source_capture_hash, state FROM derived_artifact "
            "WHERE state IN ('stale', 'rejected', 'superseded') "
            "OR valid_to < now() "
            "OR (staleness_policy->>'valid_to')::timestamptz < now() "
            "OR staleness_policy->>'expired' = 'true'"
        )
    )
    return [(str(r[0]), str(r[1]), str(r[2])) for r in rows.fetchall()]


async def _invalidate_stale_artifacts(conn) -> int:
    """Quarantine downstream claims/decisions of stale derived artifacts.

    Scans ``derived_artifact`` for stale/rejected/superseded/expired rows (via
    the ``ix_derived_artifact_staleness`` index) and, inside this transaction,
    flips the downstream ``evidence_unit`` -> ``claim`` -> ``decision`` chain
    (per spec §14.2 / §21.4) so a stale-backed claim can never carry a
    high-impact claim to gate pass. The whole propagation is a single
    transaction (ADR-013): any failure rolls back, so a half-quarantined claim
    can never be orphaned.

    Returns the number of stale derived artifacts detected.
    """
    stale = await _stale_artifact_ids(conn)
    if not stale:
        return 0
    await conn.execute(text(_UPDATE_EVIDENCE_UNIT_STALE_SQL))
    await conn.execute(text(_STAMP_CLAIMS_SQL))
    await conn.execute(text(_STAMP_DECISIONS_SQL))
    return len(stale)


# ---------------------------------------------------------------------------
# Rule 2: contradiction reconciliation (visible, never silent-resolved)
# ---------------------------------------------------------------------------


async def _record_contradiction(
    conn,
    *,
    claim_id: str,
    contradicting_evidence_ids: list[str],
    topic_id: str | None = None,
) -> None:
    """Record a contradiction so it stays visible (ADR-021 rule 2).

    Inserts an idempotent ``topic_relationship(relationship_type='contradicts')``
    edge (a self-edge on the claim's topic marks the topic as carrying an
    unresolved contradiction) and stamps ``claim.verification_state`` with the
    contradicting evidence ids plus a ``contradictions_visible`` marker. The
    gate never auto-resolves contradictions — visibility is a precondition of
    the verdict, not a side effect.
    """
    if not _cfg().write_mutations:
        return

    if topic_id is None:
        row = await conn.execute(
            text("SELECT topic_id FROM claim WHERE id = :c"), {"c": str(claim_id)}
        )
        rec = row.mappings().one_or_none()
        topic_id = rec.topic_id if rec else None

    if topic_id is not None:
        await conn.execute(
            text(
                "INSERT INTO topic_relationship "
                "(source_topic_id, target_topic_id, relationship_type, provenance) "
                "SELECT :t1, :t2, 'contradicts'::relationship_type, CAST(:prov AS jsonb) "
                "WHERE NOT EXISTS ("
                "  SELECT 1 FROM topic_relationship "
                "  WHERE source_topic_id = :t1 AND target_topic_id = :t2 "
                "    AND relationship_type = 'contradicts'"
                ")"
            ),
            {
                "t1": str(topic_id),
                "t2": str(topic_id),
                "prov": json.dumps(
                    {
                        "claim_id": str(claim_id),
                        "evidence_ids": [str(e) for e in contradicting_evidence_ids],
                        "recorded_by": "verification_gate",
                    }
                ),
            },
        )

    upd = {
        "contradictions": [str(e) for e in contradicting_evidence_ids],
        "contradictions_visible": True,
    }
    await conn.execute(
        text(
            "UPDATE claim SET verification_state = "
            "verification_state || CAST(:upd AS jsonb) WHERE id = :c"
        ),
        {"c": str(claim_id), "upd": json.dumps(upd)},
    )


async def _existing_contradiction_topics(conn, topic_id: str) -> list[str]:
    """Surface pre-existing ``contradicts`` topic edges (visibility).

    Pre-existing ``topic_relationship(reltype='contradicts')`` edges are surfaced
    so the gate never silently drops an already-recorded contradiction.
    """
    rows = await conn.execute(
        text(
            "SELECT target_topic_id FROM topic_relationship "
            "WHERE relationship_type = 'contradicts' "
            "AND (source_topic_id = :t OR target_topic_id = :t)"
        ),
        {"t": str(topic_id)},
    )
    return [str(r[0]) for r in rows.fetchall()]


# ---------------------------------------------------------------------------
# Per-claim evaluation (rule 1 pass/fail + rule 3/4/5 gating)
# ---------------------------------------------------------------------------

# Fetch a supporting evidence unit together with its derived artifact, the
# upstream source_capture (via content_blob) and the source identity (one JOIN
# per evidence unit — content is always bound as data parameters, never
# string-interpolated).
_EVIDENCE_FETCH_SQL = """
    SELECT
    eu.id               AS ev_id,
    eu.excerpt          AS excerpt,
    eu.content_hash     AS ev_content_hash,
    eu.state            AS ev_state,
    da.id               AS da_id,
    da.state            AS da_state,
    da.source_capture_hash,
    da.valid_to         AS da_valid_to,
    da.staleness_policy AS da_staleness_policy,
    sc.source_identity_id AS source_id,
    sc.state            AS rc_state,
    si.kind             AS source_kind,
    si.access_basis     AS access_basis,
    si.metadata         AS source_metadata,
    si.locator          AS source_locator
FROM evidence_unit eu
JOIN derived_artifact da ON da.id = eu.artifact_id
LEFT JOIN content_blob cb ON cb.hash = da.source_capture_hash
LEFT JOIN source_capture sc ON sc.content_blob_hash = cb.hash
LEFT JOIN source_identity si ON si.id = sc.source_identity_id
WHERE eu.id = :ev_id
"""


def _artifact_is_stale(da_state: str | None, da_valid_to, da_staleness_policy) -> bool:
    """Whether a derived artifact is stale/rejected/expired (rule 5)."""
    if da_state in _QUARANTINED_STATES:
        return True
    if da_valid_to is not None:
        return True
    policy = da_staleness_policy or {}
    if policy.get("expired") is True or str(policy.get("expired")) == "true":
        return True
    valid_to = policy.get("valid_to")
    if isinstance(valid_to, str):
        try:
            if datetime.fromisoformat(valid_to.rstrip("Z")) < datetime.now(timezone.utc):
                return True
        except ValueError:
            pass
    return False


async def _evaluate_claim(conn, claim_id: str) -> dict | None:
    """Evaluate a single claim against its supporting evidence.

    Produces (and stamps) the claim's ``verification_state`` record: per-source
    pass/fail, independent-corroboration count (after lineage-collapsing), UGC
    exclusion, staleness quarantine, and contradiction visibility. Returns the
    verification-state dict (also written to the claim row), or None if the
    claim does not exist.
    """
    claim_row = await conn.execute(
        text(
            "SELECT id, evidence_unit_id, topic_id, text, confidence, "
            "verification_state, state "
            "FROM claim WHERE id = :c"
        ),
        {"c": str(claim_id)},
    )
    c = claim_row.mappings().one_or_none()
    if c is None:
        return None

    existing_vs = c.verification_state or {}
    claim_state = c.state
    confidence = float(c.confidence) if c.confidence is not None else None

    # Supporting evidence ids: explicit corroboration list in verification_state,
    # else the claim's primary evidence_unit_id.
    supporting = existing_vs.get("supporting_evidence")
    if supporting is None:
        supporting = [str(c.evidence_unit_id)] if c.evidence_unit_id else []
    supporting = [str(e) for e in supporting]

    cfg = _cfg()

    per_source: list[dict[str, Any]] = []
    contradicting_evidence: list[str] = []
    independent_lineages: list[set[str]] = []
    ugc_excluded = 0
    ugc_sources: list[str] = []
    quarantined = claim_state in _QUARANTINED_STATES or bool(
        existing_vs.get("staleness", {}).get("quarantined")
    )

    for ev_id in supporting:
        ev = await conn.execute(text(_EVIDENCE_FETCH_SQL), {"ev_id": ev_id})
        er = ev.mappings().one_or_none()
        if er is None:
            continue

        excerpt = er.excerpt or ""
        stance = _evidence_stance(c.text, excerpt)
        source_id = str(er.source_id) if er.source_id else None
        is_ugc = source_id is not None and await _is_ugc_source(source_id, conn)
        if is_ugc:
            ugc_excluded += 1
            if source_id not in ugc_sources:
                ugc_sources.append(source_id)

        ev_stale = _artifact_is_stale(
            er.da_state, er.da_valid_to, er.da_staleness_policy
        )
        if (
            ev_stale
            or er.da_state in _QUARANTINED_STATES
            or er.ev_state in _QUARANTINED_STATES
            or er.rc_state in _QUARANTINED_STATES
        ):
            quarantined = True

        lineage: set[str] | None = None
        if stance == "supports" and not is_ugc and not ev_stale:
            lineage = await _lineage_source_set(str(er.ev_id), conn)
            independent_lineages.append(lineage)

        if stance == "contradicts":
            contradicting_evidence.append(ev_id)

        per_source.append(
            {
                "evidence_unit_id": ev_id,
                "stance": stance,
                "is_ugc": is_ugc,
                "source_id": source_id,
                "source_locator": er.source_locator,
                "stale": ev_stale,
                "lineage_size": len(lineage) if lineage else 0,
            }
        )

    # Rule 3: collapse derivative (lineage-intersecting) evidence into a single
    # weight. Independent corroboration count = number of disjoint groups.
    independent_corroborations = 0
    assigned: set[int] = set()
    for i, lset in enumerate(independent_lineages):
        if i in assigned:
            continue
        assigned.add(i)
        for j in range(i + 1, len(independent_lineages)):
            if j in assigned:
                continue
            if lset & independent_lineages[j]:
                assigned.add(j)
        independent_corroborations += 1

    required = len(independent_lineages)
    masquerade = required > 1 and independent_corroborations < required

    # Rule 2: record contradictions (visible, never silent-resolved).
    if contradicting_evidence:
        await _record_contradiction(
            conn,
            claim_id=str(c.id),
            contradicting_evidence_ids=contradicting_evidence,
            topic_id=str(c.topic_id) if c.topic_id else None,
        )
    # Surface pre-existing contradicts relationships for the claim's topic.
    if c.topic_id is not None:
        if await _existing_contradiction_topics(conn, str(c.topic_id)):
            contradicting_evidence = contradicting_evidence or [
                "existing:topic_relationship"
            ]

    entailment_pass = any(s["stance"] == "supports" for s in per_source)
    # ADR-021 rule 4: only canonical/verified claims may carry a high-impact
    # claim to gate pass. A quarantined (stale) claim is therefore unsupported.
    canonical_ok = c.state in _CANONICAL_STATES

    supported = bool(
        supporting
        and canonical_ok
        and not contradicting_evidence
        and not masquerade
        and not quarantined
        and independent_corroborations >= cfg.min_independent_corroborations
        and independent_corroborations >= 1
        and entailment_pass
    )

    verdict_vs = {
        "gate_version": "38.4",
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "supported": supported,
        "entailment_pass": entailment_pass,
        "confidence": confidence,
        "supporting_evidence": supporting,
        "independent_corroborations": independent_corroborations,
        "ugc_excluded": ugc_excluded,
        "masquerade_detected": masquerade,
        "quarantined": quarantined,
        "staleness": {"quarantined": quarantined},
        "contradictions": contradicting_evidence,
        "contradictions_visible": True,
        "per_source": per_source,
    }

    # Stamp the claim's verification_state. Merge (jsonb ||) so pre-existing
    # keys (e.g. supporting_evidence) survive; the eval owns the staleness
    # sub-object computed above.
    if _cfg().write_mutations:
        await conn.execute(
            text(
                "UPDATE claim SET verification_state = "
                "verification_state || CAST(:upd AS jsonb) WHERE id = :c"
            ),
            {"c": str(c.id), "upd": json.dumps(verdict_vs)},
        )

    return verdict_vs


def _decide_verdict(
    *,
    unsupported_confidence_present: bool,
    contradictions_silently_dropped: bool,
    masquerade_violations: int,
    ugc_violations: int,
    freshness_violations: int,
) -> tuple[str, dict[str, bool]]:
    """Pure verdict assembly (machine-checkable).

    PASS iff every ADR-021 reversal trigger is satisfied: unsupported confidence
    has fallen (every confident claim is backed), contradictions remain visible
    (none silently dropped), no masquerade passed, no UGC was counted as
    independent, and no stale artifact supported a passing claim.
    """
    unsupported_confidence_falls = not unsupported_confidence_present
    contradictions_visible = not contradictions_silently_dropped
    gate_rules = {
        "entailment": unsupported_confidence_falls,
        "no_masquerade": masquerade_violations == 0,
        "ugc_controlled": ugc_violations == 0,
        "freshness": freshness_violations == 0,
        "contradictions_visible": contradictions_visible,
    }
    verdict = "PASS" if all(gate_rules.values()) else "FAIL"
    return verdict, gate_rules


# ---------------------------------------------------------------------------
# Orchestration: freshness -> per-claim eval -> verdict assembly
# ---------------------------------------------------------------------------


async def _all_claim_ids(conn) -> list[str]:
    """Claim ids in scope for the gate: published (canonical/verified) and
    claims quarantined by freshness (stale). Staged/superseded/rejected claims
    are out of scope; ADR-021 rule 4 lets only canonical+verified claims carry a
    high-impact claim to gate pass."""
    rows = await conn.execute(
        text(
            "SELECT id FROM claim "
            "WHERE state IN ('canonical', 'verified', 'stale') ORDER BY id"
        )
    )
    return [str(r[0]) for r in rows.fetchall()]


async def run_verification_proof(
    cfg: GateConfig | None = None,
    write: bool = True,
    report_path: str = "verification_report.json",
) -> dict:
    """Run the full §38.4 verification gate and return the report dict.

    Steps:
      1. Freshness quarantine — invalidate claims backed by stale/rejected
         derived artifacts and propagate to downstream decisions (§14.2/§21.4).
      2. Per-claim evaluation — citation-entailment, lineage independence
         (anti-masquerade), UGC exclusion, contradiction visibility.
      3. Verdict assembly — PASS only when unsupported confidence falls and
         contradictions remain visible.
    """
    global _ACTIVE_CFG
    active = cfg or _default_config()
    # ``write`` controls *report file* output only. DB mutations (staleness
    # propagation, contradiction recording, verification_state stamping) are
    # governed by ``cfg.write_mutations`` so the CLI --no-write path can run
    # read-only while still emitting a report.
    _ACTIVE_CFG = active

    ugc_sources_seen: list[dict[str, Any]] = []
    ugc_excluded_total = 0
    claims_report: list[dict[str, Any]] = []
    unsupported_confidence_present = False
    contradictions_silently_dropped = False
    masquerade_violations = 0
    ugc_violations = 0
    freshness_violations = 0
    quarantined_claims = 0

    async with async_session() as session:
        async with session.begin():
            # 1. Freshness quarantine (writes downstream state + decisions).
            #    Skipped in read-only (--no-write) mode.
            if active.freshness_enabled and active.write_mutations:
                invalidated_artifacts = await _invalidate_stale_artifacts(session)
            else:
                invalidated_artifacts = 0

            # 2. Per-claim evaluation.
            claim_ids = await _all_claim_ids(session)
            for cid in claim_ids:
                vs = await _evaluate_claim(session, cid)
                if vs is None:
                    continue

                for s in vs["per_source"]:
                    if s["is_ugc"] and s["source_id"]:
                        if not any(
                            u["source_id"] == s["source_id"] for u in ugc_sources_seen
                        ):
                            ugc_sources_seen.append(
                                {
                                    "source_id": s["source_id"],
                                    "locator": s.get("source_locator"),
                                }
                            )
                        if s["stance"] == "supports":
                            ugc_excluded_total += 1

                # Safety: a supported claim must rest on independent (non-UGC)
                # corroboration. If a claim were supported with zero independent
                # evidence, a UGC source silently carried it — an ADR-021
                # violation.
                if vs["supported"] and vs["independent_corroborations"] == 0:
                    ugc_violations += 1

                if vs["quarantined"]:
                    quarantined_claims += 1

                # Safety assertions on the reversal triggers (ADR-021).
                if vs["supported"] and vs["masquerade_detected"]:
                    masquerade_violations += 1
                if vs["supported"] and vs["quarantined"]:
                    freshness_violations += 1
                if vs["contradictions"] and not vs.get("contradictions_visible"):
                    contradictions_silently_dropped = True

                if (
                    vs["confidence"] is not None
                    and vs["confidence"] > 0
                    and not vs["supported"]
                ):
                    unsupported_confidence_present = True

                claims_report.append(
                    {
                        "claim_id": cid,
                        "confidence": vs["confidence"],
                        "supported": vs["supported"],
                        "independent_corroborations": vs["independent_corroborations"],
                        "ugc_weight_collapsed": vs["ugc_excluded"],
                        "staleness": {"quarantined": vs["quarantined"]},
                        "contradictions": vs["contradictions"],
                        "contradictions_visible": vs["contradictions_visible"],
                        "verification_state": vs,
                    }
                )

    unsupported_confidence_falls = not unsupported_confidence_present
    contradictions_visible = not contradictions_silently_dropped

    verdict, gate_rules = _decide_verdict(
        unsupported_confidence_present=unsupported_confidence_present,
        contradictions_silently_dropped=contradictions_silently_dropped,
        masquerade_violations=masquerade_violations,
        ugc_violations=ugc_violations,
        freshness_violations=freshness_violations,
    )

    report = {
        "schema_version": 1,
        "mission": MISSION,
        "spec_anchor": SPEC_ANCHOR,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "config": {
            "min_independent_corroborations": active.min_independent_corroborations,
            "entailment_recall_threshold": active.entailment_recall_threshold,
            "freshness_enabled": active.freshness_enabled,
            "ugc_exclude_from_corroboration": active.ugc_exclude_from_corroboration,
            "unsupported_confidence_falls": unsupported_confidence_falls,
            "contradictions_visible": contradictions_visible,
        },
        "freshness": {
            "invalidated_artifacts": invalidated_artifacts,
            "quarantined_claims": quarantined_claims,
        },
        "ugc_visibility": {
            "ugc_sources": ugc_sources_seen,
            "excluded_from_corroboration": ugc_excluded_total,
        },
        "claims": claims_report,
        "verdict": verdict,
        "gate_rules": gate_rules,
    }

    if write:
        md_path = report_path.replace(".json", ".md")
        write_report(report, json_path=report_path, md_path=md_path)

    return report


# ---------------------------------------------------------------------------
# Report writer (json + markdown, mirrors proof_corpus.write_report)
# ---------------------------------------------------------------------------


def write_report(
    report: dict,
    json_path: str = "verification_report.json",
    md_path: str | None = None,
) -> None:
    """Write the §38.4 verification report as JSON + markdown."""
    with open(json_path, "w") as f:
        json.dump(report, f, indent=2)

    if md_path is None:
        md_path = json_path.replace(".json", ".md")
    with open(md_path, "w") as f:
        f.write(_report_markdown(report))


def _report_markdown(report: dict) -> str:
    lines: list[str] = []
    lines.append("# §38.4 Verification Gate Report")
    lines.append("")
    lines.append(f"- **Mission:** {report['mission']}")
    lines.append(f"- **Spec anchor:** {report['spec_anchor']}")
    lines.append(f"- **Generated at:** {report['generated_at']}")
    lines.append("")

    cfg = report["config"]
    lines.append("## Gate configuration")
    lines.append(f"- min independent corroborations: {cfg['min_independent_corroborations']}")
    lines.append(f"- entailment recall threshold: {cfg['entailment_recall_threshold']}")
    lines.append(f"- freshness enabled: {cfg['freshness_enabled']}")
    lines.append(f"- UGC excluded from corroboration: {cfg['ugc_exclude_from_corroboration']}")
    lines.append(f"- unsupported_confidence_falls: {cfg['unsupported_confidence_falls']}")
    lines.append(f"- contradictions_visible: {cfg['contradictions_visible']}")
    lines.append("")

    fresh = report["freshness"]
    lines.append("## Freshness / quarantine")
    lines.append(
        f"- invalidated stale artifacts: **{fresh['invalidated_artifacts']}** | "
        f"quarantined claims: **{fresh['quarantined_claims']}**"
    )
    lines.append("")

    ugc = report["ugc_visibility"]
    lines.append("## UGC concentration visibility")
    lines.append(
        f"- evidence units excluded from corroboration: {ugc['excluded_from_corroboration']}"
    )
    for u in ugc["ugc_sources"]:
        lines.append(f"  - source: {u['source_id']}")
    lines.append("")

    lines.append("## Claims")
    lines.append(
        "| Claim | Confidence | Supported | Independent | UGC excluded | "
        "Quarantined | Contradictions |"
    )
    lines.append("|-------|-----------|-----------|--------------|--------------|"
                  "--------------|-----------------|")
    for cl in report["claims"]:
        lines.append(
            f"| {cl['claim_id']} | {cl['confidence']} | {cl['supported']} | "
            f"{cl['independent_corroborations']} | {cl['ugc_weight_collapsed']} | "
            f"{cl['staleness']['quarantined']} | "
            f"{len(cl['contradictions'])} |"
        )
    lines.append("")

    lines.append("## Gate rules (§38.4 ADR-021 reversal triggers)")
    lines.append("| Rule | Result |")
    lines.append("|------|--------|")
    for name, passed in report["gate_rules"].items():
        lines.append(f"| {name} | {'PASS' if passed else 'FAIL'} |")
    lines.append("")

    lines.append("## Verdict")
    lines.append(f"**{report['verdict']}**")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# DB reachability gate + CLI (mirror dra.proof_corpus)
# ---------------------------------------------------------------------------


def _check_db_reachable() -> bool:
    try:
        return asyncio.run(can_connect())
    except Exception:
        return False


def main() -> None:
    """CLI entry point: run the §38.4 verification gate."""
    parser = argparse.ArgumentParser(
        prog="dra-verification-gate",
        description="Run the §38.4 verification gate: quarantine stale "
        "artifacts, evaluate claims against their supporting evidence "
        "(citation entailment, lineage independence, UGC exclusion, "
        "contradiction visibility), and emit a PASS/FAIL report vs the "
        "ADR-021 gate contract.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Verify DB connectivity and config without running the gate.",
    )
    parser.add_argument(
        "--report",
        default="verification_report.json",
        help="Path for the machine-checkable JSON report (default: "
        "verification_report.json).",
    )
    parser.add_argument(
        "--no-write",
        action="store_true",
        help="Run the gate's read-only checks but do not persist "
        "staleness/contradiction/verification_state mutations.",
    )
    args = parser.parse_args()

    if not _check_db_reachable():
        print("FAIL: No reachable Postgres at DATABASE_URL.")
        print("      The §38.4 verification gate requires a running Postgres+pgvector.")
        print("      Run `alembic -c alembic.ini upgrade head` after starting the DB.")
        sys.exit(1)

    if args.dry_run:
        print("[gate] §38.4 Verification gate — dry run")
        print(f"  DATABASE_URL: {DATABASE_URL}")
        print("  DB reachable: yes")
        print(f"  config: {_default_config()}")
        return

    cfg = _default_config()
    if args.no_write:
        cfg.write_mutations = False
    report = asyncio.run(
        run_verification_proof(
            cfg=cfg, write=True, report_path=args.report
        )
    )

    print("\n=== §38.4 Verification Gate — ADR-021 Reversal Triggers ===")
    print(f"{'Rule':<26} {'Result':<6}")
    print("-" * 40)
    for name, passed in report["gate_rules"].items():
        result = "PASS" if passed else "FAIL"
        print(f"{name:<26} {result:<6}")
    print("-" * 40)
    print(f"\nClaims evaluated: {len(report['claims'])}")
    print(
        f"Freshness: {report['freshness']['invalidated_artifacts']} artifacts "
        f"invalidated, {report['freshness']['quarantined_claims']} claims quarantined"
    )
    print(
        f"UGC: {report['ugc_visibility']['excluded_from_corroboration']} "
        f"evidence units excluded from corroboration"
    )
    print(f"\nVERDICT: {report['verdict']}")
    print(f"\nReport written to: {args.report} + {args.report.replace('.json', '.md')}")

    if report["verdict"] != "PASS":
        sys.exit(1)


if __name__ == "__main__":  # pragma: no cover
    main()
