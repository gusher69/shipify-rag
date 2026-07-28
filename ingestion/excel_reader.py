from pathlib import Path
from typing import List, Dict
import pandas as pd


def read_excel_pages(file_path: str) -> List[Dict]:
    """อ่าน Excel แล้วคืน [{page_number, text}] โดยแต่ละ sheet = 1 page"""
    pages = []
    try:
        wb = pd.read_excel(file_path, sheet_name=None, dtype=str)
    except Exception as e:
        print(f"⚠️ อ่าน Excel ไม่ได้: {e}")
        return []

    for sheet_num, (sheet_name, df) in enumerate(wb.items(), start=1):
        df = df.dropna(how="all").fillna("")
        if df.empty:
            continue

        lines = [f"# {sheet_name}"]
        for _, row in df.iterrows():
            values = [str(v).strip() for v in row.values if str(v).strip()]
            if values:
                lines.append(" | ".join(values))

        text = "\n".join(lines)
        if text.strip():
            pages.append({"page_number": sheet_num, "text": text})

    return pages
