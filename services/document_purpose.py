"""Generic document-purpose classification — deterministic keyword
heuristics, never entity-specific (no product/company names). Used to
distinguish documents that legitimately share heavy vocabulary overlap
(e.g. a benefit brochure and a premium-rate table for the SAME product
both say "Plan 1/2/3/4" constantly) so retrieval can prefer the document
whose PURPOSE actually matches what the question is asking about.

Two independent, reusable pieces:
  - classify_document_purpose(): ONE purpose per file, computed once at
    ingestion time from filename + headings + a text sample.
  - detect_table_semantic_labels(): per-CHUNK keyword-group hits, used to
    build a lightweight `content_signals` list stored in chunk metadata —
    lets a chunk within an otherwise-ambiguous file still carry its own
    "this looks like coverage text" / "this looks like a premium table"
    signal.

Nothing here calls an LLM and nothing here hard-filters — see
rag/hybrid_scoring.py for how these signals become a RANKING boost, never
an exclusion.
"""
import re
from typing import Dict, List, Optional

PURPOSES = ("coverage_brochure", "premium_monthly", "premium_annual", "policy", "faq", "general")

# Generic bilingual keyword groups — intentionally about DOCUMENT
# STRUCTURE/PURPOSE concepts (premium vs. coverage vs. legal wording),
# never a specific product or company name, so this works for any
# insurance-like (or similarly-shaped: brochure + price table + terms)
# knowledge base, not just Allianz.
_PREMIUM_GENERIC = ["เบี้ยประกัน", "เบี้ยประกันภัย", "อัตราเบี้ย", "premium", "price list", "rate table"]
_MONTHLY_HINTS = ["รายเดือน", "ต่อเดือน", "monthly"]
_ANNUAL_HINTS = ["รายปี", "ต่อปี", "annual", "yearly"]
_COVERAGE_HINTS = [
    "ความคุ้มครอง", "ผลประโยชน์", "coverage", "benefit", "ค่าห้อง", "ห้องผู้ป่วย",
    "icu", "ผ่าตัด", "การรักษาพยาบาล", "วงเงิน", "ไตวาย", "โรคร้ายแรง",
]
_POLICY_HINTS = [
    "กรมธรรม์ประกันภัย", "เงื่อนไขทั่วไป", "ข้อยกเว้น", "policy wording",
    "terms and conditions", "general conditions", "definition", "นิยาม",
]
_FAQ_HINTS = ["faq", "คำถามที่พบบ่อย", "q&a", "question:", "คำถาม:"]


def _normalize(text: str) -> str:
    return (text or "").lower()


def _any_hit(haystack: str, terms: List[str]) -> bool:
    return any(t.lower() in haystack for t in terms)


def _count_hits(haystack: str, terms: List[str]) -> int:
    return sum(1 for t in terms if t.lower() in haystack)


def _digit_density(text: str) -> float:
    """A real rate/premium table is overwhelmingly digits and separators
    ("1,420 | 1,554 | 1,729..."), with almost no descriptive vocabulary —
    the actual premium-table content this was built against never once
    says the word "premium"/"เบี้ย" near the numbers themselves (only
    possibly in a page header far from the sampled rows). Digit density
    is a structural signal that doesn't depend on any specific wording."""
    if not text:
        return 0.0
    digits = sum(1 for ch in text if ch.isdigit())
    return digits / len(text)


def classify_document_purpose(*, filename: str = "", headings: Optional[List[str]] = None,
                               sample_text: str = "", chunk_strategy: Optional[str] = None) -> str:
    """Returns one of PURPOSES. Computed once per file (not per chunk) —
    see ingestion/ingest.py's analyze_and_chunk(), which calls this after
    chunks are built and stamps the result onto every chunk's metadata.

    Weighted scoring rather than a strict priority chain — a strict
    "first keyword group found wins" chain broke on real data two ways:
    (1) a benefit brochure that briefly mentions "you can pay your
    premium monthly" in marketing copy would win the WHOLE file as
    premium_monthly off one incidental phrase; (2) a genuine premium-rate
    table, whose sampled rows are almost pure numbers with no
    surrounding "premium"/"เบี้ย" vocabulary at all, would never trigger
    the premium branch. Each purpose bucket instead accumulates a score
    from (a) how many DISTINCT keyword terms hit, weighted higher for
    more decisive/specific vocabulary, and (b) for premium specifically,
    a structural digit-density signal — the bucket with the highest
    score wins; ties/all-zero fall back to "general".
    """
    headings = headings or []
    haystack = _normalize(" ".join([filename, " ".join(headings), sample_text[:3000]]))

    if chunk_strategy == "faq_qa" or _any_hit(haystack, _FAQ_HINTS):
        return "faq"

    coverage_hits = _count_hits(haystack, _COVERAGE_HINTS)
    premium_hits = _count_hits(haystack, _PREMIUM_GENERIC)
    monthly_hits = _count_hits(haystack, _MONTHLY_HINTS)
    annual_hits = _count_hits(haystack, _ANNUAL_HINTS)
    policy_hits = _count_hits(haystack, _POLICY_HINTS)
    is_dense_numeric_table = _digit_density(sample_text) > 0.15

    scores = {
        # Coverage/policy vocabulary is weighted higher — those terms are
        # longer, more specific multi-syllable insurance words, so a
        # single real hit should outweigh one incidental premium mention.
        "coverage_brochure": coverage_hits * 1.5,
        "policy": policy_hits * 1.5,
        "premium": premium_hits + monthly_hits + annual_hits + (2 if is_dense_numeric_table else 0),
    }
    best = max(scores, key=scores.get)
    if scores[best] <= 0:
        return "general"
    if best == "premium":
        return "premium_monthly" if monthly_hits >= annual_hits else "premium_annual"
    return best


def detect_table_semantic_labels(text: str) -> List[str]:
    """Per-chunk keyword-group hits — a coarse content signal ("this chunk
    reads like coverage content", "this chunk reads like a premium
    table"), independent of the file-level document_purpose. Returned as
    a list of group names so a chunk can legitimately carry more than
    one (e.g. a page that discusses both eligibility age AND premiums)."""
    haystack = _normalize(text)
    labels = []
    if _any_hit(haystack, _COVERAGE_HINTS):
        labels.append("coverage")
    if _any_hit(haystack, _PREMIUM_GENERIC) or _any_hit(haystack, _MONTHLY_HINTS) or _any_hit(haystack, _ANNUAL_HINTS):
        labels.append("premium")
    if _any_hit(haystack, _POLICY_HINTS):
        labels.append("policy")
    return labels
