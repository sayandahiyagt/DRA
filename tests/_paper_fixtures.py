"""Shared fixture builder for paper-investigator tests (§11.3, §16.2).

Provides:
- ``MINIMAL_PDF``: a minimal valid 1-page PDF (raw bytes, ~300B).
- ``MINIMAL_PDF_HASH``: deterministic content hash of the PDF.
- ``ACTOR``: reused from ``tests._evidence``.
- ``build_paper_bundle()``: stages a complete paper investigation bundle for
  direct publish testing (without running the full :class:`PaperInvestigator`).
"""

from __future__ import annotations

from uuid import UUID

from dra.investigators import content_hash, normalize_locator
from dra.publish import (
    add_prov_edge,
    async_session,
    create_activity,
    stage_bundle,
    stage_claim,
    stage_derived_artifact,
    stage_evidence_unit,
    stage_gap,
    stage_source_capture,
    stage_source_identity,
)
from tests._evidence import ACTOR, reset

# A minimal valid 1-page PDF — raw bytes that pypdfium2 can render.
# This is a well-known minimal PDF (single blank page, MediaBox
# [0 0 612 792]). Verified renderable by pypdfium2.PdfDocument.
MINIMAL_PDF = (
    b"%PDF-1.0\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj "
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj "
    b"3 0 obj<<<>>/Type/Page/MediaBox[0 0 612 792]/Parent 2 0 R>>endobj "
    b"xref\n0 4\n"
    b"0000000000 65535 f \n"
    b"0000000009 00000 n \n"
    b"0000000074 00000 n \n"
    b"0000000133 00000 n \n"
    b"trailer<</Size 4/Root 1 0 R>>\n"
    b"startxref\n190\n%%EOF"
)

MINIMAL_PDF_HASH = content_hash(MINIMAL_PDF)

PAPER_LOCATOR = "https://doi.org/10.1234/deep-research"
PAPER_VERSION = "1.0"

# Deterministic content hashes matching FakeGrobidParser / FakeDoclingParser raw text
_GROBID_RAW_TEXT = " ".join([
    "This paper presents a novel method for deep research.",
    "x = 24",
    "Table 1. Training results on dataset A",
    "Figure 1. Model architecture diagram",
])
_DOCLING_RAW_TEXT = " ".join([
    "This paper presents a novel method for deep research.",
    "x = 42",
    "Table 1. Training results on dataset A",
    "Figure 1. Model architecture diagram",
])
GROBID_TEI_HASH = content_hash(_GROBID_RAW_TEXT)
DOCLING_DOC_HASH = content_hash(_DOCLING_RAW_TEXT)

_EQUATION_LOCATOR = normalize_locator("paper", {
    "version": PAPER_VERSION, "page": 1, "section": "introduction", "equation": "eq-1",
})
_TABLE_LOCATOR = normalize_locator("paper", {
    "version": PAPER_VERSION, "page": 2, "section": "results", "table": "tab-1",
})
_FIGURE_LOCATOR = normalize_locator("paper", {
    "version": PAPER_VERSION, "page": 3, "figure": "fig-1",
})


