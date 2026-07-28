"""Clarification State Engine (P0, 2026-07-22) — generic, reusable
resolution for a short reply that answers the ASSISTANT's own immediately
previous clarification question (e.g. "ต้องการเบอร์ติดต่อโกดังไทยหรือโกดังจีนคะ"
-> "ไทย"), never a list of hardcoded (question, reply) pairs.

Root cause this exists to fix: a bare reply like "ไทย" was being resolved
by the GENERIC follow-up/entity-carrying machinery (rag/query_resolution.py),
which accumulates entities across the ENTIRE conversation history with no
flow-scoping — so a stale transport/attribute entity from an earlier,
already-completed, unrelated flow (e.g. an earlier shipping-duration
question) could leak into the resolution of a reply that actually belongs
to a completely different flow (contact lookup), producing a resolved
query for the WRONG intent entirely ("ขอทราบระยะเวลาขนส่งทางรถ" instead of
"ขอเบอร์ติดต่อโกดังไทย").

This module is a separate, HIGHER-PRIORITY resolution step, checked
before the generic follow-up marker detection: when the last turn in
history is an assistant CLARIFICATION QUESTION and the current message
looks like a short answer to it, the resolved query is built from ONLY
the ORIGINAL user question that triggered the clarification (never the
full history) plus whatever new entity the reply itself supplies — this
is what keeps the original intent (e.g. contact_information) intact and
prevents any other flow's entities from leaking in.
"""
import re
from typing import Dict, List, Optional

from rag.query_resolution import extract_entities, _strip_suffix

# A clarification question this codebase asks is either an "A หรือ B"
# disambiguation, or an open "which one do you mean" question (มีคำว่า
# "ไหน"/"อะไร" + a polite question particle) — generic across every topic
# (warehouse country, bill type, product variant, ...), never tied to
# one specific wording.
_CLARIFICATION_QUESTION_RE = re.compile(r"(หรือ|ไหน|ตัวไหน|อันไหน).*(คะ|ครับ|ไหม|\?)\s*$")

# Generic reply shapes that answer a clarification — never an exhaustive
# per-topic list. A bare country/brand/selection/transport word, a
# yes/no particle, or an ordinal choice all count; anything this specific
# list doesn't recognize still falls through to the generic short-length
# check below (a short, single-fragment reply with no sentence structure
# of its own).
_YES_NO_RE = re.compile(r"^(ใช่|ไม่ใช่|ไม่|ใช่ค่ะ|ใช่ครับ)$")
_ORDINAL_RE = re.compile(r"^(อันแรก|อันที่\s?1|อันที่หนึ่ง|อันที่สอง|อันที่\s?2|อันสุดท้าย)$")
_BRAND_RE = re.compile(r"shipify|fasttrade", re.IGNORECASE)
_MAX_CLARIFICATION_REPLY_LEN = 20


def _is_clarification_question(text: str) -> bool:
    return bool(_CLARIFICATION_QUESTION_RE.search((text or "").strip()))


def _looks_like_clarification_reply(text: str, entities: Dict) -> bool:
    text = (text or "").strip()
    if not text:
        return False
    if _YES_NO_RE.match(text) or _ORDINAL_RE.match(text) or _BRAND_RE.search(text):
        return True
    if entities.get("location") or entities.get("transport"):
        return True
    # Generic fallback: a short, bare fragment with no sentence structure
    # of its own (no verb-like question markers) is very likely a direct
    # answer to whatever was just asked, not a fresh standalone question.
    return len(text) <= _MAX_CLARIFICATION_REPLY_LEN and "?" not in text


