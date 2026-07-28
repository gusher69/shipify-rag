"""Regression tests for the reported bug: conversation history
contaminating RAG answers — the model sometimes answered the PREVIOUS
question instead of the current one, because build_prompt() used to
inject the entire conversation history verbatim as separate chat
messages positioned immediately before the new question (a strong
"continue this" signal LLMs weight heavily).

services/prompt_builder.py::build_prompt() is the ONLY file this bug fix
touches. These tests assert:
  1. Raw verbatim history is NEVER present as separate chat messages.
  2. High/Medium retrieval confidence suppresses history entirely.
  3. Low/unknown confidence includes only a short, summarized version —
     never a full verbatim previous answer.
  4. Message/prompt order is always System -> [summary] -> Context -> Question.
  5. Question A's answer never appears verbatim in Question B's prompt.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.prompt_builder import (
    build_prompt, summarize_history, _should_suppress_history,
    HIGH_CONFIDENCE_THRESHOLD, MEDIUM_CONFIDENCE_THRESHOLD, PromptTemplate,
)

_TEMPLATE = PromptTemplate(id="t1", name="Test", version="1", system_prompt="You are a helpful assistant.")

# The exact reported scenario.
QUESTION_A = "พัฒนาเฉพาะภาษาอะไร"
# Deliberately NOT a generic phrase like "ไม่มีข้อมูล" — that string
# legitimately appears inside services/prompt_builder.py's fixed
# BASE_CONVERSATION_RULES text ("หากไม่มีข้อมูล ให้แจ้งตามความจริง"), which is
# now injected into every prompt, so it would collide with these
# "never appears" assertions for reasons unrelated to history leakage.
ANSWER_A = "รองรับเฉพาะภาษาไทยและอังกฤษเท่านั้น"
QUESTION_B = "ระยะเวลา Develop กี่วัน"
HISTORY_A_THEN_B = [
    {"role": "user", "content": QUESTION_A},
    {"role": "assistant", "content": ANSWER_A},
]


class TestMessagesNeverContainRawHistory(unittest.TestCase):
    def test_only_system_and_user_messages_are_ever_produced(self):
        """No matter what history is passed, build_prompt() must never
        emit a separate assistant/user message per prior turn — exactly
        2 messages (system, user), always."""
        built = build_prompt(QUESTION_B, "some context", template=_TEMPLATE,
                              history=HISTORY_A_THEN_B, retrieval_confidence=None)
        roles = [m["role"] for m in built.messages]
        self.assertEqual(roles, ["system", "user"])

    def test_previous_assistant_answer_never_appears_as_its_own_message(self):
        """No confidence signal falls back to the summarized-history path
        (same as Low confidence) — ANSWER_A may appear inside the short,
        clearly-labeled summary line, but never as its own standalone
        assistant message (there is no such message at all: roles are
        always exactly [system, user])."""
        built = build_prompt(QUESTION_B, "some context", template=_TEMPLATE,
                              history=HISTORY_A_THEN_B, retrieval_confidence=None)
        roles = [m["role"] for m in built.messages]
        self.assertEqual(roles, ["system", "user"])
        user_msg = built.messages[-1]["content"]
        if ANSWER_A in user_msg:
            self.assertIn(f"Q: {QUESTION_A}", user_msg)
            self.assertIn("for background only", user_msg)


class TestHighMediumConfidenceSuppressesHistory(unittest.TestCase):
    def test_high_confidence_suppresses_history_entirely(self):
        built = build_prompt(QUESTION_B, "retrieved context here", template=_TEMPLATE,
                              history=HISTORY_A_THEN_B, retrieval_confidence=HIGH_CONFIDENCE_THRESHOLD + 0.1)
        user_msg = built.messages[-1]["content"]
        self.assertNotIn(QUESTION_A, user_msg)
        self.assertNotIn("Conversation summary", user_msg)

    def test_medium_confidence_suppresses_history_entirely(self):
        built = build_prompt(QUESTION_B, "retrieved context here", template=_TEMPLATE,
                              history=HISTORY_A_THEN_B, retrieval_confidence=MEDIUM_CONFIDENCE_THRESHOLD)
        user_msg = built.messages[-1]["content"]
        self.assertNotIn("Conversation summary", user_msg)

    def test_low_confidence_includes_a_summary_not_raw_history(self):
        built = build_prompt(QUESTION_B, "retrieved context here", template=_TEMPLATE,
                              history=HISTORY_A_THEN_B, retrieval_confidence=0.1)
        user_msg = built.messages[-1]["content"]
        self.assertIn("Conversation summary", user_msg)
        # The summary line contains the truncated question, but never the
        # full previous answer as a standalone verbatim assistant turn.
        self.assertIn(QUESTION_A, user_msg)

    def test_no_confidence_signal_falls_back_to_summarized_history(self):
        """Callers that don't pass retrieval_confidence at all (e.g. not
        yet wired through some future call site) still get the SAFE
        summarized path, never the old verbatim-history behavior."""
        built = build_prompt(QUESTION_B, "ctx", template=_TEMPLATE,
                              history=HISTORY_A_THEN_B, retrieval_confidence=None)
        self.assertFalse(_should_suppress_history(None))
        user_msg = built.messages[-1]["content"]
        self.assertIn("Conversation summary", user_msg)


class TestPromptOrder(unittest.TestCase):
    def test_context_always_immediately_precedes_question(self):
        built = build_prompt(QUESTION_B, "THE_RETRIEVED_CONTEXT", template=_TEMPLATE,
                              history=HISTORY_A_THEN_B, retrieval_confidence=0.1)
        user_msg = built.messages[-1]["content"]
        context_idx = user_msg.index("THE_RETRIEVED_CONTEXT")
        question_idx = user_msg.index(QUESTION_B)
        self.assertLess(context_idx, question_idx)
        # nothing except the question text itself sits between the start
        # of the Context block and the question — no history block
        # squeezed in between them.
        between = user_msg[context_idx:question_idx]
        self.assertNotIn(QUESTION_A, between)

    def test_system_message_is_always_first(self):
        built = build_prompt(QUESTION_B, "ctx", template=_TEMPLATE, history=HISTORY_A_THEN_B)
        self.assertEqual(built.messages[0]["role"], "system")

    def test_final_prompt_text_shows_system_then_user_only(self):
        built = build_prompt(QUESTION_B, "ctx", template=_TEMPLATE, history=HISTORY_A_THEN_B,
                              retrieval_confidence=0.9)
        self.assertTrue(built.final_prompt_text.startswith("[SYSTEM]"))
        self.assertIn("[USER]", built.final_prompt_text)
        # Old format had per-turn [USER]/[ASSISTANT] blocks for history —
        # confirm there's exactly one [USER] marker now.
        self.assertEqual(built.final_prompt_text.count("[USER]"), 1)
        self.assertNotIn("[ASSISTANT]", built.final_prompt_text)


class TestQuestionBNeverAnswersQuestionA(unittest.TestCase):
    """The literal scenario from the bug report: verify Question B's
    built prompt can never cause the model to continue/repeat Question
    A's answer, across every confidence tier."""

    def test_high_medium_confidence_answer_a_is_never_present_at_all(self):
        """At High/Medium confidence, history is suppressed entirely — no
        trace of Question/Answer A, not even a summary."""
        for confidence in (MEDIUM_CONFIDENCE_THRESHOLD, 0.5, HIGH_CONFIDENCE_THRESHOLD, 0.9, 1.0):
            with self.subTest(confidence=confidence):
                built = build_prompt(QUESTION_B, "ระยะเวลาพัฒนา 30 วัน", template=_TEMPLATE,
                                      history=HISTORY_A_THEN_B, retrieval_confidence=confidence)
                full_text = " ".join(m["content"] for m in built.messages)
                self.assertNotIn(ANSWER_A, full_text)
                self.assertNotIn(QUESTION_A, full_text)
                self.assertIn(QUESTION_B, full_text)

    def test_low_confidence_answer_a_never_treated_as_the_answer_to_question_b(self):
        """At Low confidence a short summary IS allowed (per spec: "if
        history is needed, summarize it") — but per the follow-up
        revision ("the final LLM answer must never see old assistant
        responses verbatim"), that summary now NEVER includes A's answer
        at all, only A's own question as background topic color."""
        built = build_prompt(QUESTION_B, "ระยะเวลาพัฒนา 30 วัน", template=_TEMPLATE,
                              history=HISTORY_A_THEN_B, retrieval_confidence=0.1)
        user_msg = built.messages[-1]["content"]
        self.assertIn(QUESTION_A, user_msg)
        self.assertNotIn(ANSWER_A, user_msg)
        self.assertIn("for background only", user_msg)
        # Question B's own question/context still appear, after the summary.
        self.assertIn(QUESTION_B, user_msg)
        self.assertLess(user_msg.index(QUESTION_A), user_msg.index(QUESTION_B))

    def test_multiple_prior_turns_are_truncated_not_reproduced_in_full(self):
        long_answer_1 = "คำตอบที่ 1 แบบยาวมากและมีรายละเอียดเยอะจนเกินขนาดสรุปสั้นๆ ที่อนุญาตไว้อย่างแน่นอนเพราะยาวเกินไปมาก"
        long_answer_2 = "คำตอบที่ 2 แบบยาวมากและมีรายละเอียดเยอะเช่นกันจนเกินขนาดสรุปสั้นๆ ที่อนุญาตไว้อย่างแน่นอนเพราะยาวเกินไปมาก"
        long_history = [
            {"role": "user", "content": "คำถามที่ 1"},
            {"role": "assistant", "content": long_answer_1},
            {"role": "user", "content": "คำถามที่ 2"},
            {"role": "assistant", "content": long_answer_2},
            {"role": "user", "content": QUESTION_A},
            {"role": "assistant", "content": ANSWER_A},
        ]
        built = build_prompt(QUESTION_B, "ctx", template=_TEMPLATE,
                              history=long_history, retrieval_confidence=0.1)
        full_text = " ".join(m["content"] for m in built.messages)
        # The FULL long answers must never be reproduced verbatim — only
        # a truncated prefix (the "short conversation summary" the spec
        # requires), never the complete original text.
        self.assertNotIn(long_answer_1, full_text)
        self.assertNotIn(long_answer_2, full_text)


