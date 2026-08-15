"""Tests for services/human_handoff_service.py (Human Handoff sprint,
2026-08-13). Uses simple fake registry/executor doubles — this module
never talks to Supabase directly, only to whatever registry/executor
objects are injected, so a full DB-query fake harness isn't needed."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.human_handoff_service import (
    find_notification_action,
    build_handoff_message,
    send_handoff_notification,
)

NOTIFY_ACTION = {
    "id": "notify-1",
    "action_key": "sendlinenotics",
    "display_name": "SendLineNotiCS",
    "action_type": "API",
    "enabled": True,
    "setup_metadata": {"operation_type": "NOTIFICATION"},
    "parameters": [],
    "response_mapping": [],
}

LOOKUP_ACTION = {
    "id": "lookup-1",
    "action_key": "getdatacustomer",
    "display_name": "GetDataCustomer",
    "action_type": "API",
    "enabled": True,
    "setup_metadata": {"operation_type": "LOOKUP"},
    "parameters": [],
    "response_mapping": [],
}


class _FakeRegistry:
    def __init__(self, actions):
        self._actions = {a["id"]: a for a in actions}

    def enabled_actions(self):
        return [{"id": a["id"]} for a in self._actions.values() if a.get("enabled")]

    def get_full(self, action_id, mask_secrets=False):
        return self._actions.get(action_id)


class _FakeExecutor:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def execute(self, action_id, context):
        self.calls.append((action_id, context))
        return self.result


class TestFindNotificationAction(unittest.TestCase):
    def test_finds_the_notification_action_among_others(self):
        reg = _FakeRegistry([LOOKUP_ACTION, NOTIFY_ACTION])
        found = find_notification_action(reg)
        self.assertEqual(found["id"], "notify-1")

    def test_returns_none_when_no_notification_action_configured(self):
        reg = _FakeRegistry([LOOKUP_ACTION])
        self.assertIsNone(find_notification_action(reg))

    def test_ignores_disabled_notification_action(self):
        disabled = {**NOTIFY_ACTION, "enabled": False}
        reg = _FakeRegistry([disabled])
        self.assertIsNone(find_notification_action(reg))


class TestBuildHandoffMessage(unittest.TestCase):
    def test_includes_all_known_fields(self):
        msg = build_handoff_message(
            reason="user_requested_human", customer_name="สมชาย", cust_code="SP1014",
            line_user_id="U123", customer_message="ขอคุยกับเจ้าหน้าที่",
        )
        self.assertIn("[AI HANDOFF]", msg)
        self.assertIn("Customer: สมชาย", msg)
        self.assertIn("CustCode: SP1014", msg)
        self.assertIn("LINE User: U123", msg)
        self.assertIn("ขอคุยกับเจ้าหน้าที่", msg)
        self.assertIn("Handoff Reason:", msg)

    def test_unknown_fields_render_as_unknown_not_blank(self):
        msg = build_handoff_message(
            reason="user_requested_human", customer_name=None, cust_code=None,
            line_user_id=None, customer_message="ขอคุยกับเจ้าหน้าที่",
        )
        self.assertIn("Customer: Unknown", msg)
        self.assertIn("CustCode: Unknown", msg)
        self.assertIn("LINE User: Unknown", msg)

    def test_never_contains_a_secret_looking_key(self):
        msg = build_handoff_message(
            reason="user_requested_human", customer_name="X", cust_code="SP1014",
            line_user_id="U1", customer_message="ขอคุยกับเจ้าหน้าที่",
        )
        self.assertNotIn("SecretCode", msg)
        self.assertNotIn("Token", msg)


class TestHandoffContextPackage(unittest.TestCase):
    """Human Handoff V1 (2026-08-15), Phase 3 -- the notification must
    carry useful Customer Intelligence context, not just a generic
    "contact customer" line."""

    def test_includes_stage_intent_and_identifiers_when_known(self):
        msg = build_handoff_message(
            reason="user_requested_human", customer_name="สมชาย", cust_code="SP1014",
            line_user_id="U123", customer_message="ขอคุยกับเจ้าหน้าที่",
            customer_stage="hot", primary_intent="order_status", current_topic="order_status",
            last_order_code="POS100820260809001", last_shipment_code="FT318220260726001",
            last_tracking="testlineOnNut007", conversation_summary="user: SP1014 | assistant: ok",
        )
        self.assertIn("Customer Stage: HOT", msg)
        self.assertIn("Primary Intent: order_status", msg)
        self.assertIn("Last Order: POS100820260809001", msg)
        self.assertIn("Last Shipment: FT318220260726001", msg)
        self.assertIn("Last Tracking: testlineOnNut007", msg)
        self.assertIn("Recent Conversation:", msg)
        self.assertIn("Recommended Action:", msg)

    def test_missing_context_renders_as_placeholder_not_blank_or_none(self):
        msg = build_handoff_message(
            reason="user_requested_human", customer_name=None, cust_code=None,
            line_user_id=None, customer_message="ขอคุยกับเจ้าหน้าที่",
        )
        self.assertNotIn("None", msg)
        self.assertIn("Customer Stage: UNKNOWN", msg)
        self.assertIn("Last Order: -", msg)

    def test_recommended_action_differs_by_stage(self):
        hot_msg = build_handoff_message(reason="customer_intelligence_recommended", customer_name=None,
                                         cust_code=None, line_user_id=None, customer_message="x",
                                         customer_stage="hot")
        negative_msg = build_handoff_message(reason="customer_intelligence_recommended", customer_name=None,
                                              cust_code=None, line_user_id=None, customer_message="x",
                                              customer_stage="negative")
        self.assertIn("ปิดการขาย", hot_msg)
        self.assertIn("complaint follow-up", negative_msg)

    def test_never_contains_a_secret_looking_key_with_full_context(self):
        msg = build_handoff_message(
            reason="user_requested_human", customer_name="X", cust_code="SP1014",
            line_user_id="U1", customer_message="ขอคุยกับเจ้าหน้าที่",
            customer_stage="negative", primary_intent="complaint",
            conversation_summary="user: ของหาย | assistant: ขอโทษด้วยค่ะ",
        )
        self.assertNotIn("SecretCode", msg)
        self.assertNotIn("Token", msg)


class TestSendHandoffNotification(unittest.TestCase):
    def test_executes_the_notification_action_with_built_message(self):
        reg = _FakeRegistry([NOTIFY_ACTION])
        executor = _FakeExecutor({"status": "success", "result": {}})
        result = send_handoff_notification(
            reason="user_requested_human", customer_name="สมชาย", cust_code="SP1014",
            line_user_id="U123", customer_message="ขอคุยกับเจ้าหน้าที่",
            registry=reg, executor=executor,
        )
        self.assertTrue(result["sent"])
        self.assertEqual(result["action_key"], "sendlinenotics")
        self.assertEqual(len(executor.calls), 1)
        action_id, context = executor.calls[0]
        self.assertEqual(action_id, "notify-1")
        self.assertIn("Message", context["collected_slots"])
        self.assertIn("ขอคุยกับเจ้าหน้าที่", context["collected_slots"]["Message"])
        # The context passed to the executor must never itself carry a
        # SecretCode value — that parameter resolves server-side inside
        # the executor via the Credential Store, never through here.
        self.assertNotIn("SecretCode", context["collected_slots"])

    def test_returns_not_sent_when_no_notification_action_exists(self):
        reg = _FakeRegistry([LOOKUP_ACTION])
        executor = _FakeExecutor({"status": "success", "result": {}})
        result = send_handoff_notification(
            reason="user_requested_human", customer_message="ขอคุยกับเจ้าหน้าที่",
            registry=reg, executor=executor,
        )
        self.assertFalse(result["sent"])
        self.assertEqual(result["error"], "no_notification_action_configured")
        self.assertEqual(executor.calls, [])

    def test_returns_not_sent_when_executor_reports_error(self):
        reg = _FakeRegistry([NOTIFY_ACTION])
        executor = _FakeExecutor({"status": "error", "error": "credential_disabled"})
        result = send_handoff_notification(
            reason="user_requested_human", customer_message="ขอคุยกับเจ้าหน้าที่",
            registry=reg, executor=executor,
        )
        self.assertFalse(result["sent"])
        self.assertEqual(result["error"], "credential_disabled")


if __name__ == "__main__":
    unittest.main()
