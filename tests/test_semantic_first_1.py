# -*- coding: utf-8 -*-
"""SEMANTIC-FIRST-1 — one central conversational semantic interpretation.

services/conversation_semantics.py::interpret() is the single layer the
Decision Engine consults FIRST. Every conversational business flow
consumes its normalised {intent_family, entities, is_private,
follow_up_op} contract instead of re-classifying the raw Thai.

The paraphrases below are deliberately NOT the wordings shown in the
production prompt / earlier UAT specs — each family must resolve
SEMANTICALLY, not by exact phrase.
"""
import unittest
from unittest.mock import MagicMock, patch

from services.conversation_semantics import (
    interpret, Interpretation, INTENT_FAMILIES, FAMILY_TO_ACTIONABLE_INTENT,
)
from rag.query_understanding import classify_actionable_intent
from services.decision_engine import DecisionEngine
from tests.test_decision_engine import _fake_playground_result


# unseen paraphrase -> expected family. None of these strings appear in
# the production examples.
_UNSEEN = {
    "SHIPMENT_STATUS": [
        "ล็อตสินค้าที่สั่งไว้ตอนนี้ไปถึงไหนแล้วครับ",
        "อยากทราบว่าพัสดุผมออกจากจีนหรือยัง",
        "ออเดอร์ผมคืบหน้ายังไงบ้างคะ",
    ],
    "INVOICE": [
        "ทางร้านออกเอกสารภาษีให้ลูกค้าได้ไหมครับ",
        "อยากได้ใบเสร็จตัวจริงขอได้ที่ไหน",
    ],
    "PICKUP_LOCATION": [
        "อยากไปรับของเองต้องไปที่คลังตรงไหน",
        "โกดังฝั่งไทยตั้งอยู่แถวไหนครับ",
    ],
    "SELF_PICKUP": [
        "ขอไปหยิบสินค้าเองที่โกดังได้ไหมคะ",
        "ผมขับรถไปรับของเองเลยได้ปะ",
    ],
    "COUPON_USAGE": [
        "โค้ดส่วนลดนี่กดใช้ตรงขั้นตอนไหนของการจ่ายเงิน",
        "มีวิธีใช้ voucher ยังไงบ้าง",
    ],
    "MY_COUPONS": [
        "ตอนนี้ในบัญชีผมเหลือคูปองกี่ใบ",
    ],
    "PRODUCT_POLICY": [
        "ยาสีฟันนำเข้าได้ไหมครับ",
        "ของพวกอาหารเสริมส่งเข้ามาได้หรือเปล่า",
    ],
    "CHARTER_TRUCK": [
        "อยากจ้างรถคันนึงวิ่งส่งของต่อในไทยได้ไหม",
        "ขอใช้บริการเหมารถไปลงที่ระยองหน่อยครับ",
    ],
    "SHIPPING_ESTIMATE": [
        "ช่วยตีราคาค่าส่งของชิ้นนี้ให้หน่อยครับ",
        "กล่องขนาดประมาณนี้ส่งมาไทยประมาณกี่บาท",
    ],
    "ADDRESS_CHANGE": [
        "ขอสลับที่อยู่ผู้รับเป็นอีกที่ได้ไหมครับ",
        "อยากให้ส่งของไปที่ออฟฟิศแทนบ้านได้มั้ย",
    ],
}


class TestUnseenParaphrasesResolveSemantically(unittest.TestCase):
    """No exact-phrase rule — the compositional meaning model must
    generalise. (LLM disambiguation may be offline; the deterministic
    tier alone must carry these.)"""

    def test_every_family_resolves_an_unseen_paraphrase(self):
        failures = []
        for family, msgs in _UNSEEN.items():
            for m in msgs:
                got = interpret(m, []).intent_family
                if got != family:
                    failures.append(f"{family}: {m!r} -> {got}")
        self.assertEqual(failures, [], "unseen paraphrases misrouted:\n" + "\n".join(failures))

    def test_families_are_in_the_fixed_vocabulary(self):
        for fam in _UNSEEN:
            self.assertIn(fam, INTENT_FAMILIES)


class TestStructuralInputsSkipSemantics(unittest.TestCase):
    def test_structural_fast_path(self):
        for m, kind in [("", "empty-ish"), ("SP100045", "id"), ("https://x.co/a", "url"),
                        ("54x12x43", "dims"), ("2 3 4", "numeric")]:
            it = interpret(m, [])
            self.assertEqual(it.source, "structural", f"{m!r} should be structural")
            self.assertEqual(it.intent_family, "UNKNOWN")

    def test_greetings_and_confirmations_are_unknown_not_llm(self):
        for m in ("สวัสดีครับ", "ขอบคุณค่ะ", "ยืนยัน", "โอเคครับ", "ครับผม"):
            it = interpret(m, [])
            self.assertEqual(it.intent_family, "UNKNOWN")
            self.assertNotEqual(it.source, "llm")


