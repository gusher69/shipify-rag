# -*- coding: utf-8 -*-
"""PHASE-6D — safe Thai chat-typo normalization for INTENT / SEMANTIC /
RAG-QUERY interpretation only.

The raw customer text stays authoritative for everything that must never
be mutated: audit/logs, ERP values, URLs, bill / order / shipment /
tracking ids, CustCode, phone numbers, e-mail, amounts, dimensions,
weights, dates, percentages, customer-entered names. This module returns
BOTH strings — `raw` (verbatim) and `normalized` (typo-tolerant) — and a
list of the structured tokens it froze.

Design (reuse > extend > minimal modify > new):

  1. light clean  — Unicode NFC, whitespace collapse, BOUNDARY-ANCHORED
     3+ repeated chat characters trimmed, repeated `!`/`?` trimmed. No
     mid-word de-spacing (that corrupts multi-word slot values like a
     receiver name). Outside frozen spans only.
  2. reuse `rag.spell_correction`'s DIRECT 1:1 corrections — the tiny
     admin-curated registry ("CBเอ็ม" -> "CBM"), confidence 1.0.
  3. a tiny curated map (`_CHAT_FORMS` / `_UNIT_FORMS`) — every entry is
     a 1:1 rewrite whose left side is a NON-WORD ("ลิ้ง" -> "ลิงก์",
     "คำนวน" -> "คำนวณ", "กโล" -> "กิโล").
  4. a MINIMAL fuzzy repair whitelist (`_FUZZY_WHITELIST`) — only long
     (>=6 char) distinctive routing NOUNS with no close real-word
     neighbour, reusing `rag.spell_correction._find_fuzzy_spans`.

No general edit-distance correction runs here. Intent routing exists to
tell near-homophone verbs apart ("โอนเงิน" transfer vs "ถอนเงิน"
withdrawal, "เข้า" vs "นำเข้า"); a 1-edit fuzzy pass destroys exactly that
distinction and the Semantic Invariant Guard's entity/negation snapshot
does not catch a verb swapped for its near neighbour. The RAG pipeline
keeps its own full fuzzy layer on the retrieval query, where a loose
match only widens recall.

If the Semantic Invariant Guard (`rag.semantic_guard`) rejects the
combined result — an introduced/removed entity, a flipped negation, a
polite particle eaten into a business word — `normalized` falls back to
`raw` unchanged. Never a silent guess.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

from rag.spell_correction import _protected_spans as _sc_protected_spans

# ── structured tokens frozen before any normalization ────────────────────
# `rag.spell_correction._protected_spans` already covers URLs, e-mails,
# Thai phone numbers, `[A-Za-z]{1,4}\d{4,}` codes, bare numbers/prices and
# the known-abbreviation set. These add the shapes that matter for the
# calculator / identifier flows but are not single "codes": a dimension
# triple, a weight with unit, a percentage, an ISO-ish date, and a short
# brand-prefixed id (`SP1008`, `FT1325`) whose digit run is < 4.
_EXTRA_PROTECTED_PATTERNS = [
    re.compile(r"\d+(?:\.\d+)?\s*(?:mm|cm|m|มม\.?|ซม\.?|ม\.?|นิ้ว|in(?:ch)?)?\s*[x×*]\s*"
               r"\d+(?:\.\d+)?\s*(?:mm|cm|m|มม\.?|ซม\.?|ม\.?|นิ้ว|in(?:ch)?)?"
               r"(?:\s*[x×*]\s*\d+(?:\.\d+)?\s*(?:mm|cm|m|มม\.?|ซม\.?|ม\.?|นิ้ว|in(?:ch)?)?)?",
               re.IGNORECASE),
    re.compile(r"\d+(?:\.\d+)?\s*(?:กก\.?|กิโล(?:กรัม)?|kgs?|กรัม|g|ตัน|ปอนด์|lbs?)\b", re.IGNORECASE),
    re.compile(r"\d+(?:\.\d+)?\s*%|\d+(?:\.\d+)?\s*เปอร์เซ็นต์"),
    re.compile(r"\b\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}\b"),
    re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{2,4}\d{2,}(?![A-Za-z])"),
]


def _all_protected_spans(text: str) -> List[Tuple[int, int]]:
    spans = list(_sc_protected_spans(text))
    for pat in _EXTRA_PROTECTED_PATTERNS:
        for m in pat.finditer(text):
            if m.start() != m.end():
                spans.append((m.start(), m.end()))
    spans.sort()
    merged: List[Tuple[int, int]] = []
    for s, e in spans:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def _segments(text: str, protected: List[Tuple[int, int]]):
    """Yield (chunk, is_protected) covering `text` exactly once, in order."""
    pos = 0
    for s, e in protected:
        if s > pos:
            yield text[pos:s], False
        yield text[s:e], True
        pos = e
    if pos < len(text):
        yield text[pos:], False


# ── step 1 — light chat clean (outside protected spans only) ─────────────
# Every repeat collapse is BOUNDARY-ANCHORED: it fires only when the run
# is immediately followed by whitespace, message end, `!`/`?`/`.`/`…`, or
# a non-Thai / non-Latin character. Chat elongation ("ครับบบ", "ค่ะะะ",
# "จริงงง", "ดีมั้ยยย") is always word-trailing; a genuine mid-token
# triple ("เป็นนนทบุรี" = the last น of "เป็น" + "นนทบุรี") is left
# untouched. There is NO mid-word de-spacing — customer-entered values (a
# receiver name "สมชาย ใจดี", an address "ต.บางแก้ว อ.บางพลี") contain
# spaces and removing them corrupts the slot value.
_THAI_TRAILING = "ะัาำิีึืุู็่้๊๋์ๆ"  # vowels / tone marks / mai-yamok
_BOUNDARY = r"(?=[\s!?.…]|$|[^฀-๿a-zA-Z])"
_REPEAT_TRAILING_RE = re.compile("([" + _THAI_TRAILING + r"])\1{1,}" + _BOUNDARY)
_REPEAT_CHAR_RE = re.compile(r"([ก-ฮa-zA-Z])\1{2,}" + _BOUNDARY)
_REPEAT_PUNCT_RE = re.compile(r"([!?])\1{1,}")
_MULTISPACE_RE = re.compile(r"\s+")


def _light_clean_segment(chunk: str) -> str:
    chunk = _REPEAT_TRAILING_RE.sub(r"\1", chunk)          # ค่ะะะ  -> ค่ะ
    chunk = _REPEAT_CHAR_RE.sub(r"\1", chunk)              # จริงงง -> จริง
    chunk = _REPEAT_PUNCT_RE.sub(r"\1", chunk)             # !!! -> !  ??? -> ?
    chunk = re.sub(r"\.{3,}", "…", chunk)                  # .... -> …
    return chunk


# ── step 3 — tiny curated 1:1 map (LHS is always a NON-WORD) ────────────
# Rules: the mistyped form is a frequent chat contraction / a single
# missing char on a HIGH-FREQUENCY routing verb, it is not an identifier
# or number, it carries no new entity, and the LHS pattern NEVER matches
# the correct spelling nor any other real Thai word. Patterns end-anchor
# where a prefix collision is possible (e.g. "เบอ(?!ร)" leaves "เบอร์").
_CHAT_FORMS: List[Tuple["re.Pattern", str]] = [
    (re.compile(r"ลิ(?:๊|้)ง(?:ค์|ค)?|ลิงค์|ลิง[ดตค]์"), "ลิงก์"),
    (re.compile(r"คำนวน|คํานว[นณ]|คำนวณน|คนวณ|คนวน"), "คำนวณ"),
    (re.compile(r"ขนสง(?!\S*ส่ง)"), "ขนส่ง"),
    (re.compile(r"ค่าสง(?!\S*ส่ง)"), "ค่าส่ง"),
    (re.compile(r"ถอนเงน(?!\S*เงิน)"), "ถอนเงิน"),
    (re.compile(r"เบอ(?!ร)"), "เบอร์"),
    (re.compile(r"เวบ(?![ก-ฮ])|เว็ป(?![ก-ฮ])"), "เว็บ"),
    (re.compile(r"ติด(?:่อ|อ่)(?![ก-ฮ])"), "ติดต่อ"),
    (re.compile(r"ใบกำกั(?![บก-ฮ])"), "ใบกำกับ"),
    (re.compile(r"ใกำกับ"), "ใบกำกับ"),
    # dropped น on "นำเข้า" -> NON-WORD "นเข้า" (bare "เข้า" is real, untouched)
    (re.compile(r"(?<![ก-ฮ])นเข้า"), "นำเข้า"),
    # operational-verb typo (was decision_engine._OPERATIONAL_TYPO_FIXES)
    (re.compile(r"เปเลี่ยน"), "เปลี่ยน"),
]

# unit chat forms — end-anchored so "กิโ" is never rewritten inside
# "กิโลกรัม"; the LHS strings are never valid Thai words.
_UNIT_FORMS = [
    (re.compile(r"(กโล|กิโ)(?![ก-ฮ])"), "กิโล"),
    (re.compile(r"กรัม(ม+)(?![ก-ฮ])"), "กรัม"),
]

# ── step 4 — MINIMAL fuzzy repair whitelist. Only long, distinctive
# routing NOUNS with no close real-word neighbour (hand-checked): a 1-2
# edit repair toward one of these cannot land on a different real Thai
# word. Every ambiguous / short verb ("ถอนเงิน" vs "โอนเงิน", "นำเข้า" vs
# "เข้า", "บริการ" vs "บริหาร", "คำนวณ", "ขนส่ง", "ยกเลิก", "ติดต่อ",
# "ลิงก์") is DELIBERATELY excluded — handled only by the exact non-word
# _CHAT_FORMS above, never by edit distance.
_FUZZY_WHITELIST = sorted({
    "ใบกำกับภาษี", "โกดังจีน", "แปลงลิงก์", "เว็บไซต์",
    "เบอร์โทร", "ประเมิน", "คำแนะนำ", "สถานะพัสดุ", "ติดตามพัสดุ",
}, key=len, reverse=True)


def _apply_fuzzy_whitelist(text: str) -> Tuple[str, List[Dict]]:
    try:
        from rag.spell_correction import _find_fuzzy_spans, _apply_fuzzy_spans
        spans = _find_fuzzy_spans(text, _FUZZY_WHITELIST)
        if spans:
            return _apply_fuzzy_spans(text, spans)
    except Exception:
        pass
    return text, []


def _apply_chat_forms(chunk: str) -> Tuple[str, List[Dict]]:
    corrections: List[Dict] = []
    for pat, right in _CHAT_FORMS:
        new = pat.sub(right, chunk)
        if new != chunk:
            corrections.append({"from": chunk, "to": new, "method": "chat_form"})
            chunk = new
    for pat, repl in _UNIT_FORMS:
        new = pat.sub(repl, chunk)
        if new != chunk:
            corrections.append({"from": chunk, "to": new, "method": "chat_unit"})
            chunk = new
    return chunk, corrections


@dataclass
class NormalizedMessage:
    raw: str
    normalized: str
    protected_tokens: List[str] = field(default_factory=list)
    corrections: List[Dict] = field(default_factory=list)
    confidence: float = 1.0
    changed: bool = False
    rejected: str = ""       # the vetoed candidate, if the guard rejected it
    rejection_reason: str = ""

    def as_dict(self) -> Dict:
        return {
            "raw": self.raw, "normalized": self.normalized,
            "protected_tokens": self.protected_tokens, "corrections": self.corrections,
            "confidence": self.confidence, "changed": self.changed,
            "rejected": self.rejected, "rejection_reason": self.rejection_reason,
        }


def normalize_message(raw: str, *, carried_entities: Dict = None,
                       run_vocab_correction: bool = True) -> NormalizedMessage:
    """Return the raw text plus a typo-tolerant `normalized` form safe for
    intent / conversation-act / RAG-query interpretation. Structured
    tokens are frozen verbatim; a semantic-drift veto reverts to raw."""
    if not raw or not raw.strip():
        return NormalizedMessage(raw=raw or "", normalized=raw or "")

    nfc = unicodedata.normalize("NFC", raw)
    protected = _all_protected_spans(nfc)
    protected_tokens = [nfc[s:e] for s, e in protected]

    out_parts: List[str] = []
    corrections: List[Dict] = []
    for chunk, is_prot in _segments(nfc, protected):
        if is_prot:
            out_parts.append(chunk)
            continue
        c = _light_clean_segment(chunk)
        c, cf = _apply_chat_forms(c)
        corrections.extend(cf)
        out_parts.append(c)
    stage1 = _MULTISPACE_RE.sub(" ", "".join(out_parts)).strip()

    # step 2 — admin-curated DIRECT 1:1 corrections ("CBเอ็ม" -> "CBM").
    confidence = 1.0
    if run_vocab_correction and stage1:
        try:
            from rag.spell_correction import _apply_direct_corrections, get_fallback_dict
            direct_map = get_fallback_dict().get("direct_corrections", {})
            if direct_map:
                new_text, dcorr, _ = _apply_direct_corrections(stage1, direct_map)
                if new_text != stage1:
                    corrections.extend(dcorr)
                    stage1 = new_text
        except Exception:
            pass

    # step 4 — minimal distinctive-noun fuzzy repair (see whitelist note).
    if run_vocab_correction and stage1:
        new_text, fcorr = _apply_fuzzy_whitelist(stage1)
        if new_text != stage1:
            corrections.extend(fcorr)
            confidence = min([confidence] + [c.get("confidence", 1.0) for c in fcorr])
            stage1 = new_text

    normalized = stage1
    rejected = rejection_reason = ""
    if normalized.strip() != nfc.strip():
        try:
            from rag.semantic_guard import validate_transformation
            verdict = validate_transformation(raw, normalized, carried_entities=carried_entities)
            if not verdict.get("accepted"):
                rejected = normalized
                rejection_reason = verdict.get("reason") or "semantic drift"
                normalized = raw
                corrections = []
                confidence = 1.0
        except Exception:
            pass

    changed = normalized.strip() != raw.strip()
    return NormalizedMessage(
        raw=raw, normalized=normalized if changed else raw,
        protected_tokens=protected_tokens, corrections=corrections,
        confidence=round(confidence, 3), changed=changed,
        rejected=rejected, rejection_reason=rejection_reason,
    )
