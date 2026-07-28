"""KnowledgeAnalyzerService — runs after extraction, before chunking.

Understands WHAT a document is before deciding HOW to turn it into RAG
knowledge. Reusable by design: Import Preview calls it to show the
analysis before confirming; the real ingestion pipeline calls it to pick
a chunk strategy; the AI Playground reads its output from stored
metadata. Future modules (OCR, Vision, Excel Engine, Prompt Studio, AI
Policies, Search Ranking, Hybrid Search) are meant to call the SAME
`analyze()` entry point rather than re-implementing classification.

Design principle (repeated because it's the whole point of this
service): the original document is always the source of truth. Every
field this service produces — summary, tags, topics, suggested
questions, quality warnings — is a RETRIEVAL ENHANCEMENT stored alongside
the real content, never a replacement for it. Nothing here deletes or
rewrites extracted text.

Classification and document-understanding fields are LLM-backed
(best-effort — degrade to a deterministic keyword fallback on any
failure, never raise, never block ingestion). Section/heading detection
and quality checks are deterministic — no LLM required for those, since
they're mechanical (regex/heuristic) checks that don't need a language
model to be reliable.
"""
import json
import os
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional

ANALYSIS_VERSION = "1.0"

KNOWLEDGE_TYPES = [
    "FAQ", "Company Profile", "Product Information", "Shipping Guide",
    "Return Policy", "SOP", "Knowledge Article", "Manual",
    "Financial Report", "Price List", "Marketing Content", "Legal Document",
    "Terms & Conditions", "Promotion", "Excel Dataset", "Mixed", "Unknown",
]

# knowledge_type -> chunk strategy. This is the ONE place that decision is
# made — ingestion/ingest.py's chunk_pages_smart() and the Import Preview
# both read from here, so they can never disagree about which strategy a
# given type gets.
CHUNK_STRATEGY_BY_TYPE = {
    "FAQ": "faq_qa",
    "Company Profile": "heading_paragraph",
    "Product Information": "heading_paragraph",
    "Shipping Guide": "heading_paragraph",
    "Return Policy": "section_clause",
    "SOP": "step_sequence",
    "Knowledge Article": "heading_paragraph",
    "Manual": "procedure_hierarchy",
    "Financial Report": "excel_engine",
    "Price List": "excel_engine",
    "Marketing Content": "heading_paragraph",
    "Legal Document": "section_clause",
    "Terms & Conditions": "section_clause",
    "Promotion": "heading_paragraph",
    "Excel Dataset": "excel_engine",
    "Mixed": "hybrid",
    "Unknown": "hybrid",
}

# Deterministic fallback classifier (used when the LLM is unavailable/
# fails) — same "keyword dictionary" pattern already used elsewhere in
# this codebase (ingest.py's INTENT_KEYWORDS, smart_enrichment's tag
# keywords). Checked in order; first match wins.
_TYPE_KEYWORDS = [
    ("FAQ", ["faq", "frequently asked question", "คำถามที่พบบ่อย", "q&a", "question and answer"]),
    ("Return Policy", ["return policy", "refund", "คืนสินค้า", "คืนเงิน", "เคลม"]),
    # Deliberately multi-word/specific phrases, not bare words like
    # "shipping" alone — a generic business document can mention shipping
    # in passing without BEING a shipping guide; single generic words
    # caused false-positive classification in testing.
    ("Shipping Guide", ["shipping guide", "shipping policy", "delivery time",
                          "tracking number", "จัดส่งภายใน", "ระยะเวลาจัดส่ง", "วิธีการจัดส่ง"]),
    ("SOP", ["sop", "standard operating procedure", "ขั้นตอนการทำงาน"]),
    ("Manual", ["user manual", "instruction manual", "คู่มือการใช้งาน", "manual"]),
    ("Financial Report", ["income statement", "balance sheet", "financial report", "งบการเงิน"]),
    ("Price List", ["price list", "pricing", "ราคาสินค้า", "รายการราคา"]),
    ("Terms & Conditions", ["terms and conditions", "terms & conditions", "ข้อกำหนดและเงื่อนไข"]),
    ("Legal Document", ["agreement", "contract", "สัญญา", "ข้อตกลง"]),
    ("Promotion", ["promotion", "โปรโมชั่น", "ส่วนลด", "discount"]),
    ("Company Profile", ["company profile", "about us", "เกี่ยวกับเรา", "ประวัติบริษัท"]),
    ("Product Information", ["product information", "specification", "สินค้า", "spec"]),
    ("Marketing Content", ["marketing", "campaign", "แคมเปญ"]),
]

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_PII_PATTERNS = {
    "email": re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"),
    "phone_th": re.compile(r"0[689]\d{1}-?\d{3}-?\d{4}\b"),
    "thai_id": re.compile(r"\b\d{1}-?\d{4}-?\d{5}-?\d{2}-?\d{1}\b"),
    "credit_card": re.compile(r"\b(?:\d[ -]*?){13,16}\b"),
}
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


