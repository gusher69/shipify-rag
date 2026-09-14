# -*- coding: utf-8 -*-
"""PERSIST — hand the turn's durable facts back to the EXISTING store.

No new database, no new table, no LangGraph checkpointer backend (task
§15). The platform already persists conversation state through
services/session_service.py and the P2 structured frame column; this node
only produces the small, already-shaped payload for the channel adapter
to write, exactly as the webhook does today.

Chain-of-thought is never persisted — only resolved facts.
"""
from __future__ import annotations

from typing import Any, Dict

_DURABLE_SLOTS = ("product", "quantity", "shipping_method", "weight", "dimensions")


def persist_state(state: Dict[str, Any]) -> Dict[str, Any]:
    node_path = list(state.get("node_path") or []) + ["persist_state"]
    known = state.get("known_slots") or {}
    slots: Dict[str, Any] = {}
    for slot in _DURABLE_SLOTS:
        val = known.get(slot)
        if not val:
            continue
        slots[slot] = val if isinstance(val, dict) else {"value": val, "unit": None,
                                                         "raw": str(val)}
    payload = {
        "active_journey": state.get("active_journey"),
        "requested_slot": state.get("response_plan", {}).get("ask_next"),
        "slots": slots,
        "last_meaningful_intent": (state.get("primary_intent")
                                   if state.get("primary_intent") != "UNKNOWN"
                                   else None),
    }
    # The engine's own P2 shadow frame, when it produced one, remains the
    # thing the channel adapter actually writes. This payload is the
    # graph's equivalent view, carried for comparison — never a second
    # write path.
    engine_frame = (state.get("_engine_result") or {}).get("conversation_frame")
    return {"node_path": node_path,
            "response_plan": {**(state.get("response_plan") or {}),
                              "persisted": payload,
                              "engine_conversation_frame": engine_frame}}
