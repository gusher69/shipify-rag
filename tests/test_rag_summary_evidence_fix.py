"""Regression tests for the customer-demo P0 fix (2026-07-20):

1. rag/searcher.py::search_excel_structured() must never build a whole-
   sheet chunk for a sheet with no numeric columns (config/instruction/
   schema sheets like "Recommend" or a RAG_Knowledge schema sheet) — only
   sheets with real numeric columns to aggregate.
2. rag/searcher.py::is_broad_summary_query() must detect narrative
   "summarize/overview" style questions, independent of is_analytical().

These deliberately do NOT hit a real Supabase instance — same FakeQuery/
FakeSupabase pattern as tests/test_excel_calculator.py.
"""
import unittest
from unittest.mock import patch

from rag import searcher


class FakeResult:
    def __init__(self, data):
        self.data = data


class FakeQuery:
    def __init__(self, data):
        self._data = data

    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def gt(self, *a, **k): return self
    def order(self, *a, **k): return self
    def filter(self, *a, **k): return self
    def is_(self, *a, **k): return self
    def neq(self, *a, **k): return self
    def execute(self): return FakeResult(self._data)


class FakeSupabase:
    def __init__(self, tables: dict):
        self._tables = tables

    def table(self, name):
        return FakeQuery(self._tables.get(name, []))


SHEETS = [
    {
        "id": "sheet-recommend", "sheet_name": "Recommend",
        "headers": ["Sheet", "Recommended Profile"], "numeric_columns": [],
        "row_count": 3, "markdown_preview": "", "workbook_id": "wb-1",
    },
    {
        "id": "sheet-rag-knowledge-schema", "sheet_name": "RAG_Knowledge",
        "headers": ["Field", "Type", "Description"], "numeric_columns": [],
        "row_count": 5, "markdown_preview": "", "workbook_id": "wb-1",
    },
    {
        "id": "sheet-sales", "sheet_name": "Sales",
        "headers": ["Month", "Revenue"], "numeric_columns": ["Revenue"],
        "row_count": 6, "markdown_preview": "", "workbook_id": "wb-1",
    },
]

ROWS_BY_SHEET = {
    "sheet-recommend": [{"row_data": {"Sheet": "FAQ", "Recommended Profile": "advanced"}}],
    "sheet-rag-knowledge-schema": [{"row_data": {"Field": "question", "Type": "text", "Description": "the FAQ question"}}],
    "sheet-sales": [{"row_data": {"Month": 1, "Revenue": 100}}, {"row_data": {"Month": 5, "Revenue": 500}}],
}


class FakeSheetAwareQuery(FakeQuery):
    """excel_rows lookups are filtered by sheet_id via .eq(); everything
    else behaves like the plain FakeQuery."""
    def __init__(self, table_name, tables):
        super().__init__(tables.get(table_name, []))
        self._table_name = table_name
        self._tables = tables
        self._sheet_id = None

    def eq(self, field, value):
        if field == "sheet_id":
            self._sheet_id = value
        return self

    def execute(self):
        if self._table_name == "excel_rows" and self._sheet_id is not None:
            return FakeResult(ROWS_BY_SHEET.get(self._sheet_id, []))
        return FakeResult(self._data)


class FakeSheetAwareSupabase:
    def __init__(self, tables: dict):
        self._tables = tables

    def table(self, name):
        return FakeSheetAwareQuery(name, self._tables)


class TestSearchExcelStructuredExcludesNonNumericSheets(unittest.TestCase):
    def setUp(self):
        fake_sb = FakeSheetAwareSupabase({"excel_sheets": SHEETS})
        patcher = patch.object(searcher, "_get_supabase", return_value=fake_sb)
        self.addCleanup(patcher.stop)
        patcher.start()

    def test_config_and_schema_sheets_never_become_evidence(self):
        results = searcher.search_excel_structured("ช่วยสรุปข้อมูลบริษัทให้หน่อย")
        sheet_names = {r["source"] for r in results}
        self.assertNotIn("Recommend", sheet_names)
        self.assertNotIn("RAG_Knowledge", sheet_names)

    def test_real_numeric_sheet_still_searchable(self):
        """The fix must not blanket-exclude the whole Excel engine — a
        genuinely numeric sheet (e.g. a Sales sheet with a Revenue column)
        must still produce a chunk."""
        results = searcher.search_excel_structured("สรุปยอดขายทั้งหมด")
        sheet_names = {r["source"] for r in results}
        self.assertIn("Sales", sheet_names)

    def test_config_sheet_chunks_are_never_marked_is_structured_true(self):
        results = searcher.search_excel_structured("ช่วยสรุปข้อมูลบริษัทให้หน่อย")
        for r in results:
            self.assertNotEqual(r["source"], "Recommend")
            self.assertNotEqual(r["source"], "RAG_Knowledge")


class TestBroadSummaryIntentDetection(unittest.TestCase):
    def test_detects_thai_summary_phrasing(self):
        self.assertTrue(searcher.is_broad_summary_query("ช่วยสรุปข้อมูลบริษัทให้หน่อย"))
        self.assertTrue(searcher.is_broad_summary_query("ภาพรวมบริษัทเป็นอย่างไร"))
        self.assertTrue(searcher.is_broad_summary_query("มีบริการอะไรบ้าง"))
        self.assertTrue(searcher.is_broad_summary_query("สรุปข้อมูลการขนส่งทั้งหมด"))

    def test_detects_english_summary_phrasing(self):
        self.assertTrue(searcher.is_broad_summary_query("Can you give a company overview?"))
        self.assertTrue(searcher.is_broad_summary_query("Summary of services please"))
        self.assertTrue(searcher.is_broad_summary_query("What is the Mission and Vision?"))

    def test_exact_faq_question_is_not_broad_summary(self):
        self.assertFalse(searcher.is_broad_summary_query("บริษัทนี้ทำธุรกิจเกี่ยวกับอะไร"))
        self.assertFalse(searcher.is_broad_summary_query("ขอที่อยู่โกดังจีน"))

    def test_independent_of_is_analytical(self):
        """A question can be broad-summary without being analytical, and
        vice versa — the two detectors must not be conflated."""
        self.assertTrue(searcher.is_broad_summary_query("ช่วยสรุปข้อมูลบริษัทให้หน่อย"))
        self.assertFalse(searcher.is_analytical("มีบริการอะไรบ้าง"))
        self.assertTrue(searcher.is_broad_summary_query("มีบริการอะไรบ้าง"))


if __name__ == "__main__":
    unittest.main()
