# -*- coding: utf-8 -*-
"""services/language/thai_normalizer.py — the ONE input-normalisation layer.

Contracts (task §1/§2/§3/§4/§9/§11):
  * raw_text is never overwritten; normalized_text is a second field
  * structured identifiers survive byte-for-byte (IDENTIFIER MUTATION = 0)
  * a marks-only slip on a bounded-vocabulary word is corrected
  * MEDIUM candidates are produced, never applied here
  * a dictionary word of 4+ letters is never rewritten without evidence
  * unknown product names are preserved exactly
  * fuzzy matching only ever lands on the bounded vocabulary
  * degrade-safe: never raises
"""
import unittest

from services.language import (normalize, normalize_history, mask_identifiers,
                               skeleton, is_dictionary_word, oov_count)
from services.language import thai_normalizer as N
from services.language import vocabulary as V


class TestRawTextIsNeverOverwritten(unittest.TestCase):

    def test_both_texts_are_returned(self):
        r = normalize("เป้นรองเท้าคับ")
        self.assertEqual(r.raw_text, "เป้นรองเท้าคับ")
        self.assertEqual(r.normalized_text, "เป็นรองเท้าครับ")
        self.assertTrue(r.changed)

    def test_clean_text_is_untouched_and_confident(self):
        r = normalize("อยากสั่งของจากจีน")
        self.assertEqual(r.normalized_text, "อยากสั่งของจากจีน")
        self.assertFalse(r.changed)
        self.assertEqual(r.confidence, 1.0)
        self.assertEqual(r.applied, [])

    def test_history_user_turns_only(self):
        h = normalize_history([{"role": "user", "content": "ทางเรือคับ"},
                               {"role": "assistant", "content": "รบกวนแจ้งชื่อสินค้าคับ"}])
        self.assertEqual(h[0]["content"], "ทางเรือครับ")
        self.assertEqual(h[0]["raw_content"], "ทางเรือคับ")
        self.assertEqual(h[1]["content"], "รบกวนแจ้งชื่อสินค้าคับ")


class TestCharacterNormalisation(unittest.TestCase):

    def test_duplicated_vowel_mark(self):
        self.assertEqual(normalize("ใบกำกับภาษีี").normalized_text, "ใบกำกับภาษี")

    def test_elongation(self):
        self.assertEqual(normalize("อยากสั่งของจากจีนครับบบบ").normalized_text, "อยากสั่งของจากจีนครับ")

    def test_whitespace_zero_width_and_nbsp(self):
        self.assertEqual(normalize("ขอ  เช็ค   สถานะ  หน่อย").normalized_text, "ขอ เช็ค สถานะ หน่อย")
        self.assertEqual(normalize("อยาก​สั่งของ​จากจีน").normalized_text, "อยากสั่งของจากจีน")
        self.assertEqual(normalize("20 คู่").normalized_text, "20 คู่")

    def test_space_between_number_and_unit_is_kept(self):
        self.assertEqual(normalize("20 คู่อยากสั่งของจากจีน").normalized_text, "20 คู่อยากสั่งของจากจีน")


class TestParticles(unittest.TestCase):

    def test_informal_particles_become_canonical(self):
        for noisy, clean in (("รองเท้า คับ", "รองเท้า ครับ"), ("ค่าส่งเท่าไหร่ค้าบ", "ค่าส่งเท่าไหร่ครับ"),
                             ("ไม่เอาแล้วคับ", "ไม่เอาแล้วครับ"), ("ได้ป่าว", "ได้ป่ะ")):
            self.assertEqual(normalize(noisy).normalized_text, clean, noisy)

    def test_particle_inside_a_noun_is_not_touched(self):
        self.assertEqual(normalize("เรือบังคับครับ").normalized_text, "เรือบังคับครับ")

    def test_the_real_word_ครบ_is_guarded(self):
        self.assertEqual(normalize("ได้รับของครบ").normalized_text, "ได้รับของครบ")
        self.assertEqual(normalize("รองเท้าครบ").normalized_text, "รองเท้าครับ")


