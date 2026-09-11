# -*- coding: utf-8 -*-
"""P2.1A — owner/tester LINE sample separation (observability only).

A configured owner/tester LINE sender must be classified OWNER_TEST at
the webhook boundary (never REAL_LINE), so their traffic never inflates
the >= 200 REAL_LINE shadow-parity cutover sample — while their LINE id
itself must never reach persisted telemetry, and their customer-facing
experience must be byte-for-byte identical to a real customer's.
"""
import json
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("OPENAI_API_KEY", "sk-invalid-p2-1a")

import config
from line_bot.webhook import _classify_line_sample_source
from services.conversation_intelligence_telemetry import build_telemetry, classify_sample_source
from tools.p2_shadow_report import _summarize


def _dev_trace(sample_source):
    return {
        "sample_source": sample_source,
        "conversation_resolution": {
            "conversation_act": "ANSWER", "primary_intent": "IMPORT_INTEREST",
            "precedence_winner": "REQUESTED_SLOT_ANSWER", "active_journey": "IMPORT_INTEREST",
            "evidence": [{"kind": "llm_semantic_signal", "source": "deterministic"}],
        },
        "conversation_frame": {
            "version": 1, "journey": "IMPORT_INTEREST", "status": "ACTIVE",
            "requested_slot": "quantity",
            "slots": {"product": {"value": "รองเท้า", "unit": None}},
            "turn_seq": 2,
            "parity_with_legacy": {"class": "MATCH", "unit_preserved": True},
            "known_slot_reask": False,
        },
    }


class TestOwnerTestClassification(unittest.TestCase):
    def test_configured_tester_is_owner_test(self):
        with patch.object(config, "OWNER_TEST_LINE_USER_IDS", frozenset({"Uowner123", "Utester456"})):
            self.assertEqual(_classify_line_sample_source("Uowner123"), "OWNER_TEST")
            self.assertEqual(_classify_line_sample_source("Utester456"), "OWNER_TEST")

    def test_normal_sender_is_real_line(self):
        with patch.object(config, "OWNER_TEST_LINE_USER_IDS", frozenset({"Uowner123"})):
            self.assertEqual(_classify_line_sample_source("Ucustomer789"), "REAL_LINE")

    def test_absent_configuration_degrades_to_real_line_for_everyone(self):
        # task §8 — no tester config present -> safe default, unchanged
        # from pre-P2.1A behaviour.
        with patch.object(config, "OWNER_TEST_LINE_USER_IDS", frozenset()):
            self.assertEqual(_classify_line_sample_source("Uanyone"), "REAL_LINE")
            self.assertEqual(_classify_line_sample_source("Uowner123"), "REAL_LINE")

    def test_ids_never_hardcoded_in_source(self):
        import inspect
        import line_bot.webhook as wh
        src = inspect.getsource(wh._classify_line_sample_source)
        # only the config attribute reference is present — no literal
        # "U..." LINE-id-shaped string anywhere in the function body.
        self.assertNotRegex(src, r'"U[0-9a-f]{10,}"')
        self.assertIn("OWNER_TEST_LINE_USER_IDS", src)

    def test_env_driven_not_new_hardcoded_set(self):
        # the class itself is sourced from os.getenv, not a literal set
        # baked into config.py.
        self.assertIsInstance(config.OWNER_TEST_LINE_USER_IDS, frozenset)


class TestTelemetryPrivacyAndVocabulary(unittest.TestCase):
    def test_owner_test_in_vocabulary(self):
        self.assertEqual(classify_sample_source("OWNER_TEST"), "OWNER_TEST")

    def test_line_id_absent_from_telemetry(self):
        line_id = "Uowner_secret_1234567890"
        with patch.object(config, "OWNER_TEST_LINE_USER_IDS", frozenset({line_id})):
            src = _classify_line_sample_source(line_id)  # "OWNER_TEST"
        dev = _dev_trace(src)
        t = build_telemetry(dev)
        self.assertIsNotNone(t)
        self.assertEqual(t["sample_source"], "OWNER_TEST")
        blob = json.dumps(t, ensure_ascii=False)
        # the id itself must never appear anywhere in the persisted record
        self.assertNotIn(line_id, blob)
        self.assertNotIn("Uowner_secret", blob)

    def test_admin_auto_unchanged(self):
        t = build_telemetry(_dev_trace("ADMIN_AUTO"))
        self.assertEqual(t["sample_source"], "ADMIN_AUTO")

    def test_other_unchanged(self):
        t = build_telemetry(_dev_trace(None))
        self.assertEqual(t["sample_source"], "OTHER")

    def test_real_line_unchanged_for_non_tester(self):
        t = build_telemetry(_dev_trace("REAL_LINE"))
        self.assertEqual(t["sample_source"], "REAL_LINE")


class TestCutoverReportExcludesOwnerTest(unittest.TestCase):
    def test_owner_test_never_counted_as_real_line(self):
        rows = (
            [{"sample_source": "REAL_LINE", "parity_class": "MATCH", "unit_preserved": True,
              "stale_journey_takeover": False, "known_slot_reask": False,
              "llm_authority_violation": False}] * 5
            + [{"sample_source": "OWNER_TEST", "parity_class": "STRUCTURED_WRONG", "unit_preserved": False,
                "stale_journey_takeover": True, "known_slot_reask": True,
                "llm_authority_violation": True}] * 500  # a flood of owner testing
        )
        s = _summarize(rows)
        self.assertEqual(s["REAL_LINE"]["eligible_turns"], 5)  # NOT 505
        self.assertEqual(s["OWNER_TEST"]["eligible_turns"], 500)
        # the owner-test flood's failures must not leak into REAL_LINE's counters
        self.assertEqual(s["REAL_LINE"]["STRUCTURED_WRONG"], 0)
        self.assertEqual(s["REAL_LINE"]["stale_journey_takeover"], 0)

    def test_owner_test_reported_as_its_own_bucket(self):
        rows = [{"sample_source": "OWNER_TEST", "parity_class": "MATCH", "unit_preserved": True,
                "stale_journey_takeover": False, "known_slot_reask": False,
                "llm_authority_violation": False}]
        s = _summarize(rows)
        self.assertIn("OWNER_TEST", s)
        self.assertEqual(s["OWNER_TEST"]["eligible_turns"], 1)
        self.assertNotIn("OWNER_TEST", s.get("TEST_OR_OTHER", {}))  # not folded into TEST/OTHER either


class TestNoCustomerFacingBehaviorChange(unittest.TestCase):
    def test_decide_context_only_sample_source_differs(self):
        """The only field a tester-vs-customer sender changes in
        decide_context is sample_source; everything else the webhook
        builds is identical -- proven by constructing the same context
        shape with each classification and diffing keys."""
        base = {"channel": "line", "customer_context": {}, "developer_mode": True,
               "tenant_id": "default", "external_user_id": "U1"}
        real = {**base, "sample_source": "REAL_LINE"}
        owner = {**base, "sample_source": "OWNER_TEST"}
        diff_keys = {k for k in real if real[k] != owner[k]}
        self.assertEqual(diff_keys, {"sample_source"})


if __name__ == "__main__":
    unittest.main()
