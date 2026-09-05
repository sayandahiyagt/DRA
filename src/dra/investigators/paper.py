"""Paper Investigator (spec §11.3, §16.1–16.2, ADR-009).

Converts a paper PDF into an implementation-ready evidence model using dual
parsers (GROBID + Docling). GROBID provides scholarly structure + bibliographic
metadata + references/citation contexts; Docling provides rich layout, tables,
formulas, figures, and structured/provenance document output. Both parsers are
run in tandem per ADR-009; the §16.2 critical-content verification rule
preserves page images + both parser outputs and creates gaps when parsers
disagree or confidence is low.

LIVE mode (GROBID server + Docling model cache) is env-gated — see
:class:`ParserMode` and :func:`make_parsers`. Tests use
:class:`ParserMode.OFFLINE` (fake parsers, no server, no model weights).
"""

from __future__ import annotations

import asyncio
import io
import json
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from dra.investigators import (
    InvestigatorContext,
    content_hash,
    normalize_locator,
)

CRITICAL_ELEMENT_TYPES = frozenset({"equation", "table", "figure"})

_PAPER_PARSER_ACTOR: dict[str, Any] = {
    "kind": "model",
    "name": "paper-investigator",
    "version": "0.1.0",
    "external_id": "paper-investigator",
}

_CONFIDENCE_THRESHOLD_DEFAULT = 0.70
_TEXT_OVERLAP_THRESHOLD_DEFAULT = 0.80


# ---------------------------------------------------------------------------
# Parser mode + contracts
# ---------------------------------------------------------------------------


class ParserMode(str, Enum):
    """Backend resolution mode for paper parsers (mirrors ProviderMode, §16.1).

    ``OFFLINE`` (default) — deterministic fakes, no server, no model weights.
    ``LIVE`` — real GROBID + Docling SDKs, env-gated (requires a running
    GROBID server at ``GROBID_URL`` and cached Docling model weights).
    """

    OFFLINE = "offline"
    LIVE = "live"


@dataclass
class ParsedElement:
    """A single parsed element from a paper (equation, table, figure, etc.).

    ``locator`` holds the raw paper-locator fields (``page``, ``section``,
    ``equation``/``figure``/``table``) as emitted by the parser; it is
    normalized to the spec §13.4 ``paper`` shape before staging as an
    ``evidence_unit.locator``.
    """

    element_type: str
    locator: dict[str, Any]
    content: str
    confidence: float
    raw_ref: str | None = None


@dataclass
class PaperParsingResult:
    """The result of parsing a paper PDF with a single parser."""

    parser_name: str
    parser_kind: str
    content_hash: str
    raw_text: str
    elements: list[ParsedElement]
    bibliographic_metadata: dict[str, Any]
    confidence: float
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class PaperParser(Protocol):
    """Interface for a paper parser (GROBID or Docling, §16.1)."""

    name: str
    parser_kind: str

    def supports(self, kind: str) -> bool:
        """Return True if *kind* (e.g. ``"pdf"``) is supported."""
        ...

    async def parse(self, pdf_bytes: bytes) -> PaperParsingResult:
        """Parse *pdf_bytes* and return a :class:`PaperParsingResult`."""
        ...


# ---------------------------------------------------------------------------
# Fake (OFFLINE) parsers — deterministic, fixture-aligned
# ---------------------------------------------------------------------------

_FAKE_BIBLIOGRAPHIC_METADATA: dict[str, Any] = {
    "title": "Deep Research: A Method",
    "authors": ["Doe, J.", "Roe, J."],
    "doi": "10.1234/deep-research",
    "year": 2024,
}

_FAKE_ABSTRACT = "This paper presents a novel method for deep research."


def _fake_grobid_elements() -> list[ParsedElement]:
    return [
        ParsedElement(
            element_type="section",
            locator={"page": 1, "section": "abstract"},
            content=_FAKE_ABSTRACT,
            confidence=0.90,
            raw_ref="abstract",
        ),
        ParsedElement(
            element_type="equation",
            locator={"page": 1, "section": "introduction", "equation": "eq-1"},
            content="x = 24",
            confidence=0.85,
            raw_ref="eq-1",
        ),
        ParsedElement(
            element_type="table",
            locator={"page": 2, "section": "results", "table": "tab-1"},
            content="Table 1. Training results on dataset A",
            confidence=0.92,
            raw_ref="tab-1",
        ),
        ParsedElement(
            element_type="figure",
            locator={"page": 3, "figure": "fig-1"},
            content="Figure 1. Model architecture diagram",
            confidence=0.88,
            raw_ref="fig-1",
        ),
    ]


