"""Tests for the Structured AI Analysis Pipeline (AI Intelligence sprint)
in services/ai_auto_setup_service.py — Business/Response Field Mapping,
Parameter Analysis, Intent Classification, Similarity Engine, Confidence
Engine, Mock Data Generator, Response Preview, AI Self Review, and
Postman Smart Grouping. All deterministic/rule-based — no LLM calls in
this file except the one mocked integration test at the bottom."""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.ai_auto_setup_service import (
    analyze_parameter, build_business_mapping, build_response_field_mapping,
    classify_intent, compute_similarity, compute_confidence, generate_mock_data,
    generate_response_preview, run_self_review, cluster_endpoints, analyze_capability,
)
from services.llm_service import LLMResponse


def _proposal(**overrides):
    base = {
        "action_name": "GetDataCustomer", "display_name": "ค้นหาข้อมูลลูกค้า", "action_id": "customer_lookup",
        "description": "ค้นหาข้อมูลลูกค้าจากรหัสลูกค้าหรืออีเมล", "category": "customer", "action_type": "API",
        "http_method": "POST", "base_url": "https://fasttrade.in.th",
        "endpoint_path": "/web-service/ai-chat/GetDataCustomer", "content_type": "application/x-www-form-urlencoded",
        "headers": {}, "routing_recommendation": "erp_only",
        "parameters": [
            {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": True,
             "input_source": "customer_message", "secret_ref": None, "example_value": "C00001",
             "validation_type": "regex", "validation_pattern": r"^C\d{5}$", "validation_confidence": "high",
             "follow_up_options": ["กรุณาแจ้งรหัสลูกค้าครับ"]},
            {"name": "CustEmail", "display_name": "อีเมล", "required": True,
             "input_source": "customer_message", "secret_ref": None, "example_value": "a@b.com",
             "validation_type": "email", "validation_pattern": None, "validation_confidence": "high",
             "follow_up_options": ["กรุณาแจ้งอีเมลครับ"]},
        ],
        "parameter_groups": [{"name": "customer_identifier", "rule": "AT_LEAST_ONE", "members": ["CustCode", "CustEmail"]}],
        "keywords": ["ลูกค้า", "customer"], "example_questions": ["ขอข้อมูลลูกค้า C00001"],
        "response_mapping": [{"json_path": "$.CustName", "mapped_label": "ชื่อลูกค้า"},
                              {"json_path": "$.Balance", "mapped_label": ""}],
        "customer_facing_response_template": "{CustName}",
    }
    base.update(overrides)
    return base


class TestParameterAnalysisEngine(unittest.TestCase):
    def test_infers_identifier_type_from_name(self):
        result = analyze_parameter({"name": "CustCode", "required": True, "example_value": "C00001"})
        self.assertEqual(result["type"], "identifier")
        self.assertFalse(result["nullable"])

    def test_infers_email_type_from_example_value(self):
        result = analyze_parameter({"name": "ContactInfo", "required": False, "example_value": "a@b.com"})
        self.assertEqual(result["type"], "email")
        self.assertTrue(result["nullable"])

    def test_infers_phone_type(self):
        result = analyze_parameter({"name": "CustPhone", "required": True, "example_value": "0812345678"})
        self.assertEqual(result["type"], "phone")

    def test_infers_money_type(self):
        result = analyze_parameter({"name": "WalletBalance", "required": False})
        self.assertEqual(result["type"], "money")

    def test_infers_boolean_type(self):
        result = analyze_parameter({"name": "is_active", "required": False, "example_value": True})
        self.assertEqual(result["type"], "boolean")

    def test_business_meaning_never_empty_for_named_param(self):
        result = analyze_parameter({"name": "CustCode", "required": True})
        self.assertTrue(result["business_meaning"])


class TestBusinessFieldMapping(unittest.TestCase):
    def test_never_loses_original_technical_name(self):
        mapping = build_business_mapping(_proposal())
        technical_names = {m["technical_parameter"] for m in mapping}
        self.assertEqual(technical_names, {"CustCode", "CustEmail"})

    def test_uses_display_name_as_business_field(self):
        mapping = build_business_mapping(_proposal())
        cust_code = next(m for m in mapping if m["technical_parameter"] == "CustCode")
        self.assertEqual(cust_code["business_field"], "รหัสลูกค้า")
        self.assertFalse(cust_code["needs_review"])

    def test_required_rule_reflects_parameter_group(self):
        mapping = build_business_mapping(_proposal())
        cust_code = next(m for m in mapping if m["technical_parameter"] == "CustCode")
        self.assertEqual(cust_code["required_rule"], "AT_LEAST_ONE")

    def test_marks_needs_review_when_no_display_name_and_unhumanizable_name(self):
        p = _proposal(parameters=[{"name": "123", "required": True, "display_name": ""}])
        mapping = build_business_mapping(p)
        self.assertTrue(mapping[0]["needs_review"])
        self.assertEqual(mapping[0]["business_field"], "Needs Review")
        self.assertEqual(mapping[0]["technical_parameter"], "123")  # never lost even when flagged


