# -*- coding: utf-8 -*-
"""SYSTEM-WIDE CONVERSATION INTELLIGENCE — P2.1 shadow observability.

OBSERVABILITY ONLY. Turns the per-turn `developer_trace` (already
computed by `services/decision_engine.py::decide()` for P1/P2/P2-STAB)
into a small, privacy-safe, structured record so the >= 200 live-turn
read-cutover gate can be measured directly from persisted data instead
of by replaying message history.

This module does not call the LLM, does not read from or write to the
database, does not build a resolution or a frame, and does not decide
routing. It only reads keys `decide()` already placed in
`developer_trace` and reshapes them. Every function degrades to
``None`` / a safe default rather than raise, so a bug here can never
break a customer turn or a Conversation History write.

PRIVACY: only structured labels are ever included — intent/family/
journey/status names, slot NAMES (never slot VALUES), and booleans.
Never the raw customer message, the assistant reply, a slot value, a
tracking number, a CustCode, a phone number, an address, a URL, an ERP
response, a prompt, or any LLM reasoning text.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

RESOLVER_VERSION = 1
# P2.1A — OWNER_TEST is a configured owner/tester LINE sender (task
# §OWNER_TEST_LINE_USER_IDS in config.py); it is classified at the
# webhook boundary from a verified LINE user id and reaches this module
# ONLY as this label — never as the id itself.
SAMPLE_SOURCES = ("REAL_LINE", "OWNER_TEST", "ADMIN_AUTO", "TEST", "OTHER")
PARITY_CLASSES = ("MATCH", "STRUCTURED_IMPROVEMENT", "LEGACY_CORRECT",
                  "STRUCTURED_WRONG", "AMBIGUOUS")

# precedence tiers where a high-confidence deterministic CONTEXT signal
# was expected to be canonical (P2-STAB). Used only to flag a turn for
# review — never to change what already happened.
_CONTEXT_AUTHORITY_TIERS = ("REQUESTED_SLOT_ANSWER", "CORRECTION_REJECTION_TOPIC")


def classify_sample_source(raw: Optional[str]) -> str:
    """Normalises a caller-supplied source marker. Unknown / missing ->
    OTHER, never guessed from message content (channel adapters set this
    explicitly: line_bot/webhook.py -> REAL_LINE, or OWNER_TEST when the
    verified LINE sender is in config.OWNER_TEST_LINE_USER_IDS; the
    Admin Hybrid-Playground auto mode -> ADMIN_AUTO; anything else,
    including every existing test/harness that does not set it, is
    OTHER by default)."""
    s = (raw or "").strip().upper()
    return s if s in SAMPLE_SOURCES else "OTHER"


def is_shadow_sample_eligible(developer_trace: Dict[str, Any]) -> bool:
    """True when a ConversationResolution was built AND a structured-vs-
    legacy frame comparison actually happened this turn (i.e. decide()'s
    existing P2 shadow-write block ran to completion) — regardless of
    the outcome. A turn that produced STRUCTURED_WRONG is exactly as
    eligible as one that produced MATCH; only a genuine build/compare
    failure (conversation_resolution_error / conversation_frame_error
    present, or no parity_with_legacy at all) is excluded."""
    if not isinstance(developer_trace, dict):
        return False
    if developer_trace.get("conversation_resolution_error") or developer_trace.get("conversation_frame_error"):
        return False
    frame_trace = developer_trace.get("conversation_frame")
    if not isinstance(frame_trace, dict):
        return False
    parity = frame_trace.get("parity_with_legacy")
    return bool(isinstance(parity, dict) and parity.get("class") in PARITY_CLASSES)


def _evidence_llm_source(resolution: Dict[str, Any]) -> Optional[str]:
    for ev in (resolution.get("evidence") or []):
        if isinstance(ev, dict) and ev.get("kind") == "llm_semantic_signal":
            return ev.get("source")
    return None


def build_telemetry(developer_trace: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The small, privacy-safe structured record for ONE turn, or None
    when this turn is not an eligible shadow sample. Never raises."""
    try:
        if not is_shadow_sample_eligible(developer_trace):
            return None
        frame_trace = developer_trace.get("conversation_frame") or {}
        resolution = developer_trace.get("conversation_resolution") or {}
        parity = frame_trace.get("parity_with_legacy") or {}
        slots = frame_trace.get("slots") or {}
        # NAMES ONLY — never the slot value (task §4).
        known_slot_names = sorted(str(k) for k in slots.keys())

        winner = resolution.get("precedence_winner")
        parity_cls = parity.get("class")
        llm_src = _evidence_llm_source(resolution)

        return {
            "resolver_version": RESOLVER_VERSION,
            "frame_version": frame_trace.get("version") or 1,
            "sample_source": classify_sample_source(developer_trace.get("sample_source")),
            "conversation_act": resolution.get("conversation_act"),
            "primary_intent": resolution.get("primary_intent"),
            "precedence_winner": winner,
            "active_journey": frame_trace.get("journey"),
            "frame_status": frame_trace.get("status"),
            "requested_slot": frame_trace.get("requested_slot"),
            "known_slots": known_slot_names,
            "parity_class": parity_cls,
            "structured_frame_present": bool(frame_trace.get("journey") or slots),
            "legacy_frame_present": bool(resolution.get("active_journey")),
            "unit_preserved": bool(parity.get("unit_preserved", True)),
            # a fresh explicit opener where the structured reset lost to
            # (or merely tied) the legacy value — the takeover P2 exists
            # to prevent, flagged for review, never silently dropped.
            "stale_journey_takeover": bool(
                winner == "NEW_INTENT" and parity_cls in ("STRUCTURED_WRONG", "LEGACY_CORRECT")),
            "known_slot_reask": bool(frame_trace.get("known_slot_reask")),
            # the gated LLM decided this turn's family while a
            # high-confidence deterministic CONTEXT tier had already won
            # precedence — exactly what the P2-STAB authority rule exists
            # to prevent. A diagnostic flag, not a routing signal.
            "llm_authority_violation": bool(llm_src == "llm" and winner in _CONTEXT_AUTHORITY_TIERS),
            "eligible_for_cutover_sample": True,
        }
    except Exception:
        return None
