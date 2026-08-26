"""Generic hybrid retrieval scoring — combines vector similarity with
keyword/heading overlap so that a chunk with strong LEXICAL evidence
(exact heading match, exact package/entity name match, or a Thai-English
synonym match) isn't out-ranked or dropped by a fixed cosine cutoff.

This module is intentionally domain-agnostic: it never references any
specific product, filename, or topic (no "Google Drive", no "Shipify",
no "Mission" as a special case) — heading synonym matching is driven by
rag/query_expansion.py's generic glossary, which anyone can extend with
more terms without touching this file.

Root-cause fix (replaces the Phase 2 "weak_semantic" absolute cutoff):
the audit case "แล้วมิชชั่น คือ อะไร" against a chunk headed "Our Mission"
failed for TWO independent reasons, both fixed here:
  1. keyword/heading scoring was computed against ONLY the original,
     un-expanded question string — query_expansion.py's variants were
     computed by rag/searcher.py but never actually passed into this
     module, so an expanded "mission" variant could never help heading
     matching no matter how good the glossary was.
  2. even with variants wired in, a candidate with real hybrid evidence
     could still be discarded by a hard-coded relative-to-pool VECTOR
     threshold (0.75) that ignored keyword/heading scores entirely.
Both are fixed: scoring now takes the query_variants list, and filtering
is adaptive over the combined HYBRID score, never the raw/normalized
vector score alone.
"""
import re
from typing import Dict, List, Optional, Tuple

# NOTE: RAG_WEAK_SEMANTIC_RELATIVE_THRESHOLD was removed here (2026-08-01
# final config cleanup) — confirmed never referenced anywhere, including
# within this module itself. Adaptive filtering is driven by
# services/retrieval_settings.py instead. See LEGACY_ENV.md.

# Section headings considered generically "important" across any
# business document — never entity-specific, just common document
# structure. A chunk under one of these gets a small baseline boost so a
# short, generic heading ("FAQ", "Pricing") isn't starved of lexical
# credit purely because the question phrased the topic differently.
IMPORTANT_HEADINGS = {
    "mission", "our mission", "vision", "our vision", "services", "our services",
    "contact", "contact us", "company profile", "about us", "faq", "pricing",
    "policy", "shipping policy", "refund policy", "warranty",
}

_TOKEN_RE = re.compile(r"[a-zA-Z0-9._฀-๿-]+")

_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "be", "to", "of", "for",
    "and", "or", "what", "which", "who", "how", "do", "does", "did", "can",
    "ต้อง", "ใช้", "อะไร", "ยังไง", "หรือ", "แบบ", "ที่", "การ", "ใน", "กับ",
    "และ", "มี", "ค่ะ", "ครับ", "นะ", "แล้ว", "คือ",
    # Generic Thai question/permission particles — extremely common as a
    # trailing suffix on almost ANY FAQ-style question ("...ได้ไหม?",
    # "...ไหม?", "มีไหม?"), and "ขอ" as a leading request-filler verb
    # ("ขอราคา...", "ขอเรท..."). None of these carry topical/discriminative
    # meaning on their own — a chunk must never earn keyword credit purely
    # from sharing one of these (see rag/hybrid_scoring.py's module
    # docstring update below and compute_keyword_score's docstring).
    "ได้ไหม", "ไหม", "มีไหม", "ขอ",
}


def tokenize(text: str) -> List[str]:
    if not text:
        return []
    return [t.lower() for t in _TOKEN_RE.findall(text) if t.lower() not in _STOPWORDS and len(t) > 1]


def _normalize_heading(h: str) -> str:
    """lowercase / trim / strip punctuation — the exact-match half of
    normalized heading matching."""
    h = h.strip().lower()
    h = re.sub(r"[^\w\s฀-๿]", "", h)
    h = re.sub(r"\s+", " ", h).strip()
    return h


# Common Thai conditional/discourse particles that glue onto whatever
# word follows with no space (e.g. "ถ้าไตวายเรื้อรัง" = "ถ้า" + "ไตวายเรื้อรัง"
# as one contiguous run) — generic connectives, not domain vocabulary.
# Stripped as a PREFIX-only fallback when the full glued token doesn't
# substring-match anything, so a real entity/condition name buried behind
# one of these isn't missed just because of how it was phrased.
_THAI_LEADING_PARTICLES = ["ถ้าหาก", "หากว่า", "ถ้า", "หาก", "เมื่อ"]


def _strip_leading_particle(q_token: str) -> Optional[str]:
    for p in _THAI_LEADING_PARTICLES:
        if q_token.startswith(p) and len(q_token) > len(p) + 2:
            return q_token[len(p):]
    return None


def _thai_substring_hit(q_token: str, hay_tokens: List[str]) -> bool:
    """Thai script has no spaces between words, so _TOKEN_RE lumps a whole
    run of Thai characters into ONE token — a query token (e.g.
    "ไตวายเรื้อรัง") and the matching haystack phrase almost never tokenize
    to the exact same string (haystack: "โรคไตวายเรื้อรังโดย...", query
    possibly glued to neighboring words too, e.g. "ค่าห้องผู้ป่วยปกติเท่าไหร่").
    Exact token-set overlap alone therefore misses most real Thai matches.
    Substring containment (either direction) is a dependency-free stand-in
    for real word segmentation — only applied to short-ish tokens (>=3
    chars) to avoid single-character false positives.

    Falls back to stripping a leading Thai conditional particle (see
    _THAI_LEADING_PARTICLES) when the full glued token has no match — a
    question phrased "ถ้า<condition>" must not lose lexical credit for
    <condition> just because "ถ้า" (if) had no space after it."""
    if len(q_token) < 3:
        return False
    if any(q_token in ht or ht in q_token for ht in hay_tokens):
        return True
    stripped = _strip_leading_particle(q_token)
    if stripped and len(stripped) >= 3:
        return any(stripped in ht or ht in stripped for ht in hay_tokens)
    return False


