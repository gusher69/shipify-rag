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


# ── Fix 1.2 — a PUBLIC RAG answer's polite wording must not veto continuity

# The REAL production regression (HEAD 74e1bf3): the public RAG answer to
# "ค่าขนส่งคิดยังไง" ended with "...กรุณาแจ้งน้ำหนักและขนาด...". The Fix 1
# coercion was gated on `not _last_assistant_turn_requests_input(prev)`,
# a pure text-shape check ("แจ้ง" + "กรุณา/รบกวน"), so that polite public
# wording blocked continuity — the turn stayed AMBIGUOUS,
# _resolve_conversation_reference resurrected the remembered private
# `last_business_action=searchdatashipment`, same-customer Identifier
# Memory supplied ShipmentCode, and ERP executed.

_PUBLIC_CALC_ASKS_WEIGHT_KRUNA = (
    "ค่าขนส่งจะคำนวณจากทั้งน้ำหนักและปริมาตร (CBM) แล้วใช้ค่าที่สูงกว่าค่ะ "
    "หากต้องการประเมินค่าส่ง กรุณาแจ้งน้ำหนักและขนาดพัสดุมาได้เลยค่ะ")
_PUBLIC_CALC_ASKS_WEIGHT_RBKUAN = (
    "ค่าขนส่งคำนวณจากน้ำหนักจริงเทียบกับปริมาตรค่ะ "
    "รบกวนแจ้งน้ำหนักและขนาดกล่องมาด้วยนะคะ จะได้ประเมินค่าส่งให้")

# webhook-shaped context AFTER P8.2: verified FT3182, the stale cross-
# identity last_* identifiers already stripped, but last_business_action
# (not an account-scoped identifier) survives, and a same-customer
# ShipmentCode captured earlier this session is available.
_REAL_FAILURE_CONTEXT = {
    "cust_code": "FT3182",
    "identity_confirmed": True,
    "last_business_action": "searchdatashipment",
    "last_shipment_code": "FT318220260726001",   # this customer's own, from an earlier turn
    "segment": "hot",
    "conversation_tier": "hot",
}


class Fix12_PublicAnswerPoliteWordingDoesNotVetoContinuity(unittest.TestCase):
    def setUp(self):
        self.reg = BusinessActionRegistry(_FakeSupabase())
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
        self.reg.upsert_execution(self.detail, {"endpoint": "https://example.test/SearchDataShipment",
                                                 "http_method": "POST"})
        lst = _seed_action(self.reg, key="searchdatashipmentlist", action_type="API",
                            category="Customer Shipment Retrieval",
                            keywords=["ติดตามพัสดุ", "พัสดุ", "พัสดุล่าสุด", "shipment"])
        self.reg.replace_parameters(lst, [
            {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": True,
             "input_source": "customer_message"},
            {"name": "SecretCode", "display_name": "SecretCode", "required": True,
             "input_source": "credential_store"},
        ])
        self.reg.upsert_execution(lst, {"endpoint": "https://example.test/List", "http_method": "POST"})
        self.engine = DecisionEngine(self.reg._sb)
        self.engine.registry = self.reg

    def _decide(self, message, rag_answer):
        ctx = {"developer_mode": True, "channel": "line",
               "customer_context": dict(_REAL_FAILURE_CONTEXT)}
        history = [
            {"role": "user", "content": "ค่าขนส่งคิดยังไง"},
            {"role": "assistant", "content": rag_answer},
        ]
        with patch("services.action_executor.requests.request",
                   return_value=MagicMock(status_code=200, json=lambda: {
                       "data": {"Shipment": {"Code": "FT318220260726001", "Status": "รับเข้าที่จีน"}}})) as mock_req, \
             patch("services.playground_orchestrator.run_playground_turn",
                   return_value=_fake_playground_result(answer="PUBLIC-RAG-ANSWER", confidence=0.9)):
            result = self.engine.decide(message, history=history, context=ctx)
        return result, mock_req

    def test_1_exact_real_failure_kruna_jaeng_stays_public(self):
        result, mock_req = self._decide("54x12x43", _PUBLIC_CALC_ASKS_WEIGHT_KRUNA)
        dev = result.get("developer") or {}
        self.assertEqual(dev.get("turn_intent_coerced"), "public_clarification_continuity")
        self.assertNotEqual(result["routing"]["type"], "API")
        self.assertIsNone(dev.get("selected_business_action"))
        self.assertNotEqual(dev.get("selection_source"), "conversation_reference")
        mock_req.assert_not_called()                                   # no ERP
        self.assertNotIn(_SELF_VERIFY_ASK_PHONE_TEXT, result["reply"]["text"])
        self.assertNotIn("FT318220260726001", result["reply"]["text"])  # no shipment resurrection

    def test_2_same_bug_class_with_rbkuan_jaeng_wording(self):
        result, mock_req = self._decide("54x12x43", _PUBLIC_CALC_ASKS_WEIGHT_RBKUAN)
        dev = result.get("developer") or {}
        self.assertEqual(dev.get("turn_intent_coerced"), "public_clarification_continuity")
        self.assertNotEqual(result["routing"]["type"], "API")
        mock_req.assert_not_called()
        self.assertNotIn("FT318220260726001", result["reply"]["text"])

    def test_3_explicit_new_business_action_still_wins_after_polite_public_answer(self):
        # Fix 1.1 preserved: an explicitly-phrased new request is NOT coerced
        result, mock_req = self._decide("ขอเช็กพัสดุเดียวครับ", _PUBLIC_CALC_ASKS_WEIGHT_KRUNA)
        dev = result.get("developer") or {}
        self.assertIsNone(dev.get("turn_intent_coerced"))
        # DETAIL owns the turn (P8.3.1); no plain public-clarification swallow
        self.assertIn(dev.get("selected_business_action"), ("searchdatashipment", None))
        self.assertNotEqual(result["routing"]["type"], "RAG")


