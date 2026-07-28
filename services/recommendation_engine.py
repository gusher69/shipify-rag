"""AIRecommendationEngine — recommends how much of the AI Knowledge
Analyzer pipeline (services/knowledge_analyzer.py +
knowledge_graph_service.py) is actually worth running on a given upload.

Deliberately entirely deterministic/statistical — NO LLM calls. This has
to run BEFORE deciding whether to spend LLM budget on deep analysis, so
it can't itself depend on the thing it's deciding whether to run. Every
signal it uses (word count, section count, is-this-a-QA-sheet, language,
attachment count, file size) is available for free straight out of
extraction.

Four profiles, cheapest to most expensive:
    disabled  - skip the AI Knowledge Analyzer entirely (no LLM calls at
                all); chunking falls back to the plain generic chunker.
    basic     - classification + light metadata (category/tags/language)
                only; NO long summary, NO Knowledge Graph.
    standard  - classification + summaries + metadata; still no Knowledge
                Graph (used for structured/tabular content where the
                Excel Engine already carries the real semantics).
    advanced  - the full pipeline, including Knowledge Graph extraction.

The admin can always override the recommendation (see Import Preview) —
this module only computes a DEFAULT, never forces a choice.
"""
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

PROFILES = ["disabled", "basic", "standard", "advanced"]


@dataclass
class DocumentCharacteristics:
    filename: str
    file_size_bytes: int = 0
    page_count: int = 0
    word_count: int = 0
    section_count: int = 0
    is_qa_workbook: bool = False
    qa_row_count: int = 0
    knowledge_density: float = 0.0  # words per section (or per page if no sections)
    language: Optional[str] = None
    has_tables: bool = False
    has_images: bool = False
    attachment_count: int = 0
    likely_doc_type: str = "Unknown"  # cheap heuristic guess, not the final LLM classification


@dataclass
class ProfileRecommendation:
    profile: str
    reason: List[str] = field(default_factory=list)
    confidence: float = 0.6
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return {"profile": self.profile, "reason": self.reason,
                "confidence": self.confidence, "warnings": self.warnings}


def analyze_characteristics(pages: List[Dict], filename: str, file_size_bytes: int = 0,
                             attachment_count: int = 0) -> DocumentCharacteristics:
    from services.knowledge_analyzer import detect_sections, detect_lists_and_tables, _fallback_classify
    from ingestion.smart_enrichment import detect_language

    is_qa = any(p.get("is_qa_item") for p in pages)
    full_text = "\n\n".join(p.get("text", "") for p in pages)
    word_count = len(re.findall(r"\S+", full_text))
    page_count = len({p.get("page_number") for p in pages if p.get("page_number") is not None}) or len(pages)

    chars = DocumentCharacteristics(
        filename=filename, file_size_bytes=file_size_bytes,
        page_count=page_count, word_count=word_count,
        is_qa_workbook=is_qa, qa_row_count=len(pages) if is_qa else 0,
        language=detect_language(full_text),
        attachment_count=attachment_count,
        has_images=attachment_count > 0,
    )

    if is_qa:
        chars.likely_doc_type = "FAQ"
        chars.section_count = 0
        chars.knowledge_density = word_count / max(chars.qa_row_count, 1)
        return chars

    sections = detect_sections(full_text)
    chars.section_count = len([s for s in sections if s.heading])
    structure_info = detect_lists_and_tables(full_text)
    chars.has_tables = structure_info["table_rows"] > 0
    chars.knowledge_density = word_count / max(chars.section_count or page_count, 1)
    ktype, _ = _fallback_classify(full_text)
    chars.likely_doc_type = ktype
    return chars


def generate_warnings(chars: DocumentCharacteristics) -> List[str]:
    warnings = []
    if chars.is_qa_workbook and chars.qa_row_count <= 5:
        warnings.append(
            f"This file contains only {chars.qa_row_count} FAQ row"
            f"{'s' if chars.qa_row_count != 1 else ''}. AI Analysis may not provide "
            "significant additional value.")
    if chars.page_count >= 100:
        warnings.append(f"This document has {chars.page_count} pages. Advanced Analysis is recommended.")
    if chars.attachment_count >= 8:
        warnings.append("This document contains many images. OCR may be beneficial in a future version.")
    if chars.word_count < 20 and not chars.is_qa_workbook:
        warnings.append("This document has very little extractable text — check it isn't a scanned image without OCR.")
    return warnings


