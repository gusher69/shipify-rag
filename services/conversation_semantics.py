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
from dataclasses import dataclass, asdict, field
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


# ═════════════════════════════════════════════════════════════════════
# SEMANTIC-FIRST-1 — central conversational semantic interpretation
# ═════════════════════════════════════════════════════════════════════
# GENERALISES this module from an import-interest follow-up backbone into
# the ONE semantic interpretation layer the Decision Engine consults
# FIRST, before any workflow-local text handling. Every existing
# conversational business flow (shipment status, invoice, warehouse /
# pickup, self-pickup, coupon, product policy, charter truck, shipping
# estimate, address change, import interest, public/private, short
# follow-ups / corrections / comparisons / topic changes) consumes the
# normalised {intent_family, entities, is_private, follow_up_op} contract
# `interpret()` produces — they no longer each re-classify the raw Thai.
#
# It is a COMPOSITIONAL meaning model, NOT a phrase list: a small set of
# orthogonal marker dimensions (OBJECT / ACTION / ROLE / FOLLOW-UP) is
# detected independently and COMPOSED into a family, so paraphrases that
# share meaning collapse to the same family without an exact-phrase rule.
# A single gated LLM call (same pattern as resolve_followup) only
# disambiguates genuinely novel / ambiguous phrasing and always degrades
# back to the deterministic result. Genuinely structural inputs (bare
# ids, URLs, numeric-only, empty, platform postbacks) skip semantics.
#
# Security / authorization / business truth stay deterministic: is_private
# is a pure self-reference test and NO policy verdict or eligibility fact
# is ever produced here.

INTENT_FAMILIES = (
    "SHIPMENT_STATUS", "INVOICE", "PICKUP_LOCATION", "SELF_PICKUP",
    "COUPON_USAGE", "MY_COUPONS", "PRODUCT_POLICY", "CHARTER_TRUCK",
    "SHIPPING_ESTIMATE", "ADDRESS_CHANGE", "IMPORT_INTEREST",
    "GENERAL", "UNKNOWN",
)

FOLLOWUP_OPS = ("CORRECTION", "COMPARISON", "TOPIC_CHANGE", "CONTINUE",
                "SET_VALUE", "NONE")


@dataclass
class Interpretation:
    """The normalised semantic contract every conversational flow reads."""
    intent_family: str = "UNKNOWN"
    entities: Dict[str, object] = field(default_factory=dict)
    is_private: bool = False
    follow_up_op: str = "NONE"
    confidence: float = 0.0
    source: str = "none"          # structural | deterministic | llm | degraded

    def as_dict(self) -> Dict:
        d = asdict(self)
        d["entities"] = {k: v for k, v in (self.entities or {}).items() if v not in (None, "", [])}
        return d


# ── orthogonal meaning markers ───────────────────────────────────────
# OBJECT — what the message is ABOUT.
_OBJ_INVOICE = re.compile(r"ใบกำกับ|ใบเสร็จ|ใบแจ้งหนี้|tax\s*invoice|ภาษีมูลค่าเพิ่ม|\bvat\b|ออกบิลภาษี|เอกสารภาษี", re.IGNORECASE)
_OBJ_WAREHOUSE = re.compile(r"โกดัง|คลังสินค้า|คลังไทย|คลังจีน|จุดรับ|จุดรับของ|จุดส่ง|ที่รับของ|ที่รับสินค้า|โรงพัก|สาขา|warehouse", re.IGNORECASE)
_OBJ_COUPON = re.compile(r"คูปอง|ส่วนลด|โค้ดส่วนลด|coupon|voucher|โปรโมชั่นส่วนลด", re.IGNORECASE)
_OBJ_TRUCK = re.compile(r"เหมารถ|เหมา\s*รถ|รถเหมา|เรียกรถ|จ้างรถ|รถส่งต่อ|เหมาคันรถ|charter\s*truck", re.IGNORECASE)
_OBJ_COST = re.compile(r"ค่าส่ง|ค่าขนส่ง|ค่านำเข้า|ค่าจัดส่ง|เรทส่ง|เรทนำเข้า|ราคาส่ง|ค่าระวาง|shipping\s*cost", re.IGNORECASE)
_OBJ_PARCEL = re.compile(r"พัสดุ|ออเดอร์|order|ล็อตสินค้า|กล่องสินค้า|ของที่สั่ง|ของที่ส่ง|สินค้าที่สั่ง|ของผม|ของฉัน|สินค้าผม|บิลผม|บิลฉัน|เลขบิลผม", re.IGNORECASE)
_OBJ_ADDRESS = re.compile(r"ที่อยู่จัดส่ง|ที่อยู่ผู้รับ|ที่อยู่ในการจัดส่ง|ปลายทางจัดส่ง|delivery\s*address|ที่อยู่ส่งของ|ที่อยู่|ปลายทาง|ผู้รับ|เบอร์ผู้รับ", re.IGNORECASE)

