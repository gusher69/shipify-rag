# -*- coding: utf-8 -*-
"""REGRESSION-GATE-1 — Known REAL-LINE Regression suite.

A PERMANENT, individually-named registry of REAL LINE behaviours the
product owner has confirmed (via manual REAL LINE testing, never by
Claude) — one test per named case (`RL-STATE-01`, `RL-COUPON-02`, ...),
so a future regression against any single one is immediately
attributable by ID, not buried in an aggregate pass count.

Each case below was audited against its customer source (the 69-case
`tests/customer_uat/customer_uat_master.jsonl`, `docs/customer_uat_
sources/*`, or the exact REAL LINE session traced in
`docs/customer_uat_sources/SYSTEM_STATE_EMERGENCY_1.md`) BEFORE writing
its assertion — no expected behaviour here was invented. Two cases
(`RL-PRODUCT-01`, `RL-CUSTOM-SERVICE-01`) are KNOWN, currently-failing
residuals: their route/safety assertions are real `assertEqual`s, and
the specific still-broken behaviour is captured with
`@unittest.expectedFailure` so it stays loud (and fails THIS test file
the moment it is accidentally "fixed", which is the signal to remove
the decorator) rather than being silently hidden or invented as a PASS.

Reuses the `_Session` real-registry E2E harness and `_llm` degraded-LLM
fake already built for SYSTEM-STATE-EMERGENCY-1 (same fixtures, same
verified customer FT3182 / Uc5f5717bc090934f9eaa067513388178) — no
second harness, no second semantic engine.
"""
import re
import unittest
from unittest.mock import patch

from tests.test_system_state_emergency_1 import _Session
from tests.test_calculator_regression_2 import _PEND_ORDER, _CALC_MSG, _llm_calc

_NO_COUPON_RE = re.compile(r"ไม่พบ.*คูปอง|ไม่มีคูปอง")
_ASKS_CUSTCODE_RE = re.compile(r"รหัสลูกค้า")
_CHINESE_TRACK_ASK_RE = re.compile(r"เลขแทรคจีน|เลขติดตามจีน|tracking.*จีน")
_FAKE_SUCCESS_RE = re.compile(r"ดำเนินการเรียบร้อยแล้ว|เรียบร้อยค่ะ.*จัดส่ง")


# ── RL-STATE-01 / RL-STATE-02 — pending flow must yield to an explicit
# new actionable intent (the SYSTEM-STATE-EMERGENCY-1 central rule) ────
class TestRLState01CalculatorEntersOverPendingFlow(_Session):
    """RL-STATE-01 — a calculator request must enter the Calculator even
    with an unrelated pending collection active (the original REAL LINE
    CALCULATOR-REGRESSION-2 failure: it fell into
    `กรุณาแจ้งเลขที่คำสั่งซื้อค่ะ` instead)."""

    def test_calculator_wins_over_pending_order_lookup(self):
        with patch("services.conversation_semantics._llm_family", side_effect=_llm_calc):
            r = self.eng.decide(_CALC_MSG, history=_PEND_ORDER,
                                 context={"channel": "line", "tenant_id": "default",
                                          "external_user_id": "U_rl_state01", "developer_mode": True,
                                          "customer_context": {}})
        dev = r.get("developer") or {}
        self.assertEqual(dev.get("selection_source"), "shipping_estimate_flow")
        self.assertNotIn("เลขที่คำสั่งซื้อ", (r.get("reply") or {}).get("text") or "")


class TestRLState02ShipmentStatusEntersOverPendingFlow(_Session):
    """RL-STATE-02 — 'ร้านส่งหรือยังคะ' with an unrelated pending
    calculator/other flow active must enter Shipment Status
    (`private_state_inquiry`), never be swallowed by the stale flow."""

    def test_shipment_status_wins_over_pending_charter(self):
        self.h += [
            {"role": "user", "content": "มีบริการเหมารถไหมคะ"},
            {"role": "assistant", "content": "สวัสดีค่ะ ทางเรามีบริการเหมารถให้ได้นะคะ "
             "คุณลูกค้าแจ้งเลขบิล และโลเคชั่นปลายทาง พร้อมกับชื่อผู้รับ และเบอร์โทรผู้รับมาได้เลยนะคะ"}]
        o = self.say("ร้านส่งหรือยังคะ")
        self.assertEqual(o["src"], "private_state_inquiry")
        self.assertNotEqual(o["src"], "charter_truck_collection")


