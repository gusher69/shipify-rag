# -*- coding: utf-8 -*-
"""ANSWER — build a structured ResponsePlan before any prose exists.

The contradiction this prevents is the one the customer actually saw:

    "รับทราบ 20 คู่"   …followed by…   "ขอจำนวนด้วยค่ะ"

A reply is only allowed to ask for what `plan_next_action` decided is
missing. Anything already acknowledged is, structurally, not askable —
ACKNOWLEDGED_SLOT_CANNOT_BE_REASKED (task §14).
"""
from __future__ import annotations

from typing import Any, Dict

_ASK_SLOT_OF_ACTION = {
    "ASK_PRODUCT": "product",
    "ASK_QUANTITY": "quantity",
    "ASK_SHIPPING_METHOD": "shipping_method",
    "ASK_WEIGHT": "weight",
    "ASK_IDENTIFIER": "identifier",
}


def _acknowledged(known: Dict[str, Any]) -> Dict[str, Any]:
    """The slots this reply is entitled to state back to the customer,
    with the unit the customer themselves used."""
    out: Dict[str, Any] = {}
    for slot, val in (known or {}).items():
        if not val:
            continue
        if isinstance(val, dict):
            raw = val.get("raw") or val.get("value")
            out[slot] = {"value": val.get("value"), "unit": val.get("unit"), "raw": raw}
        else:
            out[slot] = {"value": val, "unit": None, "raw": str(val)}
    return out


def plan_response(state: Dict[str, Any]) -> Dict[str, Any]:
    node_path = list(state.get("node_path") or []) + ["plan_response"]
    action = state.get("planned_action")
    known = state.get("known_slots") or {}
    ack = _acknowledged(known)
    ask_slot = _ASK_SLOT_OF_ACTION.get(action or "")

    # the invariant, enforced rather than trusted: never ask for a slot
    # that this same turn is acknowledging.
    if ask_slot and ask_slot in ack:
        ask_slot = None

    plan = {
        **(state.get("response_plan") or {}),
        "acknowledge": ack,
        "answer": bool(action in ("ANSWER_PUBLIC_INFO", "ANSWER_POLICY",
                                  "CHECK_PRIVATE_STATUS", "CALCULATE",
                                  "CONVERT_LINK")),
        "ask_next": ask_slot,
        "tool_result": (state.get("tool_result") or {}).get("routing_type"),
        "limitations": ("identity required before any private record is read"
                        if state.get("auth_state") == "REQUIRED_MISSING" else None),
        "handoff": bool(state.get("handoff_required")),
    }
    return {"response_plan": plan, "node_path": node_path}
