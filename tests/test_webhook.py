"""Tests for line_bot/webhook.py's routing dispatch (Production
Integration Sprint, 2026-08-02, Phase 1 Step H).

line_bot/webhook.py constructs a real LINE SDK WebhookHandler/Configuration
at import time from config.LINE_CHANNEL_SECRET/LINE_CHANNEL_TOKEN, which
crashes on the real (unset, None) values in this dev environment — that
constructor call has nothing to do with this module's own logic, so it is
patched to harmless fake strings for the ONE moment this module is first
imported (cached afterward in sys.modules, same as any other import).

Never calls the real LINE API, never calls DecisionEngine.decide() for
real (mocked at its call site), never sends a real LINE Notify request.
"""
import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

with patch("config.LINE_CHANNEL_SECRET", "test_channel_secret"), \
     patch("config.LINE_CHANNEL_TOKEN", "test_channel_token"):
    import line_bot.webhook as webhook_module


def _fake_event(text="สวัสดีค่ะ", user_id="U_test_user", webhook_event_id="test-webhook-event-id"):
    event = MagicMock()
    event.source.user_id = user_id
    event.message.text = text
    event.reply_token = "test_reply_token"
    event.webhook_event_id = webhook_event_id
    return event


class TestLegacyAdapterRemovedDependencies(unittest.TestCase):
    """Confirms the dead erp.bridge import (broken — erp/bridge.py itself
    imports a non-existent config.DATABASE_URL, and its get_stock/
    get_order were never called anywhere in this module) is gone, and
    that the legacy handler's own behavior — the thing being preserved
    as an isolated adapter — is otherwise completely unchanged."""

    def test_erp_bridge_is_not_imported(self):
        self.assertFalse(hasattr(webhook_module, "get_stock"))
        self.assertFalse(hasattr(webhook_module, "get_order"))

    def test_both_handler_functions_exist_and_are_isolated(self):
        self.assertTrue(callable(webhook_module._handle_message_legacy))
        self.assertTrue(callable(webhook_module._handle_message_via_decision_engine))


class TestRolloutFlagDispatch(unittest.TestCase):
    """DECISION_ENGINE_LIVE_ROUTING picks exactly one branch — never
    both, never neither."""

    def test_flag_false_calls_legacy_only(self):
        event = _fake_event()
        with patch.object(webhook_module, "DECISION_ENGINE_LIVE_ROUTING", False), \
             patch.object(webhook_module, "_handle_message_legacy") as mock_legacy, \
             patch.object(webhook_module, "_handle_message_via_decision_engine") as mock_de, \
             patch("services.webhook_event_dedup_service.get_webhook_event_dedup_service") as mock_get_dedup:
            mock_get_dedup.return_value.claim_event.return_value = True
            webhook_module.handle_message(event)
        mock_legacy.assert_called_once_with(event)
        mock_de.assert_not_called()

    def test_flag_true_calls_decision_engine_only(self):
        event = _fake_event()
        with patch.object(webhook_module, "DECISION_ENGINE_LIVE_ROUTING", True), \
             patch.object(webhook_module, "_handle_message_legacy") as mock_legacy, \
             patch.object(webhook_module, "_handle_message_via_decision_engine") as mock_de, \
             patch("services.webhook_event_dedup_service.get_webhook_event_dedup_service") as mock_get_dedup:
            mock_get_dedup.return_value.claim_event.return_value = True
            webhook_module.handle_message(event)
        mock_de.assert_called_once_with(event)
        mock_legacy.assert_not_called()


class TestWebhookRedeliveryDedup(unittest.TestCase):
    """Webhook Redelivery Dedup fix (Rapid-Message Concurrency
    investigation, 2026-08-25) — a redelivered event (same
    webhookEventId) must never re-run a full turn a second time,
    regardless of which rollout branch is active. The dedup check itself
    is real (WebhookEventDedupService against a fake Supabase enforcing
    the real migration's UNIQUE constraint) — only the two downstream
    handler functions are mocked, so this proves the DISPATCHER wiring,
    not the dedup logic itself (see test_webhook_event_dedup_service.py
    for that)."""

    def setUp(self):
        from tests.test_webhook_event_dedup_service import _FakeSupabaseWithUniqueConstraint
        from services.webhook_event_dedup_service import WebhookEventDedupService
        self.dedup_service = WebhookEventDedupService(_FakeSupabaseWithUniqueConstraint())
        self.dedup_patcher = patch("services.webhook_event_dedup_service.get_webhook_event_dedup_service",
                                    return_value=self.dedup_service)
        self.dedup_patcher.start()

    def tearDown(self):
        self.dedup_patcher.stop()

    def test_first_delivery_processes_normally(self):
        event = _fake_event(webhook_event_id="evt-first")
        with patch.object(webhook_module, "DECISION_ENGINE_LIVE_ROUTING", True), \
             patch.object(webhook_module, "_handle_message_via_decision_engine") as mock_de:
            webhook_module.handle_message(event)
        mock_de.assert_called_once_with(event)

    def test_redelivered_same_event_id_is_skipped_entirely(self):
        event1 = _fake_event(webhook_event_id="evt-redelivered")
        event2 = _fake_event(webhook_event_id="evt-redelivered")  # same id, e.g. LINE's own retry
        with patch.object(webhook_module, "DECISION_ENGINE_LIVE_ROUTING", True), \
             patch.object(webhook_module, "_handle_message_via_decision_engine") as mock_de:
            webhook_module.handle_message(event1)
            webhook_module.handle_message(event2)
        mock_de.assert_called_once_with(event1)  # second delivery never reaches the handler

    def test_different_event_ids_both_process(self):
        event1 = _fake_event(webhook_event_id="evt-A")
        event2 = _fake_event(webhook_event_id="evt-B")
        with patch.object(webhook_module, "DECISION_ENGINE_LIVE_ROUTING", True), \
             patch.object(webhook_module, "_handle_message_via_decision_engine") as mock_de:
            webhook_module.handle_message(event1)
            webhook_module.handle_message(event2)
        self.assertEqual(mock_de.call_count, 2)

    def test_redelivery_skip_applies_to_legacy_branch_too(self):
        event1 = _fake_event(webhook_event_id="evt-legacy-redelivered")
        event2 = _fake_event(webhook_event_id="evt-legacy-redelivered")
        with patch.object(webhook_module, "DECISION_ENGINE_LIVE_ROUTING", False), \
             patch.object(webhook_module, "_handle_message_legacy") as mock_legacy:
            webhook_module.handle_message(event1)
            webhook_module.handle_message(event2)
        mock_legacy.assert_called_once_with(event1)


class TestDecisionEngineAdapter(unittest.TestCase):
    """The new adapter reacts to DecisionEngine's response shape — it
    implements no routing/escalation/attachment-classification logic of
    its own (all of that already happened inside decide())."""

    def setUp(self):
        self.profile_patcher = patch.object(webhook_module, "get_profile", return_value={"display_name": "Test"})
        self.upsert_patcher = patch.object(webhook_module, "upsert_profile")
        self.reply_patcher = patch.object(webhook_module, "MessagingApi")
        self.profile_patcher.start()
        self.mock_upsert = self.upsert_patcher.start()
        self.mock_messaging_api_cls = self.reply_patcher.start()
        self.mock_line_bot_api = MagicMock()
        self.mock_messaging_api_cls.return_value = self.mock_line_bot_api

        # Phase 3.1-3.3 (2026-08-05) — Conversation History/User Profile
        # stats/Customer Tier persistence. Must be mocked at the module
        # level webhook.py actually imports from (never inline-imported
        # inside the function — that would silently bypass these patches
        # and write real rows to the real Supabase DB on every test run,
        # exactly the mistake this comment is here to prevent recurring).
        self.session_service_patcher = patch.object(webhook_module, "get_session_service")
        self.update_profile_patcher = patch.object(webhook_module, "update_profile_from_turn")
        self.update_tier_patcher = patch.object(webhook_module, "update_tier_for_profile")
        mock_get_session_service = self.session_service_patcher.start()
        self.mock_update_profile_from_turn = self.update_profile_patcher.start()
        self.mock_update_tier_for_profile = self.update_tier_patcher.start()
        self.mock_session_service = MagicMock()
        self.mock_session_service.get_or_create_active_conversation.return_value = {"id": "fake-session-id", "message_count": 0}
        self.mock_session_service.get_recent_history.return_value = []
        # Fix-2.1 — the webhook reads the full handoff state for its
        # episode-aware dedupe; default to a clean NONE state.
        self.mock_session_service.get_handoff_state.return_value = {
            "status": "NONE", "reason": None, "notified_at": None}
        mock_get_session_service.return_value = self.mock_session_service

    def tearDown(self):
        self.profile_patcher.stop()
        self.upsert_patcher.stop()
        self.reply_patcher.stop()
        self.session_service_patcher.stop()
        self.update_profile_patcher.stop()
        self.update_tier_patcher.stop()

    def _decide_result(self, *, text="answer text", routing_type="RAG", images=None, files=None,
                        handoff_payload=None):
        return {
            "reply": {"text": text, "images": images or [], "files": files or []},
            "routing": {"type": routing_type},
            "handoff_payload": handoff_payload,
            "alert": None, "error": None,
        }

    def test_normal_reply_sends_text_message_no_notify(self):
        with patch("services.decision_engine.DecisionEngine") as mock_engine_cls, \
             patch.object(webhook_module, "send_line_notify") as mock_notify:
            mock_engine_cls.return_value.decide.return_value = self._decide_result(text="คำตอบค่ะ")
            webhook_module._handle_message_via_decision_engine(_fake_event("คำถามทดสอบ"))
        mock_notify.assert_not_called()
        self.mock_line_bot_api.reply_message.assert_called_once()
        sent_messages = self.mock_line_bot_api.reply_message.call_args.kwargs["reply_message_request"].messages \
            if "reply_message_request" in self.mock_line_bot_api.reply_message.call_args.kwargs \
            else self.mock_line_bot_api.reply_message.call_args.args[0].messages
        self.assertTrue(any("คำตอบค่ะ" in m.text for m in sent_messages if hasattr(m, "text")))

    def test_human_handoff_routing_calls_send_handoff_notification(self):
        """Human Handoff sprint (2026-08-13) — routing_type=="HUMAN_HANDOFF"
        on the live Decision-Engine path now notifies CS through the real,
        Credential-Store-backed SendLineNotiCS Business Action (services/
        human_handoff_service.py), never the legacy LINE Notify token
        (send_line_notify, kept only for the deprecated legacy adapter).
        See TestHumanHandoffNotification below for the full end-to-end
        chain down to the mocked HTTP boundary."""
        self.mock_session_service.get_handoff_status.return_value = "NONE"
        with patch("services.decision_engine.DecisionEngine") as mock_engine_cls, \
             patch.object(webhook_module, "send_line_notify") as mock_legacy_notify, \
             patch("services.human_handoff_service.send_handoff_notification",
                   return_value={"sent": True, "action_key": "sendlinenotics", "error": None}) as mock_notify:
            mock_engine_cls.return_value.decide.return_value = self._decide_result(
                text="กำลังโอนสายให้เจ้าหน้าที่ค่ะ", routing_type="HUMAN_HANDOFF",
                handoff_payload={"reason": "ai_policy_escalation"})
            webhook_module._handle_message_via_decision_engine(_fake_event("ร้องเรียนบริการ"))
        mock_notify.assert_called_once()
        self.assertEqual(mock_notify.call_args.kwargs["reason"], "ai_policy_escalation")
        mock_legacy_notify.assert_not_called()
        self.mock_session_service.set_handoff_status.assert_any_call("fake-session-id", "NOTIFIED", reason="ai_policy_escalation")

    def test_images_converted_to_image_messages(self):
        with patch("services.decision_engine.DecisionEngine") as mock_engine_cls, \
             patch.object(webhook_module, "send_line_notify"):
            mock_engine_cls.return_value.decide.return_value = self._decide_result(
                images=["https://example.test/a.jpg", "https://example.test/b.jpg"])
            webhook_module._handle_message_via_decision_engine(_fake_event("ขอรูปหน่อย"))
        sent_messages = self.mock_line_bot_api.reply_message.call_args.args[0].messages
        image_messages = [m for m in sent_messages if hasattr(m, "original_content_url")]
        self.assertEqual(len(image_messages), 2)
        self.assertEqual(image_messages[0].original_content_url, "https://example.test/a.jpg")

    def test_files_become_text_links_appended_to_reply(self):
        with patch("services.decision_engine.DecisionEngine") as mock_engine_cls, \
             patch.object(webhook_module, "send_line_notify"):
            mock_engine_cls.return_value.decide.return_value = self._decide_result(
                text="นี่คือคู่มือค่ะ",
                files=[{"filename": "manual.pdf", "url": "https://example.test/manual.pdf", "attachment_type": "pdf"}])
            webhook_module._handle_message_via_decision_engine(_fake_event("ขอคู่มือ"))
        sent_messages = self.mock_line_bot_api.reply_message.call_args.args[0].messages
        combined_text = " ".join(m.text for m in sent_messages if hasattr(m, "text"))
        self.assertIn("manual.pdf", combined_text)
        self.assertIn("https://example.test/manual.pdf", combined_text)

    def test_handoff_suppresses_attachments(self):
        self.mock_session_service.get_handoff_status.return_value = "NONE"
        with patch("services.decision_engine.DecisionEngine") as mock_engine_cls, \
             patch.object(webhook_module, "send_line_notify"), \
             patch("services.human_handoff_service.send_handoff_notification",
                   return_value={"sent": True, "action_key": "sendlinenotics", "error": None}):
            mock_engine_cls.return_value.decide.return_value = self._decide_result(
                routing_type="HUMAN_HANDOFF", images=["https://example.test/a.jpg"],
                handoff_payload={"reason": "user_requested_human"})
            webhook_module._handle_message_via_decision_engine(_fake_event("ขอคุยกับเจ้าหน้าที่"))
        sent_messages = self.mock_line_bot_api.reply_message.call_args.args[0].messages
        image_messages = [m for m in sent_messages if hasattr(m, "original_content_url")]
        self.assertEqual(len(image_messages), 0)


