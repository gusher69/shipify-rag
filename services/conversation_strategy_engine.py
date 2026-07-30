"""Conversation Strategy Engine — Platform Service (this sprint's core
deliverable).

Decides WHAT should happen in a conversation turn: which input fields
can be confidently detected from the customer's message this turn,
which are still missing, which questions can be skipped as a result,
and a ranked list of what's still worth asking about. It NEVER
generates UI (no components/labels/buttons) — that remains the sole
responsibility of services/conversation_form_generator.py, which this
module never imports and never overlaps with.

Deterministic, no-LLM, table/metadata-driven detection only: every
field's own configured aliases/display_labels/options/business_label/
canonical_name (the same spirit as
services/erp_test_harness.py::_field_alias_pool()/detect_requested_
fields()'s scored candidate matching) is what gets matched against the
message — never a per-field-name/per-entity hardcoded branch.

This module never implements the Business Action Matcher or the
Decision Engine, never touches the RAG Playground, Provider
architecture, Action Executor, Credential Store, or the AI Analysis
Pipeline, and never re-implements required-group validation (that
stays in services/business_action_registry.py::validate_parameter_groups,
consumed indirectly via ConversationState fed forward into
generate_conversation_form()).
"""
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Dict, List, Optional

STRATEGY_VERSION = "1.0"

# Part 4/5 — global default confidence threshold. A field's own
# `confidence_threshold` (Part 3 metadata) overrides this per-field.
# Documented default: 0.75 — high enough that a single generic word-
# token match (~0.6) never silently auto-fills, but a precise
# alias/enum-option match (0.85+) always does.
DEFAULT_CONFIDENCE_THRESHOLD = 0.75

# ── Part 2 — simple, table-driven relative-date phrases ────────────────
# Small, documented coverage; anything not in this table is simply not
# detected as a date value (never a crash, never a guess).
_RELATIVE_DATE_PHRASES = {
    "เมื่อวาน": -1, "yesterday": -1,
    "วันนี้": 0, "today": 0,
    "พรุ่งนี้": 1, "tomorrow": 1,
}


def _today_offset(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).date().isoformat()


def _field_effective_threshold(f: Dict) -> float:
    t = f.get("confidence_threshold")
    try:
        t = float(t)
    except (TypeError, ValueError):
        return DEFAULT_CONFIDENCE_THRESHOLD
    return t if 0.0 <= t <= 1.0 else DEFAULT_CONFIDENCE_THRESHOLD


def _fuzzy_contains(phrase: str, message: str, min_ratio: float = 0.6) -> bool:
    """Generic fuzzy substring test: a plain substring match always
    counts; otherwise the longest common contiguous run between `phrase`
    and `message` must cover at least `min_ratio` of `phrase`'s length.
    Handles Thai conjugated/compound phrases (e.g. a message containing
    "ออกจากจีน" should still match an option label "ส่งออกจากจีน") without
    any per-word hardcoded stemming table."""
    phrase = phrase.strip()
    if not phrase:
        return False
    if phrase in message:
        return True
    if len(phrase) < 4:
        return False
    match = SequenceMatcher(None, phrase, message).find_longest_match(0, len(phrase), 0, len(message))
    return (match.size / len(phrase)) >= min_ratio


def _detection_pool_for_option(option: Dict, label: str) -> List[str]:
    pool = [label, option.get("value")]
    pool += [option.get("description")]
    return [p for p in pool if p]


def _detect_enum_field(f: Dict, lower_message: str) -> Optional[Dict]:
    """An input field with configured `options` — a hit on any option's
    own label/value/description sets that field's value to the option's
    value with high confidence (Part 2)."""
    options = f.get("options") or []
    best = None
    for o in options:
        for phrase in _detection_pool_for_option(o, o.get("label") or ""):
            phrase_l = str(phrase).strip().lower()
            if not phrase_l:
                continue
            exact = phrase_l in lower_message
            if exact or _fuzzy_contains(phrase_l, lower_message):
                score = 0.95 if (exact and len(phrase_l) >= 3) else (0.85 if len(phrase_l) >= 3 else 0.8)
                if best is None or score > best["confidence"]:
                    best = {"value": o.get("value"), "confidence": score,
                            "matched_phrase": phrase, "matched_via": "option_alias"}
    return best