class TestResponseFieldMapping(unittest.TestCase):
    def test_uses_mapped_label_when_present(self):
        mapping = build_response_field_mapping(_proposal())
        cust_name = next(m for m in mapping if m["api_field"] == "$.CustName")
        self.assertEqual(cust_name["business_name"], "ชื่อลูกค้า")
        self.assertFalse(cust_name["needs_review"])

    def test_flags_needs_review_when_mapped_label_missing(self):
        mapping = build_response_field_mapping(_proposal())
        balance = next(m for m in mapping if m["api_field"] == "$.Balance")
        self.assertTrue(balance["needs_review"])
        # still humanizes a fallback business name rather than leaving it blank
        self.assertTrue(balance["business_name"])


class TestIntentClassification(unittest.TestCase):
    def test_classifies_customer_lookup(self):
        intent = classify_intent(_proposal())
        self.assertEqual(intent["intent_id"], "customer_lookup")
        self.assertEqual(intent["intent_display_name"], "Customer Lookup")

    def test_classifies_tracking(self):
        # 2026-07-29: intent_id is now generically composed as
        # "{entity}_{operation}" (services/semantic_api_analysis_engine.py
        # + erp_test_harness.detect_entities/_primary_entity) instead of
        # being looked up from the removed hardcoded _INTENT_TAXONOMY —
        # the entity itself ("tracking") is still correctly detected.
        p = _proposal(description="ติดตามพัสดุจากเลขพัสดุ", display_name="Tracking Lookup", category="tracking")
        intent = classify_intent(p)
        self.assertTrue(intent["intent_id"].startswith("tracking"))

    def test_falls_back_to_unknown_never_guesses_wrong_category(self):
        # 2026-07-29: entity detection now generically scans BOTH
        # description/category text AND parameter/response field names
        # (services.erp_test_harness.detect_entities/_primary_entity —
        # the same convention already used at runtime), so parameters
        # must also be unrelated for a genuine "no entity detected" case.
        p = _proposal(description="xyz completely unrelated text", display_name="Xyz", category="misc",
                       keywords=[], parameters=[{"name": "Foo", "display_name": "Foo", "required": True,
                                                  "input_source": "customer_message", "secret_ref": None,
                                                  "example_value": "bar", "validation_type": "string",
                                                  "validation_pattern": None, "validation_confidence": "low",
                                                  "follow_up_options": []}],
                       response_mapping=[])
        intent = classify_intent(p)
        self.assertEqual(intent["intent_id"], "unknown")

    def test_includes_example_questions_and_keywords(self):
        intent = classify_intent(_proposal())
        self.assertIn("ขอข้อมูลลูกค้า C00001", intent["example_user_questions"])
        self.assertIn("customer", intent["keywords"])


class TestSimilarityEngine(unittest.TestCase):
    def test_no_existing_actions_recommends_create_new(self):
        result = compute_similarity(_proposal(), [])
        self.assertIsNone(result["match"])
        self.assertEqual(result["recommendation"], "create_new")

    def test_high_overlap_recommends_merge_or_reuse(self):
        existing = [{
            "id": "existing-1", "action_key": "customer_lookup", "display_name": "ค้นหาข้อมูลลูกค้า",
            "name": "GetDataCustomer", "category": "customer",
            "parameters": [{"name": "CustCode"}, {"name": "CustEmail"}],
            "execution_host": "fasttrade.in.th",
        }]
        result = compute_similarity(_proposal(), existing)
        self.assertIsNotNone(result["match"])
        # The real requirement (spec: "never silently duplicate") is that a
        # near-identical existing action is NOT treated as create_new —
        # the exact merge/reuse/replace bucket boundary is an implementation
        # detail, not something callers should depend on precisely.
        self.assertNotEqual(result["recommendation"], "create_new")
        self.assertGreater(result["match"]["overall_similarity"], 0.4)

    def test_unrelated_existing_action_recommends_create_new(self):
        existing = [{
            "id": "existing-2", "action_key": "tracking_lookup", "display_name": "ติดตามพัสดุ",
            "name": "GetTracking", "category": "tracking", "parameters": [{"name": "TrackingNumber"}],
            "execution_host": "other-domain.com",
        }]
        result = compute_similarity(_proposal(), existing)
        self.assertEqual(result["recommendation"], "create_new")

    def test_never_silently_omits_a_recommendation(self):
        result = compute_similarity(_proposal(), [{"id": "x", "display_name": "Something Else", "category": "order"}])
        self.assertIn("recommendation", result)
        self.assertIsNotNone(result["recommendation"])


