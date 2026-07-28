"""Route-level tests for AI Auto Setup (admin/routes.py's ai-auto-setup
endpoints) — uses the real FastAPI app with a mocked Supabase-backed
registry (_FakeSupabase), never a real DB. The LLM call is always
mocked here — no real network/LLM call in this test file."""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from starlette.testclient import TestClient

from tests.test_business_action_registry import _FakeSupabase
from services.business_action_registry import BusinessActionRegistry


def _valid_proposal(action_id="new_action_from_ai"):
    return {
        "action_name": "Test Action", "display_name": "ทดสอบ", "action_id": action_id,
        "description": "ทดสอบ purpose", "category": "test", "action_type": "API",
        "http_method": "GET", "base_url": "https://example.test", "endpoint_path": "/api",
        "content_type": "application/json", "headers": {},
        "parameters": [
            {"name": "Foo", "display_name": "Foo", "required": True, "input_source": "customer_message",
             "secret_ref": None, "example_value": "F1", "validation_type": "non_empty",
             "validation_pattern": None, "validation_confidence": "high",
             "follow_up_options": ["กรุณาแจ้ง Foo ครับ"]},
        ],
        "parameter_groups": [], "keywords": ["test"], "example_questions": ["test question"],
        "response_mapping": [], "customer_facing_response_template": "{Foo}",
    }


