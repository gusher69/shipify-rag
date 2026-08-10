"""Tests for the ERP Action Test Harness (services/erp_test_harness.py).
Uses the same _FakeSupabase mock as the rest of the Business Action test
suite — never a real DB, never a real network call, never a real LLM
call (mocked via services.llm_service.get_llm_service)."""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_business_action_registry import _FakeSupabase
from services.business_action_registry import BusinessActionRegistry
from services.llm_service import LLMResponse
from services import erp_test_harness as harness


def _customer_lookup_action(sb=None):
    reg = BusinessActionRegistry(sb or _FakeSupabase())
    action = reg.create({"action_key": "get_customer_data", "name": "GetDataCustomer",
                          "display_name": "Customer Lookup", "action_type": "API", "category": "customer",
                          "enabled": True, "search_keywords": ["customer", "wallet"]})
    action_id = action["id"]
    reg.replace_parameters(action_id, [
        {"name": "SecretCode", "required": True, "input_source": "credential_store", "credential_ref": "fasttrade_secret"},
        {"name": "CustCode", "display_name": "Customer Code", "required": True, "input_source": "customer_message",
         "validation_type": "regex", "validation_pattern": r"^C\d{5}$",
         "description": "Please provide your customer code.", "example_value": "C00001"},
    ])
    reg.upsert_execution(action_id, {"endpoint": "https://fasttrade.in.th/web-service/ai-chat/GetDataCustomer",
                                      "http_method": "POST", "content_type": "application/x-www-form-urlencoded"})
    reg.replace_response_mapping(action_id, [
        {"json_path": "$.customer_data.CustName", "mapped_label": "Customer Name"},
        {"json_path": "$.customer_data.WalletBalance", "mapped_label": "Wallet Balance"},
    ])
    return reg, action_id


def _order_lookup_action(sb=None):
    """Cross-domain fixture #1 (Product-Agnostic Architecture sprint) —
    Order Number -> Order Status, Tracking Number. Proves the harness
    needs zero source changes for a completely different entity/domain."""
    reg = BusinessActionRegistry(sb or _FakeSupabase())
    action = reg.create({"action_key": "order_lookup", "name": "GetOrderStatus",
                          "display_name": "Order Lookup", "action_type": "API", "category": "order",
                          "enabled": True, "search_keywords": ["order", "tracking"]})
    action_id = action["id"]
    reg.replace_parameters(action_id, [
        {"name": "OrderNo", "display_name": "Order Number", "required": True, "input_source": "customer_message",
         "validation_type": "regex", "validation_pattern": r"^PO\d{6,}$",
         "description": "Please provide your order number.", "example_value": "PO202601001"},
    ])
    reg.upsert_execution(action_id, {"endpoint": "https://example-erp.test/orders/status",
                                      "http_method": "GET", "content_type": "application/json"})
    reg.replace_response_mapping(action_id, [
        {"json_path": "$.order.Status", "mapped_label": "Order Status"},
        {"json_path": "$.order.TrackingNumber", "mapped_label": "Tracking Number"},
        {"json_path": "$.order.Total", "mapped_label": "Order Total"},
    ])
    return reg, action_id


def _product_lookup_action(sb=None):
    """Cross-domain fixture #2 — SKU -> Product Name, Price, Stock."""
    reg = BusinessActionRegistry(sb or _FakeSupabase())
    action = reg.create({"action_key": "product_lookup", "name": "GetProductInfo",
                          "display_name": "Product Lookup", "action_type": "API", "category": "product",
                          "enabled": True, "search_keywords": ["product", "sku"]})
    action_id = action["id"]
    reg.replace_parameters(action_id, [
        {"name": "SKU", "display_name": "SKU", "required": True, "input_source": "customer_message",
         "validation_type": "non_empty", "description": "Please provide the product SKU.", "example_value": "SKU-1001"},
    ])
    reg.upsert_execution(action_id, {"endpoint": "https://example-erp.test/products",
                                      "http_method": "GET", "content_type": "application/json"})
    reg.replace_response_mapping(action_id, [
        {"json_path": "$.product.Name", "mapped_label": "Product Name"},
        {"json_path": "$.product.Price", "mapped_label": "Price"},
        {"json_path": "$.product.Stock", "mapped_label": "Stock Quantity"},
    ])
    return reg, action_id


def _invoice_lookup_action(sb=None):
    """Cross-domain fixture #3 — Invoice Number -> Amount, Due Date,
    Payment Status."""
    reg = BusinessActionRegistry(sb or _FakeSupabase())
    action = reg.create({"action_key": "invoice_lookup", "name": "GetInvoiceInfo",
                          "display_name": "Invoice Lookup", "action_type": "API", "category": "finance",
                          "enabled": True, "search_keywords": ["invoice", "payment"]})
    action_id = action["id"]
    reg.replace_parameters(action_id, [
        {"name": "InvoiceNo", "display_name": "Invoice Number", "required": True, "input_source": "customer_message",
         "validation_type": "non_empty", "description": "Please provide your invoice number.", "example_value": "INV-2026-001"},
    ])
    reg.upsert_execution(action_id, {"endpoint": "https://example-erp.test/invoices",
                                      "http_method": "GET", "content_type": "application/json"})
    reg.replace_response_mapping(action_id, [
        {"json_path": "$.invoice.Amount", "mapped_label": "Invoice Amount"},
        {"json_path": "$.invoice.DueDate", "mapped_label": "Due Date"},
        {"json_path": "$.invoice.PaymentStatus", "mapped_label": "Payment Status"},
    ])
    return reg, action_id


def _cancel_order_action(sb=None):
    """CANCEL-operation-type fixture — reusable helper (Part 12) so any
    test needing a command-style (non-LOOKUP-like) action doesn't need
    to inline its own registry setup. Behavior is unchanged from the
    original inline fixture in test_command_action_never_asks_for_field_selection."""
    reg = BusinessActionRegistry(sb or _FakeSupabase())
    action = reg.create({"action_key": "cancel_order", "name": "CancelOrder",
                          "display_name": "Cancel Order", "action_type": "API", "category": "order", "enabled": True})
    action_id = action["id"]
    reg.replace_parameters(action_id, [
        {"name": "OrderNo", "display_name": "Order Number", "required": True, "input_source": "customer_message",
         "validation_type": "non_empty", "example_value": "PO202601001"},
    ])
    reg.upsert_execution(action_id, {"endpoint": "https://example-erp.test/orders/cancel", "http_method": "POST"})
    reg.replace_response_mapping(action_id, [
        {"json_path": "$.result.Status", "mapped_label": "Cancellation Status"},
        {"json_path": "$.result.RefundAmount", "mapped_label": "Refund Amount"},
    ])
    return reg, action_id


def _customer_lookup_action_with_3_options(sb=None):
    """Matches the spec's own running example exactly: Customer Name /
    Wallet Balance / Available Coupons — used for the Requested
    Information Clarification sprint's tests (needs >1 visible option
    to exercise the "ask which field" branch at all)."""
    reg, action_id = _customer_lookup_action(sb)
    reg.replace_response_mapping(action_id, [
        {"json_path": "$.customer_data.CustName", "mapped_label": "Customer Name"},
        {"json_path": "$.customer_data.WalletBalance", "mapped_label": "Wallet Balance"},
        {"json_path": "$.customer_data.Coupons", "mapped_label": "Available Coupons"},
    ])
    return reg, action_id


