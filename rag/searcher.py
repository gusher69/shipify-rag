import re
from typing import List, Dict, Optional
from supabase import create_client

from config import SUPABASE_URL, SUPABASE_KEY, EMBEDDING_MODEL, TOP_K, RAG_RAW_CANDIDATE_COUNT

_supabase = None


def _get_model():
    """Kept for backward compatibility with any direct caller/test — the
    real embedding call inside search() below goes through
    services/embedding_service.py's provider abstraction (Part 4), which
    is what guarantees query and document embeddings always use the same
    provider/model/dimensions."""
    from services.embedding_service import get_embedding_provider
    provider = get_embedding_provider()
    if hasattr(provider, "_get_model"):
        return provider._get_model()
    raise RuntimeError("_get_model() is only meaningful for the local embedding provider")


def _get_supabase():
    global _supabase
    if _supabase is None:
        _supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _supabase


_allowed_file_ids_cache: Dict[str, "tuple[float, set]"] = {}
_ALLOWED_FILE_IDS_CACHE_TTL_SECONDS = 30.0


def _resolve_allowed_file_ids(collection_ids: Optional[List[str]]) -> Optional[set]:
    """Knowledge Collections (Phase 3.5, 2026-08-05) — returns the set of
    knowledge_files.id values retrieval is allowed to draw from, or None
    to mean "no restriction" (only ever returned if Collections aren't
    configured at all, e.g. migration 035 hasn't been applied yet).

    `collection_ids=None` (every existing caller, unchanged) resolves to
    the DEFAULT collection (knowledge_collections.is_default=True) — this
    is deliberately NOT "no filtering": before Collections existed, every
    uploaded file was searchable together regardless of source project,
    which is exactly the confirmed contamination this feature exists to
    fix (a ZWIZ.AI vendor demo file was reproducibly retrieved for
    Shipify customer questions — see the 2026-08-05 incident trace).
    Passing an explicit collection_ids list (e.g. from the AI Playground's
    collection picker) scopes to exactly those collections instead.

    Cached briefly (30s) since this runs on every retrieval call and the
    file->collection mapping changes rarely."""
    import time as _time
    cache_key = ",".join(sorted(collection_ids)) if collection_ids else "__default__"
    cached = _allowed_file_ids_cache.get(cache_key)
    if cached and (_time.time() - cached[0]) < _ALLOWED_FILE_IDS_CACHE_TTL_SECONDS:
        return cached[1]

    sb = _get_supabase()
    try:
        if collection_ids:
            ids = collection_ids
        else:
            default_res = sb.table("knowledge_collections").select("id").eq("is_default", True) \
                .is_("deleted_at", "null").limit(1).execute()
            if not default_res.data:
                return None  # Collections not configured — no restriction (pre-migration-035 behavior)
            ids = [default_res.data[0]["id"]]

        files_res = sb.table("knowledge_files").select("id").in_("collection_id", ids).execute()
        allowed = {f["id"] for f in (files_res.data or [])}
    except Exception as e:
        print(f"[searcher] _resolve_allowed_file_ids failed, no restriction applied: {e}")
        return None

    _allowed_file_ids_cache[cache_key] = (_time.time(), allowed)
    return allowed


# ── Analytical query detection ────────────────────────────────

_ANALYTICAL_PATTERN = re.compile(
    r"(ยอด|รวม|เปรียบเทียบ|สรุป|คำนวณ|เฉลี่ย|มากที่สุด|น้อยที่สุด|จำนวน|นับ|"
    r"total|sum|average|avg|count|compare|top\s*\d+|bottom\s*\d+|"
    r"how many|how much|highest|lowest|maximum|minimum|breakdown|aggregate)",
    re.IGNORECASE,
)


def is_analytical(question: str) -> bool:
    """Return True when the question is likely asking for aggregation/calculation.

    Public (no leading underscore) — services/playground_orchestrator.py
    reuses this exact check for its "Intent Detection" pipeline stage
    display, so the Playground can never show a different intent than the
    one search() actually acts on.
    """
    return bool(_ANALYTICAL_PATTERN.search(question))


_is_analytical = is_analytical  # internal alias, kept for any other in-module references


# ── Broad summary / overview intent detection ─────────────────
# Deliberately separate from is_analytical()/_ANALYTICAL_PATTERN above:
# "สรุป" alone means BOTH "summarize/overview" (narrative) and "total/sum"
# (numeric aggregation) in Thai, but they need opposite retrieval
# behavior — a numeric question needs the Excel calculation/structured
# path, a narrative summary needs MORE FAQ/company chunks merged, never
# the whole-sheet Excel path. This pattern is generic (no entity/company
# name), only broadened per the customer-demo P0 fix spec.
_BROAD_SUMMARY_PATTERN = re.compile(
    r"สรุป|ภาพรวม|มีอะไรบ้าง|มีบริการอะไร|"
    r"\bsummary\b|\boverview\b|\bmission\b|\bvision\b",
    re.IGNORECASE,
)


