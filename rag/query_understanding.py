"""Phase 1 of the AI Playground Intelligence Pipeline — a lightweight,
fully deterministic query-understanding layer that runs BEFORE retrieval:

    User Question -> Normalize -> Detect Intent -> Rewrite -> Expand
                                                                  |
                                                    (existing Hybrid Retrieval)

No LLM calls anywhere in this module — every step is a pure function, so
latency stays negligible (microseconds, not a network round trip) and
behavior is 100% reproducible/testable. This is layered ON TOP of the
existing rag/query_expansion.py (Level 1 Thai-English glossary expansion,
already deterministic) rather than replacing it — expand_query() there
already IS this pipeline's "Query Expansion" step; this module adds
normalization, rule-based intent detection, and one canonical rewritten
query in front of it.

Extending this pipeline:
  - New abbreviation? Add one entry to _ABBREVIATIONS.
  - New rewrite synonym? Add one entry to _REWRITE_SYNONYMS.
  - New intent? Add one regex to _INTENT_PATTERNS and its priority
    position in _INTENT_ORDER.
No other function needs to change for any of the above.
"""
import re
from typing import Dict, List, Optional

from rag.query_expansion import expand_query, detect_query_language

# NOTE: rag/intent_classifier.py already has a DIFFERENT, narrower
# classify_query_intent() (coverage_benefit/premium_price/eligibility/
# exclusion/general_product/unknown) that feeds rag/hybrid_scoring.py's
# purpose-aware ranking boost (document_purpose vs. query intent, for
# disambiguating a benefit brochure from a premium-rate table). This
# module's detect_intent() is a SEPARATE, broader classifier for the AI
# Playground's Explainability display (this Phase 1 spec's 9-intent set)
# — intentionally not merged, since the two serve different consumers
# with different vocabularies; a future phase could unify them if needed.
INTENTS = ("coverage", "premium", "policy", "faq", "company", "contact",
           "excel_calculation", "location", "unknown")


# ── 1. Query Understanding: normalization ──────────────────────

# Generic business/insurance abbreviations — recognized here only to keep
# them intact through normalization (never expanded in-place, which would
# destroy the original query); actual expansion happens in rewrite_query()
# below via _REWRITE_SYNONYMS. Kept as a separate, deliberately small map
# so "handle abbreviations" (Phase 1 spec item 1) has one obvious place to
# extend without touching normalization logic itself.
_ABBREVIATIONS = {"icu", "opd", "ipd", "ckd", "faq", "co.", "ltd.", "inc."}

_STRAY_PUNCT = ".,!?;:\"'()[]{}‘’“”"


def normalize_query(text: str) -> str:
    """Collapses whitespace and strips STRAY leading/trailing punctuation
    per token (so "ICU?" -> "ICU" but "Plan 4", "Co., Ltd.", and known
    abbreviations survive verbatim) — never lowercases, since casing is
    part of how named entities/abbreviations ("ICU", "Plan 4") are
    recognized both here and by rag/hybrid_scoring.py downstream.
    Deliberately conservative: this ONLY tidies formatting, never changes
    meaning or drops a token."""
    if not text:
        return ""
    collapsed = re.sub(r"\s+", " ", text.strip())
    tokens = collapsed.split(" ")
    cleaned = []
    for tok in tokens:
        if tok.lower() in _ABBREVIATIONS:
            cleaned.append(tok)
            continue
        stripped = tok.strip(_STRAY_PUNCT)
        if stripped:
            cleaned.append(stripped)
    return " ".join(cleaned)


# ── 2. Intent Detection (rule-based, no LLM) ────────────────────

