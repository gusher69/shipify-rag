"""Structured FAQ exact/near-exact matching — knowledge_items rows
(ingested from Q&A-style Excel sheets, see ingestion/attachment_handler.py
::_create_knowledge_items) each carry a `question`, an `answer`, and a
list of `alt_questions` (migration-013 columns). Previously these rows
were never matched at retrieval time as structured Q&A rows at all — they
were embedded and searched through the exact same generic vector+hybrid
path as any other document chunk, so a clean, exact FAQ match could still
lose to an unrelated chunk that merely shared generic phrasing (see
rag/hybrid_scoring.py's stopword fix for the other half of that bug).

This module is deliberately narrow: it only ever returns a match when the
CURRENT (already follow-up-resolved) question is an exact or very close
match to one FAQ row's Question or one of its Alternative Questions.
Anything weaker falls through to the normal retrieval pipeline untouched
— this is a targeted short-circuit, not a replacement for semantic
search.
"""
import re
from difflib import SequenceMatcher
from typing import Dict, List, Optional

# Below this similarity ratio, two questions are NOT considered a match —
# normal retrieval handles it instead. Deliberately conservative: a false
# "exact" FAQ match sends ONLY that one row to the LLM (see
# rag/searcher.py), so a wrong match here is much more costly than a
# missed one (which just falls through to hybrid retrieval as before).
NEAR_EXACT_THRESHOLD = 0.88

_PUNCT_RE = re.compile(r"[?!.,;:\"'()\[\]{}๏๚๛…]")
_WS_RE = re.compile(r"\s+")


def normalize_faq_text(text: Optional[str]) -> str:
    """Lowercase / strip punctuation / collapse whitespace — just enough
    normalization that "ใช้บัตรเครดิตได้ไหม?" and "ใช้บัตรเครดิตได้ไหม"
    compare equal, without doing any real segmentation (Thai has no
    spaces; this module compares whole normalized strings, not tokens —
    see find_best_faq_match's docstring for why that's the right
    granularity here, unlike rag/hybrid_scoring.py's token overlap)."""
    if not text:
        return ""
    t = text.strip().lower()
    t = _PUNCT_RE.sub("", t)
    t = _WS_RE.sub(" ", t).strip()
    return t


def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def find_best_faq_match(question: str, rows: List[Dict]) -> Optional[Dict]:
    """`rows`: [{"question": str, "alt_questions": [str, ...], ...row
    fields passed through untouched}]. Compares the WHOLE normalized
    question string against the whole normalized Question/each
    Alternative Question (character-level similarity, not token overlap)
    — this is intentional: two full FAQ questions differing only in a
    shared generic suffix like "ได้ไหม" (e.g. "ใช้บัตรเครดิตได้ไหม" vs
    "ออกใบกำกับได้ไหม") are NOT close as whole strings (their similarity
    ratio is dominated by the very different content before the shared
    suffix), so this naturally can't be fooled by a generic phrase the
    way keyword-token overlap can.

    Returns {"row": row, "match_type": "exact"|"near_exact",
    "matched_text": the specific Question/Alt-Question that matched,
    "score": float} for the single BEST-matching row, or None if nothing
    clears NEAR_EXACT_THRESHOLD — callers must fall back to normal
    retrieval in that case, never guess."""
    norm_q = normalize_faq_text(question)
    if not norm_q:
        return None

    best: Optional[Dict] = None
    for row in rows:
        candidates = [row.get("question")] + list(row.get("alt_questions") or [])
        for cand in candidates:
            norm_cand = normalize_faq_text(cand)
            if not norm_cand:
                continue
            if norm_cand == norm_q:
                return {"row": row, "match_type": "exact", "matched_text": cand, "score": 1.0}
            score = _similarity(norm_q, norm_cand)
            if best is None or score > best["score"]:
                best = {"row": row, "match_type": "near_exact", "matched_text": cand, "score": score}

    if best and best["score"] >= NEAR_EXACT_THRESHOLD:
        return best
    return None


def match_faq_exact(question: str, extra_queries: Optional[List[str]] = None) -> Optional[Dict]:
    """DB-backed wrapper around find_best_faq_match — fetches active
    knowledge_items rows (Q&A rows with their Alternative Questions) and
    finds the best match for `question`. Never raises: any Supabase
    failure is logged and treated as "no match," falling through to
    normal retrieval, exactly like every other best-effort enrichment
    step in rag/searcher.py.

    `extra_queries`, if given (e.g. rag/synonym_service.py's synonym-
    expanded variants), are tried IN ADDITION to `question` — never
    instead of it. `question` is always checked first: an exact match on
    the user's own wording is returned immediately, before any extra
    variant is even considered, so a synonym rewrite can enrich FAQ
    matching (catch a row phrased with "พิกัด" when the user typed
    "โลเคชั่น") without ever outranking the original question."""
    try:
        from rag.searcher import _get_supabase
        sb = _get_supabase()
        res = (
            sb.table("knowledge_items")
            .select("id,question,answer,alt_questions,sheet_name,row_index,chunk_id,knowledge_file_id")
            .is_("deleted_at", "null")
            .execute()
        )
        rows = [r for r in (res.data or []) if r.get("question")]
    except Exception as e:
        print(f"[faq_matcher] fetch failed: {e}")
        return None

    candidates = [question] + [q for q in (extra_queries or []) if q and q != question]
    best: Optional[Dict] = None
    for q in candidates:
        result = find_best_faq_match(q, rows)
        if not result:
            continue
        if result["match_type"] == "exact":
            return result
        if best is None or result["score"] > best["score"]:
            best = result
    return best
