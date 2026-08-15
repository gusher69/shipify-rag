"""Hybrid Question Classifier & Segmenter (introduced 2026-08-02 Hybrid
Question Segmentation sprint; consumed directly by services/
decision_engine.py's Hybrid routing since the 2026-08-02 Production
Integration Sprint, Phase 1 Step C — no longer Playground-only).

Every signal used here is generic and Registry-driven — no customer-
specific or endpoint-specific vocabulary is hardcoded anywhere in this
file. It reuses, never duplicates, the shared generic scoring/parameter-
binding primitives in services/action_selection_primitives.py
(`_keyword_score`, `_extract_candidates_for_binding`,
`_bind_candidate_to_parameter`, `_askable_parameters_by_name`) — the
SAME primitives services/decision_engine.py itself imports from that
module. Those primitives were extracted out of decision_engine.py
specifically so this module could depend on them without decision_engine.py
depending on this module in return (this module has no import of
decision_engine.py at all, avoiding a circular import now that
decision_engine.py imports THIS module for Hybrid routing).

Classification categories:
    RAG_ONLY              — no Business Action token/parameter evidence found.
    ERP_ONLY               — a Business Action matched, no separable RAG remainder.
    HYBRID                  — a Business Action matched AND the question cleanly
                              segments into an ERP-relevant clause and a
                              genuinely separate remainder.
    CLARIFICATION_REQUIRED — 2+ DIFFERENT Business Actions matched with
                              comparably strong evidence — which one the
                              customer means is genuinely ambiguous.
    UNKNOWN                — a greeting-only / no-content message; still
                              safely falls back to RAG execution (never
                              leaves the customer with literally nothing).
"""
import re
from typing import Dict, List, Optional

from services.action_selection_primitives import (
    _askable_parameters_by_name,
    _bind_candidate_to_parameter,
    _extract_candidates_for_binding,
    _extract_structural_candidates,
    _keyword_score,
)

# Generic, language-agnostic conjunction/clause markers — never a
# customer-specific vocabulary. Used only to split a compound question
# into candidate clauses; a message with none of these stays a single
# clause (segmentation then correctly reports "not separable").
_SEGMENT_CONJUNCTIONS = ("และ", "กับ", "แล้วก็", "รวมถึง", "พร้อมกับ", " and ", " plus ", " as well as ")

# A message that is ONLY a greeting has no actionable question content —
# deterministically UNKNOWN rather than a low-confidence guess either way.
_GREETING_RE = re.compile(r"^(สวัสดี|หวัดดี|hello|hi|hey)[\sครับค่ะ!.]*$", re.IGNORECASE)

# A candidate action within 80% of the top score is "comparably strong"
# — genuinely ambiguous, not just a distant runner-up. Fixed, documented,
# never per-customer.
_AMBIGUITY_RATIO = 0.8


def _split_clauses(message: str) -> List[str]:
    """Splits on the fixed conjunction list above only — never on any
    domain/customer-specific word. A message with no matching conjunction
    returns a single-element list (itself), which callers treat as
    "not segmentable"."""
    parts = [message]
    for conj in _SEGMENT_CONJUNCTIONS:
        next_parts: List[str] = []
        for p in parts:
            next_parts.extend(p.split(conj))
        parts = next_parts
    return [p.strip() for p in parts if p.strip()]


def _segment_by_value(message: str, matched_value: str) -> Optional[Dict[str, str]]:
    """Splits `message` into an ERP-relevant clause (the one containing
    the literal parameter VALUE that was actually extracted, e.g.
    "C00001") and a RAG-relevant remainder (everything else). Returns
    None when the message doesn't cleanly separate into 2+ clauses with
    both a matching and a non-matching one — callers must NOT force a
    HYBRID classification in that case (see classify_question)."""
    clauses = _split_clauses(message)
    if len(clauses) < 2 or not matched_value:
        return None
    erp_clauses = [c for c in clauses if matched_value in c]
    rag_clauses = [c for c in clauses if c not in erp_clauses]
    if not erp_clauses or not rag_clauses:
        return None
    return {"erp_sub_question": " ".join(erp_clauses), "rag_sub_question": " ".join(rag_clauses)}


def _result(classification: str, confidence: float, evidence: List[str], *,
            selected_action_id: Optional[str] = None, candidate_action_ids: Optional[List[str]] = None,
            erp_sub_question: Optional[str] = None, rag_sub_question: Optional[str] = None) -> Dict:
    return {
        "classification": classification, "confidence": round(confidence, 3), "evidence": evidence,
        "selected_action_id": selected_action_id, "candidate_action_ids": candidate_action_ids or [],
        "erp_sub_question": erp_sub_question, "rag_sub_question": rag_sub_question,
    }