def is_broad_summary_query(question: str) -> bool:
    """True for a narrative "summarize/overview" style question (สรุปข้อมูล
    บริษัท, ภาพรวมบริษัท, มีบริการอะไรบ้าง, company overview, mission/vision)
    — used to widen how many chunks reach the LLM so it can merge multiple
    FAQ rows into one summary instead of only answering from a single
    chunk. Independent of is_analytical(): a question can be BOTH (rare)
    or neither; this never affects the Excel calculation/structured path."""
    return bool(_BROAD_SUMMARY_PATTERN.search(question))


# ── Structured Excel search (SQL/JSONB path) ──────────────────

def search_excel_structured(question: str) -> List[Dict]:
    """Fetch relevant Excel rows from Supabase and build a text summary.

    Strategy:
    1. Find sheets whose markdown_preview semantically matches the question.
    2. Load all rows for matching sheets.
    3. Summarise numeric columns with basic aggregates (sum, min, max, count).
    """
    try:
        sb = _get_supabase()

        # Get all sheets that have data
        sheets_res = sb.table("excel_sheets").select(
            "id,sheet_name,headers,numeric_columns,row_count,markdown_preview,workbook_id"
        ).gt("row_count", 0).execute()

        sheets = sheets_res.data or []
        if not sheets:
            return []

        results = []
        for sheet in sheets:
            sheet_id = sheet["id"]
            sheet_name = sheet["sheet_name"]
            numeric_cols = sheet.get("numeric_columns") or []
            headers = sheet.get("headers") or []
            row_count = sheet.get("row_count", 0)

            # A sheet with no numeric columns has nothing this function can
            # aggregate — the whole point of search_excel_structured() is a
            # numeric summary (sum/avg/min/max). Without this guard, EVERY
            # sheet (including config/instruction/schema sheets like
            # "Recommend" or a RAG_Knowledge schema sheet) got a whole-sheet
            # chunk here with a hardcoded score=1.0/is_structured=True,
            # which apply_hybrid_ranking() (rag/hybrid_scoring.py) never
            # rescores or drops and evidence_classifier.py automatically
            # treats as DIRECT_EVIDENCE — so a non-numeric config sheet
            # could outrank and displace genuine FAQ evidence for ANY
            # question containing an analytical trigger word (e.g. "สรุป"
            # in is_analytical()'s pattern, which also means "summarize").
            # This is generic — no sheet name is ever hardcoded here.
            if not numeric_cols:
                continue

            # Pull all rows for this sheet
            rows_res = sb.table("excel_rows").select("row_data").eq("sheet_id", sheet_id).execute()
            rows = rows_res.data or []
            if not rows:
                continue

            # Build aggregate summary for numeric columns
            agg_lines = []
            for col in numeric_cols:
                values = []
                for row in rows:
                    val = (row.get("row_data") or {}).get(col)
                    if val is not None:
                        try:
                            values.append(float(val))
                        except (TypeError, ValueError):
                            pass
                if values:
                    agg_lines.append(
                        f"**{col}**: count={len(values)}, "
                        f"sum={sum(values):,.2f}, "
                        f"avg={sum(values)/len(values):,.2f}, "
                        f"min={min(values):,.2f}, max={max(values):,.2f}"
                    )

            # Build sample rows text (first 10)
            sample_rows = []
            for row in rows[:10]:
                rd = row.get("row_data") or {}
                sample_rows.append(" | ".join(str(rd.get(h, "")) for h in headers))

            text_parts = [f"## Excel Sheet: {sheet_name}", f"Total rows: {row_count}"]
            if agg_lines:
                text_parts.append("\n### Aggregates\n" + "\n".join(agg_lines))
            if sample_rows:
                text_parts.append(
                    "\n### Sample rows\n| " + " | ".join(headers) + " |\n"
                    + "| " + " | ".join("---" for _ in headers) + " |\n"
                    + "\n".join("| " + r + " |" for r in sample_rows)
                )

            results.append({
                "text":          "\n".join(text_parts),
                "source":        sheet_name,
                "intent":        "analytics",
                "score":         1.0,
                "citation":      f"Source: {sheet_name} (Excel structured data)",
                "file_name":     sheet_name,
                "page_number":   None,
                "section_title": sheet_name,
                "chunk_index":   0,
                "version":       None,
                "category":      "excel",
                "language":      "th",
                "is_structured": True,
            })

        return results

    except Exception as e:
        print(f"[searcher] search_excel_structured failed: {e}")
        return []


# ── Main search entry point ───────────────────────────────────

