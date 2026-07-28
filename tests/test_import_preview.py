"""Tests for the Import Preview dry-run analyzer (ingestion/import_preview.py).

Covers: Q&A workbook analysis (missing question/answer, duplicate-within-
file, duplicate-vs-existing-DB via text similarity, attachment dry-run
scanning), document-style analysis (Word/PDF/Markdown), and that nothing
here ever calls a DB write or a real download.
"""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# analyze_file_for_preview() now runs the AI Knowledge Analyzer (which can
# call the LLM for classification AND graph extraction) — disable both so
# this test file never depends on another test module having already set
# these env vars first (test discovery order is not something to rely on).
os.environ.setdefault("ENABLE_KNOWLEDGE_ANALYZER_LLM", "false")
os.environ.setdefault("ENABLE_KNOWLEDGE_GRAPH", "false")

from ingestion import import_preview as ip


class FakeTable:
    def __init__(self, data):
        self._data = data
    def select(self, *a, **k): return self
    def is_(self, *a, **k): return self
    def not_(self): return self
    def order(self, *a, **k): return self
    def limit(self, *a, **k): return self
    def execute(self):
        return MagicMock(data=self._data)


class FakeSb:
    def __init__(self, existing_questions=None, existing_files=None):
        self._existing_questions = existing_questions or []
        self._existing_files = existing_files or []
    def table(self, name):
        if name == "knowledge_items":
            return FakeTable(self._existing_questions)
        if name == "knowledge_files":
            return FakeTable(self._existing_files)
        return FakeTable([])


def _qa_workbook(rows, headers=None):
    return {
        "sheets": [{
            "sheet_name": "FAQ",
            "headers": headers or ["Question", "Answer"],
            "rows": rows,
        }]
    }


class TestQaAnalysis(unittest.TestCase):
    def test_missing_question_flagged_as_error(self):
        wb = _qa_workbook([{"row_index": 1, "row_data": {"Question": "", "Answer": "an answer"}}])
        items, attachments, issues = [], [], []
        ip._analyze_qa_workbook(wb, "f.xlsx", [], [], items, attachments, issues)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["status"], "error")
        self.assertTrue(any("Missing Question" in i["problem"] for i in issues))

    def test_missing_answer_flagged_as_error(self):
        wb = _qa_workbook([{"row_index": 1, "row_data": {"Question": "a question", "Answer": ""}}])
        items, attachments, issues = [], [], []
        ip._analyze_qa_workbook(wb, "f.xlsx", [], [], items, attachments, issues)
        self.assertTrue(any("Missing Answer" in i["problem"] for i in issues))

    def test_empty_row_skipped_with_info_issue(self):
        wb = _qa_workbook([{"row_index": 1, "row_data": {"Question": "", "Answer": ""}}])
        items, attachments, issues = [], [], []
        ip._analyze_qa_workbook(wb, "f.xlsx", [], [], items, attachments, issues)
        self.assertEqual(len(items), 0)
        self.assertTrue(any(i["severity"] == "info" and "Empty row" in i["problem"] for i in issues))

    def test_duplicate_question_within_file_detected(self):
        wb = _qa_workbook([
            {"row_index": 1, "row_data": {"Question": "What is the price?", "Answer": "100 baht"}},
            {"row_index": 2, "row_data": {"Question": "what is the price?", "Answer": "100 baht"}},
        ])
        items, attachments, issues = [], [], []
        ip._analyze_qa_workbook(wb, "f.xlsx", [], [], items, attachments, issues)
        self.assertTrue(any("Duplicate question within this file" in i["problem"] for i in issues))

    def test_duplicate_question_against_existing_db(self):
        existing = [{"id": "abc-123", "question": "What is the shipping cost?"}]
        wb = _qa_workbook([{"row_index": 1, "row_data": {"Question": "What is the shipping cost?", "Answer": "50 baht"}}])
        items, attachments, issues = [], [], []
        ip._analyze_qa_workbook(wb, "f.xlsx", existing, [], items, attachments, issues)
        self.assertEqual(items[0]["status"], "duplicate")
        self.assertEqual(items[0]["duplicate_of"], "abc-123")
        self.assertEqual(items[0]["duplicate_kind"], "duplicate")

    def test_very_similar_question_flagged_but_not_exact_duplicate(self):
        existing = [{"id": "abc-123", "question": "What is the shipping cost to Bangkok"}]
        wb = _qa_workbook([{"row_index": 1, "row_data": {"Question": "What is the shipping fee to Bangkok area", "Answer": "50 baht"}}])
        items, attachments, issues = [], [], []
        ip._analyze_qa_workbook(wb, "f.xlsx", existing, [], items, attachments, issues)
        # Similarity should land in "similar" or "duplicate" territory, not None.
        self.assertIsNotNone(items[0].get("duplicate_kind"))

    def test_unrelated_question_not_flagged_duplicate(self):
        existing = [{"id": "abc-123", "question": "Completely unrelated topic about refunds"}]
        wb = _qa_workbook([{"row_index": 1, "row_data": {"Question": "What is the shipping cost?", "Answer": "50 baht"}}])
        items, attachments, issues = [], [], []
        ip._analyze_qa_workbook(wb, "f.xlsx", existing, [], items, attachments, issues)
        self.assertIsNone(items[0].get("duplicate_of"))

    def test_enrichment_fields_carried_through_from_row(self):
        wb = _qa_workbook([{
            "row_index": 1,
            "row_data": {"Question": "q", "Answer": "a"},
            "_generated_category": "Shipping",
            "_generated_tags": ["shipping", "warehouse"],
            "_generated_alt_questions": ["alt phrasing"],
            "_generated_language": "en",
        }])
        items, attachments, issues = [], [], []
        ip._analyze_qa_workbook(wb, "f.xlsx", [], [], items, attachments, issues)
        item = items[0]
        self.assertEqual(item["category"], "Shipping")
        self.assertEqual(item["tags"], ["shipping", "warehouse"])
        self.assertEqual(item["alt_questions"], ["alt phrasing"])
        self.assertEqual(item["language"], "en")

    def test_attachment_url_in_flexible_column_detected_without_download(self):
        wb = _qa_workbook(
            [{"row_index": 1, "row_data": {"Question": "q", "Answer": "a", "Notes": "https://example.com/img.png"}}],
            headers=["Question", "Answer", "Notes"],
        )
        items, attachments, issues = [], [], []
        with patch("ingestion.attachment_handler._requests.head") as mock_head:
            mock_head.return_value = MagicMock(status_code=200, headers={"content-type": "image/png"})
            ip._analyze_qa_workbook(wb, "f.xlsx", [], [], items, attachments, issues)
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0]["source"], "https://example.com/img.png")
        self.assertTrue(attachments[0]["reachable"])
        # Never actually downloaded a body — only .head() was called.
        mock_head.assert_called()

    def test_broken_url_reported_as_warning_issue(self):
        wb = _qa_workbook(
            [{"row_index": 1, "row_data": {"Question": "q", "Answer": "a", "Notes": "https://example.com/broken.png"}}],
            headers=["Question", "Answer", "Notes"],
        )
        items, attachments, issues = [], [], []
        with patch("ingestion.attachment_handler._requests.head", side_effect=ConnectionError("dns fail")), \
             patch("ingestion.attachment_handler._requests.get", side_effect=ConnectionError("dns fail")):
            ip._analyze_qa_workbook(wb, "f.xlsx", [], [], items, attachments, issues)
        self.assertFalse(attachments[0]["reachable"])
        self.assertTrue(any("Broken or unreachable URL" in i["problem"] for i in issues))


