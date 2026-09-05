# -*- coding: utf-8 -*-
"""CUSTOMER-RED-HOTFIX-2 -- Purchase Withdrawal / Shipping Withdrawal.

Cases CUS-S05 (Purchase Withdrawal) / CUS-S12 (Shipping Withdrawal,
Ai.xlsx sheet '2.tongchecknairabop' rows 5/12). Real LINE failure: both
questions were swallowed by an unrelated flow (a stale operational
"add_vat" ack, already fixed separately) or, once that stopped, fell to
ordinary RAG search — which for Purchase Withdrawal decisively matched
the WRONG chunk (an order-cancellation / Chinese-seller-refund FAQ that
happens to share "คืนเงิน" vocabulary), and for Shipping Withdrawal had
no matching content in the Knowledge Base at all, producing a bare "no
confirmed info" reply.

Both answers are plain, non-personalized HOW-TO instructions (no ERP
call, no per-customer data) — exactly the kind of content the Data
Routing Rule says belongs in the Knowledge Base, not hardcoded in
Python. This module does NOT hardcode the answer text: it reads it
straight out of `knowledge_chunks` by a STABLE `intent` tag
(PURCHASE_WITHDRAWAL / SHIPPING_WITHDRAWAL_SP / SHIPPING_WITHDRAWAL_FT
-- see tools/add_withdrawal_kb_content.py, the idempotent ingestion
script that seeds/updates those rows via the existing embedding
pipeline). Retrieval here is a direct, deterministic tag lookup, never
vector similarity -- the customer source is explicit that once the
intent (and, for shipping, the brand) is known, the answer must not be
left to "which chunk scores highest".

Shipping Withdrawal additionally branches by brand (SP vs FT) per the
customer source ("หัวข้อนี้จะต้องเช็คว่าเป็นแบรนด์ไหน SP หรือ FT").
The brand is resolved from the customer's own VERIFIED CustCode prefix
(the same "FT3182"-style code already used everywhere else in this
codebase as the account identity) when available -- never guessed --
so a trusted binding never has to be re-asked; otherwise the customer
is asked once, exactly as the source instructs.
"""
import re
from typing import Dict, Optional

_BRAND_PREFIX_RE = re.compile(r"^([A-Za-z]+)")
_KNOWN_BRANDS = ("SP", "FT")

ASK_BRAND_REPLY = (
    "เรื่องนี้คำตอบจะแตกต่างกันไปตามแบรนด์ค่ะ รบกวนแจ้งด้วยนะคะว่าเป็นแบรนด์ SP หรือ FT คะ"
)

# Content-not-yet-published fallback -- never fabricated, never a fake
# success. Distinct from a generic RAG "no info" reply so a developer
# can tell "the flow fired but the KB row is missing" apart from "the
# flow never fired at all".
_KB_MISSING_REPLY = (
    "ขออภัยค่ะ ตอนนี้ข้อมูลส่วนนี้ยังไม่พร้อมในระบบ รบกวนรอแอดมินตรวจสอบและติดต่อกลับนะคะ"
)


def resolve_shipping_withdrawal_brand(customer_context: Optional[Dict]) -> Optional[str]:
    """SP / FT / None (unresolvable -> caller must ask, never guess)."""
    cust_code = (customer_context or {}).get("cust_code") or ""
    m = _BRAND_PREFIX_RE.match(cust_code.strip())
    if not m:
        return None
    brand = m.group(1).upper()
    return brand if brand in _KNOWN_BRANDS else None


def fetch_kb_answer(sb, intent_tag: str) -> Optional[str]:
    """The active knowledge_chunks.content tagged with this stable
    `intent` value, or None if not present. A direct tag lookup --
    never a vector-similarity search -- so a superficially-similar but
    wrong chunk (e.g. an order-cancellation refund FAQ) can never win
    once the intent is already known."""
    try:
        r = (sb.table("knowledge_chunks").select("content")
             .eq("intent", intent_tag).eq("is_active", True)
             .limit(1).execute())
    except Exception:
        return None
    if not r.data:
        return None
    return (r.data[0].get("content") or "").strip() or None


def purchase_withdrawal_reply(sb) -> str:
    return fetch_kb_answer(sb, "PURCHASE_WITHDRAWAL") or _KB_MISSING_REPLY


def shipping_withdrawal_reply(sb, customer_context: Optional[Dict]) -> str:
    brand = resolve_shipping_withdrawal_brand(customer_context)
    if brand is None:
        return ASK_BRAND_REPLY
    return fetch_kb_answer(sb, f"SHIPPING_WITHDRAWAL_{brand}") or _KB_MISSING_REPLY
