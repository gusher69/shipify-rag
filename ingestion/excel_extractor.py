"""Structured Excel/CSV extraction for RAG + analytics.

Two outputs per workbook:
  1. Row-group pages (Markdown table per N rows)  → chunking + embedding pipeline
  2. Structured rows (JSONB)                      → excel_rows for analytics
"""
import datetime
import re
from pathlib import Path
from typing import List, Dict, Optional

try:
    import openpyxl
    from openpyxl.utils import get_column_letter as _xl_col_letter
    _HAS_OPENPYXL = True
except ImportError:
    _HAS_OPENPYXL = False

from ingestion.smart_enrichment import (
    generate_tags, generate_alt_questions, infer_category, detect_language,
)

# Rows per chunk page — small enough to stay within token limits,
# large enough to give the LLM useful table context.
ROWS_PER_CHUNK = 30


def _col_letter(n: int) -> str:
    """Return Excel column letter for 1-based column index without openpyxl."""
    if _HAS_OPENPYXL:
        return _xl_col_letter(n)
    result = ""
    while n:
        n, rem = divmod(n - 1, 26)
        result = chr(65 + rem) + result
    return result


# ── Public API ────────────────────────────────────────────────────

def extract_workbook(file_path: Path) -> Dict:
    """Extract full structured data from an Excel workbook.

    Returns:
        {
          sheet_count: int,
          sheet_names: [str],
          sheets: [sheet_dict, ...],
        }
    Each sheet_dict has: sheet_name, sheet_index, row_count, column_count,
    headers, rows, numeric_columns, date_columns, has_formula, formula_cells,
    cell_range, markdown_preview.
    """
    if not _HAS_OPENPYXL:
        raise ImportError("openpyxl is required: pip install openpyxl")

    # Load with data_only=True to get computed cell values (not formula strings)
    wb = openpyxl.load_workbook(str(file_path), data_only=True)

    sheets = []
    for idx, sheet_name in enumerate(wb.sheetnames):
        ws = wb[sheet_name]
        sheet = _extract_sheet(ws, sheet_name, idx)
        # sheet_state is 'visible' | 'hidden' | 'veryHidden'
        sheet["is_hidden"] = getattr(ws, "sheet_state", "visible") != "visible"
        sheets.append(sheet)

    wb.close()
    return {
        "sheet_count": len(sheets),
        "sheet_names": wb.sheetnames,
        "sheets": sheets,
    }


def extract_csv(file_path: Path) -> Dict:
    """Extract a CSV file as a single-sheet workbook using the same schema as extract_workbook."""
    try:
        import pandas as pd
    except ImportError:
        raise ImportError("pandas is required for CSV support: pip install pandas")

    stem = file_path.stem
    df = None
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            df = pd.read_csv(str(file_path), encoding=enc, dtype=str)
            break
        except (UnicodeDecodeError, Exception):
            continue

    if df is None or df.empty:
        return {"sheet_count": 0, "sheet_names": [], "sheets": []}

    df = df.dropna(how="all").dropna(axis=1, how="all").reset_index(drop=True)
    if df.empty:
        return {"sheet_count": 0, "sheet_names": [], "sheets": []}

    headers = [str(c).strip() for c in df.columns]
    rows: List[Dict] = []
    numeric_cols: set = set()
    currency_cols: set = set()
    percentage_cols: set = set()
    boolean_cols: set = set()
    _bool_strs = {"true", "false", "yes", "no"}

    for r_idx, row in df.iterrows():
        row_data: Dict = {}
        has_data = False
        for h in headers:
            raw = row.get(h)
            if raw is None or (isinstance(raw, float) and __import__("math").isnan(raw)) or str(raw).strip() == "":
                row_data[h] = None
                continue
            has_data = True
            val = str(raw).strip()
            if val.lower() in _bool_strs:
                row_data[h] = val.lower() in ("true", "yes")
                boolean_cols.add(h)
                continue
            is_pct = val.endswith("%")
            is_cur = bool(_CURRENCY_FORMAT_RE.search(val))
            cleaned = val.rstrip("%").lstrip("$€£¥฿").replace(",", "").strip()
            try:
                fval = float(cleaned)
                typed = int(fval) if fval == int(fval) else fval
                row_data[h] = typed
                numeric_cols.add(h)
                if is_pct:
                    percentage_cols.add(h)
                elif is_cur:
                    currency_cols.add(h)
            except (ValueError, TypeError):
                row_data[h] = val
        if has_data:
            rows.append({"row_index": int(r_idx) + 1, "row_data": row_data})

    col_count = len(headers)
    sheet: Dict = {
        "sheet_name":      stem,
        "sheet_index":     0,
        "row_count":       len(rows),
        "column_count":    col_count,
        "headers":         headers,
        "rows":            rows,
        "numeric_columns": sorted(numeric_cols),
        "date_columns":    [],
        "currency_columns": sorted(currency_cols),
        "percentage_columns": sorted(percentage_cols),
        "boolean_columns": sorted(boolean_cols),
        "has_formula":     False,
        "formula_cells":   {},
        "cell_range":      f"A1:{_col_letter(col_count)}{1 + len(rows)}",
        "markdown_preview": _sheet_to_markdown(stem, headers, rows),
    }
    return {"sheet_count": 1, "sheet_names": [stem], "sheets": [sheet]}


