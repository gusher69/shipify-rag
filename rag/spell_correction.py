"""Deterministic Query Spell Correction — runs BEFORE retrieval (and
before Follow-up Resolution / Intent Detection / Synonym Expansion / FAQ
Exact Match), fixing common Thai typing mistakes without any LLM/API
call:

    ส่งแผนี่ให้หน่อย   -> ส่งแผนที่ให้หน่อย
    ขอโลเคชันโกดดัง   -> ขอโลเคชั่นโกดัง
    ชำระบิวอย่างไร    -> ชำระบิลอย่างไร
    เรดทางเรือ        -> เรททางเรือ
    CBเอ็มคืออะไร     -> CBM คืออะไร

Pure Python (a small stdlib Levenshtein edit-distance implementation —
raw difflib.SequenceMatcher ratio turned out too coarse for short 3-4
character Thai words: a single-character substitution in a 3-char word
already drops the ratio to ~0.67, well below any ratio threshold that
also has to reject unrelated short strings). Two layers, applied in
order:

  1. Direct corrections (data/spell_correction_fallback.json's
     "direct_corrections") — a small, exact literal substring map for
     frequent, hard-to-generalize typos (e.g. a Thai transliteration
     glued onto a Latin abbreviation, "CBเอ็ม" -> "CBM"). Always applied
     first, with confidence 1.0. Its OUTPUT span is then protected from
     the fuzzy layer below (see _apply_direct_corrections' docstring) —
     without that, "CBM" freshly inserted next to Thai text (no `\b`
     boundary between a Latin and a Thai character) could get re-mangled
     by the fuzzy pass on the very next line.
  2. Vocabulary-driven fuzzy correction — a sliding window over the
     (already normalized) query is compared against a vocabulary of
     KNOWN-CORRECT short terms (see get_vocabulary()'s docstring for
     sources) using edit distance; a window is corrected only when its
     distance clears MAX_EDIT_DISTANCE for that word's length, the
     window isn't already a DIFFERENT valid vocabulary word, and it
     doesn't overlap a protected span (see PROTECTED entities below).
     Same-length windows (substitution typos — the common case) are
     tried before length ±1 windows (insertion/deletion typos), so a
     correction never "steals" a character from an adjacent word unless
     no same-length match exists at all.

Deliberately conservative: above the max allowed edit distance, or when
a window is already a different known-valid word, the original text is
left untouched — "if ambiguous, keep the original term," never a silent
guess.
"""
import json
import os
import re
import threading
from typing import Dict, List, Optional, Tuple

_FALLBACK_JSON_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "spell_correction_fallback.json")

# Never fuzzy-correct spans shorter than this — a 1-2 character window
# has too many plausible vocabulary neighbors to correct safely.
MIN_TERM_LEN = 3


