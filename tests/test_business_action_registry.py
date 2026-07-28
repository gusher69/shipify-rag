"""Regression tests for the Business Action Center Registry Service
(services/business_action_registry.py) — CRUD, enable/disable, search,
parameter/execution/response-mapping/validation configuration, secret
masking, and embedding-source preparation. Uses a mocked Supabase
client (never a real DB), same pattern as tests/test_benchmark_service.py.
"""
import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.business_action_registry import (
    BusinessActionRegistry, mask_secret, mask_execution_secrets, build_embedding_source_text,
)


class _Query:
    def __init__(self, store, name):
        self.store = store
        self.name = name
        self._filters = {}
        self._is_filters = {}
        self._neq = None
        self._op = None
        self._payload = None
        self._order = None
        self._order_desc = False

    def select(self, *_a, **_k):
        self._op = "select"
        return self

    def insert(self, payload):
        self._op = "insert"
        self._payload = payload
        return self

    def update(self, payload):
        self._op = "update"
        self._payload = payload
        return self

    def delete(self):
        self._op = "delete"
        return self

    def eq(self, k, v):
        self._filters[k] = v
        return self

    def is_(self, k, v):
        self._is_filters[k] = v
        return self

    def order(self, k, desc=False):
        self._order = k
        self._order_desc = desc
        return self

    def _matched(self):
        rows = self.store.setdefault(self.name, [])
        out = []
        for r in rows:
            if all(r.get(k) == v for k, v in self._filters.items()):
                ok = True
                for k, v in self._is_filters.items():
                    if v == "null" and r.get(k) is not None:
                        ok = False
                if ok:
                    out.append(r)
        return out

    def execute(self):
        rows = self.store.setdefault(self.name, [])
        if self._op == "select":
            matched = self._matched()
            if self._order:
                matched = sorted(matched, key=lambda r: (r.get(self._order) is None, r.get(self._order)),
                                  reverse=self._order_desc)
            return MagicMock(data=matched)
        if self._op == "insert":
            row = dict(self._payload)
            row.setdefault("id", str(uuid.uuid4()))
            rows.append(row)
            return MagicMock(data=[row])
        if self._op == "update":
            matched = self._matched()
            for r in matched:
                r.update(self._payload)
            return MagicMock(data=matched)
        if self._op == "delete":
            matched = self._matched()
            for r in matched:
                rows.remove(r)
            return MagicMock(data=matched)
        return MagicMock(data=[])


class _FakeSupabase:
    def __init__(self):
        self.store = {}

    def table(self, name):
        return _Query(self.store, name)


def _sample_payload(key="tracking_lookup"):
    return {
        "action_key": key, "name": "Tracking Lookup", "display_name": "Tracking Lookup",
        "description": "Look up shipment status", "action_type": "API", "category": "logistics",
        "ai_description": "Use this action when the customer wants to know the shipment status.",
        "search_keywords": ["tracking", "shipment", "status"],
    }


class TestCreateAction(unittest.TestCase):
    def test_create_action(self):
        reg = BusinessActionRegistry(_FakeSupabase())
        action = reg.create(_sample_payload(), created_by="admin")
        self.assertEqual(action["action_key"], "tracking_lookup")
        self.assertEqual(action["action_type"], "API")
        self.assertEqual(action["created_by"], "admin")

    def test_create_rejects_invalid_action_type(self):
        reg = BusinessActionRegistry(_FakeSupabase())
        with self.assertRaises(ValueError):
            reg.create({**_sample_payload(), "action_type": "NOT_A_TYPE"})


class TestEditAction(unittest.TestCase):
    def test_edit_action(self):
        reg = BusinessActionRegistry(_FakeSupabase())
        action = reg.create(_sample_payload())
        updated = reg.update(action["id"], {"display_name": "New Display Name"}, updated_by="admin")
        self.assertEqual(updated["display_name"], "New Display Name")
        self.assertEqual(updated["updated_by"], "admin")


class TestDeleteAction(unittest.TestCase):
    def test_soft_delete_hides_from_list(self):
        reg = BusinessActionRegistry(_FakeSupabase())
        action = reg.create(_sample_payload())
        reg.delete(action["id"])
        self.assertIsNone(reg.get(action["id"]))
        self.assertEqual(reg.list(), [])

    def test_hard_delete_removes_row(self):
        sb = _FakeSupabase()
        reg = BusinessActionRegistry(sb)
        action = reg.create(_sample_payload())
        reg.delete(action["id"], hard=True)
        self.assertEqual(sb.store["business_actions"], [])


class TestEnableDisable(unittest.TestCase):
    def test_disable_then_enable(self):
        reg = BusinessActionRegistry(_FakeSupabase())
        action = reg.create(_sample_payload())
        reg.set_enabled(action["id"], False)
        self.assertNotIn(action["id"], [a["id"] for a in reg.enabled_actions()])
        reg.set_enabled(action["id"], True)
        self.assertIn(action["id"], [a["id"] for a in reg.enabled_actions()])


