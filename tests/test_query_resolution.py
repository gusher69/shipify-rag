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

from rag.query_resolution import resolve_followup_query, requested_transport_modes


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


class TestGenericRateDurationFollowupDoesNotInheritStaleTransport(unittest.TestCase):
    """Real LINE OA failure (2026-08-31): after "ทางรถใช้เวลากี่วัน", a
    generic "แล้วเรทนำเข้าเท่าไหร่คะ" (no transport in its OWN wording)
    was resolved to the road-only "ขอเรททางรถ", so production answered
    only the road rate. A stale carried transport must not narrow a
    generic rate/duration follow-up; an explicit transport in the
    current turn still wins."""

    def test_generic_rate_followup_after_road_duration_is_not_narrowed(self):
        history = _hist(("ทางรถใช้เวลากี่วัน", "ประมาณ 6–10 วันค่ะ"))
        resolved = resolve_followup_query("แล้วเรทนำเข้าเท่าไหร่คะ", history)
        self.assertNotIn("ทางรถ", resolved)
        self.assertNotIn("ทางเรือ", resolved)

    def test_generic_duration_followup_after_road_rate_is_not_narrowed(self):
        history = _hist(("เรททางรถเท่าไหร่", "35 บาท/กก."))
        resolved = resolve_followup_query("แล้วใช้เวลากี่วัน", history)
        self.assertNotIn("ทางรถ", resolved)
        self.assertNotIn("ทางเรือ", resolved)

    def test_explicit_transport_in_current_turn_still_wins(self):
        history = _hist(("ทางรถใช้เวลากี่วัน", "ประมาณ 6–10 วันค่ะ"))
        self.assertEqual(resolve_followup_query("แล้วเรททางรถเท่าไหร่", history), "ขอเรททางรถ")
        # elliptical, but "ทางเรือ" is in THIS turn's own wording -> narrows
        self.assertIn("ทางเรือ", resolve_followup_query("แล้วทางเรือล่ะ", history))


class TestRequestedTransportModes(unittest.TestCase):
    """Single source of truth for "which transport facet(s) did the
    customer ask about this turn" — consumed by _compose here,
    rag/canonical_query.py and services/answer_planner.py so those
    layers can never disagree."""

    def test_generic_names_none(self):
        self.assertEqual(requested_transport_modes("เรทนำเข้าเท่าไหร่"), [])
        self.assertEqual(requested_transport_modes("แล้วเรทนำเข้าเท่าไหร่คะ"), [])

    def test_single_mode(self):
        self.assertEqual(requested_transport_modes("เรททางรถเท่าไหร่"), ["รถ"])
        self.assertEqual(requested_transport_modes("เรททางเรือเท่าไหร่"), ["เรือ"])

    def test_both_modes(self):
        self.assertEqual(requested_transport_modes("ทางรถกับทางเรือกี่วัน"), ["รถ", "เรือ"])

    def test_samarth_substring_is_not_a_transport_mode(self):
        # "สามารถ" contains "รถ" — must not register as land transport
        self.assertEqual(requested_transport_modes("เราสามารถสั่งแบตเตอรี่จำนวนเยอะได้ไหมคะ"), [])

    def test_negated_mode_is_excluded(self):
        self.assertNotIn("เรือ", requested_transport_modes("ไม่เอาทางเรือ ขอทางรถ"))


class TestContactAttributeFollowup(unittest.TestCase):
    """Subject preservation for an elliptical warehouse-CONTACT follow-up
    (2026-09-01): "ขอเบอร์โกดัง" -> "ไทย" -> "แล้วจีนล่ะ" must keep the
    PHONE/CONTACT subject, not collapse to the warehouse ADDRESS."""

    def test_warehouse_phone_followup_preserves_contact_subject(self):
        history = _hist(("ขอเบอร์โกดัง", "ต้องการเบอร์ติดต่อโกดังไทยหรือโกดังจีนคะ"),
                        ("ไทย", "เบอร์โกดังไทย: อ่อนนุช โทร 064-224-7205 ค่ะ"))
        resolved = resolve_followup_query("แล้วจีนล่ะ", history)
        self.assertIn("เบอร์ติดต่อ", resolved)
        self.assertIn("จีน", resolved)
        self.assertNotIn("ไทย", resolved)

    def test_warehouse_address_followup_still_resolves_to_address(self):
        history = _hist(("ขอที่อยู่โกดัง", "ต้องการที่อยู่โกดังไทยหรือโกดังจีนคะ"),
                        ("ไทย", "มีโกดังไทย 2 ที่นะคะ พิกัด อ่อนนุช 46 ..."))
        resolved = resolve_followup_query("แล้วจีนล่ะ", history)
        self.assertIn("ที่อยู่", resolved)
        self.assertIn("จีน", resolved)
        self.assertNotIn("เบอร์", resolved)

    def test_transport_rate_and_duration_followups_unchanged(self):
        rate_h = _hist(("ทางรถเรทเท่าไหร่", "35 บาท/กก."))
        self.assertIn("ทางเรือ", resolve_followup_query("แล้วเรือล่ะ", rate_h))
        dur_h = _hist(("ทางรถใช้เวลากี่วัน", "7–10 วันค่ะ"))
        self.assertIn("ทางเรือ", resolve_followup_query("แล้วเรือล่ะ", dur_h))


if __name__ == "__main__":
    unittest.main()