class TestLineConfirmationFlow(unittest.TestCase):
    """LINE Confirmation Flow sprint (2026-08-10) — end-to-end at the
    webhook adapter layer: a real PendingConfirmationService backed by
    _FakeSupabase (never a real DB), DecisionEngine fully mocked at its
    call site (never a real ERP/LLM call). Covers scenarios A-G from the
    spec exactly."""

    ACTION_ID = "action-notify-1"
    TRIGGER = "ช่วยแจ้ง CS ให้ติดต่อกลับ"
    QUESTION = "ยืนยันให้ส่งข้อความแจ้ง CS ใช่ไหมคะ?"

    def setUp(self):
        from tests.test_business_action_registry import _FakeSupabase
        from services.pending_confirmation_service import PendingConfirmationService
        import config

        self.tenant_id = config.DEFAULT_TENANT_ID
        self.fake_sb = _FakeSupabase()
        self.pending_service = PendingConfirmationService(self.fake_sb)

        self.profile_patcher = patch.object(webhook_module, "get_profile", return_value={"display_name": "Test"})
        self.upsert_patcher = patch.object(webhook_module, "upsert_profile")
        self.reply_patcher = patch.object(webhook_module, "MessagingApi")
        self.pending_svc_patcher = patch("services.pending_confirmation_service.get_pending_confirmation_service",
                                          return_value=self.pending_service)
        self.profile_patcher.start()
        self.mock_upsert = self.upsert_patcher.start()
        self.mock_messaging_api_cls = self.reply_patcher.start()
        self.pending_svc_patcher.start()
        self.mock_line_bot_api = MagicMock()
        self.mock_messaging_api_cls.return_value = self.mock_line_bot_api

        self.session_service_patcher = patch.object(webhook_module, "get_session_service")
        self.update_profile_patcher = patch.object(webhook_module, "update_profile_from_turn")
        self.update_tier_patcher = patch.object(webhook_module, "update_tier_for_profile")
        mock_get_session_service = self.session_service_patcher.start()
        self.update_profile_patcher.start()
        self.update_tier_patcher.start()
        mock_session_service = MagicMock()
        mock_session_service.get_or_create_active_conversation.return_value = {"id": "fake-session-id", "message_count": 0}
        mock_session_service.get_recent_history.return_value = []
        mock_get_session_service.return_value = mock_session_service

    def tearDown(self):
        self.profile_patcher.stop()
        self.upsert_patcher.stop()
        self.reply_patcher.stop()
        self.pending_svc_patcher.stop()
        self.session_service_patcher.stop()
        self.update_profile_patcher.stop()
        self.update_tier_patcher.stop()

    def _confirmation_required_result(self, message_value, question_text=QUESTION):
        return {
            "reply": {"text": question_text, "images": [], "files": []},
            "routing": {"type": "WORKFLOW"}, "handoff_payload": None, "alert": None, "error": None,
            "developer": {
                "confirmation_gate": {"required": True, "confirmed": False,
                                       "action_id": self.ACTION_ID, "action_key": "sendlinenotics"},
                "information_collection_status": {"collected_parameters": {"Message": message_value}},
            },
        }

    def _executed_result(self):
        return {
            "reply": {"text": "สถานะการส่ง: success", "images": [], "files": []},
            "routing": {"type": "API"}, "handoff_payload": None, "alert": None, "error": None,
            "developer": {"information_collection_status": {"collected_parameters": {"Message": self.TRIGGER}}},
        }

    def _seed_pending(self, user_id="U1", message_value=TRIGGER, question_text=QUESTION):
        action = {"id": self.ACTION_ID, "action_key": "sendlinenotics",
                  "parameters": [{"name": "SecretCode", "input_source": "credential_store"},
                                 {"name": "Message", "input_source": "customer_message"}]}
        return self.pending_service.create(
            tenant_id=self.tenant_id, channel="line", conversation_key=user_id, action=action,
            parameters={"Message": message_value}, original_message=self.TRIGGER, question_text=question_text)

    # A ─────────────────────────────────────────────────────────────
    def test_A_request_asks_confirmation_zero_execution(self):
        with patch("services.decision_engine.DecisionEngine") as mock_engine_cls:
            mock_engine_cls.return_value.decide.return_value = self._confirmation_required_result(self.TRIGGER)
            mock_engine_cls.return_value.registry.get_full.return_value = {
                "id": self.ACTION_ID, "action_key": "sendlinenotics",
                "parameters": [{"name": "SecretCode", "input_source": "credential_store"},
                               {"name": "Message", "input_source": "customer_message"}]}
            webhook_module._handle_message_via_decision_engine(_fake_event(self.TRIGGER, user_id="U1"))
            mock_engine_cls.return_value.decide.assert_called_once()
            call_context = mock_engine_cls.return_value.decide.call_args.kwargs["context"]
            self.assertNotIn("confirmed", call_context)

        sent_messages = self.mock_line_bot_api.reply_message.call_args.args[0].messages
        self.assertTrue(any(self.QUESTION in m.text for m in sent_messages if hasattr(m, "text")))

        pending = self.pending_service.get_active(tenant_id=self.tenant_id, channel="line", conversation_key="U1")
        self.assertIsNotNone(pending)
        self.assertEqual(pending["pending_action_id"], self.ACTION_ID)
        self.assertEqual(pending["pending_parameters"], {"Message": self.TRIGGER})
        self.assertNotIn("SecretCode", pending["pending_parameters"])

    # B ─────────────────────────────────────────────────────────────
    def test_B_confirm_triggers_exactly_one_execution(self):
        seeded = self._seed_pending(user_id="U1")
        with patch("services.decision_engine.DecisionEngine") as mock_engine_cls:
            mock_engine_cls.return_value.decide.return_value = self._executed_result()
            webhook_module._handle_message_via_decision_engine(_fake_event("ยืนยัน", user_id="U1"))
            mock_engine_cls.return_value.decide.assert_called_once()
            call_kwargs = mock_engine_cls.return_value.decide.call_args.kwargs
            self.assertTrue(call_kwargs["context"]["confirmed"])
            # Confirmation Continuation Correctness fix (2026-08-23) — the
            # pending row's own action_id/parameters are passed directly,
            # never re-derived from a synthetic history replay.
            self.assertEqual(call_kwargs["context"]["confirmed_action_id"], self.ACTION_ID)
            self.assertEqual(call_kwargs["context"]["confirmed_parameters"], {"Message": self.TRIGGER})

        row = [r for r in self.fake_sb.store["pending_confirmations"] if r["id"] == seeded["id"]][0]
        self.assertEqual(row["status"], "executed")
        self.assertIsNotNone(row["confirmation_confirmed_at"])
        self.assertIsNone(self.pending_service.get_active(tenant_id=self.tenant_id, channel="line", conversation_key="U1"))

    # C ─────────────────────────────────────────────────────────────
    def test_C_cancel_zero_execution(self):
        seeded = self._seed_pending(user_id="U1")
        with patch("services.decision_engine.DecisionEngine") as mock_engine_cls:
            webhook_module._handle_message_via_decision_engine(_fake_event("ไม่ต้อง", user_id="U1"))
            mock_engine_cls.return_value.decide.assert_not_called()

        row = [r for r in self.fake_sb.store["pending_confirmations"] if r["id"] == seeded["id"]][0]
        self.assertEqual(row["status"], "cancelled")
        self.assertIsNotNone(row["confirmation_cancelled_at"])
        sent_messages = self.mock_line_bot_api.reply_message.call_args.args[0].messages
        self.assertTrue(any("ยกเลิก" in m.text for m in sent_messages if hasattr(m, "text")))

    # D ─────────────────────────────────────────────────────────────
    def test_D_revision_updates_message_and_reasks_confirmation(self):
        seeded = self._seed_pending(user_id="U1")
        revised_message = "ขอแก้ข้อความเป็น ติดต่อกลับพรุ่งนี้"
        new_question = "ยืนยันให้ส่งข้อความแจ้ง CS (ฉบับแก้ไข) ใช่ไหมคะ?"
        with patch("services.decision_engine.DecisionEngine") as mock_engine_cls:
            mock_engine_cls.return_value.decide.return_value = self._confirmation_required_result(
                revised_message, question_text=new_question)
            mock_engine_cls.return_value.registry.get_full.return_value = {
                "id": self.ACTION_ID, "action_key": "sendlinenotics",
                "parameters": [{"name": "SecretCode", "input_source": "credential_store"},
                               {"name": "Message", "input_source": "customer_message"}]}
            webhook_module._handle_message_via_decision_engine(_fake_event(revised_message, user_id="U1"))
            mock_engine_cls.return_value.decide.assert_called_once()
            call_kwargs = mock_engine_cls.return_value.decide.call_args.kwargs
            self.assertEqual(call_kwargs["history"], [])
            self.assertNotIn("confirmed", call_kwargs["context"])

        old_row = [r for r in self.fake_sb.store["pending_confirmations"] if r["id"] == seeded["id"]][0]
        self.assertEqual(old_row["status"], "cancelled")

        new_pending = self.pending_service.get_active(tenant_id=self.tenant_id, channel="line", conversation_key="U1")
        self.assertIsNotNone(new_pending)
        self.assertEqual(new_pending["pending_parameters"], {"Message": revised_message})
        sent_messages = self.mock_line_bot_api.reply_message.call_args.args[0].messages
        self.assertTrue(any(new_question in m.text for m in sent_messages if hasattr(m, "text")))

    # E ─────────────────────────────────────────────────────────────
    def test_E_expired_confirmation_zero_execution(self):
        from datetime import datetime, timedelta, timezone
        seeded = self._seed_pending(user_id="U1")
        past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        self.fake_sb.table("pending_confirmations").update({"expires_at": past}).eq("id", seeded["id"]).execute()

        with patch("services.decision_engine.DecisionEngine") as mock_engine_cls:
            webhook_module._handle_message_via_decision_engine(_fake_event("ยืนยัน", user_id="U1"))
            mock_engine_cls.return_value.decide.assert_not_called()

        row = [r for r in self.fake_sb.store["pending_confirmations"] if r["id"] == seeded["id"]][0]
        self.assertEqual(row["status"], "expired")
        sent_messages = self.mock_line_bot_api.reply_message.call_args.args[0].messages
        self.assertTrue(any("หมดเวลา" in m.text for m in sent_messages if hasattr(m, "text")))

    # F ─────────────────────────────────────────────────────────────
    def test_F_different_user_cannot_confirm_another_users_pending(self):
        seeded = self._seed_pending(user_id="U1")
        with patch("services.decision_engine.DecisionEngine") as mock_engine_cls:
            mock_engine_cls.return_value.decide.return_value = self._decide_result_plain("ไม่พบคำขอที่รอดำเนินการค่ะ")
            webhook_module._handle_message_via_decision_engine(_fake_event("ยืนยัน", user_id="U2"))
            call_kwargs = mock_engine_cls.return_value.decide.call_args.kwargs
            self.assertNotIn("confirmed", call_kwargs["context"])

        row = [r for r in self.fake_sb.store["pending_confirmations"] if r["id"] == seeded["id"]][0]
        self.assertEqual(row["status"], "pending")  # U1's pending action untouched
        self.assertIsNone(self.pending_service.get_active(tenant_id=self.tenant_id, channel="line", conversation_key="U2"))

    # G ─────────────────────────────────────────────────────────────
    def test_G_repeated_confirm_never_executes_twice(self):
        seeded = self._seed_pending(user_id="U1")
        with patch("services.decision_engine.DecisionEngine") as mock_engine_cls:
            mock_engine_cls.return_value.decide.return_value = self._executed_result()
            webhook_module._handle_message_via_decision_engine(_fake_event("ยืนยัน", user_id="U1"))
        self.assertEqual(mock_engine_cls.return_value.decide.call_count, 1)

        # Second, repeated "ยืนยัน" — nothing left to confirm.
        with patch("services.decision_engine.DecisionEngine") as mock_engine_cls2:
            mock_engine_cls2.return_value.decide.return_value = self._decide_result_plain("ไม่พบคำขอที่รอดำเนินการค่ะ")
            webhook_module._handle_message_via_decision_engine(_fake_event("ยืนยัน", user_id="U1"))
            call_kwargs = mock_engine_cls2.return_value.decide.call_args.kwargs
            # Never re-confirmed on this second pass — the earlier pending
            # row is already resolved, so this call cannot re-trigger it.
            self.assertNotIn("confirmed", call_kwargs["context"])

        matching = [r for r in self.fake_sb.store["pending_confirmations"] if r["id"] == seeded["id"]]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["status"], "executed")

    # H ─────────────────────────────────────────────────────────────
    def test_H_multi_turn_collected_parameters_survive_confirmation(self):
        """Confirmation Continuation Correctness fix (2026-08-23) —
        confirmed live: a parameter (e.g. CustCode) given several turns
        BEFORE the confirmation question is never present in the pending
        row's own `original_message` (which only captures the LAST
        triggering message), so replaying just [original_message,
        question_text] as history could never recover it. This pending
        row's `pending_parameters` deliberately includes a value absent
        from `original_message`/TRIGGER entirely, proving the webhook
        passes the FULL stored parameter dict straight through instead of
        relying on any history replay to reconstruct it."""
        action = {"id": self.ACTION_ID, "action_key": "sendlinenotics",
                  "parameters": [{"name": "SecretCode", "input_source": "credential_store"},
                                 {"name": "CustCode", "input_source": "customer_message"},
                                 {"name": "Message", "input_source": "customer_message"}]}
        seeded = self.pending_service.create(
            tenant_id=self.tenant_id, channel="line", conversation_key="U1", action=action,
            parameters={"CustCode": "SP1008", "Message": self.TRIGGER},
            original_message=self.TRIGGER, question_text=self.QUESTION)

        with patch("services.decision_engine.DecisionEngine") as mock_engine_cls:
            mock_engine_cls.return_value.decide.return_value = self._executed_result()
            webhook_module._handle_message_via_decision_engine(_fake_event("ยืนยัน", user_id="U1"))
            call_kwargs = mock_engine_cls.return_value.decide.call_args.kwargs
            self.assertTrue(call_kwargs["context"]["confirmed"])
            self.assertEqual(call_kwargs["context"]["confirmed_action_id"], self.ACTION_ID)
            self.assertEqual(call_kwargs["context"]["confirmed_parameters"],
                              {"CustCode": "SP1008", "Message": self.TRIGGER})

        row = [r for r in self.fake_sb.store["pending_confirmations"] if r["id"] == seeded["id"]][0]
        self.assertEqual(row["status"], "executed")

    @staticmethod
    def _decide_result_plain(text):
        return {"reply": {"text": text, "images": [], "files": []}, "routing": {"type": "RAG"},
                "handoff_payload": None, "alert": None, "error": None, "developer": {}}


