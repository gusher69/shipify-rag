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

import requests

_URL_RE = re.compile(r"https?://[^\s<>\"']+")

# PHASE-LINK-1688 — FastTrade's GetUrlProductDetail endpoint REQUIRES a
# CustCode even to return a PUBLIC (guest) product link: a request with
# none / an unknown one gets HTTP 400. Link Conversion is public
# (CUS-P20: "must NOT require a customer code or identity verification"),
# so a request from a customer with no verified/profile CustCode falls
# back to this Shipify guest account code. Confirmed live: FT0000 is a
# real Shipify guest account that this endpoint accepts and answers with
# the guest product URL; arbitrary shape-valid codes are rejected as
# "not found". The user is never asked and never verified.
GUEST_CUSTCODE = "FT0000"

# 1688 short / QR redirect hosts. FastTrade does NOT follow these itself
# (it 400s them), so the redirect is resolved here first — SSRF-guarded —
# and only a resolved *.1688.com product URL is ever handed onward.
_1688_SHORT_HOSTS = ("qr.1688.com", "s.1688.com", "m.1688.com/s")
_1688_HOST_SUFFIX = "1688.com"
_MOBILE_OFFER_RE = re.compile(r"^https?://m\.1688\.com/offer/(\d{4,})\.html", re.IGNORECASE)

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
    VALID.

    CUSTOMER-LINK-REAL-2 (confirmed REAL LINE regression, session
    6c9b9026-434e-403d-b513-e2c90752754b, turns 828-829): this used to
    fall back to the most recent URL anywhere in `history` whenever the
    CURRENT message carried none. That is exactly the "stale implicit
    reuse" the customer flagged: a completed conversion at turn 826-827
    (product 696614936668) leaked into a brand-new, URL-less "แปลงลิงก์
    ให้ทีค่ะ" at turn 828, which wrongly re-announced success for the
    OLD product instead of asking for a new link. Per the CORE RULE —
    URL AUTHORITY, only two provenances are ever valid: the CURRENT
    TURN's own URL, or a reply to an IMMEDIATE requested-URL slot — and
    an immediate reply to that ask is, BY DEFINITION, a message that
    itself contains the URL (that is what "answering the ask" means),
    so it is already covered by scanning `message` alone. There is no
    longer a second turn in this flow's own lifecycle (CustCode is
    never collected after the URL, see CUSTOMER-LINK-1) where a value
    from an EARLIER turn would need to be recalled at a LATER one.
    `history` is intentionally unused here now — never consulted for a
    URL, at any distance, for any reason. The `_extract_system_values`
    carry-forward this originally mirrored solves a different, still
    valid problem (system_generated resolution reading a URL back out
    of `history` at ACTUAL EXECUTION time) — this pre-execution
    classifier is reached BEFORE execution and gates it, so its own
    "no URL" verdict here already prevents that carry-forward from ever
    firing for a genuinely fresh, URL-less request (see decision_
    engine.py's scoped MISSING_URL short-circuit, which returns before
    the Executor — and therefore before `_extract_system_values` — is
    ever reached)."""
    urls = extract_urls(message)
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


def normalize_supported_url(url: str) -> str:
    """Canonicalize a supported-platform URL to the shape the existing
    conversion path handles, dropping tracking / query noise. Currently:
    a 1688 MOBILE offer URL (m.1688.com/offer/<ITEM_ID>.html?spm=...) is
    rewritten to its desktop equivalent (detail.1688.com/offer/<ITEM_ID>.html)
    — same numeric ITEM_ID, no invented format. Any other supported URL
    is returned unchanged (FastTrade already accepts detail.1688 / Taobao
    / Tmall directly)."""
    m = _MOBILE_OFFER_RE.match(url or "")
    if m:
        return f"https://detail.1688.com/offer/{m.group(1)}.html"
    return url


def is_1688_short_url(url: str) -> bool:
    """True for a 1688 QR / short-redirect URL that must be resolved to a
    real product URL before the conversion call."""
    host = _hostname(url) or ""
    if not host.endswith("." + _1688_HOST_SUFFIX) and host != _1688_HOST_SUFFIX:
        return False
    if host in ("qr.1688.com", "s.1688.com"):
        return True
    # m.1688.com/s/... share links (not the /offer/<id>.html product form)
    if host == "m.1688.com":
        return bool(re.match(r"^https?://m\.1688\.com/s/", url or "", re.IGNORECASE)) \
            and not _MOBILE_OFFER_RE.match(url or "")
    return False


def resolve_1688_short_url(url: str, *, max_hops: int = 3, timeout: float = 4.0) -> Optional[str]:
    """Follow a 1688 short/QR redirect to its final URL. SSRF-hardened:
    HTTPS only, every hop's host must be *.1688.com, at most `max_hops`
    redirects, a short timeout, redirects followed manually (no body
    fetched, no auto-redirect to an arbitrary host). Returns the final
    URL string, or None on any failure / policy violation."""
    current = url or ""
    for _ in range(max_hops + 1):
        if not current.lower().startswith("https://"):
            return None
        host = _hostname(current) or ""
        if not (host == _1688_HOST_SUFFIX or host.endswith("." + _1688_HOST_SUFFIX)):
            return None
        try:
            resp = requests.get(current, allow_redirects=False, timeout=timeout, stream=True,
                                headers={"User-Agent": "Mozilla/5.0"})
        except Exception:
            return None
        finally:
            try:
                resp.close()  # never read the body
            except Exception:
                pass
        if resp.status_code in (301, 302, 303, 307, 308):
            loc = resp.headers.get("Location") or ""
            if loc.startswith("/"):
                loc = f"https://{host}{loc}"
            if not loc.lower().startswith("https://"):
                return None
            nxt_host = _hostname(loc) or ""
            if not (nxt_host == _1688_HOST_SUFFIX or nxt_host.endswith("." + _1688_HOST_SUFFIX)):
                return None
            current = loc
            continue
        # non-redirect response: this is the final URL
        return current
    return None


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
    "SHORT_URL_UNRESOLVED": "ลิงก์นี้ยังไม่พบรหัสสินค้าค่ะ ลองส่งลิงก์หน้าสินค้า 1688 แบบเต็มมาอีกครั้งได้เลยค่ะ",
    "CONVERSION_NOT_FOUND_OR_REJECTED": "ขออภัยค่ะ ไม่สามารถแปลงลิงก์นี้ได้ "
                                        "รบกวนตรวจสอบลิงก์อีกครั้ง หรือส่งลิงก์สินค้าใหม่มาได้เลยค่ะ",
    "UPSTREAM_FAILURE": "ขออภัยค่ะ ระบบแปลงลิงก์ขัดข้องชั่วคราว รบกวนลองใหม่อีกครั้งภายหลังนะคะ",
}


def reply_for_state(state: str) -> Optional[str]:
    return _REPLIES.get(state)


def reply_for_success(link: str) -> str:
    return f"แอดมินแปลงลิงก์ให้เรียบร้อยค่ะ คุณลูกค้าเปิดบิลเข้ามาได้เลยนะคะ\n{link}"
