# -*- coding: utf-8 -*-
"""CUSTOMER-LINK-REAL-2 — REAL LINE fresh-URL / stale-history regression.

Confirmed REAL LINE failure, session `6c9b9026-434e-403d-b513-e2c90752754b`
(traced via `ai_session_messages`), turns 824-833 (2026-09-05 ~07:24-07:37
ICT):

  t824 user  "ช่วยแปลงลิงก์ให้หน่อย"                  -> asks for the link (correct)
  t825 asst  "ได้ค่ะ ส่งลิงก์สินค้าที่ต้องการแปลงมาได้เลยค่ะ"
  t826 user  "https://detail.1688.com/offer/696614936668.html"  -> converts (correct)
  t827 asst  "แอดมิน...เรียบร้อยค่ะ...696614936668..."
  t828 user  "แปลงลิงก์ให้ทีค่ะ"   <- FRESH request, NO URL
  t829 asst  "แอดมิน...เรียบร้อยค่ะ...696614936668..."   <- BUG: reused the
             CLOSED episode's stale URL and fabricated a second "success"
             for a request that named no product at all.
  t830 user  "https://detail.1688.com/offer/696614936668.html"  (resent explicitly -> fine)
  t831 asst  "แอดมิน...เรียบร้อยค่ะ...696614936668..."
  t832 user  "คูปองใช้ยังไงครับ"
  t833 asst  (correct Coupon Usage answer)

RESPONSE-ORDER AUDIT (t826-t833, all real `created_at` timestamps,
strictly increasing, one reply per turn in submission order): NO
CORRELATION BUG — this is ordinary sequential turn processing. Nothing
here needed a concurrency/event-correlation fix.

ROOT CAUSE: `services.link_conversion_flow.classify_link_request` used
to fall back to the most recent URL ANYWHERE in `history` whenever the
CURRENT message carried none (mirroring `_extract_system_values`'s
carry-forward, written for a since-removed CustCode-collection step —
see CUSTOMER-LINK-1). Since CustCode is never collected after the URL
(a completed conversion is a single turn), there is no longer any
legitimate reason for this pre-execution classifier to look at
`history` at all — a genuine "immediate requested-slot reply" already
contains its own URL in the CURRENT message. Fixed by making
`classify_link_request` a CURRENT-TURN-ONLY function.

Reuses the same real-registry E2E harness as tests.test_customer_link1.
"""
import unittest
from unittest.mock import MagicMock, patch

from services.decision_engine import DecisionEngine
from tests.test_decision_engine import _fake_playground_result
from tests.test_business_action_registry import reset_real_registry

_ASK_URL = "ได้ค่ะ ส่งลิงก์สินค้าที่ต้องการแปลงมาได้เลยค่ะ"
_OLD_URL = "https://detail.1688.com/offer/696614936668.html"
_OLD_CONVERTED = "https://fasttrade.in.th/PageProductDetailGuest/1688/696614936668/home/guest/index"
_NEW_URL = "https://detail.1688.com/offer/111111111111.html"
_NEW_CONVERTED = "https://fasttrade.in.th/PageProductDetailGuest/1688/111111111111/home/guest/index"


class _E2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        reset_real_registry()
        cls.eng = DecisionEngine()

    @staticmethod
    def _fake_upstream(*args, **kwargs):
        # Responds based on the ACTUAL outgoing URL parameter — never
        # guesses from the test's own msg/history — so a test failure
        # here always reflects the real code's provenance choice, not a
        # fixture assumption.
        sent_url = (kwargs.get("data") or {}).get("URL", "")
        converted = _OLD_CONVERTED if _OLD_URL in sent_url else _NEW_CONVERTED
        return MagicMock(status_code=200, json=lambda: {"data": {"Link": converted}})

    def _say(self, msg, history=None):
        ctx = {"channel": "line", "tenant_id": "default", "external_user_id": "U_link_real2",
               "developer_mode": True, "customer_context": {}}
        with patch("services.action_executor.requests.request",
                   side_effect=self._fake_upstream) as mock_req, \
             patch("services.playground_orchestrator.run_playground_turn",
                   return_value=_fake_playground_result(answer="[RAG]", confidence=0.9)):
            r = self.eng.decide(msg, history=list(history or []), context=ctx)
        dev = r.get("developer") or {}
        return {"routing": (r.get("routing") or {}).get("type"),
                "reply": (r.get("reply") or {}).get("text") or "",
                "erp_called": mock_req.called,
                "link_state": dev.get("link_conversion_result") or dev.get("link_conversion_state")}