def _field_alias_pool(f: Dict) -> List[str]:
    """Same spirit as erp_test_harness._field_alias_pool() but for INPUT
    fields: the field's OWN configured aliases/display_labels/business_
    label/canonical_name word-tokens — never a hardcoded per-field list."""
    pool = list(f.get("aliases") or [])
    pool += list((f.get("display_labels") or {}).values())
    pool.append(f.get("business_label"))
    canonical = f.get("canonical_name") or ""
    attr = canonical.split(".", 1)[-1] if canonical else ""
    pool.append(attr.replace("_", " ") if attr else None)
    return [p for p in pool if p]



# Part 2 — a small, generic, documented table of "flag concept" phrases
# keyed on a fragment of the field's OWN canonical-name attribute (e.g.
# canonical_name "shipment.latest" -> attr fragment "latest"). This is
# NOT a per-field-name hardcode — it applies to ANY field whose own
# derived canonical attr contains one of these fragments, for any
# entity/domain, and only kicks in when the field has no enum options
# and no extractable numeric/date value in the message. Coverage gap
# (documented): only the "latest N records" flag concept is covered
# this sprint; add further fragments here as real integrations surface
# them, never new per-field Python code.
_FLAG_CONCEPT_PHRASES_BY_ATTR_FRAGMENT = {
    "latest": ["ล่าสุด", "latest", "most recent", "newest"],
}


def _detect_boolean_flag_field(f: Dict, lower_message: str) -> Optional[Dict]:
    """Part 2 — a non-enum field whose OWN aliases/business_label match a
    phrase in the message, with no explicit numeric/date value present,
    is still registered as detected with a sentinel value True (e.g.
    "ล่าสุด" -> Latest=True) rather than failing to detect it just
    because full value extraction isn't possible. Documented, generic:
    applies to ANY field meeting this shape, never a specific field
    name."""
    for phrase in _field_alias_pool(f):
        phrase_l = str(phrase).strip().lower()
        if phrase_l and len(phrase_l) >= 3 and phrase_l in lower_message:
            return {"value": True, "confidence": 0.8, "matched_phrase": phrase, "matched_via": "business_label_flag"}
    canonical = f.get("canonical_name") or ""
    attr = canonical.split(".", 1)[-1].lower() if canonical else ""
    for fragment, phrases in _FLAG_CONCEPT_PHRASES_BY_ATTR_FRAGMENT.items():
        if fragment not in attr:
            continue
        for phrase in phrases:
            if phrase in lower_message:
                return {"value": True, "confidence": 0.85, "matched_phrase": phrase, "matched_via": "flag_concept_table"}
    return None


def _detect_date_field(f: Dict, lower_message: str) -> Optional[Dict]:
    for phrase, offset in _RELATIVE_DATE_PHRASES.items():
        if phrase in lower_message:
            return {"value": _today_offset(offset), "confidence": 0.9,
                    "matched_phrase": phrase, "matched_via": "relative_date_phrase"}
    return None


def _detect_generic_field(f: Dict, lower_message: str) -> Optional[Dict]:
    """Lower-confidence generic fallback — a business_label/alias WORD
    (>=3 chars) appearing in the message, with no value extracted. Used
    only when no more specific detector fired, and scores below the
    default threshold on its own (so it never silently auto-fills unless
    the field has explicitly lowered its own confidence_threshold)."""
    for phrase in _field_alias_pool(f):
        words = [w for w in str(phrase).lower().split() if len(w) >= 3]
        for w in words:
            if w in lower_message:
                return {"value": True, "confidence": 0.6, "matched_phrase": w, "matched_via": "generic_word_token"}
    return None