# Long-Glued-Query Partial Overlap fix (Task 04, 2026-08-26) — confirmed
# live: a natural, punctuation-free Thai sentence ("ช่วงนี้ขนส่งทางรถใช้
# เวลานานไหมครับ") has no internal spaces at all, so _TOKEN_RE lumps the
# ENTIRE SENTENCE into ONE q_token. _thai_substring_hit's whole-token
# containment then degenerates into an all-or-nothing coin flip: the
# giant blob is too long/specific to ever be a substring of a short
# haystack token, so the ONLY way it can still "hit" is if some haystack
# token happens to recur verbatim inside it — rewarding whichever chunk
# has a generic, common word as an isolated token (e.g. a bare "ขนส่ง" Tag,
# shared by nearly every shipping-related FAQ row) while a chunk whose
# OWN genuinely specific term ("ทางรถ") is merely GLUED to other
# characters ("เรททางรถ", "ค่าส่งทางรถ" — never appearing as its own
# isolated haystack token) gets ZERO credit despite containing the exact
# topic phrase. A long q_token (LONG_TOKEN_THRESHOLD+) falls back to a
# graded n-gram overlap score instead of the same binary check, so a
# real shared sub-phrase between two glued strings (however each of them
# happens to be segmented) still counts as partial evidence.
_LONG_TOKEN_THRESHOLD = 10
# A genuine shared WORD/PHRASE, not a coincidental syllable overlap
# (Thai's short, high-frequency syllables recur across totally unrelated
# compound words — e.g. "ข้อ" is the shared prefix of both "ข้อมูล" (data)
# and "ข้อตกลง" (agreement), with no topical relation at all). Confirmed
# live: a naive fixed-size n-gram window fraction let exactly this kind
# of coincidence leak enough partial credit into an UNRELATED chunk to
# nudge a genuinely-unanswerable log_event_time query's confidence up
# past its low-confidence escalation threshold. Longest-common-substring
# is the more robust signal — one real 6+ character shared phrase counts,
# however many scattered 3-character syllables merely happen to coincide.
_MIN_SHARED_SUBSTRING_LEN = 8
# Reuses the SAME generic stopword vocabulary tokenize() already strips
# post-tokenization (so a spaced query never gets credit for matching
# only a filler word) — here applied as substring removal INSIDE a long
# glued blob, where spaces never isolated these words into their own
# tokens in the first place. Longer entries first, so e.g. "ได้ไหม" is
# stripped whole rather than leaving a dangling "ไหม" match after a
# partial strip. Purely noise reduction for the n-gram fallback below —
# never applied to short tokens, never changes tokenize()'s own output.
_STOPWORD_STRIP_TERMS = sorted(_STOPWORDS, key=len, reverse=True)


def _strip_glued_stopwords(text: str) -> str:
    """Removes known generic filler/particle substrings from a long,
    space-free Thai blob before longest-common-substring scoring — see
    _STOPWORD_STRIP_TERMS above. Shortens the blob so a genuine shared
    sub-phrase (e.g. "ทางรถ") makes up a larger fraction of what remains,
    instead of being diluted by connector/particle noise a spaced query
    would never have glued onto it in the first place."""
    result = text
    for term in _STOPWORD_STRIP_TERMS:
        result = result.replace(term, " ")
    return result


def _longest_common_substring_len(a: str, b: str) -> int:
    """Length of the longest contiguous substring shared by `a` and `b`.
    Dependency-free dynamic-programming solution (O(len(a)*len(b)),
    fine for the short haystack/query tokens involved here)."""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for i in range(1, len(a) + 1):
        curr = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                curr[j] = prev[j - 1] + 1
                best = max(best, curr[j])
        prev = curr
    return best


def _thai_ngram_overlap_score(q_token: str, hay_tokens: List[str]) -> float:
    """Graded partial-credit score for a long, glued Thai token that
    whole-token substring containment (_thai_substring_hit) already
    failed to match either direction. Finds the longest genuinely shared
    substring against any haystack token; below _MIN_SHARED_SUBSTRING_LEN
    it's treated as coincidental noise (0.0) — a real match only counts
    once it's long enough to represent an actual shared word/phrase, not
    a common short syllable. Score is that shared length as a fraction of
    the (stopword-stripped) token's own length, so a chunk sharing MOST of
    the query's real content scores higher than one sharing only a small
    fragment of it."""
    q_token = _strip_glued_stopwords(q_token).replace(" ", "")
    if len(q_token) < _MIN_SHARED_SUBSTRING_LEN:
        return 0.0
    best_len = max((_longest_common_substring_len(q_token, ht) for ht in hay_tokens), default=0)
    if best_len < _MIN_SHARED_SUBSTRING_LEN:
        return 0.0
    return best_len / len(q_token)


# Knowledge Synonym Engine (rag/synonym_service.py) variants carry LESS
# weight than the original question or the pre-existing Thai-English
# glossary (rag/query_expansion.py) variants — "original query always has
# highest priority; expanded synonym queries have lower weight; never let
# a synonym rewrite replace the user's original wording." Applied as a
# discount on a synonym-only variant's contribution BEFORE taking the max
# across all variants, so the original's own score (or any non-synonym
# variant's) is never reduced or displaced — only a match that ONLY a
# synonym variant found gets counted at a discount.
SYNONYM_VARIANT_WEIGHT = 0.85


def compute_keyword_score(question: str, chunk: Dict, query_variants: Optional[List[str]] = None,
                           synonym_variant_keys: Optional[set] = None) -> float:
    """Fraction of the question's meaningful tokens that appear literally
    (or, for Thai, as a substring — see _thai_substring_hit) in the
    chunk's text/heading/section — computed across the ORIGINAL question
    AND every query-expansion variant, taking the best score. A
    dependency-free stand-in for BM25/full-text search.

    Generic Thai/English question particles (ได้ไหม/ไหม/มีไหม/ขอ/can/is/
    are/what/how/...) are stripped by tokenize()'s _STOPWORDS before this
    ever runs — a question that's just filler once those are removed
    (empty q_tokens) scores 0.0, never a false "100%" from matching only
    a generic suffix shared with an unrelated chunk.

    `synonym_variant_keys`, if given (lowercased, stripped strings — see
    rag/searcher.py's call site), marks which variants came from the
    Knowledge Synonym Engine specifically; their contribution is
    discounted by SYNONYM_VARIANT_WEIGHT before the max() below, so they
    can never outrank a match the original question (or an existing
    glossary variant) already found."""
    variants = query_variants if query_variants else [question]
    haystack = " ".join(filter(None, [
        chunk.get("text") or "",
        chunk.get("section_title") or "",
        " ".join(chunk.get("heading_path") or []),
        chunk.get("file_name") or chunk.get("source") or "",
    ]))
    hay_tokens_set = set(tokenize(haystack))
    if not hay_tokens_set:
        return 0.0
    hay_tokens_list = list(hay_tokens_set)

    best = 0.0
    for variant in variants:
        # Negation-aware: a word named only to be EXCLUDED (e.g. "จีน" in
        # "โกดังที่ไม่ใช่จีน") must never count as positive keyword
        # evidence for a chunk that happens to contain that same word
        # (e.g. "โกดังจีน") — strip negated spans before tokenizing, reusing
        # rag/query_resolution.py's negation logic rather than a second copy.
        from rag.query_resolution import strip_negated_spans
        q_tokens = set(tokenize(strip_negated_spans(variant)))
        if not q_tokens:
            continue
        hits = 0.0
        for qt in q_tokens:
            if qt in hay_tokens_set or _thai_substring_hit(qt, hay_tokens_list):
                hits += 1
            elif len(qt) >= _LONG_TOKEN_THRESHOLD:
                # Long-Glued-Query Partial Overlap fix (Task 04,
                # 2026-08-26) — whole-token containment already failed
                # both directions; for a long, multi-concept glued token
                # (the common case for a natural, space-free Thai
                # sentence) fall back to graded n-gram overlap rather
                # than counting this token as zero evidence outright.
                hits += _thai_ngram_overlap_score(qt, hay_tokens_list)
        score = hits / len(q_tokens)
        if synonym_variant_keys and variant.strip().lower() in synonym_variant_keys:
            score *= SYNONYM_VARIANT_WEIGHT
        best = max(best, score)
    return best


