# -*- coding: utf-8 -*-
"""CUSTOMER SCREENSHOT ACCEPTANCE SUITE (2026-09-17).

Five confirmed shared-mechanism defects from the owner's manual REAL LINE
screenshots, fixed as ONE canonical semantic class each (never a phrase
patch), verified through the production-equivalent path: PyThaiNLP ->
RapidFuzz evidence -> the central interpreter -> DecisionEngine (the
same execution DecisionEngine.decide() performs in production; LangGraph
calls this same engine as its own execution step, never a second one).

1. COMPARE_SHIPPING_METHODS (services/shipping_estimate_flow.py): an
   explicit comparison/superlative question ("ทางไหนราคาดีกว่ากัน",
   "ขนส่งไหนถูกสุด") means compare BOTH road and sea — never ask the
   customer to pick a method first. Reuses _method_of()'s existing
   "both" value and the SAME compute_estimate() singular source of
   truth; no second calculator.

2. Private TH-tracking/shipment-arrival family
   (services/decision_engine.py::_classify_private_state_inquiry): a
   possession probe ("มีเลขแทรคไทยมั้ย") or a desire-verb request
   ("อยากได้เลขแทรคไทย") for tracking/order domains -- which have no
   meaningful public reading -- no longer requires owner wording; a
   generic "ตรงไหน" location probe is recognised the same way "อยู่ไหน"
   already was (fixing a typo'd check-verb case for free, without any
   typo-specific rule). A genuine public policy/catalog question
   ("สินค้าที่ห้ามนำเข้ามีอะไรบ้าง") is protected by a new hard negative
   gate, never caught by the widened check.

3. Product vs. customization-clause extraction
   (services/playground_orchestrator.py): a customization add-on named
   alongside a fresh product+quantity opener ("...สั่งพิมพ์โลโก้ด้วย")
   is stripped from the product noun and captured as its own fact,
   acknowledged (never silently dropped) via services/service_intent_
   flow.py::import_interest_reply. A negated mention ("ไม่เอาโลโก้")
   never creates a customization.

4. PURCHASE_BILL_PAYMENT vs PURCHASE_WITHDRAWAL
   (services/conversation_semantics.py + service_intent_flow.py): a
   payment question ("จ่ายบิลสั่งซื้อยังไง") now resolves to its OWN
   family with a deterministic, verbatim-sourced reply BEFORE ever
   reaching RAG, so vector similarity can no longer choose between the
   payment-steps chunk and the wallet-withdrawal chunk. The withdrawal
   family (already correctly deterministic) is verified as the
   invariant's other half.

A separate P0 finding this same day (Supabase API key rejected project-
wide) is NOT part of this suite -- see the session's own infra report;
tests here run against the config.py fix already deployed for it.

Test tier is pinned offline by tests/__init__.py -- no paid API call.
"""
import unittest

from services.decision_engine import DecisionEngine, _classify_private_state_inquiry
from services.shipping_estimate_flow import (
    _method_of, opens_estimate_flow, EstimateState, extract_estimate_fields,
    estimate_missing_prompt, estimate_reply,
)
from services.playground_orchestrator import _product_interest_noun, extract_customization_clause
from services.conversation_semantics import _compose

CTX = {"channel": "line", "tenant_id": "default", "sample_source": "OWNER_TEST",
       "customer_context": {}, "developer_mode": True}


def _reply(eng, text, history=None):
    r = eng.decide(text, history=history or [], context=dict(CTX))
    return (r.get("reply") or {}).get("text") or "", (r.get("developer") or {}).get("selection_source")


class TestCompareShippingMethods(unittest.TestCase):
    """Fix 1+2 -- one canonical COMPARE_SHIPPING_METHODS class."""

    COMPARISON_FORMS = ("ทางไหนราคาดีกว่ากัน", "ขนส่งไหนถูกสุด", "รถกับเรืออันไหนถูกกว่า",
                        "อันไหนประหยัดกว่า", "แบบไหนคุ้มกว่า", "เทียบสองแบบให้หน่อย",
                        "ช่วยเทียบค่าขนส่ง", "ส่งแบบไหนถูกกว่า")

    def test_every_comparison_form_means_compare_both(self):
        for text in self.COMPARISON_FORMS:
            with self.subTest(text=text):
                self.assertEqual(_method_of(text), "both", text)

    def test_comparison_opens_the_flow_with_no_prior_value(self):
        st = EstimateState()
        self.assertTrue(opens_estimate_flow("ทางไหนราคาดีกว่ากัน", st))

    def test_missing_prompt_never_asks_to_pick_a_method(self):
        st = EstimateState()
        extract_estimate_fields("ทางไหนราคาดีกว่ากัน", st)
        prompt = estimate_missing_prompt(st)
        self.assertNotIn("ทางรถหรือทางเรือ", prompt)
        self.assertIn("น้ำหนัก", prompt)
        self.assertIn("ขนาด", prompt)

    def test_known_inputs_compute_both_via_the_one_calculator(self):
        st = EstimateState()
        extract_estimate_fields("ทางไหนราคาดีกว่ากัน 30 กก 40x30x20 ซม", st)
        reply = estimate_reply(st)
        self.assertIn("ทางรถ", reply)
        self.assertIn("ทางเรือ", reply)

    def test_exact_screenshot_journey_end_to_end(self):
        eng = DecisionEngine()
        hist = [{"role": "user", "content": "กระเป๋าผ้า 100 ใบสั่งพิมพ์โลโก้ด้วย"},
               {"role": "assistant", "content": "ได้ค่ะ รับทราบว่าต้องการนำเข้ากระเป๋าผ้าพิมพ์โลโก้ด้วย จำนวนประมาณ 100 ใบนะคะ 😊 สนใจส่งทางรถหรือทางเรือคะ"}]
        reply, src = _reply(eng, "ทางไหนราคาดีกว่ากัน", hist)
        self.assertEqual(src, "shipping_estimate_flow")
        self.assertNotIn("สนใจส่งทางรถหรือทางเรือคะ", reply, "must not just repeat the method ask")
        self.assertIn("น้ำหนัก", reply)

    def test_calc_verb_plus_superlative_asks_only_the_missing_inputs(self):
        eng = DecisionEngine()
        reply, _ = _reply(eng, "ของมาถึงแล้วช่วยคำนวณหน่อย ขนส่งไหนถูกสุด")
        self.assertNotIn("ทางรถหรือทางเรือ", reply)