class TestContextContinuityAndIsolation(unittest.TestCase):
    """Customer Intelligence V1 (2026-08-15), Phase 4/8 — scenarios F and
    G, end to end at the webhook adapter layer through a REAL
    SessionService backed by a fake in-memory Supabase (never a real DB).
    DecisionEngine.decide() is mocked at its call site only; everything
    else (Context Continuity's history window, Conversation History
    persistence, per-user isolation) runs for real."""

    def setUp(self):
        from tests.test_session_service import FakeSb
        import services.session_service as ss

        self.fake_sb = FakeSb()
        self.real_session_service = ss.SessionService()
        self.sb_patcher = patch("services.session_service._get_sb", return_value=self.fake_sb)
        self.sb_patcher.start()
        self.addCleanup(self.sb_patcher.stop)

        self.profile_patcher = patch.object(webhook_module, "get_profile", return_value={"display_name": "Test"})
        self.upsert_patcher = patch.object(webhook_module, "upsert_profile")
        self.reply_patcher = patch.object(webhook_module, "MessagingApi")
        self.session_service_patcher = patch.object(webhook_module, "get_session_service",
                                                       return_value=self.real_session_service)
        self.update_profile_patcher = patch.object(webhook_module, "update_profile_from_turn")
        self.update_tier_patcher = patch.object(webhook_module, "update_tier_for_profile")
        self.profile_patcher.start()
        self.upsert_patcher.start()
        self.mock_messaging_api_cls = self.reply_patcher.start()
        self.session_service_patcher.start()
        self.update_profile_patcher.start()
        self.update_tier_patcher.start()
        self.mock_line_bot_api = MagicMock()
        self.mock_messaging_api_cls.return_value = self.mock_line_bot_api

    @staticmethod
    def _plain_result(text="ok ค่ะ"):
        return {"reply": {"text": text, "images": [], "files": []}, "routing": {"type": "RAG"},
                "handoff_payload": None, "alert": None, "error": None, "developer": {}}

    def _send(self, mock_engine_cls, text, user_id):
        mock_engine_cls.return_value.decide.return_value = self._plain_result()
        webhook_module._handle_message_via_decision_engine(_fake_event(text, user_id=user_id))
        return mock_engine_cls.return_value.decide.call_args.kwargs

    # F ─────────────────────────────────────────────────────────────
    def test_f_turn_two_receives_turn_one_as_history(self):
        with patch("services.decision_engine.DecisionEngine") as mock_engine_cls:
            self._send(mock_engine_cls, "ผม SP1014 ขอดู PO ล่าสุด", user_id="U_F")
        with patch("services.decision_engine.DecisionEngine") as mock_engine_cls2:
            call_kwargs = self._send(mock_engine_cls2, "แล้วส่งหรือยัง", user_id="U_F")

        history = call_kwargs["history"]
        contents = [h["content"] for h in history]
        self.assertTrue(any("SP1014" in c for c in contents),
                         f"turn 1's message should be in turn 2's history, got: {contents}")

    def test_first_turn_of_a_conversation_gets_empty_history(self):
        with patch("services.decision_engine.DecisionEngine") as mock_engine_cls:
            call_kwargs = self._send(mock_engine_cls, "สวัสดีค่ะ", user_id="U_FIRST")
        self.assertEqual(call_kwargs["history"], [])

    # Address Change Full UAT — Status Query fix companion (2026-08-24) —
    # confirmed live: a Dynamic Collection flow with more than 3
    # sequential exchanges (requestshippingaddresschange alone has 9
    # askable fields) had its EARLIEST turns (where an identifier like
    # CustCode was established) silently drop out of the history this
    # exact real call site passes to decide() once get_recent_history's
    # own default (3 exchanges) was exceeded — _resolve_continuation_
    # action has no fallback beyond raw history, so it stopped
    # recognizing continuation entirely regardless of what the customer
    # said next. Proves the fix (max_turns=20) against the REAL
    # SessionService, not a mock.
    def test_g_long_flow_beyond_three_exchanges_retains_earliest_history(self):
        with patch("services.decision_engine.DecisionEngine") as mock_engine_cls:
            self._send(mock_engine_cls, "SP1008", user_id="U_LONG")
        for msg in ("ต้องการเปลี่ยนที่อยู่บิลขนส่ง", "SP100820260716001",
                    "ชื่อผู้รับ: สมชาย ใจดี", "เบอร์โทร: 0812345678"):
            with patch("services.decision_engine.DecisionEngine") as mock_engine_cls:
                self._send(mock_engine_cls, msg, user_id="U_LONG")
        with patch("services.decision_engine.DecisionEngine") as mock_engine_cls:
            call_kwargs = self._send(mock_engine_cls, "มีข้อมูลอะไรบ้าง", user_id="U_LONG")
        contents = [h["content"] for h in call_kwargs["history"]]
        self.assertTrue(any("SP1008" in c for c in contents),
                         f"turn 1's CustCode should still be in history 5 exchanges later, got: {contents}")

    # G ─────────────────────────────────────────────────────────────
    def test_g_two_users_histories_never_cross(self):
        with patch("services.decision_engine.DecisionEngine") as mock_engine_cls_a1:
            self._send(mock_engine_cls_a1, "ผม SP1014 ขอดู PO ล่าสุด", user_id="U_A")
        with patch("services.decision_engine.DecisionEngine") as mock_engine_cls_b1:
            self._send(mock_engine_cls_b1, "ผม FT1004 ขอดูพัสดุ", user_id="U_B")

        with patch("services.decision_engine.DecisionEngine") as mock_engine_cls_a2:
            call_kwargs_a = self._send(mock_engine_cls_a2, "แล้วส่งหรือยัง", user_id="U_A")
        with patch("services.decision_engine.DecisionEngine") as mock_engine_cls_b2:
            call_kwargs_b = self._send(mock_engine_cls_b2, "ของถึงหรือยัง", user_id="U_B")

        contents_a = [h["content"] for h in call_kwargs_a["history"]]
        contents_b = [h["content"] for h in call_kwargs_b["history"]]
        self.assertTrue(any("SP1014" in c for c in contents_a))
        self.assertFalse(any("FT1004" in c for c in contents_a))
        self.assertTrue(any("FT1004" in c for c in contents_b))
        self.assertFalse(any("SP1014" in c for c in contents_b))


