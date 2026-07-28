"""TextQualityEvaluator — reusable, dependency-free quality scoring for
extracted text (any source: PDF native extraction, OCR, etc). Used to
decide whether a page's native text is usable as-is or needs OCR/Vision.

Detects the concrete failure modes reported in production: custom
embedded-font subsetting maps glyphs to Private-Use-Area Unicode code
points (U+E000-U+F8FF) or literal "(cid:123)" placeholders that
pdfplumber/PyMuPDF emit when a font's ToUnicode CMap is missing/broken —
the text LOOKS non-empty but is actually unreadable garbage.
"""
import re
from dataclasses import dataclass, asdict
from typing import Optional

_CID_PATTERN = re.compile(r"\(cid:\d+\)")
_REPLACEMENT_CHAR = "�"
# Private Use Area — where broken/subsetted embedded fonts without a
# proper ToUnicode CMap frequently get mapped to by PDF extractors.
_PUA_PATTERN = re.compile(u"[-]")
_THAI_RANGE = re.compile(r"[฀-๿]")
_ALPHA_RANGE = re.compile(r"[^\W\d_]", re.UNICODE)
_REPEATED_SYMBOL = re.compile(r"([^\s])\1{6,}")  # same NON-whitespace char 7+ times in a row


@dataclass
class TextQualityResult:
    quality_score: float          # 0.0 (unusable) .. 1.0 (clean)
    extraction_status: str        # good | sparse | corrupted | empty
    reason: str
    requires_ocr: bool
    requires_vision: bool

    def as_dict(self) -> dict:
        return asdict(self)


class TextQualityEvaluator:
    """Stateless — safe to reuse/call from anywhere. Every threshold is a
    named class constant so tuning doesn't require touching the logic."""

    MIN_CHARS_FOR_A_REAL_PAGE = 40
    CID_RATIO_CORRUPTED = 0.02        # >2% of chars inside "(cid:N)" markers
    PUA_RATIO_CORRUPTED = 0.05        # >5% of chars are Private-Use-Area glyphs
    REPLACEMENT_RATIO_CORRUPTED = 0.03
    MIN_ALPHA_RATIO = 0.35            # below this, text is "mostly coordinates/symbols"
    SPARSE_CHAR_THRESHOLD = 120       # fewer real chars than this on an otherwise-full page = sparse

    def evaluate(self, text: Optional[str], *, expected_thai: bool = False) -> TextQualityResult:
        text = text or ""
        length = len(text)

        if length == 0:
            return TextQualityResult(0.0, "empty", "No text extracted from this page.", True, True)

        cid_matches = _CID_PATTERN.findall(text)
        cid_chars = sum(len(m) for m in cid_matches)
        pua_chars = len(_PUA_PATTERN.findall(text))
        replacement_chars = text.count(_REPLACEMENT_CHAR)
        alpha_chars = len(_ALPHA_RANGE.findall(text))
        thai_chars = len(_THAI_RANGE.findall(text))
        repeated_garbage = bool(_REPEATED_SYMBOL.search(text))

        cid_ratio = cid_chars / length
        pua_ratio = pua_chars / length
        replacement_ratio = replacement_chars / length
        alpha_ratio = alpha_chars / length

        # ── Hard corruption signals — any one of these alone means the
        # text is not usable, regardless of how much of it there is. ──
        if cid_ratio > self.CID_RATIO_CORRUPTED:
            return TextQualityResult(round(max(0.0, 1 - cid_ratio * 5), 3), "corrupted",
                                      "Excessive (cid:N) placeholders — embedded font ToUnicode "
                                      "mapping is missing or broken.", True, True)
        if pua_ratio > self.PUA_RATIO_CORRUPTED:
            return TextQualityResult(round(max(0.0, 1 - pua_ratio * 3), 3), "corrupted",
                                      "Text maps to Private-Use-Area Unicode code points — "
                                      "embedded font mapping failure (subsetted font, no CMap).",
                                      True, True)
        if replacement_ratio > self.REPLACEMENT_RATIO_CORRUPTED:
            return TextQualityResult(round(max(0.0, 1 - replacement_ratio * 5), 3), "corrupted",
                                      "Excessive Unicode replacement characters (U+FFFD) — "
                                      "unreadable/undecodable byte sequences.", True, True)
        if repeated_garbage:
            return TextQualityResult(0.15, "corrupted",
                                      "Long runs of a single repeated symbol — likely an extraction "
                                      "artifact rather than real content.", True, True)
        digit_ratio = sum(1 for c in text if c.isdigit()) / length
        # A low alpha ratio alone doesn't mean corruption — a legitimate
        # benefit/pricing table is legitimately numeric-heavy. Only flag
        # "mostly coordinates/symbols/encoding artifacts" when BOTH
        # letters AND digits are scarce (i.e. the page is neither prose
        # nor a real numeric table — just noise).
        if alpha_ratio < self.MIN_ALPHA_RATIO and digit_ratio < 0.15 and length > self.MIN_CHARS_FOR_A_REAL_PAGE:
            return TextQualityResult(round(alpha_ratio, 3), "corrupted",
                                      f"Very low alphabetic character ratio ({alpha_ratio:.0%}) and low "
                                      f"digit content — text is mostly symbols or encoding artifacts, "
                                      f"not prose or a numeric table.", True, True)

        # ── Sparse: real (readable) text, but not much of it — likely a
        # scanned/image page where only a caption or page number extracted
        # natively. ──
        if length < self.SPARSE_CHAR_THRESHOLD:
            return TextQualityResult(round(min(0.6, length / self.SPARSE_CHAR_THRESHOLD), 3), "sparse",
                                      f"Only {length} character(s) of readable text extracted — "
                                      f"likely a scanned or image-heavy page.", True, False)

        # ── Broken-Thai heuristic: if the page is expected to contain Thai
        # (caller can pass a hint, e.g. "most other pages in this doc are
        # Thai") but almost none decoded, that's a red flag even though
        # raw alpha_ratio might look fine (numbers/Latin can dominate). ──
        if expected_thai and thai_chars == 0 and length > self.MIN_CHARS_FOR_A_REAL_PAGE:
            return TextQualityResult(0.3, "sparse",
                                      "Document is otherwise Thai-language but no Thai characters "
                                      "decoded on this page — possible font-mapping failure.",
                                      True, True)

        score = min(1.0, alpha_ratio + 0.3)
        return TextQualityResult(round(score, 3), "good", "Text extraction looks clean.", False, False)
