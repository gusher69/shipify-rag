"""Best-effort enrichment for Q&A Excel rows that DON'T supply their own
Category / Tags / Alternative Questions / Language columns.

Every function here is deterministic (language, tags, category) except
generate_alt_questions, which is LLM-backed and best-effort — it degrades
to an empty list on any failure (no API key, no quota, network error, or
ENABLE_SMART_ALT_QUESTIONS=false) and NEVER raises. Ingestion must never
fail, slow to a crawl unexpectedly, or produce a worse result because
enrichment didn't work — these are all pure quality-of-life additions on
top of the customer's actual Question/Answer data, never a replacement
for it.

If a sheet DOES have real Category/Tags/Alternative-Questions columns,
importers should prefer those real values over anything generated here —
see excel_extractor.detect_optional_qa_columns.
"""
import os
import re
from pathlib import Path
from typing import List, Optional

_THAI_RE = re.compile(r"[฀-๿]")
_CJK_RE = re.compile(r"[一-鿿]")


def detect_language(text: str) -> str:
    """Cheap, deterministic th/en/zh detection — good enough to route
    tone/formatting, not a substitute for a real language ID model."""
    if not text:
        return "en"
    if _THAI_RE.search(text):
        return "th"
    if _CJK_RE.search(text):
        return "zh"
    return "en"


# A small, curated starter dictionary for the shipping/logistics domain
# this app operates in — NOT general-purpose NLP. Extend this dict as real
# customer FAQ content reveals more recurring terms; it is intentionally
# simple (substring match) so it's obvious what triggered any given tag.
_TAG_KEYWORDS = {
    "โกดัง":     ["โกดัง", "warehouse"],
    "จีน":       ["จีน", "china"],
    "ราคา":      ["ราคา", "price"],
    "ค่าส่ง":     ["ค่าส่ง", "shipping", "freight"],
    "พัสดุ":      ["พัสดุ", "parcel"],
    "ติดตาม":    ["ติดตาม", "tracking"],
    "ที่อยู่":     ["ที่อยู่", "address"],
    "สินค้า":     ["สินค้า", "product"],
    "จัดส่ง":     ["จัดส่ง", "delivery", "shipping"],
    "นำเข้า":     ["นำเข้า", "import"],
    "ชำระเงิน":   ["ชำระเงิน", "payment"],
    "คืนเงิน":    ["คืนเงิน", "refund"],
    "ยกเลิก":     ["ยกเลิก", "cancel"],
    "ธนาคาร":     ["ธนาคาร", "bank"],
    "ขนส่ง":      ["ขนส่ง", "shipping", "logistics"],
    "ตู้คอนเทนเนอร์": ["container", "ตู้คอนเทนเนอร์"],
    "ศุลกากร":    ["customs", "ศุลกากร"],
}


def generate_tags(question: str, answer: str = "", max_tags: int = 8) -> List[str]:
    text = f"{question or ''} {answer or ''}"
    tags: List[str] = []
    for kw, out_tags in _TAG_KEYWORDS.items():
        if kw in text:
            for t in out_tags:
                if t not in tags:
                    tags.append(t)
    return tags[:max_tags]


_GENERIC_SHEET_NAMES = {"sheet1", "sheet2", "sheet3", "sheet", "faq", "data", "import", "main", "qa"}


def infer_category(sheet_name: str = "", file_name: str = "",
                    existing_category: Optional[str] = None) -> str:
    """Priority: an explicit Category cell value (if the caller has one) >
    a meaningful (non-generic) sheet name > the file name > a flat
    default. This mirrors the order in the spec (sheet name → file name →
    existing metadata) with the one addition that a REAL Category column
    value, when present, always wins — inference only fills a gap."""
    if existing_category and str(existing_category).strip():
        return str(existing_category).strip()
    if sheet_name and sheet_name.strip().lower() not in _GENERIC_SHEET_NAMES:
        return sheet_name.strip()
    if file_name:
        stem = Path(file_name).stem.replace("_", " ").replace("-", " ").strip()
        if stem:
            return stem.title()
    return "General Knowledge"


# Process-lifetime cache keyed by normalized question text. The Import
# Preview flow analyzes a file once to build the preview, then (on
# Confirm) the real import re-extracts the same file from scratch — this
# cache means that second pass hits the same alt-questions instantly
# instead of paying for a second LLM call per row. Deliberately simple
# (no TTL/eviction) — bounded in practice by how many distinct questions
# get analyzed in a process's lifetime, which is small relative to typical
# deployments; a process restart clears it, which is fine since it's a
# cost/latency optimization, not a correctness requirement.
_alt_questions_cache: dict = {}


def generate_alt_questions(question: str, max_n: int = 4) -> List[str]:
    """LLM-backed paraphrase generation — best-effort, never raises.

    Set ENABLE_SMART_ALT_QUESTIONS=false to skip the LLM call entirely
    (faster/cheaper large imports, or when no OpenAI billing is set up).
    """
    if os.getenv("ENABLE_SMART_ALT_QUESTIONS", "true").strip().lower() in ("0", "false", "no"):
        return []
    if not question or not question.strip():
        return []
    cache_key = (question.strip().lower(), max_n)
    if cache_key in _alt_questions_cache:
        return _alt_questions_cache[cache_key]
    try:
        from services.llm_service import get_llm_service
        from config import OPENAI_CHAT_MODEL
        llm = get_llm_service()
        prompt = (
            f"Generate {max_n} short alternative ways a customer might phrase this "
            f"FAQ question, in the SAME language as the question. One per line, no "
            f"numbering, no quotes, no explanation.\n\nQuestion: {question.strip()}"
        )
        resp = llm.generate([{"role": "user", "content": prompt}],
                             model=OPENAI_CHAT_MODEL, temperature=0.5, max_tokens=150)
        lines = [l.strip("-•*0123456789. ").strip() for l in resp.text.splitlines()]
        seen_lower = question.strip().lower()
        lines = [l for l in lines if l and l.lower() != seen_lower]
        result = lines[:max_n]
        _alt_questions_cache[cache_key] = result
        return result
    except Exception as exc:
        print(f"[smart_enrichment] alt-question generation skipped: {exc}")
        return []