class TestSemanticClassify(unittest.TestCase):
    def test_matches_spec_examples(self):
        self.assertEqual(harness.semantic_classify("CustCode")["canonical_name"], "customer.identifier")
        self.assertEqual(harness.semantic_classify("CustCode")["semantic_type"], "identifier")
        self.assertEqual(harness.semantic_classify("CustName")["canonical_name"], "customer.name")
        self.assertEqual(harness.semantic_classify("WalletBalance")["canonical_name"], "wallet.balance")
        self.assertEqual(harness.semantic_classify("WalletBalance")["semantic_type"], "currency")
        self.assertEqual(harness.semantic_classify("CouponCount")["canonical_name"], "coupon.count")
        self.assertEqual(harness.semantic_classify("CouponCount")["semantic_type"], "integer")


class TestEntityAndIntentDetection(unittest.TestCase):
    def test_detects_customer_and_wallet_entities(self):
        reg, action_id = _customer_lookup_action()
        action = reg.get_full(action_id, mask_secrets=True)
        entities = harness.detect_entities(action)
        self.assertIn("customer", entities)
        self.assertIn("wallet", entities)

    def test_primary_intent_matches_capability(self):
        reg, action_id = _customer_lookup_action()
        action = reg.get_full(action_id, mask_secrets=True)
        self.assertEqual(harness.primary_intent(action), "customer.lookup")


class TestDescribeActionForSelection(unittest.TestCase):
    def test_includes_all_part2_fields(self):
        reg, action_id = _customer_lookup_action()
        action = reg.get_full(action_id, mask_secrets=True)
        desc = harness.describe_action_for_selection(action)
        for key in ("id", "capability", "endpoint", "http_method", "has_credentials",
                    "required_search_fields", "response_mappings", "semantic_intent", "entities"):
            self.assertIn(key, desc)
        self.assertTrue(desc["has_credentials"])
        self.assertEqual([f["name"] for f in desc["required_search_fields"]], ["CustCode"])
        self.assertEqual(len(desc["response_mappings"]), 2)

    def test_contract_sourced_when_sb_given_matches_fallback_shape(self):
        """Part 15 — passing `sb` sources the summary via
        services/integration_contract_service.py::describe_integration_
        cached() instead of the direct fallback derivation, but the
        returned shape/values stay equivalent for the tester UI."""
        reg, action_id = _customer_lookup_action()
        action = reg.get_full(action_id, mask_secrets=True)
        with patch("services.business_action_registry.get_registry", return_value=reg), \
             patch("services.integration_schema_service.get_registry") as mock_schema_reg:
            mock_schema_reg.return_value.get_published.return_value = None
            desc = harness.describe_action_for_selection(action, sb=reg._sb)
        self.assertEqual(sorted(f["name"] for f in desc["required_search_fields"]), ["CustCode"])
        self.assertEqual(len(desc["response_mappings"]), 2)
        self.assertIn("wallet", desc["entities"])
        self.assertIn("contract_capability", desc)
        self.assertEqual(desc["capability"], action.get("display_name") or action.get("name"))


class TestParameterExtraction(unittest.TestCase):
    def test_extracts_customer_code_from_message(self):
        reg, action_id = _customer_lookup_action()
        action = reg.get_full(action_id, mask_secrets=True)
        result = harness.extract_parameters(action, reg, {}, "ขอดูข้อมูลลูกค้ารหัส C00001")
        self.assertEqual(result["parameters"].get("CustCode"), "C00001")
        self.assertEqual(result["missing_parameters"], [])
        self.assertGreater(result["confidence"], 0.9)

    def test_missing_parameter_when_no_code_given(self):
        reg, action_id = _customer_lookup_action()
        action = reg.get_full(action_id, mask_secrets=True)
        result = harness.extract_parameters(action, reg, {}, "ขอดูข้อมูลลูกค้า")
        self.assertIn("CustCode", result["missing_parameters"])

    def test_second_turn_uses_prior_context_not_isolated(self):
        """Scenario B — CUST001-style follow-up must bind to the SAME
        still-missing parameter from the previous turn, not be treated
        as a brand new, unrelated question."""
        reg, action_id = _customer_lookup_action()
        action = reg.get_full(action_id, mask_secrets=True)
        turn1 = harness.extract_parameters(action, reg, {}, "ขอดูข้อมูลลูกค้า")
        self.assertIn("CustCode", turn1["missing_parameters"])
        turn2 = harness.extract_parameters(action, reg, turn1["parameters"], "C00002")
        self.assertEqual(turn2["parameters"].get("CustCode"), "C00002")
        self.assertEqual(turn2["missing_parameters"], [])


class TestParameterGroupExtraction(unittest.TestCase):
    """An AT_LEAST_ONE parameter group (e.g. CustCode/CustEmail/CustName/
    CustPhone — no single field individually required) must still block
    execution and surface as missing_parameters until one member is
    provided — validate_can_execute() only reports this via
    failed_groups, never missing_required, so the harness must merge
    the two itself."""

    def _action_with_at_least_one_group(self):
        reg, action_id = _customer_lookup_action()
        reg.replace_parameters(action_id, [
            {"name": "SecretCode", "required": True, "input_source": "credential_store", "credential_ref": "fasttrade_secret"},
            {"name": "CustCode", "display_name": "Customer Code", "required": False, "input_source": "customer_message",
             "validation_type": "regex", "validation_pattern": r"^C\d{5}$", "example_value": "C00001"},
            {"name": "CustEmail", "display_name": "Customer Email", "required": False, "input_source": "customer_message",
             "validation_type": "email"},
        ])
        reg.set_parameter_groups(action_id, [{"name": "identifier", "rule": "AT_LEAST_ONE", "members": ["CustCode", "CustEmail"]}])
        return reg, action_id

    def test_group_with_nothing_provided_is_reported_missing(self):
        reg, action_id = self._action_with_at_least_one_group()
        action = reg.get_full(action_id, mask_secrets=True)
        result = harness.extract_parameters(action, reg, {}, "ขอดูข้อมูลลูกค้า")
        self.assertFalse(result["can_execute"])
        self.assertTrue(set(result["missing_parameters"]) & {"CustCode", "CustEmail"})

    def test_group_satisfied_by_one_member_is_not_missing(self):
        reg, action_id = self._action_with_at_least_one_group()
        action = reg.get_full(action_id, mask_secrets=True)
        result = harness.extract_parameters(action, reg, {}, "ขอดูข้อมูลลูกค้ารหัส C00001")
        self.assertTrue(result["can_execute"])
        self.assertEqual(result["missing_parameters"], [])

    def test_clarification_question_generated_for_unsatisfied_group(self):
        reg, action_id = self._action_with_at_least_one_group()
        action = reg.get_full(action_id, mask_secrets=True)
        q = harness.build_clarification_question(action, reg, {})
        self.assertIsNotNone(q)


