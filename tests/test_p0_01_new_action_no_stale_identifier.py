"""P0-01 (REAL LINE UAT, 2026-09-03) — a FRESH explicit Business Action
request must not silently reuse an account-scoped RECORD identifier left
in session Identifier Memory from an earlier lookup.

Confirmed REAL LINE failure (production HEAD 408a33e): after an earlier
turn had captured last_shipment_code = FT318220260726001, the fresh
message "ขอเช็กพัสดุเดียวครับ" (no bill number) selected the DETAIL
action searchdatashipment, _apply_identifier_memory auto-filled
ShipmentCode from that stale session value, the action became
immediately executable, and SearchDataShipment ran — instead of asking
"กรุณาแจ้งเลขที่บิลขนส่งค่ะ" and starting a fresh DETAIL workflow.

Fix (services/decision_engine.py): _apply_identifier_memory /
_replay_business_action_collection only auto-fill an ACCOUNT-SCOPED
record identifier (last_order_code / last_shipment_code / last_tracking)
when this turn genuinely CONTINUES an active collection
(selection_source == "conversation_continuation"). CustCode (customer
identity) still fills either way; a genuine continuation and an explicit
current-turn identifier are both untouched. Identifier Memory is not
globally disabled.

Fixture identifiers/phrases appear here ONLY.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_business_action_registry import _FakeSupabase
from tests.test_decision_engine import _seed_action, _fake_playground_result, _engine_with_registry
from services.business_action_registry import BusinessActionRegistry
from services.decision_engine import _apply_identifier_memory

_SHIP_RE = r"^[A-Za-z]{2}\d{6,}$"


def _shipment_actions(reg):
    detail = _seed_action(reg, key="searchdatashipment", action_type="API",
                          category="Customer Shipment Retrieval",
                          keywords=["เลขบิลขนส่ง", "พัสดุเดียว", "shipment detail", "รายละเอียดพัสดุ"])
    reg.replace_parameters(detail, [
        {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": True,
         "input_source": "customer_message", "validation_pattern": r"^[A-Za-z]{2}\d{3,6}$"},
        {"name": "SecretCode", "display_name": "SecretCode", "required": True,
         "input_source": "credential_store"},
        {"name": "ShipmentCode", "display_name": "เลขที่บิลขนส่ง", "required": True,
         "input_source": "customer_message", "validation_pattern": _SHIP_RE},
    ])
    reg.upsert_execution(detail, {"endpoint": "https://example.test/SearchDataShipment", "http_method": "POST"})
    lst = _seed_action(reg, key="searchdatashipmentlist", action_type="API",
                       category="Customer Shipment Retrieval",
                       keywords=["ติดตามพัสดุ", "พัสดุ", "พัสดุล่าสุด", "รายการพัสดุ", "shipment"])
    reg.replace_parameters(lst, [
        {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": True,
         "input_source": "customer_message", "validation_pattern": r"^[A-Za-z]{2}\d{3,6}$"},
        {"name": "SecretCode", "display_name": "SecretCode", "required": True,
         "input_source": "credential_store"},
    ])
    reg.upsert_execution(lst, {"endpoint": "https://example.test/List", "http_method": "POST"})
    return detail, lst


# session Identifier Memory carrying a same-customer record id from an
# earlier lookup (last_business_action + last_shipment_code)
_CTX_WITH_STALE_SHIPCODE = {
    "cust_code": "FT3182", "identity_confirmed": True,
    "last_business_action": "searchdatashipment",
    "last_shipment_code": "FT318220260726001",
}
_PRIOR_HISTORY = [
    {"role": "user", "content": "ค่าขนส่งคิดยังไง"},
    {"role": "assistant", "content": "คิดจากปริมาตรเทียบน้ำหนักค่ะ"},
]


class P001_Base(unittest.TestCase):
    def setUp(self):
        self.reg = BusinessActionRegistry(_FakeSupabase())
        self.detail, self.lst = _shipment_actions(self.reg)
        self.engine = _engine_with_registry(self.reg)

    def _decide(self, message, *, history=None, ctx=None, pending_action_id=None):
        context = {"developer_mode": True, "channel": "line",
                   "customer_context": dict(ctx if ctx is not None else _CTX_WITH_STALE_SHIPCODE)}
        if pending_action_id:
            context["pending_action_id"] = pending_action_id
        with patch("services.action_executor.requests.request",
                   return_value=MagicMock(status_code=200, json=lambda: {
                       "data": {"Shipment": {"Code": "X", "Status": "รับเข้าที่จีน"}}})) as mock_req, \
             patch("services.playground_orchestrator.run_playground_turn",
                   return_value=_fake_playground_result(answer="RAG", confidence=0.9)):
            result = self.engine.decide(message, history=history if history is not None else list(_PRIOR_HISTORY),
                                         context=context)
        return result, mock_req


# ── A — the exact P0-01 real failure ───────────────────────────────

class P001_A_FreshRequestDoesNotReuseStaleRecordId(P001_Base):
    def test_fresh_detail_request_asks_for_shipment_code_no_erp(self):
        result, mock_req = self._decide("ขอเช็กพัสดุเดียวครับ")
        dev = result.get("developer") or {}
        ics = dev.get("information_collection_status") or {}
        self.assertEqual(result["routing"]["type"], "WORKFLOW")
        self.assertEqual(ics.get("selected_business_action"), "searchdatashipment")   # DETAIL owns it
        self.assertIn("ShipmentCode", ics.get("missing_parameters") or [])
        self.assertNotEqual((ics.get("collected_parameters") or {}).get("ShipmentCode"),
                            "FT318220260726001")                                       # NOT auto-filled
        self.assertIn("บิลขนส่ง", result["reply"]["text"])                             # asks for the bill
        mock_req.assert_not_called()                                                   # ERP CALL = NO

    def test_custcode_identity_still_fills_from_memory(self):
        # only the RECORD id is withheld — CustCode (identity) still fills
        result, _ = self._decide("ขอเช็กพัสดุเดียวครับ")
        ics = (result.get("developer") or {}).get("information_collection_status") or {}
        self.assertEqual((ics.get("collected_parameters") or {}).get("CustCode"), "FT3182")


# ── B — genuine continuation still uses the identifier ──────────────

class P001_B_ContinuationStillUsesIdentifier(P001_Base):
    def test_reply_to_the_bots_own_question_executes(self):
        history = _PRIOR_HISTORY + [
            {"role": "user", "content": "ขอเช็กพัสดุเดียวครับ"},
            {"role": "assistant", "content": "กรุณาแจ้งเลขที่บิลขนส่งค่ะ"},
        ]
        # the real webhook resumes an in-progress collection by passing
        # the live pending_confirmations row id
        result, _ = self._decide("FT318220260726001", history=history,
                                  pending_action_id=self.detail)
        dev = result.get("developer") or {}
        ics = dev.get("information_collection_status") or {}
        self.assertEqual(dev.get("selection_source"), "conversation_continuation")
        self.assertEqual(result["routing"]["type"], "API")             # reaches execution
        self.assertTrue(ics.get("is_complete"))
        self.assertEqual((ics.get("collected_parameters") or {}).get("ShipmentCode"),
                          "FT318220260726001")                          # continuation keeps the id


# ── C — an explicit identifier in the fresh message still executes ──

class P001_C_ExplicitCurrentTurnIdentifier(P001_Base):
    def test_bill_number_typed_now_is_used_not_the_memory_value(self):
        result, _ = self._decide("ขอเช็กพัสดุเดียว FT318220260726999")
        ics = (result.get("developer") or {}).get("information_collection_status") or {}
        self.assertEqual(result["routing"]["type"], "API")             # reaches execution
        self.assertTrue(ics.get("is_complete"))
        self.assertEqual((ics.get("collected_parameters") or {}).get("ShipmentCode"),
                          "FT318220260726999")                          # this turn's value, not memory


# ── D — generic shipment LIST unaffected ───────────────────────────

class P001_D_ListUnaffected(P001_Base):
    def test_list_still_executes_from_custcode_only(self):
        result, mock_req = self._decide("ขอดูรายการพัสดุทั้งหมด")
        # LIST requires only CustCode (identity), which still fills
        self.assertEqual(result["routing"]["type"], "API")
        dev = result.get("developer") or {}
        self.assertEqual(dev.get("selected_business_action"), "searchdatashipmentlist")


# ── unit — _apply_identifier_memory continuation gate ──────────────

class P001_ApplyIdentifierMemoryGate(unittest.TestCase):
    _ACTION = {
        "action_key": "searchdatashipment",
        "parameters": [
            {"name": "CustCode", "input_source": "customer_message", "required": True},
            {"name": "SecretCode", "input_source": "credential_store", "required": True},
            {"name": "ShipmentCode", "input_source": "customer_message", "required": True},
        ],
    }
    _CTX = {"cust_code": "FT3182", "last_shipment_code": "FT318220260726001",
            "last_order_code": "PO100420260819002", "last_tracking": "9822950447648"}

    def test_fresh_new_action_withholds_record_identifiers(self):
        got = _apply_identifier_memory(self._ACTION, {}, self._CTX, is_continuation=False)
        self.assertEqual(got.get("CustCode"), "FT3182")          # identity still fills
        self.assertNotIn("ShipmentCode", got)                    # record id withheld

    def test_continuation_fills_record_identifiers(self):
        got = _apply_identifier_memory(self._ACTION, {}, self._CTX, is_continuation=True)
        self.assertEqual(got.get("CustCode"), "FT3182")
        self.assertEqual(got.get("ShipmentCode"), "FT318220260726001")

    def test_default_is_continuation_true_backward_compatible(self):
        got = _apply_identifier_memory(self._ACTION, {}, self._CTX)
        self.assertEqual(got.get("ShipmentCode"), "FT318220260726001")

    def test_explicit_value_is_never_overridden(self):
        got = _apply_identifier_memory(self._ACTION, {"ShipmentCode": "FT318220260726999"},
                                        self._CTX, is_continuation=False)
        self.assertEqual(got["ShipmentCode"], "FT318220260726999")


if __name__ == "__main__":
    unittest.main()