@dataclass
class Section:
    level: int
    heading: str
    text: str
    heading_path: List[str] = field(default_factory=list)


@dataclass
class QualityIssue:
    kind: str
    detail: str
    severity: str  # error | warning | info
    location: Optional[str] = None


@dataclass
class KnowledgeAnalysis:
    filename: str
    knowledge_type: str = "Unknown"
    confidence: float = 0.0
    chunk_strategy: str = "hybrid"
    title: Optional[str] = None
    summary_short: Optional[str] = None
    summary_long: Optional[str] = None
    topics: List[str] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    language: Optional[str] = None
    audience: Optional[str] = None
    department: Optional[str] = None
    difficulty: Optional[str] = None
    visibility_recommendation: Optional[str] = None
    priority: Optional[str] = None
    suggested_questions: List[str] = field(default_factory=list)
    document_structure: List[Dict] = field(default_factory=list)  # [{level, heading, heading_path}]
    quality_issues: List[Dict] = field(default_factory=list)
    ai_suggestions: List[str] = field(default_factory=list)
    analysis_version: str = ANALYSIS_VERSION
    used_llm: bool = False
    # True only when the LLM was actually attempted and failed/unavailable
    # (see KnowledgeAnalyzerService.analyze's fallback branch) — NOT set for
    # the FAQ short-circuit or a "disabled" profile, since those are
    # intentional skips, not degradation.
    ai_analysis_degraded: bool = False
    knowledge_graph: Dict = field(default_factory=lambda: {"nodes": [], "edges": [], "error": False})

    def to_dict(self) -> Dict:
        return asdict(self)


# ── Deterministic: section/heading detection ───────────────────────

def detect_sections(text: str) -> List[Section]:
    """Split Markdown-ish text into sections by heading level, preserving
    the heading hierarchy (heading_path) for each section. Text with no
    headings at all becomes a single unnamed section — callers treat that
    as "no structure detected" (see quality_issues' missing_headings)."""
    lines = text.splitlines()
    sections: List[Section] = []
    stack: List[str] = []  # current heading path
    current_heading, current_level, current_lines = None, 0, []

    def flush():
        if current_lines or current_heading:
            sections.append(Section(
                level=current_level, heading=current_heading or "",
                text="\n".join(current_lines).strip(), heading_path=list(stack),
            ))

    for line in lines:
        m = _HEADING_RE.match(line.strip())
        if m:
            flush()
            level = len(m.group(1))
            heading = m.group(2).strip()
            stack = stack[:level - 1] + [heading]
            current_heading, current_level, current_lines = heading, level, []
        else:
            current_lines.append(line)
    flush()
    return [s for s in sections if s.text.strip() or s.heading]


