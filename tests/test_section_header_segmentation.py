"""Regression tests for Section-Header Segmentation (P0, 2026-07-22):
GPT can produce a multi-section answer (explanation / sea rate / road
rate / summary) using section-header LINES separated only by single
newlines — no blank line, no bullet marker. The existing paragraph-break
and bullet-block checks both miss this shape entirely; this fix adds a
third, generic structural signal (emoji/markdown/colon header lines)
without weakening the segmenter's conservative defaults.
"""
import unittest

from services.message_segmenter import segment_message


class TestEmojiSectionHeaders(unittest.TestCase):
    def test_emoji_headers_separated_by_single_newlines_split(self):
        answer = ("คำนวณจากขนาด 40 × 60 × 30 ซม. และน้ำหนัก 100 กก. ได้ปริมาตร 0.072 CBM ค่ะ\n"
                   "🚢 ทางเรือ\nคิดตามน้ำหนัก 1,900 บาท\n"
                   "🚚 ทางรถ\nคิดตามน้ำหนัก 3,500 บาท\n"
                   "📦 สรุป\nทางเรือ 1,900 บาท และทางรถ 3,500 บาท")
        result = segment_message(answer, reply_mode="auto")
        self.assertTrue(result.segmentation_applied)
        self.assertGreaterEqual(result.message_count, 2)
        self.assertLessEqual(result.message_count, 4)
        self.assertIn("section_header", result.boundary_types)

    def test_other_emoji_headers_are_generically_recognized(self):
        """Never a hardcoded shipping-only word list — 📍/📞/✅/⚠️ headers
        must be recognized the same way."""
        answer = "📍 ที่อยู่\nโกดังกรุงเทพ\n📞 ติดต่อ\n02-026-6426"
        result = segment_message(answer, reply_mode="auto")
        self.assertTrue(result.segmentation_applied)
        self.assertEqual(result.message_count, 2)


class TestMarkdownHeaders(unittest.TestCase):
    def test_markdown_headers_split(self):
        answer = "## ทางเรือ\nคิดตามน้ำหนัก 1,900 บาท\n### ทางรถ\nคิดตามน้ำหนัก 3,500 บาท"
        result = segment_message(answer, reply_mode="auto")
        self.assertTrue(result.segmentation_applied)
        self.assertEqual(result.message_count, 2)


class TestColonStyleHeaders(unittest.TestCase):
    def test_short_colon_only_headers_split(self):
        answer = "ทางเรือ:\nคิดตามน้ำหนัก 1,900 บาท\nทางรถ:\nคิดตามน้ำหนัก 3,500 บาท"
        result = segment_message(answer, reply_mode="auto")
        self.assertTrue(result.segmentation_applied)
        self.assertEqual(result.message_count, 2)

    def test_label_value_line_is_not_a_header_boundary(self):
        """"ราคา: 500 บาท" (content on the SAME line as the colon) is
        ordinary content, never a section-header boundary — this is
        what keeps an FAQ answer full of "Label: value" lines from being
        split line by line."""
        answer = "ราคา: 500 บาท\nน้ำหนัก: 2 กก.\nขนาด: 10x10x10 ซม."
        result = segment_message(answer, reply_mode="auto")
        self.assertFalse(result.segmentation_applied)


class TestShippingCalculationLogicalSegments(unittest.TestCase):
    def test_shipping_calculation_becomes_2_to_4_segments(self):
        answer = ("สำหรับการคำนวณค่าขนส่ง ขนาด 40x60x30 cm และน้ำหนัก 12 kg:\n"
                   "🚢 ทางเรือ\nปริมาตร 0.072 cbm x 4500 บาท = 324 บาท\n"
                   "🚚 ทางรถ\nปริมาตร 0.072 cbm x 6900 บาท = 496.8 บาท\n"
                   "📦 สรุป\nทางเรือ 324 บาท ทางรถ 496.8 บาท")
        result = segment_message(answer, reply_mode="auto", max_messages=4)
        self.assertGreaterEqual(result.message_count, 2)
        self.assertLessEqual(result.message_count, 4)
        # Every produced part must be non-empty and self-contained.
        for part in result.message_parts:
            self.assertTrue(part.strip())


