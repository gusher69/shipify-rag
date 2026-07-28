"""Import Preview — the dry-run analysis behind the new
Upload -> Analyze -> Preview -> User Review -> Confirm -> Background Sync
flow.

Nothing in this module writes to the database or downloads an attachment
body. It re-uses the exact same extraction/enrichment code the real import
uses (ingestion.ingest.read_file_pages, which already runs
excel_extractor.workbook_to_summary_pages and its Category/Tags/Alt-
Questions/Language enrichment for Q&A sheets) so the preview can never
show something different from what a real import would actually produce.

Duplicate detection is a lightweight text-similarity heuristic
(difflib.SequenceMatcher on normalized text against a bounded sample of
existing knowledge_items/knowledge_files) — NOT an embedding/semantic
search. That's a deliberate scope decision: running a real vector search
per preview row against the live index would be slower and would require
committing nothing-yet-real data through the same embedding path this
preview is explicitly trying to avoid running before confirmation.
"""
import difflib
import re
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from ingestion.ingest import read_file_pages
from ingestion.attachment_handler import (
    detect_attachment_columns, find_attachment_refs_in_cell,
    preview_attachment_url,
)
from ingestion.excel_extractor import is_qa_sheet, detect_qa_columns

SUPPORTED_PREVIEW_EXTS = {".xlsx", ".xls", ".csv", ".pdf", ".docx", ".doc", ".md", ".txt"}

# How many existing rows to compare each new item against for duplicate
# detection — bounded so preview analysis stays fast on a large KB.
_DUPLICATE_SAMPLE_SIZE = 500
_DUPLICATE_RATIO = 0.92        # near-identical text
_SIMILAR_RATIO = 0.75          # "very similar" but not identical


