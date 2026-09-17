"""Deterministic (no LLM call) follow-up query resolution — runs BEFORE
retrieval, using ONLY prior USER turns from `history`, never a prior
ASSISTANT answer.

Conversation Resolver 2.0: extends the original "แล้ว...ล่ะ/ละ"-only
pattern matcher (kept below as `_legacy_resolve`, still used as a
fallback tier for topics outside the known entity vocabulary) with
lightweight conversation ENTITY TRACKING — topic / location / transport
method / requested attribute — extracted from prior USER turns only,
so a much wider range of short, incomplete follow-ups ("แล้วรถล่ะ",
"กี่วัน", "แล้วจีน", "มีแผนที่ไหม", ...) resolve into a standalone
question, not just the one narrow "แล้ว...ล่ะ" wrapper shape.

Reuses rag/synonym_service.py's synonym groups as the topic vocabulary
(never a second hardcoded word list) — a group's canonical term or any
of its synonyms appearing in a turn's text marks that group's canonical
term as the "topic" entity. The "location" (map/พิกัด) group is
deliberately EXCLUDED from topic detection: "แผนที่"/"พิกัด"/"โลเคชั่น"
indicate the ATTRIBUTE being asked about (where is X), never a subject
of their own — conflating the two would make "มีแผนที่ไหม" register a
topic and never carry forward the actual entity (the warehouse) it's
asking about.

The current user message always has the highest priority: every entity
it specifies itself is used as-is; only entities it OMITS are filled in
from the tracked conversation state. A question with no recognized
follow-up marker at all (no "แล้ว" wrapper, not one of the short bare
follow-up phrases, and not a short entity-only fragment) is returned
completely unchanged — this is what keeps an unrelated new topic from
ever inheriting stale entities from an earlier part of the conversation.
"""
import re
from typing import Dict, List, Optional, Tuple

# "แล้ว<core>ล่ะ" / "แล้ว<core>ละ" — the standard Thai "and what about X"
# follow-up pattern. <core> is what actually varies between turns.
_FOLLOWUP_RE = re.compile(r"^แล้ว(?P<core>.+?)(ล่ะ|ละ)$")

# Filler verbs that precede the real topic in a request — stripped from
# BOTH the previous question and (defensively) the follow-up core, never
# treated as part of the topic itself.
_FILLER_PREFIXES = ["ขอ", "อยาก", "ช่วย", "รบกวน"]

# Trailing question-word suffixes — whichever one the PREVIOUS question
# used is reused for the resolved query, so the resolved question keeps
# asking the same KIND of thing ("...เท่าไหร่" / "...ยังไง" / ...), not just
# the same topic noun. Used only by the legacy fallback tier.
_QUESTION_SUFFIXES = ["เกี่ยวกับอะไร", "เท่าไหร่", "เท่าไร", "ยังไง", "อย่างไร", "กี่บาท",
                       "ได้ไหม", "บ้าง", "อะไร", "ไหม"]
_DEFAULT_SUFFIX = "เท่าไหร่"

# Generic transport-mode markers — a follow-up core that is ONLY one of
# these (e.g. "ทางเรือ") has no head noun of its own ("rate", "delivery
# time", etc.) and must borrow one from the previous question's topic.
_MODE_MARKER_RE = re.compile(r"ทาง(เรือ|รถ|อากาศ|เครื่องบิน|ราง)")


def _strip_prefix(text: str) -> str:
    for p in _FILLER_PREFIXES:
        if text.startswith(p):
            return text[len(p):]
    return text


def _strip_suffix(text: str) -> Tuple[str, Optional[str]]:
    for s in _QUESTION_SUFFIXES:
        if text.endswith(s):
            return text[: -len(s)], s
    return text, None


def _last_user_question(history: Optional[List[Dict]]) -> Optional[str]:
    """The most recent prior USER turn's content — assistant turns are
    never inspected, let alone reused, anywhere in this module."""
    if not history:
        return None
    for turn in reversed(history):
        if turn.get("role") == "user":
            content = (turn.get("content") or "").strip()
            if content:
                return content
    return None


def _legacy_resolve(core: str, prev_q: str) -> Optional[str]:
    """The original (pre-2.0) resolution: strip a filler prefix/known
    question suffix off the previous question, substitute a bare
    transport-mode marker if that's all `core` is, and reuse the
    previous question's own trailing question word. Kept as a fallback
    tier for any topic outside the entity vocabulary below (e.g. a
    domain word with no synonym group / attribute pattern registered for
    it yet) — still fully functional and still exercised whenever the
    entity-based tier can't compose a specific template."""
    if not core or not prev_q:
        return None
    prev_core, prev_suffix = _strip_suffix(_strip_prefix(prev_q.strip()))
    mode_match = _MODE_MARKER_RE.fullmatch(core)
    if mode_match:
        prev_mode_match = _MODE_MARKER_RE.search(prev_core)
        head = prev_core[: prev_mode_match.start()] if prev_mode_match else prev_core
        resolved_head = head + core
    else:
        resolved_head = core
    suffix = prev_suffix or _DEFAULT_SUFFIX
    return resolved_head + suffix


# ── Conversation Resolver 2.0 — entity tracking ─────────────────────────

# Substring False-Positive fix (2026-08-31) — confirmed live: "รถ" (car/
# land transport) is a bare 2-character match with no word-boundary
# protection (Thai has no spaces to anchor on), so it was matching INSIDE
# "สามารถ" (a completely ordinary word meaning "can/able to" — สา-มา-รถ —
# used constantly in normal conversation, e.g. "เราสามารถสั่ง...ได้ไหม").
# This silently set entities["transport"]="รถ" for any question merely
# containing "สามารถ", which then fed a real canonical-query REWRITE
# (rag/canonical_query.py, gated on "rate + transport detected") that
# replaced an entirely unrelated question ("เราสามารถสั่งแบตเตอรี่จำนวน
# เยอะได้ไหมคะ") with a completely different one ("อัตราค่าขนส่งทางรถ
# เท่าไหร่") — the customer's own question was never actually searched at
# all. The negative lookbehind excludes ONLY this specific false-positive
# shape ("สามา" immediately before "รถ"); a genuine transport mention
# ("รถยนต์", "ทางรถ", "ส่งรถ") is never preceded by those exact 4
# characters and is completely unaffected.
_TRANSPORT_RE = re.compile(r"เครื่องบิน|(?<!สามา)รถ|เรือ|อากาศ")
_TRANSPORT_CANONICAL = {"เครื่องบิน": "อากาศ", "รถ": "รถ", "เรือ": "เรือ", "อากาศ": "อากาศ"}
_LOCATION_RE = re.compile(r"ไทย|จีน")

# Checked in this order — duration/location markers are more distinctive
# than the generic rate markers (a bare "เท่าไหร่" is common to both a
# price question AND could otherwise be ambiguous), so they're resolved
# first to avoid a duration/location question being misread as a rate one.
_DURATION_RE = re.compile(r"กี่วัน|ระยะเวลา|นานแค่ไหน")
_LOCATION_ATTR_RE = re.compile(r"แผนที่|โลเคชั่น|พิกัด|ที่อยู่|อยู่ไหน|ที่นี่|google\s*map|gps", re.IGNORECASE)
# Contact/phone as a REQUESTED ATTRIBUTE (2026-09-01) — so an elliptical
# follow-up preserves the "phone/contact" subject, not just the topic:
# "ขอเบอร์โกดัง" -> "ไทย" -> "แล้วจีนล่ะ" must reconstruct to
# "ขอเบอร์ติดต่อโกดังจีน" (China + warehouse + CONTACT), never fall back
# to the warehouse ADDRESS. Checked before _RATE_RE so a "เบอร์…เท่าไหร่"
# phrasing is not misread as a rate question.
_CONTACT_ATTR_RE = re.compile(r"เบอร์|โทรศัพท์|ติดต่อ|contact|call\s*center", re.IGNORECASE)
_RATE_RE = re.compile(r"เรท|ราคา|ค่าขนส่ง|ค่าส่ง|อัตราค่าขนส่ง|เท่าไหร่|เท่าไร|กี่บาท")

