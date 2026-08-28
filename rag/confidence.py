"""Deterministic answer-confidence model (Phase 2, Part 19) — replaces
using raw vector similarity as "answer confidence." A 27% cosine score is
NOT the same thing as "how confident should the admin be in this answer,"
especially now that a chunk can be selected primarily on keyword/heading
evidence with a low vector score (see rag/hybrid_scoring.py).

This is intentionally simple and deterministic (no LLM call, no learned
model) — a conservative, explainable scoring function over the SAME
signals already computed by hybrid_scoring.py, generic across any domain.
"""
from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class ConfidenceResult:
    answer_confidence: float          # 0-1, the ONE number meant for end users/admins
    answerability: str                 # "direct_answer" | "partial_answer" | "no_information"
    raw_vector_similarity: Optional[float]   # top chunk's raw cosine score, shown separately, never relabeled as confidence
    hybrid_retrieval_score: Optional[float]  # top chunk's hybrid_score
    evidence_count: int
    reason: str


def _has_reliable_evidence(c: Dict) -> bool:
    """Answerability Gate (Task 04B, 2026-08-26) — `classification` /
    `evidence_label` (rag/hybrid_scoring.py) are RANKING signals: they can
    legitimately be "direct_evidence" for a chunk that doesn't answer the
    question at all, via a generic query-expansion term
    (expand_company_intent_terms's fixed vocabulary, injected whenever a
    message contains "บริษัท" regardless of what else it asks) or a bare
    Tags-line word ("ขนส่ง"/"นโยบาย" — broad categorical labels shared by
    nearly every FAQ row in the domain). Confirmed live: "บริษัทชดเชย
    คาร์บอนจากการขนส่งหรือไม่" (carbon offset — genuinely absent) and
    "มีนโยบายบริจาคกำไร...ไหม" (profit donation — genuinely absent) both
    got a PERFECT keyword_score, and "direct_evidence"/"direct_answer" at
    0.9 confidence, against completely unrelated FAQ rows.

    Answerability requires evidence that survives EXCLUDING those two
    contamination sources: `has_literal_evidence` (computed with the raw
    literal question only, no expansion, Tags line stripped — see
    rag/hybrid_scoring.py::apply_hybrid_ranking), or an intent-specific
    evidence signal that's independently reliable by construction
    (duration_evidence/log_time_evidence — a real day-count or timestamp
    pattern found directly in the chunk's own text), or a deterministic
    structured/calculated result (never a similarity guess at all)."""
    return bool(
        c.get("has_literal_evidence")
        or c.get("duration_evidence")
        or c.get("log_time_evidence")
        or c.get("is_structured")
        or c.get("is_calculated")
        or c.get("classification") == "structured_deterministic"
    )


_CONTINUITY_STRONG_VECTOR_THRESHOLD = 0.75  # matches confidence_label's own "High" cutoff


def _classify_answerability(chunks: List[Dict], is_calculated: bool, is_continuity_followup: bool = False) -> str:
    if is_calculated:
        return "direct_answer"
    if not chunks:
        return "no_information"
    has_direct = any(c.get("classification") == "direct_evidence" and _has_reliable_evidence(c) for c in chunks)
    has_any_support = any(
        c.get("classification") in ("direct_evidence", "supporting_evidence", "structured_deterministic")
        and _has_reliable_evidence(c) for c in chunks)
    if has_direct:
        return "direct_answer"
    if has_any_support:
        return "partial_answer"
    # Evidence Agreement (customer-demo P0 fix, preserved) — several
    # retrieved chunks that all survived retrieval on the same topic are
    # still a real corroborating signal for a broad question (e.g.
    # "summarize the company"), even with no single chunk individually
    # carrying reliable evidence by the stricter definition above. Task
    # 04B fix: this must only apply to a genuinely UNIFORM weak_semantic
    # pool (the original 2026-07-20 fixture's own scenario — several
    # chunks with NO lexical claim at all, agreeing purely on vector
    # similarity) — never a pool where chunks are already (falsely)
    # LABELED "direct_evidence"/"supporting_evidence" by the contaminated
    # keyword/heading match this Answerability Gate exists to distrust.
    # Confirmed live: "บริษัทมีนโยบายเรื่องการรีไซเคิลกล่องพัสดุอย่างไร"
    # retrieved exactly 3 chunks, ALL mislabeled "direct_evidence" via the
    # same company-intent-expansion contamination — the old unconditional
    # `len(chunks) >= 3` check let that "agreement" through as if it were
    # genuine corroboration, when every member was independently
    # unreliable for the identical reason.
    all_weak_semantic = all(c.get("classification") == "weak_semantic" for c in chunks)
    if len(chunks) >= 3 and all_weak_semantic:
        return "partial_answer"
    # RAG-Continuity Strong-Vector Fallback (2026-08-29) — the two guards
    # above correctly distrust `classification`/`has_lexical_evidence` on
    # their own (contaminated by query-expansion/Tags-line noise), but a
    # RAG-continuity/meta-followup turn (rag/query_resolution.py already
    # resolved it to "<the confirmed prior topic> + <this turn's modifier>"
    # — the caller passes is_continuity_followup=True only for that exact
    # shape, never for a fresh question) is a narrower situation: retrieval
    # was run against a query that already names the established topic, so
    # a chunk the ranking layer independently scored as strong evidence
    # AND that also has a high raw vector/semantic similarity to that same
    # resolved query is trustworthy even without literal-keyword overlap
    # (a rephrasing/simplification request legitimately shares no exact
    # keywords with the FAQ's own wording). Confirmed live: both turns of
    # "ช่วยอธิบายแบบง่ายๆ" / "ตอบเฉพาะเท่าที่ทราบ" retrieved the correct
    # ฝากสั่ง/ฝากนำเข้า FAQ as the top-ranked chunk (normalized_vector_score
    # 0.91 / 1.0) yet fell through to no_information for lack of literal
    # evidence alone. Never applies outside continuity turns, and never
    # lowers the has_literal_evidence bar used above for a fresh question.
    if is_continuity_followup and any(
            c.get("classification") in ("direct_evidence", "supporting_evidence")
            and (c.get("normalized_vector_score") or 0) >= _CONTINUITY_STRONG_VECTOR_THRESHOLD
            for c in chunks):
        return "partial_answer"
    # Only weak_semantic (or nothing) survived, and no reliable evidence
    # anywhere in the pool — genuinely no_information, regardless of
    # whether retrieval happened to return SOME chunks (Top-K being
    # non-empty is never the same thing as the question being answerable).
    return "no_information"