class TestClarificationQuestion(unittest.TestCase):
    def test_builds_question_from_description(self):
        reg, action_id = _customer_lookup_action()
        action = reg.get_full(action_id, mask_secrets=True)
        q = harness.build_clarification_question(action, reg, {})
        self.assertEqual(q, "Please provide your customer code.")

    def test_none_when_nothing_missing(self):
        reg, action_id = _customer_lookup_action()
        action = reg.get_full(action_id, mask_secrets=True)
        q = harness.build_clarification_question(action, reg, {"CustCode": "C00001"})
        self.assertIsNone(q)


class TestSimulationMode(unittest.TestCase):
    def test_builds_mock_for_every_response_mapping_field(self):
        reg, action_id = _customer_lookup_action()
        action = reg.get_full(action_id, mask_secrets=True)
        mock = harness.build_simulated_response(action)
        self.assertIn("Customer Name", mock)
        self.assertIn("Wallet Balance", mock)
        self.assertIsInstance(mock["Wallet Balance"], float)


class TestResponseNormalization(unittest.TestCase):
    def test_normalizes_to_canonical_keys(self):
        reg, action_id = _customer_lookup_action()
        action = reg.get_full(action_id, mask_secrets=True)
        normalized = harness.normalize_response(action, {"Customer Name": "Somchai", "Wallet Balance": 1200})
        self.assertEqual(normalized["customer.name"], {"label": "Customer Name", "value": "Somchai", "semantic_type": "string"})
        self.assertEqual(normalized["wallet.balance"]["value"], 1200)
        self.assertEqual(normalized["wallet.balance"]["semantic_type"], "currency")


class TestGroundedAnswer(unittest.TestCase):
    def test_no_record_answer_when_normalized_empty(self):
        reg, action_id = _customer_lookup_action()
        action = reg.get_full(action_id, mask_secrets=True)
        result = harness.generate_grounded_answer("ยอด wallet เท่าไหร่", action, {}, language="th")
        self.assertFalse(result["grounded"])
        self.assertIn("ไม่พบข้อมูล", result["answer"])

    def test_uses_llm_grounded_in_normalized_fields_only(self):
        reg, action_id = _customer_lookup_action()
        action = reg.get_full(action_id, mask_secrets=True)
        normalized = {"wallet.balance": {"label": "Wallet Balance", "value": 1200, "semantic_type": "currency"}}
        fake_llm = MagicMock()
        fake_llm.generate.return_value = LLMResponse(text="Your wallet balance is 1,200 THB.", model="gpt-4o",
                                                       provider="openai", input_tokens=10, output_tokens=10, latency_ms=50.0)
        with patch("services.erp_test_harness.get_llm_service", return_value=fake_llm):
            result = harness.generate_grounded_answer("ยอด wallet เท่าไหร่", action, normalized)
        self.assertTrue(result["grounded"])
        self.assertIn("1,200", result["answer"])
        sent_text = str(fake_llm.generate.call_args[0][0])
        self.assertIn("Wallet Balance", sent_text)
        self.assertIn("1200", sent_text)


class TestRunErpTestScenarios(unittest.TestCase):
    """Live-verification scenarios A-D from the spec, exercised directly
    (mocked LLM only — never a real network call)."""

    def setUp(self):
        self.reg, self.action_id = _customer_lookup_action()
        self.fake_llm = MagicMock()
        self.fake_llm.generate.return_value = LLMResponse(
            text="Your wallet balance is 1,200 THB.", model="gpt-4o", provider="openai",
            input_tokens=10, output_tokens=10, latency_ms=50.0)

    def test_scenario_a_missing_parameter_blocks_and_clarifies(self):
        with patch("services.business_action_registry.get_registry", return_value=self.reg):
            result = harness.run_erp_test(sb=self.reg._sb, action_id=self.action_id, message="ขอดูข้อมูลลูกค้า", mode="simulation")
        self.assertTrue(result["ok"])
        self.assertEqual(result["summary"]["overall"], "warning")
        self.assertIsNotNone(result["clarification_question"])
        self.assertEqual(result["missing_parameters"], ["CustCode"])
        # ERP must NOT have been called — no erp_execution "ok"/"simulated" step recorded
        erp_steps = [t for t in result["trace"] if t["step"] == "erp_execution"]
        self.assertEqual(erp_steps, [])

    def test_scenario_b_multiturn_binds_from_prior_context(self):
        with patch("services.business_action_registry.get_registry", return_value=self.reg):
            turn1 = harness.run_erp_test(sb=self.reg._sb, action_id=self.action_id, message="ขอดูข้อมูลลูกค้า", mode="intent_param")
            self.assertEqual(turn1["missing_parameters"], ["CustCode"])
            turn2 = harness.run_erp_test(sb=self.reg._sb, action_id=self.action_id, message="C00001", mode="intent_param",
                                          collected_params=turn1["collected_params"])
        self.assertEqual(turn2["collected_params"].get("CustCode"), "C00001")
        self.assertEqual(turn2["missing_parameters"], [])

    def test_contract_resolution_trace_step_sources_operation_type(self):
        """Part 14 — run_erp_test() now resolves the Integration Contract
        (services/integration_contract_service.py) once up front and
        sources operation_type/conversation behavior from it rather than
        re-deriving them a second, parallel way; a "contract_resolution"
        trace step surfaces this."""
        with patch("services.business_action_registry.get_registry", return_value=self.reg):
            result = harness.run_erp_test(sb=self.reg._sb, action_id=self.action_id, message="C00001", mode="simulation")
        contract_steps = [t for t in result["trace"] if t["step"] == "contract_resolution"]
        self.assertEqual(len(contract_steps), 1)
        self.assertEqual(contract_steps[0]["status"], "ok")
        self.assertIsNotNone(contract_steps[0]["output_summary"]["operation_type"])
        self.assertIsNotNone(contract_steps[0]["output_summary"]["capability"])

    def test_scenario_c_intent_param_never_calls_erp(self):
        with patch("services.business_action_registry.get_registry", return_value=self.reg):
            result = harness.run_erp_test(sb=self.reg._sb, action_id=self.action_id, message="ขอดูข้อมูลลูกค้า C00001", mode="intent_param")
        self.assertEqual(result["collected_params"].get("CustCode"), "C00001")
        self.assertEqual(result["missing_parameters"], [])
        self.assertIsNone(result["answer"])  # intent_param mode never generates an answer

    def test_scenario_d_simulation_generates_grounded_answer(self):
        with patch("services.business_action_registry.get_registry", return_value=self.reg), \
             patch("services.erp_test_harness.get_llm_service", return_value=self.fake_llm):
            result = harness.run_erp_test(sb=self.reg._sb, action_id=self.action_id,
                                           message="ยอด wallet ของลูกค้า C00001 เท่าไหร่", mode="simulation")
        self.assertTrue(result["ok"])
        self.assertEqual(result["summary"]["overall"], "pass")
        self.assertIn("wallet.balance", result["normalized_result"])
        self.assertIsNotNone(result["answer"])

    def test_no_action_selected_is_a_friendly_error(self):
        result = harness.run_erp_test(sb=self.reg._sb, action_id="", message="hello", mode="intent_param")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "no_action_selected")

    def test_disabled_action_is_blocked(self):
        self.reg.update(self.action_id, {"enabled": False, "is_draft": False})
        with patch("services.business_action_registry.get_registry", return_value=self.reg):
            result = harness.run_erp_test(sb=self.reg._sb, action_id=self.action_id, message="hi", mode="intent_param")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "action_disabled")

    def test_live_mode_reuses_action_executor_and_reports_erp_error(self):
        """Scenario F — a live-mode ERP error must produce a friendly
        trace entry with NO secret leakage, using the EXISTING
        ActionExecutor (never a second executor)."""
        fake_executor = MagicMock()
        fake_executor.execute.return_value = {
            "status": "error", "result": {"status_code": 500, "request": {"endpoint": "https://fasttrade.in.th/x"}},
            "metadata": {}, "latency_ms": 42.0, "error": "API responded with status 500", "logs": [],
        }
        with patch("services.erp_test_harness.get_registry", return_value=self.reg), \
             patch.object(self.reg, "resolve_secret_parameters", return_value={"SecretCode": "resolved-value"}), \
             patch("services.erp_test_harness.ActionExecutor", return_value=fake_executor):
            result = harness.run_erp_test(sb=self.reg._sb, action_id=self.action_id,
                                           message="ขอดูข้อมูลลูกค้า C00001", mode="live")
        self.assertEqual(result["summary"]["overall"], "fail")
        self.assertEqual(result["warning"], "erp_execution_failed")
        full_trace_text = str(result["trace"])
        self.assertNotIn("REAL-SECRET", full_trace_text)

    def test_live_mode_passes_system_generated_values_to_executor(self):
        """Confirmed defect fix (2026-08-09, Postman 8-endpoint onboarding)
        — live mode's exec_context never included "system_values" at all,
        so a parameter sourced from input_source='system_generated' (e.g.
        a URL extracted from the raw message — see services/
        decision_engine.py::_extract_system_values, GetUrlProductDetail's
        real integration) could never resolve through this harness even
        though the exact same Business Action worked through the real
        Decision Engine path. Reuses _extract_system_values() — never a
        second extractor — so this only asserts the harness now passes
        the SAME values through, not that URL extraction itself changed."""
        fake_executor = MagicMock()
        fake_executor.execute.return_value = {
            "status": "success", "result": {"status_code": 200, "request": {}, "mapped_fields": {}},
            "metadata": {}, "latency_ms": 10.0, "error": None, "logs": [],
        }
        with patch("services.erp_test_harness.get_registry", return_value=self.reg), \
             patch.object(self.reg, "resolve_secret_parameters", return_value={"SecretCode": "resolved-value"}), \
             patch("services.erp_test_harness.ActionExecutor", return_value=fake_executor):
            harness.run_erp_test(sb=self.reg._sb, action_id=self.action_id,
                                  message="C00001 ลิงก์นี้ค่ะ https://detail.1688.com/offer/123456.html", mode="live")
        passed_context = fake_executor.execute.call_args.kwargs.get("context")
        self.assertIsNotNone(passed_context)
        self.assertIn("system_values", passed_context)
        self.assertEqual(passed_context["system_values"].get("URL"), "https://detail.1688.com/offer/123456.html")


