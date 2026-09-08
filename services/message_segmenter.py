"""Message Segmenter — splits one LLM answer into 1-3 short, human-like
conversational message bubbles, when the answer's own STRUCTURE actually
has separable ideas (a short lead-in, a details/list block, an optional
next-step). This is a response-ORCHESTRATION step, not a prompt change:
it runs AFTER the LLM answer exists (and after policy/escalation is
already decided), never calls an LLM itself, and never invents content —
every message part is a verbatim substring of the original answer.

The canonical `answer` string is ALWAYS still available in full — for
citations, logs, analytics, "Copy Answer," and backward compatibility.
`message_parts` is a second, additive field.

Splitting only ever happens at coarse, safe boundaries — a blank-line
paragraph break, or between a plain-text line group and a contiguous
bullet-item block — never at an arbitrary character offset. That is what
structurally guarantees a split can never land inside a number, URL,
phone number, tracking ID, bullet item, markdown link, code block, or
table row: none of those ever contain a blank line or a bullet-marker
line-start internally, so they can never straddle a split point.

Deterministic, pure Python, no LLM/API call, no `time.sleep` (delay is
reported as a number of milliseconds for the CALLER to apply — see
SegmentedReply.delay_ms — never slept inside this module, so it's safe
to call from a unit test without waiting in real time).
"""
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

DEFAULT_MAX_MESSAGES = 3
# Below this, an answer is already "one short conversational reply" — no
# structural benefit to splitting it, even if reply_mode allows it.
_SHORT_ANSWER_CHAR_THRESHOLD = 60

# None = 0ms, Short = 250-400ms, Natural = 450-900ms (task spec's ranges)
# — a single deterministic representative value per tier, not a random
# draw, so callers/tests get the exact same delay every time.
_DELAY_MS = {"none": 0, "short": 320, "natural": 650}

_BULLET_LINE_RE = re.compile(r"^\s*([-•*]|[🚚🚢✈️📦🚛]|\d+[.\)])\s+")
_CODE_BLOCK_MARKER = "```"
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$", re.MULTILINE)
# A later part that's ONLY the escalation/support hand-off sentence,
# already present in the first part, is dropped rather than repeated —
# "never repeat the support/contact sentence" (safety requirement 8).
_SUPPORT_SENTENCE_RE = re.compile(r"ติดต่อเจ้าหน้าที่|ทีมงานจะติดต่อกลับ")

# ── Section-Header Segmentation (P0, 2026-07-22) ────────────────────────
# GPT can legitimately produce a multi-section answer (explanation / sea
# rate / road rate / summary) using section-header LINES separated only
# by single newlines — no blank line, no bullet marker — which the
# paragraph-break and bullet-block checks above both miss entirely. This
# is a THIRD, independent structural signal: a short line that reads as
# a section title, never a specific hardcoded word list. Three generic
# header shapes, none tied to any one domain/topic:
#   - an emoji-prefixed short line ("🚢 ทางเรือ", "📦 สรุป", "⚠️ หมายเหตุ")
#   - a markdown heading ("## ทางเรือ", "### ทางรถ")
#   - a short bare label ending in a colon with nothing after it on the
#     same line ("ทางเรือ:", "สรุป:") — deliberately NOT a "Label: value"
#     line (e.g. "ราคา: 500 บาท"), which is ordinary content, not a
#     section boundary.
# A max length keeps this from ever matching a full sentence that merely
# happens to start with a symbol or end with a colon.
_EMOJI_HEADER_RE = re.compile(
    r"^[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U00002190-\U000021FF\U00002B00-\U00002BFF️]+\s*\S"
)
_MARKDOWN_HEADER_RE = re.compile(r"^#{1,6}\s+\S")
_COLON_ONLY_HEADER_RE = re.compile(r"^\S.{0,23}:$")
_MAX_HEADER_LINE_LEN = 40
# At least this many header lines must be present before this signal is
# trusted — a single header followed by prose is far more likely to be
# an incidental line than a genuine multi-section answer worth splitting.
_MIN_SECTION_HEADERS = 2


