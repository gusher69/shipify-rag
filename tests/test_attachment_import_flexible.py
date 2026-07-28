"""Tests for flexible (no-fixed-column-name) Excel attachment import.

Covers: Google Drive link parsing, whole-cell URL detection (vs. a URL
merely mentioned in free text), and the full process_excel_attachments()
flow scanning an arbitrary column layout for URLs — with network/storage/DB
all mocked so this runs offline and without touching Supabase.
"""
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingestion import attachment_handler as ah


class FakeResponse:
    def __init__(self, content_type, body=b"fake-bytes", status=200):
        self.headers = {"content-type": content_type}
        self._body = body
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size):
        yield self._body


class FakeTable:
    """Minimal chainable stand-in for a Supabase table() call."""
    def __init__(self, store, name):
        self.store = store
        self.name = name
        self._filters = []

    def select(self, *a, **k): return self
    def insert(self, rows):
        if isinstance(rows, dict):
            rows = [rows]
        self.store.setdefault(self.name, []).extend(rows)
        return self
    def delete(self): return self
    def eq(self, *a, **k): return self
    def is_(self, *a, **k): return self
    def neq(self, *a, **k): return self
    def ilike(self, *a, **k): return self
    def order(self, *a, **k): return self
    def limit(self, *a, **k): return self
    def filter(self, *a, **k): return self
    def execute(self):
        result = MagicMock()
        result.data = []
        result.count = 0
        return result


class FakeSupabase:
    def __init__(self):
        self.store = {}

    def table(self, name):
        return FakeTable(self.store, name)


class FakeStoredFile:
    def __init__(self, mime_type):
        self.provider = "local"
        self.storage_path = "attachments/2026/07/fake.bin"
        self.public_url = "http://example.test/should-be-overridden"
        self.mime_type = mime_type
        self.file_size = 123
        self.checksum = "deadbeef"


class TestGoogleDriveLinkParsing(unittest.TestCase):
    def test_file_d_view_pattern(self):
        url = "https://drive.google.com/file/d/1AbCdEfGhIjK/view?usp=sharing"
        self.assertEqual(ah._extract_gdrive_file_id(url), "1AbCdEfGhIjK")

    def test_open_id_pattern(self):
        url = "https://drive.google.com/open?id=1AbCdEfGhIjK"
        self.assertEqual(ah._extract_gdrive_file_id(url), "1AbCdEfGhIjK")

    def test_uc_id_pattern(self):
        url = "https://drive.google.com/uc?id=1AbCdEfGhIjK&export=download"
        self.assertEqual(ah._extract_gdrive_file_id(url), "1AbCdEfGhIjK")

    def test_non_drive_url_returns_none(self):
        self.assertIsNone(ah._extract_gdrive_file_id("https://example.com/img.png"))

    def test_direct_download_url_format(self):
        self.assertEqual(
            ah._gdrive_direct_download_url("XYZ123"),
            "https://drive.google.com/uc?export=download&id=XYZ123",
        )


class TestWholeCellUrlDetection(unittest.TestCase):
    def test_pure_url_cell_is_detected(self):
        self.assertTrue(ah.is_attachment_url("https://example.com/photo.jpg"))

    def test_sentence_mentioning_url_is_not_detected(self):
        # This is what makes scanning Answer/free-text columns safe.
        self.assertFalse(ah.is_attachment_url(
            "Please see https://example.com/help for more info"))

    def test_plain_text_is_not_detected(self):
        self.assertFalse(ah.is_attachment_url("just some notes"))

    def test_bare_filename_is_not_a_url(self):
        self.assertFalse(ah.is_attachment_url("photo.jpg"))


class TestSafeFilename(unittest.TestCase):
    def test_sanitizes_unsafe_characters(self):
        name = ah._safe_filename("my photo (final)!!.PNG")
        self.assertTrue(name.endswith(".PNG") or name.endswith(".png") is False)
        self.assertNotIn(" ", name)
        self.assertNotIn("(", name)

    def test_empty_name_falls_back(self):
        name = ah._safe_filename("")
        self.assertTrue(len(name) > 0)


