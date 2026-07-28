"""Retrieval Settings — deliberately separate from Prompt Studio (which
owns ONLY system prompt / persona / tone / response instructions / prompt
version / channel mapping). Everything here (thresholds, weights, top-k,
search strategy, reranker choice) belongs to the Search/Retrieval Engine,
not the LLM prompt, and must never be edited from Prompt Studio.

Config-file-backed by default (RETRIEVAL_* env vars / module constants
below), with an optional DB override row in `rag_retrieval_settings`
(migrations/021_retrieval_settings.sql) — get_active_settings() checks
the DB first and falls back to the file defaults on any error, so this
works with zero DB setup and can move fully into the DB later without
changing any caller.
"""
import os
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional

SEARCH_STRATEGIES = ("vector_only", "hybrid", "hybrid_graph", "graph_only")
RERANKER_PROVIDERS = ("none", "heuristic", "llm", "cross_encoder")


@dataclass
class RetrievalSettings:
    preset: str = "balanced"
    search_strategy: str = "hybrid"

    candidate_top_k: int = 12
    final_context_top_k: int = 5

    absolute_minimum_score: float = 0.15
    relative_to_best_ratio: float = 0.55
    minimum_candidates: int = 3
    maximum_candidates: int = 12

    semantic_weight: float = 0.50
    keyword_weight: float = 0.20
    heading_weight: float = 0.20
    graph_weight: float = 0.10

    query_expansion_enabled: bool = True
    heading_boost_enabled: bool = True
    graph_search_enabled: bool = False  # no real graph-retrieval signal wired in yet — see report
    reranker_provider: str = "heuristic"

    heading_exact_boost: float = 0.30
    heading_synonym_boost: float = 0.22
    heading_partial_boost: float = 0.12
    important_heading_boost: float = 0.08

    def effective_weights(self) -> Dict[str, float]:
        """Weights actually usable right now — if graph search is
        disabled (or graph_weight is 0), the remaining weights are
        renormalized to sum to 1.0 among themselves, rather than silently
        capping the achievable hybrid_score at (1 - graph_weight)."""
        weights = {"semantic": self.semantic_weight, "keyword": self.keyword_weight,
                   "heading": self.heading_weight, "graph": self.graph_weight}
        if not self.graph_search_enabled:
            weights["graph"] = 0.0
        total = sum(weights.values())
        if total <= 0:
            return {"semantic": 1.0, "keyword": 0.0, "heading": 0.0, "graph": 0.0}
        return {k: v / total for k, v in weights.items()}

    def validation_errors(self) -> List[str]:
        errors = []
        if self.search_strategy not in SEARCH_STRATEGIES:
            errors.append(f"search_strategy must be one of {SEARCH_STRATEGIES}")
        if self.reranker_provider not in RERANKER_PROVIDERS:
            errors.append(f"reranker_provider must be one of {RERANKER_PROVIDERS}")
        if self.candidate_top_k < self.final_context_top_k:
            errors.append("candidate_top_k must be >= final_context_top_k")
        if self.minimum_candidates > self.maximum_candidates:
            errors.append("minimum_candidates must be <= maximum_candidates")
        if not (0.0 <= self.absolute_minimum_score <= 1.0):
            errors.append("absolute_minimum_score must be between 0 and 1")
        if not (0.0 <= self.relative_to_best_ratio <= 1.0):
            errors.append("relative_to_best_ratio must be between 0 and 1")
        raw_sum = self.semantic_weight + self.keyword_weight + self.heading_weight + self.graph_weight
        if abs(raw_sum - 1.0) > 0.01:
            errors.append(f"semantic_weight + keyword_weight + heading_weight + graph_weight must equal "
                          f"1.0 (currently {raw_sum:.3f})")
        return errors

    def as_dict(self) -> Dict:
        return asdict(self)


PRESETS: Dict[str, RetrievalSettings] = {
    "balanced": RetrievalSettings(preset="balanced"),
    "strict": RetrievalSettings(
        preset="strict", candidate_top_k=8, final_context_top_k=4,
        absolute_minimum_score=0.30, relative_to_best_ratio=0.70,
    ),
    "high_recall": RetrievalSettings(
        preset="high_recall", candidate_top_k=20, final_context_top_k=8,
        absolute_minimum_score=0.08, relative_to_best_ratio=0.40,
    ),
}