def _fetch_attachments_for_chunks(chunk_ids: List[str], file_ids: List[str]) -> Dict[str, List[Dict]]:
    """Return {chunk_id: [attachment, ...]} for a set of chunk + file IDs.

    Falls back to file-level lookup when chunk_id is NULL on the attachment record.
    """
    from ingestion.attachment_handler import get_attachments_for_chunks
    sb = _get_supabase()
    mapping = get_attachments_for_chunks(sb, chunk_ids)

    # Also pull file-level attachments (where chunk_id is NULL) and attach to any
    # chunk that came from the same file.
    if file_ids:
        try:
            ids_str = "(" + ",".join(set(file_ids)) + ")"
            res = sb.table("knowledge_attachments")\
                .select("knowledge_file_id,filename,public_url,storage_path,storage_provider,mime_type,attachment_type,metadata,status,chunk_id")\
                .filter("knowledge_file_id", "in", ids_str)\
                .is_("chunk_id", "null")\
                .is_("deleted_at", "null")\
                .neq("status", "missing")\
                .execute()
            # Group by file_id so we can fan-out to chunks
            file_att: Dict[str, List[Dict]] = {}
            for row in (res.data or []):
                file_att.setdefault(row["knowledge_file_id"], []).append(row)
            # Assign file-level attachments to the chunk_id key (use the chunk itself as key)
            # We piggyback: store under file_id key so callers can look up by either
            for fid, atts in file_att.items():
                mapping.setdefault(f"__file__{fid}", []).extend(atts)
        except Exception as exc:
            print(f"[searcher] file-level attachment fetch failed: {exc}")

    return mapping


def _trace_stage(trace, name, status, t0, detail=""):
    if trace is None:
        return
    import time as _time
    trace.append({"stage": name, "status": status, "duration_ms": round((_time.time() - t0) * 1000, 1), "detail": detail})


