"""Tests for the PaperInvestigator (spec §11.3, §16.2, ADR-009).

Pure unit tests (no DB) assert parser mode selection, fake-parser
determinism, locator normalization for the ``paper`` shape, visual-review
comparison logic, and PDF page rendering.  These run in any environment.

DB-gated tests stage a fixture PDF capture, run the dual-parser pipeline
with ``ParserMode.OFFLINE`` (fake parsers, no GROBID server, no model
weights), and publish atomically — asserting source_capture, derived artifacts,
evidence units with paper locators, gap creation on parser disagreement,
and the §21.2 provenance lineage chain.  DB-gated tests SKIP when Postgres
is unreachable (env concern, not a code defect).
"""

from __future__ import annotations

import asyncio
import os
import json

import pytest
from sqlalchemy import text

from dra.investigators import (
    LOCATOR_SHAPES,
    content_hash,
    normalize_locator,
)
from dra.publish import async_session

from dra.investigators.paper import (
    CRITICAL_ELEMENT_TYPES,
    InvestigationResult,
    PaperInvestigator,
    PaperParser,
    PaperParsingResult,
    ParsedElement,
    PdfRenderer,
    ParserMode,
    VisualReviewResult,
    VisualReviewer,
    _elem_key,
    _text_overlap,
    FakeDoclingParser,
    FakeGrobidParser,
    make_parsers,
)
from tests._db import DB
from tests._paper_fixtures import (
    MINIMAL_PDF,
    MINIMAL_PDF_HASH,
    PAPER_LOCATOR,
    PAPER_VERSION,
)
from tests._evidence import reset


# ---------------------------------------------------------------------------
# Pure unit tests (no DB — run even without Postgres)
# ---------------------------------------------------------------------------


class TestParserMode:
    """ParserMode enum values."""

    def test_mode_values(self):
        assert ParserMode.OFFLINE.value == "offline"
        assert ParserMode.LIVE.value == "live"

    def test_mode_is_enum(self):
        assert isinstance(ParserMode.OFFLINE, ParserMode)
        assert isinstance(ParserMode.OFFLINE, str)


class TestFakeParsers:
    """Fake parsers return deterministic PaperParsingResult objects."""

    def test_fake_grobid_parse(self):
        async def run():
            parser = FakeGrobidParser()
            assert parser.name == "fake-grobid"
            assert parser.parser_kind == "grobid"
            assert parser.supports("pdf")
            assert not parser.supports("html")
            result = await parser.parse(MINIMAL_PDF)
            assert isinstance(result, PaperParsingResult)
            assert result.parser_name == "fake-grobid"
            assert result.parser_kind == "grobid"
            assert len(result.elements) == 4
            assert result.elements[1].element_type == "equation"
            assert result.elements[1].content == "x = 24"
            assert result.content_hash == content_hash(result.raw_text)
            assert result.confidence > 0
        asyncio.run(run())

    def test_fake_docling_parse(self):
        async def run():
            parser = FakeDoclingParser()
            assert parser.name == "fake-docling"
            assert parser.parser_kind == "docling"
            result = await parser.parse(MINIMAL_PDF)
            assert isinstance(result, PaperParsingResult)
            assert result.parser_name == "fake-docling"
            assert len(result.elements) == 4
            eq = next(e for e in result.elements if e.element_type == "equation")
            assert eq.content == "x = 42"  # disagrees with GROBID
            assert result.content_hash == content_hash(result.raw_text)
        asyncio.run(run())

    def test_fake_parsers_deterministic(self):
        """Same input produces same output (determinism)."""
        async def run():
            g1 = await FakeGrobidParser().parse(MINIMAL_PDF)
            g2 = await FakeGrobidParser().parse(MINIMAL_PDF)
            assert g1.content_hash == g2.content_hash
            assert g1.elements == g2.elements
            assert g1.raw_text == g2.raw_text
        asyncio.run(run())

    def test_fake_parsers_match_protocol(self):
        """Fake parsers satisfy the PaperParser Protocol."""
        assert isinstance(FakeGrobidParser(), PaperParser)
        assert isinstance(FakeDoclingParser(), PaperParser)


class TestParserSelection:
    """make_parsers dispatches by mode."""

    def test_offline_returns_fakes(self):
        grobid, docling = make_parsers(ParserMode.OFFLINE)
        assert isinstance(grobid, FakeGrobidParser)
        assert isinstance(docling, FakeDoclingParser)

    def test_live_without_grobid_url_raises(self):
        old = os.environ.pop("GROBID_URL", None)
        try:
            with pytest.raises(RuntimeError, match="GROBID_URL"):
                make_parsers(ParserMode.LIVE)
        finally:
            if old is not None:
                os.environ["GROBID_URL"] = old

    def test_invalid_mode_raises(self):
        with pytest.raises(ValueError, match="unknown ParserMode"):
            make_parsers("bogus")  # type: ignore[arg-type]


