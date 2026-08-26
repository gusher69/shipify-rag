"""Regression test for rag/lexical_search.py — section_title/chunk_index/
heading_path live in the `metadata` JSONB column on knowledge_chunks, NOT
the always-NULL top-level `section_title` column (see
ingestion/ingest.py's chunk dict shape). A filter that only checks the
top-level column silently matches nothing, which is exactly how "Our
Mission" was invisible to lexical search even after its content and
heading were otherwise correct (confirmed against the real DB during
Phase 2 verification)."""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rag.lexical_search import lexical_search


class TestLexicalSearchQueriesMetadataJsonPath(unittest.TestCase):
    def test_or_filter_includes_metadata_section_title_path(self):
        mock_sb = MagicMock()
        mock_table = mock_sb.table.return_value
        mock_chain = mock_table.select.return_value.eq.return_value
        mock_chain.or_.return_value.limit.return_value.execute.return_value = MagicMock(data=[])

        with patch("admin.routes.get_sb", return_value=mock_sb):
            lexical_search(["What is Shipify's mission?"])

        self.assertTrue(mock_chain.or_.called)
        filter_arg = mock_chain.or_.call_args[0][0]
        self.assertIn("metadata->>section_title.ilike", filter_arg)

    def test_multi_token_query_issues_exactly_one_round_trip(self):
        """Task 05 latency fix (2026-08-26) — confirmed live: a company-
        intent-expanded query variant set tokenizing into 11 distinct
        tokens previously issued 11 SEPARATE, sequential Supabase
        round-trips (one per token), measured at ~3.2s total — the single
        largest contributor to that turn's latency. All tokens' OR
        conditions must now be combined into ONE query, so .execute() is
        called exactly once regardless of how many tokens are involved."""
        mock_sb = MagicMock()
        mock_chain = mock_sb.table.return_value.select.return_value.eq.return_value
        mock_chain.or_.return_value.limit.return_value.execute.return_value = MagicMock(data=[])

        with patch("admin.routes.get_sb", return_value=mock_sb):
            lexical_search(["บริษัทมีนโยบายรีไซเคิลบรรจุภัณฑ์ไหม", "องค์กรมีนโยบายรีไซเคิลบรรจุภัณฑ์ไหม",
                             "ธุรกิจมีนโยบายรีไซเคิลบรรจุภัณฑ์ไหม", "company overview policy"])

        self.assertEqual(mock_chain.or_.return_value.limit.return_value.execute.call_count, 1)
        # Every token's own 4-field OR condition must still be present in
        # the single combined filter string — batching must never drop a
        # token's evidence, only combine it into one round-trip.
        filter_arg = mock_chain.or_.call_args[0][0]
        self.assertIn("content.ilike", filter_arg)
        self.assertIn("metadata->>section_title.ilike", filter_arg)

    def test_returned_chunk_shape_prefers_metadata_over_null_top_level_column(self):
        mock_sb = MagicMock()
        mock_chain = mock_sb.table.return_value.select.return_value.eq.return_value
        mock_chain.or_.return_value.limit.return_value.execute.return_value = MagicMock(data=[{
            "id": "c1", "file_id": "f1", "content": "We aim to make cross-border shipping easy.",
            "source": "company-profile-test.md", "intent": "", "is_active": True,
            "section_title": None,  # the always-NULL legacy top-level column
            "page_number": None, "version": 1,
            "metadata": {"section_title": "Our Mission", "file_name": "company-profile-test.md",
                         "chunk_index": 1, "heading_path": ["Our Mission"]},
        }])

        with patch("admin.routes.get_sb", return_value=mock_sb):
            results = lexical_search(["mission"])

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["section_title"], "Our Mission")
        self.assertEqual(results[0]["chunk_index"], 1)


if __name__ == "__main__":
    unittest.main()
