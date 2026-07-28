import re
import uuid
from pathlib import Path
from typing import List, Dict, Optional

import tiktoken

from ingestion.pdf_reader import read_pdf, read_pdf_pages
from ingestion.word_reader import read_word, read_word_pages
from ingestion.excel_reader import read_excel_pages
from ingestion.markdown_converter import convert_to_markdown
from ingestion.excel_extractor import extract_workbook, extract_csv, workbook_to_summary_pages
from config import CHUNK_SIZE, CHUNK_OVERLAP

KNOWLEDGE_DIR = Path("knowledge")

INTENT_KEYWORDS = {
    "สต็อก":   ["สต็อก", "สินค้า", "มีของ", "ราคา", "catalog", "product"],
    "ออเดอร์": ["ออเดอร์", "order", "จัดส่ง", "ส่งของ", "tracking", "พัสดุ"],
    "นโยบาย":  ["นโยบาย", "คืน", "เคลม", "เงื่อนไข", "ประกัน", "รับประกัน"],
    "ทั่วไป":  ["faq", "วิธี", "ขั้นตอน", "สมัคร", "ติดต่อ", "บริษัท"],
}

# Simple Thai/non-ASCII detector for language field
_THAI_RE = re.compile(r"[฀-๿]")


def detect_intent(text: str) -> str:
    text_lower = text.lower()
    for intent, keywords in INTENT_KEYWORDS.items():
        if any(kw in text_lower for kw in keywords):
            return intent
    return "ทั่วไป"


def detect_language(text: str) -> str:
    return "th" if _THAI_RE.search(text) else "en"


def _extract_section_title(text: str) -> Optional[str]:
    """ดึงบรรทัดแรกที่สั้นกว่า 120 ตัวอักษรมาเป็น section title"""
    for line in text.splitlines():
        line = line.strip()
        if line and len(line) <= 120:
            return line
    return None


def chunk_text(
    text: str,
    source: str,
    *,
    file_id: Optional[str] = None,
    file_name: Optional[str] = None,
    file_type: Optional[str] = None,
    file_size: Optional[int] = None,
    storage_path: Optional[str] = None,
    document_title: Optional[str] = None,
    category: Optional[str] = None,
    scope: Optional[str] = None,
    platform: Optional[str] = None,
    tags=None,
    version: int = 1,
    page_number: Optional[int] = None,
    start_index: int = 0,
) -> List[Dict]:
    """แบ่ง text เป็น chunks พร้อม rich metadata — backward compat กับ caller เดิม

    `start_index` lets a caller that invokes chunk_text() multiple times
    for ONE file (chunk_pages() once per page, chunk_pages_by_heading()
    once per section) keep chunk_index globally increasing across those
    calls instead of every call restarting at 0 — see chunk_pages() and
    chunk_pages_by_heading() below, which both thread a running counter
    through this parameter. Defaults to 0 so any other/future caller that
    only ever calls this once per file is unaffected.
    """
    enc = tiktoken.get_encoding("cl100k_base")
    tokens = enc.encode(text)
    chunks = []
    i = 0
    chunk_index = start_index
    while i < len(tokens):
        chunk_tokens = tokens[i:i + CHUNK_SIZE]
        chunk_str = enc.decode(chunk_tokens)
        intent = detect_intent(chunk_str)
        lang = detect_language(chunk_str)
        section_title = _extract_section_title(chunk_str)
        chunk_id = str(uuid.uuid4())

        chunks.append({
            # core fields (used by embedder)
            "text":    chunk_str,
            "source":  source,
            "intent":  intent,
            "file_id": file_id,
            # rich metadata passed through to embedder
            "metadata": {
                "file_id":            file_id,
                "file_name":          file_name or source,
                "original_file_name": file_name or source,
                "file_type":          file_type or Path(source).suffix.lstrip(".").lower(),
                "file_size":          file_size,
                "storage_path":       storage_path or f"knowledge/{source}",
                "source":             "uploaded_file",
                "document_title":     document_title or Path(source).stem,
                "category":           category or "",
                "scope":              scope or "",
                "platform":           platform or "",
                "tags":               tags if isinstance(tags, list) else (tags.split(",") if tags else []),
                "page_number":        page_number,
                "section_title":      section_title,
                "chunk_index":        chunk_index,
                "chunk_id":           chunk_id,
                "version":            version,
                "uploaded_by":        None,
                "created_at":         None,   # filled at insert time
                "last_synced_at":     None,   # filled at insert time
                "is_active":          True,
                "language":           lang,
            },
        })
        i += CHUNK_SIZE - CHUNK_OVERLAP
        chunk_index += 1
    return chunks