# ACTION — what they want DONE.
_ACT_LOCATE = re.compile(r"ที่ไหน|ตรงไหน|อยู่ไหน|ที่ใด|ที่ตั้ง|แผนที่|พิกัด|เส้นทางไป|ไปยังไง|แถวไหน|ย่านไหน|โซนไหน|เขตไหน|อยู่แถว|\bwhere\b", re.IGNORECASE)
_ACT_STATUS = re.compile(r"ถึงไหน|ถึงหรือยัง|ถึงไทย|ถึงจีน|มาถึงยัง|ไปถึงไหน|สถานะ|คืบหน้า|อัปเดต|อัพเดท|เป็น(?:ยัง)?ไงบ้าง|ออกจากจีนยัง|ส่งของ(?:ให้)?\S{0,6}(?:หรือ)?ยัง|เช็ก\S{0,4}สถานะ|เช็คสถานะ|ตรวจสอบสถานะ|ติดตามพัสดุ|ติดตามสินค้า", re.IGNORECASE)
_ACT_ISSUE_GET = re.compile(r"ออก\S{0,6}(?:ได้ไหม|ให้|หรือเปล่า|หรือไม่)|ออกให้ได้|ขอ\S{0,3}(?:ใบ|เอกสาร)|มี\S{0,10}(?:ไหม|มั้ย)|ให้\S{0,6}(?:หรือเปล่า|ไหม|มั้ย)|issue|provide", re.IGNORECASE)
_ACT_HOWTO = re.compile(r"ใช้\S{0,6}(?:ยังไง|อย่างไร|ตรงไหน|ที่ไหน)|วิธีใช้|กดตรงไหน|กดยังไง|กดใช้\S{0,4}(?:ตรงไหน|ยังไง)|ทำยังไง|ขั้นตอน\S{0,6}ใช้|how\s*to\s*use", re.IGNORECASE)
_ACT_LIST_MINE = re.compile(r"มี\S{0,10}อะไรบ้าง|มี\S{0,6}(?:กี่|เท่าไหร่)|เหลือ\S{0,6}(?:ไหม|เท่าไหร่|กี่)|ของผม\S{0,12}(?:มี|เหลือ)|บัญชีผม|บัญชีฉัน|ในระบบผม", re.IGNORECASE)
_ACT_PERMIT = re.compile(r"ได้ไหม|ได้มั้ย|ได้มัย|ได้ป่าว|ได้บ่|ได้หรือเปล่า|ได้รึเปล่า|ได้หรือไม่|สามารถ\S{0,24}ได้|\bcan\s+i\b|allowed", re.IGNORECASE)
# an explicit CALCULATE / ESTIMATE verb — distinct from a bare "how much"
# price question (that stays a rate FAQ, per CUSTOMER-CALC-1).
_ACT_CALC_VERB = re.compile(r"คำนวณ|คำนวน|ประเมิน|ช่วยคิด|คิดค่า|คิดราคา|ตีราคา|estimate|calculate|quote", re.IGNORECASE)
_PRICE_Q_RE = re.compile(r"เท่าไหร่|เท่าไร|กี่บาท|ราคาเท่า|ราวๆ\s*กี่", re.IGNORECASE)
# a description of a SPECIFIC parcel — turns a price question into a
# calculation request.
_PARCEL_DESC_RE = re.compile(
    r"กล่อง|ลัง|ชิ้นนี้|ของชิ้นนี้|ชิ้นเดียว|ของเท่านี้|เท่านี้|"
    r"ขนาด\s*(?:ประมาณ|นี้|เท่านี้)|น้ำหนัก\s*\d|\d+\s*(?:กิโล|กก|โล|ชิ้น|กล่อง|ลัง)", re.IGNORECASE)
