# -*- coding: utf-8 -*-
"""CUSTOMER-CALC-1.1 — REAL LINE conversation-state fixes for the
shipping-cost estimate flow (production session 6c9b9026).

Two systemic defects the REAL LINE run exposed:
  1. conversational route variants ("รถครับ" / "เรือครับ" / "ทางรถค่ะ")
     were not normalised while the calculator was collecting.
  2. a COMPLETED / CLOSED calculation leaked its slots into the NEXT
     explicit calculator request (turns echoed a stale ROAD 192.26).

Every test below runs the exact REAL LINE sequence IN ONE SESSION — a
single DecisionEngine and a single accumulating `history`.
"""
import unittest
from unittest.mock import MagicMock, patch

from services.decision_engine import DecisionEngine
from services.shipping_estimate_flow import _method_of, derive_estimate_state
from tests.test_decision_engine import _fake_playground_result


class TestRouteTokenNormalisation(unittest.TestCase):
    """FIX 1 — structural token parsing, not a sentence dictionary."""

    def test_road_variants(self):
        for t in ("รถ", "รถครับ", "รถค่ะ", "ทางรถ", "ทางรถครับ", "ทางรถค่ะ",
                  "เอารถ", "ขอทางรถ", "ใช้รถ", "เป็นรถ"):
            self.assertEqual(_method_of(t), "road", t)

    def test_sea_variants(self):
        for t in ("เรือ", "เรือครับ", "เรือค่ะ", "ทางเรือ", "ทางเรือครับ",
                  "เอาเรือ", "ขอทางเรือ", "เรือครับ "):
            self.assertEqual(_method_of(t), "sea", t)

    def test_unrelated_sentences_with_the_word_are_not_a_route_answer(self):
        for t in ("รถของผมจอดอยู่ไหน", "เรือสินค้ามาถึงหรือยัง",
                  "สนใจนำเข้ารถมอเตอร์ไซค์", "อยากส่งของขึ้นเรือประมง "):
            self.assertIsNone(_method_of(t), t)


class TestEpisodeLifecycleUnit(unittest.TestCase):
    """FIX 2 — NEW -> COLLECTING -> CALCULATED -> CLOSED, at the
    derive_estimate_state level."""

    _PROMPT = "รับทราบค่ะ (น้ำหนัก 2 กก.) ต้องการประเมินทางรถหรือทางเรือคะ"
    _RESULT = ("ประเมินเบื้องต้นสำหรับทางรถประมาณ 192.26 บาทค่ะ "
               "โดยคิดจากปริมาตร 0.027864 CBM (เป็นการประเมินเบื้องต้น...)")

    def _h(self, *pairs):
        return [{"role": r, "content": c} for r, c in pairs]

    def test_explicit_new_request_after_completion_inherits_nothing(self):
        h = self._h(
            ("user", "ค่านำเข้าเท่าไหร่คะ น้ำหนัก 2กิโล ขนาด 54*12*43"),
            ("assistant", self._PROMPT),
            ("user", "รถครับ"),
            ("assistant", self._RESULT),
        )
        st = derive_estimate_state(h, "ช่วยคำนวณค่าส่งให้หน่อย น้ำหนัก 2 โล")
        self.assertIsNotNone(st)
        self.assertEqual(st.weight, 2.0)
        self.assertIsNone(st.length)      # NOT 54 from the closed episode
        self.assertIsNone(st.method)      # NOT "road" from the closed episode

    def test_explicit_new_request_during_collection_also_resets(self):
        h = self._h(
            ("user", "ช่วยคำนวณค่าส่ง น้ำหนัก 5 กิโล ขนาด 10x10x10"),
            ("assistant", "รับทราบค่ะ ต้องการประเมินทางรถหรือทางเรือคะ"),
        )
        st = derive_estimate_state(h, "ค่านำเข้าเท่าไหร่ น้ำหนัก 2 โล")
        self.assertEqual(st.weight, 2.0)
        self.assertIsNone(st.length)      # NOT 10 from the still-open episode

    def test_short_value_answer_continues_the_episode(self):
        h = self._h(
            ("user", "ช่วยคำนวณค่าส่งให้หน่อย น้ำหนัก 2 โล"),
            ("assistant", "ยังขาดข้อมูลสำหรับประเมินค่ะ รบกวนแจ้งขนาดสินค้า..."),
        )
        st = derive_estimate_state(h, "54x12x43")
        self.assertEqual(st.weight, 2.0)               # kept
        self.assertEqual((st.length, st.width, st.height), (54.0, 12.0, 43.0))

    def test_comparison_followup_reuses_the_completed_values(self):
        h = self._h(
            ("user", "ช่วยคำนวณค่าส่ง น้ำหนัก 2 โล ขนาด 54x12x43"),
            ("assistant", "รับทราบค่ะ ต้องการประเมินทางรถหรือทางเรือคะ"),
            ("user", "ทางรถ"),
            ("assistant", self._RESULT),
        )
        st = derive_estimate_state(h, "ถ้าเป็นทางเรือล่ะ")
        self.assertEqual(st.weight, 2.0)
        self.assertEqual((st.length, st.width, st.height), (54.0, 12.0, 43.0))
        self.assertEqual(st.method, "sea")            # comparison replaces method

    def test_unrelated_message_after_completion_is_not_a_calculator_turn(self):
        h = self._h(
            ("user", "ช่วยคำนวณค่าส่ง น้ำหนัก 2 โล ขนาด 54x12x43 ทางรถ"),
            ("assistant", self._RESULT),
        )
        for m in ("สนใจนำเข้า แบตเตอรรี่ หลาย ๆ อัน", "รถของผมจอดอยู่ไหน",
                  "สวัสดีครับ", "ขอบคุณค่ะ"):
            self.assertIsNone(derive_estimate_state(h, m), m)