def chunk_pages(
    pages: List[Dict],
    source: str,
    **kwargs,
) -> List[Dict]:
    """Chunk a page-aware list [{page_number, text}] preserving page_number per chunk.

    chunk_index is threaded as a running counter across ALL pages of this
    file — chunk_text() is called once per page, and without this it would
    restart at 0 for every page (the same class of bug fixed in
    chunk_pages_by_heading() below)."""
    all_chunks = []
    next_index = 0
    for page in pages:
        page_chunks = chunk_text(
            page["text"],
            source,
            page_number=page["page_number"],
            start_index=next_index,
            **kwargs,
        )
        # Vision+OCR pipeline provenance (ingestion/pdf_reader.py) —
        # present only for PDF pages; absent (no-op) for every other file
        # type, since read_file_pages() only sets these keys for PDFs.
        for c in page_chunks:
            if "extraction_method" in page:
                c["metadata"]["extraction_method"] = page.get("extraction_method")
                c["metadata"]["page_type"] = page.get("page_type")
                c["metadata"]["text_quality_score"] = page.get("text_quality_score")
                c["metadata"]["ocr_confidence"] = page.get("ocr_confidence")
                c["metadata"]["vision_used"] = page.get("vision_used")
                c["metadata"]["extraction_warnings"] = page.get("extraction_warnings")
                c["metadata"]["vision_trigger_reason"] = page.get("vision_trigger_reason")
        all_chunks.extend(page_chunks)
        next_index += len(page_chunks)
    return all_chunks


def read_file(file_path: Path) -> str:
    """อ่านไฟล์ทุกประเภท (backward compat — คืน plain text)"""
    ext = file_path.suffix.lower()
    if ext == ".pdf":
        return read_pdf(str(file_path))
    elif ext in (".docx", ".doc"):
        return read_word(str(file_path))
    elif ext in (".md", ".txt"):
        return file_path.read_text(encoding="utf-8")
    else:
        print(f"⚠️ ไม่รองรับ: {file_path.name}")
        return ""


_MARKDOWN_EXTS = {".pdf", ".docx", ".doc", ".pptx", ".ppt", ".html", ".htm", ".txt", ".md"}
_EXCEL_EXTS    = {".xlsx", ".xls", ".csv"}


def read_file_pages(file_path: Path, vision_ocr_profile: Optional[str] = None) -> List[Dict]:
    """อ่านไฟล์แล้วคืน [{page_number, text}] สำหรับ page-aware chunking.

    Excel → structured dual-path via excel_extractor.
    Everything else → MarkItDown Markdown conversion with fallback to original readers.

    vision_ocr_profile: explicit override for the PDF Vision/OCR pipeline's
    analysis profile ("disabled"/"basic"/"advanced"). None (the default,
    used by every caller except admin/routes.py's sync flow) falls back to
    config.VISION_OCR_ANALYSIS_PROFILE — unrelated to the AI Knowledge
    Analyzer's own "profile" concept in analyze_and_chunk() below.
    """
    ext = file_path.suffix.lower()

    # ── Excel / CSV: structured extractor → row-group pages ──────────
    if ext in _EXCEL_EXTS:
        try:
            wb = extract_csv(file_path) if ext == ".csv" else extract_workbook(file_path)
            pages = workbook_to_summary_pages(wb, file_name=file_path.name)
            if pages:
                # Attach workbook data on the first page so the sync loop
                # can store structured rows in excel_* tables.
                pages[0]["_workbook_data"] = wb
                return pages
        except Exception as e:
            print(f"[ingest] excel_extractor failed for {file_path.name}: {e}")
        # Fallback for .xlsx/.xls only
        if ext != ".csv":
            return read_excel_pages(str(file_path))
        return []

    # ── PDF: ALWAYS the per-page hybrid Vision+OCR pipeline, never
    # MarkItDown. MarkItDown collapses a whole PDF into ONE page/blob of
    # text, which is exactly what let a single corrupted page (broken
    # embedded-font mapping) poison the entire document with no way to
    # isolate which page was bad. Per-page processing is a hard
    # requirement for reliable table/infographic/scanned-page handling. ──
    if ext == ".pdf":
        if not vision_ocr_profile:
            from config import VISION_OCR_ANALYSIS_PROFILE
            vision_ocr_profile = VISION_OCR_ANALYSIS_PROFILE
        return read_pdf_pages(str(file_path), analysis_profile=vision_ocr_profile)

    # ── All other document types: MarkItDown → Markdown text ──────────
    if ext in _MARKDOWN_EXTS:
        md = convert_to_markdown(file_path)
        if md:
            return [{"page_number": 1, "text": md}]

        # Fallback per original reader if MarkItDown returns empty
        if ext in (".docx", ".doc"):
            return read_word_pages(str(file_path))
        elif ext in (".md", ".txt"):
            text = file_path.read_text(encoding="utf-8")
            return [{"page_number": 1, "text": text}] if text.strip() else []

    print(f"⚠️ ไม่รองรับ: {file_path.name}")
    return []


