"""Deterministic calculation engine for Excel-derived structured data.

Numeric/financial questions about uploaded spreadsheets (totals, sums,
averages, comparisons, filters, growth) must NEVER be answered by the LLM
reading a wall of retrieved text and doing mental math — that is unreliable
and un-auditable, and this system may be used with financial reports where
a wrong number is unacceptable. Instead:

  1. An LLM call maps the natural-language question onto a small structured
     query (which sheet, which column(s), which filter, which operation).
     The LLM only ever picks names/values from a catalog we hand it — it
     never sees row data and never computes the actual arithmetic.
  2. The plan is executed as plain Python over rows already stored in
     excel_rows (see migrations/003_excel_structured.sql) — the addition/
     sum/average/etc. is real arithmetic on real numbers, not a guess.
  3. The result carries a citation: workbook filename, sheet name, the
     column(s) used, and exactly which rows contributed.

If no sheet/plan can be confidently identified, or the requested data
doesn't exist, this returns a result with ok=False and a clear reason —
the caller must surface that as "cannot be calculated," never fall back to
guessing a number from RAG.
"""
import json
import statistics
from datetime import datetime
from typing import Dict, List, Optional
from supabase import create_client
from openai import OpenAI

from config import SUPABASE_URL, SUPABASE_KEY, OPENAI_API_KEY, OPENAI_CHAT_MODEL

_supabase = None
_client = None


def _get_supabase():
    global _supabase
    if _supabase is None:
        _supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _supabase


def _get_client():
    global _client
    if _client is None:
        _client = OpenAI(api_key=OPENAI_API_KEY)
    return _client


# sum/avg/median/min/max/count: aggregate target_columns across matched rows.
# distinct: unique values of target_columns[0] (or group_by) across matched rows.
# top_n/bottom_n: N rows with the highest/lowest target_columns[0] value.
# sort: all matched rows sorted by target_columns[0], ascending or descending.
# running_total: cumulative sum of target_columns[0], ordered by sort_by (or date_column).
# growth/yoy/mom: period-over-period % change of target_columns[0], grouped by date_column.
# percentage: what fraction (as %) the filtered subset's sum is of the whole sheet's sum.
VALID_OPERATIONS = {
    "sum", "avg", "median", "min", "max", "count", "distinct",
    "top_n", "bottom_n", "sort", "running_total", "growth", "yoy", "mom", "percentage",
}


# ── Step 1: catalog of what's available ─────────────────────────

def _load_sheet_catalog() -> List[Dict]:
    """One entry per sheet with enough metadata for the LLM to pick a target
    without ever seeing the actual row data (so it can't "guess" from it)."""
    sb = _get_supabase()
    try:
        sheets_res = sb.table("excel_sheets").select(
            "id,workbook_id,sheet_name,headers,numeric_columns,date_columns,"
            "currency_columns,percentage_columns,is_hidden,row_count"
        ).gt("row_count", 0).execute()
        sheets = sheets_res.data or []
    except Exception as e:
        print(f"[calculator] catalog load failed: {e}")
        return []

    if not sheets:
        return []

    workbook_ids = list({s["workbook_id"] for s in sheets if s.get("workbook_id")})
    filenames = {}
    if workbook_ids:
        try:
            wb_res = sb.table("excel_workbooks").select("id,filename") \
                .filter("id", "in", "(" + ",".join(workbook_ids) + ")").execute()
            filenames = {w["id"]: w["filename"] for w in (wb_res.data or [])}
        except Exception as e:
            print(f"[calculator] workbook filename lookup failed: {e}")

    catalog = []
    for s in sheets:
        catalog.append({
            "sheet_id": s["id"],
            "workbook_filename": filenames.get(s.get("workbook_id"), "unknown"),
            "sheet_name": s["sheet_name"],
            "headers": s.get("headers") or [],
            "numeric_columns": s.get("numeric_columns") or [],
            "date_columns": s.get("date_columns") or [],
            "currency_columns": s.get("currency_columns") or [],
            "percentage_columns": s.get("percentage_columns") or [],
            "is_hidden": s.get("is_hidden") or False,
            "row_count": s.get("row_count") or 0,
        })
    return catalog


# ── Step 2: LLM plans the query (never computes) ────────────────

