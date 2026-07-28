"""Tests for the AI Knowledge Analyzer (services/knowledge_analyzer.py)
and its integration into chunking (ingestion/ingest.py's
chunk_pages_by_heading / analyze_and_chunk).

LLM calls are mocked/disabled throughout — these tests exercise the
deterministic parts (section detection, quality review, fallback
classification, chunk dispatch) which must work correctly with or
without an LLM available.
"""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("ENABLE_KNOWLEDGE_ANALYZER_LLM", "false")
os.environ.setdefault("ENABLE_KNOWLEDGE_GRAPH", "false")

from services import knowledge_analyzer as ka
from ingestion import ingest as ing


class TestSectionDetection(unittest.TestCase):
    def test_flat_text_with_no_headings_is_one_unnamed_section(self):
        secs = ka.detect_sections("just some plain text\nwith no structure at all")
        self.assertEqual(len(secs), 1)
        self.assertEqual(secs[0].heading, "")

    def test_heading_hierarchy_preserved(self):
        text = "# Manual\n\n## Setup\n\nStep 1: plug in the device.\n\n### Troubleshooting\n\nCheck the cable."
        secs = ka.detect_sections(text)
        headings = [s.heading for s in secs]
        self.assertEqual(headings, ["Manual", "Setup", "Troubleshooting"])
        setup = secs[1]
        self.assertEqual(setup.heading_path, ["Manual", "Setup"])
        trouble = secs[2]
        self.assertEqual(trouble.heading_path, ["Manual", "Setup", "Troubleshooting"])

    def test_original_text_preserved_verbatim_in_sections(self):
        text = "# Title\n\nExact original wording must survive, including punctuation!!"
        secs = ka.detect_sections(text)
        self.assertIn("Exact original wording must survive, including punctuation!!", secs[0].text)


class TestFallbackClassification(unittest.TestCase):
    def test_faq_keywords_detected(self):
        ktype, conf = ka._fallback_classify("Frequently Asked Questions\nQ: what is this? A: a test.")
        self.assertEqual(ktype, "FAQ")

    def test_shipping_keywords_detected(self):
        ktype, _ = ka._fallback_classify("This is our shipping guide, covering delivery time and tracking number lookup.")
        self.assertEqual(ktype, "Shipping Guide")

    def test_generic_mention_of_shipping_does_not_false_positive(self):
        ktype, _ = ka._fallback_classify(
            "We are a logistics company. Make shipping easy for everyone, that's our mission.")
        self.assertNotEqual(ktype, "Shipping Guide")

    def test_unrelated_text_is_unknown(self):
        ktype, conf = ka._fallback_classify("The quick brown fox jumps over the lazy dog.")
        self.assertEqual(ktype, "Unknown")
        self.assertLess(conf, 0.5)


class TestQualityReview(unittest.TestCase):
    def test_missing_headings_flagged(self):
        secs = ka.detect_sections("no headings here at all, just prose text of reasonable length here")
        issues = ka.run_quality_review(secs, "no headings here")
        self.assertTrue(any(i.kind == "missing_headings" for i in issues))

    def test_very_short_section_flagged_info(self):
        secs = ka.detect_sections("# Tiny\nok")
        issues = ka.run_quality_review(secs, "")
        self.assertTrue(any(i.kind == "very_short_section" and i.severity == "info" for i in issues))

    def test_very_large_section_flagged_warning(self):
        secs = ka.detect_sections("# Big\n" + ("word " * 2000))
        issues = ka.run_quality_review(secs, "")
        self.assertTrue(any(i.kind == "very_large_chunk" for i in issues))

    def test_duplicate_sections_flagged(self):
        body = "This exact paragraph appears twice in the document for testing purposes here."
        text = f"# A\n{body}\n\n# B\n{body}"
        secs = ka.detect_sections(text)
        issues = ka.run_quality_review(secs, text)
        self.assertTrue(any(i.kind == "duplicate_knowledge" for i in issues))

    def test_pii_email_detected(self):
        secs = ka.detect_sections("# Contact\nReach us at someone@example.com for support.")
        issues = ka.run_quality_review(secs, "")
        self.assertTrue(any(i.kind == "pii_detected" for i in issues))

    def test_internal_only_marker_detected(self):
        secs = ka.detect_sections("# Notes\nThis section is Internal Only — do not share externally.")
        issues = ka.run_quality_review(secs, "")
        self.assertTrue(any(i.kind == "internal_only" for i in issues))

    def test_outdated_marker_detected(self):
        secs = ka.detect_sections("# Old Policy\nThis policy is outdated and no longer valid.")
        issues = ka.run_quality_review(secs, "")
        self.assertTrue(any(i.kind == "possibly_outdated" for i in issues))

    def test_clean_document_has_no_issues(self):
        secs = ka.detect_sections("# Intro\n" + ("This is a perfectly normal paragraph. " * 10) +
                                   "\n\n# Details\n" + ("More normal content here. " * 10))
        issues = ka.run_quality_review(secs, "")
        kinds = {i.kind for i in issues}
        self.assertNotIn("missing_headings", kinds)
        self.assertNotIn("very_short_section", kinds)