def detect_lists_and_tables(text: str) -> Dict[str, int]:
    bullet_lines = len(re.findall(r"^\s*[-*•]\s+\S", text, re.MULTILINE))
    numbered_lines = len(re.findall(r"^\s*\d+[.)]\s+\S", text, re.MULTILINE))
    table_rows = len(re.findall(r"^\s*\|.*\|\s*$", text, re.MULTILINE))
    code_blocks = len(re.findall(r"```", text)) // 2
    return {"bullet_lines": bullet_lines, "numbered_lines": numbered_lines,
            "table_rows": table_rows, "code_blocks": code_blocks}


# ── Deterministic: quality review ──────────────────────────────────

def _looks_like_ocr_garble(text: str) -> bool:
    """Cheap heuristic: a high ratio of control/replacement characters,
    or long runs of single characters repeated, suggests a bad OCR/scan
    extraction rather than real text. Not a real OCR-quality classifier —
    a mechanical red flag, not a verdict."""
    if not text:
        return False
    control_ratio = len(_CONTROL_CHAR_RE.findall(text)) / max(len(text), 1)
    replacement_ratio = text.count("�") / max(len(text), 1)
    return control_ratio > 0.02 or replacement_ratio > 0.01


def run_quality_review(sections: List[Section], full_text: str) -> List[QualityIssue]:
    issues: List[QualityIssue] = []

    if not sections or all(not s.heading for s in sections):
        issues.append(QualityIssue("missing_headings",
            "No headings detected — the document has no structure to chunk by heading.",
            "warning"))

    seen_norm: Dict[str, str] = {}
    for s in sections:
        loc = s.heading or "(unnamed section)"
        length = len(s.text)
        if length == 0:
            continue
        if length < 40:
            issues.append(QualityIssue("very_short_section",
                f"Section \"{loc}\" is only {length} characters — likely too short to be a useful standalone chunk.",
                "info", location=loc))
        elif length > 6000:
            issues.append(QualityIssue("very_large_chunk",
                f"Section \"{loc}\" is {length} characters — likely too large for one chunk; consider splitting.",
                "warning", location=loc))

        if _looks_like_ocr_garble(s.text):
            issues.append(QualityIssue("possible_ocr_issue",
                f"Section \"{loc}\" contains unusual control/replacement characters — check the source scan/OCR quality.",
                "warning", location=loc))

        norm = re.sub(r"\s+", " ", s.text.strip().lower())[:300]
        if norm and len(norm) > 30:
            for prev_loc, prev_norm in seen_norm.items():
                if norm == prev_norm:
                    issues.append(QualityIssue("duplicate_knowledge",
                        f"Section \"{loc}\" appears to duplicate \"{prev_loc}\".",
                        "warning", location=loc))
                    break
            seen_norm[loc] = norm

        for kind, pattern in _PII_PATTERNS.items():
            if pattern.search(s.text):
                issues.append(QualityIssue("pii_detected",
                    f"Section \"{loc}\" may contain personally identifiable information ({kind}).",
                    "warning", location=loc))

        if re.search(r"internal\s*only|ห้ามเผยแพร่|ภายในเท่านั้น|confidential", s.text, re.IGNORECASE):
            issues.append(QualityIssue("internal_only",
                f"Section \"{loc}\" appears to be marked internal-only/confidential — review visibility before publishing.",
                "warning", location=loc))

        if re.search(r"(expired|outdated|เลิกใช้|ยกเลิกแล้ว|no longer (valid|applicable))", s.text, re.IGNORECASE):
            issues.append(QualityIssue("possibly_outdated",
                f"Section \"{loc}\" contains language suggesting it may be outdated.",
                "info", location=loc))

    return issues


