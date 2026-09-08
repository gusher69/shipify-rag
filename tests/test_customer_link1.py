# -*- coding: utf-8 -*-
"""CUSTOMER-LINK-1 — Product Link Conversion (1688 / Taobao / Tmall ->
Shipify). Customer source: `tests/customer_uat/customer_uat_master.jsonl`
CUS-P20 (linked CUS-G29, CUS-S20; `kase_tee_tong_kae.pdf` page 17 +
`Ai.xlsx` sheet '2.tongchecknairabop' row 20 / CSW20). Explicit customer
requirement: "Link conversion is NOT an internal-data check — it must
NOT require a customer code or identity verification."

Reuses the real-registry `DecisionEngine()` E2E pattern from
tests.test_system_state_emergency_1 / tests.test_calculator_regression_2
(same house style, same isolation fix) — the live registry fix this task
applies (tools/fix_link_conversion_customer_facing_identity.py) is
exercised for real here, not against a fake.
"""
import unittest
from unittest.mock import MagicMock, patch

from services.decision_engine import DecisionEngine
from services.conversation_semantics import interpret
from services.link_conversion_flow import classify_link_request, classify_platform, GUEST_CUSTCODE
from tests.test_decision_engine import _fake_playground_result
from tests.test_business_action_registry import reset_real_registry

_MISSING_URL_REPLY = "ได้ค่ะ ส่งลิงก์สินค้าที่ต้องการแปลงมาได้เลยค่ะ"
_1688_URL = "https://detail.1688.com/offer/899413226668.html"
_TAOBAO_URL = "https://item.taobao.com/item.htm?id=878033097478"
_TAOBAO_SHORT_URL = "https://e.tb.cn/h.RyW4UZ9?tk=byX8gogXRUb"
_TMALL_URL = "https://detail.tmall.com/item.htm?id=123456"
_CONVERTED_LINK = "https://www.shipify.co.th/PageProductDetail/1688/899413226668"


class _E2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        reset_real_registry()
        cls.eng = DecisionEngine()

    def _say(self, msg, history=None, *, verified=False, exec_response=None, exec_side_effect=None):
        ctx = {"channel": "line", "tenant_id": "default", "external_user_id": "U_link1",
               "developer_mode": True,
               "customer_context": {"cust_code": "FT3182"} if verified else {}}
        kw = {}
        if exec_side_effect is not None:
            kw["side_effect"] = exec_side_effect
        else:
            kw["return_value"] = exec_response or MagicMock(
                status_code=200, json=lambda: {"data": {"Link": _CONVERTED_LINK}})
        with patch("services.action_executor.requests.request", **kw) as mock_req, \
             patch("services.playground_orchestrator.run_playground_turn",
                   return_value=_fake_playground_result(answer="[RAG]", confidence=0.9)):
            r = self.eng.decide(msg, history=list(history or []), context=ctx)
        dev = r.get("developer") or {}
        return {"routing": (r.get("routing") or {}).get("type"),
                "reply": (r.get("reply") or {}).get("text") or "",
                "action": dev.get("selected_business_action"),
                "src": dev.get("selection_source"),
                "erp_called": mock_req.called,
                "call_kwargs": mock_req.call_args.kwargs if mock_req.call_args else {},
                "handoff": (r.get("handoff_payload") or {}).get("reason"),
                # link_conversion_result (post-execution truth typing) is
                # the more specific signal when both are present — the
                # pre-execution gate always sets link_conversion_state to
                # "VALID" first for anything that reaches the Executor.
                "link_state": dev.get("link_conversion_result") or dev.get("link_conversion_state")}


