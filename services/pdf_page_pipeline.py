"""Per-page hybrid extraction orchestrator — the core of the new
Vision+OCR ingestion pipeline. Ties together TextQualityEvaluator,
page_classifier, OCRProvider, and VisionProvider into one deterministic
decision per page, gated by an analysis profile so Vision/OCR are never
called unnecessarily (cost control).

    native text -> TextQualityEvaluator -> page_classifier
                -> decide path (native / native+ocr / native+vision / merge)
                -> final_markdown
"""
from dataclasses import dataclass, field, asdict
from typing import Callable, Dict, List, Optional

from services.text_quality import TextQualityEvaluator
from services.page_classifier import classify_page
from services.ocr_provider import get_ocr_provider, OCRResult
from services.vision_provider import get_vision_provider, VisionResult

PROFILES = ("disabled", "basic", "advanced")


@dataclass
class PageResult:
    page_number: int
    page_type: str
    native_text: str
    ocr_text: str
    vision_markdown: str
    final_markdown: str
    quality_score: float
    ocr_confidence: float
    vision_used: bool
    extraction_method: str        # native | native+ocr | native+vision | ocr | vision | native+ocr+vision
    warnings: List[str] = field(default_factory=list)
    vision_trigger_reason: Optional[str] = None  # corrupted_native_text | missing_text | complex_table | informative_image | explicit_user_request | None

    def as_dict(self) -> Dict:
        return asdict(self)


def _dedup_append(base: str, addition: str) -> str:
    """Appends `addition` to `base` only if it isn't already substantially
    contained in it — the "deduplicate repeated text" merge rule."""
    if not addition or not addition.strip():
        return base
    if not base or not base.strip():
        return addition
    # Cheap containment check: if most of addition's words already appear
    # in base, treat it as redundant rather than duplicating content.
    base_words = set(base.split())
    add_words = [w for w in addition.split() if w]
    if add_words:
        overlap = sum(1 for w in add_words if w in base_words) / len(add_words)
        if overlap > 0.7:
            return base
    return base + "\n\n" + addition