# Checked in this exact order — first match wins. Ordered from most
# distinctive/narrow vocabulary to broadest, so an ambiguous question
# (e.g. one that mentions both "เบี้ย" (premium) and "กรมธรรม์" (policy))
# resolves to its more specific/likely-intended bucket rather than
# whichever pattern happens to be a dict-iteration-order accident.
_INTENT_PATTERNS: Dict[str, re.Pattern] = {
    "excel_calculation": re.compile(
        r"(ยอด|รวม|เปรียบเทียบ|สรุป|คำนวณ|เฉลี่ย|มากที่สุด|น้อยที่สุด|จำนวน|นับ|"
        r"total|sum|average|avg|count|compare|top\s*\d+|bottom\s*\d+|"
        r"how many|how much|highest|lowest|maximum|minimum|breakdown|aggregate)",
        re.IGNORECASE,
    ),
    "faq": re.compile(r"(faq|คำถามที่พบบ่อย|q\s*&\s*a|คำถาม\s*:|frequently asked)", re.IGNORECASE),
    "contact": re.compile(r"(ติดต่อ|โทร|เบอร์โทร|อีเมล|e-?mail|phone|contact\s*us|contact\b)", re.IGNORECASE),
    "location": re.compile(r"(ที่ตั้ง|สาขา|ที่อยู่|แผนที่|address|location|\bmap\b|branch)", re.IGNORECASE),
    "premium": re.compile(r"(เบี้ย|premium|ราคา|price|รายเดือน|รายปี|monthly|annual|yearly)", re.IGNORECASE),
    "coverage": re.compile(
        r"(ค่าห้อง|ห้องผู้ป่วย|icu|ความคุ้มครอง|ผลประโยชน์|วงเงิน|coverage|benefit|"
        r"ไตวาย|โรคร้ายแรง|ผ่าตัด|รักษาพยาบาล)",
        re.IGNORECASE,
    ),
    "policy": re.compile(r"(กรมธรรม์|เงื่อนไข|ข้อยกเว้น|ข้อกำหนด|\bpolicy\b|terms and conditions|exclusion)", re.IGNORECASE),
    "company": re.compile(
        r"(บริษัท|เกี่ยวกับเรา|about\s*us|\bmission\b|\bvision\b|พันธกิจ|วิสัยทัศน์|ภารกิจ|history|ประวัติ)",
        re.IGNORECASE,
    ),
}
_INTENT_ORDER = ["excel_calculation", "faq", "contact", "location", "premium", "coverage", "policy", "company"]


def detect_intent(text: str) -> str:
    """Deterministic, rule-based — returns one of INTENTS. Priority order
    is _INTENT_ORDER (first pattern match wins), not dict-iteration
    order, so adding a new intent's pattern to _INTENT_PATTERNS has no
    effect until it's also placed in _INTENT_ORDER."""
    if not text:
        return "unknown"
    for intent in _INTENT_ORDER:
        if _INTENT_PATTERNS[intent].search(text):
            return intent
    return "unknown"


# ── 3. Query Rewrite: deterministic synonym expansion ───────────

# One-directional acronym/condition -> fuller phrase(s), applied as an
# ADDITION to the query (never a replacement) so the original text is
# always preserved verbatim — see rewrite_query()'s docstring.
_REWRITE_SYNONYMS: Dict[str, List[str]] = {
    "icu": ["Intensive Care Unit", "ห้อง ICU"],
    "ไตวาย": ["โรคไตวายเรื้อรัง", "CKD"],
    "ckd": ["chronic kidney disease", "โรคไตวายเรื้อรัง"],
    "มิชชั่น": ["Mission"],
    "พันธกิจ": ["Mission"],
    "วิสัยทัศน์": ["Vision"],
    "opd": ["Outpatient Department"],
    "ipd": ["Inpatient Department"],
    "เบี้ย": ["premium"],
    "กรมธรรม์": ["policy"],
}


# Company-overview retrieval fix (P0, 2026-07-20): a bare "บริษัททำธุรกิจ
# เกี่ยวกับอะไร"-style question shares almost no literal vocabulary with
# the FAQ rows that actually describe the business (import/shipping
# service names), so it was losing to unrelated-but-lexically-closer FAQ
# rows ("มีบริการอะไรบ้าง", "ขอเบอร์ติดต่อ"). Fixed set of expansion terms
# per the task spec — appended ONLY when detect_intent() == "company",
# never for any other intent.
_COMPANY_INTENT_EXPANSION_TERMS = [
    "บริษัท", "ธุรกิจ", "Company", "Company Profile", "บริการ",
    "นำเข้าสินค้าจากจีน", "Import", "Shipping", "ฝากสั่ง", "ฝากนำเข้า",
]


def expand_company_intent_terms(text: str) -> List[str]:
    """Returns the fixed company-overview expansion term set when
    detect_intent(text) == "company", else []. Purely additive — never
    replaces or reorders anything; a caller appends these as extra query
    variants for keyword/heading scoring (rag/hybrid_scoring.py), the
    same mechanism the Knowledge Synonym Engine already uses."""
    if detect_intent(text) != "company":
        return []
    return list(_COMPANY_INTENT_EXPANSION_TERMS)


