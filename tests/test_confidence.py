"""Regression tests for rag/confidence.py — Phase 2's deterministic
answer-confidence model, which must NOT equal raw vector similarity."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rag.confidence import compute_confidence, confidence_label


class TestComputeConfidence(unittest.TestCase):
    def test_empty_chunks_is_no_information(self):
        result = compute_confidence([])
        self.assertEqual(result.answerability, "no_information")
        self.assertLess(result.answer_confidence, 0.3)

    def test_direct_evidence_chunk_gives_high_confidence_even_with_low_vector_score(self):
        """The exact case from the audit: a 27% (0.27) raw vector score
        with strong keyword/heading evidence must NOT be reported as 27%
        confidence. `has_literal_evidence` (Task 04B, 2026-08-26) marks
        this as GENUINE evidence, not a classification earned only via a
        contaminated generic-expansion/Tags-line match (see
        TestAnswerabilityGate below for that distinction)."""
        chunks = [{"score": 0.27, "hybrid_score": 0.5, "classification": "direct_evidence",
                   "has_literal_evidence": True}]
        result = compute_confidence(chunks)
        self.assertEqual(result.answerability, "direct_answer")
        self.assertGreater(result.answer_confidence, 0.7)
        self.assertNotEqual(result.answer_confidence, 0.27)
        self.assertEqual(result.raw_vector_similarity, 0.27)  # preserved separately, never overwritten

    def test_weak_semantic_only_chunk_gives_low_confidence(self):
        """Answerability Gate fix (Task 04B, 2026-08-26): a PURE
        vector-similarity guess with zero real (literal/intent/
        structured) evidence is genuinely no_information now, not a
        "partial_answer" — Top-K being non-empty was never proof the
        question is answerable. Confidence stays low either way (this
        test's original point); only the label changed."""
        chunks = [{"score": 0.9, "hybrid_score": 0.45, "classification": "weak_semantic"}]
        result = compute_confidence(chunks)
        self.assertEqual(result.answerability, "no_information")
        self.assertLess(result.answer_confidence, 0.5)
        # Raw vector score is high (0.9) but confidence must stay low —
        # this is exactly the "do not use raw vector score as confidence" rule.
        self.assertLess(result.answer_confidence, result.raw_vector_similarity)

    def test_calculated_result_gets_near_max_confidence(self):
        chunks = [{"score": 1.0, "is_calculated": True, "classification": "structured_deterministic"}]
        result = compute_confidence(chunks)
        self.assertEqual(result.answerability, "direct_answer")
        self.assertGreaterEqual(result.answer_confidence, 0.9)

    def test_supporting_evidence_gives_medium_confidence(self):
        chunks = [{"score": 0.4, "hybrid_score": 0.3, "classification": "supporting_evidence",
                   "has_literal_evidence": True}]
        result = compute_confidence(chunks)
        self.assertEqual(result.answerability, "partial_answer")
        self.assertGreater(result.answer_confidence, 0.3)
        self.assertLess(result.answer_confidence, 0.8)

    def test_raw_and_hybrid_scores_are_exposed_separately_from_confidence(self):
        chunks = [{"score": 0.27, "hybrid_score": 0.5, "classification": "direct_evidence"}]
        result = compute_confidence(chunks)
        self.assertEqual(result.raw_vector_similarity, 0.27)
        self.assertEqual(result.hybrid_retrieval_score, 0.5)
        self.assertNotEqual(result.raw_vector_similarity, result.answer_confidence)


class TestEvidenceAgreement(unittest.TestCase):
    """Customer-demo P0 fix (2026-07-20): several weak_semantic chunks
    that all survived retrieval on the same topic (e.g. a broad "summarize
    the company" question with no single exact-match chunk) must read as
    more trustworthy than one lucky weak match — without inflating a
    single weak chunk's confidence."""

    def test_single_weak_semantic_chunk_stays_low(self):
        chunks = [{"score": 0.3, "hybrid_score": 0.5, "classification": "weak_semantic"}]
        result = compute_confidence(chunks)
        self.assertLess(result.answer_confidence, 0.5)

    def test_three_or_more_weak_semantic_chunks_read_as_medium(self):
        chunks = [{"score": 0.3, "hybrid_score": 0.5, "classification": "weak_semantic"} for _ in range(3)]
        result = compute_confidence(chunks)
        self.assertEqual(result.answerability, "partial_answer")
        self.assertGreaterEqual(result.answer_confidence, 0.5)

    def test_direct_evidence_is_unaffected_by_evidence_agreement(self):
        """The evidence-agreement bump only applies inside the
        partial_answer branch — a direct_answer's own (higher) confidence
        must not change just because more chunks happen to be present."""
        chunks = [{"score": 0.27, "hybrid_score": 0.5, "classification": "direct_evidence",
                   "has_literal_evidence": True}] * 3
        result = compute_confidence(chunks)
        self.assertEqual(result.answerability, "direct_answer")
        self.assertGreaterEqual(result.answer_confidence, 0.75)


class TestConfidenceLabel(unittest.TestCase):
    def test_label_bands(self):
        self.assertEqual(confidence_label(0.9), "High")
        self.assertEqual(confidence_label(0.5), "Medium")
        self.assertEqual(confidence_label(0.1), "Low")


class TestAnswerabilityGate(unittest.TestCase):
    """Task 04B — Fix RAG Answerability + Unknown-Query Safety
    (2026-08-26). Confirmed live root cause: `classification` (rag/
    hybrid_scoring.py) is a RANKING signal — a chunk can legitimately be
    "direct_evidence" (a "perfect" keyword_score) via TWO contamination
    sources that have nothing to do with the customer's actual question:
    (1) rag/query_understanding.py::expand_company_intent_terms's FIXED
    generic vocabulary ("บริการ", "นำเข้าสินค้าจากจีน", ...), auto-injected
    as extra query variants whenever the message merely CONTAINS "บริษัท"
    regardless of what else it asks; (2) a bare word from a chunk's own
    "Tags: ..." line (a broad categorical label shared by nearly every
    FAQ row in the domain, e.g. "ขนส่ง"/"นโยบาย") happening to appear
    anywhere in a long, glued customer sentence.

    Confirmed live: "บริษัทมีนโยบายเรื่องการรีไซเคิลกล่องพัสดุอย่างไร"
    (packaging recycling — genuinely absent) and "บริษัทชดเชยคาร์บอน...
    หรือไม่" (carbon offset — genuinely absent) both scored a "perfect"
    keyword_score and "direct_answer" at 0.9 confidence against
    completely unrelated shipping/service FAQ rows, and the LLM then
    answered confidently using that unrelated content.

    Fix: `has_literal_evidence` (rag/hybrid_scoring.py::apply_hybrid_
    ranking) is a SEPARATE, additive signal computed with ONLY the raw
    literal question (no expansion/synonym variants) against a Tags-
    line-stripped haystack — genuine evidence the customer's OWN words
    overlap with the chunk's actual Question/Answer content, immune to
    both contamination sources. `_has_reliable_evidence` (this module)
    requires `has_literal_evidence` OR an independently-reliable
    intent-specific signal (duration_evidence/log_time_evidence — a real
    day-count/timestamp pattern found directly in the chunk's own text)
    OR a deterministic structured/calculated result, before a chunk's
    classification counts toward "direct_answer"/"partial_answer" — Top-K
    being non-empty was never proof the question is answerable.

    This is purely a RANKING-independent answerability signal — none of
    these tests touch `hybrid_score`/`classification`/chunk ORDER, so
    Task 04's own ranking fix (see TestTask04ShippingDurationGrounding in
    tests/test_hybrid_scoring.py) is completely unaffected."""

    # ---- Confirmed real production false positives, reproduced ----

    def test_company_intent_expansion_contamination_no_longer_answerable(self):
        """"บริษัทมีนโยบาย...รีไซเคิล..." — matched only via
        expand_company_intent_terms's fixed "บริการ" term, never the
        customer's own words. Reproduced with the real winning chunk's
        actual content (no literal overlap with "รีไซเคิล"/"นโยบาย"/
        "กล่องพัสดุ" anywhere in its Question/Answer/Alternative
        phrasings)."""
        chunks = [{
            "score": 0.2118, "hybrid_score": 1.0698, "classification": "direct_evidence",
            "keyword_score": 1.0, "has_literal_evidence": False,
            "duration_evidence": False, "log_time_evidence": False,
        }]
        result = compute_confidence(chunks)
        self.assertEqual(result.answerability, "no_information")
        self.assertLess(result.answer_confidence, 0.3)

    def test_tags_line_only_contamination_no_longer_answerable(self):
        """"บริษัทชดเชยคาร์บอน...หรือไม่" / "มีนโยบายบริจาคกำไร...ไหม" —
        matched only via a bare "ขนส่ง"/"นโยบาย" word from the winning
        chunk's OWN Tags line, not its actual content."""
        chunks = [{
            "score": 0.3115, "hybrid_score": 1.0821, "classification": "direct_evidence",
            "keyword_score": 1.0, "has_literal_evidence": False,
            "duration_evidence": False, "log_time_evidence": False,
        }]
        result = compute_confidence(chunks)
        self.assertEqual(result.answerability, "no_information")

    def test_evidence_agreement_does_not_launder_contaminated_pool(self):
        """The pre-existing 3+-chunk "Evidence Agreement" corroboration
        bump (customer-demo P0 fix) must not fire just because several
        chunks all happen to be mislabeled "direct_evidence" by the SAME
        contamination source — confirmed live: the recycling-policy query
        retrieved exactly 3 such chunks, all equally unreliable, and the
        old unconditional `len(chunks) >= 3` check let that "agreement"
        through as if it were genuine corroboration."""
        chunks = [{"score": s, "hybrid_score": h, "classification": "direct_evidence",
                   "has_literal_evidence": False, "duration_evidence": False, "log_time_evidence": False}
                  for s, h in [(0.2118, 1.0698), (0.2613, 0.9278), (0.2442, 0.8732)]]
        result = compute_confidence(chunks)
        self.assertEqual(result.answerability, "no_information")

    def test_genuine_weak_semantic_agreement_still_works(self):
        """The ORIGINAL Evidence Agreement scenario (customer-demo P0 fix)
        must still work: several genuinely weak_semantic (no lexical
        claim at all) chunks that agree on the same broad topic remain a
        real corroborating signal."""
        chunks = [{"score": 0.3, "hybrid_score": 0.5, "classification": "weak_semantic"} for _ in range(3)]
        result = compute_confidence(chunks)
        self.assertEqual(result.answerability, "partial_answer")
        self.assertGreaterEqual(result.answer_confidence, 0.5)

    # ---- Known-good queries must remain answerable (Phase 7/8) ----

    def test_duration_evidence_alone_makes_a_chunk_answerable(self):
        """"ทางรถกี่วัน"/"ช่วงนี้ขนส่งทางรถใช้เวลานานไหมครับ" — the winning
        chunk's own duration_evidence (Task 04's mechanism, a real
        day-count pattern in its text) is independently reliable evidence
        even when has_literal_evidence is False (a long, glued natural
        sentence with no exact literal overlap after Tags/expansion are
        excluded)."""
        chunks = [{"score": 0.45, "hybrid_score": 1.28, "classification": "direct_evidence",
                   "has_literal_evidence": False, "duration_evidence": True, "log_time_evidence": False}]
        result = compute_confidence(chunks)
        self.assertEqual(result.answerability, "direct_answer")

    def test_mixed_topic_chunk_stays_answerable_for_its_real_evidence(self):
        """Task 04's mixed-topic chunk ("เรทเท่าไหร่คะ" — titled as pricing,
        body genuinely contains the real "ทางรถ...7-10วัน" duration
        answer): a title mismatch alone must never make it unanswerable
        — it has real duration_evidence, which is what matters."""
        chunks = [{"score": 0.45, "hybrid_score": 1.28, "classification": "direct_evidence",
                   "duration_evidence": True, "has_literal_evidence": False, "log_time_evidence": False}]
        result = compute_confidence(chunks)
        self.assertEqual(result.answerability, "direct_answer")

    def test_short_specific_domain_word_gives_literal_evidence(self):
        """"คูปองใช้ยังไง" regression (found during implementation): a
        short but genuinely SPECIFIC domain word ("คูปอง") that IS the
        customer's own real content (not a generic company-expansion
        term, not merely a Tags-line word) must count as literal
        evidence, not be rejected purely for being short."""
        from rag.hybrid_scoring import compute_keyword_score
        chunk = {
            "text": ("Question: ใช้คูปองยังไง\nAnswer: คูปองสามารถใช้ลดค่านำเข้าเเละค่าส่งในไทยได้ค่ะ\n"
                     "Alternative phrasings: ใช้โค้ดคูปองตรงไหน / ใส่คูปองตอนชำระยังไง"),
            "section_title": "Question: ใช้คูปองยังไง",
        }
        score = compute_keyword_score("คูปองใช้ยังไง", chunk, query_variants=["คูปองใช้ยังไง"],
                                       exclude_tags_line=True)
        self.assertGreaterEqual(score, 0.5)

    def test_generic_short_morpheme_still_rejected_for_literal_evidence(self):
        """The SAME short-word allowance must not reopen the "เกี่ยว"
        coincidental-morpheme false positive (a small fraction of a much
        longer, still up-answered query) — proportional gating rejects
        it, absolute gating alone (before this fix) would too."""
        from rag.hybrid_scoring import compute_keyword_score
        chunk = {"text": "เนื้อหาที่ไม่เกี่ยวข้องกันเลยเรื่องอื่นโดยสิ้นเชิง", "section_title": "หัวข้ออื่น"}
        score = compute_keyword_score("บริษัทนี้ทำธุรกิจเกี่ยวกับอะไร", chunk,
                                       query_variants=["บริษัทนี้ทำธุรกิจเกี่ยวกับอะไร"], exclude_tags_line=True)
        self.assertEqual(score, 0.0)

    def test_structured_calculated_result_always_answerable(self):
        chunks = [{"score": 1.0, "is_calculated": True, "classification": "structured_deterministic"}]
        result = compute_confidence(chunks)
        self.assertEqual(result.answerability, "direct_answer")

    def test_empty_retrieval_is_no_information(self):
        result = compute_confidence([])
        self.assertEqual(result.answerability, "no_information")

    def test_low_relevance_top_k_is_no_information(self):
        """Results exist but are all irrelevant (pure vector guesses, no
        real evidence of any kind) — Top-K being non-empty is never the
        same thing as the question being answerable."""
        chunks = [{"score": 0.9, "hybrid_score": 0.45, "classification": "weak_semantic"}]
        result = compute_confidence(chunks)
        self.assertEqual(result.answerability, "no_information")


if __name__ == "__main__":
    unittest.main()