_PLAN_SYSTEM_PROMPT = """You turn a numeric/financial question about spreadsheet data into a JSON query plan.
You NEVER compute an answer yourself — you only select a sheet and column/filter names from the catalog given to you.

Return JSON with this exact shape:
{
  "sheet_id": "<id from catalog, or null if no sheet fits>",
  "operation": "sum" | "avg" | "median" | "min" | "max" | "count" | "distinct" | "top_n" | "bottom_n" | "sort" | "running_total" | "growth" | "yoy" | "mom" | "percentage",
  "target_columns": ["<column name(s) to read numeric values from>"],
  "filters": [
    {"column": "<column name>", "values": ["<value1>", "<value2>"]},
    {"column": "<date column>", "date_from": "<YYYY-MM-DD>", "date_to": "<YYYY-MM-DD>"},
    {"column": "<date column>", "month": <1-12>},
    {"column": "<date column>", "year": <YYYY>}
  ],
  "group_by": "<column name, or null>",
  "date_column": "<column name for growth/yoy/mom/running_total/sort ordering, or null>",
  "n": <integer, only for top_n/bottom_n, default 5>,
  "sort_order": "asc" | "desc"
}

Rules:
- "target_columns" can have MULTIPLE entries when the question asks to combine
  several named columns (e.g. a wide-format sheet with one column per month:
  question "month 5 + month 8" -> target_columns: ["Month 5", "Month 8"] if
  those are literal column names in the catalog).
- Use "filters" instead when the sheet is long-format (a single value column
  plus a category/month/status column to filter by), e.g. a "Month" column
  with row values 1-12, or a "Status" column. Use the date_from/date_to or
  month/year filter shapes for date-range/month/year questions instead of
  the values list.
- "group_by" is set for comparison/breakdown questions ("compare X by Y",
  "highest SKU revenue" -> group_by the SKU/product column) and for
  "distinct" (list unique values of group_by).
- "growth"/"yoy"/"mom" need "date_column" (a date or period column) plus one
  target_column (the value to compute period-over-period change on).
- "top_n"/"bottom_n"/"sort" need one target_column and optionally group_by
  (top N groups) or n rows directly. "sort" returns ALL matched rows in
  order (use sort_order asc/desc); top_n/bottom_n cap at n.
- "percentage" computes what % the filtered subset's sum is of the whole
  sheet's total for the same target_column — needs filters plus one
  target_column.
- If nothing in the catalog plausibly answers the question, return
  {"sheet_id": null, "operation": "sum", "target_columns": [], "filters": [], "group_by": null, "date_column": null, "n": 5, "sort_order": "desc"}.
- Only ever use column names and sheet_ids that literally appear in the catalog."""


def _plan_calculation(question: str, catalog: List[Dict]) -> Optional[Dict]:
    if not catalog:
        return None
    catalog_text = json.dumps(catalog, ensure_ascii=False, indent=2)
    try:
        resp = _get_client().chat.completions.create(
            model=OPENAI_CHAT_MODEL,
            response_format={"type": "json_object"},
            temperature=0,
            messages=[
                {"role": "system", "content": _PLAN_SYSTEM_PROMPT},
                {"role": "user", "content": f"Catalog:\n{catalog_text}\n\nQuestion: {question}"},
            ],
        )
        plan = json.loads(resp.choices[0].message.content)
    except Exception as e:
        print(f"[calculator] plan LLM call failed: {e}")
        return None

    if not plan.get("sheet_id"):
        return None
    if plan.get("operation") not in VALID_OPERATIONS:
        plan["operation"] = "sum"
    plan.setdefault("target_columns", [])
    plan.setdefault("filters", [])
    plan.setdefault("group_by", None)
    plan.setdefault("date_column", None)
    plan.setdefault("n", 5)
    plan.setdefault("sort_order", "desc")
    return plan


# ── Step 3: deterministic execution — real arithmetic, no LLM ───

def _coerce_number(val) -> Optional[float]:
    """Never estimate — either this parses to a real number, or it's None."""
    if val is None:
        return None
    if isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        return float(val)
    try:
        cleaned = str(val).replace(",", "").replace("฿", "").replace("$", "").strip()
        cleaned = cleaned.rstrip("%")
        return float(cleaned)
    except (TypeError, ValueError):
        return None