def detect_pending_clarification(history: Optional[List[Dict]]) -> Optional[Dict]:
    """Returns {"original_question": str, "clarification_question": str}
    when the LAST turn in `history` is an assistant clarification
    question, else None. `original_question` is the USER turn
    immediately BEFORE that clarification — the one whose intent must be
    preserved — found by walking backward, never by scanning/accumulating
    the whole conversation."""
    if not history:
        return None
    last = history[-1]
    if last.get("role") != "assistant" or not _is_clarification_question(last.get("content") or ""):
        return None
    for turn in reversed(history[:-1]):
        if turn.get("role") == "user":
            content = (turn.get("content") or "").strip()
            if content:
                return {"original_question": content, "clarification_question": last.get("content") or ""}
            break
    return None


def _explicit_topic_change(reply_entities: Dict, pending_topic: Optional[str]) -> bool:
    """Priority 1: if the reply itself names a DIFFERENT topic than the
    one the clarification was about, the user has abandoned the
    clarification for a new subject — this resolver must step aside and
    let the normal pipeline treat it as a fresh question."""
    reply_topic = reply_entities.get("topic")
    return bool(reply_topic and pending_topic and reply_topic != pending_topic)


def resolve_clarification_answer(reply_text: str, history: Optional[List[Dict]]) -> Optional[Dict]:
    """The core Clarification State Engine entry point. Returns
    {"resolved_question": str, "confidence": float} when `reply_text` is
    a valid answer to the immediately previous assistant clarification
    question, else None (caller falls through to the next resolution
    priority — normal follow-up handling).

    Flow-scoping guarantee: entities are extracted ONLY from
    {original_question, clarification_question, reply_text} — never
    accumulated across the rest of `history` — so an entity from an
    earlier, unrelated, already-completed flow can never leak into this
    resolution."""
    pending = detect_pending_clarification(history)
    if not pending:
        return None

    normalized = (reply_text or "").strip()
    reply_entities = extract_entities(normalized)

    original_entities = extract_entities(pending["original_question"])
    clarification_entities = extract_entities(pending["clarification_question"])
    # The topic the clarification was actually ABOUT — the assistant's
    # own wording often reveals a subject the bare original question
    # didn't name (e.g. "ขอเบอร์ติดต่อ" says nothing about a warehouse;
    # "...โกดังไทยหรือโกดังจีนคะ" does) — never inventing a topic that
    # doesn't appear in either text.
    pending_topic = original_entities.get("topic") or clarification_entities.get("topic")

    if _explicit_topic_change(reply_entities, pending_topic):
        return None

    if not _looks_like_clarification_reply(normalized, reply_entities):
        return None

    resolved = _compose_clarification_answer(
        pending["original_question"], pending_topic, reply_entities, normalized)
    if not resolved:
        return None

    return {"resolved_question": resolved, "confidence": 0.9,
            "original_question": pending["original_question"]}


def _compose_clarification_answer(original_question: str, topic: Optional[str],
                                   reply_entities: Dict, reply_text: str) -> str:
    """Combines the ORIGINAL user question (never a later or stale one)
    with whatever new entity the reply supplies. Every word used here
    comes from the original question, the clarification question (via
    `topic`, already resolved by the caller), or the reply itself —
    nothing is invented."""
    # Deliberately does NOT strip a filler prefix ("ขอ") here — unlike the
    # legacy follow-up resolver, this composer is re-attaching a topic/
    # entity word directly onto the ORIGINAL question, so keeping "ขอ..."
    # intact reads naturally ("ขอเบอร์ติดต่อโกดังไทย"), not "เบอร์ติดต่อโกดังไทย".
    core, _ = _strip_suffix(original_question.strip())
    location = reply_entities.get("location")
    transport = reply_entities.get("transport")

    if topic and location:
        return f"{core}{topic}{location}"
    if transport:
        return f"{core}ทาง{transport}"
    if location:
        return f"{core}{location}"
    # Generic fallback (yes/no, ordinal choice, brand name, or any other
    # bare reply shape not covered above) — appends the reply verbatim so
    # the ORIGINAL intent/topic is always preserved, even when this
    # module doesn't have a specific template for the entity shape.
    return f"{core} {reply_text}".strip()
