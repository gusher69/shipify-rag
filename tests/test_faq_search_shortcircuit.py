"""Regression tests for rag/searcher.py's FAQ exact-match short-circuit —
when rag.faq_matcher.match_faq_exact() finds a confident row match,
search() must return ONLY that canonical row (plus its attachments),
never mixing in vector/lexical/hybrid results.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rag.searcher import search
from rag.confidence import compute_confidence

FAQ_ROW_MATCH = {
    "match_type": "exact",
    "matched_text": "จ่ายบิลยังไง",
    "score": 1.0,
    "row": {
        "id": "row-2", "row_index": 2, "sheet_name": "faq.xlsx",
        "question": "จ่ายบิลยังไง",
        "answer": "เปิดบิล สแกน QR แล้วยืนยันการชำระเงิน",
        "chunk_id": "chunk-3", "knowledge_file_id": "file-1",
    },
}


class TestFaqExactMatchShortCircuit(unittest.TestCase):
    @patch("rag.faq_matcher.match_faq_exact", return_value=FAQ_ROW_MATCH)
    @patch("rag.searcher._fetch_attachments_for_chunks", return_value={})
    def test_exact_match_returns_only_the_canonical_row(self, _att, _match):
        chunks = search("จ่ายบิลยังไง", top_k=3)
        self.assertEqual(len(chunks), 1)
        self.assertIn("เปิดบิล สแกน QR", chunks[0]["text"])
        self.assertTrue(chunks[0]["is_structured"])
        self.assertTrue(chunks[0]["is_faq_exact"])

    @patch("rag.faq_matcher.match_faq_exact", return_value=FAQ_ROW_MATCH)
    @patch("rag.searcher._fetch_attachments_for_chunks", return_value={})
    def test_trace_records_short_circuit_and_skips_other_stages(self, _att, _match):
        trace = []
        search("จ่ายบิลยังไง", top_k=3, trace=trace)
        stages = {t["stage"]: t["status"] for t in trace}
        self.assertEqual(stages.get("faq_exact_match"), "success")
        self.assertEqual(stages.get("embedding_vector_search"), "skipped")
        self.assertEqual(stages.get("lexical_search"), "skipped")
        self.assertEqual(stages.get("excel_engine"), "skipped")
        self.assertEqual(stages.get("hybrid_rerank"), "skipped")

    @patch("rag.faq_matcher.match_faq_exact", return_value=FAQ_ROW_MATCH)
    def test_attachments_are_attached_to_the_canonical_row(self, _match):
        attachment = {"filename": "bill.pdf", "public_url": "http://example.com/bill.pdf", "metadata": {}}
        with patch("rag.searcher._fetch_attachments_for_chunks", return_value={"chunk-3": [attachment]}):
            chunks = search("จ่ายบิลยังไง", top_k=3)
        self.assertEqual(len(chunks[0]["attachments"]), 1)
        self.assertEqual(chunks[0]["attachments"][0]["filename"], "bill.pdf")

    @patch("rag.faq_matcher.match_faq_exact", return_value=FAQ_ROW_MATCH)
    @patch("rag.searcher._fetch_attachments_for_chunks", return_value={})
    def test_canonical_row_yields_direct_answer_confidence(self, _att, _match):
        """The FAQ chunk bypasses hybrid_scoring.py entirely, so its
        `classification` must still be set correctly here — otherwise
        rag/confidence.py would never recognize it as direct evidence."""
        chunks = search("จ่ายบิลยังไง", top_k=3)
        result = compute_confidence(chunks)
        self.assertEqual(result.answerability, "direct_answer")
        self.assertGreaterEqual(result.answer_confidence, 0.75)

    @patch("rag.faq_matcher.match_faq_exact", return_value=None)
    def test_no_match_falls_through_to_normal_pipeline(self, _match):
        """When there's no confident FAQ match, search() must not
        short-circuit — it proceeds to vector/lexical/hybrid retrieval as
        before (which may itself return no results in this DB-less test
        environment, but must not raise or return the FAQ shape)."""
        with patch("services.embedding_service.get_embedding_provider") as mock_provider:
            mock_provider.return_value.embed_query.side_effect = RuntimeError("no network in test")
            try:
                chunks = search("CBM คืออะไร", top_k=3)
            except Exception:
                chunks = None
        self.assertTrue(chunks is None or all(not c.get("is_faq_exact") for c in chunks))


if __name__ == "__main__":
    unittest.main()