class TestSearchAction(unittest.TestCase):
    def test_search_matches_name_and_ai_description(self):
        reg = BusinessActionRegistry(_FakeSupabase())
        reg.create(_sample_payload("tracking_lookup"))
        reg.create({**_sample_payload("invoice_lookup"), "name": "Invoice Lookup",
                    "description": "Look up invoice details",
                    "ai_description": "Use this action for tax invoice requests."})
        results = reg.search("shipment status")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["action_key"], "tracking_lookup")

    def test_empty_query_returns_all(self):
        reg = BusinessActionRegistry(_FakeSupabase())
        reg.create(_sample_payload("a"))
        reg.create(_sample_payload("b"))
        self.assertEqual(len(reg.search("")), 2)


class TestParameterValidation(unittest.TestCase):
    def test_replace_parameters_and_read_back(self):
        reg = BusinessActionRegistry(_FakeSupabase())
        action = reg.create(_sample_payload())
        params = reg.replace_parameters(action["id"], [
            {"name": "tracking_number", "display_name": "Tracking Number", "required": True,
             "validation_type": "tracking_number", "example_value": "TH123456789", "slot_type": "tracking_number"},
        ])
        self.assertEqual(len(params), 1)
        self.assertEqual(reg.get_parameters(action["id"])[0]["name"], "tracking_number")

    def test_validation_rule_requires_known_type(self):
        reg = BusinessActionRegistry(_FakeSupabase())
        action = reg.create(_sample_payload())
        with self.assertRaises(ValueError):
            reg.replace_validation_rules(action["id"], [{"validation_type": "not_a_real_type", "rule": {}}])

    def test_valid_validation_rule_persists(self):
        reg = BusinessActionRegistry(_FakeSupabase())
        action = reg.create(_sample_payload())
        rules = reg.replace_validation_rules(action["id"], [
            {"parameter_name": "tracking_number", "validation_type": "regex", "rule": {"pattern": r"\d{6,15}"}},
        ])
        self.assertEqual(rules[0]["validation_type"], "regex")


class TestExecutionConfiguration(unittest.TestCase):
    def test_upsert_execution_creates_then_updates(self):
        reg = BusinessActionRegistry(_FakeSupabase())
        action = reg.create(_sample_payload())
        reg.upsert_execution(action["id"], {"endpoint": "https://erp.example.com/tracking",
                                             "http_method": "GET", "auth_type": "bearer",
                                             "auth_config": {"bearer_token": "sk-live-abcd1234"}})
        execution = reg.get_execution(action["id"], mask=False)
        self.assertEqual(execution["endpoint"], "https://erp.example.com/tracking")

        reg.upsert_execution(action["id"], {"http_method": "POST"})
        execution = reg.get_execution(action["id"], mask=False)
        self.assertEqual(execution["http_method"], "POST")
        self.assertEqual(execution["endpoint"], "https://erp.example.com/tracking")  # untouched fields survive an update? see note

    def test_invalid_http_method_rejected(self):
        reg = BusinessActionRegistry(_FakeSupabase())
        action = reg.create(_sample_payload())
        with self.assertRaises(ValueError):
            reg.upsert_execution(action["id"], {"http_method": "TRACE"})

    def test_invalid_auth_type_rejected(self):
        reg = BusinessActionRegistry(_FakeSupabase())
        action = reg.create(_sample_payload())
        with self.assertRaises(ValueError):
            reg.upsert_execution(action["id"], {"auth_type": "not_a_real_auth"})


class TestResponseMapping(unittest.TestCase):
    def test_replace_and_read_response_mapping(self):
        reg = BusinessActionRegistry(_FakeSupabase())
        action = reg.create(_sample_payload())
        mapping = reg.replace_response_mapping(action["id"], [
            {"json_path": "$.status", "mapped_label": "Shipment Status"},
            {"json_path": "$.location", "mapped_label": "Current Location"},
            {"json_path": "$.eta", "mapped_label": "Estimated Arrival"},
        ])
        self.assertEqual(len(mapping), 3)
        labels = [m["mapped_label"] for m in reg.get_response_mapping(action["id"])]
        self.assertEqual(labels, ["Shipment Status", "Current Location", "Estimated Arrival"])


class TestRegistryLookup(unittest.TestCase):
    def test_get_by_id_and_by_key(self):
        reg = BusinessActionRegistry(_FakeSupabase())
        action = reg.create(_sample_payload())
        self.assertEqual(reg.get(action["id"])["action_key"], "tracking_lookup")
        self.assertEqual(reg.get_by_key("tracking_lookup")["id"], action["id"])

    def test_get_full_bundles_related_records(self):
        reg = BusinessActionRegistry(_FakeSupabase())
        action = reg.create(_sample_payload())
        reg.replace_tags(action["id"], ["logistics", "tracking"])
        full = reg.get_full(action["id"])
        self.assertEqual(full["tags"], ["logistics", "tracking"])
        self.assertIn("parameters", full)
        self.assertIn("execution", full)