def search(question: str, top_k: int = TOP_K, trace: Optional[list] = None,
           excluded_terms: Optional[List[str]] = None,
           actionable_intent: Optional[str] = None,
           original_question: Optional[str] = None,
           collection_ids: Optional[List[str]] = None) -> List[Dict]:
    """Search knowledge base. Analytical questions also query structured Excel data.

    Each result may include an 'attachments' key: list of {filename, public_url, mime_type}.

    trace, if given a list, gets one {"stage","status","duration_ms","detail"}
    entry appended per pipeline stage — used by the AI Playground's Pipeline
    tab. Passing None (the default, used by every existing caller) is a
    complete no-op — zero behavior/performance change for LINE OA etc.

    excluded_terms, if given (rag/query_resolution.py's excluded_entities,
    flattened by services/playground_orchestrator.py), down-ranks — never
    hard-removes — chunks containing these terms; see
    rag/hybrid_scoring.py::apply_hybrid_ranking.

    actionable_intent, if given (rag/query_understanding.py::
    classify_actionable_intent — computed by the caller BEFORE retrieval,
    since it needs conversation-merged entities searcher.py doesn't have),
    enables the company_overview/company_summary negative-boost tier in
    rag/hybrid_scoring.py::compute_company_intent_boost. None (the
    default, used by every existing caller including LINE OA) is a
    complete no-op for this — only the broader company_intent expansion/
    positive-boost behavior (detected locally via detect_intent) applies.
    """
    import time as _time
    chunks: List[Dict] = []
    chunk_ids: List[str] = []
    file_ids: List[str] = []

    # Phase 2: query expansion runs before vector search so both the
    # embedding call AND the lexical-search merge step (below) can use the
    # same variants. Level 1 (deterministic glossary) always runs; Level 2
    # (LLM rewrite) only if config.RAG_QUERY_REWRITE_ENABLED is set.
    from services.retrieval_settings import get_active_settings
    retrieval_settings = get_active_settings()

    t0 = _time.time()
    from rag.query_expansion import rewrite_query_with_llm
    # Phase 1 of the AI Playground Intelligence Pipeline (rag/
    # query_understanding.py): normalize -> detect intent -> rewrite ->
    # expand, all deterministic/no-LLM. understand_query() wraps the
    # existing expand_query() (Level 1 glossary, unchanged contract —
    # variants[0] is always the exact literal `question`) and adds one
    # more variant (the rewritten query) plus normalized_query/
    # detected_intent for the Explainability tab.
    from rag.query_understanding import understand_query
    if retrieval_settings.query_expansion_enabled:
        query_expansion_debug = understand_query(question)
        query_variants = query_expansion_debug["expanded_queries"]
    else:
        query_variants = [question]
        query_expansion_debug = {"original_query": question, "normalized_query": question,
                                  "detected_intent": "unknown", "rewritten_query": question,
                                  "expanded_queries": query_variants, "detected_language": "unknown"}
    # rewrite_query_with_llm() internally no-ops (returns []) unless
    # config.RAG_QUERY_REWRITE_ENABLED is set — always safe to call.
    if retrieval_settings.query_expansion_enabled:
        for v in rewrite_query_with_llm(question):
            if v not in query_variants:
                query_variants.append(v)
    # Knowledge Synonym Engine (rag/synonym_service.py) — expands the
    # query with domain synonym groups (พิกัด/โลเคชั่น/location/map/...,
    # โกดัง/warehouse/..., เรท/ราคา/rate/..., etc.) BEFORE embedding/hybrid
    # ranking, same as the Thai-English glossary above. Purely additive:
    # `question` stays variants[0], and if no synonym group matches at
    # all, query_variants/query_expansion_debug are untouched — the
    # pipeline behaves exactly as it did before this feature existed.
    t0 = _time.time()
    from rag.synonym_service import expand_query_with_synonyms
    synonym_result = expand_query_with_synonyms(question)
    synonym_added = 0
    for v in synonym_result["variants"][1:]:
        if v not in query_variants:
            query_variants.append(v)
            synonym_added += 1
    query_expansion_debug["expanded_queries"] = query_variants
    query_expansion_debug["canonical_terms"] = synonym_result["canonical_terms"]
    query_expansion_debug["synonyms_used"] = synonym_result["synonyms_used"]
    _trace_stage(trace, "synonym_expansion", "success", t0,
                 f"{synonym_added} synonym variant(s) added"
                 + (f" (canonical terms: {', '.join(synonym_result['canonical_terms'])})"
                    if synonym_result["canonical_terms"] else ""))

    # Company-overview retrieval fix (P0) — when the question's detected
    # intent is "company" (rag/query_understanding.py::detect_intent),
    # add the fixed company/business/import/shipping expansion terms so
    # keyword/heading scoring also matches the FAQ rows that describe the
    # company's actual core business, not just rows that happen to say
    # "บริษัท" literally.
    from rag.query_understanding import expand_company_intent_terms
    company_intent = query_expansion_debug.get("detected_intent") == "company"
    for v in expand_company_intent_terms(question):
        if v not in query_variants:
            query_variants.append(v)

    query_expansion_debug["expanded_queries"] = query_variants
    _trace_stage(trace, "query_expansion", "success", t0,
                 f"{len(query_variants)} variant(s): {', '.join(query_variants)}")
    if trace is not None:
        trace.append({"stage": "query_expansion_detail", "status": "info", "duration_ms": 0.0,
                       "detail": "", "query_expansion": query_expansion_debug})

    # Structured FAQ exact/near-exact match (rag/faq_matcher.py) — when
    # the current question is an exact or very close match to one
    # knowledge_items row's Question or an Alternative Question, that row
    # IS the answer: send ONLY its canonical Answer (plus attachments) to
    # the LLM rather than mixing it into a generic vector/lexical/hybrid
    # candidate pool where an unrelated row could still sneak in. This
    # short-circuits the rest of search() entirely — no vector search, no
    # lexical search, no Excel engine, no hybrid rerank — since there is
    # nothing left to rank against a confirmed exact FAQ row.
    t0 = _time.time()
    try:
        from rag.faq_matcher import match_faq_exact
        # query_variants already includes the synonym-expanded variants
        # computed above — the original `question` is still checked
        # first inside match_faq_exact, so this only ENRICHES FAQ exact
        # matching (e.g. catching a row phrased with a synonym), never
        # replaces the user's own wording.
        #
        # Direct FAQ Fidelity (P0, 2026-07-21) — `question` here may
        # already be a CANONICAL REWRITE (rag/canonical_query.py), not
        # the customer's actual wording (e.g. "ขอที่อยู่โกดังจีน" ->
        # "ขอที่อยู่และแผนที่โกดังจีน") — an approved FAQ's Question field
        # is written against the customer's ORIGINAL phrasing, so a
        # rewrite can silently break an otherwise-exact match. `Part 5/6
        # of the Safe Query Understanding Framework: "the exact original
        # match must not be lost because of a rewrite" — original_question,
        # when given, is tried as an equally-first-class exact-match
        # candidate, never subordinate to the canonical rewrite.
        extra = list(query_variants)
        if original_question and original_question not in extra and original_question != question:
            extra.append(original_question)
        faq_match = match_faq_exact(question, extra_queries=extra)
        if not faq_match and original_question and original_question != question:
            faq_match = match_faq_exact(original_question, extra_queries=query_variants)
    except Exception as e:
        print(f"[searcher] faq_matcher failed: {e}")
        faq_match = None

    if faq_match:
        row = faq_match["row"]
        faq_chunk_id = row.get("chunk_id")
        faq_file_id = row.get("knowledge_file_id")
        faq_chunk = {
            "text":          f"Question: {row.get('question')}\nAnswer: {row.get('answer')}",
            "source":        row.get("sheet_name") or "FAQ",
            "intent":        "faq_exact",
            "score":         1.0,
            "citation":      f"Source: {row.get('sheet_name', 'FAQ')} row {row.get('row_index')}",
            "file_name":     row.get("sheet_name"),
            "file_id":       faq_file_id,
            "chunk_id":      faq_chunk_id,
            "page_number":   None,
            "section_title": row.get("question"),
            "chunk_index":   row.get("row_index"),
            "version":       None,
            "category":      "faq",
            "language":      None,
            "is_structured": True,
            "is_faq_exact":  True,
            "faq_match_type": faq_match["match_type"],
            "faq_matched_text": faq_match["matched_text"],
            "attachments":   [],
            # This chunk never goes through rag/hybrid_scoring.py's
            # apply_hybrid_ranking() (the short-circuit skips it entirely
            # — see hybrid_rerank's "skipped" trace below), so these
            # fields must be set here directly — otherwise rag/confidence.py's
            # answerability classification (which keys off `classification`)
            # and the escalation guard in services/playground_orchestrator.py
            # would never see this as direct evidence. Uses "direct_evidence"
            # (not the generic "structured_deterministic" used for aggregate
            # Excel summaries) — a confirmed exact/near-exact FAQ row IS a
            # direct answer, so confidence must reflect that, per requirement
            # 5 ("Confidence must be based on the current FAQ match only").
            "keyword_score": None, "heading_score": None, "graph_score": None,
            "normalized_vector_score": None, "raw_vector_similarity": 1.0,
            "classification": "direct_evidence", "evidence_label": "direct_keyword",
            "hybrid_score": 1.0, "matched_query": question, "matched_heading": None,
        }
        _trace_stage(trace, "faq_exact_match", "success", t0,
                     f"{faq_match['match_type']} match on row {row.get('row_index')} "
                     f"in {row.get('sheet_name')} (matched: {faq_match['matched_text']!r})")
        try:
            att_map = _fetch_attachments_for_chunks(
                [faq_chunk_id] if faq_chunk_id else [], [faq_file_id] if faq_file_id else [])
            atts = (att_map.get(faq_chunk_id) if faq_chunk_id else None) \
                or (att_map.get(f"__file__{faq_file_id}") if faq_file_id else None) or []
            for att in atts:
                att["download_url"] = att.get("public_url")
                att["preview_url"] = (att.get("metadata") or {}).get("preview_url")
            faq_chunk["attachments"] = atts
            _trace_stage(trace, "attachment_resolver", "success", t0, f"{len(atts)} attachment(s)")
        except Exception as exc:
            print(f"[searcher] faq attachment enrichment failed: {exc}")
            _trace_stage(trace, "attachment_resolver", "failed", t0, str(exc))
        _trace_stage(trace, "embedding_vector_search", "skipped", t0, "exact FAQ row match short-circuits vector search")
        _trace_stage(trace, "lexical_search", "skipped", t0, "exact FAQ row match short-circuits lexical search")
        _trace_stage(trace, "excel_engine", "skipped", t0, "exact FAQ row match short-circuits excel engine")
        _trace_stage(trace, "hybrid_rerank", "skipped", t0,
                     "exact FAQ row match — single canonical row returned, no unrelated rows mixed in")
        return [faq_chunk]
    else:
        _trace_stage(trace, "faq_exact_match", "success", t0, "no exact/near-exact FAQ row match")

    # Vector search — fetches a LARGE raw candidate pool
    # (retrieval_settings.candidate_top_k), independent of `top_k` (the
    # FINAL number of chunks returned after hybrid reranking below). This
    # is what lets a chunk with weak vector similarity but strong keyword/
    # heading evidence survive: reranking can only work with what actually
    # got fetched, and the audit found the correct chunk ranking #12-14
    # out of 20 by raw vector score alone, well outside a top_k=3 fetch.
    raw_candidate_count = max(retrieval_settings.candidate_top_k, RAG_RAW_CANDIDATE_COUNT, top_k)
    t0 = _time.time()
    try:
        from services.embedding_service import get_embedding_provider
        query_vector = get_embedding_provider().embed_query(question)

        result = _get_supabase().rpc("match_knowledge_chunks", {
            "query_embedding": query_vector,
            "match_count":     raw_candidate_count,
        }).execute()

        # Knowledge Collections (Phase 3.5) — resolved once per call, not
        # per-row; None means "no restriction" (Collections not
        # configured), a real set means "only these file_ids may surface".
        allowed_file_ids = _resolve_allowed_file_ids(collection_ids)

        for r in result.data:
            if r.get("is_active") is False:
                continue

            meta = r.get("metadata") or {}
            chunk_id = r.get("id") or meta.get("chunk_id")
            file_id  = r.get("file_id") or meta.get("file_id")

            if allowed_file_ids is not None and file_id not in allowed_file_ids:
                continue

            parts = [meta.get("file_name") or r.get("source") or "unknown"]
            if meta.get("page_number"):
                parts.append(f"page {meta['page_number']}")
            if meta.get("section_title"):
                parts.append(f"section {meta['section_title']}")
            citation = ", ".join(parts)

            if chunk_id:
                chunk_ids.append(chunk_id)
            if file_id:
                file_ids.append(file_id)

            chunks.append({
                "text":          r["content"],
                "source":        r.get("source") or meta.get("file_name", ""),
                "intent":        r.get("intent", ""),
                "score":         round(r["similarity"], 4),
                "citation":      f"Source: {citation}",
                "file_name":     meta.get("file_name"),
                "file_id":       file_id,
                "chunk_id":      chunk_id,
                "page_number":   meta.get("page_number"),
                "section_title": meta.get("section_title"),
                "chunk_index":   meta.get("chunk_index"),
                "version":       meta.get("version"),
                "category":      meta.get("category"),
                "language":      meta.get("language"),
                "is_structured": False,
                "attachments":   [],
                # AI Knowledge Analyzer output (services/knowledge_analyzer.py)
                # — retrieval-enhancement metadata attached at chunk time by
                # ingestion/ingest.py's chunk_pages_by_heading(). Additive
                # only; "text" above is always the verbatim original chunk.
                "knowledge_type":  meta.get("knowledge_type"),
                "chunk_strategy":  meta.get("chunk_strategy"),
                "heading_path":    meta.get("heading_path"),
                "step_index":      meta.get("step_index"),
                "suggested_questions": meta.get("suggested_questions"),
                # Per-chunk embedding provenance (set at ingest time by
                # ingestion/embedder.upsert_chunks) — lets Developer Mode
                # show which model created THIS chunk, instead of relying
                # on a global header that can silently drift from what's
                # actually in the DB (see admin/routes.py's playground_status
                # bug this was written to prevent from recurring).
                "embedding_provider":   meta.get("embedding_provider"),
                "embedding_model":      meta.get("embedding_model"),
                "embedding_version":    meta.get("embedding_version"),
                "embedding_dimensions": meta.get("embedding_dimensions"),
                # Vision+OCR ingestion provenance (ingestion/pdf_reader.py /
                # services/pdf_page_pipeline.py) — present only for chunks
                # from a PDF page; None for every other source.
                "extraction_method":    meta.get("extraction_method"),
                "page_type":            meta.get("page_type"),
                "text_quality_score":   meta.get("text_quality_score"),
                "ocr_confidence":       meta.get("ocr_confidence"),
                "vision_used":          meta.get("vision_used"),
                "extraction_warnings":  meta.get("extraction_warnings"),
                "vision_trigger_reason": meta.get("vision_trigger_reason"),
                # Document-purpose / content-signal metadata (services/
                # document_purpose.py, stamped at ingest time) — used by
                # rag/hybrid_scoring.py's purpose-aware ranking boost.
                "document_purpose": meta.get("document_purpose"),
                "content_signals": meta.get("content_signals"),
            })

        _trace_stage(trace, "embedding_vector_search", "success", t0, f"{len(chunks)} candidate(s)")
    except Exception as e:
        print(f"Search failed: {e}")
        _trace_stage(trace, "embedding_vector_search", "failed", t0, str(e))

    # Parallel lexical retrieval (rag/lexical_search.py, Part 7) — finds
    # chunks by keyword/heading match that the vector search may have
    # missed entirely (e.g. a chunk whose vector embedding sits far from
    # the query but whose heading is an exact/near-exact match). Merged by
    # chunk_id — a chunk already present from vector search is never
    # duplicated, just left with its real vector score.
    t0 = _time.time()
    try:
        from rag.lexical_search import lexical_search
        existing_ids = {c.get("chunk_id") for c in chunks if c.get("chunk_id")}
        lexical_candidates = lexical_search(query_variants, limit=raw_candidate_count)
        merged_in = 0
        for lc in lexical_candidates:
            # Same Knowledge Collections restriction as the vector-search
            # loop above — lexical search is a fully separate retrieval
            # path over the same knowledge_chunks table, so a file
            # excluded from the vector loop would otherwise leak back in
            # here (confirmed live: this exact path is what surfaced the
            # ZWIZ.AI vendor file for "แพ็กเกจ"-containing questions before
            # this filter was added).
            if allowed_file_ids is not None and lc.get("file_id") not in allowed_file_ids:
                continue
            if lc.get("chunk_id") and lc["chunk_id"] not in existing_ids:
                chunks.append(lc)
                existing_ids.add(lc["chunk_id"])
                merged_in += 1
        _trace_stage(trace, "lexical_search", "success", t0,
                     f"{len(lexical_candidates)} lexical hit(s), {merged_in} new after dedup")
    except Exception as e:
        print(f"[searcher] lexical_search failed: {e}")
        _trace_stage(trace, "lexical_search", "failed", t0, str(e))

    # For analytical/numeric questions, try a DETERMINISTIC calculation first
    # — the actual arithmetic runs in Python against excel_rows, never as the
    # LLM reading a context dump and doing mental math. Only if that finds no
    # confident target do we fall back to the generic aggregate summary.
    t0 = _time.time()
    if _is_analytical(question):
        calculated = None
        try:
            from rag.calculator import answer_calculation_question
            calculated = answer_calculation_question(question)
        except Exception as e:
            print(f"[searcher] calculation tool failed: {e}")

        prepend = []
        if calculated is not None:
            # calculated["ok"] is False when a sheet was identified but the
            # calculation couldn't complete (missing data, no matching
            # rows). That must be surfaced as "cannot calculate," never
            # silently dropped in favor of a vector-search guess — this
            # system may be used with financial reports.
            sheet_name = calculated.get("sheet", "")
            prepend.append({
                "text":          calculated["text"],
                "source":        sheet_name,
                "intent":        "calculation" if calculated.get("ok") else "calculation_failed",
                "score":         1.0,
                "citation":      calculated["citation"],
                "file_name":     calculated.get("workbook", ""),
                "page_number":   None,
                "section_title": sheet_name,
                "chunk_index":   0,
                "version":       None,
                "category":      "excel",
                "language":      "th",
                "is_structured": True,
                "is_calculated": calculated.get("ok", False),
                "attachments":   [],
                "chunk_id":      None,
                "file_id":       None,
            })
        else:
            structured = search_excel_structured(question)
            for s in structured:
                s.setdefault("attachments", [])
                s.setdefault("chunk_id", None)
                s.setdefault("file_id", None)
                s.setdefault("is_calculated", False)
            prepend = structured

        chunks = prepend + chunks
        _trace_stage(trace, "excel_engine", "success", t0, f"{len(prepend)} result(s)")
    else:
        _trace_stage(trace, "excel_engine", "skipped", t0, "question not detected as analytical")

    # Enrich results with attachments — rebuild the id lists from the FULL
    # merged pool (vector + lexical) so lexically-merged chunks also get
    # their attachments resolved, not just the original vector candidates.
    chunk_ids = [c["chunk_id"] for c in chunks if c.get("chunk_id")]
    file_ids = [c["file_id"] for c in chunks if c.get("file_id")]
    t0 = _time.time()
    if chunks:
        try:
            att_map = _fetch_attachments_for_chunks(chunk_ids, file_ids)
            for chunk in chunks:
                cid = chunk.get("chunk_id")
                fid = chunk.get("file_id")
                atts = []
                if cid and cid in att_map:
                    atts = att_map[cid]
                elif fid and f"__file__{fid}" in att_map:
                    atts = att_map[f"__file__{fid}"]
                # download_url is always the original file; preview_url is
                # a real thumbnail for image/pdf (see ingestion/
                # attachment_handler._generate_preview) or None for other
                # types — callers show a generic icon based on
                # attachment_type when there's no preview_url.
                for att in atts:
                    att["download_url"] = att.get("public_url")
                    att["preview_url"] = (att.get("metadata") or {}).get("preview_url")
                chunk["attachments"] = atts
            _trace_stage(trace, "attachment_resolver", "success", t0,
                         f"{sum(len(c.get('attachments') or []) for c in chunks)} attachment(s)")
        except Exception as exc:
            print(f"[searcher] attachment enrichment failed: {exc}")
            _trace_stage(trace, "attachment_resolver", "failed", t0, str(exc))
    else:
        _trace_stage(trace, "attachment_resolver", "skipped", t0, "no chunks to enrich")

    # Hybrid re-rank + relevance filter (rag/hybrid_scoring.py) — a chunk
    # that merely sits close in embedding space but has zero real keyword/
    # heading overlap with the question gets dropped here instead of being
    # handed to the LLM as if it were supporting evidence. Generic: no
    # per-document/per-topic special-casing. Runs against the FULL merged
    # (vector + lexical) candidate pool, then truncates to the FINAL
    # top_k — the raw pool (up to RAG_RAW_CANDIDATE_COUNT) is deliberately
    # never handed to the LLM in full (Part 6: "Do not pass all 30
    # candidates to the LLM").
    t0 = _time.time()
    excluded: List[Dict] = []
    try:
        from rag.hybrid_scoring import apply_hybrid_ranking
        from rag.intent_classifier import classify_query_intent
        before = len(chunks)
        # query_variants (original + Thai-English/synonym expansions) is
        # passed through so keyword/heading scoring is computed against
        # every variant, not just the raw un-expanded question — this is
        # the actual root-cause fix (see rag/hybrid_scoring.py's
        # module docstring).
        #
        # query_intent (deterministic, no LLM — rag/intent_classifier.py)
        # feeds the purpose-aware ranking boost: distinguishes documents
        # that legitimately share heavy vocabulary overlap (a benefit
        # brochure and a premium-rate table for the SAME product both say
        # "Plan 1-4" constantly) by preferring the document whose
        # document_purpose matches what the question is actually asking.
        query_intent = classify_query_intent(question)
        # synonym_variant_keys marks which of query_variants came from the
        # Knowledge Synonym Engine (rag/synonym_service.py) specifically,
        # so hybrid_scoring can weight them lower than the original
        # question or a pre-existing glossary variant (Ranking
        # requirement: "original query always has highest priority").
        synonym_variant_keys = {v.strip().lower() for v in synonym_result["variants"][1:]}
        kept, excluded = apply_hybrid_ranking(question, chunks, return_excluded=True,
                                               query_variants=query_variants, query_intent=query_intent,
                                               synonym_variant_keys=synonym_variant_keys,
                                               excluded_terms=excluded_terms,
                                               company_intent=company_intent,
                                               actionable_intent=actionable_intent)
        final_top_k = top_k or retrieval_settings.final_context_top_k or TOP_K
        if is_broad_summary_query(question):
            from config import RAG_SUMMARY_TOP_K
            final_top_k = max(final_top_k, RAG_SUMMARY_TOP_K)
        chunks = kept[:final_top_k]
        _trace_stage(trace, "hybrid_rerank", "success", t0,
                     f"{before} candidate(s) -> {len(kept)} kept -> {len(chunks)} sent to prompt "
                     f"({len(excluded)} excluded), query_intent={query_intent}")
    except Exception as e:
        print(f"[searcher] hybrid_rerank failed, falling back to vector-only order: {e}")
        _trace_stage(trace, "hybrid_rerank", "failed", t0, str(e))
        chunks = chunks[:top_k] if top_k else chunks

    # Excluded candidates are never returned as chunks (never sent to the
    # LLM), but ARE attached to the trace for debug/Playground visibility
    # (Part 6/20) — each with its exclusion_reason from hybrid_scoring.py.
    if trace is not None:
        trace.append({"stage": "excluded_candidates", "status": "info", "duration_ms": 0.0,
                       "detail": f"{len(excluded)} candidate(s) excluded",
                       "excluded": [{"file_name": c.get("file_name"), "section_title": c.get("section_title"),
                                     "score": c.get("score"), "raw_vector_similarity": c.get("raw_vector_similarity"),
                                     "normalized_vector_score": c.get("normalized_vector_score"),
                                     "keyword_score": c.get("keyword_score"), "heading_score": c.get("heading_score"),
                                     "hybrid_score": c.get("hybrid_score"), "classification": c.get("classification"),
                                     "evidence_label": c.get("evidence_label"),
                                     "matched_query": c.get("matched_query"),
                                     "exclusion_reason": c.get("exclusion_reason")} for c in excluded]})

    return chunks


