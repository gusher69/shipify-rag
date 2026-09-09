# -*- coding: utf-8 -*-
"""OWNER-REAL-LINE-FIX-05 — a short CORRECTION / REJECTION / AMBIGUOUS
turn inside an active IMPORT_INTEREST journey is resolved against the
FRAME, before any generic RAG / general knowledge.

Real LINE:
  user: อยากสั่งของจากจีน
  bot:  ... บอกสินค้าได้เลย
  user: เป็นชั้นวางของ
  bot:  รับทราบค่ะ (สินค้า ชั้นวางของ) รบกวนแจ้งจำนวนโดยประมาณด้วยนะคะ
  user: เปลี่ยนเป้นรองเท้าได้ไหม
  bot (WRONG): store return / exchange policy answer

ROOT CAUSE: the "เปลี่ยนเป็น X" / "เอา X แทน" / "เอ้ย <n>" correction
shapes were recognised by neither _followup_op nor is_frame_followup, so
follow_up_op stayed NONE, the frame-attribution block in interpret() was
skipped, the SEM-GEN-1 decision-engine path (gated on is_frame_followup)
never fired, and the turn fell through to RAG where "can I change/
exchange to shoes" reads as a return-policy question.

FIX: recognise the correction shapes; a deterministic, typo-tolerant
resolver (resolve_frame_correction) carries the offline / degraded /
typo path, the gated LLM resolve_followup() is tried first in
production; an explicit topic switch (CONTACT_INFO / ADDRESS_CHANGE /
LINK_CONVERSION / a withdrawal / a private-state inquiry) still wins.
"""
import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("OPENAI_API_KEY", "sk-invalid-owner05")

from services.conversation_semantics import (
    resolve_frame_correction, is_frame_followup, Frame, frame_ack_reply,
    derive_active_frame,
)

_F = Frame(product="ชั้นวางของ")


class TestDeterministicResolver(unittest.TestCase):
    def _op(self, msg, frame=_F):
        return resolve_frame_correction(msg, frame)

    def test_product_corrections(self):
        for msg, want in [
            ("เปลี่ยนเป็นรองเท้า", "รองเท้า"),
            ("เปลี่ยนเป้นรองเท้าได้ไหม", "รองเท้า"),   # typo เป้น
            ("เปลียนเปนรองเท้า", "รองเท้า"),           # typo เปลียน/เปน
            ("เอาเป็นโต๊ะแทน", "โต๊ะ"),
            ("เอาเป็นเก้าอี้แทน", "เก้าอี้"),
            ("เปลี่ยนของ เป็นเสื้อผ้าได้ไหม", "เสื้อผ้า"),
            ("ไม่ใช่โต๊ะ เป็นเก้าอี้", "เก้าอี้"),
            ("ไม่ไช่โต๊ะ เป้นเก้าอี้", "เก้าอี้"),      # typo ไช่/เป้น
        ]:
            r = self._op(msg)
            self.assertEqual(r["op"], "CHANGE_TARGET", msg)
            self.assertEqual(r["product"], want, msg)

    def test_quantity_corrections(self):
        for msg, want in [("เอ้ย 20 ชิ้น", 20), ("แก้เป็น 5 คู่", 5), ("เอ้ย 20", 20)]:
            r = self._op(msg)
            self.assertEqual(r["op"], "CORRECT_QUANTITY", msg)
            self.assertEqual(r["quantity"], want, msg)

    def test_bare_quantity_is_set_quantity(self):
        for msg, want in [("20 คู่", 20), ("ประมาณ 300 ชิ้น", 300), ("10 ตัว", 10)]:
            r = self._op(msg)
            self.assertEqual(r["op"], "SET_QUANTITY", msg)
            self.assertEqual(r["quantity"], want, msg)

    def test_shipping_corrections(self):
        self.assertEqual(self._op("เปลี่ยนเป็นทางเรือ")["method"], "sea")
        self.assertEqual(self._op("เอาทางรถแทน")["method"], "road")

    def test_brand_corrections_take_the_last_token(self):
        self.assertEqual(self._op("เอ้ย FT")["brand"], "FT")
        self.assertEqual(self._op("ไม่ใช่ SP เป็น FT")["brand"], "FT")
        self.assertEqual(self._op("ไม่ไช่ SP เป้น FT")["brand"], "FT")

    def test_rejection(self):
        for msg in ["ไม่เอาแล้ว", "ไม่เอาแล้วค่ะ", "ยกเลิก", "พอแล้ว", "ไม่ต้องแล้ว"]:
            self.assertEqual(self._op(msg)["op"], "REJECT", msg)

    def test_ambiguous_change_has_no_guess(self):
        for msg in ["เปลี่ยนได้ไหม", "เปลี่ยนได้ไหมคะ", "ขอเปลี่ยนได้มั้ย"]:
            self.assertEqual(self._op(msg)["op"], "AMBIGUOUS", msg)

    def test_not_a_correction_stays_unknown(self):
        for msg in ["ขอเบอร์ติดต่อ", "สนใจนำเข้ารองเท้า", "สั่งเยอะได้ไหม",
                    "เปลี่ยนที่อยู่", "เปลี่ยนที่อยู่จัดส่งหน่อย", "ถอนเงินขนส่งยังไง",
                    "https://detail.1688.com/offer/1.html"]:
            self.assertEqual(self._op(msg)["op"], "UNKNOWN", msg)

    def test_no_frame_no_action(self):
        self.assertEqual(resolve_frame_correction("เปลี่ยนเป็นรองเท้า", None)["op"], "UNKNOWN")


