"""Customer UAT Fix 1 — Public vs Private boundary + clarification.

Confirmed REAL customer UAT defect: after a PUBLIC informational answer
("ค่าขนส่งคิดยังไง" -> the AI explains dimensions / CBM), the customer's
follow-up "54x12x43" (task-specific dimensions for a public shipping
calculation) was routed into an identity-gated Business Action, whose
Authorization Gate then asked the customer for the phone number linked
to their account.

Bug class: public / task-specific missing information is mis-read as
missing customer IDENTITY. The fix (services/decision_engine.py) coerces
such a bare clarification reply — no question particle, no self-
reference, landing right after an informational (non-input-request)
assistant turn — to SHIPIFY_INFORMATION, so the existing exclude_private
mechanism keeps it on the public RAG / clarification path.

Same _FakeSupabase / mocked-RAG convention as
tests/test_stale_erp_over_rag_boundary.py. The customer test phrases
appear in FIXTURES ONLY — never in production source.
"""
import inspect
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_business_action_registry import _FakeSupabase
from tests.test_decision_engine import _seed_action, _fake_playground_result
from services.business_action_registry import BusinessActionRegistry
from services.decision_engine import DecisionEngine, _SELF_VERIFY_ASK_PHONE_TEXT

# A real webhook-shaped customer_context: verified/hot ERP customer with
# Identifier Memory + last_business_action, exactly what
# line_bot/webhook.py builds from the user_profiles row. Present in every
# scenario so the PUBLIC cases have to stay public *despite* a private
# action being trivially satisfiable from memory.
CUSTOMER_CONTEXT = {
    "cust_code": "FT3182",
    "identity_confirmed": True,
    "last_business_action": "getdatacustomer",
    "last_order_code": "POS100820260824001",
    "last_shipment_code": "SP100820260817001",
    "last_tracking": "9822950447648",
    "segment": "hot",
    "conversation_tier": "hot",
}

# A prior PUBLIC informational answer — an ordinary RAG reply, NOT a
# parameter / confirmation / identity-verification request.
_CBM_ANSWER = ("การคิดค่าขนส่งใช้ปริมาตรกว้าง x ยาว x สูง หารด้วย 5000 "
               "เทียบกับน้ำหนักจริง แล้วเลือกค่าที่มากกว่าค่ะ")


class _Base(unittest.TestCase):
    def setUp(self):
        self.reg = BusinessActionRegistry(_FakeSupabase())
        # getdatacustomer — identity-gated customer-data lookup; CustCode
        # is the only required param and is satisfiable from Identifier
        # Memory, so nothing but the public/private classification keeps
        # it from winning every turn.
        aid = _seed_action(self.reg, key="getdatacustomer", action_type="API",
                            category="Customer Data Retrieval", keywords=["ข้อมูลลูกค้า"])
        self.reg.replace_parameters(aid, [
            {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": True,
             "input_source": "customer_message"},
        ])
        self.reg.replace_response_mapping(aid, [
            {"json_path": "$.data.CustPhone", "mapped_label": "เบอร์โทร",
             "field_metadata": {"keywords": ["เบอร์", "โทร", "phone"]}},
            {"json_path": "$.data.Coupon", "mapped_label": "คูปอง",
             "field_metadata": {"keywords": ["คูปอง", "coupon"]}},
            {"json_path": "$.data.PurchaseWallet", "mapped_label": "ยอดเงิน Wallet",
             "field_metadata": {"keywords": ["wallet", "ยอดเงิน", "เหลือ"]}},
        ])
        self.reg.upsert_execution(aid, {"endpoint": "https://example.test/customer", "http_method": "GET"})
        self.engine = DecisionEngine(self.reg._sb)
        self.engine.registry = self.reg

    def _decide(self, message, history=None):
        ctx = {"developer_mode": True, "channel": "line",
               "customer_context": dict(CUSTOMER_CONTEXT)}
        with patch("services.action_executor.requests.request",
                   return_value=MagicMock(status_code=200, json=lambda: {
                       "data": {"CustPhone": "0812345348", "Coupon": [], "PurchaseWallet": "552.61"}})) as mock_req, \
             patch("services.playground_orchestrator.run_playground_turn",
                   return_value=_fake_playground_result(answer="PUBLIC-RAG-ANSWER", confidence=0.9)):
            result = self.engine.decide(message, history=history or [], context=ctx)
        return result, mock_req


# ── TEST 1 — the confirmed production failure ────────────────────────

class Test1_PublicShippingCalculationClarification(_Base):
    def _run(self):
        history = [
            {"role": "user", "content": "ค่าขนส่งคิดยังไง"},
            {"role": "assistant", "content": _CBM_ANSWER},
        ]
        return self._decide("54x12x43", history=history)

    def test_stays_public_no_erp_no_phone_verification(self):
        result, mock_req = self._run()
        self.assertNotEqual(result["routing"]["type"], "API")
        mock_req.assert_not_called()
        self.assertNotIn(_SELF_VERIFY_ASK_PHONE_TEXT, result["reply"]["text"])
        self.assertNotIn("เบอร์", result["reply"]["text"])  # no registered-phone ask at all

    def test_coercion_marker_present_in_trace(self):
        result, _ = self._run()
        self.assertEqual((result.get("developer") or {}).get("turn_intent_coerced"),
                          "public_clarification_continuity")


