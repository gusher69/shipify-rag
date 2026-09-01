"""P7 — General vs Shipify vs Private-ERP boundary.

Verify-first task: the boundary itself (source-of-truth routing) was
already correct everywhere it was traced. The one proven gap was a
General-knowledge elliptical follow-up ("จีนอยู่ทวีปอะไร" -> "แล้วญี่ปุ่น
ล่ะ") resolving to a broken fragment ("ญี่ปุ่นอะไร") instead of the
equivalent general question ("ญี่ปุ่นอยู่ทวีปอะไร"), which then answered
"no information" instead of "Asia". Fixed with one additional branch in
the EXISTING conversation resolver's _compose() — no new router, no LLM.
"""
import unittest

from rag.query_resolution import resolve_conversation


def _hist(prev_user, prev_assistant):
    return [{"role": "user", "content": prev_user}, {"role": "assistant", "content": prev_assistant}]


class GeneralEllipticalFollowUp(unittest.TestCase):
    # Acceptance B
    def test_country_swap_keeps_the_general_question_shape(self):
        r = resolve_conversation("แล้วญี่ปุ่นล่ะ", _hist("จีนอยู่ทวีปอะไร", "จีนอยู่ในทวีปเอเชียค่ะ"))
        self.assertEqual(r["resolved_question"], "ญี่ปุ่นอยู่ทวีปอะไร")

    def test_politeness_particles_are_stripped(self):
        r = resolve_conversation("แล้วเกาหลีล่ะครับ", _hist("จีนอยู่ทวีปอะไร", "เอเชียค่ะ"))
        self.assertEqual(r["resolved_question"], "เกาหลีอยู่ทวีปอะไร")

    def test_region_question_family_also_swaps(self):
        r = resolve_conversation("แล้วเชียงใหม่ล่ะ", _hist("กรุงเทพอยู่ภาคอะไร", "ภาคกลางค่ะ"))
        self.assertEqual(r["resolved_question"], "เชียงใหม่อยู่ภาคอะไร")

    def test_a_shipify_vocabulary_word_as_the_new_subject_still_swaps(self):
        # "ไทย"/"จีน" are recognised Shipify location entities, but the
        # PRIOR turn being non-Shipify is what matters here.
        r = resolve_conversation("แล้วไทยล่ะ", _hist("จีนอยู่ทวีปอะไร", "เอเชียค่ะ"))
        self.assertEqual(r["resolved_question"], "ไทยอยู่ทวีปอะไร")


class ShipifyFollowUpsUnaffected(unittest.TestCase):
    """The new branch must never fire when the PRIOR turn (or the current
    fragment) is Shipify/company-domain — those keep their own existing
    resolver branches."""

    def test_eligibility_followup_preserved(self):
        r = resolve_conversation("แล้วน้ำมันงาล่ะ", _hist("น้ำปลานำเข้าได้ไหม", "ได้ค่ะ"))
        self.assertEqual(r["resolved_question"], "น้ำมันงานำเข้าได้ไหม")

    def test_transport_followup_preserved(self):
        r = resolve_conversation("แล้วทางเรือล่ะ", _hist("ทางรถใช้เวลากี่วัน", "7-10 วันค่ะ"))
        self.assertEqual(r["resolved_question"], "ขอทราบระยะเวลาขนส่งทางเรือ")

    def test_warehouse_followup_preserved(self):
        r = resolve_conversation("แล้วไทยล่ะ", _hist("ขอที่อยู่โกดังจีน", "..."))
        self.assertEqual(r["resolved_question"], "ขอที่อยู่โกดังไทย")

    def test_p51_product_answer_continuation_preserved(self):
        r = resolve_conversation("รองเท้าครับ", _hist("อยากนำเข้าสินค้าจากจีน", "คุณสนใจสินค้าประเภทไหนคะ"))
        self.assertEqual(r["resolved_question"], "รองเท้านำเข้าได้ไหม")
        self.assertEqual(r["followup_type"], "clarification-answer")


class SelfContainedShipifyTurnWins(unittest.TestCase):
    # Acceptance C — a fresh, self-contained Shipify question after a
    # general turn is never rewritten toward the stale general topic.
    def test_shipify_question_after_general_is_untouched(self):
        r = resolve_conversation("ค่าขนส่งทางรถเท่าไหร่", _hist("จีนอยู่ทวีปอะไร", "เอเชียค่ะ"))
        self.assertEqual(r["resolved_question"], "ค่าขนส่งทางรถเท่าไหร่")
        self.assertIsNone(r["followup_type"])


if __name__ == "__main__":
    unittest.main()
