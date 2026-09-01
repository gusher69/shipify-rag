"""P4.1 — Negative customer detection + one Admin LINE alert.

Deterministic, analytical side-channel. No LLM, no change to the AI reply.
"""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

from services.sentiment_service import (
    detect_negative_reasons, _decide, update_sentiment_from_turn,
    send_admin_negative_alert, reason_labels, _format_alert,
)

_UA = "U" + "a" * 32
_NOW = datetime(2026, 9, 1, 18, 30, tzinfo=timezone.utc)


class Detection(unittest.TestCase):
    # A
    def test_direct_repeated_wrong_answer_is_negative(self):
        r = detect_negative_reasons("ตอบผิดอีกแล้วครับ")
        self.assertIn("repeated_wrong_answer", r)

    # B
    def test_strong_complaint_plus_human_request(self):
        r = detect_negative_reasons("ระบบตอบผิดตลอด ขอคุยกับเจ้าหน้าที่")
        self.assertIn("repeated_wrong_answer", r)
        self.assertIn("human_requested", r)

    def test_strong_negative_language(self):
        self.assertIn("strong_negative_language", detect_negative_reasons("บริการแย่มาก"))

    def test_long_wait_and_unresolved(self):
        r = detect_negative_reasons("รอนานมากยังไม่ได้เรื่องเลย")
        self.assertIn("service_dissatisfaction", r)
        self.assertIn("unresolved_issue", r)

    def test_complaint_intent(self):
        self.assertIn("complaint", detect_negative_reasons("จะร้องเรียนแล้วนะครับ"))

    # C — negation / correction
    def test_correction_is_not_negative(self):
        self.assertEqual(detect_negative_reasons("ไม่ได้ถามเรื่องประกันสินค้า"), [])

    # D — factual negative wording
    def test_factual_negative_wording_is_normal(self):
        self.assertEqual(detect_negative_reasons("น้ำหอมนำเข้าไม่ได้ใช่ไหม"), [])
        self.assertEqual(detect_negative_reasons("ไม่มีขนส่งทางเครื่องบินใช่ไหม"), [])

    # E — preference
    def test_preference_is_normal(self):
        self.assertEqual(detect_negative_reasons("ไม่เอาทางเรือ เอาทางรถ"), [])

    def test_reassurance_and_thanks_are_normal(self):
        self.assertEqual(detect_negative_reasons("ไม่เป็นไรครับ"), [])
        self.assertEqual(detect_negative_reasons("ขอบคุณครับ"), [])

    def test_internal_failure_alone_is_not_negative(self):
        self.assertEqual(detect_negative_reasons("ขอเลขพัสดุหน่อยครับ",
                                                 action_failed_repeatedly=True), [])

    def test_internal_failure_with_complaint_adds_reason(self):
        r = detect_negative_reasons("ตอบผิดอีกแล้ว", action_failed_repeatedly=True)
        self.assertIn("business_action_failed_repeatedly", r)


class TransitionAndDedup(unittest.TestCase):
    def test_normal_to_negative_alerts_once(self):
        d = _decide("NORMAL", [], ["repeated_wrong_answer"], None, None, _NOW)
        self.assertEqual(d["status"], "NEGATIVE")
        self.assertTrue(d["should_alert"])
        self.assertTrue(d["is_new_incident"])

    # F — second negative message within cooldown: no second alert
    def test_second_negative_within_cooldown_no_alert(self):
        alerted = (_NOW - timedelta(minutes=5)).isoformat()
        d = _decide("NEGATIVE", ["repeated_wrong_answer"], ["unresolved_issue"],
                    (_NOW - timedelta(minutes=6)).isoformat(), alerted, _NOW)
        self.assertEqual(d["status"], "NEGATIVE")
        self.assertFalse(d["should_alert"])

    # G — new incident after cooldown
    def test_new_incident_after_cooldown_alerts_again(self):
        old = (_NOW - timedelta(hours=30)).isoformat()
        d = _decide("NEGATIVE", ["complaint"], ["complaint"], old, old, _NOW)
        self.assertTrue(d["should_alert"])

    def test_neutral_message_keeps_negative_within_24h(self):
        recent = (_NOW - timedelta(hours=2)).isoformat()
        d = _decide("NEGATIVE", ["complaint"], [], recent, recent, _NOW)
        self.assertEqual(d["status"], "NEGATIVE")
        self.assertFalse(d["should_alert"])

    def test_neutral_message_resets_after_24h(self):
        old = (_NOW - timedelta(hours=25)).isoformat()
        d = _decide("NEGATIVE", ["complaint"], [], old, old, _NOW)
        self.assertEqual(d["status"], "NORMAL")
        self.assertEqual(d["reasons"], [])

    def test_normal_stays_normal(self):
        d = _decide("NORMAL", [], [], None, None, _NOW)
        self.assertEqual(d["status"], "NORMAL")
        self.assertFalse(d["should_alert"])