# LINK-REAL-02 (the exact reproduced real bug) + LINK-REAL-01 (its simpler form)
class TestFreshRequestNeverReusesHistory(_E2E):
    def test_link_real_01_fresh_request_no_url_asks(self):
        r = self._say("แปลงลิงก์ให้ทีค่ะ")
        self.assertEqual(r["reply"], _ASK_URL)
        self.assertFalse(r["erp_called"])

    def test_link_real_02_exact_real_bug_reproduction(self):
        """Session 6c9b9026-434e-403d-b513-e2c90752754b turns 824-829."""
        h = [{"role": "user", "content": "ช่วยแปลงลิงก์ให้หน่อย"},
             {"role": "assistant", "content": _ASK_URL},
             {"role": "user", "content": _OLD_URL},
             {"role": "assistant", "content": f"แอดมินแปลงลิงก์ให้เรียบร้อยค่ะ คุณลูกค้าเปิดบิลเข้ามาได้เลยนะคะ\n{_OLD_CONVERTED}"}]
        r = self._say("แปลงลิงก์ให้ทีค่ะ", history=h)
        self.assertEqual(r["reply"], _ASK_URL, "must ask for a NEW link, not reuse the closed episode's URL")
        self.assertFalse(r["erp_called"])
        self.assertNotIn(_OLD_CONVERTED, r["reply"])
        self.assertNotIn("696614936668", r["reply"])
        self.assertNotIn("รหัสลูกค้า", r["reply"])
        self.assertEqual(r["link_state"], "MISSING_URL")


# LINK-REAL-03 — ASK_URL -> bare URL -> converts with exactly that URL
class TestBareUrlContinuation(_E2E):
    def test_link_real_03(self):
        h = [{"role": "user", "content": "ช่วยแปลงลิงก์ให้หน่อย"},
             {"role": "assistant", "content": _ASK_URL}]
        r = self._say(_NEW_URL, history=h)
        self.assertEqual(r["routing"], "API")
        self.assertTrue(r["erp_called"])
        self.assertIn(_NEW_CONVERTED, r["reply"])
        self.assertNotIn("รหัสลูกค้า", r["reply"])


# LINK-REAL-04 — conversion completes -> new request without URL -> ASK_NEW_URL
class TestNewRequestAfterCompletion(_E2E):
    def test_link_real_04(self):
        h = [{"role": "user", "content": _OLD_URL},
             {"role": "assistant", "content": f"แอดมินแปลงลิงก์ให้เรียบร้อยค่ะ คุณลูกค้าเปิดบิลเข้ามาได้เลยนะคะ\n{_OLD_CONVERTED}"}]
        r = self._say("ช่วยแปลงลิงก์ให้อีกอันค่ะ", history=h)
        self.assertEqual(r["reply"], _ASK_URL)
        self.assertFalse(r["erp_called"])
        self.assertNotIn("696614936668", r["reply"])