# Short, content-free follow-up phrases — resolved purely by carrying
# forward whichever entities they don't specify themselves. Listed
# explicitly (task requirement), on top of the generic short/bare-entity
# detection below (which covers phrasings not in this exact list, e.g.
# "แล้วจีน").
_BARE_MARKER_WORDS = {
    "อันนี้ล่ะ", "อันนั้นล่ะ", "อีกอัน", "ที่นี่", "ที่เดิม", "อันเดิม",
    "แล้วอันเมื่อกี้", "กี่วัน", "เท่าไหร่", "ทางรถ", "ทางเรือ",
}
# A short question is never treated as a bare/ambiguous follow-up
# fragment once it's longer than this — long enough to almost certainly
# be a genuinely new, complete question on its own.
_BARE_LENGTH_LIMIT = 12

# Synonym groups (rag/synonym_service.py) that represent a REQUESTED
# ATTRIBUTE rather than a subject/topic of their own — "แผนที่"/"พิกัด"/
# "โลเคชั่น" describe WHERE something is, never a standalone thing being
# discussed. Excluded from topic detection so "มีแผนที่ไหม" doesn't
# register "location" as its topic and fail to carry forward the actual
# subject (e.g. "โกดัง") it needs from the conversation.
_ATTRIBUTE_ONLY_GROUPS = {"location"}


def _extract_attribute(text: str) -> Optional[str]:
    if _DURATION_RE.search(text):
        return "duration"
    if _CONTACT_ATTR_RE.search(text):
        return "contact"
    if _LOCATION_ATTR_RE.search(text):
        return "location"
    if _EXISTENCE_RE.search(text) and _extract_topic(text) == "โกดัง":
        # "มีโกดังไหม"-style existence question about the warehouse
        # topic itself — narrowly scoped to "โกดัง" so an unrelated
        # existence question (e.g. "มีคูปองไหม") never misreads as a
        # location attribute. _EXISTENCE_RE is defined later in this
        # module (near _compose) — safe because this reference is
        # resolved at call time, by which point the module has fully
        # loaded, not at this function's definition time.
        return "location"
    if _RATE_RE.search(text):
        return "rate"
    return None


def _extract_transport(text: str) -> Optional[str]:
    m = _TRANSPORT_RE.search(text)
    return _TRANSPORT_CANONICAL[m.group(0)] if m else None


def requested_transport_modes(text: str) -> List[str]:
    """The SET of transport modes the given wording EXPLICITLY names,
    canonicalised — ``[]`` (generic / none), ``["รถ"]``, ``["เรือ"]``,
    ``["อากาศ"]``, or a combination.

    The single source of truth for "which transport facet(s) did the
    customer ask about this turn". Every rate/duration path that used to
    re-derive this — ``_compose`` here, ``rag/canonical_query.py``, and
    ``services/answer_planner.py`` — reads THIS instead of inspecting a
    first-match scalar or doing its own substring check, so those layers
    can never disagree about it again. Same ``_TRANSPORT_RE`` /
    ``_TRANSPORT_CANONICAL`` and same negation stripping as
    ``extract_entities()``; ``_extract_transport()`` stays as the
    first-match scalar accessor its own existing callers still expect."""
    seen: List[str] = []
    for m in _TRANSPORT_RE.finditer(strip_negated_spans(text or "")):
        canon = _TRANSPORT_CANONICAL[m.group(0)]
        if canon not in seen:
            seen.append(canon)
    return seen


def _extract_location(text: str) -> Optional[str]:
    m = _LOCATION_RE.search(text)
    return m.group(0) if m else None


# ── Negation-aware entity extraction ────────────────────────────────
# Phrases like "ไม่ใช่จีน"/"ไม่เอาจีน"/"ไม่ต้องการจีน"/"ยกเว้นจีน" name an
# entity ONLY to exclude it — "จีน" there is NEGATIVE evidence, never a
# positive location/transport mention. Both the positive extractors above
# (_extract_location/_extract_transport, via extract_entities) and rag/
# hybrid_scoring.py's keyword scoring (imports strip_negated_spans()
# directly — never a second copy of this logic) must never see a negated
# word as a hit.
_NEGATION_MARKERS = ["ไม่ใช่", "ไม่เอา", "ไม่ต้องการ", "ยกเว้น"]
# How far past a negation marker to look for the entity it negates — long
# enough for "ไม่ใช่ประเทศจีน", short enough to never swallow the rest of
# an unrelated sentence.
_NEGATION_WINDOW_CHARS = 12


def strip_negated_spans(text: str) -> str:
    """Blanks out (replaces with spaces, preserving length/offsets so
    this is safe to use as a pre-pass before any other regex) every
    negation-marker-plus-following-window span. What's left still has
    every POSITIVE mention intact (e.g. "ในประเทศไทย" earlier in the same
    sentence), so downstream extractors only ever see genuinely positive
    evidence."""
    result = text
    for marker in _NEGATION_MARKERS:
        idx = 0
        while True:
            pos = result.find(marker, idx)
            if pos == -1:
                break
            end = min(len(result), pos + len(marker) + _NEGATION_WINDOW_CHARS)
            result = result[:pos] + (" " * (end - pos)) + result[end:]
            idx = end
    return result


def extract_excluded_entities(text: str) -> Dict[str, List[str]]:
    """Returns {"location": ["จีน", ...], "transport": ["เรือ", ...]} —
    every location/transport word found INSIDE a negated span (see
    strip_negated_spans). Operates on the ORIGINAL text (never the
    stripped version — this is what needs to find what got stripped)."""
    excluded: Dict[str, List[str]] = {"location": [], "transport": []}
    for marker in _NEGATION_MARKERS:
        idx = 0
        while True:
            pos = text.find(marker, idx)
            if pos == -1:
                break
            window = text[pos + len(marker): pos + len(marker) + _NEGATION_WINDOW_CHARS]
            loc_match = _LOCATION_RE.search(window)
            if loc_match and loc_match.group(0) not in excluded["location"]:
                excluded["location"].append(loc_match.group(0))
            tr_match = _TRANSPORT_RE.search(window)
            if tr_match:
                canon = _TRANSPORT_CANONICAL[tr_match.group(0)]
                if canon not in excluded["transport"]:
                    excluded["transport"].append(canon)
            idx = pos + len(marker)
    return excluded


def _extract_topic(text: str) -> Optional[str]:
    """The first synonym group (excluding attribute-only ones — see
    _ATTRIBUTE_ONLY_GROUPS) whose canonical term or any synonym appears
    in `text`. Reuses rag/synonym_service.py's data — never a second,
    separately-maintained topic word list."""
    try:
        from rag.synonym_service import get_synonym_groups
        groups = get_synonym_groups()
    except Exception:
        return None
    lower = text.lower()
    for group in groups:
        if group.get("id") in _ATTRIBUTE_ONLY_GROUPS:
            continue
        candidates = [group["canonical_term"]] + list(group.get("synonyms") or [])
        if any(c and c.lower() in lower for c in candidates):
            return group["canonical_term"]
    return None


def extract_entities(text: str) -> Dict[str, Optional[str]]:
    """Positive entities only — a location/transport word inside a
    negated span (e.g. "จีน" in "ที่ไม่ใช่จีน") is stripped BEFORE
    extraction runs, so it can never register as a positive location/
    transport entity. Use extract_excluded_entities() alongside this for
    the negative side."""
    cleaned = strip_negated_spans(text)
    return {
        "topic": _extract_topic(cleaned),
        "transport": _extract_transport(cleaned),
        "location": _extract_location(cleaned),
        "attribute": _extract_attribute(text),  # attribute markers (มี.../ไหม) are never negation targets themselves
    }


