"""Tests for Phase 2 Part 2 (rag/context_builder.py) — dedup, merge
overlapping, group by source, compress repeated lines, final top-K.
Every test asserts citations/page_number/source survive."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rag.context_builder import build_context, _overlap_ratio, _merge_text


def _chunk(text, **extra):
    c = {"text": text, "citation": extra.pop("citation", "Source: test.pdf"),
         "file_name": extra.pop("file_name", "test.pdf"),
         "page_number": extra.pop("page_number", 1)}
    c.update(extra)
    return c


class TestOverlapRatio(unittest.TestCase):
    def test_identical_text_scores_one(self):
        self.assertEqual(_overlap_ratio("hello world", "hello world"), 1.0)

    def test_disjoint_text_scores_zero(self):
        self.assertEqual(_overlap_ratio("hello world", "foo bar"), 0.0)

    def test_empty_text_scores_zero(self):
        self.assertEqual(_overlap_ratio("", "hello"), 0.0)


class TestMergeText(unittest.TestCase):
    def test_drops_the_real_overlap_and_keeps_the_remainder(self):
        earlier = "the quick brown fox jumps over"
        later = "fox jumps over the lazy dog"
        result = _merge_text(earlier, later)
        self.assertIn("the lazy dog", result)
        # The overlapping phrase must not be duplicated.
        self.assertEqual(result.count("jumps over"), 1)

    def test_no_overlap_appends_everything(self):
        result = _merge_text("hello world", "goodbye moon")
        self.assertIn("hello world", result)
        self.assertIn("goodbye moon", result)


class TestRemoveDuplicateChunks(unittest.TestCase):
    def test_near_identical_chunks_deduplicated_keeping_first(self):
        chunks = [
            _chunk("Plan 4 room rate is 7,000 baht per day.", citation="Source: a.pdf, page 3"),
            _chunk("Plan 4 room rate is 7,000 baht per day!", citation="Source: a.pdf, page 5"),
        ]
        final, summary = build_context(chunks)
        self.assertEqual(len(final), 1)
        self.assertEqual(summary["duplicates_removed"], 1)
        # The duplicate's citation must be preserved on the survivor.
        self.assertIn("Source: a.pdf, page 5", final[0]["merged_citations"])
        self.assertIn("Source: a.pdf, page 3", final[0]["merged_citations"])

    def test_distinct_chunks_never_removed(self):
        chunks = [_chunk("Room rate is 7,000 baht."), _chunk("ICU rate is 10,000 baht.")]
        final, summary = build_context(chunks)
        self.assertEqual(len(final), 2)
        self.assertEqual(summary["duplicates_removed"], 0)


class TestMergeOverlappingChunks(unittest.TestCase):
    def test_adjacent_same_file_overlapping_chunks_merged(self):
        chunks = [
            _chunk("หมวดที่ 9 ค่าบริการทางการแพทย์เพื่อการบำบัดรักษาโรคไตวายเรื้อรังโดยการล้างไต ผ่านทางเส้นเลือด รายละเอียด",
                   file_id="f1", chunk_index=0, page_number=3, citation="Source: a.pdf, page 3"),
            _chunk("ผ่านทางเส้นเลือด รายละเอียด 25,000 25,000 50,000 50,000 เพิ่มเติม",
                   file_id="f1", chunk_index=1, page_number=3, citation="Source: a.pdf, page 3"),
        ]
        final, summary = build_context(chunks)
        self.assertEqual(len(final), 1)
        self.assertEqual(summary["chunks_merged"], 1)
        self.assertIn("25,000", final[0]["text"])
        self.assertIn("หมวดที่ 9", final[0]["text"])
        # citation/page_number must survive the merge.
        self.assertIsNotNone(final[0].get("citation"))
        self.assertEqual(final[0].get("page_number"), 3)

    def test_non_adjacent_chunks_not_merged(self):
        chunks = [
            _chunk("some unrelated text about topic A here for padding words",
                   file_id="f1", chunk_index=0),
            _chunk("completely different topic B content unrelated words here",
                   file_id="f1", chunk_index=5),
        ]
        final, summary = build_context(chunks)
        self.assertEqual(summary["chunks_merged"], 0)
        self.assertEqual(len(final), 2)

    def test_chunks_without_file_id_are_passed_through_untouched(self):
        chunks = [{"text": "calculated result", "is_calculated": True, "citation": "Source: calc"}]
        final, summary = build_context(chunks)
        self.assertEqual(len(final), 1)
        self.assertEqual(final[0]["text"], "calculated result")


class TestCompressRepeatedLines(unittest.TestCase):
    def test_repeated_boilerplate_line_compressed_after_first_occurrence(self):
        boilerplate = "This document is confidential and proprietary information."
        chunks = [
            _chunk(f"Unique content one.\n{boilerplate}", file_id="f1", chunk_index=0),
            _chunk(f"Unique content two.\n{boilerplate}", file_id="f1", chunk_index=10),
        ]
        final, summary = build_context(chunks)
        self.assertGreater(summary["compressed_lines_removed"], 0)
        texts = [c["text"] for c in final]
        self.assertEqual(sum(t.count(boilerplate) for t in texts), 1)

    def test_short_lines_never_compressed(self):
        chunks = [
            _chunk("Header\nContent A", file_id="f1", chunk_index=0),
            _chunk("Header\nContent B", file_id="f1", chunk_index=10),
        ]
        final, summary = build_context(chunks)
        # "Header" is shorter than COMPRESS_LINE_MIN_LEN -> never compressed.
        texts = [c["text"] for c in final]
        self.assertEqual(sum(t.count("Header") for t in texts), 2)


class TestFinalTopK(unittest.TestCase):
    def test_truncates_to_final_top_k_after_all_steps(self):
        chunks = [_chunk(f"Distinct unrelated content number {i} padding words here") for i in range(5)]
        final, summary = build_context(chunks, final_top_k=2)
        self.assertEqual(len(final), 2)
        self.assertEqual(summary["final_count"], 2)

    def test_no_final_top_k_keeps_everything_surviving(self):
        chunks = [_chunk(f"Distinct unrelated content number {i} padding words here") for i in range(5)]
        final, summary = build_context(chunks, final_top_k=None)
        self.assertEqual(len(final), 5)


class TestSummaryShape(unittest.TestCase):
    def test_summary_reports_original_and_source_documents(self):
        chunks = [_chunk("a", file_name="a.pdf"), _chunk("b unrelated content", file_name="b.pdf")]
        final, summary = build_context(chunks)
        self.assertEqual(summary["original_count"], 2)
        self.assertIn("a.pdf", summary["source_documents"])
        self.assertIn("b.pdf", summary["source_documents"])

    def test_empty_input_returns_empty_output_no_crash(self):
        final, summary = build_context([])
        self.assertEqual(final, [])
        self.assertEqual(summary["original_count"], 0)
        self.assertEqual(summary["final_count"], 0)


if __name__ == "__main__":
    unittest.main()
