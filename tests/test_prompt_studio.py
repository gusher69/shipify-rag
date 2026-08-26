"""Tests for Prompt Studio (services/prompt_studio_service.py) and the
DB-backed parts of services/prompt_builder.py."""
import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import services.prompt_studio_service as pss
import services.prompt_builder as pb


class FakeQuery:
    def __init__(self, store, name):
        self.store, self.name = store, name
        self._filters = []
        self._limit = None
        self._pending_update = None

    def select(self, *a, **k): return self
    def is_(self, col, val):
        self._filters.append(lambda r: (r.get(col) is None) == (val == "null"))
        return self
    def eq(self, col, val):
        self._filters.append(lambda r: r.get(col) == val)
        return self
    def in_(self, col, values):
        self._filters.append(lambda r: r.get(col) in values)
        return self
    def order(self, *a, **k): return self
    def limit(self, n):
        self._limit = n
        return self

    def insert(self, rows):
        rows = rows if isinstance(rows, list) else [rows]
        inserted = []
        for row in rows:
            row = dict(row)
            row.setdefault("id", str(uuid.uuid4()))
            row.setdefault("version", 1)
            row.setdefault("deleted_at", None)
            self.store.setdefault(self.name, []).append(row)
            inserted.append(row)
        self._result = inserted
        return self

    def update(self, fields):
        self._pending_update = fields
        return self

    def execute(self):
        if hasattr(self, "_result"):
            return MagicMock(data=self._result)
        data = self.store.get(self.name, [])
        for f in self._filters:
            data = [r for r in data if f(r)]
        if self._pending_update:
            for r in data:
                r.update(self._pending_update)
        if self._limit:
            data = data[:self._limit]
        return MagicMock(data=data)


class FakeSb:
    def __init__(self):
        self.store = {}
    def table(self, name):
        return FakeQuery(self.store, name)


class TestPromptCRUD(unittest.TestCase):
    def setUp(self):
        self.fake_sb = FakeSb()
        patcher = patch("services.prompt_studio_service._get_sb", return_value=self.fake_sb)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.svc = pss.PromptStudioService()

    def test_create_prompt(self):
        p = self.svc.create_prompt({"name": "ตอบสุภาพ", "system_prompt": "Be very polite.", "tone": "Formal"})
        self.assertIsNotNone(p)
        self.assertEqual(p["name"], "ตอบสุภาพ")
        self.assertEqual(p["version"], 1)

    def test_list_prompts_search_by_name(self):
        self.svc.create_prompt({"name": "ตอบสุภาพ", "system_prompt": "polite"})
        self.svc.create_prompt({"name": "ตอบสั้น กระชับ", "system_prompt": "concise"})
        results = self.svc.list_prompts(search="สุภาพ")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["name"], "ตอบสุภาพ")

    def test_update_prompt_does_not_bump_version(self):
        p = self.svc.create_prompt({"name": "Test", "system_prompt": "v1 text"})
        updated = self.svc.update_prompt(p["id"], {"system_prompt": "v1 text edited"})
        self.assertEqual(updated["version"], 1)
        self.assertEqual(updated["system_prompt"], "v1 text edited")

    def test_duplicate_prompt_is_an_active_copy(self):
        # Changed from "inactive copy": the simplified customer-facing
        # Prompt Studio UI removed the separate Activate action, so a
        # duplicate must be usable (active) immediately, or it would be
        # invisible in the AI Playground's active-only prompt dropdown
        # with no way to fix it short of Set as Customer Default.
        p = self.svc.create_prompt({"name": "Original", "system_prompt": "hello", "is_active": True})
        dup = self.svc.duplicate_prompt(p["id"])
        self.assertIn("(copy)", dup["name"])
        self.assertTrue(dup["is_active"])
        self.assertNotEqual(dup["id"], p["id"])

    def test_rename_prompt(self):
        p = self.svc.create_prompt({"name": "Old Name", "system_prompt": "x"})
        renamed = self.svc.rename_prompt(p["id"], "New Name")
        self.assertEqual(renamed["name"], "New Name")

    def test_activate_prompt(self):
        p = self.svc.create_prompt({"name": "Test", "system_prompt": "x", "is_active": False})
        self.assertTrue(self.svc.activate_prompt(p["id"]))
        self.assertTrue(self.svc.get_prompt(p["id"])["is_active"])