# ── 1. CUS-P20 exact + related G29/S20 ───────────────────────────────
class TestCUSP20AndRelated(_E2E):
    def test_cus_p20_exact_message_asks_for_the_link(self):
        # "ช่วยแปลงลิงก์ให้หน่อยค่ะ" — no URL yet -> Case 1 ask, PUBLIC,
        # no CustCode, no ERP call.
        r = self._say("ช่วยแปลงลิงก์ให้หน่อยค่ะ")
        self.assertEqual(r["routing"], "WORKFLOW")
        self.assertEqual(r["reply"], _MISSING_URL_REPLY)
        self.assertNotIn("รหัสลูกค้า", r["reply"])
        self.assertFalse(r["erp_called"])

    def test_cus_p20_variant_taobao_phrase(self):
        r = self._say("แปลงลิงก์ Taobao ให้หน่อย")
        self.assertEqual(r["routing"], "WORKFLOW")
        self.assertEqual(r["reply"], _MISSING_URL_REPLY)

    def test_cus_p20_variant_bare_url(self):
        r = self._say("https://detail.1688.com/offer/899413226668.html")
        self.assertEqual(r["routing"], "API")
        self.assertIn(_CONVERTED_LINK, r["reply"])
        self.assertNotIn("รหัสลูกค้า", r["reply"])

    def test_cus_g29_message(self):
        r = self._say("ช่วยแปลงลิงก์ให้หน่อยค่ะ")
        self.assertNotIn("รหัสลูกค้า", r["reply"])
        self.assertFalse(r["erp_called"])

    def test_cus_s20_example_conversion_flow(self):
        # CUS-S20's own worked example: ack -> real converted link.
        h = [{"role": "user", "content": "ช่วยแปลงลิงก์ให้หน่อยค่ะ"},
             {"role": "assistant", "content": _MISSING_URL_REPLY}]
        r = self._say(_1688_URL, history=h)
        self.assertEqual(r["routing"], "API")
        # OWNER-REAL-LINE-FIX-02 — system-owned success wording (no "แอดมิน")
        self.assertIn("แปลงลิงก์ให้เรียบร้อย", r["reply"])
        self.assertNotIn("แอดมิน", r["reply"])
        self.assertIn(_CONVERTED_LINK, r["reply"])


# ── 4/5/6. valid URLs per supported platform ─────────────────────────
class TestValidPlatformURLs(_E2E):
    def test_1688_url_converts(self):
        r = self._say(f"ช่วยแปลงลิงก์ 1688 {_1688_URL} ให้หน่อย")
        self.assertEqual(r["routing"], "API")
        self.assertEqual(r["action"], "geturlproductdetail")
        self.assertIn(_CONVERTED_LINK, r["reply"])
        self.assertTrue(r["erp_called"])
        self.assertEqual(r["call_kwargs"]["data"]["URL"], _1688_URL)
        # PHASE-LINK-1688 — FastTrade requires a CustCode even for a public
        # link, so an anonymous request sends the Shipify GUEST account
        # code (never asks the user, never verifies).
        self.assertEqual(r["call_kwargs"]["data"].get("CustCode"), GUEST_CUSTCODE)

    def test_taobao_full_url_converts(self):
        r = self._say(f"แปลงลิงก์ Taobao ให้หน่อย {_TAOBAO_URL}")
        self.assertEqual(r["routing"], "API")
        self.assertTrue(r["erp_called"])

    def test_taobao_share_short_link_converts(self):
        r = self._say(f"ช่วยแปลงลิงก์นี้ให้หน่อย {_TAOBAO_SHORT_URL}")
        self.assertEqual(r["routing"], "API")
        self.assertTrue(r["erp_called"])
        self.assertEqual(r["call_kwargs"]["data"]["URL"], _TAOBAO_SHORT_URL)

    def test_tmall_url_converts(self):
        # source-honest note: Tmall is named (not URL-evidenced) in the
        # action's own pre-existing ai_description/search_keywords —
        # verified here against its standard root domain.
        r = self._say(f"ช่วยแปลงลิงก์ของ Tmall ให้ทีครับ {_TMALL_URL}")
        self.assertEqual(r["routing"], "API")
        self.assertTrue(r["erp_called"])


# ── 2. missing URL ────────────────────────────────────────────────────
class TestMissingURL(_E2E):
    def test_missing_url_asks_only_for_the_link(self):
        r = self._say("ช่วยแปลงลิงก์ 1688 ให้หน่อย")
        self.assertEqual(r["reply"], _MISSING_URL_REPLY)
        self.assertNotIn("รหัสลูกค้า", r["reply"])
        self.assertFalse(r["erp_called"])
        self.assertIsNone(r["handoff"])


