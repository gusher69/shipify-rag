"""Regression tests for the Active Slot-Filling Flow resolver (P0,
2026-07-22): a bare reply like "4*6" supplying dimensions for an active
shipping-cost-calculation flow must never be misclassified as a
standalone arithmetic question ("4*6 = 24").
"""
import unittest

from rag.slot_filling_flow import (
    resolve_slot_filling_turn, resolve_bare_math_expression, detect_active_flow,
    parse_dimension_input, is_cancellation, is_explicit_math_intent,
)

ASSISTANT_ASK = (
    "ค่าขนส่งคำนวณจากปริมาตรและน้ำหนัก โดยค่าไหนมากกว่าจะถูกใช้เป็นค่าขนส่งค่ะ\n"
    "- ทางเรือ: 4,500 บาท/CBM หรือ 19 บาท/กก.\n- ทางรถ: 6,900 บาท/CBM หรือ 35 บาท/กก.\n"
    "รบกวนขอทราบขนาดและน้ำหนักของสินค้าด้วยนะคะ เพื่อจะได้คำนวณค่าส่งให้ค่ะ"
)


def _history(*turns):
    """turns: alternating (role, content) tuples."""
    return [{"role": r, "content": c} for r, c in turns]


class TestActiveFlowOutranksMath(unittest.TestCase):
    """1, 2."""

    def test_active_flow_outranks_math_intent_for_bare_expression(self):
        history = _history(("assistant", ASSISTANT_ASK))
        r = resolve_slot_filling_turn("4*6", history)
        self.assertIsNotNone(r)
        self.assertFalse(r["flow_complete"])

    def test_4x6_becomes_partial_dimensions_not_24(self):
        history = _history(("assistant", ASSISTANT_ASK))
        r = resolve_slot_filling_turn("4*6", history)
        self.assertEqual(r["captured_slots"]["dimension_values"], [4.0, 6.0])
        self.assertEqual(r["captured_slots"]["dimension_count"], 2)
        self.assertNotIn("24", r["message"])
        self.assertIn("4", r["message"])
        self.assertIn("6", r["message"])


class TestDimensionParser(unittest.TestCase):
    """3, 4."""

    def test_complete_dimensions_with_all_separators(self):
        for text in ["40x60x30", "40 x 60 x 30", "40*60*30", "40×60×30", "40 60 30"]:
            parsed = parse_dimension_input(text)
            self.assertEqual(parsed["dimension_values"], [40.0, 60.0, 30.0], msg=text)

    def test_unit_is_captured_and_preserved(self):
        parsed = parse_dimension_input("40x60x30 cm")
        self.assertEqual(parsed["dimension_values"], [40.0, 60.0, 30.0])
        self.assertEqual(parsed["dimension_unit"], "cm")

    def test_labeled_dimensions_order_independent(self):
        parsed = parse_dimension_input("ยาว 40 กว้าง 60 สูง 30")
        self.assertEqual(parsed["length"], 40.0)
        self.assertEqual(parsed["width"], 60.0)
        self.assertEqual(parsed["height"], 30.0)

    def test_meter_unit_dimensions(self):
        parsed = parse_dimension_input("0.4 x 0.6 x 0.3 m")
        self.assertEqual(parsed["dimension_values"], [0.4, 0.6, 0.3])
        self.assertEqual(parsed["dimension_unit"], "m")


class TestWeightAndSelectiveMissingSlots(unittest.TestCase):
    """5, 6, 7."""

    def test_weight_input_is_captured(self):
        parsed = parse_dimension_input("12 kg")
        self.assertEqual(parsed["weight"], 12.0)
        self.assertEqual(parsed["weight_unit"], "kg")

    def test_missing_slots_requested_selectively_never_repeats_captured(self):
        history = _history(("assistant", ASSISTANT_ASK))
        r = resolve_slot_filling_turn("40x60x30 cm", history)
        self.assertEqual(r["missing_slots"], ["weight"])
        # Only "weight" is missing — dimension_unit must never be
        # re-requested since "cm" was already captured this turn.
        self.assertNotIn("dimension_unit", r["missing_slots"])
        self.assertNotIn("third_dimension", r["missing_slots"])
        self.assertIn("น้ำหนัก", r["message"])

    def test_partial_values_persist_across_turns(self):
        history = _history(("assistant", ASSISTANT_ASK))
        first = resolve_slot_filling_turn("40x60x30 cm", history)
        history2 = history + _history(("user", "40x60x30 cm"), ("assistant", first["message"]))
        second = resolve_slot_filling_turn("12 kg", history2)
        self.assertTrue(second["flow_complete"])
        self.assertEqual(second["captured_slots"]["dimension_values"], [40.0, 60.0, 30.0])
        self.assertEqual(second["captured_slots"]["dimension_unit"], "cm")
        self.assertEqual(second["captured_slots"]["weight"], 12.0)


