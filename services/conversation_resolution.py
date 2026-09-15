# -*- coding: utf-8 -*-
"""SYSTEM-WIDE CONVERSATION INTELLIGENCE — P1 canonical resolver.

ONE place that composes the already-trusted interpretation capabilities
(``services/conversation_semantics.py``, ``services/link_conversion_flow
.py``, ``services/request_grounding_classifier.py``,
``services/thai_text_normalizer.py``) into a single structured
``ConversationResolution``.

P1 is SHADOW-FIRST and ADDITIVE:
  * no DB / schema change (P2, not approved);
  * no existing branch deleted;
  * ``DecisionEngine.decide()`` builds this ONCE per turn and logs it;
  * only low-risk *conversational* decisions (conversation act, explicit
    topic switch, correction, requested-slot compatibility, active-
    journey identity) may read it — auth / ERP truth / business policy /
    money / confirmed execution are untouched.

Nothing here calls an LLM directly: it reuses
``conversation_semantics.interpret`` (whose gated LLM disambiguation
already degrades to the deterministic tier) and otherwise deterministic
helpers. ``evidence`` carries only structured signals — never prose,
never chain-of-thought.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Any

from services.conversation_semantics import (
    interpret as _interpret,
    derive_active_frame as _derive_active_frame,
    resolve_frame_correction as _resolve_frame_correction,
    is_frame_followup as _is_frame_followup,
    _followup_op as _followup_op,
    _assistant_asked_for_product as _assistant_asked_for_product,
    _method_label as _method_label,
    _bare_product_noun as _bare_product_noun,
    _COUNT_UNIT_ALT as _CS_COUNT_UNIT_ALT,
    _HIGH_RISK_PRODUCT_RE as _HIGH_RISK_PRODUCT_RE,
    Frame as _Frame,
)

# ── vocabularies ─────────────────────────────────────────────────────
CONVERSATION_ACTS = (
    "ANSWER", "CORRECTION", "CONFIRMATION", "REJECTION", "TOPIC_SWITCH",
    "NEW_INTENT", "QUESTION", "GENERAL_CHAT", "UNKNOWN",
)
GROUNDING_REQUIREMENTS = (
    "CONVERSATION", "GENERAL", "BUSINESS_RAG", "PRIVATE_ERP",
    "CALCULATOR", "WORKFLOW", "MIXED",
)
PRECEDENCE_WINNERS = (
    "NEW_INTENT", "CORRECTION_REJECTION_TOPIC", "REQUESTED_SLOT_ANSWER",
    "ACTIVE_JOURNEY", "PENDING_WORKFLOW", "STALE_HISTORY", "NONE",
)
EXECUTION_HINTS = (
    "link_conversion", "calculator", "withdrawal", "operational_change",
    "business_action", "frame_correction", "frame_reject", "frame_clarify",
    "import_discovery", "service_intent", "rag", "private_erp", "none",
)

# journeys that a fresh explicit opener can (re)establish
_JOURNEY_FAMILIES = frozenset({"IMPORT_INTEREST"})

# explicit families that always win over an active journey (a topic
# switch); mirrors decision_engine's _F5_EXPLICIT plus the private ones.
_EXPLICIT_OVER_JOURNEY = frozenset({
    "CONTACT_INFO", "ADDRESS_CHANGE", "LINK_CONVERSION", "SHIPMENT_STATUS",
    "PURCHASE_WITHDRAWAL", "SHIPPING_WITHDRAWAL", "MY_COUPONS", "INVOICE",
    "PICKUP_LOCATION", "SELF_PICKUP", "COUPON_USAGE", "CHARTER_TRUCK",
    "SERVICE_DISCOVERY", "HELP_INTENT", "MONEY_TRANSFER_INTEREST",
    "WEBSITE_LINK_REQUEST", "WAREHOUSE_INBOUND_JOURNEY", "SHIPPING_ESTIMATE",
    "PRODUCT_POLICY",
})

_PRIVATE_FAMILIES = frozenset({"SHIPMENT_STATUS", "INVOICE", "MY_COUPONS", "ADDRESS_CHANGE"})
_BUSINESS_RAG_FAMILIES = frozenset({
    "PICKUP_LOCATION", "SELF_PICKUP", "COUPON_USAGE", "CHARTER_TRUCK",
    "PRODUCT_POLICY",
})
_CONVERSATION_FAMILIES = frozenset({
    "IMPORT_INTEREST", "HELP_INTENT", "SERVICE_DISCOVERY",
    "MONEY_TRANSFER_INTEREST", "WEBSITE_LINK_REQUEST", "CONTACT_INFO",
    "WAREHOUSE_INBOUND_JOURNEY",
})

_CONFIRM_RE = re.compile(
    r"^\s*(?:ใช่|ใช่ค่ะ|ใช่ครับ|ตกลง|โอเค|โอเคค่ะ|ok(?:ay)?|ยืนยัน|เอาเลย|จัดไป|ได้เลย|"
    r"ถูกต้อง|ถูกแล้ว|เรียบร้อย|ครับผม|ค่ะ|ตามนั้น)\s*(?:ค่ะ|ครับ|คับ|นะ)?\s*$",
    re.IGNORECASE)
_GREET_CHAT_RE = re.compile(
    r"^\s*(?:สวัสดี|หวัดดี|ดีค่ะ|ดีครับ|hello|hi|hey|ขอบคุณ|ขอบใจ|thank|thx|"
    r"โอเค|อ๋อ|อือ|ได้ค่ะ|ได้ครับ|555+|ฮ่าๆ*)\S*\s*(?:ค่ะ|ครับ|คับ|นะ|จ้า|จ้ะ)?\s*$",
    re.IGNORECASE)
_QUESTION_TAIL_RE = re.compile(r"(?:ไหม|มั้ย|มัย|หรือเปล่า|รึเปล่า|หรือไม่|ยังไง|อย่างไร|เท่าไหร่|กี่\S|\?)\s*$")

# ── multi-entity extraction ─────────────────────────────────────────
# COUNT units (a bare weight is NOT a quantity).
_COUNT_UNIT = tuple(
    # PHASE 6 closure gate H — derived from the ONE shared vocabulary in
    # services/conversation_semantics.py rather than re-listed here, so
    # P1 can no longer drift out of sync with the legacy runtime (it had:
    # "ขวด"/"พาเลท" were missing and a bottle quantity was invisible to
    # the canonical resolver). "pcs?" is a regex alternative, expanded.
    u for alt in _CS_COUNT_UNIT_ALT.split("|")
    for u in (("pcs", "pc") if alt == "pcs?" else (alt,))
)
_WEIGHT_UNIT = {
    "กก": ("kg", 1.0), "กก.": ("kg", 1.0), "กิโล": ("kg", 1.0), "กิโลกรัม": ("kg", 1.0),
    "โล": ("kg", 1.0), "kg": ("kg", 1.0), "ตัน": ("kg", 1000.0),
    "กรัม": ("kg", 0.001), "g": ("kg", 0.001), "ขีด": ("kg", 0.1),
}
_DIM_UNIT = {
    "cm": ("cm", 1.0), "ซม": ("cm", 1.0), "ซม.": ("cm", 1.0), "เซน": ("cm", 1.0),
    "เซนติเมตร": ("cm", 1.0), "mm": ("cm", 0.1), "มม": ("cm", 0.1), "มม.": ("cm", 0.1),
    "m": ("cm", 100.0), "เมตร": ("cm", 100.0), "นิ้ว": ("cm", 2.54), "inch": ("cm", 2.54),
}
# Thai has no word spaces, so a trailing \b / [^ก-๙] boundary would
# reject "12 ตัวจาก" — anchor on the number and unit only.
_QTY_RE = re.compile(
    r"(?<![\d.])(\d{1,7})\s*(" + "|".join(sorted(_COUNT_UNIT, key=len, reverse=True)) + r")",
    re.IGNORECASE)
_WEIGHT_RE = re.compile(
    r"(?<![\d.])(\d{1,6}(?:\.\d{1,3})?)\s*(" + "|".join(sorted(_WEIGHT_UNIT, key=len, reverse=True)) + r")(?!\d)",
    re.IGNORECASE)
_DIM_TRIPLE_RE = re.compile(
    r"(\d{1,5}(?:\.\d+)?)\s*[x×*]\s*(\d{1,5}(?:\.\d+)?)\s*[x×*]\s*(\d{1,5}(?:\.\d+)?)\s*"
    r"(cm|mm|ซม\.?|มม\.?|m|เมตร|นิ้ว|inch)?",
    re.IGNORECASE)
_METHOD_RE = re.compile(r"ทางเรือ|ทางรถ|ทางอากาศ|ทางเครื่องบิน|โดยเรือ|โดยรถ|โดยเครื่องบิน|by\s*sea|by\s*air|by\s*road",
                        re.IGNORECASE)
_PLATFORM_RE = re.compile(r"1688|taobao|เถาเป่า|เถาเป่า|tmall|ทีมอลล์|อาลีบาบา|alibaba|จีน", re.IGNORECASE)
_ORDER_VERB_RE = re.compile(r"(?:อยาก|จะ|ต้องการ|สนใจ)?\s*(?:สั่งซื้อ|สั่ง|ซื้อ|หาซื้อ|นำเข้า|ช้อป)\s*", re.IGNORECASE)
_BRAND_RE = re.compile(r"(?<![A-Za-z])(SP|FT)(?![A-Za-z])|เอสพี|เอฟที", re.IGNORECASE)
_TH_BRAND = {"เอสพี": "SP", "เอฟที": "FT"}


@dataclass
class SlotValue:
    value: Any
    unit: Optional[str] = None
    canonical_value: Any = None
    canonical_unit: Optional[str] = None
    raw: str = ""

    def as_dict(self) -> Dict:
        d = asdict(self)
        if d["canonical_value"] is None:
            d["canonical_value"] = d["value"]
        if d["canonical_unit"] is None:
            d["canonical_unit"] = d["unit"]
        return d


def _slot(value, unit=None, canonical_value=None, canonical_unit=None, raw=""):
    return SlotValue(value=value, unit=unit,
                     canonical_value=canonical_value if canonical_value is not None else value,
                     canonical_unit=canonical_unit if canonical_unit is not None else unit,
                     raw=raw or (f"{value} {unit}".strip() if unit else str(value)))


def extract_entities(text: str) -> Dict[str, Any]:
    """Deterministic multi-entity extraction from ONE turn. Conservative:
    only emits a slot when the shape is unambiguous. Never stops after the
    first hit."""
    t = (text or "").strip()
    out: Dict[str, Any] = {}
    if not t:
        return out

    # dimensions (triple) — most specific first
    md = _DIM_TRIPLE_RE.search(t)
    if md:
        raw = md.group(0).strip()
        u = (md.group(4) or "").lower().rstrip(".")
        cu, factor = _DIM_UNIT.get(u, ("cm", 1.0))
        vals = [float(md.group(i)) for i in (1, 2, 3)]
        can = [round(v * factor, 3) for v in vals]
        out["dimensions"] = SlotValue(
            value="x".join(str(int(v) if v == int(v) else v) for v in vals),
            unit=(u or None),
            canonical_value="x".join(str(int(c) if c == int(c) else c) for c in can),
            canonical_unit="cm", raw=raw)

    # weight
    mw = _WEIGHT_RE.search(t)
    if mw:
        v = float(mw.group(1)); u = mw.group(2).lower().rstrip(".")
        cu, factor = _WEIGHT_UNIT.get(u, ("kg", 1.0))
        out["weight"] = _slot(int(v) if v == int(v) else v, u,
                              round(v * factor, 4), "kg", mw.group(0).strip())

    # quantity (count units only)
    mq = _QTY_RE.search(t)
    if mq:
        out["quantity"] = _slot(int(mq.group(1)), mq.group(2), int(mq.group(1)),
                                mq.group(2), mq.group(0).strip())
    elif re.fullmatch(r"\s*(?:ประมาณ\s*)?\d{1,7}\s*", t):
        out["quantity"] = _slot(int(re.search(r"\d+", t).group(0)), None, raw=t.strip())

    # shipping method
    mm = _METHOD_RE.search(t)
    if mm:
        lab = _method_label(mm.group(0)) or (
            "sea" if "เรือ" in mm.group(0) or "sea" in mm.group(0).lower()
            else "road" if "รถ" in mm.group(0) or "road" in mm.group(0).lower()
            else "air" if "อากาศ" in mm.group(0) or "บิน" in mm.group(0) or "air" in mm.group(0).lower()
            else None)
        if lab:
            out["shipping_method"] = _slot(lab, None, raw=mm.group(0).strip())

    # platform
    mp = _PLATFORM_RE.search(t)
    if mp and mp.group(0).lower() != "จีน":
        out["platform"] = _slot(mp.group(0).lower(), raw=mp.group(0))

    # brand
    mb = _BRAND_RE.search(t)
    if mb:
        b = (mb.group(1) or mb.group(0)).lower()
        out["brand"] = _slot(_TH_BRAND.get(b, b.upper()), raw=mb.group(0))

    # product noun — after an order verb, before the first quantity /
    # platform / method marker. Only when an order verb is present so a
    # bare "20 คู่" never yields a product.
    mv = _ORDER_VERB_RE.search(t)
    if mv:
        seg = t[mv.end():]
        for stopper in (_QTY_RE, _WEIGHT_RE, _METHOD_RE,
                        re.compile(r"จาก|1688|taobao|tmall|จีน|ได้ไหม|ได้มั้ย", re.I)):
            ms = stopper.search(seg)
            if ms:
                seg = seg[:ms.start()]
        noun = _bare_product_noun(seg)
        if _valid_product_noun(noun):
            out["product"] = _slot(noun, raw=noun)

    return out


_PRODUCT_NOUN_VETO = re.compile(
    r"^(?:ได้|ไหม|มั้ย|มัย|จาก|อะไร|ยังไง|เท่าไหร่|กี่|นะ|ค่ะ|ครับ|แล้ว|"
    r"ที่อยู่|ปลายทาง|ผู้รับ|เบอร์|บิล|ออเดอร์)")


def _valid_product_noun(noun: Optional[str]) -> bool:
    return bool(noun and 2 <= len(noun) <= 26
               and not any(ch.isdigit() for ch in noun)
               and not _PRODUCT_NOUN_VETO.match(noun))


@dataclass
class ConversationResolution:
    raw_text: str = ""
    normalized_text: str = ""
    conversation_act: str = "UNKNOWN"
    primary_intent: str = "UNKNOWN"
    active_journey: Optional[str] = None
    topic_switch: Optional[str] = None
    entities: Dict[str, Any] = field(default_factory=dict)
    slot_updates: Dict[str, Any] = field(default_factory=dict)
    slot_corrections: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    known_slots: Dict[str, Any] = field(default_factory=dict)
    requested_slot: Optional[str] = None
    missing_slots: List[str] = field(default_factory=list)
    grounding_requirement: str = "GENERAL"
    execution_hint: str = "none"
    confidence: float = 0.0
    precedence_winner: str = "NONE"
    evidence: List[Dict] = field(default_factory=list)
    # raw sub-results kept for the (safe) decision-engine consumers so
    # they never recompute — NOT part of the public contract.
    frame_correction: Dict = field(default_factory=dict)
    is_in_frame_followup: bool = False
    semantic_family: str = "UNKNOWN"

    def as_dict(self) -> Dict:
        return {
            "conversation_act": self.conversation_act,
            "primary_intent": self.primary_intent,
            "active_journey": self.active_journey,
            "topic_switch": self.topic_switch,
            "entities": {k: (v.as_dict() if isinstance(v, SlotValue) else v)
                         for k, v in self.entities.items()},
            "slot_updates": {k: (v.as_dict() if isinstance(v, SlotValue) else v)
                             for k, v in self.slot_updates.items()},
            "slot_corrections": self.slot_corrections,
            "known_slots": {k: (v.as_dict() if isinstance(v, SlotValue) else v)
                            for k, v in self.known_slots.items()},
            "requested_slot": self.requested_slot,
            "missing_slots": self.missing_slots,
            "grounding_requirement": self.grounding_requirement,
            "execution_hint": self.execution_hint,
            "confidence": round(self.confidence, 2),
            "precedence_winner": self.precedence_winner,
            "evidence": self.evidence,
        }


# ── requested-slot inference ────────────────────────────────────────
_ASK_SLOT_RES = [
    ("quantity", re.compile(r"แจ้งจำนวน|จำนวน\S{0,4}(?:เท่าไหร่|กี่|โดยประมาณ)|กี่(?:ชิ้น|ตัว|คู่|อัน)")),
    ("shipping_method", re.compile(r"ทางรถหรือทางเรือ|ส่งทางไหน|วิธี(?:การ)?จัดส่ง|ขนส่งทางไหน|ทางรถหรือเรือ")),
    ("weight", re.compile(r"แจ้งน้ำหนัก|น้ำหนัก\S{0,4}(?:เท่าไหร่|กี่|โดยประมาณ)|หนักเท่าไหร่")),
    ("dimensions", re.compile(r"ขนาด\S{0,4}(?:เท่าไหร่|กี่|กว้าง|ยาว|สูง)|กว้างยาวสูง|แจ้งขนาด")),
    ("product", re.compile(r"สินค้า\S{0,4}(?:อะไร|ชนิดไหน|ประเภทไหน)|อยากสั่งสินค้าอะไร|นำเข้าอะไร")),
]


def _requested_slot(history: Optional[List[Dict]]) -> Optional[str]:
    for turn in reversed(list(history or [])[-3:]):
        if turn.get("role") != "assistant":
            if turn.get("role") == "user":
                return None
            continue
        c = turn.get("content") or ""
        for slot, rx in _ASK_SLOT_RES:
            if rx.search(c):
                return slot
        return None
    return None


def _frame_known_slots(frame: Optional[_Frame]) -> Dict[str, Any]:
    if not frame:
        return {}
    ks: Dict[str, Any] = {}
    if frame.product:
        ks["product"] = _slot(frame.product, raw=frame.product)
    if frame.quantity:
        # PHASE 6 POST-DEPLOY (defect class C) — the legacy Frame now
        # carries the supplied count unit, so P1's typed SlotValue no
        # longer loses it here (this was the one place where a unit that
        # HAD been parsed silently became None on the way into the
        # structured resolution).
        _fu = getattr(frame, "unit", None)
        ks["quantity"] = _slot(frame.quantity, _fu,
                               raw=(f"{frame.quantity} {_fu}".strip() if _fu
                                    else str(frame.quantity)))
    if frame.method:
        ks["shipping_method"] = _slot(frame.method, raw=frame.method)
    if getattr(frame, "weight", None):
        _wu = getattr(frame, "weight_unit", None) or "kg"
        ks["weight"] = _slot(frame.weight, _wu, raw=f"{frame.weight} {_wu}".strip())
    if getattr(frame, "dimensions", None):
        ks["dimensions"] = _slot(frame.dimensions, raw=str(frame.dimensions))
    return ks


_IMPORT_SLOT_ORDER = ("product", "quantity", "shipping_method")


def _grounding_requirement(fam: str, gc_cls: str) -> str:
    if fam in _PRIVATE_FAMILIES or gc_cls == "PRIVATE_OR_ERP_REQUIRED":
        return "PRIVATE_ERP"
    if fam == "SHIPPING_ESTIMATE" or gc_cls == "CALCULATION":
        return "CALCULATOR"
    if fam in ("PURCHASE_WITHDRAWAL", "SHIPPING_WITHDRAWAL", "ADDRESS_CHANGE"):
        return "WORKFLOW"
    if fam in _CONVERSATION_FAMILIES:
        return "CONVERSATION"
    if fam in _BUSINESS_RAG_FAMILIES or gc_cls == "BUSINESS_TRUTH_REQUIRED":
        return "BUSINESS_RAG"
    if gc_cls in ("GENERAL_ASSISTANCE", "GENERAL"):
        return "GENERAL"
    if gc_cls == "MIXED":
        return "MIXED"
    return "GENERAL"


def resolve_precedence(*, has_new_intent: bool, has_correction_reject_topic: bool,
                       answers_requested_slot: bool, has_active_journey: bool,
                       has_pending_workflow: bool) -> str:
    """The ONE explicit precedence order (task §3). Returns the winning
    tier; callers map it to behaviour. Nothing here depends on
    decision_engine branch ordering."""
    if has_new_intent:
        return "NEW_INTENT"
    if has_correction_reject_topic:
        return "CORRECTION_REJECTION_TOPIC"
    if answers_requested_slot:
        return "REQUESTED_SLOT_ANSWER"
    if has_active_journey:
        return "ACTIVE_JOURNEY"
    if has_pending_workflow:
        return "PENDING_WORKFLOW"
    return "STALE_HISTORY"


def resolve_conversation(message: str, history: Optional[List[Dict]] = None,
                         context: Optional[Dict] = None, *,
                         semantic: Optional[object] = None,
                         normalized_text: Optional[str] = None,
                         has_pending_workflow: bool = False) -> ConversationResolution:
    """Compose the trusted capabilities into ONE ConversationResolution.
    ``semantic`` / ``normalized_text`` may be passed in when the caller
    (DecisionEngine.decide) already computed them, so nothing is done
    twice."""
    history = list(history or [])
    context = context or {}
    raw = message or ""
    norm = normalized_text if normalized_text is not None else raw
    ev: List[Dict] = []

    sem = semantic if semantic is not None else _interpret(norm, history, context)
    fam = getattr(sem, "intent_family", "UNKNOWN")
    op = getattr(sem, "follow_up_op", "NONE")
    conv_act_sem = getattr(sem, "conversation_act", "NONE")
    is_priv = bool(getattr(sem, "is_private", False))
    src = getattr(sem, "source", "none")
    conf = float(getattr(sem, "confidence", 0.0) or 0.0)
    ev.append({"kind": "llm_semantic_signal", "family": fam, "op": op,
               "act": conv_act_sem, "source": src, "confidence": round(conf, 2)})

    frame = _derive_active_frame(history)
    active_journey = "IMPORT_INTEREST" if (frame and frame.product) else None
    if frame:
        ev.append({"kind": "frame_signal", "product": frame.product,
                   "quantity": frame.quantity, "unit": getattr(frame, "unit", None),
                   "method": frame.method,
                   "weight": getattr(frame, "weight", None),
                   "weight_unit": getattr(frame, "weight_unit", None)})

    fc = _resolve_frame_correction(norm, frame) if (frame and frame.product) else {
        "op": "UNKNOWN", "product": None, "quantity": None, "method": None, "brand": None}
    if fc.get("op") not in (None, "UNKNOWN"):
        ev.append({"kind": "frame_correction_signal", **{k: fc[k] for k in fc}})

    in_ffup = _is_frame_followup(norm)
    if in_ffup:
        ev.append({"kind": "regex_signal", "name": "frame_followup_shape"})

    ent_raw = extract_entities(norm)
    for k in ent_raw:
        ev.append({"kind": "regex_signal", "name": f"entity:{k}"})
    # merge in the semantic layer's own entities (product noun etc.)
    for k, v in (getattr(sem, "entities", {}) or {}).items():
        # THAI-HUMAN-LANGUAGE — the central interpreter's product noun WINS
        # over this module's own order-verb regex read: the interpreter's
        # extractor is the one that strips quantity qualifiers, method
        # verbs and service filler ("เสื้อผ้าประมาณ 100 ตัว" -> "เสื้อผ้า",
        # not "เสื้อผ้าประมาณ"). The regex read stays only as a fallback for
        # turns where the interpreter named no product at all.
        if k == "product" and isinstance(v, str) and _valid_product_noun(v.strip()):
            ent_raw["product"] = _slot(v.strip(), raw=v.strip())
        elif k == "method" and "shipping_method" not in ent_raw and v:
            _mv = v if v in ("road", "sea", "air") else _method_label(v)
            if _mv:
                ent_raw["shipping_method"] = _slot(_mv, raw=str(v))
        elif k in ("url", "identifier", "platform") and k not in ent_raw and v:
            ent_raw[k] = _slot(v, raw=str(v))
        elif k in ("question_span", "question_kind", "quantity_unit", "quantity_raw") \
                and k not in ent_raw and v:
            # LANGGRAPH UPGRADE — the separated question clause and the
            # typed quantity's unit are FACTS OF THE TURN. They were being
            # dropped here, so the canonical resolution (and therefore the
            # agent graph, which consumes only the resolution) could not
            # see that the customer had also asked about price. Forwarded
            # verbatim, never re-derived.
            ent_raw[k] = v

    # OWNER P1 — multi-fact fallback: the semantic layer alone could not
    # name a family, but the turn carries an order verb + a China/platform
    # marker (or an extracted product) -> it IS import interest (§7/§8).
    _order_verb = bool(_ORDER_VERB_RE.search(norm))
    _china = bool(_PLATFORM_RE.search(norm))
    if fam in ("UNKNOWN", "GENERAL") and (
            (_order_verb and (_china or "product" in ent_raw))
            or (_china and (_order_verb or "quantity" in ent_raw))):
        fam = "IMPORT_INTEREST"
        conf = max(conf, 0.6)
        ev.append({"kind": "regex_signal", "name": "multi_fact_import_interest"})
        # a "10 ตัว โต๊ะ จากจีน" shape carries the product noun AFTER the
        # quantity with no order verb — recover it from the residue.
        if "product" not in ent_raw and "quantity" in ent_raw:
            _resid = _QTY_RE.sub(" ", _PLATFORM_RE.sub(" ", _METHOD_RE.sub(" ", norm)))
            _resid = re.sub(r"จาก|จีน|ประเทศ", " ", _resid)
            _pn = _bare_product_noun(_resid.strip())
            if _valid_product_noun(_pn):
                ent_raw["product"] = _slot(_pn, raw=_pn)
                ev.append({"kind": "regex_signal", "name": "entity:product(residue)"})

    req_slot = _requested_slot(history)
    if req_slot:
        ev.append({"kind": "requested_slot_match", "slot": req_slot,
                   "supplied": req_slot in ent_raw})

    known = _frame_known_slots(frame)

    # ── grounding ──
    try:
        from services.request_grounding_classifier import classify_request_grounding
        gc = classify_request_grounding(norm, sem, history)
        gc_cls = gc.cls
    except Exception:
        gc_cls = "UNCLEAR"
    grounding = _grounding_requirement(fam, gc_cls)

    # ── slot updates / corrections ──
    slot_updates: Dict[str, Any] = {}
    slot_corrections: Dict[str, Dict[str, Any]] = {}
    if fc.get("op") == "CHANGE_TARGET" and fc.get("product"):
        slot_corrections["product"] = {"old": (frame.product if frame else None), "new": fc["product"]}
    elif fc.get("op") in ("CORRECT_QUANTITY",) and fc.get("quantity") is not None:
        # keep the unit the customer gave this turn ("เอ้ย 30 กล่อง") —
        # the deterministic fc drops it, extract_entities kept it.
        _eq = ent_raw.get("quantity")
        _unit = _eq.unit if (isinstance(_eq, SlotValue) and _eq.value == fc["quantity"]) else None
        slot_corrections["quantity"] = {"old": (frame.quantity if frame else None),
                                        "new": fc["quantity"], "unit": _unit,
                                        "raw": (_eq.raw if isinstance(_eq, SlotValue) else str(fc["quantity"]))}
    elif fc.get("op") == "CHANGE_METHOD" and fc.get("method"):
        slot_corrections["shipping_method"] = {"old": (frame.method if frame else None), "new": fc["method"]}
    elif fc.get("op") == "CHANGE_BRAND" and fc.get("brand"):
        slot_corrections["brand"] = {"old": None, "new": fc["brand"]}
    elif fc.get("op") == "SET_QUANTITY" and fc.get("quantity") is not None:
        # keep the unit the customer actually gave ("20 คู่") — the
        # deterministic _bareq that produced fc drops it, extract_entities
        # kept it.
        _eq = ent_raw.get("quantity")
        if isinstance(_eq, SlotValue) and _eq.value == fc["quantity"]:
            slot_updates["quantity"] = _eq
        else:
            slot_updates["quantity"] = _slot(fc["quantity"], None, raw=str(fc["quantity"]))
    # entity-derived fresh fills — what THIS turn actually supplied.
    # ONLY inside (or opening) an import journey, so a PRODUCT_POLICY /
    # SHIPMENT_STATUS / calculator turn's incidental numbers never look
    # like journey slot fills. NOT gated on `known` (which is the
    # legacy-derived frame and may be polluted) — a slot the turn
    # supplies is an update even if the legacy frame already guessed one.
    if fam == "IMPORT_INTEREST" or active_journey == "IMPORT_INTEREST":
        for k, v in ent_raw.items():
            if k in ("platform", "url", "identifier", "brand"):
                continue
            if k not in slot_corrections:
                slot_updates.setdefault(k, v)

    # ── conversation act ──
    act = "UNKNOWN"
    if fc.get("op") == "REJECT" or _GREET_CHAT_RE.match(norm) is None and conv_act_sem == "REJECT" and op != "CORRECTION":
        act = "REJECTION" if fc.get("op") == "REJECT" else "CORRECTION"
    if fc.get("op") == "REJECT":
        act = "REJECTION"
    elif fc.get("op") == "AMBIGUOUS":
        act = "QUESTION"
    elif slot_corrections or op == "CORRECTION":
        act = "CORRECTION"
    elif _CONFIRM_RE.match(norm):
        act = "CONFIRMATION"
    elif active_journey and (fam in _EXPLICIT_OVER_JOURNEY or op == "TOPIC_CHANGE"):
        act = "TOPIC_SWITCH"
    elif req_slot and (req_slot in ent_raw or (req_slot == "product" and "product" in ent_raw)):
        act = "ANSWER"
    elif fam not in ("UNKNOWN", "GENERAL") and fam not in _EXPLICIT_OVER_JOURNEY | _JOURNEY_FAMILIES and _QUESTION_TAIL_RE.search(norm):
        act = "QUESTION"
    elif fam in _JOURNEY_FAMILIES or fam in _EXPLICIT_OVER_JOURNEY:
        act = "NEW_INTENT"
    elif _GREET_CHAT_RE.match(norm):
        act = "GENERAL_CHAT"
    elif _QUESTION_TAIL_RE.search(norm):
        act = "QUESTION"
    elif fam == "GENERAL":
        act = "GENERAL_CHAT"
    else:
        act = "UNKNOWN"

    # topic switch target
    topic_switch = fam if act == "TOPIC_SWITCH" else None

    # ── precedence ──
    # a fresh explicit opener ONLY — a bare slot ANSWER to the assistant's
    # own question (act == ANSWER) is precedence tier 3, never tier 1,
    # even though its family is IMPORT_INTEREST.
    has_new_intent = act == "NEW_INTENT"
    has_crt = act in ("CORRECTION", "REJECTION", "TOPIC_SWITCH")
    answers_req = act == "ANSWER"
    winner = resolve_precedence(
        has_new_intent=has_new_intent,
        has_correction_reject_topic=has_crt,
        answers_requested_slot=answers_req,
        has_active_journey=bool(active_journey),
        has_pending_workflow=has_pending_workflow)

    # ── execution hint (advisory only in P1) ──
    if ent_raw.get("url") or fam == "LINK_CONVERSION":
        hint = "link_conversion"
    elif fc.get("op") == "REJECT":
        hint = "frame_reject"
    elif fc.get("op") == "AMBIGUOUS":
        hint = "frame_clarify"
    elif slot_corrections and active_journey:
        hint = "frame_correction"
    elif fam == "SHIPPING_ESTIMATE" or grounding == "CALCULATOR":
        hint = "calculator"
    elif fam in ("PURCHASE_WITHDRAWAL", "SHIPPING_WITHDRAWAL"):
        hint = "withdrawal"
    elif fam == "ADDRESS_CHANGE":
        hint = "operational_change"
    elif fam == "IMPORT_INTEREST" and not active_journey:
        hint = "import_discovery"
    elif fam in _CONVERSATION_FAMILIES:
        hint = "service_intent"
    elif grounding == "PRIVATE_ERP":
        hint = "private_erp"
    else:
        hint = "rag"

    # ── primary intent label ──
    if (slot_corrections or fc.get("op") in ("REJECT", "AMBIGUOUS")) and active_journey:
        primary = active_journey          # a correction/reject carries the journey's intent
    elif fam == "GENERAL" or (fam == "UNKNOWN" and grounding == "GENERAL"
                              and act in ("QUESTION", "GENERAL_CHAT")):
        primary = "GENERAL_ASSISTANCE"
    else:
        primary = fam

    # ── missing slots (import journey only) ──
    missing: List[str] = []
    if active_journey == "IMPORT_INTEREST" or fam == "IMPORT_INTEREST":
        filled = set(known) | set(slot_updates) | set(slot_corrections)
        missing = [s for s in _IMPORT_SLOT_ORDER if s not in filled]

    return ConversationResolution(
        raw_text=raw, normalized_text=norm,
        conversation_act=act, primary_intent=primary,
        active_journey=active_journey, topic_switch=topic_switch,
        entities=ent_raw, slot_updates=slot_updates, slot_corrections=slot_corrections,
        known_slots=known, requested_slot=req_slot, missing_slots=missing,
        grounding_requirement=grounding, execution_hint=hint,
        confidence=round(conf, 2), precedence_winner=winner, evidence=ev,
        frame_correction=fc, is_in_frame_followup=in_ffup, semantic_family=fam,
    )
