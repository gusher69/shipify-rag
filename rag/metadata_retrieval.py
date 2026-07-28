"""Phase 2 Part 1 of the AI Playground Intelligence Pipeline —
metadata-aware retrieval scoring.

Generalizes the document_purpose vs. query_intent matching already used
by rag/hybrid_scoring.py's purpose-aware ranking boost (see
compute_purpose_boost there) to a wider, extensible set of metadata
fields: document_type, document_purpose, product, category, channel,
language.

This module NEVER hard-filters — every function here produces a SCORE
(0.0-1.0) or an explainability annotation, consumed as an additional
ranking/confidence SIGNAL, never as an exclusion rule. The actual ranking
boost/penalty for document_purpose already lives in
rag/hybrid_scoring.py::compute_purpose_boost() (unchanged by this
module, still the thing that actually moves a chunk's rank) — this
module ADDS a per-chunk `metadata_match` annotation for the
Explainability tab and a generalized 0-1 metadata_score for
rag/retrieval_confidence.py to use as one of its inputs.

Extending this to a new metadata field: add the field name to
METADATA_FIELDS, and (if it should affect the score, not just be
reported) add an intent -> expected-value(s) mapping the same shape as
_INTENT_TO_EXPECTED_PURPOSE. Fields with no such mapping are still
reported in `checked_fields` for visibility, just don't contribute a
match/mismatch to the score yet — safe to call on chunks that don't
carry a given field at all (contributes 0, never raises).
"""
from typing import Dict, List, Optional

# Generic metadata fields this module knows how to score. Each maps to a
# same-named key on the chunk dict (see rag/searcher.py's chunk dict
# construction / services/document_purpose.py for document_purpose) — a
# field simply contributes nothing when absent, so this is safe to call
# on older chunks that don't carry newer metadata yet.
METADATA_FIELDS = ("document_type", "document_purpose", "product", "category", "channel", "language")

# Which intents (rag/query_understanding.py's 9-way Explainability
# classifier) expect which document_purpose values (services/
# document_purpose.py's PURPOSES) — keyed by THIS module's broader
# intent set, not rag/intent_classifier.py's narrower one (see that
# module's own docstring for why the two aren't merged).
_INTENT_TO_EXPECTED_PURPOSE: Dict[str, set] = {
    "coverage": {"coverage_brochure", "faq"},
    "premium": {"premium_monthly", "premium_annual"},
    "company": {"general", "faq"},
    "policy": {"policy"},
    "faq": {"faq"},
}


def compute_metadata_match(intent: Optional[str], chunk: Dict) -> Dict:
    """Returns {"matched_fields": [...], "checked_fields": int, "score":
    float 0..1}. `score` is the fraction of CHECKED fields (fields
    present on the chunk AND with a defined intent expectation) that
    actually matched — a chunk with no scorable metadata at all gets
    score=0.0 rather than being excluded from anything."""
    matched: List[str] = []
    checked = 0

    purpose = chunk.get("document_purpose")
    if purpose and intent:
        expected = _INTENT_TO_EXPECTED_PURPOSE.get(intent)
        if expected:
            checked += 1
            if purpose in expected:
                matched.append("document_purpose")

    score = (len(matched) / checked) if checked else 0.0
    return {"matched_fields": matched, "checked_fields": checked, "score": round(score, 4)}


def annotate_metadata_match(chunks: List[Dict], intent: Optional[str]) -> None:
    """Stamps `metadata_match` onto every chunk IN PLACE — purely
    additive (feeds the Explainability tab and
    rag/retrieval_confidence.py), never reorders or drops anything.
    Actual ranking is already handled elsewhere (rag/hybrid_scoring.py's
    compute_purpose_boost, applied during retrieval, before this runs)."""
    for c in chunks:
        c["metadata_match"] = compute_metadata_match(intent, c)


def summarize_metadata_match(chunks: List[Dict]) -> Dict:
    """A compact summary for the Explainability tab — e.g. "3/5 chunks
    matched document_purpose for this intent" — computed from whatever
    `metadata_match` annotate_metadata_match() already stamped on each
    chunk (call that first; this just aggregates)."""
    total = len(chunks)
    matched = sum(1 for c in chunks if (c.get("metadata_match") or {}).get("matched_fields"))
    purposes = sorted({c.get("document_purpose") for c in chunks if c.get("document_purpose")})
    return {"total_chunks": total, "matched_chunks": matched, "document_purposes_seen": purposes}