def _coerce_date(val) -> Optional[datetime]:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val
    s = str(val).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%Y-%m", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s[:len(fmt.replace('%', ''))+len(fmt)], fmt)
        except (ValueError, TypeError):
            continue
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def _row_matches_filters(row_data: Dict, filters: List[Dict]) -> bool:
    for f in filters:
        col = f.get("column")
        if not col:
            continue
        actual = row_data.get(col)

        # Date-range filter: {"column": "...", "date_from": "...", "date_to": "..."}
        if f.get("date_from") or f.get("date_to"):
            dt = _coerce_date(actual)
            if dt is None:
                return False
            lo = _coerce_date(f.get("date_from")) if f.get("date_from") else None
            hi = _coerce_date(f.get("date_to")) if f.get("date_to") else None
            if lo and dt < lo:
                return False
            if hi and dt > hi:
                return False
            continue

        # Month/year filter on a date column: {"column": "...", "month": 5} / {"year": 2026}
        if f.get("month") is not None or f.get("year") is not None:
            dt = _coerce_date(actual)
            if dt is None:
                return False
            if f.get("month") is not None and dt.month != int(f["month"]):
                return False
            if f.get("year") is not None and dt.year != int(f["year"]):
                return False
            continue

        wanted = [str(v).strip().lower() for v in (f.get("values") or [])]
        if not wanted:
            continue
        if actual is None:
            return False
        if str(actual).strip().lower() not in wanted:
            actual_num = _coerce_number(actual)
            if actual_num is None or not any(_coerce_number(w) == actual_num for w in wanted):
                return False
    return True


def _aggregate(values: List[float], operation: str) -> Optional[float]:
    if not values:
        return None
    if operation == "sum":
        return sum(values)
    if operation == "avg":
        return sum(values) / len(values)
    if operation == "median":
        return statistics.median(values)
    if operation == "min":
        return min(values)
    if operation == "max":
        return max(values)
    if operation == "count":
        return float(len(values))
    return None


def _values_for_row(row_data: Dict, target_columns: List[str], numeric_columns: List[str]) -> List[float]:
    if target_columns:
        return [v for v in (_coerce_number(row_data.get(c)) for c in target_columns) if v is not None]
    return [v for v in (_coerce_number(row_data.get(c)) for c in numeric_columns) if v is not None]


def _period_key(dt: Optional[datetime], period: str) -> Optional[str]:
    if dt is None:
        return None
    return f"{dt.year}" if period == "year" else f"{dt.year}-{dt.month:02d}"


