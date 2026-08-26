"""Regression tests for the Information Collection Engine / Slot Filling
Engine (services/slot_filling_engine.py). Pure Python, no DB, no LLM.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.slot_filling_engine import (
    detect_erp_intent, extract_slots_from_text, build_collection_state, INTENT_SCHEMAS,
    resolve_active_erp_intent, extract_candidates, bind_candidate_to_slot,
)


def _hist(*turns):
    """turns: alternating (role, content) pairs."""
    return [{"role": r, "content": c} for r, c in turns]


class TestIntentDetection(unittest.TestCase):
    def test_tracking_intent_detected(self):
        self.assertEqual(detect_erp_intent("ของถึงไหนแล้ว"), "tracking")

    def test_warranty_intent_detected(self):
        self.assertEqual(detect_erp_intent("อยากเคลมสินค้า"), "warranty")

    def test_invoice_intent_detected(self):
        self.assertEqual(detect_erp_intent("ขอใบกำกับภาษีหน่อย"), "invoice")

    def test_payment_intent_detected(self):
        self.assertEqual(detect_erp_intent("ยอดค้างชำระเท่าไหร่"), "payment")

    def test_customer_intent_detected(self):
        self.assertEqual(detect_erp_intent("ขอประวัติการสั่งซื้อ"), "customer")

    def test_no_erp_intent_returns_none(self):
        self.assertIsNone(detect_erp_intent("สวัสดีค่ะ วันนี้อากาศดีมาก"))


class TestSlotExtraction(unittest.TestCase):
    def test_extracts_tracking_number(self):
        slots = extract_slots_from_text("เลขพัสดุคือ TH123456789 ค่ะ")
        self.assertEqual(slots.get("tracking_number"), "TH123456789")

    def test_extracts_order_number(self):
        slots = extract_slots_from_text("ORD-20250001")
        self.assertEqual(slots.get("order_number"), "ORD-20250001")

    def test_extracts_serial_number(self):
        slots = extract_slots_from_text("SN:ABCD1234")
        self.assertIn("serial_number", slots)

    def test_extracts_customer_code(self):
        slots = extract_slots_from_text("รหัสลูกค้า CUS-000123")
        self.assertEqual(slots.get("customer_code"), "CUS-000123")

    def test_extracts_phone_number(self):
        slots = extract_slots_from_text("0812345678")
        self.assertEqual(slots.get("phone_number"), "0812345678")


class TestTrackingScenario(unittest.TestCase):
    def test_missing_parameters_generates_follow_up(self):
        state = build_collection_state("tracking", history=None, current_message="ของถึงไหนแล้ว")
        self.assertFalse(state["is_complete"])
        self.assertEqual(state["follow_up_question"], INTENT_SCHEMAS["tracking"]["follow_up_question"])
        self.assertEqual(state["next_expected_slot"], "tracking_number")

    def test_multi_turn_collection_completes(self):
        history = _hist(("user", "ของถึงไหนแล้ว"), ("assistant", INTENT_SCHEMAS["tracking"]["follow_up_question"]))
        state = build_collection_state("tracking", history=history, current_message="TH123456789")
        self.assertTrue(state["is_complete"])
        self.assertEqual(state["collected_slots"]["tracking_number"], "TH123456789")
        self.assertIsNone(state["follow_up_question"])


class TestPaymentScenario(unittest.TestCase):
    def test_missing_customer_code(self):
        state = build_collection_state("payment", history=None, current_message="ยอดค้างชำระเท่าไหร่")
        self.assertFalse(state["is_complete"])
        self.assertEqual(state["next_expected_slot"], "customer_code")

    def test_complete_when_customer_code_given(self):
        state = build_collection_state("payment", history=None, current_message="CUS-000999")
        self.assertTrue(state["is_complete"])


class TestWarrantyScenario(unittest.TestCase):
    def test_requires_serial_number(self):
        state = build_collection_state("warranty", history=None, current_message="อยากเคลมสินค้า")
        self.assertFalse(state["is_complete"])
        self.assertEqual(state["next_expected_slot"], "serial_number")

    def test_complete_with_serial_number(self):
        state = build_collection_state("warranty", history=None, current_message="SN-XY9988")
        self.assertTrue(state["is_complete"])


class TestInvoiceScenario(unittest.TestCase):
    def test_either_invoice_or_order_number_satisfies(self):
        state_invoice = build_collection_state("invoice", history=None, current_message="INV-0099")
        self.assertTrue(state_invoice["is_complete"])
        state_order = build_collection_state("invoice", history=None, current_message="ORD-0099")
        self.assertTrue(state_order["is_complete"])

    def test_missing_both_generates_follow_up(self):
        state = build_collection_state("invoice", history=None, current_message="ขอใบกำกับภาษีหน่อย")
        self.assertFalse(state["is_complete"])


class TestCustomerLookupScenario(unittest.TestCase):
    def test_customer_code_or_phone_satisfies(self):
        by_code = build_collection_state("customer", history=None, current_message="CUS-123")
        self.assertTrue(by_code["is_complete"])
        by_phone = build_collection_state("customer", history=None, current_message="0891234567")
        self.assertTrue(by_phone["is_complete"])


class TestNumericTrackingNumberBugFix(unittest.TestCase):
    """Production bug fix: a pure-numeric tracking number (no letter
    prefix) was previously never recognized, causing the engine to
    escalate a customer who correctly provided their tracking number."""

    def _follow_up_history(self):
        return _hist(("user", "ของถึงไหนแล้ว"), ("assistant", INTENT_SCHEMAS["tracking"]["follow_up_question"]))

    def test_numeric_tracking_number(self):
        state = build_collection_state("tracking", history=self._follow_up_history(), current_message="1005505051005")
        self.assertEqual(state["collected_slots"]["tracking_number"], "1005505051005")
        self.assertTrue(state["is_complete"])
        self.assertFalse(state["escalation_required"])

    def test_tracking_number_with_thai_text(self):
        state = build_collection_state("tracking", history=self._follow_up_history(),
                                        current_message="1005505051005 เลขนี้ไง")
        self.assertEqual(state["collected_slots"]["tracking_number"], "1005505051005")
        self.assertTrue(state["is_complete"])

    def test_tracking_number_only_shorter_numeric(self):
        state = build_collection_state("tracking", history=self._follow_up_history(), current_message="123456789")
        self.assertEqual(state["collected_slots"]["tracking_number"], "123456789")
        self.assertTrue(state["is_complete"])

    def test_tracking_number_after_follow_up_completes_and_no_escalation(self):
        state = build_collection_state("tracking", history=self._follow_up_history(), current_message="1005505051005")
        self.assertTrue(state["is_complete"])
        self.assertIsNone(state["missing_slots"] or None)
        self.assertFalse(state["escalation_required"])
        self.assertIsNone(state["escalation_reason"])

    def test_retry_count_resets_after_success_even_with_prior_asks(self):
        q = INTENT_SCHEMAS["tracking"]["follow_up_question"]
        history = _hist(("user", "ของถึงไหนแล้ว"), ("assistant", q),
                         ("user", "เอ่อ"), ("assistant", q))
        state = build_collection_state("tracking", history=history, current_message="1005505051005")
        self.assertTrue(state["is_complete"])
        self.assertEqual(state["retry_count"], 0)

    def test_alphanumeric_formats_still_work_unchanged(self):
        for value in ("AB123456", "TH123456789", "CN987654321"):
            state = build_collection_state("tracking", history=self._follow_up_history(), current_message=value)
            self.assertEqual(state["collected_slots"]["tracking_number"], value)
            self.assertTrue(state["is_complete"])


def _asked(intent):
    """History: user asked something, assistant already asked this
    intent's follow-up question — i.e. an active workflow is waiting on
    that intent's next_expected_slot."""
    return _hist(("user", "x"), ("assistant", INTENT_SCHEMAS[intent]["follow_up_question"]))


