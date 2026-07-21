"""Regression tests for the Company Overview / Summary Formatting /
Message Segmentation P0 fix (2026-07-21). Covers Part 12's test list to
the extent it's expressible as a pure-Python unit test (JS rendering and
live-KB verification are covered separately, not here — see the
deliverable report).
"""
import unittest

from rag.query_understanding import classify_actionable_intent
from rag.hybrid_scoring import (
    apply_hybrid_ranking, compute_company_intent_boost, has_strong_company_profile_evidence,
)
from services.answer_planner import plan_answer, _wants_detailed_summary
from services.message_segmenter import segment_message
from line_bot.message_adapter import build_line_text_messages


def _chunk(text, section_title, score=0.3):
    return {"text": text, "section_title": section_title, "score": score,
            "chunk_index": 0, "file_name": "AI Knowledge Master.xlsx"}


class TestCompanyOverviewExcludesSecondaryEvidence(unittest.TestCase):
    """1, 4, 18, 19."""

    def test_company_overview_excludes_contact_faq_from_top_evidence(self):
        # Identical vector scores — representative of two real FAQ rows
        # about the same general company/topic with comparable relevance,
        # where ranking is decided by the intent boost, not an artificial
        # vector-score gap that would win/lose the race on its own.
        chunks = [
            _chunk("Question: ขอเบอร์ติดต่อ\nAnswer: 02-026-6426", "Question: ขอเบอร์ติดต่อ", 0.34),
            _chunk("Question: บริษัทให้บริการอะไรบ้าง เกี่ยวกับธุรกิจอะไร\nAnswer: นำเข้าสินค้าจากจีน",
                   "Question: บริษัทให้บริการอะไรบ้าง เกี่ยวกับธุรกิจอะไร", 0.34),
        ]
        kept = apply_hybrid_ranking("บริษัทนี้ทำธุรกิจเกี่ยวกับอะไร", chunks,
                                     company_intent=True, actionable_intent="company_overview")
        self.assertNotEqual(kept[0]["section_title"], "Question: ขอเบอร์ติดต่อ")

    def test_secondary_service_never_outranks_core_business_description(self):
        """Wooden-crate service (secondary) must not outrank a chunk
        describing the actual core import/shipping business."""
        chunks = [
            _chunk("Question: มีบริการตีลังไม้ไหม\nAnswer: มีค่ะ", "Question: มีบริการตีลังไม้ไหม", 0.35),
            _chunk("Question: บริษัทให้บริการอะไรบ้าง นำเข้าสินค้าจากจีน\nAnswer: ฝากสั่ง ฝากนำเข้า ขนส่งจากจีน",
                   "Question: บริษัทให้บริการอะไรบ้าง นำเข้าสินค้าจากจีน", 0.33),
        ]
        kept = apply_hybrid_ranking("บริษัทให้บริการอะไร", chunks,
                                     company_intent=True, actionable_intent="company_overview")
        self.assertIn("นำเข้าสินค้าจากจีน", kept[0]["section_title"])

    def test_adaptive_relevance_threshold_unchanged_for_company_overview(self):
        """Negative boosts never widen what counts as relevant — a pool
        of genuinely irrelevant chunks (no lexical evidence at all) still
        has its weakest members excluded beyond minimum_candidates."""
        chunks = [
            _chunk("Question: บริษัททำธุรกิจเกี่ยวกับอะไร\nAnswer: นำเข้าสินค้าจากจีน",
                   "Question: บริษัททำธุรกิจเกี่ยวกับอะไร", 0.9),
        ] + [
            _chunk(f"เนื้อหาที่ไม่เกี่ยวข้องกันเลย {i}", f"หัวข้ออื่น {i}", 0.05 - i * 0.005)
            for i in range(6)
        ]
        kept, excluded = apply_hybrid_ranking("บริษัทนี้ทำธุรกิจเกี่ยวกับอะไร", chunks,
                                               company_intent=True, actionable_intent="company_overview",
                                               return_excluded=True)
        self.assertGreater(len(excluded), 0)

    def test_negative_boost_never_applies_outside_company_intents(self):
        """A contact-FAQ chunk must NOT be penalized when the actionable
        intent is something else entirely (e.g. warehouse_contact) — the
        negative boost is scoped only to company_overview/company_summary."""
        chunk = _chunk("Question: ขอเบอร์ติดต่อ\nAnswer: 02-026-6426", "Question: ขอเบอร์ติดต่อ")
        boost = compute_company_intent_boost(True, chunk, actionable_intent="warehouse_contact")
        self.assertEqual(boost, 0.0)