class TestBoundedVocabularyCorrection(unittest.TestCase):

    def test_owner_typos(self):
        cases = {
            "20คุ่อยากสั่งขงจากจีน": "20คู่อยากสั่งของจากจีน",
            "ส่งเรื่อได้ปะ": "ส่งเรือได้ปะ",
            "กระต่ายนำเข่าได้หรอ": "กระต่ายนำเข้าได้หรอ",
            "ขอเช็คออเดอของผมหนอย": "ขอเช็คออเดอร์ของผมหน่อย",
            "ของถึงไทหรือยัง": "ของถึงไทยหรือยัง",
            "ถอนเงืน": "ถอนเงิน", "ยกเลก": "ยกเลิก", "คุปอง": "คูปอง",
            "เช็คออเดอ": "เช็คออเดอร์", "ส่งเรื่อ": "ส่งเรือ", "สั่งของจน": "สั่งของจีน",
            "นำเข่า": "นำเข้า", "นำเขา": "นำเข้า", "เปน": "เป็น", "เป้นน": "เป็น",
            "อยากได้ 5 ชินครับ": "อยากได้ 5 ชิ้นครับ", "ส่งทางรดได้ไหม": "ส่งทางรถได้ไหม",
            "ขอเบอติดต่อ": "ขอเบอร์ติดต่อ",
        }
        for noisy, clean in cases.items():
            self.assertEqual(normalize(noisy).normalized_text, clean, noisy)

    def test_every_applied_correction_lands_on_the_vocabulary(self):
        terms = set(V.all_terms()) | set(V.PARTICLE_ALIASES.values())
        for text in ("20คุ่อยากสั่งขงจากจีน", "ขอเช็คออเดอของผมหนอย", "ยกเลกออเดอได้ไหม", "รองเท้า คับ"):
            for c in normalize(text).applied:
                if c.source == "keyboard_layout":
                    continue
                self.assertIn(c.replacement, terms, (text, c))

    def test_medium_candidates_are_reported_not_applied(self):
        r = normalize("ลิงนำเข้าได้ไหม")
        self.assertEqual(r.normalized_text, "ลิงนำเข้าได้ไหม")
        for c in r.candidates:
            self.assertNotEqual(c.tier, "HIGH")
            self.assertFalse(c.applied)

    def test_a_real_word_is_not_rewritten_towards_the_vocabulary(self):
        for text in ("เรื่องนี้ยังไง", "เรือดำน้ำ", "รถเข็นเด็ก", "3 ลัง", "ลิงนำเข้าได้ไหม",
                     "ชิปปิ้งคืออะไรคะ", "แล้วราคาเท่าไหร่อะ", "งั้นขอเบอร์ติดต่อ"):
            self.assertEqual(normalize(text).normalized_text, text, text)

    def test_keyboard_layout_recovery_needs_all_dictionary_words(self):
        self.assertEqual(normalize("l;ylfu").normalized_text, "สวัสดี")
        self.assertEqual(normalize("iv'gmhk8iy[").normalized_text, "รองเท้าครับ")
        for latin in ("ok", "kg", "taobao", "hello", "POS123456", "invoice"):
            self.assertEqual(normalize(latin).normalized_text, latin, latin)


class TestIdentifiersAreProtected(unittest.TestCase):
    CASES = ("POS123456", "FT3182", "TH1234567890TH", "1234567890123",
             "https://detail.1688.com/offer/612345678901.html",
             "https://item.taobao.com/item.htm?id=612345678901", "081-234-5678",
             "0812345678", "CS0001234", "test.user@example.com", "PO-2024-000123", "1,500")

    def test_identifiers_survive_byte_for_byte(self):
        for ident in self.CASES:
            for frame in ("ขอเช็ค {} หนอย", "{} ถึงไทหรือยัง", "ยกเลก {} ได้ปะ", "{}"):
                text = frame.format(ident)
                out = normalize(text).normalized_text
                self.assertIn(ident, out, text)

    def test_masking_for_traces(self):
        masked = mask_identifiers("ขอเช็ค POS123456 โทร 0812345678 ที่ https://x.com/a 20 คู่")
        self.assertNotIn("POS123456", masked)
        self.assertNotIn("0812345678", masked)
        self.assertNotIn("x.com", masked)
        self.assertIn("20 คู่", masked)

    def test_trace_never_carries_raw_identifiers(self):
        tr = normalize("ขอเช็ค POS123456 ของผมหนอย 0812345678").as_trace()
        blob = str(tr)
        self.assertNotIn("POS123456", blob)
        self.assertNotIn("0812345678", blob)
        self.assertTrue(tr["raw_text_present"])
        self.assertIn("normalized_text", tr)
        self.assertIn("normalization_method", tr)
        self.assertIn("candidate_count", tr)


class TestUnknownProductsArePreserved(unittest.TestCase):

    def test_unknown_nouns_stay(self):
        for noun in ("กระต่าย", "ฟิกเกอร์", "เคสไอโฟน", "ลิงยาง", "เรือบังคับ", "ชันวางของ",
                     "โคมไฟระย้า", "กลองพลาสติก"):
            for frame in ("{}ครับ", "เป็น{}", "{}นำเข้าได้ไหม", "เอา{}"):
                self.assertIn(noun, normalize(frame.format(noun)).normalized_text, frame.format(noun))


class TestHelpersAndDegradeSafety(unittest.TestCase):

    def test_skeleton(self):
        self.assertEqual(skeleton("นำเข่า"), skeleton("นำเข้า"))
        self.assertEqual(skeleton("ออเดอ"), skeleton("ออเดอร์"))
        self.assertNotEqual(skeleton("ลัง", drop_silenced=False), skeleton("ลิงก์", drop_silenced=False))
        self.assertFalse(N.marks_only_diff("ลัง", "ลิงก์"))
        self.assertTrue(N.marks_only_diff("ออเดอ", "ออเดอร์"))

    def test_dictionary_and_oov(self):
        self.assertTrue(is_dictionary_word("กระต่าย"))
        self.assertFalse(is_dictionary_word("เป้น"))
        self.assertEqual(oov_count("อยากสั่งของจากจีน"), 0)
        self.assertGreater(oov_count("ถอนเงืน"), 0)

    def test_never_raises(self):
        for text in ("", None, "   ", "​", "ๆๆๆ", "้้้", "😊", "a" * 500, "ก" * 300):
            r = normalize(text)  # must not raise
            self.assertIsInstance(r.normalized_text, str)

    def test_library_failure_degrades_to_character_passes(self):
        orig = N.PYTHAINLP_AVAILABLE, N.RAPIDFUZZ_AVAILABLE
        try:
            N.PYTHAINLP_AVAILABLE = False
            N.RAPIDFUZZ_AVAILABLE = False
            r = normalize("อยาก  สั่งของ​จากจีนครับบบบ")
            self.assertEqual(r.normalized_text, "อยาก สั่งของจากจีนครับ")
        finally:
            N.PYTHAINLP_AVAILABLE, N.RAPIDFUZZ_AVAILABLE = orig


if __name__ == "__main__":
    unittest.main()
