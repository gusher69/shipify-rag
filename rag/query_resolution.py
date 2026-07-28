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

_TRANSPORT_RE = re.compile(r"เครื่องบิน|รถ|เรือ|อากาศ")
_TRANSPORT_CANONICAL = {"เครื่องบิน": "อากาศ", "รถ": "รถ", "เรือ": "เรือ", "อากาศ": "อากาศ"}
_LOCATION_RE = re.compile(r"ไทย|จีน")

# Checked in this order — duration/location markers are more distinctive
# than the generic rate markers (a bare "เท่าไหร่" is common to both a
# price question AND could otherwise be ambiguous), so they're resolved
# first to avoid a duration/location question being misread as a rate one.
_DURATION_RE = re.compile(r"กี่วัน|ระยะเวลา|นานแค่ไหน")
_LOCATION_ATTR_RE = re.compile(r"แผนที่|โลเคชั่น|พิกัด|ที่อยู่|อยู่ไหน|ที่นี่|google\s*map|gps", re.IGNORECASE)
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
        return "meta-summary-followup" if _META_SUMMARY_MARKER_RE.search(normalized) else "meta-detail-followup"
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


def _is_meta_followup(text: str) -> bool:
    """True when `text` carries a summarize/detail-again instruction but
    introduces no new topic/attribute/transport/location entity of its
    own — i.e. it can only be answered by reusing whatever the
    conversation already established, never as a complete question in
    isolation. A question that both uses one of these words AND names
    its own concrete subject (e.g. "สรุปนโยบายการคืนสินค้า") is NOT a
    meta follow-up — it's a fresh, self-contained question and must be
    left untouched, same as any other standalone question."""
    if not (_META_SUMMARY_MARKER_RE.search(text) or _META_DETAIL_MARKER_RE.search(text)):
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

    if marker in ("meta-summary-followup", "meta-detail-followup"):
        # Reuses ONLY the previous USER question's own wording — never
        # the assistant's prior answer — as the subject to summarize/
        # detail, per the "never use previous AI answers as evidence"
        # requirement. _strip_suffix/_strip_prefix are the SAME helpers
        # the legacy tier already uses, so this stays consistent with
        # every other resolution path in this module.
        subject, _ = _strip_suffix(_strip_prefix(prev_q.strip()))
        if not subject:
            return _empty_result(original, marker, 0.0, prev_topic)
        prefix = "สรุปข้อมูลเกี่ยวกับ" if marker == "meta-summary-followup" else "อธิบายรายละเอียดเกี่ยวกับ"
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