class TestFrameAckProduct(unittest.TestCase):
    def test_product_ack_is_natural_and_state_carrying(self):
        s = frame_ack_reply(Frame(product="รองเท้า"), changed="product")
        self.assertIn("เปลี่ยนเป็นรองเท้า", s)
        self.assertNotIn("(product=", s)
        # the (สินค้า X) token must still be readable by derive_active_frame
        f = derive_active_frame([{"role": "user", "content": "สนใจนำเข้ากระเป๋า"},
                                 {"role": "assistant", "content": s}])
        self.assertEqual(f.product, "รองเท้า")


def _engine():
    from tests.test_business_action_registry import reset_real_registry
    reset_real_registry()
    from services.decision_engine import DecisionEngine
    return DecisionEngine()


def _pg():
    try:
        from tests.test_decision_engine import _fake_playground_result
        return _fake_playground_result(answer="[RAG-POLICY-ANSWER]", confidence=0.9)
    except Exception:
        return {"answer": "[RAG-POLICY-ANSWER]", "confidence": 0.9, "citations": [], "sources": []}


_A = ("ได้ค่ะ 😊 ถ้ามีลิงก์สินค้าที่สนใจจาก Taobao, 1688 หรือ Tmall ส่งมาได้เลยนะคะ "
      "ถ้ายังไม่มีลิงก์ บอกคร่าว ๆ ได้เลยว่าอยากสั่งสินค้าอะไร เดี๋ยวช่วยแนะนำขั้นตอนต่อให้ค่ะ")
_POLICY_WORDS = ("คืนสินค้า", "เปลี่ยนสินค้า", "เงื่อนไขการคืน", "ใบเสร็จ", "return", "refund",
                 "[RAG-POLICY-ANSWER]")


class _Conv:
    def setUp(self):
        self.e = _engine()
        self.h = []

    def say(self, msg):
        with patch("services.action_executor.requests.request",
                   return_value=MagicMock(status_code=200, json=lambda: {"data": {}}, text="{}")), \
             patch("services.playground_orchestrator.run_playground_turn", return_value=_pg()):
            r = self.e.decide(msg, history=list(self.h), context={
                "channel": "line", "tenant_id": "default", "external_user_id": "U_o5",
                "developer_mode": True, "customer_context": {}})
        rep = r["reply"]["text"]
        d = r.get("developer") or {}
        self.h += [{"role": "user", "content": msg}, {"role": "assistant", "content": rep}]
        return {"routing": r["routing"]["type"], "reply": rep,
                "src": d.get("selection_source"), "op": d.get("semantic_op"),
                "sel": d.get("selected_business_action"),
                "handoff": (r.get("handoff_payload") or {}).get("reason")}