_STEP_SPLIT_RE = re.compile(r"(?m)^(?=\d+[.)]\s)")


def _split_into_steps(text: str) -> List[str]:
    parts = [p.strip() for p in _STEP_SPLIT_RE.split(text) if p.strip()]
    return parts if len(parts) > 1 else [text]


def _generate_section_suggested_question(heading: str, entity: str, file_level_suggested_qs: List[str]) -> Optional[str]:
    """A section-specific suggested question, so "Our Mission" doesn't
    display the whole file's first (generic) suggested question ("What
    is <entity>?") just because it was assigned identically to every
    chunk. Generic and pattern-based — never hardcodes any specific
    company/product name; `entity` and `heading` are whatever this
    document/section actually are.

    Prefers an existing file-level suggested question that already
    mentions this heading's own words (best case: the AI Knowledge
    Analyzer already asked something section-specific); otherwise
    synthesizes a plain, heading-shaped question.
    """
    if not heading:
        return file_level_suggested_qs[0] if file_level_suggested_qs else None

    heading_tokens = {t.lower() for t in re.findall(r"[a-zA-Z0-9]+", heading) if len(t) > 2}
    for q in file_level_suggested_qs or []:
        q_tokens = {t.lower() for t in re.findall(r"[a-zA-Z0-9]+", q)}
        if heading_tokens & q_tokens:
            return q

    h = heading.strip()
    h_lower = h.lower()
    entity = entity or "this"
    # Excel Q&A-style headings ("Question: What does the box look like?")
    # already ARE a question — use it verbatim instead of wrapping it in
    # an awkward "What is <entity>'s question: ...?" template.
    if h_lower.startswith("question:") or h.endswith("?"):
        return h.split(":", 1)[-1].strip() if ":" in h else h
    if h_lower.startswith("contact"):
        return f"How can I contact {entity}?"
    if h_lower.startswith("our "):
        return f"What is {entity}'s {h[4:].strip().lower()}?"
    if h_lower.startswith("about"):
        return f"What is {entity}?"
    return f"What is {entity}'s {h_lower}?"


