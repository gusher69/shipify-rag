"""Action Selection Primitives — shared, generic, Registry-driven building
blocks for matching a customer message against a Business Action's
configured keywords/parameters.

Extracted from services/decision_engine.py (2026-08-02 Production
Integration Sprint, Phase 1 Step A) to break a circular import: services/
hybrid_question_classifier.py needs these exact primitives, and
services/decision_engine.py needs to import hybrid_question_classifier.py
for Hybrid routing — decision_engine.py can no longer be the one defining
them. Every function below was moved VERBATIM (no behavior change); it is
not duplicated anywhere. services/decision_engine.py re-imports and
re-exports all of these under its own names, so existing external
importers (e.g. services/erp_test_harness.py's
`from services.decision_engine import _askable_parameters_by_name, ...`)
keep working completely unchanged.
"""
import re
from typing import Dict, Iterable, List, Optional

from services.slot_filling_engine import _validate_generic_identifier, _validate_phone_number, extract_candidates

# validation_type -> validator, reusing the SAME primitives Contextual
# Slot Binding already defines (never redefined here) wherever the
# meaning overlaps (a phone number is a phone number either way); a
# Business Action parameter with no specific validation_type configured
# gets the shared generic-identifier shape, exactly like every ERP slot
# that isn't a phone number.
_PARAMETER_VALIDATORS = {
    "phone_number": _validate_phone_number,
    "email": lambda c: bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", c)),
    "non_empty": lambda c: bool(c and c.strip()),
}

# extract_candidates() (services/slot_filling_engine.py) requires a digit
# in every candidate token by design (tracking/order/invoice-style IDs
# always have one) — an email address often has none, so it would never
# surface as a candidate at all. Rather than modify that frozen function,
# this is a small, additive extension scoped to THIS module: any
# email-shaped substring is also offered as a candidate, on top of
# whatever extract_candidates() already found.
_EMAIL_CANDIDATE_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


def _extract_structural_candidates(message: str) -> List[str]:
    """Candidates with an actual structural signal only (a digit-bearing
    token, or an @-shaped email) — never the raw whole message. Used
    wherever binding a whole free-text sentence to a parameter would be
    semantically wrong, e.g. a member of an identifier parameter GROUP
    (see _extract_candidates_for_binding's docstring for why that
    specifically must never happen)."""
    candidates = list(extract_candidates(message))
    for m in _EMAIL_CANDIDATE_RE.finditer(message or ""):
        token = m.group(0)
        if token not in candidates:
            candidates.append(token)
    return candidates


def _extract_candidates_for_binding(message: str) -> List[str]:
    candidates = _extract_structural_candidates(message)
    # Free-text fallback (2026-08-09, SendLineNotiCS Message parameter) —
    # extract_candidates() and the email extension above both require a
    # structural signal (a digit, or an @-shaped token); a genuinely
    # free-text parameter (validation_type="non_empty", e.g. a
    # notification message body) has neither. Only offered when NO other
    # candidate was found at all, so it can never introduce a SECOND,
    # competing candidate alongside an existing code/email-shaped one
    # (which would turn a clean single-candidate bind into a spurious
    # "ambiguous" result) — it exists purely to give a pure free-text
    # message somewhere to bind to.
    #
    # IMPORTANT (Final Conversational Correctness, 2026-08-15): this
    # whole-message candidate must NEVER be used to satisfy a member of a
    # parameter GROUP (AT_LEAST_ONE/EXACTLY_ONE/ALL) — a group like
    # GetDataCustomer's customer_identifier (CustCode/CustEmail/CustName/
    # CustPhone) exists specifically to require ONE genuine identifying
    # value, and CustName's permissive "non_empty" validator would
    # otherwise happily swallow an entire unrelated sentence ("ขอข้อมูล
    # ลูกค้าหน่อยครับ") as if it were the customer's real name, silently
    # satisfying the group with zero real identifying information and
    # letting execution proceed with fabricated/empty identifiers.
    # Callers binding a GROUP member must call
    # _extract_structural_candidates() directly instead of this function.
    # Ungrouped free-text parameters (SendLineNotiCS's Message) are
    # unaffected — they were never part of a group to begin with.
    if not candidates:
        trimmed = (message or "").strip()
        if trimmed:
            candidates.append(trimmed)
    return candidates


