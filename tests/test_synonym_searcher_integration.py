"""Integration check: rag/searcher.py::search() actually runs the
Knowledge Synonym Engine before retrieval and surfaces canonical_terms/
synonyms_used in query_expansion_debug for the Explainability tab.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rag.searcher import search


class TestSynonymEngineWiredIntoSearch(unittest.TestCase):
    @patch("rag.faq_matcher.match_faq_exact", return_value=None)
    def test_trace_includes_synonym_expansion_stage_and_explainability_fields(self, _faq):
        trace = []
        with patch("services.embedding_service.get_embedding_provider") as mock_provider:
            mock_provider.return_value.embed_query.side_effect = RuntimeError("no network in test")
            try:
                search("ขอโลเคชั่นโกดัง", top_k=3, trace=trace)
            except Exception:
                pass

        stage_names = [t["stage"] for t in trace]
        self.assertIn("synonym_expansion", stage_names)

        detail_entry = next(t for t in trace if t["stage"] == "query_expansion_detail")
        qe = detail_entry["query_expansion"]
        self.assertEqual(set(qe["canonical_terms"]), {"พิกัด", "โกดัง"})
        self.assertTrue(any(s["term"] == "โลเคชั่น" and s["canonical"] == "พิกัด" for s in qe["synonyms_used"]))
        self.assertIn("ขอพิกัดโกดัง", qe["expanded_queries"])
        # Original question is still first / always present.
        self.assertEqual(qe["expanded_queries"][0], "ขอโลเคชั่นโกดัง")

    @patch("rag.faq_matcher.match_faq_exact", return_value=None)
    def test_query_with_no_synonym_match_has_empty_canonical_terms(self, _faq):
        trace = []
        with patch("services.embedding_service.get_embedding_provider") as mock_provider:
            mock_provider.return_value.embed_query.side_effect = RuntimeError("no network in test")
            try:
                search("สวัสดีครับ วันนี้อากาศดีมาก", top_k=3, trace=trace)
            except Exception:
                pass

        detail_entry = next(t for t in trace if t["stage"] == "query_expansion_detail")
        qe = detail_entry["query_expansion"]
        self.assertEqual(qe["canonical_terms"], [])
        self.assertEqual(qe["synonyms_used"], [])


if __name__ == "__main__":
    unittest.main()
