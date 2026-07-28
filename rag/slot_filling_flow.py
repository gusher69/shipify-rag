"""Active Slot-Filling Flow resolver (P0, 2026-07-22) — generic, reusable
resolution for a short reply that supplies STRUCTURED DATA (dimensions,
weight, ...) for a flow the assistant just started, never a list of
hardcoded (question) -> (answer) pairs.

Root cause this exists to fix: after the assistant asked for a package's
size and weight to calculate shipping cost, a bare "4*6" reply was
classified as a STANDALONE ARITHMETIC question ("4*6 = 24") instead of
two dimension values for the active flow — the pipeline had no concept
of "the assistant is mid-way through collecting structured input for an
active flow," so every short numeric reply fell through to the generic
analytical/calculation path regardless of context.

Resolution priority this module occupies (see rag/query_resolution.py):
    1. Explicit topic change or cancellation
    2. Pending clarification answer   (rag/clarification_state.py)
    3. Active slot-filling input      (THIS MODULE)
    4. Follow-up resolution
    5. Standalone intent classification
    6. Generic fallback

Deterministic, pure Python, no LLM call — the same architecture as
rag/clarification_state.py.
"""
import re
from typing import Dict, List, Optional

ACTIVE_FLOW_SHIPPING_COST = "shipping_cost_calculation"

REQUIRED_SLOTS = ["length", "width", "height", "dimension_unit", "weight", "weight_unit", "shipping_method"]

# The assistant just asked for a package's dimensions/weight — generic
# keyword signal, never tied to one specific wording/business.
_DIMENSION_REQUEST_RE = re.compile(r"ขนาด|น้ำหนัก|ปริมาตร|\bCBM\b|กว้าง|ยาว|สูง", re.IGNORECASE)
# A follow-up prompt THIS module itself generates when slots are still
# missing (see build_missing_slot_message) — recognizable so a captured
# value from an earlier turn in the SAME flow is never lost across
# multiple back-and-forth turns.
_FLOW_CONTINUATION_RE = re.compile(r"ยังขาด|รบกวนแจ้งในรูปแบบ")

# Cancellation / explicit topic change — a user abandoning the active
# flow for something else entirely. A bare numeric expression never
# matches this, so "4*6" can never be mistaken for a cancellation.
_CANCELLATION_RE = re.compile(r"ไม่คำนวณแล้ว|ยกเลิก|ไม่เอาแล้ว|พอแล้ว|เปลี่ยนเรื่อง")

# Explicit arithmetic intent — present, the user is asking for the
# EXPRESSION to be evaluated, not supplying dimension values (Part
# "Data entry vs Math", "4*6 ได้เท่าไหร่" -> explicit math intent).
_EXPLICIT_MATH_RE = re.compile(r"เท่าไหร่|ช่วยคำนวณ|คำนวณให้|บวก|คูณ|หาร|calculate", re.IGNORECASE)

_LABELED_DIM_PATTERNS = {
    "length": re.compile(r"ยาว\s*(\d+\.?\d*)"),
    "width": re.compile(r"กว้าง\s*(\d+\.?\d*)"),
    "height": re.compile(r"สูง\s*(\d+\.?\d*)"),
}
_WEIGHT_WITH_UNIT_RE = re.compile(r"(\d+\.?\d*)\s*(กก\.?|kg|กิโล(?:กรัม)?)", re.IGNORECASE)
_CM_UNIT_RE = re.compile(r"\bcm\b|ซม\.?|เซนติเมตร", re.IGNORECASE)
_INCH_UNIT_RE = re.compile(r"นิ้ว|\binch(?:es)?\b", re.IGNORECASE)
_METER_UNIT_RE = re.compile(r"(?<![a-zA-Z])\bm\b(?!m)|เมตร(?!ริก)", re.IGNORECASE)
_BARE_NUMBER_RE = re.compile(r"\d+\.?\d*")
_DIM_GROUP_SPLIT_RE = re.compile(r"[x×*]|,|\s+", re.IGNORECASE)


def is_cancellation(text: str) -> bool:
    return bool(_CANCELLATION_RE.search(text or ""))