class TestNormalizePaperLocator:
    """normalize_locator for the paper shape."""

    def test_paper_locator_includes_all_fields(self):
        loc = normalize_locator("paper", {
            "version": "1", "page": 1, "section": "intro",
            "equation": "eq-1", "figure": "fig-1", "table": "tab-1",
        })
        assert loc["source_kind"] == "paper"
        for f in LOCATOR_SHAPES["paper"]:
            assert f in loc

    def test_paper_locator_omits_absent_fields(self):
        loc = normalize_locator("paper", {
            "version": "1", "page": 1, "section": "intro", "equation": "eq-1",
        })
        assert loc["source_kind"] == "paper"
        assert "figure" not in loc
        assert "table" not in loc
        assert loc["equation"] == "eq-1"

    def test_paper_locator_drops_unknowns(self):
        loc = normalize_locator("paper", {
            "page": 1, "section": "intro", "equation": "eq-1",
            "bogus": "dropped",
        })
        assert "bogus" not in loc


class TestPdfRenderer:
    """PdfRenderer produces valid PNG page images."""

    def test_render_page_produces_png(self):
        renderer = PdfRenderer()
        png = renderer.render_page(MINIMAL_PDF, 0)
        assert len(png) > 0
        assert png[:8] == b"\x89PNG\r\n\x1a\n"

    def test_render_page_is_deterministic(self):
        renderer = PdfRenderer()
        png1 = renderer.render_page(MINIMAL_PDF, 0)
        png2 = renderer.render_page(MINIMAL_PDF, 0)
        assert png1 == png2


class TestVisualReview:
    """VisualReviewer comparison logic (§16.2)."""

    def test_text_overlap_identical(self):
        assert _text_overlap("hello world", "hello world") == 1.0

    def test_text_overlap_disjoint(self):
        assert _text_overlap("abc", "xyz") == 0.0

    def test_text_overlap_partial(self):
        # "x = 24" vs "x = 42": intersection {x, =}, union {x, =, 24, 42} → 0.5
        assert _text_overlap("x = 24", "x = 42") == 0.5

    def test_text_overlap_empty(self):
        assert _text_overlap("", "") == 1.0
        assert _text_overlap("a", "") == 0.0

    def test_elem_key(self):
        elem = ParsedElement(
            element_type="equation",
            locator={"page": 1, "section": "intro", "equation": "eq-1"},
            content="x = 24",
            confidence=0.9,
        )
        key = _elem_key(elem)
        assert "equation" in key
        assert "|1|" in key
        assert "section=intro" in key
        assert "equation=eq-1" in key

    def test_compare_disagreement_triggers_gap(self):
        reviewer = VisualReviewer(confidence_threshold=0.70)
        grobid_elem = ParsedElement(
            "equation", {"page": 1, "equation": "eq-1"}, "x = 24", 0.85,
        )
        docling_elem = ParsedElement(
            "equation", {"page": 1, "equation": "eq-1"}, "x = 42", 0.75,
        )
        cmp = reviewer.compare_elements(grobid_elem, docling_elem)
        assert not cmp["parsers_agree"]
        assert cmp["needs_gap"]
        assert cmp["text_overlap"] < 0.80
        assert cmp["gap_severity"] == "critical"
        assert len(cmp["gap_description"]) > 0

    def test_compare_agreement_no_gap(self):
        reviewer = VisualReviewer(confidence_threshold=0.70)
        grobid_elem = ParsedElement(
            "table", {"page": 2, "table": "tab-1"}, "Same content", 0.92,
        )
        docling_elem = ParsedElement(
            "table", {"page": 2, "table": "tab-1"}, "Same content", 0.90,
        )
        cmp = reviewer.compare_elements(grobid_elem, docling_elem)
        assert cmp["parsers_agree"]
        assert not cmp["needs_gap"]
        assert cmp["text_overlap"] == 1.0

    def test_compare_low_confidence_triggers_gap(self):
        reviewer = VisualReviewer(confidence_threshold=0.70)
        grobid_elem = ParsedElement(
            "equation", {"page": 1, "equation": "eq-1"}, "x = 24", 0.60,
        )
        docling_elem = ParsedElement(
            "equation", {"page": 1, "equation": "eq-1"}, "x = 24", 0.90,
        )
        cmp = reviewer.compare_elements(grobid_elem, docling_elem)
        assert cmp["parsers_agree"]  # same text
        assert cmp["needs_gap"]  # but low confidence

    def test_compare_missing_parser_triggers_gap(self):
        reviewer = VisualReviewer()
        grobid_elem = ParsedElement(
            "equation", {"page": 1, "equation": "eq-1"}, "x = 24", 0.90,
        )
        cmp = reviewer.compare_elements(grobid_elem, None)
        assert not cmp["parsers_agree"]
        assert cmp["needs_gap"]
        assert cmp["docling_confidence"] == 0.0


