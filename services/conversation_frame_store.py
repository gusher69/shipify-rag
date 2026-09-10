# -*- coding: utf-8 -*-
"""SYSTEM-WIDE CONVERSATION INTELLIGENCE — P2 structured frame (SHADOW-WRITE).

Turns the P1 canonical ``ConversationResolution`` into a structured,
persistent conversation frame — the additive replacement for parsing the
assistant's own previous reply text (``derive_active_frame``).

P2 STAGE = SHADOW-WRITE ONLY:
  * ``build_frame()`` computes the next structured frame from the previous
    one + this turn's resolution, following the P1 precedence order;
  * ``frame_parity()`` classifies it against the legacy text-derived
    frame so the cutover gate can be measured;
  * NOTHING here is a runtime authority. ``services/conversation_
    semantics.py::derive_active_frame`` stays the source of truth for
    routing/state until parity >= 99%.

No LLM. No business truth. No DB access (the store read/write lives in
``services/session_service.py``; this module is pure logic).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

FRAME_VERSION = 1
LIFECYCLE = ("ACTIVE", "SUSPENDED", "COMPLETED", "CANCELLED", "EXPIRED")
_JOURNEY_FAMILIES = frozenset({"IMPORT_INTEREST"})
# slots a structured import frame may carry (never forced to use all)
_IMPORT_SLOTS = ("product", "quantity", "shipping_method", "weight", "dimensions", "brand", "platform")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def empty_frame() -> Dict[str, Any]:
    return {"version": FRAME_VERSION, "journey": None, "status": None,
            "requested_slot": None, "slots": {}, "updated_at": _now(),
            "turn_seq": 0, "source": "conversation_resolution"}


def _slot_dict(v: Any) -> Dict[str, Any]:
    """Normalise a P1 SlotValue / SlotValue.as_dict() / scalar into the
    persisted slot shape. Never drops the raw semantic unit/value."""
    if isinstance(v, dict):
        d = dict(v)
        d.setdefault("value", d.get("canonical_value"))
        d.setdefault("canonical_value", d.get("value"))
        d.setdefault("unit", None)
        d.setdefault("canonical_unit", d.get("unit"))
        d.setdefault("raw", str(d.get("value")))
        return d
    if hasattr(v, "as_dict"):
        return _slot_dict(v.as_dict())
    return {"value": v, "unit": None, "canonical_value": v,
            "canonical_unit": None, "raw": str(v)}


def build_frame(prev: Optional[Dict[str, Any]], resolution: Any,
                *, turn_id: Optional[str] = None) -> Dict[str, Any]:
    """Next structured frame from the previous frame + this turn's P1
    resolution. Pure. Follows P1 precedence (task §5):
      1 explicit new intent      -> create/reset (journey) or suspend (other)
      2 correction/rejection/topic-> apply corrections / CANCELLED / SUSPENDED
      3 requested-slot answer     -> merge that slot
      4 active-journey continue   -> merge slot_updates
      5 pending workflow / 6 stale-> carry prev unchanged
    Never mutates ``prev``."""
    prev = dict(prev) if prev else empty_frame()
    prev_slots = dict(prev.get("slots") or {})
    frame = empty_frame()
    frame["turn_seq"] = int(prev.get("turn_seq") or 0) + 1
    if turn_id:
        frame["source_turn_id"] = turn_id

    act = getattr(resolution, "conversation_act", "UNKNOWN")
    intent = getattr(resolution, "primary_intent", "UNKNOWN")
    winner = getattr(resolution, "precedence_winner", "NONE")
    slot_updates = getattr(resolution, "slot_updates", {}) or {}
    slot_corrections = getattr(resolution, "slot_corrections", {}) or {}
    req_slot = getattr(resolution, "requested_slot", None)
    known = getattr(resolution, "known_slots", {}) or {}

    prev_journey = prev.get("journey")
    prev_status = prev.get("status")
    prev_active = prev_journey and prev_status in ("ACTIVE", "SUSPENDED")

    # ── 1. explicit NEW intent ──
    if winner == "NEW_INTENT" and intent in _JOURNEY_FAMILIES:
        frame["journey"] = intent
        frame["status"] = "ACTIVE"
        frame["slots"] = {k: _slot_dict(v) for k, v in slot_updates.items()
                          if k in _IMPORT_SLOTS}
        frame["requested_slot"] = req_slot
        frame["updated_at"] = _now()
        return frame

    if winner == "NEW_INTENT":
        # a fresh explicit NON-journey intent (CONTACT_INFO / status / …):
        # suspend the previous journey, never mutate its slots.
        if prev_active:
            frame.update(journey=prev_journey, status="SUSPENDED",
                         slots=prev_slots, requested_slot=prev.get("requested_slot"))
        frame["updated_at"] = _now()
        return frame

    # ── 2. correction / rejection / topic switch ──
    if act == "REJECTION":
        frame.update(journey=prev_journey or None,
                     status="CANCELLED" if prev_journey else None,
                     slots=prev_slots, requested_slot=None)
        frame["updated_at"] = _now()
        return frame

    if act == "TOPIC_SWITCH":
        if prev_active:
            frame.update(journey=prev_journey, status="SUSPENDED",
                         slots=prev_slots, requested_slot=prev.get("requested_slot"))
        frame["updated_at"] = _now()
        return frame

    if act == "CORRECTION" and prev_journey:
        merged = dict(prev_slots)
        for name, chg in slot_corrections.items():
            new_v = chg.get("new")
            if new_v is None:
                continue
            base = merged.get(name) or {}
            # unit precedence: the unit the customer gave THIS turn
            # ("เอ้ย 30 กล่อง") wins; otherwise keep the unit already on
            # file (a bare "เอ้ย 10" keeps คู่ from the earlier "20 คู่").
            unit = chg.get("unit") or base.get("unit")
            merged[name] = _slot_dict({
                "value": new_v, "unit": unit,
                "canonical_value": new_v, "canonical_unit": unit,
                "raw": (chg.get("raw") or (f"{new_v} {unit}".strip() if unit else str(new_v)))})
        for k, v in slot_updates.items():
            if k in _IMPORT_SLOTS:
                merged[k] = _slot_dict(v)
        frame.update(journey=prev_journey,
                     status="ACTIVE" if prev_status != "CANCELLED" else prev_status,
                     slots=merged, requested_slot=req_slot or prev.get("requested_slot"))
        frame["updated_at"] = _now()
        return frame

    # ── 3-4. requested-slot answer / active-journey continuation ──
    if prev_active and (act in ("ANSWER", "CONFIRMATION") or slot_updates):
        merged = dict(prev_slots)
        for k, v in slot_updates.items():
            if k in _IMPORT_SLOTS:
                merged[k] = _slot_dict(v)
        frame.update(journey=prev_journey, status="ACTIVE",
                     slots=merged, requested_slot=req_slot)
        frame["updated_at"] = _now()
        return frame

    # ── 5-6. pending / stale — carry prev unchanged (only refresh the ask) ──
    if prev_journey:
        frame.update(journey=prev_journey, status=prev_status, slots=prev_slots,
                     requested_slot=req_slot or prev.get("requested_slot"))
        frame["updated_at"] = prev.get("updated_at") or _now()
        frame["turn_seq"] = int(prev.get("turn_seq") or 0)  # no state change
        return frame

    # no journey anywhere — but still record a requested_slot if the
    # assistant just asked for one (an import discovery opener).
    if intent in _JOURNEY_FAMILIES or req_slot:
        frame.update(journey=(intent if intent in _JOURNEY_FAMILIES else None),
                     status=("ACTIVE" if intent in _JOURNEY_FAMILIES else None),
                     slots={k: _slot_dict(v) for k, v in slot_updates.items() if k in _IMPORT_SLOTS},
                     requested_slot=req_slot)
        frame["updated_at"] = _now()
        return frame

    return frame


# ── parity with the legacy text-derived frame ───────────────────────
def _legacy_dict(legacy) -> Dict[str, Any]:
    if legacy is None:
        return {}
    if isinstance(legacy, dict):
        return legacy
    return {"product": getattr(legacy, "product", None),
            "quantity": getattr(legacy, "quantity", None),
            "method": getattr(legacy, "method", None),
            "weight": getattr(legacy, "weight", None),
            "dimensions": getattr(legacy, "dimensions", None)}


def _sv(slots: Dict, name: str):
    s = (slots or {}).get(name) or {}
    return s.get("value") if isinstance(s, dict) else s


def frame_parity(legacy, structured: Optional[Dict[str, Any]],
                 resolution: Any = None) -> Dict[str, Any]:
    """Classify a structured frame against the legacy one.
    -> {"class": MATCH|STRUCTURED_IMPROVEMENT|LEGACY_CORRECT|STRUCTURED_WRONG|AMBIGUOUS,
        "reason": str, "unit_preserved": bool}

    ``resolution`` (the P1 ConversationResolution for this turn) is a
    tiebreaker: a slot the CURRENT turn corrected / answered is expected
    to differ from the legacy text-derived frame (legacy lags a turn), so
    that is a STRUCTURED_IMPROVEMENT, not a WRONG."""
    leg = _legacy_dict(legacy)
    st = structured or {}
    st_slots = st.get("slots") or {}
    st_status = st.get("status")

    _corr = set((getattr(resolution, "slot_corrections", {}) or {}).keys())
    _upd = set((getattr(resolution, "slot_updates", {}) or {}).keys())
    _act = getattr(resolution, "conversation_act", None)
    _just_touched = _corr | (_upd if _act in ("CORRECTION", "ANSWER") else set())

    # a fresh explicit opener resets the structured frame; a legacy frame
    # that still exposes an old product is the stale-journey-takeover P2
    # exists to prevent -> structured is the correct one.
    if _act == "NEW_INTENT" and (leg.get("product") or leg.get("quantity")):
        st_slots0 = st.get("slots") or {}
        if not st_slots0.get("product") or (
                _sv(st_slots0, "product") != leg.get("product")):
            return {"class": "STRUCTURED_IMPROVEMENT",
                    "reason": "fresh explicit opener; structured reset, legacy still stale",
                    "unit_preserved": True}
    # map structured slot names to the legacy field names
    _name_map = {"product": "product", "quantity": "quantity", "shipping_method": "method"}

    def _touched(legacy_field: str) -> bool:
        for st_name, lg in _name_map.items():
            if lg == legacy_field and st_name in _just_touched:
                return True
        return False

    def _leg_malformed(v) -> bool:
        return bool(v) and isinstance(v, str) and any(ch.isdigit() for ch in v)

    leg_prod, leg_qty, leg_meth = leg.get("product"), leg.get("quantity"), leg.get("method")
    st_prod = _sv(st_slots, "product")
    st_qty = _sv(st_slots, "quantity")
    st_meth = _sv(st_slots, "shipping_method")

    # normalise method casing (legacy "road"/"sea"/"air" vs structured same)
    def _m(x): return (str(x).lower() if x else None)
    leg_meth, st_meth = _m(leg_meth), _m(st_meth)

    unit_preserved = True
    q = (st_slots.get("quantity") or {})
    if st_qty is not None and isinstance(q, dict):
        unit_preserved = ("unit" in q)  # key present (may be None if user gave a bare number)

    # structured correctly ended a journey the legacy frame still shows
    if st_status in ("CANCELLED", "COMPLETED", "EXPIRED") and (leg_prod or leg_qty):
        return {"class": "STRUCTURED_IMPROVEMENT",
                "reason": f"structured status {st_status}; legacy still exposes slots",
                "unit_preserved": unit_preserved}
    if st_status == "SUSPENDED" and (leg_prod or leg_qty):
        return {"class": "STRUCTURED_IMPROVEMENT",
                "reason": "structured SUSPENDED on topic switch; legacy frame still live",
                "unit_preserved": unit_preserved}

    same_prod = (leg_prod or None) == (st_prod or None)
    same_qty = (leg_qty or None) == (st_qty or None)
    same_meth = leg_meth == st_meth

    if same_prod and same_qty and same_meth:
        # did structured also keep a unit legacy could never store?
        if st_qty is not None and isinstance(q, dict) and q.get("unit"):
            return {"class": "STRUCTURED_IMPROVEMENT",
                    "reason": f"quantity unit '{q.get('unit')}' preserved (legacy is unitless)",
                    "unit_preserved": True}
        return {"class": "MATCH", "reason": "product/quantity/method agree",
                "unit_preserved": unit_preserved}

    # one side has a slot the other lacks
    if not same_prod:
        if _touched("product"):
            return {"class": "STRUCTURED_IMPROVEMENT",
                    "reason": f"product just corrected/answered this turn -> {st_prod!r} (legacy lags: {leg_prod!r})",
                    "unit_preserved": unit_preserved}
        if _leg_malformed(leg_prod) and st_prod:
            return {"class": "STRUCTURED_IMPROVEMENT",
                    "reason": f"legacy product malformed ({leg_prod!r}); structured {st_prod!r}",
                    "unit_preserved": unit_preserved}
        if st_prod and not leg_prod:
            return {"class": "STRUCTURED_IMPROVEMENT", "reason": f"structured has product '{st_prod}', legacy none",
                    "unit_preserved": unit_preserved}
        if leg_prod and not st_prod:
            if _leg_malformed(leg_prod):
                return {"class": "STRUCTURED_IMPROVEMENT",
                        "reason": f"legacy product malformed ({leg_prod!r}); structured correctly none",
                        "unit_preserved": unit_preserved}
            return {"class": "LEGACY_CORRECT", "reason": f"legacy has product '{leg_prod}', structured none",
                    "unit_preserved": unit_preserved}
        return {"class": "STRUCTURED_WRONG", "reason": f"product mismatch legacy={leg_prod!r} structured={st_prod!r}",
                "unit_preserved": unit_preserved}
    if not same_qty:
        if _touched("quantity"):
            return {"class": "STRUCTURED_IMPROVEMENT",
                    "reason": f"quantity just corrected/answered -> {st_qty} (legacy lags: {leg_qty})",
                    "unit_preserved": unit_preserved}
        if st_qty is not None and leg_qty is None:
            return {"class": "STRUCTURED_IMPROVEMENT", "reason": f"structured quantity {st_qty}, legacy none",
                    "unit_preserved": unit_preserved}
        if leg_qty is not None and st_qty is None:
            return {"class": "LEGACY_CORRECT", "reason": f"legacy quantity {leg_qty}, structured none",
                    "unit_preserved": unit_preserved}
        return {"class": "STRUCTURED_WRONG", "reason": f"quantity mismatch legacy={leg_qty} structured={st_qty}",
                "unit_preserved": unit_preserved}
    if not same_meth:
        if _touched("method"):
            return {"class": "STRUCTURED_IMPROVEMENT",
                    "reason": f"method just corrected/answered -> {st_meth} (legacy lags: {leg_meth})",
                    "unit_preserved": unit_preserved}
        if st_meth and not leg_meth:
            return {"class": "STRUCTURED_IMPROVEMENT", "reason": f"structured method {st_meth}, legacy none",
                    "unit_preserved": unit_preserved}
        if leg_meth and not st_meth:
            return {"class": "LEGACY_CORRECT", "reason": f"legacy method {leg_meth}, structured none",
                    "unit_preserved": unit_preserved}
        return {"class": "STRUCTURED_WRONG", "reason": f"method mismatch legacy={leg_meth} structured={st_meth}",
                "unit_preserved": unit_preserved}

    return {"class": "AMBIGUOUS", "reason": "no decisive signal", "unit_preserved": unit_preserved}
