"""CS-03 — Production Human CS Behavior Validation & Final Tuning.

Focused, permanent regression suite for the CS-03 v2 candidate prompt
(tools/seed_cs03_human_style_prompt_v2.py) — covers only the DELTA from
CS-02 (tests/test_cs02_human_style_prompt.py already covers the unchanged
rules, tone block, and rendering mechanism; this file does not repeat that
coverage). See CS-03's own final report for the 53-scenario live evaluation
that found each of these four gaps and confirmed their reproducibility
before this wording change was made.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import services.prompt_studio_service as pss
import services.prompt_builder as pb
from services.prompt_builder import build_prompt, PromptTemplate
from tools.seed_cs02_human_style_prompt import RESPONSE_RULES as CS02_RESPONSE_RULES
from tools.seed_cs03_human_style_prompt_v2 import (
    CANDIDATE_NAME, CANDIDATE_SYSTEM_PROMPT, RESPONSE_RULES, CS02_TEMPLATE_ID,
)
from tests.test_prompt_studio import FakeSb

_INTERNAL_TERMS = ("Decision Engine", "Business Action", "RAG", "ai_prompt_templates",
                   "Vector DB", "vector database", "embedding")


class TestV2ResponseRulesDelta(unittest.TestCase):
    def test_exactly_one_new_key_added(self):
        added = set(RESPONSE_RULES) - set(CS02_RESPONSE_RULES)
        self.assertEqual(added, {"unknown_information_wording"})

    def test_no_key_removed_from_cs02(self):
        self.assertTrue(set(CS02_RESPONSE_RULES).issubset(set(RESPONSE_RULES)))

    def test_unchanged_keys_are_byte_identical_to_cs02(self):
        unchanged = {"identifier_first", "known_vs_pending", "action_truthfulness",
                     "emoji_policy", "personalization"}
        for key in unchanged:
            self.assertEqual(RESPONSE_RULES[key], CS02_RESPONSE_RULES[key])

    def test_unknown_information_wording_gives_natural_phrasing(self):
        rule = RESPONSE_RULES["unknown_information_wording"]
        self.assertIn("ยังไม่มีข้อมูลยืนยันเรื่องนี้ค่ะ", rule)

    def test_unknown_information_wording_forbids_knowledge_base_term(self):
        """CS-03 Category T found the model reliably (4/4 reproductions)
        used 'ฐานความรู้' wording the spec explicitly flags as undesired."""
        rule = RESPONSE_RULES["unknown_information_wording"]
        self.assertIn("ฐานความรู้", rule)
        self.assertIn("ห้ามพูดคำว่า", rule.replace(" ", ""))

    def test_urgency_acknowledgement_still_names_the_original_examples(self):
        rule = RESPONSE_RULES["urgency_acknowledgement"]
        self.assertIn("รีบใช้", rule)
        self.assertIn("ตามมาหลายวันแล้ว", rule)

    def test_urgency_acknowledgement_now_covers_indirect_signals(self):
        """CS-03 Category H found the model reliably (3/3 reproductions)
        skipped acknowledgement for an indirect urgency signal not matching
        either literal example phrase."""
        rule = RESPONSE_RULES["urgency_acknowledgement"]
        self.assertIn("ผลกระทบทางอ้อม", rule)
        self.assertNotEqual(rule, CS02_RESPONSE_RULES["urgency_acknowledgement"])

    def test_complaint_tone_still_forbids_cheerful_tone(self):
        rule = RESPONSE_RULES["complaint_tone"]
        self.assertIn("ร่าเริง", rule)

    def test_complaint_tone_now_covers_inconvenience_complaints(self):
        """CS-03 Category I found the model inconsistently (2/4
        reproductions) skipped acknowledging an inconvenience complaint
        that wasn't phrased as a formal ร้องเรียน/เคลม."""
        rule = RESPONSE_RULES["complaint_tone"]
        self.assertIn("เสียเวลา", rule)
        self.assertNotEqual(rule, CS02_RESPONSE_RULES["complaint_tone"])

    def test_customer_reported_vs_verified_still_forbids_jing_jing(self):
        rule = RESPONSE_RULES["customer_reported_vs_verified"]
        self.assertIn("จริงๆ", rule)

    def test_customer_reported_vs_verified_now_covers_multi_item_cases(self):
        """CS-03 Category L found the model occasionally (1/4
        reproductions) reused the CS-02-fixed 'จริงๆ' wording for a
        multiple-missing-items case CS-02's own test never covered."""
        rule = RESPONSE_RULES["customer_reported_vs_verified"]
        self.assertIn("ของขาดหลายรายการ", rule)
        self.assertIn("ไม่ว่าจะเป็นรายการเดียวหรือหลายรายการ", rule.replace(" ", ""))
        self.assertNotEqual(rule, CS02_RESPONSE_RULES["customer_reported_vs_verified"])

    def test_no_rule_leaks_internal_system_terminology(self):
        for key, text in RESPONSE_RULES.items():
            for term in _INTERNAL_TERMS:
                self.assertNotIn(term, text, f"rule {key!r} must never mention {term!r}")

    def test_system_prompt_unchanged_from_cs02(self):
        """CS-03 is a wording/rules tuning only -- the tone block itself is
        untouched (FACTS CHANGED: NO)."""
        from tools.seed_cs02_human_style_prompt import CANDIDATE_SYSTEM_PROMPT as CS02_SYSTEM_PROMPT
        self.assertEqual(CANDIDATE_SYSTEM_PROMPT, CS02_SYSTEM_PROMPT)