class TestInvestigationResult:
    """InvestigationResult dataclass construction."""

    def test_dataclass_defaults(self):
        result = InvestigationResult(
            bundle_id=None,  # type: ignore[arg-type]
            published_count=0,
            source_id=None,  # type: ignore[arg-type]
            raw_capture_hash="abc",
            grobid_artifact_id=None,  # type: ignore[arg-type]
            docling_artifact_id=None,  # type: ignore[arg-type]
        )
        assert result.evidence_unit_ids == []
        assert result.gap_ids == []
        assert result.claim_ids == []
        assert result.visual_review_results == []


class TestCriticalElementTypes:
    """CRITICAL_ELEMENT_TYPES covers §16.2 critical content."""

    def test_contains_expected_types(self):
        assert "equation" in CRITICAL_ELEMENT_TYPES
        assert "table" in CRITICAL_ELEMENT_TYPES
        assert "figure" in CRITICAL_ELEMENT_TYPES

    def test_excludes_non_critical(self):
        assert "section" not in CRITICAL_ELEMENT_TYPES


# ---------------------------------------------------------------------------
# DB-gated integration tests (SKIP without Postgres)
# ---------------------------------------------------------------------------


@DB
def test_paper_investigator_stages_raw_pdf_capture():
    """Stage a fixture PDF, assert source_capture with kind='pdf' exists."""

    async def run():
        await reset()
        investigator = PaperInvestigator(mode=ParserMode.OFFLINE)
        result = await investigator.investigate(
            MINIMAL_PDF, PAPER_LOCATOR, version=PAPER_VERSION,
        )
        assert result.published_count >= 8

        async with async_session() as s:
            row = await s.execute(
                text(
                    "SELECT sc.kind FROM source_capture sc "
                    "JOIN content_blob cb ON sc.content_blob_hash = cb.hash "
                    "WHERE cb.hash = :h"
                ),
                {"h": MINIMAL_PDF_HASH},
            )
            found = [r[0] for r in row.fetchall()]
            assert "pdf" in found

    asyncio.run(run())


@DB
def test_paper_investigator_stages_dual_derived_artifacts():
    """Assert derived_artifact rows with kind='grobid_tei' and 'docling_document'."""

    async def run():
        await reset()
        investigator = PaperInvestigator(mode=ParserMode.OFFLINE)
        result = await investigator.investigate(
            MINIMAL_PDF, PAPER_LOCATOR, version=PAPER_VERSION,
        )

        async with async_session() as s:
            row = await s.execute(
                text(
                    "SELECT kind FROM derived_artifact "
                    "WHERE source_capture_hash = :h"
                ),
                {"h": MINIMAL_PDF_HASH},
            )
            kinds = sorted(r[0] for r in row.fetchall())
            assert "grobid_tei" in kinds
            assert "docling_document" in kinds

    asyncio.run(run())


@DB
def test_paper_investigator_emits_evidence_unit_with_paper_locator():
    """Assert evidence_unit.locator has source_kind='paper' and shape fields."""

    async def run():
        await reset()
        investigator = PaperInvestigator(mode=ParserMode.OFFLINE)
        result = await investigator.investigate(
            MINIMAL_PDF, PAPER_LOCATOR, version=PAPER_VERSION,
        )

        async with async_session() as s:
            row = await s.execute(
                text(
                    "SELECT eu.locator FROM evidence_unit eu "
                    "JOIN prov_entity pe ON pe.id = eu.id "
                    "WHERE pe.bundle_id = :b"
                ),
                {"b": str(result.bundle_id)},
            )
            rows = row.fetchall()
            assert len(rows) >= 1
            for r in rows:
                loc = r[0]
                if isinstance(loc, str):
                    loc = json.loads(loc)
                assert loc["source_kind"] == "paper"
                assert "version" in loc
                assert "page" in loc

    asyncio.run(run())