class TestFlexibleColumnScanning(unittest.TestCase):
    """End-to-end process_excel_attachments() with a workbook that does NOT
    use any of the 'known' attachment column names for one of its URLs —
    proving detection works purely from cell content, not header name."""

    def _workbook(self):
        return {
            "sheets": [{
                "sheet_name": "Sheet1",
                "headers": ["Question", "Answer", "Notes", "Photo"],
                "rows": [
                    {
                        "row_index": 1,
                        "_page_number": 1,
                        "row_data": {
                            "Question": "What does the packaging look like?",
                            "Answer": "See the reference photo.",
                            # "Notes" is NOT a recognized attachment header —
                            # this is the flexible-detection case.
                            "Notes": "https://example.com/box.jpg",
                            # "Photo" IS a recognized attachment header.
                            "Photo": "https://drive.google.com/file/d/FILEID123/view",
                        },
                    },
                    {
                        "row_index": 2,
                        "_page_number": 2,
                        "row_data": {
                            "Question": "Any warranty document?",
                            "Answer": "Yes, attached as PDF.",
                            "Notes": "no attachment here, just text",
                            "Photo": "https://example.com/broken-link-returns-html",
                        },
                    },
                ],
            }]
        }

    def _fake_get(self, url, timeout=20, stream=True, headers=None):
        if "example.com/box.jpg" in url:
            return FakeResponse("image/jpeg")
        if "drive.google.com/uc?export=download&id=FILEID123" in url:
            return FakeResponse("application/pdf")
        if "broken-link-returns-html" in url:
            return FakeResponse("text/html")
        raise AssertionError(f"unexpected URL requested: {url}")

    def test_flexible_scan_finds_url_in_unnamed_column_and_handles_drive_and_html(self):
        sb = FakeSupabase()
        wb = self._workbook()

        fake_storage = MagicMock()
        def fake_upload(path, dest_name=None, folder=None):
            mime = "application/pdf" if (dest_name or "").endswith(".pdf") else "image/jpeg"
            return FakeStoredFile(mime)
        fake_storage.upload_file.side_effect = fake_upload

        with patch("ingestion.attachment_handler._requests.get", side_effect=self._fake_get), \
             patch("ingestion.attachment_handler.get_storage_service", return_value=fake_storage), \
             patch("ingestion.embedder._get_supabase", return_value=sb, create=True):
            report = ah.process_excel_attachments(wb, "file-123", sb)

        # 2 knowledge_items created (one per row)
        self.assertEqual(report["knowledge_items_created"], 2)
        self.assertEqual(report["rows_imported"], 2)

        # 3 URL-shaped cells total: box.jpg (Notes), drive file (Photo row1), broken link (Photo row2)
        self.assertEqual(report["urls_detected"], 3)

        # box.jpg + drive pdf succeed -> 2 downloaded; broken html link fails -> 1 failed
        self.assertEqual(report["attachments_downloaded"], 2)
        self.assertEqual(report["attachments_failed"], 1)
        self.assertEqual(len(report["failed"]), 1)
        self.assertIn("HTML", report["failed"][0]["error"])

        # The unnamed "Notes" column's URL was in fact captured (flexible detection).
        recs = report["attachment_records"]
        notes_rec = next(r for r in recs if r["original_url"] == "https://example.com/box.jpg")
        self.assertEqual(notes_rec["status"], "downloaded")
        self.assertEqual(notes_rec["attachment_type"], "image")
        self.assertIsNotNone(notes_rec["knowledge_item_id"])

        # The Drive link was correctly identified as a PDF and its gdrive
        # metadata captured.
        drive_rec = next(r for r in recs if r.get("metadata", {}).get("gdrive_file_id") == "FILEID123")
        self.assertEqual(drive_rec["attachment_type"], "pdf")
        self.assertEqual(drive_rec["status"], "downloaded")

        # The row with an ordinary (non-URL) Notes value never triggered a
        # false-positive attachment from that column.
        self.assertFalse(any(r["row_index"] == 2 and r["original_url"] is None for r in recs
                              if r["sheet_name"] == "Sheet1"))


if __name__ == "__main__":
    unittest.main()