class TestV2RendersCorrectlyThroughBuildPrompt(unittest.TestCase):
    def _template(self):
        return PromptTemplate(id="cs03-v2-candidate", name=CANDIDATE_NAME, version="3",
                               system_prompt=CANDIDATE_SYSTEM_PROMPT, response_rules=RESPONSE_RULES)

    def test_every_response_rule_value_appears_in_the_final_prompt(self):
        built = build_prompt("test question", "test context", template=self._template())
        for value in RESPONSE_RULES.values():
            self.assertIn(value, built.final_prompt_text)


class TestV2VersionsFromCS02NotFromRoot(unittest.TestCase):
    """CS-03 must version FROM the currently-active CS-02 candidate, never
    overwrite it, and must remain rollback-able back to CS-02 specifically
    (not just to the pre-CS-02 global default)."""

    def setUp(self):
        self.fake_sb = FakeSb()
        patcher1 = patch("services.prompt_studio_service._get_sb", return_value=self.fake_sb)
        patcher1.start()
        self.addCleanup(patcher1.stop)
        patcher2 = patch("services.prompt_builder._get_sb", return_value=self.fake_sb)
        patcher2.start()
        self.addCleanup(patcher2.stop)
        self.svc = pss.PromptStudioService()
        self.root = self.svc.create_prompt({"name": "Global Default", "system_prompt": "Root text."})
        self.svc.set_default(self.root["id"])
        self.cs02 = self.svc.save_as_new_version(self.root["id"], {"name": "CS-02 candidate", "system_prompt": "CS-02 text."})
        self.svc.assign_channel("LINE OA", self.cs02["id"])

    def test_v2_creation_never_touches_cs02(self):
        before = self.svc.get_prompt(self.cs02["id"])
        self.svc.save_as_new_version(self.cs02["id"], {"name": "CS-03 v2", "system_prompt": "CS-03 text."})
        after = self.svc.get_prompt(self.cs02["id"])
        self.assertEqual(before["system_prompt"], after["system_prompt"])

    def test_v2_creation_never_auto_serves(self):
        self.svc.save_as_new_version(self.cs02["id"], {"name": "CS-03 v2", "system_prompt": "CS-03 text."})
        self.assertEqual(pb.get_active_prompt_for_channel("line").system_prompt, "CS-02 text.")

    def test_activating_v2_reaches_real_line_traffic(self):
        v2 = self.svc.save_as_new_version(self.cs02["id"], {"name": "CS-03 v2", "system_prompt": "CS-03 text."})
        self.svc.assign_channel("LINE OA", v2["id"])
        self.assertEqual(pb.get_active_prompt_for_channel("line").system_prompt, "CS-03 text.")

    def test_rollback_to_cs02_specifically(self):
        v2 = self.svc.save_as_new_version(self.cs02["id"], {"name": "CS-03 v2", "system_prompt": "CS-03 text."})
        self.svc.assign_channel("LINE OA", v2["id"])
        self.svc.assign_channel("LINE OA", self.cs02["id"])
        self.assertEqual(pb.get_active_prompt_for_channel("line").system_prompt, "CS-02 text.")

    def test_v2_and_cs02_share_the_same_version_lineage(self):
        v2 = self.svc.save_as_new_version(self.cs02["id"], {"name": "CS-03 v2", "system_prompt": "CS-03 text."})
        lineage_ids = {v["id"] for v in self.svc.list_versions(self.cs02["id"])}
        self.assertIn(self.root["id"], lineage_ids)
        self.assertIn(self.cs02["id"], lineage_ids)
        self.assertIn(v2["id"], lineage_ids)


if __name__ == "__main__":
    unittest.main()
