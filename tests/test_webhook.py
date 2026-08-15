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
        self.mock_session_service.get_recent_history.return_value = []
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
        self._handoff_status: dict = {}

        def _get_or_create(user_id, **kwargs):
            if user_id not in self._conversations:
                self._conversations[user_id] = {"id": f"conv-{user_id}", "message_count": 0}
                self._handoff_status[f"conv-{user_id}"] = "NONE"
            return self._conversations[user_id]

        def _get_status(conversation_id):
            return self._handoff_status.get(conversation_id, "NONE")

        def _set_status(conversation_id, status, reason=None):
            self._handoff_status[conversation_id] = status

        self.mock_session_service = MagicMock()
        self.mock_session_service.get_or_create_active_conversation.side_effect = _get_or_create
        self.mock_session_service.get_handoff_status.side_effect = _get_status
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


if __name__ == "__main__":
    unittest.main()
