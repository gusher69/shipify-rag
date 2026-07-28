import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.text_quality import TextQualityEvaluator
from services.page_classifier import classify_page

_ev = TextQualityEvaluator()


class TestPageClassifier(unittest.TestCase):
    def test_native_text_page(self):
        text = "This is a perfectly normal clean paragraph of extracted native PDF text. " * 3
        q = _ev.evaluate(text)
        c = classify_page(text_quality=q, native_text=text, image_count=0)
        self.assertEqual(c.page_type, "native_text")

    def test_mostly_empty_page(self):
        q = _ev.evaluate("")
        c = classify_page(text_quality=q, native_text="", image_count=0)
        self.assertEqual(c.page_type, "mostly_empty")

    def test_image_only_page(self):
        q = _ev.evaluate("")
        c = classify_page(text_quality=q, native_text="", image_count=2)
        self.assertEqual(c.page_type, "image")

    def test_scanned_text_page_corrupted_no_images(self):
        text = "(cid:1)(cid:2)(cid:3)(cid:4)(cid:5)" * 10
        q = _ev.evaluate(text)
        c = classify_page(text_quality=q, native_text=text, image_count=0)
        self.assertEqual(c.page_type, "scanned_text")

    def test_infographic_page_corrupted_with_images(self):
        text = "(cid:1)(cid:2)(cid:3)(cid:4)(cid:5)" * 10
        q = _ev.evaluate(text)
        c = classify_page(text_quality=q, native_text=text, image_count=1)
        self.assertEqual(c.page_type, "infographic")

    def test_mixed_page_good_text_with_image(self):
        text = "This is a perfectly normal clean paragraph of extracted native PDF text. " * 3
        q = _ev.evaluate(text)
        c = classify_page(text_quality=q, native_text=text, image_count=1)
        self.assertEqual(c.page_type, "mixed")

    def test_table_page(self):
        text = "\n".join(["Maximum annual benefit comparison across all available plans"] +
                          ["750,000 1,500,000 3,000,000 5,000,000"] * 8)
        q = _ev.evaluate(text)
        self.assertEqual(q.extraction_status, "good")  # sanity: long enough to not be "sparse"
        c = classify_page(text_quality=q, native_text=text, image_count=0)
        self.assertEqual(c.page_type, "table")


if __name__ == "__main__":
    unittest.main()