async def build_paper_bundle(
    run_id: str = "run_paper",
    task_id: str = "task_paper",
) -> tuple[UUID, dict[str, UUID]]:
    """Stage a complete paper investigation bundle (§11.3, §16.2).

    Stages a paper source_identity + raw_capture (pdf) + grobid_tei derived
    artifact + docling_document derived artifact + evidence_units (with paper
    locators) + a visual_review activity + page image + gap + claims, all in one
    bundle.  Returns ``(bundle_id, ids)`` where ``ids`` maps prov_entity ids to
    labels for traversal assertions.
    """
    async with async_session() as session:
        async with session.begin():
            bundle_id = await stage_bundle(
                run_id, task_id, "paper-investigation", ACTOR,
            )

            source_id = await stage_source_identity(
                session, bundle_id, None, "paper", PAPER_LOCATOR,
                version=PAPER_VERSION,
                license_spdx=None, access_basis="public",
                crawl_allowed=True, redist_allowed=True,
            )
            acq = await create_activity(session, bundle_id, "acquisition", ACTOR)

            raw_eid = await stage_source_capture(
                session, bundle_id, acq, source_id, MINIMAL_PDF_HASH,
                kind="pdf", mime_type="application/pdf",
                size_bytes=len(MINIMAL_PDF), final_url="/store/paper.pdf",
            )
            await add_prov_edge(
                session, generated_entity_id=raw_eid, activity_id=acq,
            )

            parse = await create_activity(session, bundle_id, "parsing", ACTOR)
            grobid_eid = await stage_derived_artifact(
                session, bundle_id, parse, MINIMAL_PDF_HASH, GROBID_TEI_HASH,
                kind="grobid_tei", version=1,
            )
            docling_eid = await stage_derived_artifact(
                session, bundle_id, parse, MINIMAL_PDF_HASH, DOCLING_DOC_HASH,
                kind="docling_document", version=1,
            )
            await add_prov_edge(session, deriving_entity_id=grobid_eid,
                                source_entity_id=raw_eid, activity_id=parse)
            await add_prov_edge(session, deriving_entity_id=docling_eid,
                                source_entity_id=raw_eid, activity_id=parse)

            eq_eid = await stage_evidence_unit(
                session, bundle_id, parse, grobid_eid, _EQUATION_LOCATOR,
                content_hash=content_hash("x = 24"),
                metadata={"excerpt": "x = 24", "parser_kind": "grobid",
                          "element_type": "equation"},
            )
            await add_prov_edge(session, deriving_entity_id=eq_eid,
                                source_entity_id=grobid_eid, activity_id=parse)

            tbl_eid = await stage_evidence_unit(
                session, bundle_id, parse, grobid_eid, _TABLE_LOCATOR,
                content_hash=content_hash("Table 1. Training results on dataset A"),
                metadata={"excerpt": "Table 1. Training results on dataset A",
                          "parser_kind": "grobid", "element_type": "table"},
            )
            await add_prov_edge(session, deriving_entity_id=tbl_eid,
                                source_entity_id=grobid_eid, activity_id=parse)

            fig_eid = await stage_evidence_unit(
                session, bundle_id, parse, grobid_eid, _FIGURE_LOCATOR,
                content_hash=content_hash("Figure 1. Model architecture diagram"),
                metadata={"excerpt": "Figure 1. Model architecture diagram",
                          "parser_kind": "grobid", "element_type": "figure"},
            )
            await add_prov_edge(session, deriving_entity_id=fig_eid,
                                source_entity_id=grobid_eid, activity_id=parse)

            # §16.2: visual_review activity + page image + gap
            review = await create_activity(
                session, bundle_id, "visual_review", ACTOR,
                input_ids=[str(raw_eid)],
                metadata={"paper_version": PAPER_VERSION},
            )
            page_image_hash = content_hash(b"page-image-bytes")
            page_img_eid = await stage_source_capture(
                session, bundle_id, acq, source_id, page_image_hash,
                kind="image", mime_type="image/png", size_bytes=16,
                metadata={"paper_version": PAPER_VERSION,
                          "element_type": "equation", "page": 1,
                          "source": "pypdfium2"},
            )
            await add_prov_edge(
                session, generated_entity_id=page_img_eid, activity_id=acq,
            )

            gap_id = await stage_gap(
                session, bundle_id, review,
                "Parser disagreement on equation (page 1): "
                "GROBID says 'x = 24', Docling says 'x = 42'. "
                "Text overlap=0.50.",
                severity="critical",
                metadata={
                    "element_type": "equation",
                    "element_locator": _EQUATION_LOCATOR,
                    "grobid_excerpt": "x = 24",
                    "docling_excerpt": "x = 42",
                    "text_overlap": 0.50,
                    "grobid_confidence": 0.85,
                    "docling_confidence": 0.75,
                    "page_image_hash": page_image_hash,
                },
            )
            await add_prov_edge(
                session, generated_entity_id=gap_id, activity_id=review,
            )

            # §11.3 claims
            claim1 = await stage_claim(
                session, bundle_id, parse,
                "The core equation defines the primary computation variable.",
                evidence_unit_id=eq_eid, confidence=0.80,
                metadata={"derived_from": "visual_review",
                          "paper_version": PAPER_VERSION},
            )
            await add_prov_edge(session, deriving_entity_id=claim1,
                                source_entity_id=eq_eid, activity_id=parse)

            claim2 = await stage_claim(
                session, bundle_id, parse,
                "The paper presents an algorithm with polynomial complexity.",
                evidence_unit_id=tbl_eid, confidence=0.85,
                metadata={"derived_from": "method_section",
                          "paper_version": PAPER_VERSION},
            )
            await add_prov_edge(session, deriving_entity_id=claim2,
                                source_entity_id=tbl_eid, activity_id=parse)

            return bundle_id, {
                "source_id": source_id,
                "raw": raw_eid,
                "grobid_artifact": grobid_eid,
                "docling_artifact": docling_eid,
                "evidence_eq": eq_eid,
                "evidence_table": tbl_eid,
                "evidence_figure": fig_eid,
                "page_image": page_img_eid,
                "gap": gap_id,
                "claim1": claim1,
                "claim2": claim2,
                "acq": acq,
                "parse": parse,
                "review": review,
            }