def _heading_match_tier(headings: List[str], variant: str) -> Tuple[str, float]:
    """Returns (tier, overlap_fraction) for the BEST heading match of a
    single query variant — "exact" (normalized heading == normalized
    variant), "partial" (some token overlap), or "none"."""
    norm_variant = _normalize_heading(variant)
    v_tokens = set(tokenize(variant))
    if not v_tokens:
        return "none", 0.0

    best_tier, best_overlap = "none", 0.0
    for h in headings:
        norm_h = _normalize_heading(h)
        if norm_h and norm_h == norm_variant:
            return "exact", 1.0
        h_tokens = set(tokenize(h))
        if not h_tokens:
            continue
        overlap = len(h_tokens & v_tokens) / len(h_tokens)
        if overlap > best_overlap:
            best_overlap = overlap
            best_tier = "partial" if overlap > 0 else "none"
    return best_tier, best_overlap


def compute_heading_score(question: str, chunk: Dict, query_variants: Optional[List[str]] = None,
                            settings=None, synonym_variant_keys: Optional[set] = None) -> float:
    """Normalized heading matching: lowercase/trim/punctuation-stripped/
    tokenized comparison between EVERY query variant (original + Thai-
    English-synonym expansions) and the chunk's heading(s), plus a small
    flat boost for generically "important" headings (FAQ, Pricing,
    Policy, ...). Returns a single float score — see
    compute_heading_match_info() for the full match-tier/matched-variant
    explainability payload."""
    return compute_heading_match_info(question, chunk, query_variants, settings, synonym_variant_keys)["score"]


def compute_heading_match_info(question: str, chunk: Dict, query_variants: Optional[List[str]] = None,
                                 settings=None, synonym_variant_keys: Optional[set] = None) -> Dict:
    from services.retrieval_settings import get_active_settings
    settings = settings or get_active_settings()

    # Only the chunk's OWN specific (leaf) heading is used for exact/
    # partial match scoring — NOT the full heading_path ancestor chain.
    # heading_path includes the document's top-level title as its first
    # element for every section in that document (e.g. every section of
    # company-profile-test.md carries "Shipify Company Profile" as
    # heading_path[0]); scoring against the whole path let a query that
    # merely shared a word with the DOCUMENT title (e.g. "company") give
    # a false-positive partial-match boost to every unrelated section in
    # that same file. heading_path is still fine for other uses (display,
    # IMPORTANT_HEADINGS check below); only the match-tier boost is
    # restricted to the actual section this chunk represents.
    full_path = list(chunk.get("heading_path") or [])
    leaf_heading = chunk.get("section_title") or (full_path[-1] if full_path else None)
    headings = [leaf_heading] if leaf_heading else []
    variants = query_variants if query_variants else [question]

    if not headings:
        return {"score": 0.0, "match_type": "none", "matched_heading": None, "matched_variant": None}

    best_score = 0.0
    best_type = "none"
    best_heading = None
    best_variant = None
    any_real_match = False
    for i, variant in enumerate(variants):
        is_original = (i == 0)
        tier, overlap = _heading_match_tier(headings, variant)
        if tier == "exact":
            boost = settings.heading_exact_boost + overlap
            match_type = "direct_heading" if is_original else "synonym_heading"
            any_real_match = True
        elif tier == "partial" and overlap > 0:
            boost = settings.heading_partial_boost + overlap * 0.5
            match_type = "direct_heading" if is_original else "synonym_heading"
            any_real_match = True
        else:
            boost = 0.0
            match_type = "none"
        if synonym_variant_keys and variant.strip().lower() in synonym_variant_keys:
            boost *= SYNONYM_VARIANT_WEIGHT
        if boost > best_score:
            best_score = boost
            best_type = match_type
            best_heading = headings[0] if headings else None
            best_variant = variant

    # Only a SUPPLEMENT to an already-real match (see IMPORTANT_HEADINGS'
    # docstring: "short generic heading isn't starved of credit") — never a
    # standalone source of evidence. Without the any_real_match guard, any
    # chunk headed "Contact Us"/"Our Mission"/etc. got a nonzero
    # heading_score for EVERY query regardless of relevance, which let it
    # masquerade as "real evidence" and (via has_real_evidence_elsewhere in
    # apply_hybrid_ranking) crowd out genuinely relevant chunks whose Thai
    # keyword match happened to score 0.
    if any_real_match and any(_normalize_heading(h) in IMPORTANT_HEADINGS for h in headings):
        best_score += settings.important_heading_boost

    return {"score": best_score, "match_type": best_type if best_score > 0 else "none",
            "matched_heading": best_heading, "matched_variant": best_variant}


# ── Purpose-aware ranking boost ────────────────────────────────
# Generic: distinguishes documents that legitimately share heavy
# vocabulary overlap (a benefit brochure and a premium-rate table for the
# SAME product both say "Plan 1/2/3/4" constantly) by preferring the
# document whose PURPOSE (services/document_purpose.py, stamped at
# ingest time) matches the query's INTENT (rag/intent_classifier.py,
# deterministic, no LLM). A ranking preference only — never a hard
# filter, so a chunk from a "wrong-purpose" document can still surface
# (and win) on strong keyword/heading/vector evidence alone.
PURPOSE_MATCH_BOOST = 0.12
# Applied only for a document_purpose that's CLEARLY the other category's
# opposite number (coverage vs. premium — see _INTENT_TO_INCOMPATIBLE
# below), never for merely "not an exact match" (e.g. "general" or
# "policy" are never penalized against a coverage_benefit query — only
# genuinely incompatible purposes are). Still a soft ranking adjustment,
# not a filter: it can reduce a mismatched chunk's evidence tier back down
# to "supporting_evidence", but never removes it from the candidate pool.
PURPOSE_MISMATCH_PENALTY = 0.15

