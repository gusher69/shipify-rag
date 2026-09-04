# -*- coding: utf-8 -*-
"""SYSTEM-STATE-EMERGENCY-1 — central state / route authority.

REAL LINE (production session 6c9b9026, turns 780-809): once the
charter-truck (TC19) collection opened it consumed EVERY later turn until
its 4 slots were full —

    "ช่วยคำนวณค่าส่ง น้ำหนัก 2 โล ขนาด 54x12x43"  -> charter slot prompt
    "ใบกำกับค่าสินค้าออกได้ไหม"                    -> charter slot prompt
    "ผมมีคูปองอะไรบ้าง"                            -> charter slot prompt
    "ทางเรือกี่วันครับ"                            -> charter slot prompt
    "บิลนี้อยากเปลี่ยนที่อยู่จัดส่งครับ"            -> charter slot prompt

and then a bare referent-less "ของผมล่ะ" / "แล้วอันนี้ล่ะ" returned the
full customer profile + Purchase Wallet + email + phone.

Central fix (services/decision_engine.py):
  * `_current_intent_breaks_pending_flow` — the ONE arbitration rule
    applied BEFORE every flow-local pending-state continuation. A
    decisive NEW actionable intent (follow_up_op NONE, an actionable
    family, confidence >= .55) that is not the flow's own family and not
    a value the flow's slot extractor accepts, breaks the flow.
  * a bare possessive / demonstrative follow-up with no referent
    established by the last few assistant turns -> a clarification, never
    a customer-profile / wallet ERP read.

Tested as ONE accumulated session (state-contamination detection — never
a clean isolated turn per message).
"""
import re
import unittest
from unittest.mock import MagicMock, patch

from services.decision_engine import DecisionEngine
from tests.test_decision_engine import _fake_playground_result
from tests.test_business_action_registry import reset_real_registry

_UID = "Uc5f5717bc090934f9eaa067513388178"
_BIND = {"cust_code": "FT3182", "status": "verified", "channel": "line",
         "external_user_id": _UID, "tenant_id": "default"}

# the widened SEMANTIC-FIRST-2 LLM gate is reachable in production for any
# conversational Thai turn; the local test env has no LLM, so simulate a
# competent family resolver (keys on abstract cues, never a phrase table).
def _llm(message, history=None):
    s = message or ""
    if re.search(r"คำนวณ|ค่าส่ง|ค่าขนส่ง|ค่านำเข้า|ประเมิน|กี่บาท", s) and not re.search(r"กี่วัน", s):
        return {"family": "SHIPPING_ESTIMATE"}
    if re.fullmatch(r"\s*(?:เอา|ขอ|ใช้|เป็น)?\s*(?:ทาง|โดย)?\s*(?:รถ|เรือ|บก)\s*(?:ครับ|ค่ะ|คะ|นะ)?\s*", s):
        return {"family": "SHIPPING_ESTIMATE"}
    if re.search(r"เหมารถ|จ้างรถ|เช่ารถ", s):
        return {"family": "CHARTER_TRUCK"}
    if re.search(r"ใบกำกับ|ใบเสร็จ|ภาษี", s):
        return {"family": "INVOICE"}
    if re.search(r"คูปอง|ส่วนลด", s) and re.search(r"ยังไง|ตรงไหน|ใช้", s):
        return {"family": "COUPON_USAGE"}
    if re.search(r"มีคูปอง|คูปอง.*บ้าง|คูปอง.*กี่", s):
        return {"family": "MY_COUPONS", "is_private": True}
    if re.search(r"รับของ|โกดัง|คลัง|สาขา", s) and re.search(r"ไหน|ตรงไหน|ที่ไหน", s):
        return {"family": "PICKUP_LOCATION"}
    if re.search(r"กล่อง|พลาสติก|สินค้า", s) and re.search(r"นำเข้าได้ไหม|ส่งได้ไหม", s):
        return {"family": "PRODUCT_POLICY"}
    if re.search(r"เปลี่ยน|ย้าย|สลับ", s) and re.search(r"ที่อยู่|จัดส่ง|ปลายทาง", s):
        return {"family": "ADDRESS_CHANGE"}
    if re.search(r"ร้าน.{0,4}ส่ง|ออกจากร้าน|ต้นทางส่ง", s):
        return {"family": "SHIPMENT_STATUS", "is_private": True}
    if re.search(r"กี่วัน", s):
        return {"family": "SHIPMENT_STATUS"}
    return {"family": "UNKNOWN"}


