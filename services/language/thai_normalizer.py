# -*- coding: utf-8 -*-
"""THE ONE input-normalisation layer for human Thai (PLATFORM component).

What it does, in order, for one customer message:

  1. PROTECT structured identifiers — URLs, e-mails, phone numbers,
     alphanumeric codes (POS123456, FT3182, tracking strings) and
     numbers are cut out first and re-inserted untouched. Nothing below
     can ever see, let alone rewrite, them (task §2/§9: IDENTIFIER
     MUTATION = 0 by construction, not by test).
  2. CHARACTER normalisation of the free text — Unicode NFC, zero-width
     artefacts, NBSP, PyThaiNLP's Thai normalisation (mark order,
     duplicated tone/vowel marks, ํา -> ำ), elongation collapse
     ("ครับบบบ" -> "ครับ"), whitespace.
  3. PARTICLE aliasing — informal sentence-final politeness markers
     ("คับ", "ค้าบ", "คร่า") to their canonical spelling, tail position
     only.
  4. TOKEN-LEVEL candidates — PyThaiNLP tokenises the free text; every
     1..3-token window is offered to RapidFuzz against the BOUNDED
     vocabulary in vocabulary.py. A match becomes a candidate with a
     tier:
        HIGH    marks-only difference on an out-of-vocabulary span, or
                an edit-distance-1 match whose grammatical context
                confirms the role (a number before a unit, "ส่ง" before
                a shipping mode, "ถึง" before a destination)
        MEDIUM  a plausible correction the normaliser will NOT apply;
                it is handed to the semantic resolver, which decides
                whether the correction is useful (§3/§6)
     PyThaiNLP's spell checker contributes MEDIUM candidates for
     out-of-vocabulary single tokens; keyboard-layout recovery
     (eng_to_thai) contributes a HIGH candidate only when every
     resulting token is a dictionary word.
  5. APPLY the HIGH candidates (non-overlapping, best first), then run
     step 4 once more on the corrected text — a corrected typo often
     un-jams the tokenisation of the word after it.

What it deliberately does NOT do (task §2/§11):

  * change a dictionary word of 4+ characters unless the difference is
    marks-only AND the grammatical context confirms it — "กระต่าย" is a
    valid unknown product and stays "กระต่าย";
  * correct anything TOWARDS a product name — no product is in any
    vocabulary group, so a typo can never become a different product;
  * apply a MEDIUM candidate on its own authority — the semantic layer
    owns meaning; this layer produces evidence;
  * touch `raw_text` — every result carries both texts.

Degrade-safe: if PyThaiNLP or RapidFuzz cannot be imported the layer
still performs steps 1–3 and 5 and reports `methods` accordingly; it
never raises into the conversation.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field, asdict
from functools import lru_cache
from typing import Any, Dict, List, Optional, Sequence, Tuple

from services.language import vocabulary as V

# ── optional libraries (degrade-safe) ────────────────────────────────
try:                                                     # pragma: no cover
    from pythainlp.util import normalize as _pt_normalize
    from pythainlp.tokenize import word_tokenize as _pt_tokenize
    from pythainlp.corpus import thai_words as _pt_words
    from pythainlp.spell import spell as _pt_spell
    from pythainlp.util import eng_to_thai as _pt_eng_to_thai
    PYTHAINLP_AVAILABLE = True
except Exception:                                        # pragma: no cover
    PYTHAINLP_AVAILABLE = False
    _pt_normalize = _pt_tokenize = _pt_words = _pt_spell = _pt_eng_to_thai = None

try:                                                     # pragma: no cover
    from rapidfuzz import fuzz as _rf_fuzz, process as _rf_process
    from rapidfuzz.distance import Levenshtein as _rf_lev
    RAPIDFUZZ_AVAILABLE = True
except Exception:                                        # pragma: no cover
    RAPIDFUZZ_AVAILABLE = False
    _rf_fuzz = _rf_process = _rf_lev = None


# ── character classes ────────────────────────────────────────────────
THAI_CHAR_RE = re.compile(r"[฀-๿]")
_THAI_RUN_RE = re.compile(r"[฀-๿]+")
# combining marks: ั ิ ี ึ ื ุ ู ฺ ็ ่ ้ ๊ ๋ ์ ํ ๎
_MARKS = frozenset("ัิีึืฺุู"
                   "็่้๊๋์ํ๎")
_THANTHAKHAT = "์"
_TONE_MARKS = frozenset("่้๊๋")
_ZW_RE = re.compile("[​‌‍⁠﻿]")
_WS_RE = re.compile(r"[ \t 　]+")
# three or more identical Thai characters in a row is elongation
# ("ครับบบ", "มากกก"); Thai orthography never triples a character.
_ELONGATION_RE = re.compile(r"([฀-๿])\1{2,}")
_DOUBLE_RE = re.compile(r"([฀-๿])\1")


def skeleton(s: str, *, drop_silenced: bool = True) -> str:
    """The consonant/base-vowel skeleton of a Thai string: every
    combining mark removed, a silenced final consonant (X์) removed with
    its mark, ใ folded to ไ. Two strings with the same skeleton differ
    only by marks — the most common Thai typing slip."""
    out: List[str] = []
    for ch in s or "":
        if ch == _THANTHAKHAT:
            if out and drop_silenced:
                out.pop()
            continue
        if ch in _MARKS:
            continue
        if ch == "ใ":
            ch = "ไ"
        out.append(ch)
    return "".join(out)


def _silenced_dropped(term: str) -> str:
    """The term with its silenced X์ consonant(s) removed ("ออเดอร์" ->
    "ออเดอ"), all other marks intact."""
    out: List[str] = []
    for ch in term:
        if ch == _THANTHAKHAT:
            if out:
                out.pop()
            continue
        out.append(ch)
    return "".join(out)


def marks_only_diff(s: str, term: str) -> bool:
    """Is `s` the term with only tone/vowel marks wrong, OR the term with
    its silenced consonant dropped (and at most tone marks wrong)? The
    two slips are NOT combined: "ลัง" is not "ลิงก์" (a dropped ก์ AND a
    changed vowel), while "ออเดอ" is "ออเดอร์" and "นำเข่า" is "นำเข้า"."""
    if skeleton(s, drop_silenced=False) == skeleton(term, drop_silenced=False):
        return True
    if _THANTHAKHAT in term:
        base = _silenced_dropped(term)
        strip_tone = lambda x: "".join(ch for ch in x if ch not in _TONE_MARKS)  # noqa: E731
        return strip_tone(s) == strip_tone(base)
    return False


# ── identifier protection ────────────────────────────────────────────
# kinds are ordered by priority; the FIRST pattern that matches at a
# position wins. `free` is whatever remains.
_PROTECT: Sequence[Tuple[str, "re.Pattern[str]"]] = (
    ("url", re.compile(r"https?://[^\s]+|www\.[^\s]+|"
                       r"[A-Za-z0-9][A-Za-z0-9.-]*\.(?:com|net|org|co\.th|cn|th|io|shop|me|tw)"
                       r"(?:/[^\s]*)?", re.IGNORECASE)),
    ("email", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")),
    ("phone", re.compile(r"(?<!\d)(?:\+66[- ]?\d[- ]?\d{3}[- ]?\d{3,4}|0\d{1,2}[- ]?\d{3}[- ]?\d{3,4})(?!\d)")),
    # letters+digits code that STARTS with a letter: POS123456, FT3182,
    # TH1234567890TH, ABC-123, 1688-style item ids are numbers below.
    ("code", re.compile(r"(?<![A-Za-z0-9])(?=[A-Za-z0-9_-]*\d)[A-Za-z][A-Za-z0-9_-]{2,}(?![A-Za-z0-9])")),
    ("number", re.compile(r"\d+(?:[.,]\d+)*")),
    # a Latin run may carry the punctuation the Thai layout puts on
    # letter keys (";" "," "." "/" "[" "]" "'" "-" "=") — see keyboard recovery.
    ("latin", re.compile(r"[A-Za-z][A-Za-z;,./'\[\]\\=-]*")),
)
_PROTECT_ANY = re.compile("|".join(f"(?P<{k}>{p.pattern})" for k, p in _PROTECT),
                          re.IGNORECASE)
PROTECTED_KINDS = frozenset({"url", "email", "phone", "code", "number"})
_MASK_BY_KIND = {"url": "[URL]", "email": "[EMAIL]", "phone": "[PHONE]", "code": "[ID]"}


@dataclass
class Segment:
    kind: str          # free | latin | url | email | phone | code | number
    text: str


def protect_identifiers(text: str) -> List[Segment]:
    """Split text into free spans and protected identifier spans."""
    segs: List[Segment] = []
    pos = 0
    for m in _PROTECT_ANY.finditer(text or ""):
        if m.start() > pos:
            segs.append(Segment("free", text[pos:m.start()]))
        segs.append(Segment(m.lastgroup or "free", m.group(0)))
        pos = m.end()
    if pos < len(text or ""):
        segs.append(Segment("free", text[pos:]))
    return segs


def mask_identifiers(text: str) -> str:
    """The trace-safe rendering of a text: every protected identifier
    replaced by its kind, long numbers by [NUM]. Short numbers
    (quantities, weights) are kept — they are what a reviewer needs."""
    out = []
    for seg in protect_identifiers(text or ""):
        if seg.kind in _MASK_BY_KIND:
            out.append(_MASK_BY_KIND[seg.kind])
        elif seg.kind == "number" and len(re.sub(r"\D", "", seg.text)) >= 5:
            out.append("[NUM]")
        else:
            out.append(seg.text)
    return "".join(out)


# ── dictionary / tokeniser (lazy, cached) ────────────────────────────
@lru_cache(maxsize=1)
def _dictionary() -> frozenset:
    words = set(V.all_terms())
    if PYTHAINLP_AVAILABLE:
        try:
            words |= set(_pt_words())
        except Exception:                                # pragma: no cover
            pass
    return frozenset(words)


def is_dictionary_word(tok: str) -> bool:
    return bool(tok) and tok in _dictionary()


def _tokenize(text: str) -> List[str]:
    """Thai word tokens whose concatenation is exactly `text`."""
    if not text:
        return []
    if PYTHAINLP_AVAILABLE:
        try:
            toks = _pt_tokenize(text, engine="newmm", keep_whitespace=True)
            if "".join(toks) == text:
                return toks
        except Exception:                                # pragma: no cover
            pass
    return [t for t in re.split(r"(\s+)", text) if t]


def oov_count(text: str) -> int:
    """How many Thai tokens of 2+ characters are not dictionary words —
    the semantic resolver uses it to prefer a reading whose entities are
    real words over one that swallowed a typo as a product name."""
    n = 0
    for seg in protect_identifiers(text or ""):
        if seg.kind != "free":
            continue
        for tok in _tokenize(seg.text):
            if len(tok) >= 2 and THAI_CHAR_RE.search(tok) and not is_dictionary_word(tok):
                n += 1
    return n


# ── candidates ───────────────────────────────────────────────────────
@dataclass
class Candidate:
    original: str
    replacement: str
    start: int
    end: int
    score: float
    tier: str            # HIGH | MEDIUM | LOW
    reason: str
    source: str          # rapidfuzz_vocab | pythainlp_spell | keyboard_layout | particle_alias
    group: str = ""
    applied: bool = False
    # may the semantic resolver APPLY this MEDIUM candidate? False for
    # evidence-only candidates (a run of real words that happens to spell a
    # term marks-away: the phrase may simply mean what it says).
    recoverable: bool = True

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class NormalizationResult:
    raw_text: str
    normalized_text: str
    candidates: List[Candidate] = field(default_factory=list)
    confidence: float = 1.0
    methods: List[str] = field(default_factory=list)
    protected: List[Tuple[str, str]] = field(default_factory=list)

    @property
    def applied(self) -> List[Candidate]:
        return [c for c in self.candidates if c.applied]

    @property
    def pending(self) -> List[Candidate]:
        """MEDIUM candidates, offsets relative to `normalized_text`."""
        return [c for c in self.candidates if not c.applied and c.tier == "MEDIUM"]

    @property
    def changed(self) -> bool:
        return self.normalized_text != self.raw_text

    def apply(self, cands: Sequence[Candidate]) -> str:
        """A variant of `normalized_text` with the given pending
        candidates applied (used by the semantic resolver to test a
        reading; never written back here)."""
        text = self.normalized_text
        for c in sorted(_non_overlapping(list(cands)), key=lambda c: c.start, reverse=True):
            text = text[:c.start] + c.replacement + text[c.end:]
        return text

    def as_trace(self) -> Dict[str, Any]:
        """Privacy-safe summary for observability (task §14). Identifiers
        are masked by kind; corrections are vocabulary words by
        construction and never identifiers."""
        return {
            "raw_text_present": bool(self.raw_text),
            "normalized_text": mask_identifiers(self.normalized_text),
            "normalization_applied": self.changed,
            "normalization_method": list(self.methods),
            "candidate_count": len(self.candidates),
            "applied": [{"from": c.original, "to": c.replacement, "tier": c.tier,
                         "score": round(c.score, 1), "source": c.source, "group": c.group}
                        for c in self.applied],
            "pending": [{"from": c.original, "to": c.replacement, "tier": c.tier,
                         "score": round(c.score, 1), "source": c.source, "group": c.group}
                        for c in self.pending],
            "confidence": round(self.confidence, 3),
            "protected_kinds": sorted({k for k, _ in self.protected}),
            "protected_count": len(self.protected),
        }


def _non_overlapping(cands: List[Candidate]) -> List[Candidate]:
    chosen: List[Candidate] = []
    for c in sorted(cands, key=lambda c: (-c.score, -(c.end - c.start), c.start)):
        if all(c.end <= o.start or c.start >= o.end for o in chosen):
            chosen.append(c)
    return chosen


# ── step 2: character normalisation ──────────────────────────────────
def _char_normalize(s: str, methods: List[str]) -> str:
    t = unicodedata.normalize("NFC", s)
    if t != s:
        methods.append("unicode_nfc")
    t2 = _ZW_RE.sub("", t).replace(" ", " ").replace("　", " ")
    if t2 != t:
        methods.append("zero_width")
    t = t2
    if PYTHAINLP_AVAILABLE:
        try:
            # PyThaiNLP strips edge whitespace; a space between a number
            # and its unit is meaning we keep.
            lead = t[:len(t) - len(t.lstrip())]
            trail = t[len(t.rstrip()):]
            core = _pt_normalize(t.strip())
            t2 = lead + core + trail
            if t2 != t:
                methods.append("pythainlp_normalize")
            t = t2
        except Exception:                                # pragma: no cover
            pass
    t2 = _ELONGATION_RE.sub(r"\1", t)
    if t2 != t:
        methods.append("elongation")
    return t2


# ── step 3: sentence-final particle aliases ──────────────────────────
_ALIAS_KEYS = sorted(V.PARTICLE_ALIASES, key=len, reverse=True)
_TAIL_UNIT = "|".join(re.escape(x) for x in list(dict.fromkeys(_ALIAS_KEYS + list(V.PARTICLE_TAIL))))
_TAIL_RE = re.compile(r"((?:\s*(?:" + _TAIL_UNIT + r"))+)\s*$")
_ALIAS_RE = re.compile("|".join(re.escape(k) for k in _ALIAS_KEYS))


_TAIL_WORDS = frozenset(_ALIAS_KEYS) | frozenset(V.PARTICLE_TAIL)


def _tail_start(text: str) -> Optional[int]:
    """Where the sentence-final particle run begins, on a TOKEN boundary
    — so "คับ" inside "บังคับ" is never mistaken for the particle."""
    toks = _tokenize(text)
    pos = len(text)
    start = None
    for t in reversed(toks):
        if t.isspace() or t in _TAIL_WORDS:
            pos -= len(t)
            if not t.isspace():
                start = pos
            continue
        break
    return start


def _alias_particles(text: str) -> Tuple[str, List[Candidate]]:
    ts = _tail_start(text)
    if ts is None:
        return text, []
    m = _TAIL_RE.search(text, ts)
    if not m or m.start(1) < ts:
        return text, []
    tail = m.group(1)
    cands: List[Candidate] = []
    before = text[:m.start(1)]

    def _sub(mm: "re.Match[str]") -> str:
        word = mm.group(0)
        guard = V.PARTICLE_ALIAS_GUARDS.get(word)
        if guard and re.search(guard, before):
            return word
        canon = V.PARTICLE_ALIASES[word]
        cands.append(Candidate(word, canon, m.start(1) + mm.start(),
                               m.start(1) + mm.end(), 100.0, "HIGH",
                               "sentence-final politeness particle", "particle_alias",
                               "particle", True))
        return canon

    new_tail = _ALIAS_RE.sub(_sub, tail)
    if new_tail == tail:
        return text, []
    return text[:m.start(1)] + new_tail + text[m.end(1):], cands


# ── step 4: fuzzy candidates against the bounded vocabulary ──────────
_TERMS = V.all_terms()
_PARTICLE_CANON = frozenset(V.PARTICLE_ALIASES.values())
_GROUP_LEFT = {g.name: re.compile(g.left_context) if g.left_context else None for g in V.GROUPS}
_GROUP_RIGHT = {g.name: re.compile(g.right_context) if g.right_context else None for g in V.GROUPS}
_MAX_WINDOW = 3
_SPELL_MAX_TOKENS = 2
_SPELL_MAX_LEN = 5
SPELL_ENABLED = True


@dataclass
class _Tok:
    text: str
    start: int
    end: int

    @property
    def thai(self) -> bool:
        return bool(THAI_CHAR_RE.search(self.text)) and not self.text.isspace()


def _tokens_with_offsets(text: str, base: int) -> List[_Tok]:
    out, pos = [], 0
    for t in _tokenize(text):
        out.append(_Tok(t, base + pos, base + pos + len(t)))
        pos += len(t)
    return out


def _is_fragment(tok: str) -> bool:
    """A single Thai letter, or a token made only of marks — the debris
    a typo leaves behind in the tokeniser."""
    return len(tok) == 1 or all(ch in _MARKS for ch in tok)


def _best_term(s: str) -> Optional[Tuple[str, float]]:
    if not RAPIDFUZZ_AVAILABLE:
        return None
    best = _rf_process.extractOne(s, _TERMS, scorer=_rf_fuzz.ratio, score_cutoff=70)
    s2 = _DOUBLE_RE.sub(r"\1", s)
    if s2 != s:
        b2 = _rf_process.extractOne(s2, _TERMS, scorer=_rf_fuzz.ratio, score_cutoff=70)
        if b2 and (not best or b2[1] > best[1]):
            best = b2
    if not best:
        return None
    return best[0], float(best[1])


def _context_confirms(full: str, start: int, end: int, group: V.VocabGroup) -> bool:
    left = _GROUP_LEFT.get(group.name)
    right = _GROUP_RIGHT.get(group.name)
    if left and left.search(full[:start]):
        return True
    if right and right.search(full[end:]):
        return True
    return False


def _segmentation_quality(run: str) -> Tuple[int, int]:
    """(-non_word_tokens, -token_count): fewer fragments first, then fewer
    tokens. Counting +1 per word rewarded fragmentation ("เป้|นร|อง|เท้า"
    scored higher than "เป็น|รองเท้า")."""
    non_word = total = 0
    for t in _tokenize(run):
        if t.isspace():
            continue
        total += 1
        if not (len(t) >= 2 and is_dictionary_word(t)):
            non_word += 1
    return (-non_word, -total)


def _segments_better(run: str, i: int, j: int, term: str) -> bool:
    before = _segmentation_quality(run)
    after = _segmentation_quality(run[:i] + term + run[j:])
    return after > before


def _starts_word(full: str, pos: int) -> bool:
    """Does a dictionary word (2+ letters) begin at `pos`?"""
    rest = full[pos:]
    toks = [t for t in _tokenize(rest) if not t.isspace()]
    return bool(toks) and len(toks[0]) >= 2 and is_dictionary_word(toks[0])


def _vocab_candidates(full: str, toks: List[_Tok]) -> List[Candidate]:
    """Offer every plausible CHARACTER window of the Thai text to the
    bounded vocabulary.

    Character windows rather than token windows, because a typo breaks
    the tokeniser around it ("เป้นรองเท้า" -> เป้ | นร | อง | เท้า): the
    misspelt word and the token boundaries no longer coincide. The
    guard that keeps this from matching INSIDE real words is the
    token-cut rule: a window may not cut into a dictionary word — it
    may only cover whole tokens, or cut tokens that are themselves
    out-of-vocabulary debris. "เรื่อ" inside "เรื่องนี้" is therefore never a
    candidate for "เรือ", while "เป้น" cutting the OOV fragment "นร" is.
    """
    if not RAPIDFUZZ_AVAILABLE:
        return []
    cands: List[Candidate] = []
    thai_toks = [t for t in toks if t.thai]
    if not thai_toks:
        return []
    # token map for the cut rule
    # a token counts as a REAL word only when it is a dictionary word of
    # three or more letters: the tokeniser emits two-letter dictionary
    # "words" (นร, หน, อย, ไท) as debris around a typo all the time.
    tok_ok = [(t.start, t.end, is_dictionary_word(t.text) and len(t.text) >= 3)
              for t in thai_toks]
    starts = {ts for ts, _, _ in tok_ok}
    ends = {te for _, te, _ in tok_ok}

    def _cuts_dictionary_word(a: int, b: int) -> bool:
        """Does [a, b) start or end INSIDE a token of 2+ letters? (A cut
        into a real word, or into two-letter tokeniser debris such as "นร"
        or "นำ", is only allowed when the correction makes the run
        segment better — checked by the caller.)"""
        for ts, te, is_word in tok_ok:
            if te <= a or ts >= b:
                continue
            if (te - ts) >= 2 and (ts < a or te > b):
                return True
        return False

    def _whole_words(a: int, b: int) -> bool:
        """[a, b) is exactly a run of whole dictionary tokens."""
        if a not in starts or b not in ends:
            return False
        return all(is_word or te <= a or ts >= b for ts, te, is_word in tok_ok)

    seen = set()
    for run in _THAI_RUN_RE.finditer(full):
        rs, rtext = run.start(), run.group(0)
        n = len(rtext)
        for term in _TERMS:
            if not THAI_CHAR_RE.search(term):
                continue
            group = V.group_of(term)
            if group is None:
                continue
            tl = len(term)
            t0 = skeleton(term[:1])
            sk_len = len(skeleton(term))
            for L in range(max(2, min(tl, sk_len) - 1), tl + 2):
                if L > n:
                    continue
                for i in range(0, n - L + 1):
                    s = rtext[i:i + L]
                    if skeleton(s[:1]) != t0 and s[0] != term[0]:
                        continue
                    if s == term or s in _TERMS or (s, term) in seen:
                        continue          # an exact vocabulary word is canonical
                    a, b = rs + i, rs + i + L
                    if _cuts_dictionary_word(a, b) and not _segments_better(rtext, i, i + L, term):
                        # cutting into a real word is allowed only when the
                        # correction makes the run tokenise BETTER (the "word"
                        # was tokeniser debris around the typo: "เบ|อติ|ด|ต่อ"
                        # -> "เบอร์|ติดต่อ"), never when it splits a genuine
                        # word ("เรื่อง|นี้" -> "เรือ|ง|นี้").
                        continue
                    if term in s:
                        # The window already CONTAINS the exact term, so the
                        # "difference" is usually a neighbouring word's
                        # letter — unless it is the term's own final
                        # consonant typed twice ("อยากก", "จีนน", "ครับบ")
                        # and the text after it does not start a word with
                        # that letter ("ของน|ม" belongs to "นม").
                        if not (s == term + term[-1] and not _starts_word(full, b - 1)):
                            continue
                    s_dd = _DOUBLE_RE.sub(r"\1", s)
                    sk_eq = marks_only_diff(s, term) or marks_only_diff(s_dd, term)
                    if sk_eq and _THANTHAKHAT in term and is_dictionary_word(s)                             and skeleton(s, drop_silenced=False) != skeleton(term, drop_silenced=False):
                        # a REAL word that only equals the term once the
                        # term's silenced consonant is dropped ("ลิง" vs
                        # "ลิงก์") is the real word, not a typo.
                        continue
                    score = max(_rf_fuzz.ratio(s, term), _rf_fuzz.ratio(s_dd, term))
                    if sk_eq:
                        # a marks-only slip on a short word scores low on
                        # character ratio ("คุ่"/"คู่" = 67) yet is the most
                        # common Thai typo; the skeleton IS the evidence.
                        score = max(score, 80.0)
                    if score < 75:
                        continue
                    seen.add((s, term))
                    in_dict = is_dictionary_word(s)
                    if in_dict and term in _PARTICLE_CANON:
                        # a real word ("ครบ" = complete) never becomes a
                        # politeness particle here; only the guarded
                        # sentence-final alias pass may do that.
                        continue
                    whole = _whole_words(a, b)
                    aligned = a in starts and b in ends
                    dist = min(_rf_lev.distance(s, term), _rf_lev.distance(s_dd, term))
                    confirmed = _context_confirms(full, a, b, group)
                    tier: Optional[str] = None
                    reason = ""
                    recoverable = True
                    if sk_eq:
                        if whole and not in_dict and len(s) >= 4:
                            # a run of REAL words ("ส่ง|ของ", "สิน|ค่า") that spells
                            # a term marks-away may simply mean what it says
                            # (send goods vs order goods): evidence only, never
                            # applied by spelling — however good the context.
                            tier, reason = "MEDIUM", "marks-only difference across a multi-word span"
                            recoverable = False
                        elif confirmed and (not in_dict or len(s) <= 3 or tl >= 5):
                            tier, reason = "HIGH", "marks-only difference, grammatical context confirms"
                        elif in_dict and not group.correct_dictionary_words:
                            tier = None
                        elif sk_len < 2 and not confirmed:
                            # a one-consonant skeleton ("คู่" -> "ค") is
                            # too little to act on without its role.
                            tier, reason = "MEDIUM", "marks-only difference, one-consonant skeleton"
                        elif not in_dict and not whole:
                            tier, reason = "HIGH", "marks-only difference on an out-of-vocabulary span"
                        elif confirmed:
                            tier, reason = "HIGH", "marks-only difference, grammatical context confirms"
                        elif len(s) <= 2:
                            tier, reason = "MEDIUM", "marks-only difference on a two-letter word"
                        else:
                            tier = None        # a real word with no role evidence stays
                    elif (aligned and tl >= group.min_len and s[0] == term[0]
                          and dist <= (1 if tl <= 5 else 2)):
                        # an insertion/deletion/substitution is only evidence
                        # when the window is a whole token span — a window
                        # that cuts a token is borrowing the neighbour's letter.
                        if in_dict and len(s) >= 4:
                            tier = None        # a real word of 4+ letters is protected
                        elif whole and len(s) >= 4:
                            tier = None        # a run of real words, not a typo
                        elif in_dict and not confirmed:
                            tier = None        # a short real word with no role evidence
                        elif confirmed:
                            tier, reason = "HIGH", f"edit distance {dist}, grammatical context confirms"
                        else:
                            tier, reason = "MEDIUM", f"edit distance {dist}"
                    if tier is None:
                        continue
                    cands.append(Candidate(s, term, a, b, float(score), tier, reason,
                                           "rapidfuzz_vocab", group.name, recoverable=recoverable))
    return cands


@lru_cache(maxsize=4096)
def _spell_cached(tok: str) -> Tuple[str, ...]:
    return tuple(_pt_spell(tok)[:5])


def _spell_candidates(toks: List[_Tok], taken: List[Candidate]) -> List[Candidate]:
    """PyThaiNLP spell suggestions for out-of-vocabulary single tokens
    the vocabulary matcher did not claim. Always MEDIUM: general-Thai
    evidence for the semantic resolver, never applied here."""
    if not PYTHAINLP_AVAILABLE or not SPELL_ENABLED:
        return []
    out: List[Candidate] = []
    budget = _SPELL_MAX_TOKENS
    for t in toks:
        if budget <= 0:
            break
        if not t.thai or not (3 <= len(t.text) <= _SPELL_MAX_LEN) or is_dictionary_word(t.text):
            continue
        if any(c.start < t.end and c.end > t.start for c in taken):
            continue
        budget -= 1
        try:
            sugg = _spell_cached(t.text)
        except Exception:                                # pragma: no cover
            continue
        pick = None
        for s in sugg:
            if s == t.text:
                pick = None
                break
            if skeleton(s) == skeleton(t.text):
                pick = (s, 90.0, "spell: marks-only")
                break
        if pick is None and sugg and sugg[0] != t.text and RAPIDFUZZ_AVAILABLE:
            if _rf_lev.distance(sugg[0], t.text) <= 1:
                pick = (sugg[0], 80.0, "spell: edit distance 1")
        if pick:
            out.append(Candidate(t.text, pick[0], t.start, t.end, pick[1], "MEDIUM",
                                 pick[2], "pythainlp_spell", "general"))
    return out


def _keyboard_candidates(full: str, segs_pos: List[Tuple[Segment, int]]) -> List[Candidate]:
    """A Latin run that becomes all-dictionary Thai under the Thai
    keyboard layout was typed with the layout switched off."""
    if not PYTHAINLP_AVAILABLE:
        return []
    out: List[Candidate] = []
    for seg, start in segs_pos:
        if seg.kind != "latin" or len(seg.text) < 4:
            continue
        try:
            thai = _pt_eng_to_thai(seg.text)
        except Exception:                                # pragma: no cover
            continue
        if not thai or not THAI_CHAR_RE.search(thai) or thai == seg.text:
            continue
        toks = [t for t in _tokenize(thai) if not t.isspace()]
        if toks and all(len(t) >= 2 and is_dictionary_word(t) for t in toks):
            out.append(Candidate(seg.text, thai, start, start + len(seg.text), 95.0,
                                 "HIGH", "Latin run is all-dictionary Thai under the Thai layout",
                                 "keyboard_layout", "keyboard"))
    return out


# ── the pipeline ─────────────────────────────────────────────────────
def _assemble(raw: str, methods: List[str]) -> Tuple[str, List[Tuple[Segment, int]], List[Tuple[str, str]]]:
    """Character-normalise the free spans and re-insert every protected
    span untouched. Returns (text, [(segment, start)], protected)."""
    segs = protect_identifiers(raw)
    out: List[str] = []
    placed: List[Tuple[Segment, int]] = []
    protected: List[Tuple[str, str]] = []
    pos = 0
    for seg in segs:
        if seg.kind == "free":
            t = _char_normalize(seg.text, methods)
            seg = Segment("free", t)
        elif seg.kind in PROTECTED_KINDS:
            protected.append((seg.kind, seg.text))
        placed.append((seg, pos))
        out.append(seg.text)
        pos += len(seg.text)
    return "".join(out), placed, protected


def _resegment(text: str) -> List[Tuple[Segment, int]]:
    placed, pos = [], 0
    for seg in protect_identifiers(text):
        placed.append((seg, pos))
        pos += len(seg.text)
    return placed


def _collect(text: str, placed: List[Tuple[Segment, int]]) -> Tuple[List[Candidate], List[Candidate]]:
    toks: List[_Tok] = []
    for seg, start in placed:
        if seg.kind == "free":
            toks.extend(_tokens_with_offsets(seg.text, start))
    vocab = _vocab_candidates(text, toks)
    high = [c for c in vocab if c.tier == "HIGH"]
    high.extend(_keyboard_candidates(text, placed))
    medium = [c for c in vocab if c.tier == "MEDIUM"]
    medium.extend(_spell_candidates(toks, vocab))
    return high, medium


def normalize(text: str) -> NormalizationResult:
    """Normalise one message. Never raises."""
    raw = text or ""
    methods: List[str] = []
    try:
        return _normalize(raw, methods)
    except Exception as exc:                             # pragma: no cover
        return NormalizationResult(raw_text=raw, normalized_text=_WS_RE.sub(" ", raw).strip(),
                                   methods=methods + [f"degraded:{type(exc).__name__}"])


def _keyboard_whole_message(raw: str) -> Optional[str]:
    """A message with NO Thai character at all that becomes all-dictionary
    Thai under the Thai layout was typed with the layout left in English
    ("iv'gmhk8iy[" -> "รองเท้าครับ"). Checked before identifier protection
    because the Thai layout puts letters on the digit keys (8 = ค)."""
    if not PYTHAINLP_AVAILABLE or not raw or THAI_CHAR_RE.search(raw) or len(raw.strip()) < 4:
        return None
    if _PROTECT_ANY.fullmatch(raw.strip()) and _PROTECT_ANY.fullmatch(raw.strip()).lastgroup != "latin":
        return None                      # a bare identifier / number / URL
    try:
        thai = _pt_eng_to_thai(raw)
    except Exception:                                    # pragma: no cover
        return None
    if not thai or thai == raw or not THAI_CHAR_RE.search(thai):
        return None
    toks = [t for t in _tokenize(thai) if not t.isspace()]
    if not toks or any(len(t) < 2 or not is_dictionary_word(t) for t in toks):
        return None
    if sum(len(t) for t in toks) < 4:
        return None
    return thai


def _normalize(raw: str, methods: List[str]) -> NormalizationResult:
    keyboard: List[Candidate] = []
    src = raw
    kb = _keyboard_whole_message(raw)
    if kb is not None:
        keyboard.append(Candidate(raw, kb, 0, len(raw), 95.0, "HIGH",
                                  "whole message is all-dictionary Thai under the Thai layout",
                                  "keyboard_layout", "keyboard", True))
        methods.append("keyboard_layout")
        src = kb
    text, placed, protected = _assemble(src, methods)
    ws = _WS_RE.sub(" ", text).strip()
    if ws != text:
        methods.append("whitespace")
        text = ws
        placed = _resegment(text)

    text, particle_cands = _alias_particles(text)
    all_cands: List[Candidate] = keyboard + list(particle_cands)
    if particle_cands:
        methods.append("particle_alias")
        placed = _resegment(text)

    pending: List[Candidate] = []
    for _pass in range(2):
        high, medium = _collect(text, placed)
        chosen = _non_overlapping(high)
        if not chosen:
            pending = _non_overlapping(medium)
            break
        for c in chosen:
            c.applied = True
        all_cands.extend(chosen)
        for c in sorted(chosen, key=lambda c: c.start, reverse=True):
            text = text[:c.start] + c.replacement + text[c.end:]
        for src in {c.source for c in chosen}:
            if src not in methods:
                methods.append(src)
        placed = _resegment(text)
        pending = []
    else:
        # two passes applied corrections; collect what is still pending
        _, medium = _collect(text, placed)
        pending = _non_overlapping(medium)

    # medium candidates that overlap an applied span are stale
    applied_spans = [(c.start, c.end) for c in all_cands if c.applied]
    pending = [c for c in pending if not any(c.original == a.original for a in all_cands if a.applied)]
    all_cands.extend(pending)

    conf = 1.0
    applied = [c for c in all_cands if c.applied and c.source != "particle_alias"]
    if applied:
        conf = min(c.score for c in applied) / 100.0
    if pending:
        conf = min(conf, 0.6)
    return NormalizationResult(raw_text=raw, normalized_text=text, candidates=all_cands,
                               confidence=round(conf, 3), methods=methods, protected=protected)


def normalize_history(history: Optional[Sequence[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """The same normalisation applied to every USER turn of a history.
    Assistant turns are the platform's own canonical wording and are
    left exactly as sent. Each normalised user turn keeps its raw text
    under `raw_content`."""
    out: List[Dict[str, Any]] = []
    for turn in history or []:
        if not isinstance(turn, dict):
            out.append(turn)
            continue
        if (turn.get("role") or "").lower() == "user" and isinstance(turn.get("content"), str):
            res = normalize(turn["content"])
            if res.changed:
                out.append({**turn, "content": res.normalized_text, "raw_content": turn["content"]})
                continue
        out.append(turn)
    return out
