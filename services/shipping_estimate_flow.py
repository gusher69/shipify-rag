# -*- coding: utf-8 -*-
"""CUSTOMER-CALC-1 — shipping-cost estimate as a MULTI-TURN slot
collection.

"ค่านำเข้าเท่าไหร่ สินค้า1ชิ้น น้ำหนัก 2กิโล ขนาด 54*12*43" is a
shipping-cost estimate request with a missing slot (ROAD/SEA), NOT a
"no information" case. This flow: recognise the calculator intent ->
collect only the missing inputs -> remember supplied inputs across turns
-> calculate the moment there is enough data (weight + 3 dims + method).

Deterministic, history-derived — same pattern as
services/charter_truck_flow.py / services/conversation_semantics.py. No
LLM, no pending table. Reuses rag/slot_filling_flow.py's dimension/weight
parser (which already handles *, x, ×, bare "54 12 43", "2กิโล", "2 กก.",
"2 kg").
"""
from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional

from rag.slot_filling_flow import parse_dimension_input, _merge_captured, _dimension_unit

# ── trusted rate policy ───────────────────────────────────────────────
# Source of truth: the Production "เรทนำเข้า" FAQ — verified verbatim
# ("ทางรถ: 35 บาท/กิโลกรัม หรือ 6,900 บาท/CBM / ทางเรือ: 19 บาท/กิโลกรัม
# หรือ 4,500 บาท/CBM"). There is no configurable rate table to reuse;
# these 4 constants live here once and are asserted against the FAQ text
# by tests/test_customer_calc1.py so a rate change to the KB is caught.
RATES = {
    "road": {"kg": 35.0, "cbm": 6900.0},
    "sea": {"kg": 19.0, "cbm": 4500.0},
}
_METHOD_TH = {"road": "ทางรถ", "sea": "ทางเรือ"}

# CUSTOMER-CALC-MULTITURN-1 — the customer explicitly wants BOTH methods
# compared in one answer ("ของทั้งรถและเรือ", "รถกับเรือ", ...). Checked
# BEFORE the single-method patterns in _method_of since some of these
# phrases contain "รถ"/"เรือ" as substrings that would otherwise match
# the single-method branch first.
_BOTH_METHOD_RE = re.compile(
    r"ทั้งรถและเรือ|ทั้งเรือและรถ|ทั้งรถทั้งเรือ|ทั้งเรือทั้งรถ|"
    r"รถกับเรือ|เรือกับรถ|"
    r"ทั้งสองแบบ|ทั้งสองทาง|ทั้งสองวิธี|"
    r"ทางรถและทางเรือ|ทางเรือและทางรถ")

# ── intent / signal patterns ─────────────────────────────────────────
# a calculator VERB — the customer explicitly asks for a calculation.
_CALC_VERB_RE = re.compile(
    r"ช่วยคำนวณ|คำนวณให้|คำนวณค่า|คำนวนค่า|ประเมินค่า|ประเมินราคา|ขอประเมิน|ประเมินเบื้องต้น|"
    r"คิดค่า(?:ขนส่ง|ส่ง|นำเข้า)|คิดราคาค่า(?:ขนส่ง|ส่ง|สินค้า|นำเข้า)|ช่วยคิด[^\n]{0,12}(?:ราคา|ค่า)")
# a calculator TOPIC — only opens the flow together with a concrete value.
_CALC_TOPIC_RE = re.compile(r"ค่านำเข้า|ค่าขนส่ง|ค่าส่ง|เรทนำเข้า|ค่าจัดส่ง")
# this flow's own missing-slot / route question (episode is COLLECTING).
_EST_PROMPT_RE = re.compile(r"ต้องการประเมินทางรถหรือทางเรือ|"
                            r"รบกวนแจ้ง(?:น้ำหนัก|ขนาด)[^\n]{0,40}(?:เพื่อประเมิน|สำหรับประเมิน)|"
                            r"ยังขาดข้อมูลสำหรับประเมิน")
