"""Tests for Full Auto Import: Import Preview is fully removed, every
import always uses the Advanced AI Analysis Profile, and AI/Knowledge
Graph failures degrade gracefully instead of failing the import."""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("ENABLE_KNOWLEDGE_ANALYZER_LLM", "false")
os.environ.setdefault("ENABLE_KNOWLEDGE_GRAPH", "false")


class TestImportPreviewRoutesRemoved(unittest.TestCase):
    def test_no_import_preview_routes_registered(self):
        from admin.routes import app
        paths = [r.path for r in app.routes if hasattr(r, "path")]
        for p in paths:
            self.assertNotIn("import/preview", p, f"stray Import Preview route: {p}")
            self.assertNotIn("import-preview", p, f"stray Import Preview route: {p}")

    def test_dashboard_and_upload_routes_still_exist(self):
        from admin.routes import app
        paths = {r.path for r in app.routes if hasattr(r, "path")}
        self.assertIn("/admin/dashboard", paths)
        self.assertIn("/admin/upload", paths)
        self.assertIn("/admin/files/{file_id}/sync", paths)


class TestImportServiceEntryPoint(unittest.TestCase):
    def test_start_full_auto_import_delegates_to_routes(self):
        from services.import_service import ImportService
        with patch("admin.routes.start_full_auto_import_for_file",
                   return_value={"ok": True, "job_id": "job-1"}) as mock_start:
            result = ImportService.start_full_auto_import("file-123")
        mock_start.assert_called_once_with("file-123")
        self.assertEqual(result, {"ok": True, "job_id": "job-1"})


class TestAnalyzeAndChunkFailsafe(unittest.TestCase):
    def test_analyzer_crash_falls_back_to_plain_chunking(self):
        from ingestion import ingest as ing
        pages = [{"page_number": 1, "text": "# Some Heading\nSome body text here."}]
        with patch("ingestion.ingest.read_file_pages", return_value=pages), \
             patch("services.knowledge_analyzer.get_knowledge_analyzer") as mock_get:
            mock_get.return_value.analyze.side_effect = RuntimeError("boom")
            analysis, chunks, returned_pages = ing.analyze_and_chunk(
                Path("dummy.md"), source="dummy.md", knowledge_analysis_profile="advanced")
        self.assertIsNone(analysis)
        self.assertGreaterEqual(len(chunks), 1)
        self.assertEqual(returned_pages, pages)

    def test_heading_chunker_crash_falls_back_to_plain_chunking(self):
        from ingestion import ingest as ing
        pages = [{"page_number": 1, "text": "# Some Heading\nSome body text here."}]
        fake_analysis = MagicMock()
        fake_analysis.chunk_strategy = "heading_paragraph"
        with patch("ingestion.ingest.read_file_pages", return_value=pages), \
             patch("services.knowledge_analyzer.get_knowledge_analyzer") as mock_get, \
             patch("ingestion.ingest.chunk_pages_by_heading", side_effect=RuntimeError("boom")):
            mock_get.return_value.analyze.return_value = fake_analysis
            analysis, chunks, returned_pages = ing.analyze_and_chunk(
                Path("dummy.md"), source="dummy.md", knowledge_analysis_profile="advanced")
        self.assertIs(analysis, fake_analysis)
        self.assertGreaterEqual(len(chunks), 1)


class TestKnowledgeGraphErrorFlag(unittest.TestCase):
    def test_error_flag_false_when_intentionally_skipped(self):
        from services.knowledge_graph_service import get_knowledge_graph_service
        with patch.dict(os.environ, {"ENABLE_KNOWLEDGE_GRAPH": "false"}):
            result = get_knowledge_graph_service().extract_graph("some text", "f.pdf")
        self.assertFalse(result["error"])

    def test_error_flag_true_on_genuine_failure(self):
        from services.knowledge_graph_service import get_knowledge_graph_service
        with patch.dict(os.environ, {"ENABLE_KNOWLEDGE_GRAPH": "true"}), \
             patch("services.llm_service.get_llm_service", side_effect=Exception("no api key")):
            result = get_knowledge_graph_service().extract_graph("some real content here", "f2.pdf")
        self.assertTrue(result["error"])


class TestAIAnalysisDegradedFlag(unittest.TestCase):
    def test_degraded_flag_set_when_llm_fallback_used(self):
        from services.knowledge_analyzer import get_knowledge_analyzer
        pages = [{"page_number": 1, "text": "# Shipping Guide\nWe deliver within 3 days using tracking."}]
        analysis = get_knowledge_analyzer().analyze(pages, "guide.pdf", profile="advanced")
        self.assertTrue(analysis.ai_analysis_degraded)

    def test_degraded_flag_not_set_for_faq_short_circuit(self):
        from services.knowledge_analyzer import get_knowledge_analyzer
        pages = [{"page_number": 1, "text": "Question: q\nAnswer: a", "is_qa_item": True}]
        analysis = get_knowledge_analyzer().analyze(pages, "faq.xlsx", profile="advanced")
        self.assertFalse(analysis.ai_analysis_degraded)


if __name__ == "__main__":
    unittest.main()