def _resolve_parameter_validator(param: Dict):
    """Builds the validator for THIS parameter. A configured
    `validation_pattern` (AI Auto Setup / admin-authored regex, e.g.
    `^C\\d{5}$` for CustCode vs `^PO\\d{6,}$` for OrderCode) always wins —
    this is exactly what lets action selection distinguish two
    same-shaped required parameters instead of relying on parameter
    order, per the platform's own validation-inference requirement.
    Falls back to the fixed `_PARAMETER_VALIDATORS` table, then to the
    shared generic-identifier shape, unchanged from before."""
    pattern = param.get("validation_pattern")
    base = _PARAMETER_VALIDATORS.get(param.get("validation_type"), _validate_generic_identifier)
    min_len, max_len = param.get("min_length"), param.get("max_length")

    def validator(candidate: str) -> bool:
        if pattern:
            try:
                if not re.fullmatch(pattern, candidate):
                    return False
            except re.error:
                return False
        elif not base(candidate):
            return False
        if min_len is not None and len(candidate) < min_len:
            return False
        if max_len is not None and len(candidate) > max_len:
            return False
        return True

    return validator


def _bind_candidate_to_parameter(candidates: List[str], param: Dict) -> Dict:
    """The Business-Action-parameter equivalent of services/
    slot_filling_engine.py::bind_candidate_to_slot() — same three-way
    contract (bound/ambiguous/none_valid), same reused validator
    primitives, but keyed by the parameter's own `validation_type`
    (Registry-driven) instead of a fixed, closed slot-name table. This
    does not modify or replace bind_candidate_to_slot() — the two now
    coexist: the legacy path still uses the original for its own six
    fixed ERP slot names; this one drives arbitrary, admin-configured
    parameter names."""
    validator = _resolve_parameter_validator(param)
    if not candidates:
        return {"status": "none_valid", "value": None, "valid_candidates": []}
    valid = [c for c in candidates if validator(c)]
    if len(valid) == 1:
        return {"status": "bound", "value": valid[0], "valid_candidates": valid}
    if len(valid) > 1:
        return {"status": "ambiguous", "value": None, "valid_candidates": valid}
    return {"status": "none_valid", "value": None, "valid_candidates": []}


# Identifier Memory (Customer Intelligence V1, 2026-08-15; generalized for
# Final Conversational Correctness) — the fixed, small set of ERP concept
# names this platform persists onto a customer's profile as they're
# collected in conversation (profiles/manager.py::update_profile_from_turn)
# and reads back to auto-fill a still-missing parameter of the SAME
# concept name on a later turn (services/decision_engine.py::
# _handle_dynamic_collection), so a customer is never asked to repeat an
# identifier they already gave. Defined once, here, so the write side and
# the read side can never silently drift apart.
IDENTIFIER_MEMORY_FIELDS = (
    ("cust_code", "CustCode"), ("last_order_code", "OrderCode"),
    ("last_shipment_code", "ShipmentCode"), ("last_tracking", "Tracking"),
)

_NON_ASKABLE_INPUT_SOURCES = ("secret_configuration", "credential_store", "fixed_configuration", "system_generated")


def _askable_parameters_by_name(action: Dict) -> Dict[str, Dict]:
    """Every parameter the customer could actually be ASKED for —
    excludes secret-configuration and credential_store parameters
    (never requested from the customer, per the Business Action
    Center's own security rule), and fixed_configuration/system_generated
    (resolved automatically from a fixed value or computed context — see
    services/action_executor.py::_resolve_parameter_source — never from
    customer message either, so asking about them is equally wrong;
    confirmed live 2026-08-07 when a fixed_configuration parameter
    blocked SearchDataOrderList/SearchDataShipmentList from ever reaching
    is_complete=True)."""
    return {
        p["name"]: p for p in (action.get("parameters") or [])
        if p.get("input_source", "customer_message") not in _NON_ASKABLE_INPUT_SOURCES
    }


_ASCII_KEYWORD_RE = re.compile(r"^[A-Za-z0-9]+$")