def _execute_plan(plan: Dict, catalog: List[Dict]) -> Optional[Dict]:
    sheet_meta = next((s for s in catalog if s["sheet_id"] == plan["sheet_id"]), None)
    if not sheet_meta:
        return {"ok": False, "reason": "No matching sheet found in the catalog."}

    sb = _get_supabase()
    try:
        rows_res = sb.table("excel_rows").select("row_index,row_data") \
            .eq("sheet_id", plan["sheet_id"]).order("row_index").execute()
        rows = rows_res.data or []
    except Exception as e:
        print(f"[calculator] row fetch failed: {e}")
        return {"ok": False, "reason": f"Could not read structured data: {e}"}
    if not rows:
        return {"ok": False, "reason": "Sheet has no structured rows stored."}

    op = plan["operation"]
    target_columns = plan.get("target_columns") or []
    filters = plan.get("filters") or []
    group_by = plan.get("group_by")
    date_column = plan.get("date_column")
    n = int(plan.get("n") or 5)
    sort_order = plan.get("sort_order") or "desc"

    matched_rows = [r for r in rows if _row_matches_filters(r["row_data"], filters)]
    if not matched_rows:
        return {"ok": False, "reason": "No rows matched the requested filter — cannot calculate."}

    used_cols = target_columns or sheet_meta["numeric_columns"]
    # Preserve the original unit: if every target column was formatted as
    # currency/percentage in the source sheet, tag the result so the answer
    # text shows "$"/"%" instead of a bare number that silently drops units.
    unit = None
    if used_cols and all(c in (sheet_meta.get("percentage_columns") or []) for c in used_cols):
        unit = "percentage"
    elif used_cols and all(c in (sheet_meta.get("currency_columns") or []) for c in used_cols):
        unit = "currency"

    citation_base = {
        "workbook": sheet_meta["workbook_filename"],
        "sheet": sheet_meta["sheet_name"],
        "columns_used": used_cols,
        "filters": filters,
        "unit": unit,
    }

    # ── distinct ──────────────────────────────────────────────
    if op == "distinct":
        col = group_by or (target_columns[0] if target_columns else None)
        if not col:
            return {"ok": False, "reason": "No column specified for distinct values."}
        seen = sorted({str(r["row_data"].get(col)) for r in matched_rows if r["row_data"].get(col) is not None})
        return {"ok": True, "operation": op, "distinct_values": seen,
                "rows_used": [r["row_index"] for r in matched_rows], **citation_base}

    # ── percentage: filtered subset's sum as a % of the whole sheet's sum ──
    if op == "percentage":
        col = target_columns[0] if target_columns else None
        if not col:
            return {"ok": False, "reason": "No target column specified for percentage."}
        subset_values = [v for r in matched_rows for v in [_coerce_number(r["row_data"].get(col))] if v is not None]
        total_values = [v for r in rows for v in [_coerce_number(r["row_data"].get(col))] if v is not None]
        if not subset_values or not total_values:
            return {"ok": False, "reason": f"Column {col!r} had no numeric values to compute a percentage."}
        subset_sum, total_sum = sum(subset_values), sum(total_values)
        if total_sum == 0:
            return {"ok": False, "reason": f"Total for column {col!r} is zero — percentage is undefined."}
        pct = subset_sum / total_sum * 100
        return {"ok": True, "operation": op, "subset_sum": subset_sum, "total_sum": total_sum, "pct": pct,
                "rows_used": [r["row_index"] for r in matched_rows], **citation_base}

    # ── sort: ALL matched rows, ordered (not aggregated) ──────
    if op == "sort":
        col = target_columns[0] if target_columns else date_column
        if not col:
            return {"ok": False, "reason": "No column specified to sort by."}
        scored = [(r["row_index"], r["row_data"].get(col)) for r in matched_rows]
        def _sort_key(item):
            v = _coerce_number(item[1])
            if v is not None:
                return (0, v)
            dt = _coerce_date(item[1])
            if dt is not None:
                return (0, dt)
            return (1, str(item[1] or ""))
        scored.sort(key=_sort_key, reverse=(sort_order == "desc"))
        return {"ok": True, "operation": op, "sorted_rows": [{"row": i, "value": v} for i, v in scored],
                "rows_used": [i for i, _ in scored], **citation_base}

    # ── top_n / bottom_n ──────────────────────────────────────
    if op in ("top_n", "bottom_n"):
        col = target_columns[0] if target_columns else None
        if not col:
            return {"ok": False, "reason": "No target column specified for ranking."}
        if group_by:
            groups: Dict[str, float] = {}
            group_rows: Dict[str, List[int]] = {}
            for r in matched_rows:
                key = str(r["row_data"].get(group_by, "—"))
                v = _coerce_number(r["row_data"].get(col))
                if v is None:
                    continue
                groups[key] = groups.get(key, 0.0) + v
                group_rows.setdefault(key, []).append(r["row_index"])
            if not groups:
                return {"ok": False, "reason": f"Column {col!r} had no numeric values to rank."}
            ranked = sorted(groups.items(), key=lambda kv: kv[1], reverse=(op == "top_n"))[:n]
            return {"ok": True, "operation": op, "group_by": group_by,
                    "ranked": [{"group": k, "value": v} for k, v in ranked],
                    "rows_used": sorted({i for k, _ in ranked for i in group_rows[k]}),
                    **citation_base}
        else:
            scored = [(r["row_index"], _coerce_number(r["row_data"].get(col))) for r in matched_rows]
            scored = [(idx, v) for idx, v in scored if v is not None]
            if not scored:
                return {"ok": False, "reason": f"Column {col!r} had no numeric values to rank."}
            scored.sort(key=lambda t: t[1], reverse=(op == "top_n"))
            top = scored[:n]
            return {"ok": True, "operation": op, "ranked": [{"row": i, "value": v} for i, v in top],
                    "rows_used": [i for i, _ in top], **citation_base}

    # ── running_total ─────────────────────────────────────────
    if op == "running_total":
        col = target_columns[0] if target_columns else None
        if not col:
            return {"ok": False, "reason": "No target column specified for running total."}
        ordered = matched_rows
        if date_column:
            ordered = sorted(matched_rows, key=lambda r: _coerce_date(r["row_data"].get(date_column)) or datetime.min)
        running, series = 0.0, []
        for r in ordered:
            v = _coerce_number(r["row_data"].get(col))
            if v is None:
                continue
            running += v
            label = str(r["row_data"].get(date_column)) if date_column else str(r["row_index"])
            series.append({"label": label, "value": v, "running_total": running})
        if not series:
            return {"ok": False, "reason": f"Column {col!r} had no numeric values."}
        return {"ok": True, "operation": op, "series": series, "final_total": running,
                "rows_used": [r["row_index"] for r in ordered], **citation_base}

    # ── growth / yoy / mom ────────────────────────────────────
    if op in ("growth", "yoy", "mom"):
        col = target_columns[0] if target_columns else None
        if not col or not date_column:
            return {"ok": False, "reason": "Growth calculations need both a value column and a date column."}
        period = "year" if op == "yoy" else "month"
        buckets: Dict[str, List[float]] = {}
        bucket_rows: Dict[str, List[int]] = {}
        for r in matched_rows:
            dt = _coerce_date(r["row_data"].get(date_column))
            v = _coerce_number(r["row_data"].get(col))
            key = _period_key(dt, period)
            if key is None or v is None:
                continue
            buckets.setdefault(key, []).append(v)
            bucket_rows.setdefault(key, []).append(r["row_index"])
        if len(buckets) < 2:
            return {"ok": False, "reason": "Not enough distinct periods with valid data to compute growth."}
        periods = sorted(buckets.keys())
        totals = {k: sum(buckets[k]) for k in periods}
        growth_series = []
        for i in range(1, len(periods)):
            prev, cur = periods[i - 1], periods[i]
            prev_v, cur_v = totals[prev], totals[cur]
            pct = None if prev_v == 0 else (cur_v - prev_v) / prev_v * 100
            growth_series.append({"from": prev, "to": cur, "from_value": prev_v, "to_value": cur_v, "pct_change": pct})
        return {"ok": True, "operation": op, "totals_by_period": totals, "growth_series": growth_series,
                "rows_used": sorted({i for v in bucket_rows.values() for i in v}), **citation_base}

    # ── sum / avg / median / min / max / count (with optional group_by) ──
    if group_by:
        groups: Dict[str, List[float]] = {}
        group_row_indexes: Dict[str, List[int]] = {}
        for r in matched_rows:
            key = str(r["row_data"].get(group_by, "—"))
            groups.setdefault(key, []).extend(_values_for_row(r["row_data"], target_columns, sheet_meta["numeric_columns"]))
            group_row_indexes.setdefault(key, []).append(r["row_index"])
        breakdown = {k: _aggregate(v, op) for k, v in groups.items() if v}
        if not breakdown:
            return {"ok": False, "reason": "No numeric values found to group and aggregate."}
        best_key = max(breakdown, key=breakdown.get) if op in ("sum", "avg", "max", "median") else min(breakdown, key=breakdown.get)
        return {
            "ok": True, "operation": op, "group_by": group_by, "breakdown": breakdown,
            "top_group": best_key, "top_value": breakdown[best_key],
            "rows_used": sorted({idx for idxs in group_row_indexes.values() for idx in idxs}),
            **citation_base,
        }

    values: List[float] = []
    rows_used: List[int] = []
    for r in matched_rows:
        vs = _values_for_row(r["row_data"], target_columns, sheet_meta["numeric_columns"])
        if vs:
            values.extend(vs)
            rows_used.append(r["row_index"])

    result = _aggregate(values, op)
    if result is None:
        return {"ok": False, "reason": f"No numeric values found in column(s) {citation_base['columns_used']}."}

    return {"ok": True, "operation": op, "result": result, "values_used": values,
            "rows_used": rows_used, **citation_base}


