"""Customer UAT Fix 2 — trusted no-information behaviour + a REAL Human CS
handoff whenever the assistant tells the customer a staff member will
follow up.

Bug: the RAG pipeline's deterministic "no trusted information" branches
(Answerability Gate + P7.1) answered a genuine unsupported COMPANY fact
with wording that points at staff, but NO real handoff / notification was
ever created — a promise with no execution.

Fix: the RAG pipeline exposes a structured `unsupported_company_fact`
signal; the Decision Engine routes that through the SAME existing Human
CS handoff path the AI-policy escalation uses (routing_type
"HUMAN_HANDOFF" + handoff_payload{reason}), so line_bot/webhook.py's
existing send_handoff_notification + handoff_status dedup actually
notify staff. The customer only hears "staff will check" when the
notification really succeeded.

Test phrases are fixtures only. Same _FakeSupabase / mocked-RAG
convention as tests/test_stale_erp_over_rag_boundary.py; the webhook
tests reuse tests/test_webhook.py's TestDecisionEngineAdapter harness.
"""
import inspect
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datetime import datetime, timedelta, timezone

from tests.test_business_action_registry import _FakeSupabase
from tests.test_decision_engine import _seed_action, _fake_playground_result, _engine_with_registry
from services.business_action_registry import BusinessActionRegistry
from services.decision_engine import DecisionEngine
from services.session_service import _now_iso, HANDOFF_EPISODE_TTL_SECONDS

_NOINFO_TEXT = "ตอนนี้ยังไม่มีข้อมูลยืนยันเรื่องนี้ค่ะ"
_STAFF_PROMISE_FRAGMENT = "เจ้าหน้าที่"
# the REAL P7.1 no-info text carried into the handoff reply — ends with a
# SELF-SERVICE line that Fix-2.2 strips once a handoff is on the books.
_P71_NOINFO_TEXT = ("ตอนนี้ยังไม่มีข้อมูลยืนยันนโยบายเรื่องนี้ในระบบค่ะ "
                    "รบกวนสอบถามเจ้าหน้าที่เพื่อความชัดเจนอีกครั้งนะคะ")
_SELF_SERVICE_ASK = "รบกวนสอบถามเจ้าหน้าที่เพื่อความชัดเจนอีกครั้งนะคะ"
_SERVICE_MIND_FRAGMENT = "ทางเราประสานเจ้าหน้าที่"


# ── Part A — Decision Engine routing contract ───────────────────────

class Fix2_DecisionEngineRoutesUnsupportedFactToHandoff(unittest.TestCase):
    def setUp(self):
        self.reg = BusinessActionRegistry(_FakeSupabase())
        # a RAG-type action so the RAG pipeline path is taken
        _seed_action(self.reg, key="kb", action_type="RAG", keywords=["นโยบาย", "รับประกัน", "บริษัท"])
        self.engine = _engine_with_registry(self.reg)

    def _decide(self, message, *, unsupported, answer=_NOINFO_TEXT):
        ctx = {"developer_mode": True, "channel": "line",
               "customer_context": {"cust_code": "FT3182", "identity_confirmed": True}}
        with patch("services.playground_orchestrator.run_playground_turn",
                   return_value=_fake_playground_result(
                       answer=answer, confidence=0.2,
                       unsupported_company_fact=unsupported)):
            return self.engine.decide(message, history=[], context=ctx)

    def test_known_fact_answers_normally_no_handoff(self):
        r = self._decide("บริษัทมีนโยบายคืนเงินอย่างไร", unsupported=False, answer="คืนเงินได้ภายใน 7 วันค่ะ")
        self.assertEqual(r["routing"]["type"], "RAG")
        self.assertIsNone(r.get("handoff_payload"))
        self.assertIn("คืนเงิน", r["reply"]["text"])

    def test_unsupported_company_fact_routes_to_human_handoff(self):
        r = self._decide("Shipify รับประกันว่าสินค้าทุกชิ้นจะผ่านศุลกากรไหม", unsupported=True)
        self.assertEqual(r["routing"]["type"], "HUMAN_HANDOFF")
        self.assertEqual((r.get("handoff_payload") or {}).get("reason"),
                          "unsupported_company_information")
        # the reply the Decision Engine carries is the honest no-info text
        # with NO "staff will check" promise — the channel adapter adds
        # that only if the real notification succeeds
        self.assertEqual(r["reply"]["text"], _NOINFO_TEXT)
        self.assertNotIn(_STAFF_PROMISE_FRAGMENT, r["reply"]["text"])
        self.assertTrue((r.get("developer") or {}).get("unsupported_company_fact_handoff"))

    def test_clarification_shaped_turn_is_not_escalated(self):
        # a normal turn (flag False) — even a low-confidence one — never
        # routes to handoff: no over-escalation
        r = self._decide("สั่งเยอะได้ไหม", unsupported=False,
                          answer="ได้ค่ะ ไม่ทราบว่าสินค้าประเภทไหนและกี่ชิ้นคะ")
        self.assertNotEqual(r["routing"]["type"], "HUMAN_HANDOFF")
        self.assertIsNone(r.get("handoff_payload"))


