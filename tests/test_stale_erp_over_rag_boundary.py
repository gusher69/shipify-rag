"""P1 hotfix regression — stale ERP identifier memory must not override a
fresh informational / RAG turn (services/decision_engine.py).

Proven production defect (2026-09-01): a hot ERP customer whose profile
carries `last_business_action="getdatacustomer"` + a verified `cust_code`
had that identity-gated API action resurrected by
`_resolve_conversation_reference` / `detail_sibling` for ordinary
informational turns ("ขอเบอร์ติดต่อ", "ใช้คูปองยังไง", "แล้วจีนล่ะ"),
executed from Identifier Memory, and leaked the customer's own
wallet/phone/email.

These tests use a REAL webhook-shaped `customer_context` — lowercase
`cust_code` + `last_business_action` + `last_*` identifier-memory fields,
exactly what line_bot/webhook.py::_handle_message_via_decision_engine
builds from the user_profiles row. The earlier P1 suites only ever passed
a synthetic `{"CustCode": ...}` dict, which never activated this path.
Same _FakeSupabase / mocked-RAG convention as tests/test_decision_engine.py.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_business_action_registry import _FakeSupabase
from tests.test_decision_engine import _seed_action, _fake_playground_result
from services.business_action_registry import BusinessActionRegistry
from services.decision_engine import DecisionEngine

# The real user_profiles-row shape the webhook passes through as
# customer_context (lowercase keys; identifier memory + last_business_action).
STALE_ERP_CONTEXT = {
    "cust_code": "FT3182",
    "identity_confirmed": True,
    "last_business_action": "getdatacustomer",
    "last_order_code": "POS100820260824001",
    "last_shipment_code": "SP100820260817001",
    "last_tracking": "9822950447648",
    "erp_requests_count": 23,
    "segment": "hot",
    "conversation_tier": "hot",
}


class TestStaleErpOverRagBoundary(unittest.TestCase):
    def setUp(self):
        self.reg = BusinessActionRegistry(_FakeSupabase())
        # getdatacustomer — identity-gated customer-data lookup, CustCode
        # is the only required param (satisfiable from Identifier Memory),
        # response_mapping field keywords cover "เบอร์" and "คูปอง" so the
        # field-keyword branch of _resolve_conversation_reference fires.
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
            {"json_path": "$.data.CustEmail", "mapped_label": "อีเมล",
             "field_metadata": {"keywords": ["อีเมล", "email"]}},
        ])
        self.reg.upsert_execution(aid, {"endpoint": "https://example.test/customer", "http_method": "GET"})
        self.engine = DecisionEngine(self.reg._sb)
        self.engine.registry = self.reg

    def _decide(self, message, history=None, context_extra=None):
        ctx = {"developer_mode": True, "channel": "line",
               "customer_context": dict(STALE_ERP_CONTEXT)}
        if context_extra:
            ctx.update(context_extra)
        with patch("services.action_executor.requests.request",
                   return_value=MagicMock(status_code=200, json=lambda: {
                       "data": {"CustPhone": "0812345348", "Coupon": [], "CustEmail": "x@y.com"}})) as mock_req, \
             patch("services.playground_orchestrator.run_playground_turn",
                   return_value=_fake_playground_result(answer="RAG-ANSWER", confidence=0.9)):
            result = self.engine.decide(message, history=history or [], context=ctx)
        return result, mock_req

    # ── A/B/C: informational turns must NOT resurrect getdatacustomer ──
    def test_A_contact_request_with_stale_erp_context_stays_rag(self):
        result, mock_req = self._decide("ขอเบอร์ติดต่อ")
        self.assertNotEqual(result["routing"]["type"], "API")
        mock_req.assert_not_called()
        dev = result.get("developer") or {}
        self.assertNotEqual(dev.get("selection_source"), "conversation_reference")
        self.assertEqual(dev.get("stale_identity_gated_action_suppressed"),
                          "conversation_reference/informational_turn")

    def test_B_static_coupon_howto_with_stale_erp_context_stays_rag(self):
        result, mock_req = self._decide("ใช้คูปองยังไง")
        self.assertNotEqual(result["routing"]["type"], "API")
        mock_req.assert_not_called()
        self.assertEqual((result.get("developer") or {}).get("stale_identity_gated_action_suppressed"),
                          "conversation_reference/informational_turn")

    def test_C_warehouse_contrastive_followup_with_stale_erp_context_stays_rag(self):
        history = [
            {"role": "user", "content": "ขอที่อยู่โกดังหน่อย"},
            {"role": "assistant", "content": "ต้องการที่อยู่โกดังไทยหรือโกดังจีนคะ"},
            {"role": "user", "content": "ไทย"},
            {"role": "assistant", "content": "มีโกดังไทย 2 ที่นะคะ อ่อนนุช 46 ... บางนา ..."},
        ]
        result, mock_req = self._decide("แล้วจีนล่ะ", history=history)
        self.assertNotEqual(result["routing"]["type"], "API")
        mock_req.assert_not_called()
        self.assertEqual((result.get("developer") or {}).get("stale_identity_gated_action_suppressed"),
                          "conversation_reference/informational_turn")

    # ── D: valid ERP continuation must still work ──
    def test_D_custcode_reply_to_an_active_parameter_request_still_continues_erp(self):
        history = [
            {"role": "user", "content": "ขอดูข้อมูลลูกค้า"},
            {"role": "assistant", "content": "กรุณาแจ้งรหัสลูกค้าค่ะ"},
        ]
        result, _ = self._decide("FT3182", history=history)
        # Reaches the ERP path (bare identifier -> AMBIGUOUS, guard inert);
        # this seeded action then runs its own identity-verification step
        # before the HTTP call, which is not what this guard is about.
        self.assertEqual(result["routing"]["type"], "API")
        self.assertIsNone((result.get("developer") or {}).get("stale_identity_gated_action_suppressed"))

    # ── E: explicit self-referencing private request must still work ──
    def test_E_explicit_self_referencing_account_request_still_reaches_erp(self):
        result, _ = self._decide("ผมมีคูปองอะไรบ้าง")
        self.assertEqual(result["routing"]["type"], "API")
        self.assertEqual((result.get("developer") or {}).get("selection_source"), "conversation_reference")
        self.assertIsNone((result.get("developer") or {}).get("stale_identity_gated_action_suppressed"))

    def test_E2_explicit_profile_request_still_reaches_erp(self):
        result, _ = self._decide("ขอดูข้อมูลลูกค้าของผม")
        self.assertEqual(result["routing"]["type"], "API")
        self.assertIsNone((result.get("developer") or {}).get("stale_identity_gated_action_suppressed"))

    # ── F: general-knowledge question unaffected ──
    def test_F_general_knowledge_question_with_stale_erp_context_is_not_erp(self):
        result, mock_req = self._decide("จีนอยู่ทวีปอะไร")
        self.assertNotEqual(result["routing"]["type"], "API")
        mock_req.assert_not_called()

    # ── G/H/I: P1.2A informational transport comparison / multi-facet
    #    turns must stay RAG with a stale last_business_action (real LINE
    #    regression 2026-09-01 — the comparison form hit no question marker
    #    -> AMBIGUOUS, the multi-facet form tripped the multi-clause
    #    private-evidence branch -> PRIVATE_ACTION; both left an identity-
    #    gated ERP lookup eligible and leaked bill / shipment / tracking /
    #    total). classify_turn_intent now recognises them as
    #    SHIPIFY_INFORMATION via the P1.2A RequestSpec.
    def test_G_transport_rate_comparison_with_stale_erp_context_stays_rag(self):
        result, mock_req = self._decide("รถกับเรืออันไหนถูกกว่า")
        self.assertNotEqual(result["routing"]["type"], "API")
        mock_req.assert_not_called()

    def test_H_transport_duration_comparison_with_stale_erp_context_stays_rag(self):
        result, mock_req = self._decide("รถกับเรืออันไหนเร็วกว่า")
        self.assertNotEqual(result["routing"]["type"], "API")
        mock_req.assert_not_called()

    def test_I_transport_multi_facet_with_stale_erp_context_stays_rag(self):
        result, mock_req = self._decide("ทางรถกับทางเรือราคาเท่าไหร่ ใช้กี่วัน")
        self.assertNotEqual(result["routing"]["type"], "API")
        mock_req.assert_not_called()

    def test_J_private_transport_comparison_is_not_coerced_informational(self):
        # "ผม" present -> the P1.2A informational shortcut must NOT fire;
        # existing private/ambiguous handling is unchanged.
        from services.decision_engine import classify_turn_intent
        self.assertNotEqual(
            classify_turn_intent("ผมส่งของทางรถกับเรือ ของผมอันไหนถูกกว่า"), "SHIPIFY_INFORMATION")


if __name__ == "__main__":
    unittest.main()