class TestPrivateTHTrackingFamily(unittest.TestCase):
    """Fix 3 -- semantic/domain classification, not an exclusion list."""

    REQUIRED = ("เช็คของเข้าไทยตรงไหน", "เชคของเข้าไทยตรงไหน", "เลคของเข้าไทยตรงไหน",
               "มีเลขแทรคไทยมั้ย", "ขอเลขแทรคไทย", "อยากได้เลขแทรคไทย",
               "เลขพัสดุไทยมีไหม", "ของผมถึงไทยยัง", "ของเข้าไทยเมื่อไหร่",
               "เช็คของเข้าไทยให้หน่อย")

    def test_every_required_phrasing_is_a_private_inquiry(self):
        for text in self.REQUIRED:
            with self.subTest(text=text):
                self.assertIsNotNone(_classify_private_state_inquiry(text), text)

    def test_desire_verb_request_never_becomes_import_interest(self):
        """The central precedence requirement: semantic/domain beats the
        generic 'อยากได้ <product>' purchase-interest opener."""
        for text in ("อยากได้เลขแทรคไทย", "มีเลขแทรคไทยมั้ย", "เลคของเข้าไทยตรงไหน"):
            with self.subTest(text=text):
                self.assertNotEqual(_compose(text)[0], "IMPORT_INTEREST", text)

    def test_genuine_product_desire_is_unaffected(self):
        for text in ("อยากได้กระเป๋า", "อยากได้รองเท้าสีดำ"):
            with self.subTest(text=text):
                self.assertIsNone(_classify_private_state_inquiry(text), text)

    def test_public_policy_and_catalog_questions_stay_public(self):
        for text in ("มีบริการอะไรบ้าง", "มีขั้นต่ำในการสั่งไหม",
                     "สินค้าที่ห้ามนำเข้ามีอะไรบ้าง", "มีสินค้าอะไรบ้างที่ลดราคา"):
            with self.subTest(text=text):
                self.assertIsNone(_classify_private_state_inquiry(text), text)

    def test_end_to_end_asks_identifier_not_import_flow(self):
        eng = DecisionEngine()
        for text in ("อยากได้เลขแทรคไทย", "มีเลขแทรคไทยมั้ย", "เลคของเข้าไทยตรงไหน"):
            with self.subTest(text=text):
                reply, src = _reply(eng, text)
                self.assertEqual(src, "private_state_inquiry", (text, reply))
                self.assertIn("รหัสลูกค้า", reply, (text, reply))


class TestProductVsCustomization(unittest.TestCase):
    """Fix 5 -- customization survives, never welds onto the product."""

    def test_product_noun_excludes_customization(self):
        cases = [
            ("กระเป๋าผ้า 100 ใบสั่งพิมพ์โลโก้ด้วย", "กระเป๋าผ้า", "พิมพ์โลโก้"),
            ("กระเป๋าผ้า 100 ใบ พิมพ์โลโก้ด้วย", "กระเป๋าผ้า", "พิมพ์โลโก้"),
            ("เอากระเป๋าผ้า 100 ใบ สกรีนโลโก้", "กระเป๋าผ้า", "สกรีนโลโก้"),
            ("ต้องการกระเป๋าผ้าพร้อมพิมพ์แบรนด์", "กระเป๋าผ้า", "พิมพ์แบรนด์"),
        ]
        for text, product, customization in cases:
            with self.subTest(text=text):
                self.assertEqual(_product_interest_noun(text), product)
                self.assertEqual(extract_customization_clause(text), customization)

    def test_negated_mention_creates_no_customization(self):
        text = "กระเป๋าผ้า 100 ใบไม่เอาโลโก้"
        self.assertEqual(_product_interest_noun(text), "กระเป๋าผ้า")
        self.assertIsNone(extract_customization_clause(text))

    def test_real_product_with_brand_in_the_name_is_untouched(self):
        self.assertEqual(_product_interest_noun("อยากสั่งกระเป๋าแบรนด์เนม 5 ใบ"), "กระเป๋าแบรนด์เนม")

    def test_end_to_end_acknowledges_both_product_and_customization(self):
        eng = DecisionEngine()
        for text in ("กระเป๋าผ้า 100 ใบสั่งพิมพ์โลโก้ด้วย", "ต้องการกระเป๋าผ้าพร้อมพิมพ์แบรนด์"):
            with self.subTest(text=text):
                reply, _ = _reply(eng, text)
                self.assertIn("กระเป๋าผ้า", reply)
                self.assertIn("100 ใบ" if "100" in text else "จำนวน", reply)
                self.assertRegex(reply, "โลโก้|แบรนด์", reply)
                self.assertNotIn("กระเป๋าผ้าพิมพ์", reply, "customization must not weld onto the product")
                self.assertNotIn("กระเป๋าผ้าสกรีน", reply)