# this flow's own delivered ESTIMATE (episode is COMPLETE/CLOSED).
# CUSTOMER-CALC-MULTITURN-1 — also matches the BOTH-methods result
# opening phrase (estimate_reply's "both" branch), so the episode still
# reads as OPEN (flow_open) for a follow-up correction/comparison after
# a combined road+sea answer, exactly like a single-method result does.
_EST_RESULT_RE = re.compile(r"ประเมินเบื้องต้นสำหรับ[^\n]{0,20}ประมาณ|ประเมินเบื้องต้นให้ทั้งสองทาง")
_METHOD_RE = re.compile(r"ทางรถ|ทางบก|ทางเรือ|โดยรถ|โดยเรือ|(?<![ก-๙])รถ(?![ก-๙])|(?<![ก-๙])เรือ(?![ก-๙])")
# a SHORT natural route answer while a flow is active ("รถ", "รถครับ",
# "ทางรถค่ะ", "เอารถ", "ขอทางเรือ") — a structural token parser, not a
# sentence list. Only consulted inside an active calculator context.
_ROUTE_ANSWER_RE = re.compile(
    r"^\s*(?:ส่ง|เอา|ขอ|ใช้|เป็น)?\s*(?:ทาง|โดย)?\s*(รถ|บก|เรือ|เครื่องบิน|อากาศ)\s*"
    r"(?:ได้(?:ไหม|มั้ย|ปะ|ป่ะ|หรอ|เหรอ)?|ครับ|ค่ะ|คะ|นะ|น่ะ|จ้า|จ๊ะ|คับ|ครัช|เลย|ก็ได้|ดีกว่า)*\s*$")
# a CLEAR correction / comparison / anaphoric follow-up of the just-
# completed estimate — reuse existing SEM-GEN-style correction wording.
_CORRECTION_RE = re.compile(
    r"ไม่ใช่[^\n]{0,16}(?:เป็น|เอา)\s*\S"
    r"|เปลี่ยน(?:เป็น|ไปเป็น|เป็นน้ำหนัก)?\s*\d|แก้(?:เป็น|ให้เป็น)\s*\d"
    r"|(?:ถ้า(?:เป็น)?|แล้ว|ลอง|เอา|ขอ|งั้น)\s*(?:ทาง|โดย)?\s*(?:รถ|เรือ|บก)"
    r"[^\n]{0,8}(?:ล่ะ|แทน|บ้าง|ดู|มั้ย|ไหม|ครับ|ค่ะ|ดีกว่า)?\s*$")
# CUSTOMER-CALC-MULTITURN-1 — a pure UNIT correction with no new numbers
# ("เมื่อกี้หน่วยผิด แก้เป็น cm ค่ะ") — recognized only together with a
# unit token actually present (_dimension_unit), so this never fires on
# an unrelated "ผิด"/"แก้" mention.
_UNIT_CORRECTION_RE = re.compile(r"หน่วยผิด|ผิดหน่วย|หน่วยเป็น|แก้หน่วย|หน่วยแก้เป็น|เปลี่ยนหน่วย")
# a bare "please recalculate" request with no new values of its own —
# recomputes the CURRENT merged state, never resets it (see _NEW_EPISODE_RE
# below for the genuinely-different-package signal).
_RECALC_RE = re.compile(r"คำนวณใหม่|คำนวณอีกครั้ง|ลองคำนวณอีกที|คิดใหม่|คำนวณให้หน่อย")
# an explicit NEW calculator episode — a genuinely DIFFERENT package, not
# a correction/recalculation of the current one. Deliberately excludes
# bare "คำนวณใหม่" (see _RECALC_RE) — that phrase alone just asks to
# redo the math with whatever is currently known, and any new values in
# the SAME message already replace the relevant slot via the normal
# merge/correction mechanism, never requiring a full wipe.
_NEW_EPISODE_RE = re.compile(r"เริ่มใหม่|อีกกล่อง(?:นึง|หนึ่ง)?|อีกชิ้น|อีกอัน|กล่องใหม่|เคสใหม่|พัสดุใหม่")
_QUANTITY_RE = re.compile(r"(\d+)\s*(?:ชิ้น|กล่อง|อัน|ใบ|ลัง|pcs?|box(?:es)?)", re.IGNORECASE)
# quantity / count phrases that must NOT be read as dimension numbers.
_COUNT_PHRASE_RE = re.compile(
    r"\d+\s*(?:ชิ้น|กล่อง|อัน|ใบ|ลัง|pcs?|box(?:es)?)|สินค้า\s*\d+|จำนวน\s*\d+", re.IGNORECASE)