# ── RL-COUPON-01 / RL-COUPON-02 — private MY_COUPONS vs public
# coupon-usage FAQ must never be conflated ──────────────────────────
class TestRLCoupon01MyCouponsIsPrivateErp(_Session):
    """RL-COUPON-01 — 'ผมมีคูปองอะไรบ้าง' is a PRIVATE ERP read
    (MY_COUPONS). Current verified FT3182 ERP result is NO coupons —
    the reply must say so, never dump an unrelated field or invent one."""

    def test_my_coupons_is_private_and_reports_none_found(self):
        o = self.say("ผมมีคูปองอะไรบ้าง")
        self.assertIn(o["routing"], ("API", "WORKFLOW"))
        self.assertTrue(_NO_COUPON_RE.search(o["reply"]),
                         f"expected a 'no coupons found' reply, got: {o['reply']!r}")


class TestRLCoupon02CouponUsageIsPublicRag(_Session):
    """RL-COUPON-02 — 'คูปองใช้ยังไงครับ' is a PUBLIC how-to FAQ. The
    word 'coupon' alone must never trigger a CustCode/identity demand
    (the SEMANTIC-FIRST-2.1 coupon-usage residual this locks in)."""

    def test_coupon_usage_never_asks_for_customer_code(self):
        o = self.say("คูปองใช้ยังไงครับ")
        self.assertFalse(_ASKS_CUSTCODE_RE.search(o["reply"]),
                          f"a public coupon how-to must not demand a customer code: {o['reply']!r}")
        self.assertNotEqual(o["src"], "charter_truck_collection")


# ── RL-INVOICE-01 — must answer directly, never ask a generic
# clarifying question the customer has already implicitly answered ──
class TestRLInvoice01AnswersDirectly(_Session):
    """RL-INVOICE-01 — 'ใบกำกับค่าสินค้าออกได้ไหม' must answer the
    invoice policy directly; must NOT ask 'สินค้าของลูกค้าเป็นอะไร'
    (the INVOICE-PRODUCT-REGRESSION-2 failure this locks in)."""

    def test_invoice_question_is_not_redirected_to_a_product_question(self):
        o = self.say("ใบกำกับค่าสินค้าออกได้ไหม")
        self.assertNotIn("สินค้าของลูกค้าเป็นอะไร", o["reply"])
        self.assertNotEqual(o["src"], "charter_truck_collection")


# ── RL-CALC-02 — the full calculator conversation, retaining context
# across a route comparison, with a genuinely new episode after ──────
class TestRLCalc02FullConversationRetainsContext(_Session):
    """RL-CALC-02 — calc -> road (192.26) -> 'ถ้าเป็นเรือล่ะ' -> sea
    (125.39), then a brand-new calculation carries nothing over."""

    def test_full_calculator_conversation(self):
        o = self.say("ช่วยคำนวณค่าส่ง น้ำหนัก 2 โล ขนาด 54x12x43")
        self.assertEqual(o["src"], "shipping_estimate_flow")
        o = self.say("เอารถครับ")
        self.assertIn("192.26", o["reply"])
        o = self.say("ถ้าเป็นเรือล่ะ")
        self.assertIn("125.39", o["reply"])
        o = self.say("ช่วยคิดค่าส่งใหม่ น้ำหนัก 5 โล")
        self.assertNotIn("54x12x43", o["reply"])
        self.assertNotIn("192.26", o["reply"])


# ── RL-TC19-01 — charter open -> partial slots -> remaining slots ->
# complete/handoff, exactly as manually PASSed on REAL LINE at 0702e64 ─
class TestRLTC1901PartialThenCompleteSlots(_Session):
    def test_charter_partial_slots_then_complete(self):
        self.h += [
            {"role": "user", "content": "มีบริการเหมารถไหมคะ"},
            {"role": "assistant", "content": "สวัสดีค่ะ ทางเรามีบริการเหมารถให้ได้นะคะ "
             "คุณลูกค้าแจ้งเลขบิล และโลเคชั่นปลายทาง พร้อมกับชื่อผู้รับ และเบอร์โทรผู้รับมาได้เลยนะคะ"}]
        o = self.say("เลขบิล FT318220260726001 ปลายทางบางนา")
        self.assertEqual(o["src"], "charter_truck_collection")
        self.assertIn("ชื่อ", o["reply"])           # asks the still-missing name + phone
        self.assertIn("เบอร์โทร", o["reply"])
        # already-given slots are acknowledged (echoed back), never RE-ASKED
        self.assertNotIn("รบกวนแจ้งเลขบิล", o["reply"])
        self.assertIn("FT318220260726001", o["reply"])   # echoed in the ack, not re-requested
        o = self.say("ชื่อผู้รับสมชาย เบอร์ 0812345678")
        self.assertEqual(o["src"], "charter_truck_collection")
        self.assertEqual(o["routing"], "HUMAN_HANDOFF")   # all 4 slots -> handoff


