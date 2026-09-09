# -*- coding: utf-8 -*-
"""OWNER-REAL-LINE-FIX-04 — a product name supplied in reply to the
assistant's own "which product?" question must continue the active
IMPORT_INTEREST journey, not be stolen by the PRODUCT_POLICY /
prohibited-goods classifier.

Real LINE:
  user: สวัสดีค่ะ
  bot:  สวัสดีค่ะ! ...
  user: อยากสั่งของจากจีน
  bot:  ได้ค่ะ 😊 ... บอกคร่าว ๆ ได้เลยว่าอยากสั่งสินค้าอะไร ...
  user: เป็นชั้นวางของ
  bot (WRONG): "ชั้นวางของไม่ได้อยู่ในรายการสินค้าที่ห้ามนำเข้า..." +
               "มีสินค้าอย่างอื่นที่ต้องการให้ช่วยเช็กไหมคะ"

ROOT CAUSE: services/conversation_semantics.py::interpret's
INVOICE-PRODUCT-REGRESSION-2 rule force-routed ANY bare product reply to
an assistant product-question into PRODUCT_POLICY, regardless of whether
the surrounding journey was an import-interest discovery or a genuine
restriction inquiry.

FIX: the bare product name feeds whichever journey asked for it — an
IMPORT_INTEREST journey keeps going; PRODUCT_POLICY only wins on an
explicit restriction context or a trusted high-risk product marker.
"""
import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("OPENAI_API_KEY", "sk-invalid-owner04")

from services.conversation_semantics import interpret

_ASK = ("ได้ค่ะ 😊 ถ้ามีลิงก์สินค้าที่สนใจจาก Taobao, 1688 หรือ Tmall ส่งมาได้เลยนะคะ "
        "ถ้ายังไม่มีลิงก์ บอกคร่าว ๆ ได้เลยว่าอยากสั่งสินค้าอะไร เดี๋ยวช่วยแนะนำขั้นตอนต่อให้ค่ะ")

_H = [{"role": "user", "content": "สวัสดีค่ะ"},
      {"role": "assistant", "content": "สวัสดีค่ะ! มีอะไรให้ช่วยเหลือหรือสอบถามเพิ่มเติมไหมคะ? 😊"},
      {"role": "user", "content": "อยากสั่งของจากจีน"},
      {"role": "assistant", "content": _ASK}]

_POLICY_MARKERS = ("ห้ามนำเข้า", "รายการสินค้าที่ห้าม", "ไม่ได้อยู่ในรายการ",
                   "สินค้าต้องห้าม", "ของต้องห้าม")
_JOURNEY_END = ("มีสินค้าอย่างอื่น", "ให้ช่วยเช็ก")
_AUTH = ("รหัสลูกค้า", "ยืนยันตัวตน", "เบอร์ที่ผูก", "CustCode")
_FAKE_CS = ("ประสานเจ้าหน้าที่", "ทีมงานจะติดต่อกลับ", "ให้เจ้าหน้าที่ตรวจสอบ", "แอดมินช่วย")


def _engine():
    from tests.test_business_action_registry import reset_real_registry
    reset_real_registry()
    from services.decision_engine import DecisionEngine
    return DecisionEngine()


def _decide(eng, msg, history, ok_link=None):
    ctx = {"channel": "line", "tenant_id": "default", "external_user_id": "U_fix04",
           "developer_mode": True, "customer_context": {}}
    with patch("services.action_executor.requests.request") as mq:
        mq.return_value.status_code = 200 if ok_link else 400
        payload = {"data": {"Link": ok_link}} if ok_link else {"error": "bad"}
        mq.return_value.json = lambda: payload
        mq.return_value.text = "{}"
        with patch("services.playground_orchestrator.run_playground_turn",
                   return_value=_fake_pg()):
            r = eng.decide(msg, history=list(history), context=ctx)
    d = r.get("developer") or {}
    return {"routing": r["routing"]["type"], "reply": r["reply"]["text"],
            "sel": d.get("selected_business_action"),
            "src": d.get("selection_source"),
            "handoff": (r.get("handoff_payload") or {}).get("reason"),
            "fam": d.get("semantic_intent_family") or (d.get("semantic") or {}).get("intent_family")}


def _fake_pg():
    try:
        from tests.test_decision_engine import _fake_playground_result
        return _fake_playground_result(answer="[RAG]", confidence=0.9)
    except Exception:
        return {"answer": "[RAG]", "confidence": 0.9, "citations": [], "sources": []}


