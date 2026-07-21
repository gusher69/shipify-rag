"""Canonical Query Rewrite — the final deterministic (no LLM call)
standardization step before retrieval, run AFTER Follow-up/Entity
Resolution (rag/query_resolution.py) and BEFORE Synonym Expansion (rag/
synonym_service.py)/FAQ Exact Match/Hybrid Retrieval.

Some informal questions are already spell-corrected and already resolved
against conversation context, but still aren't phrased as a clear
standalone search query — e.g. "ส่งแผนที่ให้หน่อย" (please send [the]
map) names no subject at all. This module recognizes a small set of
known REQUEST INTENTS (rate / duration / location) via the same
attribute/transport/location extraction rag/query_resolution.py already
uses (reused directly, never duplicated), and — only when it can fill
every required slot from the CURRENT wording or already-resolved
entities, never by inventing one — rewrites the question into its
canonical phrasing.

Every rewrite is a template composed from words that were ACTUALLY
extracted from the question or from conversation entities already
carried forward by rag/query_resolution.py; nothing is invented. When no
template's required slots are satisfied, the corrected/resolved question
is returned completely unchanged ("if uncertain, keep the corrected
original query unchanged").
"""
from typing import Dict, Optional

from rag.query_resolution import extract_entities

# Confidence floor below which a rewrite is discarded and the original
# (already spell-corrected/resolved) question is used as-is instead —
# same "conservative, never guess" posture as every other deterministic
# module in this pipeline (rag/spell_correction.py, rag/query_resolution.py).
MIN_REWRITE_CONFIDENCE = 0.7


def _canonical_from_entities(entities: Dict[str, Optional[str]]) -> Optional[Dict]:
    """Returns {"canonical_query": str, "reason": str, "confidence": float}
    for the first template whose REQUIRED slots are all present, or None
    if nothing matches — mirrors rag/query_resolution.py's `_compose()`
    templates (same phrasing), since a canonical query and a resolved
    follow-up question should look identical when they express the same
    intent."""
    attribute = entities.get("attribute")
    transport = entities.get("transport")
    location = entities.get("location")
    topic = entities.get("topic")

    if attribute == "rate" and transport:
        return {"canonical_query": f"อัตราค่าขนส่งทาง{transport}เท่าไหร่",
                "reason": "rate + transport detected", "confidence": 0.9}
    if attribute == "duration" and transport:
        return {"canonical_query": f"ขอทราบระยะเวลาขนส่งทาง{transport}",
                "reason": "duration + transport detected", "confidence": 0.9}
    if attribute == "location" and topic == "โกดัง":
        canonical = f"ขอที่อยู่และแผนที่โกดัง{location}" if location else "ขอแผนที่โกดัง"
        return {"canonical_query": canonical,
                "reason": "location + warehouse topic detected", "confidence": 0.85}
    return None


def rewrite_canonical_query(question: str, entities: Optional[Dict[str, Optional[str]]] = None) -> Dict:
    """`entities`, if given, are already-resolved conversation entities
    from rag/query_resolution.py::resolve_conversation() (e.g. a
    transport/location carried forward from a prior turn) — used ONLY to
    fill a slot the current wording itself doesn't specify, never to
    override what the current question actually says.

    Returns:
        {
          "canonical_query": str,      # == question when rewrite_applied is False
          "rewrite_applied": bool,
          "reason": str,
          "confidence": float,
        }
    """
    if not question:
        return {"canonical_query": question, "rewrite_applied": False,
                "reason": "empty question", "confidence": 1.0}

    current_entities = extract_entities(question)
    merged = dict(entities or {})
    for key, value in current_entities.items():
        if value:
            merged[key] = value  # current wording always wins over carried entities

    result = _canonical_from_entities(merged)
    if not result or result["confidence"] < MIN_REWRITE_CONFIDENCE:
        return {"canonical_query": question, "rewrite_applied": False,
                "reason": (result["reason"] if result else "no confident canonical template matched"),
                "confidence": result["confidence"] if result else 0.0}

    canonical = result["canonical_query"]
    if canonical.strip() == question.strip():
        return {"canonical_query": question, "rewrite_applied": False,
                "reason": "already canonical", "confidence": result["confidence"]}

    # Canonical Rewrite Constraints (Part 7, P0 2026-07-21) — every slot
    # this template composes from was already extracted from the
    # question's OWN wording or from `entities` (conversation-carried
    # values), never invented; the Semantic Invariant Guard (rag/
    # semantic_guard.py) is still run as a second, independent safety net
    # — the same reusable validator rag/spell_correction.py uses — so a
    # future template can never accidentally add a fact merely by being
    # added to _canonical_from_entities without this check catching it.
    # An introduced entity is accepted only if it matches something
    # legitimately carried from the previous USER turn (`entities` here
    # IS exactly that carried set — never the previous assistant answer).
    from rag.semantic_guard import validate_transformation
    verdict = validate_transformation(question, canonical, carried_entities=entities)
    if not verdict["accepted"]:
        return {"canonical_query": question, "rewrite_applied": False,
                "reason": f"rejected by semantic guard: {verdict['reason']}", "confidence": 0.0}

    return {"canonical_query": canonical, "rewrite_applied": True,
            "reason": result["reason"], "confidence": result["confidence"]}
