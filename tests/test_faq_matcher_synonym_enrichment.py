"""Regression tests for rag/faq_matcher.py::match_faq_exact()'s
extra_queries parameter — the seam that lets the Knowledge Synonym Engine
(rag/synonym_service.py) enrich FAQ exact matching (a row phrased with a
synonym can still be found) WITHOUT ever letting a synonym variant
outrank the user's own original wording.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rag.faq_matcher import match_faq_exact

WAREHOUSE_ROW = {
    "id": "row-1", "row_index": 1, "sheet_name": "faq.xlsx",
    "question": "ขอพิกัดโกดัง",
    "alt_questions": [],
    "answer": "พิกัดโกดัง: https://maps.example.com/warehouse",
    "chunk_id": "chunk-1", "knowledge_file_id": "file-1",
}


def _mock_supabase_with_rows(rows):
    sb = MagicMock()
    sb.table.return_value.select.return_value.is_.return_value.execute.return_value.data = rows
    return sb


class TestMatchFaqExactWithExtraQueries(unittest.TestCase):
    def test_original_question_alone_does_not_match_a_synonym_phrased_row(self):
        """Without synonym enrichment, "ขอโลเคชั่นโกดัง" doesn't literally
        match a row asking "ขอพิกัดโกดัง" — establishes the gap this
        feature closes."""
        with patch("rag.searcher._get_supabase", return_value=_mock_supabase_with_rows([WAREHOUSE_ROW])):
            result = match_faq_exact("ขอโลเคชั่นโกดัง")
        self.assertIsNone(result)

    def test_synonym_expanded_variant_finds_the_row(self):
        with patch("rag.searcher._get_supabase", return_value=_mock_supabase_with_rows([WAREHOUSE_ROW])):
            result = match_faq_exact("ขอโลเคชั่นโกดัง", extra_queries=["ขอพิกัดโกดัง", "ขอแผนที่โกดัง"])
        self.assertIsNotNone(result)
        self.assertIs(result["row"], WAREHOUSE_ROW)

    def test_exact_match_on_original_question_short_circuits_before_extras_are_tried(self):
        """If the ORIGINAL question already exactly matches, extra_queries
        must never be consulted at all — original always wins first."""
        with patch("rag.searcher._get_supabase", return_value=_mock_supabase_with_rows([WAREHOUSE_ROW])):
            result = match_faq_exact("ขอพิกัดโกดัง", extra_queries=["some unrelated synonym variant"])
        self.assertEqual(result["match_type"], "exact")
        self.assertEqual(result["matched_text"], "ขอพิกัดโกดัง")

    def test_no_extra_queries_behaves_exactly_as_before(self):
        """Backward compatibility: omitting extra_queries entirely (every
        pre-existing caller) is unaffected."""
        with patch("rag.searcher._get_supabase", return_value=_mock_supabase_with_rows([WAREHOUSE_ROW])):
            result = match_faq_exact("ขอพิกัดโกดัง")
        self.assertIsNotNone(result)
        self.assertEqual(result["match_type"], "exact")


if __name__ == "__main__":
    unittest.main()