# weight — extends rag/slot_filling_flow's own pattern with bare "โล".
_WEIGHT_RE = re.compile(r"(\d+\.?\d*)\s*(?:กิโลกรัม|กิโล|กก\.?|kg|โล)", re.IGNORECASE)
# CORE-CONVERSATION — weight given in GRAMS ("5000 กรัม", "800g") is
# converted to kg (÷1000). Checked only after the kg-family pattern
# above, so "5 kg" is never mis-read here.
_WEIGHT_G_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:กรัม|grams?|(?<![กa-z])g(?![a-z]))", re.IGNORECASE)
_PER_BOX_RE = re.compile(r"กล่องละ|ต่อกล่อง|ชิ้นละ|อันละ|per\s*box", re.IGNORECASE)
_TOTAL_WEIGHT_RE = re.compile(r"น้ำหนักรวม|รวมน้ำหนัก|น้ำหนักทั้งหมด|total\s*weight", re.IGNORECASE)
# a hard non-calculator subject in the immediately-preceding user turn
# expires a stale flow.
_FLOW_EXIT_RE = re.compile(
    r"คูปอง|วอลเล็ท|wallet|ยอดเงิน|ติดตาม|แทรค|เลขพัสดุ|สถานะ|ที่อยู่โกดัง|เบอร์ติดต่อ|"
    r"ใบกำกับ|เหมารถ|สมัคร|ยกเลิก|คืนเงิน|ร้องเรียน", re.IGNORECASE)
# a clear OTHER business intent in the SAME message — the estimate flow
# must never steal a turn that is really an address change / cancel /
# claim / invoice / tracking / etc. (a "…และช่วยประเมินค่าขนส่ง…" second
# clause is handled by the normal multi-intent path, not here).
_OTHER_BUSINESS_INTENT_RE = re.compile(
    r"เปลี่ยนที่อยู่|แก้ที่อยู่|เปลี่ยน[^\n]{0,6}จัดส่ง|ย้ายที่อยู่|เปลี่ยน[^\n]{0,4}(?:ขนส่ง|เป็นรับเอง|เป็นเอกชน)|"
    r"ยกเลิก|เคลม|รวมบิล|ถอนเงิน|คืนเงิน|แก้จำนวน|เพิ่ม\s*vat|ใส่\s*vat|รีแพ็ค|รีเเพ็ค|ตีลัง|"
    r"ติดตาม[^\n]{0,4}(?:พัสดุ|สินค้า|สถานะ)|เช็ก[^\n]{0,4}พัสดุ|ออกใบกำกับ|โหลดใบกำกับ", re.IGNORECASE)

_LOOKBACK = 10


@dataclass
class EstimateState:
    weight: Optional[float] = None
    length: Optional[float] = None
    width: Optional[float] = None
    height: Optional[float] = None
    dim_unit: Optional[str] = None
    method: Optional[str] = None        # "road" | "sea"
    quantity: Optional[int] = None
    per_box_dims: bool = False
    total_weight_given: bool = False

    def as_dict(self) -> Dict:
        return {k: v for k, v in asdict(self).items() if v not in (None, False)}

    def _dims_present(self) -> int:
        return sum(1 for v in (self.length, self.width, self.height) if v is not None)

    def missing(self) -> List[str]:
        m = []
        if self.weight is None:
            m.append("weight")
        if self._dims_present() < 3:
            m.append("dimensions")
        if self.method is None:
            m.append("method")
        return m

    def known_any(self) -> bool:
        """Has the customer supplied ANY estimate slot yet? (PHASE 6 —
        distinguishes a cold opener from a collection turn in progress.)"""
        return bool(self.weight is not None or self._dims_present() or self.method)

    def complete(self) -> bool:
        return not self.missing()


def _method_of(text: str) -> Optional[str]:
    """Shipping method from a message. Trims trailing politeness
    particles so "รถครับ" / "เรือค่ะ" normalize, and accepts a SHORT
    natural route answer ("รถ", "เอาเรือ", "ขอทางรถ"). Returns "both"
    when the customer explicitly asks for both methods compared
    together (CUSTOMER-CALC-MULTITURN-1) — checked first since some of
    those phrases contain "รถ"/"เรือ" as substrings."""
    if not text:
        return None
    if _BOTH_METHOD_RE.search(text):
        return "both"
    t = re.sub(
        r"[\s]*(?:ครับ|ค่ะ|คะ|นะ|น่ะ|จ้า|จ๊ะ|คับ|ครัช|เลย|ก็ได้|ดีกว่า|"
        r"ล่ะ|หละ|ล้ะ|บ้าง|แทน|ดู|มั้ย|ไหม|หรือเปล่า|รึเปล่า)+\s*$",
        "", (text or "").strip())
    ra = _ROUTE_ANSWER_RE.match(t)
    if ra:
        w = ra.group(1)
        return "sea" if w == "เรือ" else ("air" if w in ("เครื่องบิน", "อากาศ") else "road")
    if re.search(r"ทางเรือ|โดยเรือ|(?<![ก-๙])เรือ(?![ก-๙])", t):
        return "sea"
    if re.search(r"ทางรถ|ทางบก|โดยรถ|(?<![ก-๙])รถ(?![ก-๙])", t):
        return "road"
    # CALCULATOR-REGRESSION-2 — a COMPARISON follow-up glues the route
    # word straight onto a connector ("ถ้าเป็นเรือล่ะ", "แล้วรถล่ะ",
    # "งั้นเอาเรือ") so the strict word-boundary checks above miss it.
    # Only a leading comparison/choice connector immediately before the
    # bare route noun counts — "เรือสินค้ามาถึงยัง" / "นำเข้ารถ" have no
    # such connector and stay unmatched (their own regression test).
    cm = re.search(r"(?:ถ้า|เป็น|แล้ว|ลอง|งั้น|เอา|ขอ|ใช้)(?:ทาง|โดย|เป็น)?\s*(รถ|เรือ|บก)(?![ก-๙])", t)
    if cm:
        return "sea" if cm.group(1) == "เรือ" else "road"
    return None


