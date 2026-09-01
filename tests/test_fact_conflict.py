"""P1.2B — deterministic source-of-truth conflict guard.

Covers both entry paths:
  PATH 1 — normal RAG context (several active chunks disagree)
  PATH 2 — FAQ exact/near-exact (a duplicate row's answer disagrees, and
           the orchestrator would otherwise short-circuit to faq_direct)

No LLM / no network. Synthetic fixtures only — never touches the DB.
"""
import unittest
from unittest.mock import MagicMock, patch

from rag.fact_conflict import (
    detect_conflicts, detect_conflicts_in_chunks, components_with_conflict,
)
from services.answer_planner import plan_answer
from services.prompt_builder import _build_answer_plan_block


def _ev(*texts):
    return [(f"s{i}", t) for i, t in enumerate(texts)]


class DetectorTrueConflicts(unittest.TestCase):
    def test_A_rate_true_conflict(self):
        r = detect_conflicts(_ev("ทางรถ 35 บาท/กก.", "ทางรถ 36 บาท/กก."))
        self.assertTrue(r.has_conflict())
        self.assertEqual(r.conflicts[0].key, "rate|รถ|kg")
        self.assertEqual(sorted(r.conflicts[0].normalized_values), ["35", "36"])

    def test_D_same_import_duration_conflict(self):
        r = detect_conflicts(_ev("ทางรถใช้เวลา 7–10 วัน", "ทางรถใช้เวลา 6-10 วัน"))
        self.assertTrue(r.has_conflict())
        self.assertEqual(r.conflicts[0].key, "duration|รถ|import")

    def test_F_phone_same_entity_conflict(self):
        r = detect_conflicts(_ev("โกดังนนทบุรี 091-5050-775", "โกดังนนทบุรี 091-5555-775"))
        self.assertTrue(r.has_conflict())
        self.assertEqual(r.conflicts[0].key, "phone|นนทบุรี")


class DetectorFalsePositives(unittest.TestCase):
    def test_B_rate_different_units(self):
        self.assertFalse(detect_conflicts(_ev("ทางรถ 35 บาท/กก.", "ทางรถ 6,900 บาท/CBM")).has_conflict())

    def test_C_different_modes(self):
        self.assertFalse(detect_conflicts(_ev("ทางรถ 7–10 วัน", "ทางเรือ 14–20 วัน")).has_conflict())

    def test_E_different_duration_segments(self):
        self.assertFalse(detect_conflicts(
            _ev("ร้านจีนถึงโกดังจีน 2–4 วัน", "โกดังจีนถึงไทยทางรถ 7–10 วัน")).has_conflict())

    def test_G_phone_format_equivalent(self):
        self.assertFalse(detect_conflicts(
            _ev("โกดังนนทบุรี 091-5050-775", "โกดังนนทบุรี 0915050775")).has_conflict())

    def test_H_different_warehouses(self):
        self.assertFalse(detect_conflicts(
            _ev("โกดังอ่อนนุช 064-224-7205", "โกดังนนทบุรี 091-5050-775")).has_conflict())

    def test_I_different_companies(self):
        self.assertFalse(detect_conflicts(
            _ev("Shipify 02-026-6426", "Fasttrade 02-026-6425")).has_conflict())

    def test_real_both_mode_rate_row_is_clean(self):
        self.assertFalse(detect_conflicts(_ev(
            "ทางรถ 35 บาท/กิโลกรัม หรือ 6,900 บาท/CBM ทางเรือ 19 บาท/กิโลกรัม หรือ 4,500 บาท/CBM"
        )).has_conflict())

    def test_bare_duration_no_segment_not_compared(self):
        # "ยกเลิกภายใน 3–7 วัน" (cancellation window) vs a road import
        # duration — no shared pinned key, never flagged.
        self.assertFalse(detect_conflicts(
            _ev("ยกเลิกคำสั่งซื้อได้ภายใน 3–7 วัน", "ทางเรือถึงไทย 14–20 วัน")).has_conflict())


