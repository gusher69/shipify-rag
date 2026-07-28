"""Knowledge Synonym Engine — expands a query with domain synonyms BEFORE
retrieval (embedding + hybrid ranking), so "ขอโลเคชั่นโกดัง" also searches
for "ขอพิกัดโกดัง" / "ขอแผนที่โกดัง" / "Location โกดัง" / etc. even though a
document phrased with "พิกัด" or "Google Maps" shares almost no literal
text with the original question.

Pure Python, deterministic, no LLM call, no API call — same convention as
rag/query_expansion.py (the existing Thai-English glossary) and
rag/intent_classifier.py. This module is intentionally separate from
query_expansion.py: that module is a fixed, generic bilingual glossary
maintained in code; this one is a DATA-DRIVEN synonym dictionary meant to
grow over time (administrators adding new groups) without a code change —
see get_synonym_groups()'s docstring for the DB-first/JSON-fallback
loading strategy.

Enriches, never replaces: the original query is always variants[0] (the
existing contract every caller — rag/hybrid_scoring.py's heading "is
original" check, rag/query_expansion.py's callers — already depends on),
and this module's variants are additive, appended after whatever
rag/query_expansion.py / rag/query_understanding.py already produced.
"""
import json
import os
import re
import threading
from typing import Dict, List, Optional

_DEFAULT_JSON_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "synonym_groups.json")

_cache_lock = threading.Lock()
_cached_groups: Optional[List[Dict]] = None


def _normalize_group(raw: Dict) -> Optional[Dict]:
    canonical = (raw.get("canonical_term") or "").strip()
    synonyms = [s.strip() for s in (raw.get("synonyms") or []) if s and s.strip()]
    if not canonical:
        return None
    return {"id": raw.get("id") or canonical, "canonical_term": canonical, "synonyms": synonyms}


def _load_from_json(path: Optional[str] = None) -> List[Dict]:
    path = path or _DEFAULT_JSON_PATH
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        groups = [g for g in (_normalize_group(r) for r in raw) if g]
        return groups
    except Exception as e:
        print(f"[synonym_service] failed to load {path}: {e}")
        return []


def _load_from_db() -> Optional[List[Dict]]:
    """Recommended schema (see module docstring / task spec):
        synonym_groups(id, canonical_term)
        synonym_words(group_id, word)
    Returns None (never []) on any failure or when the tables don't exist
    yet — callers must fall back to the JSON file in that case, never
    treat "DB query failed" the same as "DB genuinely has zero groups"."""
    try:
        from rag.searcher import _get_supabase
        sb = _get_supabase()
        groups_res = sb.table("synonym_groups").select("id,canonical_term").execute()
        group_rows = groups_res.data or []
        if not group_rows:
            return None
        words_res = sb.table("synonym_words").select("group_id,word").execute()
        word_rows = words_res.data or []
        words_by_group: Dict[str, List[str]] = {}
        for w in word_rows:
            words_by_group.setdefault(w["group_id"], []).append(w["word"])
        groups = []
        for g in group_rows:
            groups.append(_normalize_group({
                "id": g["id"], "canonical_term": g.get("canonical_term"),
                "synonyms": words_by_group.get(g["id"], []),
            }))
        return [g for g in groups if g]
    except Exception as e:
        print(f"[synonym_service] DB load failed, will fall back to JSON: {e}")
        return None


def get_synonym_groups(force_reload: bool = False) -> List[Dict]:
    """DB-backed with a JSON-file fallback — same graceful-degrade
    convention used throughout this app (see services/prompt_builder.py's
    _FALLBACK_TEMPLATES). Cached in-process after the first successful
    load; pass force_reload=True (or call reload_synonym_groups()) after
    an admin edits the dictionary, or in tests."""
    global _cached_groups
    with _cache_lock:
        if _cached_groups is not None and not force_reload:
            return _cached_groups
        groups = _load_from_db()
        if groups is None:
            groups = _load_from_json()
        _cached_groups = groups
        return _cached_groups


def reload_synonym_groups() -> List[Dict]:
    return get_synonym_groups(force_reload=True)


def _replace_case_insensitive(text: str, old: str, new: str) -> Optional[str]:
    pattern = re.compile(re.escape(old), re.IGNORECASE)
    if not pattern.search(text):
        return None
    return pattern.sub(new, text, count=1)


def expand_query_with_synonyms(question: str) -> Dict:
    """Returns:
        {
          "variants": [question, *expanded_variants],   # question ALWAYS first
          "canonical_terms": [canonical, ...],           # one per matched group, in match order
          "synonyms_used": [{"term": ..., "canonical": ...}, ...],  # every member of every matched group
        }

    For each synonym group with at least one member appearing literally in
    `question` (case-insensitive), generates one rewritten variant per
    OTHER member of that group, substituting the matched span. If nothing
    matches any group, returns `question` as the only variant and empty
    canonical_terms/synonyms_used — i.e. behaves exactly like before this
    feature existed (backward compatibility)."""
    variants: List[str] = [question]
    canonical_terms: List[str] = []
    synonyms_used: List[Dict] = []
    if not question:
        return {"variants": variants, "canonical_terms": canonical_terms, "synonyms_used": synonyms_used}

    lower_q = question.lower()
    seen_variant_keys = {question.strip().lower()}
    seen_synonym_pairs = set()

    for group in get_synonym_groups():
        canonical = group["canonical_term"]
        all_terms = [canonical] + group["synonyms"]
        present = [t for t in all_terms if t and t.lower() in lower_q]
        if not present:
            continue
        # Longest actual match wins — avoids "map" being treated as a hit
        # merely because it's a substring of a query that actually
        # contains "google map".
        matched_term = max(present, key=len)
        canonical_terms.append(canonical)

        for term in all_terms:
            pair_key = (term.lower(), canonical.lower())
            if pair_key not in seen_synonym_pairs:
                seen_synonym_pairs.add(pair_key)
                synonyms_used.append({"term": term, "canonical": canonical})

        for other in all_terms:
            if other.lower() == matched_term.lower():
                continue
            new_variant = _replace_case_insensitive(question, matched_term, other)
            if not new_variant:
                continue
            key = new_variant.strip().lower()
            if key not in seen_variant_keys:
                seen_variant_keys.add(key)
                variants.append(new_variant)

    return {"variants": variants, "canonical_terms": canonical_terms, "synonyms_used": synonyms_used}