_ACT_ESTIMATE = _ACT_CALC_VERB   # back-compat alias for _worth_llm_disambiguation
_ACT_CHANGE = re.compile(r"เปลี่ยน|เปลี่ยนแปลง|สลับ|แก้ไข|เเก้ไข|แก้\s*(?:เป็น|ให้|ที่อยู่|ที่ส่ง|ปลายทาง|ชื่อ|เบอร์|ข้อมูล|ผู้รับ)|ย้าย|ปรับ\S{0,4}(?:เป็น|ที่)|ขอเปลี่ยน|ขอแก้|ขอสลับ|modify|\bchange\b", re.IGNORECASE)
_ACT_SELF_DO = re.compile(r"เอง|ด้วยตัวเอง|ตัวเอง|มารับเอง|ไปรับเอง|มาเอาเอง|ไปเอาเอง|self\s*pick", re.IGNORECASE)
_ACT_CHARTER = re.compile(r"เหมา|เรียก|จ้าง|ใช้บริการเหมา|\bhire\b", re.IGNORECASE)
_PICKUP_VERB = re.compile(r"รับของ|รับสินค้า|มารับ|ไปรับ|เข้ารับ|มาเอา|ไปเอา|รับเอง|รับพัสดุ", re.IGNORECASE)
_SHIP_VERB = re.compile(r"ส่งได้|ส่งไหว|นำเข้า|เอาเข้า|ขนส่งได้|ส่งไป(?:ได้)?|ส่งเข้า(?:มา)?|ส่งมา(?:ไทย)?|เข้ามาได้|ฝากส่ง|ฝากนำเข้า", re.IGNORECASE)

_ROLE_SELF = re.compile(r"ของผม|ของฉัน|ของดิฉัน|ของหนู|ของเรา|ของกระผม|บิลผม|บิลฉัน|ออเดอร์ผม|ออเดอร์ฉัน|พัสดุผม|พัสดุฉัน|บัญชีผม|บัญชีฉัน|เลขบิลผม|ผมสั่ง|ฉันสั่ง|ที่ผมสั่ง|ที่ฉันสั่ง", re.IGNORECASE)
# a record-identifying private marker — excludes SELF_PICKUP (that stays a
# public how-to per CUSTOMER-RAG-1.1) but not a bare first-person pronoun.
_PRIV_RECORD_RE = re.compile(r"เลขบิล|เลขที่บิล|บิลผม|บิลฉัน|ออเดอร์ผม|พัสดุผม|order\s*id", re.IGNORECASE)

_STATUS_STRONG = re.compile(r"ถึงไหน(?:แล้ว)?|ถึง(?:ไทย|จีน|โกดัง)?(?:แล้ว)?(?:หรือ)?ยัง|มาถึงยัง|ไปถึงไหน|ออกจาก(?:จีน|โกดัง|ไทย)?(?:แล้ว)?(?:หรือ)?ยัง|ของถึงยัง|เช็ก\S{0,4}สถานะ|เช็คสถานะ|ตรวจสอบสถานะ|ติดตามพัสดุ|ส่งของให้\S{0,6}(?:หรือ)?ยัง", re.IGNORECASE)
_EST_COMPOSITE = re.compile(
    r"เสีย(?:เงิน|ค่า)?\S{0,8}(?:เท่าไหร่|เท่าไร|กี่บาท)"
    r"|(?:คิด|ประเมิน|คำนวณ|คำนวน|ตี)\S{0,4}(?:ค่าส่ง|ค่าขนส่ง|ค่านำเข้า|ราคาค่าส่ง)", re.IGNORECASE)
_MEASURE_RE = re.compile(r"\d+\s*(?:กิโล|กก\.?|kg|โล|ตัน)|\d+\s*[x×*]\s*\d+|กว้าง\s*\d+|ยาว\s*\d+|สูง\s*\d+", re.IGNORECASE)
_DEST_MARKER_RE = re.compile(r"(?:ไป|ปลายทาง|ส่งไปที่|ส่งไป|ที่)\s*([ก-๙A-Za-z][ก-๙A-Za-z0-9 .\-]{1,20})", re.IGNORECASE)

# a follow-up that clearly changes the CURRENT calculation / frame.
_CORR_RE = re.compile(r"ไม่ใช่\s*\S+.{0,12}(?:เป็น|เอา)\s*\S")
_CMP_RE = re.compile(r"^\s*(?:ถ้า|แล้วถ้า|หากเป็น|สมมติ|งั้นถ้า).{0,28}(?:ล่ะ|ล้ะ|หละ|มั้ย|ไหม)\s*(?:คะ|ครับ|ค่ะ)?\s*$|แล้ว\S{0,18}(?:ล่ะ|หละ)\s*(?:คะ|ครับ|ค่ะ)?\s*$")
_TOPIC_RE = re.compile(r"งั้น.{0,24}(?:ดีกว่า|แทน|แล้วกัน)|เปลี่ยนไป(?:ถาม|เรื่อง)|ขอถามเรื่อง|เอาเป็นว่าถาม|ไม่เอาแล้ว\s*ถาม")