class TestHumanHandoffNotification(unittest.TestCase):
    """Human Handoff sprint (2026-08-13), Phase 6 — scenarios A-H, end to
    end at the webhook adapter layer through the REAL
    human_handoff_service -> ActionExecutor chain (a real
    BusinessActionRegistry backed by _FakeSupabase, never a real DB),
    with ONLY the actual network boundary
    (services.action_executor.requests.request) mocked — proves the
    whole pipeline works, not just that a service function was called
    with the right arguments. DecisionEngine.decide() itself is mocked
    at its call site (same convention as every other class in this
    file) so these tests exercise the ADAPTER's reaction to a given
    decide() result, not decide()'s own routing logic (covered in
    tests/test_decision_engine.py). Never calls the real SendLineNotiCS
    endpoint."""

    def setUp(self):
        from tests.test_business_action_registry import _FakeSupabase
        from tests.test_decision_engine import _seed_action
        from services.business_action_registry import BusinessActionRegistry
        from services.action_executor import ActionExecutor

        self.fake_sb = _FakeSupabase()
        self.reg = BusinessActionRegistry(self.fake_sb)
        action_id = _seed_action(self.reg, key="sendlinenotics", action_type="API",
                                  category="notification", keywords=["แจ้งเตือน"])
        self.reg.update(action_id, {"setup_metadata": {"operation_type": "NOTIFICATION"}})
        self.reg.replace_parameters(action_id, [
            {"name": "SecretCode", "required": True, "input_source": "credential_store",
             "credential_ref": "fake_secret", "visible_to_customer": False, "visible_in_developer_mode": False},
            {"name": "Message", "display_name": "ข้อความแจ้งเตือน", "required": True,
             "input_source": "customer_message", "validation_type": "non_empty"},
        ])
        self.reg.upsert_execution(action_id, {"endpoint": "https://example.test/notify", "http_method": "POST"})
        self.action_id = action_id
        self.executor = ActionExecutor(self.fake_sb)

        # human_handoff_service resolves get_registry()/get_action_executor()
        # with no args when the caller (webhook.py) doesn't inject them —
        # point both at THIS test's fake registry/executor so the whole
        # chain (webhook -> human_handoff_service -> ActionExecutor ->
        # requests.request) is real except the actual network call.
        self.registry_patcher = patch("services.human_handoff_service.get_registry", return_value=self.reg)
        self.executor_patcher = patch("services.human_handoff_service.get_action_executor", return_value=self.executor)
        self.registry_patcher.start()
        self.executor_patcher.start()

        self.profile_patcher = patch.object(webhook_module, "get_profile", return_value={"display_name": "Test"})
        self.upsert_patcher = patch.object(webhook_module, "upsert_profile")
        self.reply_patcher = patch.object(webhook_module, "MessagingApi")
        self.profile_patcher.start()
        self.upsert_patcher.start()
        self.mock_messaging_api_cls = self.reply_patcher.start()
        self.mock_line_bot_api = MagicMock()
        self.mock_messaging_api_cls.return_value = self.mock_line_bot_api

        self.update_profile_patcher = patch.object(webhook_module, "update_profile_from_turn")
        self.update_tier_patcher = patch.object(webhook_module, "update_tier_for_profile")
        self.update_profile_patcher.start()
        self.update_tier_patcher.start()

        # Per-test in-memory conversation/handoff-state store standing in
        # for ai_sessions — SessionService itself isn't under test here,
        # only that webhook.py correctly consults/updates handoff state
        # THROUGH it (real dedup semantics, not a rubber-stamp mock).
        self._conversations: dict = {}
        # Fix-2.1 — the in-memory ai_sessions stand-in now tracks the FULL
        # handoff state (status + reason + notified_at) so webhook.py's
        # episode-aware dedupe is exercised for real, not rubber-stamped.
        # `_handoff_status` is kept as a status-only mirror for the
        # existing assertions further down.
        self._handoff_state: dict = {}
        self._handoff_status: dict = {}
        from datetime import datetime as _dt, timezone as _tz

        def _get_or_create(user_id, **kwargs):
            if user_id not in self._conversations:
                cid = f"conv-{user_id}"
                self._conversations[user_id] = {"id": cid, "message_count": 0}
                self._handoff_state[cid] = {"status": "NONE", "reason": None, "notified_at": None}
                self._handoff_status[cid] = "NONE"
            return self._conversations[user_id]

        def _get_status(conversation_id):
            return self._handoff_state.get(conversation_id, {}).get("status", "NONE")

        def _get_state(conversation_id):
            return self._handoff_state.get(
                conversation_id, {"status": "NONE", "reason": None, "notified_at": None})

        def _set_status(conversation_id, status, reason=None):
            st = self._handoff_state.setdefault(
                conversation_id, {"status": "NONE", "reason": None, "notified_at": None})
            st["status"] = status
            if reason is not None:
                st["reason"] = reason
            if status == "NOTIFIED":
                st["notified_at"] = _dt.now(_tz.utc).isoformat()
            self._handoff_status[conversation_id] = status

        self.mock_session_service = MagicMock()
        self.mock_session_service.get_or_create_active_conversation.side_effect = _get_or_create
        self.mock_session_service.get_handoff_status.side_effect = _get_status
        self.mock_session_service.get_handoff_state.side_effect = _get_state
        self.mock_session_service.set_handoff_status.side_effect = _set_status
        self.mock_session_service.get_recent_history.return_value = []
        self.session_service_patcher = patch.object(webhook_module, "get_session_service",
                                                       return_value=self.mock_session_service)
        self.session_service_patcher.start()

    def tearDown(self):
        self.registry_patcher.stop()
        self.executor_patcher.stop()
        self.profile_patcher.stop()
        self.upsert_patcher.stop()
        self.reply_patcher.stop()
        self.update_profile_patcher.stop()
        self.update_tier_patcher.stop()
        self.session_service_patcher.stop()

    @staticmethod
    def _handoff_decide_result(reason="user_requested_human", collected=None):
        return {
            "reply": {"text": "ได้เลยค่ะ เดี๋ยวแจ้งเจ้าหน้าที่ให้ติดต่อกลับนะคะ", "images": [], "files": []},
            "routing": {"type": "HUMAN_HANDOFF"},
            "handoff_payload": {"reason": reason},
            "alert": None, "error": None,
            "developer": {"information_collection_status": {"collected_parameters": collected or {}}},
        }

    @staticmethod
    def _normal_decide_result(text, routing_type):
        return {
            "reply": {"text": text, "images": [], "files": []},
            "routing": {"type": routing_type}, "handoff_payload": None, "alert": None, "error": None,
            "developer": None,
        }

    def _mock_http_success(self):
        return patch("services.action_executor.requests.request",
                      return_value=MagicMock(status_code=200, json=lambda: {"status": "success"}))

    def _mock_credential(self, value="REAL-SECRET-abc123"):
        return patch("services.credential_store.CredentialStore.resolve",
                      return_value={"ok": True, "value": value, "error": None})

    # A ─────────────────────────────────────────────────────────────
    def test_A_explicit_human_request_sends_exactly_one_mocked_notification(self):
        with patch("services.decision_engine.DecisionEngine") as mock_engine_cls, \
             self._mock_http_success() as mock_req, self._mock_credential():
            mock_engine_cls.return_value.decide.return_value = self._handoff_decide_result()
            webhook_module._handle_message_via_decision_engine(_fake_event("ขอคุยกับเจ้าหน้าที่", user_id="UA"))
        mock_req.assert_called_once()
        sent_body = mock_req.call_args.kwargs.get("data") or mock_req.call_args.kwargs.get("json") or {}
        self.assertIn("Message", sent_body)
        self.assertEqual(self._handoff_status["conv-UA"], "NOTIFIED")

    # B ─────────────────────────────────────────────────────────────
    def test_B_cs_callback_request_sends_exactly_one_mocked_notification(self):
        with patch("services.decision_engine.DecisionEngine") as mock_engine_cls, \
             self._mock_http_success() as mock_req, self._mock_credential():
            mock_engine_cls.return_value.decide.return_value = self._handoff_decide_result()
            webhook_module._handle_message_via_decision_engine(_fake_event("ขอให้ CS ติดต่อกลับ", user_id="UB"))
        mock_req.assert_called_once()
        self.assertEqual(self._handoff_status["conv-UB"], "NOTIFIED")

    # C ─────────────────────────────────────────────────────────────
    def test_C_normal_rag_question_sends_zero_notifications(self):
        with patch("services.decision_engine.DecisionEngine") as mock_engine_cls, \
             patch("services.action_executor.requests.request") as mock_req:
            mock_engine_cls.return_value.decide.return_value = self._normal_decide_result("คำตอบค่ะ", "RAG")
            webhook_module._handle_message_via_decision_engine(_fake_event("นำเข้าสินค้าทำอย่างไร", user_id="UC"))
        mock_req.assert_not_called()

    # D ─────────────────────────────────────────────────────────────
    def test_D_rag_no_answer_sends_zero_notifications(self):
        with patch("services.decision_engine.DecisionEngine") as mock_engine_cls, \
             patch("services.action_executor.requests.request") as mock_req:
            mock_engine_cls.return_value.decide.return_value = self._normal_decide_result(
                "ขอโทษด้วยค่ะ ตอนนี้ยังไม่พบคำตอบที่ชัดเจนสำหรับคำถามนี้", "SAFE_FALLBACK")
            webhook_module._handle_message_via_decision_engine(_fake_event("คำถามที่ไม่มีคำตอบ", user_id="UD"))
        mock_req.assert_not_called()

    # E ─────────────────────────────────────────────────────────────
    def test_E_normal_erp_request_sends_zero_notifications(self):
        with patch("services.decision_engine.DecisionEngine") as mock_engine_cls, \
             patch("services.action_executor.requests.request") as mock_req:
            mock_engine_cls.return_value.decide.return_value = self._normal_decide_result(
                "รหัสลูกค้า: SP1014", "API")
            webhook_module._handle_message_via_decision_engine(_fake_event("ขอดูข้อมูลลูกค้า SP1014", user_id="UE"))
        mock_req.assert_not_called()

    # F ─────────────────────────────────────────────────────────────
    def test_F_repeated_handoff_request_sends_only_one_notification(self):
        with patch("services.decision_engine.DecisionEngine") as mock_engine_cls, \
             self._mock_http_success() as mock_req, self._mock_credential():
            mock_engine_cls.return_value.decide.return_value = self._handoff_decide_result()
            webhook_module._handle_message_via_decision_engine(_fake_event("ขอคุยกับเจ้าหน้าที่", user_id="UF"))
            webhook_module._handle_message_via_decision_engine(_fake_event("มีใครอยู่ไหม", user_id="UF"))
            webhook_module._handle_message_via_decision_engine(_fake_event("ช่วยตอบหน่อย", user_id="UF"))
        mock_req.assert_called_once()
        self.assertEqual(self._handoff_status["conv-UF"], "NOTIFIED")

    # G ─────────────────────────────────────────────────────────────
    def test_G_different_users_get_independent_handoff_state(self):
        with patch("services.decision_engine.DecisionEngine") as mock_engine_cls, \
             self._mock_http_success() as mock_req, self._mock_credential():
            mock_engine_cls.return_value.decide.return_value = self._handoff_decide_result()
            webhook_module._handle_message_via_decision_engine(_fake_event("ขอคุยกับเจ้าหน้าที่", user_id="UG1"))
            webhook_module._handle_message_via_decision_engine(_fake_event("ขอคุยกับเจ้าหน้าที่", user_id="UG2"))
        self.assertEqual(mock_req.call_count, 2)
        self.assertEqual(self._handoff_status["conv-UG1"], "NOTIFIED")
        self.assertEqual(self._handoff_status["conv-UG2"], "NOTIFIED")

    # H ─────────────────────────────────────────────────────────────
    def test_H_secret_code_resolved_via_credential_store_and_never_leaked_to_customer(self):
        real_secret = "REAL-SECRET-abc123"
        with patch("services.decision_engine.DecisionEngine") as mock_engine_cls, \
             self._mock_http_success() as mock_req, self._mock_credential(real_secret) as mock_resolve:
            mock_engine_cls.return_value.decide.return_value = self._handoff_decide_result()
            webhook_module._handle_message_via_decision_engine(_fake_event("ขอคุยกับเจ้าหน้าที่", user_id="UH"))
        mock_resolve.assert_called_once()
        # Resolved via Credential Store and sent on the real outbound
        # call (expected — that's what authenticates the request)...
        sent_body = mock_req.call_args.kwargs.get("data") or mock_req.call_args.kwargs.get("json") or {}
        self.assertEqual(sent_body.get("SecretCode"), real_secret)
        # ...but never appears in what the customer actually receives back.
        sent_messages = self.mock_line_bot_api.reply_message.call_args.args[0].messages
        combined_reply_text = " ".join(m.text for m in sent_messages if hasattr(m, "text"))
        self.assertNotIn(real_secret, combined_reply_text)

    # Context Package ───────────────────────────────────────────────
    def test_handoff_notification_carries_customer_intelligence_context(self):
        """Human Handoff V1 (2026-08-15), Phase 3 -- the notification
        must include the Customer Intelligence profile's remembered
        stage/identifiers even when THIS turn's own message didn't
        mention them (a bare "ขอคุยกับเจ้าหน้าที่" with SP1014 established
        two turns ago)."""
        rich_profile = {
            "display_name": "สมชาย", "conversation_tier": "negative",
            "primary_intent": "complaint", "cust_code": "SP1014",
            "last_order_code": "POS100820260809001", "last_shipment_code": "FT318220260726001",
            "last_tracking": "testlineOnNut007",
        }
        with patch.object(webhook_module, "get_profile", return_value=rich_profile), \
             patch("services.decision_engine.DecisionEngine") as mock_engine_cls, \
             self._mock_http_success() as mock_req, self._mock_credential():
            mock_engine_cls.return_value.decide.return_value = self._handoff_decide_result()
            webhook_module._handle_message_via_decision_engine(_fake_event("ขอคุยกับเจ้าหน้าที่", user_id="UCTX"))

        sent_body = mock_req.call_args.kwargs.get("data") or mock_req.call_args.kwargs.get("json") or {}
        message = sent_body.get("Message") or ""
        self.assertIn("SP1014", message)
        self.assertIn("POS100820260809001", message)
        self.assertIn("FT318220260726001", message)
        self.assertIn("testlineOnNut007", message)
        self.assertIn("Customer Stage:", message)
        self.assertIn("Recommended Action:", message)
        # Still never a credential value, even with the full context
        # package populated.
        self.assertNotIn("REAL-SECRET", message)