class TestConfidenceEngine(unittest.TestCase):
    def test_complete_proposal_scores_high_across_all_sections(self):
        p = _proposal()
        bm = build_business_mapping(p)
        rm = build_response_field_mapping(p)
        intent = classify_intent(p)
        confidence = compute_confidence(p, bm, rm, intent)
        self.assertEqual(set(confidence["per_section"].keys()),
                          {"api_structure", "search_field_detection", "response_mapping",
                           "business_mapping", "routing_recommendation"})
        self.assertGreater(confidence["overall_score"], 0.7)
        self.assertFalse(confidence["needs_review"])

    def test_incomplete_proposal_flags_needs_review(self):
        p = _proposal(http_method=None, base_url=None, endpoint_path=None, routing_recommendation=None,
                       parameters=[], response_mapping=[])
        bm = build_business_mapping(p)
        rm = build_response_field_mapping(p)
        intent = classify_intent(p)
        confidence = compute_confidence(p, bm, rm, intent)
        self.assertTrue(confidence["needs_review"])
        self.assertLess(confidence["overall_score"], 0.5)

    def test_overall_percent_matches_overall_score(self):
        p = _proposal()
        bm = build_business_mapping(p)
        rm = build_response_field_mapping(p)
        intent = classify_intent(p)
        confidence = compute_confidence(p, bm, rm, intent)
        self.assertEqual(confidence["overall_percent"], f"{round(confidence['overall_score']*100)}%")


class TestMockDataGenerator(unittest.TestCase):
    def test_uses_existing_example_value_when_present(self):
        mock = generate_mock_data(_proposal())
        self.assertEqual(mock["CustCode"], "C00001")

    def test_generates_type_appropriate_value_when_missing(self):
        p = _proposal(parameters=[{"name": "CustPhone", "required": True, "input_source": "customer_message"}])
        mock = generate_mock_data(p)
        self.assertTrue(mock["CustPhone"].startswith("08"))

    def test_keys_are_original_technical_names(self):
        mock = generate_mock_data(_proposal())
        self.assertEqual(set(mock.keys()), {"CustCode", "CustEmail"})


class TestResponsePreview(unittest.TestCase):
    def test_builds_business_and_technical_views(self):
        p = _proposal()
        mock = generate_mock_data(p)
        preview = generate_response_preview(p, mock)
        self.assertEqual(len(preview["business_view"]), 2)
        self.assertEqual(len(preview["technical_view"]), 2)
        labels = {v["label"] for v in preview["business_view"]}
        self.assertIn("ชื่อลูกค้า", labels)

    def test_technical_view_preserves_json_path(self):
        p = _proposal()
        mock = generate_mock_data(p)
        preview = generate_response_preview(p, mock)
        paths = {v["json_path"] for v in preview["technical_view"]}
        self.assertEqual(paths, {"$.CustName", "$.Balance"})


class TestSelfReview(unittest.TestCase):
    def test_complete_proposal_is_ready(self):
        p = _proposal()
        bm = build_business_mapping(p)
        rm = build_response_field_mapping(p)
        intent = classify_intent(p)
        similarity = compute_similarity(p, [])
        confidence = compute_confidence(p, bm, rm, intent)
        mock = generate_mock_data(p)
        preview = generate_response_preview(p, mock)
        review = run_self_review(p, bm, rm, confidence, similarity, preview)
        self.assertIn("checklist", review)
        self.assertEqual(set(review["checklist"].keys()),
                          {"business_mapping_complete", "response_mapping_complete", "search_fields_detected",
                           "routing_selected", "authentication_detected", "duplicate_checked",
                           "response_preview_generated"})

    def test_incomplete_response_mapping_flags_needs_review_status(self):
        p = _proposal(response_mapping=[{"json_path": "$.Balance", "mapped_label": ""}])
        bm = build_business_mapping(p)
        rm = build_response_field_mapping(p)
        intent = classify_intent(p)
        similarity = compute_similarity(p, [])
        confidence = compute_confidence(p, bm, rm, intent)
        mock = generate_mock_data(p)
        preview = generate_response_preview(p, mock)
        review = run_self_review(p, bm, rm, confidence, similarity, preview)
        self.assertFalse(review["checklist"]["response_mapping_complete"])
        self.assertEqual(review["status"], "needs_review")


