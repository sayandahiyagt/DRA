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
    LOCATOR_SHAPES,
    InvestigatorContext,
    WebsiteInvestigator,
)
from dra.investigators.access_policy import (
    AccessPolicyGate,
    RobotsDirective,
    RobotsPolicy,
    SourceAccessBasis,
)
from dra.investigators.website import EVIDENCE_LABELS, LADDER_STEPS, InvestigationResult
from dra.publish import async_session
from dra.routing.providers import (
    _TASK_ROUTED_MATRIX,
    BrowserProvider,
    ContentProvider,
    ProviderMode,
    SearchProvider,
    SearchProviderRegistry,
    TaskType,
    make_providers,
)
from dra.storage import FilesystemBlobStore
from tests._db import DB
from tests._evidence import ACTOR
from tests._evidence import reset as _evidence_reset

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
    """Reset the shared evidence schema + the dra#26 crawl manifest.

    Disposes the SQLAlchemy async engine's connection pool before each DB test
    so connections bound to a previous ``asyncio.run()`` event loop are not
    reused (psycopg3 locks are event-loop-affine).
    """
    from dra.db import engine

    await engine.dispose()
    await _evidence_reset()
    async with async_session() as session, session.begin():
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


def test_ladder_steps_classify_discovery_vs_evidence():
    """§38/§39: discovery rungs (snippet, raw_html, screenshot, network_har) are
    is_discovery=True; content rungs that feed reasoning are is_discovery=False."""
    discovery_names = {
        s.name for s in LADDER_STEPS if s.is_discovery
    }
    evidence_names = {
        s.name for s in LADDER_STEPS if not s.is_discovery
    }
    assert discovery_names == {
        "search_snippet", "raw_html", "screenshot", "network_har"
    }
    assert evidence_names == {
        "extracted_text", "rendered_dom", "accessibility_tree",
        "interactive_session",
    }


def test_investigation_result_separates_discovery_and_evidence_counters():
    """InvestigationResult keeps a distinct discovery_count from evidence counters."""
    r = InvestigationResult(task_type="x", query="y")
    assert r.discovery_count == 0
    assert r.evidence_unit_count == 0
    assert r.claim_count == 0
    r.discovery_count += 1
    assert r.discovery_count == 1
    assert r.evidence_unit_count == 0


# ===========================================================================
# DB-GATED TESTS (require Postgres + pgvector + migration 0011)
# ===========================================================================


@DB
def test_access_policy_gate_staged_source_identity():
    """The gate result is staged on source_identity keyed by exact canonical URL (§156)."""
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
                    "FROM source_identity WHERE kind='web' AND locator=:u"
                ),
                {"u": url},
            )
            r = row.mappings().one()
            assert r["crawl_allowed"] is True
            assert r["access_basis"] == SourceAccessBasis.PUBLIC.value
            assert r["license_spdx"] == "MIT"
            # §156: locator is the exact canonical page URL, not site origin
            assert r["locator"] == url
    asyncio.run(run())


