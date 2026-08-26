"""Deterministic query-intent classification — no LLM call, dependency-
free keyword matching. Generic across any product/document set (no
company/product names) — distinguishes what KIND of question is being
asked (coverage amount vs. premium price vs. eligibility vs. exclusion)
so retrieval can prefer documents whose services.document_purpose
classification matches, without ever hard-filtering.

Deliberately mirrors rag/hybrid_scoring.py's tokenize() approach (Thai has
no word-boundary spaces, so substring containment is used alongside
tokenized matching) rather than introducing a second tokenization scheme.
"""
from typing import List

INTENTS = ("log_event_time", "duration_query", "coverage_benefit", "premium_price", "eligibility", "exclusion", "general_product", "unknown")

# Same bilingual-keyword-group philosophy as services/document_purpose.py
# — generic concepts, not entity names.
_COVERAGE_TERMS = [
    "ค่าห้อง", "ห้องผู้ป่วย", "icu", "ความคุ้มครอง", "ผลประโยชน์", "วงเงิน",
    "ไตวาย", "โรคร้ายแรง", "ผ่าตัด", "ชดเชย", "coverage", "benefit", "limit",
]
_PREMIUM_TERMS = ["เบี้ย", "premium", "ราคา", "price", "รายเดือน", "รายปี", "ต่อเดือน", "ต่อปี", "monthly", "annual", "yearly"]
_ELIGIBILITY_TERMS = ["อายุ", "สมัคร", "รับประกัน", "age", "eligib", "enroll", "apply"]
_EXCLUSION_TERMS = ["ข้อยกเว้น", "ไม่คุ้มครอง", "ไม่รวม", "exclu", "not covered", "ยกเว้น"]
# Log/system-event time cluster (generic — "did some process/system start,
# and at what time" — no product name). Checked FIRST (see classify_query_intent)
# since "เริ่มทำงาน"/"กี่โมง" are distinctive enough not to collide with the
# insurance-domain buckets above, and a bare "ระบบ" (system) mention alone
# must NOT be enough to trigger this bucket (see hybrid_scoring.py's
# noise-reduction for the same reason: "ระบบ" is far too generic on its own).
_LOG_TIME_START_TERMS = ["เริ่มทำงาน", "เริ่มระบบ", "เริ่มต้นระบบ", "เริ่มตอนไหน", "เริ่มรอบ", "started", "startup",
                          "launched", "initialized", "scheduler"]
_LOG_TIME_WHEN_TERMS = ["กี่โมง", "เวลาอะไร", "ตอนไหน", "เมื่อไหร่", "โมง", "timestamp"]

# Duration cluster (generic — "how long does X take", no product/company
# name; applies equally to a shipping question, a processing-time
# question, a warranty period question, etc.). Checked BEFORE the
# insurance-domain buckets below for the same reason log_event_time is:
# distinctive enough not to collide with them, and this is a query-side
# signal only (it never forces a topic — see hybrid_scoring.py's
# has_duration_evidence, which still requires the CANDIDATE chunk to
# carry its own explicit day-count evidence before any boost applies).
_DURATION_TERMS = [
    "กี่วัน", "นานไหม", "นานเท่าไหร่", "นานแค่ไหน", "ใช้เวลานาน", "ใช้เวลากี่วัน",
    "ใช้เวลาเท่าไหร่", "ระยะเวลา", "เวลาในการ", "how long", "how many days", "transit time",
]


def _hit(haystack: str, terms: List[str]) -> bool:
    return any(t.lower() in haystack for t in terms)


def classify_query_intent(question: str) -> str:
    """Returns one of INTENTS. Order matters — premium and eligibility
    terms are checked before the broader coverage bucket so a question
    like "อายุ 35 Plan 4 เบี้ยรายเดือนเท่าไหร่" (which contains an age
    number AND premium vocabulary) resolves to premium_price, its
    dominant intent, rather than eligibility."""
    if not question:
        return "unknown"
    haystack = question.lower()

    # Checked FIRST: distinctive log/system-event-time vocabulary. A bare
    # mention of "ระบบ" (system) never triggers this on its own — only
    # actual start-event or explicit time-question vocabulary does — so a
    # generic "เกี่ยวกับระบบ..." question doesn't get misrouted here.
    if _hit(haystack, _LOG_TIME_START_TERMS) or _hit(haystack, _LOG_TIME_WHEN_TERMS):
        return "log_event_time"

    # Checked before the insurance-domain buckets for the same reason —
    # "ระยะเวลา"/"กี่วัน" wording is distinctive of "how long" questions and
    # won't collide with premium/coverage/eligibility/exclusion terms.
    if _hit(haystack, _DURATION_TERMS):
        return "duration_query"

    has_premium = _hit(haystack, _PREMIUM_TERMS)
    has_coverage = _hit(haystack, _COVERAGE_TERMS)
    has_exclusion = _hit(haystack, _EXCLUSION_TERMS)
    has_eligibility = _hit(haystack, _ELIGIBILITY_TERMS)

    if has_premium:
        return "premium_price"
    if has_exclusion:
        return "exclusion"
    if has_coverage:
        return "coverage_benefit"
    if has_eligibility:
        return "eligibility"

    # A bare product/plan mention ("Plan 4 คืออะไร") with none of the
    # above signals — a real intent, just not one of the specific ones.
    if any(t in haystack for t in ["plan", "แผน"]):
        return "general_product"

    return "unknown"
