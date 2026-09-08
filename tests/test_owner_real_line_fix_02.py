# -*- coding: utf-8 -*-
"""OWNER-REAL-LINE-FIX-02 — link-shape validation + natural error wording.

Real LINE: "ช่วยแปลงลิงก์นี้ให้หน่อย" -> ask -> "https://detail.1688.com"
answered with a misleading "ไม่สามารถดำเนินการได้ในขณะนี้ รบกวนลองใหม่"
(a temporary-outage phrasing) even though the URL is just the 1688
home / domain entry, not a product link. Core conversion of a real
/offer/<id>.html link works.

This fix classifies an incoming URL BEFORE conversion into
VALID_PRODUCT / PLATFORM_HOME_OR_NON_PRODUCT / INCOMPLETE_PRODUCT_LINK /
UNSUPPORTED_DOMAIN, and only treats a genuine API failure on a
structurally-valid link as an operational failure — with natural,
system-owned wording, never a fake staff hand-off.
"""
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("OPENAI_API_KEY", "sk-invalid-owner02")

from services.link_conversion_flow import (
    classify_link_request, reply_for_state, reply_for_success,
    is_link_conversion_signal,
)


class TestLinkShapeClassification(unittest.TestCase):
    MATRIX = [
        # ( label, url, expected_state )
        ("1. valid detail.1688 product", "https://detail.1688.com/offer/652702302959.html", "VALID"),
        ("2. bare detail.1688.com", "https://detail.1688.com", "PLATFORM_HOME_OR_NON_PRODUCT"),
        ("3. www.1688.com homepage", "https://www.1688.com", "PLATFORM_HOME_OR_NON_PRODUCT"),
        ("4. incomplete /offer/", "https://detail.1688.com/offer/", "INCOMPLETE_PRODUCT_LINK"),
        ("5. invalid product id", "https://detail.1688.com/offer/abc.html", "INCOMPLETE_PRODUCT_LINK"),
        ("6. valid m.1688 product", "https://m.1688.com/offer/351622421.html?spm=a260k", "VALID"),
        ("7. valid qr.1688 short link", "https://qr.1688.com/s/abc123", "VALID"),
        ("8. Taobao homepage", "https://www.taobao.com", "PLATFORM_HOME_OR_NON_PRODUCT"),
        ("9. valid Taobao product", "https://item.taobao.com/item.htm?id=678901234567", "VALID"),
        ("9b. valid Taobao short e.tb.cn", "https://e.tb.cn/h.RyW4UZ9?tk=abc", "VALID"),
        ("10. Tmall homepage", "https://www.tmall.com", "PLATFORM_HOME_OR_NON_PRODUCT"),
        ("11. valid Tmall product", "https://detail.tmall.com/item.htm?id=555555555", "VALID"),
        ("12. unsupported domain", "https://www.amazon.com/dp/B00XYZ", "UNSUPPORTED_DOMAIN"),
        ("13. malformed URL (no scheme)", "1688.com/abc", "MALFORMED_URL"),
        ("13b. taobao item.htm no id", "https://item.taobao.com/item.htm", "INCOMPLETE_PRODUCT_LINK"),
    ]

    def test_matrix(self):
        for label, url, exp in self.MATRIX:
            with self.subTest(label):
                self.assertEqual(classify_link_request(url)["state"], exp)


class TestReplyWording(unittest.TestCase):
    def test_platform_home_is_natural_and_platform_aware(self):
        r1 = reply_for_state("PLATFORM_HOME_OR_NON_PRODUCT", "1688")
        self.assertIn("หน้าเว็บหลักของ 1688", r1)
        self.assertIn("/offer/", r1)
        self.assertNotIn("ขัดข้อง", r1)
        self.assertNotIn("แอดมิน", r1)
        rt = reply_for_state("PLATFORM_HOME_OR_NON_PRODUCT", "taobao")
        self.assertIn("Taobao", rt)
        rm = reply_for_state("PLATFORM_HOME_OR_NON_PRODUCT", "tmall")
        self.assertIn("Tmall", rm)

    def test_incomplete_is_not_a_system_outage(self):
        r = reply_for_state("INCOMPLETE_PRODUCT_LINK")
        self.assertIn("ไม่ครบ", r)
        for bad in ("ขัดข้อง", "ไม่สามารถดำเนินการ", "แอดมิน", "เจ้าหน้าที่"):
            self.assertNotIn(bad, r)

    def test_unsupported_domain_natural_no_auth(self):
        r = reply_for_state("UNSUPPORTED_DOMAIN")
        self.assertIn("1688", r)
        self.assertIn("Taobao", r)
        self.assertIn("Tmall", r)
        for bad in ("รหัสลูกค้า", "ยืนยันตัวตน", "แอดมิน"):
            self.assertNotIn(bad, r)

    def test_valid_link_api_failure_wording(self):
        r = reply_for_state("UPSTREAM_FAILURE")
        self.assertIn("ลิงก์ดูถูกต้องแล้ว", r)
        self.assertIn("ยังแปลงไม่สำเร็จ", r)
        for bad in ("ขัดข้อง", "แอดมิน", "เจ้าหน้าที่"):
            self.assertNotIn(bad, r)

    def test_success_wording_is_system_owned(self):
        s = reply_for_success("https://fasttrade.in.th/PageProductDetailGuest/1688/652702302959/home/guest/index")
        self.assertIn("แปลงลิงก์ให้เรียบร้อย", s)
        self.assertNotIn("แอดมิน", s)
        self.assertNotIn("คุณลูกค้าเปิดบิลเข้ามาได้เลย", s)  # old admin phrasing
        # converted URL appended verbatim, unchanged
        self.assertTrue(s.rstrip().endswith(
            "https://fasttrade.in.th/PageProductDetailGuest/1688/652702302959/home/guest/index"))