# genuinely structural (non-conversational) inputs — skip semantics.
_STRUCT_URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)
_STRUCT_ID_RE = re.compile(r"^[A-Za-z]{1,4}\d{5,}[A-Za-z0-9\-]*$")
_STRUCT_NUMERIC_RE = re.compile(r"^[\d\s.,:x×*/\-]+$", re.IGNORECASE)
_STRUCT_POSTBACK_RE = re.compile(r"^(?:action=|postback[:=]|__|/[a-z_]+$)", re.IGNORECASE)
_THAI_CHAR_RE = re.compile(r"[ก-๙]")


def _structural_kind(t: str) -> Optional[str]:
    if not t:
        return "empty"
    if _STRUCT_URL_RE.match(t):
        return "url"
    if _STRUCT_POSTBACK_RE.match(t):
        return "postback"
    if _STRUCT_ID_RE.match(t) and " " not in t:
        return "identifier"
    if _STRUCT_NUMERIC_RE.match(t) and not _THAI_CHAR_RE.search(t):
        return "numeric"
    return None


def _followup_op(t: str) -> str:
    if _CORR_RE.search(t):
        return "CORRECTION"
    if _CMP_RE.search(t):
        return "COMPARISON"
    if _TOPIC_RE.search(t):
        return "TOPIC_CHANGE"
    if is_frame_followup(t):
        if _USER_QTY_RE.search(t) or re.match(r"^\s*(?:ประมาณ\s*)?\d", t):
            return "SET_VALUE"
        return "CONTINUE"
    return "NONE"


