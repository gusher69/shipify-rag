# -*- coding: utf-8 -*-
"""CUSTOMER-TRACK-TH-1.1 — shipment follow-up context / reference memory.

REAL LINE journey (product owner testing, session context continuing
from CUSTOMER-TRACK-TH-1):
  "ขอเลขแทรคไทยค่ะ" -> "กรุณาแจ้งเลขที่บิลขนส่งค่ะ"                    PASS
  "FT318220260726001" -> bill/status/China tracking/total, no Thai yet PASS
  "ตอนนี้ถึงไหนแล้วคะ" -> "กรุณาแจ้งเลขที่บิลขนส่งค่ะ"                  FAIL (re-asked the SAME bill)
  "คูปองใช้ยังไงครับ" -> Coupon Usage                                 PASS

ROOT CAUSE (traced, not guessed): `services.decision_engine.
_resolve_conversation_reference` DOES correctly remember which Business
Action the customer was last using (`customer_context["last_business_
action"]`, persisted by `profiles/manager.py::update_profile_from_turn`)
and correctly re-selects `searchdatashipment` for a topic-free
referring follow-up ("ตอนนี้ถึงไหนแล้วคะ" matches `_REFERENCE_MARKER_RE`
via "ตอนนี้"). But two things then still asked for the bill again:

1. `_handle_dynamic_collection`'s `_is_continuation` gate (which decides
   whether Identifier Memory may auto-fill a still-missing ASKABLE
   parameter) only trusted `conversation_continuation` and
   `conversation_reference_detail` — NOT the plain `conversation_
   reference` source this exact scenario produces, even though both of
   `_resolve_conversation_reference`'s own entry conditions (a named
   response field, or a topic-free referring marker) are equally
   decisive evidence of a genuine continuation. Fixed: `conversation_
   reference` added to the trusted set.
2. Even once trusted, there was nothing to auto-fill FROM: Task 06
   (2026-08-26, a real security fix) deliberately REMOVED persistence
   of `last_shipment_code`/`last_order_code`/`last_tracking` onto the
   profile row — a customer-typed identifier is never proof of account
   ownership and must never be remembered across SESSIONS. Re-adding
   that persistence would regress Task 06. Fixed instead with a new,
   narrower, session-local recovery: `_recover_history_identifier` walks
   the CURRENT conversation's own `history` (never a DB write) for the
   most recent structurally-valid ShipmentCode whose lookup genuinely
   succeeded (never a failed/malformed one) — reused ONLY when
   `is_continuation` is already true, i.e. only for a turn already
   proven to be a decisive, referring follow-up.

Also fixed (config-only, no code change): `searchdatashipment`'s
TrackingTH response_mapping row only listed English-loanword keywords
("tracking ไทย"/"tracking thailand"), so "แทรคไทยมีหรือยังคะ" (plain
Thai) could not match `_resolve_conversation_reference`'s own
field-name condition. Added the plain-Thai transliteration forms
("แทรคไทย", "แทร็กไทย", ...).

Reuses the real-registry E2E harness from tests.test_customer_track_th1.
"""
import unittest
from unittest.mock import MagicMock

from tests.test_customer_track_th1 import _TrackE2E

_ASK_BILL = "กรุณาแจ้งเลขที่บิลขนส่งค่ะ"
_BILL_A = "FT318220260726001"
_BILL_B = "FT318220260999999"
_NOT_FOUND_BILL = "FT999999999999999"


class _FollowE2E(_TrackE2E):
    def _lookup(self, bill, *, status="รับเข้าที่จีน", tracking_th=""):
        """Turn 1 (ask) + turn 2 (successful lookup), left in self.h."""
        self._say_with_exec("ขอเลขแทรคไทยค่ะ")
        o = self._say_with_exec(bill, exec_response=self._shipment_response(
            Status=status, TrackingCH="testlineOnNut007", TrackingTH=tracking_th, TotalSum="2786.75"))
        return o

    def _failed_lookup(self, bill):
        self._say_with_exec("ขอเลขแทรคไทยค่ะ")
        return self._say_with_exec(bill, exec_response=MagicMock(
            status_code=200, json=lambda: {"data": {"Shipment": {}}}))


# TRACK-FOLLOW-01
class TestFollow01StatusAfterLookup(_FollowE2E):
    def test_status_follow_up_reuses_same_shipment(self):
        self._lookup(_BILL_A, status="รับเข้าที่จีน")
        o = self._say_with_exec("ตอนนี้ถึงไหนแล้วคะ",
                                 exec_response=self._shipment_response(Status="ส่งออกจากจีนแล้ว"))
        self.assertEqual(o["action"], "searchdatashipment")
        self.assertIn(_BILL_A, o["reply"])
        self.assertIn("ส่งออกจากจีนแล้ว", o["reply"])
        self.assertNotEqual(o["reply"], _ASK_BILL)
        self.assertTrue(o["erp_called"])   # re-queries current status, never a stale cached fact