def compute_confidence(chunks: List[Dict], is_continuity_followup: bool = False) -> ConfidenceResult:
    """`chunks` is the FINAL selected-evidence list (already filtered/
    ranked by rag/hybrid_scoring.py) — never the raw candidate pool.
    Confidence is built from: how many chunks actually support the
    answer, whether any has strong (heading/keyword) evidence vs. only a
    vector-similarity guess, and evidence count — never from a single
    cosine number alone."""
    if not chunks:
        return ConfidenceResult(
            answer_confidence=0.0, answerability="no_information",
            raw_vector_similarity=None, hybrid_retrieval_score=None,
            evidence_count=0, reason="No chunks were retrieved or survived relevance filtering.",
        )

    is_calculated = any(c.get("is_calculated") for c in chunks)
    top = chunks[0]
    raw_vector = top.get("score")
    hybrid = top.get("hybrid_score")
    answerability = _classify_answerability(chunks, is_calculated, is_continuity_followup)

    if is_calculated:
        confidence = 0.97
        reason = "A deterministic calculation was performed against the source data (not a similarity guess)."
    elif answerability == "direct_answer":
        # Strong lexical evidence (heading/keyword) is the dominant signal
        # here — a 27% raw vector score with an exact heading match is
        # trustworthy in a way a 27% score with zero lexical support isn't.
        support_bonus = min(0.15, 0.05 * sum(
            1 for c in chunks if c.get("classification") in ("direct_evidence", "supporting_evidence")))
        confidence = min(0.95, 0.75 + support_bonus)
        reason = ("At least one chunk has a direct keyword/heading match for the question — "
                   "confidence reflects lexical evidence strength, not raw vector similarity.")
    elif answerability == "partial_answer":
        has_supporting = any(c.get("classification") == "supporting_evidence" for c in chunks)
        # Evidence Agreement (customer-demo P0 fix): a SINGLE weak/semantic
        # chunk is still a low-confidence guess, but several (3+) retrieved
        # chunks that all survived retrieval on the same topic are a real
        # corroborating signal, not one lucky vector match — e.g. a broad
        # "summarize the company" question that legitimately pulls in
        # multiple related FAQ rows, none of which individually has an
        # exact heading/keyword match. Never applies to a single chunk.
        multi_chunk_agreement = len(chunks) >= 3
        if has_supporting or multi_chunk_agreement:
            confidence = 0.55
            reason = ("Multiple related chunks were retrieved and agree on the same topic — "
                       "confidence reflects that corroborating breadth, not a single weak match.") \
                if multi_chunk_agreement and not has_supporting else \
                ("Related information was found but no chunk directly and fully answers the "
                 "question — confidence reflects partial support.")
        else:
            confidence = 0.35
            reason = ("Related information was found but no chunk directly and fully answers the "
                       "question — confidence reflects partial support.")
    else:
        confidence = 0.15
        reason = "No supporting evidence was found for this question."

    return ConfidenceResult(
        answer_confidence=round(confidence, 4), answerability=answerability,
        raw_vector_similarity=raw_vector, hybrid_retrieval_score=hybrid,
        evidence_count=len(chunks), reason=reason,
    )


def confidence_label(score: float) -> str:
    if score >= 0.75:
        return "High"
    if score >= 0.45:
        return "Medium"
    return "Low"