def _compose(t: str) -> "tuple[str, float, Dict]":
    """Deterministic compositional classification. Returns
    (intent_family, confidence, entities). Order = most distinctive
    composite first."""
    ent: Dict[str, object] = {}
    obj_inv = bool(_OBJ_INVOICE.search(t))
    obj_wh = bool(_OBJ_WAREHOUSE.search(t))
    obj_cp = bool(_OBJ_COUPON.search(t))
    obj_tk = bool(_OBJ_TRUCK.search(t))
    obj_cost = bool(_OBJ_COST.search(t))
    obj_parcel = bool(_OBJ_PARCEL.search(t))
    obj_addr = bool(_OBJ_ADDRESS.search(t))

    a_locate = bool(_ACT_LOCATE.search(t))
    a_status = bool(_ACT_STATUS.search(t)) or bool(_STATUS_STRONG.search(t))
    a_issue = bool(_ACT_ISSUE_GET.search(t))
    a_howto = bool(_ACT_HOWTO.search(t))
    a_listmine = bool(_ACT_LIST_MINE.search(t))
    a_permit = bool(_ACT_PERMIT.search(t))
    a_calc = bool(_ACT_CALC_VERB.search(t))
    a_price_q = bool(_PRICE_Q_RE.search(t))
    a_parcel_desc = bool(_PARCEL_DESC_RE.search(t))
    a_est_composite = bool(_EST_COMPOSITE.search(t))
    a_change = bool(_ACT_CHANGE.search(t))
    a_self = bool(_ACT_SELF_DO.search(t))
    a_charter = bool(_ACT_CHARTER.search(t))
    v_pickup = bool(_PICKUP_VERB.search(t))
    v_ship = bool(_SHIP_VERB.search(t))
    has_measure = bool(_MEASURE_RE.search(t))
    m = _method_label(t) if _METHOD_WORD_RE.search(t) else None
    if m:
        ent["method"] = m

    # CHARTER TRUCK — a charter-truck object is itself decisive (a hire /
    # request move is implied by naming it).
    if obj_tk:
        md = _DEST_MARKER_RE.search(t)
        if md:
            ent["destination"] = md.group(1).strip()
        return "CHARTER_TRUCK", 0.85, ent

    # SHIPPING ESTIMATE — an explicit calculate/estimate verb, a
    # "how-much-will-it-cost-me" phrasing, or a price question about a
    # SPECIFIC parcel. A bare generic rate question ("ค่านำเข้าเท่าไหร่",
    # no parcel, no verb) is NOT this — it stays a rate FAQ
    # (CUSTOMER-CALC-1).
    if a_calc and (obj_cost or obj_parcel or has_measure or a_price_q or a_parcel_desc):
        return "SHIPPING_ESTIMATE", 0.85, ent
    if a_est_composite:
        return "SHIPPING_ESTIMATE", 0.8, ent
    if a_price_q and a_parcel_desc and (v_ship or obj_cost or re.search(r"ส่งมา|มาไทย|ส่งของ", t)):
        return "SHIPPING_ESTIMATE", 0.75, ent
    if obj_cost and (has_measure or m):
        return "SHIPPING_ESTIMATE", 0.7, ent

    # INVOICE — a tax-document object with an issue / permit / how-to move.
    if obj_inv and (a_issue or a_permit or a_howto or a_locate):
        return "INVOICE", 0.85, ent
    if obj_inv:
        return "INVOICE", 0.55, ent

    # SELF PICKUP — a self / personally marker with a pickup verb and NO
    # where-question (a public how-to, per CUSTOMER-RAG-1.1: "เอง" + a
    # where-question is a LOCATION question, handled next).
    if a_self and (v_pickup or obj_wh) and not _PRIV_RECORD_RE.search(t) and not a_locate:
        return "SELF_PICKUP", 0.8, ent

    # PICKUP LOCATION — a warehouse / pickup-point object (or a pickup
    # verb) with a where-question. A "เอง" marker does NOT block this
    # (CUSTOMER-RAG-1.1: the where-question dominates).
    if (obj_wh or v_pickup) and a_locate:
        return "PICKUP_LOCATION", 0.85, ent

    # COUPON — how-to vs. list-mine.
    if obj_cp and a_howto:
        return "COUPON_USAGE", 0.85, ent
    if obj_cp and a_listmine:
        return "MY_COUPONS", 0.8, ent
    if obj_cp and (a_permit or a_issue):
        return "COUPON_USAGE", 0.6, ent
    if obj_cp:
        return "COUPON_USAGE", 0.5, ent

    # PRODUCT POLICY — a can-I-ship / can-I-import move on some goods,
    # without a cost / warehouse / coupon / invoice object.
    if a_permit and v_ship and not (obj_cost or obj_wh or obj_cp or obj_inv):
        lead = re.split(r"\s+", t.strip())[0]
        if lead and _THAI_CHAR_RE.search(lead):
            ent["product"] = lead
        return "PRODUCT_POLICY", 0.7, ent

    # SHIPMENT STATUS — a parcel object (or a strong status phrasing)
    # asking about progress.
    if (obj_parcel and a_status) or _STATUS_STRONG.search(t):
        return "SHIPMENT_STATUS", 0.8, ent

    # ADDRESS CHANGE — a change move on a delivery-address object, or a
    # "send it somewhere else instead" phrasing (the "instead" IS the
    # change signal).
    if (a_change and obj_addr) or re.search(
            r"ส่งไป\S{0,12}(?:อีกที่|ที่อื่น|ที่ใหม่)|เปลี่ยนที่ส่ง|ส่ง(?:ของ)?ไป(?:ที่|ยัง)\S{1,20}แทน|"
            r"ส่ง\S{0,8}ที่อื่น\S{0,4}แทน|จัดส่ง\S{0,8}ที่อื่นแทน", t):
        return "ADDRESS_CHANGE", 0.8, ent

    # IMPORT INTEREST — reuse the FIX-2.3 recogniser verbatim.
    if _is_import_interest(t):
        p = _import_noun(t)
        if p:
            ent["product"] = p
        return "IMPORT_INTEREST", 0.7, ent

    # a recognised OBJECT but no actionable move -> a general FAQ turn
    # (keeps it OUT of the LLM-disambiguation tier and lets the ordinary
    # RAG / regex path answer it).
    if obj_cost or obj_inv or obj_wh or obj_cp or obj_parcel or obj_addr:
        return "GENERAL", 0.5, ent

    return "UNKNOWN", 0.0, ent


# ── gated LLM family disambiguation ──────────────────────────────────
_FAMILY_SYS_PROMPT = (
    "You label ONE Thai customer message for an import / logistics support "
    "bot with its INTENT FAMILY. Output STRICT JSON only, no prose.\n"
    "Schema: {\"family\": <one of " + "|".join(INTENT_FAMILIES) + ">, "
    "\"is_private\": <true|false>, \"product\": <string|null>, "
    "\"destination\": <string|null>, \"method\": <\"road\"|\"sea\"|\"air\"|null>}\n"
    "Families: SHIPMENT_STATUS=asking progress/where-is-it of a shipment/order; "
    "INVOICE=tax invoice / receipt issuance; PICKUP_LOCATION=where is the "
    "warehouse / pickup point; SELF_PICKUP=may I collect it myself / how; "
    "COUPON_USAGE=how to use a coupon/discount; MY_COUPONS=what coupons do I "
    "have (private); PRODUCT_POLICY=can this kind of goods be shipped/imported; "
    "CHARTER_TRUCK=hire / charter a whole truck for local delivery; "
    "SHIPPING_ESTIMATE=estimate/quote a shipping cost; ADDRESS_CHANGE=change "
    "the delivery address/recipient; IMPORT_INTEREST=wants to import some "
    "product (early sales interest); GENERAL=any other FAQ; UNKNOWN=cannot "
    "tell.\n"
    "is_private=true only when it refers to the customer's OWN specific "
    "record/account. Classify MEANING ONLY — never a policy verdict, an "
    "eligibility answer, or personal data."
)