def accumulate_entities(history: Optional[List[Dict]]) -> Dict[str, Optional[str]]:
    """Walks every prior USER turn in order (oldest first) and keeps
    OVERWRITING each entity slot as a later turn re-specifies it — the
    result is the "last known value" per slot, e.g. two prior turns
    mentioning transport อากาศ then เรือ end up with transport=เรือ,
    matching "only the most recent turn should win for a given slot."
    Assistant turns are never inspected.

    Public (not underscore-prefixed) so rag/conversation_state.py can
    reuse this exact accumulation instead of a second copy — it needs
    the conversation's last-known entity values even for a turn whose
    own wording doesn't match resolve_conversation()'s follow-up-marker
    detection (e.g. "ขอเบอร์" alone isn't recognized as a follow-up
    marker, but should still inherit the active warehouse/location while
    the topic itself hasn't changed)."""
    entities: Dict[str, Optional[str]] = {"topic": None, "transport": None, "location": None, "attribute": None}
    if not history:
        return entities
    for turn in history:
        if turn.get("role") != "user":
            continue
        content = (turn.get("content") or "").strip()
        if not content:
            continue
        for key, value in extract_entities(content).items():
            if value:
                entities[key] = value
    return entities


# Backward-compatible alias — every existing call site in this module
# keeps using the underscore-prefixed name unchanged.
_accumulate_entities = accumulate_entities


# Contrastive follow-up markers that DON'T necessarily start with "แล้ว"
# — "ฝั่งไทยมีไหม" (what about the Thai side), "ของไทยล่ะ" (the Thai one),
# "อีกประเทศล่ะ" (the other country), or any negation marker (a negated
# entity is itself strong evidence this turn is contrasting against
# something the conversation already established).
_CONTRASTIVE_MARKER_RE = re.compile(
    r"อีกประเทศ|ฝั่ง(ไทย|จีน)|ของ(ไทย|จีน)|" + "|".join(re.escape(m) for m in _NEGATION_MARKERS)
)


def _detect_marker(normalized: str) -> Optional[str]:
    """Returns a short label describing WHICH follow-up shape matched, or
    None if `normalized` looks like a complete, standalone question with
    no follow-up wrapper at all (in which case it must never be touched
    — "current user message always has highest priority")."""
    if _FOLLOWUP_RE.match(normalized):
        return "แล้ว...ล่ะ (contrastive)" if _CONTRASTIVE_MARKER_RE.search(normalized) else "แล้ว...ล่ะ"
    if normalized in _BARE_MARKER_WORDS:
        return f"bare:{normalized}"
    if _is_meta_followup(normalized):
        # No length cap here (unlike the bare-entity fallback below) —
        # "ช่วยสรุปข้อมูลบริษัทให้หน่อย" is deliberately a full sentence,
        # not a short fragment; _is_meta_followup() itself is the guard
        # against misfiring on a genuinely new, self-contained question.
        # Checked in this order — a message could plausibly contain more
        # than one marker family; summary/detail take precedence since
        # they were established first and this ordering preserves their
        # exact prior behavior unchanged.
        if _META_SUMMARY_MARKER_RE.search(normalized):
            return "meta-summary-followup"
        if _META_DETAIL_MARKER_RE.search(normalized):
            return "meta-detail-followup"
        if _META_SIMPLIFY_MARKER_RE.search(normalized):
            return "meta-simplify-followup"
        return "meta-partial-followup"
    if normalized.startswith("แล้ว"):
        # A "แล้ว" prefix is ALREADY a deliberate, strong follow-up
        # signal on its own — no length cap here (unlike the bare-entity
        # fallback below), since a longer contrastive sentence like
        # "แล้วในประเทศไทยล่ะ มีไหมที่ไม่ใช่จีนอ่ะ" is exactly the shape this
        # needs to catch, not exclude.
        return ("แล้ว<word> (contrastive)" if _CONTRASTIVE_MARKER_RE.search(normalized) else "แล้ว<word>")
    if _CONTRASTIVE_MARKER_RE.search(normalized) and len(normalized) <= _BARE_LENGTH_LIMIT + 10:
        return "contrastive marker"
    if len(normalized) <= _BARE_LENGTH_LIMIT and not _extract_topic(normalized):
        if _extract_attribute(normalized) or _extract_transport(normalized) or _extract_location(normalized):
            return "bare-entity"
    # Bare NOUN + "ล่ะ/ละ" with no "แล้ว" wrapper — "น้ำหอมล่ะ", "แชมพูล่ะ"
    # (P1.2A product follow-up). Only ever acted on when the immediately
    # preceding USER turn is an import-eligibility question (guarded in
    # resolve_conversation) — otherwise returned completely unchanged, so
    # ordinary chit-chat ending in "ละ" is never rewritten.
    if re.search(r"(ล่ะ|ละ)$", normalized) and len(normalized) <= _BARE_LENGTH_LIMIT + 6:
        return "bare-suffix-followup"
    return None


# ── Meta follow-up ("summarize/detail what we just discussed") ─────────
# Customer-demo P0 fix (2026-07-20): a follow-up like "ช่วยสรุปข้อมูล
# บริษัทให้หน่อย" / "ขอสรุปอีกที" / "ขอแบบละเอียด" never names a NEW topic
# of its own (no registered topic/attribute/transport/location entity) —
# it's an instruction about HOW to answer (summarize / give more detail /
# again), referring back to whatever the PREVIOUS USER QUESTION was
# about. This is deliberately separate from the entity-composition tier
# above (_compose): it never touches or reuses a prior ASSISTANT answer
# as evidence — only the prior USER question's own wording is reused, as
# a standalone retrieval query, matching the requirement that retrieval
# stay grounded in real KB content rather than treating the AI's own
# earlier answer as fact.
_META_SUMMARY_MARKER_RE = re.compile(r"สรุป|อีกที|ทบทวน")
_META_DETAIL_MARKER_RE = re.compile(r"ละเอียด|ขยายความ|เพิ่มเติม")

# Final Blocker B fix (2026-08-29) — the SAME "instruction about HOW to
# answer, not a new topic" concept as the summary/detail markers above,
# extended to two more generic shapes confirmed live to reach retrieval
# with no topic of their own, pulling unrelated chunks purely because the
# active conversation's own substantive question was never carried
# forward: (1) a request to explain more SIMPLY ("ช่วยอธิบายแบบง่ายๆได้ไหม"
# right after a genuine question, e.g. "ฝากสั่งกับฝากนำเข้าต่างกันยังไง"),
# and (2) a request to answer with whatever's AVAILABLE/PARTIAL ("ถ้า
# ข้อมูลบางส่วนยังไม่มี ช่วยบอกเท่าที่ทราบได้ไหม"). Neither names its own
# topic/attribute/transport/location entity, exactly like สรุป/ละเอียด —
# reuses the identical _is_meta_followup gate, never a new mechanism.
_META_SIMPLIFY_MARKER_RE = re.compile(r"ง่ายๆ|ง่าย ๆ|เข้าใจง่าย")
_META_PARTIAL_MARKER_RE = re.compile(r"เท่าที่ทราบ|เท่าที่มี|เท่าที่รู้|บางส่วน")


def _is_meta_followup(text: str) -> bool:
    """True when `text` carries a summarize/detail/simplify/partial-info-
    again instruction but introduces no new topic/attribute/transport/
    location entity of its own — i.e. it can only be answered by reusing
    whatever the conversation already established, never as a complete
    question in isolation. A question that both uses one of these words
    AND names its own concrete subject (e.g. "สรุปนโยบายการคืนสินค้า") is
    NOT a meta follow-up — it's a fresh, self-contained question and must
    be left untouched, same as any other standalone question."""
    if not (_META_SUMMARY_MARKER_RE.search(text) or _META_DETAIL_MARKER_RE.search(text)
            or _META_SIMPLIFY_MARKER_RE.search(text) or _META_PARTIAL_MARKER_RE.search(text)):
        return False
    entities = extract_entities(text)
    if any(entities.get(k) for k in ("transport", "location", "attribute")):
        return False
    topic = entities.get("topic")
    # "บริษัท" (the company synonym group added for the company-overview
    # retrieval fix) is a GENERIC self-reference to whatever company is
    # already being discussed, never a new distinguishing subject the
    # way โกดัง/เรท/CBM/etc. are — it must not block meta-followup
    # detection. Any OTHER registered topic still blocks it, since that
    # genuinely names a new subject the current turn introduced itself.
    return not topic or topic == "บริษัท"