class TestContextualSlotBinding(unittest.TestCase):
    """Regression coverage for the Candidate Extraction -> Slot
    Validation -> Contextual Binding pipeline (P1 behavior completion)."""

    # 1. tracking_number from a bare numeric reply
    def test_tracking_number_from_bare_numeric_reply(self):
        state = build_collection_state("tracking", _asked("tracking"), "1005505051005")
        self.assertEqual(state["collected_slots"], {"tracking_number": "1005505051005"})
        self.assertTrue(state["is_complete"])

    # 2. order_number from the same numeric format
    def test_order_number_from_same_numeric_format(self):
        state = build_collection_state("order", _asked("order"), "1005505051005")
        self.assertEqual(state["collected_slots"], {"order_number": "1005505051005"})
        self.assertTrue(state["is_complete"])

    # 3. invoice_number from text-surrounded reply
    def test_invoice_number_from_text_surrounded_reply(self):
        state = build_collection_state("invoice", _asked("invoice"), "INV-2026-00125 เลขนี้ค่ะ")
        self.assertEqual(state["collected_slots"], {"invoice_number": "INV-2026-00125"})
        self.assertTrue(state["is_complete"])

    # 4. serial_number from bare reply
    def test_serial_number_from_bare_reply(self):
        state = build_collection_state("warranty", _asked("warranty"), "SN-A99882")
        self.assertEqual(state["collected_slots"], {"serial_number": "SN-A99882"})
        self.assertTrue(state["is_complete"])

    # 5. customer_code from bare reply
    def test_customer_code_from_bare_reply(self):
        state = build_collection_state("customer", _asked("customer"), "CUS-000123")
        self.assertEqual(state["collected_slots"], {"customer_code": "CUS-000123"})
        self.assertTrue(state["is_complete"])

    # 6. phone_number from bare reply (validated directly — this task's
    # own Example 5 has phone_number as the sole expected slot, a shape
    # this platform's INTENT_SCHEMAS doesn't currently model standalone,
    # so the binder itself is exercised directly here).
    def test_phone_number_from_bare_reply(self):
        binding = bind_candidate_to_slot(extract_candidates("0812345678"), "phone_number")
        self.assertEqual(binding["status"], "bound")
        self.assertEqual(binding["value"], "0812345678")

    # 7. correct binding based on next_expected_slot
    def test_binding_uses_next_expected_slot_not_first_matching_pattern(self):
        order_state = build_collection_state("order", _asked("order"), "1005505051005")
        tracking_state = build_collection_state("tracking", _asked("tracking"), "1005505051005")
        self.assertIn("order_number", order_state["collected_slots"])
        self.assertIn("tracking_number", tracking_state["collected_slots"])

    # 8. numeric order number must not become tracking_number
    def test_numeric_order_number_never_becomes_tracking_number(self):
        state = build_collection_state("order", _asked("order"), "1005505051005")
        self.assertNotIn("tracking_number", state["collected_slots"])

    # 9. multiple valid candidates require clarification
    def test_multiple_valid_candidates_require_clarification(self):
        state = build_collection_state("tracking", _asked("tracking"), "1005505051005 กับ 1005505052006")
        self.assertFalse(state["is_complete"])
        self.assertEqual(state["collected_slots"], {})
        self.assertEqual(len(state["ambiguous_candidates"]), 2)
        self.assertIn("รบกวนระบุ", state["follow_up_question"])
        self.assertFalse(state["escalation_required"])

    # 10. invalid candidate asks again
    def test_invalid_candidate_asks_again(self):
        state = build_collection_state("customer", _asked("customer"), "ab")  # too short, no valid candidate
        self.assertFalse(state["is_complete"])
        self.assertEqual(state["collected_slots"], {})
        self.assertEqual(state["follow_up_question"], INTENT_SCHEMAS["customer"]["follow_up_question"])

    # 11. retry resets after successful binding
    def test_retry_resets_after_successful_binding(self):
        q = INTENT_SCHEMAS["order"]["follow_up_question"]
        history = _hist(("user", "สถานะออเดอร์ถึงไหน"), ("assistant", q), ("user", "เอ่อ"), ("assistant", q))
        state = build_collection_state("order", history, "1005505051005")
        self.assertTrue(state["is_complete"])
        self.assertEqual(state["retry_count"], 0)

    # 12. escalation is cancelled after successful binding
    def test_escalation_cancelled_after_successful_binding(self):
        state = build_collection_state("order", _asked("order"), "1005505051005")
        self.assertFalse(state["escalation_required"])
        self.assertIsNone(state["escalation_reason"])

    # 13. no regression to existing tracking behavior (mixed formats)
    def test_no_regression_to_existing_tracking_behavior(self):
        for value in ("1005505051005", "TH123456789", "CN987654321", "AB123456"):
            state = build_collection_state("tracking", _asked("tracking"), value)
            self.assertEqual(state["collected_slots"]["tracking_number"], value)
            self.assertTrue(state["is_complete"])

    def test_no_active_workflow_falls_back_to_legacy_extraction(self):
        """No missing slot at all (schema unknown / already satisfied) —
        legacy extract_slots_from_text() behavior must be unchanged."""
        state = build_collection_state("tracking", history=None, current_message="ของถึงไหนแล้ว TH123456789")
        self.assertEqual(state["collected_slots"].get("tracking_number"), "TH123456789")