# once the resolver LLM is proven unreachable in this process, stop
# calling it — every caller then just keeps the deterministic family.
_LLM_RESOLVER_DOWN = False


def _llm_family(message: str, history: Optional[List[Dict]]) -> Optional[Dict]:
    """Single gated LLM call -> {family, is_private, product, destination,
    method}. Returns None on any failure so the caller marks the result
    'degraded' and keeps the deterministic family."""
    global _LLM_RESOLVER_DOWN
    if _LLM_RESOLVER_DOWN:
        return None
    try:
        from services.llm_service import get_llm_service
        recent = " | ".join(
            (t.get("content") or "")[:80] for t in (history or [])[-4:] if t.get("role") == "user")
        user = "recent_user_turns=" + json.dumps(recent, ensure_ascii=False) + \
               "\nmessage=" + json.dumps(message or "", ensure_ascii=False)
        resp = get_llm_service().generate(
            [{"role": "system", "content": _FAMILY_SYS_PROMPT},
             {"role": "user", "content": user}],
            model=_RESOLVER_MODEL, temperature=0.0, max_tokens=90)
        raw = (resp.text or "").strip()
        if raw.startswith("```"):
            raw = raw.strip("`").split("\n", 1)[-1].rsplit("```", 1)[0]
        s, e = raw.find("{"), raw.rfind("}")
        if s == -1 or e == -1:
            return None
        data = json.loads(raw[s:e + 1])
    except Exception as ex:  # pragma: no cover - network/parse degradation
        name = type(ex).__name__
        if any(k in name for k in ("Connection", "Timeout", "Auth", "APIError", "RateLimit")):
            _LLM_RESOLVER_DOWN = True
        print(f"[conversation_semantics] family resolver degraded ({ex!r})")
        return None
    fam = str(data.get("family") or "").strip().upper()
    if fam not in INTENT_FAMILIES:
        return None
    out = {"family": fam, "is_private": bool(data.get("is_private")),
           "product": None, "destination": None, "method": None}
    for k in ("product", "destination"):
        v = data.get(k)
        if isinstance(v, str) and 1 < len(v.strip()) <= 40:
            out[k] = v.strip()
    mth = str(data.get("method") or "").strip().lower()
    if mth in ("road", "sea", "air"):
        out["method"] = mth
    return out


# a message that is ONLY a social greeting / acknowledgement / bare
# confirmation (optionally with trailing politeness particles) — no task
# content. Anchored both ends so "ตกลงราคาได้ไหม" is NOT a match.
_GREET_CONFIRM_RE = re.compile(
    r"^(?:สวัสดี\S*|หวัดดี\S*|ดีครับ|ดีค่ะ|ดีจ้า|ขอบคุณ\S*|ขอบใจ\S*|โอเค\S*|โอเต\S*|"
    r"okay|ok|k|thx|thanks?|thank\s*you|"
    r"ยืนยัน|ตกลง|รับทราบ|เข้าใจแล้ว|เข้าใจ|ได้ครับ|ได้ค่ะ|ได้เลย)"
    r"[\s\.!,~ครับคับค่ะคะจ้าาๆนะฮะฮ่ะผมโว้ยว้อยเลย]*$", re.IGNORECASE)
_INTENT_SHAPE_RES = (_OBJ_INVOICE, _OBJ_WAREHOUSE, _OBJ_COUPON, _OBJ_TRUCK, _OBJ_COST,
                     _OBJ_PARCEL, _OBJ_ADDRESS, _ACT_LOCATE, _ACT_STATUS, _ACT_ISSUE_GET,
                     _ACT_HOWTO, _ACT_LIST_MINE, _ACT_ESTIMATE, _ACT_CHANGE, _ACT_CHARTER,
                     _PICKUP_VERB, _SHIP_VERB)