# ── 10. bare URL follow-up (Case 3) ──────────────────────────────────
class TestBareURLFollowUp(_E2E):
    def test_bare_url_after_missing_url_ask_converts(self):
        h = [{"role": "user", "content": "ช่วยแปลงลิงก์ 1688 ให้หน่อย"},
             {"role": "assistant", "content": _MISSING_URL_REPLY}]
        r = self._say(_1688_URL, history=h)
        self.assertEqual(r["routing"], "API")
        self.assertIn(_CONVERTED_LINK, r["reply"])
        self.assertNotIn("รหัสลูกค้า", r["reply"])
        self.assertNotEqual(r["src"], "shipping_estimate_flow")


# ── 8. malformed URL ──────────────────────────────────────────────────
class TestMalformedURL(_E2E):
    def test_malformed_url_gets_a_distinct_honest_reply(self):
        r = self._say("ช่วยแปลงลิงก์ 1688.com/abc ให้หน่อย")
        self.assertEqual(r["routing"], "WORKFLOW")
        self.assertNotEqual(r["reply"], _MISSING_URL_REPLY)
        # OWNER-REAL-LINE-FIX-02 — honest, actionable, not a system outage
        self.assertIn("ไม่ครบ", r["reply"])
        self.assertNotIn("ขัดข้อง", r["reply"])
        self.assertNotIn("แอดมิน", r["reply"])
        self.assertFalse(r["erp_called"])
        self.assertIsNone(r["handoff"])


# ── 9. unsupported domain ────────────────────────────────────────────
class TestUnsupportedDomain(_E2E):
    def test_unsupported_domain_names_the_supported_platforms(self):
        r = self._say("ช่วยแปลงลิงก์นี้ให้หน่อย https://www.amazon.com/dp/B08N5WRWNW")
        self.assertEqual(r["routing"], "WORKFLOW")
        self.assertIn("1688", r["reply"])
        self.assertIn("Taobao", r["reply"])
        self.assertIn("Tmall", r["reply"])
        self.assertFalse(r["erp_called"])
        self.assertIsNone(r["handoff"])


# ── 13. text mentions platform, no conversion request ────────────────
class TestPlatformMentionWithoutConversionRequest(_E2E):
    def test_incidental_platform_mention_does_not_enter_link_flow(self):
        r = self._say("ผมซื้อของใน 1688")
        self.assertNotEqual(r["reply"], _MISSING_URL_REPLY)
        self.assertFalse(r["erp_called"])
        self.assertNotEqual(r["action"], "geturlproductdetail")


# ── 14. multiple URLs ─────────────────────────────────────────────────
class TestMultipleURLs(_E2E):
    def test_two_urls_asks_for_one_at_a_time(self):
        r = self._say(f"ช่วยแปลง {_1688_URL} และ {_TAOBAO_URL} ให้หน่อย")
        self.assertEqual(r["routing"], "WORKFLOW")
        self.assertIn("ทีละ", r["reply"])
        self.assertFalse(r["erp_called"])


