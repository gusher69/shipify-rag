"""Tests for the deterministic Excel Calculation Engine (rag/calculator.py)
and the "duplicate filename upload never inherits old state" guarantee
(ingestion/embedder.py::register_file).

Run with:
    python -m unittest tests.test_excel_calculator -v

These tests deliberately do NOT hit a real Supabase instance — they inject
a fake client so the test suite runs anywhere, deterministically, and
proves the calculation logic itself (the part that must never hallucinate)
without depending on network access or fixture data being present in a
live database.
"""
import unittest
from unittest.mock import patch

from rag import calculator as calc


class FakeResult:
    def __init__(self, data):
        self.data = data


class FakeQuery:
    """Minimal stand-in for the supabase-py fluent query builder — every
    chain method just returns self; only .execute() matters for these tests."""
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


def make_catalog(sheet_id="sheet-1", **overrides):
    catalog_entry = {
        "sheet_id": sheet_id,
        "workbook_filename": "sales.xlsx",
        "sheet_name": "Sales",
        "headers": ["Month", "Product", "Status", "Revenue", "Date"],
        "numeric_columns": ["Revenue"],
        "date_columns": ["Date"],
        "currency_columns": [],
        "percentage_columns": [],
        "is_hidden": False,
        "row_count": 6,
    }
    catalog_entry.update(overrides)
    return [catalog_entry]


SAMPLE_ROWS = [
    {"row_index": 1, "row_data": {"Month": 1, "Product": "Widget", "Status": "Completed", "Revenue": 100, "Date": "2026-01-15"}},
    {"row_index": 2, "row_data": {"Month": 5, "Product": "Widget", "Status": "Completed", "Revenue": 500, "Date": "2026-05-10"}},
    {"row_index": 3, "row_data": {"Month": 5, "Product": "Gadget", "Status": "Pending",   "Revenue": 300, "Date": "2026-05-20"}},
    {"row_index": 4, "row_data": {"Month": 8, "Product": "Widget", "Status": "Completed", "Revenue": 800, "Date": "2026-08-01"}},
    {"row_index": 5, "row_data": {"Month": 8, "Product": "Gadget", "Status": "Pending",   "Revenue": 200, "Date": "2026-08-15"}},
    {"row_index": 6, "row_data": {"Month": 12, "Product": "Gizmo", "Status": "Completed", "Revenue": 50, "Date": "2026-12-31"}},
]