def rewrite_query(text: str) -> str:
    """Appends recognized synonym glosses in parentheses — e.g. "ICU
    Plan 2 ได้เท่าไหร่" -> "ICU Plan 2 ได้เท่าไหร่ (Intensive Care Unit, ห้อง
    ICU)". The ORIGINAL query is always fully intact as a prefix of the
    result; nothing is replaced or removed, only appended, so a caller
    that only cares about the literal question can still find it."""
    if not text:
        return text
    haystack = text.lower()
    additions: List[str] = []
    seen = set()
    for term, synonyms in _REWRITE_SYNONYMS.items():
        if term.lower() not in haystack:
            continue
        for syn in synonyms:
            key = syn.strip().lower()
            if key and key not in haystack and key not in seen:
                seen.add(key)
                additions.append(syn)
    if not additions:
        return text
    return f"{text} ({', '.join(additions)})"


# ── 4. Query Expansion — delegates to the existing, already-tested
#      rag/query_expansion.py::expand_query() (Level 1 Thai-English
#      glossary). Not reimplemented here; see understand_query() below
#      for how this pipeline composes with it. ──────────────────


def understand_query(question: str) -> Dict:
    """The full Phase 1 pipeline in one call: normalize -> detect intent
    -> rewrite -> expand. Returns a dict with every field the
    Explainability tab needs (see admin/templates/preview.html's
    renderExplain — this reuses the SAME "Query Expansion" panel, just
    with more fields, per the Phase 1 spec's "reuse the existing
    Explainability tab" requirement):

        original_query, normalized_query, detected_intent,
        rewritten_query, expanded_queries, detected_language

    `expanded_queries` keeps expand_query()'s existing contract
    (variants[0] is always the exact literal original question — several
    downstream callers, e.g. rag/hybrid_scoring.py's heading-match
    "is_original" check, depend on that) and ADDS the rewritten query as
    one more variant, so Phase 1 genuinely improves retrieval (more
    lexical surface area for keyword/heading matching) rather than only
    adding explainability metadata.
    """
    normalized = normalize_query(question)
    intent = detect_intent(normalized)
    rewritten = rewrite_query(normalized)

    variants = expand_query(question)
    key = rewritten.strip().lower()
    if key and key not in {v.strip().lower() for v in variants}:
        variants.append(rewritten)

    return {
        "original_query": question,
        "normalized_query": normalized,
        "detected_intent": intent,
        "rewritten_query": rewritten,
        "expanded_queries": variants,
        "detected_language": detect_query_language(question),
    }


# ── Unified Intent Classification (Conversation Intelligence) ──────────
# CONSOLIDATES the two existing classifiers rather than adding a third:
#   - detect_intent() above stays the "broad_intent" (unchanged — still
#     feeds rag/metadata_retrieval.py and the Explainability tab exactly
#     as before).
#   - rag/intent_classifier.py::classify_query_intent() is untouched —
#     still the ONLY thing rag/hybrid_scoring.py's purpose-aware ranking
#     boost reads. Retrieval ranking behavior is unaffected by this
#     section entirely.
# This adds a new, more PRECISE "actionable_intent" for answer/attachment
# planning, built from entities already extracted by
# rag/query_resolution.py::extract_entities() (topic/transport/location/
# attribute — reused directly, not re-implemented) plus a small set of
# additional keyword patterns for buckets query_resolution's attribute
# vocabulary doesn't cover yet (payment/coupon/invoice/prohibited goods/
# tracking/attachment requests/human handoff/summary).
ACTIONABLE_INTENTS = (
    "warehouse_location", "warehouse_map", "warehouse_contact", "service_information",
    "shipping_rate", "shipping_duration", "shipping_calculation",
    "payment_instruction", "payment_policy", "coupon_policy", "invoice_policy",
    "prohibited_goods", "tracking_status", "attachment_request", "human_agent_request",
    "summary", "company_overview", "company_summary", "unknown",
)

