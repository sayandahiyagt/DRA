"""§11.4 Browser/DOM investigator tests (dra#26).

Mirrors the established repo convention (see ``test_storage_proof.py`` /
``test_investigators.py``): synchronous ``def test_*()`` wrapping an
``async def run()`` driven via ``asyncio.run``; DB-gated tests reuse the
``tests/_db.py`` ``DB`` skipif so they skip cleanly without Postgres.

* Offline tests (no DB, no network) are always green and assert the pure
  contracts: evidence labels, locator shapes, and the RFC 9309 access-policy
  gate against a fixture robots map.
* DB-gated tests (@DB) drive the full ``WebsiteInvestigator`` end-to-end with
  the deterministic offline fakes, asserting the staged→canonical publish path,
  the access-policy gate staged on ``source_identity``, RFC 9309 crawl-skip
  behavior against a fixture URL set, and HAR authorization gating.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text

from dra.investigators import (
    InvestigatorContext,
    LOCATOR_SHAPES,
    WebsiteInvestigator,
)
from dra.investigators.access_policy import (
    AccessPolicyGate,
    RobotsDirective,
    RobotsPolicy,
    SourceAccessBasis,
)
from dra.investigators.website import EVIDENCE_LABELS, LADDER_STEPS
from dra.publish import async_session
from dra.routing.providers import (
    BrowserProvider,
    ContentProvider,
    ProviderMode,
    SearchProvider,
    SearchProviderRegistry,
    TaskType,
    _TASK_ROUTED_MATRIX,
    make_providers,
)
from tests._db import DB
from tests._evidence import ACTOR, reset as _evidence_reset


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_ALLOWED_ORIGIN = "https://allowed.example.com"
_FORBIDDEN_ORIGIN = "https://forbidden.example.com"


def _robots_fetcher(
    allow_origins: tuple[str, ...] = (_ALLOWED_ORIGIN,),
    forbid_origins: tuple[str, ...] = (_FORBIDDEN_ORIGIN,),
):
    """Deterministic robots.txt fetcher keyed by origin (no network)."""

    def fetcher(robots_txt_url: str) -> str | None:
        for origin in allow_origins:
            if origin in robots_txt_url:
                return (
                    "User-agent: *\n"
                    "Allow: /\n"
                    f"Sitemap: {origin}/sitemap.xml\n"
                    "Crawl-delay: 1\n"
                )
        for origin in forbid_origins:
            if origin in robots_txt_url:
                return "User-agent: *\nDisallow: /\n"
        return None

    return fetcher


def _make_investigator(
    *,
    page_content: dict[str, str] | None = None,
    fixture_results: dict[str, list[dict]] | None = None,
    har_authorized: bool = False,
    concurrency: int = 4,
    **extra: object,
) -> WebsiteInvestigator:
    """Build an offline investigator with a deterministic access-policy gate."""
    return WebsiteInvestigator(
        provider_mode=ProviderMode.OFFLINE,
        robots_fetcher=_robots_fetcher(),  # type: ignore[arg-type]
        license_resolver=lambda u: "MIT",
        page_content=page_content,
        fixture_results=fixture_results,
        har_authorized=har_authorized,
        concurrency=concurrency,
        **extra,  # type: ignore[arg-type]
    )


async def _reset() -> None:
    """Reset the shared evidence schema + the dra#26 crawl manifest."""
    await _evidence_reset()
    async with async_session() as session:
        async with session.begin():
            # PostgreSQL does not support TRUNCATE IF EXISTS; the table is
            # guaranteed present once migration 0007 is applied (DB-gated tests
            # assume a migrated schema, per tests/_db.py convention).
            exists = await session.scalar(
                text(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_name = 'web_crawl_manifest'"
                )
            )
            if exists:
                await session.execute(
                    text(
                        "TRUNCATE TABLE web_crawl_manifest "
                        "RESTART IDENTITY CASCADE"
                    )
                )


def _actor() -> dict:
    return dict(ACTOR)


# ===========================================================================
# OFFLINE / PURE TESTS (no DB, no network — always green)
# ===========================================================================


def test_locator_shapes_cover_web_browser():
    """§13.4 locator shapes cover the web and browser source kinds."""
    assert "web" in LOCATOR_SHAPES
    assert "browser" in LOCATOR_SHAPES
    assert set(LOCATOR_SHAPES["web"]) == {
        "canonical_url",
        "captured_artifact",
        "dom_locator",
        "text_locator",
    }
    assert "selector" in LOCATOR_SHAPES["browser"]
    assert "screenshot" in LOCATOR_SHAPES["browser"]
    assert "har_event" in LOCATOR_SHAPES["browser"]