# ── RL-AMBIGUOUS-01 / RL-AMBIGUOUS-02 — referent-less possessive/
# demonstrative follow-up must clarify, never dump private ERP data ──
class TestRLAmbiguous01NoReferentClarifies(_Session):
    def test_possessive_with_no_referent_clarifies(self):
        self.h += [{"role": "user", "content": "ค่าส่งเท่าไหร่"},
                   {"role": "assistant", "content": "การคิดค่าขนส่งใช้ปริมาตร กว้าง x ยาว x สูง หารด้วย 5000 ค่ะ"}]
        o = self.say("ของผมล่ะ")
        self.assertTrue(o["ref_clarify"])
        self.assertIn("ไม่แน่ใจว่าหมายถึง", o["reply"])


class TestRLAmbiguous02DemonstrativeWithNoReferentClarifies(_Session):
    def test_demonstrative_with_no_referent_clarifies(self):
        self.h += [{"role": "user", "content": "บิลนี้อยากเปลี่ยนที่อยู่จัดส่งครับ"},
                   {"role": "assistant", "content": "รบกวนแจ้งเลขที่บิล/Shipmentค่ะ"}]
        o = self.say("แล้วอันนี้ล่ะ")
        self.assertTrue(o["ref_clarify"])
        self.assertIn("ไม่แน่ใจว่าหมายถึง", o["reply"])


# ── RL-WAREHOUSE-01 — pickup-location is PUBLIC info, no ERP call, no
# fabricated 'done' confirmation ─────────────────────────────────────
class TestRLWarehouse01PublicNoFakeSuccess(_Session):
    def test_pickup_location_is_public_no_erp_no_fake_success(self):
        with patch("services.action_executor.requests.request") as mock_req:
            o = self.say("ไปรับของแถวไหนครับ")
            self.assertFalse(mock_req.called, "pickup-location is public info; must not call ERP")
        self.assertFalse(_FAKE_SUCCESS_RE.search(o["reply"]),
                          f"must not fabricate a completed-action reply: {o['reply']!r}")


# ── RL-TRACK-TH-01 — customer-required: Thai tracking via the shipment
# bill/reference, never demanding a Chinese tracking number ─────────
class TestRLTrackTH01ThaiTrackingViaShipmentBill(_Session):
    """Source: `tests/customer_uat/customer_uat_master.jsonl` CUS-S17
    ('ขอแทรคไทยค่ะ (กรณีลูกค้าไม่เข้ามาเช็คในระบบเอง)', Ai.xlsx sheet
    '2.tongchecknairabop' row 17 / CSW17, api_input_hint: shipment_bill_no).
    Customer-approved answer asks for the SHIPMENT BILL, never a Chinese
    tracking number, then returns the Thai-leg tracking/status.

    Fixed by CUSTOMER-TRACK-TH-1 (confirmed REAL LINE failures:
    'กรุณาแจ้งเลข Tracking จีนค่ะ' and a false Human CS handoff). Root
    cause: `_PSI_DOMAIN_ACTION_HINTS['tracking']['track_hint']` preferred
    `searchdatatracking` (search BY a China tracking number — the
    OPPOSITE direction, confirmed live via its own ai_description
    'ค้นหาบิลขนส่งด้วยเลข Tracking จีน') for ANY message matching the
    'tracking' domain, including a Thai-tracking OUTPUT ask that never
    provides one. Fixed: `_PSI_THAI_TRACKING_OUTPUT_RE` detects this
    shape and routes to `searchdatashipment` instead (ShipmentCode ->
    TrackingTH/TrackingCH/Status/TotalSum), which already returns Thai
    tracking directly with no China tracking as input."""

    def test_thai_tracking_asks_shipment_bill_not_chinese_tracking(self):
        o = self.say("ขอแทรคไทยค่ะ (กรณีลูกค้าไม่เข้ามาเช็คในระบบเอง)")
        self.assertIn(o["routing"], ("API", "WORKFLOW"))
        self.assertEqual(o["src"], "private_state_inquiry")
        self.assertFalse(_CHINESE_TRACK_ASK_RE.search(o["reply"]),
                          f"must never ask for a Chinese tracking number: {o['reply']!r}")
        self.assertIn("บิลขนส่ง", o["reply"])
        self.assertNotEqual(o["routing"], "HUMAN_HANDOFF")