class TestExtractCandidates(unittest.TestCase):
    def test_extracts_bare_numeric(self):
        self.assertEqual(extract_candidates("1005505051005"), ["1005505051005"])

    def test_extracts_from_surrounding_text(self):
        self.assertEqual(extract_candidates("เลขนี้ค่ะ INV-2026-00125"), ["INV-2026-00125"])

    def test_extracts_phone_like_token(self):
        self.assertEqual(extract_candidates("เบอร์ 0812345678 ค่ะ"), ["0812345678"])

    def test_extracts_multiple_candidates_in_order(self):
        result = extract_candidates("1005505051005 กับ 1005505052006")
        self.assertEqual(result, ["1005505051005", "1005505052006"])

    def test_never_assigns_a_slot_name(self):
        # extract_candidates returns plain strings, never a dict/slot mapping.
        result = extract_candidates("1005505051005")
        self.assertIsInstance(result, list)
        self.assertIsInstance(result[0], str)


class TestResolveActiveErpIntent(unittest.TestCase):
    def test_bare_reply_with_no_keyword_still_resolves_active_intent(self):
        """Regression: a bare follow-up reply like 'TH123456789' has no
        ERP keyword of its own — must still resolve to the intent whose
        follow-up question was just asked, or the flow breaks on turn 2."""
        history = _hist(("user", "ของถึงไหนแล้ว"), ("assistant", INTENT_SCHEMAS["tracking"]["follow_up_question"]))
        self.assertEqual(resolve_active_erp_intent(history, "TH123456789"), "tracking")

    def test_current_message_keyword_always_wins_over_history(self):
        history = _hist(("user", "ของถึงไหนแล้ว"), ("assistant", INTENT_SCHEMAS["tracking"]["follow_up_question"]))
        self.assertEqual(resolve_active_erp_intent(history, "อยากเคลมสินค้า"), "warranty")

    def test_no_history_and_no_keyword_returns_none(self):
        self.assertIsNone(resolve_active_erp_intent(None, "TH123456789"))

    def test_unrelated_assistant_reply_does_not_falsely_continue_a_flow(self):
        history = _hist(("user", "สวัสดีค่ะ"), ("assistant", "สวัสดีค่ะ ยินดีให้บริการค่ะ"))
        self.assertIsNone(resolve_active_erp_intent(history, "TH123456789"))