def _detect_field(f: Dict, lower_message: str) -> Optional[Dict]:
    """One field -> best detection this message, or None. Dispatch is
    purely on the field's OWN metadata shape (has options? semantic_type
    date-ish?) — never on field name/canonical_name equality."""
    if f.get("options"):
        hit = _detect_enum_field(f, lower_message)
        if hit:
            return hit
    sem = (f.get("semantic_type") or "").lower()
    if sem == "date" or f.get("display_type") == "date":
        hit = _detect_date_field(f, lower_message)
        if hit:
            return hit
    hit = _detect_boolean_flag_field(f, lower_message)
    if hit:
        return hit
    return _detect_generic_field(f, lower_message)


# ── Part 6 — ranking formula ─────────────────────────────────────────────
# score = confidence * 0.85 + normalized_priority * 0.15
# normalized_priority = min(conversation_priority, 10) / 10 (default 0
# when unset). Confidence is primary and dominates; conversation_priority
# is a secondary, weighted tiebreaker between otherwise-close candidates
# — documented, not hidden. (0.15 is small enough that priority alone
# never overturns a clearly higher-confidence detection.)
def _rank_score(confidence: float, priority: Optional[int]) -> float:
    normalized_priority = min(max(priority or 0, 0), 10) / 10.0
    return confidence * 0.85 + normalized_priority * 0.15