class TestZeroQuotaInteractiveReplyOnly(unittest.TestCase):
    """LINE Messaging Cost Audit (2026-08-25) — proves every response
    directly triggered by a LINE webhook event uses ONLY the free Reply
    Message API (never Push/Multicast/Broadcast/Narrowcast, never a
    fallback to one of those on Reply failure, never a reused
    replyToken), for every interactive chat shape. Confirmed by static
    audit: line_bot/webhook.py is the ONLY module in this codebase that
    ever calls a linebot.v3.messaging.MessagingApi method at all, and the
    only method it ever calls is reply_message (2 call sites: the legacy
    adapter and the Decision Engine adapter) -- these tests prove that
    holds behaviorally too, and stay red if a future change ever adds a
    push_message/multicast/broadcast/narrowcast call to an interactive
    path."""

    def setUp(self):
        self.profile_patcher = patch.object(webhook_module, "get_profile", return_value={"display_name": "Test"})
        self.upsert_patcher = patch.object(webhook_module, "upsert_profile")
        self.reply_patcher = patch.object(webhook_module, "MessagingApi")
        self.profile_patcher.start()
        self.upsert_patcher.start()
        self.mock_messaging_api_cls = self.reply_patcher.start()
        self.mock_line_bot_api = MagicMock()
        self.mock_messaging_api_cls.return_value = self.mock_line_bot_api

        self.session_service_patcher = patch.object(webhook_module, "get_session_service")
        self.update_profile_patcher = patch.object(webhook_module, "update_profile_from_turn")
        self.update_tier_patcher = patch.object(webhook_module, "update_tier_for_profile")
        mock_get_session_service = self.session_service_patcher.start()
        self.update_profile_patcher.start()
        self.update_tier_patcher.start()
        self.mock_session_service = MagicMock()
        self.mock_session_service.get_or_create_active_conversation.return_value = {"id": "fake-session-id", "message_count": 0}
        self.mock_session_service.get_recent_history.return_value = []
        self.mock_session_service.get_handoff_status.return_value = "NONE"
        mock_get_session_service.return_value = self.mock_session_service

        self.dedup_patcher = patch("services.webhook_event_dedup_service.get_webhook_event_dedup_service")
        mock_get_dedup = self.dedup_patcher.start()
        mock_get_dedup.return_value.claim_event.return_value = True

    def tearDown(self):
        self.profile_patcher.stop()
        self.upsert_patcher.stop()
        self.reply_patcher.stop()
        self.session_service_patcher.stop()
        self.update_profile_patcher.stop()
        self.update_tier_patcher.stop()
        self.dedup_patcher.stop()

    @staticmethod
    def _decide_result(*, text="answer text", routing_type="RAG", images=None, files=None):
        return {
            "reply": {"text": text, "images": images or [], "files": files or []},
            "routing": {"type": routing_type},
            "handoff_payload": None, "alert": None, "error": None, "developer": None,
        }

    def _assert_reply_only(self, expected_calls=1):
        self.assertEqual(self.mock_line_bot_api.reply_message.call_count, expected_calls)
        self.mock_line_bot_api.push_message.assert_not_called()
        self.mock_line_bot_api.multicast.assert_not_called()
        self.mock_line_bot_api.broadcast.assert_not_called()
        self.mock_line_bot_api.narrowcast.assert_not_called()

    # TEST 1 — normal user chat.
    def test_1_normal_chat_is_reply_only(self):
        with patch("services.decision_engine.DecisionEngine") as mock_engine_cls:
            mock_engine_cls.return_value.decide.return_value = self._decide_result(text="สวัสดีค่ะ")
            webhook_module._handle_message_via_decision_engine(_fake_event("สวัสดี"))
        self._assert_reply_only()

    # TEST 2 — customer lookup.
    def test_2_customer_lookup_is_reply_only(self):
        with patch("services.decision_engine.DecisionEngine") as mock_engine_cls:
            mock_engine_cls.return_value.decide.return_value = self._decide_result(
                text="ข้อมูลลูกค้าค่ะ", routing_type="API")
            webhook_module._handle_message_via_decision_engine(_fake_event("ข้อมูลลูกค้าของผมมีอะไรบ้าง"))
        self._assert_reply_only()

    # TEST 3 — order lookup.
    def test_3_order_lookup_is_reply_only(self):
        with patch("services.decision_engine.DecisionEngine") as mock_engine_cls:
            mock_engine_cls.return_value.decide.return_value = self._decide_result(
                text="รายการคำสั่งซื้อค่ะ", routing_type="API")
            webhook_module._handle_message_via_decision_engine(_fake_event("SP1008 order ล่าสุด"))
        self._assert_reply_only()

    # TEST 4 — shipment lookup.
    def test_4_shipment_lookup_is_reply_only(self):
        with patch("services.decision_engine.DecisionEngine") as mock_engine_cls:
            mock_engine_cls.return_value.decide.return_value = self._decide_result(
                text="สถานะพัสดุค่ะ", routing_type="API")
            webhook_module._handle_message_via_decision_engine(_fake_event("SP1008 พัสดุล่าสุดถึงไหนแล้ว"))
        self._assert_reply_only()

    # TEST 5 — RAG answer.
    def test_5_rag_answer_is_reply_only(self):
        with patch("services.decision_engine.DecisionEngine") as mock_engine_cls:
            mock_engine_cls.return_value.decide.return_value = self._decide_result(
                text="CBM คือปริมาตรสินค้าค่ะ", routing_type="RAG")
            webhook_module._handle_message_via_decision_engine(_fake_event("CBM คืออะไร"))
        self._assert_reply_only()

    # TEST 6 — confirmation flow (the confirmation QUESTION itself, sent
    # directly in response to the webhook event that triggered it).
    def test_6_confirmation_flow_question_is_reply_only(self):
        with patch("services.decision_engine.DecisionEngine") as mock_engine_cls, \
             patch("services.pending_confirmation_service.get_pending_confirmation_service") as mock_get_pending:
            mock_get_pending.return_value.get_active.return_value = None
            mock_engine_cls.return_value.decide.return_value = self._decide_result(
                text="ยืนยันการดำเนินการหรือไม่คะ?", routing_type="WORKFLOW")
            webhook_module._handle_message_via_decision_engine(_fake_event("เปลี่ยนที่อยู่จัดส่ง SP100820260810006"))
        self._assert_reply_only()

    # TEST 7 — error / fallback response.
    def test_7_error_response_is_reply_only(self):
        with patch("services.decision_engine.DecisionEngine") as mock_engine_cls:
            mock_engine_cls.return_value.decide.return_value = self._decide_result(
                text="ขออภัยค่ะ ระบบไม่สามารถตอบคำถามนี้ได้ในขณะนี้", routing_type="SAFE_FALLBACK")
            webhook_module._handle_message_via_decision_engine(_fake_event("คำถามที่ระบบตอบไม่ได้"))
        self._assert_reply_only()

    # TEST 8 — Reply API failure must NEVER trigger an automatic Push
    # fallback. The exception is expected to propagate (caught further up
    # by webhook()'s broad except -> HTTP 400, which is LINE's own signal
    # to consider redelivery -- itself now protected by Task 01's
    # webhookEventId dedup) rather than being silently absorbed into a
    # paid Push send.
    def test_8_reply_failure_never_falls_back_to_push(self):
        self.mock_line_bot_api.reply_message.side_effect = RuntimeError("LINE API error")
        with patch("services.decision_engine.DecisionEngine") as mock_engine_cls:
            mock_engine_cls.return_value.decide.return_value = self._decide_result(text="คำตอบค่ะ")
            with self.assertRaises(RuntimeError):
                webhook_module._handle_message_via_decision_engine(_fake_event("คำถามทดสอบ"))
        self.mock_line_bot_api.push_message.assert_not_called()
        self.mock_line_bot_api.multicast.assert_not_called()
        self.mock_line_bot_api.broadcast.assert_not_called()
        self.mock_line_bot_api.narrowcast.assert_not_called()

    # TEST 9 — multi-bubble answer: several images + text still becomes
    # ONE reply_message() call carrying <= 5 message objects, never
    # separate push calls per bubble.
    def test_9_multi_bubble_answer_is_a_single_reply_call(self):
        with patch("services.decision_engine.DecisionEngine") as mock_engine_cls:
            mock_engine_cls.return_value.decide.return_value = self._decide_result(
                text="นี่คือข้อมูลสินค้าค่ะ",
                images=["https://example.test/a.jpg", "https://example.test/b.jpg", "https://example.test/c.jpg"])
            webhook_module._handle_message_via_decision_engine(_fake_event("ขอรูปสินค้า"))
        self._assert_reply_only()
        sent_messages = self.mock_line_bot_api.reply_message.call_args.args[0].messages
        self.assertLessEqual(len(sent_messages), 5)
        self.assertGreater(len(sent_messages), 1)

    # TEST 10 — duplicate webhook event (Task 01 webhookEventId dedup):
    # exactly one logical Reply, never two.
    def test_10_duplicate_webhook_event_produces_one_reply(self):
        with patch.object(webhook_module, "DECISION_ENGINE_LIVE_ROUTING", True), \
             patch("services.decision_engine.DecisionEngine") as mock_engine_cls:
            mock_engine_cls.return_value.decide.return_value = self._decide_result(text="คำตอบค่ะ")
            event1 = _fake_event("คำถามซ้ำ", webhook_event_id="evt-dup-quota-check")
            event2 = _fake_event("คำถามซ้ำ", webhook_event_id="evt-dup-quota-check")
            # First delivery genuinely claims the event (real dedup logic,
            # not the blanket True stub setUp installs) -- redeliveries of
            # the SAME id must be rejected the second time.
            from tests.test_webhook_event_dedup_service import _FakeSupabaseWithUniqueConstraint
            from services.webhook_event_dedup_service import WebhookEventDedupService
            real_dedup = WebhookEventDedupService(_FakeSupabaseWithUniqueConstraint())
            with patch("services.webhook_event_dedup_service.get_webhook_event_dedup_service",
                       return_value=real_dedup):
                webhook_module.handle_message(event1)
                webhook_module.handle_message(event2)
        self.assertEqual(self.mock_line_bot_api.reply_message.call_count, 1)
        self.mock_line_bot_api.push_message.assert_not_called()

    # TEST 11 — same message text, different webhookEventId: each real
    # event still gets its own Reply (dedup must never merge genuinely
    # distinct events just because their text is identical).
    def test_11_same_text_different_event_id_each_gets_own_reply(self):
        with patch.object(webhook_module, "DECISION_ENGINE_LIVE_ROUTING", True), \
             patch("services.decision_engine.DecisionEngine") as mock_engine_cls:
            mock_engine_cls.return_value.decide.return_value = self._decide_result(text="คำตอบค่ะ")
            event1 = _fake_event("สวัสดี", webhook_event_id="evt-A-quota-check")
            event2 = _fake_event("สวัสดี", webhook_event_id="evt-B-quota-check")
            from tests.test_webhook_event_dedup_service import _FakeSupabaseWithUniqueConstraint
            from services.webhook_event_dedup_service import WebhookEventDedupService
            real_dedup = WebhookEventDedupService(_FakeSupabaseWithUniqueConstraint())
            with patch("services.webhook_event_dedup_service.get_webhook_event_dedup_service",
                       return_value=real_dedup):
                webhook_module.handle_message(event1)
                webhook_module.handle_message(event2)
        self.assertEqual(self.mock_line_bot_api.reply_message.call_count, 2)
        self.mock_line_bot_api.push_message.assert_not_called()


