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
    _invoice_issuance_branch_applies,
    _INVOICE_ISSUANCE_ANSWER,
)
from services.conversation_semantics import interpret, Interpretation


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


class TestInvoiceRegression1SemanticFirstGate(unittest.TestCase):
    """INVOICE-REGRESSION-1 — the trusted-invoice branch keys on the
    central INVOICE family, so paraphrases the literal phrase regex
    misses ("tax invoice", "ใบเสร็จค่าขนส่ง", "e-tax invoice") still
    reach the trusted answer instead of Fix-2 Human Handoff."""

    def _interp(self, fam):
        return Interpretation(intent_family=fam, confidence=0.85)

    def test_case_A_exact_real_failure_applies(self):
        self.assertTrue(_invoice_issuance_branch_applies(
            "ใบกำกับค่าสินค้าออกได้ไหม", "invoice_policy", self._interp("INVOICE")))

    def test_case_B_tax_invoice_request_applies(self):
        self.assertTrue(_invoice_issuance_branch_applies(
            "ขอใบกำกับภาษีครับ", "invoice_policy", self._interp("INVOICE")))

    def test_case_C_tax_invoice_paraphrase_applies_via_family(self):
        # literal regex misses this — the family carries it
        self.assertFalse(_is_invoice_issuance_question("บริษัทออก tax invoice ให้ไหมครับ"))
        self.assertTrue(_invoice_issuance_branch_applies(
            "บริษัทออก tax invoice ให้ไหมครับ", "invoice_policy", self._interp("INVOICE")))

    def test_more_unseen_invoice_paraphrases_apply(self):
        for q in ["ขอใบเสร็จค่าขนส่งได้ไหม", "มี e-tax invoice ไหมครับ",
                  "บริษัทออกใบกำกับให้หรือเปล่า", "อยากได้ใบเสร็จตัวจริงขอได้ไหม"]:
            self.assertEqual(interpret(q, []).intent_family, "INVOICE", q)
            self.assertTrue(_invoice_issuance_branch_applies(q, "invoice_policy", interpret(q, [])), q)

    def test_case_D_download_never_applies(self):
        for q in ["โหลดใบกำกับยังไง", "ดาวน์โหลดใบกำกับภาษีทำยังไง", "ใบกำกับหาได้ที่ไหน"]:
            self.assertFalse(_invoice_issuance_branch_applies(q, "invoice_policy", self._interp("INVOICE")), q)

    def test_non_invoice_intent_never_applies(self):
        # a different actionable intent -> branch is off regardless of wording
        self.assertFalse(_invoice_issuance_branch_applies(
            "ใบกำกับค่าสินค้าออกได้ไหม", "tracking_status", self._interp("INVOICE")))

    def test_import_tax_question_does_not_apply(self):
        # "ภาษีนำเข้า..." is not an invoice question; family is not INVOICE
        self.assertNotEqual(interpret("ภาษีนำเข้าคิดยังไงครับ", []).intent_family, "INVOICE")
        self.assertFalse(_invoice_issuance_branch_applies(
            "ภาษีนำเข้าคิดยังไงครับ", "prohibited_goods", interpret("ภาษีนำเข้าคิดยังไงครับ", [])))

    def test_no_interpretation_still_covers_the_literal_cases(self):
        # Playground / benchmark callers pass no interpretation
        self.assertTrue(_invoice_issuance_branch_applies(
            "ใบกำกับค่าสินค้าออกได้ไหม", "invoice_policy", None))
        self.assertFalse(_invoice_issuance_branch_applies(
            "บริษัทออก tax invoice ให้ไหมครับ", "invoice_policy", None))


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