def _is_section_header_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped or len(stripped) > _MAX_HEADER_LINE_LEN:
        return False
    if _MARKDOWN_HEADER_RE.match(stripped):
        return True
    if _EMOJI_HEADER_RE.match(stripped):
        return True
    if _COLON_ONLY_HEADER_RE.match(stripped):
        return True
    return False


def _group_by_section_header(answer: str) -> List[str]:
    """Groups contiguous lines into sections, starting a NEW section at
    every recognized header line (an intro line before the first header,
    if any, becomes its own leading section). Requires at least
    _MIN_SECTION_HEADERS header lines to activate at all — this is what
    keeps a normal answer with a single incidental colon-ending or
    emoji-led line from being split for no reason, and keeps an address
    block (no header lines at all) from ever being split line-by-line."""
    lines = [l for l in answer.split("\n") if l.strip()]
    if len(lines) < 2:
        return [answer]
    header_count = sum(1 for l in lines if _is_section_header_line(l))
    if header_count < _MIN_SECTION_HEADERS:
        return [answer]

    groups: List[List[str]] = []
    for line in lines:
        if _is_section_header_line(line) or not groups:
            groups.append([line])
        else:
            groups[-1].append(line)
    return ["\n".join(g).strip() for g in groups if g]

# Natural Prose Message Segmentation — safe semantic-boundary markers for
# a SINGLE flowing paragraph (no blank lines, no bullet list) that still
# expresses more than one idea. A marker word STARTS a new segment (the
# text up to and including just before it becomes the previous segment).
# These are examples, not exhaustive — Thai has no reliable free
# sentence-boundary detector without a real tokenizer, so this stays a
# deliberately small, conservative marker-word list rather than guessing
# at clause boundaries generically.
_CONTRAST_MARKERS = ["ส่วน", "สำหรับ", "อีกกรณี", "แต่"]
# "สามารถ" ("can/able to") and "แจ้ง" ("inform") were removed: both are
# ordinary MID-SENTENCE verbs ("คุณสามารถวางลิงก์...", "รบกวนแจ้ง...") — a
# marker split before either one severs a subject from its predicate and
# produced fragments like a lone "...คุณ" bubble (OWNER-REAL-LINE-FIX-01).
# The sentence-boundary guard in _find_prose_boundaries is the general
# defence; this list stays to genuinely clause-initial words only.
_NEXT_STEP_MARKERS = ["หาก", "รบกวน"]

# a prose split may only land where the text BEFORE it actually ends a
# sentence — i.e. it closes with a Thai polite particle or sentence
# punctuation. Anything else is mid-sentence and is skipped.
_SENTENCE_END_RE = re.compile(r"(?:ค่ะ|คะ|ค่า|ครับ|คับ|นะคะ|นะครับ|จ้ะ|จ้า|นะ|ค่ะๆ|[.!?…])\s*$")
# A boundary this close to the previous one (or to the very start/end of
# the text) would produce a near-empty fragment that can't "make sense
# independently" on its own — merged away instead of kept as its own bubble.
_MIN_SEGMENT_CHARS = 12


@dataclass
class SegmentedReply:
    """The channel-safe interface a future LINE OA sender (or any other
    channel) can consume without this module knowing anything about
    LINE's SDK. `delay_ms[i]` is the gap to wait BEFORE sending
    `message_parts[i]` (delay_ms[0] is always 0 — the first bubble sends
    immediately)."""
    answer: str                      # canonical full text — citations/logs/analytics/Copy Answer
    message_parts: List[str]
    delay_ms: List[int]
    attachment_order: List[str]      # e.g. ["text", "attachment", "instruction"], [] if no attachments
    reply_mode_used: str             # single | multi | auto
    segmentation_applied: bool
    message_count: int
    segment_reasons: List[str] = field(default_factory=list)
    boundary_types: List[str] = field(default_factory=list)  # e.g. ["paragraph_break"], ["bullet_block"], ["prose_marker"]