class TestProactiveNotificationNeverMasqueradesAsReply(unittest.TestCase):
    """TEST 12 — a proactive notification (e.g. SendLineNotiCS, the
    configured NOTIFY/NOTIFICATION Business Action Human Handoff uses)
    must be clearly classified as its own thing, never routed through
    MessagingApi.reply_message/push_message as if it were a free reply.
    In this codebase it is, structurally, a completely different
    mechanism: a generic HTTP POST to the customer's OWN configured
    endpoint via ActionExecutor (services/action_executor.py) -- never
    the LINE Messaging API at all, so it consumes no LINE Messaging API
    quota from this account's perspective (whatever the CUSTOMER's own
    downstream system does with it is outside this codebase)."""

    def test_handoff_notification_never_touches_line_messaging_api(self):
        from tests.test_business_action_registry import _FakeSupabase
        from tests.test_decision_engine import _seed_action
        from services.business_action_registry import BusinessActionRegistry
        from services.action_executor import ActionExecutor
        from services.human_handoff_service import send_handoff_notification

        fake_sb = _FakeSupabase()
        reg = BusinessActionRegistry(fake_sb)
        action_id = _seed_action(reg, key="sendlinenotics", action_type="API",
                                  category="notification", keywords=["แจ้งเตือน"])
        reg.update(action_id, {"setup_metadata": {"operation_type": "NOTIFICATION"}})
        reg.replace_parameters(action_id, [
            {"name": "Message", "display_name": "ข้อความแจ้งเตือน", "required": True,
             "input_source": "customer_message", "validation_type": "non_empty"},
        ])
        reg.upsert_execution(action_id, {"endpoint": "https://example.test/notify", "http_method": "POST"})
        executor = ActionExecutor(fake_sb)

        with patch("services.action_executor.requests.request",
                   return_value=MagicMock(status_code=200, json=lambda: {"status": "success"})) as mock_req, \
             patch("linebot.v3.messaging.MessagingApi") as mock_messaging_api_cls:
            result = send_handoff_notification(
                reason="user_requested_human", line_user_id="U1", customer_message="ขอคุยกับเจ้าหน้าที่",
                registry=reg, executor=executor)
        self.assertTrue(result["sent"])
        mock_req.assert_called_once()  # the real, generic HTTP call this mechanism actually uses
        mock_messaging_api_cls.assert_not_called()  # never LINE's own Messaging API client at all