_EXISTENCE_RE = re.compile(r"มี.*ไหม")

# P7 — general-knowledge elliptical follow-up. The previous USER turn is a
# self-contained "<subject> <predicate> <interrogative>" world-knowledge
# question ("จีนอยู่ทวีปอะไร", "กรุงเทพอยู่ภาคอะไร") and the current turn is
# a bare "แล้ว <noun> ล่ะ" — swap ONLY the leading subject, keep the whole
# predicate + interrogative so it stays the SAME kind of general question
# about a different subject. Never fires for a Shipify/company-context
# prior turn (that has its own resolver branches / General Chat gate).
_GENERAL_SUBJECT_SWAP_RE = re.compile(
    r"^\s*\S{1,20}?\s*(?P<rest>(?:ตั้งอยู่|อยู่|คือ|เป็น|มี)[^\n]{0,32}?"
    r"(?:อะไร|ที่ไหน|ไหน|กี่[ก-๙]{0,10}|เท่าไหร่|เท่าไร|ยังไง|อย่างไร)\s*(?:คะ|ครับ|ค่ะ)?)\s*$")
_SHIPIFY_CONTEXT_RE = re.compile(
    r"โกดัง|ขนส่ง|นำเข้า|ฝากสั่ง|คูปอง|บิล|ใบกำกับ|ภาษี|ศุลกากร|พัสดุ|ตีลัง|แพ็ก|"
    r"ค่าส่ง|ค่าขนส่ง|ค่าบริการ|เรท|ทางรถ|ทางเรือ|shipify|fasttrade|taobao|1688|tmall",
    re.IGNORECASE)


def _compose(merged: Dict[str, Optional[str]], core: str, prev_q: Optional[str]) -> Tuple[Optional[str], float]:
    """Specific, natural-phrasing templates for the attribute/transport/
    location combinations the task's own examples cover, tried first;
    falls back to `_legacy_resolve` (generic mode-marker + suffix-reuse)
    for any topic outside this vocabulary, so nothing that used to
    resolve stops resolving."""
    attribute = merged.get("attribute")
    transport = merged.get("transport")
    location = merged.get("location")
    topic = merged.get("topic")

    if attribute == "rate" and transport:
        return f"ขอเรททาง{transport}", 0.9
    if attribute == "duration" and transport:
        return f"ขอทราบระยะเวลาขนส่งทาง{transport}", 0.9
    if attribute == "contact" and topic == "โกดัง":
        # Subject preservation for an elliptical warehouse-phone follow-up
        # ("ขอเบอร์โกดัง" -> "ไทย" -> "แล้วจีนล่ะ"). Keeps "เบอร์ติดต่อ" as
        # the subject but pairs it with "ที่อยู่": a warehouse's contact
        # info lives in the SAME trusted FAQ row as its address (the Thai
        # rows carry the phone inline; the China row's contact route is
        # its website menu), so this phrasing retrieves that row and
        # answers with whatever contact info exists — instead of the
        # phone-only phrasing being rejected as "no information".
        return (f"ขอเบอร์ติดต่อและที่อยู่โกดัง{location}" if location
                else "ขอเบอร์ติดต่อและที่อยู่โกดัง"), 0.85
    if attribute == "location" and topic == "โกดัง":
        if location and _EXISTENCE_RE.search(core):
            # "มีไหม" (existence question) — e.g. "แล้วในประเทศไทยล่ะ มีไหม
            # ที่ไม่ใช่จีนอ่ะ" -> "มีโกดังในประเทศไทยไหม และอยู่ที่ไหน", never
            # keeping the negated entity in this POSITIVE search query.
            phrase = f"ประเทศ{location}" if f"ประเทศ{location}" in core else location
            return f"มีโกดังใน{phrase}ไหม และอยู่ที่ไหน", 0.9
        return (f"ขอที่อยู่โกดัง{location}" if location else "ขอที่อยู่โกดัง"), 0.85
    if attribute == "location" and location:
        return f"ขอแผนที่{location}", 0.7

    # Product-eligibility follow-up (P1.2A) — "แบตเตอรี่นำเข้าได้ไหม" then
    # "แล้วแชมพูล่ะ" / "น้ำหอมล่ะ": the previous USER turn is an
    # import-eligibility question and `core` is a bare product noun with no
    # recognised attribute/transport/location of its own — carry the
    # eligibility intent forward. `_ELIGIBILITY_INTENT_RE` is defined later
    # in this module (resolved at call time, module fully loaded).
    if (prev_q and _ELIGIBILITY_INTENT_RE.search(prev_q)
            and not (attribute or transport or location)
            and 1 <= len(core) <= 20):
        return f"{core}นำเข้าได้ไหม", 0.8

    # P7 — general-knowledge elliptical follow-up ("จีนอยู่ทวีปอะไร" ->
    # "แล้วญี่ปุ่นล่ะ" -> "ญี่ปุ่นอยู่ทวีปอะไร"). Fires only when the prior
    # turn is a NON-Shipify "<subject> <predicate> <interrogative>"
    # question and this fragment names no attribute/transport/topic of its
    # own (a carried location word like "จีน" from the prior turn is fine).
    if (prev_q and not (attribute or transport or topic)
            and not _SHIPIFY_CONTEXT_RE.search(prev_q)):
        _noun = re.sub(r"(ล่ะ|ละ|ครับ|ค่ะ|คะ|นะ|น่ะ|อ่ะ|อะ)+$", "", core).strip()
        gm = _GENERAL_SUBJECT_SWAP_RE.match(prev_q.strip())
        if gm and 1 <= len(_noun) <= 25 and not re.search(
                r"อะไร|ไหน|กี่|เท่าไหร่|ยังไง|อย่างไร|ไหม|\?", _noun):
            return f"{_noun}{gm.group('rest')}", 0.75

    legacy = _legacy_resolve(core, prev_q) if prev_q else None
    if legacy:
        return legacy, 0.6
    return None, 0.0


def _empty_result(resolved_question: str, followup_type: Optional[str], confidence: float,
                   prev_topic: Optional[str], excluded_entities: Optional[Dict] = None) -> Dict:
    return {
        "resolved_question": resolved_question, "followup_type": followup_type,
        "entities_carried": {}, "confidence": confidence, "prev_topic": prev_topic,
        "excluded_entities": excluded_entities or {"location": [], "transport": []},
        "replaced_entities": {},
    }


