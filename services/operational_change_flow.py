# -*- coding: utf-8 -*-
"""CUSTOMER-ACTION-1 — operational change / verify requests with NO
approved executable Business Action.

Cases S02 / S03 / S11 / S13 / S15 / G18 (from the customer UAT master).
Each is a request to CHANGE or VERIFY something on the customer's own
record for which there is NO ERP write action — the customer-approved
behaviour is uniform:

    understood intent
      -> acknowledge + ask for the ONE required identifier / input
      -> hand to Human CS (a staff member performs the change)

Never a "no information" dead-end, never a fake "ดำเนินการเรียบร้อยค่ะ",
never an unrelated ERP read. Deterministic + history-derived — the SAME
"assistant reply IS the state" pattern services/charter_truck_flow.py
uses. No LLM, no pending table. Semantic-First (services/
conversation_semantics.py) supplies the natural-language intent; this
module only decides the deterministic workflow.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, asdict, field
from typing import Dict, List, Optional

from services.slot_filling_engine import _validate_generic_identifier as _valid_id

# ── request-kind recognisers — a small COMPOSITIONAL verb + object set,
#    never a phrase dictionary. A kind fires when its verb marker AND its
#    object marker both appear in the message (order-independent — a bill
#    number may sit between them), except the two kinds (duplicate_bill /
#    topup_not_credited) that are self-describing on their own. Each kind
#    carries its own ack (part-1 of the CS-approved answer) + input label.
_CHANGE_VERB_RE = re.compile(r"เปลี่ยน|แก้ไข|แก้|ปรับ|ขอเปลี่ยน|ขอแก้|ต้องการเปลี่ยน|ต้องการแก้|อยากเปลี่ยน|อยากแก้|ลืมเลือก|ต้องการ\s*vat|อยากได้\s*vat", re.IGNORECASE)

_KINDS = [
    # kind, verb_re (or None), object_re, ack, input_label
    # CUSTOMER-CSW2-QUANTITY-CHANGE-1 — "จำนวนสินค้า" alone (no "บิล"
    # word) still counts as the object: a real-line-style paraphrase
    # ("แก้จำนวนสินค้าได้ไหม") never names the bill explicitly, only the
    # change verb + "quantity of the product". Safe to widen — no other
    # kind's object pattern mentions "จำนวน", so this can't collide with
    # a sibling kind, and the CHANGE VERB is still required.
    ("modify_bill_qty", _CHANGE_VERB_RE,
     re.compile(r"จำนวน(?:สินค้า)?(?:ในบิล|บิล|ที่สั่ง)|จำนวนในบิล|จำนวนสินค้า"),
     "แอดมินขอเลขบิลสั่งซื้อของรายการนี้หน่อยนะคะ", "เลขบิลสั่งซื้อ"),
    ("change_shipping_method", _CHANGE_VERB_RE,
     re.compile(r"(?:จัดส่ง|ส่ง|ขนส่ง)[^\n]{0,6}(?:ทางรถ|ทางเรือ)|เป็นทาง(?:รถ|เรือ)|วิธี(?:ส่ง|จัดส่ง|ขนส่ง)|ทาง(?:รถ|เรือ)[^\n]{0,6}ได้ไหม"),
     "สามารถเปลี่ยนได้ค่ะ แอดมินรบกวนขอเลขบิลหน่อยนะคะ", "เลขบิล"),
    ("change_carrier_or_selfpickup", _CHANGE_VERB_RE,
     re.compile(r"เป็นรับเอง|มารับเอง|รับสินค้าเอง|ส่งเอกชน|เป็นเอกชน|ขนส่งเอกชน|ส่ง\s*flash|เป็น\s*flash", re.IGNORECASE),
     "แอดมินรบกวนขอเลขบิลขนส่งหน่อยนะคะ", "เลขบิลขนส่ง"),
    ("add_vat", _CHANGE_VERB_RE,
     re.compile(r"\bvat\b|ภาษีมูลค่าเพิ่ม|ใบกำกับภาษี", re.IGNORECASE),
     "คุณลูกค้าแจ้งเลขบิลสั่งซื้อที่ต้องการ VAT มาให้แอดมินได้เลยนะคะ", "เลขบิลสั่งซื้อ"),
    ("duplicate_bill", None,
     re.compile(r"บิลซ้ำ|บิลซ้ำกัน|มีบิลซ้ำ|บิลออกมาซ้ำ|บิลตีซ้ำ|บิลเบิ้ล"),
     "แอดมินเช็คบิลซ้ำและลบบิลให้นะคะ รบกวนขอเลขแทรคจีนหน่อยนะคะ", "เลขแทรคจีน"),
    ("verify_warehouse_address",
     re.compile(r"ถูกไหม|ถูกมั้ย|ถูกต้อง|ถูกรึเปล่า|ถูกหรือเปล่า|เช็ค|ตรวจสอบ|\bcheck\b", re.IGNORECASE),
     re.compile(r"(?:ที่อยู่)?โกดังจีน"),
     "แอดมินช่วยตรวจสอบความถูกต้องให้ค่ะ รบกวนแจ้งที่อยู่โกดังจีนที่กรอกไว้ในระบบและรหัสลูกค้ามาให้ตรวจสอบด้วยนะคะ",
     "ที่อยู่โกดังจีนที่กรอกไว้ และรหัสลูกค้า"),
    ("topup_not_credited", None,
     re.compile(r"ยอดเงินไม่เข้า|ยอดไม่เข้า|เงิน(?:ที่เติม)?[^\n]{0,10}(?:ยัง)?ไม่เข้า"
                r"|เติมเงินแล้ว[^\n]{0,16}(?:ไม่เข้า|ยังไม่เข้า|รอตรวจสอบ|ยอดยังไม่ขึ้น)"),
     "สวัสดีค่ะ แอดมินรบกวนขอสลิปการโอนเงินหน่อยนะคะ", "สลิปการโอนเงิน"),
    # CUSTOMER-RED-8 — CUS-G11 (Ai.xlsx sheet '1.thameuangton' row 11):
    # missing/incomplete item claim. Self-describing (no separate verb
    # needed, same shape as duplicate_bill/topup_not_credited above).
    ("missing_item_claim", None,
     re.compile(r"ได้รับสินค้าไม่ครบ|สินค้าไม่ครบ|ของไม่ครบ|เคลมสินค้า|ขอเคลม|สินค้าเสียหาย|ของเสียหาย|พัสดุเสียหาย"),
     "สวัสดีค่ะ คุณลูกค้าแจ้งเลขบิลสั่งซื้อ และรูปหน้าแทรคจีนที่ติดข้างกล่อง กับวิดิโอตอนแกะสินค้า "
     "รวมทั้งรูปสินค้าทั้งหมดที่ได้รับมาให้แอดมินได้เลยนะคะ",
     "เลขบิลสั่งซื้อ, รูปหน้าแทรคจีนที่ติดข้างกล่อง, วิดิโอตอนแกะสินค้า, รูปสินค้าทั้งหมด"),
    # CUSTOMER-RED-8 — CUS-S06 (Ai.xlsx sheet '2.tongchecknairabop' row 6
    # / CSW6): custom production / screen-print / order-to-spec.
    ("custom_production", None,
     re.compile(r"สั่งผลิตตามสเปค|สั่งสกรีนโลโก้|สกรีนโลโก้|สั่งผลิต(?:สินค้า)?ตามสเปค|ผลิตตามสเปค|สั่งทำโลโก้"),
     "คุณลูกค้าแจ้งเลขบิลสั่งซื้อ และแจ้งสเปคสินค้า จำนวน สีกับโลโก้มาให้แอดได้เลยค่ะ แอดจะประสานงานกับทางร้านให้นะคะ",
     "เลขบิลสั่งซื้อ, สเปคสินค้า, จำนวน, สี, โลโก้"),
    # CUSTOMER-RED-8 / CUSTOMER-CHARTER-COMBINE-REAL-1 — CUS-S16
    # (Ai.xlsx sheet '2.tongchecknairabop' row 16 / CSW16): combine
    # multiple bills into one charter-truck shipment. Distinct from a
    # FRESH single-shipment charter request (services/charter_truck_
    # flow.py, opened only after its own TC19 FAQ turn) — this is its
    # own ack + collect + Human-CS coordination shape, reusing the SAME
    # established pattern as every other kind here rather than a
    # bespoke multi-bill collector.
    #
    # REAL LINE (2026-09-05 ~17:13 ICT): the ORIGINAL ack ("รับทราบค่ะ
    # แอดมินรวมบิลที่เข้าไทยเหมารถให้นะคะ") sounds like the combine is
    # ALREADY under way and never actually asks for anything — so a
    # customer who then supplied bill numbers just got the exact SAME
    # sentence echoed back (operational_ask_prompt always returns
    # state.ack, whether this is the first ask or a still-missing
    # retry), reading as if nothing had been collected. Fixed: the ack
    # now explicitly asks for the bill numbers, matching stage A of the
    # 3-stage journey (A. request -> ask bills, B. bills collected ->
    # forward to Human CS, C. real confirmed result only -> completion
    # text) — never implying the combine itself has started.
    ("combine_bills_charter", None,
     re.compile(r"รวมบิล.{0,6}เหมารถ|เหมารถ.{0,6}รวมบิล|รวมบิลขนส่งเหมารถ"),
     "ได้ค่ะ รบกวนแจ้งเลขบิลขนส่งที่ต้องการรวมเหมารถมาได้เลยค่ะ หากมีหลายบิลสามารถส่งมาพร้อมกันได้เลยนะคะ",
     "เลขบิลขนส่งที่ต้องการรวม"),
]

# a delivery-ADDRESS change is a different case (CUS-S09) with its own
# approved Business Action (requestshippingaddresschange) — this flow
# must never steal it.
_ADDRESS_CHANGE_RE = re.compile(r"ที่อยู่จัดส่ง|ที่อยู่ผู้รับ|ที่อยู่ในการจัดส่ง|เปลี่ยนที่อยู่(?!โกดัง)")

_ACK_MARKER_RE = re.compile(
    r"แอดมินขอเลขบิลสั่งซื้อของรายการนี้|แอดมินรบกวนขอเลขบิล|แอดมินเช็คบิลซ้ำและลบบิลให้"
    r"|แอดมินช่วยตรวจสอบความถูกต้องให้|แอดมินรบกวนขอสลิปการโอนเงิน"
    r"|คุณลูกค้าแจ้งเลขบิลสั่งซื้อที่ต้องการ\s*VAT"
    r"|คุณลูกค้าแจ้งเลขบิลสั่งซื้อ และรูปหน้าแทรคจีน"
    r"|คุณลูกค้าแจ้งเลขบิลสั่งซื้อ และแจ้งสเปคสินค้า"
    r"|รบกวนแจ้งเลขบิลขนส่งที่ต้องการรวมเหมารถ")
_DONE_MARKER_RE = re.compile(
    r"รับเรื่องคำขอดำเนินการเรียบร้อยค่ะ|เจ้าหน้าที่จะติดต่อดำเนินการให้"
    r"|รับข้อมูลบิลที่ต้องการรวมแล้วค่ะ")

_FRAME_LOOKBACK = 10

_BILL_TOKEN_RE = re.compile(r"\b([A-Za-z]{2,4}\d{4,})\b")
_PHONE_RE = re.compile(r"(?<!\d)(0\d[\d\- ]{7,10}\d)(?!\d)")


def classify_operational_request(message: str, interpretation: Optional[object] = None) -> Optional[Dict]:
    """Return {kind, ack, input_label} for an operational change/verify
    request with no executable action, else None. A kind with a verb
    marker needs BOTH its verb and its object to appear (order-
    independent); the self-describing kinds need only their object.
    Excludes the delivery-address change (CUS-S09 — has its own
    Business Action)."""
    t = message or ""
    if _ADDRESS_CHANGE_RE.search(t):
        return None
    for kind, verb_re, obj_re, ack, label in _KINDS:
        if not obj_re.search(t):
            continue
        if verb_re is not None and not verb_re.search(t):
            continue
        return {"kind": kind, "ack": ack, "input_label": label}
    return None


@dataclass
class OperationalState:
    kind: str = ""
    ack: str = ""
    input_label: str = ""
    bill: Optional[str] = None          # bill / tracking token, when supplied
    # CUSTOMER-CHARTER-COMBINE-1.1 — combine_bills_charter's own
    # required input is a LIST of shipment bills (CUS-S16's
    # api_input_hint: "shipment_bill_no_list"), unlike every other kind
    # here which needs only ONE. Kept as its own field rather than
    # overloading `bill` so every existing single-bill kind is
    # completely unaffected.
    bills: List[str] = field(default_factory=list)
    free_text: Optional[str] = None     # address / other free-form detail
    phone: Optional[str] = None

    def as_dict(self) -> Dict:
        return {k: v for k, v in asdict(self).items() if v}

    def has_input(self) -> bool:
        return bool(self.bill or self.bills or self.free_text or self.phone)


_CN_TRACKING_RE = re.compile(r"(?<!\d)(\d{9,16})(?!\d)")


def extract_operational_fields(message: str, into: OperationalState) -> OperationalState:
    t = message or ""
    if into.kind == "combine_bills_charter":
        # multiple bills may arrive in one message ("FT001 FT002") or
        # accumulate across turns ("FT001" then later "FT002 ด้วยค่ะ") —
        # every genuinely NEW bill token is appended, never replacing an
        # earlier one, so nothing already supplied is lost.
        for tok in _BILL_TOKEN_RE.findall(t):
            if _valid_id(tok) and not tok.isdigit():
                tok_u = tok.upper()
                if tok_u not in into.bills:
                    into.bills.append(tok_u)
    elif not into.bill:
        m = _BILL_TOKEN_RE.search(t)
        if m and _valid_id(m.group(1)) and not m.group(1).isdigit():
            into.bill = m.group(1).upper()
        elif into.kind == "duplicate_bill":
            # a CN tracking is typically an all-digit run
            mt = _CN_TRACKING_RE.search(t)
            if mt:
                into.bill = mt.group(1)
    if not into.phone:
        m = _PHONE_RE.search(t)
        if m:
            into.phone = re.sub(r"[\s\-]", "", m.group(1))
    # for the verify-address / slip cases a free-text detail counts as the
    # required input once the customer replies with something substantive.
    if into.kind in ("verify_warehouse_address", "topup_not_credited") and not into.free_text:
        stripped = t.strip()
        if len(stripped) >= 6 and not classify_operational_request(stripped):
            into.free_text = stripped[:400]
    return into


def derive_operational_state(history: Optional[List[Dict]], current_message: str,
                             interpretation: Optional[object] = None) -> Optional[OperationalState]:
    """Reconstruct an active operational-change collection, or open a new
    one from `current_message`. Returns None when nothing operational is
    in play (or the request was already handed off)."""
    turns = list(history or [])[-_FRAME_LOOKBACK:]

    # 1/2. whether an episode is open, already closed, or neither is
    # decided SOLELY by the MOST RECENT assistant turn — never by
    # scanning the whole lookback window for a done-marker anywhere in
    # it. REAL LINE regression (CUSTOMER-CSW2-REAL-1): a done-marker
    # from an EARLIER, unrelated, already-closed episode (e.g. a
    # combine_bills_charter handoff) sitting anywhere within the last
    # _FRAME_LOOKBACK turns silently blocked EVERY subsequent
    # operational request — including a brand-new, unrelated one
    # ("ต้องการแก้จำนวนสินค้าในบิล") several turns later — because the
    # old step 1 returned None unconditionally the instant it found
    # ANY done-marker in the window, before step 2 ever got a chance to
    # see that the actual MOST RECENT assistant turn was neither an ack
    # nor a done-marker at all (an unrelated RAG/Coupon reply). Folded
    # into one scan of only the single most recent assistant turn: a
    # done-marker there means THIS episode just closed (nothing to
    # reopen); an ack marker there means THIS episode is still open
    # (recovers its kind, as before); anything else means neither, and
    # a fresh request is free to open (step 3).
    open_kind = None
    for t in reversed(turns):
        if t.get("role") != "assistant":
            continue
        c = t.get("content") or ""
        if _DONE_MARKER_RE.search(c):
            return None
        if _ACK_MARKER_RE.search(c):
            # recover which kind from the ack wording. Match on the FULL
            # ack text, never a truncated prefix: several kinds share an
            # identical opening clause ("คุณลูกค้าแจ้งเลขบิลสั่งซื้อ...") —
            # add_vat / missing_item_claim / custom_production all start
            # this way. A truncated N-char prefix check (`ack[:24] in c`)
            # matches whichever kind is EARLIEST in this list purely
            # because its short prefix happens to appear inside a LATER
            # kind's longer, different ack (real production bug: a
            # missing_item_claim ack was recovered as add_vat, then every
            # unrelated follow-up intent — Purchase Withdrawal, Shipping
            # Withdrawal, Custom Production, Charter Combine — was
            # answered with the wrong, stale "VAT" ack). The FULL ack
            # strings are themselves mutually non-overlapping, so this
            # substring check is unambiguous.
            for kind, _vrx, _orx, ack, label in _KINDS:
                if ack in c:
                    open_kind = (kind, ack, label)
                    break
        break

    if open_kind:
        kind, _ack_txt, label = open_kind
        st = OperationalState(kind=kind, ack=_ack_txt, input_label=label)
        # accumulate every user turn since the ack
        seen_ack = False
        for t in turns:
            if t.get("role") == "assistant" and _ACK_MARKER_RE.search(t.get("content") or ""):
                seen_ack = True
                continue
            if seen_ack and t.get("role") == "user":
                extract_operational_fields(t.get("content") or "", st)
        return st

    # 3. a fresh request in the current message.
    cls = classify_operational_request(current_message, interpretation)
    if not cls:
        return None
    st = OperationalState(kind=cls["kind"], ack=cls["ack"], input_label=cls["input_label"])
    extract_operational_fields(current_message, st)
    return st


_KIND_TH = {
    "modify_bill_qty": "ขอแก้จำนวนสินค้าในบิล",
    "change_shipping_method": "ขอเปลี่ยนวิธีจัดส่ง (ทางรถ/ทางเรือ)",
    "change_carrier_or_selfpickup": "ขอเปลี่ยนเป็นรับเอง / เปลี่ยนขนส่งเอกชน",
    "add_vat": "ขอเพิ่ม VAT / ใบกำกับภาษี ในบิลสั่งซื้อ",
    "duplicate_bill": "แจ้งบิลซ้ำ ขอให้ตรวจสอบและลบบิลซ้ำ",
    "verify_warehouse_address": "ขอให้ตรวจสอบความถูกต้องของที่อยู่โกดังจีนที่กรอกไว้",
    "topup_not_credited": "แจ้งเติมเงินแล้วยอดยังไม่เข้า ขอให้ตรวจสอบ",
    "missing_item_claim": "แจ้งได้รับสินค้าไม่ครบ/เสียหาย ขอเคลม",
    "custom_production": "ขอสั่งผลิตสินค้าตามสเปค / สกรีนโลโก้",
    "combine_bills_charter": "ขอรวมบิลขนส่งที่เข้าไทยเป็นเหมารถ",
}


def operational_ask_prompt(state: OperationalState) -> str:
    return state.ack


def operational_handoff_summary(state: OperationalState) -> str:
    parts = [_KIND_TH.get(state.kind, state.kind)]
    if state.bills:
        parts.append("เลขบิลขนส่งที่ต้องการรวม: " + ", ".join(state.bills))
    if state.bill:
        parts.append(f"เลขบิل/แทรค: {state.bill}")
    if state.phone:
        parts.append(f"เบอร์: {state.phone}")
    if state.free_text:
        parts.append(f"รายละเอียด: {state.free_text[:200]}")
    return "คำขอดำเนินการกับบัญชีลูกค้า — " + " | ".join(parts)


# the neutral reply once the input is collected — carries NO staff
# promise; line_bot/webhook.py appends the coordination line
# (" เดี๋ยวเจ้าหน้าที่จะติดต่อดำเนินการให้นะคะ") only when a real Human CS
# notification for THIS episode actually goes out (or is already NOTIFIED).
OPERATIONAL_HANDOFF_REPLY = "รับเรื่องคำขอดำเนินการเรียบร้อยค่ะ"

# CUSTOMER-CHARTER-COMBINE-1.1 / -REAL-1 -- CUS-S16's own source (Ai.xlsx
# sheet '2.tongchecknairabop' row 16 / CSW16) marks this a Human-CS-only
# operation (H19="Human CS": staff physically combine the bills and
# adjust their charter/เหมารถ status in the external warehouse system --
# there is no API this platform can call to do it, and no way for this
# platform to learn the result automatically either). The customer-
# approved THIRD-stage answer ("รวมบิลเหมารถเรียบร้อยค่ะ, เป็นบิล ...")
# is only valid once that real, staff-confirmed result exists -- it is
# NEVER used here, since collecting the bill list is not that
# confirmation. This is stage B of the 3-stage journey (A. ask bills,
# B. bills collected -> forward to Human CS/logistics -- THIS reply,
# C. real confirmed result only -> completion text): it honestly says
# the BILL LIST (not the combine itself) was received and forwarded.
# Every other kind keeps the generic OPERATIONAL_HANDOFF_REPLY, unchanged.
_KIND_HANDOFF_REPLY = {
    "combine_bills_charter": (
        "รับข้อมูลบิลที่ต้องการรวมแล้วค่ะ เดี๋ยวแอดมิน/ทีมขนส่งตรวจสอบและดำเนินการรวมบิลเหมารถให้นะคะ"),
}


def operational_handoff_reply(state: OperationalState) -> str:
    return _KIND_HANDOFF_REPLY.get(state.kind, OPERATIONAL_HANDOFF_REPLY)