# knowledge_type (from the cheap fallback classifier) -> a profile the
# type alone strongly suggests, when there's enough content to matter.
# Matches the spec's own examples: Financial Report/Price List -> Standard
# (Excel Engine carries the real semantics), SOP/Manual -> Advanced
# (procedural knowledge graphs are exactly where Knowledge Graph earns its
# cost), Company Profile/Knowledge Article -> Advanced (rich prose with
# real entity relationships).
_TYPE_PROFILE_HINTS = {
    "Financial Report": ("standard", "Needs metadata and summaries; the Excel Engine already handles calculations."),
    "Price List": ("standard", "Needs metadata and summaries; the Excel Engine already handles structured pricing data."),
    "Excel Dataset": ("standard", "Structured tabular data — the Excel Engine carries the real semantics."),
    "SOP": ("advanced", "Procedural knowledge with a clear step sequence — Knowledge Graph recommended."),
    "Manual": ("advanced", "Procedural/hierarchical knowledge — Knowledge Graph recommended."),
    "Company Profile": ("advanced", "Multiple sections with rich semantic relationships between entities."),
    "Knowledge Article": ("advanced", "Multi-section prose likely to contain real entity relationships."),
    "Legal Document": ("advanced", "Clause-level cross-references benefit from relationship extraction."),
    "Terms & Conditions": ("advanced", "Clause-level cross-references benefit from relationship extraction."),
}


def recommend_profile(chars: DocumentCharacteristics) -> ProfileRecommendation:
    warnings = generate_warnings(chars)

    if chars.word_count < 20 and not chars.is_qa_workbook:
        return ProfileRecommendation(
            profile="disabled", confidence=0.8,
            reason=["Not enough extractable text for meaningful AI analysis."], warnings=warnings)

    if chars.is_qa_workbook:
        reason = ["Mostly structured FAQ (Question + Answer columns detected).",
                  "No need for Knowledge Graph — each row is already an independent, well-defined answer."]
        confidence = 0.85 if chars.qa_row_count <= 20 else 0.7
        return ProfileRecommendation(profile="basic", confidence=confidence, reason=reason, warnings=warnings)

    hint = _TYPE_PROFILE_HINTS.get(chars.likely_doc_type)
    if hint:
        profile, reason_text = hint
        confidence = 0.75
        if chars.page_count >= 100 and profile != "advanced":
            profile = "advanced"
            reason_text += " Document is also long enough that Advanced Analysis is worthwhile."
        return ProfileRecommendation(profile=profile, confidence=confidence, reason=[reason_text], warnings=warnings)

    # No strong type hint — fall back to structure/size heuristics.
    if chars.section_count >= 3 and chars.word_count >= 300:
        return ProfileRecommendation(
            profile="advanced", confidence=0.6,
            reason=["Multiple sections detected with substantial content.",
                    "Likely to contain real entity relationships worth extracting."],
            warnings=warnings)

    if chars.section_count >= 1 or chars.word_count >= 100:
        return ProfileRecommendation(
            profile="standard", confidence=0.55,
            reason=["Has some structure/content, but not clearly complex enough to need a Knowledge Graph."],
            warnings=warnings)

    return ProfileRecommendation(
        profile="basic", confidence=0.5,
        reason=["Short, lightly structured document — basic classification and tagging should suffice."],
        warnings=warnings)


class AIRecommendationEngine:
    """The single entry point Import Preview (and any future caller) uses
    instead of re-implementing this heuristic inline."""

    def analyze(self, pages: List[Dict], filename: str, file_size_bytes: int = 0,
                attachment_count: int = 0) -> DocumentCharacteristics:
        return analyze_characteristics(pages, filename, file_size_bytes, attachment_count)

    def recommend(self, chars: DocumentCharacteristics) -> ProfileRecommendation:
        return recommend_profile(chars)


_engine_singleton: Optional[AIRecommendationEngine] = None


def get_recommendation_engine() -> AIRecommendationEngine:
    global _engine_singleton
    if _engine_singleton is None:
        _engine_singleton = AIRecommendationEngine()
    return _engine_singleton
