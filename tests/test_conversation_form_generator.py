"""Tests for services/conversation_form_generator.py (Requirement 8's
named scenarios A-F). Uses the same _FakeSupabase mock as the rest of the
suite — never a real DB, network, or LLM call.

FIXTURE-ONLY vs REAL-DB NOTE (Requirement 7): every fixture in this file
(_shipment_search_action, and the reused _cancel_order_action) is an
in-memory test double built with _FakeSupabase. There is NO real
"Shipment Search" action in the live Supabase DB — this suite never
claims otherwise. Real-DB-backed verification lives separately in
scripts/verify_conversation_form_generator.py, which exercises the
actually-published live actions (customer_data_lookup,
fixture_order_lookup, fixture_product_lookup, fixture_invoice_lookup,
fixture_cancel_order) — that script is print-only, not a unit test.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_business_action_registry import _FakeSupabase
from services.business_action_registry import BusinessActionRegistry
from services import integration_schema_service as iss
from services.integration_contract_service import resolve_effective_contract
from tests.test_erp_test_harness import _cancel_order_action
from services.conversation_form_generator import (
    ConversationState, InvalidStateTransition, generate_conversation_form,
    apply_user_selection,
)


# ── Fixture-only "Shipment Search" action (no real DB equivalent) ──────

def _shipment_search_action(sb=None):
    sb = sb or _FakeSupabase()
    reg = BusinessActionRegistry(sb)
    action = reg.create({"action_key": "shipment_search", "name": "SearchShipment",
                          "display_name": "Shipment Search", "action_type": "API",
                          "category": "shipment", "enabled": True,
                          "search_keywords": ["shipment", "parcel", "tracking"]})
    action_id = action["id"]
    reg.replace_parameters(action_id, [
        {"name": "CustCode", "display_name": "Customer Code", "required": True,
         "input_source": "customer_message", "validation_type": "regex",
         "validation_pattern": r"^C\d{5}$", "example_value": "C00001"},
        {"name": "BillStatus", "display_name": "Bill Status", "required": False,
         "input_source": "customer_message", "example_value": "2"},
        {"name": "Latest", "display_name": "Latest N Records", "required": False,
         "input_source": "customer_message", "example_value": "5"},
        {"name": "ReceivedDate", "display_name": "Received Date", "required": False,
         "input_source": "customer_message", "example_value": "2026-07-01"},
        {"name": "ExportDate", "display_name": "Export Date", "required": False,
         "input_source": "customer_message", "example_value": "2026-07-15"},
    ])
    reg.set_parameter_groups(action_id, [
        {"name": "search_criterion", "rule": "AT_LEAST_ONE",
         "members": ["BillStatus", "Latest", "ReceivedDate", "ExportDate"]},
    ])
    reg.upsert_execution(action_id, {"endpoint": "https://example-erp.test/shipments/search",
                                      "http_method": "GET", "content_type": "application/json"})
    reg.replace_response_mapping(action_id, [
        {"json_path": "$.shipment.TrackingNumber", "mapped_label": "Tracking Number"},
        {"json_path": "$.shipment.Status", "mapped_label": "Shipment Status"},
    ])

    # Publish a schema that adds semantic_type + enum `options` on
    # BillStatus, and marks ReceivedDate/ExportDate as date fields —
    # Requirement 1's configurable display metadata, generic (no field
    # name is special-cased in the generator itself; this is just data).
    action_full = reg.get_full(action_id, mask_secrets=True)
    draft = iss.derive_default_schema(action_full)
    for f in draft["input_fields"]:
        if f["technical_name"] == "BillStatus":
            f["semantic_type"] = "status"
            f["options"] = [
                {"value": "1", "label": "รับเข้าโกดังจีน"},
                {"value": "2", "label": "ส่งออกจากจีน"},
                {"value": "3", "label": "รับเข้าโกดังไทย"},
                {"value": "4", "label": "จัดส่งสำเร็จ"},
            ]
        elif f["technical_name"] == "Latest":
            f["semantic_type"] = "integer"
            f["display_type"] = "number"
        elif f["technical_name"] in ("ReceivedDate", "ExportDate"):
            f["semantic_type"] = "date"
            f["display_type"] = "date"
    schema_reg = iss.get_registry(sb)
    schema_reg.save_draft(action_id, draft)
    schema_reg.publish(action_id)
    return reg, action_id, sb


def _contract_for(action_id, sb):
    result = resolve_effective_contract(action_id, sb)
    assert result["contract"] is not None, result["validation"]
    return result["contract"]


class TestConversationStateTransitions(unittest.TestCase):
    def test_allowed_edges(self):
        s = ConversationState()
        self.assertEqual(s.current_state, "WaitingInput")
        self.assertTrue(s.transition_to("ReadyToExecute"))
        self.assertTrue(s.transition_to("WaitingConfirmation"))
        self.assertTrue(s.transition_to("Executing"))
        self.assertTrue(s.transition_to("Completed"))

    def test_waiting_input_can_cancel_or_timeout(self):
        s1 = ConversationState()
        self.assertTrue(s1.transition_to("Cancelled"))
        s2 = ConversationState()
        self.assertTrue(s2.transition_to("Timeout"))

    def test_ready_to_execute_can_go_straight_to_executing(self):
        s = ConversationState()
        s.transition_to("ReadyToExecute")
        self.assertTrue(s.transition_to("Executing"))

    def test_waiting_confirmation_can_cancel(self):
        s = ConversationState()
        s.transition_to("ReadyToExecute")
        s.transition_to("WaitingConfirmation")
        self.assertTrue(s.transition_to("Cancelled"))

    def test_disallowed_transition_rejected_safely(self):
        s = ConversationState()
        # WaitingInput -> Executing is not an allowed edge.
        self.assertFalse(s.transition_to("Executing"))
        self.assertEqual(s.current_state, "WaitingInput")  # unchanged, no corruption

    def test_completed_is_terminal(self):
        s = ConversationState()
        s.transition_to("ReadyToExecute")
        s.transition_to("Executing")
        s.transition_to("Completed")
        self.assertFalse(s.transition_to("WaitingInput"))
        self.assertEqual(s.current_state, "Completed")

    def test_unknown_state_name_rejected(self):
        s = ConversationState()
        self.assertFalse(s.transition_to("NotARealState"))


class TestScenarioA_CriterionSelectionShown(unittest.TestCase):
    """FIXTURE-ONLY (Shipment Search). CustCode present, no criterion
    selected yet -> generator shows criterion-selection options; state
    stays WaitingInput; group correctly reported unsatisfied."""

    def test_shows_criterion_selection_not_ready(self):
        reg, action_id, sb = _shipment_search_action()
        contract = _contract_for(action_id, sb)
        state = ConversationState(collected_values={"CustCode": "C00001"})
        form = generate_conversation_form(contract, state, language="th")

        self.assertFalse(form["ready_to_execute"])
        self.assertEqual(state.current_state, "WaitingInput")
        criterion_steps = [s for s in form["steps"] if s["kind"] == "criterion_selection"]
        self.assertEqual(len(criterion_steps), 1)
        option_values = {o["value"] for o in criterion_steps[0]["components"][0]["options"]}
        self.assertEqual(option_values, {"BillStatus", "Latest", "ReceivedDate", "ExportDate"})
        group_status = next(g for g in form["groups_status"] if g["name"] == "search_criterion")
        self.assertFalse(group_status["satisfied"])


class TestScenarioB_ValueStepAfterCriterionChosen(unittest.TestCase):
    """User selects BillStatus as the criterion -> next step asks for
    BillStatus's own enum VALUE, not the criterion list again."""

    def test_asks_for_billstatus_value(self):
        reg, action_id, sb = _shipment_search_action()
        contract = _contract_for(action_id, sb)
        state = ConversationState(collected_values={"CustCode": "C00001"})
        apply_user_selection(contract, state, group_name="search_criterion", field="BillStatus")

        form = generate_conversation_form(contract, state, language="th")
        self.assertFalse(form["ready_to_execute"])
        criterion_steps = [s for s in form["steps"] if s["kind"] == "criterion_selection"]
        self.assertEqual(criterion_steps, [])  # not repeated
        value_steps = [s for s in form["steps"] if s["kind"] == "value_input" and s["field"] == "BillStatus"]
        self.assertEqual(len(value_steps), 1)
        options = value_steps[0]["components"][0]["options"]
        self.assertIn("ส่งออกจากจีน", [o["label"] for o in options])