# ── 15/16/17. conversion result truth ────────────────────────────────
class TestConversionResultTruth(_E2E):
    def test_upstream_http_failure_is_never_fake_success(self):
        # A raised exception inside the executor is caught by the
        # EXISTING, pre-existing generic Action Executor error path
        # (services/decision_engine.py's `status == "error"` branch,
        # reached BEFORE this action's own scoped truth-typing block) —
        # already honest and non-fake; classify_conversion_result is
        # never reached for THIS specific shape of failure, only for a
        # response that came back (200 or otherwise) with no usable Link.
        r = self._say(f"แปลงลิงก์ {_1688_URL} ให้หน่อย", exec_side_effect=Exception("connection reset"))
        # never phrased as a completed conversion (OWNER-REAL-LINE-FIX-02
        # reply is "ลิงก์ดูถูกต้องแล้วค่ะ แต่ตอนนี้ยังแปลงไม่สำเร็จ …" — an
        # explicit not-done statement, no success prefix, no fake staff)
        self.assertNotIn("เรียบร้อย", r["reply"])
        self.assertNotIn("แปลงลิงก์ให้เรียบร้อย", r["reply"])
        self.assertIn("ยังแปลงไม่สำเร็จ", r["reply"])
        self.assertNotIn("แอดมิน", r["reply"])
        self.assertEqual(r["link_state"], "VALID")   # pre-execution gate passed; executor itself failed

    def test_conversion_not_found_is_never_fake_success(self):
        r = self._say(f"แปลงลิงก์ {_1688_URL} ให้หน่อย",
                       exec_response=MagicMock(status_code=200, json=lambda: {"data": {}}))
        self.assertNotIn("เรียบร้อยค่ะ", r["reply"])
        self.assertEqual(r["link_state"], "CONVERSION_NOT_FOUND_OR_REJECTED")

    def test_real_success_returns_the_actual_converted_link(self):
        r = self._say(f"แปลงลิงก์ {_1688_URL} ให้หน่อย")
        self.assertEqual(r["link_state"], "CONVERSION_SUCCESS")
        self.assertIn(_CONVERTED_LINK, r["reply"])


# ── identity: no manual CustCode collection; verified customer's own
# code is supplied internally, never a stranger's ────────────────────
class TestIdentityBehavior(_E2E):
    def test_anonymous_customer_never_asked_for_custcode(self):
        r = self._say(f"แปลงลิงก์ {_1688_URL} ให้หน่อย", verified=False)
        self.assertTrue(r["erp_called"])
        # the customer is NEVER asked for a code and NEVER verified;
        # internally the public GUEST account code is supplied so the
        # FastTrade endpoint (which requires one) does not 400.
        self.assertEqual(r["call_kwargs"]["data"].get("CustCode"), GUEST_CUSTCODE)
        self.assertNotIn("รหัสลูกค้า", r["reply"])
        self.assertNotIn("ยืนยันตัวตน", r["reply"])

    def test_verified_customer_own_custcode_supplied_internally(self):
        r = self._say(f"แปลงลิงก์ {_1688_URL} ให้หน่อย", verified=True)
        self.assertEqual(r["call_kwargs"]["data"].get("CustCode"), "FT3182")
        self.assertNotIn("รหัสลูกค้า", r["reply"])


# ── 11/12. semantic recognition — required examples + genuinely unseen
class TestSemanticRecognition(unittest.TestCase):
    def test_required_natural_wordings(self):
        for msg in ("ช่วยแปลงลิงก์นี้ให้หน่อย",
                    "เอาลิงก์ 1688 นี้เข้า Shipify ให้หน่อย",
                    "ลิงก์ Taobao นี้ใช้กับ Shipify ยังไง",
                    "ช่วยแปลงลิงก์ของ Tmall ให้ทีครับ"):
            self.assertEqual(interpret(msg).intent_family, "LINK_CONVERSION", msg)

    def test_genuinely_unseen_paraphrase(self):
        r = interpret("รบกวนช่วยเปลี่ยนลิงก์สินค้าจากจีนให้เป็นลิงก์ของ Shipify หน่อยค่ะ")
        self.assertEqual(r.intent_family, "LINK_CONVERSION")

    def test_incidental_platform_mention_is_not_link_conversion(self):
        # this message alone is genuinely ambiguous without the LLM gate;
        # asserting only the one hard requirement: no false-positive
        # verb/URL match forces it to LINK_CONVERSION deterministically.
        from services.link_conversion_flow import is_link_conversion_signal
        self.assertFalse(is_link_conversion_signal("ผมซื้อของใน 1688"))