class TestDefaultAndDeleteSafety(unittest.TestCase):
    def setUp(self):
        self.fake_sb = FakeSb()
        patcher = patch("services.prompt_studio_service._get_sb", return_value=self.fake_sb)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.svc = pss.PromptStudioService()

    def test_set_default_clears_previous_default(self):
        a = self.svc.create_prompt({"name": "A", "system_prompt": "a"})
        b = self.svc.create_prompt({"name": "B", "system_prompt": "b"})
        self.svc.set_default(a["id"])
        self.assertTrue(self.svc.get_prompt(a["id"])["is_default"])
        self.svc.set_default(b["id"])
        self.assertFalse(self.svc.get_prompt(a["id"])["is_default"])
        self.assertTrue(self.svc.get_prompt(b["id"])["is_default"])

    def test_cannot_delete_the_only_default_prompt(self):
        p = self.svc.create_prompt({"name": "Only Default", "system_prompt": "x"})
        self.svc.set_default(p["id"])
        result = self.svc.delete_prompt(p["id"])
        self.assertFalse(result["ok"])
        self.assertIn("Default", result["error"])

    def test_deleting_assigned_prompt_warns_without_force(self):
        p = self.svc.create_prompt({"name": "Assigned", "system_prompt": "x"})
        self.svc.assign_channel("LINE OA", p["id"])
        result = self.svc.delete_prompt(p["id"])
        self.assertFalse(result["ok"])
        self.assertTrue(result.get("warning"))
        self.assertIn("LINE OA", result["error"])

    def test_deleting_assigned_prompt_succeeds_with_force(self):
        p = self.svc.create_prompt({"name": "Assigned", "system_prompt": "x"})
        self.svc.assign_channel("LINE OA", p["id"])
        result = self.svc.delete_prompt(p["id"], force=True)
        self.assertTrue(result["ok"])
        self.assertIsNone(self.svc.get_prompt(p["id"]))

    def test_delete_unassigned_non_default_prompt_succeeds(self):
        p = self.svc.create_prompt({"name": "Plain", "system_prompt": "x"})
        result = self.svc.delete_prompt(p["id"])
        self.assertTrue(result["ok"])


class TestVersioning(unittest.TestCase):
    def setUp(self):
        self.fake_sb = FakeSb()
        patcher = patch("services.prompt_studio_service._get_sb", return_value=self.fake_sb)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.svc = pss.PromptStudioService()

    def test_save_as_new_version_increments_and_keeps_parent(self):
        p = self.svc.create_prompt({"name": "Base", "system_prompt": "v1"})
        v2 = self.svc.save_as_new_version(p["id"], {"system_prompt": "v2 text"})
        self.assertEqual(v2["version"], 2)
        self.assertEqual(v2["parent_id"], p["id"])
        # original v1 row is untouched
        original = self.svc.get_prompt(p["id"])
        self.assertEqual(original["system_prompt"], "v1")

    def test_list_versions_returns_full_lineage(self):
        p = self.svc.create_prompt({"name": "Base", "system_prompt": "v1"})
        v2 = self.svc.save_as_new_version(p["id"], {"system_prompt": "v2"})
        v3 = self.svc.save_as_new_version(v2["id"], {"system_prompt": "v3"})
        versions = self.svc.list_versions(v3["id"])
        self.assertEqual([v["version"] for v in versions], [1, 2, 3])

    def test_rollback_creates_new_version_matching_target_without_deleting_history(self):
        p = self.svc.create_prompt({"name": "Base", "system_prompt": "v1 text"})
        v2 = self.svc.save_as_new_version(p["id"], {"system_prompt": "v2 text — broken"})
        rolled_back = self.svc.rollback(v2["id"], p["id"])
        self.assertEqual(rolled_back["version"], 3)
        self.assertEqual(rolled_back["system_prompt"], "v1 text")
        # v2 still exists, untouched
        self.assertIsNotNone(self.svc.get_prompt(v2["id"]))