class TestProductSlotContinuesImportJourney(unittest.TestCase):
    def setUp(self):
        self.e = _engine()

    def _assert_import_continuation(self, r, product_hint=None):
        # never the prohibited-goods framing, never a journey-ending
        # "anything else to check", never auth / fake handoff
        for m in _POLICY_MARKERS:
            self.assertNotIn(m, r["reply"], r["reply"])
        for m in _JOURNEY_END:
            self.assertNotIn(m, r["reply"], r["reply"])
        for m in _AUTH + _FAKE_CS:
            self.assertNotIn(m, r["reply"], r["reply"])
        self.assertIsNone(r["handoff"])
        self.assertNotEqual(r["sel"], "geturlproductdetail")
        self.assertTrue(r["reply"].strip())

    def test_A_bare_shelf_continues_journey(self):
        self._assert_import_continuation(_decide(self.e, "เป็นชั้นวางของ", _H))

    def test_B_table(self):
        self._assert_import_continuation(_decide(self.e, "โต๊ะค่ะ", _H))

    def test_C_machinery(self):
        self._assert_import_continuation(_decide(self.e, "เครื่องจักร", _H))

    def test_D_clothes(self):
        self._assert_import_continuation(_decide(self.e, "เสื้อผ้า", _H))

    def test_interpret_family_is_import_interest(self):
        for m in ("เป็นชั้นวางของ", "โต๊ะค่ะ", "เก้าอี้", "เสื้อผ้า", "เครื่องจักร",
                  "กล่องพลาสติก", "ของใช้ในบ้าน"):
            it = interpret(m, _H)
            self.assertEqual(it.intent_family, "IMPORT_INTEREST", m)
            self.assertTrue(it.entities.get("product"), m)


class TestHighRiskProductStillPolicy(unittest.TestCase):
    def test_E_liquid_cleaner_routes_policy(self):
        self.assertEqual(interpret("เป็นน้ำยา", _H).intent_family, "PRODUCT_POLICY")

    def test_F_battery_routes_policy(self):
        self.assertEqual(interpret("มีแบตเตอรี่", _H).intent_family, "PRODUCT_POLICY")

    def test_perfume_routes_policy(self):
        self.assertEqual(interpret("น้ำหอมครับ", _H).intent_family, "PRODUCT_POLICY")


class TestExplicitPolicyUnchanged(unittest.TestCase):
    def test_G_explicit_import_question_is_policy(self):
        self.assertEqual(interpret("ชั้นวางของนำเข้าได้ไหม", []).intent_family, "PRODUCT_POLICY")

    def test_H_battery_ship_question_is_policy(self):
        self.assertEqual(interpret("แบตเตอรี่ส่งได้ไหม", []).intent_family, "PRODUCT_POLICY")

    def test_restriction_context_in_history_makes_bare_noun_policy(self):
        hp = [{"role": "user", "content": "มีของห้ามนำเข้าไหมคะ"},
              {"role": "assistant", "content": "มีค่ะ เช่น ของเหลว อาหาร ... สินค้าที่ต้องการนำเข้าคืออะไรคะ"}]
        self.assertEqual(interpret("โต๊ะค่ะ", hp).intent_family, "PRODUCT_POLICY")

    def test_bare_noun_with_no_product_question_stays_unknown(self):
        self.assertEqual(interpret("กล่องพลาสติกครับ", []).intent_family, "UNKNOWN")
        self.assertEqual(interpret("แก้วน้ำ", []).intent_family, "UNKNOWN")


class TestTopicSwitchAndLinkContinuation(unittest.TestCase):
    def setUp(self):
        self.e = _engine()

    def test_J_topic_switch_after_product_wins(self):
        h = _H + [{"role": "user", "content": "เป็นชั้นวางของ"},
                  {"role": "assistant", "content": "รับทราบค่ะ (สินค้า ชั้นวางของ) รบกวนแจ้งจำนวนโดยประมาณด้วยนะคะ"},
                  {"role": "user", "content": "ไม่เอาแล้ว"},
                  {"role": "assistant", "content": "ได้ค่ะ มีอะไรให้ช่วยเพิ่มเติมไหมคะ"}]
        r = _decide(self.e, "ขอเบอร์ติดต่อ", h)
        self.assertNotEqual(r["sel"], "geturlproductdetail")
        # a contact request must not resurface the stale product journey
        self.assertNotIn("ชั้นวางของ", r["reply"])

    def test_I_valid_link_after_product_still_converts(self):
        h = _H + [{"role": "user", "content": "เป็นชั้นวางของ"},
                  {"role": "assistant", "content": "รับทราบค่ะ (สินค้า ชั้นวางของ) รบกวนแจ้งจำนวนโดยประมาณด้วยนะคะ"}]
        r = _decide(self.e, "https://detail.1688.com/offer/652702302959.html", h,
                    ok_link="https://fasttrade.in.th/PageProductDetailGuest/1688/652702302959/home/guest/index")
        self.assertEqual(r["routing"], "API")
        self.assertIn("fasttrade.in.th/PageProductDetailGuest/1688/652702302959", r["reply"])


if __name__ == "__main__":
    unittest.main()