# ── Part B — webhook: promise wording follows the REAL send outcome ─
#
# Minimal replica of tests/test_webhook.py::TestDecisionEngineAdapter's
# harness (not subclassed, to keep `unittest discover` import order
# clean).

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


class Fix2_WebhookPromiseMatchesRealHandoff(unittest.TestCase):
    def setUp(self):
        self._patchers = [
            patch.object(webhook_module, "get_profile", return_value={"display_name": "Test"}),
            patch.object(webhook_module, "upsert_profile"),
            patch.object(webhook_module, "MessagingApi"),
            patch.object(webhook_module, "get_session_service"),
            patch.object(webhook_module, "update_profile_from_turn"),
            patch.object(webhook_module, "update_tier_for_profile"),
        ]
        started = [p.start() for p in self._patchers]
        self.mock_line_bot_api = MagicMock()
        started[2].return_value = self.mock_line_bot_api          # MessagingApi()
        self.mock_session_service = MagicMock()
        self.mock_session_service.get_or_create_active_conversation.return_value = {
            "id": "fake-session-id", "message_count": 0}
        self.mock_session_service.get_recent_history.return_value = []
        started[3].return_value = self.mock_session_service       # get_session_service()

    def tearDown(self):
        for p in self._patchers:
            p.stop()

    def _decide_result(self, *, text, routing_type, handoff_payload):
        return {"reply": {"text": text, "images": [], "files": []},
                "routing": {"type": routing_type}, "handoff_payload": handoff_payload,
                "alert": None, "error": None}

    def _run(self, *, handoff_status, send_result, handoff_state=None, reason="unsupported_company_information",
             message="Shipify การันตีของหายไหม", base_reply=None):
        # decide() still only consults the status string
        self.mock_session_service.get_handoff_status.return_value = handoff_status
        # Fix-2.1 — the webhook's episode-aware dedupe consults the full state
        state = handoff_state if handoff_state is not None else {
            "status": handoff_status, "reason": reason if handoff_status in ("NOTIFIED", "PENDING") else None,
            "notified_at": _now_iso() if handoff_status == "NOTIFIED" else None}
        self.mock_session_service.get_handoff_state.return_value = state
        # keep the real episode helper (a MagicMock session_service would
        # otherwise stub it away)
        from services import session_service as _ss
        self.mock_session_service.is_same_handoff_episode.side_effect = _ss.is_same_handoff_episode
        with patch("services.decision_engine.DecisionEngine") as mock_engine_cls, \
             patch.object(webhook_module, "send_line_notify"), \
             patch("services.human_handoff_service.send_handoff_notification",
                   return_value=send_result) as mock_notify:
            mock_engine_cls.return_value.decide.return_value = self._decide_result(
                text=base_reply if base_reply is not None else _NOINFO_TEXT,
                routing_type="HUMAN_HANDOFF", handoff_payload={"reason": reason})
            webhook_module._handle_message_via_decision_engine(_fake_event(message))
        sent_text = " ".join(
            m.text for m in self.mock_line_bot_api.reply_message.call_args.args[0].messages
            if hasattr(m, "text"))
        return mock_notify, sent_text

    def test_real_handoff_success_notifies_and_promises_followup(self):
        mock_notify, sent_text = self._run(
            handoff_status="NONE", send_result={"sent": True, "action_key": "sendlinenotics", "error": None})
        mock_notify.assert_called_once()
        self.assertEqual(mock_notify.call_args.kwargs["reason"], "unsupported_company_information")
        self.mock_session_service.set_handoff_status.assert_any_call(
            "fake-session-id", "NOTIFIED", reason="unsupported_company_information")
        self.assertIn("เจ้าหน้าที่", sent_text)            # follow-up promise present — it is true now
        self.assertIn("ยังไม่", sent_text)                  # still the honest no-info wording

    def test_handoff_failure_does_not_fake_success(self):
        mock_notify, sent_text = self._run(
            handoff_status="NONE", send_result={"sent": False, "action_key": None,
                                                 "error": "no_notification_action_configured"})
        mock_notify.assert_called_once()
        self.assertNotIn("เจ้าหน้าที่", sent_text)          # NO fabricated staff promise
        self.assertIn("ยังไม่", sent_text)
        self.mock_session_service.set_handoff_status.assert_any_call(
            "fake-session-id", "NONE", reason="unsupported_company_information")

    def test_dedupe_does_not_storm_but_still_reassures(self):
        # SAME issue (unsupported_company_information) already NOTIFIED
        # within the active window -> real duplicate, no storm.
        mock_notify, sent_text = self._run(
            handoff_status="NOTIFIED", send_result={"sent": True},
            handoff_state={"status": "NOTIFIED", "reason": "unsupported_company_information",
                           "notified_at": _now_iso()})
        mock_notify.assert_not_called()                     # no notification storm
        self.assertIn("เจ้าหน้าที่", sent_text)             # CS already engaged for THIS issue — reassurance is true

    def test_fresh_conversation_status_none_does_not_block_new_handoff(self):
        # a new/rolled conversation reports NONE regardless of any older
        # session's NOTIFIED — the send proceeds
        mock_notify, _ = self._run(
            handoff_status="NONE", send_result={"sent": True})
        mock_notify.assert_called_once()