def _fake_llm_echoing_fields():
    """A fake LLM whose reply just lists whichever field labels/values
    were actually present in the LAST prompt sent — lets tests assert
    on answer filtering (Part 7) without depending on real wording."""
    fake = MagicMock()

    def _generate(messages, **kwargs):
        user_msg = next((m["content"] for m in messages if m["role"] == "user"), "")
        return LLMResponse(text="ANSWERED:" + user_msg, model="gpt-4o", provider="openai",
                            input_tokens=10, output_tokens=10, latency_ms=10.0)
    fake.generate.side_effect = _generate
    return fake


class TestBuildAvailableResponseOptions(unittest.TestCase):
    def test_options_generated_from_response_mapping_not_hardcoded(self):
        reg, action_id = _customer_lookup_action_with_3_options()
        action = reg.get_full(action_id, mask_secrets=True)
        options = harness.build_available_response_options(action)
        canonical_names = [o["canonical_name"] for o in options]
        self.assertEqual(canonical_names, ["customer.name", "wallet.balance", "coupon.list"])
        self.assertEqual(options[1]["display_label_th"], "ยอดเงินในกระเป๋า")
        self.assertEqual(options[2]["display_label_th"], "คูปองที่ใช้ได้")

    def test_excludes_fields_without_usable_values(self):
        reg, action_id = _customer_lookup_action_with_3_options()
        action = reg.get_full(action_id, mask_secrets=True)
        normalized = {
            "customer.name": {"label": "Customer Name", "value": "Somchai", "semantic_type": "string"},
            "wallet.balance": {"label": "Wallet Balance", "value": None, "semantic_type": "currency"},
        }
        options = harness.build_available_response_options(action, normalized)
        self.assertEqual([o["canonical_name"] for o in options], ["customer.name"])


class TestDetectRequestedFields(unittest.TestCase):
    def setUp(self):
        reg, action_id = _customer_lookup_action_with_3_options()
        self.options = harness.build_available_response_options(reg.get_full(action_id, mask_secrets=True))

    def test_detects_wallet_balance_by_thai_alias(self):
        result = harness.detect_requested_fields("ยอดเงินในกระเป๋า", self.options)
        self.assertEqual(result["requested_fields"], ["wallet.balance"])

    def test_detects_by_numeric_selection(self):
        result = harness.detect_requested_fields("2", self.options)
        self.assertEqual(result["requested_fields"], ["wallet.balance"])

    def test_detects_all_keyword(self):
        result = harness.detect_requested_fields("ทั้งหมด", self.options)
        self.assertEqual(set(result["requested_fields"]), {"customer.name", "wallet.balance", "coupon.list"})

    def test_detects_multiple_fields_in_one_message(self):
        result = harness.detect_requested_fields("ขอดูชื่อและยอดเงิน", self.options)
        self.assertEqual(set(result["requested_fields"]), {"customer.name", "wallet.balance"})

    def test_no_match_for_unrelated_message(self):
        result = harness.detect_requested_fields("ขอดูวันเกิด", self.options)
        self.assertEqual(result["requested_fields"], [])

    def test_out_of_range_numeric_selection_no_match(self):
        result = harness.detect_requested_fields("99", self.options)
        self.assertEqual(result["requested_fields"], [])


class TestBuildClarificationMessage(unittest.TestCase):
    def test_lists_every_option_in_thai(self):
        reg, action_id = _customer_lookup_action_with_3_options()
        options = harness.build_available_response_options(reg.get_full(action_id, mask_secrets=True))
        msg = harness.build_clarification_message(options, language="th")
        self.assertIn("ชื่อลูกค้า", msg)
        self.assertIn("ยอดเงินในกระเป๋า", msg)
        self.assertIn("คูปองที่ใช้ได้", msg)