def suggest_actions(issues: List[QualityIssue], sections: List[Section]) -> List[str]:
    suggestions = []
    kinds = {i.kind for i in issues}
    if "duplicate_knowledge" in kinds:
        suggestions.append("Merge duplicated sections before importing to avoid redundant knowledge items.")
    if "very_large_chunk" in kinds:
        suggestions.append("Split oversized sections into smaller sub-sections for more precise retrieval.")
    if "missing_headings" in kinds:
        suggestions.append("Add headings to the source document — chunking quality improves significantly with clear structure.")
    if "pii_detected" in kinds:
        suggestions.append("Review flagged sections for personal data before making this knowledge base-wide searchable.")
    if not sections or len(sections) <= 1:
        suggestions.append("Consider generating alternative questions to improve retrieval for this single-section document.")
    return suggestions


# ── LLM-backed: classification + document understanding ────────────

def _fallback_classify(text: str) -> tuple:
    lower = text.lower()
    for ktype, keywords in _TYPE_KEYWORDS:
        if any(kw in lower for kw in keywords):
            return ktype, 0.5
    return "Unknown", 0.2


def _llm_analyze(text_sample: str, filename: str) -> Optional[Dict]:
    if os.getenv("ENABLE_KNOWLEDGE_ANALYZER_LLM", "true").strip().lower() in ("0", "false", "no"):
        return None
    try:
        from services.llm_service import get_llm_service
        from config import OPENAI_CHAT_MODEL
        llm = get_llm_service()
        types_list = ", ".join(KNOWLEDGE_TYPES)
        prompt = (
            "You are a document classification and understanding engine for a customer-support "
            "knowledge base. Analyze the document below and respond with ONLY a JSON object "
            "(no markdown fences, no explanation) with exactly these keys:\n"
            '{"knowledge_type": one of [' + types_list + '], "confidence": 0-1 float, '
            '"title": string, "summary_short": string (<=200 chars), "summary_long": string (<=800 chars), '
            '"topics": [string,...] (<=8), "keywords": [string,...] (<=10), "tags": [string,...] (<=8), '
            '"audience": string, "department": string, "difficulty": one of [Basic, Intermediate, Advanced], '
            '"visibility_recommendation": one of [Public, Internal, Restricted], '
            '"priority": one of [Low, Medium, High], '
            '"suggested_questions": [string,...] (5-8 likely customer questions this document answers, '
            "in the SAME language as the document)}\n\n"
            f"Filename: {filename}\n\nDocument content:\n{text_sample[:6000]}"
        )
        resp = llm.generate([{"role": "user", "content": prompt}],
                             model=OPENAI_CHAT_MODEL, temperature=0.2, max_tokens=900)
        raw = resp.text.strip()
        raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
        data = json.loads(raw)
        if data.get("knowledge_type") not in KNOWLEDGE_TYPES:
            data["knowledge_type"] = "Unknown"
        return data
    except Exception as exc:
        print(f"[knowledge_analyzer] LLM analysis skipped/failed: {exc}")
        return None