# ── RL-PRODUCT-01 — KNOWN RESIDUAL. Route is correct (PRODUCT_POLICY,
# no flow theft); the ANSWER FRAMING is still a generic service-ack
# instead of a prohibited-goods verdict (documented in SYSTEM_STATE_
# EMERGENCY_1.md's "Remaining regression" — out of scope for that
# state-authority blocker, tracked here as a permanent named case). ──
class TestRLProduct01ProductPolicyRoute(_Session):
    def test_product_policy_route_is_correct(self):
        o = self.say("กล่องพลาสติกนำเข้าได้ไหมครับ")
        self.assertNotEqual(o["src"], "charter_truck_collection")
        self.assertIn(o["routing"], ("RAG", "GENERAL", "WORKFLOW"))

    @unittest.expectedFailure
    def test_KNOWN_FAILURE_answer_is_a_verdict_not_a_generic_ack(self):
        """KNOWN FAILURE (RL-PRODUCT-01) — the routing-only harness
        stubs RAG, so this exercises the REAL playground answer path
        via the live RAG pipeline being unavailable in this test env;
        recorded here so the case is never silently dropped. Remove
        this decorator once `run_playground_turn` returns a genuine
        prohibited-goods verdict instead of the
        `_product_answer_service_continuation` ack for this phrasing."""
        o = self.say("กล่องพลาสติกนำเข้าได้ไหมครับ")
        self.assertNotIn("รับทราบค่ะ", o["reply"])


# ── RL-CUSTOM-SERVICE-01 — audited source found (Ai.xlsx CUS-S06 /
# CSW6): an approved ack+collect+coordinate process EXISTS, so honest
# no-info is NOT the correct target here — this is a KNOWN capability
# gap (not covered by CUSTOMER-ACTION-1's operational-change kinds),
# recorded as a permanent named case rather than invented as PASS. ──
class TestRLCustomService01LogoPrintingRequest(_Session):
    """Source: `tests/customer_uat/customer_uat_master.jsonl` CUS-S06
    ('ต้องการสั่งผลิตตามสเปค ,สั่งสกรีนโลโก้ได้ไหมคะ', Ai.xlsx sheet
    '2.tongchecknairabop' row 6 / CSW6). Customer-approved answer:
    acknowledge -> collect (bill number + product spec/qty/color/logo)
    -> coordinate with the store -> report back. `services.operational_
    change_flow._KINDS` does not yet include a custom-production/
    screen-print kind, so this currently falls through to a RAG stub
    instead of the ack+collect flow. Real capability gap, not a test
    artifact — tracked here rather than hidden."""

    def test_route_does_not_fabricate_an_answer(self):
        o = self.say("สั่งสกรีนโลโก้เสื้อได้ไหมคะ")
        # whatever it does, it must not fabricate acceptance of a
        # service the platform has no configured way to fulfil
        self.assertNotIn("ดำเนินการเรียบร้อย", o["reply"])

    @unittest.expectedFailure
    def test_KNOWN_FAILURE_no_ack_collect_flow_for_custom_production(self):
        """KNOWN FAILURE (RL-CUSTOM-SERVICE-01) — no operational-change
        kind covers 'custom production / screen-print / logo' yet, so
        this does not yet ask for the bill + spec as the customer's
        approved script requires. Remove this decorator once
        `operational_change_flow._KINDS` gains that kind."""
        o = self.say("สั่งสกรีนโลโก้เสื้อได้ไหมคะ")
        self.assertIn("เลขบิล", o["reply"])


if __name__ == "__main__":
    unittest.main()