# Checked in this order — more specific/explicit requests first, so e.g.
# an explicit "ส่งรูป...ให้หน่อย" (attachment_request) is never
# misclassified as whatever topic the photo happens to be about.
_ATTACHMENT_REQUEST_RE = re.compile(r"ขอรูป|ส่งรูป|ขอภาพ(?!รวม)|ส่งภาพ|ขอไฟล์|ขอเอกสาร")
_HUMAN_AGENT_RE = re.compile(r"คุยกับเจ้าหน้าที่|ขอเจ้าหน้าที่|ติดต่อคน|ขอสายเจ้าหน้าที่")
_MAP_WORD_RE = re.compile(r"แผนที่|โลเคชั่น|พิกัด|google\s*map|gps", re.IGNORECASE)
_ADDRESS_WORD_RE = re.compile(r"ที่อยู่|อยู่ไหน|ที่ตั้ง")
_CONTACT_WORD_RE = re.compile(r"เบอร์|โทร|ติดต่อ")
_PAYMENT_POLICY_RE = re.compile(r"บัตรเครดิต|ค่าธรรมเนียม|ขั้นต่ำ")
_PAYMENT_INSTRUCTION_RE = re.compile(r"จ่ายบิล|ชำระบิล|ชำระเงิน|วิธีจ่าย|วิธีชำระ")
_COUPON_RE = re.compile(r"คูปอง|ส่วนลด|โค้ดส่วนลด")
_INVOICE_RE = re.compile(r"ใบกำกับ|ใบเสร็จ|ภาษี|vat", re.IGNORECASE)
_PROHIBITED_RE = re.compile(r"สินค้าต้องห้าม|ห้ามส่ง|ของต้องห้าม|ผิดกฎหมาย")
# Import-eligibility question ("<goods> นำเข้าได้ไหม") — P1.2A. Routes to
# the prohibited_goods plan so retrieval brings back the trusted
# prohibited/category evidence instead of falling through to a generic
# service_information answer. "ฝากนำเข้าได้ไหม" (the import-agent SERVICE
# question) is excluded via the lookbehind.
_IMPORT_ELIGIBILITY_RE = re.compile(
    r"(?<!ฝาก)นำเข้าได้(?:ไหม|มั้ย|มัย|รึเปล่า|หรือเปล่า|หรือไม่|ป่าว)"
    r"|(?<!นำ)เข้าได้(?:ไหม|มั้ย|มัย|รึเปล่า|หรือเปล่า|หรือไม่|ป่าว)"
    r"|เอาเข้า(?:มา)?ได้(?:ไหม|มั้ย)")
_TRACKING_RE = re.compile(r"ติดตามพัสดุ|เช็คสถานะ|ตรวจสอบสถานะ|tracking", re.IGNORECASE)
_SUMMARY_RE = re.compile(r"สรุป|โดยรวมแล้ว")
_CREDIT_CARD_RE = re.compile(r"บัตรเครดิต")

# Company-overview / company-summary retrieval fix (P0, 2026-07-21) —
# these used to fall through to the generic "service_information" bucket
# (same as any bare FAQ/contact question), which never told the Answer
# Planner/prompt to prioritize business-description evidence over
# lexically-closer contact/service FAQ rows. Checked BEFORE the generic
# _SUMMARY_RE/service_information fallback below, so a company-flavored
# summary/overview question is never misclassified as the generic
# "summary" (conversation-recap) or "service_information" intent.
_COMPANY_SUMMARY_RE = re.compile(
    r"สรุป.{0,10}บริษัท|บริษัท.{0,10}สรุป|ภาพรวมบริษัท|สรุปทั้งหมด|ข้อมูลบริษัทแบบย่อ|"
    r"company\s*summary",
    re.IGNORECASE,
)
_COMPANY_OVERVIEW_RE = re.compile(
    r"ทำธุรกิจ(เกี่ยวกับ)?อะไร|"                                  # "...ทำธุรกิจ(เกี่ยวกับ)อะไร" — specific, safe on its own
    r"(บริษัท|shipify|fasttrade|องค์กร).{0,15}ทำอะไร|"           # "<company word> ... ทำอะไร" — needs a company word to avoid misfiring on generic small talk
    r"แนะนำบริษัท|บริษัทให้บริการอะไร|เกี่ยวกับบริษัท|"
    r"company\s*profile|about\s*company|business\s*overview|company\s*overview",
    re.IGNORECASE,
)

# actionable_intent -> the fact LABELS (never actual values — those only
# ever come from Retrieved Context) an Answer Planner should focus on.
REQUESTED_ATTRIBUTES_BY_INTENT: Dict[str, List[str]] = {
    "warehouse_location": ["address"],
    "warehouse_map": ["map_url", "address"],
    "warehouse_contact": ["phone"],
    "service_information": ["general_info"],
    "shipping_rate": ["rate_per_kg", "rate_per_cbm"],
    "shipping_duration": ["duration_days"],
    "shipping_calculation": ["calculated_rate"],
    "payment_instruction": ["payment_steps"],
    "payment_policy": ["minimum_amount", "fee_percent", "invoice_restriction"],
    "coupon_policy": ["coupon_steps"],
    "invoice_policy": ["invoice_conditions"],
    "prohibited_goods": ["prohibited_list"],
    "tracking_status": ["tracking_info"],
    "attachment_request": ["attachment"],
    "human_agent_request": [],
    "summary": ["summary"],
    "company_overview": ["core_business", "china_import_context", "core_services"],
    "company_summary": ["company_overview", "core_services"],
    "unknown": [],
}


