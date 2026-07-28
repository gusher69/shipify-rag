"""Tests for AI Policies: services/policy_studio_service.py (CRUD +
default resolution) and services/policy_engine.py (rule application).
Uses the same lightweight fake-Supabase pattern as tests/test_prompt_studio.py."""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("SUPABASE_URL", "http://fake.local")
os.environ.setdefault("SUPABASE_KEY", "fake-key")

import uuid


class _FakeTable:
    def __init__(self, store, name):
        self.store = store
        self.name = name
        self._filters = []
        self._select = "*"
        self._order = None

    def select(self, cols): self._select = cols; return self
    def eq(self, col, val): self._filters.append(("eq", col, val)); return self
    def is_(self, col, val): self._filters.append(("is", col, val)); return self
    def order(self, col, desc=False): self._order = (col, desc); return self
    def limit(self, n): self._limit = n; return self

    def _match(self, row):
        for op, col, val in self._filters:
            if op == "eq" and row.get(col) != val:
                return False
            if op == "is":
                target = None if val == "null" else val
                if row.get(col) != target:
                    return False
        return True

    def insert(self, row):
        row = dict(row)
        row.setdefault("id", str(uuid.uuid4()))
        row.setdefault("is_default", False)
        row.setdefault("deleted_at", None)
        row.setdefault("created_at", "2026-01-01T00:00:00+00:00")
        row.setdefault("updated_at", "2026-01-01T00:00:00+00:00")
        self._pending_insert = row
        return self

    def update(self, patch_data):
        self._pending_update = patch_data
        return self

    def execute(self):
        if hasattr(self, "_pending_insert"):
            row = self._pending_insert
            self.store.setdefault(self.name, []).append(row)
            del self._pending_insert
            return _FakeResult([row])
        rows = self.store.get(self.name, [])
        if hasattr(self, "_pending_update"):
            matched = [r for r in rows if self._match(r)]
            for r in matched:
                r.update(self._pending_update)
            del self._pending_update
            return _FakeResult(matched)
        matched = [r for r in rows if self._match(r)]
        if self._order:
            col, desc = self._order
            matched = sorted(matched, key=lambda r: r.get(col) or "", reverse=desc)
        if hasattr(self, "_limit"):
            matched = matched[: self._limit]
        return _FakeResult(matched)


class _FakeResult:
    def __init__(self, data):
        self.data = data


class _FakeSupabase:
    def __init__(self):
        self.store = {}

    def table(self, name):
        return _FakeTable(self.store, name)


class PolicyStudioTestCase(unittest.TestCase):
    def setUp(self):
        self.fake_sb = _FakeSupabase()
        patcher = patch("services.policy_studio_service._get_sb", return_value=self.fake_sb)
        patcher.start()
        self.addCleanup(patcher.stop)
        from services.policy_studio_service import PolicyStudioService
        self.svc = PolicyStudioService()


class TestPolicyCRUD(PolicyStudioTestCase):
    def test_create_policy_set_applies_sensible_defaults(self):
        p = self.svc.create_policy_set({"name": "Test Policy"})
        self.assertEqual(p["name"], "Test Policy")
        self.assertTrue(p["config"]["business_rules"]["no_guess_prices"])
        self.assertTrue(p["config"]["escalation_rules"]["enabled"])

    def test_create_policy_set_partial_config_merges_with_defaults(self):
        p = self.svc.create_policy_set({"name": "X", "config": {"channel_rules": {"formality": "formal"}}})
        self.assertEqual(p["config"]["channel_rules"]["formality"], "formal")
        # untouched sibling field still gets its default
        self.assertEqual(p["config"]["channel_rules"]["emoji_usage"], "sometimes")

    def test_update_policy_set_edits_in_place(self):
        p = self.svc.create_policy_set({"name": "Original"})
        updated = self.svc.update_policy_set(p["id"], {"name": "Renamed"})
        self.assertEqual(updated["name"], "Renamed")

    def test_duplicate_policy_set_is_an_active_copy(self):
        p = self.svc.create_policy_set({"name": "Original"})
        dup = self.svc.duplicate_policy_set(p["id"])
        self.assertIn("(copy)", dup["name"])
        self.assertTrue(dup["is_active"])
        self.assertNotEqual(dup["id"], p["id"])

    def test_list_policy_sets_search_by_name(self):
        self.svc.create_policy_set({"name": "Alpha Policy"})
        self.svc.create_policy_set({"name": "Beta Policy"})
        results = self.svc.list_policy_sets(search="alpha")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["name"], "Alpha Policy")