class TestDocumentAnalysis(unittest.TestCase):
    def test_document_pages_produce_items_without_qa_structure(self):
        pages = [{"page_number": 1, "text": "This is a PDF section with real content."}]
        items, attachments, issues = [], [], []
        ip._analyze_document_pages(pages, "manual.pdf", [], items, attachments, issues)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["type"], "document")
        self.assertIsNone(items[0]["question"])

    def test_empty_page_flagged_as_warning(self):
        pages = [{"page_number": 1, "text": "   "}]
        items, attachments, issues = [], [], []
        ip._analyze_document_pages(pages, "manual.pdf", [], items, attachments, issues)
        self.assertEqual(len(items), 0)
        self.assertTrue(any("Empty page" in i["problem"] for i in issues))

    def test_similar_filename_flagged_as_duplicate_file(self):
        items, attachments, issues = [], [], []
        ip._analyze_document_pages(
            [{"page_number": 1, "text": "content"}], "warehouse_manual.pdf",
            ["warehouse_manual (1).pdf"], items, attachments, issues,
        )
        self.assertTrue(any("very similar name" in i["problem"] for i in issues))


class TestStatsComputation(unittest.TestCase):
    def test_stats_reflect_items_and_attachments(self):
        result = {
            "items": [
                {"type": "qa", "question": "q1", "answer": "a1", "status": "ok", "language": "en",
                 "category": "Shipping", "tags": ["a", "b"], "alt_questions": ["x"]},
                {"type": "qa", "question": "q2", "answer": "a2", "status": "error", "language": "th",
                 "category": None, "tags": [], "alt_questions": []},
            ],
            "attachments": [
                {"attachment_type": "image", "reachable": True},
                {"attachment_type": "pdf", "reachable": False},
            ],
            "issues": [
                {"severity": "warning"}, {"severity": "error"}, {"severity": "info"},
            ],
        }
        ip._compute_stats(result)
        stats = result["stats"]
        self.assertEqual(stats["knowledge_items"], 2)
        self.assertEqual(stats["rows_skipped"], 1)
        self.assertEqual(stats["detected_attachments"], 2)
        self.assertEqual(stats["attachments_by_type"]["image"], 1)
        self.assertEqual(stats["attachments_unreachable"], 1)
        self.assertEqual(stats["warnings"], 1)
        self.assertEqual(stats["errors"], 1)
        self.assertIn("en", stats["languages"])
        self.assertIn("th", stats["languages"])


class TestAnalyzeFileForPreviewNeverWritesToDb(unittest.TestCase):
    def test_unsupported_extension_returns_error_issue_without_touching_db(self):
        sb = FakeSb()
        with patch.object(sb, "table") as mock_table:
            result = ip.analyze_file_for_preview(Path("fake.exe"), "fake.exe", sb)
        # Unsupported-extension path returns immediately, before any DB read.
        mock_table.assert_not_called()
        self.assertEqual(result["stats"]["errors"], 1)
        self.assertEqual(result["items"], [])


if __name__ == "__main__":
    unittest.main()