class TestActionableIntentClassification(unittest.TestCase):
    """Supports items 1-3, 5 by confirming the intent layer feeding them."""

    def test_company_overview_detected(self):
        r = classify_actionable_intent("บริษัทนี้ทำธุรกิจเกี่ยวกับอะไร", entities={})
        self.assertEqual(r["actionable_intent"], "company_overview")

    def test_company_summary_detected(self):
        r = classify_actionable_intent("ช่วยสรุปข้อมูลบริษัทให้หน่อย", entities={})
        self.assertEqual(r["actionable_intent"], "company_summary")

    def test_wooden_crate_question_is_not_company_overview(self):
        r = classify_actionable_intent("มีบริการตีลังไม้ไหม", entities={})
        self.assertNotIn(r["actionable_intent"], ("company_overview", "company_summary"))

    def test_contact_question_is_not_company_overview(self):
        r = classify_actionable_intent("ขอเบอร์ติดต่อ", entities={})
        self.assertNotIn(r["actionable_intent"], ("company_overview", "company_summary"))


class TestAnswerPlanExclusions(unittest.TestCase):
    """4, 5, 6, 7, 8."""

    def test_company_summary_plan_starts_with_overview_and_services(self):
        plan = plan_answer("ช่วยสรุปข้อมูลบริษัทให้หน่อย", "company_summary", chunks=[{"score": 0.3}])
        self.assertEqual(plan["required_facts"][0], "company_overview")
        self.assertIn("core_services", plan["required_facts"])

    def test_company_summary_excludes_prices_and_phone_by_default(self):
        plan = plan_answer("ช่วยสรุปข้อมูลบริษัทให้หน่อย", "company_summary", chunks=[{"score": 0.3}])
        self.assertIn("exact_prices", plan["excluded_facts"])
        self.assertIn("phone_numbers", plan["excluded_facts"])

    def test_company_summary_excludes_prohibited_goods_by_default(self):
        plan = plan_answer("ช่วยสรุปข้อมูลบริษัทให้หน่อย", "company_summary", chunks=[{"score": 0.3}])
        self.assertIn("prohibited_goods_list", plan["excluded_facts"])

    def test_detailed_summary_widens_optional_facts_instead_of_excluding(self):
        plan = plan_answer("ช่วยสรุปข้อมูลบริษัทแบบละเอียด", "company_summary", chunks=[{"score": 0.3}])
        self.assertNotIn("phone_numbers", plan["excluded_facts"])
        self.assertIn("phone_numbers", plan["optional_facts"])

    def test_wants_detailed_summary_detector(self):
        self.assertTrue(_wants_detailed_summary("ช่วยสรุปข้อมูลบริษัทแบบละเอียด"))
        self.assertFalse(_wants_detailed_summary("ช่วยสรุปข้อมูลบริษัทให้หน่อย"))

    def test_company_overview_excludes_secondary_services_and_prices(self):
        plan = plan_answer("บริษัทนี้ทำธุรกิจเกี่ยวกับอะไร", "company_overview", chunks=[{"score": 0.3}])
        self.assertIn("secondary_services", plan["excluded_facts"])
        self.assertIn("prices", plan["excluded_facts"])
        self.assertIn("phone_numbers", plan["excluded_facts"])


