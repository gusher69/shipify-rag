"""Semantic Parameter Inference — Platform-First, metadata-driven layer
(2026-08-09, ERP semantic-filter sprint).

Converts natural-language filter phrases ("ที่ส่งออกจากจีนแล้ว", "3 รายการ
ล่าสุด") into real Business Action parameter values (BillStatus=3, Latest=3)
— WITHOUT any endpoint-specific code here or in Decision Engine. Every
signal this module acts on is read from a parameter's own
`field_metadata` (services/business_action_registry.py's existing,
already-migrated JSONB column — see migrations/032_field_metadata.sql),
which any admin can configure for any Business Action's any parameter.
An action with no such metadata configured is completely unaffected —
this module returns {} for it, same as before this sprint existed.

Two independent, deterministic (no LLM) capabilities:

1. Enum phrase matching — `field_metadata.enum` maps a parameter's real
   wire value (e.g. "3") to a list of natural-language phrases a customer
   might use to mean it. Configuration shape:
       {"enum": {"3": {"label": "exported_china",
                        "phrases": ["ส่งออกจากจีน", "ออกจากจีนแล้ว"]}}}

2. Numeric limit extraction — `field_metadata.numeric_limit: true` marks
   a parameter as a "how many" quantity the customer might state
   directly ("3 รายการล่าสุด", "ขอ 10 รายการ", "เอา 5 อันล่าสุด").

Both are pure text-matching, not fuzzy/AI classification — deterministic
post-processing kept clearly separate from any LLM-generated content, per
CLAUDE.md's "Deterministic vs. AI-generated logic must stay separably
labeled" rule.
"""
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional


def infer_enum_parameters(parameters: List[Dict], message: str) -> Dict[str, str]:
    """For every parameter carrying field_metadata.enum, checks whether
    the message contains any of that value's configured phrases. Returns
    {parameter_name: matched_enum_key} for every match found — a
    parameter with no phrase match in the message is simply absent from
    the result (never guessed)."""
    matches: Dict[str, str] = {}
    message_l = (message or "").lower()
    if not message_l:
        return matches
    for p in parameters:
        enum_map = (p.get("field_metadata") or {}).get("enum")
        if not enum_map:
            continue
        for key, spec in enum_map.items():
            phrases = (spec or {}).get("phrases") or []
            if any(str(phrase).lower() in message_l for phrase in phrases if phrase):
                matches[p["name"]] = str(key)
                break  # first matching value for THIS parameter wins; do not overwrite with a later, weaker match
    return matches


_LIMIT_KEYWORD = "ล่าสุด"
_LIMIT_WINDOW = 15  # chars of lookaround around the keyword to search for a count
_LIMIT_LEADING_RE = re.compile(r"(?:ขอ|เอา)\s*(\d+)\s*(?:รายการ|อัน|ชิ้น)")


def _extract_limit_digit(message: str) -> Optional[str]:
    """Finds the customer-stated count for a "how many" request. Thai has
    no mandatory spacing between a noun and a following/preceding
    keyword (e.g. "3 พัสดุล่าสุด" glues "พัสดุ" directly onto "ล่าสุด" with
    no separator, so a strict adjacency regex misses it) — so instead of
    matching an exact token sequence, this looks for the nearest digit
    within a small window on either side of the "ล่าสุด" keyword itself,
    which is robust to whatever noun/item-name sits in between. Falls
    back to the "ขอ/เอา N รายการ" phrasing when "ล่าสุด" isn't present at
    all. Never matches a bare digit with no limit-indicating keyword
    nearby — a raw number elsewhere in the message (e.g. inside a
    customer/order code) is not treated as a limit."""
    idx = message.find(_LIMIT_KEYWORD)
    if idx != -1:
        before = message[max(0, idx - _LIMIT_WINDOW):idx]
        after = message[idx + len(_LIMIT_KEYWORD): idx + len(_LIMIT_KEYWORD) + _LIMIT_WINDOW]
        nums_before = re.findall(r"\d+", before)
        if nums_before:
            return nums_before[-1]  # nearest number before the keyword
        nums_after = re.findall(r"\d+", after)
        if nums_after:
            return nums_after[0]  # nearest number after the keyword
    m = _LIMIT_LEADING_RE.search(message)
    if m:
        return m.group(1)
    return None