def test_evidence_labels_are_the_section11_4_set():
    """The investigator only emits the distinct §11.4 evidence labels."""
    expected = {
        "direct DOM",
        "visible UI",
        "accessibility-tree",
        "network-observed",
        "inferred frontend architecture",
        "speculation",
    }
    assert set(EVIDENCE_LABELS) == expected
    # Every ladder rung carries a label from that set — never a source-of-truth
    # label like "private" or "canonical".
    for step in LADDER_STEPS:
        assert step.label in EVIDENCE_LABELS, step


def test_evidence_labels_never_include_source_of_truth():
    """No label reifies DOM/network observation as private source truth."""
    forbidden = {"private", "canonical", "source-of-truth", "ground_truth"}
    assert not (EVIDENCE_LABELS & forbidden)


def test_access_policy_gate_denies_disallowed():
    """RFC 9309 Disallow:/ yields crawl_allowed=False (result, not auth)."""
    robots = RobotsPolicy(
        fetcher=lambda url: "User-agent: *\nDisallow: /\n"
    )
    gate = AccessPolicyGate(
        robots=robots, license_resolver=lambda url: "CC-BY-4.0"
    )
    res = gate.evaluate(
        "https://forbidden.example.com/page", task_type="dom_reasoning"
    )
    assert res.crawl_allowed is False
    assert res.robots_directive == RobotsDirective.DISALLOWED
    assert res.access_basis == SourceAccessBasis.PUBLIC.value
    assert res.license_spdx == "CC-BY-4.0"


def test_access_policy_gate_allows_open():
    """An open robots / Allow:/ yields crawl_allowed=True."""
    robots = RobotsPolicy(fetcher=lambda url: "User-agent: *\nAllow: /\n")
    gate = AccessPolicyGate(robots=robots)
    res = gate.evaluate("https://allowed.example.com/page")
    assert res.crawl_allowed is True
    assert res.robots_directive == RobotsDirective.ALLOWED


def test_access_policy_gate_no_robots_txt_is_distinct():
    """A host with no robots.txt is allowed but reported distinctly."""
    robots = RobotsPolicy(fetcher=lambda url: None)
    res = robots.evaluate("https://host.example.com/page")
    assert res.directive == RobotsDirective.NO_ROBOTS_TXT
    assert res.crawl_allowed is True


def test_access_policy_gate_specificity():
    """A specific Allow beats a general Disallow (RFC 9309 longest-match)."""
    robots = RobotsPolicy(
        fetcher=lambda url: "User-agent: *\nDisallow: /\nAllow: /public/\n"
    )
    assert robots.evaluate("https://h.example.com/public/x").crawl_allowed is True
    assert robots.evaluate("https://h.example.com/private/x").crawl_allowed is False


def test_access_policy_gate_result_maps_to_source_identity_columns():
    """AccessPolicyResult fields map exactly onto source_identity staging cols."""
    robots = RobotsPolicy(fetcher=lambda url: "User-agent: *\nAllow: /\n")
    res = AccessPolicyGate(
        robots=robots, license_resolver=lambda url: "MIT"
    ).evaluate(
        "https://allowed.example.com/a",
        task_type="dom_reasoning",
        access_basis="public",
        license_spdx=None,
        auth_scope="anonymous",
        redist_allowed=True,
    )
    # §22 / ADR-015 fields recorded on source_identity.
    assert hasattr(res, "access_basis")
    assert hasattr(res, "crawl_allowed")
    assert hasattr(res, "auth_scope")
    assert hasattr(res, "license_spdx")
    assert hasattr(res, "redist_allowed")
    assert res.access_basis == "public"
    assert res.crawl_allowed is True
    assert res.auth_scope == "anonymous"
    assert res.license_spdx == "MIT"
    assert res.redist_allowed is True


def test_routing_abstraction_selects_providers_by_task():
    """SearchProviderRegistry task-routes: DOM_REASONING → ordered candidates."""
    reg = SearchProviderRegistry()
    cands = reg.select_providers(TaskType.DOM_REASONING)
    names = [c.name for c in cands]
    # Routing reflects the authoritative dra#9 task-routed matrix (consumed,
    # not mutated).  rendered_browser is always the final fall-through.
    assert names == [c.name for c in _TASK_ROUTED_MATRIX[TaskType.DOM_REASONING]]
    assert cands[-1].provider_type == "browser"
    assert cands[-1].name == "rendered_browser"
    assert reg.has_rendered_browser_fallback(TaskType.DOM_REASONING)