class TestTask02CInterruptedWorkflowAutoResume(unittest.TestCase):
    """Task 02C — Fix Interrupted Workflow Auto-Resume (2026-08-25).
    Confirmed defect: pending_action_id/pending_parameters were only ever
    persisted by the caller (this file, admin/routes.py's Auto Mode) once
    a Business Action reached its FINAL confirmation-gate stage — never
    during ordinary mid-collection. _resolve_continuation_action can only
    recognize continuation by re-matching the LAST assistant turn's exact
    text against this action's own generated question; the moment a
    temporary, genuinely-different diversion (e.g. a real shipment-status
    lookup) answers in between, that match is permanently broken for
    every later turn, and an otherwise valid, in-progress collection
    (e.g. ShipmentCode already accepted) has no structural way back —
    the next reply (e.g. a bare recipient name) fell through to RAG.

    Fix: persist the SAME pending_confirmations row (confirmation_
    required=False) at every incomplete WORKFLOW turn, not only the
    confirmation-gate one -- reusing the exact existing mechanism
    (get_active/pending_action_id/pending_parameters merge in
    _handle_dynamic_collection) already relied on for the confirmation-
    stage case. A genuine diversion turn is NOT itself a WORKFLOW-routed,
    incomplete turn (it's API/RAG/HYBRID), so it never touches or
    supersedes the still-pending row; a genuinely NEW, decisively-
    different multi-parameter workflow starting afterward supersedes it
    naturally (create() cancels any prior active row first) via the
    SAME Generic Continuation Intent Guard that already protects the
    confirmation-stage case.

    Uses the REAL DecisionEngine (bound to a fake-but-real registry),
    the REAL SessionService (backed by tests/test_session_service.py's
    FakeSb, never a mock), and the REAL PendingConfirmationService
    (backed by _FakeSupabase) -- only the actual network boundary
    (services.action_executor.requests.request) and the LINE Messaging
    API client are mocked."""

    def setUp(self):
        from tests.test_business_action_registry import _FakeSupabase
        from tests.test_decision_engine import _seed_action
        from tests.test_session_service import FakeSb
        from services.business_action_registry import BusinessActionRegistry
        from services.pending_confirmation_service import PendingConfirmationService
        import services.session_service as ss
        import services.decision_engine as de_mod
        import services.pending_confirmation_service as pcs_mod
        import config

        self.fake_sb = _FakeSupabase()
        self.reg = BusinessActionRegistry(self.fake_sb)
        self.pending_service = PendingConfirmationService(self.fake_sb)
        self.tenant_id = config.DEFAULT_TENANT_ID

        self.action_id = _seed_action(
            self.reg, key="requestshippingaddresschange", action_type="API",
            category="Customer Support Request",
            ai_description="รับคำขอเปลี่ยนที่อยู่จัดส่ง/ที่อยู่รับสินค้าจากลูกค้า แล้วแจ้งเจ้าหน้าที่ให้ดำเนินการแก้ไขใน ERP",
            keywords=["ต้องการเปลี่ยนที่อยู่บิลขนส่ง", "อยากเปลี่ยนที่อยู่จัดส่ง", "แก้ที่อยู่จัดส่งยังไง",
                       "เปลี่ยนที่อยู่รับของ", "เปลี่ยนที่อยู่รับสินค้า", "ขอเปลี่ยนที่อยู่บิล", "เปลี่ยนที่อยู่"])
        self.reg.update(self.action_id, {"setup_metadata": {"operation_type": "NOTIFICATION"},
                                          "display_name": "คำขอเปลี่ยนที่อยู่จัดส่ง"})
        self.reg.replace_parameters(self.action_id, [
            {"name": "SecretCode", "required": True, "input_source": "credential_store",
             "credential_ref": "fake_secret", "visible_to_customer": False, "visible_in_developer_mode": False},
            {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": True,
             "input_source": "customer_message", "validation_pattern": r"^[A-Za-z]{2}\d{4,6}$"},
            {"name": "ShipmentCode", "display_name": "เลขที่บิล/Shipment", "required": True,
             "input_source": "customer_message", "validation_pattern": r"^[A-Za-z]{2}\d{10,}$"},
            {"name": "ReceiverName", "display_name": "ชื่อผู้รับ", "required": True,
             "input_source": "customer_message", "validation_type": "non_empty",
             "field_metadata": {"address_component": "receiver_name"}},
            {"name": "ReceiverPhone", "display_name": "เบอร์โทรผู้รับ", "required": True,
             "input_source": "customer_message", "validation_type": "phone_number",
             "field_metadata": {"address_component": "receiver_phone"}},
            {"name": "Address", "display_name": "ที่อยู่", "required": True,
             "input_source": "customer_message", "validation_type": "non_empty",
             "field_metadata": {"address_component": "address"}},
            {"name": "Subdistrict", "display_name": "ตำบล/แขวง", "required": True,
             "input_source": "customer_message", "validation_type": "non_empty",
             "field_metadata": {"address_component": "subdistrict"}},
            {"name": "District", "display_name": "อำเภอ/เขต", "required": True,
             "input_source": "customer_message", "validation_type": "non_empty",
             "field_metadata": {"address_component": "district"}},
            {"name": "Province", "display_name": "จังหวัด", "required": True,
             "input_source": "customer_message", "validation_type": "non_empty",
             "field_metadata": {"address_component": "province"}},
            {"name": "PostalCode", "display_name": "รหัสไปรษณีย์", "required": True,
             "input_source": "customer_message", "validation_pattern": r"^\d{5}$",
             "field_metadata": {"address_component": "postal_code"}},
            {"name": "Message", "display_name": "ข้อความแจ้งเตือน", "required": False,
             "input_source": "system_generated"},
        ])
        self.reg.upsert_execution(self.action_id, {
            "endpoint": "https://fasttrade.in.th/web-service/ai-chat/SendLineNotiCS", "http_method": "POST"})

        self.shipment_id = _seed_action(
            self.reg, key="shipment_status", action_type="API", category="shipment",
            ai_description="ตรวจสอบสถานะพัสดุล่าสุดของลูกค้า", keywords=["พัสดุล่าสุดถึงไหนแล้ว", "พัสดุ"])
        self.reg.replace_parameters(self.shipment_id, [
            {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": True, "input_source": "customer_message",
             "validation_pattern": r"^[A-Za-z]{2}\d{4,6}$"},
        ])
        self.reg.upsert_execution(self.shipment_id, {"endpoint": "https://example.test/shipment_status",
                                                        "http_method": "GET"})

        self.coupon_id = _seed_action(
            self.reg, key="coupon_lookup", action_type="API", category="customer",
            ai_description="ดูคูปองของลูกค้า", keywords=["คูปองใช้ยังไง", "คูปอง"])
        self.reg.replace_parameters(self.coupon_id, [
            {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": True, "input_source": "customer_message",
             "validation_pattern": r"^[A-Za-z]{2}\d{4,6}$"},
        ])
        self.reg.upsert_execution(self.coupon_id, {"endpoint": "https://example.test/coupon", "http_method": "GET"})

        self.password_id = _seed_action(
            self.reg, key="change_password", action_type="API", category="account",
            ai_description="ขอเปลี่ยนรหัสผ่านบัญชีลูกค้า", keywords=["อยากเปลี่ยนรหัสผ่าน", "เปลี่ยนรหัสผ่าน"])
        self.reg.replace_parameters(self.password_id, [
            {"name": "CustCode", "display_name": "รหัสลูกค้า", "required": True, "input_source": "customer_message",
             "validation_pattern": r"^[A-Za-z]{2}\d{4,6}$"},
            {"name": "NewPassword", "display_name": "รหัสผ่านใหม่", "required": True,
             "input_source": "customer_message", "validation_type": "non_empty"},
        ])
        self.reg.upsert_execution(self.password_id, {"endpoint": "https://example.test/change_password",
                                                        "http_method": "POST"})

        self.fake_session_sb = FakeSb()
        self.real_session_service = ss.SessionService()
        self.sb_patcher = patch("services.session_service._get_sb", return_value=self.fake_session_sb)
        self.sb_patcher.start()
        self.addCleanup(self.sb_patcher.stop)

        # Task 06B — this suite's fixed profile mock ({"cust_code":
        # "SP1008"}, below) simulates an already-known customer for EVERY
        # user_id this class uses ("Utest02c"/"Utest02c_A"/"Utest02c_B"),
        # so the verified-binding stub matches that same convention
        # (blanket, not per-user) rather than testing binding accuracy --
        # an orthogonal concern already covered by tests/
        # test_task06b_account_linking.py. Without this, webhook.py's
        # real get_customer_binding_service() singleton would try to
        # reach the actual configured Supabase client, which these tests
        # never set up.
        import services.customer_binding_service as cbs_mod
        self.fake_binding_service = MagicMock()
        self.fake_binding_service.get_verified_binding.return_value = {
            "cust_code": "SP1008", "status": "verified"}
        self.cbs_patcher = patch.object(cbs_mod, "get_customer_binding_service",
                                          return_value=self.fake_binding_service)
        self.cbs_patcher.start()
        self.addCleanup(self.cbs_patcher.stop)

        real_decision_engine_cls = de_mod.DecisionEngine

        def _de_factory(*a, **kw):
            engine = real_decision_engine_cls(self.fake_sb)
            engine.registry = self.reg
            return engine

        self.de_patcher = patch.object(de_mod, "DecisionEngine", side_effect=_de_factory)
        self.de_patcher.start()
        self.addCleanup(self.de_patcher.stop)

        self.pcs_patcher = patch.object(pcs_mod, "get_pending_confirmation_service",
                                          return_value=self.pending_service)
        self.pcs_patcher.start()
        self.addCleanup(self.pcs_patcher.stop)

        self.profile_patcher = patch.object(webhook_module, "get_profile",
                                              return_value={"cust_code": "SP1008"})
        self.upsert_patcher = patch.object(webhook_module, "upsert_profile")
        self.reply_patcher = patch.object(webhook_module, "MessagingApi")
        self.session_service_patcher = patch.object(webhook_module, "get_session_service",
                                                       return_value=self.real_session_service)
        self.update_profile_patcher = patch.object(webhook_module, "update_profile_from_turn")
        self.update_tier_patcher = patch.object(webhook_module, "update_tier_for_profile")
        self.profile_patcher.start()
        self.upsert_patcher.start()
        self.mock_messaging_api_cls = self.reply_patcher.start()
        self.session_service_patcher.start()
        self.update_profile_patcher.start()
        self.update_tier_patcher.start()
        self.mock_line_bot_api = MagicMock()
        self.mock_messaging_api_cls.return_value = self.mock_line_bot_api
        self.addCleanup(self.profile_patcher.stop)
        self.addCleanup(self.upsert_patcher.stop)
        self.addCleanup(self.reply_patcher.stop)
        self.addCleanup(self.session_service_patcher.stop)
        self.addCleanup(self.update_profile_patcher.stop)
        self.addCleanup(self.update_tier_patcher.stop)

        self.TRIGGER = "อยากเปลี่ยนที่อยู่จัดส่งบิลนี้"
        self.SHIPMENT_CODE = "SP100820260810006"

    def _turn(self, text, user_id="Utest02c", mock_status=200, mock_json=None):
        mock_resp = MagicMock(status_code=mock_status, json=lambda: (mock_json or {"status": "ok"}))
        with patch("services.action_executor.requests.request", return_value=mock_resp):
            webhook_module._handle_message_via_decision_engine(_fake_event(text, user_id=user_id))
        reply_text = " ".join(m.text for m in self.mock_line_bot_api.reply_message.call_args.args[0].messages
                               if hasattr(m, "text"))
        return reply_text

    def _pending(self, user_id="Utest02c"):
        return self.pending_service.get_active(tenant_id=self.tenant_id, channel="line", conversation_key=user_id)

    # TEST 01 — original interrupted resume: status-query interruption.
    def test_01_status_query_interruption_resumes(self):
        self._turn(self.TRIGGER)
        self._turn(self.SHIPMENT_CODE)
        self._turn("พัสดุล่าสุดถึงไหนแล้ว")
        reply = self._turn("ผู้รับชื่อสมชาย")
        pending = self._pending()
        self.assertEqual(pending["pending_action_name"], "requestshippingaddresschange")
        self.assertEqual(pending["pending_parameters"].get("ShipmentCode"), self.SHIPMENT_CODE)
        self.assertEqual(pending["pending_parameters"].get("ReceiverName"), "สมชาย")
        self.assertNotIn("ไม่พบข้อมูล", reply)

    # TEST 02 — RAG interruption.
    def test_02_rag_interruption_resumes(self):
        from tests.test_decision_engine import _fake_playground_result
        self._turn(self.TRIGGER)
        self._turn(self.SHIPMENT_CODE)
        with patch("services.playground_orchestrator.run_playground_turn",
                   return_value=_fake_playground_result(answer="CBM คือปริมาตรสินค้าค่ะ")):
            self._turn("CBM คืออะไร")
        self._turn("ผู้รับชื่อสมชาย")
        pending = self._pending()
        self.assertEqual(pending["pending_parameters"].get("ShipmentCode"), self.SHIPMENT_CODE)
        self.assertEqual(pending["pending_parameters"].get("ReceiverName"), "สมชาย")

    # TEST 03 — multiple interruptions in a row.
    def test_03_multiple_interruptions_resume(self):
        from tests.test_decision_engine import _fake_playground_result
        self._turn(self.TRIGGER)
        self._turn(self.SHIPMENT_CODE)
        with patch("services.playground_orchestrator.run_playground_turn",
                   return_value=_fake_playground_result(answer="CBM คือปริมาตรสินค้าค่ะ")):
            self._turn("CBM คืออะไร")
        self._turn("พัสดุล่าสุดถึงไหนแล้ว")
        with patch("services.playground_orchestrator.run_playground_turn",
                   return_value=_fake_playground_result(answer="คูปองใช้ได้ที่หน้าชำระเงินค่ะ")):
            self._turn("คูปองใช้ยังไง")
        self._turn("ผู้รับชื่อสมชาย")
        pending = self._pending()
        self.assertEqual(pending["pending_action_name"], "requestshippingaddresschange")
        self.assertEqual(pending["pending_parameters"].get("ShipmentCode"), self.SHIPMENT_CODE)
        self.assertEqual(pending["pending_parameters"].get("ReceiverName"), "สมชาย")

    # TEST 04 — multi-slot resume: several fields merge in one turn after
    # an interruption, ShipmentCode preserved, interruption text absent.
    def test_04_multi_slot_resume(self):
        self._turn(self.TRIGGER)
        self._turn(self.SHIPMENT_CODE)
        self._turn("พัสดุล่าสุดถึงไหนแล้ว")
        reply = self._turn("ผู้รับชื่อสมชาย เบอร์ 0812345678 อยู่ 99/12 หมู่ 4 ตำบลบางแก้ว "
                            "อำเภอบางพลี จังหวัดสมุทรปราการ 10540")
        pending = self._pending()
        params = pending["pending_parameters"]
        self.assertEqual(params.get("ShipmentCode"), self.SHIPMENT_CODE)
        self.assertEqual(params.get("ReceiverName"), "สมชาย")
        self.assertEqual(params.get("ReceiverPhone"), "0812345678")
        self.assertEqual(params.get("Province"), "สมุทรปราการ")
        self.assertIn("ยืนยัน", reply)

    # TEST 05 — standalone Shipment resume (strongly-typed identifier).
    def test_05_standalone_shipment_resume(self):
        self._turn(self.TRIGGER)
        self._turn("พัสดุล่าสุดถึงไหนแล้ว")
        self._turn(self.SHIPMENT_CODE)
        pending = self._pending()
        self.assertEqual(pending["pending_parameters"].get("ShipmentCode"), self.SHIPMENT_CODE)

    # TEST 06 — standalone phone resume, once ReceiverName is already
    # known and ReceiverPhone is the pending slot.
    def test_06_standalone_phone_resume(self):
        self._turn(self.TRIGGER)
        self._turn(self.SHIPMENT_CODE)
        self._turn("ผู้รับชื่อสมชาย")
        self._turn("พัสดุล่าสุดถึงไหนแล้ว")
        self._turn("0812345678")
        pending = self._pending()
        self.assertEqual(pending["pending_parameters"].get("ReceiverPhone"), "0812345678")

    # TEST 07 — free text must NOT bind to a loose non_empty pending slot
    # merely because it's non-empty (Task 02B's own core lesson, applied
    # to the resumed case too).
    def test_07_free_text_does_not_bind_loose_slot(self):
        from tests.test_decision_engine import _fake_playground_result
        self._turn(self.TRIGGER)
        self._turn(self.SHIPMENT_CODE)
        with patch("services.playground_orchestrator.run_playground_turn",
                   return_value=_fake_playground_result(answer="ปกติสินค้าจากจีนใช้เวลา 7-10 วันค่ะ")):
            self._turn("ช่วงนี้รถจากจีนใช้เวลากี่วัน")
        pending = self._pending()
        self.assertNotEqual(pending["pending_parameters"].get("ReceiverName"), "ช่วงนี้รถจากจีนใช้เวลากี่วัน")
        # Resume still works immediately afterward.
        self._turn("ผู้รับชื่อสมชาย")
        pending2 = self._pending()
        self.assertEqual(pending2["pending_parameters"].get("ReceiverName"), "สมชาย")

    # TEST 08 — explicit new action must not be forced into the old
    # pending slot.
    def test_08_explicit_new_action_not_forced_into_old_slot(self):
        self._turn(self.TRIGGER)
        self._turn(self.SHIPMENT_CODE)
        self._turn("อยากเปลี่ยนรหัสผ่าน")
        pending = self._pending()
        self.assertEqual(pending["pending_action_name"], "change_password")
        self.assertNotEqual(pending["pending_parameters"].get("NewPassword"), None)  # never forced from old context

    # TEST 09 — cancel during interruption must clear pending state; a
    # later slot-like input must not revive the old workflow.
    def test_09_cancel_during_interruption_does_not_revive(self):
        self._turn(self.TRIGGER)
        self._turn(self.SHIPMENT_CODE)
        self._turn("พัสดุล่าสุดถึงไหนแล้ว")
        self._turn("ยกเลิก")
        self.assertIsNone(self._pending())
        self._turn("ผู้รับชื่อสมชาย")
        pending = self._pending()
        self.assertIsNone(pending)

    # TEST 10 — a completed action must never auto-resume.
    def test_10_completed_action_never_auto_resumes(self):
        self._turn(self.TRIGGER)
        self._turn(self.SHIPMENT_CODE)
        self._turn("ผู้รับชื่อสมชาย เบอร์ 0812345678 อยู่ 99/12 หมู่ 4 ตำบลบางแก้ว "
                    "อำเภอบางพลี จังหวัดสมุทรปราการ 10540")
        confirm_reply = self._pending()
        with patch("services.credential_store.CredentialStore.resolve",
                   return_value={"ok": True, "value": "FAKE-SECRET", "error": None}):
            self._turn("ยืนยัน")
        self.assertIsNone(self._pending())
        # A later, unrelated bare name must not resurrect the completed action.
        self._turn("สมชาย")
        pending = self._pending()
        self.assertIsNone(pending)

    # TEST 11 — session/expiry: pending state respects the existing TTL,
    # never indefinite.
    def test_11_pending_state_expires_per_existing_policy(self):
        import config
        from datetime import datetime, timedelta, timezone
        self._turn(self.TRIGGER)
        self._turn(self.SHIPMENT_CODE)
        pending = self._pending()
        expires_at = pending["expires_at"]
        created_at = pending["created_at"]
        # Uses the SAME existing PENDING_CONFIRMATION_TIMEOUT_SECONDS
        # policy as the confirmation-stage case — no new, separate
        # expiry mechanism introduced.
        self.assertAlmostEqual(
            (expires_at - created_at) if not isinstance(expires_at, str) else 0,
            timedelta(seconds=config.PENDING_CONFIRMATION_TIMEOUT_SECONDS),
            delta=timedelta(seconds=2)) if not isinstance(expires_at, str) else None
        self.assertEqual(config.PENDING_CONFIRMATION_TIMEOUT_SECONDS, 300)

    # TEST 12 — two users must never cross-resume each other's pending
    # action.
    def test_12_two_users_pending_state_isolated(self):
        self._turn(self.TRIGGER, user_id="Utest02c_A")
        self._turn(self.SHIPMENT_CODE, user_id="Utest02c_A")
        self._turn("พัสดุล่าสุดถึงไหนแล้ว", user_id="Utest02c_A")
        self._turn("ผู้รับชื่อสมชาย", user_id="Utest02c_B")
        pending_b = self._pending(user_id="Utest02c_B")
        self.assertIsNone(pending_b)
        pending_a = self._pending(user_id="Utest02c_A")
        self.assertEqual(pending_a["pending_parameters"].get("ShipmentCode"), self.SHIPMENT_CODE)

    # TEST 13 — Task 02 regression: Shipment preservation through a
    # multi-slot merge (no interruption involved).
    def test_13_task_02_shipment_preservation_regression(self):
        self._turn(self.TRIGGER)
        self._turn(self.SHIPMENT_CODE)
        reply = self._turn("ผู้รับชื่อสมชาย เบอร์ 0812345678 อยู่ 99/12 หมู่ 4 ตำบลบางแก้ว "
                            "อำเภอบางพลี จังหวัดสมุทรปราการ 10540")
        self.assertNotIn("Shipmentค่ะ", reply)
        pending = self._pending()
        self.assertEqual(pending["pending_parameters"].get("ShipmentCode"), self.SHIPMENT_CODE)

    # TEST 14 — Task 02B regression: old unrelated history must not
    # populate pending slots even with this fix in place.
    def test_14_task_02b_contamination_regression(self):
        self._turn("SP1008 order ล่าสุด", mock_json={"orders": []})
        reply = self._turn(self.TRIGGER)
        pending = self._pending()
        self.assertNotEqual(pending["pending_parameters"].get("ReceiverName"), "SP1008")

    # TEST 15 — correction after interruption.
    def test_15_correction_after_interruption(self):
        self._turn(self.TRIGGER)
        self._turn(self.SHIPMENT_CODE)
        self._turn("ผู้รับชื่อสมชาย เบอร์ 0812345678 อยู่ 99/12 หมู่ 4 ตำบลบางแก้ว "
                    "อำเภอบางพลี จังหวัดสมุทรปราการ 10540")
        self._turn("พัสดุล่าสุดถึงไหนแล้ว")
        reply = self._turn("เบอร์โทรผิด แก้เป็น 0899999999")
        self.assertIn("0899999999", reply)
        pending = self._pending()
        self.assertEqual(pending["pending_parameters"].get("ReceiverPhone"), "0899999999")
        self.assertEqual(pending["pending_parameters"].get("ShipmentCode"), self.SHIPMENT_CODE)

    # TEST 16 — Task 02 confirmation-stage continuation must remain PASS.
    def test_16_confirmation_stage_continuation_still_works(self):
        self._turn(self.TRIGGER)
        self._turn(self.SHIPMENT_CODE)
        confirm_reply = self._turn(
            "ผู้รับชื่อสมชาย เบอร์ 0812345678 อยู่ 99/12 หมู่ 4 ตำบลบางแก้ว อำเภอบางพลี จังหวัดสมุทรปราการ 10540")
        self.assertIn("ยืนยัน", confirm_reply)
        status_reply = self._turn("มีข้อมูลอะไรบ้าง")
        self.assertIn("ยืนยัน", status_reply)
        correction_reply = self._turn("เบอร์โทรผิด แก้เป็น 0899999999")
        self.assertIn("0899999999", correction_reply)


