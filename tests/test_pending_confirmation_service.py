"""Tests for services/pending_confirmation_service.py (LINE Confirmation
Flow sprint, 2026-08-10) — pure service-level tests against the same
_FakeSupabase convention used throughout this test suite. Never a real
DB, never a real network call.
"""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_business_action_registry import _FakeSupabase
from services.pending_confirmation_service import (
    PendingConfirmationService, classify_confirmation_reply,
)

ACTION = {
    "id": "action-1", "action_key": "notify_cs",
    "parameters": [
        {"name": "SecretCode", "input_source": "credential_store"},
        {"name": "Message", "input_source": "customer_message"},
    ],
}


class TestSecretsNeverPersisted(unittest.TestCase):
    def test_create_strips_credential_store_parameter(self):
        svc = PendingConfirmationService(_FakeSupabase())
        row = svc.create(tenant_id="t1", channel="line", conversation_key="U1", action=ACTION,
                          parameters={"Message": "hello", "SecretCode": "REAL-SECRET"},
                          original_message="hello", question_text="confirm?")
        self.assertEqual(row["pending_parameters"], {"Message": "hello"})
        self.assertNotIn("SecretCode", row["pending_parameters"])


class TestActiveLookupAndExpiry(unittest.TestCase):
    def setUp(self):
        self.sb = _FakeSupabase()
        self.svc = PendingConfirmationService(self.sb)

    def test_no_pending_returns_none(self):
        self.assertIsNone(self.svc.get_active(tenant_id="t1", channel="line", conversation_key="U1"))

    def test_active_pending_is_returned(self):
        self.svc.create(tenant_id="t1", channel="line", conversation_key="U1", action=ACTION,
                         parameters={"Message": "hi"}, original_message="hi", question_text="confirm?")
        row = self.svc.get_active(tenant_id="t1", channel="line", conversation_key="U1")
        self.assertIsNotNone(row)
        self.assertEqual(row["pending_action_name"], "notify_cs")
        self.assertEqual(row["status"], "pending")

    def test_expired_pending_is_marked_expired_and_not_returned(self):
        row = self.svc.create(tenant_id="t1", channel="line", conversation_key="U1", action=ACTION,
                               parameters={"Message": "hi"}, original_message="hi", question_text="confirm?",
                               ttl_seconds=300)
        # Simulate time passing by rewriting expires_at directly (no
        # persistence layer's own clock to fast-forward otherwise).
        past = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
        self.sb.table("pending_confirmations").update({"expires_at": past}).eq("id", row["id"]).execute()

        self.assertIsNone(self.svc.get_active(tenant_id="t1", channel="line", conversation_key="U1"))
        most_recent = self.svc.get_most_recent(tenant_id="t1", channel="line", conversation_key="U1")
        self.assertEqual(most_recent["status"], "expired")

    def test_new_pending_supersedes_old_one(self):
        first = self.svc.create(tenant_id="t1", channel="line", conversation_key="U1", action=ACTION,
                                 parameters={"Message": "first"}, original_message="first", question_text="confirm?")
        second = self.svc.create(tenant_id="t1", channel="line", conversation_key="U1", action=ACTION,
                                  parameters={"Message": "second"}, original_message="second", question_text="confirm?")
        active = self.svc.get_active(tenant_id="t1", channel="line", conversation_key="U1")
        self.assertEqual(active["id"], second["id"])
        first_row = [r for r in self.sb.store["pending_confirmations"] if r["id"] == first["id"]][0]
        self.assertEqual(first_row["status"], "cancelled")


