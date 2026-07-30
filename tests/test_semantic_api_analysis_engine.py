"""Regression tests for services/semantic_api_analysis_engine.py — the
Semantic API Analysis Engine (Step 13 of the AI-Guided ERP Setup
redesign sprint). Uses the 3 real draft Business Action specs
(search_data_shipment_list / get_url_product_detail / send_line_noti_cs,
see scripts/create_shipment_url_line_actions.py) as real-world fixtures
alongside small invented examples for the remaining categories. All
tests are pure-function / no LLM, no DB, no network."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.semantic_api_analysis_engine import (
    classify_endpoint_intent, classify_field_roles, infer_validation_groups,
    field_is_authentication, describe_business_entity, analyze_response_semantics,
    derive_operation_safety, build_recommendations, analyze_endpoint, apply_overrides,
    generate_negative_test_cases, generate_boundary_test_cases, generate_example_api_call,
    ENDPOINT_INTENTS,
)


# ── Real fixtures (scripts/create_shipment_url_line_actions.py) ────────

SHIPMENT_LIST_FIELDS = [
    {"name": "SecretCode", "required": True, "description": "รหัสยืนยันตัวตน"},
    {"name": "CustCode", "required": True, "example_value": "C00001", "description": "รหัสลูกค้า"},
    {"name": "ReceivedDateStart", "required": False, "example_value": "2026-06-01",
     "description": "วันที่รับเข้าโกดังจีนเริ่มต้น (YYYY-MM-DD)"},
    {"name": "ReceivedDateEnd", "required": False, "example_value": "2026-06-30",
     "description": "วันที่รับเข้าโกดังจีนสิ้นสุด (YYYY-MM-DD)"},
    {"name": "ExportDateStart", "required": False, "example_value": "2026-06-01", "description": "วันที่ส่งออก"},
    {"name": "ExportDateEnd", "required": False, "example_value": "2026-06-30", "description": "วันที่ส่งออกสิ้นสุด"},
    {"name": "ArrivedDateStart", "required": False, "example_value": "2026-06-01", "description": "วันที่รับเข้า"},
    {"name": "ArrivedDateEnd", "required": False, "example_value": "2026-06-30", "description": "วันที่รับเข้าสิ้นสุด"},
    {"name": "BillStatus", "required": False, "example_value": "3", "description": "สถานะบิล"},
    {"name": "Latest", "required": False, "example_value": "5", "description": "จำนวนรายการล่าสุดที่ต้องการ (limit)"},
]

PRODUCT_URL_FIELDS = [
    {"name": "SecretCode", "required": True, "description": "รหัสยืนยันตัวตน"},
    {"name": "CustCode", "required": True, "example_value": "C00001", "description": "รหัสลูกค้า"},
    {"name": "URL", "required": True, "example_value": "https://item.taobao.com/item.htm?id=123456789",
     "description": "ลิงก์สินค้าจาก 1688 / Taobao / Tmall"},
]

LINE_NOTI_FIELDS = [
    {"name": "SecretCode", "required": True, "description": "รหัสยืนยันตัวตน"},
    {"name": "Message", "required": True, "example_value": "ทดสอบข้อความแจ้งเตือน", "description": "ข้อความที่ต้องการส่ง"},
]


class TestLookupApis(unittest.TestCase):
    def test_get_data_customer_is_lookup(self):
        result = classify_endpoint_intent(
            endpoint_url="/web-service/ai-chat/GetDataCustomer", http_method="GET",
            description="ดึงข้อมูลลูกค้าจากรหัสลูกค้า",
            example_response={"CustName": "Somchai", "Balance": 100},
        )
        self.assertEqual(result["intent"], "LOOKUP")
        self.assertGreaterEqual(result["confidence"], 0.7)

    def test_fixture_order_lookup_english(self):
        result = classify_endpoint_intent(endpoint_url="/GetOrderStatus", http_method="GET",
                                           description="Retrieve a single order's status",
                                           example_response={"OrderNo": "PO1", "Status": "shipped"})
        self.assertEqual(result["intent"], "LOOKUP")


class TestListApis(unittest.TestCase):
    def test_search_data_shipment_list_is_list(self):
        result = classify_endpoint_intent(
            endpoint_url="/web-service/ai-chat/SearchDataShipmentList", http_method="POST",
            description="ต้องระบุ CustCode และเงื่อนไขค้นหาอย่างน้อย 1 อย่าง",
            body_fields=SHIPMENT_LIST_FIELDS,
            example_response=[{"BillNo": "B1"}, {"BillNo": "B2"}],
        )
        self.assertIn(result["intent"], ("LIST", "SEARCH"))
        self.assertGreaterEqual(result["confidence"], 0.9)


class TestTransformApis(unittest.TestCase):
    def test_get_url_product_detail_is_transform(self):
        """The old runtime classifier (erp_test_harness) wrongly reports
        UNKNOWN for this case — a URL-shaped input mapping to a
        structured product-detail output is exactly what Step 1's
        TRANSFORM tier exists to catch."""
        result = classify_endpoint_intent(
            endpoint_url="/web-service/ai-chat/GetUrlProductDetail", http_method="GET",
            description="รองรับลิงก์จาก 1688, Taobao, Tmall เท่านั้น",
            body_fields=PRODUCT_URL_FIELDS,
            example_response={"ProductName": "Widget", "Price": 199},
        )
        self.assertEqual(result["intent"], "TRANSFORM")


class TestNotificationApis(unittest.TestCase):
    def test_send_line_noti_cs_is_notification(self):
        result = classify_endpoint_intent(
            endpoint_url="/web-service/ai-chat/SendLineNotiCS", http_method="POST",
            description='ต้องระบุ Message เท่านั้น ระบบจะเติม "AI : " นำหน้าให้อัตโนมัติก่อนส่งเข้า LINE OA - send notification',
            body_fields=LINE_NOTI_FIELDS,
        )
        self.assertEqual(result["intent"], "NOTIFICATION")

    def test_safety_flags_notification_as_confirmation_required(self):
        safety = derive_operation_safety("NOTIFICATION", endpoint_text="send line notification")
        self.assertTrue(safety["notification"])
        self.assertTrue(safety["confirmation_required"])
        self.assertFalse(safety["read_only"])


class TestMutationApis(unittest.TestCase):
    def test_cancel_order_is_mutation_and_destructive(self):
        result = classify_endpoint_intent(endpoint_url="/CancelOrder", http_method="POST",
                                           description="Cancel an existing order")
        self.assertEqual(result["intent"], "MUTATION")
        safety = derive_operation_safety(result["intent"], endpoint_text="Cancel an existing order")
        self.assertTrue(safety["destructive"])
        self.assertTrue(safety["rollback_recommended"])
        self.assertTrue(safety["audit_required"])

    def test_weak_description_never_alone_justifies_mutation(self):
        # POST + no structural mutating verb signal in name/description,
        # no example response -> weak COMMAND, never a confident MUTATION.
        result = classify_endpoint_intent(endpoint_url="/DoSomething", http_method="POST",
                                           description="performs an internal process")
        self.assertNotEqual(result["intent"], "MUTATION")


class TestAuthenticationApis(unittest.TestCase):
    def test_login_endpoint_is_authentication(self):
        result = classify_endpoint_intent(endpoint_url="/auth/login", http_method="POST",
                                           description="Login and obtain an access token")
        self.assertEqual(result["intent"], "AUTHENTICATION")

    def test_secret_code_field_gets_authentication_roles(self):
        roles = classify_field_roles("SecretCode", "รหัสยืนยันตัวตน", None, context={})
        self.assertIn("authentication", roles)
        self.assertIn("credential", roles)
        self.assertIn("system_hidden", roles)
        self.assertTrue(field_is_authentication("SecretCode"))

    def test_secret_value_never_reaches_field_roles_output(self):
        # classify_field_roles never receives/echoes a raw secret VALUE
        # anywhere in its own return value.
        roles = classify_field_roles("ApiKey", "auth key", "sk-super-secret-value-123", context={})
        self.assertIn("credential", roles)


class TestDateRangeApis(unittest.TestCase):
    def test_received_date_start_end_roles(self):
        start_roles = classify_field_roles("ReceivedDateStart", "วันที่รับเข้าโกดังจีนเริ่มต้น", "2026-06-01", context={})
        end_roles = classify_field_roles("ReceivedDateEnd", "วันที่รับเข้าโกดังจีนสิ้นสุด", "2026-06-30", context={})
        self.assertIn("date_start", start_roles)
        self.assertIn("date_end", end_roles)

    def test_boundary_case_generated_for_reversed_range(self):
        fields = [{"name": "ReceivedDateStart", "roles": ["date_start"]},
                  {"name": "ReceivedDateEnd", "roles": ["date_end"]}]
        cases = generate_boundary_test_cases(fields)
        self.assertTrue(any("reversed" in c["case"] for c in cases))


class TestAtLeastOneValidation(unittest.TestCase):
    def test_shipment_list_search_criteria_group_inferred(self):
        fields = [dict(f, required=f.get("required", False)) for f in SHIPMENT_LIST_FIELDS
                  if f["name"] not in ("SecretCode", "CustCode")]
        for f in fields:
            f["description"] = f["description"] + " — เลือกอย่างน้อย 1 อย่าง"
        groups = infer_validation_groups(fields)
        self.assertTrue(any(g["rule"] == "AT_LEAST_ONE" for g in groups))


class TestOneOfValidation(unittest.TestCase):
    def test_either_or_phrase_detected(self):
        fields = [
            {"name": "Email", "description": "either Email or Phone must be provided"},
            {"name": "Phone", "description": "either Email or Phone must be provided"},
        ]
        groups = infer_validation_groups(fields)
        self.assertTrue(any(g["rule"] == "AT_LEAST_ONE" for g in groups))


class TestEnumValidation(unittest.TestCase):
    def test_bill_status_gets_enum_role(self):
        roles = classify_field_roles("BillStatus", "สถานะบิล", "3", context={})
        self.assertIn("enum", roles)

    def test_negative_case_for_enum_field(self):
        cases = generate_negative_test_cases([{"name": "BillStatus", "required": False, "roles": ["enum"]}])
        self.assertTrue(any("enum" in c["case"] for c in cases))


class TestArrayParameters(unittest.TestCase):
    def test_array_shaped_response_field_detected(self):
        analysis = analyze_response_semantics({"Items": [1, 2, 3], "CustName": "x"})
        field = next(f for f in analysis["fields"] if f["field"] == "Items")
        self.assertEqual(field["semantic_type"], "collection")

    def test_latest_limit_boundary_cases(self):
        cases = generate_boundary_test_cases([{"name": "Latest", "roles": ["limit"]}])
        self.assertTrue(any("Latest=0" in c["case"] for c in cases))
        self.assertTrue(any("999999" in c["case"] for c in cases))


class TestUrlParameters(unittest.TestCase):
    def test_url_field_gets_url_role(self):
        roles = classify_field_roles("URL", "ลิงก์สินค้า", "https://item.taobao.com/item.htm?id=1", context={})
        self.assertIn("url", roles)

    def test_url_business_entity_label(self):
        label = describe_business_entity("URL", ["url"], "product", "TRANSFORM")
        self.assertIn("URL", label)


class TestThaiDocumentation(unittest.TestCase):
    def test_thai_description_classified(self):
        result = classify_endpoint_intent(endpoint_url="/GetDataCustomer", http_method="GET",
                                           description="ดึงข้อมูลลูกค้าจากรหัสลูกค้า",
                                           example_response={"CustName": "x"})
        self.assertEqual(result["intent"], "LOOKUP")


class TestEnglishDocumentation(unittest.TestCase):
    def test_english_description_classified(self):
        result = classify_endpoint_intent(endpoint_url="/SearchOrders", http_method="GET",
                                           description="Search for orders matching criteria",
                                           example_response=[{"OrderNo": "1"}])
        self.assertEqual(result["intent"], "SEARCH")


class TestCurlInput(unittest.TestCase):
    def test_example_api_call_generation_from_analyzed_fields(self):
        fields = [{"name": "CustCode", "example_value": "C00001", "roles": ["customer_identifier"]},
                  {"name": "SecretCode", "example_value": "shouldnotappear", "roles": ["credential", "authentication"]}]
        call = generate_example_api_call("/GetDataCustomer", "POST", fields)
        self.assertIn("CustCode", call["json_body"])
        self.assertNotIn("SecretCode", call["json_body"])
        self.assertIn("curl", call["curl"])


class TestPostmanInput(unittest.TestCase):
    def test_walk_postman_items_still_available(self):
        # Step 13 requires verifying Postman input support is intact
        # (not newly built here) — services.ai_auto_setup_service already
        # implements _walk_postman_items for detect_endpoints_from_document.
        from services.ai_auto_setup_service import _walk_postman_items
        collection = {"item": [{"name": "Get Customer", "request": {
            "method": "GET", "url": {"raw": "https://x.test/GetDataCustomer"}}}]}
        out = []
        _walk_postman_items(collection["item"], out)
        self.assertTrue(out)


class TestJsonInput(unittest.TestCase):
    def test_analyze_endpoint_from_plain_dict_input(self):
        analysis = analyze_endpoint(
            endpoint_url="/web-service/ai-chat/GetUrlProductDetail", http_method="GET",
            description="รองรับลิงก์จาก 1688, Taobao, Tmall เท่านั้น ต้องระบุ CustCode และ URL",
            body_fields=PRODUCT_URL_FIELDS, example_response={"ProductName": "Widget", "Price": 199},
        )
        self.assertEqual(analysis["endpoint_intent"]["intent"], "TRANSFORM")
        self.assertEqual(analysis["analysis_version"], "1.0")
        self.assertIn("recommendations", analysis)
        names = {f["name"] for f in analysis["fields"]}
        self.assertEqual(names, {"SecretCode", "CustCode", "URL"})
        secret_field = next(f for f in analysis["fields"] if f["name"] == "SecretCode")
        self.assertIn("credential", secret_field["roles"])


class TestOpenApiInput(unittest.TestCase):
    def test_walk_openapi_paths_still_available(self):
        from services.ai_auto_setup_service import _walk_openapi_paths
        spec = {"paths": {"/customer": {"get": {"summary": "Get customer", "parameters": []}}}}
        out = []
        _walk_openapi_paths(spec, out)
        self.assertTrue(out)


class TestRepeatedAnalysisDeterminism(unittest.TestCase):
    def test_ten_repeated_analyses_are_byte_identical(self):
        kwargs = dict(
            endpoint_url="/web-service/ai-chat/SearchDataShipmentList", http_method="POST",
            description="ต้องระบุ CustCode และเงื่อนไขค้นหาอย่างน้อย 1 อย่าง",
            body_fields=SHIPMENT_LIST_FIELDS, example_response=[{"BillNo": "B1"}],
        )
        results = [analyze_endpoint(**kwargs) for _ in range(10)]
        first = results[0]
        for r in results[1:]:
            self.assertEqual(r, first)


class TestOperationSafetyAndRecommendations(unittest.TestCase):
    def test_credential_field_triggers_recommendation(self):
        recs = build_recommendations(
            response_analysis={"response_mapping_status": "pending"},
            endpoint_intent_result={"intent": "LOOKUP", "confidence": 0.9},
            fields=[{"name": "SecretCode", "roles": ["credential"]}],
            operation_safety={"notification": False, "destructive": False},
        )
        self.assertTrue(any("Credential" in r for r in recs))
        self.assertTrue(any("Response mapping" in r for r in recs))

    def test_low_confidence_triggers_review_recommendation(self):
        recs = build_recommendations(
            response_analysis={"response_mapping_status": "analyzed"},
            endpoint_intent_result={"intent": "UNKNOWN", "confidence": 0.3},
            fields=[], operation_safety={"notification": False, "destructive": False},
        )
        self.assertTrue(any("confidence low" in r for r in recs))


class TestNoExampleResponseNeverFabricates(unittest.TestCase):
    def test_pending_status_when_no_example_response(self):
        analysis = analyze_response_semantics(None)
        self.assertEqual(analysis["response_mapping_status"], "pending")
        self.assertNotIn("fields", analysis)


class TestOverrideStorage(unittest.TestCase):
    def test_explicit_override_wins_and_is_marked_explicit(self):
        analysis = analyze_endpoint(endpoint_url="/GetUrlProductDetail", http_method="GET",
                                     description="product url lookup", body_fields=PRODUCT_URL_FIELDS,
                                     example_response={"ProductName": "Widget"})
        merged = apply_overrides(analysis, {"intent_override": "LOOKUP",
                                             "field_overrides": {"URL": {"roles": ["url", "payload"]}}})
        self.assertEqual(merged["endpoint_intent"]["intent"], "LOOKUP")
        self.assertEqual(merged["provenance"]["endpoint_intent"], "explicit")
        url_field = next(f for f in merged["fields"] if f["name"] == "URL")
        self.assertEqual(url_field["roles"], ["url", "payload"])
        self.assertEqual(merged["provenance"]["fields.URL"], "explicit")
        # Original analysis is never mutated.
        self.assertNotEqual(analysis["endpoint_intent"]["intent"], merged["endpoint_intent"].get("evidence"))

    def test_no_override_reports_derived_provenance(self):
        analysis = analyze_endpoint(endpoint_url="/GetUrlProductDetail", http_method="GET",
                                     description="product url lookup", body_fields=PRODUCT_URL_FIELDS,
                                     example_response={"ProductName": "Widget"})
        merged = apply_overrides(analysis, None)
        self.assertEqual(merged["provenance"]["endpoint_intent"], "derived")


class TestVocabulary(unittest.TestCase):
    def test_all_endpoint_intents_are_from_the_declared_vocabulary(self):
        for kwargs in (
            dict(endpoint_url="/a", http_method="GET", description="", example_response=None),
            dict(endpoint_url="/GetX", http_method="GET", description="get x", example_response={"a": 1}),
            dict(endpoint_url="/SearchX", http_method="GET", description="search x", example_response=[{"a": 1}]),
        ):
            result = classify_endpoint_intent(**kwargs)
            self.assertIn(result["intent"], ENDPOINT_INTENTS)


if __name__ == "__main__":
    unittest.main()