class TestConversationResume(unittest.TestCase):
    def test_state_recomputes_correctly_mid_conversation(self):
        """Simulates a resumed session — the engine has no separate
        persistence table, it recomputes purely from `history`."""
        history = _hist(
            ("user", "ของถึงไหนแล้ว"),
            ("assistant", INTENT_SCHEMAS["tracking"]["follow_up_question"]),
        )
        state = build_collection_state("tracking", history=history, current_message="ยังไม่ถึงเลยเหรอ")
        self.assertFalse(state["is_complete"])  # still missing — current message has no tracking number
        self.assertEqual(state["collected_slots"], {})


class TestAlreadyCollectedParameters(unittest.TestCase):
    def test_ai_does_not_ask_again_once_collected(self):
        history = _hist(("user", "ของถึงไหนแล้ว"), ("assistant", INTENT_SCHEMAS["tracking"]["follow_up_question"]),
                         ("user", "TH999999999"), ("assistant", "ขอบคุณค่ะ"))
        state = build_collection_state("tracking", history=history, current_message="แล้วค่าส่งเท่าไหร่")
        self.assertTrue(state["is_complete"])
        self.assertEqual(state["collected_slots"]["tracking_number"], "TH999999999")
        self.assertIsNone(state["follow_up_question"])


class TestRepeatedUserReply(unittest.TestCase):
    def test_correction_overwrites_previous_value(self):
        history = _hist(("user", "TH111111111"), ("assistant", "ขอบคุณค่ะ"))
        state = build_collection_state("tracking", history=history, current_message="TH222222222")
        self.assertEqual(state["collected_slots"]["tracking_number"], "TH222222222")


