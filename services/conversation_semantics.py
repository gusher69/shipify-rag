# -*- coding: utf-8 -*-
"""SEM-GEN-1 — conversational semantic backbone.

A small, reusable layer that lets the Decision Engine understand a SHORT
follow-up inside an ONGOING import-interest conversation without a
product dictionary or per-phrase rules:

    "สนใจนำเข้ารองเท้า"      -> frame: IMPORT_INTEREST, product=รองเท้า
    "ถ้าเป็นกางเกงล่ะ"       -> CHANGE_TARGET  product -> กางเกง
    "เสื้อยืดล่ะ"            -> CHANGE_TARGET  product -> เสื้อยืด
    "200 ตัว"               -> SET_QUANTITY   quantity = 200
    "ไม่ใช่ 200 เอา 300"     -> CORRECT_QUANTITY quantity -> 300
    "ถ้าทางเรือล่ะ"          -> CHANGE_METHOD  method = sea (product/qty kept)
    "งั้นถามเรื่องคูปองดีกว่า" -> CHANGE_TOPIC   topic = coupon (frame dropped)

Split of responsibilities:
  * THIS module classifies MEANING / STATE only. It never authorizes
    private data and never invents a policy fact.
  * A CHANGE_TARGET is rewritten to the canonical "สนใจนำเข้า<product>"
    and handed back to the normal pipeline, so the existing FIX-2.3 /
    prohibited-goods eligibility path decides the business answer — the
    prohibited-product policy is NOT baked into target recognition.

The open-vocabulary step (what product/topic a bare follow-up names) is
a single, tightly-gated structured call to the EXISTING LLM service
(services/llm_service.py) — no new framework, no per-turn LLM call: it
runs only when a deterministic gate already established (a) an active
IMPORT_INTEREST frame in recent history and (b) the current message has
a follow-up shape. On any error / unparseable output it degrades to
op=UNKNOWN and the caller falls back to ordinary routing.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional

_RESOLVER_MODEL = "gpt-4o-mini"

# ── deterministic frame derivation ────────────────────────────────────
# An assistant product-acknowledgement reply ("รับทราบค่ะ เป็น<X>นะคะ",
# "รับทราบค่ะ สนใจนำเข้า<X>") is how the frame's product is carried across
# turns (same "reply wording IS the state" pattern the P2 purpose markers
# use). Never a persisted column, so no migration and no stale-state bug.
# product carriers in the assistant's own reply. Deliberately no digits
# in the class and a keyword veto (`_NOT_A_PRODUCT_RE`) so the frame-
# acknowledgement's own "…จำนวนประมาณ 200 ชิ้น…" wording is never read
# back as the product.
_ASSIST_PRODUCT_RES = (
    re.compile(r"รับทราบค่ะ\s*เป็น(?P<p>[ก-๙A-Za-z ]{2,28}?)นะคะ"),
    re.compile(r"\(สินค้า\s*(?P<p>[ก-๙A-Za-z ]{2,28}?)\s*(?:•|\)|จำนวน|ขนส่ง)"),
    re.compile(r"สนใจนำเข้า(?P<p>[ก-๙A-Za-z ]{2,28}?)(?:นะคะ|ค่ะ|$)"),
)
_NOT_A_PRODUCT_RE = re.compile(
    r"จำนวน|ชิ้น|ตัว|ประมาณ|ขนส่ง|ทางรถ|ทางเรือ|ทางอากาศ|กิโล|น้ำหนัก|ราคา|บริการ|Shipify", re.IGNORECASE)
_ASSIST_QTY_RE = re.compile(r"จำนวน(?:ประมาณ)?\s*(?P<q>\d{1,7})")
_ASSIST_METHOD_RE = re.compile(r"ขนส่งทาง(?P<m>รถ|เรือ|อากาศ)")

_USER_QTY_RE = re.compile(
    r"(?:ประมาณ\s*)?(?P<q>\d{1,7})\s*(ตัว|ชิ้น|อัน|ใบ|คู่|ชุด|กล่อง|โหล|แพ็ค|แพค|ลัง|ผืน|หลัง|เครื่อง|pcs?)\b",
    re.IGNORECASE)
_METHOD_WORD_RE = re.compile(r"ทางรถ|ทางเรือ|ทางอากาศ|ทางเครื่องบิน")

# import / product interest reused from the FIX-2.3 recognizer so the
# frame opens on exactly the turns FIX-2.3 already treats as import
# interest — no second definition.
try:  # pragma: no cover - import guard
    from services.playground_orchestrator import (
        _is_product_import_interest as _is_import_interest,
        _product_interest_noun as _import_noun,
    )
except Exception:  # pragma: no cover
    def _is_import_interest(_q: str) -> bool:
        return False

    def _import_noun(_q: str):
        return None

# a turn that clearly leaves the import frame (the customer is now asking
# about something unrelated). Used only to EXPIRE a stale frame, never to
# route.
_FRAME_EXIT_RE = re.compile(
    r"คูปอง|coupon|วอลเล็ท|wallet|ยอดเงิน|ติดตาม|แทรค|แทร็ก|เลขพัสดุ|บิลขนส่ง|สถานะ|"
    r"ที่อยู่โกดัง|เบอร์ติดต่อ|สมัคร|ลงทะเบียน|ยกเลิก|คืนเงิน|ร้องเรียน", re.IGNORECASE)

_FRAME_MAX_LOOKBACK = 14
_FRAME_MAX_GAP = 3  # off-topic-weight after the frame opened -> it is stale


@dataclass
class Frame:
    intent: str = "IMPORT_INTEREST"
    product: Optional[str] = None
    quantity: Optional[int] = None
    weight: Optional[str] = None
    dimensions: Optional[str] = None
    method: Optional[str] = None

    def as_dict(self) -> Dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


def _method_label(word: str) -> Optional[str]:
    if "เรือ" in word:
        return "sea"
    if "รถ" in word:
        return "road"
    if "อากาศ" in word or "เครื่องบิน" in word:
        return "air"
    return None


def derive_active_frame(history: Optional[List[Dict]]) -> Optional[Frame]:
    """Deterministically reconstruct the current IMPORT_INTEREST frame
    from recent conversation, or None when there is no active frame (or
    it has gone stale behind unrelated turns). No LLM."""
    turns = list(history or [])[-_FRAME_MAX_LOOKBACK:]
    if not turns:
        return None

    # 1. find the most recent frame-open: a user import-interest turn OR
    #    an assistant product-acknowledgement.
    open_idx = None
    open_product = None
    for i in range(len(turns) - 1, -1, -1):
        t = turns[i]
        content = t.get("content") or ""
        if t.get("role") == "user" and _is_import_interest(content):
            open_idx, open_product = i, _import_noun(content)
            break
        if t.get("role") == "assistant":
            for rx in _ASSIST_PRODUCT_RES:
                m = rx.search(content)
                if m and not _NOT_A_PRODUCT_RE.search(m.group("p")):
                    open_idx, open_product = i, m.group("p").strip()
                    break
            if open_idx is not None:
                break
    if open_idx is None:
        return None

    # 2. staleness — user turns AFTER the open that are neither a follow-up
    #    shape nor frame material; a hard subject switch weighs double, and
    #    the immediately-preceding user turn being a hard switch expires it.
    drift = 0
    last_user = None
    for t in turns[open_idx + 1:]:
        if t.get("role") != "user":
            continue
        c = (t.get("content") or "").strip()
        last_user = c
        if is_frame_followup(c) or _USER_QTY_RE.search(c) or _METHOD_WORD_RE.search(c):
            continue
        drift += 2 if _FRAME_EXIT_RE.search(c) else 1
    if drift >= _FRAME_MAX_GAP:
        return None
    if last_user and _FRAME_EXIT_RE.search(last_user):
        return None

    # 3. accumulate product / quantity / method across the whole window
    #    (newest wins).
    product = open_product
    quantity = method = None
    for t in reversed(turns):
        c = t.get("content") or ""
        role = t.get("role")
        if product is None and role == "assistant":
            for rx in _ASSIST_PRODUCT_RES:
                m = rx.search(c)
                if m and not _NOT_A_PRODUCT_RE.search(m.group("p")):
                    product = m.group("p").strip()
                    break
        if quantity is None:
            mq = (_ASSIST_QTY_RE.search(c) if role == "assistant" else _USER_QTY_RE.search(c))
            if mq:
                quantity = int(mq.group("q"))
        if method is None:
            mm = (_ASSIST_METHOD_RE.search(c) if role == "assistant" else None)
            if mm:
                method = _method_label(mm.group("m"))

    if product is None:
        return None
    return Frame(product=product, quantity=quantity, method=method)


# ── deterministic follow-up-shape gate ────────────────────────────────
_FOLLOWUP_SHAPE_RES = (
    re.compile(r"(ล่ะ|หละ|ละ)\s*(คะ|ครับ|ค่ะ)?\s*$"),
    re.compile(r"^\s*ถ้า(เป็น)?"),
    re.compile(r"^\s*แล้ว(ถ้า|เป็น)?"),
    re.compile(r"^\s*(ประมาณ\s*)?\d{1,7}\s*(ตัว|ชิ้น|อัน|ใบ|คู่|ชุด|กล่อง|โหล|แพ็ค|แพค|ลัง|ผืน|"
               r"หลัง|เครื่อง|กก\.?|กิโล|kg|pcs?)?\s*$", re.IGNORECASE),
    re.compile(r"ไม่ใช่\s*\S+\s*(เอา|เป็น)\s*\S+"),
    re.compile(r"งั้น.*(ดีกว่า|แทน|แล้วกัน)"),
    re.compile(r"เปลี่ยน(ไป|เป็น)?(ถาม|เรื่อง)"),
    re.compile(r"^\s*(ทางรถ|ทางเรือ|ทางอากาศ|รถ|เรือ|เครื่องบิน)\s*(ล่ะ|มั้ย|ไหม)?\s*$"),
)
_FOLLOWUP_MAX_LEN = 36


def is_frame_followup(message: str) -> bool:
    t = (message or "").strip()
    if not t or len(t) > _FOLLOWUP_MAX_LEN:
        return False
    return any(rx.search(t) for rx in _FOLLOWUP_SHAPE_RES)


# ── gated structured LLM resolver ─────────────────────────────────────
_OPS = ("CHANGE_TARGET", "SET_QUANTITY", "CORRECT_QUANTITY",
        "CHANGE_METHOD", "CHANGE_TOPIC", "CONTINUE", "UNKNOWN")

_SYS_PROMPT = (
    "You classify ONE short Thai follow-up message inside an ongoing "
    "product-import conversation. Output STRICT JSON only, no prose.\n"
    "Schema: {\"op\": <one of "
    + "|".join(_OPS) + ">, \"product\": <string|null>, "
    "\"quantity\": <integer|null>, \"method\": <\"road\"|\"sea\"|\"air\"|null>, "
    "\"topic\": <string|null>}\n"
    "Rules:\n"
    "- CHANGE_TARGET: the message names a DIFFERENT product/goods to import "
    "(e.g. \"ถ้าเป็นกางเกงล่ะ\", \"เสื้อยืดล่ะ\", \"หมวกล่ะ\"). Put the bare "
    "product noun in \"product\". Any goods noun is valid — never refuse an "
    "unfamiliar product.\n"
    "- SET_QUANTITY: a first quantity (\"200 ตัว\"). CORRECT_QUANTITY: replaces "
    "a stated quantity (\"ไม่ใช่ 200 เอา 300\"). Put the NEW number in \"quantity\".\n"
    "- CHANGE_METHOD: names a shipping method (\"ถ้าทางเรือล่ะ\"). Set \"method\".\n"
    "- CHANGE_TOPIC: the customer switches to an unrelated subject "
    "(\"งั้นถามเรื่องคูปองดีกว่า\"). Put a short topic word in \"topic\".\n"
    "- CONTINUE: stays in the frame but adds no new slot.\n"
    "- UNKNOWN: cannot tell.\n"
    "Classify MEANING ONLY. Never output a policy statement, an eligibility "
    "verdict, or any personal data."
)


def resolve_followup(message: str, frame: Frame) -> Dict:
    """Single gated LLM call. Returns {op, product, quantity, method,
    topic}. Never raises — degrades to op=UNKNOWN."""
    out = {"op": "UNKNOWN", "product": None, "quantity": None, "method": None, "topic": None}
    try:
        from services.llm_service import get_llm_service
        user = ("active_frame=" + json.dumps(frame.as_dict(), ensure_ascii=False)
                + "\nfollow_up=" + json.dumps(message or "", ensure_ascii=False))
        resp = get_llm_service().generate(
            [{"role": "system", "content": _SYS_PROMPT},
             {"role": "user", "content": user}],
            model=_RESOLVER_MODEL, temperature=0.0, max_tokens=120)
        raw = (resp.text or "").strip()
        if raw.startswith("```"):
            raw = raw.strip("`").split("\n", 1)[-1].rsplit("```", 1)[0]
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end == -1:
            return out
        data = json.loads(raw[start:end + 1])
    except Exception as e:  # pragma: no cover - network/parse degradation
        print(f"[conversation_semantics] resolver degraded ({e!r}) -> UNKNOWN")
        return out

    op = str(data.get("op") or "").strip().upper()
    if op not in _OPS:
        return out
    out["op"] = op
    prod = data.get("product")
    if isinstance(prod, str) and 1 < len(prod.strip()) <= 30:
        out["product"] = prod.strip()
    q = data.get("quantity")
    if isinstance(q, (int, float)) and 0 < int(q) < 10_000_000:
        out["quantity"] = int(q)
    meth = str(data.get("method") or "").strip().lower()
    if meth in ("road", "sea", "air"):
        out["method"] = meth
    topic = data.get("topic")
    if isinstance(topic, str) and topic.strip():
        out["topic"] = topic.strip()[:40]

    # deterministic safety net: a CHANGE_TARGET with no usable product is
    # not actionable.
    if out["op"] == "CHANGE_TARGET" and not out["product"]:
        out["op"] = "UNKNOWN"
    if out["op"] in ("SET_QUANTITY", "CORRECT_QUANTITY") and out["quantity"] is None:
        mq = _USER_QTY_RE.search(message or "") or re.search(r"(\d{1,7})", message or "")
        if mq:
            out["quantity"] = int(mq.group(1))
        else:
            out["op"] = "UNKNOWN"
    return out


_METHOD_TH = {"road": "ทางรถ", "sea": "ทางเรือ", "air": "ทางอากาศ"}


def frame_ack_reply(frame: Frame, *, changed: str) -> str:
    """Deterministic acknowledgement that also RE-STATES the frame so the
    next turn's derive_active_frame() can read it back. Never a policy
    claim, never an eligibility verdict."""
    bits = []
    if frame.product:
        bits.append(f"สินค้า {frame.product}")
    if frame.quantity:
        bits.append(f"จำนวนประมาณ {frame.quantity} ชิ้น")
    if frame.method:
        bits.append(f"ขนส่ง{_METHOD_TH.get(frame.method, frame.method)}")
    summary = " • ".join(bits) if bits else "รายละเอียดการนำเข้า"
    head = "รับทราบค่ะ "
    if changed == "quantity":
        head = f"รับทราบค่ะ ปรับเป็นจำนวนประมาณ {frame.quantity} ชิ้นนะคะ "
    elif changed == "method":
        head = f"รับทราบค่ะ เปลี่ยนเป็นขนส่ง{_METHOD_TH.get(frame.method, frame.method)}นะคะ "
    ask = ""
    if not frame.quantity:
        ask = " รบกวนแจ้งจำนวนโดยประมาณด้วยนะคะ"
    elif not frame.method:
        ask = " สนใจส่งทางรถหรือทางเรือคะ"
    elif not frame.weight:
        ask = " รบกวนแจ้งน้ำหนักโดยประมาณเพิ่มเติมได้ไหมคะ"
    return f"{head}({summary}){ask}"