class TestScenarioC_GroupSatisfiedAfterValueChosen(unittest.TestCase):
    """User selects a BillStatus value -> collected_values set, group
    satisfied, state becomes ReadyToExecute (CustCode also present)."""

    def test_group_satisfied_and_ready(self):
        reg, action_id, sb = _shipment_search_action()
        contract = _contract_for(action_id, sb)
        state = ConversationState(collected_values={"CustCode": "C00001"})
        apply_user_selection(contract, state, group_name="search_criterion", field="BillStatus", value="2")

        self.assertEqual(state.collected_values["BillStatus"], "2")
        form = generate_conversation_form(contract, state, language="th")
        group_status = next(g for g in form["groups_status"] if g["name"] == "search_criterion")
        self.assertTrue(group_status["satisfied"])
        self.assertTrue(form["ready_to_execute"])
        self.assertTrue(state.transition_to("ReadyToExecute"))


class TestScenarioD_LatestAsksForNumber(unittest.TestCase):
    """User selects "Latest" as the criterion -> next step asks for a
    NUMERIC limit (Number component, not text/enum)."""

    def test_latest_uses_number_component(self):
        reg, action_id, sb = _shipment_search_action()
        contract = _contract_for(action_id, sb)
        state = ConversationState(collected_values={"CustCode": "C00001"})
        apply_user_selection(contract, state, group_name="search_criterion", field="Latest")

        form = generate_conversation_form(contract, state, language="th")
        value_steps = [s for s in form["steps"] if s["kind"] == "value_input" and s["field"] == "Latest"]
        self.assertEqual(len(value_steps), 1)
        self.assertEqual(value_steps[0]["components"][0]["component_type"], "number")