class TestCorrectionThroughDecisionEngine(_Conv, unittest.TestCase):
    def _seed(self, product="ชั้นวางของ"):
        self.h = [{"role": "user", "content": "อยากสั่งของจากจีน"},
                  {"role": "assistant", "content": _A},
                  {"role": "user", "content": f"เป็น{product}"},
                  {"role": "assistant", "content": f"รับทราบค่ะ (สินค้า {product}) รบกวนแจ้งจำนวนโดยประมาณด้วยนะคะ"}]

    def _assert_no_policy(self, r):
        for w in _POLICY_WORDS:
            self.assertNotIn(w, r["reply"], r["reply"])
        self.assertIsNone(r["handoff"])
        self.assertNotEqual(r["routing"], "RAG")

    def test_A_repro_product_correction_with_typo(self):
        self._seed()
        r = self.say("เปลี่ยนเป้นรองเท้าได้ไหม")
        self._assert_no_policy(r)
        self.assertEqual(r["op"], "CHANGE_TARGET")
        self.assertIn("รองเท้า", r["reply"])

    def test_B_product_correction_take_instead(self):
        self._seed("โต๊ะ")
        r = self.say("เอาเป็นเก้าอี้แทน")
        self._assert_no_policy(r)
        self.assertIn("เก้าอี้", r["reply"])

    def test_C_not_ใช่_form(self):
        self._seed("โต๊ะ")
        r = self.say("ไม่ใช่โต๊ะ เป็นเก้าอี้")
        self._assert_no_policy(r)
        self.assertIn("เก้าอี้", r["reply"])

    def test_high_risk_correction_goes_to_trusted_policy(self):
        self._seed()
        r = self.say("เปลี่ยนเป็นแบตเตอรี่")
        # trusted policy path is allowed to answer here (RAG); never a
        # fake handoff, never an auth prompt
        self.assertIsNone(r["handoff"])
        for a in ("รหัสลูกค้า", "ยืนยันตัวตน"):
            self.assertNotIn(a, r["reply"])

    def test_shipping_method_correction(self):
        self.h = [{"role": "user", "content": "อยากสั่งของจากจีน"},
                  {"role": "assistant", "content": _A},
                  {"role": "user", "content": "เป็นชั้นวางของ"},
                  {"role": "assistant", "content": "รับทราบค่ะ (สินค้า ชั้นวางของ • จำนวนประมาณ 10 ชิ้น) สนใจส่งทางรถหรือทางเรือคะ"}]
        r = self.say("เปลี่ยนเป็นทางเรือ")
        self._assert_no_policy(r)
        self.assertIn("ทางเรือ", r["reply"])
        self.assertIn("ชั้นวางของ", r["reply"])   # product retained

    def test_rejection_cancels_journey_and_next_turn_is_free(self):
        self._seed("รองเท้า")
        r1 = self.say("ไม่เอาแล้ว")
        self.assertEqual(r1["src"], "frame_reject_fix05")
        self._assert_no_policy(r1)
        r2 = self.say("ขอเบอร์ติดต่อ")
        self.assertNotEqual(r2["sel"], "geturlproductdetail")
        self.assertIn("02-026-6426", r2["reply"])
        self.assertNotIn("รองเท้า", r2["reply"])

    def test_ambiguous_change_asks_which_slot(self):
        self._seed()
        r = self.say("เปลี่ยนได้ไหม")
        self.assertEqual(r["src"], "frame_correction_ambiguous_fix05")
        self.assertIn("สินค้า", r["reply"])
        self.assertIn("จำนวน", r["reply"])
        self.assertIn("จัดส่ง", r["reply"])

    def test_topic_switch_not_intercepted(self):
        self._seed()
        r = self.say("ขอเบอร์ติดต่อ")
        self.assertNotIn(r["src"], ("frame_correction_fix05", "frame_reject_fix05",
                                    "frame_correction_ambiguous_fix05"))
        self.assertIn("02-026-6426", r["reply"])

    def test_url_topic_switch_not_intercepted(self):
        self._seed()
        r = self.say("https://detail.1688.com/offer/652702302959.html")
        self.assertNotIn(str(r["src"]), ("frame_correction_fix05", "frame_reject_fix05"))

    def test_full_conversation_A(self):
        self.h = []
        self.say("อยากสั่งของจากจีน")
        self.say("เป็นชั้นวางของ")
        r3 = self.say("เปลี่ยนเป้นรองเท้าได้ไหม")
        self.assertIn("รองเท้า", r3["reply"])
        r4 = self.say("20 คู่")
        self.assertIn("20", r4["reply"])
        self.assertIn("รองเท้า", r4["reply"])
        for w in _POLICY_WORDS:
            self.assertNotIn(w, r3["reply"] + r4["reply"])

    def test_full_conversation_B(self):
        self.h = []
        self.say("อยากสั่งของจากจีน")
        self.say("โต๊ะ")
        self.say("10 ตัว")
        r4 = self.say("เอ้ย เปลี่ยนเป็นเก้าอี้")
        self.assertIn("เก้าอี้", r4["reply"])
        self.assertIn("10", r4["reply"])          # quantity retained
        r5 = self.say("20 ตัว")
        self.assertIn("20", r5["reply"])
        self.assertIn("เก้าอี้", r5["reply"])


if __name__ == "__main__":
    unittest.main()
