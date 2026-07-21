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
        confidence."""
        chunks = [{"score": 0.27, "hybrid_score": 0.5, "classification": "direct_evidence"}]
        result = compute_confidence(chunks)
        self.assertEqual(result.answerability, "direct_answer")
        self.assertGreater(result.answer_confidence, 0.7)
        self.assertNotEqual(result.answer_confidence, 0.27)
        self.assertEqual(result.raw_vector_similarity, 0.27)  # preserved separately, never overwritten

    def test_weak_semantic_only_chunk_gives_low_confidence(self):
        chunks = [{"score": 0.9, "hybrid_score": 0.45, "classification": "weak_semantic"}]
        result = compute_confidence(chunks)
        self.assertEqual(result.answerability, "partial_answer")
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
        chunks = [{"score": 0.4, "hybrid_score": 0.3, "classification": "supporting_evidence"}]
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
        chunks = [{"score": 0.27, "hybrid_score": 0.5, "classification": "direct_evidence"}] * 3
        result = compute_confidence(chunks)
        self.assertEqual(result.answerability, "direct_answer")
        self.assertGreaterEqual(result.answer_confidence, 0.75)


class TestConfidenceLabel(unittest.TestCase):
    def test_label_bands(self):
        self.assertEqual(confidence_label(0.9), "High")
        self.assertEqual(confidence_label(0.5), "Medium")
        self.assertEqual(confidence_label(0.1), "Low")


if __name__ == "__main__":
    unittest.main()