def process_pdf_page(
    *,
    page_number: int,
    native_text: str,
    image_count: int,
    analysis_profile: str = "basic",
    render_page_image: Optional[Callable[[], bytes]] = None,
    expected_thai: bool = False,
    image_coverage_ratio: float = 0.0,
) -> PageResult:
    """`render_page_image`, if given, is called AT MOST ONCE and only if
    OCR/Vision is actually required for this page — rendering (and any
    paid Vision call) never happens for pages with good native text.

    `expected_thai` defaults to False — the CALLER (ingest.py) should set
    it True only when it has already detected this document is
    predominantly Thai (e.g. most other pages in the file decoded Thai
    characters). Defaulting this to True would misclassify every clean
    English-only document/page as suspicious and trigger unnecessary
    OCR/Vision calls.
    """
    if analysis_profile not in PROFILES:
        analysis_profile = "basic"

    warnings: List[str] = []
    evaluator = TextQualityEvaluator()
    quality = evaluator.evaluate(native_text, expected_thai=expected_thai)
    classification = classify_page(text_quality=quality, native_text=native_text,
                                    image_count=image_count, image_coverage_ratio=image_coverage_ratio)

    ocr_text = ""
    ocr_confidence = 0.0
    vision_markdown = ""
    vision_used = False
    methods = []

    use_native = quality.extraction_status == "good"
    if use_native:
        methods.append("native")

    # A page can also need OCR/Vision purely because it was classified as a
    # complex table, even when its native text quality LOOKS "good" by raw
    # character stats — flattened table structure doesn't show up as
    # corrupted/sparse text. This is the only classification-driven
    # trigger allowed to render the page independent of quality; a merely
    # image-heavy ("infographic") page never renders on classification
    # alone (see vision_trigger_reason logic below).
    needs_ocr_or_vision = (
        (quality.requires_ocr or quality.requires_vision or classification.page_type == "table")
        and analysis_profile != "disabled"
    )
    vision_trigger_reason: Optional[str] = None

    if needs_ocr_or_vision and render_page_image is not None:
        image_bytes: Optional[bytes] = None
        try:
            image_bytes = render_page_image()
        except Exception as e:
            warnings.append(f"Failed to render page {page_number} as an image: {e}")

        if image_bytes is not None:
            # OCR first (free/local, if available) — never spend on Vision
            # for something a local OCR engine can already recover.
            # Failsafe: an OCR crash must not fail the page — fall through
            # to Vision (if the profile allows it) or whatever native text
            # exists.
            ocr_result: Optional[OCRResult] = None
            try:
                ocr_provider = get_ocr_provider()
                ocr_result = ocr_provider.recognize(image_bytes, languages=["tha", "eng"])
            except Exception as e:
                warnings.append(f"OCR failed for page {page_number}: {e}")

            if ocr_result and ocr_result.available and ocr_result.text.strip():
                ocr_text = ocr_result.text
                ocr_confidence = ocr_result.confidence
                methods.append("ocr")
            elif ocr_result is not None and not ocr_result.available:
                warnings.append("Local OCR engine unavailable — relying on Vision if enabled.")
            if ocr_result is not None:
                warnings.extend(ocr_result.warnings)

            # Vision gating — decision order matters (native text quality is
            # evaluated FIRST; page classification is only a SECONDARY
            # signal, and only for the "table" case, which commonly loses
            # row/column structure in native extraction even when the raw
            # character stats look "good"). A page with good, sufficient
            # native text is never sent to Vision merely because it's
            # image-heavy ("infographic"/"image"/"scanned_text" alone,
            # decorative images) — those only count when the text quality
            # signal ALSO indicates missing/corrupted content.
            if quality.extraction_status == "corrupted":
                vision_trigger_reason = "corrupted_native_text"
            elif quality.extraction_status in ("sparse", "empty") or quality.requires_vision:
                vision_trigger_reason = "missing_text"
            elif classification.page_type == "table":
                vision_trigger_reason = "complex_table"
            elif classification.page_type in ("infographic", "image", "scanned_text") and not use_native:
                vision_trigger_reason = "informative_image"
            else:
                vision_trigger_reason = None

            vision_needed = analysis_profile == "advanced" and vision_trigger_reason is not None
            if vision_needed:
                try:
                    vision_provider = get_vision_provider(enabled=True)
                    vision_result: VisionResult = vision_provider.analyze_page_image(
                        image_bytes, context_hint=f"Page {page_number} of a business document.")
                    if vision_result.markdown.strip():
                        vision_markdown = vision_result.markdown
                        vision_used = True
                        methods.append("vision")
                except Exception as e:
                    warnings.append(f"Vision analysis failed for page {page_number}: {e}")
        elif needs_ocr_or_vision:
            warnings.append(f"Page {page_number} needed OCR/Vision but no page-image renderer was provided.")
    elif needs_ocr_or_vision and render_page_image is None:
        warnings.append(f"Page {page_number} needed OCR/Vision but no page-image renderer was provided.")

    # ── Merge strategy ──
    # Vision's structured Markdown is the most complete/faithful
    # reconstruction when available — it becomes the primary content.
    # Native text is layered in ONLY if it adds something Vision missed
    # (rare, but Vision doesn't invent facts and may omit boilerplate);
    # OCR is layered in only if genuinely additive over what's already
    # present. Good native text alone never gets overwritten by lower-
    # quality OCR (Failsafe rule).
    if vision_markdown:
        final_markdown = vision_markdown
        if use_native:
            final_markdown = _dedup_append(final_markdown, native_text)
    elif use_native:
        final_markdown = native_text
        if ocr_text:
            final_markdown = _dedup_append(final_markdown, ocr_text)
            if "ocr" not in methods:
                methods.append("ocr")
    elif ocr_text:
        final_markdown = ocr_text
    else:
        final_markdown = native_text or ""
        if not final_markdown.strip():
            warnings.append(f"Page {page_number}: no usable content from any extraction method.")
        elif quality.extraction_status in ("corrupted", "sparse"):
            # e.g. the 'disabled' analysis profile: OCR/Vision are never
            # invoked, so a low-quality page's native text is used as-is
            # — still surfaced as a warning so the import report shows it
            # rather than silently shipping garbled content.
            warnings.append(f"Page {page_number}: using low-quality native text as-is "
                             f"(quality_score={quality.quality_score}, {quality.reason}) — "
                             f"OCR/Vision were not run (analysis_profile='{analysis_profile}').")

    extraction_method = "+".join(methods) if methods else "none"

    return PageResult(
        page_number=page_number, page_type=classification.page_type,
        native_text=native_text or "", ocr_text=ocr_text, vision_markdown=vision_markdown,
        final_markdown=final_markdown, quality_score=quality.quality_score,
        ocr_confidence=ocr_confidence, vision_used=vision_used,
        extraction_method=extraction_method, warnings=warnings,
        vision_trigger_reason=vision_trigger_reason if vision_used else None,
    )