_INTENT_TO_PURPOSES = {
    "coverage_benefit": {"coverage_brochure"},
    "premium_price": {"premium_monthly", "premium_annual"},
    "eligibility": {"coverage_brochure", "policy"},
    "exclusion": {"coverage_brochure", "policy"},
    "general_product": set(),
    "unknown": set(),
}
_INTENT_TO_SIGNALS = {
    "coverage_benefit": "coverage",
    "premium_price": "premium",
    "eligibility": "coverage",
    "exclusion": "policy",
}
# Only pairs that are structurally each other's opposite (a benefit
# amount is never found in a pure premium-rate table and vice versa) —
# deliberately NOT exhaustive (eligibility/exclusion have no listed
# incompatible purposes; those questions can legitimately be answered
# from a coverage brochure OR policy wording, so no penalty applies).
_INTENT_TO_INCOMPATIBLE = {
    "coverage_benefit": {"premium_monthly", "premium_annual"},
    "premium_price": {"coverage_brochure"},
}


# ── Log-event-time evidence (generic: HH:MM:SS clock stamps + startup
# vocabulary) ────────────────────────────────────────────────────────
# For a "when did X start" log query, a bare "ระบบ" (system) token match
# is nearly worthless as evidence — it's one of the most common words in
# any internal document (contracts, DevOps notes, etc.) — while a real log
# line combining an actual clock timestamp WITH start/launch vocabulary is
# strong, specific evidence that should survive even a low vector score.
_LOG_TIMESTAMP_RE = re.compile(r"\b\d{1,2}:\d{2}:\d{2}\b")
_LOG_STARTUP_TERMS = {
    "started", "startup", "start", "launched", "launch", "initialized",
    "init", "boot", "booted", "scheduler", "เริ่มทำงาน", "เริ่มระบบ",
    "เริ่มต้น", "เปิดระบบ", "เริ่ม", "เริ่มรอบ",
}
# Generic tokens that must NOT, on their own, count as real lexical
# evidence for a log_event_time query — stripped from the query variants
# before keyword/heading scoring runs (only for this intent), so a
# candidate can't earn evidence credit purely by containing the word
# "system"/"ระบบ" the way almost every internal document does.
_LOG_TIME_NOISE_TERMS = ["ระบบ", "system"]


def has_log_time_evidence(chunk: Dict) -> bool:
    """True only when a chunk contains BOTH a clock-like timestamp
    (HH:MM:SS) AND startup/start-event vocabulary — the deterministic
    dual-signal a log_event_time query is actually looking for. Neither
    signal alone is enough (a timestamp alone could be any log line; a
    "started" mention alone could be prose with no time attached)."""
    text = chunk.get("text") or ""
    if not _LOG_TIMESTAMP_RE.search(text):
        return False
    lower = text.lower()
    return any(term in lower for term in _LOG_STARTUP_TERMS)


def _strip_log_time_noise(text: str) -> str:
    """Removes generic log_event_time noise terms (e.g. "ระบบ") from a
    query variant before keyword/heading scoring — see
    _LOG_TIME_NOISE_TERMS above. Only ever called for query_intent ==
    "log_event_time"; every other intent scores the untouched variant."""
    result = text
    for term in _LOG_TIME_NOISE_TERMS:
        result = re.sub(re.escape(term), " ", result, flags=re.IGNORECASE)
    return result


# ── Duration-query evidence (generic: an explicit day-count/day-range in
# the chunk text — no product/company name) ──────────────────────────
# Task 04 (2026-08-26) — confirmed live: "ช่วงนี้ขนส่งทางรถใช้เวลานานไหม
# ครับ" (a genuine "how long does shipping-by-truck take" question) did
# NOT retrieve the chunk that actually answers it ("เรทเท่าไหร่คะ", a rate
# FAQ row whose body ALSO states "ระยะเวลา 7-10วัน...") highly enough to
# get cited — a DIFFERENT chunk whose HEADING literally contains the word
# "ระยะเวลา" ("ระยะเวลาการส่งจากร้านจีน-โกดังจีน", answering a completely
# different leg of the shipping process) outranked it purely because the
# correct chunk's own heading is about "rate", not "duration", even though
# its body carries the real answer. Mirrors has_log_time_evidence exactly:
# a chunk containing an explicit numeric day-count/range is strong,
# specific evidence for a duration_query regardless of its heading, and
# must not be at the mercy of a heading-keyword mismatch. Deliberately a
# pure text-pattern check — never a hardcoded shipping fact/number, and
# never touches the knowledge source's own content — so it applies
# equally to ANY future "how long does X take" question (warranty period,
# processing time, etc.), not just shipping.
_DURATION_PATTERN_RE = re.compile(
    # No \b immediately after "วัน" — Thai has no spaces between words, so
    # a trailing politeness particle ("วันค่ะ") shares no word-boundary
    # with "วัน" at all (Python's \w treats Thai letters as word chars).
    # "days?" still gets its own \b (English text does have real spaces)
    # so this never matches inside an unrelated word like "Monday".
    r"\d+\s*(?:[-–~]|to)\s*\d+\s*(?:วัน|days?\b)|\d+\s*(?:วัน|days?\b)", re.IGNORECASE)


def has_duration_evidence(chunk: Dict) -> bool:
    """True when a chunk's text contains an explicit day-count or
    day-range (e.g. "7-10วัน", "2–4 วัน", "5 days") — see the module-level
    comment above for why this exists and why it's a pure text-pattern
    check, never a hardcoded fact."""
    text = chunk.get("text") or ""
    return bool(_DURATION_PATTERN_RE.search(text))


_MONTHLY_QUERY_HINTS = ["รายเดือน", "ต่อเดือน", "monthly"]
_ANNUAL_QUERY_HINTS = ["รายปี", "ต่อปี", "annual", "yearly"]