def _worth_llm_disambiguation(t: str) -> bool:
    """SEMANTIC-FIRST-2 — the gated LLM family call fires for ANY genuinely
    conversational Thai message the deterministic tier could not resolve.
    It is NOT pre-gated on a deterministic topic marker matching first —
    that made novel/short phrasings ("ร้านส่งหรือยังคะ", "ต้นทางส่งมาหรือ
    ยัง") fall straight through to no-info / Fix-2. Still excluded:
    greetings, confirmations, bare structural values, empty / single-char
    or over-long noise. Still one gated call, still degrades safely, still
    self-disables for the process on the first unreachable-LLM error."""
    s = (t or "").strip()
    if not _THAI_CHAR_RE.search(s) or not (4 <= len(s) <= 120):
        return False
    if _GREET_CONFIRM_RE.match(s):
        return False
    if _structural_kind(s) is not None:
        return False
    if _MEASURE_RE.fullmatch(s):   # a bare weight / dimensions value
        return False
    return True


# INVOICE-PRODUCT-REGRESSION-2 (Problem B) — the assistant asked the
# customer WHICH product (a legitimate product-elicitation turn). A small
# structural marker set, never a product list.
_ASSISTANT_ASKED_PRODUCT_RE = re.compile(
    r"สินค้า\S{0,4}(?:อะไร|ชนิดไหน|ประเภทไหน|แบบไหน|อะไรบ้าง)"
    r"|(?:นำเข้า|สั่ง|ฝากสั่ง|ฝากนำเข้า)\S{0,4}อะไร"
    r"|สินค้าที่(?:ต้องการ|จะ|อยาก)\S{0,20}(?:คืออะไร|อะไร)"
    r"|เป็นสินค้าอะไร|สินค้าของลูกค้าเป็นอะไร|ประเภทสินค้า\S{0,4}(?:คือ|อะไร)"
    r"|รบกวน\S{0,10}(?:ประเภท|ชนิด)สินค้า")
# a Thai question particle that would make the reply itself a question,
# not a bare answer.
_REPLY_IS_QUESTION_RE = re.compile(r"ไหม|มั้ย|หรือเปล่า|หรือไม่|ยังไง|อย่างไร|เท่าไหร่|กี่|ที่ไหน|\?")
_BARE_PRODUCT_STRIP_RE = re.compile(
    r"^(?:เป็น|คือ|ก็|น่าจะ|ประมาณ|พวก|เป็นพวก|จำพวก|ชนิด|ประเภท|สินค้า|ของ|อยากได้|ต้องการ|สั่ง|นำเข้า)\s*"
    r"|\s*(?:ครับ|ค่ะ|คะ|ค่า|นะ|น่ะ|จ้า|จ้ะ|เลย|อ่ะ|อะ|ล่ะ|หน่อย|ด้วย|ค่ะๆ|ครับๆ|ใส่ของ|ใส่ของได้)+\s*$")


def _assistant_asked_for_product(history: Optional[List[Dict]]) -> bool:
    for t in reversed(list(history or [])[-4:]):
        if t.get("role") == "assistant":
            return bool(_ASSISTANT_ASKED_PRODUCT_RE.search(t.get("content") or ""))
        if t.get("role") == "user":
            continue
    return False


_QUANTITY_REPLY_RE = re.compile(
    r"\d+\s*(?:ชิ้น|อัน|ใบ|กล่อง|ลัง|ตัว|ชุด|คู่|โหล|แพ็ค|แพ็ก|pcs?|kg|กิโล|กก|โล|ตัน|บาท|หยวน)",
    re.IGNORECASE)


def _looks_like_bare_product(t: str) -> bool:
    s = (t or "").strip()
    if not (1 <= len(s) <= 42) or not _THAI_CHAR_RE.search(s):
        return False
    if _GREET_CONFIRM_RE.match(s) or _REPLY_IS_QUESTION_RE.search(s):
        return False
    # not a structural value — weight / dimensions / bare number /
    # a quantity ("500 ชิ้น"), a price, a URL/id.
    if (_MEASURE_RE.search(s) or _QUANTITY_REPLY_RE.search(s)
            or re.fullmatch(r"[\d\s.,x×*/\-]+", s)
            or _STRUCT_URL_RE.match(s) or _STRUCT_ID_RE.match(s)):
        return False
    # too digit-heavy to be a product noun
    if sum(ch.isdigit() for ch in s) > len(s) / 3:
        return False
    # not one of the other intent families (a real request, not a noun)
    if any(rx.search(s) for rx in (_OBJ_INVOICE, _OBJ_WAREHOUSE, _OBJ_COUPON, _OBJ_TRUCK,
                                   _OBJ_COST, _ACT_CHANGE, _ACT_ESTIMATE)):
        return False
    return True


