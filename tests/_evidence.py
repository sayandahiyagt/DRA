"""Shared fixture builder for evidence-graph tests.

Builds a canonical §21.2 lineage bundle (source_identity -> raw capture ->
derived artifact -> evidence unit -> claim -> decision -> handoff) with full
W3C-PROV generation + derivation edges, all staged inside one bundle.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text

from dra.publish import (
    add_prov_edge,
    async_session,
    create_activity,
    stage_bundle,
    stage_claim,
    stage_decision,
    stage_derived_artifact,
    stage_evidence_unit,
    stage_handoff,
    stage_implementation_entity,
    stage_raw_capture,
    stage_source_identity,
)

RAW_HASH = "a" * 64
DERIVED_HASH = "b" * 64
EV_HASH = "c" * 64
IMPL_HASH = "d" * 64  # implementation_entity content_hash

ACTOR = {"kind": "model", "name": "gpt", "version": "1.0", "external_id": "gpt-1.0"}


async def reset() -> None:
    async with async_session() as session:
        async with session.begin():
            await session.execute(
                text(
                    "TRUNCATE TABLE handoff_statement, gap, decision, "
                    "implementation_entity, claim, topic_relationship, topic, "
                    "evidence_unit, derived_artifact, raw_capture, source_identity, "
                    "prov_derivation, prov_generation, prov_entity, prov_activity, "
                    "prov_bundle, prov_agent RESTART IDENTITY CASCADE"
                )
            )


async def build_lineage_bundle(
    run_id: str = "run_lin",
    task_id: str = "task_lin",
    include_handoff: bool = True,
    broken_claim: bool = False,
    bad_hash: str | None = None,
) -> tuple[uuid.UUID, dict]:
    """Stage a full §21.2 lineage bundle.

    Returns ``(bundle_id, ids)`` where ``ids`` maps each domain prov_entity id
    to a human label, so traversal tests can anchor on a known node.
    """
    async with async_session() as session:
        async with session.begin():
            bundle_id = await stage_bundle(
                run_id, task_id, "lineage", ACTOR,
            )
            source_id = await stage_source_identity(
                session, bundle_id, None, "repo", "https://example.com/src",
                state="staged", license_spdx="MIT", access_basis="public",
                crawl_allowed=True, redist_allowed=True,
            )
            acq = await create_activity(session, bundle_id, "acquisition", ACTOR)
            raw_eid = await stage_raw_capture(
                session, bundle_id, acq, RAW_HASH, source_id,
                kind="repo_snapshot", mime_type="text/plain", stored_at="/store/raw",
            )
            await add_prov_edge(session, generated_entity_id=raw_eid, activity_id=acq)

            parse = await create_activity(session, bundle_id, "parsing", ACTOR)
            da_eid = await stage_derived_artifact(
                session, bundle_id, parse, RAW_HASH, bad_hash or DERIVED_HASH,
                kind="parsed", version=1,
            )
            await add_prov_edge(
                session, deriving_entity_id=da_eid, source_entity_id=raw_eid, activity_id=parse,
            )

            ev_eid = await stage_evidence_unit(
                session, bundle_id, parse, da_eid,
                locator={"file": "x.md", "range": [0, 10]}, content_hash=EV_HASH,
            )
            await add_prov_edge(
                session, deriving_entity_id=ev_eid, source_entity_id=da_eid, activity_id=parse,
            )

            ev_id_arg = None if broken_claim else ev_eid
            claim_eid = await stage_claim(
                session, bundle_id, parse, "claims something",
                evidence_unit_id=ev_id_arg,
            )
            await add_prov_edge(
                session, deriving_entity_id=claim_eid, source_entity_id=ev_eid, activity_id=parse,
            )

            ids = {
                "source_id": source_id,
                "raw": raw_eid,
                "derived": da_eid,
                "evidence": ev_eid,
                "claim": claim_eid,
                "acq": acq,
                "parse": parse,
            }

            if include_handoff:
                dec_eid = await stage_decision(
                    session, bundle_id, parse, claim_eid, "Decide X", run_id=run_id,
                )
                await add_prov_edge(
                    session, deriving_entity_id=dec_eid, source_entity_id=claim_eid,
                    activity_id=parse,
                )
                hs_eid = await stage_handoff(
                    session, bundle_id, parse, dec_eid, {"claim_id": str(claim_eid)},
                    run_id=run_id, content="handoff body",
                )
                await add_prov_edge(
                    session, deriving_entity_id=hs_eid, source_entity_id=dec_eid,
                    activity_id=parse,
                )
                ids.update({"decision": dec_eid, "handoff": hs_eid})

    return bundle_id, ids


async def build_implementation_bundle(
    run_id: str = "run_impl",
    task_id: str = "task_impl",
) -> tuple[uuid.UUID, dict]:
    """Stage a minimal dra#23 emission bundle.

    Stages a repo source identity + raw capture + derived artifact + evidence
    unit + implementation entity in a single bundle, recording the acquisition
    + parsing ``prov_activity`` rows and the W3C-PROV generation / derivation
    edges.  The implementation entity links back to the repo source identity
    via ``repo_source_id``, exercising the dra#23 locator/source contract.

    Returns ``(bundle_id, ids)`` where ``ids`` maps each prov_entity id to a
    label for traversal assertions.
    """
    async with async_session() as session:
        async with session.begin():
            bundle_id = await stage_bundle(
                run_id, task_id, "implementation", ACTOR,
            )
            source_id = await stage_source_identity(
                session, bundle_id, None, "repo", "https://example.com/src",
                state="staged", license_spdx="MIT", access_basis="public",
                crawl_allowed=True, redist_allowed=True,
            )
            acq = await create_activity(session, bundle_id, "acquisition", ACTOR)
            raw_eid = await stage_raw_capture(
                session, bundle_id, acq, RAW_HASH, source_id,
                kind="repo_snapshot", mime_type="text/plain", stored_at="/store/raw",
            )
            await add_prov_edge(session, generated_entity_id=raw_eid, activity_id=acq)

            parse = await create_activity(session, bundle_id, "parsing", ACTOR)
            da_eid = await stage_derived_artifact(
                session, bundle_id, parse, RAW_HASH, DERIVED_HASH,
                kind="parsed", version=1,
            )
            await add_prov_edge(
                session, deriving_entity_id=da_eid, source_entity_id=raw_eid, activity_id=parse,
            )

            ev_eid = await stage_evidence_unit(
                session, bundle_id, parse, da_eid,
                locator={"file": "x.md", "range": [0, 10]}, content_hash=EV_HASH,
            )
            await add_prov_edge(
                session, deriving_entity_id=ev_eid, source_entity_id=da_eid, activity_id=parse,
            )

            impl_eid = await stage_implementation_entity(
                session, bundle_id, parse, source_id,
                kind="symbol", path="src/main.py", symbol_name="foo",
                commit_sha="abc123", line_start=10, line_end=20,
                content_hash=IMPL_HASH,
            )
            await add_prov_edge(
                session, generated_entity_id=impl_eid, activity_id=parse,
            )

            return bundle_id, {
                "source_id": source_id,
                "raw": raw_eid,
                "derived": da_eid,
                "evidence": ev_eid,
                "implementation_entity": impl_eid,
                "acq": acq,
                "parse": parse,
            }