def _normalize_for_compare(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _load_existing_questions(sb) -> List[Dict]:
    try:
        res = sb.table("knowledge_items").select("id,question,knowledge_file_id") \
            .is_("deleted_at", "null").not_.is_("question", "null") \
            .order("created_at", desc=True).limit(_DUPLICATE_SAMPLE_SIZE).execute()
        return res.data or []
    except Exception as exc:
        print(f"[import_preview] could not load existing questions for duplicate check: {exc}")
        return []


def _load_existing_filenames(sb) -> List[str]:
    try:
        res = sb.table("knowledge_files").select("filename").is_("deleted_at", "null") \
            .order("uploaded_at", desc=True).limit(_DUPLICATE_SAMPLE_SIZE).execute()
        return [r["filename"] for r in (res.data or [])]
    except Exception as exc:
        print(f"[import_preview] could not load existing filenames for duplicate check: {exc}")
        return []


def _find_duplicate(text: str, existing: List[Dict], text_key: str) -> Optional[Dict]:
    if not text or not text.strip():
        return None
    norm = _normalize_for_compare(text)
    best = None
    best_ratio = 0.0
    for row in existing:
        other = _normalize_for_compare(row.get(text_key) or "")
        if not other:
            continue
        ratio = difflib.SequenceMatcher(None, norm, other).ratio()
        if ratio > best_ratio:
            best_ratio, best = ratio, row
    if best_ratio >= _DUPLICATE_RATIO:
        return {"kind": "duplicate", "ratio": round(best_ratio, 3), "match": best}
    if best_ratio >= _SIMILAR_RATIO:
        return {"kind": "similar", "ratio": round(best_ratio, 3), "match": best}
    return None


def _issue(row_num, problem, recommendation, severity):
    return {"row": row_num, "problem": problem, "recommendation": recommendation, "severity": severity}


def _attachment_preview_entries(refs, row_num: int, item_index: int, issues: list) -> List[Dict]:
    """refs: [(ref, is_url), ...] from find_attachment_refs_in_cell.
    Returns attachment preview dicts; issues are appended in place for any
    broken/unreachable URL."""
    entries = []
    seen = set()
    for ref, is_url in refs:
        if ref in seen:
            continue
        seen.add(ref)
        entry = {
            "id": str(uuid.uuid4()), "item_index": item_index, "row": row_num,
            "source": ref, "is_url": is_url,
        }
        if is_url:
            check = preview_attachment_url(ref)
            entry.update({
                "reachable": check["reachable"],
                "content_type": check["content_type"],
                "content_length": check["content_length"],
                "attachment_type": check["attachment_type"],
                "error": check["error"],
            })
            if not check["reachable"]:
                issues.append(_issue(
                    row_num, f"Broken or unreachable URL: {ref}",
                    check["error"] or "Verify the link opens directly to the file.",
                    "warning",
                ))
        else:
            # Bare filename — cannot verify existence without scanning the
            # same local dirs the real importer does; report as
            # "unverified" rather than pretending to know either way.
            entry.update({
                "reachable": None, "content_type": None, "content_length": None,
                "attachment_type": "file", "error": None,
            })
        entries.append(entry)
    return entries


def _analyze_qa_workbook(wb: Dict, filename: str, existing_questions, existing_filenames,
                          items: list, attachments: list, issues: list):
    seen_in_file: Dict[str, int] = {}
    item_index = 0

    for sheet in wb.get("sheets", []):
        headers = sheet.get("headers", [])
        sname = sheet.get("sheet_name", "")
        att_indices = detect_attachment_columns(headers)
        att_headers = {headers[i] for i in att_indices}

        if not is_qa_sheet(headers):
            continue
        qa_cols = detect_qa_columns(headers)
        skip_headers = {qa_cols.get("question"), qa_cols.get("answer")}
        scan_headers = [h for h in headers if h not in skip_headers]

        for row in sheet.get("rows", []):
            ri = row.get("row_index", 0)
            rd = row.get("row_data", {})
            question = rd.get(qa_cols["question"])
            answer = rd.get(qa_cols["answer"])

            if not question and not answer:
                issues.append(_issue(ri, "Empty row (no Question or Answer)",
                                      "Remove this row or fill in the missing data.", "info"))
                continue
            if not question:
                issues.append(_issue(ri, "Missing Question",
                                      "Add a question — a row with only an answer can't be matched to a user query.",
                                      "error"))
            if not answer:
                issues.append(_issue(ri, "Missing Answer",
                                      "Add an answer — this row won't produce a useful response otherwise.",
                                      "error"))

            item = {
                "index": item_index, "row": ri, "sheet": sname, "type": "qa",
                "question": question, "answer": answer,
                "category": row.get("_generated_category"),
                "tags": row.get("_generated_tags") or [],
                "alt_questions": row.get("_generated_alt_questions") or [],
                "language": row.get("_generated_language"),
                "channel": row.get("_generated_channel"),
                "status": "ok", "warnings": [],
            }

            norm_q = _normalize_for_compare(str(question or ""))
            if norm_q:
                if norm_q in seen_in_file:
                    issues.append(_issue(ri, f"Duplicate question within this file (also row {seen_in_file[norm_q]})",
                                          "Merge these rows or remove one — both will otherwise create separate, redundant knowledge items.",
                                          "warning"))
                    item["warnings"].append("duplicate_in_file")
                else:
                    seen_in_file[norm_q] = ri
                dup = _find_duplicate(str(question), existing_questions, "question")
                if dup:
                    kind_label = "Duplicate" if dup["kind"] == "duplicate" else "Very similar"
                    issues.append(_issue(
                        ri, f"{kind_label} question already in the knowledge base "
                            f"(similarity {int(dup['ratio']*100)}%): \"{(dup['match'].get('question') or '')[:80]}\"",
                        "Skip, replace the existing item, or import anyway if this is intentionally a variant.",
                        "warning" if dup["kind"] == "duplicate" else "info",
                    ))
                    item["duplicate_of"] = dup["match"].get("id")
                    item["duplicate_kind"] = dup["kind"]
                    item["duplicate_ratio"] = dup["ratio"]
                    item["status"] = "duplicate"
                    item["warnings"].append(dup["kind"])

            refs = []
            for h in scan_headers:
                cell = rd.get(h)
                if not cell:
                    continue
                refs.extend(find_attachment_refs_in_cell(cell, h in att_headers))
            att_entries = _attachment_preview_entries(refs, ri, item_index, issues)
            attachments.extend(att_entries)
            item["attachment_ids"] = [a["id"] for a in att_entries]
            if any(a.get("error") for a in att_entries):
                item["warnings"].append("attachment_issue")

            if item["status"] == "ok" and (item["warnings"]):
                item["status"] = "warning"
            elif not question or not answer:
                item["status"] = "error"

            items.append(item)
            item_index += 1


def _analyze_document_pages(pages: List[Dict], filename: str, existing_filenames,
                             items: list, attachments: list, issues: list):
    """Word/PDF/Markdown/plain-text — no Question/Answer structure, so each
    page/section becomes a "document" row. Duplicate check here is at the
    FILE level (same filename already in the knowledge base), since
    comparing arbitrary page text against the whole KB page-by-page would
    be noisy without a real semantic search."""
    from ingestion.attachment_handler import _normalize_filename_for_match
    norm_name = _normalize_filename_for_match(filename)
    dup_file = None
    for existing in existing_filenames:
        # Exact match on the NORMALIZED name — this already folds away
        # case, whitespace/underscore/dash, and cosmetic suffixes like
        # " (1)"/"-final"/"_copy"/"v2" (see _normalize_filename_for_match),
        # so a plain equality check here is more precise for filenames
        # than a fuzzy ratio would be (fuzzy ratio is used for QUESTION
        # TEXT duplicate detection above, where there's no such fixed
        # cosmetic-suffix pattern to strip).
        if _normalize_filename_for_match(existing) == norm_name:
            dup_file = existing
            break
    if dup_file:
        issues.append(_issue(None, f"A file with a very similar name already exists: \"{dup_file}\"",
                              "Skip, replace the existing file, or import anyway if this is a genuinely different document.",
                              "warning"))

    for idx, page in enumerate(pages):
        text = page.get("text") or ""
        if not text.strip():
            issues.append(_issue(page.get("page_number"), "Empty page/section",
                                  "No text extracted — check the source file isn't a scanned image without OCR.",
                                  "warning"))
            continue

        refs = find_attachment_refs_in_cell(text, is_known_attachment_header=False)
        att_entries = _attachment_preview_entries(refs, page.get("page_number"), idx, issues)
        attachments.extend(att_entries)

        items.append({
            "index": idx, "row": page.get("page_number"), "sheet": None, "type": "document",
            "question": None,
            "answer": text[:280] + ("…" if len(text) > 280 else ""),
            "category": None, "tags": [], "alt_questions": [], "language": None, "channel": None,
            "status": "warning" if att_entries and any(a.get("error") for a in att_entries) else "ok",
            "warnings": ["attachment_issue"] if any(a.get("error") for a in att_entries) else [],
            "attachment_ids": [a["id"] for a in att_entries],
            "duplicate_of": dup_file,
        })


def analyze_file_for_preview(file_path: Path, filename: str, sb) -> Dict:
    """Run the full dry-run analysis. Returns a dict ready to hand to the
    Import Preview API response AND to stash server-side (see
    admin/routes.py's _import_previews) for re-use at confirm time —
    `workbook_data`/`pages` carry the exact enrichment (tags/alt_questions/
    category/language) already computed here, so confirming an import
    never needs to re-run extraction, LLM alt-question generation, or
    re-detect anything."""
    ext = file_path.suffix.lower()
    result: Dict = {
        "filename": filename, "file_type": ext.lstrip("."),
        "items": [], "attachments": [], "issues": [], "stats": {},
        "workbook_data": None, "pages": None, "analyzed_at": time.time(),
        "knowledge_analysis": None,
        "ai_profile_recommendation": None, "ai_profile": None,
    }

    if ext not in SUPPORTED_PREVIEW_EXTS:
        result["issues"].append(_issue(
            None, f"Unsupported file type '{ext}'",
            "Convert to one of: xlsx, xls, csv, pdf, docx, doc, md, txt.", "error"))
        _compute_stats(result)
        return result

    try:
        pages = read_file_pages(file_path)
    except Exception as exc:
        result["issues"].append(_issue(None, f"Could not read file: {exc}",
                                        "Check the file isn't corrupted or password-protected.", "error"))
        _compute_stats(result)
        return result

    if not pages:
        result["issues"].append(_issue(None, "No content could be extracted from this file",
                                        "Check the file has readable text/rows.", "error"))
        _compute_stats(result)
        return result

    result["pages"] = pages
    wb = pages[0].get("_workbook_data")
    result["workbook_data"] = wb

    # AI Knowledge Analyzer runs here too — Preview must show the SAME
    # knowledge_type/chunk_strategy/summary/suggested_questions the real
    # import will use, computed via the exact same service
    # (services.knowledge_analyzer) so preview and confirm can never
    # disagree. Never blocks the rest of the preview if it fails.
    # AI Recommendation Engine — decides, BEFORE spending any LLM budget,
    # how much of the analyzer pipeline below is actually worth running.
    # Purely deterministic/statistical (see services/recommendation_engine.py
    # for why it can't depend on the LLM classification it's choosing
    # whether to run). The admin can override the recommended profile
    # before confirming (see the PATCH .../ai-profile endpoint); until then
    # this recommendation IS what gets used.
    recommended_profile = "advanced"
    try:
        from services.recommendation_engine import get_recommendation_engine
        try:
            file_size = file_path.stat().st_size
        except OSError:
            file_size = 0
        engine = get_recommendation_engine()
        chars = engine.analyze(pages, filename, file_size_bytes=file_size)
        rec = engine.recommend(chars)
        result["ai_profile_recommendation"] = rec.to_dict()
        result["ai_profile"] = rec.profile
        recommended_profile = rec.profile
    except Exception as exc:
        print(f"[import_preview] AI profile recommendation failed: {exc}")

    try:
        from services.knowledge_analyzer import get_knowledge_analyzer
        analysis = get_knowledge_analyzer().analyze(pages, filename, profile=recommended_profile)
        result["knowledge_analysis"] = analysis.to_dict()
        # Internal only (leading underscore; never sent to the browser —
        # admin/routes.py's _serialize_preview whitelists fields
        # explicitly). Keeps the LIVE GraphNode/GraphEdge objects (not the
        # serialized copy above) so the accept/reject/edit PATCH endpoint
        # can mutate them in place — those mutations are what survive into
        # Confirm via knowledge_graph_service's content-hash cache.
        result["_live_knowledge_graph"] = analysis.knowledge_graph
    except Exception as exc:
        print(f"[import_preview] knowledge analysis failed: {exc}")
        result["knowledge_analysis"] = None
        result["_live_knowledge_graph"] = None

    existing_questions = _load_existing_questions(sb)
    existing_filenames = _load_existing_filenames(sb)

    if wb:
        _analyze_qa_workbook(wb, filename, existing_questions, existing_filenames,
                              result["items"], result["attachments"], result["issues"])
    else:
        _analyze_document_pages(pages, filename, existing_filenames,
                                 result["items"], result["attachments"], result["issues"])

    _compute_stats(result)
    return result


def _compute_stats(result: Dict):
    items = result["items"]
    attachments = result["attachments"]
    issues = result["issues"]

    languages = sorted({i["language"] for i in items if i.get("language")})
    categories = sorted({i["category"] for i in items if i.get("category")})
    by_type: Dict[str, int] = {}
    for a in attachments:
        t = a.get("attachment_type") or "file"
        by_type[t] = by_type.get(t, 0) + 1

    result["stats"] = {
        "rows_read": len(items),
        "rows_importable": len([i for i in items if i["status"] != "error"]),
        "rows_skipped": len([i for i in items if i["status"] == "error"]),
        "duplicate_questions": len([i for i in items if i.get("duplicate_kind") == "duplicate"]),
        "similar_questions": len([i for i in items if i.get("duplicate_kind") == "similar"]),
        "generated_tags": sum(len(i.get("tags") or []) for i in items),
        "generated_categories": len(categories),
        "generated_alt_questions": sum(len(i.get("alt_questions") or []) for i in items),
        "detected_attachments": len(attachments),
        "attachments_by_type": by_type,
        "attachments_unreachable": len([a for a in attachments if a.get("reachable") is False]),
        "languages": languages,
        "categories": categories,
        "warnings": len([i for i in issues if i["severity"] == "warning"]),
        "errors": len([i for i in issues if i["severity"] == "error"]),
        "infos": len([i for i in issues if i["severity"] == "info"]),
        "questions": len([i for i in items if i["type"] == "qa" and i.get("question")]),
        "answers": len([i for i in items if i["type"] == "qa" and i.get("answer")]),
        "knowledge_items": len(items),
    }
