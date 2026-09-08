# -*- coding: utf-8 -*-
"""PHASE-6C — Request Grounding Classifier unit tests."""
import os
import unittest

os.environ.setdefault("OPENAI_API_KEY", "sk-invalid-rgc")

from services.request_grounding_classifier import classify_request_grounding, CLASSES
from services.conversation_semantics import interpret


def _c(msg):
    return classify_request_grounding(msg, interpret(msg, [])).cls


class TestGroundingClassifier(unittest.TestCase):
    def test_general_assistance(self):
        for m in ["ของแตกง่ายควรแพ็กยังไงดี", "จานเซรามิกส่งไกลๆ ห่อแบบไหนถึงจะปลอดภัย",
                  "CBM มันคำนวณยังไงเหรอ", "ทางเรือกับทางรถต่างกันตรงไหนบ้าง",
                  "ภาษีนำเข้าโดยทั่วไปคิดจากอะไร", "มือใหม่อยากนำเข้าสินค้าจากจีน เริ่มยังไงดี"]:
            self.assertEqual(_c(m), "GENERAL_ASSISTANCE", m)

    def test_business_truth_required(self):
        for m in ["ค่าตีลังไม้ของที่นี่คิดยังไง", "นโยบายคืนเงินของ Shipify เป็นยังไง",
                  "ที่นี่รับประกันของเสียหายไหม", "ค่าห่อกันกระแทกกี่บาท"]:
            self.assertEqual(_c(m), "BUSINESS_TRUTH_REQUIRED", m)

    def test_private_or_erp_required(self):
        for m in ["ยอดเงินในบัญชีผมเหลือเท่าไหร่", "ออเดอร์ที่ผมสั่งไปถึงไหนแล้ว",
                  "บิลของฉันจ่ายเงินไปหรือยัง", "เช็คสถานะพัสดุ FT99123456"]:
            self.assertEqual(_c(m), "PRIVATE_OR_ERP_REQUIRED", m)

    def test_mixed(self):
        for m in ["ของแก้วควรแพ็กยังไง แล้ว Shipify คิดค่าห่อเพิ่มไหม",
                  "พวกกระเบื้องแตกง่ายมั้ย แล้วบริษัทมีตีลังไม้ให้หรือเปล่า"]:
            self.assertEqual(_c(m), "MIXED", m)

    def test_unclear(self):
        for m in ["อันนี้เท่าไหร่", "ทำไงต่อ", "มันได้ไหม", "ช่วยดูให้หน่อย"]:
            self.assertEqual(_c(m), "UNCLEAR", m)

    def test_never_downgrades_a_business_or_private_marker_to_general(self):
        # a general-knowhow verb + a Shipify/private marker -> never GENERAL_ASSISTANCE
        for m in ["ของแตกง่ายควรแพ็กยังไง แล้ว Shipify คิดเงินไหม",
                  "ห่อของยังไงดี ของในบิลผม FT100 อ่ะ"]:
            self.assertNotEqual(_c(m), "GENERAL_ASSISTANCE", m)

    def test_output_is_always_a_known_class(self):
        for m in ["", "x", "สวัสดี", "1234", "อยากได้", "https://a.b/c"]:
            self.assertIn(_c(m), CLASSES, m)


if __name__ == "__main__":
    unittest.main()