class _Session(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        reset_real_registry()  # TEST-ISOLATION (REGRESSION-GATE-1) — see reset_real_registry() docstring
        cls.eng = DecisionEngine()

    def setUp(self):
        self.h = []

    def say(self, msg, *, verified=True):
        bsvc = MagicMock()
        bsvc.get_verified_binding.return_value = _BIND if verified else None
        bsvc.get_verified_binding_for_custcode.return_value = _BIND if verified else None
        ctx = {"channel": "line", "tenant_id": "default", "external_user_id": _UID,
               "developer_mode": True,
               "customer_context": {"cust_code": "FT3182", "identity_confirmed": True,
                                    "last_business_action": "getdatacustomer"} if verified else {}}
        with patch("services.conversation_semantics._llm_family", side_effect=_llm), \
             patch("services.customer_binding_service.get_customer_binding_service", return_value=bsvc), \
             patch("services.credential_store.CredentialStore.resolve",
                   return_value={"ok": True, "value": "S", "error": None}), \
             patch("services.action_executor.requests.request",
                   return_value=MagicMock(status_code=200, json=lambda: {"data": [{
                       "CustCode": "FT3182", "CustName": "TEST", "PurchaseWallet": "69616.82",
                       "ShippingWallet": "0.00", "CustEmail": "x@y.com", "CustPhone": "0812345678",
                       "Coupon": []}]})):
            r = self.eng.decide(msg, history=list(self.h), context=ctx)
        dev = r.get("developer") or {}
        reply = (r.get("reply") or {}).get("text") or ""
        self.h += [{"role": "user", "content": msg}, {"role": "assistant", "content": reply}]
        return {"routing": (r.get("routing") or {}).get("type"),
                "src": dev.get("selection_source"),
                "broke": dev.get("pending_flow_broken_by_current_intent"),
                "ref_clarify": dev.get("referentless_private_clarify"),
                "reply": reply}


_CHARTER_SLOT = "รบกวนแจ้งเลขบิลขนส่ง และ โลเคชั่นปลายทาง และ ชื่อผู้รับ และ เบอร์โทรผู้รับ"
_WALLET_DUMP = re.compile(r"Purchase Wallet|69,?616|ยอดเงิน\s*Purchase")


class TestExactProductOwnerJourney(_Session):
    """The 15-turn journey in ONE accumulated session. The charter flow
    is opened by a TC19 FAQ answer that is already in `self.h`."""

    def _seed_charter_open(self):
        self.h += [
            {"role": "user", "content": "มีบริการเหมารถไหมคะ"},
            {"role": "assistant", "content": "สวัสดีค่ะ ทางเรามีบริการเหมารถให้ได้นะคะ "
             "คุณลูกค้าแจ้งเลขบิล และโลเคชั่นปลายทาง พร้อมกับชื่อผู้รับ และเบอร์โทรผู้รับมาได้เลยนะคะ"}]

    def test_journey(self):
        self._seed_charter_open()
        steps = [
            ("ช่วยคำนวณค่าส่ง น้ำหนัก 2 โล ขนาด 54x12x43", "shipping_estimate_flow"),
            ("ช่วยคิดค่าส่งใหม่ น้ำหนัก 5 โล", "shipping_estimate_flow"),
            ("ร้านส่งหรือยังคะ", "private_state_inquiry"),
            ("ใบกำกับค่าสินค้าออกได้ไหม", None),          # RAG invoice policy
            ("ผมมีคูปองอะไรบ้าง", None),                  # MY_COUPONS private
            ("ไปรับของแถวไหนครับ", None),                 # warehouse RAG
            ("กล่องพลาสติกนำเข้าได้ไหมครับ", None),        # product policy RAG
            ("มีบริการเหมารถไหมคะ", None),                # re-enters charter (RAG FAQ)
            ("ร้านส่งหรือยังคะ", "private_state_inquiry"),
            ("คูปองใช้ยังไงครับ", None),                  # coupon usage RAG
            ("ทางเรือกี่วันครับ", None),                  # transit-time RAG
            ("บิลนี้อยากเปลี่ยนที่อยู่จัดส่งครับ", None),   # ADDRESS_CHANGE
        ]
        for msg, want_src in steps:
            o = self.say(msg)
            self.assertNotIn(_CHARTER_SLOT, o["reply"],
                             f"{msg!r} was swallowed by the sticky charter flow")
            self.assertNotEqual(o["src"], "charter_truck_collection", msg)
            if want_src:
                self.assertEqual(o["src"], want_src, msg)

        # 14 + 15 — referent-less possessive after a non-private-topic turn
        for msg in ("ของผมล่ะ", "แล้วอันนี้ล่ะ"):
            o = self.say(msg)
            self.assertTrue(o["ref_clarify"], f"{msg!r} did not clarify")
            self.assertEqual(o["routing"], "WORKFLOW")
            self.assertIn("ไม่แน่ใจว่าหมายถึง", o["reply"])
            self.assertFalse(_WALLET_DUMP.search(o["reply"]),
                             f"{msg!r} leaked private wallet/profile data")


class TestValidContinuationsPreserved(_Session):
    def _seed_charter_open(self):
        self.h += [
            {"role": "user", "content": "มีบริการเหมารถไหมคะ"},
            {"role": "assistant", "content": "สวัสดีค่ะ ทางเรามีบริการเหมารถให้ได้นะคะ "
             "คุณลูกค้าแจ้งเลขบิล และโลเคชั่นปลายทาง พร้อมกับชื่อผู้รับ และเบอร์โทรผู้รับมาได้เลยนะคะ"}]

    def test_charter_slot_values_still_continue_charter(self):
        self._seed_charter_open()
        o = self.say("เลขบิล FT318220260726001 ปลายทางบางนา")
        self.assertEqual(o["src"], "charter_truck_collection")
        o = self.say("ชื่อผู้รับสมชาย เบอร์ 0812345678")
        self.assertEqual(o["src"], "charter_truck_collection")
        self.assertEqual(o["routing"], "HUMAN_HANDOFF")   # all 4 -> handoff

    def test_partial_destination_still_continues_charter(self):
        self._seed_charter_open()
        o = self.say("ปลายทางบางนา")
        self.assertEqual(o["src"], "charter_truck_collection")

    def test_charter_retrigger_continues(self):
        self._seed_charter_open()
        o = self.say("มีบริการเหมารถไหมครับ")
        self.assertNotEqual(o["src"], "fresh_search")

    def test_calculator_route_answer_and_comparison_continue(self):
        o = self.say("ช่วยคำนวณค่าส่ง น้ำหนัก 2 โล ขนาด 54x12x43")
        self.assertEqual(o["src"], "shipping_estimate_flow")
        o = self.say("เอารถครับ")
        self.assertIn("192.26", o["reply"])
        o = self.say("ถ้าเป็นเรือล่ะ")
        self.assertIn("125.39", o["reply"])
        o = self.say("ช่วยคิดค่าส่งใหม่ น้ำหนัก 5 โล")     # NEW episode, no inheritance
        self.assertNotIn("54x12x43", o["reply"])
        self.assertNotIn("192.26", o["reply"])


class TestReferentResolution(_Session):
    def test_possessive_with_context_referent_is_not_clarified(self):
        # the assistant JUST offered a specific private-data topic
        self.h += [
            {"role": "user", "content": "อยากเช็กคูปอง"},
            {"role": "assistant", "content": "ต้องการให้ตรวจสอบคูปองในบัญชีของคุณไหมคะ"}]
        o = self.say("ของผมล่ะ")
        self.assertFalse(o["ref_clarify"],
                         "a possessive WITH a recent private-data referent must not be force-clarified")


class TestReferentlessNoDataDumpUnverified(_Session):
    def test_referentless_never_dumps_even_conceptually(self):
        self.h += [{"role": "user", "content": "ค่าส่งเท่าไหร่"},
                   {"role": "assistant", "content": "การคิดค่าขนส่งใช้ปริมาตร กว้าง x ยาว x สูง หารด้วย 5000 ค่ะ"}]
        o = self.say("ของผมล่ะ")
        self.assertTrue(o["ref_clarify"])
        self.assertFalse(_WALLET_DUMP.search(o["reply"]))


if __name__ == "__main__":
    unittest.main()
