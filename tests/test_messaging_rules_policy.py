"""Regression tests for the AI Policies "Message Segmenter" controls
(Reply Mode / Maximum Messages / Message Delay) — services/
policy_studio_service.py's new "messaging_rules" DEFAULT_CONFIG section
and services/policy_engine.py::get_messaging_settings(). Backward
compatibility: an existing policy set saved before this feature existed
(config with no "messaging_rules" key at all) must still resolve to
sensible defaults, never a missing-key crash.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.policy_studio_service import DEFAULT_CONFIG, _merge_config
from services.policy_engine import get_messaging_settings


class TestDefaults(unittest.TestCase):
    def test_default_config_has_messaging_rules_section(self):
        self.assertIn("messaging_rules", DEFAULT_CONFIG)

    def test_documented_defaults(self):
        rules = DEFAULT_CONFIG["messaging_rules"]
        self.assertEqual(rules["reply_mode"], "auto")
        self.assertEqual(rules["max_messages"], 3)
        self.assertEqual(rules["message_delay"], "natural")


class TestBackwardCompatibility(unittest.TestCase):
    def test_old_config_missing_messaging_rules_entirely_gets_defaults(self):
        """An existing policy set saved before this feature existed —
        `config` has no "messaging_rules" key at all."""
        old_config = {
            "business_rules": {"no_guess_prices": True},
            "escalation_rules": {"enabled": True},
        }
        merged = _merge_config(old_config)
        self.assertEqual(merged["messaging_rules"], DEFAULT_CONFIG["messaging_rules"])

    def test_old_config_with_none_config_gets_defaults(self):
        merged = _merge_config(None)
        self.assertEqual(merged["messaging_rules"]["reply_mode"], "auto")

    def test_partial_messaging_rules_fills_in_missing_keys(self):
        """An admin who only set reply_mode still gets sensible defaults
        for max_messages/message_delay."""
        merged = _merge_config({"messaging_rules": {"reply_mode": "single"}})
        self.assertEqual(merged["messaging_rules"]["reply_mode"], "single")
        self.assertEqual(merged["messaging_rules"]["max_messages"], 3)
        self.assertEqual(merged["messaging_rules"]["message_delay"], "natural")


class TestGetMessagingSettings(unittest.TestCase):
    def test_reads_configured_values(self):
        policy_set = {"config": {"messaging_rules": {"reply_mode": "multi", "max_messages": 2, "message_delay": "short"}}}
        settings = get_messaging_settings(policy_set)
        self.assertEqual(settings, {"reply_mode": "multi", "max_messages": 2, "message_delay": "short"})

    def test_missing_section_falls_back_to_defaults(self):
        policy_set = {"config": {"business_rules": {}}}
        settings = get_messaging_settings(policy_set)
        self.assertEqual(settings["reply_mode"], "auto")
        self.assertEqual(settings["max_messages"], 3)
        self.assertEqual(settings["message_delay"], "natural")

    def test_missing_config_key_entirely_falls_back_to_defaults(self):
        """A non-empty policy_set dict with no "config" key at all —
        distinct from passing {} itself, which would trigger
        get_messaging_settings' OWN `policy_set or get_default_policy_set()`
        fallback (a real DB call) rather than testing this merge path."""
        settings = get_messaging_settings({"id": "some-policy", "name": "Test"})
        self.assertEqual(settings["reply_mode"], "auto")


if __name__ == "__main__":
    unittest.main()
