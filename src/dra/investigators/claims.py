"""Behavioral claim helpers for evidence grading (spec §13.5 / §15.4).

Only *behavioral* claims — assertions about runtime/test behavior — carry the
``INFERENCE`` vs ``EXECUTION_VERIFIED`` evidence-status label.  Structural
observations extracted from source (modules/classes/functions, imports, line
spans) are NOT claims: they are ``implementation_entity`` + ``evidence_unit``
rows stamped ``DIRECT_CODE_OBSERVATION`` (spec §13.5 — ``DIRECT_CODE_OBSERVATION``
is distinct from ``INFERENCE``).

The helpers here decide which label a behavioral claim receives based on
whether the :class:`~dra.sandbox.Sandbox` actually executed the repo's test
suite (and the targeted test passed).  When no execution runtime is available
the sandbox degrades to static inspection (spec §8.3) and every behavioral
claim is stamped ``INFERENCE`` rather than hard-failing the investigation.
"""

from __future__ import annotations

import os
from typing import Any

from dra.sandbox import Sandbox, SandboxCapability

#: Evidence-status stamp for a behavioral claim confirmed by running tests in
#: the sandbox (spec §13.5).
EXECUTION_VERIFIED = "EXECUTION_VERIFIED"
#: Evidence-status stamp for a behavioral claim supported only by static
#: inspection — never a hard failure (spec §15.4 / §8.3).
INFERENCE = "INFERENCE"
#: Evidence-status stamp for direct structural observations of source code
#: (spec §13.5 — distinct from INFERENCE).
DIRECT_CODE_OBSERVATION = "DIRECT_CODE_OBSERVATION"

#: Spec section referenced by behavioral claims staged through this module.
SPEC_SECTION = "§15.4"


def detect_capability() -> SandboxCapability:
    """Return the sandbox capability the investigator should use.

    Honors the ``DRA_SANDBOX_CAPABILITY`` override (``static_only`` or
    ``docker``) so tests can drive the static-fallback path deterministically;
    otherwise delegates to :meth:`Sandbox.detect`.
    """
    forced = os.environ.get("DRA_SANDBOX_CAPABILITY")
    if forced == "static_only":
        return SandboxCapability.STATIC_ONLY
    if forced == "docker":
        return SandboxCapability.DOCKER
    return Sandbox.detect()


def behavioral_evidence_status(
    ctx: Any, sandbox: Sandbox, sandbox_ran_test: bool
) -> str:
    """Classify the evidence status of a behavioral claim.

    Returns ``EXECUTION_VERIFIED`` when the sandbox actually executed the test
    suite and the targeted test passed (``sandbox_ran_test`` is ``True``);
    otherwise ``INFERENCE`` (static-only inspection, a failed test execution,
    or no test target — spec §8.3 + §15.4).  ``ctx`` is accepted to mirror the
    plan's contract surface; the decision is pure and does not touch the DB.
    """
    del ctx  # contract parameter; status is a pure function of sandbox outcome
    if sandbox.capability is SandboxCapability.STATIC_ONLY:
        return INFERENCE
    if not sandbox_ran_test:
        return INFERENCE
    return EXECUTION_VERIFIED


async def stage_behavioral_claim(
    ctx: Any,
    sandbox: Sandbox,
    artifact_id: Any,
    claim_text: str,
    *,
    sandbox_ran_test: bool = False,
    confidence: float | None = None,
    command: list[str] | None = None,
    output_hash: str | None = None,
) -> tuple[str, Any]:
    """Stage a behavioral claim + its backing evidence unit.

    Emits an ``execution``-locator evidence unit (the test-run result) tethered
    to an existing derived ``artifact_id`` and a claim referencing that evidence
    unit, both stamped with the evidence status computed by
    :func:`behavioral_evidence_status`.  When the sandbox is static-only the
    bundle still publishes — the investigator degrades to ``INFERENCE`` rather
    than hard-failing (spec §8.3).  Returns ``(status, evidence_unit_id)``.

    ``artifact_id`` must reference a staged ``derived_artifact`` (publish
    validation rejects evidence_units whose ``artifact_id`` is null or absent,
    so the caller stages an execution-output derived_artifact first).
    """
    status = behavioral_evidence_status(ctx, sandbox, sandbox_ran_test)
    evidence_unit_id = await ctx.stage_evidence_unit(
        artifact_id,
        {
            "source_kind": "execution",
            "environment_manifest": {},
            "command": command or [],
            "output_hash": output_hash or "",
        },
        metadata={"evidence_status": status, "spec_section": SPEC_SECTION},
    )
    await ctx.stage_claim(
        claim_text,
        evidence_unit_id=evidence_unit_id,
        confidence=confidence,
        metadata={"evidence_status": status, "spec_section": SPEC_SECTION},
    )
    return status, evidence_unit_id