class TestRealLineSequenceOneSession(unittest.TestCase):
    """The production turn order, threaded through ONE engine + ONE
    growing history."""

    @classmethod
    def setUpClass(cls):
        cls.eng = DecisionEngine()

    def setUp(self):
        self.h = []

    def _say(self, msg):
        ctx = {"channel": "line", "tenant_id": "default",
               "external_user_id": "U_calc11", "developer_mode": True,
               "customer_context": {}}
        with patch("services.action_executor.requests.request",
                   return_value=MagicMock(status_code=200, json=lambda: {"data": {}})), \
             patch("services.playground_orchestrator.run_playground_turn",
                   return_value=_fake_playground_result(answer="[RAG]", confidence=0.9)):
            r = self.eng.decide(msg, history=list(self.h), context=ctx)
        dev = r.get("developer") or {}
        reply = (r.get("reply") or {}).get("text") or ""
        self.h += [{"role": "user", "content": msg},
                   {"role": "assistant", "content": reply}]
        return {"routing": (r.get("routing") or {}).get("type"),
                "src": dev.get("selection_source"),
                "handoff": (r.get("handoff_payload") or {}).get("reason"),
                "reply": reply}

    def test_full_sequence(self):
        # 1. exact REAL failure input -> asks route only (not Human CS).
        r = self._say("ค่านำเข้าเท่าไหร่คะ สินค้า1ชิ้น น้ำหนัก 2กิโล ขนาด 54*12*43")
        self.assertEqual(r["routing"], "WORKFLOW")
        self.assertEqual(r["src"], "shipping_estimate_flow")
        self.assertIsNone(r["handoff"])
        self.assertIn("ทางรถหรือทางเรือ", r["reply"])

        # 2. FIX 1 — "รถครับ" is a ROAD answer -> calculates.
        r = self._say("รถครับ")
        self.assertEqual(r["routing"], "GENERAL")
        self.assertIn("192.26", r["reply"])

        # 3. FIX 2 — a NEW explicit request must NOT reuse ROAD/dims.
        r = self._say("ค่านำเข้าเท่าไหร่คะ สินค้า1ชิ้น น้ำหนัก 2กิโล ขนาด 54*12*43")
        self.assertEqual(r["routing"], "WORKFLOW")
        self.assertIn("ทางรถหรือทางเรือ", r["reply"])
        self.assertNotIn("192.26", r["reply"])          # did not auto-calc ROAD

        # 4. FIX 1 — "เรือครับ " (trailing space) is a SEA answer.
        r = self._say("เรือครับ ")
        self.assertEqual(r["routing"], "GENERAL")
        self.assertIn("125.39", r["reply"])

        # 5. FLOW B — new weight-only request: ask dims + route, inherit
        #    NEITHER the 54x12x43 nor the method.
        r = self._say("ช่วยคำนวณค่าส่งให้หน่อย น้ำหนัก 2 โล")
        self.assertEqual(r["routing"], "WORKFLOW")
        self.assertIn("ขนาด", r["reply"])
        self.assertNotIn("192.26", r["reply"])
        self.assertNotIn("125.39", r["reply"])

        # 6. short value answer continues THIS episode.
        r = self._say("54x12x43")
        self.assertEqual(r["routing"], "WORKFLOW")
        self.assertIn("ทางรถหรือทางเรือ", r["reply"])

        # 7. route answer completes it.
        r = self._say("ทางรถ")
        self.assertEqual(r["routing"], "GENERAL")
        self.assertIn("192.26", r["reply"])

        # 8. FLOW C — comparison follow-up reuses weight+dims, swaps method.
        r = self._say("ถ้าเป็นทางเรือล่ะ")
        self.assertEqual(r["routing"], "GENERAL")
        self.assertIn("125.39", r["reply"])

        # 9. correction reuses the episode, swaps the weight.
        r = self._say("ไม่ใช่ 2 กิโล เป็น 3 กิโล")
        self.assertEqual(r["routing"], "GENERAL")
        self.assertIn("125.39", r["reply"])            # CBM still dominates

        # 10. FLOW E — unrelated sales interest is NOT hijacked.
        r = self._say("สนใจนำเข้า แบตเตอรรี่ หลาย ๆ อัน")
        self.assertNotEqual(r["src"], "shipping_estimate_flow")

        # 11. FLOW E — a sentence containing "รถ" is NOT a route answer.
        r = self._say("รถของผมจอดอยู่ไหน")
        self.assertNotEqual(r["src"], "shipping_estimate_flow")

        # 12. a fresh unrelated topic is unaffected by the stale episode.
        r = self._say("มีบริการเหมารถไหมคะ")
        self.assertNotEqual(r["src"], "shipping_estimate_flow")