class TestSummarizeHistory(unittest.TestCase):
    def test_empty_history_returns_empty_string(self):
        self.assertEqual(summarize_history(None), "")
        self.assertEqual(summarize_history([]), "")

    def test_truncates_long_turns(self):
        long_q = "ก" * 200
        long_a = "ข" * 200
        summary = summarize_history([{"role": "user", "content": long_q},
                                       {"role": "assistant", "content": long_a}])
        self.assertLess(len(summary), len(long_q) + len(long_a))

    def test_never_includes_assistant_content(self):
        """summarize_history() only ever lists prior USER topics — an
        assistant answer, even short, must never appear."""
        summary = summarize_history(HISTORY_A_THEN_B)
        self.assertIn(QUESTION_A, summary)
        self.assertNotIn(ANSWER_A, summary)


class TestBackwardCompatibility(unittest.TestCase):
    def test_build_prompt_works_without_retrieval_confidence_kwarg(self):
        """Existing callers that don't pass retrieval_confidence at all
        must not break — the parameter is optional."""
        built = build_prompt(QUESTION_B, "ctx", template=_TEMPLATE)
        self.assertEqual(built.question, QUESTION_B)
        self.assertEqual(len(built.messages), 2)

    def test_build_prompt_works_with_no_history_at_all(self):
        built = build_prompt(QUESTION_B, "ctx", template=_TEMPLATE, history=None,
                              retrieval_confidence=0.9)
        self.assertEqual(len(built.messages), 2)
        self.assertNotIn("Conversation summary", built.messages[-1]["content"])


if __name__ == "__main__":
    unittest.main()