def _keyword_matches(kw: str, message_l: str) -> bool:
    """Case-insensitive containment check used by every keyword-scoring
    caller below. A pure-ASCII keyword (e.g. "PO") must match as its own
    token, never as a prefix fragment silently swallowed inside a longer
    identifier VALUE the customer supplied in the same message (e.g. "PO"
    must not match inside an OrderCode like "POS100820260815001" — a
    real, generic false-positive discovered via customer UAT 2026-08-17:
    it made _resolve_continuation_action's tie-break pick the wrong
    sibling action and drop the already-known OrderCode). Thai-script
    keywords are deliberately left on plain substring matching — Thai has
    no space-delimited word boundaries, so an ASCII-style boundary guard
    would reject legitimate compound-word matches instead of protecting
    anything."""
    kw_l = str(kw or "").lower()
    if not kw_l:
        return False
    if _ASCII_KEYWORD_RE.match(kw_l):
        return re.search(r"(?<![A-Za-z0-9])" + re.escape(kw_l) + r"(?![A-Za-z0-9])", message_l) is not None
    return kw_l in message_l


def _keyword_score(action: Dict, message: str) -> float:
    message_l = (message or "").lower()
    score = 0.0
    for kw in action.get("search_keywords") or []:
        if _keyword_matches(kw, message_l):
            score += 1.0
    for ex in (action.get("_examples_text") or []):
        if ex and _keyword_matches(ex, message_l):
            score += 0.5
    ai_desc = (action.get("ai_description") or "").lower()
    if ai_desc and any(word in ai_desc for word in message_l.split() if len(word) > 2):
        score += 0.25
    return score


def select_requested_mapped_fields(question: str, mapped_fields: Optional[Dict], response_mapping: Optional[List[Dict]] = None,
                                    input_param_names: Optional[Iterable[str]] = None) -> Optional[Dict]:
    """Generic Requested-Field Filtering — narrows an already-mapped ERP
    result down to only the field(s) the customer's own question actually
    asked about, driven entirely by each response_mapping row's own
    `field_metadata.keywords` (falls back to the row's `mapped_label`
    itself when a row has no curated keywords). Not specific to any one
    Business Action — any action whose response_mapping rows carry
    keywords gets this behavior for free; one with no keywords configured
    keeps its original, fully-unfiltered summary.

    Matching is a simple, deterministic, case-insensitive substring test —
    the same technique `_keyword_score` above already uses for action
    selection — never an LLM call, so it stays cheap and fully explainable
    (see CLAUDE.md: deterministic post-processing must stay separably
    labeled from AI-generated output).

    Identifier Self-Match Guard (2026-08-16) — a row that merely echoes
    back one of THIS turn's own input parameters (e.g. a "รหัสลูกค้า" /
    CustCode row, json_path ending "...CustCode") is skipped when
    computing matches, never let alone satisfy the filter. Otherwise any
    message that simply NAMES the identifier the customer already gave
    ("อยากทราบข้อมูลลูกค้ารหัส FT3182") collides with that field's own
    keywords ("รหัส"/"code") and narrows a rich customer record down to
    a bare echo of the code the customer already supplied — the customer
    is naming their identifier, not asking to have it read back. The row
    is still included whenever nothing else narrows the result (the
    "safe by construction" default below still shows it).

    Safe by construction: if nothing in the question matches any field's
    keywords (e.g. "ดูข้อมูลทั้งหมด" / "show me everything", or a business
    action with no curated keywords at all), the full `mapped_fields` dict
    is returned unchanged — a customer's data is never dropped by a failed
    or ambiguous match."""
    if not isinstance(mapped_fields, dict) or not mapped_fields:
        return mapped_fields
    question_l = (question or "").lower()
    input_names_lower = {str(n).lower() for n in (input_param_names or [])}
    matched_labels = set()
    for m in response_mapping or []:
        label = m.get("mapped_label")
        if not label or label not in mapped_fields:
            continue
        json_path = m.get("json_path") or ""
        last_segment = json_path.rsplit(".", 1)[-1].lower() if json_path else ""
        if input_names_lower and last_segment in input_names_lower:
            continue
        keywords = (m.get("field_metadata") or {}).get("keywords") or [label]
        if any(str(kw).lower() in question_l for kw in keywords if kw):
            matched_labels.add(label)
    if not matched_labels:
        return mapped_fields
    return {label: value for label, value in mapped_fields.items() if label in matched_labels}