def _has_calc_signal(text: str) -> bool:
    p = parse_dimension_input(text or "")
    return bool(_CALC_VERB_RE.search(text or "") or _CALC_TOPIC_RE.search(text or "")
                or _method_of(text or "") or p["weight"] is not None or len(p["dimension_values"]) >= 2
                or _WEIGHT_G_RE.search(text or "") is not None)


def extract_estimate_fields(message: str, into: Optional[EstimateState] = None) -> EstimateState:
    """Merge whatever estimate inputs `message` supplies into `into`
    (current wording wins). Field order is irrelevant."""
    st = into or EstimateState()
    t = message or ""

    if _PER_BOX_RE.search(t):
        st.per_box_dims = True
    if _TOTAL_WEIGHT_RE.search(t):
        st.total_weight_given = True

    q = _QUANTITY_RE.search(t)
    if q:
        try:
            st.quantity = int(q.group(1))
        except ValueError:
            pass

    m = _method_of(t)
    if m:
        st.method = m

    # carve out weight (incl. bare "โล") and count phrases BEFORE the
    # dimension parser, so "สินค้า1ชิ้น" / "3 กล่อง" / "2 โล" never leak
    # into the dimension numbers.
    clean = t
    weight_here = None
    # Collect every weight mention — kg-family AND grams — with its
    # position and kg value. "ไม่ใช่ 2 กิโล เป็น 3 กิโล" / "ไม่ใช่ 10 กิโล
    # เอา 5500 กรัม" state two values; the LAST-STATED one wins (SEM-GEN
    # correction semantics), whatever unit it is in.
    wcands = [(m.start(), m.end(), float(m.group(1))) for m in _WEIGHT_RE.finditer(clean)]
    wcands += [(m.start(), m.end(), float(m.group(1)) / 1000.0)
               for m in _WEIGHT_G_RE.finditer(clean)]
    if wcands:
        wcands.sort(key=lambda c: c[0])
        weight_here = wcands[-1][2]
        for s, e, _v in sorted(wcands, key=lambda c: c[0], reverse=True):
            clean = clean[:s] + " " + clean[e:]
    clean = _COUNT_PHRASE_RE.sub(" ", clean)

    parsed = parse_dimension_input(clean)
    # PHASE-6B P2-1 — canonicalize every dimension to CENTIMETRES at
    # capture time (mm ÷10, m ×100, inch ×2.54). Chinese sellers give mm;
    # Shipify computes and displays in cm. Doing it here (not only inside
    # compute_estimate) keeps the stored state, the missing-slot prompt,
    # corrections and new cycles all consistently in cm.
    _pu = (parsed.get("dimension_unit") or "").lower()
    if _pu in ("mm", "m", "inch") and parsed.get("dimension_values"):
        _f = {"mm": 0.1, "m": 100.0, "inch": 2.54}[_pu]
        parsed["dimension_values"] = [round(v * _f, 4) for v in parsed["dimension_values"]]
        for _k in ("length", "width", "height"):
            if parsed.get(_k) is not None:
                parsed[_k] = round(parsed[_k] * _f, 4)
        parsed["dimension_unit"] = "cm"
    if weight_here is not None:
        parsed["weight"] = weight_here
        parsed["weight_unit"] = "kg"
    merged = _merge_captured(
        {"length": st.length, "width": st.width, "height": st.height,
         "weight": st.weight, "dimension_unit": st.dim_unit, "weight_unit": "kg"},
        parsed)
    st.length, st.width, st.height = merged.get("length"), merged.get("width"), merged.get("height")
    if merged.get("weight") is not None:
        st.weight = merged["weight"]
    if merged.get("dimension_unit"):
        st.dim_unit = merged["dimension_unit"]
    return st


