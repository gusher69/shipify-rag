import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.table_markdown import build_table_markdown


class TestBuildTableMarkdown(unittest.TestCase):
    def test_produces_a_real_markdown_table_not_flattened_text(self):
        md = build_table_markdown(
            headers=["Benefit", "Plan 1", "Plan 2", "Plan 3", "Plan 4"],
            rows=[
                ["Maximum annual benefit", "750,000", "1,500,000", "3,000,000", "5,000,000"],
                ["Room per day", "4,000", "5,000", "6,000", "7,000"],
                ["ICU per day", "8,000", "10,000", "12,000", "14,000"],
            ],
            title="Smarter Health Benefits",
            numeric_columns=[1, 2, 3, 4],
        )
        lines = md.splitlines()
        self.assertEqual(lines[0], "## Smarter Health Benefits")
        self.assertIn("| Benefit | Plan 1 | Plan 2 | Plan 3 | Plan 4 |", md)
        self.assertIn("|---|---:|---:|---:|---:|", md)
        self.assertIn("| Maximum annual benefit | 750,000 | 1,500,000 | 3,000,000 | 5,000,000 |", md)
        # Every data row must be a real table row, never flattened prose.
        data_rows = [l for l in lines if l.startswith("| ") and "Benefit" not in l and "---" not in l]
        self.assertEqual(len(data_rows), 3)

    def test_short_rows_are_padded_not_misaligned(self):
        md = build_table_markdown(headers=["A", "B", "C"], rows=[["x", "y"]])
        self.assertIn("| x | y |  |", md)

    def test_no_headers_returns_empty_string(self):
        self.assertEqual(build_table_markdown(headers=[], rows=[["x"]]), "")


if __name__ == "__main__":
    unittest.main()