# TRACK-FOLLOW-02
class TestFollow02ThaiTrackingStillUnavailable(_FollowE2E):
    def test_thai_tracking_follow_up_reuses_same_shipment(self):
        self._lookup(_BILL_A)
        o = self._say_with_exec("แทรคไทยมีหรือยังคะ", exec_response=self._shipment_response(TrackingTH=""))
        self.assertEqual(o["action"], "searchdatashipment")
        self.assertNotEqual(o["reply"], _ASK_BILL)
        self.assertIn("ยังไม่มีเลข Tracking ไทย", o["reply"])


# TRACK-FOLLOW-03
class TestFollow03StatusShorthand(_FollowE2E):
    def test_status_shorthand_reuses_same_shipment(self):
        self._lookup(_BILL_A)
        o = self._say_with_exec("แล้วสถานะล่ะ", exec_response=self._shipment_response(Status="ส่งออกจากจีนแล้ว"))
        self.assertEqual(o["action"], "searchdatashipment")
        self.assertIn(_BILL_A, o["reply"])
        self.assertNotEqual(o["reply"], _ASK_BILL)


# TRACK-FOLLOW-04
class TestFollow04FreshSessionNoReferent(_FollowE2E):
    def test_fresh_session_asks_no_guessed_shipment(self):
        o = self._say_with_exec("ตอนนี้ถึงไหนแล้วคะ")
        self.assertEqual(o["reply"], _ASK_BILL)
        self.assertFalse(o["erp_called"])


# TRACK-FOLLOW-05
class TestFollow05NewShipmentOverridesOld(_FollowE2E):
    def test_second_lookup_wins(self):
        self._lookup(_BILL_A)
        self._lookup(_BILL_B)
        o = self._say_with_exec("ตอนนี้ถึงไหนแล้วคะ", exec_response=self._shipment_response(
            **{"Code": _BILL_B, "Status": "รับเข้าที่จีน"}))
        self.assertIn(_BILL_B, o["reply"])
        self.assertNotIn(_BILL_A, o["reply"])


# TRACK-FOLLOW-06
class TestFollow06FailedLookupNeverBecomesReferent(_FollowE2E):
    def test_failed_lookup_never_reported_as_success(self):
        self._failed_lookup(_NOT_FOUND_BILL)
        o = self._say_with_exec("ตอนนี้ถึงไหนแล้วคะ")
        self.assertNotIn("เรียบร้อยค่ะ", o["reply"])
        self.assertNotIn("รับเข้าที่จีน", o["reply"])   # never fabricates a real status for a not-found bill
        self.assertNotIn("สำเร็จ", o["reply"])


# TRACK-FOLLOW-07
class TestFollow07ShipmentContextThenCoupon(_FollowE2E):
    def test_coupon_wins(self):
        self._lookup(_BILL_A)
        o = self._say_with_exec("คูปองใช้ยังไงครับ")
        self.assertNotEqual(o["action"], "searchdatashipment")
        self.assertFalse(o["erp_called"] and o["action"] == "searchdatashipment")


# TRACK-FOLLOW-08
class TestFollow08ShipmentContextThenCalculator(_FollowE2E):
    def test_calculator_wins(self):
        self._lookup(_BILL_A)
        o = self._say_with_exec("ช่วยคำนวณค่าส่ง น้ำหนัก 2 โล ขนาด 54x12x43")
        self.assertNotEqual(o["action"], "searchdatashipment")
        self.assertIn("ทางรถหรือทางเรือ", o["reply"])


# TRACK-FOLLOW-09
class TestFollow09ReferentlessStillClarifies(_FollowE2E):
    def test_referentless_possessive_still_clarifies_no_dump(self):
        self._lookup(_BILL_A)
        o = self._say_with_exec("ของผมล่ะ")
        # the clarify template's own wording NAMES "Wallet"/"สถานะสินค้า"
        # as example TOPICS ("รบกวนระบุเพิ่มเติม เช่น ... หรือยอด Wallet
        # ค่ะ") -- that is not a leak. A real leak would be an actual
        # VALUE (the shipment bill just looked up, or a wallet amount).
        self.assertIn("ไม่แน่ใจว่าหมายถึง", o["reply"])
        self.assertNotIn(_BILL_A, o["reply"])
        self.assertNotIn("2786.75", o["reply"])
        self.assertNotIn("testlineOnNut007", o["reply"])
        self.assertNotEqual(o["action"], "searchdatashipment")


if __name__ == "__main__":
    unittest.main()