class TestPurchaseBillPaymentVsWithdrawal(unittest.TestCase):
    """Fix 6 -- one deterministic family per direction, resolved before
    RAG; vector similarity never chooses between them."""

    PAYMENT_FORMS = ("จ่ายค่าบิลที่ซื้อของไปทำไงคะ", "ชำระบิลที่สั่งซื้อไป",
                     "วิธีการชำระบิลสั่งซื้อ", "จ่ายบิลสั่งซื้อยังไง", "จ่ายค่าของตรงไหน")
    WITHDRAWAL_FORMS = ("ถอนเงิน", "ถอนเครดิตสั่งซื้อ", "เงินร้านคืนมา จะถอนยังไง",
                        "ขอถอนยอด", "เอาเครดิตออกมา", "ถอนเงินกลับเข้าบัญชี")

    def test_payment_forms_resolve_to_the_payment_family(self):
        for text in self.PAYMENT_FORMS:
            with self.subTest(text=text):
                self.assertEqual(_compose(text)[0], "PURCHASE_BILL_PAYMENT", text)

    def test_withdrawal_forms_never_resolve_to_payment(self):
        for text in self.WITHDRAWAL_FORMS:
            with self.subTest(text=text):
                self.assertNotEqual(_compose(text)[0], "PURCHASE_BILL_PAYMENT", text)

    def test_end_to_end_payment_gets_the_qr_steps_never_withdrawal_steps(self):
        eng = DecisionEngine()
        for text in self.PAYMENT_FORMS:
            with self.subTest(text=text):
                reply, src = _reply(eng, text)
                self.assertEqual(src, "phase6b_service_intent", (text, reply))
                self.assertIn("QR Code", reply, (text, reply))
                self.assertNotIn("รายการเติมเงิน", reply, "must never answer with the withdrawal procedure")

    def test_end_to_end_withdrawal_never_gets_the_payment_steps(self):
        eng = DecisionEngine()
        for text in self.WITHDRAWAL_FORMS:
            with self.subTest(text=text):
                reply, _ = _reply(eng, text)
                self.assertNotIn("QR Code", reply, (text, reply))

    def test_shipping_bill_payment_is_excluded_from_this_family(self):
        """ชำระบิลขนส่ง/จ่ายค่านำเข้า is a SEPARATE, pre-existing concept —
        must not be captured by the new purchase-bill family."""
        for text in ("ชำระบิลขนส่งยังไง", "จ่ายค่านำเข้ายังไง"):
            with self.subTest(text=text):
                self.assertNotEqual(_compose(text)[0], "PURCHASE_BILL_PAYMENT", text)

    def test_payment_question_survives_a_pending_different_collection(self):
        """REAL LINE 2026-09-17 (P0, found by the owner's manual retest,
        same day as this suite's own first version): a NEWLY added
        semantic family must be registered in every shared 'is this a
        decisive intent' set (_ACTIONABLE_INTENT_FAMILIES,
        _FLOW_ONLY_INTENT_FAMILIES, Fix05's _F5_EXPLICIT) -- omitting it
        from even one is invisible in an isolated, fresh-history test and
        only surfaces once a DIFFERENT collection is already pending.
        Exact repro: three private-tracking questions exhaust the
        identifier-retry budget and escalate; the customer's NEXT,
        unrelated payment question must still get the payment answer,
        never be swallowed as an attempted answer to the stale
        collection."""
        eng = DecisionEngine()
        history = []
        for text in ("มีเลขแทรคไทยมั้ย", "อยากได้เลขแทรคไทย", "เชคของเข้าไทยตรงไหน"):
            reply, _ = _reply(eng, text, history)
            history = history + [{"role": "user", "content": text}, {"role": "assistant", "content": reply}]
        reply, src = _reply(eng, "จ่ายค่าบิลที่ซื้อของไปทำไงคะ", history)
        self.assertEqual(src, "phase6b_service_intent", reply)
        self.assertIn("QR Code", reply)
        self.assertNotIn("รหัสลูกค้า", reply)


if __name__ == "__main__":
    unittest.main()