class KnowledgeAnalyzerService:
    """The single entry point future modules (OCR, Vision, Excel Engine,
    Prompt Studio, AI Policies, Search Ranking, Hybrid Search) should call
    instead of re-implementing document classification."""

    def analyze(self, pages: List[Dict], filename: str,
                language_hint: Optional[str] = None,
                profile: Optional[str] = None) -> KnowledgeAnalysis:
        """profile (from services.recommendation_engine, or an admin
        override): "disabled" skips this analyzer entirely (no LLM calls at
        all); "basic"/"standard" skip Knowledge Graph extraction regardless
        of ENABLE_KNOWLEDGE_GRAPH; "advanced"/None run the full pipeline
        (graph extraction still gated by ENABLE_KNOWLEDGE_GRAPH as before)."""
        full_text = "\n\n".join(p.get("text", "") for p in pages)
        is_qa_workbook = any(p.get("is_qa_item") for p in pages)

        analysis = KnowledgeAnalysis(filename=filename)

        if profile == "disabled":
            analysis.knowledge_type = "Unknown"
            analysis.confidence = 0.0
            analysis.chunk_strategy = "hybrid"
            analysis.title = Path(filename).stem
            analysis.summary_short = (full_text.strip()[:180] + "…") if len(full_text) > 180 else full_text.strip()
            analysis.ai_suggestions = ["AI Analysis disabled for this file (per selected AI Analysis Profile)."]
            return analysis

        if is_qa_workbook:
            # Already unambiguous — an Excel/CSV sheet with detected
            # Question+Answer columns IS an FAQ by definition; skip
            # classification uncertainty entirely.
            analysis.knowledge_type = "FAQ"
            analysis.confidence = 1.0
            analysis.chunk_strategy = CHUNK_STRATEGY_BY_TYPE["FAQ"]
            analysis.document_structure = []
            return analysis

        sections = detect_sections(full_text)
        analysis.document_structure = [
            {"level": s.level, "heading": s.heading, "heading_path": s.heading_path}
            for s in sections if s.heading
        ]
        structure_info = detect_lists_and_tables(full_text)

        llm_result = _llm_analyze(full_text, filename)
        if llm_result:
            analysis.used_llm = True
            analysis.knowledge_type = llm_result.get("knowledge_type", "Unknown")
            analysis.confidence = float(llm_result.get("confidence") or 0.6)
            analysis.title = llm_result.get("title")
            analysis.summary_short = llm_result.get("summary_short")
            analysis.summary_long = llm_result.get("summary_long")
            analysis.topics = llm_result.get("topics") or []
            analysis.keywords = llm_result.get("keywords") or []
            analysis.tags = llm_result.get("tags") or []
            analysis.audience = llm_result.get("audience")
            analysis.department = llm_result.get("department")
            analysis.difficulty = llm_result.get("difficulty")
            analysis.visibility_recommendation = llm_result.get("visibility_recommendation")
            analysis.priority = llm_result.get("priority")
            analysis.suggested_questions = llm_result.get("suggested_questions") or []
        else:
            ktype, conf = _fallback_classify(full_text)
            analysis.knowledge_type = ktype
            analysis.confidence = conf
            analysis.title = Path(filename).stem
            analysis.summary_short = (full_text.strip()[:180] + "…") if len(full_text) > 180 else full_text.strip()
            analysis.ai_analysis_degraded = True

        analysis.language = language_hint or _detect_lang(full_text)
        analysis.chunk_strategy = CHUNK_STRATEGY_BY_TYPE.get(analysis.knowledge_type, "hybrid")

        issues = run_quality_review(sections, full_text)
        analysis.quality_issues = [asdict(i) for i in issues]
        analysis.ai_suggestions = suggest_actions(issues, sections)

        # Knowledge Graph Extraction — runs here, after classification/
        # summary/topics/suggested-questions and BEFORE chunking/embedding,
        # per the pipeline ordering this feature specifies. Best-effort;
        # never blocks the rest of analysis or ingestion on failure.
        if profile in ("basic", "standard"):
            analysis.ai_suggestions.append(
                f"Knowledge Graph skipped for this file (AI Analysis Profile: {profile}).")
            return analysis

        try:
            from services.knowledge_graph_service import get_knowledge_graph_service
            # Kept as real GraphNode/GraphEdge dataclass instances (not
            # pre-serialized) — to_dict() below (via asdict()) serializes
            # them for API/preview responses, while admin/routes.py's sync
            # loop uses the objects directly with
            # KnowledgeGraphService.save_graph().
            analysis.knowledge_graph = get_knowledge_graph_service().extract_graph(
                full_text, filename, knowledge_type=analysis.knowledge_type)
        except Exception as exc:
            print(f"[knowledge_analyzer] graph extraction skipped: {exc}")

        return analysis


def _detect_lang(text: str) -> str:
    from ingestion.smart_enrichment import detect_language
    return detect_language(text)


_analyzer_singleton: Optional[KnowledgeAnalyzerService] = None


def get_knowledge_analyzer() -> KnowledgeAnalyzerService:
    global _analyzer_singleton
    if _analyzer_singleton is None:
        _analyzer_singleton = KnowledgeAnalyzerService()
    return _analyzer_singleton
