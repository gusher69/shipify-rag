import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.text_quality import TextQualityEvaluator


class TestTextQualityEvaluator(unittest.TestCase):
    def setUp(self):
        self.ev = TextQualityEvaluator()

    def test_clean_long_text_is_good(self):
        text = ("This is a perfectly normal, clean paragraph of extracted native PDF text. " * 3)
        r = self.ev.evaluate(text)
        self.assertEqual(r.extraction_status, "good")
        self.assertFalse(r.requires_ocr)
        self.assertFalse(r.requires_vision)

    def test_empty_text_is_empty(self):
        r = self.ev.evaluate("")
        self.assertEqual(r.extraction_status, "empty")
        self.assertTrue(r.requires_ocr)
        self.assertTrue(r.requires_vision)

    def test_cid_placeholders_are_corrupted(self):
        text = "(cid:12)(cid:45)(cid:99)(cid:100)(cid:8)" * 10
        r = self.ev.evaluate(text)
        self.assertEqual(r.extraction_status, "corrupted")
        self.assertIn("cid", r.reason.lower())
        self.assertTrue(r.requires_ocr)
        self.assertTrue(r.requires_vision)

    def test_private_use_area_glyphs_are_corrupted(self):
        text = "" * 10
        r = self.ev.evaluate(text)
        self.assertEqual(r.extraction_status, "corrupted")
        self.assertIn("font mapping", r.reason.lower())

    def test_replacement_characters_are_corrupted(self):
        text = ("�" * 20) + ("normal text here " * 5)
        r = self.ev.evaluate(text)
        self.assertEqual(r.extraction_status, "corrupted")

    def test_sparse_short_text(self):
        r = self.ev.evaluate("Page 3")
        self.assertEqual(r.extraction_status, "sparse")
        self.assertTrue(r.requires_ocr)
        self.assertFalse(r.requires_vision)

    def test_repeated_symbol_runs_are_corrupted_but_whitespace_runs_are_not(self):
        garbage = "xxxxxxxxxxxxxxx normal words follow this garbage run of characters here today"
        r = self.ev.evaluate(garbage)
        self.assertEqual(r.extraction_status, "corrupted")

        # Long runs of SPACES (common in form templates with blank fields)
        # must never be flagged as corruption.
        form_text = "Name:" + (" " * 30) + "Surname:" + (" " * 30) + "Phone number and email contact details here."
        r2 = self.ev.evaluate(form_text)
        self.assertNotEqual(r2.extraction_status, "corrupted")

    def test_expected_thai_but_no_thai_decoded_is_flagged(self):
        # Long enough, alpha-heavy, but zero Thai characters despite being told to expect Thai.
        text = "abcdefghijklmnopqrstuvwxyz " * 5
        r = self.ev.evaluate(text, expected_thai=True)
        self.assertTrue(r.requires_ocr)
        self.assertTrue(r.requires_vision)

    def test_real_allianz_corrupted_table_page_pattern(self):
        """Reproduces the actual reported production failure pattern:
        Thai text mapped to Private-Use-Area glyphs by a broken embedded
        font, with numeric benefit values still readable."""
        text = ("\n"
                "Smarter Health\nPlan 1\nPlan 2\nPlan 3\nPlan 4\n750,000\n1,500,000\n3,000,000\n5,000,000")
        r = self.ev.evaluate(text, expected_thai=True)
        self.assertIn(r.extraction_status, ("corrupted", "sparse"))
        self.assertTrue(r.requires_ocr)
        self.assertTrue(r.requires_vision)


if __name__ == "__main__":
    unittest.main()