# ── TEST 2 / 3 — ambiguous & service-availability public questions ───

class Test2_3_AmbiguousPublicQuestions(_Base):
    def test_2_bulk_order_question_stays_public(self):
        result, mock_req = self._decide("สั่งเยอะได้ไหม")
        self.assertNotEqual(result["routing"]["type"], "API")
        mock_req.assert_not_called()
        self.assertNotIn(_SELF_VERIFY_ASK_PHONE_TEXT, result["reply"]["text"])

    def test_3_tax_invoice_availability_stays_public(self):
        result, mock_req = self._decide("ออกใบกำกับได้ไหม")
        self.assertNotEqual(result["routing"]["type"], "API")
        mock_req.assert_not_called()


# ── TEST 4/5/6 — established public RAG cases (regression) ───────────

class Test4_5_6_PublicRagCases(_Base):
    def test_4_generic_road_duration_is_rag(self):
        result, mock_req = self._decide("ทางรถใช้เวลากี่วัน")
        self.assertNotEqual(result["routing"]["type"], "API")
        mock_req.assert_not_called()

    def test_5_company_contact_is_rag_not_registered_phone(self):
        result, mock_req = self._decide("ขอเบอร์ติดต่อ")
        self.assertNotEqual(result["routing"]["type"], "API")
        mock_req.assert_not_called()

    def test_6_static_coupon_instructions_is_rag(self):
        result, mock_req = self._decide("ใช้คูปองยังไง")
        self.assertNotEqual(result["routing"]["type"], "API")
        mock_req.assert_not_called()


# ── TEST 7/8/9 — PRIVATE account questions stay protected ───────────

class Test7_8_9_PrivateErpCasesUnchanged(_Base):
    def test_7_coupon_ownership_is_private(self):
        result, _ = self._decide("ผมมีคูปองอะไรบ้าง")
        self.assertEqual(result["routing"]["type"], "API")
        self.assertIsNone((result.get("developer") or {}).get("turn_intent_coerced"))

    def test_8_wallet_balance_is_private(self):
        result, _ = self._decide("ยอด Wallet ของผมเท่าไหร่")
        self.assertEqual(result["routing"]["type"], "API")
        self.assertIsNone((result.get("developer") or {}).get("turn_intent_coerced"))

    def test_9_registered_phone_is_private(self):
        result, _ = self._decide("เบอร์ที่ผมลงทะเบียนไว้คืออะไร")
        self.assertEqual(result["routing"]["type"], "API")
        self.assertIsNone((result.get("developer") or {}).get("turn_intent_coerced"))


# ── Guard — a reply that continues a genuine PARAMETER ASK is not coerced

class Guard_ParameterAskContinuationStillReachesErp(_Base):
    def test_bare_code_after_an_identity_parameter_request_still_reaches_erp(self):
        history = [
            {"role": "user", "content": "ขอดูข้อมูลลูกค้า"},
            {"role": "assistant", "content": "กรุณาแจ้งรหัสลูกค้าค่ะ"},
        ]
        result, _ = self._decide("FT3182", history=history)
        self.assertEqual(result["routing"]["type"], "API")
        self.assertIsNone((result.get("developer") or {}).get("turn_intent_coerced"))


# ── TEST B — explicit NEW Business Action after a public answer ─────
#
# Fix 1.1 regression guard: an explicitly-phrased new ERP request that
# merely lacks a Thai question particle (classify_turn_intent -> AMBIGUOUS)
# must NOT be swallowed by public continuity — its own configured
# Business Action evidence owns the turn.

_PUBLIC_ROAD_ANSWER = "ระยะเวลาขนส่งทางรถจากจีนถึงไทยประมาณ 7–10 วันค่ะ"