# ── deterministic URL / domain classifier unit tests ─────────────────
class TestDeterministicClassifier(unittest.TestCase):
    def test_supported_platforms(self):
        self.assertEqual(classify_platform(_1688_URL), "1688")
        self.assertEqual(classify_platform(_TAOBAO_URL), "taobao")
        self.assertEqual(classify_platform(_TAOBAO_SHORT_URL), "taobao")
        self.assertEqual(classify_platform(_TMALL_URL), "tmall")

    def test_unsupported_domain(self):
        self.assertIsNone(classify_platform("https://www.amazon.com/dp/B08N5WRWNW"))

    def test_missing_vs_malformed_vs_multiple(self):
        self.assertEqual(classify_link_request("ช่วยแปลงลิงก์ให้หน่อย")["state"], "MISSING_URL")
        self.assertEqual(classify_link_request("1688.com/abc")["state"], "MALFORMED_URL")
        self.assertEqual(classify_link_request(f"{_1688_URL} {_TAOBAO_URL}")["state"], "MULTIPLE_URLS")
        self.assertEqual(classify_link_request(_1688_URL)["state"], "VALID")


# ── STATE / ROUTE AUTHORITY — LINK_CONVERSION vs every other pending
# flow, both directions, accumulated-session ─────────────────────────
class TestStateAuthority(_E2E):
    _TC19_OPEN = [
        {"role": "user", "content": "มีบริการเหมารถไหมคะ"},
        {"role": "assistant", "content": "สวัสดีค่ะ ทางเรามีบริการเหมารถให้ได้นะคะ "
         "คุณลูกค้าแจ้งเลขบิล และโลเคชั่นปลายทาง พร้อมกับชื่อผู้รับ และเบอร์โทรผู้รับมาได้เลยนะคะ"}]
    _CALC_PENDING = [
        {"role": "user", "content": "ช่วยคำนวณค่าส่ง น้ำหนัก 2 โล ขนาด 54x12x43"},
        {"role": "assistant", "content": "รับทราบค่ะ (น้ำหนัก 2 กก. • ขนาด 54x12x43 ซม.) "
         "ต้องการประเมินทางรถหรือทางเรือคะ"}]
    _SHIPMENT_PENDING = [{"role": "user", "content": "ของผมถึงไหนแล้ว"},
                         {"role": "assistant", "content": "กรุณาแจ้งเลขที่บิลขนส่งค่ะ"}]
    _LINK_PENDING = [{"role": "user", "content": "ช่วยแปลงลิงก์ 1688 ให้หน่อย"},
                     {"role": "assistant", "content": _MISSING_URL_REPLY}]

    def test_tc19_pending_then_link_conversion_wins(self):
        r = self._say(f"ช่วยแปลงลิงก์ 1688 {_1688_URL} ให้หน่อย", history=self._TC19_OPEN)
        self.assertEqual(r["routing"], "API")
        self.assertIn(_CONVERTED_LINK, r["reply"])

    def test_calculator_pending_then_link_conversion_wins(self):
        r = self._say(f"ช่วยแปลงลิงก์ 1688 {_1688_URL} ให้หน่อย", history=self._CALC_PENDING)
        self.assertEqual(r["routing"], "API")
        self.assertIn(_CONVERTED_LINK, r["reply"])

    def test_shipment_pending_then_link_conversion_wins(self):
        r = self._say(f"ช่วยแปลงลิงก์ 1688 {_1688_URL} ให้หน่อย", history=self._SHIPMENT_PENDING)
        self.assertEqual(r["routing"], "API")
        self.assertIn(_CONVERTED_LINK, r["reply"])

    def test_link_conversion_pending_then_coupon_usage_wins(self):
        r = self._say("คูปองใช้ยังไงครับ", history=self._LINK_PENDING)
        self.assertEqual(r["routing"], "RAG")
        self.assertNotEqual(r["reply"], _MISSING_URL_REPLY)

    def test_link_conversion_pending_then_transit_time_wins(self):
        r = self._say("ทางเรือกี่วันครับ", history=self._LINK_PENDING)
        self.assertEqual(r["routing"], "RAG")
        self.assertNotEqual(r["reply"], _MISSING_URL_REPLY)


if __name__ == "__main__":
    unittest.main()