class TestTenantChannelConversationIsolation(unittest.TestCase):
    def setUp(self):
        self.svc = PendingConfirmationService(_FakeSupabase())

    def test_different_conversation_key_never_sees_pending(self):
        self.svc.create(tenant_id="t1", channel="line", conversation_key="U1", action=ACTION,
                         parameters={"Message": "hi"}, original_message="hi", question_text="confirm?")
        self.assertIsNone(self.svc.get_active(tenant_id="t1", channel="line", conversation_key="U2"))

    def test_different_tenant_never_sees_pending(self):
        self.svc.create(tenant_id="t1", channel="line", conversation_key="U1", action=ACTION,
                         parameters={"Message": "hi"}, original_message="hi", question_text="confirm?")
        self.assertIsNone(self.svc.get_active(tenant_id="t2", channel="line", conversation_key="U1"))

    def test_different_channel_never_sees_pending(self):
        self.svc.create(tenant_id="t1", channel="line", conversation_key="U1", action=ACTION,
                         parameters={"Message": "hi"}, original_message="hi", question_text="confirm?")
        self.assertIsNone(self.svc.get_active(tenant_id="t1", channel="playground", conversation_key="U1"))


class TestStatusTransitionsAndAudit(unittest.TestCase):
    def setUp(self):
        self.svc = PendingConfirmationService(_FakeSupabase())
        self.row = self.svc.create(tenant_id="t1", channel="line", conversation_key="U1", action=ACTION,
                                    parameters={"Message": "hi"}, original_message="hi", question_text="confirm?")

    def test_mark_confirmed_sets_timestamp_and_source(self):
        updated = self.svc.mark_confirmed(self.row["id"], source="line_text_reply")
        self.assertEqual(updated["status"], "confirmed")
        self.assertIsNotNone(updated["confirmation_confirmed_at"])
        self.assertEqual(updated["confirmation_source"], "line_text_reply")

    def test_mark_executed_removes_from_active(self):
        self.svc.mark_confirmed(self.row["id"], source="line_text_reply")
        self.svc.mark_executed(self.row["id"])
        self.assertIsNone(self.svc.get_active(tenant_id="t1", channel="line", conversation_key="U1"))

    def test_mark_cancelled_sets_timestamp_and_source(self):
        updated = self.svc.mark_cancelled(self.row["id"], source="line_text_reply")
        self.assertEqual(updated["status"], "cancelled")
        self.assertIsNotNone(updated["confirmation_cancelled_at"])

    def test_no_secret_field_anywhere_in_stored_row(self):
        dumped = str(self.row)
        self.assertNotIn("SecretCode", dumped)


class TestClassifyConfirmationReply(unittest.TestCase):
    def test_thai_confirm_phrases(self):
        for phrase in ["ใช่", "ยืนยัน", "ตกลง", "ส่งเลย", "โอเค", "ดำเนินการเลย"]:
            self.assertEqual(classify_confirmation_reply(phrase), "confirm", phrase)

    def test_english_confirm_phrases(self):
        for phrase in ["yes", "Confirm", "OK", "proceed", "Send it"]:
            self.assertEqual(classify_confirmation_reply(phrase), "confirm", phrase)

    def test_thai_cancel_phrases(self):
        for phrase in ["ไม่", "ไม่ต้อง", "ยกเลิก"]:
            self.assertEqual(classify_confirmation_reply(phrase), "cancel", phrase)

    def test_english_cancel_phrases(self):
        for phrase in ["cancel", "No"]:
            self.assertEqual(classify_confirmation_reply(phrase), "cancel", phrase)

    def test_trailing_thai_politeness_particles_still_match(self):
        self.assertEqual(classify_confirmation_reply("ยืนยันค่ะ"), "confirm")
        self.assertEqual(classify_confirmation_reply("ไม่ต้องค่ะ"), "cancel")
        self.assertEqual(classify_confirmation_reply("ตกลงครับ"), "confirm")

    def test_unrelated_revision_text_is_other_not_confirm(self):
        self.assertEqual(classify_confirmation_reply("ขอแก้ข้อความเป็น ติดต่อกลับพรุ่งนี้"), "other")

    def test_long_sentence_containing_confirm_word_is_other(self):
        """A substring-anywhere match would misfire here — 'ยืนยัน'
        appears inside a sentence that is NOT a plain confirmation."""
        self.assertEqual(classify_confirmation_reply("ยืนยันไม่ได้ค่ะ ขอคิดดูก่อน"), "other")

    def test_empty_message_is_other(self):
        self.assertEqual(classify_confirmation_reply(""), "other")
        self.assertEqual(classify_confirmation_reply("   "), "other")


if __name__ == "__main__":
    unittest.main()
