"""Reranker provider abstraction (Part 9) — runs AFTER candidate merge +
hybrid scoring, immediately before final top-k truncation. Default
provider is 'heuristic': zero added latency/cost, no external
dependency. 'llm' and 'cross_encoder' are real interface members but not
implemented yet (raise a clear error if ever selected) — never called by
default, so no project using the default config pays any extra cost.
"""
from abc import ABC, abstractmethod
from typing import Dict, List

from rag.hybrid_scoring import tokenize, IMPORTANT_HEADINGS, _normalize_heading, purpose_adjusted_tier


class RerankerProvider(ABC):
    @abstractmethod
    def rerank(self, query: str, candidates: List[Dict], top_n: int) -> List[Dict]:
        ...


class NoneReranker(RerankerProvider):
    """Passthrough — keeps whatever order hybrid scoring already produced."""
    def rerank(self, query: str, candidates: List[Dict], top_n: int) -> List[Dict]:
        for c in candidates:
            c["rerank_score"] = c.get("hybrid_score")
        return candidates[:top_n]


class HeuristicReranker(RerankerProvider):
    """No LLM call, no extra embedding call — combines signals already
    computed by hybrid scoring (semantic/keyword/heading/graph) with a
    few additional deterministic signals: exact vs. synonym keyword
    match, expanded-query match, and generic section/document-title
    relevance. This is the DEFAULT reranker."""

    def _score(self, query: str, c: Dict) -> float:
        hybrid = c.get("hybrid_score") or 0.0
        score = hybrid

        # Exact keyword match on the ORIGINAL (un-expanded) query is the
        # strongest possible signal; a match only via an expanded/synonym
        # variant is still good, but slightly less certain.
        q_tokens = set(tokenize(query))
        matched_query = c.get("matched_query") or query
        is_synonym_match = matched_query.strip().lower() != query.strip().lower()
        if c.get("keyword_score", 0) and c.get("keyword_score", 0) > 0:
            score += 0.05 if not is_synonym_match else 0.03

        if c.get("heading_score", 0) and c.get("heading_score", 0) > 0:
            score += 0.05

        if c.get("evidence_label") in ("direct_heading", "direct_keyword"):
            score += 0.05
        elif c.get("evidence_label") == "synonym_heading":
            score += 0.03

        if c.get("graph_score"):
            score += 0.02 * c["graph_score"]

        # rag/hybrid_scoring.py's purpose-aware boost (document_purpose vs.
        # query_intent — e.g. preferring a coverage brochure chunk over a
        # same-vocabulary premium-rate-table chunk for a benefit question)
        # is already folded into `hybrid` above, but this reranker's own
        # heading/keyword bonuses just above can add up to ~0.10-0.15,
        # easily swamping a single 0.12 boost and silently undoing it.
        # Re-applying it here keeps the purpose signal decisive through
        # reranking too, without changing its magnitude in hybrid_scoring.
        if c.get("purpose_boost"):
            score += c["purpose_boost"]

        # Generic section/document-title relevance — a chunk under a
        # generically "important" heading gets a tiny nudge over one
        # that isn't, all else equal.
        headings = list(c.get("heading_path") or [])
        if c.get("section_title"):
            headings.append(c["section_title"])
        if any(_normalize_heading(h) in IMPORTANT_HEADINGS for h in headings):
            score += 0.01

        return score

    def rerank(self, query: str, candidates: List[Dict], top_n: int) -> List[Dict]:
        for c in candidates:
            c["rerank_score"] = round(self._score(query, c), 4)
        # Preserve the tier ordering (structured > direct > supporting >
        # weak) hybrid_scoring already established — only re-order WITHIN
        # a tier by rerank_score, never let a lower tier jump ahead.
        # purpose_adjusted_tier() (not the raw classification) is used so
        # a purpose match/mismatch shifts a chunk's tier the same way here
        # as it did in rag/hybrid_scoring.py's own sort — otherwise this
        # reranker's re-sort would silently undo that adjustment.
        candidates = sorted(candidates, key=lambda c: (purpose_adjusted_tier(c),
                                                        -c.get("rerank_score", 0.0)))
        return candidates[:top_n]


class LLMReranker(RerankerProvider):
    """Not implemented — calling an LLM to rerank every query by default
    would silently add cost/latency to every request. Select explicitly
    (reranker_provider='llm') only once a real implementation exists."""
    def rerank(self, query: str, candidates: List[Dict], top_n: int) -> List[Dict]:
        raise NotImplementedError(
            "LLMReranker is not implemented yet — select 'heuristic' (default) or 'none'."
        )


class CrossEncoderReranker(RerankerProvider):
    """Not implemented — would require a new model dependency. Select
    explicitly (reranker_provider='cross_encoder') only once available."""
    def rerank(self, query: str, candidates: List[Dict], top_n: int) -> List[Dict]:
        raise NotImplementedError(
            "CrossEncoderReranker is not implemented yet — select 'heuristic' (default) or 'none'."
        )


_PROVIDERS = {
    "none": NoneReranker,
    "heuristic": HeuristicReranker,
    "llm": LLMReranker,
    "cross_encoder": CrossEncoderReranker,
}


def get_reranker(provider: str = "heuristic") -> RerankerProvider:
    cls = _PROVIDERS.get(provider, HeuristicReranker)
    return cls()