def resolve_conversation(question: str, history: Optional[List[Dict]] = None) -> Dict:
    """The full Conversation Resolver 2.0 result — everything
    Explainability needs:
        {
          "resolved_question": str,
          "followup_type": Optional[str],   # None when no follow-up marker matched at all
          "entities_carried": {slot: value, ...},  # only entities NOT already in the current message
          "confidence": float,
          "prev_topic": Optional[str],
          "excluded_entities": {"location": [...], "transport": [...]},  # negated this turn — never positive evidence
          "replaced_entities": {slot: {"from": old, "to": new}, ...},    # a carried entity the CURRENT turn overrode
        }
    High confidence (>= 0.7) means a specific entity template matched;
    the legacy fallback tier reports 0.6 (still applied, just less
    certain); below 0.5 the original question is returned unchanged —
    "ambiguous -> keep original and allow clarification."

    Entity replacement: when the current turn names a NEW value for a
    slot the conversation already had (e.g. prev location=จีน, current
    names ไทย), the current value always wins (never both carried
    forward) — recorded in `replaced_entities` so Explainability can show
    exactly what changed. A location/transport mentioned only inside a
    negation ("ที่ไม่ใช่จีน") is NEVER treated as the new positive value
    for that slot — see extract_entities()/extract_excluded_entities().
    """
    original = question or ""
    prev_entities = _accumulate_entities(history)
    prev_topic = prev_entities.get("topic")

    if not original:
        return _empty_result(original, None, 1.0, prev_topic)

    normalized = re.sub(r"\s+", " ", original.strip())
    normalized = re.sub(r"[?!.]+$", "", normalized)

    # Clarification State Engine (P0, 2026-07-22, rag/clarification_state.py)
    # — checked BEFORE the generic follow-up marker detection below, since
    # a reply to the assistant's OWN immediately previous clarification
    # question must never be resolved via the generic entity-carrying
    # machinery, which accumulates entities across the ENTIRE history
    # with no flow-scoping and could pull in a stale entity from an
    # earlier, unrelated, already-completed flow. This step only ever
    # uses the ORIGINAL user question that triggered the clarification —
    # never `prev_entities`/the rest of `history` — so the user's
    # original intent (e.g. contact_information) can never be replaced
    # by a different, unrelated intent (e.g. shipping_duration).
    from rag.clarification_state import resolve_clarification_answer
    clarification = resolve_clarification_answer(normalized, history)
    if clarification:
        return {
            "resolved_question": clarification["resolved_question"], "followup_type": "clarification-answer",
            "entities_carried": {}, "confidence": clarification["confidence"], "prev_topic": prev_topic,
            "excluded_entities": {"location": [], "transport": []}, "replaced_entities": {},
        }

    marker = _detect_marker(normalized)
    if marker is None:
        # A complete, standalone question on its own — never touched,
        # and never allowed to inherit unrelated prior-turn entities.
        return _empty_result(original, None, 1.0, prev_topic)

    prev_q = _last_user_question(history)
    if not prev_q:
        return _empty_result(original, marker, 1.0, prev_topic)

    if marker == "bare-suffix-followup" and not (prev_q and _ELIGIBILITY_INTENT_RE.search(prev_q)):
        # Not a product-eligibility follow-up context — leave the message
        # exactly as typed (followup_type None), never guess a rewrite.
        return _empty_result(original, None, 1.0, prev_topic)

    if marker in ("meta-summary-followup", "meta-detail-followup",
                  "meta-simplify-followup", "meta-partial-followup"):
        # Reuses ONLY the previous USER question's own wording — never
        # the assistant's prior answer — as the subject to summarize/
        # detail/simplify/partially-answer, per the "never use previous
        # AI answers as evidence" requirement. _strip_suffix/_strip_prefix
        # are the SAME helpers the legacy tier already uses, so this stays
        # consistent with every other resolution path in this module.
        subject, _ = _strip_suffix(_strip_prefix(prev_q.strip()))
        if not subject:
            return _empty_result(original, marker, 0.0, prev_topic)
        _META_PREFIXES = {
            "meta-summary-followup": "สรุปข้อมูลเกี่ยวกับ",
            "meta-detail-followup": "อธิบายรายละเอียดเกี่ยวกับ",
            # Final Blocker B fix (2026-08-29) — preserves the active
            # substantive topic (`subject`, the previous real question)
            # while appending the CURRENT turn's own modifier, so
            # retrieval never runs on a topic-less fragment alone.
            "meta-simplify-followup": "อธิบายแบบง่ายเกี่ยวกับ",
            "meta-partial-followup": "ตอบเฉพาะข้อมูลที่รองรับเกี่ยวกับ",
        }
        prefix = _META_PREFIXES[marker]
        resolved = prefix + subject
        carried = {k: v for k, v in prev_entities.items() if v}
        return {
            "resolved_question": resolved, "followup_type": marker, "entities_carried": carried,
            "confidence": 0.85, "prev_topic": prev_topic,
            "excluded_entities": {"location": [], "transport": []}, "replaced_entities": {},
        }

    m = _FOLLOWUP_RE.match(normalized)
    if m:
        core = m.group("core").strip()
    elif normalized.startswith("แล้ว"):
        core = normalized[len("แล้ว"):].strip()
    else:
        core = normalized
    # Trailing "ล่ะ/ละ" particle carries no meaning once it's been used as
    # the follow-up marker — strip it so a bare-noun follow-up ("น้ำหอมล่ะ")
    # composes cleanly.
    core = re.sub(r"(ล่ะ|ละ)$", "", core).strip()

    cur_entities = extract_entities(core)
    cur_excluded = extract_excluded_entities(core)

    merged: Dict[str, Optional[str]] = {}
    carried: Dict[str, str] = {}
    replaced: Dict[str, Dict[str, str]] = {}
    for key in ("topic", "transport", "location", "attribute"):
        if cur_entities.get(key):
            merged[key] = cur_entities[key]
            prev_value = prev_entities.get(key)
            if prev_value and prev_value != cur_entities[key]:
                # Entity replacement — current turn overrides a carried
                # value; the two are never both carried forward.
                replaced[key] = {"from": prev_value, "to": cur_entities[key]}
        elif prev_entities.get(key):
            merged[key] = prev_entities[key]
            carried[key] = prev_entities[key]

    # A carried value that the CURRENT turn explicitly excludes must
    # never survive the merge (e.g. prev transport=เรือ, current excludes
    # เรือ and names รถ — already handled by replacement above, but this
    # also covers a carried value with no new positive replacement at all).
    for key, excluded_values in cur_excluded.items():
        if merged.get(key) in excluded_values:
            merged.pop(key, None)
            carried.pop(key, None)

    # Transport facet — single source of truth (requested_transport_modes).
    # This turn names exactly one mode -> use it. This turn names two, OR
    # is a "แล้ว…"/contrastive wrapper that switches to a new GENERIC facet
    # ("แล้วเรทนำเข้าเท่าไหร่คะ" after a road-duration answer) -> drop any
    # stale carried mode so it isn't narrowed. But a BARE attribute
    # follow-up ("กี่วัน" / "เท่าไหร่") that merely supplies the missing
    # attribute for the SAME established context ("ทางรถส่งจากจีนมาไทย" ->
    # "กี่วัน") must KEEP the carried mode.
    if merged.get("attribute") in ("rate", "duration"):
        cur_modes = requested_transport_modes(core)
        _wrapper_switch = (marker or "").startswith("แล้ว") or "contrastive" in (marker or "")
        if len(cur_modes) == 1:
            merged["transport"] = cur_modes[0]
            carried.pop("transport", None)
        elif len(cur_modes) >= 2 or _wrapper_switch:
            merged.pop("transport", None)
            carried.pop("transport", None)
        # else: bare attribute follow-up — keep the carried transport mode.

    resolved, confidence = _compose(merged, core, prev_q)

    if not resolved or confidence < 0.5:
        return _empty_result(original, marker, confidence, prev_topic, cur_excluded)

    return {
        "resolved_question": resolved, "followup_type": marker, "entities_carried": carried,
        "confidence": confidence, "prev_topic": prev_topic,
        "excluded_entities": cur_excluded, "replaced_entities": replaced,
    }


def resolve_followup_query(question: str, history: Optional[List[Dict]] = None) -> str:
    """Backward-compatible wrapper around resolve_conversation() for any
    caller that only needs the resolved string (no Explainability
    detail) — same public contract as before Conversation Resolver 2.0."""
    return resolve_conversation(question, history)["resolved_question"]


# ── P1.2A — RequestSpec: minimal multi-valued request representation ────
# A real customer can put several components in one message ("แบตเตอรี่
# น้ำหอม น้ำยาซักผ้า นำเข้าได้ไหม", "ทางรถกับทางเรือราคาเท่าไหร่ ใช้กี่วัน",
# "ใบกำกับออกได้ไหม แล้วโหลดจากไหน"). The single-value entity model above
# stays exactly as-is for every existing caller; this is an ADDITIVE
# decomposition read by services/answer_planner.py and
# services/playground_orchestrator.py to (a) tell synthesis about every
# requested component and (b) run multi-target retrieval ONLY when there
# genuinely is more than one evidence target. Deterministic, no LLM call.
# Reuses requested_transport_modes() as the single transport source of
# truth — never a second transport parser.
from dataclasses import dataclass, field