class TestProtectedBehaviourUnchanged(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.eng = DecisionEngine()

    def _run(self, msg, hist=None):
        ctx = {"channel": "line", "tenant_id": "default",
               "external_user_id": "U_calc11p", "developer_mode": True,
               "customer_context": {}}
        with patch("services.action_executor.requests.request",
                   return_value=MagicMock(status_code=200, json=lambda: {"data": {}})), \
             patch("services.playground_orchestrator.run_playground_turn",
                   return_value=_fake_playground_result(answer="[RAG]", confidence=0.9)):
            r = self.eng.decide(msg, history=hist or [], context=ctx)
        dev = r.get("developer") or {}
        return {"routing": (r.get("routing") or {}).get("type"),
                "src": dev.get("selection_source")}

    def test_bare_rate_faq_still_not_the_calculator(self):
        for m in ("เรทเท่าไหร่คะ", "ค่านำเข้าเท่าไหร่คะ", "เรทนำเข้าเท่าไหร่"):
            self.assertNotEqual(self._run(m)["src"], "shipping_estimate_flow", m)

    def test_fix2_true_no_info_still_not_the_calculator(self):
        r = self._run("Shipify รับประกันว่าสินค้าทุกชิ้นจะผ่านศุลกากรไหม")
        self.assertNotEqual(r["src"], "shipping_estimate_flow")

    def test_address_change_business_action_not_stolen(self):
        r = self._run("อยากเปลี่ยนที่อยู่จัดส่งบิลนี้ และช่วยประเมินค่าขนส่งถึงบ้านให้หน่อย")
        self.assertNotEqual(r["src"], "shipping_estimate_flow")


if __name__ == "__main__":
    unittest.main()