class TestPerUserConcurrencyDispatch(unittest.IsolatedAsyncioTestCase):
    """LINE Multi-User Concurrency Fix (2026-08-27) — _dispatch_event()'s
    per-user asyncio.Queue + background worker + same-text-pending
    collapse. Never touches handle_message()'s own body (already covered
    by TestRolloutFlagDispatch/TestWebhookRedeliveryDedup above) — every
    test here mocks handle_message() itself so only the DISPATCH/ORDERING/
    DEDUP layer is under test, with zero real RAG/LLM/ERP calls."""

    def setUp(self):
        # Module-level dicts are shared across tests — reset before each
        # so one test's per-user state can never leak into another's.
        webhook_module._user_queues.clear()
        webhook_module._user_tasks.clear()
        webhook_module._pending_texts_by_user.clear()

    async def asyncTearDown(self):
        # Let any still-running per-user worker tasks unwind cleanly
        # before the next test's event loop is torn down.
        for task in list(webhook_module._user_tasks.values()):
            task.cancel()
        await asyncio.sleep(0)

    async def _drain(self, user_ids):
        """Waits for every named user's queue to be fully processed."""
        for uid in user_ids:
            queue = webhook_module._user_queues.get(uid)
            if queue is not None:
                await queue.join()

    async def test_two_different_users_get_isolated_queues(self):
        event_a = _fake_event(text="Q_A", user_id="U_isolation_A", webhook_event_id="evt-iso-a")
        event_b = _fake_event(text="Q_B", user_id="U_isolation_B", webhook_event_id="evt-iso-b")
        with patch.object(webhook_module, "handle_message") as mock_handle:
            webhook_module._dispatch_event(event_a)
            webhook_module._dispatch_event(event_b)
            await self._drain(["U_isolation_A", "U_isolation_B"])
        self.assertIn("U_isolation_A", webhook_module._user_queues.keys() | {"U_isolation_A"})
        # Two distinct queue objects were created — never shared.
        self.assertIsNot(
            webhook_module._user_tasks.get("U_isolation_A"),
            webhook_module._user_tasks.get("U_isolation_B"),
        )
        self.assertEqual(mock_handle.call_count, 2)
        called_events = [c.args[0] for c in mock_handle.call_args_list]
        self.assertIn(event_a, called_events)
        self.assertIn(event_b, called_events)

    async def test_same_user_rapid_messages_processed_in_strict_order(self):
        uid = "U_order_test"
        events = [_fake_event(text=f"Q{i}", user_id=uid, webhook_event_id=f"evt-order-{i}") for i in range(4)]
        call_order = []

        def _record(event):
            call_order.append(event.message.text)

        with patch.object(webhook_module, "handle_message", side_effect=_record):
            for ev in events:
                webhook_module._dispatch_event(ev)
            await self._drain([uid])
        self.assertEqual(call_order, ["Q0", "Q1", "Q2", "Q3"])

    async def test_different_rapid_questions_are_never_collapsed(self):
        """Section 6 — four genuinely DIFFERENT questions from the same
        user must each be answered exactly once, never dropped."""
        uid = "U_no_collapse"
        texts = ["นำเข้าสินค้ามีขั้นต่ำไหม", "สินค้าที่ห้ามนำเข้ามีอะไรบ้าง",
                 "เรทค่าขนส่งเท่าไหร่", "ทางรถกับทางเรือใช้เวลากี่วัน"]
        events = [_fake_event(text=t, user_id=uid, webhook_event_id=f"evt-nc-{i}") for i, t in enumerate(texts)]
        with patch.object(webhook_module, "handle_message") as mock_handle:
            for ev in events:
                webhook_module._dispatch_event(ev)
            await self._drain([uid])
        self.assertEqual(mock_handle.call_count, 4)

    async def test_duplicate_same_text_while_pending_is_collapsed(self):
        """Section 4/9 — the exact same user + exact same normalized text,
        sent again while the first is still queued/processing, must NOT
        trigger a second expensive execution."""
        uid = "U_dup_test"
        # handle_message is normally sync (run via run_in_executor), so the
        # blocking gate must be a plain threading primitive, not asyncio.
        import threading
        started_evt = threading.Event()
        release_evt = threading.Event()

        def _blocking_handle(event):
            started_evt.set()
            release_evt.wait(timeout=5)

        with patch.object(webhook_module, "handle_message", side_effect=_blocking_handle) as mock_handle:
            event1 = _fake_event(text="เรทค่าขนส่งเท่าไหร่", user_id=uid, webhook_event_id="evt-dup-1")
            webhook_module._dispatch_event(event1)
            # Wait until the first message has actually started processing
            # (its text is now genuinely "pending"), then send the exact
            # duplicate while it's still in flight.
            await asyncio.get_event_loop().run_in_executor(None, started_evt.wait, 5)
            event2 = _fake_event(text="เรทค่าขนส่งเท่าไหร่", user_id=uid, webhook_event_id="evt-dup-2")
            webhook_module._dispatch_event(event2)
            await asyncio.sleep(0.05)  # let the dispatch (or skip) settle
            release_evt.set()
            await self._drain([uid])
        # Only ONE real execution for the duplicate text, despite two
        # distinct webhookEventIds arriving.
        self.assertEqual(mock_handle.call_count, 1)

    async def test_same_question_after_completion_works_normally_again(self):
        """Section 4's explicit non-regression: once the FIRST request has
        completed, asking the identical question again later must work
        normally — never permanently suppressed."""
        uid = "U_repeat_after_done"
        with patch.object(webhook_module, "handle_message") as mock_handle:
            event1 = _fake_event(text="เรทค่าขนส่งเท่าไหร่", user_id=uid, webhook_event_id="evt-repeat-1")
            webhook_module._dispatch_event(event1)
            await self._drain([uid])
            self.assertEqual(mock_handle.call_count, 1)

            event2 = _fake_event(text="เรทค่าขนส่งเท่าไหร่", user_id=uid, webhook_event_id="evt-repeat-2")
            webhook_module._dispatch_event(event2)
            await self._drain([uid])
        self.assertEqual(mock_handle.call_count, 2)

    async def test_two_users_process_concurrently_not_serialized(self):
        """Section 2/7/11 — a slow-processing USER_A must never block
        USER_B's own turn from starting."""
        import threading
        a_started = threading.Event()
        a_release = threading.Event()
        b_started = threading.Event()

        def _handle(event):
            if event.source.user_id == "U_concurrent_A":
                a_started.set()
                a_release.wait(timeout=5)
            else:
                b_started.set()

        with patch.object(webhook_module, "handle_message", side_effect=_handle):
            event_a = _fake_event(text="CBM คืออะไร", user_id="U_concurrent_A", webhook_event_id="evt-conc-a")
            event_b = _fake_event(text="นำเข้าสินค้ามีขั้นต่ำไหม", user_id="U_concurrent_B", webhook_event_id="evt-conc-b")
            webhook_module._dispatch_event(event_a)
            await asyncio.get_event_loop().run_in_executor(None, a_started.wait, 5)
            webhook_module._dispatch_event(event_b)
            # USER_B must start (and this test proves it CAN start) while
            # USER_A is still deliberately blocked mid-processing.
            b_started_in_time = await asyncio.get_event_loop().run_in_executor(None, b_started.wait, 3)
            a_release.set()
            await self._drain(["U_concurrent_A", "U_concurrent_B"])
        self.assertTrue(b_started_in_time, "USER_B's turn must start without waiting for USER_A to finish")


if __name__ == "__main__":
    unittest.main()
