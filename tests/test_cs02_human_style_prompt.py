"""CS-02 — Minimal Human Customer Service Prompt/Policy Integration.

Focused, permanent regression suite for the CS-02 candidate prompt
(tools/seed_cs02_human_style_prompt.py — the single git-tracked source of
truth for its content; a database row alone is never sufficient).

Two kinds of coverage, deliberately never a live LLM call (matching this
repo's own established convention — see e.g. tests/test_decision_engine.py,
which never calls a real model either):

1. CONTENT tests — deterministic checks that the committed constants
   themselves satisfy each CS-01 finding (docs/customer-service/) this
   candidate is supposed to encode. A live-LLM before/after qualitative
   comparison was run once, manually, during CS-02 development (see the
   CS-02 final report) — that kind of check is inherently non-deterministic
   and does not belong in a repeatable CI suite.
2. MECHANISM tests — using the same FakeSb double as tests/test_prompt_studio.py,
   proves the activation/rollback lifecycle this candidate relies on:
   creating a new version is non-destructive, the CS-02 channel-mapping fix
   (services/prompt_builder.py) correctly resolves real LINE traffic
   (channel="line") to whatever is assigned under the admin-facing "LINE OA"
   label, and reassigning/deactivating rolls back cleanly.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import services.prompt_studio_service as pss
import services.prompt_builder as pb
from services.prompt_builder import build_prompt, BASE_CONVERSATION_RULES, STRICT_GROUNDING_RULES, PromptTemplate
from tools.seed_cs02_human_style_prompt import (
    CANDIDATE_NAME, CANDIDATE_SYSTEM_PROMPT, RESPONSE_RULES,
)
from tests.test_prompt_studio import FakeSb

# Terms that must never leak into customer-facing rule text (Section 7.3 /
# CS-01's "no internal system terminology" DON'T item).
_INTERNAL_TERMS = ("Decision Engine", "Business Action", "RAG", "ai_prompt_templates",
                   "Vector DB", "vector database", "embedding")


# ── 1. CONTENT — the committed constants satisfy each CS-01 finding ────────

class TestCandidateResponseRulesContent(unittest.TestCase):
    def test_exactly_the_expected_rule_keys_exist(self):
        expected = {
            "identifier_first", "known_vs_pending", "action_truthfulness",
            "urgency_acknowledgement", "complaint_tone", "emoji_policy",
            "personalization", "customer_reported_vs_verified",
        }
        self.assertEqual(set(RESPONSE_RULES.keys()), expected)

    def test_identifier_first_mentions_the_identifier_types(self):
        rule = RESPONSE_RULES["identifier_first"]
        for term in ("PO", "บิล", "แทรค"):
            self.assertIn(term, rule)

    def test_known_vs_pending_distinguishes_all_three_states(self):
        rule = RESPONSE_RULES["known_vs_pending"]
        self.assertIn("ยังไม่ได้ตรวจสอบ", rule)
        self.assertIn("รอคำตอบ", rule)
        self.assertIn("ได้รับคำตอบแล้ว", rule)

    def test_action_truthfulness_forbids_unearned_supplier_notified_claim(self):
        self.assertIn("แจ้งร้านแล้ว", RESPONSE_RULES["action_truthfulness"])

    def test_action_truthfulness_forbids_unearned_cancellation_claim(self):
        self.assertIn("ยกเลิกเรียบร้อยแล้ว", RESPONSE_RULES["action_truthfulness"])

    def test_action_truthfulness_forbids_unearned_refund_claim(self):
        self.assertIn("คืนเงินเรียบร้อยแล้ว", RESPONSE_RULES["action_truthfulness"])

    def test_action_truthfulness_requires_real_evidence(self):
        self.assertIn("ผลลัพธ์จริง", RESPONSE_RULES["action_truthfulness"])

    def test_action_truthfulness_offers_a_safe_alternative_wording(self):
        rule = RESPONSE_RULES["action_truthfulness"]
        self.assertTrue("ส่งเรื่องให้ตรวจสอบแล้ว" in rule or "กำลังติดตามให้อยู่" in rule)

    def test_urgency_acknowledgement_names_urgency_signals(self):
        rule = RESPONSE_RULES["urgency_acknowledgement"]
        self.assertIn("รีบใช้", rule)
        self.assertIn("ตามมาหลายวันแล้ว", rule)

    def test_urgency_acknowledgement_forbids_generic_cheerfulness(self):
        self.assertIn("ร่าเริง", RESPONSE_RULES["urgency_acknowledgement"])

    def test_complaint_tone_forbids_cheerful_tone(self):
        rule = RESPONSE_RULES["complaint_tone"]
        self.assertIn("ร่าเริง", rule)
        self.assertIn("ไม่ขอโทษซ้ำเกินความจำเป็น", rule.replace(" ", ""))

    def test_emoji_policy_caps_at_zero_or_one(self):
        self.assertIn("0-1", RESPONSE_RULES["emoji_policy"])

    def test_emoji_policy_forbids_emoji_during_every_serious_case_type(self):
        rule = RESPONSE_RULES["emoji_policy"]
        for case_type in ("ร้องเรียน", "ของขาด", "ของผิด", "ของเสียหาย", "ดีเลย์"):
            self.assertIn(case_type, rule)

    def test_personalization_forbids_invented_nicknames(self):
        rule = RESPONSE_RULES["personalization"]
        self.assertIn("ห้ามคิดชื่อเล่น", rule.replace(" ", ""))

    def test_personalization_requires_trusted_existing_data(self):
        self.assertIn("เชื่อถือได้", RESPONSE_RULES["personalization"])

    def test_customer_reported_vs_verified_rule_exists(self):
        """CS-02's own before/after evaluation found an early candidate
        draft restating an unverified customer claim as confirmed truth
        ("จริงๆ") — this rule closes that gap. See the CS-02 final report."""
        rule = RESPONSE_RULES["customer_reported_vs_verified"]
        self.assertIn("จริงๆ", rule)
        self.assertIn("ห้ามใช้คำว่า", rule.replace(" ", ""))

    def test_no_rule_leaks_internal_system_terminology(self):
        for key, text in RESPONSE_RULES.items():
            for term in _INTERNAL_TERMS:
                self.assertNotIn(term, text, f"rule {key!r} must never mention {term!r}")


class TestCandidateSystemPromptContent(unittest.TestCase):
    def test_keeps_the_existing_tone_guidance_block_format(self):
        """Same [TONE GUIDANCE] ... [END TONE GUIDANCE] shape every other
        Prompt Studio template already uses — never a novel format."""
        self.assertIn("[TONE GUIDANCE", CANDIDATE_SYSTEM_PROMPT)
        self.assertIn("[END TONE GUIDANCE]", CANDIDATE_SYSTEM_PROMPT)

    def test_adds_a_traceable_human_cs_guidance_block(self):
        self.assertIn("[HUMAN CUSTOMER SERVICE GUIDANCE", CANDIDATE_SYSTEM_PROMPT)
        self.assertIn("[END HUMAN CUSTOMER SERVICE GUIDANCE]", CANDIDATE_SYSTEM_PROMPT)

    def test_traces_back_to_the_cs_01_documentation(self):
        self.assertIn("docs/customer-service", CANDIDATE_SYSTEM_PROMPT)

    def test_system_prompt_does_not_leak_internal_terminology(self):
        for term in _INTERNAL_TERMS:
            self.assertNotIn(term, CANDIDATE_SYSTEM_PROMPT)

    def test_system_prompt_does_not_override_the_greet_once_policy(self):
        """The candidate must never instruct a greeting on every turn --
        that policy belongs solely to the fixed, platform-wide
        BASE_CONVERSATION_RULES (services/prompt_builder.py), never a
        per-template override."""
        self.assertNotIn("ทักทายทุกครั้ง", CANDIDATE_SYSTEM_PROMPT)
        self.assertNotIn("สวัสดีทุกข้อความ", CANDIDATE_SYSTEM_PROMPT)


# ── 2. RENDERING — the content actually reaches the assembled prompt ───────

class TestCandidateRendersCorrectlyThroughBuildPrompt(unittest.TestCase):
    def _template(self):
        return PromptTemplate(id="cs02-candidate", name=CANDIDATE_NAME, version="2",
                               system_prompt=CANDIDATE_SYSTEM_PROMPT, response_rules=RESPONSE_RULES)

    def test_every_response_rule_key_appears_in_the_final_prompt(self):
        built = build_prompt("test question", "test context", template=self._template())
        for key in RESPONSE_RULES:
            self.assertIn(key, built.final_prompt_text)

    def test_every_response_rule_value_appears_in_the_final_prompt(self):
        built = build_prompt("test question", "test context", template=self._template())
        for value in RESPONSE_RULES.values():
            self.assertIn(value, built.final_prompt_text)

    def test_base_conversation_rules_still_present_and_unmodified(self):
        """The platform-wide layer is never duplicated or overridden by a
        per-template candidate -- it must appear verbatim regardless."""
        built = build_prompt("test question", "test context", template=self._template())
        self.assertIn(BASE_CONVERSATION_RULES, built.final_prompt_text)

    def test_strict_grounding_rules_still_present_when_enabled(self):
        with patch("services.prompt_builder.RAG_GROUNDING_MODE", "strict"):
            built = build_prompt("test question", "test context", template=self._template())
        self.assertIn(STRICT_GROUNDING_RULES, built.final_prompt_text)

    def test_context_and_question_still_placed_after_the_system_message(self):
        built = build_prompt("มีของหรือยัง", "some retrieved context", template=self._template())
        self.assertEqual(built.messages[0]["role"], "system")
        self.assertEqual(built.messages[1]["role"], "user")
        self.assertIn("มีของหรือยัง", built.messages[1]["content"])
        self.assertIn("some retrieved context", built.messages[1]["content"])

    def test_tone_guidance_and_human_cs_guidance_both_reach_the_final_prompt(self):
        built = build_prompt("test question", "test context", template=self._template())
        self.assertIn("[TONE GUIDANCE", built.final_prompt_text)
        self.assertIn("[HUMAN CUSTOMER SERVICE GUIDANCE", built.final_prompt_text)


# ── 3. MECHANISM — non-destructive versioning + the channel-mapping fix ────

class TestCS02ActivationLifecycle(unittest.TestCase):
    """Uses synthetic content standing in for the real CS-02 candidate --
    proves the MECHANISM CS-02 relies on end-to-end: versioning, channel
    resolution for real LINE traffic (channel="line"), and rollback."""

    def setUp(self):
        self.fake_sb = FakeSb()
        patcher1 = patch("services.prompt_studio_service._get_sb", return_value=self.fake_sb)
        patcher1.start()
        self.addCleanup(patcher1.stop)
        patcher2 = patch("services.prompt_builder._get_sb", return_value=self.fake_sb)
        patcher2.start()
        self.addCleanup(patcher2.stop)
        self.svc = pss.PromptStudioService()
        self.old = self.svc.create_prompt({"name": "Current LINE OA Prompt", "system_prompt": "Old text."})
        self.svc.set_default(self.old["id"])

    def test_creating_a_candidate_never_touches_the_old_version(self):
        before = self.svc.get_prompt(self.old["id"])
        self.svc.save_as_new_version(self.old["id"], {"name": "Candidate", "system_prompt": "New text."})
        after = self.svc.get_prompt(self.old["id"])
        self.assertEqual(before["system_prompt"], after["system_prompt"])
        self.assertTrue(after["is_default"])

    def test_candidate_never_auto_serves_on_creation(self):
        candidate = self.svc.save_as_new_version(self.old["id"], {"name": "Candidate", "system_prompt": "New text."})
        self.assertFalse(candidate["is_default"])
        template = pb.get_active_prompt_for_channel("line")
        self.assertEqual(template.system_prompt, "Old text.")

    def test_activation_via_line_oa_assignment_reaches_real_line_traffic(self):
        """The CS-02 channel-mapping fix: real traffic's channel="line" must
        resolve to whatever an admin assigned under the "LINE OA" label."""
        candidate = self.svc.save_as_new_version(self.old["id"], {"name": "Candidate", "system_prompt": "New text."})
        self.svc.assign_channel("LINE OA", candidate["id"])
        template = pb.get_active_prompt_for_channel("line")
        self.assertEqual(template.system_prompt, "New text.")

    def test_rollback_by_reassigning_restores_old_behavior(self):
        candidate = self.svc.save_as_new_version(self.old["id"], {"name": "Candidate", "system_prompt": "New text."})
        self.svc.assign_channel("LINE OA", candidate["id"])
        self.assertEqual(pb.get_active_prompt_for_channel("line").system_prompt, "New text.")

        # Rollback: reassign "LINE OA" back to the old version.
        self.svc.assign_channel("LINE OA", self.old["id"])
        self.assertEqual(pb.get_active_prompt_for_channel("line").system_prompt, "Old text.")

    def test_rollback_by_deactivating_falls_back_to_global_default(self):
        candidate = self.svc.save_as_new_version(self.old["id"], {"name": "Candidate", "system_prompt": "New text."})
        self.svc.assign_channel("LINE OA", candidate["id"])
        self.assertEqual(pb.get_active_prompt_for_channel("line").system_prompt, "New text.")

        # Deactivate the assignment entirely (no reassignment) -- falls
        # back to whichever template is_default, exactly like before any
        # assignment existed.
        self.fake_sb.table("ai_prompt_assignments").update(
            {"is_active": False}).eq("channel", "LINE OA").eq("is_active", True).execute()
        self.assertEqual(pb.get_active_prompt_for_channel("line").system_prompt, "Old text.")

    def test_line_oa_assignment_does_not_affect_an_unrelated_channel(self):
        candidate = self.svc.save_as_new_version(self.old["id"], {"name": "Candidate", "system_prompt": "New text."})
        self.svc.assign_channel("LINE OA", candidate["id"])
        website_template = pb.get_active_prompt_for_channel("Website")
        self.assertEqual(website_template.system_prompt, "Old text.")

    def test_direct_template_id_bypasses_channel_resolution_entirely(self):
        """The AI Playground's own "test a specific template" flow (an
        explicit template_id) must remain unaffected by any LINE OA channel
        assignment -- it never goes through get_active_prompt_for_channel
        at all."""
        candidate = self.svc.save_as_new_version(self.old["id"], {"name": "Candidate", "system_prompt": "New text."})
        self.svc.assign_channel("LINE OA", candidate["id"])
        built = pb.build_prompt("q", "c", template_id=self.old["id"])
        self.assertIn("Old text.", built.final_prompt_text)

    def test_only_one_active_line_oa_assignment_at_a_time(self):
        c1 = self.svc.save_as_new_version(self.old["id"], {"name": "Candidate 1", "system_prompt": "V1 text."})
        c2 = self.svc.save_as_new_version(self.old["id"], {"name": "Candidate 2", "system_prompt": "V2 text."})
        self.svc.assign_channel("LINE OA", c1["id"])
        self.svc.assign_channel("LINE OA", c2["id"])
        active = [a for a in self.fake_sb.store.get("ai_prompt_assignments", [])
                  if a["channel"] == "LINE OA" and a["is_active"]]
        self.assertEqual(len(active), 1)
        self.assertEqual(pb.get_active_prompt_for_channel("line").system_prompt, "V2 text.")


if __name__ == "__main__":
    unittest.main()