def _edit_distance(a: str, b: str) -> int:
    """Standard Levenshtein distance (insert/delete/substitute), O(len(a)*len(b))
    — both strings here are always short (a handful of characters), so
    this is negligible even run thousands of times per query."""
    if a == b:
        return 0
    la, lb = len(a), len(b)
    if la == 0:
        return lb
    if lb == 0:
        return la
    prev_row = list(range(lb + 1))
    for i in range(1, la + 1):
        cur_row = [i] + [0] * lb
        for j in range(1, lb + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur_row[j] = min(prev_row[j] + 1, cur_row[j - 1] + 1, prev_row[j - 1] + cost)
        prev_row = cur_row
    return prev_row[lb]


def _best_substring_alignment(word: str, text: str) -> Tuple[int, int, int]:
    """Finds the substring of `text` that BEST (minimum edit distance)
    aligns with `word`, using free start/end gaps on the text side —
    i.e. "approximate substring search" (a classic, textbook DP; not a
    naive fixed-width sliding window). This is what makes the correction
    robust on unspaced Thai text: a naive "try every fixed-length window
    at every offset" approach produces multiple off-by-one candidates
    with the SAME edit distance near the true typo (e.g. one window
    missing the first character, another missing the last), and picking
    among those arbitrarily corrupts adjacent text. This DP instead finds
    the single best-aligned span directly.

    Returns (start, end, edit_distance) for the best-scoring span. O(len(word)*len(text)) — trivial for
    short vocabulary words against a short query."""
    m, n = len(word), len(text)
    # dp[i][j]: min edit distance aligning word[:i] to text ending at
    # position j, with a FREE start anywhere in text (dp[0][j] = 0 for
    # all j) — this is what lets the match start mid-string.
    dp = [[0] * (n + 1) for _ in range(m + 1)]
    for i in range(1, m + 1):
        dp[i][0] = i
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            cost = 0 if word[i - 1] == text[j - 1] else 1
            dp[i][j] = min(dp[i - 1][j] + 1, dp[i][j - 1] + 1, dp[i - 1][j - 1] + cost)

    def _traceback(end_j: int) -> int:
        i, j = m, end_j
        while i > 0:
            if j > 0 and dp[i][j] == dp[i - 1][j - 1] + (0 if word[i - 1] == text[j - 1] else 1):
                i, j = i - 1, j - 1
            elif j > 0 and dp[i][j] == dp[i][j - 1] + 1:
                j -= 1
            else:
                i -= 1
        return j

    best_dist = min(dp[m])
    # Multiple end positions can tie for the global minimum distance —
    # e.g. matching only 2 of a 3-char word's characters plus one "free"
    # trailing insertion costs the SAME as matching all 3 with one
    # substitution. Among ties, prefer the one whose recovered span
    # length is closest to len(word) (then earliest position) — the
    # properly-sized alignment, not a degenerate short/long one.
    best_start, best_end = 0, 0
    best_len_delta = None
    for j in range(0, n + 1):
        if dp[m][j] != best_dist:
            continue
        start = _traceback(j)
        len_delta = abs((j - start) - m)
        if best_len_delta is None or len_delta < best_len_delta or (len_delta == best_len_delta and start < best_start):
            best_len_delta, best_start, best_end = len_delta, start, j
    return best_start, best_end, best_dist


def _max_edit_distance(word_len: int) -> int:
    """Conservative, length-scaled tolerance: a longer word can absorb
    more edits before becoming a DIFFERENT plausible word, but a short
    word can only ever safely tolerate a single edit. Deliberately tight
    (roughly distance <= ~20% of length) — e.g. a 5-char word allowing
    distance 2 (40% of its characters) was loose enough to "correct" the
    valid, unrelated word "ส่ง" into "ขนส่ง" purely because they share a
    suffix; distance <= 1 up to length 5 rejects that."""
    if word_len <= 5:
        return 1
    if word_len <= 9:
        return 2
    return 3


# ── Protected entities — a span matching ANY of these is NEVER touched,
# by either correction layer. ──────────────────────────────────────────
_PROTECTED_PATTERNS = [
    re.compile(r"https?://\S+", re.IGNORECASE),                       # URLs
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),                           # emails
    re.compile(r"\b0\d{1,2}[-\s]?\d{3}[-\s]?\d{3,4}\b"),                # Thai phone numbers
    re.compile(r"\b[A-Za-z]{1,4}\d{4,}\b"),                            # tracking/order/bill/product codes (e.g. FT123456789)
    re.compile(r"\b\d[\d,]*\.?\d*\b"),                                 # numbers/prices (incl. thousands separators)
    # Known English abbreviations — uses a Latin-only lookaround instead
    # of `\b` on both sides: Python's `\b` treats Thai letters as word
    # characters too, so "CBM" glued directly onto Thai text (e.g.
    # "CBMคืออะไร", no space) has NO `\b` between "M" and "ค" and would
    # otherwise slip through unprotected.
    re.compile(r"(?<![A-Za-z])(CBM|API|VAT|OPD|ICU|GPS|FAQ|SKU|POD|QR)(?![A-Za-z])", re.IGNORECASE),
]


