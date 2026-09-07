"""Regression tests for the built-in "Base Conversation Rules" — a fixed,
platform-standard, read-only block that Prompt Studio no longer requires
authors to hand-write into every template. Covers: it's injected into
EVERY built prompt automatically (including templates with no relation to
this feature — backward compatibility), it always comes first in the
system message, and it's never altered by any template/history/tone.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.prompt_builder import build_prompt, BASE_CONVERSATION_RULES, PromptTemplate

_TEMPLATE = PromptTemplate(id="t1", name="Test", version="1", system_prompt="You are a helpful assistant.")


class TestBaseConversationRulesAlwaysInjected(unittest.TestCase):
    def test_present_verbatim_with_no_history(self):
        built = build_prompt("CBM คืออะไร", "ctx", template=_TEMPLATE, history=None)
        self.assertIn(BASE_CONVERSATION_RULES, built.messages[0]["content"])

    def test_present_verbatim_with_history(self):
        """Backward compatibility: an existing template + an ongoing
        conversation still gets Base Conversation Rules injected exactly
        the same way — nothing in the DB needs to change."""
        history = [{"role": "user", "content": "CBM คืออะไร"}, {"role": "assistant", "content": "..."}]
        built = build_prompt("ขอเรททางเรือ", "ctx", template=_TEMPLATE, history=history)
        self.assertIn(BASE_CONVERSATION_RULES, built.messages[0]["content"])

    def test_identical_text_regardless_of_template_content(self):
        """The block itself never varies — it's a fixed platform constant,
        not per-template, not editable."""
        other_template = PromptTemplate(id="t2", name="Other", version="1",
                                         system_prompt="คุณคือ AI อีกบริษัทหนึ่ง")
        built1 = build_prompt("Q", "ctx", template=_TEMPLATE)
        built2 = build_prompt("Q", "ctx", template=other_template)
        self.assertIn(BASE_CONVERSATION_RULES, built1.messages[0]["content"])
        self.assertIn(BASE_CONVERSATION_RULES, built2.messages[0]["content"])

    def test_contains_core_conversation_invariant_sections(self):
        # PROMPT-STUDIO-BASE-RULES-CLEANUP — trimmed to Core Conversation
        # Invariants. Assert the section headings + the load-bearing rules.
        for heading in ("## Core Conversation Rules", "### Conversation Context",
                        "### Intent & Data Source", "### Business Truth",
                        "### Public & Private Information", "### Fallback",
                        "### Response Safety"):
            self.assertIn(heading, BASE_CONVERSATION_RULES)

    def test_contains_context_priority_and_no_guessing_rules(self):
        self.assertIn("ห้ามใช้คำตอบเก่าของ AI เป็นแหล่งข้อมูลอ้างอิง", BASE_CONVERSATION_RULES)
        self.assertIn("ให้ความสำคัญกับข้อความล่าสุดของลูกค้าสูงสุด", BASE_CONVERSATION_RULES)
        self.assertIn("ห้ามเดา", BASE_CONVERSATION_RULES)
        self.assertIn("ห้ามสร้างข้อมูล", BASE_CONVERSATION_RULES)
        self.assertIn("KB_NOT_FOUND ไม่ได้หมายความว่าบทสนทนาต้องจบ", BASE_CONVERSATION_RULES)
        self.assertIn("ห้ามให้ workflow หรือข้อมูลเก่าที่จบไปแล้วกลับมาควบคุมคำถามใหม่", BASE_CONVERSATION_RULES)


class TestPromptAssemblyOrder(unittest.TestCase):
    def test_base_rules_come_before_customer_system_prompt(self):
        built = build_prompt("Q", "ctx", template=_TEMPLATE)
        system_msg = built.messages[0]["content"]
        self.assertLess(system_msg.index(BASE_CONVERSATION_RULES), system_msg.index(_TEMPLATE.system_prompt))

    def test_base_rules_come_before_active_ai_policies(self):
        built = build_prompt("Q", "ctx", template=_TEMPLATE, policy_notes=["Some policy note"])
        system_msg = built.messages[0]["content"]
        self.assertLess(system_msg.index(BASE_CONVERSATION_RULES), system_msg.index("Some policy note"))

    def test_retrieved_context_and_question_always_last_in_user_message(self):
        built = build_prompt("current question", "retrieved context", template=_TEMPLATE)
        user_msg = built.messages[-1]["content"]
        self.assertIn("retrieved context", user_msg)
        self.assertIn("current question", user_msg)
        self.assertNotIn(BASE_CONVERSATION_RULES, user_msg)


if __name__ == "__main__":
    unittest.main()