def test_routing_matrix_spans_all_providers():
    """The task-routed matrix covers Exa/Perplexity/Tavily/Firecrawl/browser."""
    reg = SearchProviderRegistry()
    names: set[str] = set()
    for task in TaskType:
        for c in reg.select_providers(task):
            names.add(c.name)
    for n in ("exa", "perplexity", "tavily", "firecrawl", "rendered_browser"):
        assert n in names, f"provider {n} not in task matrix"


def test_offline_investigator_uses_fakes():
    """Default offline investigator wires deterministic fakes (no SDK deps)."""
    inv = _make_investigator()
    assert inv.providers["search"].name == "fake_search"
    assert inv.providers["content"].name == "fake_content"
    assert inv.providers["browser"].name == "fake_browser"
    assert isinstance(inv.providers["browser"], BrowserProvider)
    assert isinstance(inv.providers["content"], ContentProvider)
    assert isinstance(inv.providers["search"], SearchProvider)


def test_browser_provider_protocol_has_ladder_methods():
    """FakeBrowserProvider satisfies the extended §11.4 BrowserProvider."""
    p = make_providers(ProviderMode.OFFLINE)["browser"]
    assert isinstance(p, BrowserProvider)
    for m in ("open_session", "navigate", "dom_snapshot", "screenshot",
              "close_session", "accessibility_snapshot", "interact",
              "network_capture"):
        assert callable(getattr(p, m))


# ===========================================================================
# DB-GATED TESTS (require Postgres + pgvector + migration 0007)
# ===========================================================================


@DB
def test_access_policy_gate_staged_source_identity():
    """The gate result is staged on source_identity (crawl_allowed/license)."""
    url = "https://allowed.example.com/a"

    async def run():
        await _reset()
        inv = _make_investigator()
        async with InvestigatorContext(
            run_id="run_si", task_id="task_si", actor=_actor()
        ) as ctx:
            await inv.investigate(
                ctx,
                task_type="dom_reasoning",
                query="hello",
                target_urls=[url],
                max_step=3,
            )
        assert ctx.published_count >= 1

        async with async_session() as s:
            row = await s.execute(
                text(
                    "SELECT access_basis, crawl_allowed, license_spdx, "
                    "auth_scope, redist_allowed, locator "
                    "FROM source_identity WHERE kind='web' AND locator=:o"
                ),
                {"o": "https://allowed.example.com"},
            )
            r = row.mappings().one()
            assert r["crawl_allowed"] is True
            assert r["access_basis"] == SourceAccessBasis.PUBLIC.value
            assert r["license_spdx"] == "MIT"
    asyncio.run(run())


@DB
def test_capture_evidence_claim_publish_path():
    """raw_capture -> derived_artifact -> evidence_unit -> claim publishes canonical."""
    url = "https://allowed.example.com/a"

    async def run():
        await _reset()
        inv = _make_investigator()
        async with InvestigatorContext(
            run_id="run_publish", task_id="task_publish", actor=_actor()
        ) as ctx:
            await inv.investigate(
                ctx,
                task_type="dom_reasoning",
                query="hello",
                target_urls=[url],
                max_step=3,
            )
        bundle_id = ctx._bundle_id

        assert ctx.published_count >= 1
        async with async_session() as s:
            # 1. No staged prov_entities remain — all flipped to canonical.
            staged = await s.scalar(
                text(
                    "SELECT count(*) FROM prov_entity "
                    "WHERE bundle_id = :b AND state = 'staged'"
                ),
                {"b": str(bundle_id)},
            )
            assert staged == 0

            # 2. Raw captures emitted (snippet=text, fetch=html).
            raws = await s.execute(
                text(
                    "SELECT content_hash, kind FROM raw_capture "
                    "WHERE metadata->>'canonical_url' = :u"
                ),
                {"u": url},
            )
            raw_kinds = sorted(r["kind"] for r in raws.mappings().all())
            assert "text" in raw_kinds
            assert "html" in raw_kinds

            # 3. Derived artifacts link back to a raw capture (ADR-004 chain).
            der = await s.execute(
                text(
                    "SELECT da.source_capture_hash, da.content_hash, da.kind "
                    "FROM derived_artifact da "
                    "JOIN raw_capture rc ON rc.content_hash = da.source_capture_hash "
                    "WHERE da.kind = 'normalized'"
                )
            )
            assert len(der.mappings().all()) >= 1

            # 4. Evidence units conform to the web locator shape.
            evs = await s.execute(
                text(
                    "SELECT eu.locator FROM evidence_unit eu "
                    "JOIN derived_artifact da ON da.id = eu.artifact_id"
                )
            )
            rows = evs.mappings().all()
            assert len(rows) >= 1
            for r in rows:
                loc = r["locator"]
                assert loc["source_kind"] == "web"
                for field_name in LOCATOR_SHAPES["web"]:
                    assert field_name in loc

            # 5. Claims trace back to an evidence unit.
            claims = await s.execute(
                text(
                    "SELECT c.evidence_unit_id FROM claim c "
                    "WHERE c.evidence_unit_id IS NOT NULL "
                    "AND EXISTS (SELECT 1 FROM evidence_unit eu "
                    "WHERE eu.id = c.evidence_unit_id)"
                )
            )
            assert len(claims.mappings().all()) >= 1
    asyncio.run(run())


