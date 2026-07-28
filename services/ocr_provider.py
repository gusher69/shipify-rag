"""OCR provider abstraction. Local/free (Tesseract via pytesseract) when
available; degrades gracefully (never crashes ingestion) when it isn't —
the page simply falls through to Vision instead (see
pdf_page_pipeline.py), which is the actual OCR/structure fallback used in
this deployment today since Tesseract is not installed in this
environment. Kept as a real, separate abstraction so a future deployment
that DOES have Tesseract (or another OCR engine) gets it for free,
without any ingestion-pipeline code change.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class OCRResult:
    text: str
    confidence: float          # 0..1, averaged across recognized words/lines
    engine: str
    available: bool
    warnings: List[str]


class OCRProvider(ABC):
    @abstractmethod
    def recognize(self, image_bytes: bytes, *, languages: Optional[List[str]] = None) -> OCRResult:
        ...


class NullOCRProvider(OCRProvider):
    """No OCR engine available/enabled — returns an empty, clearly-marked
    result so callers can fall back to Vision instead of crashing."""
    def recognize(self, image_bytes: bytes, *, languages: Optional[List[str]] = None) -> OCRResult:
        return OCRResult(text="", confidence=0.0, engine="none", available=False,
                          warnings=["OCR is not available in this environment (no engine configured)."])


class TesseractOCRProvider(OCRProvider):
    """Local, free OCR via pytesseract + the Tesseract binary. Supports
    Thai + English out of the box via the 'tha+eng' language pack. Raises
    a clear, caught-by-the-caller exception if pytesseract/the binary
    isn't installed — this class never silently pretends to work."""

    def __init__(self, lang: str = "tha+eng"):
        self._lang = lang
        try:
            import pytesseract  # noqa: F401
        except ImportError as e:
            raise RuntimeError(
                "TesseractOCRProvider requires the 'pytesseract' package and the Tesseract binary "
                "(with the Thai language pack) to be installed. Falls back to Vision if unavailable."
            ) from e

    def recognize(self, image_bytes: bytes, *, languages: Optional[List[str]] = None) -> OCRResult:
        import io
        import pytesseract
        from PIL import Image

        img = Image.open(io.BytesIO(image_bytes))
        lang = "+".join(languages) if languages else self._lang
        warnings: List[str] = []
        try:
            data = pytesseract.image_to_data(img, lang=lang, output_type=pytesseract.Output.DICT)
            words = [w for w in data.get("text", []) if w and w.strip()]
            confidences = [float(c) for c in data.get("conf", []) if c not in ("-1", -1)]
            text = " ".join(words)
            avg_conf = (sum(confidences) / len(confidences) / 100.0) if confidences else 0.0
        except Exception as e:
            warnings.append(f"Tesseract OCR failed: {e}")
            text, avg_conf = "", 0.0

        return OCRResult(text=text, confidence=round(avg_conf, 3), engine="tesseract",
                          available=True, warnings=warnings)


def get_ocr_provider() -> OCRProvider:
    """Auto-detects Tesseract; falls back to NullOCRProvider (never
    raises) so callers can always call get_ocr_provider().recognize(...)
    safely and check `.available` instead of catching exceptions."""
    try:
        return TesseractOCRProvider()
    except RuntimeError:
        return NullOCRProvider()