def _attachment_order(has_attachments: bool) -> List[str]:
    """Default order (requirement 7): short text, then the attachment,
    then an optional final instruction. Metadata only — this module never
    touches attachment objects/URLs themselves, and never duplicates an
    attachment URL into more than one message part."""
    return ["text", "attachment", "instruction"] if has_attachments else []


def _contains_unsplittable_block(answer: str) -> bool:
    """A code block or a markdown table is never split — simplest safe
    guarantee: if either construct is present anywhere in the answer,
    the whole answer stays one message rather than risk splitting
    through the middle of one."""
    return _CODE_BLOCK_MARKER in answer or bool(_TABLE_ROW_RE.search(answer))


def _protected_spans_for_text(answer: str) -> List[Tuple[int, int]]:
    """Reuses rag/spell_correction.py's protected-entity regexes (URLs,
    emails, phone numbers, tracking/product codes, numbers/prices, known
    English abbreviations) — never a second, separately-maintained list.
    A prose split point is never allowed to fall inside one of these."""
    try:
        from rag.spell_correction import _protected_spans
        return _protected_spans(answer)
    except Exception:
        return []


def _find_prose_boundaries(answer: str) -> List[int]:
    """Start positions of every contrast/next-step marker occurrence that
    doesn't overlap a protected span, with boundaries too close together
    (or too close to the start/end of the text) merged away so no
    fragment shorter than _MIN_SEGMENT_CHARS is ever produced — that
    fragment couldn't "make sense independently" as its own bubble."""
    protected = _protected_spans_for_text(answer)
    raw_positions = []
    # CONTRAST markers ("ส่วน", "สำหรับ", …) legitimately split a compound
    # "A ส่วน B" sentence at its own midpoint, so they do NOT require a
    # preceding sentence end. NEXT-STEP markers ("หาก", "รบกวน") are
    # clause-initial: only accept one when the text before it actually
    # closes a sentence (a polite particle / punctuation) — otherwise the
    # split lands mid-sentence (OWNER-REAL-LINE-FIX-01, the "...คุณ" bubble).
    for marker in _CONTRAST_MARKERS + _NEXT_STEP_MARKERS:
        needs_sentence_end = marker in _NEXT_STEP_MARKERS
        start = 0
        while True:
            idx = answer.find(marker, start)
            if idx == -1:
                break
            # "ไม่สามารถ" ("cannot") is a NEGATION, not a next-step
            # marker — splitting there would separate "ไม่" from the verb
            # it negates, changing what the fragment means on its own.
            preceding = answer[max(0, idx - 3):idx]
            in_protected = any(s <= idx < e for s, e in protected)
            at_sentence_end = (not needs_sentence_end) or bool(_SENTENCE_END_RE.search(answer[:idx]))
            if not (in_protected or preceding.endswith("ไม่")) and at_sentence_end:
                raw_positions.append(idx)
            start = idx + len(marker)

    raw_positions.sort()
    boundaries: List[int] = []
    last = 0
    for pos in raw_positions:
        if pos - last < _MIN_SEGMENT_CHARS:
            continue  # too close to the previous boundary (or the start) — would create a tiny fragment
        boundaries.append(pos)
        last = pos
    if boundaries and (len(answer) - boundaries[-1]) < _MIN_SEGMENT_CHARS:
        boundaries.pop()  # the LAST segment would be a tiny trailing fragment
    return boundaries