@DB
def test_rfc9309_crawl_skip_behavior():
    """RFC 9309 disallow → skip in manifest, no raw capture, crawl_allowed=False."""
    forbidden_url = "https://forbidden.example.com/x"
    allowed_url = "https://allowed.example.com/a"

    async def run():
        await _reset()
        inv = _make_investigator()
        async with InvestigatorContext(
            run_id="run_skip", task_id="task_skip", actor=_actor()
        ) as ctx:
            await inv.investigate(
                ctx,
                task_type="dom_reasoning",
                query="hello",
                target_urls=[allowed_url, forbidden_url],
                max_step=3,
            )
        async with async_session() as s:
            # The forbidden origin is recorded as skipped with an RFC 9309 reason.
            rows = await s.execute(
                text(
                    "SELECT result, reason FROM web_crawl_manifest "
                    "WHERE url = :u"
                ),
                {"u": forbidden_url},
            )
            found = rows.mappings().all()
            assert any(
                r["result"] == "skipped" and "RFC 9309" in (r["reason"] or "")
                for r in found
            )

            # No raw capture was ever produced for the forbidden URL.
            n = await s.scalar(
                text(
                    "SELECT count(*) FROM raw_capture "
                    "WHERE metadata->>'canonical_url' = :u"
                ),
                {"u": forbidden_url},
            )
            assert n == 0

            # The source identity records crawl_allowed=False.
            si = await s.scalar(
                text(
                    "SELECT crawl_allowed FROM source_identity "
                    "WHERE locator = :o"
                ),
                {"o": "https://forbidden.example.com"},
            )
            assert si is False
    asyncio.run(run())


@DB
def test_har_captured_only_when_authorized():
    """HAR (step 8) raw captures exist iff har_authorized=True."""
    url = "https://allowed.example.com/a"

    async def run(har_authorized: bool) -> int:
        await _reset()
        inv = _make_investigator(har_authorized=har_authorized)
        async with InvestigatorContext(
            run_id=f"run_har_{har_authorized}", task_id="task_har", actor=_actor()
        ) as ctx:
            await inv.investigate(
                ctx,
                task_type="dom_reasoning",
                query="hello",
                target_urls=[url],
                max_step=8,
            )
        async with async_session() as s:
            return await s.scalar(
                text(
                    "SELECT count(*) FROM raw_capture "
                    "WHERE metadata->>'ladder_step' = 'network_har'"
                )
            )

    assert asyncio.run(run(False)) == 0
    assert asyncio.run(run(True)) >= 1


@DB
def test_crawl_manifest_records_attempted_crawled():
    """Crawled allowed URLs produce attempted + crawled manifest entries."""
    url = "https://allowed.example.com/a"

    async def run():
        await _reset()
        inv = _make_investigator()
        async with InvestigatorContext(
            run_id="run_manifest", task_id="task_manifest", actor=_actor()
        ) as ctx:
            await inv.investigate(
                ctx,
                task_type="dom_reasoning",
                query="hello",
                target_urls=[url],
                max_step=3,
            )
        async with async_session() as s:
            rows = await s.execute(
                text(
                    "SELECT result, step FROM web_crawl_manifest "
                    "WHERE url = :u ORDER BY attempted_at"
                ),
                {"u": url},
            )
            manifest = rows.mappings().all()
            results = {r["result"] for r in manifest}
            assert "attempted" in results
            assert "crawled" in results
            # every crawled entry references a ladder step
            crawled_steps = {
                r["step"] for r in manifest if r["result"] == "crawled"
            }
            assert crawled_steps, f"no crawled steps recorded: {manifest}"
    asyncio.run(run())


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
