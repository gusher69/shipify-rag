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


_cross_encoder_model = None
_CROSS_ENCODER_MODEL_NAME = "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1"


def _get_cross_encoder_model():
    """Lazily loads the cross-encoder model on first actual use — never
    at import time, so a deployment that never selects this reranker pays
    zero extra startup cost/dependency-download risk. A MULTILINGUAL
    model (mMARCO-trained) is used deliberately, not an English-only one
    (e.g. ms-marco-MiniLM), since this platform is Thai-first (per
    CLAUDE.md) — an English-only cross-encoder would silently
    mis-score Thai query/chunk pairs."""
    global _cross_encoder_model
    if _cross_encoder_model is None:
        from sentence_transformers import CrossEncoder
        _cross_encoder_model = CrossEncoder(_CROSS_ENCODER_MODEL_NAME)
    return _cross_encoder_model


class CrossEncoderReranker(RerankerProvider):
    """Phase 3.6 (2026-08-05) — "Cross-Encoder Re-ranking (if available)"
    per spec. Runs a real cross-encoder model (sentence-transformers) over
    (query, chunk_content) pairs and re-sorts by its relevance score,
    WITHIN the same tier ordering hybrid_scoring.py already established
    (same convention as HeuristicReranker.rerank — never lets a lower
    evidence tier jump ahead of a higher one just because the model liked
    its wording better).

    Genuinely optional, per spec's "(if available)": if the
    sentence-transformers package or model can't be loaded (not
    installed, no network to download the model on first use, etc.), this
    raises a clear, actionable error — exactly like LLMReranker's stub
    already did — rather than silently falling back to a different
    reranker (that would hide a real configuration problem from whoever
    turned this on). Never selected by default (get_reranker's default
    stays 'heuristic'); an admin must explicitly opt in via
    services/retrieval_settings.py, after confirming the added latency
    (a real model inference call per candidate) is acceptable."""

    def rerank(self, query: str, candidates: List[Dict], top_n: int) -> List[Dict]:
        if not candidates:
            return []
        try:
            model = _get_cross_encoder_model()
        except Exception as e:
            raise RuntimeError(
                f"CrossEncoderReranker could not load model '{_CROSS_ENCODER_MODEL_NAME}' "
                f"(sentence-transformers installed? network access to download it on first use?): {e}"
            ) from e

        pairs = [(query, (c.get("content") or c.get("text") or "")[:2000]) for c in candidates]
        scores = model.predict(pairs)
        for c, score in zip(candidates, scores):
            c["rerank_score"] = round(float(score), 4)

        # Same tier-preserving sort as HeuristicReranker — the
        # cross-encoder score only breaks ties WITHIN a tier.
        candidates = sorted(candidates, key=lambda c: (purpose_adjusted_tier(c),
                                                        -c.get("rerank_score", 0.0)))
        return candidates[:top_n]


_PROVIDERS = {
    "none": NoneReranker,
    "heuristic": HeuristicReranker,
    "llm": LLMReranker,
    "cross_encoder": CrossEncoderReranker,
}


def get_reranker(provider: str = "heuristic") -> RerankerProvider:
    cls = _PROVIDERS.get(provider, HeuristicReranker)
    return cls()
