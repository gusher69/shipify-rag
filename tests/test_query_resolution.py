"""Regression tests for rag/query_resolution.py — deterministic (no LLM
call) follow-up resolution. Uses ONLY prior USER turns from `history`,
never a prior ASSISTANT answer (see services/prompt_builder.py's
summarize_history for the other half of that guarantee).

Conversation Resolver 2.0 update: the exact resolved-string format
changed from the original ("<topic><transport>เท่าไหร่", e.g.
"เรททางรถเท่าไหร่") to entity-template phrasing ("ขอเรททางรถ") — a
deliberate behavior upgrade (see rag/query_resolution.py's module
docstring), so the specific expected strings below were updated to
match; the STRUCTURAL guarantees (never contains a prior assistant
answer, unchanged with no history, unchanged for a genuinely new
question) are unchanged and still enforced.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rag.query_resolution import resolve_followup_query


def _hist(*pairs):
    """pairs: (user_text, assistant_text) tuples, oldest first."""
    history = []
    for u, a in pairs:
        history.append({"role": "user", "content": u})
        history.append({"role": "assistant", "content": a})
    return history


class TestFollowupResolution(unittest.TestCase):
    def test_truck_rate_followup_resolves_to_standalone_question(self):
        history = _hist(("ขอเรททางเรือ", "19 บาท/กก. หรือ 4,500 บาท/CBM, 14-20 วัน"))
        resolved = resolve_followup_query("แล้วเรททางรถล่ะ", history)
        self.assertEqual(resolved, "ขอเรททางรถ")

    def test_sea_rate_followup_borrows_previous_topic_head(self):
        history = _hist(("เรททางรถเท่าไหร่", "35 บาท/กก."))
        resolved = resolve_followup_query("แล้วทางเรือล่ะ", history)
        self.assertEqual(resolved, "ขอเรททางเรือ")

    def test_followup_never_contains_previous_assistant_answer(self):
        history = _hist(("ขอเรททางเรือ", "19 บาท/กก. หรือ 4,500 บาท/CBM, 14-20 วัน"))
        resolved = resolve_followup_query("แล้วเรททางรถล่ะ", history)
        self.assertNotIn("บาท", resolved)
        self.assertNotIn("CBM", resolved)

    def test_non_followup_question_is_returned_unchanged(self):
        history = _hist(("ขอเรททางเรือ", "19 บาท/กก."))
        self.assertEqual(resolve_followup_query("จ่ายบิลยังไง", history), "จ่ายบิลยังไง")
        self.assertEqual(resolve_followup_query("ใช้บัตรเครดิตได้ไหม", history), "ใช้บัตรเครดิตได้ไหม")
        self.assertEqual(resolve_followup_query("CBM คืออะไร", history), "CBM คืออะไร")

    def test_followup_with_no_history_is_returned_unchanged(self):
        """No prior user question to resolve against — never guess."""
        self.assertEqual(resolve_followup_query("แล้วเรททางรถล่ะ", None), "แล้วเรททางรถล่ะ")
        self.assertEqual(resolve_followup_query("แล้วเรททางรถล่ะ", []), "แล้วเรททางรถล่ะ")

    def test_previous_question_suffix_is_reused(self):
        history = _hist(("ค่าส่งยังไง", "..."))
        resolved = resolve_followup_query("แล้วค่าประกันล่ะ", history)
        self.assertTrue(resolved.endswith("ยังไง"))

    def test_uses_only_last_user_turn_not_assistant(self):
        """Multiple prior turns — only the LAST user turn (never any
        assistant turn) is used to resolve the follow-up."""
        history = _hist(
            ("ขอเรททางอากาศ", "80 บาท/กก."),
            ("ขอเรททางเรือ", "19 บาท/กก."),
        )
        resolved = resolve_followup_query("แล้วทางรถล่ะ", history)
        self.assertEqual(resolved, "ขอเรททางรถ")


if __name__ == "__main__":
    unittest.main()