DEFAULT_PRESET = "balanced"


def get_preset(name: str) -> RetrievalSettings:
    return PRESETS.get(name, PRESETS[DEFAULT_PRESET])


def _settings_from_env() -> RetrievalSettings:
    base = get_preset(os.getenv("RAG_RETRIEVAL_PRESET", DEFAULT_PRESET))
    return RetrievalSettings(
        preset=base.preset,
        search_strategy=os.getenv("RAG_SEARCH_STRATEGY", base.search_strategy),
        candidate_top_k=int(os.getenv("RAG_CANDIDATE_TOP_K", base.candidate_top_k)),
        final_context_top_k=int(os.getenv("RAG_FINAL_CONTEXT_TOP_K", base.final_context_top_k)),
        absolute_minimum_score=float(os.getenv("RAG_ABSOLUTE_MINIMUM_SCORE", base.absolute_minimum_score)),
        relative_to_best_ratio=float(os.getenv("RAG_RELATIVE_TO_BEST_RATIO", base.relative_to_best_ratio)),
        minimum_candidates=int(os.getenv("RAG_MINIMUM_CANDIDATES", base.minimum_candidates)),
        maximum_candidates=int(os.getenv("RAG_MAXIMUM_CANDIDATES", base.maximum_candidates)),
        semantic_weight=float(os.getenv("RAG_SEMANTIC_WEIGHT", base.semantic_weight)),
        keyword_weight=float(os.getenv("RAG_KEYWORD_WEIGHT_V2", base.keyword_weight)),
        heading_weight=float(os.getenv("RAG_HEADING_WEIGHT_V2", base.heading_weight)),
        graph_weight=float(os.getenv("RAG_GRAPH_WEIGHT", base.graph_weight)),
        query_expansion_enabled=os.getenv("RAG_QUERY_EXPANSION_ENABLED", "true").lower() == "true",
        heading_boost_enabled=os.getenv("RAG_HEADING_BOOST_ENABLED", "true").lower() == "true",
        graph_search_enabled=os.getenv("RAG_GRAPH_SEARCH_ENABLED", "false").lower() == "true",
        reranker_provider=os.getenv("RAG_RERANKER_PROVIDER", base.reranker_provider),
    )


_cached: Optional[RetrievalSettings] = None


def get_active_settings(force_reload: bool = False) -> RetrievalSettings:
    """DB override (rag_retrieval_settings, scope_type='global') first,
    falling back to config/env defaults on any error — so this works with
    zero DB setup, and a future admin UI can move the source of truth
    into the DB without any caller changing."""
    global _cached
    if _cached is not None and not force_reload:
        return _cached
    try:
        from ingestion.embedder import _get_supabase
        sb = _get_supabase()
        rows = sb.table("rag_retrieval_settings").select("*").eq("scope_type", "global").limit(1).execute().data
        if rows:
            row = rows[0]
            defaults = _settings_from_env()
            _cached = RetrievalSettings(**{
                f: row.get(f, getattr(defaults, f)) for f in defaults.as_dict()
                if row.get(f) is not None
            })
            return _cached
    except Exception as e:
        print(f"[retrieval_settings] DB read failed, using file defaults: {e}")
    _cached = _settings_from_env()
    return _cached


def save_settings(settings: RetrievalSettings) -> Dict:
    """Upserts the single global settings row. Raises ValueError if the
    settings fail validation — callers (the Settings UI route) must
    surface validation_errors() to the admin before ever calling this."""
    errors = settings.validation_errors()
    if errors:
        raise ValueError("; ".join(errors))
    from ingestion.embedder import _get_supabase
    sb = _get_supabase()
    payload = dict(settings.as_dict())
    payload["scope_type"] = "global"
    payload["scope_id"] = None
    existing = sb.table("rag_retrieval_settings").select("id").eq("scope_type", "global").execute().data
    if existing:
        sb.table("rag_retrieval_settings").update(payload).eq("id", existing[0]["id"]).execute()
    else:
        sb.table("rag_retrieval_settings").insert(payload).execute()
    global _cached
    _cached = settings
    return payload
