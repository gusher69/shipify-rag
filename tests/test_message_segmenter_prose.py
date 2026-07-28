"""Regression tests for Natural Prose Message Segmentation — the
extension to services/message_segmenter.py's Auto mode that splits a
SINGLE flowing paragraph (no blank lines, no bullet list) at safe
contrast/next-step marker boundaries (เช่น "ส่วน", "หาก", "สามารถ"),
never inside a protected span (numbers, URLs, phone numbers, tracking
codes — reused directly from rag/spell_correction.py, never duplicated).
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.message_segmenter import segment_message

FLOWING_RATE_ANSWER = (
    "ค่าขนส่งคิดจากน้ำหนักหรือปริมาตร โดยใช้ค่าที่มากกว่า "
    "ทางรถคิด 35 บาทต่อกิโลกรัมหรือ 6,900 บาทต่อ CBM "
    "ส่วนทางเรือคิด 19 บาทต่อกิโลกรัมหรือ 4,500 บาทต่อ CBM "
    "หากมีขนาดและน้ำหนักสามารถส่งมาให้ช่วยคำนวณได้ค่ะ"
)
CREDIT_CARD_ANSWER = "ใช้บัตรเครดิตได้ไหม ขั้นต่ำ 500 บาท ค่าธรรมเนียม 3% ไม่สามารถออกใบกำกับภาษีได้"


class TestFlowingProseExample(unittest.TestCase):
    def test_splits_into_2_to_3_parts(self):
        r = segment_message(FLOWING_RATE_ANSWER, reply_mode="auto")
        self.assertIn(r.message_count, (2, 3))
        self.assertIn("prose_marker", r.boundary_types)

    def test_every_part_is_a_verbatim_substring(self):
        r = segment_message(FLOWING_RATE_ANSWER, reply_mode="auto")
        for part in r.message_parts:
            self.assertIn(part.replace(" ", ""), FLOWING_RATE_ANSWER.replace(" ", "").replace("\n", ""))

    def test_numbers_and_cbm_remain_intact(self):
        r = segment_message(FLOWING_RATE_ANSWER, reply_mode="auto")
        joined = " ".join(r.message_parts)
        for value in ("35", "6,900", "19", "4,500", "CBM"):
            self.assertIn(value, joined)


class TestNegationNeverSplit(unittest.TestCase):
    """Regression: "ไม่สามารถ" ("cannot") must never be split right after
    "ไม่" just because "สามารถ" is a next-step marker word — that would
    separate a negation from the verb it negates."""

    def test_credit_card_answer_negation_stays_intact(self):
        r = segment_message(CREDIT_CARD_ANSWER, reply_mode="auto")
        joined = " ".join(r.message_parts)
        self.assertIn("ไม่สามารถออกใบกำกับภาษีได้", joined)
        self.assertIn(r.message_count, (1, 2))


class TestRegressionScenarios(unittest.TestCase):
    def test_7_short_contact_answer_is_1_bubble(self):
        r = segment_message("ขอเบอร์ติดต่อ", reply_mode="auto")
        self.assertEqual(r.message_count, 1)

    def test_8_long_flowing_shipping_rate_paragraph_is_2_to_3_bubbles(self):
        r = segment_message(FLOWING_RATE_ANSWER, reply_mode="auto")
        self.assertIn(r.message_count, (2, 3))

    def test_9_credit_card_answer_preserves_all_facts(self):
        r = segment_message(CREDIT_CARD_ANSWER, reply_mode="auto")
        self.assertIn(r.message_count, (1, 2))
        joined = " ".join(r.message_parts)
        self.assertIn("500", joined)
        self.assertIn("3%", joined)
        self.assertIn("ไม่สามารถออกใบกำกับภาษีได้", joined)

    def test_10_coupon_steps_preserved_in_order(self):
        answer = ("ใช้คูปองยังไง\n\n1. เปิดหน้าชำระเงิน\n2. กรอกโค้ดคูปอง\n3. กดยืนยัน\n\n"
                  "หากคูปองใช้ไม่ได้ ลองเช็ควันหมดอายุนะคะ")
        r = segment_message(answer, reply_mode="auto")
        steps_part = next(p for p in r.message_parts if "1. เปิดหน้าชำระเงิน" in p)
        self.assertLess(steps_part.index("1. เปิดหน้าชำระเงิน"), steps_part.index("2. กรอกโค้ดคูปอง"))
        self.assertLess(steps_part.index("2. กรอกโค้ดคูปอง"), steps_part.index("3. กดยืนยัน"))

    def test_11_fallback_and_escalation_are_exactly_1_bubble(self):
        r_fallback = segment_message(FLOWING_RATE_ANSWER, reply_mode="auto", is_fallback=True)
        self.assertEqual(r_fallback.message_count, 1)
        r_escalation = segment_message(FLOWING_RATE_ANSWER, reply_mode="auto", is_escalation=True)
        self.assertEqual(r_escalation.message_count, 1)

    def test_12_url_phone_and_tracking_id_remain_intact(self):
        answer = ("ติดตามพัสดุได้ที่ https://track.example.com/FT123456789 "
                  "หากมีปัญหาโทร 091-5050-775 ได้เลยค่ะ "
                  "สามารถเช็คสถานะได้ตลอด 24 ชั่วโมง")
        r = segment_message(answer, reply_mode="auto")
        joined = " ".join(r.message_parts)
        self.assertIn("https://track.example.com/FT123456789", joined)
        self.assertIn("091-5050-775", joined)


class TestAmbiguousSplitKeepsOneBubble(unittest.TestCase):
    def test_no_marker_found_stays_one_bubble(self):
        answer = "ค่าขนส่งคิดตามน้ำหนักของพัสดุที่ลูกค้าส่งมาให้ทางเราตรวจสอบก่อนเริ่มกระบวนการจัดส่งค่ะ"
        r = segment_message(answer, reply_mode="auto")
        self.assertEqual(r.message_count, 1)


if __name__ == "__main__":
    unittest.main()