class TestScenarioE_DateCriterionSatisfiesGroupAlone(unittest.TestCase):
    """User picks a date-type criterion (ReceivedDate) and provides a
    value -> group satisfied by that single field; generator must NOT
    ask for another criterion."""

    def test_date_field_satisfies_group_no_extra_question(self):
        reg, action_id, sb = _shipment_search_action()
        contract = _contract_for(action_id, sb)
        state = ConversationState(collected_values={"CustCode": "C00001"})
        apply_user_selection(contract, state, group_name="search_criterion",
                              field="ReceivedDate", value="2026-07-01")

        form = generate_conversation_form(contract, state, language="th")
        criterion_steps = [s for s in form["steps"] if s["kind"] == "criterion_selection"]
        self.assertEqual(criterion_steps, [])
        self.assertTrue(form["ready_to_execute"])


class TestScenarioF_CommandRequiresConfirmation(unittest.TestCase):
    """A COMMAND-type action (reused _cancel_order_action fixture) with
    require_confirmation_before_execute=True -> once parameters are
    collected, state must go to WaitingConfirmation, not straight to
    Executing."""

    def test_command_needs_confirmation_before_execute(self):
        sb = _FakeSupabase()
        reg, action_id = _cancel_order_action(sb)
        contract = _contract_for(action_id, sb)
        self.assertTrue(contract["conversation"]["require_confirmation_before_execute"])

        state = ConversationState(collected_values={"OrderNo": "PO202601001"})
        form = generate_conversation_form(contract, state, language="th")
        self.assertTrue(form["ready_to_execute"])
        self.assertTrue(form["needs_confirmation"])

        self.assertTrue(state.transition_to("ReadyToExecute"))
        self.assertTrue(state.transition_to("WaitingConfirmation"))
        self.assertFalse(state.transition_to("Completed"))  # cannot skip Executing
        self.assertTrue(state.transition_to("Executing"))
        self.assertTrue(state.transition_to("Completed"))


