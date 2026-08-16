"""Tests for services/hybrid_playground_router.py — the Playground-only
Auto-mode router and Hybrid-mode synthesis (Hybrid Question Segmentation
sprint, 2026-08-02). Pure functions, no DB/network/LLM involved.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.hybrid_playground_router import naive_auto_route, synthesize_hybrid_answer


class TestNaiveAutoRoute(unittest.TestCase):
    """Pre-existing behavior, unchanged by this sprint — kept for
    anything still calling it directly (Auto mode itself now uses
    services/hybrid_question_classifier.py instead)."""

    def test_matches_erp_action_token(self):
        actions = [{"id": "a1", "name": "ดึงข้อมูลลูกค้า", "action_key": "get_customer", "enabled": True}]
        result = naive_auto_route("ดึงข้อมูลลูกค้ารหัส C00001", actions)
        self.assertEqual(result["route"], "erp")
        self.assertEqual(result["action_id"], "a1")

    def test_defaults_to_rag_when_nothing_matches(self):
        result = naive_auto_route("ขอทราบนโยบายการคืนสินค้า", [])
        self.assertEqual(result["route"], "rag")


class TestSynthesizeHybridAnswer(unittest.TestCase):
    def test_both_sections_present_customer_answer_has_no_architecture_labels(self):
        """Customer Response Quality (2026-08-16) — merged_answer is what
        a real customer reads: their own two answers back to back, never
        internal wording like "ERP" or "Knowledge Base"."""
        result = synthesize_hybrid_answer(
            erp_answer="ลูกค้า C00001 มีคูปอง 2 ใบ", rag_answer="คูปองใช้งานได้ที่หน้าชำระเงินค่ะ",
            rag_citations=["FAQ.xlsx, page 3"],
        )
        self.assertIn("ลูกค้า C00001 มีคูปอง 2 ใบ", result["erp_section"])
        self.assertIn("คูปองใช้งานได้ที่หน้าชำระเงินค่ะ", result["rag_section"])
        self.assertIn("ลูกค้า C00001 มีคูปอง 2 ใบ", result["merged_answer"])
        self.assertIn("คูปองใช้งานได้ที่หน้าชำระเงินค่ะ", result["merged_answer"])
        self.assertNotIn("ข้อมูลเฉพาะลูกค้า", result["merged_answer"])
        self.assertNotIn("ความรู้ทั่วไป", result["merged_answer"])
        self.assertNotIn("ERP", result["merged_answer"])
        self.assertNotIn("Knowledge Base", result["merged_answer"])

    def test_labeled_answer_still_available_for_developer_trace(self):
        """The pre-2026-08-16 labeled/citation format still exists — just
        never shown to the customer — so Developer Mode keeps its detail."""
        result = synthesize_hybrid_answer(
            erp_answer="ลูกค้า C00001 มีคูปอง 2 ใบ", rag_answer="คูปองใช้งานได้ที่หน้าชำระเงินค่ะ",
            rag_citations=["FAQ.xlsx, page 3"],
        )
        self.assertIn("ข้อมูลเฉพาะลูกค้า", result["labeled_answer"])
        self.assertIn("ความรู้ทั่วไป", result["labeled_answer"])
        self.assertIn("FAQ.xlsx", result["labeled_answer"])
        self.assertEqual(result["citations"], ["FAQ.xlsx, page 3"])

    def test_citation_never_attached_to_erp_section_or_customer_answer(self):
        result = synthesize_hybrid_answer(
            erp_answer="ลูกค้า C00001 มีคูปอง 2 ใบ", rag_answer="คูปองใช้งานได้ที่หน้าชำระเงินค่ะ",
            rag_citations=["FAQ.xlsx, page 3"],
        )
        self.assertNotIn("FAQ.xlsx", result["erp_section"])
        # Citations never appear in the customer-facing merged_answer at
        # all — only in labeled_answer (developer-only) and citations.
        self.assertNotIn("FAQ.xlsx", result["merged_answer"])
        self.assertIn("FAQ.xlsx", result["labeled_answer"])
        labeled_erp_start = result["labeled_answer"].index("ข้อมูลเฉพาะลูกค้า")
        labeled_rag_start = result["labeled_answer"].index("ความรู้ทั่วไป")
        citation_pos = result["labeled_answer"].index("FAQ.xlsx")
        self.assertGreater(citation_pos, labeled_rag_start)
        self.assertLess(labeled_erp_start, labeled_rag_start)

    def test_no_citations_means_no_source_block(self):
        result = synthesize_hybrid_answer(erp_answer="A", rag_answer="B", rag_citations=[])
        self.assertNotIn("แหล่งที่มา", result["merged_answer"])
        self.assertNotIn("แหล่งที่มา", result["labeled_answer"])

    def test_erp_failure_shown_honestly_not_fabricated(self):
        result = synthesize_hybrid_answer(erp_error="API ตอบกลับด้วยสถานะ 400", rag_answer="คำตอบจาก KB")
        self.assertIn("ไม่สามารถดึงข้อมูลลูกค้าได้", result["erp_section"])
        self.assertIn("API ตอบกลับด้วยสถานะ 400", result["erp_section"])
        self.assertIn("คำตอบจาก KB", result["rag_section"])

    def test_rag_failure_shown_honestly(self):
        result = synthesize_hybrid_answer(erp_answer="ข้อมูลลูกค้า", rag_error="embedding provider unreachable")
        self.assertIn("ไม่สามารถค้นหาข้อมูลจากฐานความรู้ได้", result["rag_section"])
        self.assertIn("embedding provider unreachable", result["rag_section"])

    def test_path_that_did_not_run_produces_no_fabricated_section(self):
        result = synthesize_hybrid_answer(erp_answer="ข้อมูลลูกค้า C00001", rag_answer=None)
        self.assertIsNone(result["rag_section"])
        self.assertNotIn("ความรู้ทั่วไป", result["merged_answer"])

    def test_neither_path_produced_anything(self):
        result = synthesize_hybrid_answer()
        self.assertIsNone(result["erp_section"])
        self.assertIsNone(result["rag_section"])
        self.assertIn("ไม่พบคำตอบ", result["merged_answer"])

    def test_strategy_is_always_labeled_explicit_not_intelligent(self):
        result = synthesize_hybrid_answer(erp_answer="A", rag_answer="B")
        self.assertIn("explicit synthesis", result["strategy"])
        self.assertIn("no LLM rewrite", result["strategy"])


if __name__ == "__main__":
    unittest.main()