def infer_numeric_limit_parameters(parameters: List[Dict], message: str) -> Dict[str, str]:
    """For every parameter carrying field_metadata.numeric_limit=true,
    looks for an explicit customer-stated count near a limit-indicating
    Thai keyword ("ล่าสุด"/"รายการ"/"อัน"/"ขอ"/"เอา")."""
    matches: Dict[str, str] = {}
    if not message:
        return matches
    digit = _extract_limit_digit(message)
    if digit is None:
        return matches
    for p in parameters:
        if (p.get("field_metadata") or {}).get("numeric_limit"):
            matches[p["name"]] = digit
    return matches


_RELATIVE_DATE_PHRASES = ("วันนี้", "เมื่อวาน", "เดือนนี้", "เดือนที่แล้ว")
_ABSOLUTE_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b|\b(\d{2}/\d{2}/\d{4})\b")


def _relative_date_range(phrase: str, now: Optional[datetime] = None) -> Optional["tuple[str, str]"]:
    now = now or datetime.now()
    today = now.date()
    if phrase == "วันนี้":
        return today.isoformat(), today.isoformat()
    if phrase == "เมื่อวาน":
        d = (today - timedelta(days=1)).isoformat()
        return d, d
    if phrase == "เดือนนี้":
        start = today.replace(day=1)
        return start.isoformat(), today.isoformat()
    if phrase == "เดือนที่แล้ว":
        first_of_this_month = today.replace(day=1)
        last_of_prev_month = first_of_this_month - timedelta(days=1)
        first_of_prev_month = last_of_prev_month.replace(day=1)
        return first_of_prev_month.isoformat(), last_of_prev_month.isoformat()
    return None


def infer_date_range_parameters(parameters: List[Dict], message: str,
                                 now: Optional[datetime] = None) -> Dict[str, str]:
    """Maps a relative-date phrase (วันนี้/เมื่อวาน/เดือนนี้/เดือนที่แล้ว) or an
    absolute YYYY-MM-DD / DD/MM/YYYY date onto whichever *Start/*End date
    parameter pair the selected Business Action actually has — never
    invents a field. `field_metadata.date_range_group` on a *Start
    parameter names its paired *End parameter, e.g.
    {"date_range_group": "ExportDateEnd"}.

    Deliberately conservative: if the action has MORE THAN ONE
    configured date-range group and the message doesn't clearly name
    which one, this returns {} rather than guessing — per spec, a
    genuinely ambiguous multi-dimension date phrase should prompt one
    clarification question upstream, not be silently misassigned to the
    wrong date field. (The clarification-question UX itself is not
    implemented by this pass — see PROJECT_STATE.md for the scope note.)"""
    date_groups = [
        (p["name"], (p.get("field_metadata") or {}).get("date_range_group"))
        for p in parameters if (p.get("field_metadata") or {}).get("date_range_group")
    ]
    if not date_groups:
        return {}

    matches: Dict[str, str] = {}
    abs_match = _ABSOLUTE_DATE_RE.search(message or "")
    relative_phrase = next((ph for ph in _RELATIVE_DATE_PHRASES if ph in (message or "")), None)

    if len(date_groups) > 1 and (abs_match or relative_phrase):
        return {}  # ambiguous which date dimension — do not guess

    if not date_groups:
        return {}
    start_name, end_name = date_groups[0]

    if relative_phrase:
        rng = _relative_date_range(relative_phrase, now)
        if rng:
            matches[start_name], matches[end_name] = rng
    elif abs_match:
        iso, dmy = abs_match.group(1), abs_match.group(2)
        if iso:
            matches[start_name] = matches[end_name] = iso
        elif dmy:
            d, mth, y = dmy.split("/")
            matches[start_name] = matches[end_name] = f"{y}-{mth}-{d}"
    return matches


def infer_semantic_parameters(parameters: List[Dict], message: str) -> Dict[str, str]:
    """The single entry point Decision Engine calls — merges all three
    inference kinds. Later kinds never overwrite an earlier match for the
    same parameter name (enum > numeric limit > date), though in
    practice they target disjoint parameters since each parameter only
    carries ONE relevant field_metadata shape."""
    result: Dict[str, str] = {}
    result.update(infer_date_range_parameters(parameters, message))
    result.update(infer_numeric_limit_parameters(parameters, message))
    result.update(infer_enum_parameters(parameters, message))
    return result