# LINK-REAL-05 / 06 — waiting for URL, topic switches away
class TestTopicSwitchWhileWaiting(_E2E):
    _WAITING = [{"role": "user", "content": "ช่วยแปลงลิงก์ให้หน่อย"},
                {"role": "assistant", "content": _ASK_URL}]

    def test_link_real_05_coupon_wins(self):
        r = self._say("คูปองใช้ยังไงครับ", history=self._WAITING)
        self.assertEqual(r["routing"], "RAG")
        self.assertFalse(r["erp_called"])
        self.assertNotEqual(r["reply"], _ASK_URL)

    def test_link_real_06_transit_wins(self):
        r = self._say("ทางเรือกี่วันครับ", history=self._WAITING)
        self.assertEqual(r["routing"], "RAG")
        self.assertFalse(r["erp_called"])
        self.assertNotEqual(r["reply"], _ASK_URL)


# LINK-REAL-07 — completed conversion -> Coupon Usage, no duplicate conversion
class TestTopicSwitchAfterConversion(_E2E):
    def test_link_real_07(self):
        h = [{"role": "user", "content": _OLD_URL},
             {"role": "assistant", "content": f"แอดมินแปลงลิงก์ให้เรียบร้อยค่ะ คุณลูกค้าเปิดบิลเข้ามาได้เลยนะคะ\n{_OLD_CONVERTED}"}]
        r = self._say("คูปองใช้ยังไงครับ", history=h)
        self.assertEqual(r["routing"], "RAG")
        self.assertNotIn("696614936668", r["reply"])
        self.assertNotIn("แปลงลิงก์", r["reply"])


# LINK-REAL-08 — explicit resend of the SAME URL is processed normally
class TestExplicitSameUrlResend(_E2E):
    def test_link_real_08(self):
        h = [{"role": "user", "content": "ช่วยแปลงลิงก์ให้หน่อย"},
             {"role": "assistant", "content": _ASK_URL},
             {"role": "user", "content": _OLD_URL},
             {"role": "assistant", "content": f"แอดมินแปลงลิงก์ให้เรียบร้อยค่ะ คุณลูกค้าเปิดบิลเข้ามาได้เลยนะคะ\n{_OLD_CONVERTED}"}]
        r = self._say(_OLD_URL, history=h)   # customer explicitly resends the SAME link
        self.assertEqual(r["routing"], "API")
        self.assertTrue(r["erp_called"])
        self.assertIn(_OLD_CONVERTED, r["reply"])


# LINK-REAL-09 — current message has a NEW url; history has an old one -> current wins
class TestCurrentTurnUrlOverridesHistory(_E2E):
    def test_link_real_09(self):
        h = [{"role": "user", "content": _OLD_URL},
             {"role": "assistant", "content": f"แอดมินแปลงลิงก์ให้เรียบร้อยค่ะ คุณลูกค้าเปิดบิลเข้ามาได้เลยนะคะ\n{_OLD_CONVERTED}"}]
        r = self._say(f"ช่วยแปลงลิงก์นี้ให้หน่อย {_NEW_URL}", history=h)
        self.assertEqual(r["routing"], "API")
        self.assertIn(_NEW_CONVERTED, r["reply"])
        self.assertNotIn(_OLD_CONVERTED, r["reply"])


# LINK-REAL-10 — explicit "convert the same one again" referent: audited,
# NOT currently supported by any referent-resolution mechanism for URLs
# -> honestly asks the customer to resend, never guesses.
class TestExplicitPreviousLinkReferent(_E2E):
    def test_link_real_10_explicit_referent_asks_rather_than_guesses(self):
        h = [{"role": "user", "content": _OLD_URL},
             {"role": "assistant", "content": f"แอดมินแปลงลิงก์ให้เรียบร้อยค่ะ คุณลูกค้าเปิดบิลเข้ามาได้เลยนะคะ\n{_OLD_CONVERTED}"}]
        r = self._say("แปลงลิงก์เดิมให้อีกครั้ง", history=h)
        self.assertFalse(r["erp_called"])
        self.assertEqual(r["reply"], _ASK_URL)
        self.assertNotIn("696614936668", r["reply"])


if __name__ == "__main__":
    unittest.main()