class TestChannelAssignment(unittest.TestCase):
    def setUp(self):
        self.fake_sb = FakeSb()
        patcher = patch("services.prompt_studio_service._get_sb", return_value=self.fake_sb)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.svc = pss.PromptStudioService()

    def test_assign_channel_only_one_active_per_channel(self):
        p1 = self.svc.create_prompt({"name": "A", "system_prompt": "a"})
        p2 = self.svc.create_prompt({"name": "B", "system_prompt": "b"})
        self.svc.assign_channel("LINE OA", p1["id"])
        self.svc.assign_channel("LINE OA", p2["id"])
        active = [a for a in self.svc.list_assignments() if a["channel"] == "LINE OA" and a["is_active"]]
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["prompt_template_id"], p2["id"])

    def test_assign_channel_rejects_unknown_channel(self):
        p = self.svc.create_prompt({"name": "A", "system_prompt": "a"})
        result = self.svc.assign_channel("Nonexistent Channel", p["id"])
        self.assertFalse(result["ok"])


class TestPromptBuilderDBIntegration(unittest.TestCase):
    def setUp(self):
        self.fake_sb = FakeSb()
        patcher1 = patch("services.prompt_studio_service._get_sb", return_value=self.fake_sb)
        patcher1.start()
        self.addCleanup(patcher1.stop)
        patcher2 = patch("services.prompt_builder._get_sb", return_value=self.fake_sb)
        patcher2.start()
        self.addCleanup(patcher2.stop)
        self.svc = pss.PromptStudioService()

    def test_get_active_prompt_for_channel_uses_assignment(self):
        p = self.svc.create_prompt({"name": "ตอบใส่ใจมาก ๆ", "system_prompt": "Be caring."})
        self.svc.assign_channel("LINE OA", p["id"])
        template = pb.get_active_prompt_for_channel("LINE OA")
        self.assertEqual(template.name, "ตอบใส่ใจมาก ๆ")
        self.assertEqual(template.system_prompt, "Be caring.")

    def test_get_active_prompt_for_channel_falls_back_to_global_default(self):
        g = self.svc.create_prompt({"name": "Global Default", "system_prompt": "Global text"})
        self.svc.set_default(g["id"])
        template = pb.get_active_prompt_for_channel("LINE OA")  # never assigned
        self.assertEqual(template.name, "Global Default")

    def test_internal_line_channel_resolves_the_line_oa_assignment(self):
        """CS-02 (2026-08-26) regression: the real LINE webhook's runtime
        channel value is the raw string "line" (line_bot/webhook.py), never
        the admin-facing Prompt Studio label "LINE OA" — confirmed live that
        before this fix, an admin's "LINE OA" assignment could never take
        effect for real production traffic. get_active_prompt_for_channel
        must resolve "line" to whatever is assigned under "LINE OA"."""
        p = self.svc.create_prompt({"name": "Human CS Style", "system_prompt": "Be human."})
        self.svc.assign_channel("LINE OA", p["id"])
        template = pb.get_active_prompt_for_channel("line")
        self.assertEqual(template.name, "Human CS Style")
        self.assertEqual(template.system_prompt, "Be human.")

    def test_internal_line_channel_mapping_never_leaks_to_other_channels(self):
        """The "line" -> "LINE OA" mapping must be specific to this one
        value — an unrelated channel string must still resolve literally,
        never accidentally redirected."""
        p = self.svc.create_prompt({"name": "Website Prompt", "system_prompt": "Website text."})
        self.svc.assign_channel("Website", p["id"])
        template = pb.get_active_prompt_for_channel("Website")
        self.assertEqual(template.name, "Website Prompt")

    def test_build_prompt_includes_response_and_safety_rules(self):
        p = self.svc.create_prompt({
            "name": "Rules Test", "system_prompt": "Base prompt.",
            "response_rules": {"max_length": "short"}, "safety_rules": {"no_pricing": "never quote prices"},
        })
        built = pb.build_prompt("test question", "some context", template_id=p["id"])
        self.assertIn("max_length", built.final_prompt_text)
        self.assertIn("no_pricing", built.final_prompt_text)

    def test_different_prompts_produce_different_final_prompts(self):
        polite = self.svc.create_prompt({"name": "Polite", "system_prompt": "Be very formal and polite."})
        caring = self.svc.create_prompt({"name": "Caring", "system_prompt": "Be warm and empathetic."})
        built_polite = pb.build_prompt("q", "ctx", template_id=polite["id"])
        built_caring = pb.build_prompt("q", "ctx", template_id=caring["id"])
        self.assertNotEqual(built_polite.final_prompt_text, built_caring.final_prompt_text)
        self.assertIn("formal and polite", built_polite.final_prompt_text)
        self.assertIn("warm and empathetic", built_caring.final_prompt_text)


if __name__ == "__main__":
    unittest.main()