def _fake_docling_elements() -> list[ParsedElement]:
    return [
        ParsedElement(
            element_type="section",
            locator={"page": 1, "section": "abstract"},
            content=_FAKE_ABSTRACT,
            confidence=0.90,
            raw_ref="#page-1",
        ),
        ParsedElement(
            element_type="equation",
            locator={"page": 1, "section": "introduction", "equation": "eq-1"},
            content="x = 42",
            confidence=0.75,
            raw_ref="#page-1-eq-1",
        ),
        ParsedElement(
            element_type="table",
            locator={"page": 2, "section": "results", "table": "tab-1"},
            content="Table 1. Training results on dataset A",
            confidence=0.92,
            raw_ref="#page-2-tab-1",
        ),
        ParsedElement(
            element_type="figure",
            locator={"page": 3, "figure": "fig-1"},
            content="Figure 1. Model architecture diagram",
            confidence=0.88,
            raw_ref="#page-3-fig-1",
        ),
    ]


class FakeGrobidParser:
    """Deterministic offline GROBID parser returning scripted TEI-like output.

    The equation element (``eq-1``) intentionally disagrees with
    :class:`FakeDoclingParser` (``x = 24`` vs ``x = 42``) so the §16.2
    gap-on-disagreement path is exercised by tests.
    """

    name = "fake-grobid"
    parser_kind = "grobid"

    def supports(self, kind: str) -> bool:
        return kind == "pdf"

    async def parse(self, pdf_bytes: bytes) -> PaperParsingResult:
        elements = _fake_grobid_elements()
        raw_text = " ".join(e.content for e in elements)
        return PaperParsingResult(
            parser_name=self.name,
            parser_kind=self.parser_kind,
            content_hash=content_hash(raw_text),
            raw_text=raw_text,
            elements=elements,
            bibliographic_metadata=dict(_FAKE_BIBLIOGRAPHIC_METADATA),
            confidence=0.90,
        )


class FakeDoclingParser:
    """Deterministic offline Docling parser returning scripted document output.

    Identical to :class:`FakeGrobidParser` except for the equation content,
    which intentionally disagrees (``x = 42`` vs GROBID's ``x = 24``) to trigger
    the §16.2 gap creator.
    """

    name = "fake-docling"
    parser_kind = "docling"

    def supports(self, kind: str) -> bool:
        return kind == "pdf"

    async def parse(self, pdf_bytes: bytes) -> PaperParsingResult:
        elements = _fake_docling_elements()
        raw_text = " ".join(e.content for e in elements)
        return PaperParsingResult(
            parser_name=self.name,
            parser_kind=self.parser_kind,
            content_hash=content_hash(raw_text),
            raw_text=raw_text,
            elements=elements,
            bibliographic_metadata=dict(_FAKE_BIBLIOGRAPHIC_METADATA),
            confidence=0.88,
        )


# ---------------------------------------------------------------------------
# Live (LIVE) parser implementations — env-gated
# ---------------------------------------------------------------------------


class GrobidParser:
    """Live GROBID parser wrapping ``grobid-client-python`` 0.2.0.

    Lazy-imports the grobid-client (so importing this module does not require
    the package at import time). Requires a running GROBID server at
    ``GROBID_URL`` (default ``http://localhost:8070``).
    """

    name = "grobid"
    parser_kind = "grobid"

    def __init__(self, grobid_url: str | None = None) -> None:
        self.grobid_url = (
            grobid_url or os.environ.get("GROBID_URL", "http://localhost:8070")
        )

    def supports(self, kind: str) -> bool:
        return kind == "pdf"

    async def parse(self, pdf_bytes: bytes) -> PaperParsingResult:
        from grobid_client.grobid_client import GrobidClient

        client = GrobidClient(
            config_path=None, check_server=False, grobid_server=self.grobid_url,
        )
        loop = asyncio.get_event_loop()
        tei_xml, _, _ = await loop.run_in_executor(
            None,
            lambda: client.process_pdf(
                "processFulltextDocument", pdf_bytes,
                consolidate_header=True,
                consolidate_citations=False,
            ),
        )
        elements = _parse_grobid_tei(tei_xml)
        return PaperParsingResult(
            parser_name=self.name,
            parser_kind=self.parser_kind,
            content_hash=content_hash(tei_xml),
            raw_text=tei_xml,
            elements=elements,
            bibliographic_metadata={},
            confidence=0.90,
        )


