"""Phase 2 Part 3 of the AI Playground Intelligence Pipeline — Retrieval
Confidence. Combines multiple already-computed retrieval SIGNALS into
one `retrieval_confidence` float in [0, 1], calculated on the FINAL
context chunk set BEFORE the LLM is called.

This is deliberately a DIFFERENT metric from rag/confidence.py's
answer_confidence: that module scores evidence STRENGTH per chunk to
produce an answerability verdict (direct_answer/partial_answer/
no_information) for the FINAL ANSWER. retrieval_confidence instead
measures how much the RETRIEVAL STAGE agrees with itself — do the top
chunks come from the same document, do they carry real lexical evidence,
is every chunk actually traceable to a citation. Both are returned
side-by-side (see services/playground_orchestrator.py); neither replaces
the other.

Purely additive: this never changes what gets sent to the LLM, only
reports a number for the Explainability tab. No LLM calls, no external
dependency — every input is a field already computed by rag/
hybrid_scoring.py or rag/metadata_retrieval.py.
"""
from typing import Dict, List, Optional

# Equal-weighted by default — each signal contributes 1/len(components)
# to the final score. Extending this: add a new key to the components
# dict built in compute_retrieval_confidence() below and it's picked up
# automatically (the average is computed over whatever keys exist).
_DEFAULT_COMPONENT_WEIGHT = 1.0


def _avg(values: List[Optional[float]]) -> float:
    present = [v for v in values if v is not None]
    return sum(present) / len(present) if present else 0.0


def compute_retrieval_confidence(chunks: List[Dict]) -> Dict:
    """Returns {"retrieval_confidence": float, "components": {...}}.

    components:
      semantic_score      - avg normalized vector similarity across chunks
      keyword_score       - avg literal keyword-overlap score
      heading_score       - avg heading-match score
      metadata_score      - avg metadata_match score (rag/metadata_retrieval.py)
      citation_coverage   - fraction of chunks with a real citation/source
      document_agreement  - 1/num_distinct_source_documents (all-one-doc = 1.0)
      chunk_agreement     - fraction of chunks with real lexical evidence
                            (has_lexical_evidence — see rag/hybrid_scoring.py)
    """
    empty_components = {
        "semantic_score": 0.0, "keyword_score": 0.0, "heading_score": 0.0,
        "metadata_score": 0.0, "citation_coverage": 0.0,
        "document_agreement": 0.0, "chunk_agreement": 0.0,
    }
    if not chunks:
        return {"retrieval_confidence": 0.0, "components": empty_components}

    semantic = _avg([c.get("normalized_vector_score") for c in chunks])
    keyword = _avg([c.get("keyword_score") for c in chunks])
    heading = _avg([c.get("heading_score") for c in chunks])
    metadata = _avg([(c.get("metadata_match") or {}).get("score") for c in chunks])

    with_citation = sum(1 for c in chunks if c.get("citation") or c.get("is_structured") or c.get("is_calculated"))
    citation_coverage = with_citation / len(chunks)

    sources = {c.get("file_name") or c.get("source") for c in chunks if (c.get("file_name") or c.get("source"))}
    document_agreement = (1.0 / len(sources)) if sources else 0.0

    lexical_chunks = sum(1 for c in chunks if c.get("has_lexical_evidence") or c.get("is_structured") or c.get("is_calculated"))
    chunk_agreement = lexical_chunks / len(chunks)

    components = {
        "semantic_score": round(semantic, 4),
        "keyword_score": round(keyword, 4),
        "heading_score": round(heading, 4),
        "metadata_score": round(metadata, 4),
        "citation_coverage": round(citation_coverage, 4),
        "document_agreement": round(document_agreement, 4),
        "chunk_agreement": round(chunk_agreement, 4),
    }

    retrieval_confidence = sum(components.values()) / len(components)
    retrieval_confidence = min(1.0, max(0.0, retrieval_confidence))

    return {"retrieval_confidence": round(retrieval_confidence, 4), "components": components}