class _Engine:
    def setUp(self):
        from tests.test_business_action_registry import reset_real_registry
        reset_real_registry()
        from services.decision_engine import DecisionEngine
        self.engine = DecisionEngine()

    def _say(self, msg, history=None, mock_ok_link=None):
        ctx = {"developer_mode": True}
        if mock_ok_link:
            with patch("services.action_executor.requests.request") as mq:
                mq.return_value.status_code = 200
                payload = {"data": {"Link": mock_ok_link}}
                mq.return_value.json = lambda: payload
                mq.return_value.text = "{}"
                r = self.engine.decide(msg, history=list(history or []), context=ctx)
        else:
            with patch("services.action_executor.requests.request") as mq:
                mq.return_value.status_code = 400
                mq.return_value.json = lambda: {"error": "bad request"}
                mq.return_value.text = '{"error":"bad request"}'
                r = self.engine.decide(msg, history=list(history or []), context=ctx)
        d = r.get("developer") or {}
        return {"routing": r["routing"]["type"], "reply": r["reply"]["text"],
                "lc_state": d.get("link_conversion_state"),
                "lc_result": d.get("link_conversion_result"),
                "sel": d.get("selected_business_action")}


class TestEndToEndThroughDecisionEngine(_Engine, unittest.TestCase):
    _ASK = "ได้ค่ะ ส่งลิงก์สินค้าที่ต้องการแปลงมาได้เลยค่ะ"

    def test_14_invalid_then_valid_same_conversation_converts_immediately(self):
        h = [{"role": "user", "content": "ช่วยแปลงลิงก์นี้ให้หน่อย"},
             {"role": "assistant", "content": self._ASK}]
        r1 = self._say("https://detail.1688.com", history=h)
        self.assertEqual(r1["lc_state"], "PLATFORM_HOME_OR_NON_PRODUCT")
        self.assertIn("หน้าเว็บหลักของ 1688", r1["reply"])
        self.assertNotIn("ไม่สามารถดำเนินการ", r1["reply"])
        h += [{"role": "user", "content": "https://detail.1688.com"},
              {"role": "assistant", "content": r1["reply"]}]
        # the FULL product link now — no need to repeat "ช่วยแปลงลิงก์"
        r2 = self._say("https://detail.1688.com/offer/652702302959.html", history=h,
                       mock_ok_link="https://fasttrade.in.th/PageProductDetailGuest/1688/652702302959/home/guest/index")
        self.assertEqual(r2["routing"], "API")
        self.assertEqual(r2["lc_state"], "VALID")
        self.assertIn("fasttrade.in.th/PageProductDetailGuest/1688/652702302959", r2["reply"])

    def test_2_bare_domain_no_custcode_prompt(self):
        h = [{"role": "user", "content": "ช่วยแปลงลิงก์"},
             {"role": "assistant", "content": self._ASK}]
        r = self._say("https://detail.1688.com", history=h)
        for bad in ("รหัสลูกค้า", "ยืนยันตัวตน", "เบอร์ที่ผูก"):
            self.assertNotIn(bad, r["reply"])

    def test_16_valid_looking_link_api_failure_is_operational_not_generic(self):
        h = [{"role": "user", "content": "ช่วยแปลงลิงก์"},
             {"role": "assistant", "content": self._ASK}]
        r = self._say("https://detail.1688.com/offer/652702302959.html", history=h)  # mock 400
        self.assertNotIn("ไม่สามารถดำเนินการได้ในขณะนี้", r["reply"])
        self.assertIn("ยังแปลงไม่สำเร็จ", r["reply"])
        self.assertNotIn("แอดมิน", r["reply"])

    def test_18_topic_switch_out_of_link_conversion_still_works(self):
        h = [{"role": "user", "content": "ขอแปลงลิงก์"},
             {"role": "assistant", "content": self._ASK}]
        r = self._say("ไม่เอาแล้ว ขอถามค่าตีลังไม้แทน", history=h)
        self.assertNotEqual(r["sel"], "geturlproductdetail")
        self.assertNotIn("ส่งลิงก์สินค้าที่ต้องการแปลง", r["reply"])

    def test_19_negated_link_from_fix01_still_green(self):
        self.assertFalse(is_link_conversion_signal("อยากซื้อของจาก 1688 แต่ยังไม่มีลิงก์"))
        self.assertTrue(is_link_conversion_signal("แปลงลิงก์ 1688 นี้ให้หน่อย"))


if __name__ == "__main__":
    unittest.main()
