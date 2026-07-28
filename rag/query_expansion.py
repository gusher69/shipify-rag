"""Generic Thai-English query expansion — Level 1 (deterministic
glossary) is always on; Level 2 (optional LLM rewrite) is off by default
(config.RAG_QUERY_REWRITE_ENABLED) and the pipeline works fully without
it.

This module never hardcodes a specific product/entity (no "Shipify", no
"Google Drive") — the glossary only maps GENERIC bilingual intent/
technical terms (mission/purpose, version, package, warranty, etc.) that
apply to any document. Named entities, product names, package names,
and filenames are always preserved verbatim in every variant, never
translated or dropped, since those are exactly the tokens
rag/hybrid_scoring.py's keyword/heading matching depends on.

Design (matches the audit finding): a query like "พันธกิจของ Shipify
คืออะไร" and a chunk headed "Our Mission" share almost no lexical tokens
in the SAME language, so rag/hybrid_scoring.py's keyword/heading overlap
(computed per-variant, then OR'd across variants — see
rag/searcher.py::search()) is nearly always zero without expansion. This
module produces an English variant ("Shipify mission") specifically so
that keyword/heading scoring gets a chance to match "Our Mission" too,
not just the raw vector embedding.
"""
import re
from typing import Dict, List

# Generic bilingual glossary — intent/technical concepts only, never a
# specific entity or product name. Each Thai term maps to one or more
# ENGLISH-ONLY synonyms; expansion runs in both directions. Thai-to-Thai
# cross references (e.g. "มิชชั่น" <-> "พันธกิจ") live in
# _THAI_SYNONYM_GROUPS below, kept separate so this dict (and the
# English-keyed _REVERSE_GLOSSARY built from it) never mixes languages.
_GLOSSARY: Dict[str, List[str]] = {
    # Mission/vision/goal cluster — includes the transliteration
    # "มิชชั่น" (a very common way Thai speakers actually say "mission"),
    # which a pure-translation entry for "พันธกิจ" alone would never
    # catch (transliteration != translation).
    "มิชชั่น": ["mission", "mission statement"],
    "พันธกิจ": ["mission", "mission statement", "purpose"],
    "ภารกิจ": ["mission", "mission statement"],
    "เป้าหมาย": ["goal", "objective", "aim", "mission"],
    "จุดประสงค์": ["purpose", "objective"],
    "วิสัยทัศน์": ["vision", "company vision"],
    "บริการ": ["service", "services"],
    "ติดต่อ": ["contact"],
    "เวอร์ชัน": ["version"],
    "แพ็กเกจ": ["package", "dependency"],
    "แพคเกจ": ["package", "dependency"],
    "เอกสาร": ["document", "documentation"],
    "รับประกัน": ["warranty", "guarantee"],
    "คืนสินค้า": ["return", "refund"],
    "จัดส่ง": ["shipping", "delivery"],
    "ราคา": ["price", "pricing"],
    "โกดังจีน": ["China warehouse", "warehouse in China", "Guangzhou warehouse"],
    "ไลน์": ["LINE", "LINE OA", "Official Account"],
    # Log-event-time cluster (system/process start events + "what time").
    # Generic log vocabulary, not tied to any specific system/filename.
    "เริ่มทำงาน": ["started", "startup", "start time", "launched", "initialized"],
    "เริ่มระบบ": ["started", "startup", "start time", "launched", "initialized", "system start"],
    "เริ่มต้นระบบ": ["started", "startup", "start time", "launched", "initialized"],
    "เริ่มตอนไหน": ["started", "startup", "start time", "launched"],
    "กี่โมง": ["time", "timestamp", "what time"],
    "เวลาอะไร": ["time", "timestamp"],
}

# Thai terms that are synonyms of EACH OTHER (not just of an English
# phrase) — e.g. a query using the transliteration "มิชชั่น" should also
# match a document/heading using the more formal "พันธกิจ" or "ภารกิจ",
# independent of any English translation.
_THAI_SYNONYM_GROUPS: List[List[str]] = [
    ["มิชชั่น", "พันธกิจ", "ภารกิจ"],
]
_THAI_CROSS_SYNONYMS: Dict[str, List[str]] = {}
for _group in _THAI_SYNONYM_GROUPS:
    for _term in _group:
        _THAI_CROSS_SYNONYMS[_term] = [t for t in _group if t != _term]

# Reverse direction (English -> Thai) built once, so a mostly-English
# query against a Thai-only chunk also gets a chance.
_REVERSE_GLOSSARY: Dict[str, List[str]] = {}
for th, en_list in _GLOSSARY.items():
    for en in en_list:
        _REVERSE_GLOSSARY.setdefault(en, []).append(th)

# Attribute-distinguishing terms — used only to make sure expansion never
# BLURS the specific attribute being asked about (Part 8/17's "runtime
# version vs package version vs API version vs OS version vs model
# version vs driver version" requirement). Purely descriptive; retrieval
# behavior itself (not blending attributes) is enforced in
# services/prompt_builder.py's STRICT_GROUNDING_RULES, not here — this
# module only affects what's SEARCHED for, never what's claimed as fact.
ATTRIBUTE_HINTS = {
    "runtime_version": ["python version", "runtime version", "รันไทม์", "ภาษา version"],
    "package_version": ["package version", "dependency version", "library version",
                          "แพ็กเกจ version", "เวอร์ชัน package"],
    "api_version": ["api version", "sdk version"],
    "os_version": ["operating system version", "os version", "ระบบปฏิบัติการ"],
    "model_version": ["model version"],
    "driver_version": ["driver version"],
}

