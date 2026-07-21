"""RAGService — thin, named wrapper around rag/searcher.py.

The actual retrieval logic (embedding, vector search, Excel-engine
routing, attachment enrichment) already lives in rag/searcher.py and is
used in production by line_bot/webhook.py — this wrapper does not
duplicate any of it. It exists so the AI Playground (and any future
caller) depends on a stable "RAGService" name/interface rather than
reaching into rag.searcher's module-level functions directly, and so the
Playground can request a per-stage trace without every caller needing to
know that parameter exists.
"""
from typing import List, Dict, Optional
from rag.searcher import search, format_context


class RAGService:
    def retrieve(self, question: str, top_k: int = 3, trace: Optional[list] = None,
                 excluded_terms: Optional[List[str]] = None,
                 actionable_intent: Optional[str] = None,
                 original_question: Optional[str] = None) -> List[Dict]:
        return search(question, top_k=top_k, trace=trace, excluded_terms=excluded_terms,
                      actionable_intent=actionable_intent, original_question=original_question)

    def build_context(self, chunks: List[Dict]) -> str:
        return format_context(chunks)


_instance = RAGService()


def get_rag_service() -> RAGService:
    return _instance