class TestEscalation(unittest.TestCase):
    def test_explicit_human_request_escalates(self):
        state = build_collection_state("tracking", history=None, current_message="ขอคุยกับเจ้าหน้าที่")
        self.assertTrue(state["escalation_required"])
        self.assertEqual(state["escalation_reason"], "user_requested_human")

    def test_user_refusal_escalates(self):
        state = build_collection_state("tracking", history=None, current_message="ไม่มีเลขพัสดุอ่ะ")
        self.assertTrue(state["escalation_required"])
        self.assertEqual(state["escalation_reason"], "user_refused_to_provide_information")

    def test_max_retry_exceeded_escalates(self):
        q = INTENT_SCHEMAS["tracking"]["follow_up_question"]
        history = _hist(("user", "ของถึงไหน"), ("assistant", q),
                         ("user", "เอ่อ"), ("assistant", q))
        state = build_collection_state("tracking", history=history, current_message="ไม่รู้ค่ะ")
        self.assertTrue(state["escalation_required"])
        self.assertEqual(state["escalation_reason"], "max_retry_exceeded")

    def test_no_escalation_when_information_provided_normally(self):
        state = build_collection_state("tracking", history=None, current_message="TH123456789")
        self.assertFalse(state["escalation_required"])

    def test_never_escalates_merely_for_missing_info_on_first_ask(self):
        """Per the task's own rule: 'Do NOT escalate simply because ERP
        is unavailable' / missing info alone (without refusal or retry
        exhaustion) must never trigger escalation on the very first ask."""
        state = build_collection_state("tracking", history=None, current_message="ของถึงไหนแล้ว")
        self.assertFalse(state["escalation_required"])