class TestRegistrySearch(unittest.TestCase):
    def test_find_by_category_and_type(self):
        reg = BusinessActionRegistry(_FakeSupabase())
        reg.create(_sample_payload("a"))
        reg.create({**_sample_payload("b"), "category": "notifications", "action_type": "NOTIFICATION"})
        self.assertEqual(len(reg.find_by_category("logistics")), 1)
        self.assertEqual(len(reg.find_by_type("NOTIFICATION")), 1)


class TestRegistryEnabledList(unittest.TestCase):
    def test_enabled_actions_excludes_disabled(self):
        reg = BusinessActionRegistry(_FakeSupabase())
        a = reg.create(_sample_payload("a"))
        reg.create({**_sample_payload("b"), "enabled": False})
        enabled = reg.enabled_actions()
        self.assertEqual([x["id"] for x in enabled], [a["id"]])


class TestEmbeddingPreparation(unittest.TestCase):
    def test_build_embedding_source_text_includes_all_sources(self):
        action = {"name": "Tracking Lookup", "description": "Look up shipment status",
                   "ai_description": "Use this when the customer wants shipment status.",
                   "business_description": "Logistics status inquiry"}
        examples = [{"example_text": "ของถึงไหนแล้ว"}, {"example_text": "เช็คพัสดุ"}]
        tags = ["logistics", "tracking"]
        text = build_embedding_source_text(action, examples, tags)
        for expected in ("Tracking Lookup", "shipment status", "ของถึงไหนแล้ว", "เช็คพัสดุ", "logistics", "tracking"):
            self.assertIn(expected, text)

    def test_prepare_embedding_source_persists_pending_status(self):
        reg = BusinessActionRegistry(_FakeSupabase())
        action = reg.create(_sample_payload())
        reg.replace_examples(action["id"], [{"example_text": "ของถึงไหนแล้ว"}])
        row = reg.prepare_embedding_source(action["id"])
        self.assertEqual(row["status"], "pending")
        self.assertIn("Tracking Lookup", row["embedding_source_text"])
        self.assertIsNone(row.get("embedding"))


class TestMaskSecrets(unittest.TestCase):
    def test_mask_secret_keeps_last_four_chars(self):
        self.assertEqual(mask_secret("sk-live-abcd1234"), "****1234")

    def test_mask_secret_handles_short_values(self):
        self.assertEqual(mask_secret("ab"), "****")

    def test_mask_secret_handles_empty(self):
        self.assertEqual(mask_secret(None), "")
        self.assertEqual(mask_secret(""), "")

    def test_mask_execution_secrets_masks_auth_config_and_headers(self):
        execution = {
            "endpoint": "https://erp.example.com/tracking",
            "headers": {"X-Api-Key": "abcd1234efgh5678", "Content-Type": "application/json"},
            "auth_config": {"bearer_token": "sk-live-abcd1234", "note": "not a secret"},
        }
        masked = mask_execution_secrets(execution)
        self.assertEqual(masked["headers"]["X-Api-Key"], "****5678")
        self.assertEqual(masked["headers"]["Content-Type"], "application/json")  # not secret-shaped, untouched
        self.assertEqual(masked["auth_config"]["bearer_token"], "****1234")
        self.assertEqual(masked["auth_config"]["note"], "not a secret")
        # Original must never be mutated.
        self.assertEqual(execution["auth_config"]["bearer_token"], "sk-live-abcd1234")

    def test_get_execution_masks_by_default(self):
        reg = BusinessActionRegistry(_FakeSupabase())
        action = reg.create(_sample_payload())
        reg.upsert_execution(action["id"], {"auth_type": "bearer", "auth_config": {"bearer_token": "sk-live-abcd1234"}})
        masked = reg.get_execution(action["id"])
        self.assertEqual(masked["auth_config"]["bearer_token"], "****1234")
        unmasked = reg.get_execution(action["id"], mask=False)
        self.assertEqual(unmasked["auth_config"]["bearer_token"], "sk-live-abcd1234")


class TestDuplicateAction(unittest.TestCase):
    def test_duplicate_creates_disabled_copy_with_new_key(self):
        reg = BusinessActionRegistry(_FakeSupabase())
        action = reg.create(_sample_payload())
        reg.replace_tags(action["id"], ["logistics"])
        copy = reg.duplicate(action["id"])
        self.assertNotEqual(copy["id"], action["id"])
        self.assertEqual(copy["action_key"], "tracking_lookup_copy")
        self.assertFalse(copy["enabled"])


class TestImportExport(unittest.TestCase):
    def test_export_then_import_creates_disabled_copy(self):
        reg = BusinessActionRegistry(_FakeSupabase())
        action = reg.create(_sample_payload())
        reg.replace_parameters(action["id"], [{"name": "tracking_number", "required": True}])
        exported = reg.export_action(action["id"])
        imported = reg.import_action(exported)
        self.assertFalse(imported["enabled"])
        self.assertEqual(len(reg.get_parameters(imported["id"])), 1)


if __name__ == "__main__":
    unittest.main()
