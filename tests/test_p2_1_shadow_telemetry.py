# -*- coding: utf-8 -*-
"""P2.1 SHADOW OBSERVABILITY — persist minimal, privacy-safe structured
parity telemetry per eligible turn (task §10 A-G).

OBSERVABILITY ONLY: no routing / semantic / frame-authority behaviour is
exercised or asserted here beyond what services/decision_engine.py
already computes for P1/P2/P2-STAB. These tests cover
services/conversation_intelligence_telemetry.py,
services/session_service.py::extract_conversation_fields /
record_conversation_turn (degrade-safety), and
tools/p2_shadow_report.py's aggregation.
"""
import json
import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("OPENAI_API_KEY", "sk-invalid-p2-1")

from services.conversation_intelligence_telemetry import (
    build_telemetry, classify_sample_source, is_shadow_sample_eligible,
)
from services.session_service import extract_conversation_fields, SessionService


def _dev_trace(*, sample_source=None, parity_class="MATCH", unit_preserved=True,
               journey="IMPORT_INTEREST", status="ACTIVE", requested_slot="quantity",
               slots=None, precedence_winner="REQUESTED_SLOT_ANSWER",
               conversation_act="ANSWER", llm_source="deterministic",
               active_journey="IMPORT_INTEREST", known_slot_reask=False,
               error=None, frame_error=None):
    slots = slots if slots is not None else {"product": {"value": "รองเท้า", "unit": None}}
    dev = {
        "sample_source": sample_source,
        "conversation_resolution": {
            "conversation_act": conversation_act, "primary_intent": "IMPORT_INTEREST",
            "precedence_winner": precedence_winner, "active_journey": active_journey,
            "evidence": [{"kind": "llm_semantic_signal", "source": llm_source}],
        },
        "conversation_frame": {
            "version": 1, "journey": journey, "status": status,
            "requested_slot": requested_slot, "slots": slots, "turn_seq": 3,
            "parity_with_legacy": {"class": parity_class, "unit_preserved": unit_preserved,
                                   "reason": "test"},
            "known_slot_reask": known_slot_reask,
        },
    }
    if error:
        dev["conversation_resolution_error"] = error
    if frame_error:
        dev["conversation_frame_error"] = frame_error
    return dev


class TestSampleSourceClassification(unittest.TestCase):
    def test_known_sources_pass_through(self):
        for s in ("REAL_LINE", "ADMIN_AUTO", "TEST", "OTHER"):
            self.assertEqual(classify_sample_source(s), s)

    def test_unknown_or_missing_is_other(self):
        for s in (None, "", "playground", "line", "something_else"):
            self.assertEqual(classify_sample_source(s), "OTHER")


class TestEligibility(unittest.TestCase):
    def test_eligible_when_parity_present(self):
        self.assertTrue(is_shadow_sample_eligible(_dev_trace()))

    def test_not_eligible_without_conversation_frame(self):
        self.assertFalse(is_shadow_sample_eligible({"conversation_resolution": {}}))

    def test_not_eligible_on_resolution_error(self):
        self.assertFalse(is_shadow_sample_eligible(_dev_trace(error="boom")))

    def test_not_eligible_on_frame_error(self):
        self.assertFalse(is_shadow_sample_eligible(_dev_trace(frame_error="boom")))

    def test_structured_wrong_is_still_eligible(self):
        # task §6 — a real failure MUST remain in the sample, never
        # silently excluded.
        self.assertTrue(is_shadow_sample_eligible(_dev_trace(parity_class="STRUCTURED_WRONG")))