# Named/technical tokens that must survive expansion untouched — package
# names, versions, filenames. Kept intact so a variant never accidentally
# drops the one token keyword-matching actually needs.
_TECHNICAL_TOKEN_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9._-]*[a-zA-Z0-9]")


def _extract_technical_terms(query: str) -> List[str]:
    """Latin-script technical terms (package names, product names, version
    strings) — preserved verbatim in every expansion variant."""
    return [t for t in _TECHNICAL_TOKEN_RE.findall(query) if len(t) > 2]


def normalize_query(query: str) -> str:
    """Level 1, step 1 — casing/punctuation normalization only. Never
    touches technical terms' internal casing (e.g. "google-api-python-client")
    since re.sub below only strips OUTER punctuation/whitespace, not
    hyphens/dots inside a token."""
    q = query.strip()
    q = re.sub(r"\s+", " ", q)
    q = re.sub(r"[?!.]+$", "", q)
    return q


def expand_query(query: str) -> List[str]:
    """Returns [original_query, *variants] — original is ALWAYS first and
    always included, so a caller that ignores expansion entirely still
    gets correct (if unexpanded) behavior. Deterministic, no LLM call
    (see rewrite_query_with_llm for the optional Level 2 addition)."""
    normalized = normalize_query(query)
    variants = [normalized]
    technical_terms = _extract_technical_terms(normalized)

    lower = normalized.lower()
    matched_en: List[str] = []
    matched_th_cross: List[str] = []
    for th_term, en_terms in _GLOSSARY.items():
        if th_term in normalized:
            matched_en.extend(en_terms)
            matched_th_cross.extend(_THAI_CROSS_SYNONYMS.get(th_term, []))
    matched_th: List[str] = []
    for en_term, th_terms in _REVERSE_GLOSSARY.items():
        if en_term in lower:
            matched_th.extend(th_terms)

    # Build a compact English-leaning variant: technical terms (entities/
    # package names/filenames) + any matched English glossary terms. This
    # is what gives keyword/heading scoring a real chance against an
    # English-only heading when the original query was Thai-only. Each
    # matched English term ALSO gets its own single-term variant (not
    # just one combined string) so a short heading like "Vision" can be
    # matched even when the query also contains unrelated words.
    if matched_en and technical_terms:
        variants.append(" ".join(technical_terms + matched_en))
    elif matched_en:
        variants.append(" ".join(matched_en))
    for term in matched_en:
        variants.append(term)

    if matched_th and technical_terms:
        variants.append(" ".join(technical_terms + matched_th))

    # Thai-to-Thai cross synonyms (มิชชั่น <-> พันธกิจ <-> ภารกิจ) — helps
    # keyword/heading matching against a Thai-language document/heading
    # even when no English translation is involved at all.
    for term in matched_th_cross:
        variants.append(term)

    # De-duplicate while preserving order.
    seen = set()
    unique_variants = []
    for v in variants:
        key = v.strip().lower()
        if key and key not in seen:
            seen.add(key)
            unique_variants.append(v)
    return unique_variants


_THAI_CHAR_RE = re.compile(r"[฀-๿]")


def detect_query_language(query: str) -> str:
    """Coarse Thai/English/Mixed detection for Explainability display —
    not used for any retrieval decision, purely descriptive."""
    if not query:
        return "English"
    thai_chars = len(_THAI_CHAR_RE.findall(query))
    letters = len(re.findall(r"[^\W\d_]", query, re.UNICODE))
    if letters == 0:
        return "English"
    ratio = thai_chars / letters
    if ratio > 0.6:
        return "Thai"
    if ratio > 0.05:
        return "Mixed Thai-English"
    return "English"


def explain_query_expansion(query: str) -> Dict:
    """Explainability payload (Part 11): original_query, expanded_queries,
    detected_language — exactly what the AI Playground's Explainability
    tab needs to show WHY a given variant was searched."""
    return {
        "original_query": query,
        "expanded_queries": expand_query(query),
        "detected_language": detect_query_language(query),
    }


def rewrite_query_with_llm(query: str, max_variants: int = 3) -> List[str]:
    """Level 2 — OPTIONAL, gated by config.RAG_QUERY_REWRITE_ENABLED
    (default False). Never called automatically by expand_query(); a
    caller (rag/searcher.py) checks the flag itself and calls this
    separately, so the deterministic Level 1 path always works with zero
    LLM dependency. On any failure, returns [] (caller falls back to
    Level 1 variants only) — never raises, never blocks retrieval."""
    from config import RAG_QUERY_REWRITE_ENABLED
    if not RAG_QUERY_REWRITE_ENABLED:
        return []
    try:
        from services.llm_service import get_llm_service
        from config import OPENAI_CHAT_MODEL
        llm = get_llm_service()
        prompt = (
            "Generate up to {n} short alternative search-query phrasings for the question below, "
            "to help a search engine find the right document section. Preserve every named entity, "
            "product name, package name, and technical term EXACTLY as written — do not translate or "
            "paraphrase them. Never answer the question itself. Respond with ONLY a JSON array of "
            "strings, no explanation.\n\nQuestion: {q}"
        ).format(n=max_variants, q=query)
        resp = llm.generate([{"role": "user", "content": prompt}],
                             model=OPENAI_CHAT_MODEL, temperature=0.0, max_tokens=200)
        import json
        variants = json.loads(resp.text.strip())
        if isinstance(variants, list):
            return [str(v) for v in variants[:max_variants]]
        return []
    except Exception as e:
        print(f"[query_expansion] LLM rewrite skipped/failed: {e}")
        return []
