"""Tests for services/customer_tier_service.py -- both the existing
aggregate compute_tier() (whole-relationship score) and the new,
per-message Customer Stage / Handoff Recommendation logic added for
Customer Intelligence V1 (2026-08-15, Phase 5/6)."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.customer_tier_service import (
    compute_tier, classify_message_stage, compute_handoff_recommendation,
    _merge_stage, update_tier_for_profile,
)


class TestComputeTier(unittest.TestCase):
    """Unchanged aggregate scoring -- regression coverage only, not the
    focus of this sprint."""

    def test_empty_profile_is_cold(self):
        self.assertEqual(compute_tier({})["tier"], "cold")

    def test_complaint_forces_negative_even_with_high_frequency(self):
        result = compute_tier({"conversation_count": 10, "erp_requests_count": 10,
                                "complaint_count": 1, "negative_count": 1, "positive_count": 0})
        self.assertEqual(result["tier"], "negative")


class TestClassifyMessageStage(unittest.TestCase):
    """Phase 8 test matrix scenarios A-E (message-level classification
    only; handoff recommendation is covered separately below)."""

    def test_a_general_service_question_is_cold(self):
        result = classify_message_stage("Shipify ให้บริการอะไรบ้าง")
        self.assertEqual(result["stage"], "cold")

    def test_b_service_cost_question_is_warm(self):
        result = classify_message_stage("นำเข้าสินค้าจากจีนคิดค่าบริการยังไง")
        self.assertEqual(result["stage"], "warm")

    def test_c_interest_plus_callback_request_is_hot(self):
        result = classify_message_stage("สนใจใช้บริการ ขอให้เซลส์ติดต่อกลับ")
        self.assertEqual(result["stage"], "hot")

    def test_d_complaint_plus_human_request_is_negative(self):
        result = classify_message_stage("บริการแย่มาก ขอคุยกับเจ้าหน้าที่")
        self.assertEqual(result["stage"], "negative")

    def test_e_shipment_not_arrived_is_negative_or_warm_never_hot_or_cold(self):
        result = classify_message_stage("ของยังไม่ถึง ช่วยเช็กให้หน่อย")
        self.assertIn(result["stage"], ("negative", "warm"))

    def test_hot_interest_without_callback_request_is_still_hot(self):
        # Phase 6's own worked example: HOT but handoff_recommended=false.
        result = classify_message_stage("สนใจมากครับ ขอรายละเอียดราคา")
        self.assertEqual(result["stage"], "hot")

    def test_empty_message_is_cold_never_raises(self):
        result = classify_message_stage("")
        self.assertEqual(result["stage"], "cold")

    def test_every_result_has_a_reason(self):
        for msg in ("Shipify ให้บริการอะไรบ้าง", "สนใจใช้บริการ", "บริการแย่มาก", ""):
            result = classify_message_stage(msg)
            self.assertTrue(result["reason"])


class TestComputeHandoffRecommendation(unittest.TestCase):
    """Phase 6 -- Customer Stage != Human Handoff."""

    def test_hot_with_explicit_callback_request_recommends_handoff(self):
        result = compute_handoff_recommendation("hot", "สนใจใช้บริการ ขอให้เซลส์ติดต่อกลับ")
        self.assertTrue(result["recommended"])

    def test_hot_without_callback_request_does_not_recommend_handoff(self):
        result = compute_handoff_recommendation("hot", "สนใจมากครับ ขอรายละเอียดราคา")
        self.assertFalse(result["recommended"])

    def test_negative_with_human_request_recommends_handoff(self):
        result = compute_handoff_recommendation("negative", "บริการแย่มาก ขอคุยกับเจ้าหน้าที่")
        self.assertTrue(result["recommended"])

    def test_negative_erp_resolvable_concern_does_not_recommend_handoff(self):
        result = compute_handoff_recommendation("negative", "ของยังไม่ถึง ช่วยเช็กให้หน่อย")
        self.assertFalse(result["recommended"])

    def test_cold_and_warm_never_recommend_handoff(self):
        self.assertFalse(compute_handoff_recommendation("cold", "Shipify ให้บริการอะไรบ้าง")["recommended"])
        self.assertFalse(compute_handoff_recommendation("warm", "นำเข้าสินค้าจากจีนคิดค่าบริการยังไง")["recommended"])


class TestMergeStage(unittest.TestCase):
    def test_hot_message_always_wins_even_on_cold_aggregate(self):
        merged = _merge_stage("cold", {"stage": "hot", "confidence": 0.85, "reason": "x"})
        self.assertEqual(merged["stage"], "hot")

    def test_negative_message_always_wins_even_on_hot_aggregate(self):
        merged = _merge_stage("hot", {"stage": "negative", "confidence": 0.9, "reason": "x"})
        self.assertEqual(merged["stage"], "negative")

    def test_neutral_message_never_downgrades_higher_aggregate(self):
        merged = _merge_stage("hot", {"stage": "cold", "confidence": 0.5, "reason": "x"})
        self.assertEqual(merged["stage"], "hot")

    def test_neutral_message_can_raise_a_lower_aggregate(self):
        merged = _merge_stage("cold", {"stage": "warm", "confidence": 0.7, "reason": "x"})
        self.assertEqual(merged["stage"], "warm")


class TestUpdateTierForProfileWithMessage(unittest.TestCase):
    """update_tier_for_profile(line_user_id, message=...) persists the
    merged stage plus stage_reason/stage_confidence/handoff_recommended/
    handoff_reason onto the SAME conversation_tier/tier_score columns --
    no second stage axis."""

    def test_new_user_hot_message_persists_hot_and_handoff_true(self):
        mock_table = MagicMock()
        with patch("profiles.manager.get_profile", return_value={}), \
             patch("profiles.manager.supabase") as mock_supabase, \
             patch("profiles.manager.TABLE", "user_profiles"):
            mock_supabase.table.return_value = mock_table
            mock_table.update.return_value = mock_table
            mock_table.eq.return_value = mock_table
            result = update_tier_for_profile("U_NEW", message="สนใจใช้บริการ ขอให้เซลส์ติดต่อกลับ")

        self.assertEqual(result["tier"], "hot")
        update_kwargs = mock_table.update.call_args.args[0]
        self.assertEqual(update_kwargs["conversation_tier"], "hot")
        self.assertTrue(update_kwargs["handoff_recommended"])
        self.assertIn("stage_reason", update_kwargs)
        self.assertIn("stage_confidence", update_kwargs)

    def test_negative_message_never_erases_to_a_lower_tier(self):
        mock_table = MagicMock()
        with patch("profiles.manager.get_profile", return_value={}), \
             patch("profiles.manager.supabase") as mock_supabase, \
             patch("profiles.manager.TABLE", "user_profiles"):
            mock_supabase.table.return_value = mock_table
            mock_table.update.return_value = mock_table
            mock_table.eq.return_value = mock_table
            result = update_tier_for_profile("U_NEG", message="บริการแย่มาก ขอคุยกับเจ้าหน้าที่")

        self.assertEqual(result["tier"], "negative")
        update_kwargs = mock_table.update.call_args.args[0]
        self.assertTrue(update_kwargs["handoff_recommended"])

    def test_never_raises_on_persistence_failure(self):
        with patch("profiles.manager.get_profile", return_value={}), \
             patch("profiles.manager.supabase") as mock_supabase, \
             patch("profiles.manager.TABLE", "user_profiles"):
            mock_supabase.table.side_effect = Exception("db down")
            result = update_tier_for_profile("U_ERR", message="สนใจใช้บริการ")
        self.assertEqual(result["tier"], "hot")


if __name__ == "__main__":
    unittest.main()
