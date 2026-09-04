# -*- coding: utf-8 -*-
"""CUSTOMER-LINK-1 — Product Link Conversion (1688 / Taobao / Tmall ->
Shipify) as a deterministic capability, same pattern as
services/shipping_estimate_flow.py / services/charter_truck_flow.py /
services/operational_change_flow.py: a small, dedicated module owning
ONE capability's structural/deterministic decisions. `interpret()`
(services/conversation_semantics.py) still owns intent RECOGNITION —
this module owns ONLY URL/domain structural validation and
conversion-RESULT truth typing, neither of which may ever depend on an
LLM (see CUSTOMER-LINK-1 spec: "LLM may understand the intent. LLM must
NOT decide whether the URL itself is valid.").

Supported platforms / evidenced URL shapes — source: `tests/customer_
uat/customer_uat_master.jsonl` CUS-P20 (linked CUS-G29, CUS-S20; source
files `kase_tee_tong_kae.pdf` page 17 + `Ai.xlsx` sheet
'2.tongchecknairabop' row 20 / CSW20). CUS-S20's own worked example
pastes THREE real customer URLs:
  - Taobao short/share link:  https://e.tb.cn/h.RyW4UZ9?tk=...
  - Taobao full listing:      https://item.taobao.com/item.htm?...
  - 1688 listing:             https://detail.1688.com/offer/...html
Tmall is named (not URL-evidenced) in the action's own pre-existing
`ai_description` / `search_keywords` ("taobao", "tmall", "1688") — its
standard root domain is supported by that existing declaration; no
Tmall subdomain/short-link variant is invented beyond it, per the
explicit "do NOT assume domain variants" instruction.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

_URL_RE = re.compile(r"https?://[^\s<>\"']+")

# platform -> accepted hostname suffixes (a hostname matches if it EQUALS
# one of these or ends with "." + one of these — ordinary subdomain
# matching, not an invented variant).
SUPPORTED_DOMAINS: Dict[str, tuple] = {
    "1688": ("1688.com",),
    "taobao": ("taobao.com", "tb.cn"),
    "tmall": ("tmall.com",),
}

# a bare platform-domain MENTION with no scheme (e.g. "1688.com/abc") —
# the customer clearly attempted to reference/paste a link, but it does
# not parse as a well-formed https?:// URL. Distinct from MISSING_URL
# (nothing link-shaped mentioned at all).
_BARE_DOMAIN_RE = re.compile(
    r"(?<![\w.])(?:1688\.com|taobao\.com|tmall\.com|tb\.cn)(?![\w.])", re.IGNORECASE)

# an explicit conversion-intent verb/phrase — deterministic, not a large
# phrase dictionary: one shape ("แปลง" + "ลิงก์/ลิงค์/link"), either order.
LINK_CONVERSION_VERB_RE = re.compile(
    r"แปลง\S{0,6}(?:ลิงก์|ลิงค์|link)|(?:ลิงก์|ลิงค์|link)\S{0,6}แปลง|"
    r"convert\s+(?:this\s+)?link", re.IGNORECASE)

# two orthogonal markers, composed (not a phrase list): "a link" +
# "a Shipify-branded platform reference" together, either order, cover
# natural wordings with no "แปลง" verb ("เอาลิงก์ 1688 นี้เข้า Shipify
# ให้หน่อย" / "ลิงก์ Taobao นี้ใช้กับ Shipify ยังไง") the same way
# _compose()'s obj_X + act_Y composites work everywhere else in
# services/conversation_semantics.py.
_LINK_WORD_RE = re.compile(r"ลิงก์|ลิงค์|link", re.IGNORECASE)
_PLATFORM_OR_SHIPIFY_WORD_RE = re.compile(r"1688|taobao|tmall|shipify", re.IGNORECASE)


def _hostname(url: str) -> Optional[str]:
    m = re.match(r"^https?://([^/\s?#]+)", url, re.IGNORECASE)
    if not m:
        return None
    return m.group(1).split("@")[-1].split(":")[0].lower()


def classify_platform(url: str) -> Optional[str]:
    """The supported platform name ('1688'/'taobao'/'tmall') for this
    URL's hostname, or None if its domain is not customer-approved."""
    host = _hostname(url)
    if not host:
        return None
    for platform, domains in SUPPORTED_DOMAINS.items():
        for d in domains:
            if host == d or host.endswith("." + d):
                return platform
    return None


def extract_urls(text: str) -> List[str]:
    return _URL_RE.findall(text or "")