class TestAddressBlockNotOverSplit(unittest.TestCase):
    def test_multi_line_address_is_not_split_line_by_line(self):
        answer = ("โกดังไทยอยู่ที่ 76 ซอยเจริญเผ่า-สุขใย\n"
                   "ตำบลบางม่วง บางใหญ่\n"
                   "นนทบุรี 11140\n"
                   "โทร 091-5050-775")
        result = segment_message(answer, reply_mode="auto")
        self.assertFalse(result.segmentation_applied)
        self.assertEqual(result.message_count, 1)

    def test_two_labeled_warehouse_locations_split_logically_not_per_line(self):
        answer = ("📍 โกดังไทย\nที่อยู่ 76 ซอยเจริญเผ่า-สุขใย ตำบลบางม่วง บางใหญ่ นนทบุรี\n"
                   "📍 โกดังจีน\nที่อยู่ตามรูปแนบ ติดต่อร้านค้าจีนโดยตรง")
        result = segment_message(answer, reply_mode="auto")
        self.assertTrue(result.segmentation_applied)
        # Logical (one bubble per location), never one bubble per line.
        self.assertLess(result.message_count, len([l for l in answer.split("\n") if l.strip()]))
        self.assertEqual(result.message_count, 2)


class TestShortAnswersRemainOneMessage(unittest.TestCase):
    def test_short_contact_answer_remains_one_message(self):
        result = segment_message("Shipify ฝ่ายบริการลูกค้า: 02-026-6426", reply_mode="auto")
        self.assertEqual(result.message_count, 1)
        self.assertFalse(result.segmentation_applied)

    def test_clarification_question_remains_one_message(self):
        result = segment_message("ต้องการเบอร์ติดต่อโกดังไทยหรือโกดังจีนคะ", reply_mode="auto")
        self.assertEqual(result.message_count, 1)

    def test_clarification_is_never_split_even_in_auto_mode(self):
        result = segment_message("ต้องการเบอร์ติดต่อโกดังไทยหรือโกดังจีนคะ", reply_mode="auto",
                                  is_escalation=False, is_fallback=False)
        self.assertEqual(result.message_count, 1)

    def test_incomplete_slot_filling_response_remains_one_message(self):
        answer = ("ได้รับขนาด 4 × 6 แล้วค่ะ\nแต่ยังขาดความสูง หน่วยของขนาด น้ำหนักสินค้า\n\n"
                   "รบกวนแจ้งในรูปแบบ เช่น:\n40 × 60 × 30 ซม. น้ำหนัก 12 กก.")
        # This is the exact single-message path used by
        # services/playground_orchestrator.py (reply_mode forced to
        # "single" for an active slot-filling turn) — verified directly.
        result = segment_message(answer, reply_mode="single")
        self.assertEqual(result.message_count, 1)

    def test_bare_math_result_remains_one_message(self):
        result = segment_message("4*6 = 24", reply_mode="auto")
        self.assertEqual(result.message_count, 1)


class TestUrlsStayAttachedToCorrectSection(unittest.TestCase):
    def test_url_remains_inside_its_own_section_not_split_out(self):
        answer = ("📦 สรุป\nดูรายละเอียดเพิ่มเติมได้ที่ https://shipify.example.com/rates\n"
                   "📞 ติดต่อ\n02-026-6426")
        result = segment_message(answer, reply_mode="auto")
        self.assertTrue(result.segmentation_applied)
        url_part = [p for p in result.message_parts if "https://" in p]
        self.assertEqual(len(url_part), 1)
        self.assertIn("https://shipify.example.com/rates", url_part[0])
        self.assertIn("สรุป", url_part[0])


class TestNoDuplicatedGreetingsOrClosings(unittest.TestCase):
    def test_repeated_support_sentence_across_sections_is_not_duplicated(self):
        answer = ("🚢 ทางเรือ\nคิดตามน้ำหนัก 1,900 บาท ทีมงานจะติดต่อกลับ\n"
                   "🚚 ทางรถ\nคิดตามน้ำหนัก 3,500 บาท ทีมงานจะติดต่อกลับ")
        result = segment_message(answer, reply_mode="auto")
        support_hits = sum(1 for p in result.message_parts if "ทีมงานจะติดต่อกลับ" in p)
        self.assertEqual(support_hits, 1)


if __name__ == "__main__":
    unittest.main()