# Fuzzy-Correction-Only Phrase Guards (Strict Shipify RAG Grounding,
# 2026-08-27) — a SEPARATE list from _PROTECTED_PATTERNS above, checked
# ONLY by _find_fuzzy_spans (via _correction_protected_spans below), never
# by rag/semantic_guard.py::_extract_identifiers. A domain_terms entry
# only prevents the EXACT-length, EXACT-position window from being
# overwritten (the `substr in vocab_set` check); it does nothing for a
# DIFFERENT, one-character-shifted window that happens to align well
# against some OTHER vocabulary/tag word instead. Confirmed live:
# "ขั้นตอนการนำเข้าสินค้าจากจีนเข้าไทยมีอะไรบ้าง" (a genuine RAG-042
# question, no typo at all) still got "การนำเข้า" shifted one character to
# "ารนำเข้า" and fuzzy-corrected to the registered vocabulary/tag term
# "เรทนำเข้า" ("import rate"), sending a completely unrelated corrupted
# query into retrieval and RAG-042 never being found. A protected PATTERN
# (unlike a domain_terms entry) blocks every span that OVERLAPS it,
# regardless of offset, and — critically — is never itself added to the
# correction-target vocabulary, so (unlike registering the shifted
# fragment itself as a domain_term, which was tried and rejected: it
# started corrupting OTHER unrelated messages by becoming a fuzzy-
# correction target in its own right) this cannot introduce a new
# corruption anywhere else.
#
# Deliberately kept OUT of _PROTECTED_PATTERNS itself: that list is also
# reused by rag/semantic_guard.py::_extract_identifiers to build each
# side of the before/after entity diff a Canonical Query Rewrite is
# checked against — confirmed live, putting these ordinary business
# PHRASES there (as opposed to _PROTECTED_PATTERNS' actual identifiers:
# URLs/emails/phone numbers/tracking codes/known abbreviations, which are
# supposed to never appear/disappear across a rewrite) broke the
# legitimate "ขอเรทเรือ" -> "อัตราค่าขนส่งทางเรือเท่าไหร่" canonical
# rewrite: revealing "ทางเรือ" this way is exactly the SAME kind of safe,
# structure-revealing completion the topic/attribute exemption in
# semantic_guard.py already allows, not a genuinely introduced identifier.
_FUZZY_CORRECTION_PHRASE_GUARDS = [
    re.compile(r"การนำเข้า"),
    # Same class, same mechanism — "นำเข้าสินค้า" ("import goods," a
    # completely ordinary phrase, e.g. "สนใจนำเข้าสินค้าจากจีน") had its
    # "นำเข้าสิน" window fuzzy-corrected to the vocabulary/tag term
    # "นำเข้าจีน" for the identical reason.
    re.compile(r"นำเข้าสินค้า"),
    # Same class, same mechanism — "ทางเรือ" ("by sea," e.g. "ทางรถกับ
    # ทางเรือกี่วัน") had "เรื" fuzzy-corrected into "เรท" ("rate") for
    # the identical reason (one edit apart, "เรท" a registered
    # vocabulary/tag term).
    re.compile(r"ทางเรือ"),
    # Semantic RAG Retrieval fix (2026-08-27) — same class, same mechanism
    # — "เริ่ม" ("to start/begin," an ordinary word, e.g. "เริ่มนำเข้า
    # สินค้าจากจีนยังไงครับ") had its own leading fragment "เริ" fuzzy-
    # corrected into "เรท" ("rate") for the identical reason (one edit
    # apart, "เรท" a registered vocabulary/tag term) — confirmed live to
    # corrupt "เริ่มนำเข้า..." into "เรท่มนำเข้า...".
    re.compile(r"เริ่ม"),
]


def _protected_spans(text: str) -> List[Tuple[int, int]]:
    spans = []
    for pat in _PROTECTED_PATTERNS:
        for m in pat.finditer(text):
            spans.append((m.start(), m.end()))
    return spans


def _correction_protected_spans(text: str) -> List[Tuple[int, int]]:
    """_protected_spans() PLUS _FUZZY_CORRECTION_PHRASE_GUARDS — used only
    by _find_fuzzy_spans (fuzzy correction itself), never by
    rag/semantic_guard.py's identifier diff (see that list's own
    docstring for why the two must stay separate)."""
    spans = _protected_spans(text)
    for pat in _FUZZY_CORRECTION_PHRASE_GUARDS:
        for m in pat.finditer(text):
            spans.append((m.start(), m.end()))
    return spans


def _overlaps(start: int, end: int, spans: List[Tuple[int, int]]) -> bool:
    return any(not (end <= s or start >= e) for s, e in spans)


# ── Fallback JSON (domain_terms + direct_corrections) ──────────────────

