# -*- coding: utf-8 -*-
"""SEM-GEN-1 — deterministic pieces of the conversational semantic
backbone (frame derivation + follow-up-shape gate). The open-vocabulary
LLM resolver is exercised by tests/customer_uat/sem_gen_eval.py (needs
the live model)."""
import unittest

from services.conversation_semantics import (
    Frame,
    derive_active_frame,
    is_frame_followup,
    frame_ack_reply,
)


def _h(*pairs):
    out = []
    for role, content in pairs:
        out.append({"role": role, "content": content})
    return out


class TestFrameDerivation(unittest.TestCase):
    def test_no_history_no_frame(self):
        self.assertIsNone(derive_active_frame(None))
        self.assertIsNone(derive_active_frame([]))

    def test_product_from_user_import_interest(self):
        f = derive_active_frame(_h(("user", "สนใจนำเข้ารองเท้า"),
                                   ("assistant", "รับทราบค่ะ เป็นรองเท้านะคะ 😊 …ทางรถและทางเรือค่ะ")))
        self.assertIsNotNone(f)
        self.assertEqual(f.intent, "IMPORT_INTEREST")
        self.assertEqual(f.product, "รองเท้า")

    def test_frame_ack_wording_is_not_read_back_as_the_product(self):
        # the acknowledgement's own "จำนวนประมาณ 200 ชิ้น" text must never
        # become the product on the next turn.
        hist = _h(("user", "สนใจนำเข้ากางเกง"),
                  ("assistant", "รับทราบค่ะ เป็นกางเกงนะคะ 😊 …"),
                  ("user", "200 ตัว"),
                  ("assistant", frame_ack_reply(Frame(product="กางเกง", quantity=200), changed="quantity")))
        f = derive_active_frame(hist)
        self.assertEqual(f.product, "กางเกง")
        self.assertEqual(f.quantity, 200)

    def test_quantity_and_method_accumulate(self):
        hist = _h(("user", "สนใจนำเข้ากางเกง"),
                  ("assistant", "รับทราบค่ะ เป็นกางเกงนะคะ 😊 …"),
                  ("assistant", frame_ack_reply(Frame(product="กางเกง", quantity=300, method="sea"), changed="method")))
        f = derive_active_frame(hist)
        self.assertEqual(f.product, "กางเกง")
        self.assertEqual(f.quantity, 300)
        self.assertEqual(f.method, "sea")

    def test_frame_expires_behind_unrelated_turns(self):
        hist = _h(("user", "สนใจนำเข้าแบตเตอรี่"),
                  ("assistant", "แบตเตอรี่เป็นสินค้าต้องห้ามค่ะ"),
                  ("user", "ของผมเข้าไทยหรือยัง"), ("assistant", "กรุณาแจ้งเลขบิลค่ะ"),
                  ("user", "คูปองใช้ยังไง"), ("assistant", "ใช้ลดค่านำเข้าค่ะ"),
                  ("user", "ทางเรือกี่วัน"), ("assistant", "14–20 วันค่ะ"),
                  ("user", "เบอร์ติดต่อ"), ("assistant", "02-xxx"))
        self.assertIsNone(derive_active_frame(hist))


class TestFollowupShapeGate(unittest.TestCase):
    def test_follow_up_shapes(self):
        for m in ["ถ้าเป็นกางเกงล่ะ", "เสื้อยืดล่ะ", "หมวกล่ะ", "200 ตัว",
                  "ประมาณ 300 ชิ้น", "ไม่ใช่ 200 เอา 300", "ถ้าทางเรือล่ะ",
                  "งั้นถามเรื่องคูปองดีกว่า", "แล้วถ้าเป็นผ้าห่ม"]:
            self.assertTrue(is_frame_followup(m), m)

    def test_not_follow_up_shapes(self):
        for m in ["สั่งเยอะได้ไหม", "ขอเบอร์ติดต่อ", "ของผมเข้าไทยหรือยัง",
                  "คูปองใช้ยังไง", "สนใจนำเข้ารองเท้า",
                  "อยากทราบว่าถ้าจะสั่งของจำนวนมากจากจีนต้องทำยังไงบ้างคะ"]:
            self.assertFalse(is_frame_followup(m), m)


if __name__ == "__main__":
    unittest.main()
