"""Convert any document to Markdown text using MarkItDown (Microsoft).

Supports: PDF, DOCX, PPTX, HTML, CSV, TXT, and more.
Excel (.xlsx/.xls) is handled separately by excel_extractor.py for structured data.
"""
from pathlib import Path

_converter = None


def _get_converter():
    global _converter
    if _converter is None:
        from markitdown import MarkItDown
        _converter = MarkItDown()
    return _converter


def convert_to_markdown(file_path: Path) -> str:
    """Convert a document to clean Markdown text.

    Returns empty string on failure (caller should fall back to original reader).
    """
    try:
        result = _get_converter().convert(str(file_path))
        text = (result.text_content or "").strip()
        return text
    except Exception as e:
        print(f"[markdown_converter] {file_path.name}: {e}")
        return ""
