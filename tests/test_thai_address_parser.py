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

    def test_bare_shipment_code_never_fabricates_a_postal_code(self):
        # Address Change Full UAT fix (2026-08-24) — confirmed live: a
        # bare ShipmentCode reply ("SP100820260716001", answering this
        # action's OWN ShipmentCode parameter) was silently fabricating
        # postal_code="16001" from its own trailing digits, corrupting
        # the confirmation summary with a postal code the customer never
        # typed. A trailing digit run embedded inside a longer ASCII
        # identifier is never a postal code.
        self.assertEqual(parse_thai_address("SP100820260716001"), {})
        self.assertEqual(parse_thai_address("PO100820260815001"), {})
        self.assertEqual(parse_thai_address("FT318220260726001"), {})

    def test_postal_code_immediately_after_colon_no_space_still_recognized(self):
        self.assertEqual(parse_thai_address("รหัสไปรษณีย์:21120"), {"postal_code": "21120"})

    def test_casual_no_label_phrasing_with_receiver_marker_before_name(self):
        # Address Change Full UAT fix (2026-08-24) — a real customer
        # phrasing: "ผู้รับ" immediately followed by "ชื่อ" (reversed word
        # order vs. the usual "ชื่อผู้รับ"), a bare "เบอร์" phone label,
        # and a bare "อยู่" address label with no "ที่" prefix. Every
        # field must resolve cleanly, with none of "เบอร์"/"อยู่"/"ชื่อ"
        # leaking into a neighbouring value.
        result = parse_thai_address(
            "ผู้รับชื่อสมชาย เบอร์ 0812345678 อยู่ 99/12 หมู่ 4 "
            "ตำบลบางแก้ว อำเภอบางพลี จังหวัดสมุทรปราการ 10540")
        self.assertEqual(result, {
            "receiver_name": "สมชาย", "receiver_phone": "0812345678",
            "address": "99/12 หมู่ 4", "subdistrict": "บางแก้ว",
            "district": "บางพลี", "province": "สมุทรปราการ", "postal_code": "10540",
        })

    def test_postal_code_immediately_after_thai_text_no_space_still_recognized(self):
        # Thai-script adjacency (no space before the digits) must remain
        # unaffected by the ASCII-only boundary guard above.
        result = parse_thai_address("8/7 ม.8 ต.ตาขัน อ.บ้านค่าย จ.ระยอง21120")
        self.assertEqual(result["postal_code"], "21120")
        self.assertEqual(result["province"], "ระยอง")

    def test_postal_code_alone_still_recognized(self):
        # No admin-division marker, but a trailing 5-digit run is still a
        # postal code -- useful on its own for a "missing postal code"
        # follow-up turn.
        result = parse_thai_address("21120")
        self.assertEqual(result, {"postal_code": "21120"})


class TestParseThaiAddressColonLabeled(unittest.TestCase):
    """Colon-labeled format fix (2026-08-24, Production Safety Check) —
    confirmed live: a customer typing "ตำบล: ตาขัน" style labels (rather
    than the compact "ต.ตาขัน" form) got a leading ": " left in every geo
    value, and Province (the last geo marker, with nothing recognized to
    bound it) swallowed the next field's own "รหัสไปรษณีย์:" label text
    whole. Covers cases A-H from the fix's own requirement list."""

    FULL_ADDRESS_COLON = (
        "เลขที่บิล: SP100820260716001\n"
        "ผู้รับ: ทดสอบระบบ\n"
        "เบอร์โทร: 0616807329\n"
        "ที่อยู่: 8/7 ม.8\n"
        "ตำบล: ตาขัน\n"
        "อำเภอ: บ้านค่าย\n"
        "จังหวัด: ระยอง\n"
        "รหัสไปรษณีย์: 21120"
    )

    # A — full multiline colon-labeled address.
    def test_A_full_multiline_colon_labeled_address(self):
        result = parse_thai_address(self.FULL_ADDRESS_COLON)
        self.assertEqual(result["receiver_name"], "ทดสอบระบบ")
        self.assertEqual(result["receiver_phone"], "0616807329")
        self.assertEqual(result["address"], "8/7 ม.8")
        self.assertEqual(result["subdistrict"], "ตาขัน")
        self.assertEqual(result["district"], "บ้านค่าย")
        self.assertEqual(result["province"], "ระยอง")
        self.assertEqual(result["postal_code"], "21120")
        # No leading ":" garbage and no swallowed next-field label on any value.
        for value in result.values():
            self.assertFalse(value.startswith(":"), f"leading colon leaked into: {value!r}")
            self.assertNotIn("รหัสไปรษณีย์", value)

    # B — colon with no spaces on either side.
    def test_B_colon_no_spaces(self):
        result = parse_thai_address("ที่อยู่:8/7 ม.8\nตำบล:ตาขัน\nอำเภอ:บ้านค่าย\nจังหวัด:ระยอง\nรหัสไปรษณีย์:21120")
        self.assertEqual(result["address"], "8/7 ม.8")
        self.assertEqual(result["subdistrict"], "ตาขัน")
        self.assertEqual(result["district"], "บ้านค่าย")
        self.assertEqual(result["province"], "ระยอง")
        self.assertEqual(result["postal_code"], "21120")

    # C — spaces around the colon, both sides and colon-with-leading-space-only.
    def test_C_spaces_around_colon(self):
        result = parse_thai_address("ตำบล : ตาขัน\nอำเภอ :บ้านค่าย\nจังหวัด: ระยอง")
        self.assertEqual(result["subdistrict"], "ตาขัน")
        self.assertEqual(result["district"], "บ้านค่าย")
        self.assertEqual(result["province"], "ระยอง")

    # D — Province immediately followed by a labeled PostalCode line must
    # never bleed into one value.
    def test_D_province_followed_by_postal_code_label(self):
        result = parse_thai_address("จังหวัด: ระยอง\nรหัสไปรษณีย์: 21120")
        self.assertEqual(result["province"], "ระยอง")
        self.assertEqual(result["postal_code"], "21120")
        self.assertNotIn("รหัสไปรษณีย์", result["province"])

    # E — existing compact space-separated format must still work
    # unchanged (no regression from the colon-handling fix).
    def test_E_existing_compact_format_unaffected(self):
        result = parse_thai_address("8/7 ม.8 ต.ตาขัน อ.บ้านค่าย จ.ระยอง 21120")
        self.assertEqual(result, {
            "address": "8/7 ม.8", "subdistrict": "ตาขัน", "district": "บ้านค่าย",
            "province": "ระยอง", "postal_code": "21120",
        })

    # F — partial colon-labeled address (only some fields given) must
    # still parse cleanly, never inventing the missing ones.
    def test_F_partial_colon_labeled_address(self):
        result = parse_thai_address("ที่อยู่: 8/7 ม.8\nตำบล: ตาขัน")
        self.assertEqual(result["address"], "8/7 ม.8")
        self.assertEqual(result["subdistrict"], "ตาขัน")
        self.assertNotIn("district", result)
        self.assertNotIn("province", result)
        self.assertNotIn("postal_code", result)

    # G — field correction must be unaffected by this fix (detect_field_
    # correction is a separate function from parse_thai_address).
    def test_G_field_correction_unaffected(self):
        self.assertEqual(detect_field_correction("จังหวัดผิด เป็นชลบุรี"), ("province", "ชลบุรี"))

    # H — the intent phrase itself must never be parsed as an address,
    # colon-handling change included.
    def test_H_intent_phrase_never_parsed_as_address(self):
        self.assertEqual(parse_thai_address("ต้องการเปลี่ยนที่อยู่บิลขนส่ง"), {})


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