def _split_prose(answer: str) -> List[str]:
    """Splits a single flowing paragraph at safe contrast/next-step
    marker boundaries — e.g. "...6,900 บาทต่อ CBM ส่วนทางเรือคิด..." splits
    right before "ส่วน". Every marker occurrence inside a protected span
    (a number, URL, phone number, tracking code, etc.) is skipped, so a
    split can never land inside one. Returns [answer] unchanged (no
    split) when no safe boundary is found."""
    boundaries = _find_prose_boundaries(answer)
    if not boundaries:
        return [answer]
    cut_points = [0] + boundaries + [len(answer)]
    segments = [answer[cut_points[i]:cut_points[i + 1]].strip() for i in range(len(cut_points) - 1)]
    segments = [s for s in segments if s]
    return segments if len(segments) > 1 else [answer]


def _has_separable_structure(answer: str) -> bool:
    """Auto mode only splits when the answer's own structure already has
    more than one idea — a blank-line paragraph break, a genuine
    multi-line bullet block distinct from surrounding plain text, at
    least two recognized section-header lines (emoji/markdown/colon —
    see _group_by_section_header), or (for a single flowing paragraph) a
    safe contrast/next-step marker boundary — see _split_prose(). A
    single flowing sentence with no such boundary stays as one bubble
    even if it's long."""
    if "\n\n" in answer:
        return True
    lines = [l for l in answer.split("\n") if l.strip()]
    bullet_lines = sum(1 for l in lines if _BULLET_LINE_RE.match(l))
    if len(lines) >= 2 and bullet_lines >= 2:
        return True
    if len(_group_by_section_header(answer)) > 1:
        return True
    return len(_split_prose(answer)) > 1


def _group_by_structure(answer: str) -> List[str]:
    """Groups contiguous lines by whether they're a bullet item or plain
    text — e.g. an intro sentence, then a multi-line rate list, then a
    closing sentence become 3 groups, each internally still in original
    line order (steps/rates are never reordered, only grouped)."""
    lines = [l for l in answer.split("\n") if l.strip()]
    groups: List[List[str]] = []
    current_is_bullet: Optional[bool] = None
    for line in lines:
        is_bullet = bool(_BULLET_LINE_RE.match(line))
        if current_is_bullet is None or is_bullet != current_is_bullet:
            groups.append([line])
            current_is_bullet = is_bullet
        else:
            groups[-1].append(line)

    bullet_group_count = sum(1 for g in groups if g and _BULLET_LINE_RE.match(g[0]) and len(g) >= 2)
    if bullet_group_count == 0:
        return [answer]
    return ["\n".join(g).strip() for g in groups if g]


def _split_into_parts(answer: str, max_messages: int) -> Tuple[List[str], List[str], List[str]]:
    reasons: List[str] = []
    boundary_types: List[str] = []

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", answer) if p.strip()]
    if len(paragraphs) > 1:
        boundary_types.append("paragraph_break")

    if len(paragraphs) <= 1:
        paragraphs = _group_by_structure(answer)
        if len(paragraphs) > 1:
            boundary_types.append("bullet_block")

    if len(paragraphs) <= 1:
        # Neither a paragraph break nor a bullet block — try Section-
        # Header Segmentation (emoji/markdown/colon header lines
        # separated only by single newlines, e.g. "🚢 ทางเรือ" / "🚚 ทางรถ"
        # with no blank line between them).
        paragraphs = _group_by_section_header(answer)
        if len(paragraphs) > 1:
            boundary_types.append("section_header")

    if len(paragraphs) <= 1:
        # Neither a paragraph break, a bullet block, nor section headers
        # — try Natural Prose Message Segmentation (contrast/next-step
        # marker words) on the single flowing paragraph.
        paragraphs = _split_prose(answer)
        if len(paragraphs) > 1:
            boundary_types.append("prose_marker")

    if len(paragraphs) <= 1:
        return [answer], [], []

    if len(paragraphs) > max_messages:
        # Prose-origin fragments read as one continuous sentence, so an
        # overflow merge joins them with a space; paragraph/bullet-origin
        # segments came from real blank lines, so a merge preserves that.
        join_str = " " if boundary_types == ["prose_marker"] else "\n\n"
        head = paragraphs[: max_messages - 1]
        tail = join_str.join(paragraphs[max_messages - 1:])
        paragraphs = head + [tail]
        reasons.append(f"merged extra segments to respect the {max_messages}-message limit")

    cleaned = [paragraphs[0]]
    for p in paragraphs[1:]:
        if _SUPPORT_SENTENCE_RE.search(p) and _SUPPORT_SENTENCE_RE.search(paragraphs[0]):
            continue  # never repeat the support/contact sentence across parts
        cleaned.append(p)
    paragraphs = cleaned or [answer]

    if len(paragraphs) >= 3:
        reasons.append("answer + details + next step")
    elif len(paragraphs) == 2:
        reasons.append("answer + additional detail")
    return paragraphs, reasons, boundary_types


