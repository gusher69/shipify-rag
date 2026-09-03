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

from rag.slot_filling_flow import parse_dimension_input, _merge_captured

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

# ── intent / signal patterns ─────────────────────────────────────────
# a calculator VERB — the customer explicitly asks for a calculation.
_CALC_VERB_RE = re.compile(
    r"ช่วยคำนวณ|คำนวณให้|คำนวณค่า|ประเมินค่า|ประเมินราคา|คิดค่า(?:ขนส่ง|ส่ง|นำเข้า)|"
    r"ขอประเมิน|ประเมินเบื้องต้น")
# a calculator TOPIC — only opens the flow together with a concrete value.
_CALC_TOPIC_RE = re.compile(r"ค่านำเข้า|ค่าขนส่ง|ค่าส่ง|เรทนำเข้า|ค่าจัดส่ง")
# this flow's own missing-slot / estimate prompt (so a multi-turn flow
# is recognised on the next turn).
_EST_PROMPT_RE = re.compile(r"ประเมินเบื้องต้นสำหรับทาง|ต้องการประเมินทางรถหรือทางเรือ|"
                            r"รบกวนแจ้ง(?:น้ำหนัก|ขนาด).{0,40}(?:เพื่อประเมิน|สำหรับประเมิน)|ยังขาดข้อมูลสำหรับประเมิน")
_METHOD_RE = re.compile(r"ทางรถ|ทางบก|ทางเรือ|โดยรถ|โดยเรือ|(?<![ก-๙])รถ(?![ก-๙])|(?<![ก-๙])เรือ(?![ก-๙])")
_QUANTITY_RE = re.compile(r"(\d+)\s*(?:ชิ้น|กล่อง|อัน|ใบ|ลัง|pcs?|box(?:es)?)", re.IGNORECASE)
# quantity / count phrases that must NOT be read as dimension numbers.
_COUNT_PHRASE_RE = re.compile(
    r"\d+\s*(?:ชิ้น|กล่อง|อัน|ใบ|ลัง|pcs?|box(?:es)?)|สินค้า\s*\d+|จำนวน\s*\d+", re.IGNORECASE)
# weight — extends rag/slot_filling_flow's own pattern with bare "โล".
_WEIGHT_RE = re.compile(r"(\d+\.?\d*)\s*(?:กิโลกรัม|กิโล|กก\.?|kg|โล)", re.IGNORECASE)
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

    def complete(self) -> bool:
        return not self.missing()


def _method_of(text: str) -> Optional[str]:
    if not text:
        return None
    if re.search(r"ทางเรือ|โดยเรือ|(?<![ก-๙])เรือ(?![ก-๙])", text):
        return "sea"
    if re.search(r"ทางรถ|ทางบก|โดยรถ|(?<![ก-๙])รถ(?![ก-๙])", text):
        return "road"
    return None


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
    wms = list(_WEIGHT_RE.finditer(clean))
    if wms:
        # a "ไม่ใช่ 2 กิโล เป็น 3 กิโล" correction states two values —
        # the LAST is the corrected one (SEM-GEN correction semantics).
        wm = wms[-1]
        weight_here = float(wm.group(1))
        for w in reversed(wms):
            clean = clean[:w.start()] + " " + clean[w.end():]
    clean = _COUNT_PHRASE_RE.sub(" ", clean)

    parsed = parse_dimension_input(clean)
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


def opens_estimate_flow(message: str, state: EstimateState) -> bool:
    """A turn opens the flow when it explicitly asks for a calculation,
    names a shipping-cost topic together with a concrete input, OR
    already supplies enough structured values to compute (weight + >=2
    dims, or a method + weight/dims). A bare "เรทเท่าไหร่" / "ค่านำเข้า
    เท่าไหร่" (no value, no calc verb) stays a rate FAQ."""
    t = (message or "").strip()
    if _OTHER_BUSINESS_INTENT_RE.search(t):
        return False
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


