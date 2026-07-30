"""Tests for the Integration Schema Studio (services/integration_schema_service.py)
and its wiring into services/erp_test_harness.py. Uses the same
_FakeSupabase mock as the rest of the Business Action test suite — never
a real DB, never a real network call, never a real LLM call."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_business_action_registry import _FakeSupabase
from services.business_action_registry import BusinessActionRegistry
from services import integration_schema_service as iss
from services import erp_test_harness as harness
from tests.test_erp_test_harness import (
    _customer_lookup_action, _order_lookup_action, _cancel_order_action,
)


def _wallet_action(sb):
    """Same customer_data_lookup-style fixture used elsewhere, keyed on
    Wallet Balance to test AI Synonyms (wallet ~ balance/credit/point)."""
    reg, action_id = _customer_lookup_action(sb)
    return reg, action_id


class TestDeriveDefaultSchema(unittest.TestCase):
    def test_derives_input_and_response_fields_from_action_data(self):
        sb = _FakeSupabase()
        reg, action_id = _customer_lookup_action(sb)
        action = reg.get_full(action_id, mask_secrets=True)
        schema = iss.derive_default_schema(action)
        self.assertIn("input_fields", schema)
        self.assertIn("response_fields", schema)
        names = [f["technical_name"] for f in schema["input_fields"]]
        self.assertIn("CustCode", names)
        # The credential_store SecretCode parameter must never be
        # customer-visible by default.
        secret_field = next(f for f in schema["input_fields"] if f["technical_name"] == "SecretCode")
        self.assertEqual(secret_field["visibility"], "hidden")
        canonical_names = [f["canonical_name"] for f in schema["response_fields"]]
        self.assertIn("wallet.balance", canonical_names)

    def test_operation_type_and_conversation_absent_on_fresh_derivation(self):
        """Part 1-2 — a brand-new draft never bakes a concrete
        operation_type/conversation value in; those are absent
        ('inherit') until an admin explicitly sets them, or until the
        resolver computes them at read time (see
        TestResolveEffectiveIntegrationSchema below)."""
        sb = _FakeSupabase()
        reg, action_id = _cancel_order_action(sb)
        action = reg.get_full(action_id, mask_secrets=True)
        schema = iss.derive_default_schema(action)
        self.assertIsNone(schema["general"]["operation_type"])
        self.assertEqual(schema["conversation"], {})
        self.assertEqual(schema["_schema_meta"]["provenance_version"], 2)

    def test_usable_as_pre_fill_seed_never_blank(self):
        sb = _FakeSupabase()
        reg, action_id = _order_lookup_action(sb)
        action = reg.get_full(action_id, mask_secrets=True)
        schema = iss.derive_default_schema(action)
        self.assertTrue(schema["input_fields"])
        self.assertTrue(schema["response_fields"])


class TestRegistryVersioning(unittest.TestCase):
    def test_get_draft_creates_one_prefilled_when_none_exists(self):
        sb = _FakeSupabase()
        reg, action_id = _customer_lookup_action(sb)
        action = reg.get_full(action_id, mask_secrets=True)
        schema_reg = iss.get_registry(sb)
        draft = schema_reg.get_draft(action_id, action=action)
        self.assertEqual(draft["status"], "draft")
        self.assertEqual(draft["version_number"], 1)
        self.assertTrue(draft["schema"]["input_fields"])

    def test_save_draft_updates_in_place(self):
        sb = _FakeSupabase()
        reg, action_id = _customer_lookup_action(sb)
        action = reg.get_full(action_id, mask_secrets=True)
        schema_reg = iss.get_registry(sb)
        draft = schema_reg.get_draft(action_id, action=action)
        new_schema = dict(draft["schema"])
        new_schema["general"] = {**new_schema["general"], "operation_type": "LOOKUP"}
        saved = schema_reg.save_draft(action_id, new_schema, updated_by="admin")
        self.assertEqual(saved["schema"]["general"]["operation_type"], "LOOKUP")
        self.assertEqual(len(schema_reg.list_versions(action_id)), 1)  # still one row, in place

    def test_publish_promotes_draft_and_archives_previous(self):
        sb = _FakeSupabase()
        reg, action_id = _customer_lookup_action(sb)
        action = reg.get_full(action_id, mask_secrets=True)
        schema_reg = iss.get_registry(sb)
        schema_reg.get_draft(action_id, action=action)
        published = schema_reg.publish(action_id, updated_by="admin")
        self.assertEqual(published["status"], "published")
        self.assertIsNotNone(published["published_at"])

        # A NEW draft can now be started and published again; the old
        # published version must be archived, never deleted.
        draft2 = schema_reg.get_draft(action_id)
        schema_reg.save_draft(action_id, draft2["schema"], updated_by="admin")
        published2 = schema_reg.publish(action_id, updated_by="admin")
        self.assertEqual(published2["status"], "published")
        versions = schema_reg.list_versions(action_id)
        statuses = [v["status"] for v in versions]
        self.assertIn("archived", statuses)
        self.assertEqual(statuses.count("published"), 1)

    def test_rollback_creates_new_draft_never_destroys_history(self):
        sb = _FakeSupabase()
        reg, action_id = _customer_lookup_action(sb)
        action = reg.get_full(action_id, mask_secrets=True)
        schema_reg = iss.get_registry(sb)
        schema_reg.get_draft(action_id, action=action)
        v1 = schema_reg.publish(action_id)

        draft2 = schema_reg.get_draft(action_id)
        edited = dict(draft2["schema"])
        edited["general"] = {**edited["general"], "operation_type": "SEARCH"}
        schema_reg.save_draft(action_id, edited)
        v2 = schema_reg.publish(action_id)
        self.assertEqual(v2["schema"]["general"]["operation_type"], "SEARCH")

        rolled_back = schema_reg.rollback(action_id, v1["version_number"])
        self.assertEqual(rolled_back["status"], "draft")
        self.assertNotEqual(rolled_back["schema"]["general"]["operation_type"], "SEARCH")
        # Both v1 and v2 still exist — rollback never deletes history.
        version_numbers = [v["version_number"] for v in schema_reg.list_versions(action_id)]
        self.assertIn(v1["version_number"], version_numbers)
        self.assertIn(v2["version_number"], version_numbers)

    def test_diff_versions(self):
        sb = _FakeSupabase()
        reg, action_id = _customer_lookup_action(sb)
        action = reg.get_full(action_id, mask_secrets=True)
        schema_reg = iss.get_registry(sb)
        schema_reg.get_draft(action_id, action=action)
        v1 = schema_reg.publish(action_id)
        draft2 = schema_reg.get_draft(action_id)
        edited = dict(draft2["schema"])
        edited["general"] = {**edited["general"], "operation_type": "SEARCH"}
        schema_reg.save_draft(action_id, edited)
        v2 = schema_reg.publish(action_id)
        diff = schema_reg.diff_versions(action_id, v1["version_number"], v2["version_number"])
        self.assertIn("general", diff)
        self.assertEqual(diff["general"]["after"]["operation_type"], "SEARCH")


class TestResolveEffectiveSchema(unittest.TestCase):
    def test_falls_back_to_derived_when_nothing_published(self):
        sb = _FakeSupabase()
        reg, action_id = _customer_lookup_action(sb)
        action = reg.get_full(action_id, mask_secrets=True)
        effective = iss.resolve_effective_schema(action, sb)
        self.assertTrue(effective["input_fields"])

    def test_published_overrides_merge_over_derived(self):
        sb = _FakeSupabase()
        reg, action_id = _customer_lookup_action(sb)
        action = reg.get_full(action_id, mask_secrets=True)
        schema_reg = iss.get_registry(sb)
        draft = schema_reg.get_draft(action_id, action=action)
        schema = draft["schema"]
        for f in schema["response_fields"]:
            if f["canonical_name"] == "wallet.balance":
                f["aliases"] = ["custom wallet alias"]
                f["display_labels"] = {"th": "ยอดพิเศษ", "en": "Special Balance"}
        schema_reg.save_draft(action_id, schema)
        schema_reg.publish(action_id)

        effective = iss.resolve_effective_schema(action, sb)
        wallet_field = next(f for f in effective["response_fields"] if f["canonical_name"] == "wallet.balance")
        self.assertIn("custom wallet alias", wallet_field["aliases"])
        self.assertEqual(wallet_field["display_labels"]["th"], "ยอดพิเศษ")
        # Untouched fields still come through from derived defaults.
        names = [f["canonical_name"] for f in effective["response_fields"]]
        self.assertIn("customer.name", names)


class TestErpHarnessWiring(unittest.TestCase):
    """Confirms every Part 16 live-verification claim already holds true
    purely from wiring — a schema dict changes runtime behavior with
    zero further code changes once passed in."""

    def _customer_options_and_schema(self):
        sb = _FakeSupabase()
        reg, action_id = _customer_lookup_action(sb)
        action = reg.get_full(action_id, mask_secrets=True)
        schema = iss.derive_default_schema(action)
        return action, schema

    def test_alias_change_changes_detection(self):
        action, schema = self._customer_options_and_schema()
        for f in schema["response_fields"]:
            if f["canonical_name"] == "wallet.balance":
                f["synonyms"] = ["schema-only-magic-word"]
        options = harness.build_available_response_options(action, schema=schema)
        detection = harness.detect_requested_fields("schema-only-magic-word", options)
        self.assertIn("wallet.balance", detection["requested_fields"])

    def test_display_label_change_reflected_in_options(self):
        action, schema = self._customer_options_and_schema()
        for f in schema["response_fields"]:
            if f["canonical_name"] == "wallet.balance":
                f["display_labels"] = {"th": "จำนวนพิเศษ", "en": "Special Amount"}
        options = harness.build_available_response_options(action, schema=schema)
        wallet_opt = next(o for o in options if o["canonical_name"] == "wallet.balance")
        self.assertEqual(wallet_opt["display_label_th"], "จำนวนพิเศษ")

    def test_follow_up_prompt_used_over_generic(self):
        sb = _FakeSupabase()
        reg, action_id = _customer_lookup_action(sb)
        action = reg.get_full(action_id, mask_secrets=True)
        schema = iss.derive_default_schema(action)
        for f in schema["input_fields"]:
            if f["technical_name"] == "CustCode":
                f["follow_up_prompts"] = {"th": "กรุณาระบุรหัสลูกค้าของคุณค่ะ (custom)"}
        registry = reg
        question = harness.build_clarification_question(action, registry, {}, schema=schema, language="th")
        self.assertEqual(question, "กรุณาระบุรหัสลูกค้าของคุณค่ะ (custom)")

    def test_visibility_hidden_excludes_field(self):
        action, schema = self._customer_options_and_schema()
        for f in schema["response_fields"]:
            if f["canonical_name"] == "wallet.balance":
                f["visibility"] = "hidden"
        options = harness.build_available_response_options(action, schema=schema)
        names = [o["canonical_name"] for o in options]
        self.assertNotIn("wallet.balance", names)
        normalized = harness.normalize_response(action, {"Wallet Balance": 500, "Customer Name": "Test"}, schema=schema)
        self.assertNotIn("wallet.balance", normalized)

    def test_operation_type_override_changes_ask_behavior(self):
        sb = _FakeSupabase()
        reg, action_id = _customer_lookup_action(sb)
        action = reg.get_full(action_id, mask_secrets=True)
        schema = iss.derive_default_schema(action)
        schema["general"]["operation_type"] = "CREATE"
        schema["conversation"] = {}  # let the new operation-type-derived defaults apply
        behavior = harness.get_conversation_behavior(action, schema=schema)
        self.assertFalse(behavior["ask_requested_information"])
        self.assertEqual(harness.infer_operation_type(action, schema=schema), "CREATE")

    def test_formatting_changes_rendered_value(self):
        action, schema = self._customer_options_and_schema()
        for f in schema["response_fields"]:
            if f["canonical_name"] == "wallet.balance":
                f["formatting"] = {"type": "currency"}
        normalized = harness.normalize_response(action, {"Wallet Balance": 1250, "Customer Name": "Test"}, schema=schema)
        self.assertIn("฿", str(normalized["wallet.balance"]["value"]))

    def test_format_value_table_driven(self):
        self.assertEqual(harness.format_value(1234.5, {"type": "currency"}), "฿1,234.50")
        self.assertEqual(harness.format_value(True, {"type": "boolean"}, language="en"), "Yes")
        self.assertEqual(harness.format_value(50, {"type": "percent"}), "50%")
        self.assertEqual(harness.format_value("x", None), "x")
        self.assertEqual(harness.format_value(None, {"type": "currency"}), None)

    def test_build_generic_action_model_exposes_schema_data(self):
        action, schema = self._customer_options_and_schema()
        for f in schema["response_fields"]:
            if f["canonical_name"] == "wallet.balance":
                f["synonyms"] = ["credit", "point"]
        model = harness.build_generic_action_model(action, schema=schema)
        wallet_field = next(f for f in model["response_fields"] if f["canonical_name"] == "wallet.balance")
        self.assertIn("credit", wallet_field["synonyms"])

    def test_run_erp_test_uses_schema_via_effective_resolution(self):
        """End-to-end: run_erp_test() must fetch and apply the published
        schema without the caller passing anything new — schema=None is
        never required, resolve_effective_schema() is called internally."""
        sb = _FakeSupabase()
        reg, action_id = _customer_lookup_action(sb)
        schema_reg = iss.get_registry(sb)
        action = reg.get_full(action_id, mask_secrets=True)
        draft = schema_reg.get_draft(action_id, action=action)
        schema = draft["schema"]
        for f in schema["response_fields"]:
            if f["canonical_name"] == "wallet.balance":
                f["visibility"] = "hidden"
        schema_reg.save_draft(action_id, schema)
        schema_reg.publish(action_id)

        result = harness.run_erp_test(sb=sb, action_id=action_id, message="C00001", mode="intent_param")
        self.assertTrue(result["ok"])


class TestResolveEffectiveIntegrationSchema(unittest.TestCase):
    """Part 3/8/9/10/12 — the 3-layer resolver + provenance + warnings,
    verified against the exact scenarios A-D and F from the sprint spec."""

    def _cancel_action(self):
        sb = _FakeSupabase()
        reg, action_id = _cancel_order_action(sb)
        return reg.get_full(action_id, mask_secrets=True)

    def test_scenario_a_cancel_no_override_requires_confirmation(self):
        action = self._cancel_action()
        result = iss.resolve_effective_integration_schema(action, None)
        conv = result["effective_schema"]["conversation"]
        self.assertFalse(conv["ask_requested_information"])
        self.assertTrue(conv["require_confirmation_before_execute"])
        self.assertEqual(result["provenance"]["general.operation_type"], "derived")

    def test_scenario_b_operation_type_change_cascades_conversation(self):
        action = self._cancel_action()
        published = {"_schema_meta": {"provenance_version": 2},
                     "general": {"operation_type": "LOOKUP"}, "conversation": {}}
        result = iss.resolve_effective_integration_schema(action, published)
        conv = result["effective_schema"]["conversation"]
        self.assertTrue(conv["ask_requested_information"])
        self.assertEqual(result["provenance"]["general.operation_type"], "explicit")
        self.assertEqual(result["provenance"]["conversation.ask_requested_information"], "runtime_default")

    def test_scenario_c_explicit_conversation_override_wins(self):
        action = self._cancel_action()
        published = {"_schema_meta": {"provenance_version": 2},
                     "general": {"operation_type": "LOOKUP"},
                     "conversation": {"ask_requested_information": False}}
        result = iss.resolve_effective_integration_schema(action, published)
        conv = result["effective_schema"]["conversation"]
        self.assertFalse(conv["ask_requested_information"])
        self.assertEqual(result["provenance"]["conversation.ask_requested_information"], "explicit")
        # An unrelated change (primary_entity) doesn't disturb the explicit override.
        published["general"]["primary_entity"] = "order"
        result2 = iss.resolve_effective_integration_schema(action, published)
        self.assertFalse(result2["effective_schema"]["conversation"]["ask_requested_information"])

    def test_scenario_d_reset_explicit_override_follows_operation_type_default_again(self):
        action = self._cancel_action()
        published = {"_schema_meta": {"provenance_version": 2},
                     "general": {"operation_type": "LOOKUP"},
                     "conversation": {"ask_requested_information": None}}  # reset to inherited
        result = iss.resolve_effective_integration_schema(action, published)
        conv = result["effective_schema"]["conversation"]
        self.assertTrue(conv["ask_requested_information"])
        self.assertEqual(result["provenance"]["conversation.ask_requested_information"], "runtime_default")

    def test_scenario_f_ambiguous_weak_text_never_guesses_destructive(self):
        sb = _FakeSupabase()
        reg = BusinessActionRegistry(sb)
        action = reg.create({"action_key": "ambiguous_action", "name": "AmbiguousAction",
                              "display_name": "Ambiguous Action", "action_type": "API",
                              "category": "misc", "enabled": True})
        action_id = action["id"]
        reg.upsert_execution(action_id, {"endpoint": "https://example-erp.test/ambiguous", "http_method": "POST"})
        reg.update(action_id, {"description": "This will cancel and delete the pending item."})
        action_full = reg.get_full(action_id, mask_secrets=True)
        result = iss.resolve_effective_integration_schema(action_full, None)
        self.assertEqual(result["effective_schema"]["general"]["operation_type"], "UNKNOWN")
        self.assertNotIn(result["effective_schema"]["general"]["operation_type"], ("CANCEL", "DELETE"))
        self.assertTrue(any("UNKNOWN" in w for w in result["warnings"]))

    def test_part10_pre_existing_schema_without_provenance_treated_as_explicit(self):
        """Part 10 — a schema published before this sprint (no
        `_schema_meta` marker at all) must have its pre-existing
        populated general.operation_type/conversation.* values treated
        as explicit (never silently reinterpreted/overridden), with a
        warning surfaced for administrator review."""
        action = self._cancel_action()
        legacy_published = {  # no "_schema_meta" key at all — pre-dates provenance tracking
            "general": {"operation_type": "LOOKUP"},
            "conversation": {"ask_requested_information": True, "require_confirmation_before_execute": False},
        }
        result = iss.resolve_effective_integration_schema(action, legacy_published)
        conv = result["effective_schema"]["conversation"]
        self.assertEqual(result["effective_schema"]["general"]["operation_type"], "LOOKUP")
        self.assertTrue(conv["ask_requested_information"])
        self.assertFalse(conv["require_confirmation_before_execute"])
        self.assertEqual(result["provenance"]["general.operation_type"], "explicit")
        self.assertTrue(any("predates provenance tracking" in w for w in result["warnings"]))

    def test_field_level_provenance_marks_explicit_vs_derived(self):
        sb = _FakeSupabase()
        reg, action_id = _customer_lookup_action(sb)
        action = reg.get_full(action_id, mask_secrets=True)
        published = {"_schema_meta": {"provenance_version": 2},
                     "response_fields": [{"canonical_name": "wallet.balance", "aliases": ["custom"]}]}
        result = iss.resolve_effective_integration_schema(action, published)
        self.assertEqual(result["provenance"]["response_fields.wallet.balance.aliases"], "explicit")
        self.assertEqual(result["provenance"]["response_fields.customer.name.aliases"], "derived")

    def test_resolve_effective_schema_wrapper_returns_flat_dict(self):
        sb = _FakeSupabase()
        reg, action_id = _customer_lookup_action(sb)
        action = reg.get_full(action_id, mask_secrets=True)
        effective = iss.resolve_effective_schema(action, sb)
        self.assertIn("general", effective)
        self.assertIn("conversation", effective)
        self.assertNotIn("_schema_meta", effective)


class TestDiffCascadeNotes(unittest.TestCase):
    """Part 7 — diff notes a downstream effective conversation change
    caused purely by an operation_type change (cascaded, not explicit)."""

    def test_diff_notes_cascaded_conversation_effect(self):
        sb = _FakeSupabase()
        reg, action_id = _cancel_order_action(sb)
        action = reg.get_full(action_id, mask_secrets=True)
        schema_a = iss.derive_default_schema(action)
        schema_a["general"]["operation_type"] = "CANCEL"
        schema_b = dict(schema_a)
        schema_b["general"] = {**schema_a["general"], "operation_type": "LOOKUP"}
        diff = iss.diff_schema(schema_a, schema_b)
        self.assertIn("_cascaded_conversation_effects", diff)
        self.assertTrue(any("ask_requested_information" in n for n in diff["_cascaded_conversation_effects"]))


if __name__ == "__main__":
    unittest.main()