class Fix12_SelfVerificationContinuationNotCoerced(unittest.TestCase):
    """A reply to the self-service identity-verification prompt is a real
    continuation (recognised structurally, verbatim match), so the
    Fix 1.2 structural veto still holds it back from public coercion."""
    def setUp(self):
        self.reg = BusinessActionRegistry(_FakeSupabase())
        aid = _seed_action(self.reg, key="getdatacustomer", action_type="API",
                            category="Customer Data Retrieval", keywords=["ข้อมูลลูกค้า"])
        self.reg.replace_parameters(aid, [
            {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": True,
             "input_source": "customer_message"},
        ])
        self.reg.upsert_execution(aid, {"endpoint": "https://example.test/customer", "http_method": "GET"})
        self.engine = DecisionEngine(self.reg._sb)
        self.engine.registry = self.reg

    def test_phone_reply_to_self_verify_prompt_is_not_coerced_to_public(self):
        ctx = {"developer_mode": True, "channel": "line",
               "customer_context": {"cust_code": "FT3182", "identity_confirmed": True,
                                    "last_business_action": "getdatacustomer"}}
        history = [
            {"role": "user", "content": "ขอดูข้อมูลลูกค้าของผม"},
            {"role": "assistant", "content": _SELF_VERIFY_ASK_PHONE_TEXT},
        ]
        with patch("services.action_executor.requests.request",
                   return_value=MagicMock(status_code=200, json=lambda: {"data": {}})), \
             patch("services.playground_orchestrator.run_playground_turn",
                   return_value=_fake_playground_result(answer="RAG", confidence=0.9)):
            result = self.engine.decide("0812345678", history=history, context=ctx)
        self.assertIsNone((result.get("developer") or {}).get("turn_intent_coerced"))


# ── Fix 1.3 — a coerced public turn keeps its TASK semantics ────────
#
# After Fix 1.2 stopped the private-action resurrection, "54x12x43" was
# still trapped by the Decision Engine's bare-identifier guard (it passes
# _validate_generic_identifier) and answered "ต้องการตรวจสอบข้อมูลอะไรของ
# 54x12x43 คะ เช่น ข้อมูลลูกค้า คำสั่งซื้อ หรือพัสดุ". Fix 1.3: a turn
# already coerced by public_clarification_continuity skips both bare-
# identifier guards and reaches the shared RAG pipeline, where the
# EXISTING Active Slot-Filling Flow (rag/slot_filling_flow.py, already
# wired into playground_orchestrator) recognises the structural value.

_DIMENSION_REQUEST_ANSWER = (
    "ค่าขนส่งคำนวณจากน้ำหนักจริงเทียบกับปริมาตรค่ะ "
    "กรุณาแจ้งน้ำหนักและขนาด กว้าง × ยาว × สูง")

_GENERIC_AMBIGUITY_MARKER = "ต้องการตรวจสอบข้อมูลอะไรของ"