def chunk_pages_by_heading(pages: List[Dict], analysis, source: str, **kwargs) -> List[Dict]:
    """Heading-aware chunking for knowledge_type/chunk_strategy combos that
    aren't a flat FAQ or an Excel dataset (Company Profile, Knowledge
    Article, SOP, Manual, Return Policy, Legal Document, Terms &
    Conditions, Promotion, Marketing Content — see
    services.knowledge_analyzer.CHUNK_STRATEGY_BY_TYPE).

    Splits by Markdown heading (preserving the full heading_path — "Manual
    > Setup > Step 3" style hierarchy — in each chunk's metadata) instead
    of blindly slicing by token count, which is what the generic
    chunk_pages() below does and is still used as the fallback for
    anything Mixed/Unknown. For step_sequence/procedure_hierarchy
    strategies (SOP/Manual), a section is further split at numbered-step
    boundaries so step order survives into metadata (step_index) instead
    of being flattened into one undifferentiated blob.

    This NEVER rewrites or drops the original text — every chunk's `text`
    field is still a verbatim slice of the source document; only the
    SPLIT POINTS and metadata differ from the generic chunker.
    """
    from services.knowledge_analyzer import detect_sections

    page_texts = [p.get("text", "") for p in pages]
    full_text = "\n\n".join(page_texts)
    sections = detect_sections(full_text)
    if not sections:
        return chunk_pages(pages, source, **kwargs)

    # Map each page's starting offset within full_text, so a section's true
    # originating page can be recovered by locating its text — detect_sections()
    # itself doesn't track offsets/page boundaries (it operates on one joined
    # blob), so page number would otherwise be lost (previously: incremented
    # once per detected SECTION instead of once per real page).
    page_starts = []
    cursor = 0
    for t in page_texts:
        page_starts.append(cursor)
        cursor += len(t) + 2  # + the "\n\n" separator

    def _page_for_offset(offset: int) -> int:
        page_idx = 0
        for i, start in enumerate(page_starts):
            if start <= offset:
                page_idx = i
            else:
                break
        return pages[page_idx].get("page_number", page_idx + 1)

    split_steps = analysis.chunk_strategy in ("step_sequence", "procedure_hierarchy")
    suggested_qs = getattr(analysis, "suggested_questions", None) or []
    knowledge_type = getattr(analysis, "knowledge_type", None)
    chunk_strategy = getattr(analysis, "chunk_strategy", None)

    entity = kwargs.get("document_title") or Path(source).stem
    pages_by_number = {p.get("page_number"): p for p in pages}

    all_chunks: List[Dict] = []
    search_cursor = 0
    next_index = 0  # global running counter across ALL sections/steps of this file — the confirmed chunk_index=0 fix
    for sec in sections:
        if not sec.text.strip():
            continue
        # Locate this section's true page by finding where its body text
        # actually sits in the joined blob (monotonic search cursor handles
        # duplicate/repeated text across sections).
        found_at = full_text.find(sec.text, search_cursor)
        if found_at == -1:
            found_at = full_text.find(sec.text)
        page_num = _page_for_offset(found_at) if found_at != -1 else _page_for_offset(search_cursor)
        if found_at != -1:
            search_cursor = found_at + len(sec.text)

        pieces = _split_into_steps(sec.text) if split_steps else [sec.text]
        # Section-specific suggested question — generated ONCE per section
        # (not per chunk-within-section, and never the whole file's first
        # question copy-pasted onto every chunk regardless of what that
        # chunk is actually about).
        section_question = _generate_section_suggested_question(sec.heading, entity, suggested_qs)
        source_page = pages_by_number.get(page_num, {})
        for step_index, piece in enumerate(pieces):
            piece_chunks = chunk_text(piece, source, page_number=page_num, start_index=next_index, **kwargs)
            next_index += len(piece_chunks)
            for c in piece_chunks:
                c["metadata"]["section_title"] = sec.heading or c["metadata"].get("section_title")
                c["metadata"]["heading_path"] = sec.heading_path
                c["metadata"]["knowledge_type"] = knowledge_type
                c["metadata"]["chunk_strategy"] = chunk_strategy
                if split_steps and len(pieces) > 1:
                    c["metadata"]["step_index"] = step_index
                if section_question:
                    c["metadata"]["suggested_questions"] = [section_question]
                # Vision+OCR pipeline provenance (see chunk_pages() above) —
                # present only for PDF pages routed through the new pipeline.
                if "extraction_method" in source_page:
                    c["metadata"]["extraction_method"] = source_page.get("extraction_method")
                    c["metadata"]["page_type"] = source_page.get("page_type")
                    c["metadata"]["text_quality_score"] = source_page.get("text_quality_score")
                    c["metadata"]["ocr_confidence"] = source_page.get("ocr_confidence")
                    c["metadata"]["vision_used"] = source_page.get("vision_used")
                    c["metadata"]["extraction_warnings"] = source_page.get("extraction_warnings")
                    c["metadata"]["vision_trigger_reason"] = source_page.get("vision_trigger_reason")
            all_chunks.extend(piece_chunks)
    return all_chunks


