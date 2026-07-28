"""Lightweight, dependency-free PDF page classification — decides what
KIND of page this is using cheap heuristics (text quality, image
coverage, numeric/table density) BEFORE ever calling Vision, so Vision is
only used on pages that actually need it (cost control).
"""
import re
from dataclasses import dataclass
from typing import Optional

from services.text_quality import TextQualityResult

PAGE_TYPES = ("native_text", "scanned_text", "image", "infographic", "table", "mixed", "mostly_empty", "unknown")

# A line that's mostly digits/currency/percent/short tokens suggests a
# table row (benefit amounts, prices) rather than prose.
_TABLE_ROW_RE = re.compile(r"^[\d,.\s%฿$บาท()/-]{3,}$")


def _table_line_ratio(text: str) -> float:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines:
        return 0.0
    table_lines = sum(1 for l in lines if _TABLE_ROW_RE.match(l))
    return table_lines / len(lines)


@dataclass
class PageClassification:
    page_type: str
    reason: str


def classify_page(*, text_quality: TextQualityResult, native_text: str,
                   image_count: int, image_coverage_ratio: float = 0.0) -> PageClassification:
    """`image_coverage_ratio` (0..1) is the fraction of the page's visible
    area covered by embedded images, if the caller can compute it (e.g.
    via PyMuPDF image bbox areas) — 0.0 if unknown/not provided, in which
    case classification falls back to text-only heuristics."""
    text = native_text or ""

    if text_quality.extraction_status == "empty" and image_count == 0:
        return PageClassification("mostly_empty", "No text and no images on this page.")

    if text_quality.extraction_status == "empty" and image_count > 0:
        return PageClassification("image", "No extractable text; page contains only image(s).")

    if image_coverage_ratio >= 0.85 and text_quality.quality_score < 0.5:
        return PageClassification("image", "Page is almost entirely covered by image content.")

    table_ratio = _table_line_ratio(text)
    if table_ratio >= 0.4 and text_quality.extraction_status == "good":
        return PageClassification("table", f"{table_ratio:.0%} of lines look like table/numeric rows.")

    if text_quality.extraction_status in ("corrupted", "sparse") and image_count > 0:
        return PageClassification("infographic",
                                   "Text extraction is unreliable and the page contains embedded "
                                   "images — likely an infographic or table rendered as a graphic.")

    if text_quality.extraction_status in ("corrupted", "sparse") and image_count == 0:
        return PageClassification("scanned_text",
                                   "Text extraction is unreliable with no embedded images — likely a "
                                   "scanned page (the 'text' is extraction noise, not real content).")

    if text_quality.extraction_status == "good" and image_count > 0:
        return PageClassification("mixed", "Good native text alongside embedded image(s).")

    if text_quality.extraction_status == "good":
        return PageClassification("native_text", "Clean, directly usable native text extraction.")

    return PageClassification("unknown", "Could not confidently classify this page.")