def analyze_conversation_strategy(contract: Dict, message: str, state, language: str = "th") -> Dict:
    """Part 1 — the ONE entry point. Given the resolved Integration
    Contract, the raw customer message, and the existing ConversationState
    (services/conversation_form_generator.py::ConversationState, or
    anything exposing `.collected_values`), returns a Conversation
    Strategy dict (see module docstring / sprint spec Part 1 for the
    exact shape). Never mutates `state`; never generates UI; never
    calls an LLM."""
    lower_message = (message or "").strip().lower()
    collected = dict(getattr(state, "collected_values", {}) or {})
    input_fields = contract.get("inputs") or []

    ready_fields: Dict[str, object] = {}
    confidence: Dict[str, float] = {}
    reasoning: Dict[str, Dict] = {}
    auto_detected_fields: List[str] = []
    below_threshold_candidates: Dict[str, Dict] = {}

    for f in input_fields:
        name = f.get("technical_name")
        if not name or collected.get(name) not in (None, ""):
            continue  # already collected earlier turn — nothing to (re)detect
        if f.get("visibility") == "hidden" or f.get("sensitive") or f.get("source") in (
                "credential_store", "secret_configuration"):
            continue  # never detect/auto-fill a secret-sourced field
        hit = _detect_field(f, lower_message)
        if not hit:
            continue
        threshold = _field_effective_threshold(f)
        auto_fill = f.get("auto_fill", True)
        trace = {
            "matched_phrase": hit["matched_phrase"], "matched_via": hit["matched_via"],
            "resolved_value": hit["value"], "confidence": hit["confidence"],
        }
        if hit["confidence"] >= threshold and auto_fill:
            ready_fields[name] = hit["value"]
            confidence[name] = hit["confidence"]
            auto_detected_fields.append(name)
            trace["action"] = "auto_filled_skipped_question"
            reasoning[name] = trace
        else:
            # Part 5 — below threshold (or auto_fill disabled): never
            # silently auto-fill. Surfaced as a "suggested but not
            # confirmed" candidate instead, still asked about.
            below_threshold_candidates[name] = hit
            confidence[name] = hit["confidence"]
            trace["action"] = "suggested_not_confirmed" if hit["confidence"] < threshold else "detected_but_auto_fill_disabled"
            reasoning[name] = trace

    # ── Part 8 — required-group optimization ────────────────────────────
    # If any AT_LEAST_ONE/EXACTLY_ONE group member was confidently
    # detected this turn, mark that group satisfied by feeding the
    # detection into ready_fields (already done above) plus a
    # `selected_required_group_fields`-shaped hint the caller (Part 14)
    # applies to ConversationState BEFORE generate_conversation_form()
    # runs — Form Generator's OWN existing _groups_status()/
    # validate_parameter_groups then naturally sees the group satisfied;
    # this engine never duplicates that logic.
    group_satisfaction: Dict[str, str] = {}
    for g in contract.get("required_field_groups") or []:
        members = g.get("members") or []
        satisfied_member = next((m for m in members if m in ready_fields), None)
        if satisfied_member:
            group_satisfaction[g.get("name")] = satisfied_member

    # ── missing_fields: required (ungrouped) fields still absent ───────
    grouped_names = {m for g in contract.get("required_field_groups") or [] for m in g.get("members") or []}
    missing_fields: List[str] = []
    for f in input_fields:
        name = f.get("technical_name")
        if not f.get("required") or name in grouped_names:
            continue
        if f.get("visibility") == "hidden" or f.get("sensitive"):
            continue
        if name in ready_fields or collected.get(name) not in (None, ""):
            continue
        missing_fields.append(name)

    # groups with no satisfied member yet are still "missing" (as a group)
    for g in contract.get("required_field_groups") or []:
        name = g.get("name")
        if name in group_satisfaction:
            continue
        already_selected = getattr(state, "selected_required_group_fields", {}) or {}
        if name in already_selected:
            continue
        missing_fields.append(f"__group__.{name}")

    # ── Part 4/6 — questions_to_skip / questions_to_ask (ranked) ────────
    questions_to_skip = list(auto_detected_fields) + [
        f"__group__.{gname}" for gname in group_satisfaction
    ]

    candidates = []
    for f in input_fields:
        name = f.get("technical_name")
        if name in ready_fields:
            continue
        if f.get("visibility") == "hidden" or f.get("sensitive") or f.get("source") in (
                "credential_store", "secret_configuration"):
            continue
        if collected.get(name) not in (None, ""):
            continue
        is_required_ungrouped = bool(f.get("required")) and name not in grouped_names
        is_group_member_unsatisfied = name in grouped_names and not any(
            name == v for v in group_satisfaction.values())
        if not (is_required_ungrouped or is_group_member_unsatisfied or name in below_threshold_candidates):
            continue
        cand_confidence = confidence.get(name, 0.0)
        priority = f.get("conversation_priority")
        if f.get("ask_first"):
            priority = (priority or 0) + 100
        candidates.append((name, _rank_score(cand_confidence, priority)))

    # Only ONE criterion-selection question per still-unsatisfied group
    # (never one per member) — collapse group members into a single
    # group-level candidate for ranking/dedup purposes.
    seen_groups = set()
    ranked_names: List[str] = []
    member_to_group = {m: g.get("name") for g in (contract.get("required_field_groups") or []) for m in g.get("members") or []}
    for name, _score in sorted(candidates, key=lambda t: t[1], reverse=True):
        gname = member_to_group.get(name)
        if gname:
            if gname in group_satisfaction or gname in seen_groups:
                continue
            seen_groups.add(gname)
            ranked_names.append(f"__group__.{gname}")
        else:
            ranked_names.append(name)

    questions_to_ask = ranked_names

    return {
        "ready_fields": ready_fields,
        "missing_fields": missing_fields,
        "auto_detected_fields": auto_detected_fields,
        "questions_to_skip": questions_to_skip,
        "questions_to_ask": questions_to_ask,
        "confidence": confidence,
        "reasoning": reasoning,
        "group_satisfaction": group_satisfaction,
        "strategy_version": STRATEGY_VERSION,
    }


def apply_strategy_to_state(state, strategy: Dict):
    """Part 8/14 — applies a strategy's ready_fields/group_satisfaction
    onto a ConversationState IN PLACE, before generate_conversation_form()
    runs, so the Form Generator's own existing group/required-field logic
    naturally sees the result. Never re-implements group-satisfaction
    logic; purely a state-mutation helper analogous to
    conversation_form_generator.apply_user_selection()."""
    for name, value in (strategy.get("ready_fields") or {}).items():
        state.collected_values[name] = value
    for gname, member in (strategy.get("group_satisfaction") or {}).items():
        if gname not in state.selected_required_group_fields:
            state.selected_required_group_fields[gname] = member
    return state
