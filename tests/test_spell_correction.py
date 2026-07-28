"""Regression tests for the deterministic Query Spell Correction engine
(rag/spell_correction.py) — pure Python, no LLM/API call.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rag.spell_correction import (
    correct_query, get_vocabulary, get_fallback_dict, _edit_distance,
    _best_substring_alignment, _protected_spans,
)


class TestProtectedSpansNeverModified(unittest.TestCase):
    def test_tracking_number_untouched(self):
        r = correct_query("FT123456789 อยู่ไหน")
        self.assertEqual(r["corrected_query"], "FT123456789 อยู่ไหน")
        self.assertEqual(r["corrections"], [])

    def test_price_with_thousands_separator_untouched(self):
        r = correct_query("ราคา 4,500 บาท")
        self.assertEqual(r["corrected_query"], "ราคา 4,500 บาท")
        self.assertEqual(r["corrections"], [])

    def test_url_untouched(self):
        r = correct_query("ดูที่ https://maps.app.goo.gl/abc123 นะคะ")
        self.assertIn("https://maps.app.goo.gl/abc123", r["corrected_query"])

    def test_email_untouched(self):
        r = correct_query("ส่งมาที่ support@example.com ด้วยค่ะ")
        self.assertIn("support@example.com", r["corrected_query"])

    def test_phone_number_untouched(self):
        r = correct_query("โทร 091-5050-775 นะคะ")
        self.assertIn("091-5050-775", r["corrected_query"])

    def test_known_abbreviation_untouched_even_glued_to_thai(self):
        """CBM directly followed by Thai text (no space, no regex \\b
        boundary between a Latin and a Thai character) must still be
        protected."""
        r = correct_query("CBMคำนวณยังไง")
        self.assertIn("CBM", r["corrected_query"])
        self.assertNotIn("ืออะไร", r["corrected_query"])  # no accidental mangling nearby


class TestDocumentedTypoExamples(unittest.TestCase):
    """The 5 examples given in the task spec, verbatim."""

    def test_map_typo(self):
        r = correct_query("ส่งแผนี่ให้หน่อย")
        self.assertEqual(r["corrected_query"], "ส่งแผนที่ให้หน่อย")
        self.assertTrue(any(c["from"] == "แผนี่" and c["to"] == "แผนที่" for c in r["corrections"]))

    def test_location_warehouse_typo(self):
        r = correct_query("ขอโลเคชันโกดดัง")
        self.assertEqual(r["corrected_query"], "ขอโลเคชั่นโกดัง")

    def test_bill_typo(self):
        r = correct_query("ชำระบิวอย่างไร")
        self.assertEqual(r["corrected_query"], "ชำระบิลอย่างไร")

    def test_rate_typo(self):
        r = correct_query("เรดทางเรือ")
        self.assertEqual(r["corrected_query"], "เรททางเรือ")

    def test_cbm_transliteration_typo(self):
        r = correct_query("CBเอ็มคืออะไร")
        self.assertIn("CBM", r["corrected_query"])
        self.assertTrue(any(c["from"] == "CBเอ็ม" and c["to"] == "CBM" and c["method"] == "direct"
                             for c in r["corrections"]))


class TestSafetyAndAmbiguity(unittest.TestCase):
    def test_original_query_always_preserved(self):
        r = correct_query("ขอโลเคชันโกดดัง")
        self.assertEqual(r["original_query"], "ขอโลเคชันโกดดัง")

    def test_unknown_legitimate_word_stays_unchanged(self):
        r = correct_query("อยากทราบเวลาทำการของร้าน")
        self.assertEqual(r["corrected_query"], "อยากทราบเวลาทำการของร้าน")
        self.assertEqual(r["corrections"], [])

    def test_empty_query(self):
        r = correct_query("")
        self.assertEqual(r["corrected_query"], "")
        self.assertEqual(r["corrections"], [])

    def test_no_corrections_yields_full_confidence(self):
        r = correct_query("สวัสดีค่ะ")
        self.assertEqual(r["correction_confidence"], 1.0)

    def test_never_overwrites_an_already_valid_different_word(self):
        """"ส่ง" ("send") is a real, unrelated word — it must never be
        "corrected" into a longer vocabulary word ("ขนส่ง") just because
        they share a suffix."""
        r = correct_query("ส่งแผนี่ให้หน่อย")
        self.assertIn("ส่ง", r["corrected_query"])
        self.assertNotIn("ขนส่ง", r["corrected_query"])


class TestEditDistanceAndAlignment(unittest.TestCase):
    def test_edit_distance_basic(self):
        self.assertEqual(_edit_distance("บิว", "บิล"), 1)
        self.assertEqual(_edit_distance("same", "same"), 0)

    def test_best_substring_alignment_finds_properly_sized_span(self):
        start, end, dist = _best_substring_alignment("เรท", "เรดทางเรือ")
        self.assertEqual("เรดทางเรือ"[start:end], "เรด")
        self.assertEqual(dist, 1)


class TestVocabularySources(unittest.TestCase):
    def test_vocabulary_includes_synonym_terms(self):
        vocab = get_vocabulary()
        self.assertIn("พิกัด", vocab)
        self.assertIn("โกดัง", vocab)

    def test_vocabulary_includes_fallback_domain_terms(self):
        vocab = get_vocabulary()
        self.assertIn("บิล", vocab)

    def test_fallback_dict_has_direct_corrections(self):
        fb = get_fallback_dict()
        self.assertIn("CBเอ็ม", fb["direct_corrections"])


if __name__ == "__main__":
    unittest.main()