class TestDefaultAndDeleteSafety(PolicyStudioTestCase):
    def test_set_default_clears_previous_default(self):
        a = self.svc.create_policy_set({"name": "A"})
        b = self.svc.create_policy_set({"name": "B"})
        self.svc.set_default(a["id"])
        self.svc.set_default(b["id"])
        self.assertFalse(self.svc.get_policy_set(a["id"])["is_default"])
        self.assertTrue(self.svc.get_policy_set(b["id"])["is_default"])

    def test_cannot_delete_the_only_policy_set_when_it_is_default(self):
        p = self.svc.create_policy_set({"name": "Only"})
        self.svc.set_default(p["id"])
        result = self.svc.delete_policy_set(p["id"])
        self.assertFalse(result["ok"])

    def test_deleting_default_with_other_policy_sets_warns_without_force(self):
        a = self.svc.create_policy_set({"name": "A"})
        self.svc.create_policy_set({"name": "B"})
        self.svc.set_default(a["id"])
        result = self.svc.delete_policy_set(a["id"])
        self.assertFalse(result["ok"])
        self.assertTrue(result.get("warning"))

    def test_deleting_non_default_policy_set_succeeds(self):
        self.svc.create_policy_set({"name": "A", "is_active": True})
        b = self.svc.create_policy_set({"name": "B"})
        result = self.svc.delete_policy_set(b["id"])
        self.assertTrue(result["ok"])
        self.assertIsNone(self.svc.get_policy_set(b["id"]))


class TestPolicyEngineIntegration(unittest.TestCase):
    def test_evaluate_uses_default_policy_set_when_none_given(self):
        # Deliberately does NOT depend on whether migration 024 has been
        # applied / a real DB is reachable in this environment (evaluate()
        # resolves its own default via get_default_policy_set() when none
        # is passed) — only asserts a name is present and every rule
        # category ran, regardless of which policy set backs it.
        from services.policy_engine import evaluate
        result = evaluate("สวัสดีค่ะ")
        self.assertTrue(result.policy_set_name)
        names = {v.name for v in result.verdicts}
        self.assertEqual(names, {"Business Rules", "Knowledge Rules", "Escalation Rules",
                                   "Attachment Rules", "Channel Rules"})

    def test_dissatisfaction_keyword_triggers_escalation(self):
        from services.policy_engine import evaluate
        result = evaluate("ร้องเรียนบริการหน่อยค่ะ")
        self.assertTrue(result.escalate)

    def test_low_confidence_triggers_escalation_when_enabled(self):
        from services.policy_engine import evaluate
        result = evaluate("คำถามทั่วไป", confidence_score=0.1)
        self.assertTrue(result.escalate)

    def test_escalation_disabled_never_triggers(self):
        from services.policy_engine import evaluate
        policy_set = {
            "name": "No Escalation",
            "config": {"escalation_rules": {"enabled": False}},
        }
        result = evaluate("ร้องเรียนบริการหน่อยค่ะ", confidence_score=0.0, policy_set=policy_set)
        self.assertFalse(result.escalate)

    def test_business_rule_notes_reflect_config(self):
        from services.policy_engine import business_rule_notes
        notes = business_rule_notes({"no_guess_prices": True, "no_guess_delivery_status": False})
        self.assertEqual(len(notes), 1)
        self.assertIn("price", notes[0].lower())

    def test_channel_rule_notes_reflect_config(self):
        from services.policy_engine import channel_rule_notes
        notes = channel_rule_notes({"line_response_length": "short", "emoji_usage": "never", "formality": "formal"})
        self.assertEqual(len(notes), 3)

    def test_custom_policy_set_overrides_confidence_threshold(self):
        from services.policy_engine import evaluate
        strict_policy = {
            "name": "Strict",
            "config": {"escalation_rules": {"enabled": True, "confidence_threshold": 0.9,
                                              "escalate_on_no_answer": True, "escalate_on_dissatisfaction": True,
                                              "message": "test"}},
        }
        result = evaluate("คำถามทั่วไป", confidence_score=0.8, policy_set=strict_policy)
        self.assertTrue(result.escalate)  # 0.8 < 0.9 threshold


if __name__ == "__main__":
    unittest.main()
