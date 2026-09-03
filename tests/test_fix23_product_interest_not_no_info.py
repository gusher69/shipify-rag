# -*- coding: utf-8 -*-
"""FIX-2.3 — product / import interest must NOT become a TRUE no-info
Human CS handoff.

REAL LINE failure (2026-09-03, session 6c9b9026):
    "สั่งเยอะได้ไหม" -> (PPC-1 clarification)
    "สนใจนำเข้ารองเท้า"
    -> bot: "ตอนนี้ยังไม่มีข้อมูลยืนยันเรื่องนี้ค่ะ เดี๋ยวทางเราประสาน
             เจ้าหน้าที่ช่วยตรวจสอบเพิ่มเติมให้นะคะ"  (HUMAN_HANDOFF)

"สนใจนำเข้ารองเท้า" is early sales / import interest, not an understood
company-policy fact question. The Answerability Gate promoted it to
`unsupported_company_fact` merely because every retrieved chunk was
RELATED_CONTEXT. Fix-2 stays reserved for: an understood COMPANY FACT
question + trusted search performed + trusted answer genuinely
unavailable (e.g. "Shipify รับประกันว่า…ผ่านศุลกากรไหม").
"""
import inspect
import re
import unittest

import services.playground_orchestrator as po
from services.playground_orchestrator import (
    _is_product_import_interest,
    _product_interest_noun,
)


class TestProductImportInterestRecognizer(unittest.TestCase):
    def test_product_interest_declaratives(self):
        for q in ["สนใจนำเข้ารองเท้า", "อยากนำเข้าเสื้อผ้า", "จะนำเข้าอะไหล่",
                  "กำลังสนใจสั่งของจากจีน", "ต้องการฝากสั่งกระเป๋า",
                  "สนใจนำเข้าน้ำยาปรับผ้านุ่ม"]:
            self.assertTrue(_is_product_import_interest(q), q)

    def test_company_guarantee_question_is_not_product_interest(self):
        # this stays TRUE no-info -> Fix-2
        for q in ["Shipify รับประกันว่าสินค้าทุกชิ้นจะผ่านศุลกากรไหม",
                  "ทางบริษัทการันตีไหมคะว่าของจะผ่านด่านทุกครั้ง",
                  "รับผิดชอบไหมถ้าของเสียหาย"]:
            self.assertFalse(_is_product_import_interest(q), q)

    def test_plain_public_or_clarification_turns_are_not_product_interest(self):
        for q in ["ขอเบอร์ติดต่อ", "คูปองใช้ยังไง", "สั่งเยอะได้ไหม",
                  "ค่าส่งเท่าไหร่", "บริษัทมีนโยบายรีไซเคิลกล่องยังไง",
                  "โกดังอยู่ที่ไหน"]:
            self.assertFalse(_is_product_import_interest(q), q)

    def test_noun_extraction_is_a_generic_strip_not_a_dictionary(self):
        self.assertEqual(_product_interest_noun("สนใจนำเข้ารองเท้า"), "รองเท้า")
        self.assertEqual(_product_interest_noun("อยากนำเข้าเสื้อผ้า"), "เสื้อผ้า")
        self.assertEqual(_product_interest_noun("จะนำเข้าอะไหล่"), "อะไหล่")
        # nothing concrete left -> None (generic acknowledgement path)
        self.assertIsNone(_product_interest_noun("กำลังสนใจสั่งของจากจีน"))


class TestAnswerabilityGateStructure(unittest.TestCase):
    """The product/import-interest guard must sit inside the Answerability
    Gate branch, BEFORE `unsupported_company_fact = True`, and must not
    itself set that flag."""

    def setUp(self):
        self.src = inspect.getsource(po.run_playground_turn)

    def test_flag_still_set_in_exactly_two_places(self):
        # FIX-2.3 adds a NEW branch that does NOT set the flag — the count
        # stays 2 (Answerability Gate + P7.1), same as Fix-2.
        self.assertEqual(self.src.count("unsupported_company_fact = True"), 2)

    def test_product_interest_branch_precedes_the_flag_and_omits_it(self):
        marker = "_is_product_import_interest(question)"
        self.assertIn(marker, self.src)
        # the branch body: from the elif to the next `else:` at the same
        # indent (the deterministic no-info branch that sets the flag).
        after = self.src.split(marker, 1)[1]
        branch_body = after.split("\n        else:\n", 1)[0]
        self.assertNotIn("unsupported_company_fact = True", branch_body)
        self.assertIn("FIX-2.3", branch_body)

    def test_guarantee_question_excluded_from_the_guard(self):
        src = inspect.getsource(po._is_product_import_interest)
        self.assertIn("_COMPANY_GUARANTEE_Q_RE", src)


if __name__ == "__main__":
    unittest.main()
