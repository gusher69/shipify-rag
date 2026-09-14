# -*- coding: utf-8 -*-
"""UNDERSTAND — normalise the input and resolve this turn's meaning ONCE."""
from __future__ import annotations

import re
from typing import Any, Dict

from services.agent.adapters import existing_engine as engine

# Whitespace/zero-width normalisation only. Deliberately NOT spelling
# correction or synonym folding: meaning-changing normalisation belongs
# to the central interpreter, which already owns typo tolerance.
_ZERO_WIDTH_RE = re.compile(r"[​‌‍﻿]")
_WS_RE = re.compile(r"[ \t ]+")


def normalize_input(state: Dict[str, Any]) -> Dict[str, Any]:
    raw = state.get("raw_message") or ""
    norm = _ZERO_WIDTH_RE.sub("", raw)
    norm = _WS_RE.sub(" ", norm).strip()
    return {"normalized_message": norm,
            "node_path": list(state.get("node_path") or []) + ["normalize_input"]}


def _slot_plain(v: Any) -> Any:
    """A SlotValue -> its dict form; anything else unchanged. Keeps the
    unit and the customer's own raw wording attached to the value, which
    is what defect class C existed to preserve."""
    return v.as_dict() if hasattr(v, "as_dict") else v


def resolve_current_turn(state: Dict[str, Any]) -> Dict[str, Any]:
    """THE single semantic read of the current message (task §4/§6).

    Everything downstream reads these fields. No other node re-parses the
    text, so two parts of the graph can no longer disagree about what the
    customer just said.
    """
    msg = state.get("normalized_message") or state.get("raw_message") or ""
    out: Dict[str, Any] = {"node_path": list(state.get("node_path") or [])
                           + ["resolve_current_turn"]}
    try:
        res = engine.resolve_turn(msg, state.get("history") or [],
                                  state.get("_decide_context") or {})
    except Exception as exc:
        return {**out, "errors": list(state.get("errors") or [])
                + [f"resolve_current_turn: {exc!r}"]}

    entities = {k: _slot_plain(v) for k, v in (res.entities or {}).items()}
    # the slot updates this turn CARRIES are part of "what was said" and
    # must not be lost just because the winning family was something else.
    for k, v in (res.slot_updates or {}).items():
        entities.setdefault(k, _slot_plain(v))
    # A frame CORRECTION ("ส่งเรือครับ" answering the assistant's own
    # "ทางรถหรือทางเรือ") is also something this turn said. The resolver
    # already computed it as a structured sub-result; without folding it
    # in, the answer to a slot the assistant just requested would be
    # invisible to the planner even though the engine acts on it.
    _fc = getattr(res, "frame_correction", None) or {}
    for _key, _slot_name in (("product", "product"), ("quantity", "quantity"),
                             ("method", "shipping_method")):
        _val = _fc.get(_key)
        if _val and not entities.get(_slot_name):
            entities[_slot_name] = {"value": _val, "unit": _fc.get("unit"),
                                    "raw": str(_val)}

    act = res.conversation_act or "UNKNOWN"
    return {
        **out,
        "conversation_act": act,
        "primary_intent": res.primary_intent or "UNKNOWN",
        "semantic_family": res.semantic_family or "UNKNOWN",
        "active_journey": res.active_journey,
        "entities": entities,
        "known_slots": {k: _slot_plain(v) for k, v in (res.known_slots or {}).items()},
        "missing_slots": list(res.missing_slots or []),
        "requested_slot": res.requested_slot,
        "topic_switch": res.topic_switch,
        "correction": bool((res.slot_corrections or {})
                           or act in ("CORRECTION", "TOPIC_SWITCH")),
        "rejection": act == "REJECTION",
        "precedence_winner": res.precedence_winner or "NONE",
        "grounding_requirement": res.grounding_requirement or "GENERAL",
        "confidence": float(res.confidence or 0.0),
        "_resolution": res,
    }