class TestExcelCalculationEngine(unittest.TestCase):
    def setUp(self):
        self.catalog = make_catalog()
        patcher = patch.object(calc, "_get_supabase", return_value=FakeSupabase({"excel_rows": SAMPLE_ROWS}))
        self.addCleanup(patcher.stop)
        patcher.start()

    def test_sum_by_month_may_plus_august(self):
        """'total revenue of May and August' — the exact example from the spec."""
        plan = {"sheet_id": "sheet-1", "operation": "sum", "target_columns": ["Revenue"],
                "filters": [{"column": "Month", "values": ["5", "8"]}],
                "group_by": None, "date_column": None, "n": 5, "sort_order": "desc"}
        result = calc._execute_plan(plan, self.catalog)
        self.assertTrue(result["ok"])
        # rows 2,3,4,5 all have Month in (5,8): 500+300+800+200 = 1800
        self.assertEqual(result["result"], 1800)
        self.assertEqual(sorted(result["rows_used"]), [2, 3, 4, 5])

    def test_filter_by_status_pending(self):
        plan = {"sheet_id": "sheet-1", "operation": "count", "target_columns": ["Revenue"],
                "filters": [{"column": "Status", "values": ["Pending"]}],
                "group_by": None, "date_column": None, "n": 5, "sort_order": "desc"}
        result = calc._execute_plan(plan, self.catalog)
        self.assertTrue(result["ok"])
        self.assertEqual(result["result"], 2)  # rows 3 and 5

    def test_top_n(self):
        plan = {"sheet_id": "sheet-1", "operation": "top_n", "target_columns": ["Revenue"],
                "filters": [], "group_by": None, "date_column": None, "n": 2, "sort_order": "desc"}
        result = calc._execute_plan(plan, self.catalog)
        self.assertTrue(result["ok"])
        self.assertEqual([r["value"] for r in result["ranked"]], [800, 500])
        self.assertEqual([r["row"] for r in result["ranked"]], [4, 2])

    def test_bottom_n(self):
        plan = {"sheet_id": "sheet-1", "operation": "bottom_n", "target_columns": ["Revenue"],
                "filters": [], "group_by": None, "date_column": None, "n": 2, "sort_order": "desc"}
        result = calc._execute_plan(plan, self.catalog)
        self.assertTrue(result["ok"])
        self.assertEqual([r["value"] for r in result["ranked"]], [50, 100])

    def test_average(self):
        plan = {"sheet_id": "sheet-1", "operation": "avg", "target_columns": ["Revenue"],
                "filters": [], "group_by": None, "date_column": None, "n": 5, "sort_order": "desc"}
        result = calc._execute_plan(plan, self.catalog)
        self.assertTrue(result["ok"])
        expected = (100 + 500 + 300 + 800 + 200 + 50) / 6
        self.assertAlmostEqual(result["result"], expected)

    def test_median(self):
        plan = {"sheet_id": "sheet-1", "operation": "median", "target_columns": ["Revenue"],
                "filters": [], "group_by": None, "date_column": None, "n": 5, "sort_order": "desc"}
        result = calc._execute_plan(plan, self.catalog)
        self.assertTrue(result["ok"])
        # sorted: 50,100,200,300,500,800 -> median = (200+300)/2 = 250
        self.assertEqual(result["result"], 250)

    def test_group_by_highest_product_revenue(self):
        plan = {"sheet_id": "sheet-1", "operation": "sum", "target_columns": ["Revenue"],
                "filters": [], "group_by": "Product", "date_column": None, "n": 5, "sort_order": "desc"}
        result = calc._execute_plan(plan, self.catalog)
        self.assertTrue(result["ok"])
        self.assertEqual(result["breakdown"]["Widget"], 1400)  # 100+500+800
        self.assertEqual(result["breakdown"]["Gadget"], 500)   # 300+200
        self.assertEqual(result["top_group"], "Widget")

    def test_date_range_filter(self):
        plan = {"sheet_id": "sheet-1", "operation": "sum", "target_columns": ["Revenue"],
                "filters": [{"column": "Date", "date_from": "2026-05-01", "date_to": "2026-08-31"}],
                "group_by": None, "date_column": None, "n": 5, "sort_order": "desc"}
        result = calc._execute_plan(plan, self.catalog)
        self.assertTrue(result["ok"])
        self.assertEqual(result["result"], 1800)  # rows 2,3,4,5

    def test_month_filter_helper(self):
        plan = {"sheet_id": "sheet-1", "operation": "sum", "target_columns": ["Revenue"],
                "filters": [{"column": "Date", "month": 5}],
                "group_by": None, "date_column": None, "n": 5, "sort_order": "desc"}
        result = calc._execute_plan(plan, self.catalog)
        self.assertTrue(result["ok"])
        self.assertEqual(result["result"], 800)  # rows 2,3 (May)

    def test_distinct(self):
        plan = {"sheet_id": "sheet-1", "operation": "distinct", "target_columns": [],
                "filters": [], "group_by": "Status", "date_column": None, "n": 5, "sort_order": "desc"}
        result = calc._execute_plan(plan, self.catalog)
        self.assertTrue(result["ok"])
        self.assertEqual(result["distinct_values"], ["Completed", "Pending"])

    def test_sort(self):
        plan = {"sheet_id": "sheet-1", "operation": "sort", "target_columns": ["Revenue"],
                "filters": [], "group_by": None, "date_column": None, "n": 5, "sort_order": "asc"}
        result = calc._execute_plan(plan, self.catalog)
        self.assertTrue(result["ok"])
        self.assertEqual([r["value"] for r in result["sorted_rows"]], [50, 100, 200, 300, 500, 800])

    def test_percentage(self):
        plan = {"sheet_id": "sheet-1", "operation": "percentage", "target_columns": ["Revenue"],
                "filters": [{"column": "Product", "values": ["Widget"]}],
                "group_by": None, "date_column": None, "n": 5, "sort_order": "desc"}
        result = calc._execute_plan(plan, self.catalog)
        self.assertTrue(result["ok"])
        total = 100 + 500 + 300 + 800 + 200 + 50
        self.assertAlmostEqual(result["pct"], 1400 / total * 100)

    def test_missing_column_never_estimates(self):
        """Financial safety: a column that doesn't exist must return ok=False
        with a clear reason — never a guessed/estimated number."""
        plan = {"sheet_id": "sheet-1", "operation": "sum", "target_columns": ["DoesNotExist"],
                "filters": [], "group_by": None, "date_column": None, "n": 5, "sort_order": "desc"}
        result = calc._execute_plan(plan, self.catalog)
        self.assertFalse(result["ok"])
        self.assertIn("reason", result)
        self.assertNotIn("result", result)

    def test_no_matching_filter_never_estimates(self):
        plan = {"sheet_id": "sheet-1", "operation": "sum", "target_columns": ["Revenue"],
                "filters": [{"column": "Month", "values": ["99"]}],
                "group_by": None, "date_column": None, "n": 5, "sort_order": "desc"}
        result = calc._execute_plan(plan, self.catalog)
        self.assertFalse(result["ok"])
        self.assertIn("reason", result)

    def test_unknown_sheet_never_estimates(self):
        plan = {"sheet_id": "does-not-exist", "operation": "sum", "target_columns": ["Revenue"],
                "filters": [], "group_by": None, "date_column": None, "n": 5, "sort_order": "desc"}
        result = calc._execute_plan(plan, self.catalog)
        self.assertFalse(result["ok"])

    def test_currency_unit_preserved_in_output_text(self):
        catalog = make_catalog(currency_columns=["Revenue"])
        plan = {"sheet_id": "sheet-1", "operation": "sum", "target_columns": ["Revenue"],
                "filters": [], "group_by": None, "date_column": None, "n": 5, "sort_order": "desc"}
        result = calc._execute_plan(plan, catalog)
        self.assertEqual(result["unit"], "currency")

    def test_running_total(self):
        plan = {"sheet_id": "sheet-1", "operation": "running_total", "target_columns": ["Revenue"],
                "filters": [], "group_by": None, "date_column": "Date", "n": 5, "sort_order": "desc"}
        result = calc._execute_plan(plan, self.catalog)
        self.assertTrue(result["ok"])
        self.assertEqual(result["final_total"], 100 + 500 + 300 + 800 + 200 + 50)

    def test_mom_growth(self):
        plan = {"sheet_id": "sheet-1", "operation": "mom", "target_columns": ["Revenue"],
                "filters": [], "group_by": None, "date_column": "Date", "n": 5, "sort_order": "desc"}
        result = calc._execute_plan(plan, self.catalog)
        self.assertTrue(result["ok"])
        self.assertGreaterEqual(len(result["growth_series"]), 1)