def is_explicit_math_intent(text: str) -> bool:
    return bool(_EXPLICIT_MATH_RE.search(text or ""))


def _dimension_unit(text: str) -> Optional[str]:
    if _CM_UNIT_RE.search(text):
        return "cm"
    if _INCH_UNIT_RE.search(text):
        return "inch"
    if _METER_UNIT_RE.search(text):
        return "m"
    return None


def parse_dimension_input(text: str) -> Dict:
    """Extracts whatever dimension/weight information is present in
    `text`, WITHOUT assuming anything not actually stated — no default
    unit, no assumed length/width/height ordering for a bare numeric
    group, no invented third dimension, no invented weight. Returns:
        {"dimension_values": [float, ...], "dimension_unit": str|None,
         "length": float|None, "width": float|None, "height": float|None,
         "weight": float|None, "weight_unit": str|None}
    """
    text = text or ""
    result = {"dimension_values": [], "dimension_unit": None,
              "length": None, "width": None, "height": None,
              "weight": None, "weight_unit": None}

    # Labeled form ("ยาว 40 กว้าง 60 สูง 30") — order-independent, only
    # the slots actually present are captured.
    labeled_any = False
    for slot, pattern in _LABELED_DIM_PATTERNS.items():
        m = pattern.search(text)
        if m:
            result[slot] = float(m.group(1))
            labeled_any = True

    # Weight (always extracted independently of the dimension form —
    # "12 kg" can arrive in its own turn, or alongside dimensions).
    weight_match = _WEIGHT_WITH_UNIT_RE.search(text)
    working_text = text
    if weight_match:
        result["weight"] = float(weight_match.group(1))
        result["weight_unit"] = "kg"
        working_text = text[:weight_match.start()] + text[weight_match.end():]

    if labeled_any:
        result["dimension_values"] = [v for v in (result["length"], result["width"], result["height"])
                                       if v is not None]
        result["dimension_unit"] = _dimension_unit(text)
        return result

    result["dimension_unit"] = _dimension_unit(working_text)

    # Bare numeric-group form ("4*6", "40x60x30", "40 60 30", "30 cm").
    # Only counted as dimension values when the weight number (if any)
    # has already been carved out above — this is what keeps "12 kg"
    # alone from also being misread as a bare dimension number.
    remaining_nums = _BARE_NUMBER_RE.findall(working_text)
    if remaining_nums:
        result["dimension_values"] = [float(n) for n in remaining_nums]

    return result


def _merge_captured(existing: Dict, new: Dict) -> Dict:
    """A later turn's values ADD to (never silently overwrite with
    None) whatever was already captured — e.g. two dimension values from
    turn 1, a third + unit from turn 2, must all still be present after
    turn 2. A genuinely NEW value for an already-filled slot (the user
    correcting themselves) does overwrite, since it's the more recent
    statement of that fact."""
    merged = dict(existing)
    for slot in ("length", "width", "height", "weight"):
        if new.get(slot) is not None:
            merged[slot] = new[slot]
    if new.get("dimension_unit"):
        merged["dimension_unit"] = new["dimension_unit"]
    if new.get("weight_unit"):
        merged["weight_unit"] = new["weight_unit"]

    # Bare (unlabeled) dimension_values fill length/width/height IN
    # ORDER, skipping slots already filled by an earlier labeled/bare
    # turn — never overwriting an already-captured value with a bare,
    # ambiguous later number.
    if new.get("dimension_values"):
        slots_in_order = ["length", "width", "height"]
        open_slots = [s for s in slots_in_order if merged.get(s) is None]
        for value, slot in zip(new["dimension_values"], open_slots):
            merged[slot] = value
    return merged


def _captured_to_public(captured: Dict) -> Dict:
    values = [captured.get(s) for s in ("length", "width", "height") if captured.get(s) is not None]
    return {
        "dimension_values": values,
        "dimension_count": len(values),
        "dimension_unit": captured.get("dimension_unit"),
        "weight": captured.get("weight"),
        "weight_unit": captured.get("weight_unit"),
    }