class TestB_ExplicitNewBusinessActionAfterPublicAnswer(unittest.TestCase):
    def setUp(self):
        self.reg = BusinessActionRegistry(_FakeSupabase())
        # DETAIL — configures a specific-record discriminator keyword;
        # LIST — configures the generic substring. P8.3.1's shared
        # disambiguation resolves the pair to DETAIL.
        self.detail = _seed_action(
            self.reg, key="searchdatashipment", action_type="API",
            category="Customer Shipment Retrieval",
            keywords=["เลขบิลขนส่ง", "พัสดุเดียว", "shipment detail", "รายละเอียดพัสดุ"])
        self.reg.replace_parameters(self.detail, [
            {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": True,
             "input_source": "customer_message"},
            {"name": "SecretCode", "display_name": "SecretCode", "required": True,
             "input_source": "credential_store"},
            {"name": "ShipmentCode", "display_name": "เลขที่บิลขนส่ง", "required": True,
             "input_source": "customer_message"},
        ])
        self.reg.upsert_execution(self.detail, {"endpoint": "https://example.test/s", "http_method": "GET"})
        lst = _seed_action(self.reg, key="searchdatashipmentlist", action_type="API",
                            category="Customer Shipment Retrieval",
                            keywords=["ติดตามพัสดุ", "พัสดุ", "พัสดุล่าสุด", "สถานะรับเข้าไทย", "shipment"])
        self.reg.replace_parameters(lst, [
            {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": True,
             "input_source": "customer_message"},
            {"name": "SecretCode", "display_name": "SecretCode", "required": True,
             "input_source": "credential_store"},
        ])
        self.reg.upsert_execution(lst, {"endpoint": "https://example.test/l", "http_method": "GET"})
        self.engine = DecisionEngine(self.reg._sb)
        self.engine.registry = self.reg

    def _run(self, message):
        ctx = {"developer_mode": True, "channel": "line",
               "customer_context": {"cust_code": "FT3182", "identity_confirmed": True,
                                    "last_business_action": "getdatacustomer"}}
        history = [
            {"role": "user", "content": "ทางรถใช้เวลากี่วัน"},
            {"role": "assistant", "content": _PUBLIC_ROAD_ANSWER},
        ]
        with patch("services.action_executor.requests.request",
                   return_value=MagicMock(status_code=200, json=lambda: {"data": {}})) as mock_req, \
             patch("services.playground_orchestrator.run_playground_turn",
                   return_value=_fake_playground_result(answer="PUBLIC-RAG-ANSWER", confidence=0.9)):
            result = self.engine.decide(message, history=history, context=ctx)
        return result, mock_req

    def test_new_shipment_action_is_not_coerced_and_collects_its_identifier(self):
        result, mock_req = self._run("ขอเช็กพัสดุเดียวครับ")
        dev = result.get("developer") or {}
        # continuity did NOT fire — the message owns its turn
        self.assertIsNone(dev.get("turn_intent_coerced"))
        # DETAIL selected, now collecting the missing record identifier
        self.assertEqual(result["routing"]["type"], "WORKFLOW")
        self.assertIn("บิลขนส่ง", result["reply"]["text"])
        mock_req.assert_not_called()   # no ERP call until the slot exists


# ── TEST C — synthetic portability (no Shipify names) ──────────────

class TestC_SyntheticPortabilityNewActionAfterPublicAnswer(unittest.TestCase):
    def setUp(self):
        self.reg = BusinessActionRegistry(_FakeSupabase())
        self.x = _seed_action(self.reg, key="action_x", action_type="API",
                               category="Records", keywords=["specific_record"])
        self.reg.replace_parameters(self.x, [
            {"name": "CustCode", "display_name": "c", "required": True,
             "input_source": "customer_message"},
            {"name": "RecordId", "display_name": "record id", "required": True,
             "input_source": "customer_message"},
        ])
        self.reg.upsert_execution(self.x, {"endpoint": "https://example.test/x", "http_method": "GET"})
        y = _seed_action(self.reg, key="action_y", action_type="API",
                          category="Misc", keywords=["something_unrelated"])
        self.reg.replace_parameters(y, [
            {"name": "CustCode", "display_name": "c", "required": True,
             "input_source": "customer_message"},
        ])
        self.reg.upsert_execution(y, {"endpoint": "https://example.test/y", "http_method": "GET"})
        self.engine = DecisionEngine(self.reg._sb)
        self.engine.registry = self.reg

    def test_configured_keyword_owns_the_turn_after_a_public_answer(self):
        ctx = {"developer_mode": True, "channel": "line",
               "customer_context": {"cust_code": "FT3182", "identity_confirmed": True}}
        history = [
            {"role": "user", "content": "ทางรถใช้เวลากี่วัน"},
            {"role": "assistant", "content": _PUBLIC_ROAD_ANSWER},
        ]
        with patch("services.action_executor.requests.request",
                   return_value=MagicMock(status_code=200, json=lambda: {"data": {}})) as mock_req, \
             patch("services.playground_orchestrator.run_playground_turn",
                   return_value=_fake_playground_result(answer="PUBLIC-RAG-ANSWER", confidence=0.9)):
            result = self.engine.decide("please specific_record", history=history, context=ctx)
        dev = result.get("developer") or {}
        self.assertIsNone(dev.get("turn_intent_coerced"))
        self.assertEqual(result["routing"]["type"], "WORKFLOW")   # action_x owns it, collecting RecordId
        mock_req.assert_not_called()


# ── Guard — the fix carries no hardcoded customer phrase ────────────

class Guard_NoPhraseHardcode(unittest.TestCase):
    def test_coercion_block_has_no_fixture_phrase(self):
        import services.decision_engine as de
        src = inspect.getsource(de.DecisionEngine.decide)
        block = src.split("Public Clarification Continuity", 1)[1].split("developer_trace[\"turn_intent\"]", 1)[0]
        for banned in ("54x12x43", "สั่งเยอะ", "ค่าขนส่งคิดยังไง", "ออกใบกำกับ",
                       "ใบกำกับ", "ใช้คูปอง", "ทางรถ", "getdatacustomer"):
            self.assertNotIn(banned, block)


if __name__ == "__main__":
    unittest.main()
