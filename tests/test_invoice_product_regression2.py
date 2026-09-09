# -*- coding: utf-8 -*-
"""INVOICE-PRODUCT-REGRESSION-2.

Problem A — the invoice FIRST turn must answer the trusted policy
directly, with NO "สินค้าของลูกค้าเป็นอะไรคะ" product question. The
corrupted chunk 546c1bd5's answer is replaced at its source (see
tools/fix_corrupted_invoice_chunk_546c1bd5.py) so it can no longer
escape any path.

Problem B — a bare product NAME given in reply to the assistant's own
"what product?" question must be understood generically as a PRODUCT
entity (no product dictionary), and NEVER become UNKNOWN /
unsupported_company_information / Human CS just because the noun is
novel. The trusted policy — not the semantic layer — decides prohibited
vs. allowed.

OWNER-REAL-LINE-FIX-04 refinement — that bare product name belongs to
the journey that asked for it. In an IMPORT_INTEREST discovery journey
it is a PRODUCT-SLOT response and the journey continues (family
IMPORT_INTEREST). It routes to PRODUCT_POLICY only when the exchange is
already in a restriction/eligibility context, or the named product
matches a trusted high-risk category (battery, liquid, chemical,
perfume, …) — so the trusted policy still owns the verdict for the
cases that need it.
"""
import unittest

from services.conversation_semantics import (
    interpret, _assistant_asked_for_product, _looks_like_bare_product, _bare_product_noun,
)
from services.slot_filling_engine import detect_erp_intent
from services.playground_orchestrator import (
    _is_invoice_issuance_question, _invoice_issuance_branch_applies, _INVOICE_ISSUANCE_ANSWER,
)

_ASK = "สินค้าที่ต้องการนำเข้าคืออะไรคะ"
_H = [{"role": "user", "content": "สนใจนำเข้าสินค้าจากจีน"},
      {"role": "assistant", "content": _ASK}]


class TestProblemA_InvoiceFirstTurn(unittest.TestCase):
    def test_clean_answer_has_no_product_question_and_carries_the_conditions(self):
        a = _INVOICE_ISSUANCE_ANSWER
        self.assertNotIn("สินค้าของลูกค้าเป็นอะไร", a)
        self.assertIn("ตามเงื่อนไขของบิลและวิธีชำระเงิน", a)
        self.assertIn("ข้อมูลผู้เสียภาษี", a)
        self.assertIn("บัตรเครดิต", a)

    def test_issuance_branch_applies_for_the_literal_and_family_paths(self):
        it = interpret("ใบกำกับค่าสินค้าออกได้ไหม", [])
        self.assertEqual(it.intent_family, "INVOICE")
        self.assertTrue(_is_invoice_issuance_question("ใบกำกับค่าสินค้าออกได้ไหม"))
        self.assertTrue(_invoice_issuance_branch_applies("ใบกำกับค่าสินค้าออกได้ไหม", "invoice_policy", it))

    def test_invoice_B_polite_request_is_a_policy_question_not_a_lookup(self):
        # "ขอใบกำกับภาษีหน่อยครับ" -> RAG trusted policy, not the
        # invoice/order-number collection dead-end.
        self.assertIsNone(detect_erp_intent("ขอใบกำกับภาษีหน่อยครับ"))
        self.assertEqual(interpret("ขอใบกำกับภาษีหน่อยครับ", []).intent_family, "INVOICE")

    def test_genuine_retrieval_request_still_a_lookup(self):
        self.assertEqual(detect_erp_intent("ช่วยส่งใบเสร็จให้หน่อยครับ"), "invoice")
        self.assertEqual(detect_erp_intent("ขอใบกำกับภาษีของผมหน่อย"), "invoice")

    def test_invoice_C_and_D_families(self):
        self.assertEqual(interpret("บริษัทมี tax invoice ไหมครับ", []).intent_family, "INVOICE")
        it_d = interpret("โหลดใบกำกับยังไง", [])
        self.assertEqual(it_d.intent_family, "INVOICE")
        self.assertFalse(_invoice_issuance_branch_applies("โหลดใบกำกับยังไง", "invoice_policy", it_d))


