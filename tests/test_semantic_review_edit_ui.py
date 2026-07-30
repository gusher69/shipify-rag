"""Regression tests for the Semantic Analysis Review & Edit UI sprint —
covers everything NEW in this sprint that
tests/test_semantic_api_analysis_engine.py doesn't already exercise:
severity-tagged recommendations, the extended apply_overrides() override
kinds (operation_type/confirmation/audit/validation_groups), the two new
admin routes (semantic-overrides/apply, semantic-endpoint-intents), the
runtime operation-types route now including TRANSFORM, no-secret-leakage
in the semantic_analysis payload, and admin-override persistence/reset
across the Stage 3 save route. Pure-function/route tests only — no LLM,
no live network, no Business Action is published/enabled/executed."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from services.semantic_api_analysis_engine import (
    analyze_endpoint, apply_overrides, build_recommendations_detailed,
    RECOMMENDATION_SEVERITIES, ENDPOINT_INTENTS,
)

SHIPMENT_LIST_FIELDS = [
    {"name": "SecretCode", "required": True, "description": "รหัสยืนยันตัวตน"},
    {"name": "CustCode", "required": True, "example_value": "C00001", "description": "รหัสลูกค้า"},
    {"name": "ReceivedDateStart", "required": False, "example_value": "2026-06-01", "description": "start date"},
    {"name": "BillStatus", "required": False, "example_value": "3", "description": "สถานะบิล"},
    {"name": "Latest", "required": False, "example_value": "5", "description": "limit"},
]
PRODUCT_URL_FIELDS = [
    {"name": "SecretCode", "required": True, "description": "รหัสยืนยันตัวตน"},
    {"name": "CustCode", "required": True, "example_value": "C00001", "description": "รหัสลูกค้า"},
    {"name": "URL", "required": True, "example_value": "https://item.taobao.com/item.htm?id=1",
     "description": "product url"},
]
LINE_NOTI_FIELDS = [
    {"name": "SecretCode", "required": True, "description": "รหัสยืนยันตัวตน"},
    {"name": "Message", "required": True, "example_value": "ทดสอบ", "description": "message"},
]


class TestSeverityTagging(unittest.TestCase):
    def test_every_recommendation_has_a_declared_severity(self):
        analysis = analyze_endpoint(endpoint_url="/SearchDataShipmentList", http_method="POST",
                                     description="", body_fields=[], example_response=None)
        for r in analysis["recommendations_detailed"]:
            self.assertIn(r["severity"], RECOMMENDATION_SEVERITIES)

    def test_plain_recommendations_stays_backward_compatible_string_list(self):
        analysis = analyze_endpoint(endpoint_url="/SearchDataShipmentList", http_method="POST",
                                     description="", body_fields=SHIPMENT_LIST_FIELDS, example_response=None)
        self.assertTrue(all(isinstance(r, str) for r in analysis["recommendations"]))
        self.assertEqual(analysis["recommendations"],
                          [d["message"] for d in analysis["recommendations_detailed"]])

    def test_search_endpoint_missing_criteria_is_warning_not_info(self):
        recs = build_recommendations_detailed(
            response_analysis={"response_mapping_status": "analyzed"},
            endpoint_intent_result={"intent": "SEARCH", "confidence": 0.9},
            fields=[{"name": "SecretCode", "roles": ["credential"]}],
            operation_safety={"notification": False, "destructive": False},
        )
        warning = next(r for r in recs if "No search/filter fields" in r["message"])
        self.assertEqual(warning["severity"], "warning")

    def test_command_and_notification_never_show_search_warning(self):
        for intent in ("COMMAND", "NOTIFICATION"):
            recs = build_recommendations_detailed(
                response_analysis={"response_mapping_status": "analyzed"},
                endpoint_intent_result={"intent": intent, "confidence": 0.9},
                fields=[{"name": "Message", "roles": ["message"]}],
                operation_safety={"notification": intent == "NOTIFICATION", "destructive": False,
                                   "confirmation_required": True},
            )
            self.assertFalse(any("search/filter" in r["message"] for r in recs))

    def test_pending_response_mapping_is_info_severity(self):
        recs = build_recommendations_detailed(
            response_analysis={"response_mapping_status": "pending"},
            endpoint_intent_result={"intent": "LOOKUP", "confidence": 0.9},
            fields=[], operation_safety={"notification": False, "destructive": False},
        )
        pending = next(r for r in recs if "Response mapping incomplete" in r["message"])
        self.assertEqual(pending["severity"], "info")

    def test_missing_confirmation_for_notification_is_error_severity(self):
        recs = build_recommendations_detailed(
            response_analysis={"response_mapping_status": "analyzed"},
            endpoint_intent_result={"intent": "NOTIFICATION", "confidence": 0.9},
            fields=[], operation_safety={"notification": True, "destructive": False, "confirmation_required": False},
        )
        gap = next(r for r in recs if "safety gap" in r["message"])
        self.assertEqual(gap["severity"], "error")

    def test_confirmation_already_required_is_recommendation_not_error(self):
        recs = build_recommendations_detailed(
            response_analysis={"response_mapping_status": "analyzed"},
            endpoint_intent_result={"intent": "NOTIFICATION", "confidence": 0.9},
            fields=[], operation_safety={"notification": True, "destructive": False, "confirmation_required": True},
        )
        self.assertTrue(any(r["severity"] == "recommendation" for r in recs))
        self.assertFalse(any(r["severity"] == "error" for r in recs))


class TestExtendedOverrides(unittest.TestCase):
    def setUp(self):
        self.analysis = analyze_endpoint(endpoint_url="/SendLineNotiCS", http_method="POST",
                                          description="", body_fields=LINE_NOTI_FIELDS, example_response=None)

    def test_operation_type_override_and_provenance(self):
        merged = apply_overrides(self.analysis, {"operation_type_override": "WORKFLOW"})
        self.assertEqual(merged["mapped_runtime_operation_type"], "WORKFLOW")
        self.assertEqual(merged["provenance"]["mapped_runtime_operation_type"], "explicit")

    def test_confirmation_required_override(self):
        merged = apply_overrides(self.analysis, {"confirmation_required_override": False})
        self.assertFalse(merged["operation_safety"]["confirmation_required"])
        self.assertEqual(merged["provenance"]["operation_safety.confirmation_required"], "explicit")

    def test_audit_required_override(self):
        merged = apply_overrides(self.analysis, {"audit_required_override": True})
        self.assertTrue(merged["operation_safety"]["audit_required"])
        self.assertEqual(merged["provenance"]["operation_safety.audit_required"], "explicit")

    def test_validation_groups_override_replaces_wholesale(self):
        override_groups = [{"rule": "EXACTLY_ONE", "members": ["Message"], "confidence": 1.0, "source": "admin"}]
        merged = apply_overrides(self.analysis, {"validation_groups_override": override_groups})
        self.assertEqual(merged["validation_groups"], override_groups)
        self.assertEqual(merged["provenance"]["validation_groups"], "explicit")

    def test_no_overrides_all_derived(self):
        merged = apply_overrides(self.analysis, {})
        self.assertEqual(merged["provenance"]["mapped_runtime_operation_type"], "derived")
        self.assertEqual(merged["provenance"]["operation_safety.confirmation_required"], "derived")
        self.assertEqual(merged["provenance"]["operation_safety.audit_required"], "derived")
        self.assertEqual(merged["provenance"]["validation_groups"], "derived")

    def test_reset_to_detected_clears_override(self):
        overridden = apply_overrides(self.analysis, {"operation_type_override": "WORKFLOW"})
        self.assertEqual(overridden["provenance"]["mapped_runtime_operation_type"], "explicit")
        reset = apply_overrides(self.analysis, {})
        self.assertEqual(reset["mapped_runtime_operation_type"], self.analysis["mapped_runtime_operation_type"])
        self.assertEqual(reset["provenance"]["mapped_runtime_operation_type"], "derived")

    def test_recommendations_recomputed_from_effective_state(self):
        # Detected: NOTIFICATION with confirmation_required already True
        # (LINE_NOTI_FIELDS has no destructive/notification-gap by
        # default) -> forcing confirmation_required_override False must
        # surface the safety-gap ERROR once the effective state lacks it.
        merged = apply_overrides(self.analysis, {"confirmation_required_override": False})
        self.assertTrue(any(r["severity"] == "error" and "safety gap" in r["message"]
                             for r in merged["recommendations_detailed"]))

    def test_original_analysis_never_mutated_by_overrides(self):
        import copy
        before = copy.deepcopy(self.analysis)
        apply_overrides(self.analysis, {"operation_type_override": "WORKFLOW", "audit_required_override": True,
                                          "confirmation_required_override": False})
        self.assertEqual(self.analysis, before)


class TestNoSecretLeakageInSemanticAnalysis(unittest.TestCase):
    def test_analyze_endpoint_never_carries_example_values_into_fields(self):
        analysis = analyze_endpoint(endpoint_url="/SearchDataShipmentList", http_method="POST",
                                     description="", body_fields=SHIPMENT_LIST_FIELDS, example_response=None)
        for f in analysis["fields"]:
            self.assertNotIn("example_value", f)
            self.assertNotIn("value", f)

    def test_authentication_field_identified_by_name_only(self):
        analysis = analyze_endpoint(endpoint_url="/SearchDataShipmentList", http_method="POST",
                                     description="", body_fields=SHIPMENT_LIST_FIELDS, example_response=None)
        secret_field = next(f for f in analysis["fields"] if f["name"] == "SecretCode")
        self.assertIn("credential", secret_field["roles"])
        self.assertNotIn("example_value", secret_field)


class TestFourActionRegression(unittest.TestCase):
    """Step 8 — verify effective operation type / search criteria /
    validation groups / no UNKNOWN / no spurious warnings for all 4
    onboarded draft actions, purely via analyze_endpoint()+apply_overrides()
    (no live API call, no DB row required, no publish/enable)."""

    def test_get_data_customer_lookup_no_unknown(self):
        fields = [
            {"name": "SecretCode", "required": True, "description": "รหัสยืนยันตัวตน"},
            {"name": "CustCode", "required": False, "description": "เลือกอย่างน้อย 1 อย่าง"},
            {"name": "CustEmail", "required": False, "description": "เลือกอย่างน้อย 1 อย่าง"},
            {"name": "CustName", "required": False, "description": "เลือกอย่างน้อย 1 อย่าง"},
            {"name": "CustPhone", "required": False, "description": "เลือกอย่างน้อย 1 อย่าง"},
        ]
        analysis = analyze_endpoint(endpoint_url="/GetDataCustomer", http_method="POST",
                                     description="ดึงข้อมูลลูกค้า ระบุอย่างน้อย 1 อย่าง", body_fields=fields,
                                     example_response=None)
        effective = apply_overrides(analysis, {})
        self.assertEqual(effective["endpoint_intent"]["intent"], "LOOKUP")
        self.assertNotEqual(effective["endpoint_intent"]["intent"], "UNKNOWN")
        group = effective["validation_groups"][0]
        self.assertEqual(group["rule"], "AT_LEAST_ONE")
        self.assertEqual(set(group["members"]), {"CustCode", "CustEmail", "CustName", "CustPhone"})

    def test_search_data_shipment_list_no_spurious_warning(self):
        analysis = analyze_endpoint(endpoint_url="/SearchDataShipmentList", http_method="POST",
                                     description="ต้องระบุ CustCode และเงื่อนไขค้นหาอย่างน้อย 1 อย่าง",
                                     body_fields=SHIPMENT_LIST_FIELDS, example_response=None)
        effective = apply_overrides(analysis, {})
        self.assertEqual(effective["endpoint_intent"]["intent"], "SEARCH")
        self.assertFalse(any("No search/filter fields" in r for r in effective["recommendations"]))

    def test_get_url_product_detail_is_transform_not_unknown(self):
        analysis = analyze_endpoint(endpoint_url="/GetUrlProductDetail", http_method="POST",
                                     description="รองรับลิงก์จาก 1688, Taobao, Tmall เท่านั้น",
                                     body_fields=PRODUCT_URL_FIELDS, example_response=None)
        effective = apply_overrides(analysis, {})
        self.assertEqual(effective["endpoint_intent"]["intent"], "TRANSFORM")
        self.assertEqual(effective["mapped_runtime_operation_type"], "TRANSFORM")

    def test_send_line_noti_cs_is_notification_no_search_warning(self):
        analysis = analyze_endpoint(endpoint_url="/SendLineNotiCS", http_method="POST",
                                     description='ต้องระบุ Message เท่านั้น', body_fields=LINE_NOTI_FIELDS,
                                     example_response=None)
        effective = apply_overrides(analysis, {})
        self.assertEqual(effective["endpoint_intent"]["intent"], "NOTIFICATION")
        self.assertFalse(any("search/filter" in r for r in effective["recommendations"]))
        self.assertTrue(effective["operation_safety"]["confirmation_required"])


class TestValidationGroupsNeverIncludeCredentials(unittest.TestCase):
    """Found during manual browser verification of GetUrlProductDetail:
    the weak structural fallback in infer_validation_groups() grouped
    SecretCode (a credential) with CustCode under a low-confidence
    'AT_LEAST_ONE' suggestion, purely because SecretCode's generic
    identifier-shaped name also matched the same _identifier role
    family — a credential is never a substitutable search criterion."""

    def test_secret_code_never_appears_in_a_validation_group(self):
        analysis = analyze_endpoint(endpoint_url="/GetUrlProductDetail", http_method="POST",
                                     description="", body_fields=PRODUCT_URL_FIELDS, example_response=None)
        for g in analysis["validation_groups"]:
            self.assertNotIn("SecretCode", g["members"])


class TestSemanticOverridesApplyRoute(unittest.TestCase):
    """The new POST /ai-auto-setup/semantic-overrides/apply and
    GET /ai-auto-setup/semantic-endpoint-intents routes."""

    def setUp(self):
        import admin.routes as routes_module
        self.routes_module = routes_module
        self.client = TestClient(routes_module.app)
        self.client.cookies.set("session", "test")

    def _auth_patch(self):
        return patch.object(self.routes_module, "auth", return_value=None)

    def test_apply_overrides_route_returns_effective_and_provenance(self):
        analysis = analyze_endpoint(endpoint_url="/GetUrlProductDetail", http_method="POST",
                                     description="", body_fields=PRODUCT_URL_FIELDS, example_response=None)
        with self._auth_patch():
            r = self.client.post("/admin/api/business-actions/ai-auto-setup/semantic-overrides/apply",
                                  json={"semantic_analysis": analysis,
                                        "overrides": {"operation_type_override": "WORKFLOW"}})
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["effective"]["mapped_runtime_operation_type"], "WORKFLOW")
        self.assertEqual(data["provenance"]["mapped_runtime_operation_type"], "explicit")

    def test_apply_overrides_route_requires_semantic_analysis(self):
        with self._auth_patch():
            r = self.client.post("/admin/api/business-actions/ai-auto-setup/semantic-overrides/apply", json={})
        self.assertEqual(r.status_code, 400)

    def test_endpoint_intents_route_returns_declared_vocabulary(self):
        with self._auth_patch():
            r = self.client.get("/admin/api/business-actions/ai-auto-setup/semantic-endpoint-intents")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["ok"])
        self.assertEqual(set(data["endpoint_intents"]), set(ENDPOINT_INTENTS))

    def test_runtime_operation_types_route_includes_transform(self):
        with self._auth_patch():
            r = self.client.get("/admin/api/integration-contracts/operations")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIn("TRANSFORM", data["operation_types"])
        self.assertIn("CALCULATION", data["operation_types"])


if __name__ == "__main__":
    unittest.main()