def analyze_and_chunk(file_path: Path, source: str, knowledge_analysis_profile: Optional[str] = None,
                       vision_ocr_profile: Optional[str] = None, **chunk_kwargs):
    """The new pipeline entry point: Extract -> AI Knowledge Analyzer ->
    Chunk Strategy Selection -> Chunking, in one call. Returns
    (analysis, chunks, pages) — `pages` is returned too since callers
    (the sync loop) still need `_workbook_data` off it for the Excel
    structured-storage path, unchanged from before this feature existed.

    knowledge_type/chunk_strategy decide ONLY how the text is split and
    what metadata gets attached — never what the text IS. The Excel Q&A
    (faq_qa) and Excel Dataset/Financial Report (excel_engine) strategies
    intentionally reuse the existing chunk_pages() path unchanged, since
    those already have correct, purpose-built handling (one page per Q&A
    row / summary-only page respectively) that predates this feature and
    must not regress.

    knowledge_analysis_profile and vision_ocr_profile are DELIBERATELY
    separate parameters — they used to share the generic name "profile"
    here, which is exactly how a real Admin sync could pass "advanced" and
    have it silently apply ONLY to the Knowledge Analyzer below while
    Vision/OCR (read_file_pages' PDF branch) fell back to
    config.VISION_OCR_ANALYSIS_PROFILE's "basic" default, uncontrolled.
    """
    from services.knowledge_analyzer import get_knowledge_analyzer

    pages = read_file_pages(file_path, vision_ocr_profile=vision_ocr_profile)
    if not pages:
        return None, [], pages

    # Full Auto Import failsafe: KnowledgeAnalyzerService already degrades
    # gracefully internally (fallback classifier, empty Knowledge Graph) on
    # LLM failure — but if the analyzer call itself raises (a genuine bug,
    # not just "no API key"), the import must still continue rather than
    # fail outright. Falls back to the traditional Extract -> Chunk ->
    # Embed pipeline (plain chunk_pages(), same as before this feature
    # existed) with analysis=None, which every downstream caller already
    # treats as "no AI analysis available for this file".
    try:
        analysis = get_knowledge_analyzer().analyze(pages, file_path.name, profile=knowledge_analysis_profile)
    except Exception as exc:
        print(f"[analyze_and_chunk] AI Knowledge Analyzer crashed, falling back to standard "
              f"RAG ingestion for {file_path.name}: {exc}")
        chunks = chunk_pages(pages, source, **chunk_kwargs)
        _stamp_profile_metadata(chunks, knowledge_analysis_profile, vision_ocr_profile)
        _stamp_purpose_metadata(chunks, file_path.name, chunk_strategy=None)
        return None, chunks, pages

    if analysis.chunk_strategy in ("faq_qa", "excel_engine"):
        chunks = chunk_pages(pages, source, **chunk_kwargs)
    else:
        try:
            chunks = chunk_pages_by_heading(pages, analysis, source, **chunk_kwargs)
        except Exception as exc:
            print(f"[analyze_and_chunk] heading-based chunking crashed, falling back to standard "
                  f"chunker for {file_path.name}: {exc}")
            chunks = chunk_pages(pages, source, **chunk_kwargs)

    _stamp_profile_metadata(chunks, knowledge_analysis_profile, vision_ocr_profile)
    _stamp_purpose_metadata(chunks, file_path.name, chunk_strategy=analysis.chunk_strategy)
    return analysis, chunks, pages


def _stamp_purpose_metadata(chunks: List[Dict], filename: str, chunk_strategy: Optional[str]) -> None:
    """Classifies this FILE's document_purpose once (services/
    document_purpose.py — generic, deterministic, no LLM) from its
    filename + the headings/section titles its own chunks already carry
    + a text sample, then stamps it onto every chunk. Also stamps each
    chunk's own content_signals (per-chunk keyword-group hits) so a
    single ambiguous file's chunks can still individually read as
    "coverage-like" or "premium-like" content.

    This is what lets rag/hybrid_scoring.py's purpose-aware boost prefer
    a coverage brochure over a premium-rate table (or vice versa) for a
    same-vocabulary ("Plan 1-4") query — see that module's
    apply_hybrid_ranking() for how the boost is applied (never a hard
    filter)."""
    if not chunks:
        return
    from services.document_purpose import classify_document_purpose, detect_table_semantic_labels

    headings = []
    for c in chunks:
        h = c["metadata"].get("section_title")
        if h and h not in headings:
            headings.append(h)
    sample_text = "\n".join(c.get("text", "") for c in chunks[:5])

    purpose = classify_document_purpose(
        filename=filename, headings=headings, sample_text=sample_text, chunk_strategy=chunk_strategy,
    )
    for c in chunks:
        c["metadata"]["document_purpose"] = purpose
        c["metadata"]["content_signals"] = detect_table_semantic_labels(c.get("text", ""))


def _stamp_profile_metadata(chunks: List[Dict], knowledge_analysis_profile: Optional[str],
                             vision_ocr_profile: Optional[str]) -> None:
    """Records which profile actually produced each chunk — makes it
    unambiguous during debugging which Vision/OCR mode a given chunk came
    from, without needing to cross-reference the sync job log."""
    for c in chunks:
        c["metadata"]["knowledge_analysis_profile"] = knowledge_analysis_profile
        c["metadata"]["vision_ocr_profile"] = vision_ocr_profile


def load_all_documents() -> List[Dict]:
    """โหลดทุกไฟล์ใน knowledge/ folder"""
    all_chunks = []
    files = list(KNOWLEDGE_DIR.rglob("*"))
    supported = [f for f in files if f.suffix.lower() in {".pdf", ".docx", ".md", ".txt", ".xlsx", ".xls", ".csv"}]
    print(f"📂 พบ {len(supported)} ไฟล์")
    for file_path in supported:
        print(f"  📄 อ่าน {file_path.name}...")
        pages = read_file_pages(file_path)
        if pages:
            chunks = chunk_pages(pages, source=file_path.name)
            all_chunks.extend(chunks)
            print(f"     ✅ {len(chunks)} chunks")
        else:
            print(f"     ⚠️ ไม่มีข้อความ")
    print(f"\n✅ รวม {len(all_chunks)} chunks พร้อม embed")
    return all_chunks