class TestMessageSegmentationContract(unittest.TestCase):
    """9, 14, 15."""

    def test_structured_multi_section_answer_produces_multiple_messages(self):
        answer = "เกี่ยวกับบริษัท\n\nShipify ให้บริการนำเข้าสินค้าจากจีน\n\nบริการหลัก\n- ฝากสั่ง\n- ฝากนำเข้า"
        result = segment_message(answer, reply_mode="auto")
        self.assertGreater(result.message_count, 1)
        self.assertTrue(result.segmentation_applied)

    def test_short_answer_remains_one_message(self):
        result = segment_message("มีบริการตีลังไม้ให้ค่ะ", reply_mode="auto")
        self.assertEqual(result.message_count, 1)
        self.assertFalse(result.segmentation_applied)

    def test_attachment_ordering_preserved_when_segmented(self):
        answer = "ข้อความแรก\n\nข้อความที่สอง"
        result = segment_message(answer, reply_mode="auto", has_attachments=True)
        self.assertEqual(result.attachment_order, ["text", "attachment", "instruction"])

    def test_messages_contract_shape_matches_message_parts(self):
        """The {"type": "text", "content": ...} contract (Part 6) must be
        built from the exact same message_parts — never a second,
        independently-computed split."""
        answer = "หัวข้อแรก\n\nหัวข้อที่สอง"
        result = segment_message(answer, reply_mode="auto")
        messages = [{"type": "text", "content": p} for p in result.message_parts]
        self.assertEqual([m["content"] for m in messages], result.message_parts)
        self.assertTrue(all(m["type"] == "text" for m in messages))


class TestLineAdapterSegmentation(unittest.TestCase):
    """12."""

    def test_multi_section_answer_becomes_multiple_text_messages(self):
        answer = "เกี่ยวกับบริษัท\n\nShipify ให้บริการนำเข้าสินค้าจากจีน\n\nบริการหลัก\n- ฝากสั่ง\n- ฝากนำเข้า"
        messages = build_line_text_messages(answer, max_text_messages=3)
        self.assertGreater(len(messages), 1)

    def test_short_answer_becomes_one_text_message(self):
        messages = build_line_text_messages("มีบริการตีลังไม้ให้ค่ะ", max_text_messages=3)
        self.assertEqual(len(messages), 1)

    def test_respects_max_text_messages_limit_when_images_present(self):
        """When images already consume reply slots, the LEAST important
        trailing text segments are merged, never silently dropped —
        total text messages never exceeds the given budget."""
        answer = "หัวข้อ 1\n\nหัวข้อ 2\n\nหัวข้อ 3\n\nหัวข้อ 4\n\nหัวข้อ 5"
        messages = build_line_text_messages(answer, max_text_messages=2)
        self.assertLessEqual(len(messages), 2)

    def test_order_and_content_preserved_across_segments(self):
        answer = "ข้อความแรก\n\nข้อความที่สอง"
        messages = build_line_text_messages(answer, max_text_messages=3)
        combined = " ".join(m.text for m in messages)
        self.assertIn("ข้อความแรก", combined)
        self.assertIn("ข้อความที่สอง", combined)
        self.assertLess(combined.index("ข้อความแรก"), combined.index("ข้อความที่สอง"))


class TestKnowledgeGapWarning(unittest.TestCase):
    """Part 10."""

    def test_no_warning_when_profile_heading_evidence_exists(self):
        chunks = [_chunk("Company Profile: Shipify นำเข้าสินค้าจากจีน", "Company Profile")]
        from rag.hybrid_scoring import apply_hybrid_ranking
        kept = apply_hybrid_ranking("บริษัทนี้ทำธุรกิจเกี่ยวกับอะไร", chunks,
                                     company_intent=True, actionable_intent="company_overview")
        self.assertTrue(has_strong_company_profile_evidence(kept))

    def test_warning_signal_when_only_generic_service_chunks_exist(self):
        chunks = [_chunk("Question: มีบริการอะไรบ้าง\nAnswer: ฝากสั่ง ฝากนำเข้า", "Question: มีบริการอะไรบ้าง")]
        kept = apply_hybrid_ranking("บริษัทนี้ทำธุรกิจเกี่ยวกับอะไร", chunks,
                                     company_intent=True, actionable_intent="company_overview")
        self.assertFalse(has_strong_company_profile_evidence(kept))


if __name__ == "__main__":
    unittest.main()