_QUESTION_COL_RE = re.compile(r"^(question|คำถาม|q)s?$|^(question|คำถาม)", re.IGNORECASE)
_ANSWER_COL_RE   = re.compile(r"^(answer|คำตอบ|a)s?$|^(answer|คำตอบ)", re.IGNORECASE)
# Optional metadata columns — recognized when present, but the importer
# never REQUIRES them (see ingestion/smart_enrichment.py, which fills the
# gap with inference/generation when a sheet doesn't provide these).
_CATEGORY_COL_RE = re.compile(r"^categor(y|ies)$|หมวดหมู่|ประเภท", re.IGNORECASE)
_CHANNEL_COL_RE  = re.compile(r"^channel[s]?$|ช่องทาง", re.IGNORECASE)
_TAGS_COL_RE     = re.compile(r"^tag[s]?$|แท็ก", re.IGNORECASE)
_ALT_Q_COL_RE    = re.compile(r"alternative\s*question|alt.?question|คำถามอื่น|คำถามสำรอง", re.IGNORECASE)


def detect_optional_qa_columns(headers: List[str]) -> Dict[str, Optional[str]]:
    """Return {"category": header_or_None, "channel": ..., "tags": ...,
    "alt_questions": ...} — best-effort recognition of columns a customer
    MAY have included. Absence of any of these is completely normal and
    handled by generation/inference, never an import error."""
    return {
        "category": next((h for h in headers if _CATEGORY_COL_RE.search(str(h).strip())), None),
        "channel": next((h for h in headers if _CHANNEL_COL_RE.search(str(h).strip())), None),
        "tags": next((h for h in headers if _TAGS_COL_RE.search(str(h).strip())), None),
        "alt_questions": next((h for h in headers if _ALT_Q_COL_RE.search(str(h).strip())), None),
    }


def detect_qa_columns(headers: List[str]) -> Dict[str, Optional[str]]:
    """Return {"question": header_or_None, "answer": header_or_None}."""
    q = next((h for h in headers if _QUESTION_COL_RE.search(str(h).strip())), None)
    a = next((h for h in headers if _ANSWER_COL_RE.search(str(h).strip())), None)
    return {"question": q, "answer": a}


def is_qa_sheet(headers: List[str]) -> bool:
    """A sheet is Q&A-style (one row = one independently retrievable answer,
    e.g. an FAQ import) if it has BOTH a question-like and an answer-like
    column. Everything else is treated as a data table."""
    cols = detect_qa_columns(headers)
    return bool(cols["question"] and cols["answer"])


