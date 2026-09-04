# -*- coding: utf-8 -*-
"""CUSTOMER-RAG-AUDIT — remaining customer RAG/public-knowledge gaps.

Two defects surfaced auditing the customer UAT master against production:

  CUS-G09 / CUS-G26 — RETRIEVAL_FAILURE: the fuzzy spell-corrector mangled
    "ชำระค่าสินค้ายังไง" -> "…เคลมสินค้า…" and "ชำระค่านำเข้ายังไง" ->
    "…การนำเข้า…", routing valid payment questions into the claims FAQ /
    a no_information Human handoff. The customer-approved answers and
    their alt-phrasings already exist in the KB.

  CUS-G10 / CUS-G14 — ANSWER_COMPOSITION: an LLM family GUESS of INVOICE
    for "บิลขนส่งชำระได้เลยไหม" / "ชำระบัตรเครดิตได้ไหม" drove the
    deterministic trusted-invoice branch, returning the invoice answer
    for a shipping-timing / credit-card-policy question.
"""
import unittest

from rag.spell_correction import correct_query
from services.conversation_semantics import Interpretation
from services.playground_orchestrator import _invoice_issuance_branch_applies


class TestSpellCorrectorLeavesPaymentPhrasesAlone(unittest.TestCase):
    def test_payment_phrases_are_not_fuzzy_mangled(self):
        for q in ["ชำระค่าสินค้ายังไง", "ชำระค่านำเข้ายังไง",
                  "จ่ายค่าสินค้ายังไง", "ชำระค่าสินค้ากับค่านำเข้ายังไง"]:
            self.assertEqual(correct_query(q)["corrected_query"], q, q)

    def test_a_real_typo_nearby_still_corrects(self):
        # guard must not disable genuine correction elsewhere in the query
        out = correct_query("ชำระค่าสินค้ายังงัย")["corrected_query"]
        self.assertIn("ค่าสินค้า", out)


class TestInvoiceBranchIgnoresLlmFamilyGuess(unittest.TestCase):
    def _i(self, fam, source):
        return Interpretation(intent_family=fam, confidence=0.8, source=source)

    def test_llm_invoice_guess_does_not_open_branch(self):
        for q in ["บิลขนส่งสามารถชำระได้เลยไหม", "ชำระบัตรเครดิตได้ไหม"]:
            self.assertFalse(
                _invoice_issuance_branch_applies(q, "invoice_policy", self._i("INVOICE", "llm")), q)

    def test_deterministic_invoice_family_still_opens_branch(self):
        for q in ["บริษัทออก tax invoice ให้ไหมครับ", "ขอใบเสร็จค่าขนส่งได้ไหม"]:
            self.assertTrue(
                _invoice_issuance_branch_applies(q, "invoice_policy", self._i("INVOICE", "deterministic")), q)

    def test_literal_issuance_wording_still_opens_branch_without_interpretation(self):
        self.assertTrue(_invoice_issuance_branch_applies(
            "ใบกำกับค่าสินค้าออกได้ไหม", "invoice_policy", None))


if __name__ == "__main__":
    unittest.main()
