# -*- coding: utf-8 -*-
"""CUSTOMER-INVOICE-1 — a goods-invoice issuance question must be answered
directly and completely from the trusted invoice conditions, with no
unrelated product-type follow-up.

REAL LINE (session 6c9b9026, turn 712): "ใบกำกับค่าสินค้าออกได้ไหม" ->
"…ทางเราสามารถออกใบกำกับค่าสินค้า และใบเสร็จค่าขนส่งให้ได้นะคะ ไม่ทราบว่า
สินค้าของลูกค้าเป็นอะไรคะ" — the FAQ-exact matcher returned the
Quick_FAQ_Patch row 546c1bd5 VERBATIM (0 LLM tokens); its stored Answer
itself ends with an unrelated product question and omits the conditions
(credit-card limitation, taxpayer-info requirement) that the sibling
trusted rows ff288877 / 99390831 / a2618c6d carry.
"""
import re
import unittest

from services.playground_orchestrator import (
    _is_invoice_issuance_question,
    _INVOICE_ISSUANCE_ANSWER,
)


class TestInvoiceIssuanceRecognizer(unittest.TestCase):
    def test_issuance_yes_no_questions(self):
        for q in ["ใบกำกับค่าสินค้าออกได้ไหม",            # A
                  "ไม่สามารถออกใบกำกับค่าสินค้าได้หรอ",   # B
                  "ขอใบกำกับค่าสินค้าไม่ได้หรอ",          # C
                  "มีใบกำกับให้ไหม",
                  "ออกใบกำกับได้ไหม"]:
            self.assertTrue(_is_invoice_issuance_question(q), q)

    def test_download_howto_is_not_an_issuance_question(self):
        # D — the download flow keeps its own trusted FAQ answer
        for q in ["โหลดใบกำกับยังไง", "ดาวน์โหลดใบกำกับภาษีทำยังไง",
                  "ขอใบกำกับภาษีต้องทำยังไง", "ใบกำกับหาได้ที่ไหน"]:
            self.assertFalse(_is_invoice_issuance_question(q), q)

    def test_unrelated_tax_and_other_questions_not_matched(self):
        for q in ["นำเข้าจากจีนต้องเสียภาษีไหม", "ต้องเสียภาษีนำเข้าเท่าไหร่",
                  "ขอเบอร์ติดต่อ", "สนใจนำเข้ารองเท้า", "ชำระบัตรเครดิตได้ไหม"]:
            self.assertFalse(_is_invoice_issuance_question(q), q)


class TestInvoiceIssuanceAnswer(unittest.TestCase):
    def test_answer_has_the_trusted_conditions_and_no_product_question(self):
        a = _INVOICE_ISSUANCE_ANSWER
        # issuance confirmed
        self.assertIn("ออกใบกำกับค่าสินค้า", a)
        self.assertIn("ใบเสร็จค่าขนส่ง", a)
        # bill / payment condition
        self.assertIn("ตามเงื่อนไขของบิลและวิธีชำระเงิน", a)
        # taxpayer info requirement
        self.assertIn("ข้อมูลผู้เสียภาษี", a)
        # credit-card limitation
        self.assertIn("บัตรเครดิต", a)
        self.assertIn("ไม่สามารถออกใบกำกับได้", a)
        # NO unrelated product-type follow-up
        self.assertNotRegex(a, r"สินค้าของลูกค้าเป็นอะไร|สินค้า.{0,6}เป็นอะไรคะ")


if __name__ == "__main__":
    unittest.main()
