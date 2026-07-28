"""Regression tests for Human-like Multi-Message Replies
(services/message_segmenter.py) — splits one LLM answer into 1-3 short
conversational message bubbles when the answer's own structure supports
it. Pure Python, deterministic, no LLM call, never invents content
(every message part is a verbatim substring of the original answer).
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.message_segmenter import segment_message, SegmentedReply, DEFAULT_MAX_MESSAGES

RATE_ANSWER = (
    "ค่าขนส่งคิดจากน้ำหนักหรือปริมาตร โดยเลือกค่าที่มากกว่า\n\n"
    "🚚 ทางรถ: 35 บาท/กก. หรือ 6,900 บาท/CBM\n"
    "🚢 ทางเรือ: 19 บาท/กก. หรือ 4,500 บาท/CBM\n\n"
    "แจ้งขนาดและน้ำหนักมาได้เลยค่ะ เดี๋ยวช่วยคำนวณให้"
)
COUPON_ANSWER = (
    "ใช้คูปองยังไง\n\n"
    "1. เปิดหน้าชำระเงิน\n"
    "2. กรอกโค้ดคูปอง\n"
    "3. กดยืนยัน\n\n"
    "หากคูปองใช้ไม่ได้ ลองเช็ควันหมดอายุนะคะ"
)
CREDIT_CARD_ANSWER = "ใช้บัตรเครดิตได้ไหม ขั้นต่ำ 500 บาท ค่าธรรมเนียม 3% ไม่สามารถออกใบกำกับภาษีได้"


class TestDocumentedExample(unittest.TestCase):
    def test_rate_answer_matches_the_documented_example_exactly(self):
        r = segment_message(RATE_ANSWER, reply_mode="auto", max_messages=3, message_delay="natural")
        self.assertEqual(r.message_parts, [
            "ค่าขนส่งคิดจากน้ำหนักหรือปริมาตร โดยเลือกค่าที่มากกว่า",
            "🚚 ทางรถ: 35 บาท/กก. หรือ 6,900 บาท/CBM\n🚢 ทางเรือ: 19 บาท/กก. หรือ 4,500 บาท/CBM",
            "แจ้งขนาดและน้ำหนักมาได้เลยค่ะ เดี๋ยวช่วยคำนวณให้",
        ])

    def test_canonical_answer_field_is_always_the_full_original_string(self):
        r = segment_message(RATE_ANSWER, reply_mode="auto")
        self.assertEqual(r.answer, RATE_ANSWER)


class TestRegressionScenarios(unittest.TestCase):
    """The 10 numbered regression scenarios from the task spec."""

    def test_1_contact_number_question_is_one_part(self):
        r = segment_message("ขอเบอร์ติดต่อ", reply_mode="auto")
        self.assertEqual(r.message_count, 1)

    def test_2_shipping_calc_question_is_2_to_3_parts(self):
        r = segment_message(RATE_ANSWER, reply_mode="auto")
        self.assertIn(r.message_count, (2, 3))

    def test_3_credit_card_question_preserves_all_facts_in_1_or_2_parts(self):
        r = segment_message(CREDIT_CARD_ANSWER, reply_mode="auto")
        self.assertIn(r.message_count, (1, 2))
        joined = " ".join(r.message_parts)
        self.assertIn("500", joined)
        self.assertIn("3%", joined)
        self.assertIn("ไม่สามารถออกใบกำกับภาษีได้", joined)

    def test_4_coupon_steps_are_2_to_3_parts_and_stay_ordered(self):
        r = segment_message(COUPON_ANSWER, reply_mode="auto")
        self.assertIn(r.message_count, (2, 3))
        # Steps 1/2/3 must remain in original order within whichever part
        # holds them — never reordered or interleaved.
        steps_part = next(p for p in r.message_parts if "1. เปิดหน้าชำระเงิน" in p)
        self.assertLess(steps_part.index("1. เปิดหน้าชำระเงิน"), steps_part.index("2. กรอกโค้ดคูปอง"))
        self.assertLess(steps_part.index("2. กรอกโค้ดคูปอง"), steps_part.index("3. กดยืนยัน"))

    def test_5_fallback_is_always_1_part(self):
        r = segment_message("ไม่มีข้อมูลใน Knowledge Base", reply_mode="auto", is_fallback=True)
        self.assertEqual(r.message_count, 1)

    def test_6_escalation_is_always_1_part(self):
        r = segment_message("ขออภัยค่ะ ทีมงานจะติดต่อกลับเพื่อช่วยเหลือเพิ่มเติมนะคะ",
                             reply_mode="auto", is_escalation=True)
        self.assertEqual(r.message_count, 1)

    def test_7_url_and_phone_number_remain_intact(self):
        answer = "ติดต่อได้ที่ https://example.com/contact หรือโทร 091-5050-775 นะคะ"
        r = segment_message(answer, reply_mode="auto")
        joined = " ".join(r.message_parts)
        self.assertIn("https://example.com/contact", joined)
        self.assertIn("091-5050-775", joined)

    def test_8_single_message_policy_always_yields_exactly_one_part(self):
        r = segment_message(RATE_ANSWER, reply_mode="single", max_messages=3)
        self.assertEqual(len(r.message_parts), 1)
        self.assertEqual(r.message_parts[0], RATE_ANSWER)

    def test_9_max_messages_2_never_exceeds_2_parts(self):
        r = segment_message(RATE_ANSWER, reply_mode="auto", max_messages=2)
        self.assertLessEqual(r.message_count, 2)

    def test_10_auto_mode_very_short_answer_is_1_part(self):
        r = segment_message("ขอบคุณค่ะ", reply_mode="auto", max_messages=3)
        self.assertEqual(r.message_count, 1)


class TestProtectedConstructsNeverSplit(unittest.TestCase):
    def test_code_block_kept_as_one_message(self):
        answer = "ตัวอย่างโค้ด:\n\n```\nprint('hello')\n```\n\nลองดูนะคะ"
        r = segment_message(answer, reply_mode="auto")
        self.assertEqual(r.message_count, 1)
        self.assertEqual(r.message_parts[0], answer)

    def test_markdown_table_kept_as_one_message(self):
        answer = "ราคาแต่ละแผน:\n\n| แผน | ราคา |\n|-----|------|\n| A | 100 |\n\nสนใจแผนไหนคะ"
        r = segment_message(answer, reply_mode="auto")
        self.assertEqual(r.message_count, 1)

    def test_markdown_link_never_broken_across_parts(self):
        answer = "ดูแผนที่ได้ที่นี่ [แผนที่โกดัง](https://maps.example.com/a)\n\nสอบถามเพิ่มเติมได้เลยค่ะ"
        r = segment_message(answer, reply_mode="auto")
        joined = " ".join(r.message_parts)
        self.assertIn("[แผนที่โกดัง](https://maps.example.com/a)", joined)


class TestSafety(unittest.TestCase):
    def test_never_invents_content_every_part_is_a_substring_of_answer(self):
        r = segment_message(RATE_ANSWER, reply_mode="auto")
        for part in r.message_parts:
            self.assertIn(part, RATE_ANSWER)

    def test_repeated_support_sentence_is_dropped_not_duplicated(self):
        answer = ("ขออภัยค่ะ ทีมงานจะติดต่อกลับเพื่อช่วยเหลือเพิ่มเติมนะคะ\n\n"
                  "รายละเอียดเพิ่มเติม\n\n"
                  "ขออภัยค่ะ ทีมงานจะติดต่อกลับเพื่อช่วยเหลือเพิ่มเติมนะคะ")
        r = segment_message(answer, reply_mode="multi")
        support_count = sum(1 for p in r.message_parts if "ทีมงานจะติดต่อกลับ" in p)
        self.assertEqual(support_count, 1)

    def test_empty_answer_yields_zero_parts(self):
        r = segment_message("", reply_mode="auto")
        self.assertEqual(r.message_parts, [])
        self.assertEqual(r.message_count, 0)

    def test_max_messages_is_clamped_between_1_and_3(self):
        r = segment_message(RATE_ANSWER, reply_mode="auto", max_messages=10)
        self.assertLessEqual(r.message_count, 3)


class TestDelayMapping(unittest.TestCase):
    def test_none_delay_is_zero(self):
        r = segment_message(RATE_ANSWER, reply_mode="auto", message_delay="none")
        self.assertTrue(all(d == 0 for d in r.delay_ms))

    def test_short_delay_is_within_spec_range(self):
        r = segment_message(RATE_ANSWER, reply_mode="auto", message_delay="short")
        for d in r.delay_ms[1:]:
            self.assertTrue(250 <= d <= 400)

    def test_natural_delay_is_within_spec_range(self):
        r = segment_message(RATE_ANSWER, reply_mode="auto", message_delay="natural")
        for d in r.delay_ms[1:]:
            self.assertTrue(450 <= d <= 900)

    def test_first_delay_is_always_zero(self):
        r = segment_message(RATE_ANSWER, reply_mode="auto", message_delay="natural")
        self.assertEqual(r.delay_ms[0], 0)

    def test_delay_list_length_matches_message_parts_length(self):
        r = segment_message(RATE_ANSWER, reply_mode="auto")
        self.assertEqual(len(r.delay_ms), len(r.message_parts))


class TestAttachmentOrdering(unittest.TestCase):
    def test_default_order_when_attachments_present(self):
        r = segment_message(RATE_ANSWER, reply_mode="auto", has_attachments=True)
        self.assertEqual(r.attachment_order, ["text", "attachment", "instruction"])

    def test_empty_order_when_no_attachments(self):
        r = segment_message(RATE_ANSWER, reply_mode="auto", has_attachments=False)
        self.assertEqual(r.attachment_order, [])


class TestNoSleepInModule(unittest.TestCase):
    def test_segment_message_runs_instantly_never_sleeps(self):
        import time
        t0 = time.time()
        segment_message(RATE_ANSWER, reply_mode="auto", message_delay="natural")
        self.assertLess(time.time() - t0, 0.5)


if __name__ == "__main__":
    unittest.main()