# Import-eligibility intent ("<goods> นำเข้าได้ไหม" / "ส่งเข้าไทยได้ไหม").
# Negative lookbehind on "ฝาก" keeps the ฝากนำเข้า *service* question
# ("ฝากนำเข้าได้ไหมคะ") out of this.
# Verb-first eligibility form — "(เรา)สามารถนำเข้า <goods> ได้ไหม" — the
# product sits BETWEEN the verb and "ได้ไหม", not before it. The negative
# lookahead keeps a generic service question ("นำเข้าจากจีนได้ไหม",
# "นำเข้าสินค้าจากจีนได้ไหม") out; "ฝากนำเข้า" is out via (?<!ฝาก).
_VERB_FIRST_ELIG_RE = re.compile(
    r"(?<!ฝาก)นำเข้า\s*(?P<goods>(?!จาก|มาจาก|สินค้าจาก|ของจาก|เข้ามาจาก)\S[^\n]{0,24}?)\s*"
    r"ได้(?:ไหม|มั้ย|มัย|รึเปล่า|หรือเปล่า|หรือไม่|ป่าว)\s*(?:คะ|ครับ|ค่ะ)?\s*$"
)
_ELIGIBILITY_INTENT_RE = re.compile(
    r"(?<!ฝาก)นำเข้าได้(?:ไหม|มั้ย|มัย|รึเปล่า|หรือเปล่า|หรือไม่|ป่าว)"
    r"|(?<!นำ)เข้าได้(?:ไหม|มั้ย|มัย|รึเปล่า|หรือเปล่า|หรือไม่|ป่าว)"
    r"|ส่งเข้าไทยได้(?:ไหม|มั้ย)|ห้ามนำเข้า(?:ไหม|มั้ย|หรือเปล่า)|เอาเข้า(?:มา)?ได้(?:ไหม|มั้ย)"
    r"|(?<!ฝาก)นำเข้า\s*(?!จาก|มาจาก|สินค้าจาก|ของจาก)\S[^\n]{0,24}?ได้(?:ไหม|มั้ย|มัย|รึเปล่า|หรือเปล่า|หรือไม่|ป่าว)\s*(?:คะ|ครับ|ค่ะ)?\s*$"
)
# Trailing eligibility phrase stripped off a product list to leave just
# the goods: "แบตเตอรี่ น้ำหอม นำเข้าได้ไหมคะ" -> "แบตเตอรี่ น้ำหอม".
# "(?:นำ)?เข้า" also strips the colloquial "…เข้าได้ไหม" (no "นำ").
_ELIGIBILITY_TAIL_RE = re.compile(
    r"\s*(?:พวก|สินค้า|ของ)?\s*(?:(?:นำ)?เข้า|ส่งเข้าไทย|เอาเข้า(?:มา)?)\s*"
    r"ได้(?:ไหม|มั้ย|มัย|รึเปล่า|หรือเปล่า|หรือไม่|ป่าว)\s*(?:คะ|ครับ|ค่ะ|บ้าง)?\s*$"
)
_LEADING_ASK_RE = re.compile(r"^\s*(?:ขอถามว่า|อยากถามว่า|สอบถามว่า|ถามว่า|รบกวนถามว่า)\s*")
_LIST_SEP_RE = re.compile(r"\s*(?:แล้วก็|และก็|และ|กับ|,|、|/)\s*|\s+")

# Trusted prohibited-category vocabulary appended to an eligibility
# retrieval query so the category-policy chunks (the "สินค้าที่ห้ามนำเข้า"
# list + the ของเหลว / อาหาร FAQ rows) reliably surface for ANY concrete
# product — never a per-product mapping, just the category names the
# trusted knowledge itself uses.
ELIGIBILITY_CATEGORY_TERMS = (
    "นำเข้าได้ไหม สินค้าต้องห้าม ของเหลว อาหาร ของกิน เครื่องดื่ม เครื่องสำอาง "
    "ยาเวชภัณฑ์ วัตถุไวไฟ แบตเตอรี่ ของมีคม สิ่งมีชีวิต พืช"
)
_LIST_FILLER = {"ก็", "แล้ว", "และ", "กับ", "พวก", "สินค้า", "ของ", "หรือ", "รวมถึง"}

_MINIMUM_RE = re.compile(r"ขั้นต่ำ|ขั้นตํ่า|ขั้นตำ่|ยอดขั้นต่ำ|minimum", re.IGNORECASE)

_COMPARISON_MAP = [
    (re.compile(r"ถูกกว่า|ถูกที่สุด|คุ้มกว่า|ประหยัดกว่า|ราคาดีกว่า"), "cheaper"),
    (re.compile(r"แพงกว่า|แพงที่สุด|แพงสุด"), "more_expensive"),
    (re.compile(r"เร็วกว่า|เร็วที่สุด|ไวกว่า|เร็วสุด"), "faster"),
    (re.compile(r"ช้ากว่า|ช้าที่สุด|นานกว่า|นานสุด"), "slower"),
]
_COMPARISON_FACET = {"cheaper": "rate", "more_expensive": "rate", "faster": "duration", "slower": "duration"}

# Multi-topic sentence split — an explicit "แล้ว/และ" JOIN between two
# self-contained sub-questions. A bare short follow-up ("แล้วเรือล่ะ")
# never survives the >=2 substantive-segment test below.
_SUBQ_SPLIT_RE = re.compile(r"\s+(?:แล้ว|และ|อีกอย่าง|อีกเรื่อง)\s*")
_SUBQ_MARKER_RE = re.compile(r"ไหม|มั้ย|ยังไง|อย่างไร|จากไหน|ที่ไหน|อะไร|กี่|เท่าไหร่|เท่าไร|หรือไม่|คืนได้|ได้ไม")

# Explicit correction: "ไม่ได้ถาม X ... ถาม/เอา/หมายถึง Y".
_NEG_CORR_RE = re.compile(r"ไม่(?:ได้ถาม|เอา|ใช่|ต้องการ)\s*(.{1,12}?)(?=\s|$|ถาม|เอา|หมายถึง)")
_POS_CORR_RE = re.compile(r"(?<!ไม่ได้)(?<!ไม่)(?:ถาม|เอา|หมายถึง)\s*(.{1,12}?)(?=\s|$|ไม่)")
_COUPON_MINE_RE = re.compile(r"คูปอง.*(?:ของผม|ของฉัน|ของดิฉัน|ที่ผมมี|ที่ฉันมี)|(?:ของผม|ของฉัน).*คูปอง")
_COUPON_USAGE_RE = re.compile(r"วิธีใช้|ใช้ยังไง|ใช้งาน|ใช้อย่างไร")


def _facets_in(text: str) -> List[str]:
    out: List[str] = []
    if _RATE_RE.search(text):
        out.append("rate")
    if _DURATION_RE.search(text):
        out.append("duration")
    if _MINIMUM_RE.search(text):
        out.append("minimum")
    return out


def _entities_in(text: str) -> List[str]:
    """Product/goods nouns named in an import-eligibility message. Empty
    for any message that is not an eligibility question — this never fires
    for a normal rate/duration/contact question."""
    if not _ELIGIBILITY_INTENT_RE.search(text):
        return []
    _vf = _VERB_FIRST_ELIG_RE.search(text)
    if _vf:
        # "(สามารถ)นำเข้า <goods> ได้ไหม" — the goods are the middle span.
        body = _vf.group("goods")
    else:
        body = _ELIGIBILITY_TAIL_RE.sub("", text)
    body = _LEADING_ASK_RE.sub("", body).strip()
    ents: List[str] = []
    for raw in _LIST_SEP_RE.split(body):
        p = re.sub(r"^(?:ก็|แล้ว|และ|กับ)\s*", "", (raw or "").strip()).strip().strip(",")
        if len(p) < 2 or p in _LIST_FILLER:
            continue
        # A residual verb/question fragment is not a product noun.
        if _ELIGIBILITY_INTENT_RE.search(p) or _SUBQ_MARKER_RE.search(p):
            continue
        if p not in ents:
            ents.append(p)
    return ents


