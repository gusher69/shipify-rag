# -*- coding: utf-8 -*-
"""FIX-LINK-CONVERSION-1688-REAL-URL — focused coverage for the
production bug: REAL LINE Link Conversion recognised the intent, asked
for the URL, then returned "ไม่สามารถดำเนินการได้ในขณะนี้" for a real 1688
URL.

Root cause: FastTrade's GetUrlProductDetail endpoint returns HTTP 400
("กรุณาระบุข้อมูลให้ครบถ้วน") for a request with NO CustCode — and Link
Conversion is public, so most callers have none. Secondary gap: FastTrade
does not follow 1688 QR/short redirects (qr.1688.com/...), it 400s them.

Fix: send the Shipify GUEST account code when the caller has no
verified/profile CustCode (never asked, never verified); resolve 1688
QR/short URLs to their real product URL first (SSRF-guarded); canonicalize
m.1688.com/offer/<id>.html.
"""
import unittest
from unittest.mock import MagicMock, patch

from services.decision_engine import DecisionEngine
from services.link_conversion_flow import (
    GUEST_CUSTCODE, is_1688_short_url, normalize_supported_url,
    resolve_1688_short_url, classify_platform,
)
from tests.test_decision_engine import _fake_playground_result
from tests.test_business_action_registry import reset_real_registry

_ASK_URL = "ได้ค่ะ ส่งลิงก์สินค้าที่ต้องการแปลงมาได้เลยค่ะ"
_M1688 = "https://m.1688.com/offer/351622421.html?spm=a260k.home2025&scm=1&pos=3"
_DETAIL1688 = "https://detail.1688.com/offer/899413226668.html"
_QR1688 = "https://qr.1688.com/s/abc123"
_TAOBAO = "https://item.taobao.com/item.htm?id=878033097478"
_TMALL = "https://detail.tmall.com/item.htm?id=123456"
_UPSTREAM_LINK = "https://fasttrade.in.th/PageProductDetailGuest/1688/351622421/home/guest/index"


class _E2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        reset_real_registry()
        cls.eng = DecisionEngine()

    def _say(self, msg, *, history=None, verified=False, redirect_to=None,
             upstream_status=200, upstream_link=_UPSTREAM_LINK):
        cc = {"cust_code": "FT3182"} if verified else {}
        ctx = {"channel": "line", "tenant_id": "default",
               "external_user_id": "Ulinkfix000000000000000000000001",
               "developer_mode": True, "customer_context": cc}

        def _fake_http(method, url, **kw):
            body = kw.get("data") or {}
            return MagicMock(status_code=upstream_status,
                             json=lambda: ({"data": {"Link": upstream_link}} if upstream_status == 200
                                           else {"status": "error", "message": "กรุณาระบุข้อมูลให้ครบถ้วน"}),
                             text="{}")

        redirect_calls = []

        def _fake_get(url, **kw):
            redirect_calls.append(url)
            if redirect_to and url == _QR1688:
                return MagicMock(status_code=302, headers={"Location": redirect_to}, close=lambda: None)
            return MagicMock(status_code=200, headers={}, close=lambda: None)

        with patch("services.action_executor.requests.request", side_effect=_fake_http) as mreq, \
             patch("services.link_conversion_flow.requests.get", side_effect=_fake_get), \
             patch("services.playground_orchestrator.run_playground_turn",
                   return_value=_fake_playground_result(answer="[RAG]", confidence=0.9)):
            r = self.eng.decide(msg, history=history or [], context=ctx)
        d = r.get("developer") or {}
        return {
            "routing": (r.get("routing") or {}).get("type"),
            "action": d.get("selected_business_action"),
            "lc_state": d.get("link_conversion_state"),
            "reply": (r.get("reply") or {}).get("text") or "",
            "erp_called": bool(mreq.call_args),
            "call_data": (mreq.call_args.kwargs.get("data") if mreq.call_args else {}) or {},
            "redirect_calls": redirect_calls,
            "resolved": d.get("link_conversion_short_url_resolved"),
            "normalized": d.get("link_conversion_url_normalized"),
        }




# ── 1. m.1688 mobile product URL ────────────────────────────────────────
class TestMobile1688(_E2E):
    def test_m1688_direct_converts_and_is_canonicalized(self):
        r = self._say(_M1688)
        self.assertEqual(r["routing"], "API")
        self.assertEqual(r["action"], "geturlproductdetail")
        self.assertTrue(r["erp_called"])
        self.assertEqual(r["call_data"].get("URL"), "https://detail.1688.com/offer/351622421.html")
        self.assertEqual(r["call_data"].get("CustCode"), GUEST_CUSTCODE)
        self.assertNotIn("ไม่สามารถดำเนินการได้", r["reply"])
        self.assertIn(_UPSTREAM_LINK, r["reply"])


