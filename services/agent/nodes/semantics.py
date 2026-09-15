# -*- coding: utf-8 -*-
"""UNDERSTAND — normalise the human language, then resolve this turn's
meaning ONCE.

    normalize_language     raw_message -> normalized_message (+ evidence)
    resolve_current_turn   normalized_message -> canonical semantics

The normaliser (services/language, reached through the language
adapter) corrects what it is SURE about — a marks-only slip on a word
from the bounded vocabulary, an informal particle, a keyboard-layout
run — and hands everything else over as MEDIUM candidates. Whether a
MEDIUM candidate is worth applying is decided HERE, by asking the
central interpreter's deterministic tier which reading it understands
better (semantic recovery, task §6). The normaliser never decides
meaning; the resolver never guesses spellings.
"""
from __future__ import annotations

from typing import Any, Dict, List

from services.agent.adapters import existing_engine as engine
from services.agent.adapters import language as lang
from services.agent.state import effective_history

# the semantic-recovery search is bounded: at most this many single
# candidates plus the "all together" variant are ever scored.
_MAX_RECOVERY_CANDIDATES = 4


def normalize_language(state: Dict[str, Any]) -> Dict[str, Any]:
    """ONE normalisation of the current message and of every user turn
    in the history. `raw_message` is never overwritten."""
    node_path = list(state.get("node_path") or []) + ["normalize_language"]
    raw = state.get("raw_message") or ""
    try:
        res = lang.normalize(raw)
        hist = lang.normalize_history(state.get("history") or [])
    except Exception as exc:                       # never block the turn
        return {"normalized_message": raw.strip(),
                "normalized_history": list(state.get("history") or []),
                "normalization_trace": {"raw_text_present": bool(raw),
                                        "normalization_applied": False,
                                        "normalization_method": [f"degraded:{exc!r}"]},
                "node_path": node_path,
                "notes": list(state.get("notes") or []) + [f"normalize_language degraded: {exc!r}"]}
    notes = list(state.get("notes") or [])
    if res.changed:
        notes.append("normalize_language applied: "
                     + ", ".join(f"{c.original}->{c.replacement}" for c in res.applied))
    return {
        "normalized_message": res.normalized_text,
        "normalized_history": hist,
        "normalization_candidates": [c.as_dict() for c in res.candidates],
        "normalization_confidence": float(res.confidence),
        "normalization_applied": bool(res.changed),
        "normalization_method": list(res.methods),
        "normalization_trace": res.as_trace(),
        "node_path": node_path,
        "notes": notes,
    }


def _slot_plain(v: Any) -> Any:
    """A SlotValue -> its dict form; anything else unchanged. Keeps the
    unit and the customer's own raw wording attached to the value, which
    is what defect class C existed to preserve."""
    return v.as_dict() if hasattr(v, "as_dict") else v


def _semantic_recovery(state: Dict[str, Any]) -> Dict[str, Any]:
    """Decide whether any PENDING (MEDIUM) normalisation candidate makes
    this turn more understandable, using the interpreter's own
    deterministic reading as the judge.

    Returns the partial state to merge: possibly a new
    `normalized_message`, the candidates marked applied, and a note.
    Ties keep the customer's own wording (task §11: if a candidate could
    change business meaning, it is not applied silently — here "silently"
    means without the resolver preferring it).
    """
    msg = state.get("normalized_message") or ""
    pending = [c for c in (state.get("normalization_candidates") or [])
               if not c.get("applied") and c.get("tier") == "MEDIUM"
               and c.get("recoverable", True)]
    if not pending or not msg:
        return {}
    pending = sorted(pending, key=lambda c: -float(c.get("score") or 0))[:_MAX_RECOVERY_CANDIDATES]

    def _apply(cands: List[Dict[str, Any]]) -> str:
        text = msg
        for c in sorted(cands, key=lambda c: int(c["start"]), reverse=True):
            text = text[:int(c["start"])] + str(c["replacement"]) + text[int(c["end"]):]
        return text

    base = lang.reading_quality(msg)
    best_text, best_q, best_set = msg, base, []
    variants = [[c] for c in pending]
    if len(pending) > 1:
        # only non-overlapping candidates can be combined
        combo, taken = [], []
        for c in pending:
            if all(int(c["end"]) <= int(o["start"]) or int(c["start"]) >= int(o["end"]) for o in taken):
                combo.append(c)
                taken.append(c)
        if len(combo) > 1:
            variants.append(combo)
    for cands in variants:
        text = _apply(cands)
        q = lang.reading_quality(text)
        if q > best_q or (q == best_q and q > base and len(cands) < len(best_set)):
            best_text, best_q, best_set = text, q, cands
    if not best_set:
        return {}
    applied_ids = {(c["start"], c["end"], c["replacement"]) for c in best_set}
    new_cands = []
    for c in state.get("normalization_candidates") or []:
        if (c.get("start"), c.get("end"), c.get("replacement")) in applied_ids:
            c = {**c, "applied": True, "reason": (c.get("reason") or "") + "; accepted by semantic recovery"}
        new_cands.append(c)
    trace = dict(state.get("normalization_trace") or {})
    trace["normalization_applied"] = True
    trace["normalization_method"] = list(trace.get("normalization_method") or []) + ["semantic_recovery"]
    trace["applied"] = list(trace.get("applied") or []) + [
        {"from": c["original"], "to": c["replacement"], "tier": "MEDIUM",
         "score": c.get("score"), "source": c.get("source"), "group": c.get("group")}
        for c in best_set]
    trace["pending"] = [p for p in (trace.get("pending") or [])
                        if not any(p.get("from") == c["original"] and p.get("to") == c["replacement"]
                                   for c in best_set)]
    return {
        "normalized_message": best_text,
        "normalization_candidates": new_cands,
        "normalization_applied": True,
        "normalization_method": list(state.get("normalization_method") or []) + ["semantic_recovery"],
        "normalization_trace": trace,
        "notes": list(state.get("notes") or [])
        + ["semantic recovery applied: "
           + ", ".join(f"{c['original']}->{c['replacement']}" for c in best_set)],
    }


def resolve_current_turn(state: Dict[str, Any]) -> Dict[str, Any]:
    """THE single semantic read of the current message (task §4/§6).

    Everything downstream reads these fields. No other node re-parses the
    text, so two parts of the graph can no longer disagree about what the
    customer just said.
    """
    out: Dict[str, Any] = {"node_path": list(state.get("node_path") or [])
                           + ["resolve_current_turn"]}
    try:
        recovered = _semantic_recovery(state)
    except Exception as exc:                       # evidence only; never fatal
        recovered = {"notes": list(state.get("notes") or [])
                     + [f"semantic recovery skipped: {exc!r}"]}
    if recovered:
        state = {**state, **recovered}
        out.update(recovered)
    msg = state.get("normalized_message") or state.get("raw_message") or ""
    try:
        res = engine.resolve_turn(msg, effective_history(state),
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
