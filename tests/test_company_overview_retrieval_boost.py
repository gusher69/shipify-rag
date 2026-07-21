"""Regression tests for the company-overview retrieval fix (P0,
2026-07-20): "บริษัทนี้ทำธุรกิจเกี่ยวกับอะไร" was losing to lexically
closer but less-relevant FAQ rows ("มีบริการอะไรบ้าง", "ขอเบอร์ติดต่อ",
"มีบริการตีลังไม้ไหม"). Covers:
  1. rag/query_understanding.py::expand_company_intent_terms()
  2. rag/hybrid_scoring.py::compute_company_intent_boost()
  3. apply_hybrid_ranking() end-to-end ranking with company_intent=True
  4. Never lowers the adaptive relevance threshold / never fires for any
     other intent.
"""
import unittest

from rag.query_understanding import expand_company_intent_terms, detect_intent
from rag.hybrid_scoring import apply_hybrid_ranking, compute_company_intent_boost


class TestCompanyIntentExpansion(unittest.TestCase):
    def test_company_question_gets_expansion_terms(self):
        terms = expand_company_intent_terms("บริษัทนี้ทำธุรกิจเกี่ยวกับอะไร")
        self.assertIn("บริษัท", terms)
        self.assertIn("ธุรกิจ", terms)
        self.assertIn("นำเข้าสินค้าจากจีน", terms)
        self.assertIn("Shipping", terms)

    def test_non_company_question_gets_no_expansion(self):
        self.assertEqual(expand_company_intent_terms("ค่าขนส่งคิดยังไง"), [])
        self.assertNotEqual(detect_intent("ค่าขนส่งคิดยังไง"), "company")


class TestComputeCompanyIntentBoost(unittest.TestCase):
    def test_returns_zero_when_company_intent_is_false(self):
        chunk = {"text": "Question: บริษัททำธุรกิจเกี่ยวกับอะไร\nAnswer: ..."}
        self.assertEqual(compute_company_intent_boost(False, chunk), 0.0)

    def test_exact_company_overview_faq_gets_decisive_boost(self):
        chunk = {"text": "Question: บริษัททำธุรกิจเกี่ยวกับอะไร\nAnswer: นำเข้าสินค้าจากจีน"}
        boost = compute_company_intent_boost(True, chunk)
        self.assertGreaterEqual(boost, 5.0)

    def test_services_faq_gets_supporting_boost(self):
        chunk = {"text": "Question: มีบริการอะไรบ้าง\nAnswer: ฝากสั่ง ฝากนำเข้า"}
        boost = compute_company_intent_boost(True, chunk)
        self.assertGreater(boost, 0.0)
        self.assertLess(boost, 5.0)

    def test_china_import_shipping_keyword_gets_small_boost(self):
        chunk = {"text": "ระยะเวลาขนส่งจากจีนใช้เวลากี่วัน"}
        boost = compute_company_intent_boost(True, chunk)
        self.assertGreater(boost, 0.0)

    def test_unrelated_chunk_gets_no_boost(self):
        chunk = {"text": "ขอเบอร์ติดต่อฝ่ายบริการลูกค้า", "section_title": "Question: ขอเบอร์ติดต่อ"}
        self.assertEqual(compute_company_intent_boost(True, chunk), 0.0)


class TestApplyHybridRankingCompanyIntent(unittest.TestCase):
    def _base_chunk(self, text, section_title, score):
        return {
            "text": text, "section_title": section_title, "score": score,
            "chunk_index": 0, "file_name": "AI Knowledge Master.xlsx",
        }

    def test_exact_overview_faq_ranks_first_even_with_lower_vector_score(self):
        """The core regression scenario: an exact company-overview FAQ row
        with a LOWER raw vector score than an unrelated-but-lexically-
        closer FAQ row must still rank first once company_intent=True."""
        chunks = [
            self._base_chunk("Question: ขอเบอร์ติดต่อ\nAnswer: 02-026-6426",
                              "Question: ขอเบอร์ติดต่อ", 0.35),
            self._base_chunk("Question: มีบริการตีลังไม้ไหม\nAnswer: มีค่ะ",
                              "Question: มีบริการตีลังไม้ไหม", 0.33),
            self._base_chunk("Question: บริษัททำธุรกิจเกี่ยวกับอะไร\nAnswer: นำเข้าสินค้าจากจีน",
                              "Question: บริษัททำธุรกิจเกี่ยวกับอะไร", 0.20),
        ]
        kept = apply_hybrid_ranking("บริษัทนี้ทำธุรกิจเกี่ยวกับอะไร", chunks, company_intent=True)
        self.assertEqual(kept[0]["section_title"], "Question: บริษัททำธุรกิจเกี่ยวกับอะไร")

    def test_no_boost_applied_when_company_intent_false(self):
        """Never affects ranking for a non-company query — regression
        guard against this boost leaking into unrelated retrieval."""
        chunks = [
            self._base_chunk("Question: ขอเบอร์ติดต่อ\nAnswer: 02-026-6426",
                              "Question: ขอเบอร์ติดต่อ", 0.35),
            self._base_chunk("Question: บริษัททำธุรกิจเกี่ยวกับอะไร\nAnswer: นำเข้าสินค้าจากจีน",
                              "Question: บริษัททำธุรกิจเกี่ยวกับอะไร", 0.20),
        ]
        kept = apply_hybrid_ranking("ค่าขนส่งคิดยังไง", chunks, company_intent=False)
        for c in kept:
            self.assertEqual(c.get("company_intent_boost"), 0.0)

    def test_never_lowers_the_adaptive_relevance_threshold(self):
        """A pool of chunks with zero lexical/company evidence at all must
        still have its weakest members excludable (beyond
        minimum_candidates) — this fix only ever ADDS a boost to a
        genuinely relevant chunk, it never widens what counts as
        relevant enough to keep for anything else."""
        chunks = [
            self._base_chunk("Question: บริษัททำธุรกิจเกี่ยวกับอะไร\nAnswer: นำเข้าสินค้าจากจีน",
                              "Question: บริษัททำธุรกิจเกี่ยวกับอะไร", 0.9),
        ] + [
            self._base_chunk(f"เนื้อหาที่ไม่เกี่ยวข้องกันเลยเรื่องอื่นโดยสิ้นเชิง หมายเลข {i}",
                              f"หัวข้ออื่นที่ไม่เกี่ยวข้อง {i}", 0.05 - i * 0.005)
            for i in range(6)
        ]
        kept, excluded = apply_hybrid_ranking("บริษัทนี้ทำธุรกิจเกี่ยวกับอะไร", chunks,
                                               company_intent=True, return_excluded=True)
        self.assertGreater(len(excluded), 0)
        for c in excluded:
            self.assertEqual(c.get("company_intent_boost"), 0.0)


if __name__ == "__main__":
    unittest.main()