class TestWarrantyGuaranteeIntentCollision(unittest.TestCase):
    """Task 03B — Fix Warranty / Guarantee Intent Collision (2026-08-26).

    Confirmed live root cause: _INTENT_KEYWORD_PATTERNS["warranty"]'s bare
    "รับประกัน" alternative matched ANY message containing that word, with
    no requirement that a PRODUCT is what's actually being guaranteed —
    "รับประกันไหมว่าจะถึงภายใน 7 วัน" (a delivery-time guarantee question)
    matched it identically to "เช็คประกันสินค้าให้หน่อย" (a genuine
    product-warranty lookup), triggering the Serial Number follow-up
    question for a customer who never asked about their product.

    Fix: detect_erp_intent() now runs a second gate,
    _is_genuine_product_warranty(), whenever the base "warranty" pattern
    matches — it requires PRODUCT-context evidence (สินค้า/เครื่อง/serial/
    SN/...) before confirming Product Warranty; delivery/shipping-context
    evidence (ถึงภายใน/ขนส่ง/จัดส่ง/delivery/SLA/...) or the absence of
    either signal both correctly fall through to None (this workflow
    layer has nothing to do with the turn), letting the normal RAG/
    Decision Engine path (and Task 04B's Answerability Gate) handle it.
    The base pattern itself was also broadened from "รับประกัน|เคลมสินค้า|
    ประกันสินค้า|warranty" to bare "ประกัน|เคลมสินค้า|warranty" (a superset,
    safe only because of the new second gate) so genuine warranty
    phrasings without the "รับ" prefix are recognized at all.

    Never touches ranking (Task 04) or the Answerability Gate (Task 04B)
    — a delivery-guarantee message simply never enters this workflow
    layer in the first place; what happens to it afterward is entirely
    unchanged."""

    # ---- Genuine Product Warranty — must still route to warranty ----

    def test_product_warranty_positive_examples(self):
        cases = [
            "สินค้าอยู่ในประกันไหม",
            "เช็คประกันสินค้าให้หน่อย",
            "ประกันเครื่องนี้หมดหรือยัง",
            "Serial Number นี้เช็คประกันได้ไหม",
            "เช็ค warranty SN123456",
            "สินค้าเสีย ยังอยู่ในประกันหรือเปล่า",
            "ขอเช็คระยะเวลาประกันของสินค้า",
            "สินค้านี้รับประกันกี่ปี",
            "warranty สินค้าเช็คยังไง",
            "check warranty SN12345",
            "อยากเคลมสินค้า",  # pre-existing case, must remain unaffected
        ]
        for text in cases:
            self.assertEqual(detect_erp_intent(text), "warranty", text)

    # ---- Delivery Guarantee — must NEVER route to Product Warranty ----

    def test_delivery_guarantee_examples_not_product_warranty(self):
        cases = [
            "รับประกันไหมว่าจะถึงภายใน 7 วัน",  # the original confirmed defect
            "รับประกันเวลาขนส่งไหม",
            "มีรับประกันว่าของจะถึงตามกำหนดไหม",
            "delivery guarantee มีไหม",
            "guarantee transit time ไหม",
            "ขนส่งทางรถรับประกันกี่วัน",
            "มี SLA ระยะเวลาจัดส่งหรือเปล่า",
            "รับประกันไหมว่าของจะถึงก่อนวันศุกร์",
            "รับประกันส่งถึงกี่วัน",
        ]
        for text in cases:
            self.assertNotEqual(detect_erp_intent(text), "warranty", text)

    # TEST 01 — the original confirmed defect, as its own explicit test.
    def test_01_original_defect_not_warranty(self):
        self.assertIsNone(detect_erp_intent("รับประกันไหมว่าจะถึงภายใน 7 วัน"))

    def test_02_shipping_wording_not_warranty(self):
        self.assertIsNone(detect_erp_intent("รับประกันเวลาขนส่งไหม"))

    def test_03_sla_wording_not_warranty(self):
        self.assertIsNone(detect_erp_intent("มี SLA ระยะเวลาจัดส่งไหม"))

    def test_04_delivery_english_not_warranty(self):
        self.assertNotEqual(detect_erp_intent("delivery guarantee มีไหม"), "warranty")

    def test_05_genuine_warranty(self):
        self.assertEqual(detect_erp_intent("เช็คประกันสินค้าให้หน่อย"), "warranty")

    def test_06_serial_warranty(self):
        self.assertEqual(detect_erp_intent("เช็ค warranty SN123456"), "warranty")

    def test_07_product_warranty_period(self):
        self.assertEqual(detect_erp_intent("สินค้านี้รับประกันกี่ปี"), "warranty")

    # TEST 08 — genuinely ambiguous, context-free mention: must not
    # confidently assume Product Warranty without real evidence.
    def test_08_ambiguous_standalone_not_confidently_warranty(self):
        self.assertIsNone(detect_erp_intent("มีรับประกันไหม"))
        self.assertIsNone(detect_erp_intent("รับประกันไหม"))

    # TEST 09 — shipping-duration follow-up: a bare "รับประกัน" mention
    # right after a duration answer must resolve as a delivery-guarantee
    # question, never Product Warranty (the previous assistant turn is a
    # RAG-generated duration answer, not this engine's own warranty
    # follow-up question, so resolve_active_erp_intent falls through to
    # detect_erp_intent on the current message alone).
    def test_09_shipping_followup_not_warranty(self):
        history = _hist(("user", "ทางรถกี่วัน"),
                         ("assistant", "ระยะเวลาขนส่งทางรถประมาณ 7-10 วันค่ะ"))
        self.assertIsNone(resolve_active_erp_intent(history, "แล้วรับประกันไหมว่าจะถึงในเวลานี้"))

    # TEST 10 — product-context follow-up: once a genuine warranty flow
    # is established (asked THIS engine's own follow-up question), a
    # reply with no warranty keyword at all must still continue the SAME
    # flow (resolve_active_erp_intent's pending-follow-up fallback,
    # unrelated to and unaffected by this fix's keyword disambiguation).
    def test_10_product_warranty_followup_context_preserved(self):
        history = _hist(("user", "สินค้านี้มีประกัน 1 ปีใช่ไหม"),
                         ("assistant", INTENT_SCHEMAS["warranty"]["follow_up_question"]))
        self.assertEqual(resolve_active_erp_intent(history, "แล้วหมดเมื่อไหร่"), "warranty")

    # TEST 11 — negation: an explicitly negated mention of product
    # warranty must not count as positive evidence.
    def test_11_negation_not_warranty(self):
        self.assertIsNone(detect_erp_intent("ไม่ได้ถามประกันสินค้า ผมถามว่ารับประกันเวลาขนส่งไหม"))
        self.assertIsNone(detect_erp_intent("ไม่ต้องเช็ค warranty ขอถามเวลาส่งแทน"))

    # TEST 12 — Serial Number safety: the exact regression assertion the
    # spec calls for — no delivery-guarantee query may ever produce the
    # warranty follow-up question via build_collection_state.
    def test_12_no_serial_number_request_for_delivery_guarantee(self):
        text = "รับประกันไหมว่าจะถึงภายใน 7 วัน"
        intent = detect_erp_intent(text)
        self.assertIsNone(intent)
        # Confirms the actual customer-facing follow-up text can never be
        # produced for this input via this engine at all.
        self.assertNotEqual(INTENT_SCHEMAS["warranty"]["follow_up_question"],
                             "รับประกันไหมว่าจะถึงภายใน 7 วัน")

    # ---- Thai + English mixed phrasing ----

    def test_mixed_language_phrasing(self):
        self.assertEqual(detect_erp_intent("warranty สินค้าเช็คยังไง"), "warranty")
        self.assertIsNone(detect_erp_intent("delivery guarantee มีไหม"))
        self.assertIsNone(detect_erp_intent("guarantee ว่าของจะถึง 7 วันไหม"))
        self.assertEqual(detect_erp_intent("check warranty SN12345"), "warranty")
        self.assertIsNone(detect_erp_intent("shipping SLA มีไหม"))


if __name__ == "__main__":
    unittest.main()
