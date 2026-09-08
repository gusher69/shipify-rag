# -*- coding: utf-8 -*-
"""OWNER-REAL-LINE-FIX-03 — an explicit URL from an UNSUPPORTED domain
must reach link-conversion's UNSUPPORTED_DOMAIN handling, not a RAG
no-info / Human CS handoff.

Real LINE: "https://www.google.com/test" -> "ตอนนี้ยังไม่มีข้อมูลยืนยัน
เรื่องนี้ค่ะ เดี๋ยวทางเราประสานเจ้าหน้าที่ช่วยตรวจสอบเพิ่มเติมให้นะคะ" (a
RAG no-info answer + a real Human CS handoff).

ROOT CAUSE: services/conversation_semantics.py::interpret's structural
bare-URL fast path returned LINK_CONVERSION only for a SUPPORTED-platform
hostname; an unsupported bare URL fell to UNKNOWN, so the decision
engine's link gate never fired and the turn dropped through to RAG.

FIX: any bare well-formed URL is a LINK_CONVERSION turn; the
deterministic link classifier decides the sub-state (VALID / platform
home / incomplete / UNSUPPORTED_DOMAIN).
"""
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("OPENAI_API_KEY", "sk-invalid-owner03")

from services.conversation_semantics import interpret
from services.link_conversion_flow import is_link_conversion_signal

_ASK = "ได้ค่ะ ส่งลิงก์สินค้าที่ต้องการแปลงมาได้เลยค่ะ"
_AUTH = ("รหัสลูกค้า", "ยืนยันตัวตน", "เบอร์ที่ผูก")
_FAKE_CS = ("ประสานเจ้าหน้าที่", "ทีมงานจะติดต่อกลับ", "ให้เจ้าหน้าที่ตรวจสอบ",
            "แอดมินช่วยตรวจสอบ")
_NOINFO = ("ยังไม่มีข้อมูลยืนยันเรื่องนี้", "ไม่พบข้อมูล")


def _engine():
    from tests.test_business_action_registry import reset_real_registry
    reset_real_registry()
    from services.decision_engine import DecisionEngine
    return DecisionEngine()


def _decide(eng, msg, history, ok_link=None):
    with patch("services.action_executor.requests.request") as mq:
        if ok_link:
            mq.return_value.status_code = 200
            payload = {"data": {"Link": ok_link}}
            mq.return_value.json = lambda: payload
        else:
            mq.return_value.status_code = 400
            mq.return_value.json = lambda: {"error": "bad"}
        mq.return_value.text = "{}"
        r = eng.decide(msg, history=list(history), context={"developer_mode": True})
    d = r.get("developer") or {}
    return {"routing": r["routing"]["type"], "reply": r["reply"]["text"],
            "lc_state": d.get("link_conversion_state"),
            "sel": d.get("selected_business_action")}


class TestUnsupportedExplicitURL(unittest.TestCase):
    def setUp(self):
        self.e = _engine()

    def _assert_unsupported(self, r):
        self.assertEqual(r["routing"], "WORKFLOW")
        self.assertEqual(r["lc_state"], "UNSUPPORTED_DOMAIN")
        self.assertIn("ยังไม่รองรับการแปลง", r["reply"])
        for p in ("1688", "Taobao", "Tmall"):
            self.assertIn(p, r["reply"])
        for a in _AUTH:
            self.assertNotIn(a, r["reply"])
        for f in _FAKE_CS:
            self.assertNotIn(f, r["reply"])
        for n in _NOINFO:
            self.assertNotIn(n, r["reply"])

    def test_A_bare_google_url(self):
        self._assert_unsupported(_decide(self.e, "https://www.google.com/test", []))

    def test_B_example_com_product_path(self):
        self._assert_unsupported(_decide(self.e, "https://example.com/product/123", []))

    def test_F_unsupported_url_after_shipping_answer_uses_current_turn(self):
        h = [{"role": "user", "content": "ช่วยแปลงลิงก์นี้"},
             {"role": "assistant", "content": _ASK},
             {"role": "user", "content": "https://detail.1688.com/offer/652702302959.html"},
             {"role": "assistant", "content": "แปลงลิงก์ให้เรียบร้อยแล้วค่ะ 😊\nhttps://fasttrade.in.th/x"},
             {"role": "user", "content": "แล้วค่าขนส่งทางเรือกิโลละเท่าไหร่"},
             {"role": "assistant", "content": "ทางเรือคิด 19 บาท/กิโลกรัม ค่ะ"}]
        self._assert_unsupported(_decide(self.e, "https://www.google.com/test", h))

    def test_I_unsupported_url_with_history_no_auth_no_handoff(self):
        h = [{"role": "user", "content": "สวัสดีค่ะ"},
             {"role": "assistant", "content": "สวัสดีค่ะ 😊 มีอะไรให้ช่วยไหมคะ?"}]
        r = _decide(self.e, "เอาลิ้งนี้ https://www.bing.com/search?q=abc", h)
        # embedded-in-sentence URL is not a bare URL -> may route elsewhere,
        # but must never auth-gate or fake a handoff
        for a in _AUTH:
            self.assertNotIn(a, r["reply"])
        for f in _FAKE_CS:
            self.assertNotIn(f, r["reply"])


class TestFix02StillGreen(unittest.TestCase):
    def setUp(self):
        self.e = _engine()

    def test_C_platform_home_preserved(self):
        r = _decide(self.e, "https://detail.1688.com",
                    [{"role": "user", "content": "ช่วยแปลงลิงก์"}, {"role": "assistant", "content": _ASK}])
        self.assertEqual(r["lc_state"], "PLATFORM_HOME_OR_NON_PRODUCT")
        self.assertIn("หน้าเว็บหลักของ 1688", r["reply"])

    def test_D_valid_conversion_preserved(self):
        r = _decide(self.e, "https://detail.1688.com/offer/652702302959.html",
                    [{"role": "user", "content": "ช่วยแปลงลิงก์"}, {"role": "assistant", "content": _ASK}],
                    ok_link="https://fasttrade.in.th/PageProductDetailGuest/1688/652702302959/home/guest/index")
        self.assertEqual(r["routing"], "API")
        self.assertEqual(r["lc_state"], "VALID")
        self.assertIn("fasttrade.in.th/PageProductDetailGuest/1688/652702302959", r["reply"])


class TestIntentSeparation(unittest.TestCase):
    def test_G_website_link_request_not_conversion(self):
        self.assertEqual(interpret("ขอลิงก์เว็บ Taobao กับ Tmall หน่อย", []).intent_family,
                         "WEBSITE_LINK_REQUEST")

    def test_H_general_knowledge_not_url_conversion(self):
        self.assertIn(interpret("Google ใช้ทำอะไร", []).intent_family,
                      ("UNKNOWN", "GENERAL", "PRODUCT_POLICY"))

    def test_import_interest_unchanged(self):
        self.assertEqual(interpret("อยากสั่งของจากจีน", []).intent_family, "IMPORT_INTEREST")

    def test_bare_urls_all_route_link_conversion(self):
        for u in ("https://www.google.com/test", "https://example.com/x",
                  "https://detail.1688.com", "https://detail.1688.com/offer/1.html"):
            self.assertEqual(interpret(u, []).intent_family, "LINK_CONVERSION", u)

    def test_negated_link_from_fix01_unchanged(self):
        self.assertFalse(is_link_conversion_signal("อยากซื้อของจาก 1688 แต่ยังไม่มีลิงก์"))


if __name__ == "__main__":
    unittest.main()