# CORE-CONVERSATION — a message that is (essentially) JUST a box-
# dimensions triple ("54x12x43", "กล่อง 50x40x30", "50 × 40 × 30 ซม"). The
# x/×/* separator between three sane box-size numbers is itself the "here
# are my box sizes, work out shipping" signal — no calc verb needed. A
# Thai/Gregorian date uses "-" / "/" (never x), a code is letter-prefixed
# with no separator, and a longer sentence fails the anchors — so none of
# those trigger it.
_BARE_DIMS_TRIPLE_RE = re.compile(
    r"^(?:กล่อง|ขนาด|box)?\s*"
    r"\d{1,3}(?:\.\d+)?\s*[x×*]\s*\d{1,3}(?:\.\d+)?\s*[x×*]\s*\d{1,3}(?:\.\d+)?"
    r"\s*(?:ซม\.?|ซ\.ม\.?|cm|มม\.?|mm|นิ้ว|เมตร|เมตร\.?|m)?\s*(?:ครับ|ค่ะ|คะ|นะคะ|นะ)?\s*$",
    re.IGNORECASE)


def is_bare_dims_triple(message: str) -> bool:
    return bool(_BARE_DIMS_TRIPLE_RE.match((message or "").strip()))


def opens_estimate_flow(message: str, state: EstimateState) -> bool:
    """A turn opens the flow when it explicitly asks for a calculation,
    names a shipping-cost topic together with a concrete input, is
    essentially just a box-dimensions triple, OR already supplies enough
    structured values to compute (weight + >=2 dims, or a method +
    weight/dims). A bare "เรทเท่าไหร่" / "ค่านำเข้าเท่าไหร่" (no value, no
    calc verb) stays a rate FAQ."""
    t = (message or "").strip()
    if _OTHER_BUSINESS_INTENT_RE.search(t):
        return False
    if is_bare_dims_triple(t):
        return True
    has_value = state.weight is not None or state._dims_present() >= 2
    if has_value and (_CALC_VERB_RE.search(t) or _CALC_TOPIC_RE.search(t)
                      or state.method is not None or state.complete()):
        return True
    if state.weight is not None and state._dims_present() >= 2:
        return True
    # a short, calc-only request with no measurement yet ("ช่วยคำนวณ
    # ค่าส่งให้หน่อย") opens the flow so it can ask for the inputs.
    if _CALC_VERB_RE.search(t) and len(t) <= 42 and not _method_of(t):
        return True
    return False


def _is_explicit_new_request(text: str, interpretation: Optional[object] = None) -> bool:
    """The customer explicitly starts a NEW calculation. SEMANTIC-FIRST-1:
    the central Interpretation's SHIPPING_ESTIMATE family is the primary
    signal; the calculator VERB / cost TOPIC regex is the fallback for a
    degraded / absent interpretation. A bare value / route answer /
    correction is NOT a new request (it continues the current thread)."""
    t = text or ""
    if _OTHER_BUSINESS_INTENT_RE.search(t):
        return False
    fam = getattr(interpretation, "intent_family", None) if interpretation is not None else None
    op = getattr(interpretation, "follow_up_op", "NONE") if interpretation is not None else "NONE"
    p = parse_dimension_input(t)
    has_value = (p["weight"] is not None or len(p["dimension_values"]) >= 2
                 or _WEIGHT_RE.search(t) is not None or _WEIGHT_G_RE.search(t) is not None)
    # CALCULATOR-REGRESSION-2 — a BARE route/method answer ("เอารถครับ",
    # "รถ", "เรือค่ะ") is a CONTINUATION value, never a fresh calculation
    # — even though the widened SEMANTIC-FIRST-2 LLM gate now labels it
    # SHIPPING_ESTIMATE / SET_VALUE. It carries no weight / dimensions /
    # calc verb of its own, so branch B (accumulate the active thread)
    # must own it; treating it as "explicit new" wiped the retained
    # weight + dimensions on the REAL LINE route-answer turn.
    if _method_of(t) is not None and not has_value and not _CALC_VERB_RE.search(t):
        return False
    if fam == "SHIPPING_ESTIMATE" and op == "NONE":
        return True
    if fam == "SHIPPING_ESTIMATE" and op == "SET_VALUE" and (has_value or _CALC_VERB_RE.search(t)):
        return True
    if _CALC_VERB_RE.search(t):
        return True
    return bool(_CALC_TOPIC_RE.search(t) and has_value)