class TestDuplicateFilenameUpload(unittest.TestCase):
    """register_file() must ALWAYS insert a new row — never look up and
    reuse an existing row by filename. This is what makes a duplicate
    filename upload behave like a completely new file."""

    def test_register_file_always_inserts_never_selects_by_filename(self):
        from ingestion import embedder

        insert_calls = []

        class FakeInsertResult:
            data = [{"id": "new-uuid-1234"}]

        class FakeTable:
            def insert(self, payload):
                insert_calls.append(payload)
                return self

            def select(self, *a, **k):
                raise AssertionError(
                    "register_file() must never SELECT-by-filename to look "
                    "for an existing row to reuse — every upload gets a new row."
                )

            def execute(self):
                return FakeInsertResult()

        fake_sb = FakeSupabase({})
        fake_sb.table = lambda name: FakeTable()

        with patch.object(embedder, "_get_supabase", return_value=fake_sb):
            file_id = embedder.register_file("requirements.txt", 1234)

        self.assertEqual(file_id, "new-uuid-1234")
        self.assertEqual(len(insert_calls), 1)
        self.assertEqual(insert_calls[0]["filename"], "requirements.txt")

    def test_two_uploads_same_filename_get_different_ids(self):
        from ingestion import embedder

        counter = {"n": 0}

        class FakeTable:
            def insert(self, payload):
                counter["n"] += 1
                self._id = f"uuid-{counter['n']}"
                return self

            def execute(self):
                return type("R", (), {"data": [{"id": self._id}]})()

        fake_sb = FakeSupabase({})
        fake_sb.table = lambda name: FakeTable()

        with patch.object(embedder, "_get_supabase", return_value=fake_sb):
            id1 = embedder.register_file("duplicate.xlsx", 100)
            id2 = embedder.register_file("duplicate.xlsx", 200)

        self.assertNotEqual(id1, id2)


if __name__ == "__main__":
    unittest.main()