def workbook_to_summary_pages(workbook_data: Dict, file_name: str = "") -> List[Dict]:
    """Convert extracted workbook to [{page_number, text, sheet_name}] for
    chunking/embedding.

    Two distinct paths, chosen per sheet:

    - Data-table sheets (financial/numeric — the common case) get ONE
      high-level summary page, no row data at all. Embedding raw cells (a
      previous version of this function did — one markdown-table chunk per
      30 rows) is exactly what the deterministic Calculation Engine must
      NOT depend on: vector search over a table dump is unreliable for
      sums/averages/comparisons, and doesn't scale (100k+ rows would
      produce thousands of chunks). That real data lives in excel_rows and
      is queried deterministically by rag/calculator.py.

    - Q&A-style sheets (has both a Question and an Answer column — e.g. an
      FAQ import where each row is its own independently retrievable
      answer, possibly with its own attachments) get ONE page PER ROW,
      containing just that row's question+answer text. This is NOT "raw
      cell embedding" in the sense the calculation engine forbids — each
      row genuinely is a distinct piece of retrievable knowledge, the same
      as a paragraph in a PDF would be. These pages are later matched back
      to knowledge_items rows (see ingestion/embedder.py) so attachments
      can be linked to the exact row, not the whole file.
    """
    pages = []
    page_num = 1

    for sheet in workbook_data["sheets"]:
        if sheet["row_count"] == 0:
            continue

        sheet_name = sheet["sheet_name"]
        headers    = sheet["headers"]
        total_rows = sheet["row_count"]

        if is_qa_sheet(headers):
            qa_cols = detect_qa_columns(headers)
            opt_cols = detect_optional_qa_columns(headers)
            for row in sheet["rows"]:
                rd = row["row_data"]
                question = rd.get(qa_cols["question"])
                answer = rd.get(qa_cols["answer"])
                if not question and not answer:
                    # No page/chunk for this row — stash None so callers
                    # (attachment linking) know there's no chunk to bind to.
                    row["_page_number"] = None
                    continue

                # Real columns win when present; otherwise generate/infer.
                # This never fails ingestion — every helper here is
                # best-effort (see ingestion/smart_enrichment.py).
                real_tags = rd.get(opt_cols["tags"]) if opt_cols["tags"] else None
                real_alt_q = rd.get(opt_cols["alt_questions"]) if opt_cols["alt_questions"] else None
                real_category = rd.get(opt_cols["category"]) if opt_cols["category"] else None
                real_channel = rd.get(opt_cols["channel"]) if opt_cols["channel"] else None

                tags = [t.strip() for t in str(real_tags).split(",") if t.strip()] if real_tags \
                    else generate_tags(str(question or ""), str(answer or ""))
                alt_questions = [a.strip() for a in re.split(r"[\n;|]+", str(real_alt_q)) if a.strip()] \
                    if real_alt_q else generate_alt_questions(str(question or ""))
                category = infer_category(sheet_name=sheet_name, file_name=file_name,
                                           existing_category=real_category)
                language = detect_language(f"{question or ''} {answer or ''}")

                text_parts = [f"Question: {question or ''}", f"Answer: {answer or ''}"]
                if alt_questions:
                    text_parts.append("Alternative phrasings: " + " / ".join(alt_questions))
                if tags:
                    text_parts.append("Tags: " + ", ".join(tags))
                text = "\n".join(text_parts)

                pages.append({
                    "page_number": page_num,
                    "text":        text,
                    "sheet_name":  sheet_name,
                    "is_qa_item":  True,
                    "row_index":   row["row_index"],
                    "question":    question,
                    "answer":      answer,
                })
                row["_generated_tags"] = tags
                row["_generated_alt_questions"] = alt_questions
                row["_generated_category"] = category
                row["_generated_language"] = language
                row["_generated_channel"] = real_channel
                # Single source of truth for "which page/chunk represents
                # this row" — attachment_handler.py and the knowledge_items
                # creation step both read this back instead of recomputing
                # page-number arithmetic themselves (which drifted out of
                # sync with this function once before).
                row["_page_number"] = page_num
                page_num += 1
            continue

        lines = [
            f"## Sheet: {sheet_name}",
            f"Columns ({sheet['column_count']}): {', '.join(str(h) for h in headers)}",
            f"Total rows: {total_rows}",
        ]
        if sheet.get("numeric_columns"):
            lines.append(f"Numeric columns: {', '.join(sheet['numeric_columns'])}")
        if sheet.get("date_columns"):
            lines.append(f"Date columns: {', '.join(sheet['date_columns'])}")
        if sheet.get("is_hidden"):
            lines.append("Note: this sheet is hidden in the original workbook.")
        lines.append(
            "\nThis is a structured data table — for calculations (sum, average, "
            "compare, filter, etc.) use the Excel Calculation Engine against the "
            "underlying structured rows, not this summary."
        )

        pages.append({
            "page_number": page_num,
            "text":        "\n".join(lines),
            "sheet_name":  sheet_name,
        })
        sheet["_page_number"] = page_num
        page_num += 1

    return pages


# Backward-compat alias — old callers/imports still resolve, but everyone
# should move to workbook_to_summary_pages.
workbook_to_pages = workbook_to_summary_pages


# ── Internal helpers ──────────────────────────────────────────────

_CURRENCY_FORMAT_RE = re.compile(r"[$€£¥฿]|USD|THB|EUR|GBP|JPY", re.IGNORECASE)
_PERCENT_FORMAT_RE = re.compile(r"%")


def _classify_cell_format(number_format: str) -> Optional[str]:
    """Return 'currency' | 'percentage' | None from an openpyxl number_format string."""
    if not number_format or number_format == "General":
        return None
    if _PERCENT_FORMAT_RE.search(number_format):
        return "percentage"
    if _CURRENCY_FORMAT_RE.search(number_format):
        return "currency"
    return None


