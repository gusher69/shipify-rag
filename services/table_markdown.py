"""Deterministic Markdown table builder — reusable by both the Vision
provider's structured output (Part "TABLE EXTRACTION") and any future
native/OCR-based table detector. Never flattens a table into unreadable
inline text; always emits a real Markdown table with a header separator.
"""
from typing import List, Optional


def _looks_numeric(cell: str) -> bool:
    if not cell or not cell.strip():
        return False
    stripped = cell.replace(",", "").replace(".", "").replace("(", "").replace(")", "").strip()
    return stripped.isdigit()


def validate_table_shape(headers: List[str], rows: List[List[str]]) -> List[str]:
    """Deterministic sanity checks for Vision-extracted table structure —
    catches the confirmed production defect where row labels are dropped
    entirely and every column (including what should be the label column)
    ends up numeric. Returns a list of human-readable issue strings (empty
    = no issues found). Never raises; purely advisory so a malformed table
    can be flagged/repaired rather than silently shipped."""
    issues: List[str] = []
    if not headers:
        issues.append("Table has no headers.")
        return issues
    if not rows:
        issues.append("Table has no data rows.")
        return issues

    col_count = len(headers)
    for i, row in enumerate(rows):
        if len(row) != col_count:
            issues.append(f"Row {i} has {len(row)} cell(s), expected {col_count} to match the header row.")

    # The confirmed real-world defect: the row-label column got dropped
    # entirely, so every row's first cell is a numeric plan VALUE instead
    # of a benefit label — the whole first column reads as numeric.
    first_col_cells = [row[0] for row in rows if row]
    if first_col_cells and all(_looks_numeric(c) for c in first_col_cells):
        issues.append(
            "Every row's first column is numeric — the row-label column "
            "appears to be missing (values may have shifted left into the "
            "label column instead of a real benefit description)."
        )
    return issues


def build_table_markdown(headers: List[str], rows: List[List[str]], *, title: Optional[str] = None,
                          numeric_columns: Optional[List[int]] = None) -> str:
    """`numeric_columns` (0-indexed) get right-aligned (`---:`) separator
    cells, matching the spec's benefit-table example (amounts right-
    aligned under numeric plan columns)."""
    if not headers:
        return ""
    numeric_columns = set(numeric_columns or [])

    lines = []
    if title:
        lines.append(f"## {title}")
        lines.append("")

    lines.append("| " + " | ".join(headers) + " |")
    sep_cells = [("---:" if i in numeric_columns else "---") for i in range(len(headers))]
    lines.append("|" + "|".join(sep_cells) + "|")

    for row in rows:
        padded = list(row) + [""] * (len(headers) - len(row))
        padded = padded[:len(headers)]
        lines.append("| " + " | ".join(str(c) if c is not None else "" for c in padded) + " |")

    return "\n".join(lines)