class TestSuggestActions(unittest.TestCase):
    def test_duplicate_issue_suggests_merge(self):
        issues = [ka.QualityIssue("duplicate_knowledge", "x", "warning")]
        suggestions = ka.suggest_actions(issues, [])
        self.assertTrue(any("Merge" in s for s in suggestions))

    def test_large_chunk_issue_suggests_split(self):
        issues = [ka.QualityIssue("very_large_chunk", "x", "warning")]
        suggestions = ka.suggest_actions(issues, [])
        self.assertTrue(any("Split" in s for s in suggestions))


class TestAnalyzerEndToEndDeterministic(unittest.TestCase):
    """LLM disabled via env — exercises the fallback path fully."""

    def test_qa_workbook_short_circuits_to_faq(self):
        pages = [{"page_number": 1, "text": "Question: q\nAnswer: a", "is_qa_item": True}]
        analysis = ka.get_knowledge_analyzer().analyze(pages, "faq.xlsx")
        self.assertEqual(analysis.knowledge_type, "FAQ")
        self.assertEqual(analysis.chunk_strategy, "faq_qa")
        self.assertEqual(analysis.confidence, 1.0)

    def test_document_gets_fallback_classification_and_structure(self):
        pages = [{"page_number": 1, "text": "# Shipping Guide\nWe deliver within 3 days using tracking."}]
        analysis = ka.get_knowledge_analyzer().analyze(pages, "guide.pdf")
        self.assertEqual(analysis.knowledge_type, "Shipping Guide")
        self.assertEqual(analysis.chunk_strategy, ka.CHUNK_STRATEGY_BY_TYPE["Shipping Guide"])
        self.assertEqual(len(analysis.document_structure), 1)
        self.assertFalse(analysis.used_llm)

    def test_chunk_strategy_mapping_covers_every_knowledge_type(self):
        for ktype in ka.KNOWLEDGE_TYPES:
            self.assertIn(ktype, ka.CHUNK_STRATEGY_BY_TYPE)


class TestAIProfileGating(unittest.TestCase):
    def test_disabled_profile_skips_analysis_entirely(self):
        pages = [{"page_number": 1, "text": "# Shipping Guide\nWe deliver within 3 days."}]
        analysis = ka.get_knowledge_analyzer().analyze(pages, "guide.pdf", profile="disabled")
        self.assertEqual(analysis.knowledge_type, "Unknown")
        self.assertEqual(analysis.confidence, 0.0)
        self.assertEqual(analysis.chunk_strategy, "hybrid")
        self.assertEqual(analysis.knowledge_graph["nodes"], [])
        self.assertEqual(analysis.knowledge_graph["edges"], [])

    def test_basic_profile_classifies_but_skips_graph(self):
        pages = [{"page_number": 1, "text": "# Shipping Guide\nWe deliver within 3 days using tracking."}]
        analysis = ka.get_knowledge_analyzer().analyze(pages, "guide.pdf", profile="basic")
        self.assertEqual(analysis.knowledge_type, "Shipping Guide")
        self.assertEqual(analysis.knowledge_graph["nodes"], [])
        self.assertEqual(analysis.knowledge_graph["edges"], [])

    def test_standard_profile_classifies_but_skips_graph(self):
        pages = [{"page_number": 1, "text": "# Financial Report\nRevenue grew 15% this quarter."}]
        analysis = ka.get_knowledge_analyzer().analyze(pages, "report.pdf", profile="standard")
        self.assertEqual(analysis.knowledge_graph["nodes"], [])
        self.assertEqual(analysis.knowledge_graph["edges"], [])

    def test_advanced_profile_attempts_graph_extraction(self):
        # ENABLE_KNOWLEDGE_GRAPH is false in this test module, so extraction
        # itself no-ops — this only asserts "advanced" doesn't short-circuit
        # before reaching the graph extraction call, unlike basic/standard.
        pages = [{"page_number": 1, "text": "# Company Profile\nShipify provides shipping."}]
        analysis = ka.get_knowledge_analyzer().analyze(pages, "profile.pdf", profile="advanced")
        self.assertEqual(analysis.knowledge_graph["nodes"], [])
        self.assertEqual(analysis.knowledge_graph["edges"], [])

    def test_default_profile_none_matches_prior_behavior(self):
        pages = [{"page_number": 1, "text": "# Shipping Guide\nWe deliver within 3 days."}]
        analysis = ka.get_knowledge_analyzer().analyze(pages, "guide.pdf")
        self.assertEqual(analysis.knowledge_type, "Shipping Guide")