# RLock (not Lock): get_vocabulary() acquires this and, WHILE holding it,
# calls get_fallback_dict() which acquires it again — a plain Lock would
# self-deadlock on that reentrant acquisition from the same thread.
_cache_lock = threading.RLock()
_cached_fallback: Optional[Dict] = None
_cached_vocabulary: Optional[List[str]] = None


def _load_fallback_json(path: Optional[str] = None) -> Dict:
    path = path or _FALLBACK_JSON_PATH
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {
            "domain_terms": [t for t in (data.get("domain_terms") or []) if t],
            "direct_corrections": {k: v for k, v in (data.get("direct_corrections") or {}).items() if k and v},
        }
    except Exception as e:
        print(f"[spell_correction] failed to load {path}: {e}")
        return {"domain_terms": [], "direct_corrections": {}}


def get_fallback_dict(force_reload: bool = False) -> Dict:
    global _cached_fallback
    with _cache_lock:
        if _cached_fallback is not None and not force_reload:
            return _cached_fallback
        _cached_fallback = _load_fallback_json()
        return _cached_fallback


def get_vocabulary(force_reload: bool = False) -> List[str]:
    """Correction vocabulary — the set of KNOWN-CORRECT short terms a
    fuzzy-matched window may be corrected TO. Sources (requirement 4):
      - Synonym groups (rag/synonym_service.py) — canonical term + every
        synonym; already clean, atomic strings, no segmentation needed.
      - Tags on knowledge_items (FAQ rows) — also already atomic strings
        (a list column), best-effort DB read.
      - The JSON fallback's "domain_terms" — a small curated list for
        frequent domain words (e.g. "บิล") that aren't captured by a
        synonym group or a tag yet.
    Full FAQ Question/Alternative Question/heading SENTENCES are
    deliberately NOT word-segmented here: Thai has no reliable
    dependency-free tokenizer available in this project (no pythainlp/
    similar installed), and guessing word boundaries would risk
    inventing bogus vocabulary entries. Tags and synonym terms are
    already correctly segmented, which is what makes them safe to use
    directly.
    Cached in-process; pass force_reload=True (or call
    reload_vocabulary()) after an admin edits synonym groups/tags/the
    fallback file, or in tests."""
    global _cached_vocabulary
    with _cache_lock:
        if _cached_vocabulary is not None and not force_reload:
            return _cached_vocabulary
        terms = set()

        try:
            from rag.synonym_service import get_synonym_groups
            for g in get_synonym_groups():
                terms.add(g["canonical_term"])
                terms.update(g["synonyms"])
        except Exception as e:
            print(f"[spell_correction] synonym vocabulary load failed (non-fatal): {e}")

        try:
            from rag.searcher import _get_supabase
            sb = _get_supabase()
            res = sb.table("knowledge_items").select("tags").is_("deleted_at", "null").execute()
            for row in (res.data or []):
                for t in (row.get("tags") or []):
                    if t:
                        terms.add(t)
        except Exception as e:
            print(f"[spell_correction] tags vocabulary load failed (non-fatal): {e}")

        terms.update(get_fallback_dict().get("domain_terms", []))

        # Longest-first so a longer, more specific vocabulary word is
        # tried before a shorter one that might also loosely match the
        # same span (e.g. prefer "google map" over "map").
        _cached_vocabulary = sorted({t for t in terms if t and len(t) >= MIN_TERM_LEN}, key=len, reverse=True)
        return _cached_vocabulary


def reload_vocabulary() -> List[str]:
    return get_vocabulary(force_reload=True)


# ── Correction ───────────────────────────────────────────────────────

def _apply_direct_corrections(text: str, direct_map: Dict[str, str]) -> Tuple[str, List[Dict], List[Tuple[int, int]]]:
    """Returns (corrected_text, corrections, protected_spans) — the third
    element is the character range of every `right` value just inserted,
    so the fuzzy layer never re-touches text this layer already fixed
    (a freshly-inserted "CBM" glued to Thai text has no regex `\\b`
    boundary on the Thai side and would otherwise be fair game again)."""
    corrections = []
    just_corrected: List[Tuple[int, int]] = []
    for wrong, right in direct_map.items():
        idx = text.find(wrong)
        if idx == -1:
            continue
        text = text.replace(wrong, right)
        # Every occurrence gets its own protected span (search fresh each
        # time since prior replacements can shift subsequent offsets).
        search_from = 0
        while True:
            pos = text.find(right, search_from)
            if pos == -1:
                break
            just_corrected.append((pos, pos + len(right)))
            search_from = pos + len(right)
        corrections.append({"from": wrong, "to": right, "confidence": 1.0, "method": "direct"})
    return text, corrections, just_corrected