# ── Part B.1 — Fix-2.1: dedupe is ISSUE / EPISODE aware ─────────────

class Fix21_HandoffDedupeIsEpisodeAware(Fix2_WebhookPromiseMatchesRealHandoff):
    """The REAL LINE defect: a 3-day-old, unrelated `self_verification_failed`
    NOTIFIED state suppressed a genuine new `unsupported_company_information`
    handoff. Dedupe must key on (reason class + active episode window),
    not on any historical NOTIFIED."""

    _OLD_SELF_VERIF = {
        "status": "NOTIFIED",
        "reason": "self_verification_failed: CustCode FT3182: phone did not match record on file",
        "notified_at": (datetime.now(timezone.utc) - timedelta(days=3)).isoformat(),
    }

    def test_A_old_unrelated_notified_does_not_suppress_new_issue(self):
        mock_notify, sent_text = self._run(
            handoff_status="NOTIFIED", send_result={"sent": True, "action_key": "sendlinenotics", "error": None},
            handoff_state=self._OLD_SELF_VERIF)
        mock_notify.assert_called_once()                                  # NEW notification really sent
        self.assertEqual(mock_notify.call_args.kwargs["reason"], "unsupported_company_information")
        self.mock_session_service.set_handoff_status.assert_any_call(
            "fake-session-id", "NOTIFIED", reason="unsupported_company_information")  # evidence updated
        self.assertIn("เจ้าหน้าที่", sent_text)

    def test_B_same_issue_immediate_repeat_is_deduped(self):
        mock_notify, sent_text = self._run(
            handoff_status="NOTIFIED", send_result={"sent": True},
            handoff_state={"status": "NOTIFIED", "reason": "unsupported_company_information",
                           "notified_at": _now_iso()})
        mock_notify.assert_not_called()
        self.assertIn("เจ้าหน้าที่", sent_text)

    def test_C_same_episode_paraphrase_is_deduped_no_exact_string(self):
        # a different sentence, same reason class, still inside the window
        mock_notify, _ = self._run(
            handoff_status="NOTIFIED", send_result={"sent": True},
            handoff_state={"status": "NOTIFIED", "reason": "unsupported_company_information",
                           "notified_at": (datetime.now(timezone.utc) - timedelta(seconds=90)).isoformat()},
            message="แล้ว Shipify รับผิดชอบไหมถ้าของติดศุลกากร")
        mock_notify.assert_not_called()

    def test_D_same_class_after_episode_window_notifies_again(self):
        lapsed = (datetime.now(timezone.utc)
                  - timedelta(seconds=HANDOFF_EPISODE_TTL_SECONDS + 120)).isoformat()
        mock_notify, _ = self._run(
            handoff_status="NOTIFIED", send_result={"sent": True},
            handoff_state={"status": "NOTIFIED", "reason": "unsupported_company_information",
                           "notified_at": lapsed})
        mock_notify.assert_called_once()                                  # new episode -> notify again

    def test_E_new_issue_notification_failure_no_false_promise(self):
        mock_notify, sent_text = self._run(
            handoff_status="NOTIFIED",
            send_result={"sent": False, "action_key": None, "error": "execution_failed"},
            handoff_state=self._OLD_SELF_VERIF)
        mock_notify.assert_called_once()
        self.assertNotIn("เจ้าหน้าที่", sent_text)                        # no fabricated promise
        self.assertIn("ยังไม่", sent_text)
        self.mock_session_service.set_handoff_status.assert_any_call(
            "fake-session-id", "NONE", reason="unsupported_company_information")  # retryable


