import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.thai_address_parser import parse_thai_address, detect_field_correction


class TestParseThaiAddressAbbreviated(unittest.TestCase):
    def test_full_address_with_abbreviated_markers(self):
        result = parse_thai_address("8/7 ม.8 ต.ตาขัน อ.บ้านค่าย จ.ระยอง 21120")
        self.assertEqual(result, {
            "address": "8/7 ม.8", "subdistrict": "ตาขัน", "district": "บ้านค่าย",
            "province": "ระยอง", "postal_code": "21120",
        })


class TestParseThaiAddressFullWords(unittest.TestCase):
    def test_full_address_with_full_word_markers_no_postal(self):
        result = parse_thai_address("8/7 ม.8 ตำบลตาขัน อำเภอบ้านค่าย จังหวัดระยอง")
        self.assertEqual(result, {
            "address": "8/7 ม.8", "subdistrict": "ตาขัน", "district": "บ้านค่าย", "province": "ระยอง",
        })
        self.assertNotIn("postal_code", result)


class TestParseThaiAddressBangkok(unittest.TestCase):
    def test_bangkok_khet_khwaeng_variant(self):
        result = parse_thai_address("แขวงลุมพินี เขตปทุมวัน กรุงเทพมหานคร 10330")
        self.assertEqual(result["subdistrict"], "ลุมพินี")
        self.assertEqual(result["district"], "ปทุมวัน")
        self.assertEqual(result["province"], "กรุงเทพมหานคร")
        self.assertEqual(result["postal_code"], "10330")

    def test_bangkok_abbreviated_kmt(self):
        result = parse_thai_address("บ้านเลขที่ 1 แขวงสีลม เขตบางรัก กทม. 10500")
        self.assertEqual(result["province"], "กรุงเทพมหานคร")
        self.assertEqual(result["postal_code"], "10500")


class TestParseThaiAddressFullRequestBlock(unittest.TestCase):
    def test_receiver_name_and_phone_with_leading_intent_phrase(self):
        # "เปลี่ยนที่อยู่จัดส่ง" in the leading intent phrase must never be
        # mistaken for the real "ที่อยู่" address label further down.
        message = ("ช่วยเปลี่ยนที่อยู่จัดส่งในไทยของบิล SP100820260716001 ให้หน่อย\n"
                    "ผู้รับ หญิง\n0616807329\nที่อยู่ 8/7 ม.8 ต.ตาขัน อ.บ้านค่าย จ.ระยอง 21120")
        result = parse_thai_address(message)
        self.assertEqual(result["receiver_name"], "หญิง")
        self.assertEqual(result["receiver_phone"], "0616807329")
        self.assertEqual(result["address"], "8/7 ม.8")
        self.assertEqual(result["subdistrict"], "ตาขัน")
        self.assertEqual(result["district"], "บ้านค่าย")
        self.assertEqual(result["province"], "ระยอง")
        self.assertEqual(result["postal_code"], "21120")

    def test_receiver_name_full_label_variant(self):
        result = parse_thai_address("ชื่อผู้รับ สมชาย ใจดี\nที่อยู่ 99 ต.ตาขัน อ.บ้านค่าย จ.ระยอง 21120")
        self.assertEqual(result["receiver_name"], "สมชาย ใจดี")
        self.assertEqual(result["address"], "99")


class TestParseThaiAddressNoMarkers(unittest.TestCase):
    def test_no_marker_at_all_returns_empty_dict(self):
        # Confirmed live defect (2026-08-20, Production UAT): this
        # function's pre-pass runs BEFORE the main per-parameter binding
        # loop and uses setdefault(), so an earlier "treat any markerless
        # text as the address" fallback grabbed unrelated text (a bare
        # "SP1008" replayed against this action's own history slot, and
        # even the customer's own request sentence "ต้องการเปลี่ยนที่อยู่
        # บิลขนส่ง") as the address BEFORE more specific fields (CustCode)
        # ever got a chance to bind correctly. Only an EXPLICIT signal (a
        # "ที่อยู่" label, or at least one geo marker) now earns the
        # "address" attribution -- a genuinely bare, unmarked line is left
        # for the existing, already-proven-safe generic free-text
        # fallback to bind once it is truly the only thing left missing.
        self.assertEqual(parse_thai_address("บ้านเลขที่ 8/7"), {})

    def test_empty_string(self):
        self.assertEqual(parse_thai_address(""), {})
        self.assertEqual(parse_thai_address(None), {})

    def test_postal_code_alone_still_recognized(self):
        # No admin-division marker, but a trailing 5-digit run is still a
        # postal code -- useful on its own for a "missing postal code"
        # follow-up turn.
        result = parse_thai_address("21120")
        self.assertEqual(result, {"postal_code": "21120"})


class TestDetectFieldCorrection(unittest.TestCase):
    def test_province_correction(self):
        self.assertEqual(detect_field_correction("จังหวัดผิด เป็นชลบุรี"), ("province", "ชลบุรี"))

    def test_postal_code_correction_alternate_phrasing(self):
        self.assertEqual(detect_field_correction("รหัสไปรษณีย์ผิด ที่ถูกคือ 21121"), ("postal_code", "21121"))

    def test_receiver_phone_correction(self):
        self.assertEqual(detect_field_correction("เบอร์โทรผิด เป็น0891234567"), ("receiver_phone", "0891234567"))

    def test_no_correction_cue_returns_none(self):
        self.assertIsNone(detect_field_correction("สวัสดีค่ะ"))

    def test_field_name_without_cue_returns_none(self):
        # Names a field but has no correction cue ("ผิด"/"เปลี่ยนเป็น"/etc.)
        # -- must not be misread as a correction.
        self.assertIsNone(detect_field_correction("จังหวัดระยองอยู่ตรงไหน"))

    def test_cue_without_new_value_returns_none(self):
        # "ผิด" present but no "เป็น"/"คือ" naming the replacement value --
        # never guess what the customer meant.
        self.assertIsNone(detect_field_correction("จังหวัดผิดค่ะ"))

    def test_empty_and_none_input(self):
        self.assertIsNone(detect_field_correction(""))
        self.assertIsNone(detect_field_correction(None))


if __name__ == "__main__":
    unittest.main()
