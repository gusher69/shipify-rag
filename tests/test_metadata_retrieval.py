"""Tests for Phase 2 Part 1 (rag/metadata_retrieval.py) — metadata-aware
retrieval scoring. Purely additive: never reorders/drops chunks."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rag.metadata_retrieval import compute_metadata_match, annotate_metadata_match, summarize_metadata_match


class TestComputeMetadataMatch(unittest.TestCase):
    def test_matching_purpose_scores_one(self):
        chunk = {"document_purpose": "coverage_brochure"}
        result = compute_metadata_match("coverage", chunk)
        self.assertEqual(result["score"], 1.0)
        self.assertIn("document_purpose", result["matched_fields"])

    def test_mismatched_purpose_scores_zero(self):
        chunk = {"document_purpose": "premium_monthly"}
        result = compute_metadata_match("coverage", chunk)
        self.assertEqual(result["score"], 0.0)
        self.assertNotIn("document_purpose", result["matched_fields"])

    def test_missing_metadata_never_raises_and_scores_zero(self):
        result = compute_metadata_match("coverage", {})
        self.assertEqual(result["score"], 0.0)
        self.assertEqual(result["checked_fields"], 0)

    def test_no_intent_never_raises(self):
        result = compute_metadata_match(None, {"document_purpose": "coverage_brochure"})
        self.assertEqual(result["score"], 0.0)

    def test_premium_intent_matches_monthly_and_annual(self):
        self.assertEqual(compute_metadata_match("premium", {"document_purpose": "premium_monthly"})["score"], 1.0)
        self.assertEqual(compute_metadata_match("premium", {"document_purpose": "premium_annual"})["score"], 1.0)

    def test_policy_intent_matches_policy_purpose(self):
        self.assertEqual(compute_metadata_match("policy", {"document_purpose": "policy"})["score"], 1.0)


class TestAnnotateMetadataMatch(unittest.TestCase):
    def test_stamps_metadata_match_in_place_without_reordering(self):
        chunks = [
            {"document_purpose": "premium_monthly", "text": "a"},
            {"document_purpose": "coverage_brochure", "text": "b"},
        ]
        annotate_metadata_match(chunks, "coverage")
        self.assertEqual(chunks[0]["text"], "a")  # order unchanged
        self.assertEqual(chunks[1]["text"], "b")
        self.assertIn("metadata_match", chunks[0])
        self.assertIn("metadata_match", chunks[1])
        self.assertEqual(chunks[1]["metadata_match"]["score"], 1.0)
        self.assertEqual(chunks[0]["metadata_match"]["score"], 0.0)

    def test_never_removes_a_chunk(self):
        chunks = [{"document_purpose": "policy"}, {}]
        annotate_metadata_match(chunks, "coverage")
        self.assertEqual(len(chunks), 2)


class TestSummarizeMetadataMatch(unittest.TestCase):
    def test_counts_matched_chunks_and_purposes_seen(self):
        chunks = [
            {"document_purpose": "coverage_brochure"},
            {"document_purpose": "premium_monthly"},
        ]
        annotate_metadata_match(chunks, "coverage")
        summary = summarize_metadata_match(chunks)
        self.assertEqual(summary["total_chunks"], 2)
        self.assertEqual(summary["matched_chunks"], 1)
        self.assertEqual(set(summary["document_purposes_seen"]), {"coverage_brochure", "premium_monthly"})


if __name__ == "__main__":
    unittest.main()