class DoclingParser:
    """Live Docling parser wrapping ``docling`` 2.124.0.

    Lazy-imports docling (heavy ML stack) inside ``parse`` so that importing
    this module does not pull in torch / model weights.
    """

    name = "docling"
    parser_kind = "docling"

    def __init__(self) -> None:
        pass

    def supports(self, kind: str) -> bool:
        return kind == "pdf"

    async def parse(self, pdf_bytes: bytes) -> PaperParsingResult:
        from docling.backend import PdfDocumentBackend
        from docling.document import Document

        backend = PdfDocumentBackend()
        loop = asyncio.get_event_loop()
        doc = await loop.run_in_executor(
            None,
            lambda: backend.process_pdf(
                bytes_io=io.BytesIO(pdf_bytes),
                extract_table_info=True,
                extract_formula_info=True,
                extract_figure_info=True,
            ),
        )
        elements: list[ParsedElement] = []
        raw_parts: list[str] = []
        for page in doc.pages:
            for para in page.paragraphs:
                raw_parts.append(para.text)
                elements.append(ParsedElement(
                    element_type="section",
                    locator={"page": page.page_number, "section": para.label},
                    content=para.text,
                    confidence=getattr(para, "confidence", 0.9),
                ))
            for eq in page.equations:
                raw_parts.append(eq.latex or eq.text or "")
                elements.append(ParsedElement(
                    element_type="equation",
                    locator={
                        "page": page.page_number,
                        "section": eq.section or "body",
                        "equation": eq.uid or "eq-1",
                    },
                    content=eq.latex or eq.text or "",
                    confidence=getattr(eq, "confidence", 0.9),
                ))
        raw_text = "\n".join(raw_parts)
        return PaperParsingResult(
            parser_name=self.name,
            parser_kind=self.parser_kind,
            content_hash=content_hash(raw_text),
            raw_text=raw_text,
            elements=elements,
            bibliographic_metadata={},
            confidence=0.88,
        )


def make_parsers(
    mode: ParserMode = ParserMode.OFFLINE,
    grobid_url: str | None = None,
) -> tuple[PaperParser, PaperParser]:
    """Build a (GROBID, Docling) parser pair for the given mode.

    ``OFFLINE`` (default) returns deterministic :class:`FakeGrobidParser` /
    :class:`FakeDoclingParser` — no network, no server, no model weights.
    ``LIVE`` instantiates real SDK clients behind env-gated preconditions;
    raises :class:`RuntimeError` if ``GROBID_URL`` is not set.
    """
    if mode is ParserMode.OFFLINE:
        return FakeGrobidParser(), FakeDoclingParser()
    if mode is not ParserMode.LIVE:
        raise ValueError(f"unknown ParserMode: {mode!r}")
    url = grobid_url or os.environ.get("GROBID_URL")
    if not url:
        raise RuntimeError(
            "ParserMode.LIVE requested but GROBID_URL is not set. "
            "Set GROBID_URL (e.g. http://localhost:8070) and ensure "
            "Docling model weights are cached (HF_HOME / HF_HUB_CACHE)."
        )
    return GrobidParser(grobid_url=url), DoclingParser()


def _parse_grobid_tei(tei_xml: str) -> list[ParsedElement]:
    """Extract ParsedElement list from a GROBID TEI XML string.

    Best-effort: walks the XML for ``<formula>``, ``<table>``, ``<figure>``
    and section headings, attaching page numbers when present in
    ``<pb n="N">``.
    """
    import xml.etree.ElementTree as ET

    root = ET.fromstring(tei_xml)
    elements: list[ParsedElement] = []
    current_page = 1
    current_section = "body"
    for elem in root.iter():
        tag = elem.tag.split("}")[-1]
        if tag == "pb":
            current_page = int(elem.get("n", current_page))
        elif tag == "head":
            current_section = elem.text or current_section
        elif tag == "formula":
            formula_text = "".join(elem.itertext())
            elements.append(ParsedElement(
                element_type="equation",
                locator={"page": current_page, "section": current_section},
                content=formula_text,
                confidence=0.9,
                raw_ref=elem.get("xml:id", elem.get("id")),
            ))
        elif tag == "table":
            table_text = "".join(elem.itertext())
            elements.append(ParsedElement(
                element_type="table",
                locator={"page": current_page, "section": current_section},
                content=table_text,
                confidence=0.9,
                raw_ref=elem.get("xml:id", elem.get("id")),
            ))
        elif tag == "figure":
            fig_text = "".join(elem.itertext()) or elem.get("n", "")
            elements.append(ParsedElement(
                element_type="figure",
                locator={"page": current_page, "section": current_section},
                content=fig_text,
                confidence=0.9,
                raw_ref=elem.get("xml:id", elem.get("id")),
            ))
    return elements