def format_context(chunks: List[Dict]) -> str:
    if not chunks:
        return ""
    parts = []
    has_calculated = any(c.get("is_calculated") for c in chunks)
    has_calc_failure = any(c.get("intent") == "calculation_failed" for c in chunks)
    if has_calculated:
        parts.append(
            "IMPORTANT: one of the sources below is labeled CALCULATED RESULT. "
            "That number was computed deterministically from the spreadsheet — "
            "state it exactly as given. Do not recompute, round differently, or "
            "guess a different number."
        )
    if has_calc_failure:
        parts.append(
            "IMPORTANT: one of the sources below is labeled CANNOT CALCULATE. "
            "Tell the user plainly that this specific number cannot be "
            "calculated from the available data, and why. Do NOT estimate, "
            "guess, or invent a number to fill the gap."
        )
    for i, c in enumerate(chunks, 1):
        citation = c.get("citation") or f"source: {c['source']}"
        score_tag = f", score: {c['score']}" if not c.get("is_structured") else ", structured"
        parts.append(f"[{i}] ({citation}{score_tag})\n{c['text']}")
    return "\n\n".join(parts)


if __name__ == "__main__":
    q = "สินค้าเสียหายจะทำยังไง"
    results = search(q)
    for r in results:
        print(f"score: {r['score']} | {r.get('citation', r['source'])}")
        print(f"  {r['text'][:150]}")