class TestProblemB_GenericProductEntity(unittest.TestCase):
    _UNSEEN = "โคมไฟตั้งโต๊ะ"  # not written in the task prompt

    def test_assistant_product_question_is_recognised(self):
        self.assertTrue(_assistant_asked_for_product(_H))
        self.assertFalse(_assistant_asked_for_product(
            [{"role": "assistant", "content": "ต้องการที่อยู่โกดังไทยหรือจีนคะ"}]))

    def test_ordinary_and_unseen_products_continue_the_import_journey(self):
        # OWNER-REAL-LINE-FIX-04 — an ordinary product name inside an
        # IMPORT_INTEREST discovery journey is a PRODUCT-SLOT response;
        # the journey continues, it is NOT a prohibited-goods question.
        for m in ["กล่องพลาสติกครับ", "เป็นพวกกล่องพลาสติกใส่ของครับ", "เสื้อผ้าครับ",
                  "ชั้นวางของ", "แก้วน้ำ", "กระเป๋า", "อะไหล่รถยนต์ครับ", self._UNSEEN + "ค่ะ"]:
            it = interpret(m, _H)
            self.assertEqual(it.intent_family, "IMPORT_INTEREST", m)
            self.assertTrue(it.entities.get("product"), m)
            self.assertNotEqual(it.entities["product"], "", m)
            # the real Problem-B invariant: never a dead-end family
            self.assertNotIn(it.intent_family, ("UNKNOWN",), m)

    def test_high_risk_products_still_route_to_trusted_policy(self):
        # a trusted high-risk category name still reaches PRODUCT_POLICY so
        # the trusted policy (not this layer) returns the verdict.
        for m in ["น้ำหอมครับ", "แบตเตอรี่ครับ", "เป็นน้ำยา"]:
            it = interpret(m, _H)
            self.assertEqual(it.intent_family, "PRODUCT_POLICY", m)
            self.assertTrue(it.entities.get("product"), m)

    def test_safe_term_control_kaew_nam_and_nam_nak(self):
        # "แก้วน้ำ" (a glass) must be a PRODUCT entity, never flagged
        # prohibited by the semantic layer for containing "น้ำ".
        it = interpret("แก้วน้ำ", _H)
        self.assertEqual(it.entities.get("product"), "แก้วน้ำ")
        # "น้ำหนัก 2 กิโล" is a structural value, not a product.
        self.assertFalse(_looks_like_bare_product("น้ำหนัก 2 กิโล"))
        self.assertNotEqual(interpret("น้ำหนัก 2 กิโล", _H).intent_family, "PRODUCT_POLICY")

    def test_no_product_question_no_recognition(self):
        # a bare noun with NO preceding product question stays UNKNOWN
        self.assertEqual(interpret("กล่องพลาสติกครับ", []).intent_family, "UNKNOWN")
        self.assertEqual(interpret("แก้วน้ำ", []).intent_family, "UNKNOWN")

    def test_a_real_follow_up_question_is_not_a_bare_product(self):
        for m in ["ราคาเท่าไหร่ครับ", "ส่งได้ไหม", "มีขั้นต่ำไหม"]:
            self.assertFalse(_looks_like_bare_product(m), m)

    def test_noun_extraction_strips_particles_and_prefixes(self):
        self.assertEqual(_bare_product_noun("เป็นพวกกล่องพลาสติกใส่ของครับ"), "กล่องพลาสติก")
        self.assertEqual(_bare_product_noun("อะไหล่รถยนต์ครับ"), "อะไหล่รถยนต์")
        self.assertIsNone(_bare_product_noun("ครับ"))


if __name__ == "__main__":
    unittest.main()