class TestAiAutoSetupRoutes(unittest.TestCase):
    def setUp(self):
        from admin.routes import app
        self.client = TestClient(app)
        self.client.post("/admin/login", data={"username": "admin", "password": "shipify2026"})
        self.fake_sb = _FakeSupabase()
        self.registry = BusinessActionRegistry(self.fake_sb)
        self.patcher = patch("admin.routes.get_sb", return_value=self.fake_sb)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()

    def test_analyze_requires_api_input_and_purpose(self):
        resp = self.client.post("/admin/api/business-actions/ai-auto-setup/analyze",
                                 json={"api_input": "", "business_purpose": ""})
        self.assertEqual(resp.status_code, 400)

    def test_analyze_returns_structured_proposal(self):
        with patch("services.ai_auto_setup_service.analyze_capability",
                   return_value={"ok": True, "proposal": _valid_proposal(), "errors": [],
                                 "redacted_secrets_found": [], "raw_text": "{}"}):
            resp = self.client.post("/admin/api/business-actions/ai-auto-setup/analyze",
                                     json={"user_input": "GET /api", "business_purpose": "test",
                                           "example_questions": ["q1"]})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["proposal"]["action_id"], "new_action_from_ai")

    def test_save_creates_new_draft_action(self):
        resp = self.client.post("/admin/api/business-actions/ai-auto-setup/save",
                                 json={"proposal": _valid_proposal("brand_new_action"), "enabled": False})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertFalse(data["was_update"])
        self.assertFalse(data["action"]["enabled"])

    def test_save_updates_existing_action_without_duplicating(self):
        existing = self.registry.create({"action_key": "search_data_order", "name": "SearchDataOrder",
                                          "action_type": "API", "enabled": True})
        resp = self.client.post("/admin/api/business-actions/ai-auto-setup/save",
                                 json={"proposal": _valid_proposal("search_data_order"), "enabled": False})
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["was_update"])
        self.assertEqual(data["action"]["id"], existing["id"])
        all_actions = self.registry.list()
        self.assertEqual(len([a for a in all_actions if a["action_key"] == "search_data_order"]), 1)

    def test_saving_draft_never_disables_an_already_enabled_existing_action(self):
        """Regression: Test Connection / Save Draft on an existing,
        already-enabled permanent action must not silently disable it."""
        existing = self.registry.create({"action_key": "search_data_order", "name": "SearchDataOrder",
                                          "action_type": "API", "enabled": True})
        resp = self.client.post("/admin/api/business-actions/ai-auto-setup/save",
                                 json={"proposal": _valid_proposal("search_data_order"), "enabled": False})
        data = resp.json()
        self.assertTrue(data["action"]["enabled"])

    def test_invalid_proposal_schema_rejected(self):
        bad_proposal = _valid_proposal()
        del bad_proposal["endpoint_path"]
        resp = self.client.post("/admin/api/business-actions/ai-auto-setup/save",
                                 json={"proposal": bad_proposal, "enabled": False})
        self.assertEqual(resp.status_code, 400)

    def test_enable_check_route(self):
        action = self.registry.create({"action_key": "incomplete_one", "name": "Incomplete", "action_type": "API"})
        resp = self.client.get(f"/admin/api/business-actions/{action['id']}/enable-check")
        data = resp.json()
        self.assertFalse(data["ok"] and data.get("reasons") == [])
        self.assertIn("missing_endpoint", data["reasons"])

    def test_detect_endpoints_route_returns_empty_for_plain_curl(self):
        resp = self.client.post("/admin/api/business-actions/ai-auto-setup/detect-endpoints",
                                 json={"document_text": "curl https://example.com/api"})
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["endpoints"], [])
        self.assertFalse(data["is_multi"])

    def test_detect_endpoints_route_detects_postman_collection(self):
        import json
        collection = {"item": [
            {"name": "Customer Lookup", "request": {"method": "POST", "url": {"raw": "https://fasttrade.in.th/GetDataCustomer"}, "header": []}},
            {"name": "Order Lookup", "request": {"method": "POST", "url": {"raw": "https://fasttrade.in.th/GetDataOrder"}, "header": []}},
        ]}
        resp = self.client.post("/admin/api/business-actions/ai-auto-setup/detect-endpoints",
                                 json={"document_text": json.dumps(collection)})
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["is_multi"])
        self.assertEqual(len(data["endpoints"]), 2)

    def test_save_persists_search_info_and_default_routing(self):
        proposal = _valid_proposal("with_routing")
        proposal["search_info"] = "รหัสลูกค้า หรืออีเมล"
        proposal["routing_recommendation"] = "erp_then_kb"
        resp = self.client.post("/admin/api/business-actions/ai-auto-setup/save",
                                 json={"proposal": proposal, "enabled": False})
        data = resp.json()
        self.assertTrue(data["ok"])
        meta = data["action"]["setup_metadata"]
        self.assertEqual(meta["search_info"], "รหัสลูกค้า หรืออีเมล")
        self.assertEqual(meta["routing"]["source_preference"], "erp_then_kb")

    def test_save_preserves_existing_routing_on_re_save(self):
        """Re-running Test Connection / Save Draft from the AI wizard on an
        existing action must not silently wipe routing config an admin
        already set via the Advanced Editor's Routing tab."""
        existing = self.registry.create({
            "action_key": "has_routing", "name": "HasRouting", "action_type": "API", "enabled": True,
            "setup_metadata": {"routing": {"source_preference": "kb_then_erp", "intent": "custom_intent"}},
        })
        resp = self.client.post("/admin/api/business-actions/ai-auto-setup/save",
                                 json={"proposal": _valid_proposal("has_routing"), "enabled": False})
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["action"]["setup_metadata"]["routing"]["source_preference"], "kb_then_erp")
        self.assertEqual(data["action"]["setup_metadata"]["routing"]["intent"], "custom_intent")


    def test_part9_case5_edit_preserves_credential_ref_without_replacement(self):
        """Part 9, test case 5 — editing/re-saving an existing action whose
        secret is already in the Credential Store must NOT require the
        admin to re-type the secret, must NOT erase the stored credential
        reference, and must NEVER return a raw secret value anywhere in
        the response."""
        proposal = _valid_proposal("customer_lookup")
        proposal["parameters"].append({
            "name": "SecretCode", "display_name": "Secret Code", "required": True,
            "input_source": "credential_store", "credential_ref": "fasttrade_secret_code",
            "secret_ref": None, "example_value": None, "validation_type": None,
            "validation_pattern": None, "validation_confidence": "high", "follow_up_options": [],
        })
        # First save — WITH a credential value, simulating the original
        # cURL submission that created the stored credential.
        first = self.client.post("/admin/api/business-actions/ai-auto-setup/save", json={
            "proposal": proposal, "enabled": False,
            "credential_values": {"fasttrade_secret_code": "REAL-SECRET-VALUE-999"},
            "credential_display_names": {"fasttrade_secret_code": "FastTrade Secret Code"},
        })
        self.assertTrue(first.json()["ok"])
        self.assertNotIn("REAL-SECRET-VALUE-999", first.text)

        # Second save (an "edit" / re-run of Save Draft) — NO credential
        # value sent at all, exactly what happens when the admin didn't
        # touch the Credentials section on re-open.
        second = self.client.post("/admin/api/business-actions/ai-auto-setup/save",
                                   json={"proposal": proposal, "enabled": False})
        data = second.json()
        self.assertTrue(data["ok"])
        self.assertNotIn("REAL-SECRET-VALUE-999", second.text)
        params_by_name = {p["name"]: p for p in data["action"]["parameters"]}
        self.assertEqual(params_by_name["SecretCode"]["credential_ref"], "fasttrade_secret_code")
        self.assertEqual(params_by_name["SecretCode"]["input_source"], "credential_store")

    def test_suggest_questions_requires_display_name(self):
        resp = self.client.post("/admin/api/business-actions/ai-auto-setup/suggest-questions", json={})
        self.assertEqual(resp.status_code, 400)

    def test_suggest_questions_returns_generated_list(self):
        with patch("services.ai_auto_setup_service.generate_suggested_questions",
                   return_value=["ขอข้อมูลลูกค้า", "เช็คข้อมูลสมาชิก"]):
            resp = self.client.post("/admin/api/business-actions/ai-auto-setup/suggest-questions",
                                     json={"display_name": "ค้นหาข้อมูลลูกค้า", "category": "customer"})
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["questions"], ["ขอข้อมูลลูกค้า", "เช็คข้อมูลสมาชิก"])

    def test_create_duplicate_action_key_returns_friendly_error_not_raw_db_error(self):
        self.registry.create({"action_key": "dup_action", "name": "Dup", "action_type": "API"})
        resp = self.client.post("/admin/api/business-actions",
                                 json={"action_key": "dup_action", "name": "Dup Again", "action_type": "API"})
        self.assertEqual(resp.status_code, 409)
        data = resp.json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error_type"], "duplicate_action")
        self.assertEqual(data["error"], "Business Action already exists.")
        self.assertNotIn("duplicate key value violates", data["error"])
        self.assertEqual(set(data["options"]), {"update_existing", "save_as_new", "rename_action", "cancel"})

    def test_create_non_duplicate_action_succeeds_normally(self):
        resp = self.client.post("/admin/api/business-actions",
                                 json={"action_key": "brand_new_unique_key", "name": "Brand New", "action_type": "API"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])


