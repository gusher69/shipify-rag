"""Regression tests for rag/faq_matcher.py — structured FAQ exact/near-
exact matching against knowledge_items rows (Question + Alternative
Questions). Only find_best_faq_match() (the pure, DB-free function) is
tested here; match_faq_exact() is a thin Supabase wrapper around it.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rag.faq_matcher import find_best_faq_match, normalize_faq_text

CREDIT_CARD_ROW = {
    "id": "row-1", "row_index": 5, "sheet_name": "faq.xlsx",
    "question": "ใช้บัตรเครดิตได้ไหม",
    "alt_questions": ["จ่ายด้วยบัตรเครดิตได้หรือเปล่า", "รับบัตรเครดิตไหม"],
    "answer": "ขั้นต่ำ 500 บาท ค่าธรรมเนียม 3% ไม่สามารถออกใบกำกับภาษีได้",
    "chunk_id": "chunk-1", "knowledge_file_id": "file-1",
}
TAX_INVOICE_ROW = {
    "id": "row-2", "row_index": 8, "sheet_name": "faq.xlsx",
    "question": "ออกใบกำกับภาษีได้ไหม",
    "alt_questions": ["ขอใบกำกับภาษีได้ไหม"],
    "answer": "ออกใบกำกับภาษีได้ค่ะ กรุณาแจ้งเลขผู้เสียภาษี",
    "chunk_id": "chunk-2", "knowledge_file_id": "file-1",
}
BILL_PAYMENT_ROW = {
    "id": "row-3", "row_index": 2, "sheet_name": "faq.xlsx",
    "question": "จ่ายบิลยังไง",
    "alt_questions": ["ชำระเงินยังไง", "จ่ายเงินยังไง"],
    "answer": "เปิดบิล สแกน QR แล้วยืนยันการชำระเงิน",
    "chunk_id": "chunk-3", "knowledge_file_id": "file-1",
}
ROWS = [CREDIT_CARD_ROW, TAX_INVOICE_ROW, BILL_PAYMENT_ROW]


class TestNormalizeFaqText(unittest.TestCase):
    def test_strips_punctuation_and_whitespace(self):
        self.assertEqual(normalize_faq_text("ใช้บัตรเครดิตได้ไหม?"), normalize_faq_text("ใช้บัตรเครดิตได้ไหม"))

    def test_empty_input(self):
        self.assertEqual(normalize_faq_text(None), "")
        self.assertEqual(normalize_faq_text(""), "")


class TestFindBestFaqMatch(unittest.TestCase):
    def test_exact_question_match(self):
        result = find_best_faq_match("ใช้บัตรเครดิตได้ไหม", ROWS)
        self.assertIsNotNone(result)
        self.assertEqual(result["match_type"], "exact")
        self.assertIs(result["row"], CREDIT_CARD_ROW)

    def test_exact_alternative_question_match(self):
        result = find_best_faq_match("รับบัตรเครดิตไหม", ROWS)
        self.assertIsNotNone(result)
        self.assertEqual(result["match_type"], "exact")
        self.assertIs(result["row"], CREDIT_CARD_ROW)

    def test_near_exact_match_with_trailing_politeness_particle(self):
        result = find_best_faq_match("ใช้บัตรเครดิตได้ไหมคะ", ROWS)
        self.assertIsNotNone(result)
        self.assertEqual(result["match_type"], "near_exact")
        self.assertIs(result["row"], CREDIT_CARD_ROW)

    def test_credit_card_question_never_matches_tax_invoice_row(self):
        """Core requirement: "ใช้บัตรเครดิตได้ไหม" must not match "ออกใบกำกับ
        ได้ไหม" just because both end in "ได้ไหม" — the winning row must
        be the credit-card one, never the tax-invoice one."""
        result = find_best_faq_match("ใช้บัตรเครดิตได้ไหม", ROWS)
        self.assertIsNotNone(result)
        self.assertIsNot(result["row"], TAX_INVOICE_ROW)

    def test_unrelated_question_returns_no_match(self):
        result = find_best_faq_match("CBM คืออะไร", ROWS)
        self.assertIsNone(result)

    def test_bill_payment_question_matches_its_own_row_only(self):
        result = find_best_faq_match("จ่ายบิลยังไง", ROWS)
        self.assertIsNotNone(result)
        self.assertIs(result["row"], BILL_PAYMENT_ROW)

    def test_empty_question_returns_no_match(self):
        self.assertIsNone(find_best_faq_match("", ROWS))
        self.assertIsNone(find_best_faq_match("   ", ROWS))

    def test_empty_rows_returns_no_match(self):
        self.assertIsNone(find_best_faq_match("ใช้บัตรเครดิตได้ไหม", []))


if __name__ == "__main__":
    unittest.main()