class FaqDuplicatePath(unittest.TestCase):
    def test_K_duplicate_faq_conflicting_rate_flags(self):
        faq_chunk = {"text": "Question: เรททางรถเท่าไหร่\nAnswer: ทางรถ 35 บาท/กก.",
                      "is_faq_exact": True,
                      "faq_conflict_texts": ["ทางรถ 35 บาท/กก.", "ทางรถ 36 บาท/กก."]}
        r = detect_conflicts_in_chunks([faq_chunk])
        self.assertTrue(r.has_conflict())

    def test_J_duplicate_faq_same_answer_clean(self):
        faq_chunk = {"text": "Question: เรททางรถเท่าไหร่\nAnswer: ทางรถ 35 บาท/กก.",
                      "is_faq_exact": True,
                      "faq_conflict_texts": ["ทางรถ 35 บาท/กก.", "ทางรถ 35 บาท/กิโลกรัม"]}
        self.assertFalse(detect_conflicts_in_chunks([faq_chunk]).has_conflict())

    def test_L_duplicate_faq_complementary_units_clean(self):
        faq_chunk = {"text": "Question: เรททางรถเท่าไหร่\nAnswer: ทางรถ 35 บาท/กก.",
                      "is_faq_exact": True,
                      "faq_conflict_texts": ["ทางรถ 35 บาท/กก.", "ทางรถ 6,900 บาท/CBM"]}
        self.assertFalse(detect_conflicts_in_chunks([faq_chunk]).has_conflict())

    def test_match_faq_exact_returns_eligible_answers(self):
        rows = [
            {"id": "r1", "question": "เรททางรถเท่าไหร่", "alt_questions": [],
             "answer": "ทางรถ 35 บาท/กก.", "sheet_name": "faq", "row_index": 1,
             "chunk_id": "c1", "knowledge_file_id": "f1"},
            {"id": "r2", "question": "เรททางรถเท่าไหร่", "alt_questions": [],
             "answer": "ทางรถ 36 บาท/กก.", "sheet_name": "faq", "row_index": 2,
             "chunk_id": "c2", "knowledge_file_id": "f1"},
        ]
        fake_sb = MagicMock()
        fake_sb.table.return_value.select.return_value.is_.return_value.execute.return_value = \
            MagicMock(data=rows)
        with patch("rag.searcher._get_supabase", return_value=fake_sb):
            from rag.faq_matcher import match_faq_exact
            m = match_faq_exact("เรททางรถเท่าไหร่")
        self.assertIsNotNone(m)
        self.assertIn("ทางรถ 35 บาท/กก.", m["eligible_answers"])
        self.assertIn("ทางรถ 36 บาท/กก.", m["eligible_answers"])
        self.assertTrue(detect_conflicts_in_chunks(
            [{"text": "x", "faq_conflict_texts": m["eligible_answers"]}]).has_conflict())


class ComponentMapping(unittest.TestCase):
    def test_multi_component_subset_flagged(self):
        comps = [("รถ / rate", "q"), ("เรือ / rate", "q")]
        r = detect_conflicts(_ev("ทางรถ 35 บาท/กก.", "ทางรถ 36 บาท/กก."))
        self.assertEqual(components_with_conflict(comps, r), ["รถ / rate"])

    def test_single_component_synthesizes_label(self):
        r = detect_conflicts(_ev("ทางรถ 35 บาท/กก.", "ทางรถ 36 บาท/กก."))
        labels = components_with_conflict([], r)
        self.assertEqual(labels, ["ค่าขนส่งทางรถ (บาท/กก.)"])

    def test_no_conflict_no_labels(self):
        r = detect_conflicts(_ev("ทางรถ 35 บาท/กก.", "ทางเรือ 19 บาท/กก."))
        self.assertEqual(components_with_conflict([("รถ / rate", "q")], r), [])


class PlannerAndPrompt(unittest.TestCase):
    def test_partial_component_safety(self):
        plan = plan_answer("รถกับเรือราคาเท่าไหร่", "unknown", [], {}, chunks=[],
                            requested_components=["รถ / rate", "เรือ / rate"],
                            conflicting_components=["รถ / rate"])
        self.assertEqual(plan["conflicting_components"], ["รถ / rate"])
        self.assertEqual(plan["requested_components"], ["รถ / rate", "เรือ / rate"])

    def test_faq_direct_suppressed_on_conflict(self):
        faq_chunk = {"text": "Question: q\nAnswer: ทางรถ 35 บาท/กก.", "is_faq_exact": True}
        plan = plan_answer("เรททางรถเท่าไหร่", "shipping_rate", ["rate_per_kg"], {},
                            chunks=[faq_chunk], conflicting_components=["ค่าขนส่งทางรถ (บาท/กก.)"])
        self.assertNotEqual(plan["response_shape"], "faq_direct")

    def test_prompt_block_renders_conflict_section(self):
        block = _build_answer_plan_block({
            "answer_goal": "g", "response_shape": "answer_then_details",
            "required_facts": [], "optional_facts": [], "excluded_facts": [],
            "conflicting_components": ["ค่าขนส่งทางรถ (บาท/กก.)"],
        })
        self.assertIn("SOURCE CONFLICT", block)
        self.assertIn("ค่าขนส่งทางรถ (บาท/กก.)", block)
        self.assertIn("not yet confirmed", block)

    def test_clean_plan_has_empty_conflict_list(self):
        plan = plan_answer("เรททางรถเท่าไหร่", "shipping_rate", ["rate_per_kg"],
                            {"transport": "รถ"}, chunks=[])
        self.assertEqual(plan["conflicting_components"], [])


if __name__ == "__main__":
    unittest.main()