class Fix13_DecisionEngineRoutesCoercedPublicTurnToRag(unittest.TestCase):
    def setUp(self):
        self.reg = BusinessActionRegistry(_FakeSupabase())
        d = _seed_action(self.reg, key="searchdatashipment", action_type="API",
                          category="Customer Shipment Retrieval",
                          keywords=["เลขบิลขนส่ง", "พัสดุเดียว", "รายละเอียดพัสดุ"])
        self.reg.replace_parameters(d, [
            {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": True,
             "input_source": "customer_message"},
            {"name": "SecretCode", "display_name": "SecretCode", "required": True,
             "input_source": "credential_store"},
            {"name": "ShipmentCode", "display_name": "เลขที่บิลขนส่ง", "required": True,
             "input_source": "customer_message"},
        ])
        self.reg.upsert_execution(d, {"endpoint": "https://example.test/s", "http_method": "POST"})
        g = _seed_action(self.reg, key="getdatacustomer", action_type="API",
                          category="Customer Data Retrieval", keywords=["ข้อมูลลูกค้า"])
        self.reg.replace_parameters(g, [
            {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": True,
             "input_source": "customer_message"},
        ])
        self.reg.upsert_execution(g, {"endpoint": "https://example.test/c", "http_method": "GET"})
        self.engine = DecisionEngine(self.reg._sb)
        self.engine.registry = self.reg

    def _decide(self, message):
        ctx = {"developer_mode": True, "channel": "line",
               "customer_context": {"cust_code": "FT3182", "identity_confirmed": True,
                                    "last_business_action": "searchdatashipment",
                                    "last_shipment_code": "FT318220260726001"}}
        history = [
            {"role": "user", "content": "ค่าขนส่งคิดยังไง"},
            {"role": "assistant", "content": _DIMENSION_REQUEST_ANSWER},
        ]
        with patch("services.action_executor.requests.request",
                   return_value=MagicMock(status_code=200, json=lambda: {"data": {}})) as mock_req, \
             patch("services.playground_orchestrator.run_playground_turn",
                   return_value=_fake_playground_result(answer="RAG-PIPELINE-REACHED", confidence=0.9)):
            result = self.engine.decide(message, history=history, context=ctx)
        return result, mock_req

    def test_1_real_failure_reaches_rag_pipeline_not_identifier_ambiguity(self):
        result, mock_req = self._decide("54x12x43")
        dev = result.get("developer") or {}
        self.assertEqual(dev.get("turn_intent_coerced"), "public_clarification_continuity")
        self.assertEqual(result["reply"]["text"], "RAG-PIPELINE-REACHED")   # shared RAG pipeline ran
        self.assertNotIn(_GENERIC_AMBIGUITY_MARKER, result["reply"]["text"])
        self.assertNotEqual(result["routing"]["type"], "API")
        self.assertIsNone(dev.get("selected_business_action"))
        mock_req.assert_not_called()

    def test_2_explicit_new_business_action_still_wins(self):
        result, _ = self._decide("ขอเช็กพัสดุเดียวครับ")
        dev = result.get("developer") or {}
        self.assertIsNone(dev.get("turn_intent_coerced"))
        self.assertNotEqual(result["reply"]["text"], "RAG-PIPELINE-REACHED")

    def test_3_new_private_intent_still_wins(self):
        result, _ = self._decide("ยอด Wallet ของผมเท่าไหร่")
        self.assertEqual(result["routing"]["type"], "API")
        self.assertIsNone((result.get("developer") or {}).get("turn_intent_coerced"))


class Fix13_ActiveSlotFillingFlowRecognisesTaskData(unittest.TestCase):
    """The existing rag/slot_filling_flow resolver (reached once Fix 1.3
    routes the coerced turn to the RAG pipeline) understands the customer's
    reply as dimensions / weight against the previous public request and
    asks only for what is still missing — deterministic, no LLM."""
    _DIM_HISTORY = [
        {"role": "user", "content": "ค่าขนส่งคิดยังไง"},
        {"role": "assistant", "content": _DIMENSION_REQUEST_ANSWER},
    ]

    def _resolve(self, reply):
        from rag.slot_filling_flow import resolve_slot_filling_turn
        return resolve_slot_filling_turn(reply, self._DIM_HISTORY)

    def test_dimensions_recognised_and_next_field_requested(self):
        for variant in ("54x12x43", "54 x 12 x 43", "54*12*43", "กว้าง 54 ยาว 12 สูง 43"):
            r = self._resolve(variant)
            self.assertIsNotNone(r, variant)
            self.assertFalse(r["flow_complete"], variant)
            self.assertIn("weight", r["missing_slots"], variant)
            self.assertIn("ได้รับขนาด", r["message"], variant)      # understood as dimensions
            self.assertIn("น้ำหนัก", r["message"], variant)         # asks for the still-missing field

    def test_weight_recognised(self):
        for variant in ("12 kg", "12 กก.", "12 กิโล"):
            r = self._resolve(variant)
            self.assertIsNotNone(r, variant)
            self.assertIn("ได้รับน้ำหนัก", r["message"], variant)


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
