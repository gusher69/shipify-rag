"""Tests for the "smart" Excel importer additions: inline/multi-URL
extraction, expanded file-type classification (Content-Type -> magic
bytes -> extension), fuzzy filename matching, download retry, and the
deterministic parts of smart_enrichment (language/tags/category)."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingestion import attachment_handler as ah
from ingestion import smart_enrichment as se


class TestInlineAndMultiUrlExtraction(unittest.TestCase):
    def test_url_embedded_mid_sentence(self):
        text = "Please see the latest warehouse image: https://example.com/a.png Thank you."
        self.assertEqual(ah.extract_urls_from_text(text), ["https://example.com/a.png"])

    def test_url_on_its_own_line_within_a_note(self):
        text = "ดูรายละเอียดตามรูป\nhttps://example.com/image.png"
        self.assertEqual(ah.extract_urls_from_text(text), ["https://example.com/image.png"])

    def test_multiple_urls_various_separators(self):
        text = "https://a.com/image1.png, https://b.com/image2.png; https://c.com/manual.pdf"
        urls = ah.extract_urls_from_text(text)
        self.assertEqual(urls, [
            "https://a.com/image1.png",
            "https://b.com/image2.png",
            "https://c.com/manual.pdf",
        ])

    def test_space_separated_urls(self):
        text = "https://a.com/1.png https://b.com/2.png"
        self.assertEqual(ah.extract_urls_from_text(text),
                          ["https://a.com/1.png", "https://b.com/2.png"])

    def test_trailing_punctuation_stripped(self):
        text = "Warehouse: https://drive.google.com/file/d/xxxxx/view."
        urls = ah.extract_urls_from_text(text)
        self.assertEqual(urls, ["https://drive.google.com/file/d/xxxxx/view"])

    def test_no_url_returns_empty(self):
        self.assertEqual(ah.extract_urls_from_text("just a regular answer, no links"), [])

    def test_duplicate_url_only_returned_once(self):
        text = "https://a.com/x.png https://a.com/x.png"
        self.assertEqual(ah.extract_urls_from_text(text), ["https://a.com/x.png"])


class TestLooksLikeFilename(unittest.TestCase):
    def test_recognizes_many_extensions(self):
        for name in ["report.docx", "sheet.xlsx", "deck.pptx", "archive.zip",
                     "clip.mp4", "data.csv", "notes.txt", "config.json", "feed.xml"]:
            self.assertTrue(ah._looks_like_filename(name), name)

    def test_plain_word_is_not_a_filename(self):
        self.assertFalse(ah._looks_like_filename("warehouse"))

    def test_long_sentence_is_not_a_filename(self):
        self.assertFalse(ah._looks_like_filename("x" * 200 + ".png"))


class TestFuzzyFilenameNormalization(unittest.TestCase):
    def test_case_and_spacing_variants_match(self):
        base = ah._normalize_filename_for_match("warehouse.png")
        for variant in ["Warehouse.png", "WAREHOUSE.PNG", "ware house.png", "ware_house.png"]:
            self.assertEqual(ah._normalize_filename_for_match(variant), base, variant)

    def test_common_suffixes_are_stripped(self):
        base = ah._normalize_filename_for_match("warehouse.png")
        for variant in ["warehouse (1).png", "warehouse-final.png", "warehouse_copy.png", "warehouse v2.png"]:
            self.assertEqual(ah._normalize_filename_for_match(variant), base, variant)


class TestSmartContentTypeClassification(unittest.TestCase):
    def test_content_type_wins_when_specific(self):
        self.assertEqual(ah._classify_attachment_type("application/pdf", None, ".png"), "pdf")

    def test_falls_back_to_magic_bytes_when_content_type_generic(self):
        self.assertEqual(
            ah._classify_attachment_type("application/octet-stream", "image/png", ".bin"), "image")

    def test_falls_back_to_extension_when_zip_family_ambiguous(self):
        # docx/xlsx/pptx/zip all share the same magic bytes (PK..) —
        # extension must resolve which one it actually is.
        self.assertEqual(ah._classify_attachment_type(None, "application/zip", ".docx"), "document")
        self.assertEqual(ah._classify_attachment_type(None, "application/zip", ".xlsx"), "spreadsheet")
        self.assertEqual(ah._classify_attachment_type(None, "application/zip", ".pptx"), "presentation")
        self.assertEqual(ah._classify_attachment_type(None, "application/zip", ".zip"), "archive")

    def test_unrecognized_type_is_generic_file_not_a_failure(self):
        self.assertEqual(ah._classify_attachment_type(None, None, ".xyz"), "file")

    def test_video_extensions(self):
        for ext in [".mp4", ".mov", ".avi", ".webm"]:
            self.assertEqual(ah._classify_attachment_type(None, None, ext), "video")

    def test_magic_byte_sniffing_png(self):
        png_header = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20
        self.assertEqual(ah._sniff_magic_bytes(png_header), "image/png")

    def test_magic_byte_sniffing_pdf(self):
        self.assertEqual(ah._sniff_magic_bytes(b"%PDF-1.4 rest of file"), "application/pdf")

    def test_magic_byte_sniffing_unknown_returns_none(self):
        self.assertIsNone(ah._sniff_magic_bytes(b"not a known signature"))


class FakeResp:
    def __init__(self, status=200, ct="image/jpeg", body=b"abc"):
        self.status_code = status
        self.headers = {"content-type": ct}
        self._body = body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")

    def iter_content(self, n):
        yield self._body


class TestDownloadRetry(unittest.TestCase):
    def test_retries_up_to_max_then_fails(self):
        calls = {"n": 0}

        def flaky_get(*a, **k):
            calls["n"] += 1
            raise ConnectionError("boom")

        with patch("ingestion.attachment_handler._requests.get", side_effect=flaky_get), \
             patch("ingestion.attachment_handler._time.sleep"):
            _, _, _, err, _ = ah._download_to_temp("https://example.com/a.jpg")

        self.assertEqual(calls["n"], ah.DOWNLOAD_MAX_RETRIES)
        self.assertIn("Failed after", err)

    def test_succeeds_after_one_transient_failure(self):
        calls = {"n": 0}

        def flaky_get(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionError("transient")
            return FakeResp()

        with patch("ingestion.attachment_handler._requests.get", side_effect=flaky_get), \
             patch("ingestion.attachment_handler._time.sleep"):
            path, mime, name, err, meta = ah._download_to_temp("https://example.com/a.jpg")

        self.assertIsNone(err)
        self.assertIsNotNone(path)
        self.assertEqual(calls["n"], 2)
        if path:
            path.unlink(missing_ok=True)


class TestSmartEnrichmentDeterministic(unittest.TestCase):
    def test_language_detection(self):
        self.assertEqual(se.detect_language("ขอที่อยู่โกดังจีน"), "th")
        self.assertEqual(se.detect_language("what is the shipping cost"), "en")
        self.assertEqual(se.detect_language("这是中文问题"), "zh")
        self.assertEqual(se.detect_language(""), "en")

    def test_tag_generation_from_keywords(self):
        tags = se.generate_tags("ขอที่อยู่โกดังจีน")
        self.assertIn("โกดัง", tags)
        self.assertIn("warehouse", tags)
        self.assertIn("จีน", tags)
        self.assertIn("china", tags)

    def test_tag_generation_no_keywords_returns_empty(self):
        self.assertEqual(se.generate_tags("hello world"), [])

    def test_category_prefers_existing_value(self):
        self.assertEqual(
            se.infer_category(sheet_name="Sheet1", file_name="x.xlsx", existing_category="Billing"),
            "Billing")

    def test_category_falls_back_to_meaningful_sheet_name(self):
        self.assertEqual(se.infer_category(sheet_name="Warehouse FAQ", file_name="x.xlsx"), "Warehouse FAQ")

    def test_category_falls_back_to_filename_when_sheet_is_generic(self):
        self.assertEqual(se.infer_category(sheet_name="Sheet1", file_name="warehouse_faq.xlsx"),
                          "Warehouse Faq")

    def test_category_default_when_nothing_available(self):
        self.assertEqual(se.infer_category(), "General Knowledge")

    def test_alt_questions_disabled_via_env_returns_empty(self):
        with patch.dict("os.environ", {"ENABLE_SMART_ALT_QUESTIONS": "false"}):
            self.assertEqual(se.generate_alt_questions("ขอที่อยู่โกดังจีน"), [])

    def test_alt_questions_empty_question_returns_empty(self):
        self.assertEqual(se.generate_alt_questions(""), [])

    def test_alt_questions_llm_failure_degrades_to_empty_not_raise(self):
        with patch.dict("os.environ", {"ENABLE_SMART_ALT_QUESTIONS": "true"}), \
             patch("services.llm_service.get_llm_service", side_effect=Exception("no api key")):
            result = se.generate_alt_questions("ขอที่อยู่โกดังจีน")
        self.assertEqual(result, [])


class TestKnowledgeItemsMigration013Fallback(unittest.TestCase):
    """Regression test: migration 013 (category/tags/alt_questions/language
    columns) not yet applied on a live DB must NOT break knowledge_items
    creation entirely — that would silently break attachment linking too,
    since attachments are matched to knowledge_item UUIDs created here."""

    def test_falls_back_to_legacy_columns_when_insert_with_new_columns_fails(self):
        inserted_batches = []

        class FailFirstThenSucceedTable:
            def __init__(self, name):
                self.name = name
            def select(self, *a, **k): return self
            def delete(self): return self
            def eq(self, *a, **k): return self
            def execute(self):
                return MagicMock(data=[], count=0)
            def insert(self, rows):
                if self.name == "knowledge_items":
                    if any("category" in r for r in rows):
                        raise Exception('column knowledge_items.category does not exist')
                    inserted_batches.append(rows)
                return self

        class FakeSb:
            def table(self, name):
                return FailFirstThenSucceedTable(name)

        workbook = {
            "sheets": [{
                "sheet_name": "FAQ",
                "headers": ["Question", "Answer"],
                "rows": [{
                    "row_index": 1, "_page_number": 1,
                    "row_data": {"Question": "q1", "Answer": "a1"},
                    "_generated_category": "General Knowledge",
                    "_generated_tags": ["tag1"],
                    "_generated_alt_questions": ["alt1"],
                    "_generated_language": "en",
                }],
            }]
        }

        item_ids = ah._create_knowledge_items(workbook, "file-1", FakeSb(), {})

        self.assertEqual(len(item_ids), 1)
        self.assertEqual(len(inserted_batches), 1)
        # The successful (legacy) insert must not contain the new columns.
        for row in inserted_batches[0]:
            for col in ("category", "tags", "alt_questions", "language"):
                self.assertNotIn(col, row)
            self.assertEqual(row["question"], "q1")


if __name__ == "__main__":
    unittest.main()