class AlertDelivery(unittest.TestCase):
    def _ok_executor(self):
        ex = MagicMock()
        ex.execute.return_value = {"status": "success"}
        return ex

    def test_reuses_notify_business_action(self):
        ex = self._ok_executor()
        with patch("services.human_handoff_service.find_notification_action",
                   return_value={"id": "act-1", "action_key": "sendlinenotics"}), \
             patch("services.action_executor.get_action_executor", return_value=ex):
            out = send_admin_negative_alert(display_name="GuDz", cust_code="FT3182",
                                            lead_stage="WARM", reasons=["repeated_wrong_answer"],
                                            last_message="ตอบผิดอีกแล้ว", when=_NOW)
        self.assertTrue(out["sent"])
        slots = ex.execute.call_args.kwargs["context"]["collected_slots"]
        self.assertIn("⚠️ ลูกค้าต้องการการดูแล", slots["Message"])
        self.assertIn("FT3182", slots["Message"])
        self.assertIn("Sentiment: NEGATIVE", slots["Message"])

    def test_no_notify_action_is_blocked_not_crash(self):
        with patch("services.human_handoff_service.find_notification_action", return_value=None):
            out = send_admin_negative_alert(display_name="x", cust_code=None, lead_stage="COLD",
                                            reasons=["complaint"], last_message="แย่มาก")
        self.assertFalse(out["sent"])
        self.assertEqual(out["error"], "no_notification_action_configured")

    # K — push failure
    def test_push_failure_is_swallowed(self):
        ex = MagicMock()
        ex.execute.side_effect = RuntimeError("LINE 500")
        with patch("services.human_handoff_service.find_notification_action",
                   return_value={"id": "a", "action_key": "k"}), \
             patch("services.action_executor.get_action_executor", return_value=ex):
            out = send_admin_negative_alert(display_name="x", cust_code=None, lead_stage="COLD",
                                            reasons=["complaint"], last_message="แย่มาก")
        self.assertFalse(out["sent"])
        self.assertTrue(out["error"])

    def test_alert_omits_unverified_custcode_as_dash(self):
        txt = _format_alert(display_name="x", cust_code=None, lead_stage="WARM",
                            reasons=["complaint"], last_message="แย่มาก", when=_NOW)
        self.assertIn("CustCode: —", txt)