# ── 2. detail.1688 product URL (regression) ─────────────────────────────
class TestDetail1688(_E2E):
    def test_detail1688_still_converts(self):
        r = self._say(_DETAIL1688)
        self.assertEqual(r["routing"], "API")
        self.assertEqual(r["call_data"].get("URL"), _DETAIL1688)
        self.assertEqual(r["call_data"].get("CustCode"), GUEST_CUSTCODE)
        self.assertIn(_UPSTREAM_LINK, r["reply"])


# ── 3. qr.1688 redirect URL ────────────────────────────────────────────
class TestQrShort1688(_E2E):
    def test_qr_resolves_then_converts(self):
        r = self._say(_QR1688, redirect_to="https://detail.1688.com/offer/351622421.html")
        self.assertIn(_QR1688, r["redirect_calls"])
        self.assertEqual(r["call_data"].get("URL"), "https://detail.1688.com/offer/351622421.html")
        self.assertEqual(r["call_data"].get("CustCode"), GUEST_CUSTCODE)
        self.assertIn(_UPSTREAM_LINK, r["reply"])
        self.assertIsNotNone(r["resolved"])

    def test_qr_that_resolves_to_nothing_useful_gives_friendly_reply(self):
        r = self._say(_QR1688, redirect_to=None)   # 200, no Location -> unresolved
        self.assertEqual(r["lc_state"], "SHORT_URL_UNRESOLVED")
        self.assertIn("ยังไม่พบรหัสสินค้า", r["reply"])
        self.assertNotIn("ไม่สามารถดำเนินการได้ในขณะนี้", r["reply"])
        self.assertFalse(r["erp_called"])


# ── 4. URL with a long query string ───────────────────────────────────
class TestLongQuery(_E2E):
    def test_long_query_string_is_handled(self):
        u = _DETAIL1688 + "?" + "&".join(f"k{i}=v{i}" for i in range(40)) + "&spm=a260k"
        r = self._say(u)
        self.assertEqual(r["routing"], "API")
        self.assertTrue(r["call_data"].get("URL", "").startswith("https://detail.1688.com/offer/899413226668.html"))
        self.assertEqual(r["call_data"].get("CustCode"), GUEST_CUSTCODE)


# ── 5/6. multi-turn + direct ──────────────────────────────────────────
class TestConversationState(_E2E):
    def test_url_after_ask_is_consumed_by_pending_flow(self):
        h = [{"role": "user", "content": "แปลงลิงก์ได้ไหม"},
             {"role": "assistant", "content": _ASK_URL}]
        r = self._say(_M1688, history=h)
        self.assertEqual(r["routing"], "API")
        self.assertEqual(r["action"], "geturlproductdetail")
        self.assertIn(_UPSTREAM_LINK, r["reply"])

    def test_direct_url_no_prior_context_converts(self):
        r = self._say(_DETAIL1688, history=[])
        self.assertEqual(r["routing"], "API")
        self.assertIn(_UPSTREAM_LINK, r["reply"])

    def test_ask_first_then_url_never_asks_for_custcode_or_verifies(self):
        h = [{"role": "user", "content": "แปลงลิงก์ได้ไหม"},
             {"role": "assistant", "content": _ASK_URL}]
        r = self._say(_DETAIL1688, history=h)
        self.assertNotIn("รหัสลูกค้า", r["reply"])
        self.assertNotIn("ยืนยันตัวตน", r["reply"])
        self.assertNotIn("เบอร์โทร", r["reply"])


# ── 7. invalid 1688 URL ──────────────────────────────────────────────
class TestInvalid1688(_E2E):
    def test_unsupported_domain_url_gets_supported_platforms_reply(self):
        r = self._say("แปลงลิงก์ https://www.aliexpress.com/item/1005001.html ให้หน่อย")
        self.assertEqual(r["lc_state"], "UNSUPPORTED_DOMAIN")
        self.assertIn("1688", r["reply"])
        self.assertFalse(r["erp_called"])


