"""Regression tests for Canonical Query Rewrite (rag/canonical_query.py)
— deterministic (no LLM call) standardization of an already spell-
corrected/resolved question into a clear standalone search query, using
detected intent (attribute/transport/location extraction reused from
rag/query_resolution.py) and carried conversation entities. Never
invents an entity that wasn't actually present in the current wording or
already-resolved conversation state.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rag.canonical_query import rewrite_canonical_query, MIN_REWRITE_CONFIDENCE


class TestDocumentedExamples(unittest.TestCase):
    def test_truck_rate_question(self):
        r = rewrite_canonical_query("รถเท่าไหร่")
        self.assertEqual(r["canonical_query"], "อัตราค่าขนส่งทางรถเท่าไหร่")
        self.assertTrue(r["rewrite_applied"])

    def test_sea_rate_question(self):
        r = rewrite_canonical_query("ขอเรทเรือ")
        self.assertEqual(r["canonical_query"], "อัตราค่าขนส่งทางเรือเท่าไหร่")
        self.assertTrue(r["rewrite_applied"])

    def test_warehouse_location_question_with_carried_entities(self):
        """Without any conversation context, "ส่งแผนที่ให้หน่อย" doesn't
        name a warehouse at all — nothing to invent, so no rewrite. WITH
        carried entities (topic=โกดัง, location=ไทย, from Conversation
        Resolver 2.0), it composes the full canonical form."""
        r_no_context = rewrite_canonical_query("ส่งแผนที่ให้หน่อย")
        self.assertFalse(r_no_context["rewrite_applied"])
        self.assertEqual(r_no_context["canonical_query"], "ส่งแผนที่ให้หน่อย")

        r_with_context = rewrite_canonical_query(
            "ส่งแผนที่ให้หน่อย", entities={"topic": "โกดัง", "location": "ไทย"})
        self.assertEqual(r_with_context["canonical_query"], "ขอที่อยู่และแผนที่โกดังไทย")
        self.assertTrue(r_with_context["rewrite_applied"])

    def test_bare_warehouse_location_question(self):
        r = rewrite_canonical_query("โกดังอยู่ไหน")
        self.assertTrue(r["rewrite_applied"])
        self.assertIn("โกดัง", r["canonical_query"])
        self.assertIn("แผนที่", r["canonical_query"])


class TestSafety(unittest.TestCase):
    def test_original_query_never_lost(self):
        """The pre-rewrite question is always derivable — rewrite_applied
        is False whenever nothing changed, and canonical_query equals
        the input in that case."""
        r = rewrite_canonical_query("จ่ายบิลยังไง")
        self.assertFalse(r["rewrite_applied"])
        self.assertEqual(r["canonical_query"], "จ่ายบิลยังไง")

    def test_never_invents_entities_not_present(self):
        """No transport/location/topic anywhere (current text or carried
        entities) -> no rewrite, ever."""
        r = rewrite_canonical_query("สวัสดีค่ะ วันนี้อากาศดีมาก")
        self.assertFalse(r["rewrite_applied"])

    def test_confidence_floor_is_respected(self):
        self.assertGreaterEqual(MIN_REWRITE_CONFIDENCE, 0.5)

    def test_empty_question(self):
        r = rewrite_canonical_query("")
        self.assertFalse(r["rewrite_applied"])
        self.assertEqual(r["canonical_query"], "")

    def test_current_wording_wins_over_carried_entities(self):
        """Carried entities only fill what's MISSING — the current
        question's own transport/location always takes priority."""
        r = rewrite_canonical_query("รถเท่าไหร่", entities={"transport": "เรือ"})
        self.assertEqual(r["canonical_query"], "อัตราค่าขนส่งทางรถเท่าไหร่")


if __name__ == "__main__":
    unittest.main()