def _classify_actionable(text: str, entities: Dict[str, Optional[str]]) -> "tuple[str, float]":
    """Returns (actionable_intent, confidence). `entities` is the ALREADY
    merged {topic, transport, location, attribute} dict — current
    wording's own entities take priority over carried ones, exactly like
    rag/query_resolution.py's own merge, so the caller should pass that
    same merged dict (never recomputed differently here)."""
    if _ATTACHMENT_REQUEST_RE.search(text):
        return "attachment_request", 0.9
    if _HUMAN_AGENT_RE.search(text):
        return "human_agent_request", 0.9

    topic = entities.get("topic")
    attribute = entities.get("attribute")
    transport = entities.get("transport")

    if topic == "โกดัง":
        if _CONTACT_WORD_RE.search(text) and not _ADDRESS_WORD_RE.search(text):
            return "warehouse_contact", 0.85
        if attribute == "location":
            if _MAP_WORD_RE.search(text):
                return "warehouse_map", 0.9
            if _ADDRESS_WORD_RE.search(text):
                return "warehouse_location", 0.85
            return "warehouse_location", 0.6

    if attribute == "rate" and transport:
        return "shipping_rate", 0.9
    if attribute == "duration" and transport:
        return "shipping_duration", 0.9
    if attribute == "rate" and _PAYMENT_POLICY_RE.search(text):
        pass  # fall through — a credit-card FEE question, not a shipping rate
    try:
        from rag.searcher import is_analytical
        if attribute == "rate" and is_analytical(text):
            return "shipping_calculation", 0.75
    except Exception:
        pass

    if _PAYMENT_POLICY_RE.search(text):
        return "payment_policy", 0.85
    if _PAYMENT_INSTRUCTION_RE.search(text):
        return "payment_instruction", 0.85
    if _COUPON_RE.search(text):
        return "coupon_policy", 0.85
    if topic == "ใบกำกับ" or _INVOICE_RE.search(text):
        return "invoice_policy", 0.8
    if _PROHIBITED_RE.search(text):
        return "prohibited_goods", 0.85
    if _IMPORT_ELIGIBILITY_RE.search(text):
        return "prohibited_goods", 0.8
    if topic == "Tracking" or _TRACKING_RE.search(text):
        return "tracking_status", 0.85
    if _COMPANY_SUMMARY_RE.search(text):
        return "company_summary", 0.8
    if _COMPANY_OVERVIEW_RE.search(text):
        return "company_overview", 0.8
    if _SUMMARY_RE.search(text):
        return "summary", 0.7

    broad = detect_intent(text)
    if broad in ("company", "faq", "contact"):
        return "service_information", 0.5

    return "unknown", 0.0


def classify_actionable_intent(question: str, entities: Optional[Dict[str, Optional[str]]] = None) -> Dict:
    """Unified Intent Classification — the single entry point for both
    the broad intent (unchanged, still used by retrieval-adjacent
    Explainability/metadata matching) and the new, precise
    actionable_intent used by the Answer Planner (services/
    answer_planner.py) and Attachment Planner (services/
    attachment_planner.py).

    `question` should be the FINAL (spell-corrected, resolved, canonical)
    question text; `entities` should be the merged conversation-entity
    dict rag/query_resolution.py's resolver already computed for this
    turn (current wording's own entities win; missing ones are already
    filled in from carried conversation state) — never re-derived
    differently here, so intent and query-resolution never disagree
    about what "location"/"transport" means for this turn.

    Returns:
        {
          "broad_intent": str,          # unchanged 9-bucket classifier
          "actionable_intent": str,     # one of ACTIONABLE_INTENTS
          "entities": {"location":..., "transport":..., "payment_method":...},
          "requested_attributes": [str, ...],
          "confidence": float,
        }
    Uncertain input (nothing matched) returns actionable_intent="unknown",
    confidence=0.0 — callers must treat that as "preserve existing
    retrieval/prompt behavior," never guess further.
    """
    entities = entities or {}
    broad = detect_intent(question)
    actionable, confidence = _classify_actionable(question, entities)

    payment_method = "credit_card" if _CREDIT_CARD_RE.search(question) else None

    return {
        "broad_intent": broad,
        "actionable_intent": actionable,
        "entities": {
            "location": entities.get("location"),
            "transport": entities.get("transport"),
            "payment_method": payment_method,
        },
        "requested_attributes": list(REQUESTED_ATTRIBUTES_BY_INTENT.get(actionable, [])),
        "confidence": confidence,
    }