def derive_estimate_state(history: Optional[List[Dict]], current_message: str,
                          interpretation: Optional[object] = None) -> Optional[EstimateState]:
    """State of the CURRENT calculation thread, or None.

    Episode lifecycle:  NEW -> COLLECTING -> COMPLETE/CALCULATED -> CLOSED.
      * An EXPLICIT NEW calculator request always starts a fresh thread —
        it inherits NOTHING from any prior (even incomplete) thread.
      * A short value / route answer / correction / comparison continues
        the current thread — accumulated from the last explicit-new
        request, so corrections chain correctly across recomputes.
      * Anything else after a delivered estimate is not a calculator turn.
    Deterministic, no LLM.
    """
    turns = list(history or [])[-_LOOKBACK:]
    cur_msg = (current_message or "").strip()
    _op = getattr(interpretation, "follow_up_op", "NONE") if interpretation is not None else "NONE"

    # REAL LINE 2026-09-16 (P0) — an explicit NEW import-journey opener
    # closes any open calculator thread: it is never a calculator turn,
    # however recent the last estimate prompt was and whatever bare
    # number it carries ("อยากสั่งของจากจีน 20 คู่" — the "20" is an order
    # quantity, not a dimension). The ONE journey boundary, shared with
    # every other state system (services/conversation_semantics.py::
    # is_new_journey_opener). Lazy import: conversation_semantics
    # imports this module's parser.
    from services.conversation_semantics import is_new_journey_opener as _is_new_journey
    if _is_new_journey(cur_msg):
        return None

    # is there an estimate-flow assistant turn in the recent window
    # (the newest assistant turn only — anything else means the topic
    # moved on)?
    flow_open = False
    for t in reversed(turns):
        if t.get("role") != "assistant":
            continue
        c = t.get("content") or ""
        flow_open = bool(_EST_RESULT_RE.search(c) or _EST_PROMPT_RE.search(c))
        break

    if not flow_open:
        # A) no active thread. An explicit NEW request, or the message
        # standing alone, can open one (a method + weight/dims, or
        # weight + >=2 dims). Nothing prior to inherit either way.
        if _is_explicit_new_request(cur_msg, interpretation):
            return extract_estimate_fields(cur_msg)
        cur = extract_estimate_fields(cur_msg)
        return cur if opens_estimate_flow(cur_msg, cur) else None

    # CUSTOMER-CALC-MULTITURN-1 — a flow IS already open. Two signals
    # discard the accumulated state and start fresh: an explicit "this
    # is a DIFFERENT package" wording (_NEW_EPISODE_RE — "เริ่มใหม่",
    # "อีกกล่องนึง", ...), or the message NAMING THE COST TOPIC itself
    # (_CALC_TOPIC_RE — "ค่านำเข้า", "ค่าส่ง", ...), which is how a
    # customer opens a genuinely NEW ask ("ค่านำเข้าเท่าไหร่คะ ... ",
    # "ช่วยคิดค่าส่งใหม่ ...") even right after a completed estimate —
    # CUSTOMER-CALC-1.1's own protected behaviour. A bare calc VERB with
    # NO topic word ("ลองคำนวณให้หน่อย" attached to a dimension
    # correction) is NOT by itself enough — it merges into the current
    # thread like every other continuation instead (REAL LINE: "52cm
    # 22cm 110cm ลองคำนวณให้หน่อย" after a complete 20kg+BOTH state
    # wiped weight/method because ANY calc-verb mention used to force a
    # full reset here).
    if _NEW_EPISODE_RE.search(cur_msg) or _CALC_TOPIC_RE.search(cur_msg):
        return extract_estimate_fields(cur_msg)

    # B) a short value / route answer / correction / comparison continues
    # the current thread. Anything else is not a calculator turn.
    is_route = _method_of(cur_msg) is not None
    parsed_cur = parse_dimension_input(cur_msg)
    is_value = (parsed_cur["weight"] is not None or bool(parsed_cur["dimension_values"])
                or _WEIGHT_RE.search(cur_msg) is not None or _WEIGHT_G_RE.search(cur_msg) is not None)
    is_unit_only_correction = bool(_dimension_unit(cur_msg)) and _UNIT_CORRECTION_RE.search(cur_msg)
    is_corr = (bool(_CORRECTION_RE.search(cur_msg)) or _op in ("CORRECTION", "COMPARISON")
               or bool(is_unit_only_correction) or bool(_RECALC_RE.search(cur_msg)))
    if not (is_route or is_value or is_corr):
        return None

    # accumulate the thread: every user turn since the most recent reset
    # (the SAME two signals that reset the state above — a "different
    # package" wording or a fresh cost-topic ask), or the start of the
    # window.
    start = 0
    for i in range(len(turns) - 1, -1, -1):
        t = turns[i]
        tc = t.get("content") or ""
        if t.get("role") == "user" and (_NEW_EPISODE_RE.search(tc) or _CALC_TOPIC_RE.search(tc)):
            start = i
            break
    users = [t.get("content") or "" for i, t in enumerate(turns)
             if t.get("role") == "user" and i >= start]
    if users and _FLOW_EXIT_RE.search(users[-1]) and not _has_calc_signal(users[-1]):
        return None

    st = EstimateState()
    for u in users:
        extract_estimate_fields(u, st)
    # fold in THIS turn's value / route / correction — a comparison
    # ("ถ้าเป็นทางเรือล่ะ") or correction ("ไม่ใช่ 2 กิโล เป็น 3 กิโล")
    # overwrites the relevant slot, everything else is retained.
    extract_estimate_fields(cur_msg, st)
    return st