def _refine_premium_purpose(question: Optional[str]) -> Optional[str]:
    """When the query itself names monthly or annual explicitly (e.g.
    "Plan 4 เบี้ยรายปีเท่าไหร่"), premium_price should prefer THAT specific
    premium table over the other one — both are otherwise equally
    "premium", so without this a monthly and an annual rate table get
    the identical boost and the wrong one can still win on raw score.
    Returns None (no refinement — both premium purposes stay equally
    boosted) if the question doesn't name one specifically."""
    if not question:
        return None
    q = question.lower()
    has_monthly = any(h in q for h in _MONTHLY_QUERY_HINTS)
    has_annual = any(h in q for h in _ANNUAL_QUERY_HINTS)
    if has_monthly and not has_annual:
        return "premium_monthly"
    if has_annual and not has_monthly:
        return "premium_annual"
    return None


def compute_purpose_boost(query_intent: Optional[str], chunk: Dict, question: Optional[str] = None) -> float:
    """Signed adjustment: a moderate positive boost when the chunk's
    document_purpose matches the query's intent (smaller if only the
    finer-grained per-chunk content_signals match — covers a file whose
    overall purpose wasn't decisively classified, e.g. "general"); a
    moderate negative adjustment when the purpose is CLEARLY the
    opposite category (coverage vs. premium) — this is what keeps a
    same-vocabulary chunk from the wrong-purpose document (e.g. a
    premium table that happens to literally contain the query's words)
    from still winning purely on keyword overlap. Returns 0.0 for
    unknown/general_product intent or a purpose with no defined
    relationship to this intent. Still a ranking adjustment only —
    apply_hybrid_ranking() never drops a candidate because of this value
    alone.

    `question`, if given, lets premium_price further prefer the SPECIFIC
    monthly/annual table the query names (see _refine_premium_purpose) —
    both premium purposes are otherwise equally compatible with the
    single "premium_price" intent bucket."""
    if not query_intent or query_intent in ("unknown", "general_product"):
        return 0.0
    document_purpose = chunk.get("document_purpose")

    if query_intent == "premium_price":
        refined = _refine_premium_purpose(question)
        if refined:
            if document_purpose == refined:
                return PURPOSE_MATCH_BOOST
            if document_purpose in _INTENT_TO_PURPOSES["premium_price"]:
                # The other premium table (right general category, wrong
                # specific period) — still clearly better than a coverage
                # brochure, just not the best match. A small boost, not
                # zero and not a penalty.
                return PURPOSE_MATCH_BOOST * 0.3

    compatible_purposes = _INTENT_TO_PURPOSES.get(query_intent, set())
    if document_purpose in compatible_purposes:
        return PURPOSE_MATCH_BOOST
    incompatible_purposes = _INTENT_TO_INCOMPATIBLE.get(query_intent, set())
    if document_purpose in incompatible_purposes:
        return -PURPOSE_MISMATCH_PENALTY
    signal = _INTENT_TO_SIGNALS.get(query_intent)
    if signal and signal in (chunk.get("content_signals") or []):
        return PURPOSE_MATCH_BOOST * 0.5
    return 0.0


# ── Company-overview retrieval boost (P0, 2026-07-20) ───────────────────
# A "บริษัททำธุรกิจเกี่ยวกับอะไร"-style question was losing to lexically
# closer but less-relevant FAQ rows ("มีบริการอะไรบ้าง", "ขอเบอร์ติดต่อ",
# "มีบริการตีลังไม้ไหม") purely on keyword-overlap grounds. These boosts
# are ranking-only additions (same additive pattern as compute_purpose_
# boost/log_time_boost below) — they never lower or bypass the adaptive
# relevance threshold, and never apply for any intent other than
# "company" (rag/query_understanding.py::detect_intent).
_COMPANY_OVERVIEW_FAQ_RE = re.compile(r"บริษัท(นี้)?ทำธุรกิจ(เกี่ยวกับ)?อะไร")
_COMPANY_SERVICES_FAQ_RE = re.compile(r"มีบริการอะไรบ้าง")

# Priority 3 (task Part 2) — company-profile section/heading vocabulary.
# Generic label words, never a specific company name.
_COMPANY_PROFILE_HEADING_RE = re.compile(
    r"company\s*profile|เกี่ยวกับบริษัท|ธุรกิจของบริษัท|บริการหลัก|about|overview",
    re.IGNORECASE,
)
# Priority 4 — FAQ rows describing the actual core business (import from
# China / core service names). Broader than the original China/import/
# shipping keyword list, per the task's explicit vocabulary.
_COMPANY_KEYWORD_BOOST_TERMS = [
    "จีน", "china", "นำเข้า", "import", "ขนส่ง", "shipping", "โกดังจีน",
    "สั่งซื้อสินค้าจากจีน", "ฝากสั่ง", "ฝากนำเข้า", "ฝากโอนเงิน",
]

# Priority 5 — secondary/off-topic categories that must never outrank
# true business-description evidence for a company_overview/company_summary
# question. Generic keyword groups (never a specific company name) —
# these chunks REMAIN candidates (never excluded outright, per the task's
# "may remain candidates but must not outrank" requirement), just ranked
# lower via a negative boost, exactly like compute_purpose_boost's
# mismatch penalty below.
_COMPANY_NEGATIVE_TERMS = {
    "contact": ["เบอร์", "โทร", "ติดต่อ", "contact", "phone"],
    "warehouse_hours": ["เวลาทำการ", "เปิดทำการ", "ปิดทำการ", "opening hours", "operating hours"],
    "shipping_price": ["ค่าขนส่ง", "เรท", "อัตราค่าขนส่ง", "ราคา", "rate", "price"],
    "prohibited_goods": ["สินค้าต้องห้าม", "ห้ามส่ง", "ของต้องห้าม", "prohibited"],
    "payment": ["ชำระเงิน", "ชำระบิล", "จ่ายบิล", "payment"],
    "coupon": ["คูปอง", "ส่วนลด", "coupon"],
    "crate_service": ["ตีลัง", "crate"],
}

# Large enough to guarantee rank 1 among any realistic small candidate
# pool (hybrid_score is otherwise bounded ~0-1.5 even with other boosts
# stacked) — this is the "always boost to top" requirement, applied only
# when the exact company-overview FAQ row genuinely exists in the pool.
_COMPANY_OVERVIEW_EXACT_BOOST = 5.0
_COMPANY_SERVICES_FAQ_BOOST = 0.3
_COMPANY_KEYWORD_BOOST = 0.15
_COMPANY_PROFILE_HEADING_BOOST = 0.25
_COMPANY_NEGATIVE_BOOST = -0.2

# actionable_intent values (rag/query_understanding.py) for which the
# Priority-5 negative/exclusion boost applies. Never applied for any
# OTHER intent — a question actually classified as "warehouse_contact"
# or "shipping_rate", say, is a completely different actionable_intent
# and never reaches this function with one of these two values, so its
# own contact/rate evidence is never penalized.
_COMPANY_NARROW_INTENTS = ("company_overview", "company_summary")