def _sub_questions_in(text: str) -> List[str]:
    segs = [s.strip() for s in _SUBQ_SPLIT_RE.split(text or "") if s and s.strip()]
    substantive = [s for s in segs if _SUBQ_MARKER_RE.search(s) or len(s) >= 12]
    return substantive if len(substantive) >= 2 else []


def _corrections_in(text: str) -> Dict:
    c = {
        "removed_facets": [], "added_facets": [],
        "removed_transport": [], "added_transport": [],
        "removed_location": [], "added_location": [],
        "interpretation": None,
    }
    if not re.search(r"ไม่(?:ได้ถาม|เอา|ใช่|ต้องการ)|หมายถึง", text or ""):
        return c
    for span in _NEG_CORR_RE.findall(text):
        c["removed_facets"] += _facets_in(span)
        c["removed_transport"] += requested_transport_modes(span)
        loc = _LOCATION_RE.search(span)
        if loc:
            c["removed_location"].append(loc.group(0))
        if _COUPON_MINE_RE.search(span) or ("คูปอง" in span and re.search(r"ของผม|ของฉัน", span)):
            c["interpretation"] = "rag"
    for span in _POS_CORR_RE.findall(text):
        c["added_facets"] += _facets_in(span)
        c["added_transport"] += requested_transport_modes(span)
        loc = _LOCATION_RE.search(span)
        if loc:
            c["added_location"].append(loc.group(0))
        if _COUPON_USAGE_RE.search(span):
            c["interpretation"] = "rag"
    for k in ("removed_facets", "added_facets", "removed_transport", "added_transport",
              "removed_location", "added_location"):
        c[k] = list(dict.fromkeys(c[k]))
    return c


@dataclass
class RequestSpec:
    entities: List[str] = field(default_factory=list)
    facets: List[str] = field(default_factory=list)
    transport_modes: List[str] = field(default_factory=list)
    sub_questions: List[str] = field(default_factory=list)
    comparison: Optional[str] = None
    corrections: Dict = field(default_factory=dict)

    def effective_facets(self) -> List[str]:
        """Facets after applying an explicit in-message correction
        ("ไม่ได้ถามราคา ถามระยะเวลา" -> ['duration'])."""
        added = self.corrections.get("added_facets") or []
        removed = set(self.corrections.get("removed_facets") or [])
        if added:
            return list(added)
        return [f for f in self.facets if f not in removed] or list(self.facets)

    def is_multi_component(self) -> bool:
        facets = self.effective_facets()
        return bool(
            len(self.entities) >= 2
            or len(self.sub_questions) >= 2
            or (len(self.transport_modes) >= 2 and (facets or self.comparison))
            or len(facets) >= 2
        )


def decompose_request(question: str, history: Optional[List[Dict]] = None,
                       raw_question: Optional[str] = None) -> RequestSpec:
    """Deterministic multi-component decomposition of ONE resolved user
    message. `question` should be the post-follow-up-resolution / canonical
    text (so "แล้วแชมพูล่ะ" is already "แชมพูนำเข้าได้ไหม").

    `raw_question` (the customer's own never-rewritten wording) is consulted
    ALONGSIDE `question` for transport-mode and comparison detection only —
    same defensive pattern services/answer_planner.py already uses, since
    spell correction can corrupt a bare transport word ("เรือ" -> "เรทอ")
    before this layer ever sees it."""
    text = question or ""
    raw = raw_question or ""
    corrections = _corrections_in(text)

    modes = list(dict.fromkeys(requested_transport_modes(text) + requested_transport_modes(raw)))
    comparison = None
    for rx, label in _COMPARISON_MAP:
        if rx.search(text) or rx.search(raw):
            comparison = label
            break
    if comparison and len(modes) < 2 and history:
        seen: List[str] = []
        for turn in history:
            if turn.get("role") != "user":
                continue
            for m in requested_transport_modes(turn.get("content") or ""):
                if m not in seen:
                    seen.append(m)
        if len(seen) >= 2:
            modes = seen

    return RequestSpec(
        entities=_entities_in(text),
        facets=_facets_in(text),
        transport_modes=modes,
        sub_questions=_sub_questions_in(text),
        comparison=comparison,
        corrections=corrections,
    )


_FACET_QUERY = {
    "rate": ("อัตราค่าขนส่งทาง{m}เท่าไหร่", "ค่าขนส่งราคาเท่าไหร่"),
    "duration": ("ระยะเวลาขนส่งทาง{m}ใช้กี่วัน", "ขนส่งใช้เวลากี่วัน"),
    "minimum": ("ขนส่งทาง{m}มีขั้นต่ำไหม", "การขนส่งมีขั้นต่ำไหม"),
}


def build_request_components(spec: RequestSpec) -> List["tuple[str, str]"]:
    """[(component_label, focused_retrieval_query), ...] — empty when the
    request is single-component (caller then uses the existing single
    retrieval path unchanged). Never composes an LLM call."""
    comps: List["tuple[str, str]"] = []
    facets = spec.effective_facets()

    if len(spec.entities) >= 2:
        for e in spec.entities:
            comps.append((f"{e} / eligibility", f"{e} {ELIGIBILITY_CATEGORY_TERMS}"))
        return comps

    if len(spec.transport_modes) >= 2 and (facets or spec.comparison):
        use_facets = facets or [_COMPARISON_FACET.get(spec.comparison, "rate")]
        for m in spec.transport_modes:
            for f in use_facets:
                tmpl, _ = _FACET_QUERY.get(f, ("การขนส่งทาง{m}", ""))
                comps.append((f"{m} / {f}", tmpl.format(m=m)))
        return comps

    if len(facets) >= 2:
        for f in facets:
            _, generic = _FACET_QUERY.get(f, ("", ""))
            comps.append((f, generic or f))
        return comps

    if len(spec.sub_questions) >= 2:
        # Carry the first sub-question's subject noun into the later,
        # terser fragments ("ใบกำกับออกได้ไหม" + "แล้วโหลดจากไหน" -> the
        # 2nd retrieval query / label becomes "ใบกำกับ โหลดจากไหน") so an
        # elliptical second question is not searched / shown context-free.
        subj = re.split(r"ออก|ใช้|โหลด|ดาวน์โหลด|มี|คืน|ได้|ยังไง|จาก|ไหม|เท่าไหร่|กี่",
                        spec.sub_questions[0])[0].strip()
        for i, sq in enumerate(spec.sub_questions):
            if i == 0 or len(subj) < 2 or subj in sq:
                comps.append((sq, sq))
            else:
                comps.append((f"{sq} ({subj})", f"{subj} {sq}"))
        return comps

    return comps


def single_eligibility_component(spec: RequestSpec) -> Optional["tuple[str, str]"]:
    """P2A blocker fix — the SAME eligibility-focused retrieval enrichment
    build_request_components() gives each entity of a MULTI-product
    question, applied to a SINGLE concrete product too ("น้ำหอมนำเข้าได้ไหม"
    on its own). Returns (component_label, focused_retrieval_query) or
    None. Not multi-component: the caller keeps the single retrieval path
    and normal answer plan, only swapping in the enriched query + passing
    the one component label so the P1.2A semantic-classification /
    'unconfirmed only if neither item nor category has policy' clause
    still applies (glass stays unconfirmed, perfume/shampoo -> liquid)."""
    if len(spec.entities) == 1 and not spec.sub_questions and not spec.transport_modes:
        e = spec.entities[0]
        return (f"{e} / eligibility", f"{e} {ELIGIBILITY_CATEGORY_TERMS}")
    return None