@DB
def test_paper_investigator_publishes_atomically():
    """Core acceptance test: full investigate(), assert atomic publication.

    Asserts:
    - published_count >= 8
    - No staged entities remain (state='canonical' for all)
    - Provenance traversal: evidence_unit -> derived_artifact -> source_capture -> source_identity
    - Paper locator shape present in evidence_unit.locator
    """

    async def run():
        await reset()
        investigator = PaperInvestigator(mode=ParserMode.OFFLINE)
        result = await investigator.investigate(
            MINIMAL_PDF, PAPER_LOCATOR, version=PAPER_VERSION,
        )
        assert result.published_count >= 8

        async with async_session() as s:
            staged = await s.scalar(
                text(
                    "SELECT count(*) FROM prov_entity "
                    "WHERE bundle_id = :b AND state = 'staged'"
                ),
                {"b": str(result.bundle_id)},
            )
            assert staged == 0

            traversal = await s.execute(
                text(
                    "SELECT si.locator, si.kind, sc.kind as raw_kind "
                    "FROM evidence_unit eu "
                    "JOIN prov_entity pe ON pe.id = eu.id "
                    "JOIN derived_artifact da ON da.id = eu.artifact_id "
                    "JOIN content_blob cb ON cb.hash = da.source_capture_hash "
                    "JOIN source_capture sc ON sc.content_blob_hash = cb.hash "
                    "JOIN source_identity si ON si.id = sc.source_identity_id "
                    "WHERE pe.bundle_id = :b"
                ),
                {"b": str(result.bundle_id)},
            )
            rows = traversal.fetchall()
            assert len(rows) >= 1
            for row in rows:
                assert row[1] == "paper"  # source_identity.kind
                assert row[2] == "pdf"  # source_capture.kind

    asyncio.run(run())


@DB
def test_paper_investigator_creates_gap_on_disagreement():
    """With FakeGrobidParser/FakeDoclingParser disagreeing on an equation:

    - A gap entity (entity_kind='gap', gap_severity='critical') exists
    - A visual_review prov_activity exists
    - The page image source_capture (kind='image') exists
    """

    async def run():
        await reset()
        investigator = PaperInvestigator(mode=ParserMode.OFFLINE)
        result = await investigator.investigate(
            MINIMAL_PDF, PAPER_LOCATOR, version=PAPER_VERSION,
        )

        async with async_session() as s:
            gap_row = await s.execute(
                text(
                    "SELECT g.severity FROM gap g "
                    "JOIN prov_entity pe ON pe.id = g.id "
                    "WHERE pe.bundle_id = :b"
                ),
                {"b": str(result.bundle_id)},
            )
            gaps = gap_row.fetchall()
            assert len(gaps) >= 1
            assert gaps[0][0] == "critical"

            activity_row = await s.execute(
                text(
                    "SELECT activity_type FROM prov_activity "
                    "WHERE bundle_id = :b AND activity_type = 'visual_review'"
                ),
                {"b": str(result.bundle_id)},
            )
            activities = activity_row.fetchall()
            assert len(activities) >= 1

            image_row = await s.execute(
                text(
                    "SELECT cb.hash FROM content_blob cb "
                    "JOIN source_capture sc ON sc.content_blob_hash = cb.hash "
                    "WHERE sc.source_identity_id IN ("
                    "  SELECT id FROM source_identity WHERE locator = :loc "
                    "  AND version = :ver"
                    ") AND sc.kind = 'image'"
                ),
                {"loc": PAPER_LOCATOR, "ver": PAPER_VERSION},
            )
            images = image_row.fetchall()
            assert len(images) >= 1

    asyncio.run(run())


@DB
def test_paper_investigator_claims_linked_to_evidence():
    """Every staged claim has a valid evidence_unit_id provenance link."""

    async def run():
        await reset()
        investigator = PaperInvestigator(mode=ParserMode.OFFLINE)
        result = await investigator.investigate(
            MINIMAL_PDF, PAPER_LOCATOR, version=PAPER_VERSION,
        )
        assert len(result.claim_ids) >= 1

        async with async_session() as s:
            row = await s.execute(
                text(
                    "SELECT c.id, eu.id FROM claim c "
                    "JOIN evidence_unit eu ON eu.id = c.evidence_unit_id "
                    "JOIN prov_entity pe ON pe.id = c.id "
                    "WHERE pe.bundle_id = :b"
                ),
                {"b": str(result.bundle_id)},
            )
            found = row.fetchall()
            assert len(found) == len(result.claim_ids)

    asyncio.run(run())