class TestChannelVariantPreview(unittest.TestCase):
    """Hardening pass Requirement 2 — the already-generated channel_variants
    metadata per component, and that an explicitly-configured
    channel_overrides schema key wins over the generic derivation for that
    one channel only (never a second generation path)."""

    def test_all_four_channels_present_on_every_component(self):
        reg, action_id, sb = _shipment_search_action()
        contract = _contract_for(action_id, sb)
        state = ConversationState(collected_values={"CustCode": "C00001"})
        form = generate_conversation_form(contract, state, language="th")
        criterion_step = next(s for s in form["steps"] if s["kind"] == "criterion_selection")
        variants = criterion_step["components"][0]["channel_variants"]
        for channel in ("web", "line", "api", "cli"):
            self.assertIn(channel, variants)
        self.assertEqual(variants["cli"]["render_as"], "numbered_choice")
        self.assertEqual(variants["cli"]["choices"][0]["index"], 1)
        self.assertEqual(variants["line"]["render_as"], "quick_reply")
        self.assertEqual(variants["api"]["render_as"], "structured")

    def test_explicit_channel_override_wins_for_that_channel_only(self):
        sb = _FakeSupabase()
        reg, action_id = _cancel_order_action(sb)
        action_full = reg.get_full(action_id, mask_secrets=True)
        draft = iss.derive_default_schema(action_full)
        for f in draft["input_fields"]:
            if f["technical_name"] == "OrderNo":
                f["channel_overrides"] = {"line": {"render_as": "text", "note": "no quick reply for free text"}}
        schema_reg = iss.get_registry(sb)
        schema_reg.save_draft(action_id, draft)
        schema_reg.publish(action_id)

        contract = _contract_for(action_id, sb)
        state = ConversationState()
        form = generate_conversation_form(contract, state, language="th")
        value_step = next(s for s in form["steps"] if s["kind"] == "value_input")
        variants = value_step["components"][0]["channel_variants"]
        self.assertEqual(variants["line"]["render_as"], "text")
        self.assertEqual(variants["line"]["note"], "no quick reply for free text")
        # web/api/cli were never overridden — still the generic derivation.
        self.assertIn("render_as", variants["web"])
        self.assertEqual(variants["cli"]["render_as"], "text_prompt")


class TestFieldProvenanceForDisplayMetadata(unittest.TestCase):
    """Hardening pass Requirement 1 — the new configurable display-
    metadata keys must flow through the EXISTING provenance mechanism
    (services/integration_schema_service.py::resolve_effective_integration_
    schema), never a second provenance resolver."""

    def test_explicitly_configured_display_type_is_explicit_provenance(self):
        reg, action_id, sb = _shipment_search_action()
        action_full = reg.get_full(action_id, mask_secrets=True)
        published_schema = iss.get_registry(sb).get_published_schema(action_id)
        resolution = iss.resolve_effective_integration_schema(action_full, published_schema)
        provenance = resolution["provenance"]
        self.assertEqual(provenance["input_fields.Latest.display_type"], "explicit")
        self.assertEqual(provenance["input_fields.BillStatus.options"], "explicit")
        # A field with no configured display_type falls back to "derived".
        self.assertEqual(provenance["input_fields.CustCode.display_type"], "derived")


if __name__ == "__main__":
    unittest.main()