class UpdateFromTurn(unittest.TestCase):
    def _sb(self):
        captured = {}
        def _update(row):
            captured["row"] = row
            return MagicMock(eq=lambda *a, **k: MagicMock(execute=lambda: MagicMock(data=[])))
        tbl = MagicMock(); tbl.update.side_effect = _update
        sb = MagicMock(); sb.table.return_value = tbl
        return sb, captured

    def _run(self, profile, question, *, alert_sent=True):
        sb, captured = self._sb()
        with patch("profiles.manager.get_profile", return_value=profile), \
             patch("profiles.manager.supabase", sb), \
             patch("profiles.manager._profile_cache_clear"), \
             patch("services.sentiment_service.send_admin_negative_alert",
                   return_value={"sent": alert_sent, "error": None if alert_sent else "boom"}) as al:
            out = update_sentiment_from_turn(_UA, question=question, display_name="GuDz",
                                             cust_code="FT3182", now=_NOW)
        return out, captured.get("row"), al

    # A
    def test_direct_complaint_persists_negative_and_alerts(self):
        out, row, al = self._run({"sentiment_status": "NORMAL", "lead_stage": "WARM"},
                                 "ตอบผิดอีกแล้วครับ")
        self.assertEqual(row["sentiment_status"], "NEGATIVE")
        self.assertIn("repeated_wrong_answer", row["sentiment_reasons"])
        self.assertIn("negative_last_detected_at", row)
        self.assertIn("negative_last_alert_at", row)
        al.assert_called_once()
        # only sentiment columns written — lead stage untouched
        self.assertNotIn("lead_stage", row)
        self.assertNotIn("conversation_tier", row)

    # H / I — lead stage not overwritten (different column entirely)
    def test_hot_customer_complaint_keeps_lead_stage(self):
        out, row, al = self._run({"sentiment_status": "NORMAL", "lead_stage": "HOT",
                                  "lead_score": 80}, "บริการแย่มาก")
        self.assertEqual(row["sentiment_status"], "NEGATIVE")
        self.assertTrue(all(k.startswith(("sentiment_", "negative_")) for k in row))

    # F — dedup across two turns
    def test_dedup_second_negative_no_alert(self):
        prof = {"sentiment_status": "NEGATIVE", "sentiment_reasons": ["repeated_wrong_answer"],
                "negative_last_detected_at": (_NOW - timedelta(minutes=5)).isoformat(),
                "negative_last_alert_at": (_NOW - timedelta(minutes=5)).isoformat()}
        out, row, al = self._run(prof, "ยังผิดอยู่ครับ")
        self.assertEqual(row["sentiment_status"], "NEGATIVE")
        al.assert_not_called()
        self.assertNotIn("negative_last_alert_at", row)

    # C/D/E — false positives never write NEGATIVE or alert
    def test_false_positive_stays_normal_no_alert(self):
        for q in ("ไม่ได้ถามเรื่องประกันสินค้า", "น้ำหอมนำเข้าไม่ได้ใช่ไหม",
                  "ไม่เอาทางเรือ เอาทางรถ", "ขอบคุณครับ"):
            out, row, al = self._run({"sentiment_status": "NORMAL"}, q)
            self.assertEqual(row["sentiment_status"], "NORMAL", msg=q)
            al.assert_not_called()

    # K — alert failure: sentiment still persisted, alert retried next turn (no alert_at)
    def test_alert_failure_persists_sentiment_and_allows_retry(self):
        out, row, al = self._run({"sentiment_status": "NORMAL"}, "บริการแย่มาก", alert_sent=False)
        self.assertEqual(row["sentiment_status"], "NEGATIVE")
        self.assertNotIn("negative_last_alert_at", row)

    # J — playground / synthetic user is skipped entirely
    def test_playground_user_skipped(self):
        with patch("profiles.manager.get_profile") as gp:
            self.assertIsNone(update_sentiment_from_turn("playground:UAT-1", question="บริการแย่มาก"))
            gp.assert_not_called()

    def test_probe_shape_user_skipped(self):
        self.assertIsNone(update_sentiment_from_turn("Uprobe0000000000000000000000000A",
                                                     question="บริการแย่มาก"))


class Labels(unittest.TestCase):
    def test_thai_labels(self):
        out = reason_labels(["repeated_wrong_answer", "human_requested", "nope"])
        self.assertEqual(out[0], "แจ้งว่าระบบตอบผิดซ้ำ")
        self.assertEqual(out[1], "ขอคุยกับเจ้าหน้าที่")
        self.assertEqual(out[2], "nope")


if __name__ == "__main__":
    unittest.main()