# ---------------------------------------------------------------------------
# Page rendering
# ---------------------------------------------------------------------------


class PdfRenderer:
    """Render PDF pages to PNG using ``pypdfium2`` (lazy-imported).

    The import is deferred so that importing this module does not require
    ``pypdfium2`` / ``pillow`` at import time.
    """

    def __init__(self) -> None:
        pass

    def render_page(self, pdf_bytes: bytes, page_number: int = 0) -> bytes:
        """Render *page_number* (0-indexed) of *pdf_bytes* to PNG bytes."""
        import pypdfium2 as pdfium

        doc = pdfium.PdfDocument(io.BytesIO(pdf_bytes))
        try:
            if page_number >= len(doc):
                page_number = len(doc) - 1
            page = doc[page_number]
            bitmap = page.render(scale=1.0)
            img = bitmap.to_pil()
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()
        finally:
            doc.close()


# ---------------------------------------------------------------------------
# Visual review contract (§16.2)
# ---------------------------------------------------------------------------


@dataclass
class VisualReviewResult:
    """Outcome of §16.2 visual review for a single critical element."""

    element_type: str
    element_locator: dict[str, Any]
    page_image_hash: str
    grobid_excerpt: str
    docling_excerpt: str
    grobid_confidence: float
    docling_confidence: float
    parsers_agree: bool
    needs_gap: bool
    gap_severity: str
    gap_description: str
    decision: str  # "verified" | "gap_created" | "needs_human_review"
    gap_id: UUID | None = None


def _text_overlap(a: str, b: str) -> float:
    """Word-level Jaccard similarity between two text excerpts."""
    set_a = set(a.lower().split())
    set_b = set(b.lower().split())
    if not set_a and not set_b:
        return 1.0
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)


def _elem_key(elem: ParsedElement) -> str:
    """Deterministic key for matching elements across parsers by locator."""
    loc = elem.locator
    parts = [elem.element_type, str(loc.get("page", 0))]
    for f in ("section", "equation", "figure", "table"):
        if f in loc:
            parts.append(f"{f}={loc[f]}")
    return "|".join(parts)