# ── Public entry point ───────────────────────────────────────────

def answer_calculation_question(question: str) -> Optional[Dict]:
    """Try to answer a numeric/financial question deterministically from
    structured Excel data.

    Returns None only when no sheet/plan could even be identified (caller
    should fall back to plain RAG search in that case). If a sheet WAS
    identified but the calculation can't be completed (missing data, no
    matching rows), returns a dict with ok=False and a human-readable
    "reason" — the caller must surface that as "cannot be calculated,"
    never silently fall back to a vector-search guess.
    """
    catalog = _load_sheet_catalog()
    if not catalog:
        return None
    plan = _plan_calculation(question, catalog)
    if not plan:
        return None
    result = _execute_plan(plan, catalog)
    if result is None:
        return None

    if not result.get("ok"):
        result["text"] = f"CANNOT CALCULATE: {result.get('reason', 'Unknown reason.')}"
        result["citation"] = f"Attempted against: {result.get('workbook','')} — sheet {result.get('sheet','')}"
        return result

    result["text"] = _format_result_text(result)
    result["citation"] = _format_citation(result)
    return result


def _fmt_num(v: float, unit: Optional[str] = None) -> str:
    """Preserve the original unit in the rendered number — never drop it,
    since "15" and "15%" and "$15" are different facts."""
    if unit == "percentage":
        return f"{v * 100:,.2f}%" if abs(v) <= 1 else f"{v:,.2f}%"
    if unit == "currency":
        return f"{v:,.2f}"  # currency symbol itself varies per-locale; amount precision preserved exactly
    return f"{v:,.2f}"