# ── 8. malicious / non-allowlisted redirect blocked ─────────────────
class TestRedirectSSRFGuards(unittest.TestCase):
    def test_redirect_to_non_1688_host_is_refused(self):
        with patch("services.link_conversion_flow.requests.get") as mget:
            mget.return_value = MagicMock(status_code=302,
                                          headers={"Location": "https://169.254.169.254/latest/meta-data/"},
                                          close=lambda: None)
            self.assertIsNone(resolve_1688_short_url(_QR1688))

    def test_http_downgrade_redirect_is_refused(self):
        with patch("services.link_conversion_flow.requests.get") as mget:
            mget.return_value = MagicMock(status_code=302,
                                          headers={"Location": "http://qr.1688.com/x"},
                                          close=lambda: None)
            self.assertIsNone(resolve_1688_short_url(_QR1688))

    def test_non_https_input_is_refused_without_any_fetch(self):
        with patch("services.link_conversion_flow.requests.get") as mget:
            self.assertIsNone(resolve_1688_short_url("http://qr.1688.com/s/abc"))
            mget.assert_not_called()

    def test_non_1688_input_host_is_not_a_short_url(self):
        self.assertFalse(is_1688_short_url("https://qr.evil.com/s/abc"))
        self.assertFalse(is_1688_short_url("https://qr.1688.com.evil.com/s/abc"))

    def test_redirect_loop_is_bounded(self):
        with patch("services.link_conversion_flow.requests.get") as mget:
            mget.return_value = MagicMock(status_code=302,
                                          headers={"Location": "https://qr.1688.com/s/loop"},
                                          close=lambda: None)
            # never raises / hangs; returns None after the hop budget
            self.assertIsNone(resolve_1688_short_url(_QR1688, max_hops=3))
            self.assertLessEqual(mget.call_count, 4)


# ── 9/10. Taobao + Tmall regression ─────────────────────────────────
class TestTaobaoTmallRegression(_E2E):
    def test_taobao_still_converts(self):
        r = self._say(_TAOBAO)
        self.assertEqual(r["routing"], "API")
        self.assertEqual(r["call_data"].get("URL"), _TAOBAO)
        self.assertEqual(r["call_data"].get("CustCode"), GUEST_CUSTCODE)
        self.assertIn(_UPSTREAM_LINK, r["reply"])

    def test_tmall_still_converts(self):
        r = self._say(_TMALL)
        self.assertEqual(r["routing"], "API")
        self.assertEqual(r["call_data"].get("URL"), _TMALL)
        self.assertIn(_UPSTREAM_LINK, r["reply"])

    def test_taobao_url_is_not_treated_as_a_1688_short_url(self):
        self.assertFalse(is_1688_short_url(_TAOBAO))
        self.assertEqual(normalize_supported_url(_TAOBAO), _TAOBAO)


# ── 11. public flow never starts verification ──────────────────────
class TestPublicNoVerification(_E2E):
    def test_never_asks_custcode_phone_email_or_verification(self):
        for msg in (_M1688, _DETAIL1688, _TAOBAO, _TMALL):
            r = self._say(msg, verified=False)
            for bad in ("รหัสลูกค้า", "ยืนยันตัวตน", "เบอร์โทรที่ผูก", "อีเมลที่ผูก", "เลขสมาชิก"):
                self.assertNotIn(bad, r["reply"], f"{msg} -> {bad}")
            self.assertNotIn("verification", (r["lc_state"] or "").lower())

    def test_verified_customer_own_custcode_used_not_guest(self):
        r = self._say(_DETAIL1688, verified=True)
        self.assertEqual(r["call_data"].get("CustCode"), "FT3182")


# ── 12. stale history does not steal link intent ───────────────────
class TestStaleHistory(_E2E):
    def test_prior_shipment_lookup_does_not_hijack_a_fresh_link_ask(self):
        h = [{"role": "user", "content": "เช็คสถานะบิล FT318220260726001"},
             {"role": "assistant", "content": "สถานะบิลขนส่ง: รับเข้าที่จีนค่ะ"}]
        r = self._say("แปลงลิงก์ได้ไหม", history=h)
        self.assertEqual(r["reply"], _ASK_URL)          # asks for the URL, does not run a shipment lookup
        self.assertFalse(r["erp_called"])

    def test_old_converted_link_in_history_does_not_leak_into_a_new_urlless_ask(self):
        h = [{"role": "user", "content": _DETAIL1688},
             {"role": "assistant", "content": "แอดมินแปลงลิงก์ให้เรียบร้อยค่ะ\n" + _UPSTREAM_LINK}]
        r = self._say("แปลงลิงก์ให้อีกอันค่ะ", history=h)
        self.assertEqual(r["reply"], _ASK_URL)
        self.assertNotIn("899413226668", r["reply"])
        self.assertFalse(r["erp_called"])


if __name__ == "__main__":
    unittest.main()