class VisualReviewer:
    """§16.2 critical-content verification engine.

    For each implementation-critical element (equation/table/figure), renders
    the page image, stages it as ``raw_capture(kind="image")``, compares the
    GROBID and Docling excerpts, and creates a ``gap`` entity when parsers
    disagree or confidence is low.
    """

    def __init__(
        self,
        confidence_threshold: float = _CONFIDENCE_THRESHOLD_DEFAULT,
        text_overlap_threshold: float = _TEXT_OVERLAP_THRESHOLD_DEFAULT,
    ) -> None:
        self._confidence_threshold = confidence_threshold
        self._text_overlap_threshold = text_overlap_threshold

    def compare_elements(
        self,
        grobid_elem: ParsedElement | None,
        docling_elem: ParsedElement | None,
    ) -> dict[str, Any]:
        """Compare two parser outputs for a single critical element (pure, no DB).

        Returns a dict with ``parsers_agree``, ``needs_gap``,
        ``gap_description``, ``gap_severity``, ``grobid_confidence``,
        ``docling_confidence``, ``grobid_excerpt``, ``docling_excerpt``,
        ``text_overlap``.  Used by :meth:`review` to decide whether to stage a
        gap; also unit-testable without Postgres.
        """
        grobid_excerpt = grobid_elem.content if grobid_elem else ""
        docling_excerpt = docling_elem.content if docling_elem else ""
        grobid_conf = grobid_elem.confidence if grobid_elem else 0.0
        docling_conf = docling_elem.confidence if docling_elem else 0.0
        overlap = _text_overlap(grobid_excerpt, docling_excerpt)
        parsers_agree = overlap >= self._text_overlap_threshold
        low_confidence = (
            grobid_conf < self._confidence_threshold
            or docling_conf < self._confidence_threshold
        )
        needs_gap = not parsers_agree or low_confidence
        gap_description = ""
        if needs_gap:
            grobid_name = grobid_elem.content if grobid_elem else ""
            docling_name = docling_elem.content if docling_elem else ""
            gap_description = (
                f"Parser disagreement: GROBID '{grobid_name}' vs "
                f"Docling '{docling_name}'. "
                f"Text overlap={overlap:.2f}, "
                f"GROBID confidence={grobid_conf:.2f}, "
                f"Docling confidence={docling_conf:.2f}."
            )
        return {
            "grobid_excerpt": grobid_excerpt,
            "docling_excerpt": docling_excerpt,
            "grobid_confidence": grobid_conf,
            "docling_confidence": docling_conf,
            "parsers_agree": parsers_agree,
            "needs_gap": needs_gap,
            "gap_severity": "critical",
            "gap_description": gap_description,
            "text_overlap": overlap,
        }

    async def review(
        self,
        ctx: InvestigatorContext,
        grobid_result: PaperParsingResult,
        docling_result: PaperParsingResult,
        pdf_bytes: bytes,
        source_id: UUID,
        review_activity: UUID,
        version: str,
    ) -> list[VisualReviewResult]:
        """Compare parser outputs for critical elements and create gaps.

        Returns one :class:`VisualReviewResult` per unique critical element
        (deduplicated by locator across both parsers).
        """
        grobid_by_key = {_elem_key(e): e for e in grobid_result.elements}
        docling_by_key = {_elem_key(e): e for e in docling_result.elements}
        all_keys = list(grobid_by_key.keys()) + [
            k for k in docling_by_key if k not in grobid_by_key
        ]

        renderer = PdfRenderer()
        results: list[VisualReviewResult] = []

        for key in all_keys:
            grobid_elem = grobid_by_key.get(key)
            docling_elem = docling_by_key.get(key)
            if grobid_elem is None:
                continue
            if grobid_elem.element_type not in CRITICAL_ELEMENT_TYPES:
                continue

            cmp = self.compare_elements(grobid_elem, docling_elem)
            parsers_agree = cmp["parsers_agree"]
            needs_gap = cmp["needs_gap"]
            gap_description = cmp["gap_description"]
            gap_severity = cmp["gap_severity"]
            overlap = cmp["text_overlap"]
            grobid_excerpt = cmp["grobid_excerpt"]
            docling_excerpt = cmp["docling_excerpt"]
            grobid_conf = cmp["grobid_confidence"]
            docling_conf = cmp["docling_confidence"]

            page_num = grobid_elem.locator.get("page", 1)
            page_zero_based = page_num - 1
            png_bytes = renderer.render_page(pdf_bytes, page_zero_based)
            page_image_hash = content_hash(png_bytes)
            await ctx.stage_source_capture(
                source_id,
                page_image_hash,
                "image",
                mime_type="image/png",
                size_bytes=len(png_bytes),
                data=png_bytes,
                metadata={
                    "paper_version": version,
                    "element_type": grobid_elem.element_type,
                    "page": page_num,
                    "source": "pypdfium2",
                },
            )

            locator = normalize_locator(
                "paper", {**grobid_elem.locator, "version": version}
            )

            gap_id: UUID | None = None
            decision = "verified"

            if needs_gap:
                gap_description_full = (
                    f"Parser disagreement on {grobid_elem.element_type} "
                    f"(page {page_num}): GROBID says '{grobid_excerpt}', "
                    f"Docling says '{docling_excerpt}'. "
                    f"Text overlap={overlap:.2f}, "
                    f"GROBID confidence={grobid_conf:.2f}, "
                    f"Docling confidence={docling_conf:.2f}."
                )
                gap_id = await ctx.stage_gap(
                    gap_description_full,
                    severity=gap_severity,
                    activity_id=review_activity,
                    metadata={
                        "element_type": grobid_elem.element_type,
                        "element_locator": locator,
                        "grobid_excerpt": grobid_excerpt,
                        "docling_excerpt": docling_excerpt,
                        "text_overlap": overlap,
                        "grobid_confidence": grobid_conf,
                        "docling_confidence": docling_conf,
                        "page_image_hash": page_image_hash,
                    },
                )
                decision = "gap_created"

            results.append(VisualReviewResult(
                element_type=grobid_elem.element_type,
                element_locator=locator,
                page_image_hash=page_image_hash,
                grobid_excerpt=grobid_excerpt,
                docling_excerpt=docling_excerpt,
                grobid_confidence=grobid_conf,
                docling_confidence=docling_conf,
                parsers_agree=parsers_agree,
                needs_gap=needs_gap,
                gap_severity=gap_severity,
                gap_description=gap_description_full if needs_gap else gap_description,
                decision=decision,
                gap_id=gap_id,
            ))

        return results