def _find_fuzzy_spans(text: str, vocabulary: List[str],
                       extra_protected: Optional[List[Tuple[int, int]]] = None) -> List[Tuple[int, int, str, str, int]]:
    """Returns non-overlapping (start, end, original_substr, replacement,
    edit_distance) tuples, lowest-distance (best) spans chosen first.
    For each vocabulary word, finds its single BEST-aligned span via
    _best_substring_alignment (not a brute-force fixed-width window scan
    — see that function's docstring for why that distinction matters on
    unspaced Thai text) and keeps it only if it clears the length-scaled
    max edit distance, isn't already a DIFFERENT valid vocabulary word,
    doesn't cross a whitespace boundary the word itself doesn't have, and
    doesn't overlap a protected span."""
    protected = _correction_protected_spans(text) + (extra_protected or [])
    vocab_set = set(vocabulary)
    candidates: List[Tuple[int, int, str, str, int]] = []

    for word in vocabulary:
        wlen = len(word)
        if wlen < MIN_TERM_LEN or wlen > len(text):
            continue
        max_dist = _max_edit_distance(wlen)
        start, end, dist = _best_substring_alignment(word, text)
        if dist == 0 or dist > max_dist:
            continue  # already correct, or too different to safely correct
        substr = text[start:end]
        if len(substr) < MIN_TERM_LEN or abs(len(substr) - wlen) > 1:
            # The free-start/free-end DP can land on a degenerate
            # alignment (e.g. matching only 2 of a 3-char word's
            # characters plus one "free" insertion) that's the same
            # edit distance as the intended, properly-sized alignment —
            # reject anything whose matched span length isn't within 1
            # character of the target word's own length.
            continue
        if _overlaps(start, end, protected):
            continue
        if substr == word:
            continue  # already correct — nothing to do
        if substr in vocab_set:
            continue  # already a DIFFERENT valid vocabulary word — never overwrite a real term
        if (" " in substr) != (" " in word):
            continue  # never swallow/lose a whitespace boundary that isn't part of the word itself
        candidates.append((start, end, substr, word, dist))

    # Ambiguity guard: if two DIFFERENT vocabulary words both align to
    # the EXACT SAME span at the SAME (best) edit distance, that span is
    # genuinely ambiguous — "if ambiguous, keep the original term." One
    # exception: if exactly one of the tied words is a curated
    # data/spell_correction_fallback.json domain_term (added by an admin
    # SPECIFICALLY to resolve a known frequent typo — e.g. "บิล" for the
    # generic synonym-engine term "คิว", both equidistant from "บิว"),
    # that explicit, more specific signal wins; a tie among domain_terms
    # themselves (or among non-domain terms) is still left ambiguous.
    domain_terms = set(get_fallback_dict().get("domain_terms", []))
    by_span: Dict[Tuple[int, int], List[Tuple[int, int, str, str, int]]] = {}
    for c in candidates:
        by_span.setdefault((c[0], c[1]), []).append(c)
    unambiguous = []
    for span, group in by_span.items():
        best_dist = min(c[4] for c in group)
        tied = [c for c in group if c[4] == best_dist]
        tied_words = {c[3] for c in tied}
        if len(tied_words) > 1:
            domain_tied = [c for c in tied if c[3] in domain_terms]
            if len(domain_tied) == 1:
                unambiguous.append(domain_tied[0])
            continue  # still ambiguous otherwise — e.g. two domain_terms tied, or two synonym terms tied
        unambiguous.append(min(group, key=lambda c: c[4]))

    # Lowest edit distance first, then longer span, then earliest
    # position — greedy non-overlapping selection over what's left.
    unambiguous.sort(key=lambda c: (c[4], -(c[1] - c[0]), c[0]))
    chosen: List[Tuple[int, int, str, str, int]] = []
    occupied: List[Tuple[int, int]] = []
    for start, end, substr, word, dist in unambiguous:
        if _overlaps(start, end, occupied):
            continue
        chosen.append((start, end, substr, word, dist))
        occupied.append((start, end))
    chosen.sort(key=lambda c: c[0])
    return chosen