def _format_result_text(result: Dict) -> str:
    op = result["operation"]
    unit = result.get("unit")
    fmt = lambda v: _fmt_num(v, unit)
    if "pct" in result:
        return (f"CALCULATED RESULT (percentage): {fmt(result['subset_sum'])} / {fmt(result['total_sum'])} "
                f"= {result['pct']:.2f}%")
    if "sorted_rows" in result:
        lines = [f"- row {r['row']}: {r['value']}" for r in result["sorted_rows"][:50]]
        more = f"\n(+{len(result['sorted_rows']) - 50} more rows)" if len(result["sorted_rows"]) > 50 else ""
        return "CALCULATED RESULT (sorted rows):\n" + "\n".join(lines) + more
    if "breakdown" in result:
        lines = [f"- {k}: {fmt(v)}" for k, v in result["breakdown"].items()]
        return (f"CALCULATED RESULT (operation={op}, grouped by {result['group_by']}):\n"
                + "\n".join(lines) + f"\nHighest: {result['top_group']} = {fmt(result['top_value'])}")
    if "distinct_values" in result:
        return "CALCULATED RESULT (distinct values): " + ", ".join(result["distinct_values"])
    if "ranked" in result:
        label = "group" if "group" in (result["ranked"][0] if result["ranked"] else {}) else "row"
        lines = [f"- {r.get('group', r.get('row'))}: {fmt(r['value'])}" for r in result["ranked"]]
        return f"CALCULATED RESULT ({op} by {label}):\n" + "\n".join(lines)
    if "series" in result:
        lines = [f"- {s['label']}: {fmt(s['value'])} (running total: {fmt(s['running_total'])})" for s in result["series"]]
        return "CALCULATED RESULT (running total):\n" + "\n".join(lines) + f"\nFinal total: {fmt(result['final_total'])}"
    if "growth_series" in result:
        lines = []
        for g in result["growth_series"]:
            pct_str = f"{g['pct_change']:+.2f}%" if g["pct_change"] is not None else "N/A (previous period was zero)"
            lines.append(f"- {g['from']} → {g['to']}: {fmt(g['from_value'])} → {fmt(g['to_value'])} ({pct_str})")
        return f"CALCULATED RESULT ({result['operation']} growth):\n" + "\n".join(lines)
    return f"CALCULATED RESULT: {result['operation']}({', '.join(result['columns_used'])}) = {fmt(result['result'])}"


def _format_citation(result: Dict) -> str:
    rows = result.get("rows_used") or []
    citation = (
        f"Source: {result['workbook']} — sheet \"{result['sheet']}\", "
        f"column(s) {', '.join(result['columns_used'])}, "
        f"rows {', '.join(str(i) for i in rows[:20])}"
        + (f" (+{len(rows) - 20} more)" if len(rows) > 20 else "")
    )
    if result.get("filters"):
        citation += f", filtered by {result['filters']}"
    return citation