# ── Part B.2 — Fix-2.2: Service-Mind handoff wording ───────────────

class Fix22_ServiceMindWording(Fix2_WebhookPromiseMatchesRealHandoff):
    """Once a Human CS handoff is on the books, the reply must read
    'we will coordinate a staff follow-up for you', never 'go ask staff
    yourself'. On notification failure the customer keeps the neutral
    no-info wording with no coordination promise."""

    def test_A_fresh_success_uses_service_mind_not_self_service(self):
        mock_notify, sent_text = self._run(
            handoff_status="NONE", send_result={"sent": True, "action_key": "sendlinenotics", "error": None},
            base_reply=_P71_NOINFO_TEXT)
        mock_notify.assert_called_once()
        self.assertNotIn(_SELF_SERVICE_ASK, sent_text)          # "go ask staff yourself" removed
        self.assertIn(_SERVICE_MIND_FRAGMENT, sent_text)         # "we coordinate staff for you" present
        self.assertIn("ยังไม่มีข้อมูลยืนยัน", sent_text)         # honest no-info kept

    def test_B_same_episode_dedupe_still_service_mind_no_second_notify(self):
        mock_notify, sent_text = self._run(
            handoff_status="NOTIFIED", send_result={"sent": True},
            handoff_state={"status": "NOTIFIED", "reason": "unsupported_company_information",
                           "notified_at": _now_iso()},
            base_reply=_P71_NOINFO_TEXT)
        mock_notify.assert_not_called()                          # no notification storm
        self.assertNotIn(_SELF_SERVICE_ASK, sent_text)
        self.assertIn(_SERVICE_MIND_FRAGMENT, sent_text)

    def test_C_notification_failure_neutral_no_info_no_coordination_promise(self):
        mock_notify, sent_text = self._run(
            handoff_status="NONE",
            send_result={"sent": False, "action_key": None, "error": "execution_failed"},
            base_reply=_P71_NOINFO_TEXT)
        mock_notify.assert_called_once()
        self.assertNotIn(_SERVICE_MIND_FRAGMENT, sent_text)      # no false coordination promise
        self.assertNotIn(_SELF_SERVICE_ASK, sent_text)          # self-service line also dropped
        self.assertIn("ยังไม่มีข้อมูลยืนยัน", sent_text)         # neutral no-info remains

    def test_D_normal_rag_reply_untouched(self):
        # a non-handoff reply is never rewritten by this path
        with patch("services.decision_engine.DecisionEngine") as mock_engine_cls, \
             patch.object(webhook_module, "send_line_notify"):
            mock_engine_cls.return_value.decide.return_value = {
                "reply": {"text": "Shipify ฝ่ายบริการลูกค้า : 02-026-6426", "images": [], "files": []},
                "routing": {"type": "RAG"}, "handoff_payload": None, "alert": None, "error": None}
            webhook_module._handle_message_via_decision_engine(_fake_event("ขอเบอร์ติดต่อ"))
        sent = " ".join(m.text for m in self.mock_line_bot_api.reply_message.call_args.args[0].messages
                        if hasattr(m, "text"))
        self.assertEqual(sent, "Shipify ฝ่ายบริการลูกค้า : 02-026-6426")


# ── Part C — the RAG pipeline exposes the structured signal ─────────

class Fix2_RagPipelineExposesStructuredSignal(unittest.TestCase):
    def test_playground_result_has_the_field_defaulting_false(self):
        from services.playground_orchestrator import PlaygroundResult
        self.assertIn("unsupported_company_fact", PlaygroundResult.__dataclass_fields__)
        self.assertIs(PlaygroundResult.__dataclass_fields__["unsupported_company_fact"].default, False)

    def test_both_no_info_branches_set_the_flag(self):
        import services.playground_orchestrator as po
        src = inspect.getsource(po.run_playground_turn)
        # set in exactly two places: the Answerability-Gate deterministic
        # no-info company-fact branch, and the P7.1 guarantee branch
        self.assertEqual(src.count("unsupported_company_fact = True"), 2)
        # the P7.1 branch specifically
        p71 = src.split('answer_text = ("ตอนนี้ยังไม่มีข้อมูลยืนยันนโยบายเรื่องนี้ในระบบค่ะ "', 1)[1] \
                 .split("stages.append", 1)[0]
        self.assertIn("unsupported_company_fact = True", p71)

    def test_decision_engine_surfaces_it_without_re_deriving(self):
        import services.decision_engine as de
        src = inspect.getsource(de.DecisionEngine._run_rag_pipeline)
        self.assertIn('"unsupported_company_fact"', src)
        self.assertIn("getattr(result", src)


if __name__ == "__main__":
    unittest.main()