class TestNewTopicHandling(unittest.TestCase):
    """8."""

    def test_cancellation_exits_the_flow(self):
        history = _history(("assistant", ASSISTANT_ASK))
        self.assertIsNone(resolve_slot_filling_turn("ไม่คำนวณแล้ว", history))
        self.assertIsNone(detect_active_flow(history + _history(("user", "ไม่คำนวณแล้ว"))))

    def test_bare_numeric_expression_never_cancels_the_flow(self):
        self.assertFalse(is_cancellation("4*6"))
        self.assertFalse(is_cancellation("40x60x30"))

    def test_explicit_new_topic_words_are_recognized_as_cancellation_signals(self):
        for phrase in ["ยกเลิก", "ขอถามเรื่องโกดัง", "ไม่คำนวณแล้ว"]:
            history = _history(("assistant", ASSISTANT_ASK))
            self.assertIsNone(resolve_slot_filling_turn(phrase, history))


class TestExplicitMathStillWorksWithoutActiveFlow(unittest.TestCase):
    """9."""

    def test_bare_expression_evaluates_as_math_with_no_active_flow(self):
        result = resolve_bare_math_expression("4*6", None)
        self.assertIsNotNone(result)
        self.assertEqual(result["value"], 24)

    def test_bare_expression_does_not_evaluate_as_math_during_active_flow(self):
        history = _history(("assistant", ASSISTANT_ASK))
        self.assertIsNone(resolve_bare_math_expression("4*6", history))

    def test_explicit_math_marker_defers_to_normal_calculation_path(self):
        history = _history(("assistant", ASSISTANT_ASK))
        self.assertIsNone(resolve_slot_filling_turn("4*6 ได้เท่าไหร่", history))
        self.assertTrue(is_explicit_math_intent("4*6 ได้เท่าไหร่"))


class TestNonMathDataIsNeverMisreadAsArithmetic(unittest.TestCase):
    """10, 11, 12 — order IDs, dates, and phone numbers must never be
    evaluated as arithmetic, active flow or not."""

    def test_order_id_is_not_treated_as_math(self):
        self.assertIsNone(resolve_bare_math_expression("เช็คออเดอร์ FT123456789", None))

    def test_date_is_not_treated_as_division(self):
        # "10/5/2569" looks like it has a "/" divide operator but is not
        # a safe bare arithmetic expression once real dates are involved
        # — the character-class gate rejects it outright since it's not
        # phrased as pure digits/operators (has surrounding context in
        # realistic use); a bare "10/5" alone actually IS ambiguous with
        # division and is accepted, matching "any bare arithmetic
        # expression is math when no active flow exists."
        self.assertIsNone(resolve_bare_math_expression("วันที่ 10/5/2569", None))

    def test_phone_number_is_not_treated_as_a_generic_math_expression(self):
        self.assertIsNone(resolve_bare_math_expression("091-5050-775", None))


class TestConfirmationRepliesStillWork(unittest.TestCase):
    """13 — yes/no confirmations to something else (unrelated to this
    flow) never get swallowed by the dimension-slot resolver."""

    def test_confirmation_reply_without_active_flow_is_not_slot_filling(self):
        self.assertIsNone(resolve_slot_filling_turn("ใช่", None))

    def test_confirmation_reply_with_active_flow_and_no_numbers_is_not_slot_filling(self):
        history = _history(("assistant", ASSISTANT_ASK))
        self.assertIsNone(resolve_slot_filling_turn("ใช่", history))


if __name__ == "__main__":
    unittest.main()