def _bare_product_noun(t: str) -> Optional[str]:
    s = (t or "").strip()
    for _ in range(3):
        s = _BARE_PRODUCT_STRIP_RE.sub("", s).strip()
    s = re.sub(r"\s+", "", s)
    return s if 2 <= len(s) <= 30 and _THAI_CHAR_RE.search(s) else None


def interpret(message: str, history: Optional[List[Dict]] = None,
              context: Optional[Dict] = None) -> Interpretation:
    """The ONE central semantic interpretation. Natural language ->
    normalised {intent_family, entities, is_private, follow_up_op}. A
    single gated LLM call only disambiguates novel / ambiguous phrasing
    and always degrades to the deterministic result."""
    raw = message or ""
    t = raw.strip()

    kind = _structural_kind(t)
    if kind is not None:
        ent: Dict[str, object] = {}
        if kind == "identifier":
            ent["identifier"] = t
        elif kind == "url":
            ent["url"] = t
        return Interpretation(intent_family="UNKNOWN", entities=ent,
                              follow_up_op="NONE", confidence=0.0, source="structural")

    op = _followup_op(t)
    fam, conf, ent = _compose(t)
    is_priv = bool(_ROLE_SELF.search(t))

    # INVOICE-PRODUCT-REGRESSION-2 (Problem B) — a bare product NAME given
    # in reply to the assistant's own "what product?" question is a
    # PRODUCT-slot response, not an UNKNOWN standalone company question.
    # Structural: the previous assistant turn asked for the product AND
    # this turn is a short noun phrase (no product dictionary). Trusted
    # policy still decides the verdict downstream — here we only name the
    # entity.
    if fam == "UNKNOWN" and _assistant_asked_for_product(history) and _looks_like_bare_product(t):
        noun = _bare_product_noun(t)
        if noun:
            return Interpretation(intent_family="PRODUCT_POLICY",
                                  entities={"product": noun}, is_private=is_priv,
                                  follow_up_op="SET_VALUE", confidence=0.7,
                                  source="deterministic")

    source = "deterministic"
    # SEMANTIC-FIRST-2 — the gated LLM family call also runs when the
    # deterministic tier could only reach GENERAL: "conversational but no
    # specific family" is exactly the case one semantic call is meant to
    # disambiguate (e.g. "ช่วยกะราคาส่งของกล่องนี้ให้หน่อยดิ" -> GENERAL
    # deterministically, SHIPPING_ESTIMATE once interpreted).
    if (fam in ("UNKNOWN", "GENERAL") or conf < 0.5) and _worth_llm_disambiguation(t):
        llm = _llm_family(t, history)
        if llm and llm["family"] != "UNKNOWN":
            fam = llm["family"]
            conf = max(conf, 0.6)
            source = "llm"
            if llm.get("is_private"):
                is_priv = True
            for k in ("product", "destination", "method"):
                if llm.get(k) and not ent.get(k):
                    ent[k] = llm[k]
        elif llm is None:
            source = "degraded"

    # a short in-frame follow-up refines the op and, when the family is
    # still unknown, attributes it to the running import-interest frame.
    frame = derive_active_frame(history)
    if frame and frame.product and op in ("CORRECTION", "COMPARISON", "TOPIC_CHANGE", "CONTINUE", "SET_VALUE"):
        ent.setdefault("product", frame.product)
        if fam == "UNKNOWN":
            fam, conf = "IMPORT_INTEREST", max(conf, 0.55)

    return Interpretation(intent_family=fam, entities=ent, is_private=is_priv,
                          follow_up_op=op, confidence=round(conf, 2), source=source)


# family -> the existing rag/query_understanding.py actionable_intent
# bucket. None => let the ordinary Business-Action / private-state
# routing own the turn (never force a RAG intent).
FAMILY_TO_ACTIONABLE_INTENT = {
    "SHIPMENT_STATUS": "tracking_status",
    "INVOICE": "invoice_policy",
    "PICKUP_LOCATION": "warehouse_location",
    "SELF_PICKUP": "self_pickup_permission",
    "COUPON_USAGE": "coupon_policy",
    "MY_COUPONS": None,
    "PRODUCT_POLICY": "prohibited_goods",
    "CHARTER_TRUCK": "service_information",
    "SHIPPING_ESTIMATE": "shipping_calculation",
    "ADDRESS_CHANGE": None,
    "IMPORT_INTEREST": None,
    "GENERAL": None,
    "UNKNOWN": None,
}