def _missing_slots(captured: Dict) -> List[str]:
    missing = []
    dim_count = sum(1 for s in ("length", "width", "height") if captured.get(s) is not None)
    if dim_count < 3:
        missing.append("third_dimension" if dim_count == 2 else "dimensions")
    if not captured.get("dimension_unit"):
        missing.append("dimension_unit")
    if captured.get("weight") is None:
        missing.append("weight")
    if captured.get("weight") is not None and not captured.get("weight_unit"):
        missing.append("weight_unit")
    return missing


def detect_active_flow(history: Optional[List[Dict]]) -> Optional[Dict]:
    """Scans `history` backward for an active shipping_cost_calculation
    flow. Returns None the moment a cancellation is found in a USER turn
    (the flow was abandoned since), or if the most recent assistant turn
    is neither the original dimension-request trigger nor this module's
    own "still missing" continuation prompt (i.e. the conversation moved
    on to something else entirely). Captured slots are accumulated ONLY
    from user turns that are part of THIS flow (after the trigger),
    never from the rest of the conversation."""
    if not history:
        return None

    user_turns_in_flow: List[str] = []
    trigger_found = False
    for turn in reversed(history):
        role = turn.get("role")
        content = (turn.get("content") or "")
        if role == "user":
            if is_cancellation(content):
                return None
            user_turns_in_flow.append(content)
        elif role == "assistant":
            # Check our OWN continuation-prompt wording FIRST — it also
            # legitimately contains dimension/weight vocabulary (it's
            # restating what's still missing), so it would otherwise
            # false-match _DIMENSION_REQUEST_RE and make the walk stop
            # one turn too early, losing everything captured before it.
            if _FLOW_CONTINUATION_RE.search(content):
                continue  # our own mid-flow prompt — keep walking backward
            if _DIMENSION_REQUEST_RE.search(content):
                trigger_found = True
                break  # the ORIGINAL trigger — flow starts here
            return None  # an unrelated assistant turn — no active flow

    if not trigger_found:
        return None

    user_turns_in_flow.reverse()  # oldest first
    # `history` holds only PRIOR turns (the CURRENT reply being resolved
    # is passed separately as `text`, never itself inside `history` —
    # same convention as rag/clarification_state.py) — every user turn
    # found here already happened before this one.
    captured: Dict = {}
    for prior_text in user_turns_in_flow:
        captured = _merge_captured(captured, parse_dimension_input(prior_text))

    return {"active_flow": ACTIVE_FLOW_SHIPPING_COST, "pending_slots": list(REQUIRED_SLOTS),
            "captured_slots_before": captured}


_MISSING_SLOT_LABELS_TH = {
    "third_dimension": "ความสูง", "dimensions": "ขนาดสินค้า", "dimension_unit": "หน่วยของขนาด",
    "weight": "น้ำหนักสินค้า", "weight_unit": "หน่วยน้ำหนัก",
}


def build_missing_slot_message(newly_captured: Dict, missing: List[str]) -> str:
    """Acknowledges only what was ACTUALLY captured this turn, asks only
    for what's still missing — never re-requests an already-captured
    value (Part "Selective Missing-Slot Request")."""
    lines = []
    dims = newly_captured.get("dimension_values") or []
    if len(dims) >= 2:
        joined = " × ".join(_format_num(v) for v in dims)
        lines.append(f"ได้รับขนาด {joined} แล้วค่ะ")
    elif len(dims) == 1:
        lines.append(f"ได้รับค่า {_format_num(dims[0])} แล้วค่ะ")
    if newly_captured.get("weight") is not None:
        lines.append(f"ได้รับน้ำหนัก {_format_num(newly_captured['weight'])} แล้วค่ะ")

    missing_labels = [_MISSING_SLOT_LABELS_TH.get(m, m) for m in missing]
    if missing_labels:
        lead = "แต่ยังขาด" if lines else "ยังขาด"
        lines.append(f"{lead}{' '.join(missing_labels)}")
    lines.append("")
    lines.append("รบกวนแจ้งในรูปแบบ เช่น:")
    lines.append("40 × 60 × 30 ซม. น้ำหนัก 12 กก.")
    return "\n".join(lines)