class TestRequestedInformationClarificationScenarios(unittest.TestCase):
    """Live-verification scenarios A-H from the spec."""

    def setUp(self):
        self.reg, self.action_id = _customer_lookup_action_with_3_options()
        self.fake_llm = _fake_llm_echoing_fields()

    def _run(self, message, **kwargs):
        with patch("services.business_action_registry.get_registry", return_value=self.reg), \
             patch("services.erp_test_harness.get_llm_service", return_value=self.fake_llm):
            return harness.run_erp_test(sb=self.reg._sb, action_id=self.action_id, message=message,
                                         mode="simulation", **kwargs)

    def test_scenario_a_identifier_only_asks_which_field_not_generic_fallback(self):
        result = self._run("C00001")
        self.assertTrue(result["ok"])
        self.assertIsNone(result["answer"])
        self.assertTrue(result["awaiting_information_selection"])
        self.assertIsNotNone(result["clarification_question"])
        # The core bug fix: never the old generic "unavailable" fallback.
        self.assertNotIn("ไม่สามารถใช้ได้", result["clarification_question"] or "")
        self.assertIn("ยอดเงินในกระเป๋า", result["clarification_question"])
        self.assertEqual(len(result["available_response_options"]), 3)

    def test_scenario_b_field_specified_in_same_message_answers_directly(self):
        result = self._run("ขอดูยอดเงินในกระเป๋าของรหัส C00001")
        self.assertTrue(result["ok"])
        self.assertFalse(result["awaiting_information_selection"])
        self.assertEqual(result["requested_fields"], ["wallet.balance"])
        self.assertIsNotNone(result["answer"])
        self.assertEqual(result["collected_params"].get("CustCode"), "C00001")

    def test_scenario_c_natural_language_follow_up_retains_custcode(self):
        turn1 = self._run("C00001")
        self.assertTrue(turn1["awaiting_information_selection"])
        turn2 = self._run("ยอดเงินในกระเป๋า", collected_params=turn1["collected_params"],
                           awaiting_information_selection=True,
                           available_response_options=turn1["available_response_options"],
                           last_normalized_result=turn1["last_normalized_result"])
        self.assertEqual(turn2["collected_params"].get("CustCode"), "C00001")
        self.assertEqual(turn2["requested_fields"], ["wallet.balance"])
        self.assertIsNotNone(turn2["answer"])
        self.assertFalse(turn2["awaiting_information_selection"])

    def test_scenario_d_numeric_option_selection(self):
        turn1 = self._run("C00001")
        turn2 = self._run("2", collected_params=turn1["collected_params"],
                           awaiting_information_selection=True,
                           available_response_options=turn1["available_response_options"],
                           last_normalized_result=turn1["last_normalized_result"])
        self.assertEqual(turn2["requested_fields"], ["wallet.balance"])

    def test_scenario_e_multiple_requested_fields_returns_both_only(self):
        result = self._run("ขอดูชื่อและคูปองของรหัส C00001")
        self.assertEqual(set(result["requested_fields"]), {"customer.name", "coupon.list"})
        self.assertNotIn("wallet.balance", result["normalized_result"])

    def test_scenario_f_unavailable_field_shows_options_not_generic_fallback(self):
        turn1 = self._run("C00001")
        turn2 = self._run("ขอดูวันเกิด", collected_params=turn1["collected_params"],
                           awaiting_information_selection=True,
                           available_response_options=turn1["available_response_options"],
                           last_normalized_result=turn1["last_normalized_result"])
        self.assertTrue(turn2["awaiting_information_selection"])
        self.assertIsNone(turn2["answer"])
        self.assertIn("ยอดเงินในกระเป๋า", turn2["clarification_question"])
        self.assertNotIn("ไม่สามารถใช้ได้", turn2["clarification_question"])

    def test_scenario_g_all_keyword_returns_every_approved_field(self):
        result = self._run("ทั้งหมด ของรหัส C00001")
        self.assertEqual(set(result["requested_fields"]), {"customer.name", "wallet.balance", "coupon.list"})

    def test_scenario_h_empty_erp_result_never_asks_for_field_selection(self):
        # Force an empty simulated response by stripping response_mapping
        # before simulating — represents "no record found."
        reg2, action_id2 = _customer_lookup_action_with_3_options()
        reg2.replace_response_mapping(action_id2, [])
        with patch("services.business_action_registry.get_registry", return_value=reg2), \
             patch("services.erp_test_harness.get_llm_service", return_value=self.fake_llm):
            result = harness.run_erp_test(sb=reg2._sb, action_id=action_id2, message="C00001", mode="simulation")
        self.assertFalse(result["awaiting_information_selection"])
        self.assertIsNone(result["clarification_question"])
        self.assertEqual(result["warning"], "erp_data_unavailable")

    def test_single_visible_option_never_asks_answers_directly(self):
        reg2, action_id2 = _customer_lookup_action()  # only 1 non-secret response mapping available? actually 2
        reg2.replace_response_mapping(action_id2, [{"json_path": "$.customer_data.WalletBalance", "mapped_label": "Wallet Balance"}])
        with patch("services.business_action_registry.get_registry", return_value=reg2), \
             patch("services.erp_test_harness.get_llm_service", return_value=self.fake_llm):
            result = harness.run_erp_test(sb=reg2._sb, action_id=action_id2, message="C00001", mode="simulation")
        self.assertFalse(result["awaiting_information_selection"])
        self.assertIsNotNone(result["answer"])

    def test_new_missing_parameter_clears_awaiting_selection_state(self):
        """A fresh, unrelated turn asking for a NEW search parameter
        must never be confused with an in-progress field-selection
        answer — situations 1 and 2 must never be conflated."""
        turn1 = self._run("C00001")
        self.assertTrue(turn1["awaiting_information_selection"])
        # Simulate a brand new conversation state where CustCode was
        # cleared (e.g. admin reset mid-flow) — should ask for CustCode
        # again, never misinterpret as an information-selection reply.
        turn2 = self._run("สวัสดีครับ", collected_params={}, awaiting_information_selection=True,
                           available_response_options=turn1["available_response_options"],
                           last_normalized_result=turn1["last_normalized_result"])
        self.assertFalse(turn2["awaiting_information_selection"])
        self.assertIn("CustCode", turn2["missing_parameters"])


