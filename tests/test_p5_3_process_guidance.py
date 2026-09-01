"""P5.3 — a GENERIC ordering/import process question retrieves the
high-level Shipify service journey, not the marketplace/link sub-flow.

Unit-level: the generic_process_component() gate. The full retrieval/
answer shape is verified end-to-end separately.
"""
import unittest

from rag.query_resolution import generic_process_component as g


class GenericProcessIsEnriched(unittest.TestCase):
    GENERIC = [
        "ขอขั้นตอนในการสั่งซื้อสินค้าหน่อย",          # the real-LINE query
        "ขั้นตอนนำเข้าสินค้าจากจีนมีอะไรบ้าง",
        "อยากนำเข้าสินค้าจากจีน ต้องทำยังไง",
        "เริ่มใช้บริการ Shipify ยังไง",
        "ขั้นตอนการสั่งของจากจีนทำยังไง",
        "วิธีการนำเข้าสินค้าจากจีน",
    ]

    def test_generic_process_questions_get_import_process_enrichment(self):
        for q in self.GENERIC:
            comp = g(q)
            self.assertIsNotNone(comp, msg=q)
            self.assertEqual(comp[0], "import_process", msg=q)
            self.assertIn("นำเข้าสินค้าจากจีนเข้าไทย", comp[1], msg=q)


class MarketplaceSpecificIsNotTouched(unittest.TestCase):
    SPECIFIC = [
        "สั่งของจาก Taobao ยังไง",
        "วางลิงก์สินค้า 1688 ยังไง",
        "รองรับ 1688 ไหม",
        "รองรับเว็บอะไรบ้าง",
        "ส่งลิงก์สินค้าให้เจ้าหน้าที่ได้ไหม",
        "สั่งสินค้าจากเว็บจีนอะไรได้บ้าง",
    ]

    def test_marketplace_or_link_questions_are_left_alone(self):
        for q in self.SPECIFIC:
            self.assertIsNone(g(q), msg=q)


class UnrelatedQuestionsAreNotEnriched(unittest.TestCase):
    OTHER = [
        "อยากนำเข้าสินค้าจากจีน",          # P5.1 turn 1 — no process marker
        "รองเท้าครับ",                      # P5.1 product answer
        "ค่าขนส่งเท่าไหร่",
        "โกดังอ่อนนุชเปิดกี่โมง",
        "ใช้คูปองยังไง",
        "น้ำหอมนำเข้าได้ไหม",
        "ทางรถกับทางเรือกี่วัน",
    ]

    def test_no_false_enrichment(self):
        for q in self.OTHER:
            self.assertIsNone(g(q), msg=q)


if __name__ == "__main__":
    unittest.main()