def _distance_to_confidence(distance: int, word_len: int) -> float:
    """A simple, explainable confidence score derived from the edit
    distance relative to the word's length — 0 edits (shouldn't happen,
    already-equal windows are skipped) would be 1.0; 1 edit on a short
    word is a solid ~0.75+, scaling down as the ratio of edits to length
    grows, floored so it never reads as falsely certain."""
    return round(max(0.5, 1.0 - (distance / max(word_len, 1))), 3)


def _apply_fuzzy_spans(text: str, spans: List[Tuple[int, int, str, str, int]]) -> Tuple[str, List[Dict]]:
    result = text
    for start, end, substr, word, dist in sorted(spans, key=lambda c: -c[0]):
        result = result[:start] + word + result[end:]
    corrections = [{"from": s, "to": w, "confidence": _distance_to_confidence(d, len(w)), "method": "fuzzy"}
                   for _, _, s, w, d in spans]
    return result, corrections


def correct_query(question: str, carried_entities: Optional[Dict] = None) -> Dict:
    """Returns:
        {
          "original_query": question,
          "corrected_query": ...,
          "corrections": [{"from":..., "to":..., "confidence":..., "method": "direct"|"fuzzy"}, ...],
          "correction_confidence": float,  # min confidence across applied corrections, 1.0 if none
          "rejected_correction": Optional[str],   # the candidate that failed the guard, if any
          "rejection_reason": Optional[str],      # Developer Mode only — never shown to the customer
        }
    The original query is always preserved verbatim in the result (never
    mutated in place) — Explainability requirement 6. If nothing is
    corrected (no typo recognized, or everything protected/ambiguous),
    corrected_query == original_query and corrections == [] — i.e. the
    pipeline behaves exactly as before this feature existed.

    Semantic Invariant Guard (P0, 2026-07-21, rag/semantic_guard.py) —
    the direct+fuzzy correction above is a PROPOSAL only; before it's
    accepted, it's diffed against the original for an introduced/removed/
    changed entity, a flipped negation, or a Thai polite particle
    partially consumed into a business word (e.g. "โกดังหน่อย" ->
    "โกดังจีน่อย" — a real production bug this exact guard exists to
    catch generically, for every topic, not just this one). A rejected
    proposal reverts to the ORIGINAL query untouched and is reported in
    `rejected_correction`/`rejection_reason` for Developer Mode only.
    `carried_entities`, if given, lets an entity legitimately carried
    from the previous USER turn be accepted (Part 2.B) — never an entity
    that would only be justified by the previous ASSISTANT answer."""
    if not question:
        return {"original_query": question, "corrected_query": question, "corrections": [], "correction_confidence": 1.0,
                "rejected_correction": None, "rejection_reason": None}

    fallback = get_fallback_dict()
    text, direct_corrections, direct_protected = _apply_direct_corrections(question, fallback.get("direct_corrections", {}))

    vocabulary = get_vocabulary()
    spans = _find_fuzzy_spans(text, vocabulary, extra_protected=direct_protected) if vocabulary else []
    text, fuzzy_corrections = _apply_fuzzy_spans(text, spans)

    corrections = direct_corrections + fuzzy_corrections
    confidence = min((c["confidence"] for c in corrections), default=1.0)

    rejected_correction: Optional[str] = None
    rejection_reason: Optional[str] = None
    if text.strip() != question.strip():
        from rag.semantic_guard import validate_transformation
        only_direct = bool(corrections) and all(c.get("method") == "direct" for c in corrections)
        verdict = validate_transformation(question, text, carried_entities=carried_entities,
                                           trust_identifier_introduction=only_direct)
        if not verdict["accepted"]:
            rejected_correction = text
            rejection_reason = verdict["reason"]
            text = question
            corrections = []
            confidence = 1.0

    return {
        "original_query": question,
        "corrected_query": text,
        "corrections": corrections,
        "correction_confidence": confidence,
        "rejected_correction": rejected_correction,
        "rejection_reason": rejection_reason,
    }