class TestCrossDomainGenericity(unittest.TestCase):
    """Product-Agnostic Architecture requirement — proves the exact same
    services/erp_test_harness.py code (zero changes per fixture) handles
    completely different entities/domains: Order lookup, Product
    lookup, Invoice lookup. If any of these needed a source change, the
    "no domain-specific branching" requirement would be violated."""

    def setUp(self):
        self.fake_llm = _fake_llm_echoing_fields()

    def _run(self, reg, action_id, message, **kwargs):
        with patch("services.business_action_registry.get_registry", return_value=reg), \
             patch("services.erp_test_harness.get_llm_service", return_value=self.fake_llm):
            return harness.run_erp_test(sb=reg._sb, action_id=action_id, message=message, mode="simulation", **kwargs)

    # ── Fixture 1: Order lookup ──────────────────────────────────────────
    def test_order_lookup_semantic_classification(self):
        reg, action_id = _order_lookup_action()
        action = reg.get_full(action_id, mask_secrets=True)
        options = harness.build_available_response_options(action)
        self.assertEqual({o["canonical_name"] for o in options}, {"order.status", "tracking.identifier", "order.balance"})

    def test_order_lookup_asks_which_field_with_only_identifier(self):
        reg, action_id = _order_lookup_action()
        result = self._run(reg, action_id, "PO202601001")
        self.assertTrue(result["awaiting_information_selection"])
        self.assertIsNotNone(result["clarification_question"])
        # The Thai display label (auto-derived, never hardcoded per
        # domain) takes priority in the rendered message over the raw
        # English business_label — assert on the OPTIONS themselves
        # (which do carry business_label) rather than the localized text.
        self.assertEqual({o["business_label"] for o in result["available_response_options"]},
                          {"Order Status", "Tracking Number", "Order Total"})

    def test_order_lookup_direct_field_request_answers_immediately(self):
        reg, action_id = _order_lookup_action()
        result = self._run(reg, action_id, "What's the tracking number for order PO202601001?")
        self.assertEqual(result["requested_fields"], ["tracking.identifier"])
        self.assertIsNotNone(result["answer"])
        self.assertNotIn("order.status", result["normalized_result"])

    # ── Fixture 2: Product lookup ────────────────────────────────────────
    def test_product_lookup_semantic_classification(self):
        reg, action_id = _product_lookup_action()
        action = reg.get_full(action_id, mask_secrets=True)
        options = harness.build_available_response_options(action)
        canonical_names = {o["canonical_name"] for o in options}
        # "Price" and "Stock" are generic currency/quantity concepts —
        # deliberately grouped under the same "balance"/"count" attr as
        # wallet balance / coupon count, not a new per-domain attribute.
        self.assertEqual(canonical_names, {"product.name", "product.balance", "product.count"})

    def test_product_lookup_asks_which_field(self):
        reg, action_id = _product_lookup_action()
        result = self._run(reg, action_id, "SKU-1001")
        self.assertTrue(result["awaiting_information_selection"])
        self.assertEqual({o["business_label"] for o in result["available_response_options"]},
                          {"Product Name", "Price", "Stock Quantity"})

    def test_product_lookup_direct_price_request(self):
        reg, action_id = _product_lookup_action()
        result = self._run(reg, action_id, "What's the price of SKU-1001?")
        self.assertEqual(result["requested_fields"], ["product.balance"])
        self.assertIsNotNone(result["answer"])

    # ── Fixture 3: Invoice lookup ────────────────────────────────────────
    def test_invoice_lookup_semantic_classification(self):
        reg, action_id = _invoice_lookup_action()
        action = reg.get_full(action_id, mask_secrets=True)
        options = harness.build_available_response_options(action)
        canonical_names = {o["canonical_name"] for o in options}
        self.assertEqual(canonical_names, {"invoice.balance", "invoice.date", "payment.status"})

    def test_invoice_lookup_asks_which_field(self):
        reg, action_id = _invoice_lookup_action()
        result = self._run(reg, action_id, "INV-2026-001")
        self.assertTrue(result["awaiting_information_selection"])
        self.assertEqual({o["business_label"] for o in result["available_response_options"]},
                          {"Invoice Amount", "Due Date", "Payment Status"})

    def test_invoice_lookup_multiple_fields_requested(self):
        reg, action_id = _invoice_lookup_action()
        result = self._run(reg, action_id, "What's the amount and due date for invoice INV-2026-001?")
        self.assertEqual(set(result["requested_fields"]), {"invoice.balance", "invoice.date"})
        self.assertNotIn("payment.status", result["normalized_result"])

    def test_invoice_lookup_numeric_selection_after_clarification(self):
        reg, action_id = _invoice_lookup_action()
        turn1 = self._run(reg, action_id, "INV-2026-001")
        turn2 = self._run(reg, action_id, "3", collected_params=turn1["collected_params"],
                           awaiting_information_selection=True,
                           available_response_options=turn1["available_response_options"],
                           last_normalized_result=turn1["last_normalized_result"])
        self.assertEqual(turn2["requested_fields"], ["payment.status"])
        self.assertIsNotNone(turn2["answer"])

    # ── Operation Type gating ────────────────────────────────────────────
    def test_lookup_action_infers_lookup_operation_type(self):
        reg, action_id = _order_lookup_action()
        action = reg.get_full(action_id, mask_secrets=True)
        self.assertEqual(harness.infer_operation_type(action), "LOOKUP")

    def test_command_action_never_asks_for_field_selection(self):
        """A CANCEL-style command action must never be offered the
        'what would you like to check?' clarification, even with
        multiple visible response fields configured."""
        reg, action_id = _cancel_order_action()
        action_full = reg.get_full(action_id, mask_secrets=True)
        self.assertEqual(harness.infer_operation_type(action_full), "CANCEL")
        result = self._run(reg, action_id, "PO202601001")
        self.assertFalse(result["awaiting_information_selection"])
        self.assertIsNotNone(result["answer"])  # answers directly with the full result, never asks


class TestBuildGenericActionModel(unittest.TestCase):
    """Part 2 — the domain-agnostic Integration Action Model."""

    def test_customer_action_model_shape(self):
        reg, action_id = _customer_lookup_action_with_3_options()
        action = reg.get_full(action_id, mask_secrets=True)
        model = harness.build_generic_action_model(action)
        for key in ("action_id", "name", "operation_type", "capability", "entities", "input_fields",
                    "required_field_groups", "response_fields", "conversation_behavior",
                    "execution_config", "semantic_metadata"):
            self.assertIn(key, model)
        self.assertEqual(model["operation_type"], "LOOKUP")
        self.assertEqual([f["name"] for f in model["input_fields"]], ["CustCode"])
        self.assertEqual(len(model["response_fields"]), 3)
        self.assertEqual(model["execution_config"]["http_method"], "POST")

    def test_cancel_action_model_marks_non_lookup(self):
        reg, action_id = _cancel_order_action()
        action = reg.get_full(action_id, mask_secrets=True)
        model = harness.build_generic_action_model(action)
        self.assertEqual(model["operation_type"], "CANCEL")
        self.assertFalse(model["semantic_metadata"]["is_lookup_like"])
        self.assertFalse(model["conversation_behavior"]["ask_requested_information"])

    def test_order_action_includes_parameter_groups(self):
        reg, action_id = _customer_lookup_action()
        reg.set_parameter_groups(action_id, [{"name": "identifier", "rule": "AT_LEAST_ONE", "members": ["CustCode"]}])
        action = reg.get_full(action_id, mask_secrets=True)
        model = harness.build_generic_action_model(action)
        self.assertEqual(model["required_field_groups"], [{"name": "identifier", "rule": "AT_LEAST_ONE", "members": ["CustCode"]}])