def _format_num(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else str(v)


# Data entry vs math (Part "Data Entry vs Math") — a bare arithmetic
# expression is only ever evaluated as MATH when there is NO active
# slot-filling flow at all; the moment one exists, the exact same "4*6"
# text is dimension input instead (see resolve_slot_filling_turn above).
# Deliberately restricted to digits/operators/parentheses/whitespace only
# — never a general eval(), so this can never execute anything beyond
# basic arithmetic.
_SAFE_ARITHMETIC_CHARS_RE = re.compile(r"^[\d\s.\+\-\*x×÷/()]+$", re.IGNORECASE)
_HAS_OPERATOR_RE = re.compile(r"[+\-*x×÷/]", re.IGNORECASE)


def _safe_eval_arithmetic(expr: str) -> Optional[float]:
    import ast
    import operator

    allowed_ops = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
                   ast.Div: operator.truediv, ast.USub: operator.neg, ast.UAdd: operator.pos}

    def _eval(node):
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in allowed_ops:
            return allowed_ops[type(node.op)](_eval(node.left), _eval(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in allowed_ops:
            return allowed_ops[type(node.op)](_eval(node.operand))
        raise ValueError("unsupported expression")

    try:
        normalized = expr.replace("x", "*").replace("×", "*").replace("÷", "/").lower()
        tree = ast.parse(normalized, mode="eval")
        return _eval(tree)
    except Exception:
        return None


def resolve_bare_math_expression(text: str, history: Optional[List[Dict]]) -> Optional[Dict]:
    """Evaluates `text` as a literal arithmetic expression ONLY when no
    active slot-filling flow exists (Part "Data Entry vs Math" — the
    exact same "4*6" text must resolve completely differently depending
    on conversation state). Returns {"expression": str, "value": float}
    or None when there's an active flow, the text isn't a safe bare
    arithmetic expression, or it has no operator at all (a single bare
    number alone is never treated as "a math question")."""
    if detect_active_flow(history):
        return None
    normalized = (text or "").strip()
    if not normalized or not _SAFE_ARITHMETIC_CHARS_RE.match(normalized) or not _HAS_OPERATOR_RE.search(normalized):
        return None
    value = _safe_eval_arithmetic(normalized)
    if value is None:
        return None
    return {"expression": normalized, "value": value}


def resolve_slot_filling_turn(text: str, history: Optional[List[Dict]]) -> Optional[Dict]:
    """The core Active Slot-Filling Flow entry point. Returns None when
    there is no active flow, the user cancelled it, explicitly asked for
    the expression to be computed as math, or named an unrelated new
    topic — in every such case the caller falls through to the next
    resolution priority. Otherwise returns:
        {
          "flow_complete": bool,
          "captured_slots": {...},          # cumulative, this turn included
          "newly_captured": {...},          # what THIS turn added
          "missing_slots": [str, ...],
          "message": str,                   # only when not flow_complete
        }
    """
    normalized = (text or "").strip()
    if is_cancellation(normalized):
        return None

    flow = detect_active_flow(history)
    if not flow:
        return None

    if is_explicit_math_intent(normalized):
        return None  # explicit arithmetic request — let the normal calculation path handle it

    parsed = parse_dimension_input(normalized)
    if not parsed["dimension_values"] and parsed["weight"] is None:
        return None  # nothing slot-shaped in this reply — not for this resolver to handle

    before = flow["captured_slots_before"]
    after = _merge_captured(before, parsed)
    missing = _missing_slots(after)
    newly_captured_public = _captured_to_public(_merge_captured({}, parsed))

    if not missing:
        return {"flow_complete": True, "captured_slots": _captured_to_public(after),
                "newly_captured": newly_captured_public, "missing_slots": [], "message": None}

    message = build_missing_slot_message(newly_captured_public, missing)
    return {"flow_complete": False, "captured_slots": _captured_to_public(after),
            "newly_captured": newly_captured_public, "missing_slots": missing, "message": message}