# ── computation ──────────────────────────────────────────────────────
def _to_cm(v: float, unit: Optional[str]) -> float:
    if unit == "mm":
        return v * 0.1
    if unit == "inch":
        return v * 2.54
    if unit == "m":
        return v * 100.0
    return v  # cm / unknown -> treat as cm


def compute_estimate(state: EstimateState, method: str) -> Dict:
    unit = state.dim_unit
    L, W, H = (_to_cm(state.length, unit), _to_cm(state.width, unit), _to_cm(state.height, unit))
    cbm_one = round((L * W * H) / 1_000_000.0, 6)
    qty = state.quantity or 1
    cbm_total = round(cbm_one * qty, 6) if state.per_box_dims and qty > 1 else cbm_one

    if state.total_weight_given:
        weight_total = state.weight
    elif state.per_box_dims and qty > 1:
        weight_total = round(state.weight * qty, 4)
    else:
        weight_total = state.weight

    r = RATES[method]
    kg_charge = round(weight_total * r["kg"], 4)
    cbm_charge = round(cbm_total * r["cbm"], 4)
    use_cbm = cbm_charge >= kg_charge
    return {
        "method": method, "cbm": cbm_total, "weight": weight_total,
        "kg_charge": kg_charge, "cbm_charge": cbm_charge,
        "estimate": round(max(kg_charge, cbm_charge), 2),
        "basis": "cbm" if use_cbm else "weight",
    }


def _fmt(v: float) -> str:
    return f"{v:.2f}" if v % 1 else str(int(v))


def _basis_line(c: Dict) -> str:
    if c["basis"] == "cbm":
        return (f"โดยคิดจากปริมาตร {c['cbm']:g} CBM ({_fmt(c['cbm_charge'])} บาท) "
                f"ซึ่งมีค่ามากกว่าการคิดตามน้ำหนัก ({_fmt(c['kg_charge'])} บาท)")
    return (f"โดยคิดจากน้ำหนัก {_fmt(c['weight'])} กก. ({_fmt(c['kg_charge'])} บาท) "
            f"ซึ่งมีค่ามากกว่าการคิดตามปริมาตร {c['cbm']:g} CBM ({_fmt(c['cbm_charge'])} บาท)")


_ESTIMATE_FOOTER = ("เป็นการประเมินเบื้องต้นจากข้อมูลที่แจ้งมา "
                    "ค่าจริงจะคิดจากการวัดขนาดและน้ำหนักที่โกดังอีกครั้งค่ะ")


def estimate_reply(state: EstimateState) -> str:
    # CUSTOMER-CALC-MULTITURN-1 — BOTH methods requested: compute road
    # AND sea with compute_estimate() (the SAME single source of truth
    # used for a single method, never a parallel calculation), return
    # both in one answer, never ask which one.
    if state.method == "both":
        c_road = compute_estimate(state, "road")
        c_sea = compute_estimate(state, "sea")
        return (
            f"ประเมินเบื้องต้นให้ทั้งสองทางค่ะ\n"
            f"ทางรถ: ประมาณ {c_road['estimate']:.2f} บาท ({_basis_line(c_road)})\n"
            f"ทางเรือ: ประมาณ {c_sea['estimate']:.2f} บาท ({_basis_line(c_sea)})\n"
            f"({_ESTIMATE_FOOTER})")
    c = compute_estimate(state, state.method)
    m_th = _METHOD_TH[state.method]
    return (f"ประเมินเบื้องต้นสำหรับ{m_th}ประมาณ {c['estimate']:.2f} บาทค่ะ {_basis_line(c)} "
            f"({_ESTIMATE_FOOTER})")


_MISSING_TH = {"weight": "น้ำหนักสินค้า", "dimensions": "ขนาดสินค้า (กว้าง x ยาว x สูง)",
               "method": "วิธีขนส่ง (ทางรถ / ทางเรือ)"}