def compute_company_intent_boost(company_intent: bool, chunk: Dict,
                                  actionable_intent: Optional[str] = None) -> float:
    """Returns 0.0 whenever company_intent is False — never affects any
    other query. Otherwise, additive tiers (never a hard exclusion — the
    adaptive relevance threshold below is completely untouched):
      1. The exact "บริษัททำธุรกิจเกี่ยวกับอะไร" FAQ row (if present) is
         always boosted to the top of the pool.
      2. The "มีบริการอะไรบ้าง" FAQ row gets a supporting boost.
      3. A chunk whose own section/heading uses company-profile
         vocabulary (Company Profile / เกี่ยวกับบริษัท / บริการหลัก / ...)
         gets a supporting boost.
      4. A chunk whose text/heading describes the core import/shipping
         business gets a smaller supporting boost.
      5. When actionable_intent is company_overview or company_summary
         (the two intents this fix adds), a chunk whose main content is a
         secondary/off-topic category (contact, phone, warehouse hours,
         shipping price, prohibited goods, payment, coupon, crate
         service) gets a NEGATIVE boost — it remains a candidate, it just
         must not outrank real business-description evidence.
    Boosts 2-5 can stack (except boost 1, which is decisive on its own)."""
    if not company_intent:
        return 0.0
    text = chunk.get("text") or ""
    section = chunk.get("section_title") or ""
    if _COMPANY_OVERVIEW_FAQ_RE.search(text) or _COMPANY_OVERVIEW_FAQ_RE.search(section):
        return _COMPANY_OVERVIEW_EXACT_BOOST
    boost = 0.0
    if _COMPANY_SERVICES_FAQ_RE.search(text) or _COMPANY_SERVICES_FAQ_RE.search(section):
        boost += _COMPANY_SERVICES_FAQ_BOOST
    if _COMPANY_PROFILE_HEADING_RE.search(section):
        boost += _COMPANY_PROFILE_HEADING_BOOST
    haystack = (text + " " + section).lower()
    if any(term.lower() in haystack for term in _COMPANY_KEYWORD_BOOST_TERMS):
        boost += _COMPANY_KEYWORD_BOOST
    if actionable_intent in _COMPANY_NARROW_INTENTS:
        for terms in _COMPANY_NEGATIVE_TERMS.values():
            if any(term.lower() in haystack for term in terms):
                boost += _COMPANY_NEGATIVE_BOOST
                break
    return boost


def has_strong_company_profile_evidence(chunks: List[Dict]) -> bool:
    """True when at least one chunk carries decisive (exact FAQ match) or
    strong supporting (a real Company Profile/เกี่ยวกับบริษัท/บริการหลัก
    heading) company-profile evidence. Checks the SAME two regexes
    compute_company_intent_boost uses for its decisive/heading tiers
    directly — deliberately NOT inferring this from the combined boost
    MAGNITUDE (a generic services-FAQ boost can coincidentally reach the
    same numeric size as the heading-tier boost without actually being
    profile evidence). Used by services/playground_orchestrator.py
    (Part 10, Knowledge Gap Handling) to decide whether a company_
    overview/company_summary answer stands on real, explicit company-
    profile evidence vs. a synthesis from generic service FAQ rows that
    merely happen to mention import/shipping vocabulary — a Developer-
    Mode-only signal, never shown to the customer, never used to block
    or alter the answer itself."""
    for c in chunks:
        text = c.get("text") or ""
        section = c.get("section_title") or ""
        if _COMPANY_OVERVIEW_FAQ_RE.search(text) or _COMPANY_OVERVIEW_FAQ_RE.search(section):
            return True
        if _COMPANY_PROFILE_HEADING_RE.search(section):
            return True
    return False


# Shared tier ordering — real evidence always outranks a pure-semantic
# guess. Exposed at module level (not local to apply_hybrid_ranking) so
# services/reranker.py's final re-sort (which must preserve the SAME tier
# ordering while only re-ranking within a tier) uses the identical
# definition instead of a second hardcoded copy that could drift.
TIER_ORDER = {"structured_deterministic": 0, "direct_evidence": 1,
              "supporting_evidence": 2, "weak_semantic": 3}


def purpose_adjusted_tier(chunk: Dict) -> int:
    """The chunk's tier, shifted one level up (toward higher priority) for
    a purpose MATCH or one level down for a clear purpose MISMATCH (see
    compute_purpose_boost — positive/negative respectively; 0.0 leaves
    the tier untouched). This is what lets a purpose-correct chunk with
    only semantic/supporting evidence still outrank a purpose-INCORRECT
    chunk that happens to have strong literal keyword overlap, without
    having to fake that chunk's own evidence_label/classification.

    Also folds in company_intent_boost (see compute_company_intent_boost)
    the same way: the FINAL sort key is (tier, -hybrid_score), so a boost
    that only raised hybrid_score would still lose to a better-tier chunk
    on a lexically-closer-but-less-relevant FAQ row — exactly the
    original company-overview regression. The decisive exact-FAQ boost
    forces tier 0 outright (the "always boost to top" requirement); the
    smaller supporting boosts shift the tier up by one, same treatment as
    a purpose match."""
    base = TIER_ORDER.get(chunk.get("classification"), 9)
    purpose_boost = chunk.get("purpose_boost") or 0.0
    if purpose_boost > 0:
        base = max(0, base - 1)
    elif purpose_boost < 0:
        base = base + 1

    company_boost = chunk.get("company_intent_boost") or 0.0
    if company_boost >= _COMPANY_OVERVIEW_EXACT_BOOST:
        return 0
    if company_boost > 0:
        base = max(0, base - 1)
    elif company_boost < 0:
        base = base + 1
    return base



# Applied to any candidate chunk whose text contains a term the current
# conversation turn explicitly excluded (e.g. location=จีน after "ที่ไม่ใช่จีน")
# — a steep down-rank, never a hard removal, so a China-only pool still
# surfaces something rather than returning empty (relies on the existing
# adaptive-filter minimum_candidates floor below for that guarantee).
EXCLUDED_TERM_PENALTY_FACTOR = 0.3


