"""Regression tests for the Ranking requirement: the original question
always has highest priority; Knowledge Synonym Engine variants
(rag/synonym_service.py) carry LESS weight and can enrich but never
replace/outrank a match the original question already found.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rag.hybrid_scoring import compute_keyword_score, SYNONYM_VARIANT_WEIGHT
from rag.synonym_service import expand_query_with_synonyms


def _chunk(text, file_name="unknown"):
    return {"text": text, "heading_path": [], "section_title": None, "file_name": file_name, "source": file_name}


class TestSynonymVariantDiscount(unittest.TestCase):
    # Deliberately spaced Thai fixtures (same convention as
    # tests/test_hybrid_scoring.py) — Thai script has no word-boundary
    # spaces, so _TOKEN_RE would otherwise lump an entire phrase into one
    # unsplittable token, making token-overlap scoring untestable.
    QUESTION = "ขอ โลเคชั่น โกดัง"

    def test_synonym_only_match_is_discounted_below_full_score(self):
        """A chunk that only the SYNONYM variant ("พิกัด") matches — not
        the original question's own wording ("โลเคชั่น") — must score
        less than the same match would without the discount."""
        chunk_synonym_only = _chunk("เอกสาร พิกัด ของบริษัท")  # matches "พิกัด" only, never "โลเคชั่น"
        variants = expand_query_with_synonyms(self.QUESTION)["variants"]
        synonym_keys = {v.strip().lower() for v in variants[1:]}

        undiscounted = compute_keyword_score(self.QUESTION, chunk_synonym_only, variants)
        discounted = compute_keyword_score(self.QUESTION, chunk_synonym_only, variants, synonym_keys)
        self.assertGreater(undiscounted, 0.0)
        self.assertLess(discounted, undiscounted)
        self.assertAlmostEqual(discounted, undiscounted * SYNONYM_VARIANT_WEIGHT, places=4)

    def test_original_question_match_is_never_discounted(self):
        """A chunk the ORIGINAL question matches directly must keep its
        full score even when synonym_variant_keys is provided — synonym
        variants only discount contributions that come ONLY from them."""
        chunk_original_match = _chunk("ขอ โลเคชั่น โกดัง ด่วน")  # contains the literal original wording
        variants = expand_query_with_synonyms(self.QUESTION)["variants"]
        synonym_keys = {v.strip().lower() for v in variants[1:]}

        undiscounted = compute_keyword_score(self.QUESTION, chunk_original_match, variants)
        discounted = compute_keyword_score(self.QUESTION, chunk_original_match, variants, synonym_keys)
        self.assertGreater(undiscounted, 0.0)
        self.assertEqual(discounted, undiscounted)

    def test_no_synonym_keys_behaves_exactly_as_before(self):
        """Backward compatibility: omitting synonym_variant_keys (every
        existing caller) must produce identical scores to before this
        feature existed."""
        chunk = _chunk("เอกสาร พิกัด ของบริษัท")
        variants = expand_query_with_synonyms(self.QUESTION)["variants"]
        score_no_arg = compute_keyword_score(self.QUESTION, chunk, variants)
        score_none = compute_keyword_score(self.QUESTION, chunk, variants, None)
        self.assertEqual(score_no_arg, score_none)


if __name__ == "__main__":
    unittest.main()