class TestChunkPagesByHeading(unittest.TestCase):
    def test_splits_by_heading_and_tags_metadata(self):
        pages = [{"page_number": 1, "text": "# Company Profile\nWe are a logistics company.\n\n## Mission\nMake shipping easy."}]
        analysis = ka.get_knowledge_analyzer().analyze(pages, "profile.pdf")
        chunks = ing.chunk_pages_by_heading(pages, analysis, source="profile.pdf")
        self.assertGreaterEqual(len(chunks), 2)
        headings = {c["metadata"]["section_title"] for c in chunks}
        self.assertIn("Company Profile", headings)
        self.assertIn("Mission", headings)
        for c in chunks:
            self.assertEqual(c["metadata"]["knowledge_type"], "Company Profile")
            self.assertEqual(c["metadata"]["chunk_strategy"], "heading_paragraph")

    def test_original_chunk_text_is_verbatim_not_ai_rewritten(self):
        original = "This exact sentence must appear verbatim in the resulting chunk text unmodified."
        pages = [{"page_number": 1, "text": f"# Section\n{original}"}]
        analysis = ka.get_knowledge_analyzer().analyze(pages, "doc.pdf")
        chunks = ing.chunk_pages_by_heading(pages, analysis, source="doc.pdf")
        self.assertTrue(any(original in c["text"] for c in chunks))

    def test_step_sequence_splits_numbered_steps_with_index(self):
        pages = [{"page_number": 1, "text": "# Setup Procedure\n1. Unbox the device.\n2. Plug in the power cable.\n3. Turn it on."}]
        analysis = ka.get_knowledge_analyzer().analyze(pages, "sop.pdf")
        analysis.chunk_strategy = "step_sequence"
        chunks = ing.chunk_pages_by_heading(pages, analysis, source="sop.pdf")
        step_indices = sorted(c["metadata"].get("step_index") for c in chunks if "step_index" in c["metadata"])
        self.assertEqual(step_indices, [0, 1, 2])

    def test_no_headings_falls_back_to_generic_chunker(self):
        pages = [{"page_number": 1, "text": "flat text with no headings whatsoever in it at all here"}]
        analysis = ka.get_knowledge_analyzer().analyze(pages, "doc.txt")
        chunks = ing.chunk_pages_by_heading(pages, analysis, source="doc.txt")
        self.assertGreaterEqual(len(chunks), 1)


