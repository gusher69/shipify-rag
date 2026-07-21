"""Regression tests for the conversational meta follow-up fix (P0,
2026-07-20): "ช่วยสรุปข้อมูลบริษัทให้หน่อย" (and similar "summarize/give
more detail/again" follow-ups) must resolve using the PREVIOUS USER
QUESTION's own topic — never asking "บริษัทไหน" (which company) and
never treating a prior ASSISTANT answer as evidence.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rag.query_resolution import resolve_conversation


class TestMetaSummaryFollowupResolution(unittest.TestCase):
    def test_company_summary_followup_never_asks_which_company(self):
        history = [
            {"role": "user", "content": "บริษัทนี้ทำธุรกิจเกี่ยวกับอะไร"},
            {"role": "assistant", "content": "บริษัทให้บริการฝากสั่งและฝากนำเข้าสินค้าจากต่างประเทศ"},
        ]
        r = resolve_conversation("ช่วยสรุปข้อมูลบริษัทให้หน่อย", history)
        self.assertEqual(r["followup_type"], "meta-summary-followup")
        self.assertGreaterEqual(r["confidence"], 0.7)
        resolved = r["resolved_question"]
        # Must never regress to asking which company.
        self.assertNotIn("บริษัทไหน", resolved)
        # Must be grounded in the PREVIOUS USER QUESTION's own topic —
        # "บริษัท" (company) — not left as the literal original wording.
        self.assertIn("บริษัท", resolved)
        self.assertNotEqual(resolved, "ช่วยสรุปข้อมูลบริษัทให้หน่อย")

    def test_never_reuses_previous_assistant_answer_as_evidence(self):
        """The resolved query must be built from the prior USER question's
        wording only — an assistant answer containing a distinctive term
        (e.g. a specific product name) must never leak into the rewritten
        retrieval query."""
        history = [
            {"role": "user", "content": "บริษัทนี้ทำธุรกิจเกี่ยวกับอะไร"},
            {"role": "assistant", "content": "เราขาย UNIQUE_PRODUCT_NAME_XYZ เป็นหลัก"},
        ]
        r = resolve_conversation("ช่วยสรุปข้อมูลบริษัทให้หน่อย", history)
        self.assertNotIn("UNIQUE_PRODUCT_NAME_XYZ", r["resolved_question"])

    def test_warehouse_address_summary_again_reuses_prior_topic(self):
        history = [
            {"role": "user", "content": "ขอที่อยู่โกดังจีน"},
            {"role": "assistant", "content": "โกดังจีนอยู่ที่..."},
        ]
        r = resolve_conversation("ขอสรุปอีกที", history)
        self.assertEqual(r["followup_type"], "meta-summary-followup")
        resolved = r["resolved_question"]
        self.assertIn("โกดัง", resolved)
        self.assertIn("จีน", resolved)

    def test_shipping_rate_detail_request_reuses_prior_topic(self):
        history = [
            {"role": "user", "content": "ค่าขนส่งคิดยังไง"},
            {"role": "assistant", "content": "คิดตามน้ำหนักและปริมาตรค่ะ"},
        ]
        r = resolve_conversation("ขอแบบละเอียด", history)
        self.assertEqual(r["followup_type"], "meta-detail-followup")
        self.assertIn("ค่าขนส่ง", r["resolved_question"])

    def test_standalone_question_with_own_topic_is_never_touched(self):
        """A question that both uses a meta-instruction word AND names its
        own concrete, registered subject is a fresh standalone question —
        must be left completely unchanged, same as any other complete
        question."""
        history = [{"role": "user", "content": "บริษัทนี้ทำธุรกิจเกี่ยวกับอะไร"}]
        r = resolve_conversation("ขอเรททางเรือ", history)
        self.assertEqual(r["resolved_question"], "ขอเรททางเรือ")

    def test_meta_followup_with_no_prior_history_stays_unchanged(self):
        """No previous user turn to resolve against — must never invent a
        subject out of nothing; original question stays untouched."""
        r = resolve_conversation("ช่วยสรุปข้อมูลบริษัทให้หน่อย", None)
        self.assertEqual(r["resolved_question"], "ช่วยสรุปข้อมูลบริษัทให้หน่อย")

    def test_no_answer_uses_previous_assistant_turn_directly(self):
        """_last_user_question must keep skipping assistant turns — a
        conversation ending in an assistant turn still resolves from the
        last USER turn, not the assistant's."""
        history = [
            {"role": "user", "content": "มีบริการอะไรบ้าง"},
            {"role": "assistant", "content": "มีบริการฝากสั่ง ฝากนำเข้า และฝากโอนเงินค่ะ"},
        ]
        r = resolve_conversation("สรุปให้หน่อย", history)
        self.assertIn("บริการ", r["resolved_question"])


if __name__ == "__main__":
    unittest.main()