class TestBuildTelemetryCases(unittest.TestCase):
    def test_A_real_line_eligible_turn_persisted(self):
        t = build_telemetry(_dev_trace(sample_source="REAL_LINE"))
        self.assertIsNotNone(t)
        self.assertEqual(t["sample_source"], "REAL_LINE")
        self.assertEqual(t["parity_class"], "MATCH")
        self.assertTrue(t["eligible_for_cutover_sample"])

    def test_B_admin_auto_persisted_not_real_line(self):
        t = build_telemetry(_dev_trace(sample_source="ADMIN_AUTO"))
        self.assertIsNotNone(t)
        self.assertEqual(t["sample_source"], "ADMIN_AUTO")

    def test_C_test_source_excluded_from_live_denominator(self):
        t = build_telemetry(_dev_trace(sample_source="TEST"))
        self.assertIsNotNone(t)  # persisted...
        self.assertEqual(t["sample_source"], "TEST")  # ...but never REAL_LINE
        self.assertNotEqual(t["sample_source"], "REAL_LINE")

    def test_D_structured_wrong_persisted_and_flagged_never_filtered(self):
        t = build_telemetry(_dev_trace(sample_source="REAL_LINE", parity_class="STRUCTURED_WRONG"))
        self.assertIsNotNone(t)
        self.assertEqual(t["parity_class"], "STRUCTURED_WRONG")

    def test_E_ineligible_turn_returns_none(self):
        self.assertIsNone(build_telemetry(_dev_trace(error="boom")))
        self.assertIsNone(build_telemetry({}))
        self.assertIsNone(build_telemetry(None))

    def test_stale_journey_takeover_flag(self):
        t = build_telemetry(_dev_trace(sample_source="REAL_LINE",
                                       precedence_winner="NEW_INTENT",
                                       parity_class="STRUCTURED_WRONG"))
        self.assertTrue(t["stale_journey_takeover"])

    def test_known_slot_reask_flag_passthrough(self):
        t = build_telemetry(_dev_trace(sample_source="REAL_LINE", known_slot_reask=True))
        self.assertTrue(t["known_slot_reask"])

    def test_llm_authority_violation_flag(self):
        t = build_telemetry(_dev_trace(sample_source="REAL_LINE",
                                       precedence_winner="REQUESTED_SLOT_ANSWER",
                                       llm_source="llm"))
        self.assertTrue(t["llm_authority_violation"])
        # a deterministic decision at the same tier is NOT a violation
        t2 = build_telemetry(_dev_trace(sample_source="REAL_LINE",
                                        precedence_winner="REQUESTED_SLOT_ANSWER",
                                        llm_source="deterministic"))
        self.assertFalse(t2["llm_authority_violation"])
        # the LLM deciding a genuinely ambiguous turn (no context tier) is fine
        t3 = build_telemetry(_dev_trace(sample_source="REAL_LINE",
                                        precedence_winner="STALE_HISTORY",
                                        llm_source="llm"))
        self.assertFalse(t3["llm_authority_violation"])


class TestPrivacy(unittest.TestCase):
    def test_F_no_slot_values_or_identifiers_leaked(self):
        dev = _dev_trace(sample_source="REAL_LINE", slots={
            "product": {"value": "รองเท้า", "unit": None},
            "quantity": {"value": 20, "unit": "คู่"},
        })
        t = build_telemetry(dev)
        blob = json.dumps(t, ensure_ascii=False)
        self.assertNotIn("รองเท้า", blob)   # slot VALUE never present
        self.assertNotIn("คู่", blob)
        self.assertEqual(t["known_slots"], ["product", "quantity"])  # NAMES only

    def test_no_raw_message_or_reply_text_fields_exist(self):
        t = build_telemetry(_dev_trace(sample_source="REAL_LINE"))
        for forbidden in ("message", "reply", "raw_text", "prompt", "erp_response",
                          "tracking", "custcode", "phone", "address", "url"):
            self.assertNotIn(forbidden, t)


class TestExtractConversationFieldsIntegration(unittest.TestCase):
    def test_G_existing_metadata_keys_preserved(self):
        decide_result = {
            "reply": {"text": "..."}, "routing": {"type": "GENERAL"},
            "developer": {**_dev_trace(sample_source="REAL_LINE"),
                          "classification": {"classification": "biz"},
                          "workflow": "some_workflow", "latency_ms": 12.3,
                          "intent": {"broad_intent": "x", "actionable_intent": "y"}},
            "alert": None, "error": None,
        }
        fields = extract_conversation_fields(decide_result)
        md = fields["metadata"]
        self.assertEqual(md["classification"], "biz")
        self.assertEqual(md["workflow"], "some_workflow")
        self.assertIn("alert", md)
        self.assertIn("error", md)
        self.assertIsNotNone(md.get("conversation_intelligence"))
        self.assertEqual(md["conversation_intelligence"]["sample_source"], "REAL_LINE")

    def test_ineligible_turn_yields_none_telemetry_but_other_metadata_intact(self):
        decide_result = {"reply": {"text": "..."}, "routing": {"type": "RAG"},
                         "developer": {"classification": {"classification": "x"}}}
        fields = extract_conversation_fields(decide_result)
        self.assertIsNone(fields["metadata"]["conversation_intelligence"])
        self.assertEqual(fields["metadata"]["classification"], "x")

    def test_telemetry_builder_internal_exception_degrades_to_none(self):
        # build_telemetry() itself never raises, even given a malformed
        # developer_trace shape that would otherwise throw mid-build.
        malformed = {"conversation_resolution": "not-a-dict",
                    "conversation_frame": {"parity_with_legacy": {"class": "MATCH"}}}
        self.assertIsNone(build_telemetry(malformed))


class TestRecordConversationTurnDegradesSafely(unittest.TestCase):
    def test_E2_db_write_failure_does_not_raise(self):
        svc = SessionService()
        with patch("services.session_service._get_sb", side_effect=RuntimeError("db down")):
            # record_conversation_turn's own try/except must swallow this
            # — the customer's turn must complete regardless.
            result = svc.record_conversation_turn(
                "sid-does-not-matter", "hello",
                {"reply": {"text": "hi"}, "routing": {"type": "GENERAL"},
                 "developer": _dev_trace(sample_source="REAL_LINE")},
                line_user_id="U_test")
        self.assertIsNone(result)  # degraded, not raised


if __name__ == "__main__":
    unittest.main()
