"""Hybrid Playground — Auto-mode router and Hybrid-mode answer merge.

Sprint scope note (2026-07-29 Hybrid Integration Flow sprint): this
module is DELIBERATELY DUMB. It is a naive keyword heuristic used only
to decide RAG-vs-ERP in "Auto" mode, and (via naive_merge_hybrid_answer,
legacy) a naive answer concatenation — NOT the future Business Action
Matcher and NOT the production Decision Engine. Do not extend this file
with real intent classification, scoring models, or learned routing.

Playground-only status (2026-08-02 Production Integration Sprint, Phase 1
Step B): the real synthesis logic used by BOTH the Playground and the
production Decision Engine now lives in services/hybrid_runtime_service.py
— `synthesize_hybrid_answer` is re-exported here, unchanged, purely for
backward compatibility with existing imports/tests. Nothing in this file
is imported by services/decision_engine.py.
"""
from typing import Dict, List, Optional

# Re-exported for backward compatibility — the real implementation moved
# to services/hybrid_runtime_service.py (2026-08-02 Production Integration
# Sprint, Phase 1 Step B) so production code (services/decision_engine.py)
# never has to import this Playground-only module.
from services.hybrid_runtime_service import synthesize_hybrid_answer  # noqa: F401


def naive_auto_route(question: str, erp_actions: List[Dict]) -> Dict:
    """Naive keyword heuristic — NOT the future Business Action Matcher/
    Decision Engine. Picks ERP only when the question's text contains a
    substring match against one of an enabled ERP action's own
    name/action_key/capability/business-label tokens; otherwise RAG.
    This is intentionally simplistic: no scoring, no ML, no learned
    weights — just a plain substring check, appropriate only for this
    integration/wiring sprint's Auto mode."""
    q = (question or "").lower().strip()
    best_action_id = None
    best_token = None
    for a in erp_actions or []:
        if not a.get("enabled", True):
            continue
        tokens = set()
        for key in ("name", "action_key", "capability"):
            if a.get(key):
                tokens.add(str(a[key]).lower())
        for f in (a.get("required_search_fields") or []):
            if f.get("business_label"):
                tokens.add(str(f["business_label"]).lower())
        for tok in tokens:
            if tok and len(tok) >= 3 and tok in q:
                best_action_id = a.get("id")
                best_token = tok
                break
        if best_action_id:
            break
    if best_action_id:
        return {"route": "erp", "action_id": best_action_id,
                "reason": f"naive keyword heuristic — question matched ERP action token {best_token!r}"}
    return {"route": "rag", "action_id": None,
            "reason": "naive keyword heuristic — no ERP action token matched, defaulting to RAG"}


def naive_merge_hybrid_answer(rag_answer: Optional[str], erp_answer: Optional[str]) -> str:
    """Simple, honestly-labeled merge — NOT an intelligent hybrid-answer
    synthesis. If ERP produced an answer, it is shown first (it's the
    more authoritative, structured/transactional source), followed by
    the knowledge-base answer if one exists; otherwise whichever single
    answer is available. This is plain concatenation with section
    labels, never a re-write or LLM-based combination of the two.

    Kept for backward compatibility — superseded by
    services/hybrid_runtime_service.py::synthesize_hybrid_answer()
    (2026-08-02 Hybrid Question Segmentation sprint; re-exported above),
    which additionally handles per-path errors honestly and never
    fabricates a section for a path that produced nothing."""
    parts = []
    if erp_answer:
        parts.append(f"From ERP:\n{erp_answer}")
    if rag_answer:
        parts.append(f"From Knowledge Base:\n{rag_answer}")
    if not parts:
        return "No answer was found from either ERP or the knowledge base."
    return "\n\n".join(parts)