class TestGetConversationBehavior(unittest.TestCase):
    """Part 7 — operation-type-derived defaults, admin override wins."""

    def test_lookup_defaults_ask_requested_information(self):
        reg, action_id = _customer_lookup_action()
        action = reg.get_full(action_id, mask_secrets=True)
        behavior = harness.get_conversation_behavior(action)
        self.assertTrue(behavior["ask_requested_information"])
        self.assertFalse(behavior["require_confirmation_before_execute"])

    def test_cancel_defaults_require_confirmation_not_ask(self):
        reg, action_id = _cancel_order_action()
        action = reg.get_full(action_id, mask_secrets=True)
        behavior = harness.get_conversation_behavior(action)
        self.assertFalse(behavior["ask_requested_information"])
        self.assertTrue(behavior["require_confirmation_before_execute"])

    def test_explicit_setup_metadata_override_wins(self):
        reg, action_id = _customer_lookup_action()
        reg.update(action_id, {"setup_metadata": {"conversation_behavior": {"ask_requested_information": False}}})
        action = reg.get_full(action_id, mask_secrets=True)
        behavior = harness.get_conversation_behavior(action)
        self.assertFalse(behavior["ask_requested_information"])


class TestSaferOperationTypeInference(unittest.TestCase):
    """Part 8/9 — structural, confidence-scored operation-type inference
    that never lets weak description prose alone drive a destructive
    classification."""

    def test_description_never_causes_false_delete_classification(self):
        """The exact bug that motivated this sprint: an Order Lookup
        action whose ADMIN-AUTHORED DESCRIPTION happens to contain
        'safe to delete' must still resolve to LOOKUP, never DELETE —
        regardless of the description wording, because description text
        is only ever consulted as WEAK evidence and can never alone
        justify a destructive/command classification."""
        reg, action_id = _order_lookup_action()
        reg.update(action_id, {"description": "This record is disposable and safe to delete once verification is done."})
        action = reg.get_full(action_id, mask_secrets=True)
        evidence = harness.infer_operation_type_with_evidence(action)
        self.assertEqual(evidence["operation_type"], "LOOKUP")
        self.assertNotEqual(evidence["operation_type"], "DELETE")
        self.assertEqual(harness.infer_operation_type(action), "LOOKUP")

    def test_capability_text_only_never_reads_description(self):
        """Signal 4 (name/capability semantic analysis) must only ever
        consult capability/action_key/display_name — never description
        prose — even when the description contains a strong verb match
        for a DIFFERENT operation than the capability itself implies."""
        reg, action_id = _order_lookup_action()
        reg.update(action_id, {"description": "Please cancel and remove any stale cached copy after reading this."})
        action = reg.get_full(action_id, mask_secrets=True)
        evidence = harness.infer_operation_type_with_evidence(action)
        self.assertEqual(evidence["operation_type"], "LOOKUP")

    def test_weak_text_evidence_prefers_unknown_over_destructive_guess(self):
        """An action with NO structural signal at all (no response
        mapping, no GET method, no recognizable capability wording) and
        only description text mentioning a destructive verb must return
        UNKNOWN, never guess CANCEL/DELETE/CREATE/UPDATE/NOTIFY from weak
        text evidence alone."""
        reg = BusinessActionRegistry(_FakeSupabase())
        action = reg.create({"action_key": "mystery_action", "name": "MysteryAction",
                              "display_name": "Mystery Action", "action_type": "API",
                              "category": "misc", "enabled": True})
        action_id = action["id"]
        reg.upsert_execution(action_id, {"endpoint": "https://example-erp.test/mystery", "http_method": "POST"})
        reg.update(action_id, {"description": "This will cancel and delete the pending record."})
        action_full = reg.get_full(action_id, mask_secrets=True)
        evidence = harness.infer_operation_type_with_evidence(action_full)
        self.assertEqual(evidence["operation_type"], "UNKNOWN")
        self.assertLessEqual(evidence["confidence"], 0.5)
        self.assertNotIn(evidence["operation_type"], ("CANCEL", "DELETE", "CREATE", "UPDATE", "NOTIFY"))

    def test_unknown_treated_conservatively_like_a_command(self):
        defaults = harness.get_conversation_behavior_defaults("UNKNOWN")
        self.assertFalse(defaults["ask_requested_information"])
        self.assertTrue(defaults["require_confirmation_before_execute"])
        self.assertNotIn("UNKNOWN", harness.LOOKUP_LIKE_OPERATION_TYPES)

    def test_explicit_schema_override_has_full_confidence(self):
        reg, action_id = _order_lookup_action()
        action = reg.get_full(action_id, mask_secrets=True)
        evidence = harness.infer_operation_type_with_evidence(action, schema={"general": {"operation_type": "CANCEL"}})
        self.assertEqual(evidence["operation_type"], "CANCEL")
        self.assertEqual(evidence["confidence"], 1.0)


class TestDetectRequestedFieldsCandidates(unittest.TestCase):
    """Part 5 — the additive scored `candidates` list."""

    def test_candidates_present_and_scored(self):
        reg, action_id = _customer_lookup_action_with_3_options()
        options = harness.build_available_response_options(reg.get_full(action_id, mask_secrets=True))
        result = harness.detect_requested_fields("ยอดเงินในกระเป๋า", options)
        self.assertIn("candidates", result)
        self.assertTrue(any(c["canonical_name"] == "wallet.balance" for c in result["candidates"]))
        for c in result["candidates"]:
            self.assertGreater(c["score"], 0)
            self.assertIn("evidence", c)

    def test_candidates_empty_list_when_no_match(self):
        reg, action_id = _customer_lookup_action_with_3_options()
        options = harness.build_available_response_options(reg.get_full(action_id, mask_secrets=True))
        result = harness.detect_requested_fields("ขอดูวันเกิด", options)
        self.assertEqual(result["candidates"], [])


class TestExtendedResponseOptionFields(unittest.TestCase):
    """Part 4 — display_labels/aliases/visible/answerable/sensitive additions."""

    def test_options_carry_new_additive_keys(self):
        reg, action_id = _customer_lookup_action_with_3_options()
        options = harness.build_available_response_options(reg.get_full(action_id, mask_secrets=True))
        for o in options:
            self.assertIn("display_labels", o)
            self.assertIn("th", o["display_labels"])
            self.assertIn("aliases", o)
            self.assertTrue(o["visible"])
            self.assertTrue(o["answerable"])
            self.assertFalse(o["sensitive"])

    def test_configured_alias_used_when_present(self):
        # NOTE: services/business_action_registry.py's save flow
        # (replace_response_mapping) is frozen per the sprint's standing
        # constraints and does not yet persist a field_metadata column
        # (see the migration noted in the sprint report) — so this test
        # exercises harness functions directly against an in-memory
        # action dict carrying field_metadata, exactly how a future
        # registry version (once the additive column/migration is
        # applied) would hand it to this module.
        reg, action_id = _customer_lookup_action()
        action = reg.get_full(action_id, mask_secrets=True)
        action["response_mapping"] = [
            {"json_path": "$.customer_data.WalletBalance", "mapped_label": "Wallet Balance",
             "field_metadata": {"aliases": ["เครดิต"], "display_labels": {"th": "เครดิตของฉัน"}}},
        ]
        options = harness.build_available_response_options(action)
        self.assertEqual(options[0]["aliases"], ["เครดิต"])
        self.assertEqual(options[0]["display_labels"]["th"], "เครดิตของฉัน")
        result = harness.detect_requested_fields("เครดิต", options)
        self.assertEqual(result["requested_fields"], ["wallet.balance"])

    def test_sensitive_field_excluded_from_options(self):
        reg, action_id = _customer_lookup_action()
        reg.replace_response_mapping(action_id, [
            {"json_path": "$.customer_data.CustName", "mapped_label": "Customer Name"},
            {"json_path": "$.customer_data.SecretToken", "mapped_label": "Secret Token"},
        ])
        action = reg.get_full(action_id, mask_secrets=True)
        options = harness.build_available_response_options(action)
        self.assertEqual([o["canonical_name"] for o in options], ["customer.name"])


