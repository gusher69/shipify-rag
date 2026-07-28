"""PDF reading — now a hybrid Vision+OCR pipeline (see
services/pdf_page_pipeline.py). Every page is processed INDEPENDENTLY:
native text extraction (PyMuPDF) -> TextQualityEvaluator -> page
classification -> OCR/Vision only when the page actually needs it.

This is what fixes the reported production failure: pages with a broken
embedded-font mapping (garbled "(cid:N)"/Private-Use-Area text) used to
poison the WHOLE document, because the previous path (MarkItDown, then a
pdfplumber fallback) treated a PDF as a single blob of text with no
per-page quality awareness. Chunking now consumes each page's
`final_markdown`, never the raw corrupted native text.
"""
from typing import Dict, List, Optional

import fitz  # PyMuPDF — also used to render a page to an image for OCR/Vision

from services.pdf_page_pipeline import process_pdf_page, PageResult


def read_pdf(file_path: str) -> str:
    """อ่าน PDF แล้วคืน text ทั้งหมด (backward compat)"""
    pages = read_pdf_pages(file_path)
    return "\n".join(p["text"] for p in pages).strip()


def read_pdf_pages(file_path: str, *, analysis_profile: str = "basic") -> List[Dict]:
    """Returns [{page_number, text, ...extraction metadata}] — `text` is
    each page's FINAL markdown (post hybrid pipeline), never raw
    corrupted native text. Analysis profile controls OCR/Vision usage:
    'disabled' (native only), 'basic' (native + local OCR fallback,
    free), 'advanced' (native + OCR + Vision for pages that need it —
    the only profile that spends real money, and only on pages that
    actually require it)."""
    pages: List[Dict] = []
    try:
        doc = fitz.open(file_path)
    except Exception as e:
        print(f"❌ อ่าน PDF ไม่ได้: {file_path} — {e}")
        return pages

    # Document-level Thai hint: if MOST pages that have any alphabetic
    # content contain Thai characters, treat the whole doc as
    # Thai-expected — a single page failing this check (e.g. an
    # English-only cover) must not be judged against the wrong language.
    try:
        raw_texts = [p.get_text() for p in doc]
    except Exception:
        raw_texts = []
    thai_pages = sum(1 for t in raw_texts if any("฀" <= ch <= "๿" for ch in t))
    expected_thai = thai_pages > 0 and thai_pages >= max(1, len(raw_texts) // 3)

    for i, page in enumerate(doc, start=1):
        native_text = page.get_text() or ""
        images = page.get_images(full=True)

        def _render(_page=page):
            # Rendered at 2x zoom (~144 DPI from a 72-DPI base) — enough
            # resolution for OCR/Vision without producing huge payloads.
            pix = _page.get_pixmap(matrix=fitz.Matrix(2, 2))
            return pix.tobytes("png")

        try:
            result: PageResult = process_pdf_page(
                page_number=i, native_text=native_text, image_count=len(images),
                analysis_profile=analysis_profile, render_page_image=_render,
                expected_thai=expected_thai,
            )
        except Exception as e:
            print(f"[pdf_reader] page {i} pipeline failed, using raw native text: {e}")
            pages.append({"page_number": i, "text": native_text.strip()})
            continue

        if result.final_markdown.strip():
            pages.append({
                "page_number": i,
                "text": result.final_markdown.strip(),
                # Extraction provenance — carried through ingest.py into
                # chunk metadata (Part "CHUNKING"/"DATABASE / METADATA").
                "extraction_method": result.extraction_method,
                "page_type": result.page_type,
                "text_quality_score": result.quality_score,
                "ocr_confidence": result.ocr_confidence,
                "vision_used": result.vision_used,
                "extraction_warnings": result.warnings,
                "vision_trigger_reason": result.vision_trigger_reason,
            })
        elif result.warnings:
            print(f"[pdf_reader] page {i} produced no usable content: {'; '.join(result.warnings)}")

    doc.close()
    return pages