class TestPrivacyStaysDeterministic(unittest.TestCase):
    def test_self_reference_marks_private_without_changing_family(self):
        it = interpret("ของผมถึงไหนแล้ว", [])
        self.assertEqual(it.intent_family, "SHIPMENT_STATUS")
        self.assertTrue(it.is_private)
        it2 = interpret("ของถึงไทยหรือยัง", [])
        self.assertEqual(it2.intent_family, "SHIPMENT_STATUS")
        self.assertFalse(it2.is_private)

    def test_self_pickup_is_public_even_with_a_pronoun(self):
        it = interpret("ผมขับรถไปรับของเองเลยได้ปะ", [])
        self.assertEqual(it.intent_family, "SELF_PICKUP")


class TestFollowUpOps(unittest.TestCase):
    def test_correction_comparison_topic_change(self):
        self.assertEqual(interpret("ไม่ใช่ 2 กิโล เป็น 3 กิโล", []).follow_up_op, "CORRECTION")
        self.assertEqual(interpret("ถ้าเป็นทางเรือล่ะ", []).follow_up_op, "COMPARISON")
        self.assertEqual(interpret("งั้นถามเรื่องคูปองดีกว่า", []).follow_up_op, "TOPIC_CHANGE")

    def test_bare_value_reply_is_set_value(self):
        self.assertEqual(interpret("200 ตัว", []).follow_up_op, "SET_VALUE")


class TestActionableIntentIsSemanticFirst(unittest.TestCase):
    """classify_actionable_intent maps the central family FIRST; the
    regex path is the fallback (and is untouched when no interpretation
    is supplied)."""

    def test_family_maps_to_bucket(self):
        for fam, bucket in FAMILY_TO_ACTIONABLE_INTENT.items():
            if not bucket:
                continue
            interp = Interpretation(intent_family=fam, confidence=0.8)
            r = classify_actionable_intent("อะไรก็ได้", interpretation=interp)
            self.assertEqual(r["actionable_intent"], bucket, fam)

    def test_regex_path_unchanged_without_interpretation(self):
        # a wording the regex classifier has always handled
        self.assertEqual(
            classify_actionable_intent("ใช้คูปองยังไง")["actionable_intent"], "coupon_policy")
        self.assertEqual(
            classify_actionable_intent("สวัสดีครับ")["actionable_intent"], "unknown")

    def test_unknown_family_falls_back_to_regex(self):
        interp = Interpretation(intent_family="UNKNOWN", confidence=0.0)
        r = classify_actionable_intent("ใช้คูปองยังไง", interpretation=interp)
        self.assertEqual(r["actionable_intent"], "coupon_policy")


class TestDownstreamRoutingConsumesTheInterpretation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.eng = DecisionEngine()

    def _run(self, msg, hist=None):
        ctx = {"channel": "line", "tenant_id": "default", "external_user_id": "U_sf1",
               "developer_mode": True, "customer_context": {}}
        with patch("services.action_executor.requests.request",
                   return_value=MagicMock(status_code=200, json=lambda: {"data": {}})), \
             patch("services.playground_orchestrator.run_playground_turn",
                   return_value=_fake_playground_result(answer="[RAG]", confidence=0.9)):
            r = self.eng.decide(msg, history=hist or [], context=ctx)
        dev = r.get("developer") or {}
        return {"routing": (r.get("routing") or {}).get("type"),
                "src": dev.get("selection_source"),
                "interp": dev.get("semantic_interpretation") or {},
                "intent": ((dev.get("intent") or {}).get("actionable_intent")
                           if isinstance(dev.get("intent"), dict) else None),
                "reply": (r.get("reply") or {}).get("text") or ""}

    def test_interpretation_is_on_every_turn(self):
        r = self._run("ใบกำกับออกได้ไหม")
        self.assertEqual(r["interp"].get("intent_family"), "INVOICE")
        self.assertEqual(r["intent"], "invoice_policy")

    def test_estimate_paraphrase_opens_the_calculator_not_no_info(self):
        r = self._run("ช่วยตีราคาค่าส่งของชิ้นนี้ให้หน่อยครับ")
        self.assertEqual(r["interp"].get("intent_family"), "SHIPPING_ESTIMATE")
        self.assertEqual(r["src"], "shipping_estimate_flow")
        self.assertEqual(r["routing"], "WORKFLOW")

    def test_shipment_status_paraphrase_is_tracking_intent(self):
        r = self._run("ล็อตสินค้าที่สั่งไว้ตอนนี้ไปถึงไหนแล้วครับ")
        self.assertEqual(r["interp"].get("intent_family"), "SHIPMENT_STATUS")
        self.assertEqual(r["intent"], "tracking_status")

    def test_charter_paraphrase_is_service_information(self):
        r = self._run("อยากจ้างรถคันนึงวิ่งส่งของต่อในไทยได้ไหม")
        self.assertEqual(r["interp"].get("intent_family"), "CHARTER_TRUCK")


class TestNoRouteWordHijack(unittest.TestCase):
    def test_unrelated_sentence_with_vehicle_word_is_not_estimate(self):
        it = interpret("รถของผมจอดอยู่ไหน", [])
        self.assertNotEqual(it.intent_family, "SHIPPING_ESTIMATE")
        self.assertNotEqual(it.intent_family, "PICKUP_LOCATION")


if __name__ == "__main__":
    unittest.main()