# PHASE 6 — CURRENT-TURN QUESTION MUST NOT BE DROPPED.
# "ค่าขนส่งคิดยังไง คำนวนค่าส่งให้หน่อย" carries TWO intents in one turn: a
# PUBLIC question about HOW the charge is derived, and a request to run
# the calculation. The estimate flow used to consume only the second and
# reply with a bare slot request, silently dropping a question the
# customer had actually asked (customer_uat CUS-G04 / CUS-P07). This
# recognizes the basis/rate half so the reply can answer it before asking
# for what is still missing. It is a QUESTION-SHAPE test only — it never
# decides routing and never invents a number.
_RATE_BASIS_Q_RE = re.compile(
    r"(?:ค่าขนส่ง|ค่าส่ง|ค่านำเข้า|ราคา|เรท|คิดเงิน|คิดราคา|ค่าบริการ)\S{0,6}"
    r"(?:คิด|คำนวณ|คำนวน|ประเมิน)\S{0,4}(?:ยังไง|ยังงัย|อย่างไร|จากอะไร|ไง)"
    r"|(?:คิด|คำนวณ|คำนวน)\S{0,4}(?:ยังไง|ยังงัย|อย่างไร|จากอะไร)"
    r"|คิดจาก(?:อะไร|ไหน)|คิดตาม(?:อะไร|ไหน)|ใช้อะไรคิด",
    re.IGNORECASE)


def asks_rate_basis(message: str) -> bool:
    """True when the turn also asks HOW the shipping charge is derived."""
    return bool(_RATE_BASIS_Q_RE.search(message or ""))


def rate_basis_answer() -> str:
    """The truthful charge-basis explanation, rendered from the SAME
    RATES table compute_estimate() charges from — one source of truth, so
    this can never drift from what the calculator actually applies, and
    no rate is ever restated by hand."""
    road, sea = RATES["road"], RATES["sea"]
    return ("ค่าขนส่งคิดจากน้ำหนักและปริมาตรค่ะ ค่าไหนมากกว่าจะถูกใช้เป็นค่าขนส่ง "
            f"โดยทางรถ {road['kg']:g} บาท/กก. หรือ {road['cbm']:g} บาท/คิว "
            f"และทางเรือ {sea['kg']:g} บาท/กก. หรือ {sea['cbm']:g} บาท/คิวค่ะ")


def estimate_turn_answers(state: EstimateState, *, basis_question: bool = False) -> bool:
    """True when THIS turn's reply carries a public answer (the charge
    basis) and not only a slot request. A cold opener with nothing known
    always answers; a partial turn that merely acknowledges what was
    supplied and asks for the rest does not. The caller uses this to
    label the execution route truthfully: a turn that answers is the same
    GENERAL route the completed estimate already uses, while a pure
    collection turn stays WORKFLOW."""
    return bool(basis_question or not state.known_any())


def estimate_missing_prompt(state: EstimateState, *, basis_question: bool = False) -> str:
    """`basis_question` -> the turn ALSO asked how the charge is derived;
    answer that first (PHASE 6), then ask for what is still missing, so a
    question the customer actually asked is never silently dropped."""
    miss = state.missing()
    if not miss:
        return rate_basis_answer() if basis_question else ""
    got = []
    if state.weight is not None:
        got.append(f"น้ำหนัก {_fmt(state.weight)} กก.")
    if state._dims_present() == 3:
        got.append(f"ขนาด {_fmt(state.length)}x{_fmt(state.width)}x{_fmt(state.height)} "
                   f"{state.dim_unit or 'ซม.'}")
    if state.method:
        got.append("ทั้งทางรถและทางเรือ" if state.method == "both" else _METHOD_TH[state.method])
    # only method left -> the short preferred question
    # Answer-bearing first turn: when NOTHING is known yet the customer
    # has supplied no slot to acknowledge, so a bare slot request would
    # return nothing at all for the question they asked. The public rate
    # basis is free to give and is exactly what the CS team's own scripted
    # answer leads with (AI_API_Requirement_For_Client.xlsx, sheet
    # "1.ถามเบื้องต้น" row 4), so lead with it too.
    basis = (rate_basis_answer() + " ") if (basis_question or not got) else ""
    if miss == ["method"]:
        head = f"รับทราบค่ะ ({' • '.join(got)}) " if got else ""
        return basis + head + "ต้องการประเมินทางรถหรือทางเรือคะ"
    ack = f"รับทราบค่ะ ({' • '.join(got)}) " if got else ""
    want = " และ ".join(_MISSING_TH[k] for k in miss)
    return f"{basis}{ack}ยังขาดข้อมูลสำหรับประเมินค่ะ รบกวนแจ้ง{want}เพิ่มเติมด้วยนะคะ"