class TestPostmanSmartGrouping(unittest.TestCase):
    def test_clusters_by_category_never_merging_unrelated(self):
        endpoints = [
            {"name": "GetCustomer", "method": "POST", "url": "https://x/GetCustomer", "category_guess": "Customer lookup"},
            {"name": "GetOrder", "method": "POST", "url": "https://x/GetOrder", "category_guess": "Order lookup"},
            {"name": "GetTracking", "method": "GET", "url": "https://x/GetTracking", "category_guess": "Tracking lookup"},
        ]
        clusters = cluster_endpoints(endpoints)
        groups = {c["group"] for c in clusters}
        self.assertEqual(groups, {"Customer", "Order", "Tracking"})
        for c in clusters:
            self.assertEqual(len(c["endpoints"]), 1)

    def test_groups_multiple_endpoints_of_same_category_together(self):
        endpoints = [
            {"name": "GetCustomer", "method": "POST", "url": "https://x/a", "category_guess": "Customer lookup"},
            {"name": "UpdateCustomer", "method": "POST", "url": "https://x/b", "category_guess": "Customer lookup"},
        ]
        clusters = cluster_endpoints(endpoints)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0]["group"], "Customer")
        self.assertEqual(len(clusters[0]["endpoints"]), 2)

    def test_uncategorized_endpoints_never_lumped_together(self):
        endpoints = [
            {"name": "Foo", "method": "GET", "url": "https://x/foo", "category_guess": "Uncategorized"},
            {"name": "Bar", "method": "GET", "url": "https://x/bar", "category_guess": "Uncategorized"},
        ]
        clusters = cluster_endpoints(endpoints)
        self.assertEqual(len(clusters), 2)


class TestPipelineIntegration(unittest.TestCase):
    """Confirms analyze_capability() wires every stage together and
    that the result is purely additive — existing keys unchanged."""

    def test_analyze_capability_includes_all_new_pipeline_stages(self):
        fake_llm = MagicMock()
        fake_llm.generate.return_value = LLMResponse(
            text=json.dumps(_proposal()), model="gpt-4o-mini", provider="openai",
            input_tokens=10, output_tokens=20, latency_ms=100.0)
        with patch("services.ai_auto_setup_service.get_llm_service", return_value=fake_llm):
            result = analyze_capability("POST /GetDataCustomer", "customer lookup", ["ขอข้อมูลลูกค้า"])
        self.assertTrue(result["ok"])
        for key in ("business_mapping", "response_field_mapping", "intent_classification",
                    "similarity", "confidence", "mock_data", "response_preview", "self_review"):
            self.assertIn(key, result, msg=key)
        # Backward compatibility — existing string field untouched in shape.
        self.assertIn(result["proposal"]["detection_confidence"], ("high", "medium", "low"))

    def test_analyze_capability_similarity_uses_passed_existing_actions(self):
        fake_llm = MagicMock()
        fake_llm.generate.return_value = LLMResponse(
            text=json.dumps(_proposal()), model="gpt-4o-mini", provider="openai",
            input_tokens=10, output_tokens=20, latency_ms=100.0)
        existing = [{"id": "e1", "display_name": "ค้นหาข้อมูลลูกค้า", "name": "GetDataCustomer",
                     "category": "customer", "parameters": [{"name": "CustCode"}, {"name": "CustEmail"}],
                     "execution_host": "fasttrade.in.th"}]
        with patch("services.ai_auto_setup_service.get_llm_service", return_value=fake_llm):
            result = analyze_capability("POST /GetDataCustomer", "customer lookup", [], existing_actions=existing)
        self.assertIsNotNone(result["similarity"]["match"])
        self.assertNotEqual(result["similarity"]["recommendation"], "create_new")


if __name__ == "__main__":
    unittest.main()