@DB
def test_capture_evidence_claim_publish_path():
    """raw_capture -> derived_artifact -> evidence_unit -> claim publishes canonical.

    With max_step=3 the ladder runs:
      - search_snippet (discovery)  → source_candidate, NO raw_capture/evidence/claim
      - extracted_text (evidence)   → raw_capture(text) + derived + evidence_unit + claim
      - raw_html   (acquisition)     → raw_capture(html) + source_capture, NO evidence/claim
    """
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

            # 1b. No staged standalone rows remain (source_candidate flipped to
            # canonical via _STANDALONE_STATE_TABLES).
            cand_staged = await s.scalar(
                text(
                    "SELECT count(*) FROM source_candidate "
                    "WHERE bundle_id = :b AND state = 'staged'"
                ),
                {"b": str(bundle_id)},
            )
            assert cand_staged == 0

            # 2. A source_candidate was staged for the search snippet (not a
            # raw_capture) — §37 binds the snippet to its own returned_url.
            cands = await s.execute(
                text(
                    "SELECT returned_url, snippet FROM source_candidate "
                    "WHERE query = :q"
                ),
                {"q": "hello"},
            )
            cand_rows = cands.mappings().all()
            assert len(cand_rows) >= 1
            for c in cand_rows:
                assert c["returned_url"]  # bound to its own URL, not the target
                assert c["snippet"]  # snippet text preserved

            # 2b. The snippet is NOT recorded as a raw_capture (§37/§38).
            snippet_rc = await s.scalar(
                text(
                    "SELECT count(*) FROM raw_capture "
                    "WHERE metadata->>'ladder_step' = 'search_snippet'"
                )
            )
            assert snippet_rc == 0

            # 3. Raw captures: text (extracted_text) + html (raw_html).
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

            # 4. Derived artifacts link back to a raw capture (ADR-004 chain).
            der = await s.execute(
                text(
                    "SELECT da.source_capture_hash, da.content_hash, da.kind "
                    "FROM derived_artifact da "
                    "JOIN content_blob cb ON cb.hash = da.source_capture_hash "
                    "WHERE da.kind = 'normalized'"
                )
            )
            assert len(der.mappings().all()) >= 1

            # 5. Evidence units conform to the web locator shape (only from
            # evidence rungs, not acquisition/discovery rungs).
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

            # 6. Claims trace back to an evidence unit (only from evidence rungs).
            claims = await s.execute(
                text(
                    "SELECT c.evidence_unit_id FROM claim c "
                    "WHERE c.evidence_unit_id IS NOT NULL "
                    "AND EXISTS (SELECT 1 FROM evidence_unit eu "
                    "WHERE eu.id = c.evidence_unit_id)"
                )
            )
            assert len(claims.mappings().all()) >= 1

            # 7. Acquisition captures (raw_html) must NOT produce claims
            # (§38/§39: acquisition observations are not researched conclusions).
            acq_claims = await s.scalar(
                text(
                    "SELECT count(*) FROM claim "
                    "WHERE metadata->>'ladder_step' IN "
                    "('raw_html', 'screenshot', 'network_har')"
                )
            )
            assert acq_claims == 0
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

            # The source identity records crawl_allowed=False, keyed by the exact
            # canonical URL (§156), not the site origin.
            si = await s.scalar(
                text(
                    "SELECT crawl_allowed FROM source_identity "
                    "WHERE locator = :u"
                ),
                {"u": forbidden_url},
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


@DB
def test_source_identity_uses_exact_canonical_url():
    """§156: source_identity.locator is the exact canonical page URL, not origin.

    Multiple pages on one site must NOT collapse into one identity.
    """
    url_a = "https://allowed.example.com/page1"
    url_b = "https://allowed.example.com/page2"

    async def run():
        await _reset()
        inv = _make_investigator()
        async with InvestigatorContext(
            run_id="run_url", task_id="task_url", actor=_actor()
        ) as ctx:
            await inv.investigate(
                ctx,
                task_type="dom_reasoning",
                query="hello",
                target_urls=[url_a, url_b],
                max_step=3,
            )
        async with async_session() as s:
            rows = await s.execute(
                text(
                    "SELECT locator FROM source_identity "
                    "WHERE kind = 'web' ORDER BY locator"
                )
            )
            locators = [r[0] for r in rows.fetchall()]
            assert url_a in locators
            assert url_b in locators
            # The origin must NOT appear as a source_identity locator
            assert "https://allowed.example.com" not in locators
    asyncio.run(run())


@DB
def test_source_representation_records_origin_and_publisher():
    """§156: origin/publisher are metadata on source_representation, not the locator."""
    url = "https://allowed.example.com/a"

    async def run():
        await _reset()
        inv = _make_investigator()
        async with InvestigatorContext(
            run_id="run_rep", task_id="task_rep", actor=_actor()
        ) as ctx:
            await inv.investigate(
                ctx,
                task_type="dom_reasoning",
                query="hello",
                target_urls=[url],
                max_step=3,
            )
        async with async_session() as s:
            # Representations linked to a source_capture whose final_url is the
            # target URL — these carry the exact canonical page URL + origin.
            rows = await s.execute(
                text(
                    "SELECT rep.origin, rep.publisher, rep.canonical_url "
                    "FROM source_representation rep "
                    "JOIN source_capture sc ON sc.representation_id = rep.id "
                    "WHERE sc.final_url = :u"
                ),
                {"u": url},
            )
            found = rows.mappings().all()
            assert len(found) >= 1
            for r in found:
                assert r["canonical_url"] == url
                assert r["origin"] == "https://allowed.example.com"
    asyncio.run(run())


@DB
def test_search_snippet_bound_to_source_candidate():
    """§37: each search snippet is bound to its own returned_url via source_candidate,
    not to the investigated target URL path."""
    url = "https://allowed.example.com/a"

    async def run():
        await _reset()
        inv = _make_investigator()
        async with InvestigatorContext(
            run_id="run_cand", task_id="task_cand", actor=_actor()
        ) as ctx:
            await inv.investigate(
                ctx,
                task_type="dom_reasoning",
                query="hello",
                target_urls=[url],
                max_step=8,
            )
        async with async_session() as s:
            rows = await s.execute(
                text(
                    "SELECT query, provider, returned_url, snippet "
                    "FROM source_candidate"
                )
            )
            cands = rows.mappings().all()
            assert len(cands) >= 1
            for c in cands:
                assert c["query"] == "hello"
                assert c["returned_url"]  # bound to its own URL
                assert c["snippet"] is not None  # snippet text preserved

            # The candidate's returned_url must differ from the target URL
            # (the offline fake search returns https://example.org/N, not the
            # target).  This proves the snippet is not silently attached to the
            # investigated target identity (§37).
            for c in cands:
                assert c["returned_url"] != url
    asyncio.run(run())


@DB
def test_acquisition_captures_have_no_claims():
    """§38/§39: raw_html/screenshot/network_har produce source_capture + content_blob
    rows but do NOT produce EvidenceUnit→Claim rows."""
    url = "https://allowed.example.com/a"

    async def run():
        await _reset()
        inv = _make_investigator(har_authorized=True)
        async with InvestigatorContext(
            run_id="run_acq", task_id="task_acq", actor=_actor()
        ) as ctx:
            await inv.investigate(
                ctx,
                task_type="dom_reasoning",
                query="hello",
                target_urls=[url],
                max_step=8,
            )
        async with async_session() as s:
            # Each acquisition rung produces a source_capture.
            for step_name in ("raw_html", "screenshot", "network_har"):
                sc_count = await s.scalar(
                    text(
                        "SELECT count(*) FROM source_capture sc "
                        "WHERE sc.metadata->>'ladder_step' = :s"
                    ),
                    {"s": step_name},
                )
                assert sc_count >= 1, f"no source_capture for {step_name}"

            # ...and each source_capture links to a resolvable content_blob.
            blob_rows = await s.execute(
                text(
                    "SELECT cb.storage_uri FROM content_blob cb "
                    "JOIN source_capture sc ON sc.content_blob_hash = cb.hash "
                    "WHERE sc.metadata->>'ladder_step' IN "
                    "('raw_html', 'screenshot', 'network_har')"
                )
            )
            uris = [r[0] for r in blob_rows.fetchall()]
            assert len(uris) >= 3  # at least one per acquisition rung

            # No claims reference acquisition-observation ladder steps.
            acq_claims = await s.scalar(
                text(
                    "SELECT count(*) FROM claim "
                    "WHERE metadata->>'ladder_step' IN "
                    "('raw_html', 'screenshot', 'network_har')"
                )
            )
            assert acq_claims == 0
    asyncio.run(run())


@DB
def test_content_captures_produce_evidence_and_claims():
    """§38/§39: evidence rungs (extracted_text, rendered_dom, a11y_tree,
    interactive_session) still produce evidence_unit→claim, asserts_private_truth=False."""
    url = "https://allowed.example.com/a"

    async def run():
        await _reset()
        inv = _make_investigator(har_authorized=True)
        async with InvestigatorContext(
            run_id="run_ev", task_id="task_ev", actor=_actor()
        ) as ctx:
            await inv.investigate(
                ctx,
                task_type="dom_reasoning",
                query="hello",
                target_urls=[url],
                max_step=8,
            )
        async with async_session() as s:
            # Claims from evidence rungs exist and are not private-truth.
            rows = await s.execute(
                text(
                    "SELECT metadata->>'ladder_step' AS step, "
                    "metadata->>'asserts_private_truth' AS apt "
                    "FROM claim WHERE metadata ? 'ladder_step'"
                )
            )
            evidence_claims = rows.mappings().all()
            assert len(evidence_claims) >= 1
            for c in evidence_claims:
                assert c["step"] in (
                    "extracted_text", "rendered_dom",
                    "accessibility_tree", "interactive_session",
                )
                assert c["apt"] == "false"  # never source-of-truth (JSONB bool)
    asyncio.run(run())


@DB
def test_durable_content_blob_storage_uri():
    """§160/§D4: content_blob.storage_uri is non-null and resolvable via BlobStore."""
    url = "https://allowed.example.com/a"

    async def run():
        await _reset()
        inv = _make_investigator()
        store = FilesystemBlobStore()
        async with InvestigatorContext(
            run_id="run_uri", task_id="task_uri", actor=_actor()
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
                    "SELECT cb.storage_uri, cb.hash FROM content_blob cb "
                    "JOIN source_capture sc ON sc.content_blob_hash = cb.hash "
                    "WHERE sc.final_url = :u"
                ),
                {"u": url},
            )
            blobs = rows.mappings().all()
            assert len(blobs) >= 1
            for b in blobs:
                assert b["storage_uri"] is not None
                # The durable URI must resolve to the correct bytes.
                assert await store.verify(b["storage_uri"], b["hash"]) is True
    asyncio.run(run())


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