def derive_estimate_state(history: Optional[List[Dict]], current_message: str) -> Optional[EstimateState]:
    """The active shipping-estimate collection (accumulated from EVERY
    user turn since the flow opened), or None. Deterministic, no LLM."""
    turns = list(history or [])[-_LOOKBACK:]

    # walk backward: collect user turns; skip our own mid-flow prompts;
    # stop at the first substantive non-prompt assistant turn.
    user_turns: List[str] = []
    saw_prompt = False
    for turn in reversed(turns):
        role = turn.get("role")
        c = turn.get("content") or ""
        if role == "user":
            user_turns.append(c)
        elif role == "assistant":
            if _EST_PROMPT_RE.search(c) or "ประเมินเบื้องต้นสำหรับ" in c:
                saw_prompt = True
                continue  # our own prompt/result — keep walking to the trigger
            break  # an unrelated assistant turn — the flow (if any) is newer
    user_turns.reverse()  # oldest first

    # is a flow active?
    active = saw_prompt
    if not active:
        for u in user_turns + [current_message]:
            if opens_estimate_flow(u, extract_estimate_fields(u)):
                active = True
                break
    if not active:
        return None

    # staleness — the newest prior user turn is a hard non-calculator
    # subject with no calc value.
    if user_turns:
        last = user_turns[-1]
        if _FLOW_EXIT_RE.search(last) and not (
                _method_of(last) or parse_dimension_input(last)["weight"] is not None
                or parse_dimension_input(last)["dimension_values"]):
            return None

    st = EstimateState()
    for u in user_turns:
        extract_estimate_fields(u, st)
    return st


# ── computation ──────────────────────────────────────────────────────
def _to_cm(v: float, unit: Optional[str]) -> float:
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


def estimate_reply(state: EstimateState) -> str:
    c = compute_estimate(state, state.method)
    m_th = _METHOD_TH[state.method]
    if c["basis"] == "cbm":
        basis_line = (f"โดยคิดจากปริมาตร {c['cbm']:g} CBM ({_fmt(c['cbm_charge'])} บาท) "
                      f"ซึ่งมีค่ามากกว่าการคิดตามน้ำหนัก ({_fmt(c['kg_charge'])} บาท)")
    else:
        basis_line = (f"โดยคิดจากน้ำหนัก {_fmt(c['weight'])} กก. ({_fmt(c['kg_charge'])} บาท) "
                      f"ซึ่งมีค่ามากกว่าการคิดตามปริมาตร {c['cbm']:g} CBM ({_fmt(c['cbm_charge'])} บาท)")
    return (f"ประเมินเบื้องต้นสำหรับ{m_th}ประมาณ {c['estimate']:.2f} บาทค่ะ {basis_line} "
            f"(เป็นการประเมินเบื้องต้นจากข้อมูลที่แจ้งมา ค่าจริงจะคิดจากการวัดขนาดและน้ำหนักที่โกดังอีกครั้งค่ะ)")


_MISSING_TH = {"weight": "น้ำหนักสินค้า", "dimensions": "ขนาดสินค้า (กว้าง x ยาว x สูง)",
               "method": "วิธีขนส่ง (ทางรถ / ทางเรือ)"}


def estimate_missing_prompt(state: EstimateState) -> str:
    miss = state.missing()
    if not miss:
        return ""
    got = []
    if state.weight is not None:
        got.append(f"น้ำหนัก {_fmt(state.weight)} กก.")
    if state._dims_present() == 3:
        got.append(f"ขนาด {_fmt(state.length)}x{_fmt(state.width)}x{_fmt(state.height)} "
                   f"{state.dim_unit or 'ซม.'}")
    if state.method:
        got.append(_METHOD_TH[state.method])
    # only method left -> the short preferred question
    if miss == ["method"]:
        head = f"รับทราบค่ะ ({' • '.join(got)}) " if got else ""
        return head + "ต้องการประเมินทางรถหรือทางเรือคะ"
    ack = f"รับทราบค่ะ ({' • '.join(got)}) " if got else ""
    want = " และ ".join(_MISSING_TH[k] for k in miss)
    return f"{ack}ยังขาดข้อมูลสำหรับประเมินค่ะ รบกวนแจ้ง{want}เพิ่มเติมด้วยนะคะ"