def is_link_conversion_signal(message: str) -> bool:
    """True when THIS message alone gives a deterministic, structural
    reason to treat it as a link-conversion request: an explicit
    conversion verb, or an actual https?:// URL. A bare textual mention
    of a platform name/domain with NEITHER (e.g. "ผมซื้อของใน 1688") is
    deliberately NOT a signal — Case 6 of the CUSTOMER-LINK-1 spec:
    mentioning a platform is not automatically a conversion request."""
    t = message or ""
    if LINK_CONVERSION_VERB_RE.search(t):
        return True
    if _URL_RE.search(t):
        return True
    if _LINK_WORD_RE.search(t) and _PLATFORM_OR_SHIPIFY_WORD_RE.search(t):
        return True
    if LINK_CONVERSION_VERB_RE.search(t) is None and _BARE_DOMAIN_RE.search(t) and (
            re.search(r"/", t)):
        # a bare domain WITH a path segment (e.g. "1688.com/abc") reads as
        # an attempted-but-malformed link, not just a platform name.
        return True
    return False


def classify_link_request(message: str, history: Optional[List[Dict]] = None) -> Dict:
    """Deterministic pre-execution classification for a LINK_CONVERSION
    turn. Returns {"state", "url", "platform"}. `state` is one of:
    MISSING_URL / MALFORMED_URL / MULTIPLE_URLS / UNSUPPORTED_DOMAIN /
    VALID. Cross-turn carry-forward mirrors `services.decision_engine.
    _extract_system_values`: only consulted when THIS turn's message
    carries no URL of its own, most-recent user turn first."""
    urls = extract_urls(message)
    source_text = message or ""
    if not urls and history:
        for turn in reversed(history):
            if turn.get("role") == "user":
                found = extract_urls(turn.get("content") or "")
                if found:
                    urls = found
                    source_text = turn.get("content") or ""
                    break
    if not urls:
        if _BARE_DOMAIN_RE.search(message or ""):
            return {"state": "MALFORMED_URL", "url": None, "platform": None}
        return {"state": "MISSING_URL", "url": None, "platform": None}
    if len(urls) > 1:
        return {"state": "MULTIPLE_URLS", "url": None, "platform": None}
    url = urls[0]
    platform = classify_platform(url)
    if not platform:
        return {"state": "UNSUPPORTED_DOMAIN", "url": url, "platform": None}
    return {"state": "VALID", "url": url, "platform": platform}


# ── conversion-result truth typing (post-execution) ─────────────────
# Never collapse an upstream failure/rejection into a success-looking
# reply. The upstream contract for this endpoint is not otherwise
# documented in this repo (no test_payload, no prior execution log) —
# reusing the SAME already-mapped `mapped_fields` (response_mapping ->
# label/value) every other Business Action reply already composes from
# (services/decision_engine.py::_execute_selected_action), never a
# second raw-JSON re-parse: did the Action Executor itself report an
# error, and did the mapped Link field come back as a real-looking URL.
def classify_conversion_result(*, executor_error: bool, mapped_fields: Optional[Dict]) -> Dict:
    """Returns {"state", "link"}. state is one of: CONVERSION_SUCCESS /
    CONVERSION_NOT_FOUND_OR_REJECTED / UPSTREAM_FAILURE."""
    if executor_error:
        return {"state": "UPSTREAM_FAILURE", "link": None}
    link = None
    if isinstance(mapped_fields, dict):
        for v in mapped_fields.values():
            if isinstance(v, str) and v.strip().lower().startswith(("http://", "https://")):
                link = v.strip()
                break
    if link:
        return {"state": "CONVERSION_SUCCESS", "link": link}
    return {"state": "CONVERSION_NOT_FOUND_OR_REJECTED", "link": None}


# ── customer-facing replies (fixed, source-grounded wording) ────────
_REPLIES = {
    "MISSING_URL": "ได้ค่ะ ส่งลิงก์สินค้าที่ต้องการแปลงมาได้เลยค่ะ",
    "MALFORMED_URL": "ลิงก์ที่ส่งมาดูเหมือนจะไม่ถูกต้องหรือไม่สมบูรณ์ค่ะ "
                      "รบกวนส่งลิงก์สินค้าที่ถูกต้องอีกครั้งนะคะ",
    "MULTIPLE_URLS": "รบกวนส่งลิงก์สินค้าทีละ 1 ลิงก์นะคะ",
    "UNSUPPORTED_DOMAIN": "ขออภัยค่ะ ระบบแปลงลิงก์รองรับเฉพาะลิงก์จาก 1688, Taobao และ Tmall เท่านั้นค่ะ",
    "CONVERSION_NOT_FOUND_OR_REJECTED": "ขออภัยค่ะ ไม่สามารถแปลงลิงก์นี้ได้ "
                                        "รบกวนตรวจสอบลิงก์อีกครั้ง หรือส่งลิงก์สินค้าใหม่มาได้เลยค่ะ",
    "UPSTREAM_FAILURE": "ขออภัยค่ะ ระบบแปลงลิงก์ขัดข้องชั่วคราว รบกวนลองใหม่อีกครั้งภายหลังนะคะ",
}


def reply_for_state(state: str) -> Optional[str]:
    return _REPLIES.get(state)


def reply_for_success(link: str) -> str:
    return f"แอดมินแปลงลิงก์ให้เรียบร้อยค่ะ คุณลูกค้าเปิดบิลเข้ามาได้เลยนะคะ\n{link}"