class TestAiSetupSaveDuplicateBug(unittest.TestCase):
    """Reproduces the EXACT reported bug: the AI Setup screen's own save
    button (POST .../ai-auto-setup/save) — not the generic manual-editor
    create endpoint — leaked a raw Postgres duplicate-key error when the
    colliding row was soft-deleted (get_by_key()'s deleted_at filter hid
    it from the pre-save check, so the code fell through to reg.create()
    and hit the real unique constraint)."""

    def setUp(self):
        from admin.routes import app
        self.client = TestClient(app)
        self.client.post("/admin/login", data={"username": "admin", "password": "shipify2026"})
        self.fake_sb = _FakeSupabase()
        self.registry = BusinessActionRegistry(self.fake_sb)
        self.patcher = patch("admin.routes.get_sb", return_value=self.fake_sb)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()

    def test_reproduces_bug_duplicate_against_soft_deleted_action(self):
        # 1) Save once through the real AI Setup save endpoint.
        first = self.client.post("/admin/api/business-actions/ai-auto-setup/save",
                                  json={"proposal": _valid_proposal("get_data_customer"), "enabled": False})
        self.assertTrue(first.json()["ok"])
        action_id = first.json()["action"]["id"]

        # 2) Soft-delete it (e.g. admin deleted a draft, or a prior test
        # left one soft-deleted) — get_by_key() can no longer see it.
        self.registry.delete(action_id)
        self.assertIsNone(self.registry.get_by_key("get_data_customer"))

        # 3) Re-analyze and save the SAME key again through the real
        # AI Setup save button's endpoint — must NEVER leak a raw
        # Postgres error, and must not silently create a duplicate row.
        second = self.client.post("/admin/api/business-actions/ai-auto-setup/save",
                                   json={"proposal": _valid_proposal("get_data_customer"), "enabled": False})
        self.assertEqual(second.status_code, 409)
        data = second.json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error_type"], "duplicate_action")
        self.assertEqual(data["existing_action_id"], action_id)
        self.assertEqual(set(data["options"]), {"update_existing", "save_as_new", "rename_action", "cancel"})
        full_text = json.dumps(data)
        for leaked in ("duplicate key value violates", "unique constraint", "23505", "psycopg", "Traceback"):
            self.assertNotIn(leaked, full_text)

    def test_update_existing_restores_soft_deleted_row_without_duplicating(self):
        first = self.client.post("/admin/api/business-actions/ai-auto-setup/save",
                                  json={"proposal": _valid_proposal("get_data_customer"), "enabled": False})
        action_id = first.json()["action"]["id"]
        self.registry.delete(action_id)

        dup = self.client.post("/admin/api/business-actions/ai-auto-setup/save",
                                json={"proposal": _valid_proposal("get_data_customer"), "enabled": False})
        existing_id = dup.json()["existing_action_id"]

        proposal = _valid_proposal("get_data_customer")
        proposal["display_name"] = "ค้นหาข้อมูลลูกค้า (Updated)"
        resolved = self.client.post("/admin/api/business-actions/ai-auto-setup/save", json={
            "proposal": proposal, "enabled": False,
            "resolve_duplicate": {"action": "update_existing", "target_action_id": existing_id},
        })
        data = resolved.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["action"]["id"], existing_id)
        self.assertEqual(data["action"]["display_name"], "ค้นหาข้อมูลลูกค้า (Updated)")
        # Restored — no longer soft-deleted.
        self.assertIsNotNone(self.registry.get_by_key("get_data_customer"))
        all_rows = [r for r in self.fake_sb.store.get("business_actions", []) if r.get("action_key") == "get_data_customer"]
        self.assertEqual(len(all_rows), 1)  # never duplicated

    def test_save_as_new_via_suffixed_key_creates_second_action(self):
        first = self.client.post("/admin/api/business-actions/ai-auto-setup/save",
                                  json={"proposal": _valid_proposal("get_data_customer"), "enabled": False})
        self.assertTrue(first.json()["ok"])

        # Frontend's "Save as New" resolution: mutate the proposal's own
        # action_id client-side and resend the SAME save call.
        second = self.client.post("/admin/api/business-actions/ai-auto-setup/save",
                                   json={"proposal": _valid_proposal("get_data_customer_2"), "enabled": False})
        self.assertTrue(second.json()["ok"])
        all_rows = [r for r in self.fake_sb.store.get("business_actions", []) if not r.get("deleted_at")]
        self.assertEqual({r["action_key"] for r in all_rows}, {"get_data_customer", "get_data_customer_2"})

    def test_cancel_leaves_no_new_row_and_original_untouched(self):
        first = self.client.post("/admin/api/business-actions/ai-auto-setup/save",
                                  json={"proposal": _valid_proposal("get_data_customer"), "enabled": False})
        action_id = first.json()["action"]["id"]
        # "Cancel" is purely client-side (close the modal, do nothing) —
        # confirm no duplicate call was ever made server-side and the
        # original row is untouched.
        active = self.registry.list()
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["id"], action_id)

    def test_unexpected_db_error_never_leaks_stack_trace_or_details(self):
        with patch.object(BusinessActionRegistry, "create", side_effect=RuntimeError("connection reset by peer at 10.0.0.5:5432")):
            resp = self.client.post("/admin/api/business-actions/ai-auto-setup/save",
                                     json={"proposal": _valid_proposal("some_new_key_xyz"), "enabled": False})
        self.assertEqual(resp.status_code, 400)
        data = resp.json()
        self.assertFalse(data["ok"])
        self.assertNotIn("10.0.0.5", json.dumps(data))
        self.assertNotIn("connection reset", json.dumps(data))
        self.assertEqual(data["error"], "Unable to save the Business Action. Please try again or review the configuration.")

    def test_duplicate_key_exception_that_slips_past_precheck_still_gets_friendly_response(self):
        """Defense-in-depth (Part 5C level 2) — even if a duplicate-key
        exception reaches reg.create() itself (e.g. a race), the route
        must still translate it, never forward the raw message."""
        with patch.object(BusinessActionRegistry, "create",
                           side_effect=Exception('duplicate key value violates unique constraint '
                                                   '"business_actions_action_key_key"\nDETAIL: Key (action_key)=(race_key) already exists.')):
            resp = self.client.post("/admin/api/business-actions/ai-auto-setup/save",
                                     json={"proposal": _valid_proposal("race_key"), "enabled": False})
        self.assertEqual(resp.status_code, 409)
        data = resp.json()
        self.assertEqual(data["error_type"], "duplicate_action")
        self.assertNotIn("DETAIL", json.dumps(data))
        self.assertNotIn("unique constraint", json.dumps(data))


if __name__ == "__main__":
    unittest.main()
