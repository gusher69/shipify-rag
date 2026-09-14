# -*- coding: utf-8 -*-
"""REMEMBER — merge conversation state, then apply the precedence order.

This pair of nodes is the answer to the customer's actual complaint:

    "เวลาเปลี่ยนคำ เปลี่ยนบริบท AI ตอบไม่ได้ ทั้งที่ความหมายเดียวกัน"

The bot must reason about conversation STATE, not only the latest phrase.
"""
from __future__ import annotations

from typing import Any, Dict

from services.agent.adapters import existing_engine as engine

# which slots each journey needs, in the order they should be asked for.
_JOURNEY_SLOT_ORDER = {
    "IMPORT_INTEREST": ("product", "quantity", "shipping_method"),
}


def _frame_slots(frame) -> Dict[str, Any]:
    """The legacy text-derived frame, expressed as slots. It remains the
    platform's sole READ authority for conversation state — the graph
    reads it, it does not replace it."""
    if frame is None:
        return {}
    out: Dict[str, Any] = {}
    if getattr(frame, "product", None):
        out["product"] = {"value": frame.product, "unit": None, "raw": frame.product}
    if getattr(frame, "quantity", None):
        unit = getattr(frame, "unit", None)
        out["quantity"] = {"value": frame.quantity, "unit": unit,
                           "raw": f"{frame.quantity} {unit}".strip() if unit
                           else str(frame.quantity)}
    if getattr(frame, "method", None):
        out["shipping_method"] = {"value": frame.method, "unit": None,
                                  "raw": frame.method}
    if getattr(frame, "weight", None):
        out["weight"] = {"value": frame.weight, "unit": None, "raw": str(frame.weight)}
    return out


def merge_conversation_state(state: Dict[str, Any]) -> Dict[str, Any]:
    """COMPATIBLE KNOWN INFORMATION SURVIVES FOLLOW-UP TURNS (task §7).

    Merge order, weakest first, so a value stated THIS turn always wins:
        conversation frame  <  resolver's known slots  <  this turn's entities

    A rejection clears the journey's slots — the customer said no; it is
    not "compatible known information" any more.
    """
    node_path = list(state.get("node_path") or []) + ["merge_conversation_state"]
    if state.get("rejection"):
        return {"known_slots": {}, "missing_slots": list(state.get("missing_slots") or []),
                "active_journey": None, "node_path": node_path,
                "notes": list(state.get("notes") or []) + ["rejection cleared journey slots"]}

    merged: Dict[str, Any] = {}
    try:
        merged.update(_frame_slots(engine.active_frame(state.get("history") or [])))
    except Exception as exc:
        return {"node_path": node_path,
                "errors": list(state.get("errors") or []) + [f"merge_state: {exc!r}"]}
    merged.update(state.get("known_slots") or {})

    # this turn's own values override remembered ones — including a
    # corrected quantity, which brings its own unit with it.
    for slot in ("product", "quantity", "shipping_method", "weight", "dimensions"):
        cur = (state.get("entities") or {}).get(slot)
        if cur not in (None, "", [], {}):
            merged[slot] = cur

    journey = state.get("active_journey")
    if not journey and state.get("primary_intent") in _JOURNEY_SLOT_ORDER:
        # A journey opens on the INTENT, not on having a product yet: a
        # quantity-only opener ("20 คู่อยากสั่งของจากจีน") is an open import
        # journey whose product slot is the one still missing. Requiring a
        # product here is what would make the bot answer generically
        # instead of asking for the one thing it needs.
        journey = state.get("primary_intent")
    needed = _JOURNEY_SLOT_ORDER.get(journey or "", ())
    missing = [s for s in needed if not merged.get(s)]
    return {"known_slots": merged, "missing_slots": missing,
            "active_journey": journey, "node_path": node_path}


def resolve_precedence(state: Dict[str, Any]) -> Dict[str, Any]:
    """EXPLICIT CURRENT INTENT beats a running journey (task §8).

    The order itself is not re-implemented here: it is
    services/conversation_resolution.py::resolve_precedence, whose winner
    the resolver already recorded in `precedence_winner`. This node only
    makes the CONSEQUENCE explicit in the state, so the planner does not
    have to re-derive it — notably that a decisive new public question
    ("กระต่ายนำเข้าได้หรอ") suspends the import journey instead of
    continuing to ask it for a quantity.
    """
    node_path = list(state.get("node_path") or []) + ["resolve_precedence"]
    winner = state.get("precedence_winner") or "NONE"
    intent = state.get("primary_intent") or "UNKNOWN"
    notes = list(state.get("notes") or [])
    out: Dict[str, Any] = {"node_path": node_path}

    journey_intents = ("IMPORT_INTEREST",)
    if winner == "NEW_INTENT" and intent not in journey_intents and intent != "UNKNOWN":
        # an explicit non-journey intent this turn: the journey is
        # SUSPENDED (its slots are kept, so returning to it later does
        # not re-ask), but it must not drive this turn's next action.
        out["active_journey"] = None
        notes.append(f"explicit current intent {intent} suspended the active journey")
    elif winner == "CORRECTION_REJECTION_TOPIC" and state.get("topic_switch"):
        out["active_journey"] = None
        notes.append(f"topic switch to {state.get('topic_switch')} suspended the journey")
    out["notes"] = notes
    return out
