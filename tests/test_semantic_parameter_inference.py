"""Tests for services/semantic_parameter_inference.py — the generic,
metadata-driven natural-language filter layer (2026-08-09 ERP
semantic-filter sprint). Pure unit tests, no DB, no network: every
signal comes from a parameter's own field_metadata dict, passed in
directly, so these tests exercise the module's logic in isolation from
the Decision Engine wiring (covered separately in
tests/test_decision_engine.py::TestSemanticParameterInference)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.semantic_parameter_inference import (
    infer_enum_parameters,
    infer_numeric_limit_parameters,
    infer_date_range_parameters,
    infer_semantic_parameters,
)

BILL_STATUS_PARAM = {
    "name": "BillStatus",
    "field_metadata": {"enum": {
        "1": {"label": "received_china", "phrases": ["เข้าโกดังจีน", "ถึงโกดังจีน", "รับเข้าที่จีน"]},
        "3": {"label": "exported_china", "phrases": ["ส่งออกจากจีน", "ออกจากจีนแล้ว", "export แล้ว"]},
        "4": {"label": "received_thailand", "phrases": ["เข้าโกดังไทย", "ถึงไทยแล้ว", "รับเข้าที่ไทย"]},
        "6": {"label": "delivered", "phrases": ["ส่งถึงแล้ว", "จัดส่งสำเร็จ", "ลูกค้าได้รับแล้ว"]},
    }},
}

PO_STATUS_PARAM = {
    "name": "POStatus",
    "field_metadata": {"enum": {
        "3": {"phrases": ["สั่งซื้อสำเร็จ", "ซื้อสำเร็จ"]},
        "5": {"phrases": ["ยกเลิก", "คืนเงิน"]},
    }},
}

LATEST_PARAM = {"name": "Latest", "field_metadata": {"numeric_limit": True}}

DATE_PARAMS = [
    {"name": "ExportDateStart", "field_metadata": {"date_range_group": "ExportDateEnd"}},
    {"name": "ExportDateEnd", "field_metadata": {}},
]

MULTI_DATE_PARAMS = DATE_PARAMS + [
    {"name": "ReceivedDateStart", "field_metadata": {"date_range_group": "ReceivedDateEnd"}},
    {"name": "ReceivedDateEnd", "field_metadata": {}},
]


class TestEnumPhraseMatching(unittest.TestCase):
    def test_matches_configured_phrase(self):
        result = infer_enum_parameters([BILL_STATUS_PARAM], "ขอดูพัสดุ SP1014 ที่ส่งออกจากจีนแล้ว")
        self.assertEqual(result, {"BillStatus": "3"})

    def test_matches_a_different_enum_value(self):
        result = infer_enum_parameters([BILL_STATUS_PARAM], "ขอดูพัสดุ SP1014 ที่เข้าโกดังไทยแล้ว")
        self.assertEqual(result, {"BillStatus": "4"})

    def test_po_status_phrase(self):
        result = infer_enum_parameters([PO_STATUS_PARAM], "ขอดู PO SP1014 ที่สั่งซื้อสำเร็จ")
        self.assertEqual(result, {"POStatus": "3"})

    def test_no_phrase_present_returns_nothing(self):
        result = infer_enum_parameters([BILL_STATUS_PARAM], "ขอดูพัสดุของ SP1014")
        self.assertEqual(result, {})

    def test_parameter_without_enum_metadata_is_ignored(self):
        plain_param = {"name": "CustCode", "field_metadata": {}}
        result = infer_enum_parameters([plain_param], "ส่งออกจากจีนแล้ว")
        self.assertEqual(result, {})

    def test_empty_message_returns_nothing(self):
        self.assertEqual(infer_enum_parameters([BILL_STATUS_PARAM], ""), {})


class TestNumericLimitExtraction(unittest.TestCase):
    def test_number_before_keyword_with_intervening_noun(self):
        result = infer_numeric_limit_parameters([LATEST_PARAM], "ขอดู 3 พัสดุล่าสุดของ SP1014")
        self.assertEqual(result, {"Latest": "3"})

    def test_number_before_keyword_with_spaced_noun(self):
        result = infer_numeric_limit_parameters([LATEST_PARAM], "ขอดู 2 PO ล่าสุดของ SP1014")
        self.assertEqual(result, {"Latest": "2"})

    def test_number_after_keyword(self):
        result = infer_numeric_limit_parameters([LATEST_PARAM], "ล่าสุด 3 รายการ")
        self.assertEqual(result, {"Latest": "3"})

    def test_kho_ao_leading_phrase_without_keyword(self):
        result = infer_numeric_limit_parameters([LATEST_PARAM], "ขอ 10 รายการ")
        self.assertEqual(result, {"Latest": "10"})

    def test_ao_leading_phrase_glued_to_keyword(self):
        result = infer_numeric_limit_parameters([LATEST_PARAM], "เอา 5 อันล่าสุด")
        self.assertEqual(result, {"Latest": "5"})

    def test_no_count_stated_returns_nothing(self):
        result = infer_numeric_limit_parameters([LATEST_PARAM], "ขอดูพัสดุของ SP1014")
        self.assertEqual(result, {})

    def test_digit_inside_customer_code_is_not_treated_as_a_limit(self):
        result = infer_numeric_limit_parameters([LATEST_PARAM], "ขอดูพัสดุของ SP1014")
        self.assertNotIn("Latest", result)

    def test_digit_inside_customer_code_adjacent_to_keyword_is_not_a_limit(self):
        """Confirmed live bug (2026-08-13, Customer Server UAT) — unlike
        the case above, "ล่าสุด" IS present here and sits right next to the
        customer code with no stated count anywhere, which used to make
        the window-based digit search read "SP1014"'s own digits as
        Latest=1014 (then, in one live case, =014 after a first attempted
        fix). Both of the customer's own example phrasings are covered
        here verbatim."""
        result = infer_numeric_limit_parameters([LATEST_PARAM], "ขอดู PO ล่าสุดของ SP1014")
        self.assertNotIn("Latest", result)
        result = infer_numeric_limit_parameters([LATEST_PARAM], "ขอดูพัสดุล่าสุดของ SP1014")
        self.assertNotIn("Latest", result)

    def test_parameter_without_numeric_limit_metadata_is_ignored(self):
        plain_param = {"name": "CustCode", "field_metadata": {}}
        result = infer_numeric_limit_parameters([plain_param], "3 รายการล่าสุด")
        self.assertEqual(result, {})


class TestDateRangeInference(unittest.TestCase):
    def test_no_date_range_group_configured_returns_nothing(self):
        result = infer_date_range_parameters([{"name": "X", "field_metadata": {}}], "วันนี้")
        self.assertEqual(result, {})

    def test_relative_phrase_today(self):
        from datetime import datetime
        now = datetime(2026, 8, 9)
        result = infer_date_range_parameters(DATE_PARAMS, "ขอดูพัสดุวันนี้", now=now)
        self.assertEqual(result, {"ExportDateStart": "2026-08-09", "ExportDateEnd": "2026-08-09"})

    def test_relative_phrase_this_month(self):
        from datetime import datetime
        now = datetime(2026, 8, 9)
        result = infer_date_range_parameters(DATE_PARAMS, "ขอดูพัสดุเดือนนี้", now=now)
        self.assertEqual(result, {"ExportDateStart": "2026-08-01", "ExportDateEnd": "2026-08-09"})

    def test_absolute_iso_date(self):
        result = infer_date_range_parameters(DATE_PARAMS, "ขอดูพัสดุวันที่ 2026-05-22")
        self.assertEqual(result, {"ExportDateStart": "2026-05-22", "ExportDateEnd": "2026-05-22"})

    def test_absolute_dmy_date(self):
        result = infer_date_range_parameters(DATE_PARAMS, "ขอดูพัสดุวันที่ 22/05/2026")
        self.assertEqual(result, {"ExportDateStart": "2026-05-22", "ExportDateEnd": "2026-05-22"})

    def test_multiple_date_groups_with_ambiguous_wording_does_not_guess(self):
        result = infer_date_range_parameters(MULTI_DATE_PARAMS, "ขอดูพัสดุวันนี้")
        self.assertEqual(result, {})

    def test_multiple_date_groups_with_no_date_phrase_returns_nothing(self):
        result = infer_date_range_parameters(MULTI_DATE_PARAMS, "ขอดูพัสดุของ SP1014")
        self.assertEqual(result, {})


class TestInferSemanticParametersEntryPoint(unittest.TestCase):
    def test_merges_enum_and_numeric_limit(self):
        params = [BILL_STATUS_PARAM, LATEST_PARAM]
        result = infer_semantic_parameters(params, "ขอดู 3 พัสดุล่าสุด SP1014 ที่ส่งออกจากจีนแล้ว")
        self.assertEqual(result, {"BillStatus": "3", "Latest": "3"})

    def test_action_with_no_field_metadata_returns_empty(self):
        params = [{"name": "CustCode", "field_metadata": {}}, {"name": "OrderCode"}]
        result = infer_semantic_parameters(params, "ขอดู 3 รายการล่าสุด ที่ส่งออกจากจีนแล้ว")
        self.assertEqual(result, {})


if __name__ == "__main__":
    unittest.main()