def _single(answer: str, reply_mode: str, reasons: List[str]) -> SegmentedReply:
    parts = [answer] if answer else []
    return SegmentedReply(
        answer=answer, message_parts=parts, delay_ms=[0] if parts else [],
        attachment_order=[], reply_mode_used=reply_mode, segmentation_applied=False,
        message_count=len(parts), segment_reasons=reasons,
    )


def segment_message(
    answer: str,
    *,
    reply_mode: str = "auto",
    max_messages: int = DEFAULT_MAX_MESSAGES,
    message_delay: str = "natural",
    is_escalation: bool = False,
    is_fallback: bool = False,
    has_attachments: bool = False,
) -> SegmentedReply:
    """The one entry point. `reply_mode`/`max_messages`/`message_delay`
    normally come straight from services/policy_engine.py's
    get_messaging_settings(); `is_escalation`/`is_fallback`/
    `has_attachments` come from the caller's already-decided state
    (policy.escalate, answerability == "no_information", whether any
    chunk carried an attachment) — this module never re-derives them.

    Safety rules enforced here (never overridden by reply_mode):
      - reply_mode == "single" -> always exactly 1 part.
      - is_escalation -> always exactly 1 part (never split a hand-off message).
      - is_fallback -> always exactly 1 part (never split "no information" text).
      - a code block or markdown table anywhere in the answer -> 1 part.
      - a short answer (<= ~60 chars, single line) -> 1 part.
      - "auto" mode additionally requires the answer to already HAVE
        separable structure (a paragraph break or a real bullet block) —
        a single long flowing paragraph stays as one bubble even in auto.
    """
    answer = (answer or "").strip()
    max_messages = max(1, min(3, int(max_messages or DEFAULT_MAX_MESSAGES)))

    if not answer:
        return _single(answer, reply_mode, ["empty answer"])

    if reply_mode == "single":
        return _single(answer, "single", ["Single Message policy"])

    if is_escalation:
        return _single(answer, reply_mode, ["escalation responses are never split"])

    if is_fallback:
        return _single(answer, reply_mode, ["fallback/no-information responses are never split"])

    if _contains_unsplittable_block(answer):
        return _single(answer, reply_mode, ["answer contains a code block or table — kept as one message"])

    if len(answer) <= _SHORT_ANSWER_CHAR_THRESHOLD and "\n" not in answer:
        return _single(answer, reply_mode, ["answer is short — no split needed"])

    if reply_mode == "auto" and not _has_separable_structure(answer):
        return _single(answer, "auto", ["answer has no clearly separable structure"])

    parts, reasons, boundary_types = _split_into_parts(answer, max_messages)
    if len(parts) <= 1:
        return _single(answer, reply_mode, reasons or ["no safe split point found"])

    delay = _DELAY_MS.get(message_delay, _DELAY_MS["natural"])
    delay_ms = [0] + [delay] * (len(parts) - 1)

    return SegmentedReply(
        answer=answer, message_parts=parts, delay_ms=delay_ms,
        attachment_order=_attachment_order(has_attachments), reply_mode_used=reply_mode,
        segmentation_applied=True, message_count=len(parts), segment_reasons=reasons,
        boundary_types=boundary_types,
    )
