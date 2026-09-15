# -*- coding: utf-8 -*-
"""Adapter over the platform's human-language input layer.

The graph's `normalize_language` node and the semantic-recovery step in
`resolve_current_turn` reach the normaliser ONLY through here, so the
nodes stay free of any language rule (tests/test_agent_graph.py checks
that mechanically).

Reused primitives, and who owns each decision:

  normalisation / candidates   services/language/thai_normalizer.py
                               (identifier protection, PyThaiNLP,
                               RapidFuzz against the bounded vocabulary)
  structural family scoring    services/conversation_semantics.py::_compose
                               — the deterministic, LLM-free tier of the
                               ONE central interpreter. Used to decide
                               whether a MEDIUM candidate makes the turn
                               more understandable; it never routes.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from services.language import thai_normalizer as _tn
from services.conversation_semantics import _compose as _structural_compose

NormalizationResult = _tn.NormalizationResult
Candidate = _tn.Candidate


def normalize(text: str) -> NormalizationResult:
    return _tn.normalize(text or "")


def normalize_history(history: Optional[Sequence[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    return _tn.normalize_history(history)


def oov_count(text: str) -> int:
    return _tn.oov_count(text or "")


def is_dictionary_word(tok: str) -> bool:
    return _tn.is_dictionary_word(tok)


def structural_reading(text: str) -> Tuple[str, float, Dict[str, Any]]:
    """(family, confidence, entities) from the deterministic tier of the
    central interpreter — no history, no LLM, no side effects."""
    try:
        fam, conf, ent = _structural_compose(text or "")
        return fam or "UNKNOWN", float(conf or 0.0), dict(ent or {})
    except Exception:
        return "UNKNOWN", 0.0, {}


def reading_quality(text: str) -> Tuple[int, float, int, int]:
    """A comparable quality tuple for one reading of a message, used by
    semantic recovery to decide whether a pending correction is USEFUL:

        (family_known, confidence, -oov_entity_values, entity_count)

    A reading whose entity values are real words beats one that swallowed
    a typo as a product name; a known family beats UNKNOWN; and only then
    does confidence decide. Ties keep the customer's own wording."""
    fam, conf, ent = structural_reading(text)
    known = 0 if fam in ("UNKNOWN", "GENERAL", "") else 1
    oov_vals = 0
    for v in ent.values():
        if isinstance(v, str) and v:
            oov_vals += 1 if oov_count(v) else 0
    return known, round(conf, 3), -oov_vals, len(ent)