def apply_hybrid_ranking(question: str, chunks: List[Dict], return_excluded: bool = False,
                          query_variants: Optional[List[str]] = None, settings=None,
                          query_intent: Optional[str] = None, synonym_variant_keys: Optional[set] = None,
                          excluded_terms: Optional[List[str]] = None, company_intent: bool = False,
                          actionable_intent: Optional[str] = None):
    """Scores, re-ranks, and ADAPTIVELY filters a list of chunk dicts (the
    exact shape rag/searcher.py's search() already returns).

    Adds keyword_score/heading_score/hybrid_score/raw_vector_rank/
    raw_vector_similarity/normalized_vector_score/classification/
    evidence_label/matched_query/matched_heading to every chunk it keeps.

    `query_variants`, if given (rag/searcher.py always passes the
    query-expansion output), is what actually fixes the root-cause bug:
    keyword/heading scoring is computed against every variant, not just
    the raw original question.

    `synonym_variant_keys`, if given (rag/searcher.py's Knowledge Synonym
    Engine variants — see rag/synonym_service.py), marks which of
    `query_variants` came from synonym expansion specifically; their
    contribution to keyword_score/heading_score is discounted (see
    SYNONYM_VARIANT_WEIGHT) so they can enrich matching without ever
    outranking the original question or a pre-existing glossary variant.

    Structured/deterministic results (is_structured or is_calculated) are
    never rescored or dropped: they're already the product of a targeted,
    deterministic lookup.

    When return_excluded=True, returns (kept, excluded) where `excluded`
    is [{**chunk, "exclusion_reason": str}] for debug/Playground
    visibility. Default False keeps the original single-list return.
    """
    if settings is None:
        from services.retrieval_settings import get_active_settings
        settings = get_active_settings()
    variants = query_variants if query_variants else [question]
    is_log_time_query = query_intent == "log_event_time"
    is_duration_query = query_intent == "duration_query"
    # Noise-stripped variants used ONLY for keyword/heading scoring on a
    # log_event_time query — see _strip_log_time_noise's docstring. The
    # original `variants` (and `question`) are still used everywhere else
    # (matched_query display, non-log intents), so no other query path is
    # affected by this at all.
    scoring_variants = [_strip_log_time_noise(v) for v in variants] if is_log_time_query else variants

    for i, c in enumerate(chunks):
        c["raw_vector_rank"] = i + 1

    structured = [c for c in chunks if c.get("is_structured") or c.get("is_calculated")]
    candidates = [c for c in chunks if not (c.get("is_structured") or c.get("is_calculated"))]

    vector_scores = [c.get("score") or 0.0 for c in candidates]
    max_vector = max(vector_scores) if vector_scores else 0.0
    min_vector = min(vector_scores) if vector_scores else 0.0
    vector_range = max_vector - min_vector

    weights = settings.effective_weights()

    kept: List[Dict] = []
    excluded: List[Dict] = []

    for c in structured:
        c["keyword_score"] = None
        c["heading_score"] = None
        c["graph_score"] = None
        c["normalized_vector_score"] = None
        c["raw_vector_similarity"] = c.get("score")
        c["classification"] = "structured_deterministic"
        c["evidence_label"] = "direct_keyword"
        c["hybrid_score"] = c.get("score", 1.0)
        c["matched_query"] = question
        c["matched_heading"] = None
        kept.append(c)

    for c in candidates:
        keyword_score = compute_keyword_score(question, c, scoring_variants, synonym_variant_keys)
        heading_info = compute_heading_match_info(question, c, scoring_variants, settings, synonym_variant_keys) \
            if settings.heading_boost_enabled \
            else {"score": 0.0, "match_type": "none", "matched_heading": None, "matched_variant": None}
        heading_score = heading_info["score"]
        vector_score = c.get("score") or 0.0
        graph_score = 0.0  # no retrieval-time graph signal wired in yet — see report's Known Limitations

        if vector_range > 0:
            normalized_vector = (vector_score - min_vector) / vector_range
        else:
            normalized_vector = 1.0 if vector_score > 0 else 0.0

        pre_boost_hybrid = (
            normalized_vector * weights["semantic"]
            + keyword_score * weights["keyword"]
            + heading_score * weights["heading"]
            + graph_score * weights["graph"]
        )
        purpose_boost = compute_purpose_boost(query_intent, c, question)
        log_time_evidence = is_log_time_query and has_log_time_evidence(c)
        # Fixed, deterministic boost (not settings-tunable) — a real
        # timestamp+startup-event match is decisive evidence for this
        # intent regardless of how the pool's vector scores happen to be
        # distributed, so it must not be at the mercy of relative-to-best
        # ratios computed over an otherwise-irrelevant candidate pool.
        log_time_boost = 0.5 if log_time_evidence else 0.0
        duration_evidence = is_duration_query and has_duration_evidence(c)
        # Same fixed, deterministic weight as log_time_boost above, for the
        # same reason: an explicit day-count/range in the chunk's own text
        # is decisive, specific evidence for a duration_query, regardless
        # of whether its heading happens to contain "ระยะเวลา" or not.
        duration_boost = 0.5 if duration_evidence else 0.0
        company_boost = compute_company_intent_boost(company_intent, c, actionable_intent=actionable_intent)
        hybrid = pre_boost_hybrid + purpose_boost + log_time_boost + duration_boost + company_boost

        excluded_term_hit = False
        if excluded_terms:
            chunk_text = c.get("text") or ""
            if any(term and term in chunk_text for term in excluded_terms):
                excluded_term_hit = True
                hybrid *= EXCLUDED_TERM_PENALTY_FACTOR

        c["keyword_score"] = round(keyword_score, 4)
        c["heading_score"] = round(heading_score, 4)
        c["graph_score"] = round(graph_score, 4)
        c["query_intent"] = query_intent
        c["score_before_purpose_boost"] = round(pre_boost_hybrid, 4)
        c["purpose_boost"] = round(purpose_boost, 4)
        c["company_intent_boost"] = round(company_boost, 4)
        c["normalized_vector_score"] = round(normalized_vector, 4)
        c["raw_vector_similarity"] = vector_score
        c["hybrid_score"] = round(hybrid, 4)
        c["excluded_term_penalty"] = excluded_term_hit
        c["matched_query"] = heading_info.get("matched_variant") or question
        c["matched_heading"] = heading_info.get("matched_heading")

        # Evidence label — a candidate with ANY real heading/keyword/
        # synonym support (has_lexical_evidence) is never subject to the
        # adaptive vector-based filter below and never labeled merely
        # "weak", even with a low raw vector score. Only a PURE-vector
        # candidate (zero lexical evidence at all) is judged on
        # normalized_vector / adaptive filtering — this mirrors the
        # pre-existing architecture (has_lexical_evidence chunks were
        # never excluded before either) instead of letting a single
        # high-vector-but-irrelevant chunk's inflated hybrid_score push a
        # genuine lexical match below the pool's relative-to-best ratio.
        # log_time_evidence (an actual timestamp + startup vocabulary
        # match, not just a raw token overlap) counts as real lexical
        # evidence on its own — it must never be gated behind
        # keyword/heading scores, since those were computed on the
        # noise-stripped variants and may legitimately be 0 for a chunk
        # that's still the correct answer (a log line rarely repeats the
        # exact Thai question wording).
        has_lexical_evidence = keyword_score > 0 or heading_score > 0 or log_time_evidence or duration_evidence
        strong_lexical = keyword_score >= 0.5 or heading_score >= 0.5
        if log_time_evidence or duration_evidence:
            evidence_label = "direct_keyword"
        elif strong_lexical and heading_info["match_type"] == "synonym_heading":
            evidence_label = "synonym_heading"
        elif strong_lexical and heading_info["match_type"] == "direct_heading":
            evidence_label = "direct_heading"
        elif strong_lexical:
            evidence_label = "direct_keyword"
        elif has_lexical_evidence:
            evidence_label = "semantic_supporting"
        elif normalized_vector >= 0.75:
            evidence_label = "semantic_strong"
        else:
            evidence_label = "weak"
        c["evidence_label"] = evidence_label
        c["has_lexical_evidence"] = has_lexical_evidence
        c["log_time_evidence"] = log_time_evidence
        c["duration_evidence"] = duration_evidence

        # Legacy `classification` tier — kept for backward compatibility
        # with existing callers/tests/UI; derived from evidence_label so
        # the two never disagree.
        if evidence_label in ("direct_heading", "direct_keyword", "synonym_heading"):
            c["classification"] = "direct_evidence"
        elif evidence_label == "semantic_supporting":
            c["classification"] = "supporting_evidence"
        elif evidence_label == "semantic_strong":
            c["classification"] = "weak_semantic"
        else:
            c["classification"] = "excluded_weak_semantic"

        kept.append(c)  # adaptive filtering happens as a SECOND pass below, over hybrid_score

    # ── Adaptive filtering — replaces the old hard reject on normalized
    # vector score. Applies ONLY to candidates with ZERO lexical/heading/
    # synonym evidence (has_lexical_evidence=False) — anything with real
    # keyword/heading/synonym support is never filtered here regardless
    # of its hybrid_score, exactly like the pre-existing architecture
    # (see the module docstring's root-cause explanation: a competing
    # pure-vector chunk's inflated hybrid_score must never be able to
    # push a genuinely-evidenced chunk below a relative-to-best cutoff).
    #
    # Among the pure-vector-only candidates, one is kept if its hybrid
    # score clears an absolute floor OR is within `relative_to_best_ratio`
    # of the pool's best hybrid score — while ALWAYS keeping at least
    # `minimum_candidates` pure-vector candidates when any exist, so a
    # legitimately-relevant vector-only match near the threshold is never
    # lost to an off-by-a-hair cutoff.
    non_structured_kept = [c for c in kept if c not in structured]
    lexical_survivors = [c for c in non_structured_kept if c.get("has_lexical_evidence")]
    pure_vector = [c for c in non_structured_kept if not c.get("has_lexical_evidence")]
    pure_vector.sort(key=lambda c: -c.get("hybrid_score", 0.0))

    survivors: List[Dict] = list(lexical_survivors)
    has_real_evidence_elsewhere = bool(lexical_survivors) or bool(structured)

    if has_real_evidence_elsewhere:
        # Real keyword/heading/synonym evidence exists somewhere in this
        # pool — a pure-vector-only guess isn't NEEDED as evidence, and
        # (the actual original bug) must never be kept just because it
        # happens to have the highest raw vector score among a cluster of
        # otherwise-irrelevant candidates. It's only backfilled if the
        # real-evidence set alone doesn't reach minimum_candidates.
        backfill_slots = max(0, settings.minimum_candidates - len(survivors) - len(structured))
        for c in pure_vector[:backfill_slots]:
            survivors.append(c)
        for c in pure_vector[backfill_slots:]:
            c["exclusion_reason"] = ("Real keyword/heading/synonym evidence was found elsewhere in the "
                                      "candidate pool; a vector-only match without lexical support is not "
                                      "needed as additional evidence.")
            excluded.append(c)
    else:
        # Nothing in the pool has any lexical evidence at all — fall back
        # to adaptive filtering purely on hybrid (=weighted vector) score:
        # keep a candidate if it clears an absolute floor OR is within
        # `relative_to_best_ratio` of this pool's best score, always
        # keeping at least `minimum_candidates` when any exist.
        best_hybrid = pure_vector[0]["hybrid_score"] if pure_vector else 0.0
        for idx, c in enumerate(pure_vector):
            passes = (
                c["hybrid_score"] >= settings.absolute_minimum_score
                and c["hybrid_score"] >= best_hybrid * settings.relative_to_best_ratio
            )
            if passes or idx < settings.minimum_candidates:
                survivors.append(c)
            else:
                c["exclusion_reason"] = (
                    f"Hybrid score {c['hybrid_score']:.2f} is below both the absolute minimum "
                    f"({settings.absolute_minimum_score:.2f}) and {settings.relative_to_best_ratio:.0%} of "
                    f"this query's best hybrid score ({best_hybrid:.2f}) — no keyword/heading/synonym "
                    f"evidence was found for this candidate either."
                )
                excluded.append(c)

    survivors = survivors[:settings.maximum_candidates]
    kept = structured + survivors

    # Tier ordering: real evidence always outranks a pure-semantic guess,
    # regardless of raw hybrid_score — EXCEPT a purpose match/mismatch
    # (see compute_purpose_boost) shifts a chunk one tier up or down
    # first. Without this, a same-vocabulary chunk from the WRONG-purpose
    # document (e.g. a premium-rate table that happens to literally
    # contain the query's words) could still out-tier a purpose-correct
    # chunk purely on raw keyword overlap, silently undoing the boost
    # baked into hybrid_score — tier always won the sort ahead of score.
    kept.sort(key=lambda c: (purpose_adjusted_tier(c), -c.get("hybrid_score", c.get("score", 0.0))))

    # Optional reranking layer — provider-abstracted, default 'heuristic'
    # (no LLM call, no added cost/latency by default).
    try:
        from services.reranker import get_reranker
        reranker = get_reranker(settings.reranker_provider)
        kept = reranker.rerank(question, kept, top_n=len(kept))
    except Exception as e:
        print(f"[hybrid_scoring] reranker '{settings.reranker_provider}' failed, keeping hybrid order: {e}")

    return (kept, excluded) if return_excluded else kept