# ── P5.3 — generic ordering/import PROCESS question ────────────────────
# "ขอขั้นตอนในการสั่งซื้อสินค้าหน่อย" asks for the high-level Shipify
# service journey, but its wording ("สั่งซื้อสินค้า") is lexically closest
# to the marketplace/link FAQ ("สั่งสินค้าจากเว็บจีนอะไรได้บ้าง" ->
# Taobao/1688/Tmall), so retrieval returns that sub-flow instead of the
# process journey. Same enrichment pattern as single_eligibility_component:
# only the retrieval query is swapped (intent/routing/synthesis question
# unchanged), so the "ขั้นตอนการนำเข้าสินค้าจากจีนเข้าไทย" journey FAQ
# ranks first. A marketplace/link-SPECIFIC question is deliberately
# excluded so "สั่งของจาก Taobao ยังไง" / "วางลิงก์สินค้ายังไง" keep their
# own answer.
_PROCESS_INTENT_RE = re.compile(
    r"ขั้นตอน|มีขั้นตอน|ทำ\s*(?:ยัง|อย่าง)ไง|ต้องทำอะไร(?:บ้าง)?|ต้องทำยังไง|"
    r"เริ่ม[^\n]{0,14}(?:ยังไง|อย่างไร|ต้องทำ|ใช้บริการ)|วิธี(?:การ)?(?:ใช้บริการ|นำเข้า|สั่ง)")
_PROCESS_OBJECT_RE = re.compile(
    r"สั่งซื้อสินค้า|สั่งสินค้า|สั่งของ|สั่งซื้อของ|นำเข้าสินค้า|นำเข้าของ|นำเข้าจากจีน|"
    r"ฝากนำเข้า|ฝากสั่ง|ใช้บริการ\s*shipify|ใช้บริการนำเข้า|import", re.IGNORECASE)
# Marketplace / product-link SPECIFIC — never enrich these (E/F must stay
# a specific sourcing answer, not the generic journey).
_MARKETPLACE_SPECIFIC_RE = re.compile(
    r"taobao|1688|tmall|เถาเป่า|เถาเป่าว|ทีมอลล์|ทมอลล์|"
    r"ลิงก์|ลิ้งก์|ลิงค์|\blink\b|"
    r"เว็บจีน|เว็บไหน|เว็บอะไร|เว็บไซต์ไหน|มาร์เก็ตเพลส|ร้านจีนไหน|รองรับเว็บ", re.IGNORECASE)

_PROCESS_JOURNEY_TERMS = (
    "ขั้นตอนการนำเข้าสินค้าจากจีนเข้าไทยทำอย่างไร ฝากนำเข้า ที่อยู่โกดังจีน "
    "ร้านจีนจัดส่งเข้าโกดังจีน ขนส่งจีนไทย ทางรถ ทางเรือ สินค้าถึงไทย ชำระค่าขนส่ง รับสินค้า")


def generic_process_component(question: str) -> Optional["tuple[str, str]"]:
    """(component_label, enriched_retrieval_query) for a GENERIC ordering/
    import process question, else None. Excludes marketplace/link-specific
    wording. Deterministic; no LLM."""
    q = question or ""
    if _MARKETPLACE_SPECIFIC_RE.search(q):
        return None
    if _PROCESS_INTENT_RE.search(q) and _PROCESS_OBJECT_RE.search(q):
        return ("import_process",
                f"ขั้นตอนการนำเข้าสินค้าจากจีนเข้าไทยทำอย่างไร {_PROCESS_JOURNEY_TERMS}")
    return None


# ── Bare product-list continuation of an import-eligibility thread ──────
# "น้ำปลา นำเข้าได้ไหม" -> "น้ำเปล่า ละ น้ำมัน น้ำมันงา": the second turn
# is just a short list of product nouns, no question of its own. When the
# IMMEDIATELY preceding USER turn was an eligibility question, carry that
# goal forward for the whole list. Extends the existing "แล้ว X ล่ะ"
# behaviour to N items. Never fires without that context (a bare list on
# its own stays a bare list).
_LIST_CONT_SEP_RE = re.compile(r"\s*(?:แล้วก็|และก็|แล้ว|และ|ละ|กับ|,|、|/|\+)\s*|\s+")
_LIST_CONT_STOP_RE = re.compile(
    # CUSTOMER SCREENSHOT 2026-09-17 -- "ไหน" (which/where: "วันไหน",
    # "ตรงไหน", "ที่ไหน", "ทางไหน") is a DIFFERENT word from "ไหม" (the
    # yes/no particle) above, and was missing here entirely. Any real
    # question using it ("ชำระบิลขนส่งแล้ว สินค้าจะจัดส่งถึงบ้านวันไหน") was
    # never recognised as a question, so right after an eligibility
    # answer it was wrongly read as "another bare product name in the
    # list" and had "...นำเข้าได้ไหม" appended to it -- corrupting an
    # unrelated delivery/payment question into a 2-component eligibility
    # request and producing a bundled, off-topic multi-part answer.
    r"ไหม|ไหน|มั้ย|มัย|ยังไง|อย่างไร|เท่าไหร่|เท่าไร|กี่|ทำไม|\?|ราคา|ค่าส่ง|นำเข้าได้|ส่งได้|"
    r"เข้าได้|ขอบคุณ|สวัสดี|ครับผมขอ")
_LIST_CONT_DROP = {"ก็", "ละ", "แล้ว", "และ", "กับ", "พวก", "อัน", "ตัว", "นี้", "นั้น", "ด้วย",
                   "อีก", "ที", "หน่อย", "ครับ", "ค่ะ", "คะ", "นะ", "น่ะ"}


def reconstruct_product_list_continuation(message: str, history: Optional[List[Dict]]) -> Optional[str]:
    """-> "<a> <b> <c> นำเข้าได้ไหม" when the last USER turn was an
    import-eligibility question and `message` is a short bare list of
    2+ product-noun tokens with no question / verb of its own. Else None."""
    if not message or not history:
        return None
    last_user = next((t.get("content") or "" for t in reversed(history)
                       if t.get("role") == "user"), "")
    if not _ELIGIBILITY_INTENT_RE.search(last_user):
        return None
    txt = message.strip()
    if len(txt) > 60 or _LIST_CONT_STOP_RE.search(txt) or _ELIGIBILITY_INTENT_RE.search(txt):
        return None
    tokens: List[str] = []
    for raw in _LIST_CONT_SEP_RE.split(txt):
        tok = (raw or "").strip()
        if len(tok) >= 2 and tok not in _LIST_CONT_DROP and tok not in tokens:
            tokens.append(tok)
    if len(tokens) < 2:
        return None
    return " ".join(tokens) + " นำเข้าได้ไหม"


# ── P2A blocker fix — shared regexes for consuming a reply to a P2
#    "elicit_product_type" follow-up. The consumption itself is done by
#    rag/clarification_state.py (which already owns "reply answers the
#    assistant's own last question"); these live here so both modules use
#    one definition. _IMPORT_INTEREST_RE mirrors the canonical one in
#    services/answer_planner.py (1-line copy, avoids an import cycle).
_PRODUCT_REPLY_STRIP_RE = re.compile(r"\s*(ครับผม|ครับ|ค่ะ|คะ|ค่า|นะ|น่ะ|จ้า|จ้ะ|เลย|อ่ะ|อะ)\s*$")
_ELICIT_MARKER_RE = re.compile(
    r"สินค้าประเภท|ประเภทไหน|ประเภทอะไร|สินค้าอะไร|นำเข้าสินค้าอะไร|ขนส่งสินค้าประเภท|"
    r"สินค้าชนิดไหน|ของประเภทไหน")
_IMPORT_INTEREST_RE = re.compile(
    r"อยาก.{0,6}(นำเข้า|สั่งของ|สั่งซื้อ|ชิป|ขนส่ง|ใช้บริการ)|สนใจ.{0,6}(นำเข้า|บริการ|สั่งของ)"
    r"|ต้องการ.{0,8}นำเข้า|อยากนำเข้า|สนใจนำเข้า|ขอใช้บริการนำเข้า|อยากใช้บริการ")