def _extract_sheet(ws, sheet_name: str, sheet_index: int) -> Dict:
    # Collect rows WITH cell objects (not values_only) so we can read each
    # cell's number_format — this is how we tell "0.15" (a plain number)
    # apart from "15%" (percentage) or "$15.00" (currency); openpyxl's
    # values_only mode discards this and both would just look like floats.
    cell_rows = list(ws.iter_rows(values_only=False))
    all_rows = [tuple(c.value for c in row) for row in cell_rows]

    empty_result = {
        "sheet_name":       sheet_name,
        "sheet_index":      sheet_index,
        "row_count":        0,
        "column_count":     0,
        "headers":          [],
        "rows":             [],
        "numeric_columns":  [],
        "date_columns":     [],
        "currency_columns": [],
        "percentage_columns": [],
        "boolean_columns":  [],
        "has_formula":      False,
        "formula_cells":    {},
        "cell_range":       "",
        "markdown_preview": f"## Sheet: {sheet_name}\n\nNo data.\n",
    }

    if not all_rows:
        return empty_result

    # Find first non-empty row as header
    header_row_idx = 0
    for i, row in enumerate(all_rows):
        if any(v is not None for v in row):
            header_row_idx = i
            break

    raw_headers = all_rows[header_row_idx] if all_rows else ()

    # Build header list — use column letter as fallback for blank cells
    headers: List[str] = []
    for col_idx, val in enumerate(raw_headers):
        label = str(val).strip() if val is not None and str(val).strip() else _col_letter(col_idx + 1)
        headers.append(label)

    # Trim trailing columns that are entirely blank in data rows
    data_rows_raw = all_rows[header_row_idx + 1:]
    data_cell_rows = cell_rows[header_row_idx + 1:]
    while headers:
        col = len(headers) - 1
        if all(
            col >= len(row) or row[col] is None
            for row in data_rows_raw
        ):
            headers.pop()
        else:
            break

    if not headers:
        return empty_result

    col_count = len(headers)

    # Extract structured rows
    numeric_cols: set = set()
    date_cols: set = set()
    currency_cols: set = set()
    percentage_cols: set = set()
    boolean_cols: set = set()
    rows: List[Dict] = []

    for r_idx, raw_row in enumerate(data_rows_raw):
        cell_row = data_cell_rows[r_idx] if r_idx < len(data_cell_rows) else ()
        row_data: Dict = {}
        has_data = False

        for c_idx in range(col_count):
            header = headers[c_idx]
            val = raw_row[c_idx] if c_idx < len(raw_row) else None

            if val is None:
                row_data[header] = None
                continue

            has_data = True

            if isinstance(val, bool):
                row_data[header] = val
                boolean_cols.add(header)
            elif isinstance(val, datetime.datetime):
                row_data[header] = val.isoformat()
                date_cols.add(header)
            elif isinstance(val, datetime.date):
                row_data[header] = val.isoformat()
                date_cols.add(header)
            elif isinstance(val, (int, float)):
                row_data[header] = val
                numeric_cols.add(header)
                # Number stays a real number either way — the format tag
                # (currency/percentage) is preserved separately so a
                # calculation can still sum/average it correctly while the
                # UI/answer text can still show "$" or "%" appropriately.
                cell = cell_row[c_idx] if c_idx < len(cell_row) else None
                fmt_kind = _classify_cell_format(getattr(cell, "number_format", None)) if cell is not None else None
                if fmt_kind == "percentage":
                    percentage_cols.add(header)
                elif fmt_kind == "currency":
                    currency_cols.add(header)
            else:
                row_data[header] = str(val).strip()

        if has_data:
            rows.append({
                "row_index": r_idx + 1,   # 1-based (header = row 0)
                "row_data":  row_data,
            })

    if not rows:
        return empty_result

    cell_range = f"A{header_row_idx + 1}:{_col_letter(col_count)}{header_row_idx + 1 + len(rows)}"
    markdown_preview = _sheet_to_markdown(sheet_name, headers, rows)

    return {
        "sheet_name":       sheet_name,
        "sheet_index":      sheet_index,
        "row_count":        len(rows),
        "column_count":     col_count,
        "headers":          headers,
        "rows":             rows,
        "numeric_columns":  sorted(numeric_cols),
        "date_columns":     sorted(date_cols),
        "currency_columns": sorted(currency_cols),
        "percentage_columns": sorted(percentage_cols),
        "boolean_columns":  sorted(boolean_cols),
        "has_formula":      False,   # data_only=True replaces formulas with computed values
        "formula_cells":    {},
        "cell_range":       cell_range,
        "markdown_preview": markdown_preview,
    }


def _sheet_to_markdown(sheet_name: str, headers: List[str], rows: List[Dict]) -> str:
    """Render a sheet as a Markdown table (capped at 200 rows for preview)."""
    lines = [f"## Sheet: {sheet_name}", ""]
    lines.append("| " + " | ".join(str(h) for h in headers) + " |")
    lines.append("| " + " | ".join("---" for _ in headers) + " |")

    for row in rows[:200]:
        cells = [str(row["row_data"].get(h, "")) if row["row_data"].get(h) is not None else "" for h in headers]
        lines.append("| " + " | ".join(cells) + " |")

    if len(rows) > 200:
        lines.append(f"\n_Showing 200 of {len(rows)} rows._")

    lines.append("")
    return "\n".join(lines)