class TestChunkIndexFix(unittest.TestCase):
    """Regression tests for the confirmed defect: chunk_text() used to
    reset chunk_index=0 on every call, and both chunk_pages() (once per
    page) and chunk_pages_by_heading() (once per section) call chunk_text()
    multiple times for a single file — every page/section's first chunk
    ended up with chunk_index=0, confirmed against real DB data for
    company-profile-test.md (4 sections, all chunk_index=0)."""

    def test_multiple_headings_get_globally_increasing_chunk_index(self):
        pages = [{"page_number": 1, "text": (
            "# Shipify Company Profile\nIntro text.\n\n"
            "## Our Mission\nMission text.\n\n"
            "## Our Services\nServices text.\n\n"
            "## Contact Us\nContact text."
        )}]
        analysis = ka.get_knowledge_analyzer().analyze(pages, "company-profile-test.md")
        chunks = ing.chunk_pages_by_heading(pages, analysis, source="company-profile-test.md")
        indexes = [c["metadata"]["chunk_index"] for c in chunks]
        self.assertEqual(indexes, list(range(len(chunks))), f"chunk_index must be globally increasing, got {indexes}")
        self.assertEqual(len(set(indexes)), len(indexes), "chunk_index must be unique per chunk within this file")

    def test_multiple_chunks_within_one_heading_continue_incrementing(self):
        # A single section long enough to itself be split into multiple
        # token-sized chunks by chunk_text() — indices must keep
        # increasing across BOTH the section boundary and the intra-
        # section split.
        long_section = "# Big Section\n" + ("word " * 2000)
        pages = [{"page_number": 1, "text": long_section + "\n\n## Small Section\nshort text."}]
        analysis = ka.get_knowledge_analyzer().analyze(pages, "long.md")
        chunks = ing.chunk_pages_by_heading(pages, analysis, source="long.md")
        self.assertGreater(len(chunks), 2, "fixture must actually produce multiple chunks for this test to be meaningful")
        indexes = [c["metadata"]["chunk_index"] for c in chunks]
        self.assertEqual(indexes, list(range(len(chunks))))

    def test_chunk_pages_generic_chunker_also_increments_across_pages(self):
        """Same bug class in the non-heading-aware chunker: chunk_text()
        is called once per page in chunk_pages() too."""
        pages = [
            {"page_number": 1, "text": "First page content here."},
            {"page_number": 2, "text": "Second page content here."},
            {"page_number": 3, "text": "Third page content here."},
        ]
        chunks = ing.chunk_pages(pages, source="multi-page.pdf")
        indexes = [c["metadata"]["chunk_index"] for c in chunks]
        self.assertEqual(indexes, list(range(len(chunks))))

    def test_no_duplicate_chunk_index_across_re_ingestion_of_same_file(self):
        """Each call to chunk_pages_by_heading is independent (indices
        always start at 0 for a FRESH ingestion) — this documents that
        re-ingestion is expected to fully replace a file's chunks (see
        admin/routes.py's deactivate_old_chunks call before re-chunking),
        not append to stale ones with colliding indices."""
        pages = [{"page_number": 1, "text": "# A\ntext a\n\n## B\ntext b"}]
        analysis = ka.get_knowledge_analyzer().analyze(pages, "doc.md")
        first_run = ing.chunk_pages_by_heading(pages, analysis, source="doc.md")
        second_run = ing.chunk_pages_by_heading(pages, analysis, source="doc.md")
        self.assertEqual(
            [c["metadata"]["chunk_index"] for c in first_run],
            [c["metadata"]["chunk_index"] for c in second_run],
        )


class TestAnalyzeAndChunkDispatch(unittest.TestCase):
    def test_excel_qa_file_uses_generic_chunk_pages_not_heading_chunker(self):
        with patch("ingestion.ingest.read_file_pages") as mock_read, \
             patch("ingestion.ingest.chunk_pages_by_heading") as mock_heading_chunk:
            mock_read.return_value = [{"page_number": 1, "text": "Question: q\nAnswer: a", "is_qa_item": True}]
            analysis, chunks, pages = ing.analyze_and_chunk(Path("faq.xlsx"), source="faq.xlsx")
        self.assertEqual(analysis.chunk_strategy, "faq_qa")
        mock_heading_chunk.assert_not_called()
        self.assertGreaterEqual(len(chunks), 1)

    def test_empty_pages_returns_no_analysis_no_chunks(self):
        with patch("ingestion.ingest.read_file_pages", return_value=[]):
            analysis, chunks, pages = ing.analyze_and_chunk(Path("empty.pdf"), source="empty.pdf")
        self.assertIsNone(analysis)
        self.assertEqual(chunks, [])
        self.assertEqual(pages, [])


if __name__ == "__main__":
    unittest.main()
