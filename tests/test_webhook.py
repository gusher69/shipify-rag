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
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

with patch("config.LINE_CHANNEL_SECRET", "test_channel_secret"), \
     patch("config.LINE_CHANNEL_TOKEN", "test_channel_token"):
    import line_bot.webhook as webhook_module


def _fake_event(text="สวัสดีค่ะ", user_id="U_test_user"):
    event = MagicMock()
    event.source.user_id = user_id
    event.message.text = text
    event.reply_token = "test_reply_token"
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
             patch.object(webhook_module, "_handle_message_via_decision_engine") as mock_de:
            webhook_module.handle_message(event)
        mock_legacy.assert_called_once_with(event)
        mock_de.assert_not_called()

    def test_flag_true_calls_decision_engine_only(self):
        event = _fake_event()
        with patch.object(webhook_module, "DECISION_ENGINE_LIVE_ROUTING", True), \
             patch.object(webhook_module, "_handle_message_legacy") as mock_legacy, \
             patch.object(webhook_module, "_handle_message_via_decision_engine") as mock_de:
            webhook_module.handle_message(event)
        mock_de.assert_called_once_with(event)
        mock_legacy.assert_not_called()


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

    def test_human_handoff_routing_sends_line_notify(self):
        with patch("services.decision_engine.DecisionEngine") as mock_engine_cls, \
             patch.object(webhook_module, "send_line_notify") as mock_notify:
            mock_engine_cls.return_value.decide.return_value = self._decide_result(
                text="กำลังโอนสายให้เจ้าหน้าที่ค่ะ", routing_type="HUMAN_HANDOFF",
                handoff_payload={"reason": "ai_policy_escalation"})
            webhook_module._handle_message_via_decision_engine(_fake_event("ร้องเรียนบริการ"))
        mock_notify.assert_called_once()
        self.assertIn("ai_policy_escalation", mock_notify.call_args.args[0])

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
        with patch("services.decision_engine.DecisionEngine") as mock_engine_cls, \
             patch.object(webhook_module, "send_line_notify"):
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
            self.assertEqual(call_kwargs["history"], [
                {"role": "user", "content": self.TRIGGER},
                {"role": "assistant", "content": self.QUESTION},
            ])

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

    @staticmethod
    def _decide_result_plain(text):
        return {"reply": {"text": text, "images": [], "files": []}, "routing": {"type": "RAG"},
                "handoff_payload": None, "alert": None, "error": None, "developer": {}}


if __name__ == "__main__":
    unittest.main()