def classify_question(message: str, registry, *, forced_action_id: Optional[str] = None) -> Dict:
    """The one entry point. `forced_action_id`: when the caller already
    knows which Business Action to use (an explicit ERP/Hybrid mode
    selection), evidence-gathering for WHICH action is skipped — this
    still performs parameter-value segmentation against that action's own
    configured parameters, so explicit Hybrid mode benefits from
    segmentation exactly like Auto-detected Hybrid does."""
    message = (message or "").strip()
    if not message:
        return _result("UNKNOWN", 0.0, ["empty message"])
    if _GREETING_RE.match(message):
        return _result("UNKNOWN", 0.9, ["greeting-only message, no actionable question content"])

    matched_action = None
    candidate_ids: List[str] = []

    if forced_action_id:
        matched_action = registry.get_full(forced_action_id, mask_secrets=True)
        if matched_action:
            candidate_ids = [forced_action_id]
    else:
        try:
            enabled = [a for a in registry.enabled_actions() if a.get("action_type") in ("API", "WEBHOOK")]
        except Exception:
            enabled = []
        scored = sorted(
            ({**a, "_kw_score": _keyword_score(a, message)} for a in enabled),
            key=lambda a: a["_kw_score"], reverse=True,
        )
        scored = [a for a in scored if a["_kw_score"] > 0]
        if scored:
            top_score = scored[0]["_kw_score"]
            close = [a for a in scored if a["_kw_score"] >= top_score * _AMBIGUITY_RATIO]
            if len(close) > 1:
                # Final Conversational Correctness (2026-08-15) — a tied
                # KEYWORD score alone (e.g. SearchDataTracking and
                # SearchDataShipmentList both configuring "tracking" as a
                # search keyword) must not be the ONLY signal once real
                # parameter evidence can settle it. If the message
                # carries a STRUCTURAL value (never the whole-message
                # free-text fallback — see _extract_structural_candidates)
                # that a tied candidate's own REQUIRED parameter can bind,
                # AND no OTHER tied candidate can bind that SAME value
                # (e.g. a shared CustCode present in every candidate is
                # never discriminating on its own — mirrors the same
                # "shared evidence is weak evidence for any ONE candidate"
                # principle services/decision_engine.py's own
                # _identifier_pattern_score already applies), that
                # candidate wins outright instead of forcing a
                # clarification the evidence doesn't actually require.
                # Deliberately REQUIRED parameters only — an optional
                # filter/passthrough parameter (date-range/status with no
                # real validation configured yet) says nothing about which
                # action the customer means, and would otherwise
                # spuriously "match" via the same permissive
                # generic-identifier fallback every unconfigured parameter
                # shares. A generic message with no discriminating value
                # (e.g. "ขอดู tracking ของผม FT3182" — only a bare CustCode
                # every tied action requires identically) still correctly
                # falls through to clarification below.
                structural_candidates = _extract_structural_candidates(message)
                decisive = []
                if structural_candidates:
                    bindable_values_by_action = {}
                    for a in close:
                        full = registry.get_full(a["id"], mask_secrets=True)
                        required_askable = [p for p in _askable_parameters_by_name(full).values() if p.get("required")]
                        bindable_values_by_action[a["id"]] = {
                            v for v in structural_candidates
                            if any(_bind_candidate_to_parameter([v], p)["status"] == "bound" for p in required_askable)
                        }
                    for a in close:
                        others = set().union(*(vals for aid, vals in bindable_values_by_action.items()
                                                if aid != a["id"])) if len(close) > 1 else set()
                        if bindable_values_by_action[a["id"]] - others:
                            decisive.append(a)
                if len(decisive) == 1:
                    close = decisive
                else:
                    return _result(
                        "CLARIFICATION_REQUIRED", 0.4,
                        [f"{len(close)} Business Actions matched with comparably strong evidence "
                         f"({', '.join(a.get('action_key') or a.get('name') or a['id'] for a in close)})"],
                        candidate_action_ids=[a["id"] for a in close],
                    )
            matched_action = registry.get_full(close[0]["id"], mask_secrets=True)
            candidate_ids = [close[0]["id"]]

    if not matched_action:
        return _result("RAG_ONLY", 0.6, ["no Business Action keyword/example/description overlap found"],
                        rag_sub_question=message)

    evidence = [f"Business Action matched: {matched_action.get('action_key') or matched_action.get('name')}"]

    matched_value = None
    matched_param_name = None
    askable = _askable_parameters_by_name(matched_action)
    candidates = _extract_candidates_for_binding(message)
    for name, param in askable.items():
        binding = _bind_candidate_to_parameter(candidates, param)
        if binding["status"] == "bound":
            matched_value, matched_param_name = binding["value"], name
            break

    if not matched_value:
        evidence.append("Business Action matched by keyword, but no parameter value found yet "
                         "(the Action's own clarification flow will ask for it — no ERP execution without one)")
        return _result("ERP_ONLY", 0.5, evidence, selected_action_id=matched_action["id"],
                        candidate_action_ids=candidate_ids, erp_sub_question=message)

    evidence.append(f"parameter value matched: {matched_param_name}={matched_value}")
    segmentation = _segment_by_value(message, matched_value)
    if segmentation:
        evidence.append("question cleanly segments into an ERP-relevant clause and a separate remainder")
        return _result("HYBRID", min(0.95, 0.6 + 0.15 * len(evidence)), evidence,
                        selected_action_id=matched_action["id"], candidate_action_ids=candidate_ids,
                        erp_sub_question=segmentation["erp_sub_question"], rag_sub_question=segmentation["rag_sub_question"])

    return _result("ERP_ONLY", min(0.9, 0.5 + 0.15 * len(evidence)), evidence,
                    selected_action_id=matched_action["id"], candidate_action_ids=candidate_ids,
                    erp_sub_question=message)
