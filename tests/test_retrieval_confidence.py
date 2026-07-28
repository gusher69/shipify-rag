"""Tests for Phase 2 Part 3 (rag/retrieval_confidence.py) — combines
already-computed retrieval signals into a single 0-1 retrieval_confidence
score, distinct from rag/confidence.py's answer_confidence."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rag.retrieval_confidence import compute_retrieval_confidence


class TestComputeRetrievalConfidence(unittest.TestCase):
    def test_empty_chunks_returns_zero_confidence(self):
        result = compute_retrieval_confidence([])
        self.assertEqual(result["retrieval_confidence"], 0.0)

    def test_score_always_between_zero_and_one(self):
        chunks = [{
            "normalized_vector_score": 0.9, "keyword_score": 0.8, "heading_score": 0.7,
            "metadata_match": {"score": 1.0}, "citation": "Source: a.pdf",
            "has_lexical_evidence": True, "file_name": "a.pdf",
        }]
        result = compute_retrieval_confidence(chunks)
        self.assertGreaterEqual(result["retrieval_confidence"], 0.0)
        self.assertLessEqual(result["retrieval_confidence"], 1.0)

    def test_strong_evidence_scores_higher_than_weak_evidence(self):
        strong = [{
            "normalized_vector_score": 0.9, "keyword_score": 0.9, "heading_score": 0.8,
            "metadata_match": {"score": 1.0}, "citation": "Source: a.pdf",
            "has_lexical_evidence": True, "file_name": "a.pdf",
        }] * 3
        weak = [{
            "normalized_vector_score": 0.1, "keyword_score": 0.0, "heading_score": 0.0,
            "metadata_match": {"score": 0.0}, "citation": None,
            "has_lexical_evidence": False, "file_name": "a.pdf",
        }, {
            "normalized_vector_score": 0.1, "keyword_score": 0.0, "heading_score": 0.0,
            "metadata_match": {"score": 0.0}, "citation": None,
            "has_lexical_evidence": False, "file_name": "b.pdf",
        }]
        strong_result = compute_retrieval_confidence(strong)
        weak_result = compute_retrieval_confidence(weak)
        self.assertGreater(strong_result["retrieval_confidence"], weak_result["retrieval_confidence"])

    def test_all_chunks_from_one_document_maximizes_document_agreement(self):
        chunks = [{"file_name": "a.pdf"}, {"file_name": "a.pdf"}]
        result = compute_retrieval_confidence(chunks)
        self.assertEqual(result["components"]["document_agreement"], 1.0)

    def test_chunks_from_many_documents_lowers_document_agreement(self):
        chunks = [{"file_name": "a.pdf"}, {"file_name": "b.pdf"}, {"file_name": "c.pdf"}, {"file_name": "d.pdf"}]
        result = compute_retrieval_confidence(chunks)
        self.assertLess(result["components"]["document_agreement"], 0.5)

    def test_full_citation_coverage_when_every_chunk_has_a_citation(self):
        chunks = [{"citation": "Source: a.pdf"}, {"citation": "Source: b.pdf"}]
        result = compute_retrieval_confidence(chunks)
        self.assertEqual(result["components"]["citation_coverage"], 1.0)

    def test_structured_calculated_chunks_count_toward_citation_coverage(self):
        chunks = [{"is_calculated": True, "citation": None}]
        result = compute_retrieval_confidence(chunks)
        self.assertEqual(result["components"]["citation_coverage"], 1.0)

    def test_all_components_present_in_output(self):
        result = compute_retrieval_confidence([{"file_name": "a.pdf"}])
        for key in ("semantic_score", "keyword_score", "heading_score", "metadata_score",
                    "citation_coverage", "document_agreement", "chunk_agreement"):
            self.assertIn(key, result["components"])

    def test_never_raises_on_missing_fields(self):
        # A minimal, mostly-empty chunk dict must not raise.
        result = compute_retrieval_confidence([{}])
        self.assertIsInstance(result["retrieval_confidence"], float)


if __name__ == "__main__":
    unittest.main()
