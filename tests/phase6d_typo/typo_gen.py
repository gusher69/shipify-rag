# -*- coding: utf-8 -*-
"""Deterministic Thai typo transforms for the PHASE-6D corpus.

Given a clean sentence and a seed, produce a small set of realistic
minor-typo variants — each applying exactly ONE transformation class so a
failure points at a specific weakness. Structured tokens (URLs, ids,
numbers, dimensions, phones) are located and NEVER mutated by the
transforms — the generator is not allowed to create the very corruption
the normalizer must prevent.
"""
from __future__ import annotations

import random
import re
from typing import Callable, List, Tuple

from services.thai_text_normalizer import _all_protected_spans

_THAI_CONS = "กขคงจฉชซญฎฏฐฑฒณดตถทธนบปผฝพฟภมยรลวศษสหอฮ"
_THAI_TONE = "่้๊๋"
_THAI_UPPER_VOWEL = "ิีึืั็"
# rough adjacent-key clusters on the Kedmanee Thai layout
_ADJ = {
    "ก": "ด", "ด": "ก", "า": "ส", "ส": "ห", "ห": "ก", "เ": "แ", "แ": "เ",
    "ง": "ก", "น": "บ", "บ": "น", "ม": "ท", "ท": "ม", "ค": "ต", "ต": "จ",
    "ล": "ส", "ว": "ง", "อ": "ร", "ร": "พ", "พ": "ะ", "ย": "ั",
}
_PHONETIC = [
    ("ไหม", "มั้ย"), ("ไหม", "มั๊ย"), ("ครับ", "คับ"), ("ค่ะ", "คะ"),
    ("ด้วย", "ด้วน"), ("หน่อย", "หนอย"), ("ยังไง", "ไง"), ("เท่าไหร่", "เท่าไร"),
    ("ทำไม", "ทามไม"), ("อะไร", "ไร"),
]


def _is_free(i: int, spans) -> bool:
    return not any(s <= i < e for s, e in spans)


def _drop_char(s: str, rnd: random.Random, klass: str) -> str:
    spans = _all_protected_spans(s)
    idxs = [i for i, ch in enumerate(s)
            if _is_free(i, spans) and i > 1 and (
                (klass == "cons" and ch in _THAI_CONS) or
                (klass == "vowel" and ch in _THAI_UPPER_VOWEL + "ะาำ") or
                (klass == "tone" and ch in _THAI_TONE))
            # keep the leading consonant of a word (prev char is a space)
            and not (i > 0 and s[i - 1] == " ")
            # dropping a consonant that leaves an orphan tone mark is not a
            # realistic minor typo
            and not (klass == "cons" and i + 1 < len(s) and s[i + 1] in _THAI_TONE)]
    if not idxs:
        return s
    i = rnd.choice(idxs)
    return s[:i] + s[i + 1:]


def _dup_char(s: str, rnd: random.Random) -> str:
    spans = _all_protected_spans(s)
    idxs = [i for i, ch in enumerate(s) if _is_free(i, spans) and ch.strip()
            and ch in _THAI_CONS + "ะา" + "abcdefghijklmnopqrstuvwxyz"]
    if not idxs:
        return s
    i = rnd.choice(idxs)
    return s[:i + 1] + s[i] * rnd.choice([1, 2]) + s[i + 1:]


def _adjacent_key(s: str, rnd: random.Random) -> str:
    spans = _all_protected_spans(s)
    # never the very first character of the message or of a word — a
    # typist rarely fat-fingers a word-initial consonant into an unrelated
    # one; mid-word slips are the realistic case.
    idxs = [i for i, ch in enumerate(s)
            if _is_free(i, spans) and ch in _ADJ and i > 0 and s[i - 1] != " "]
    if not idxs:
        return s
    i = rnd.choice(idxs)
    return s[:i] + _ADJ[s[i]] + s[i + 1:]


def _join_words(s: str, rnd: random.Random) -> str:
    parts = s.split(" ")
    if len(parts) < 2:
        return s
    i = rnd.randrange(len(parts) - 1)
    parts[i] = parts[i] + parts[i + 1]
    del parts[i + 1]
    return " ".join(parts)


def _split_word(s: str, rnd: random.Random) -> str:
    spans = _all_protected_spans(s)
    idxs = [i for i in range(2, len(s) - 2)
            if _is_free(i, spans) and s[i] in _THAI_CONS and s[i - 1] != " " and s[i + 1] != " "]
    if not idxs:
        return s
    i = rnd.choice(idxs)
    return s[:i] + " " + s[i:]


def _dup_punct(s: str, rnd: random.Random) -> str:
    if s and s[-1] in "!?":
        return s + s[-1] * rnd.choice([2, 3])
    return s + rnd.choice(["!!", "??", " ...", "!!!"])


def _phonetic(s: str, rnd: random.Random) -> str:
    opts = [(a, b) for a, b in _PHONETIC if a in s]
    if not opts:
        return s
    a, b = rnd.choice(opts)
    return s.replace(a, b, 1)


def _case_noise(s: str, rnd: random.Random) -> str:
    def flip(m):
        w = m.group(0)
        return w.upper() if w.islower() else w.lower()
    return re.sub(r"[A-Za-z]{2,}", flip, s, count=1)


_TRANSFORMS: List[Tuple[str, Callable]] = [
    ("missing_consonant", lambda s, r: _drop_char(s, r, "cons")),
    ("missing_vowel", lambda s, r: _drop_char(s, r, "vowel")),
    ("missing_tone", lambda s, r: _drop_char(s, r, "tone")),
    ("duplicated_char", _dup_char),
    ("adjacent_key", _adjacent_key),
    ("joined_words", _join_words),
    ("separated_words", _split_word),
    ("phonetic_informal", _phonetic),
    ("punct_noise", _dup_punct),
    ("mixed_casing", _case_noise),
]


def variants(sentence: str, *, seed: int, n: int = 4) -> List[Tuple[str, str]]:
    """Return up to `n` (transform_name, mutated_sentence) pairs that
    actually differ from the original."""
    rnd = random.Random(seed)
    order = list(_TRANSFORMS)
    rnd.shuffle(order)
    out: List[Tuple[str, str]] = []
    for name, fn in order:
        try:
            mut = fn(sentence, rnd)
        except Exception:
            mut = sentence
        if mut and mut != sentence and mut not in {m for _, m in out}:
            out.append((name, mut))
        if len(out) >= n:
            break
    return out