class TestExtendedSemanticTypes(unittest.TestCase):
    """Part 10 — percentage/boolean/url/address detection."""

    def test_percentage(self):
        self.assertEqual(harness.semantic_classify("DiscountRate")["semantic_type"], "percentage")

    def test_boolean(self):
        self.assertEqual(harness.semantic_classify("IsActive")["semantic_type"], "boolean")

    def test_url(self):
        self.assertEqual(harness.semantic_classify("TrackingUrl")["semantic_type"], "url")

    def test_address(self):
        self.assertEqual(harness.semantic_classify("ShippingAddress")["semantic_type"], "address")


class TestNormalizeResponseWrapped(unittest.TestCase):
    """Part 10 — additive record_type wrapper (kept separate from the
    flat canonical-keyed dict normalize_response() itself returns)."""

    def test_single_record_type(self):
        reg, action_id = _customer_lookup_action()
        action = reg.get_full(action_id, mask_secrets=True)
        wrapped = harness.normalize_response_wrapped(action, {"Customer Name": "Somchai", "Wallet Balance": 1200})
        self.assertEqual(wrapped["record_type"], "single")
        self.assertEqual(wrapped["fields"]["customer.name"]["value"], "Somchai")

    def test_collection_record_type(self):
        reg, action_id = _customer_lookup_action_with_3_options()
        action = reg.get_full(action_id, mask_secrets=True)
        wrapped = harness.normalize_response_wrapped(action, {"Available Coupons": ["A", "B"]})
        self.assertEqual(wrapped["record_type"], "collection")


class TestRunIntegrationConversationTurn(unittest.TestCase):
    """Part 9 — the generic-named adapter over run_erp_test()."""

    def test_adapter_matches_run_erp_test_behavior(self):
        reg, action_id = _customer_lookup_action()
        fake_llm = _fake_llm_echoing_fields()
        with patch("services.erp_test_harness.get_registry", return_value=reg), \
             patch("services.erp_test_harness.get_llm_service", return_value=fake_llm):
            result = harness.run_integration_conversation_turn(
                sb=reg._sb, action_id=action_id, message="ยอด wallet ของลูกค้า C00001 เท่าไหร่", mode="simulation")
        for key in ("reply", "conversation_state", "execution_result", "normalized_result", "trace", "status"):
            self.assertIn(key, result)
        self.assertEqual(result["status"], "pass")
        self.assertIsNotNone(result["reply"])
        self.assertEqual(result["conversation_state"]["collected_parameters"].get("CustCode"), "C00001")

    def test_adapter_carries_conversation_state_across_turns(self):
        reg, action_id = _customer_lookup_action_with_3_options()
        fake_llm = _fake_llm_echoing_fields()
        with patch("services.erp_test_harness.get_registry", return_value=reg), \
             patch("services.erp_test_harness.get_llm_service", return_value=fake_llm):
            turn1 = harness.run_integration_conversation_turn(sb=reg._sb, action_id=action_id, message="C00001", mode="simulation")
            self.assertTrue(turn1["conversation_state"]["awaiting_information_selection"])
            turn2 = harness.run_integration_conversation_turn(
                sb=reg._sb, action_id=action_id, message="ยอดเงินในกระเป๋า", mode="simulation",
                conversation_state=turn1["conversation_state"])
        self.assertFalse(turn2["conversation_state"]["awaiting_information_selection"])
        self.assertIsNotNone(turn2["reply"])


class TestConversationFormGeneratorWiring(unittest.TestCase):
    """Part 7 (Conversation Form Generator sprint) — run_erp_test() must
    ADDITIVELY expose `conversation_form`/`conversation_state` without
    changing any pre-existing return key or existing caller behavior."""

    def setUp(self):
        self.fake_llm = MagicMock()
        self.fake_llm.generate.return_value = LLMResponse(
            text="Your order has been cancelled.", model="gpt-4o", provider="openai",
            input_tokens=10, output_tokens=10, latency_ms=50.0)

    def test_missing_parameter_turn_exposes_conversation_form_and_state(self):
        reg, action_id = _customer_lookup_action()
        with patch("services.business_action_registry.get_registry", return_value=reg):
            result = harness.run_erp_test(sb=reg._sb, action_id=action_id, message="ขอดูข้อมูลลูกค้า", mode="simulation")
        self.assertIn("conversation_form", result)
        self.assertIn("conversation_state", result)
        self.assertEqual(result["conversation_state"]["current_state"], "WaitingInput")
        self.assertIsNotNone(result["conversation_form"])
        self.assertFalse(result["conversation_form"]["ready_to_execute"])
        # Pre-existing keys must still be present/unchanged.
        self.assertEqual(result["missing_parameters"], ["CustCode"])

    def test_confirmation_gate_not_enforced_by_default_existing_behavior_unchanged(self):
        """Scenario F must be entirely opt-in — a pre-existing caller
        that never passes enforce_confirmation_gate keeps executing a
        COMMAND-type action directly, exactly as before this sprint."""
        reg, action_id = _cancel_order_action()
        with patch("services.business_action_registry.get_registry", return_value=reg), \
             patch("services.erp_test_harness.get_llm_service", return_value=self.fake_llm):
            result = harness.run_erp_test(sb=reg._sb, action_id=action_id, message="PO202601001", mode="simulation")
        self.assertIsNotNone(result["answer"])
        gate_steps = [t for t in result["trace"] if t["step"] == "confirmation_gate"]
        self.assertEqual(len(gate_steps), 1)
        self.assertEqual(gate_steps[0]["status"], "skipped")

    def test_confirmation_gate_blocks_then_confirms_scenario_f(self):
        """Scenario F, opted in — a COMMAND-type action with
        require_confirmation_before_execute=true must transition to
        WaitingConfirmation and block execution until confirmed=True."""
        reg, action_id = _cancel_order_action()
        with patch("services.business_action_registry.get_registry", return_value=reg), \
             patch("services.erp_test_harness.get_llm_service", return_value=self.fake_llm):
            blocked = harness.run_erp_test(sb=reg._sb, action_id=action_id, message="PO202601001", mode="simulation",
                                            enforce_confirmation_gate=True)
            self.assertIsNone(blocked["answer"])
            self.assertEqual(blocked["warning"], "awaiting_confirmation")
            self.assertEqual(blocked["conversation_state"]["current_state"], "WaitingConfirmation")

            confirmed = harness.run_erp_test(sb=reg._sb, action_id=action_id, message="PO202601001", mode="simulation",
                                              enforce_confirmation_gate=True, confirmed=True,
                                              conversation_form_state=blocked["conversation_state"])
        self.assertIsNotNone(confirmed["answer"])
        self.assertEqual(confirmed["conversation_state"]["current_state"], "Completed")


if __name__ == "__main__":
    unittest.main()