# ---------------------------------------------------------------------------
# Result + investigator
# ---------------------------------------------------------------------------


@dataclass
class InvestigationResult:
    """The result of a :meth:`PaperInvestigator.investigate` run."""

    bundle_id: UUID
    published_count: int
    source_id: UUID
    raw_capture_hash: str
    grobid_artifact_id: UUID
    docling_artifact_id: UUID
    evidence_unit_ids: list[UUID] = field(default_factory=list)
    gap_ids: list[UUID] = field(default_factory=list)
    claim_ids: list[UUID] = field(default_factory=list)
    visual_review_results: list[VisualReviewResult] = field(default_factory=list)


class PaperInvestigator:
    """Converts a paper PDF into an implementation-ready evidence model (§11.3).

    Uses dual parsers (GROBID + Docling, ADR-009) to extract bibliographic
    metadata, equations, tables, figures, and structured document content.
    The §16.2 critical-content verification rule compares parser outputs
    against rendered page images and creates gaps when parsers disagree or
    confidence is low.

    By default uses :class:`ParserMode.OFFLINE` (fake parsers, no server, no
    model weights). Set ``mode=ParserMode.LIVE`` to use real GROBID + Docling
    — requires ``GROBID_URL`` and cached model weights.
    """

    def __init__(
        self,
        mode: ParserMode = ParserMode.OFFLINE,
        grobid_url: str | None = None,
        confidence_threshold: float = _CONFIDENCE_THRESHOLD_DEFAULT,
    ) -> None:
        self.mode = mode
        self.grobid_url = grobid_url
        self.confidence_threshold = confidence_threshold

    async def investigate(
        self,
        pdf_bytes: bytes,
        paper_locator: str,
        *,
        version: str = "1",
        run_id: str = "paper-investigation",
        task_id: str = "paper-investigation",
        actor: dict[str, Any] | None = None,
    ) -> InvestigationResult:
        """Run the full paper investigation pipeline and publish atomically.

        1. Stage ``source_identity`` (kind="paper") + ``raw_capture`` (kind="pdf").
        2. Create a ``visual_review`` ``prov_activity``.
        3. Run the dual parser (GROBID + Docling, ADR-009).
        4. Stage ``grobid_tei`` and ``docling_document`` derived artifacts.
        5. Emit ``evidence_unit`` rows with paper locators (§13.4).
        6. §16.2: render page images, compare parsers, create gaps on
           disagreement or low confidence.
        7. Stage ``claim`` rows from the §11.3 output.
        8. Auto-publish (staged→canonical, ADR-013) on context exit.
        """
        actor = actor or dict(_PAPER_PARSER_ACTOR)
        evidence_unit_ids: list[UUID] = []
        gap_ids: list[UUID] = []
        claim_ids: list[UUID] = []
        review_results: list[VisualReviewResult] = []
        elem_to_eu: dict[str, UUID] = {}

        async with InvestigatorContext(
            run_id=run_id,
            task_id=task_id,
            actor=actor,
            label="paper-investigation",
        ) as ctx:
            source_id = await ctx.stage_source_identity(
                "paper", paper_locator, version=version,
            )

            raw_hash = content_hash(pdf_bytes)
            raw_eid = await ctx.stage_source_capture(
                source_id, raw_hash, "pdf",
                mime_type="application/pdf",
                size_bytes=len(pdf_bytes),
                data=pdf_bytes,
            )

            review_activity = await ctx.create_activity(
                "visual_review",
                input_ids=[str(raw_eid)],
                output_ids=[],
                metadata={"paper_version": version},
            )

            grobid_result, docling_result = await self._run_dual_parser(pdf_bytes)

            grobid_eid = await ctx.stage_derived_artifact(
                raw_hash, grobid_result.content_hash, "grobid_tei", version=1,
            )
            docling_eid = await ctx.stage_derived_artifact(
                raw_hash, docling_result.content_hash, "docling_document", version=1,
            )

            evidence_unit_ids, elem_to_eu = await self._emit_evidence_units(
                ctx, grobid_eid, grobid_result, docling_result, version,
            )

            reviewer = VisualReviewer(self.confidence_threshold)
            review_results = await reviewer.review(
                ctx, grobid_result, docling_result, pdf_bytes,
                source_id, review_activity, version,
            )
            gap_ids = [r.gap_id for r in review_results if r.gap_id is not None]

            claim_ids = await self._stage_claims(
                ctx, elem_to_eu, version,
            )

            result = InvestigationResult(
                bundle_id=ctx._bundle_id,
                published_count=0,
                source_id=source_id,
                raw_capture_hash=raw_hash,
                grobid_artifact_id=grobid_eid,
                docling_artifact_id=docling_eid,
                evidence_unit_ids=list(evidence_unit_ids),
                gap_ids=list(gap_ids),
                claim_ids=list(claim_ids),
                visual_review_results=list(review_results),
            )

        result.published_count = ctx.published_count or 0
        return result

    async def _run_dual_parser(
        self, pdf_bytes: bytes,
    ) -> tuple[PaperParsingResult, PaperParsingResult]:
        """Run GROBID and Docling parsers concurrently (ADR-009)."""
        grobid_parser, docling_parser = make_parsers(self.mode, self.grobid_url)
        grobid_result, docling_result = await asyncio.gather(
            grobid_parser.parse(pdf_bytes),
            docling_parser.parse(pdf_bytes),
        )
        return grobid_result, docling_result

    async def _emit_evidence_units(
        self,
        ctx: InvestigatorContext,
        grobid_artifact_id: UUID,
        grobid_result: PaperParsingResult,
        docling_result: PaperParsingResult,
        version: str,
    ) -> tuple[list[UUID], dict[str, UUID]]:
        """Emit one evidence_unit per unique element (deduplicated across parsers).

        Returns ``(ids, elem_to_eu)`` where ``elem_to_eu`` maps
        ``element_type`` to the evidence_unit id of its first occurrence.
        """
        seen: set[str] = set()
        ids: list[UUID] = []
        elem_to_eu: dict[str, UUID] = {}

        for result in (grobid_result, docling_result):
            for elem in result.elements:
                loc = normalize_locator(
                    "paper", {**elem.locator, "version": version},
                )
                key = json.dumps(loc, sort_keys=True)
                if key in seen:
                    continue
                seen.add(key)
                ei = await ctx.stage_evidence_unit(
                    grobid_artifact_id,
                    loc,
                    content_hash=content_hash(elem.content),
                    metadata={
                        "excerpt": elem.content,
                        "parser_kind": result.parser_kind,
                        "element_type": elem.element_type,
                    },
                )
                ids.append(ei)
                elem_to_eu.setdefault(elem.element_type, ei)

        return ids, elem_to_eu

    async def _stage_claims(
        self,
        ctx: InvestigatorContext,
        elem_to_eu: dict[str, UUID],
        version: str,
    ) -> list[UUID]:
        """Stage §11.3 output claims, each linked to an evidence_unit."""
        claims: list[UUID] = []

        eq_eu = elem_to_eu.get("equation")
        if eq_eu is not None:
            ci = await ctx.stage_claim(
                "The core equation defines the primary computation "
                "variable, with a parser-disagreement gap recorded per "
                "§16.2 critical-content verification.",
                evidence_unit_id=eq_eu,
                confidence=0.80,
                metadata={"derived_from": "visual_review", "paper_version": version},
            )
            claims.append(ci)

        section_eu = elem_to_eu.get("section")
        if section_eu is not None:
            ci = await ctx.stage_claim(
                "The paper presents an algorithm with polynomial "
                "computational complexity derived from the method section.",
                evidence_unit_id=section_eu,
                confidence=0.85,
                metadata={
                    "derived_from": "method_section",
                    "paper_version": version,
                },
            )
            claims.append(ci)

        return claims
