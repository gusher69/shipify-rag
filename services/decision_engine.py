"""AI Middleware Decision Engine v2 — the central orchestration layer.

Architecture:

    Channel Adapter -> Decision Engine -> Conversation State -> Intent
    Resolution -> Workflow Resolution -> Information Collection Engine ->
    Business Action Registry -> Generic Action Executor -> Response
    Builder -> Channel Adapter

This module ONLY ORCHESTRATES existing, frozen components. It never
executes an API itself (that's services/action_executor.py's job), never
re-implements slot filling (services/slot_filling_engine.py owns that),
never re-implements retrieval (services/rag_service.py owns that), and
never hardcodes a specific business action ("if tracking: ... if
customer: ...") — every routing decision reads from the Business Action
Registry, which any current or future customer configures without a
code change.

No embeddings, no semantic routing, no LINE/notification adapter, no
Decision-Engine-side OAuth — those are explicitly future phases. This
engine only prepares the interface for them (see `_semantic_score`,
`_embedding_score` — both no-ops today, scored 0.0, ready to be wired to
a real model later without changing this module's call sites).
"""
import re
import time
from typing import Dict, List, Optional

from rag.query_understanding import classify_actionable_intent
from services.slot_filling_engine import (
    ERP_INTENTS,
    _HANDOFF_FOLLOWUP_RE,
    _HUMAN_REQUEST_RE,
    _REFUSAL_RE,
    _validate_generic_identifier,
    _validate_phone_number,
    build_collection_state,
    extract_candidates,
    resolve_active_erp_intent,
)
from services.business_action_registry import get_registry, sanitize_for_preview
from services.action_executor import ActionExecutor, get_action_executor
# Action Selection Primitives (2026-08-02 Production Integration Sprint,
# Phase 1 Step A) — moved out of this module, verbatim, so services/
# hybrid_question_classifier.py can import them WITHOUT importing this
# module (this module needs to import the classifier for Hybrid routing
# — the two can no longer import each other). Re-imported and re-exported
# here under the SAME names so every existing external import (e.g.
# services/erp_test_harness.py's `from services.decision_engine import
# _askable_parameters_by_name, ...`) keeps working unchanged.
from services.action_selection_primitives import (
    _PARAMETER_VALIDATORS,
    _EMAIL_CANDIDATE_RE,
    _extract_candidates_for_binding,
    _extract_structural_candidates,
    _resolve_parameter_validator,
    _bind_candidate_to_parameter,
    _NON_ASKABLE_INPUT_SOURCES,
    _askable_parameters_by_name,
    _keyword_score,
    IDENTIFIER_MEMORY_FIELDS,
    select_requested_mapped_fields,
)
# Hybrid Question Classifier (2026-08-02 Production Integration Sprint,
# Phase 1 Step C) — the SAME classifier the AI Playground's Auto mode
# already uses (services/hybrid_question_classifier.py), never a second
# implementation. Safe to import here (no cycle) because that module now
# depends only on services/action_selection_primitives.py, not on this
# module.
from services.hybrid_question_classifier import (
    classify_question, _QUESTION_MARKER_RE, _REQUEST_MARKER_RE, _split_clauses,
)
# Hybrid Runtime Service (2026-08-02 Production Integration Sprint, Phase
# 1 Step B/C) — the SAME synthesis function the AI Playground's Hybrid
# mode already uses (services/hybrid_runtime_service.py); this module
# never imports services/hybrid_playground_router.py (Playground-only).
from services.hybrid_runtime_service import synthesize_hybrid_answer

# ── Dynamic, Business-Action-driven Information Collection ────────────────
#
# Single Source of Truth refactor: required parameters now come from the
# Business Action Registry (`business_action_parameters` +
# `parameter_groups`), never from a second, independently-maintained
# schema. `INTENT_SCHEMAS` (services/slot_filling_engine.py) is kept ONLY
# as a legacy fallback for Business Actions that have no parameter
# metadata configured yet (see `_handle_legacy_workflow` below) — its
# responsibility is now limited to intent classification / workflow
# hints / conversation wording, never the authoritative "what's
# required" answer for a configured Business Action.
#
# Validation reuses `registry.validate_can_execute()` (existing, in
# services/business_action_registry.py) as the ONE place that decides
# "is this action executable yet" — the exact same function the Generic
# Action Executor itself calls before running. Because both the
# Information Collection step here and the Executor call the identical
# function against the identical stored configuration, the Executor can
# no longer discover a missing required parameter this engine believed
# was already complete.


def _generate_parameter_question(param: Dict) -> str:
    """Auto-generates a natural Thai follow-up question from a Business
    Action parameter's own metadata — Display Name drives the wording.
    Manual override: if the parameter's own `description` field already
    holds custom follow-up text (an admin configured it in the Business
    Action Center), that text is used verbatim instead of auto-generating
    one."""
    override = (param.get("description") or "").strip()
    if override:
        return override
    display = param.get("display_name") or param.get("name")
    return f"กรุณาแจ้ง{display}ค่ะ"


# Generic Collection Status Query (Address Change Full UAT — Status
# Query fix, 2026-08-24) — a customer asking "what do you have so far /
# what's still missing" mid-collection, deterministic and Registry-
# driven like every other conversation-intelligence signal here; never
# tied to one Business Action. Kept intentionally narrow (explicit
# "ข้อมูล"/"ขาด" phrasing) so it can never fire on an ordinary answer
# that happens to share a word with it.
_COLLECTION_STATUS_QUERY_RE = re.compile(
    r"มีข้อมูลอะไร|ข้อมูลที่ให้ไปมีอะไร|ขาดอะไร|ต้องการข้อมูลอะไรอีก|ให้ข้อมูล.{0,15}ไปแล้ว",
    re.IGNORECASE)


def _compose_collection_status_reply(action: Dict, registry, collected: Dict) -> Optional[str]:
    """Answers a Collection Status Query from the SAME real `collected`
    dict and `registry.validate_can_execute()` result every other step
    of this flow already trusts — reuses _compose_field_summary (the
    exact composer the confirmation summary itself calls) for the "have"
    half, and validate_can_execute's own missing-parameter/group list for
    the "still need" half, so it can never invent a value or a field
    name that isn't genuinely present or genuinely missing. Returns None
    only when there is truly nothing to report (no customer_message
    parameters collected AND nothing missing) — the caller falls back to
    ordinary handling in that case."""
    have_summary = _compose_field_summary(action, collected)
    validation = registry.validate_can_execute(action["id"], collected)
    askable = _askable_parameters_by_name(action)
    missing_names = list(validation.get("missing_required") or [])
    for group in validation.get("failed_groups") or []:
        missing_names.extend(m for m in (group.get("members") or []) if m not in collected)
    missing_lines = [f"- {askable[n].get('display_name') or n}" for n in missing_names if n in askable]
    if not have_summary and not missing_lines:
        return None
    parts = [f"ข้อมูลที่ได้รับตอนนี้ค่ะ\n\n{have_summary}" if have_summary else "ยังไม่ได้รับข้อมูลใดๆ ค่ะ"]
    if missing_lines:
        parts.append("ข้อมูลที่ยังขาด:\n" + "\n".join(missing_lines))
    next_after = _next_expected_parameter(action, registry, collected)
    if next_after:
        parts.append(_generate_parameter_question(next_after))
    elif not missing_lines and _requires_confirmation(action):
        # Status Query at the confirmation stage (Task 02, 2026-08-25) —
        # everything askable is already collected, so there is no next
        # PARAMETER question to append, but a confirmation is still
        # genuinely pending. Without this, the status-query reply never
        # mentions (or ends with) the confirmation question at all, so
        # _resolve_continuation_action's own confirmation-matching branch
        # can never recognize the customer's NEXT reply (e.g. a
        # correction, or "ยืนยัน" itself) as continuing this action —
        # mirrors exactly why the parameter-question branch above already
        # needed the same treatment (2026-08-24 fix).
        parts.append(_generate_confirmation_question(action, collected))
    return "\n\n".join(parts)


def _compose_field_summary(action: Dict, collected: Dict) -> str:
    """Generic, config-driven summary composer (2026-08-20) — used both
    to enrich the confirmation-gate question below and to build the
    composed 'Message' system value some NOTIFICATION-type actions
    declare (see _compose_notification_message_system_value). Iterates
    the action's OWN parameters (never a hardcoded field list), emitting
    "{display_name}: {value}" for every customer_message-sourced
    parameter that actually has a collected value — any current or future
    action gets a readable summary automatically just by having ordinary
    parameters with display_name set, no per-action code."""
    lines = []
    for p in action.get("parameters") or []:
        if p.get("input_source") != "customer_message":
            continue
        value = collected.get(p["name"])
        if value:
            lines.append(f"{p.get('display_name') or p['name']}: {value}")
    return "\n".join(lines)


def _generate_confirmation_question(action: Dict, collected: Optional[Dict] = None) -> str:
    """The exact text asked when a COMMAND-type action (see
    _requires_confirmation) has all its parameters collected but hasn't
    been confirmed yet. Factored out to a single function — used both to
    BUILD the question (_execute_selected_action) and to RECOGNIZE a
    reply to it on the next turn (_resolve_continuation_action) — so the
    two can never silently drift apart. `collected`, when given, is a
    pure input (same value in -> same text out at both call sites) — a
    non-empty _compose_field_summary() result is prepended so the
    customer sees exactly what they're about to confirm (2026-08-20,
    Shipping Address Change Request) instead of a bare yes/no; an action
    with no customer_message parameters collected yet is unaffected."""
    action_label = action.get("display_name") or action.get("name") or "การดำเนินการนี้"
    base = f"ยืนยันการดำเนินการ '{action_label}' หรือไม่คะ? กรุณาตอบ 'ยืนยัน' เพื่อดำเนินการต่อ"
    summary = _compose_field_summary(action, collected) if collected else ""
    if summary:
        return f"รบกวนตรวจสอบข้อมูลอีกครั้งนะคะ\n\n{summary}\n\n{base}"
    return base


def _compose_notification_message_system_value(action: Dict, collected: Dict) -> Dict[str, str]:
    """Generic support (2026-08-20) for a NOTIFICATION-type action whose
    real endpoint expects one free-text message field (e.g.
    sendlinenotics's own 'Message' parameter) built from this
    conversation's OTHER collected, customer-message-sourced parameters —
    so a Business Action can be configured with real structured slots
    (a proper per-field Slot Filling experience) while still sending a
    single composed string to an endpoint that only accepts one. Only
    engages for a parameter literally named "Message" with input_source
    "system_generated" — any action may opt into this by shaping its own
    parameters this way; nothing here names a specific action_key."""
    for p in action.get("parameters") or []:
        if p.get("name") == "Message" and p.get("input_source") == "system_generated":
            summary = _compose_field_summary(action, collected)
            if not summary:
                return {}
            header = action.get("display_name") or action.get("name") or ""
            return {"Message": f"{header}\n\n{summary}\n\nสถานะ: ลูกค้ายืนยันข้อมูลแล้ว"}
    return {}


def _next_expected_parameter(action: Dict, registry, collected: Dict) -> Optional[Dict]:
    """Reads registry.validate_can_execute() — the SAME function the
    Executor calls — to find what's still missing, then picks the first
    ASKABLE (non-secret) parameter: first from any ungrouped required
    parameter still missing, else the first not-yet-collected member of
    the first unsatisfied parameter group (AT_LEAST_ONE/ALL/etc — the
    Registry's own generic group-rule evaluation decides when a group is
    satisfied, never re-implemented here)."""
    validation = registry.validate_can_execute(action["id"], collected)
    if validation["ok"]:
        return None
    askable = _askable_parameters_by_name(action)
    for name in validation.get("missing_required") or []:
        if name in askable:
            return askable[name]
    for group in validation.get("failed_groups") or []:
        for name in group.get("members") or []:
            if name not in collected and name in askable:
                return askable[name]
    return None  # nothing left to ask (e.g. only a secret is missing)


_RETRY_LOOKBACK_TURNS = 16  # ~8 exchanges — see Recency-Bounded Retry Window note below


def _count_genuine_retries(registry, history: List[Dict], expected_question: str) -> int:
    """Companion to the Generic Continuation Intent Guard (2026-08-24,
    same fix) — retry_count below is retroactively recomputed from
    `history` every turn (no separate persisted counter, same convention
    as every other conversation-intelligence module here), by counting
    how many times `expected_question` already appears as an assistant
    turn. That alone can't tell "customer ignored/couldn't answer" apart
    from "customer's reply was itself a valid, different (or same-
    action) request that got correctly diverted for that one turn" —
    confirmed live: a customer whose message decisively matched a
    DIFFERENT action (so that turn was properly handled as ITS OWN
    intent, never reaching this action's collection step at all) still
    left the SAME repeated question sitting in history, so a later
    return to this action's own flow found the count already at
    max_retry and escalated immediately. Walks each (assistant question,
    following customer reply) pair; a reply that decisively matches SOME
    Business Action (via the SAME generic search_candidate_actions/
    select_best_action scoring used everywhere else for fresh routing —
    same action or a different one, never re-derived here) is never
    counted as a failed attempt. A reply with no decisive match for
    anything (genuinely off-topic/confused/silent replies) still counts
    exactly as before.

    Recency-Bounded Retry Window (2026-08-29 production fix) — confirmed
    live: a customer_identifier parameter question asked (and left
    unanswered) during one action's collection episode that had already
    been closed out by its own escalation to human handoff was still
    being counted as 2/2 genuine retries almost a full day and ~18-20
    messages later, against a BRAND-NEW same-day request for a completely
    different Business Action that merely happens to need the same
    parameter group — permanently escalating that customer's very FIRST
    attempt at every future request needing that identifier, forever,
    long after the original episode was already closed. `history` here
    carries no timestamp (services/session_service.py::get_recent_history
    only returns role/content), so position within `history` is the only
    recency signal available without changing that caller contract —
    only the most recent _RETRY_LOOKBACK_TURNS entries are scanned, still
    generous enough to preserve the original 2026-08-24 fix's own "customer
    decisively diverts to a different action for a turn or two, then comes
    back" scenario (which resolves within a handful of exchanges in
    practice), while excluding a stale match from many exchanges/a full
    day earlier."""
    count = 0
    recent_history = history[-_RETRY_LOOKBACK_TURNS:] if history else history
    for i, t in enumerate(recent_history):
        if t.get("role") != "assistant" or not _reply_matches_question(
                (t.get("content") or "").strip(), expected_question):
            continue
        if i + 1 < len(recent_history) and recent_history[i + 1].get("role") == "user":
            reply = recent_history[i + 1].get("content") or ""
            candidates = search_candidate_actions(registry, workflow=None, message=reply, collected_slots={})
            if select_best_action(candidates, minimum_score=1.0):
                continue
        count += 1
    return count


def _is_execution_ready(validation: Dict, next_after: Optional[Dict], ambiguous_candidates) -> bool:
    """Confirmed defect fix (2026-08-02, Local Production Pipeline
    Verification), shared by _handle_dynamic_collection and
    _handle_hybrid_turn so the fix lives in exactly one place —
    validation["ok"] alone conflates "the customer hasn't provided a
    required CUSTOMER-facing value yet" with "a credential_store/
    secret_configuration-sourced required parameter isn't in `collected`"
    (it NEVER will be — those are resolved separately, at execution time,
    by the Action Executor's own Credential Store resolution, see
    services/action_executor.py). Before this fix, ANY Business Action
    with a credential-sourced required parameter (i.e. essentially every
    real ERP action) could never actually execute: validation["ok"] was
    always False, looping forever on a generic "need more info" reply
    even after the customer supplied everything askable. Ready to execute
    now means: nothing is missing at all, OR the only remaining gap is
    non-askable (next_after is None, computed via _next_expected_parameter
    above) with no ambiguity left to resolve."""
    if validation["ok"]:
        return True
    if ambiguous_candidates:
        return False
    return next_after is None


def _requires_confirmation(action: Dict) -> bool:
    """Generic, metadata-driven confirmation gate (2026-08-09, SendLineNotiCS
    enablement) — ANY Business Action whose operation type is a COMMAND-type
    real-world side effect (NOTIFICATION/NOTIFY/WORKFLOW today; extensible to
    CREATE/UPDATE/CANCEL/DELETE later, never a per-action special case) must
    never reach the Action Executor without an explicit confirmation for
    THIS turn. Reuses services/erp_test_harness.py's existing, already-tested
    operation-type inference (infer_operation_type — reads
    action.setup_metadata.operation_type first, confidence 0.95) and its
    conversation-defaults table (get_conversation_behavior_defaults) — the
    SAME rule the AI Playground / ERP Conversation Tester's own confirmation
    gate already uses, not a second, divergent copy of it. Imported inline
    to avoid a circular import (erp_test_harness imports FROM decision_engine
    at module level)."""
    from services.erp_test_harness import infer_operation_type, get_conversation_behavior_defaults
    operation_type = infer_operation_type(action)
    return bool(get_conversation_behavior_defaults(operation_type).get("require_confirmation_before_execute"))


def _bind_message_to_action(action: Dict, registry, collected: Dict, message: str,
                             exclude_values: Optional[set] = None) -> Dict[str, str]:
    """Attempts to satisfy whatever is CURRENTLY missing using this
    turn's message. For an ungrouped missing required parameter, binds
    against that specific parameter only (mirrors Contextual Slot
    Binding's single-slot contract). For a still-unsatisfied parameter
    GROUP (e.g. AT_LEAST_ONE), tries every askable member in turn — a
    customer may spontaneously answer with any one alternative (e.g.
    email instead of customer code), not necessarily whichever member
    happens to be listed first. Returns {"valid_candidates": [...]}
    merged into a plain dict update — never mutates `collected` itself,
    caller decides what to keep. `exclude_values` lets a caller (see
    `_bind_all_from_message`) prevent the SAME literal value from being
    bound to a second parameter after it was already consumed by a
    first one within the same message."""
    validation = registry.validate_can_execute(action["id"], collected)
    if validation["ok"]:
        return {"bound": None, "ambiguous_candidates": []}
    askable = _askable_parameters_by_name(action)
    # A URL's own internal structure (domain segments, numeric path
    # components — e.g. the "1688" in "https://detail.1688.com/offer/...")
    # must never be scanned as a free-standing customer-supplied
    # identifier for an UNRELATED parameter. Confirmed live: this exact
    # substring was binding to a different action's "Tracking" parameter,
    # which then out-scored the real URL-consuming action
    # (GetUrlProductDetail) in _resolve_continuation_action's "already
    # collected" tie-break — after asking for CustCode, the customer's
    # very next reply got silently misrouted to the wrong action. The URL
    # itself is captured separately, whole, via _extract_system_values()
    # for any system_generated parameter that wants it; stripping it here
    # only removes it from GENERIC digit/email candidate scanning, never
    # from the message the customer actually sent. Structural candidates
    # are scanned against the stripped text; the whole-message free-text
    # fallback (see _extract_candidates_for_binding's own docstring)
    # still uses the ORIGINAL message when no structural candidate is
    # found at all, so a genuinely free-text parameter's behavior
    # (e.g. SendLineNotiCS's Message) is unchanged.
    scan_message = _URL_RE.sub(" ", message or "")
    structural_candidates = _extract_structural_candidates(scan_message)
    if structural_candidates:
        candidates = list(structural_candidates)
    else:
        # Mirrors _extract_candidates_for_binding's own whole-message
        # fallback (see its docstring) directly, rather than calling it —
        # calling it here would re-run structural extraction on the
        # UNSTRIPPED message and rediscover the very URL substring just
        # excluded above. The ORIGINAL (unstripped) message is used as
        # the free-text candidate itself: a genuinely free-text parameter
        # (e.g. SendLineNotiCS's Message) may legitimately need the URL
        # as part of its own content, unlike a structural digit/email scan.
        trimmed = (message or "").strip()
        candidates = [trimmed] if trimmed else []
    candidates = [c for c in candidates if c not in (exclude_values or set())]
    # Group members deliberately use STRUCTURAL candidates only — never
    # the whole-message free-text fallback _extract_candidates_for_binding
    # offers (see that function's own docstring). A parameter GROUP (e.g.
    # GetDataCustomer's customer_identifier AT_LEAST_ONE) exists to
    # require ONE genuine identifying value; letting a permissive
    # non_empty member swallow an entire unrelated sentence would satisfy
    # the group with zero real identifying information.
    group_candidates = [c for c in structural_candidates if c not in (exclude_values or set())]

    # Try the MORE SPECIFIC validators first (a configured
    # validation_pattern, or a non-generic validation_type like email/
    # phone_number) before a permissive catch-all — this is what lets
    # two ungrouped required parameters with the SAME shape otherwise
    # (e.g. CustCode vs OrderCode) be told apart once each has its own
    # pattern configured, instead of the first-in-order one always
    # winning regardless of which value the customer actually gave.
    missing_required = sorted(
        validation.get("missing_required") or [],
        key=lambda n: not (askable.get(n, {}).get("validation_pattern")
                            or askable.get(n, {}).get("validation_type") not in (None, "non_empty")))
    for name in missing_required:
        param = askable.get(name)
        if not param:
            continue
        # Address-component-tagged parameters (services/thai_address_
        # parser.py's field_metadata.address_component -- e.g. ReceiverName,
        # Subdistrict) are populated exclusively by their own dedicated
        # pre-pass in _bind_all_from_message, never by this generic
        # whole-message free-text fallback. Confirmed live (2026-08-20
        # Production UAT): a loosely-validated field like ReceiverName
        # (validation_type "non_empty") otherwise greedily swallows an
        # entire unrelated sentence -- even the customer's own REQUEST
        # message itself ("ต้องการเปลี่ยนที่อยู่บิลขนส่ง") -- whenever that
        # message has no structural (digit/email) candidate at all, since
        # the fallback only requires SOME non-empty text. Restricting
        # these parameters to structural candidates only mirrors exactly
        # how a parameter GROUP member already excludes the same fallback
        # a few lines below, for the same reason.
        is_address_component = bool((param.get("field_metadata") or {}).get("address_component"))
        param_candidates = group_candidates if is_address_component else candidates
        binding = _bind_candidate_to_parameter(param_candidates, param)
        if binding["status"] == "bound":
            return {"bound": (name, binding["value"]), "ambiguous_candidates": []}
        if binding["status"] == "ambiguous":
            return {"bound": None, "ambiguous_candidates": binding["valid_candidates"]}

    for group in validation.get("failed_groups") or []:
        members = [m for m in (group.get("members") or []) if m not in collected and m in askable]
        # Try the MORE SPECIFIC validators first (email/phone_number)
        # before a permissive catch-all (non_empty/None) — otherwise a
        # loosely-validated member listed earlier in the group (e.g.
        # CustCode with validation_type "non_empty") would greedily
        # accept a value clearly meant for a stricter sibling (e.g. an
        # email meant for CustEmail), miscategorizing it.
        members.sort(key=lambda name: askable[name].get("validation_type") in (None, "non_empty")
                     and not askable[name].get("validation_pattern"))
        for name in members:
            binding = _bind_candidate_to_parameter(group_candidates, askable[name])
            if binding["status"] == "bound":
                return {"bound": (name, binding["value"]), "ambiguous_candidates": []}
            if binding["status"] == "ambiguous":
                return {"bound": None, "ambiguous_candidates": binding["valid_candidates"]}

    return {"bound": None, "ambiguous_candidates": []}


def _bind_all_from_message(action: Dict, registry, collected: Dict, message: str) -> Dict:
    """Binds AS MANY still-missing parameters as this single message
    actually provides (e.g. "เช็ค PO202601001 ของลูกค้า C00001" contains
    both CustCode and OrderCode) — `_bind_message_to_action` on its own
    only ever finds one match per call, so this repeatedly re-invokes it
    against a growing `collected` dict, excluding values already
    consumed, until a turn makes no further progress. Returns
    {"collected": updated dict (new copy), "ambiguous_candidates": [...]}."""
    working = dict(collected)

    # Semantic Parameter Inference (2026-08-09) — runs FIRST, separately
    # from the required-parameter binding loop below, because it targets
    # OPTIONAL filter parameters (Latest, BillStatus, POStatus, date
    # ranges) that _bind_message_to_action would never touch at all (it
    # only ever tries to satisfy MISSING REQUIRED parameters/groups).
    # Entirely metadata-driven (services/semantic_parameter_inference.py
    # reads each parameter's own field_metadata) — no action-specific
    # code here or there. Never overwrites a value collected from an
    # earlier turn; only fills in parameters still absent.
    from services.semantic_parameter_inference import infer_semantic_parameters
    semantic_matches = infer_semantic_parameters(action.get("parameters") or [], message)
    for name, value in semantic_matches.items():
        working.setdefault(name, value)

    # Thai Address Compound Parsing + Field Correction (2026-08-20,
    # Shipping Address Change Request) — same metadata-driven convention
    # as Semantic Parameter Inference above: field_metadata.address_component
    # tags which of THIS action's parameters holds which parsed address
    # piece (e.g. {"address_component": "province"}); an action with no
    # such tags is completely unaffected. Lets a customer supply a full
    # address block in one message ("8/7 ม.8 ต.ตาขัน อ.บ้านค่าย จ.ระยอง
    # 21120") and have it decomposed into separate slots instead of being
    # asked for each piece individually. Field correction is the one
    # deliberate exception to "never overwrite" -- its whole purpose is
    # undoing a mistake in an ALREADY-collected value before confirmation,
    # never discarding the sibling fields collected alongside it, so it
    # takes priority and skips the compound parse for the same message
    # (a correction like "จังหวัดผิด เป็นชลบุรี" would otherwise itself get
    # misparsed as a full address block by the generic parser above).
    address_param_by_component = {
        (p.get("field_metadata") or {}).get("address_component"): p["name"]
        for p in (action.get("parameters") or [])
        if (p.get("field_metadata") or {}).get("address_component")
    }
    # Address Change Full UAT fix (2026-08-24) — `used_values` (below)
    # must ALREADY contain whatever this pre-pass itself just consumed
    # from `message` before the generic required-parameter loop gets a
    # turn. Confirmed live: a natural, unlabeled address block ("...เบอร์
    # 0812345678 อยู่ 99/12 หมู่ 4 ...10540") got its phone/postal-code
    # substrings correctly attributed here, but — since this pre-pass
    # never recorded them as "used" — the SAME raw substrings were then
    # re-offered as fresh, unclaimed candidates to the free-form Address
    # parameter's loose non_empty validator (which happily accepts ANY
    # non-empty string), manufacturing a false "found 3 possible values,
    # which one?" ambiguity out of values that were already correctly
    # assigned elsewhere.
    used_values: set = set()
    if address_param_by_component:
        from services.thai_address_parser import parse_thai_address, detect_field_correction
        correction = detect_field_correction(message)
        if correction and correction[0] in address_param_by_component:
            working[address_param_by_component[correction[0]]] = correction[1]
        else:
            for component, value in parse_thai_address(message).items():
                param_name = address_param_by_component.get(component)
                if param_name and param_name not in working:
                    working[param_name] = value
                    used_values.add(value)
                    # Status Query fix companion (2026-08-24) — also
                    # exclude any structural sub-token WITHIN this
                    # value (e.g. the house-number "99/12" inside a full
                    # Address value "99/12 หมู่ 4") from the generic
                    # required-parameter loop below. Confirmed live: when
                    # a DIFFERENT geo field (e.g. District) has no marker
                    # in the message at all, that same sub-token was
                    # picked up FRESH from the raw message — via its own,
                    # independent, shorter structural-candidate match —
                    # and bound to the unrelated missing field, since only
                    # the FULL address string had been recorded as used,
                    # never the sub-token also living inside it.
                    used_values.update(_extract_structural_candidates(value))

    ambiguous_candidates: List[str] = []
    while True:
        outcome = _bind_message_to_action(action, registry, working, message, exclude_values=used_values)
        if outcome["bound"]:
            name, value = outcome["bound"]
            working[name] = value
            used_values.add(value)
            continue
        if outcome["ambiguous_candidates"] and not used_values:
            ambiguous_candidates = outcome["ambiguous_candidates"]
        break
    return {"collected": working, "ambiguous_candidates": ambiguous_candidates}


def _apply_identifier_memory(action: Dict, collected: Dict, customer_context: Optional[Dict]) -> Dict[str, str]:
    """Fills any still-missing ASKABLE parameter from Identifier Memory
    (a value the customer already established earlier — this
    conversation or a prior one — persisted onto their profile by
    profiles/manager.py::update_profile_from_turn, keyed by
    IDENTIFIER_MEMORY_FIELDS). Never overrides a value already present in
    `collected` — an explicit/fresher value always wins. Returns a NEW
    dict; never mutates the one passed in."""
    if not customer_context:
        return collected
    working = dict(collected)
    askable = _askable_parameters_by_name(action)
    for profile_field, param_name in IDENTIFIER_MEMORY_FIELDS:
        if param_name in working or param_name not in askable:
            continue
        remembered = customer_context.get(profile_field)
        if remembered:
            working[param_name] = remembered
    return working


def _last_assistant_turn_requests_input(text: str) -> bool:
    """Cheap, no-DB pre-check: could the last assistant turn plausibly be
    a parameter / confirmation / identity-verification request that a
    customer's next message would be CONTINUING?

    _resolve_continuation_action's deep replay (get_full() per enabled
    API/WEBHOOK action + a 20-turn history walk = dozens of Supabase
    calls) can ONLY ever return a match when the last assistant turn
    matches one of these generated shapes (see _match_against /
    _reply_matches_question). If it matches none of them, the replay is
    guaranteed to return [] — so for an ordinary standalone message
    (greeting / independent RAG / independent General question) that
    merely happens to sit after old ERP history, we skip the replay
    entirely. This is a shape/state check, never a hardcoded
    greeting/keyword list. A genuine mid-collection continuation also
    carries an explicit pending_confirmations row (handled by
    line_bot/webhook.py before decide()), so nothing that truly continues
    is lost here."""
    t = (text or "").strip()
    if not t:
        return False
    # auto-generated parameter question  ->  f"กรุณาแจ้ง{display}ค่ะ"
    # identity-verification asks          ->  contain "รบกวนแจ้ง...บัญชี"
    if "แจ้ง" in t and ("กรุณา" in t or "รบกวน" in t):
        return True
    # confirmation question (_generate_confirmation_question)
    if "ยืนยันการดำเนินการ" in t or "กรุณาตอบ 'ยืนยัน'" in t:
        return True
    # Collection Status Query reply (always ends with the same generated
    # question, recognised by _reply_matches_question's "startswith" arm)
    if "ข้อมูลที่ได้รับตอนนี้" in t or "ยังไม่ได้รับข้อมูลใดๆ" in t or "ข้อมูลที่ยังขาด" in t:
        return True
    if t in (_SELF_VERIFY_ASK_PHONE_TEXT, _SELF_VERIFY_ASK_EMAIL_TEXT):
        return True
    return False


def _reply_matches_question(actual_text: str, expected_question: str) -> bool:
    """Tolerant match between an assistant turn's actual text and a
    generated parameter/confirmation question, used everywhere history-
    replay or continuation-resolution needs to recognize "this reply was
    generated by asking for this exact thing". An exact match is the
    common case; two known variations also count, since both keep the
    generated question intact and recognizable, just with extra text
    attached on one side: a Collection Status Query reply prepends a
    preamble before the same question (2026-08-24, "ข้อมูลที่ได้รับตอนนี้
    ค่ะ...\n\n{question}"), and a Multi-Intent Preservation acknowledgment
    (Task 03, 2026-08-26) appends a short sentence after it
    ("{question}\n\nรับทราบอีกเรื่อง..."). Without the latter tolerance, a
    compound trigger message's own acknowledged reply would fail every
    later exact-string comparison used to reconstruct collection state,
    silently discarding every parameter collected up to that point."""
    if not expected_question:
        return False
    return (actual_text == expected_question
            or actual_text.endswith("\n\n" + expected_question)
            or actual_text.startswith(expected_question + "\n\n"))


def _replay_business_action_collection(action: Dict, registry, history: List[Dict],
                                        customer_context: Optional[Dict] = None) -> Dict[str, str]:
    """Reconstructs 'Collected Parameters' purely from `history` — no
    separate persistence table, same convention every other
    conversation-intelligence module in this codebase already follows.
    Walks turns in order; whenever an assistant turn's text matches
    EXACTLY the question this engine would have generated for whatever
    parameter was next-expected at that point, the following user turn
    is bound as that parameter's answer. This generalizes the legacy
    engine's own "does the prior assistant turn match my follow-up
    question" continuation trick to an arbitrary number of dynamically-
    ordered parameters instead of one fixed schema question.

    Identifier-Memory Replay Desync fix (Task 02, 2026-08-25) —
    `customer_context` seeds `collected` with Identifier-Memory values
    BEFORE the walk begins, exactly like _handle_dynamic_collection's own
    live turn already does. Without this, replay's internal "next
    expected parameter" tracking silently desyncs from the REAL
    conversation the moment an earlier turn's actual next-question was
    influenced by an Identifier-Memory fill-in the pure-text replay can
    never see (e.g. CustCode resolved from the profile rather than typed)
    — every SUBSEQUENT turn's `preceding.content != expected_question`
    check then mismatches, silently discarding that turn's real, already-
    accepted answer (e.g. a validly supplied ShipmentCode) from every
    later reconstruction. Confirmed live: SP1008 (Identifier Memory) ->
    "อยากเปลี่ยนที่อยู่..." (asks ShipmentCode) -> "SP100820260810006"
    (accepted, correctly bound) -> a later turn needing to replay through
    that point reconstructed collected={} instead of {ShipmentCode:...},
    re-asking for a field the customer had already supplied."""
    collected: Dict[str, str] = _apply_identifier_memory(action, {}, customer_context)
    for i, turn in enumerate(history):
        if turn.get("role") != "user":
            continue
        if i == 0:
            # Cross-Topic Contamination fix (Task 02B, 2026-08-25) — the
            # very first user turn in `history` is only trusted as THIS
            # action's own trigger message if the very next assistant
            # turn is consistent with THIS action having generated it
            # (one of its own parameter questions, given what tentatively
            # binding this message would collect, or its confirmation
            # question) — never unconditionally. `history` is the full
            # recent session window (session_service.get_recent_history),
            # not a per-action transcript, so array index 0 is NOT
            # reliably "where this action's collection began" — it may
            # equally well be an EARLIER, unrelated topic's message that
            # merely happens to sit at the front of a truncated window.
            # Confirmed live: an earlier "SP1008 order ล่าสุด" turn got
            # bound to a LATER, unrelated address-change action's own
            # ReceiverName (a loosely-validated non_empty field) purely
            # because it occupied history[0] — validation answers "is
            # this value structurally allowed", never "does this turn
            # belong to this action's slot", and only the surrounding
            # question/answer chain can answer that. A genuinely fresh,
            # single-topic conversation (including a first message that
            # fully satisfies every parameter in one shot, e.g.
            # SendLineNotiCS's free-text Message) is completely
            # unaffected: its own next assistant turn is, by definition,
            # generated by/for this exact action, so the check below
            # passes and behavior is identical to before this fix.
            tentative = _bind_all_from_message(action, registry, collected, turn.get("content") or "")
            tentative_collected = tentative["collected"]
            next_turn = history[1] if len(history) > 1 else None
            if next_turn is not None and next_turn.get("role") == "assistant":
                next_text = (next_turn.get("content") or "").strip()
                tentative_next = _next_expected_parameter(action, registry, tentative_collected)
                if tentative_next:
                    expected_q = _generate_parameter_question(tentative_next)
                    is_this_action = _reply_matches_question(next_text, expected_q)
                elif _requires_confirmation(action):
                    expected_q = _generate_confirmation_question(action, tentative_collected)
                    is_this_action = _reply_matches_question(next_text, expected_q)
                else:
                    # Nothing left to ask and no confirmation gate — this
                    # action would already be complete/executed by this
                    # single message, so there is no question to check
                    # against; trust it (matches the pre-fix behavior for
                    # this specific shape).
                    is_this_action = True
                if not is_this_action:
                    continue  # not this action's trigger — skip, don't bind
            collected = tentative_collected
            continue
        preceding = history[i - 1]
        if preceding.get("role") != "assistant":
            continue
        next_param = _next_expected_parameter(action, registry, collected)
        if not next_param:
            continue
        expected_q = _generate_parameter_question(next_param)
        preceding_matches = _reply_matches_question((preceding.get("content") or "").strip(), expected_q)
        # Mid-Collection Interruption Continuation fix (Customer Journey
        # UAT, 2026-08-27) — companion to the identical fix in
        # _resolve_continuation_action. A genuine RAG-answered interruption
        # (e.g. "CBM คืออะไรครับ" answered while Address is pending) sits
        # as its own (user, assistant) pair immediately before this turn,
        # so `preceding` is the INTERRUPTION's answer, never the real
        # pending question — replay silently skipped binding this turn
        # entirely (never even reaching _bind_all_from_message, so a field
        # correction inside it — services/thai_address_parser.py::
        # detect_field_correction — could never be replayed forward into
        # any LATER turn's reconstruction either). Confirmed live: a
        # ReceiverName correction sent right after such an interruption was
        # correctly applied to THAT turn's own live collected_parameters,
        # but reverted back to the stale original on the very NEXT turn,
        # because replaying up through the correction for that next turn
        # hit exactly this skip. Only looks back exactly one interruption
        # pair (i-3: past the interruption's own assistant reply AND its
        # own user question) — never an unbounded lookback.
        if not preceding_matches and i >= 3:
            further_back = history[i - 3]
            if further_back.get("role") == "assistant":
                preceding_matches = _reply_matches_question(
                    (further_back.get("content") or "").strip(), expected_q)
        if not preceding_matches:
            continue
        result = _bind_all_from_message(action, registry, collected, turn.get("content") or "")
        collected = result["collected"]
    return collected


# Self-Service Identity Verification (2026-08-29) — the customer-facing
# question text for each step of the verify-with-a-second-factor sub-flow
# (DecisionEngine._attempt_self_verification). Matched EXACTLY against
# the most recent assistant turn (same convention as
# _generate_parameter_question's own literal-template matching elsewhere
# in this file) to recover which step of the sub-flow the CURRENT
# message is answering — no separate persisted state, same "recompute
# from history" convention this whole module already follows everywhere
# else. Module-level (not a class attribute) so _resolve_continuation_
# action's own nested _match_against closure — a plain function, no
# `self` in scope — can reference them too (see its own comment there).
_SELF_VERIFY_ASK_PHONE_TEXT = "เพื่อยืนยันตัวตนก่อนดูข้อมูลนี้ รบกวนแจ้งเบอร์โทรที่ผูกกับบัญชีลูกค้าด้วยค่ะ"
_SELF_VERIFY_ASK_EMAIL_TEXT = "เบอร์โทรที่แจ้งมาไม่ตรงกับข้อมูลในระบบค่ะ รบกวนแจ้งอีเมลที่ผูกกับบัญชีลูกค้าแทนได้ไหมคะ"


def _all_collected_values_seen_in_history(collected: Dict, history: List[Dict]) -> bool:
    """True when every collected parameter value can be found verbatim in
    SOME customer message within `history` — i.e., the customer actually
    typed it in THIS conversation, rather than it being silently
    inherited purely from customer_context/Identifier Memory prefill
    (services/action_selection_primitives.py::IDENTIFIER_MEMORY_FIELDS).

    Phantom-Identifier Disambiguation companion fix (2026-08-31) — used
    ONLY by _resolve_continuation_action's Self-Service Identity
    Verification branch above, to reject a candidate action whose
    "fully collected" status is an illusion built entirely from stale,
    cross-session memory rather than anything the customer said this
    conversation. A value that isn't a non-empty string (None, a number,
    a nested dict) is skipped, not treated as a failure — this only
    guards against a PHANTOM identifier string, never penalizes an
    ordinary parameter with nothing meaningful to check."""
    user_texts = [str(t.get("content") or "") for t in (history or []) if t.get("role") == "user"]
    for value in (collected or {}).values():
        if not value or not isinstance(value, str):
            continue
        if not any(value in text for text in user_texts):
            return False
    return True


def _resolve_continuation_action(registry, history: List[Dict], workflow_hint: Optional[str] = None,
                                  customer_context: Optional[Dict] = None) -> Optional[Dict]:
    """Conversation Continuation without a persistence layer: if the
    LAST assistant turn is exactly the question some enabled, parameter-
    having API/WEBHOOK Business Action would currently ask (given what
    history-so-far has already collected for it), that action is still
    "active" this turn — the Registry-driven equivalent of services/
    slot_filling_engine.py::resolve_active_erp_intent()'s own follow-up-
    question matching, generalized from one fixed schema question per
    workflow to any action's dynamically-generated question.

    Known limitation: two Business Actions can legitimately generate the
    IDENTICAL auto-question (e.g. two unrelated actions both have a
    `CustCode` parameter displayed as "รหัสลูกค้า"). When more than one
    match is found, this breaks the tie using (1) the workflow hint's
    category, then (2) a keyword/category score against the
    conversation's original triggering message — never an arbitrary
    "whichever the registry returned first" order."""
    if not history:
        return None
    assistant_indices = [i for i, t in enumerate(history) if t.get("role") == "assistant"]
    if not assistant_indices:
        return None

    # Latency short-circuit (2026-08-31) — the deep per-action, per-
    # history-turn replay below only ever produces a match when the LAST
    # assistant turn was itself a parameter / confirmation / identity
    # request (that is the entire premise of _match_against). If it
    # wasn't, there is nothing to continue: skip the replay instead of
    # fetching get_full() for every enabled API/WEBHOOK action and
    # walking 20 turns of history — dozens of Supabase calls — for an
    # ordinary greeting / independent RAG / independent General message
    # that merely follows old ERP history. Real mid-collection
    # continuations reach decide() with an explicit pending_confirmations
    # row (line_bot/webhook.py resolves it first), so this loses nothing.
    # Check the last assistant turn, and — matching the one-interruption-
    # pair look-back the _match_against branch further down already
    # allows — the one before it (so "asked -> customer interrupted ->
    # customer answers" still continues). Never scan further than that.
    _recent_asst = assistant_indices[-2:]
    if not any(_last_assistant_turn_requests_input(history[i].get("content")) for i in _recent_asst):
        return None

    try:
        candidates = registry.enabled_actions()
    except Exception:
        return None

    def _match_against(last_text: str, replay_history: List[Dict], *, allow_confirmation: bool) -> List[Dict]:
        found = []
        for action in candidates:
            if action.get("action_type") not in ("API", "WEBHOOK"):
                continue
            # `candidates` are bare rows from enabled_actions() (no joined
            # parameters table) — must fetch the full record before knowing
            # whether this action actually has parameter metadata.
            full_action = registry.get_full(action["id"], mask_secrets=False)
            if not (full_action.get("parameters") or full_action.get("parameter_groups")):
                continue
            collected_so_far = _replay_business_action_collection(full_action, registry, replay_history, customer_context)
            next_param = _next_expected_parameter(full_action, registry, collected_so_far)
            # Status Query companion fix (2026-08-24) — a Collection Status
            # Query reply ("ข้อมูลที่ได้รับตอนนี้ค่ะ...\n\n{same question}")
            # always ENDS with this exact same generated question/
            # confirmation text (see _compose_collection_status_reply), but
            # is never IDENTICAL to it — confirmed live: continuation
            # resolution went straight to None after a status-query turn
            # (an exact-equality check only), so the very next reply (a
            # correction, or any answer with no identifier/keyword of its
            # own to win fresh selection by coincidence) fell through to RAG
            # entirely. Accepting "ends with" alongside exact-equality only
            # ever ADDS a recognized match; every existing exact-match case
            # is completely unaffected.
            if next_param:
                expected_q = _generate_parameter_question(next_param)
                if _reply_matches_question(last_text, expected_q):
                    found.append(full_action)
            elif next_param is None and last_text in (
                    _SELF_VERIFY_ASK_PHONE_TEXT, _SELF_VERIFY_ASK_EMAIL_TEXT):
                # Self-Service Identity Verification (2026-08-29) — all
                # required parameters are collected (next_param is None)
                # but execution was denied for lack of a verified binding,
                # so decision_engine._execute_selected_action's own denial
                # branch asked one of these two questions instead of
                # executing. Without this, the customer's phone/email
                # reply has no identifier/keyword pattern of its own to
                # win FRESH re-selection, and (confirmed live) a bare
                # phone-number-shaped reply gets mis-matched to a
                # completely different action (e.g. an order-lookup
                # action whose own next-missing parameter also happens to
                # be numeric) instead of continuing THIS action's
                # verification step. Reusing requires_verified_identity()
                # keeps this scoped to only actions that could actually
                # have asked this question in the first place.
                #
                # Phantom-Identifier Disambiguation companion fix
                # (2026-08-31) — confirmed live: a STALE, cross-session
                # OrderCode sitting in customer_context (from an entirely
                # earlier, unrelated conversation's Identifier Memory —
                # see services/action_selection_primitives.py::
                # IDENTIFIER_MEMORY_FIELDS) made a COMPLETELY DIFFERENT
                # action (e.g. an order lookup, sharing the SAME CustCode
                # but with its own OrderCode silently seeded from that
                # stale memory, never actually typed this conversation)
                # look "fully collected" at the exact same moment THIS
                # action's self-verification question was pending — both
                # then qualified as `found` candidates, and the tie-break
                # below picked the wrong one, executing against an
                # identifier the customer never gave in this conversation
                # at all. requires_verified_identity() alone can't tell
                # them apart (both need identity verification); only a
                # candidate whose EVERY collected value can actually be
                # found in the customer's own messages this conversation
                # is trusted here — a phantom value inherited purely from
                # customer_context never survives this check.
                from services.authorization_service import requires_verified_identity
                if requires_verified_identity(full_action) and _all_collected_values_seen_in_history(
                        collected_so_far, replay_history):
                    found.append(full_action)
            elif next_param is None and allow_confirmation and _requires_confirmation(full_action):
                # The last assistant turn was THIS action's confirmation-gate
                # question (2026-08-09, SendLineNotiCS enablement) — the
                # customer's reply this turn (e.g. "ยืนยัน"/"ยกเลิก", or a
                # correction) is an answer to it, not a fresh, independently-
                # routable message. Also accepts a Collection Status Query
                # reply ending with this same confirmation question (Task 02,
                # 2026-08-25 companion to the 2026-08-24 parameter-question
                # fix above) — mirrors that fix exactly, for the same reason:
                # a status-query turn at the confirmation stage must not
                # silently break continuation for whatever comes next.
                expected_confirmation_q = _generate_confirmation_question(full_action, collected_so_far)
                if _reply_matches_question(last_text, expected_confirmation_q):
                    found.append(full_action)
        return found

    last_idx = assistant_indices[-1]
    last_text = (history[last_idx].get("content") or "").strip()
    matches = _match_against(last_text, history[:-1], allow_confirmation=True) if last_text else []

    # Mid-Collection Interruption Continuation fix (Customer Journey UAT,
    # 2026-08-27) — confirmed live: a genuine RAG-answered interruption
    # mid-collection (e.g. "CBM คืออะไรครับ" answered while Address is
    # pending — Task 03's own "Mid-Collection RAG Diversion fix" lets this
    # answer through, exactly as intended) becomes the new last assistant
    # turn, so the NEXT customer turn (any reply that carries no keyword/
    # identifier-pattern evidence of its own to win FRESH selection by
    # coincidence — a correction like "ชื่อผู้รับไม่ใช่สมชาย เป็นสมศักดิ์" is
    # exactly this shape) can never match ANY action's expected question
    # here, falls through to RAG entirely, and _bind_message_to_action's
    # own field_correction handling (services/thai_address_parser.py::
    # detect_field_correction) never even gets a chance to run. A message
    # that DOES carry its own strong signal (e.g. a full address block)
    # was already unaffected by this — it wins FRESH re-selection via
    # search_candidate_actions independently of continuation resolution,
    # which is why this went unnoticed until a correction-shaped reply was
    # tested. Fix: if the true last assistant turn matches nothing, retry
    # against the assistant turn from ONE turn further back (skipping
    # exactly the most recent interruption's own exchange, never more) —
    # _replay_business_action_collection already tolerates an interruption
    # pair sitting in the middle of history when reconstructing collected
    # slots (proven by the address-block case above), so only the
    # question-matching check itself needed to look past it too.
    #
    # allow_confirmation=False here is deliberate and load-bearing:
    # confirmed live (Task 02C regression) that allowing the confirmation-
    # question branch through this look-back path resurrects an ALREADY-
    # EXECUTED action — a completed action's own "ยืนยัน" reply plus its
    # completion message look, textually, exactly like "one interruption
    # pair" too, but must never be reopened by a later, unrelated message
    # (e.g. a bare name sent long after checkout). The parameter-question
    # branch has no such risk (an action can't be "done" while it still
    # has a next parameter to ask), so only that branch is retried here.
    if not matches and len(assistant_indices) >= 2:
        prior_idx = assistant_indices[-2]
        prior_text = (history[prior_idx].get("content") or "").strip()
        if prior_text and prior_text != last_text:
            lookback_matches = _match_against(prior_text, history[:prior_idx + 1], allow_confirmation=False)
            # Terminal-Outcome Guard (Mixed Routing / Stuck Workflow
            # production fix, 2026-08-27) — confirmed live: the look-back
            # above assumes the skipped exchange (prior_text ... last_text)
            # is always a genuine mid-collection RAG interruption, but the
            # SAME shape (one assistant turn that doesn't match anything,
            # preceded by one that does) also occurs when an action reached
            # a TERMINAL outcome — e.g. CustCode was supplied, the action
            # executed via the API/WEBHOOK executor, and authorization was
            # DENIED (last_text is that denial message, not a follow-up
            # question). That action is done, not "waiting on its next
            # parameter" — its own denial reply must never be mistaken for
            # an unrelated interruption and used to re-open it for a later,
            # completely unrelated message. Distinguish the two by
            # replaying each look-back match against the FULL prior
            # history (not just the truncated slice up to prior_idx): if
            # the action is ALREADY complete once the turns in between are
            # accounted for, the gap was a terminal outcome, not an
            # interruption, and this match must be dropped.
            matches = [
                a for a in lookback_matches
                if _next_expected_parameter(
                    a, registry, _replay_business_action_collection(a, registry, history[:-1], customer_context)
                ) is not None
            ]

    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]
    if workflow_hint:
        by_category = [a for a in matches if a.get("category") == workflow_hint]
        if len(by_category) == 1:
            return by_category[0]
        if by_category:
            matches = by_category

    # Already-Collected-Parameter Preference (Customer UAT fix,
    # 2026-08-17) — runs BEFORE the keyword-score tie-break below. Many
    # Business Actions share an identical generated clarification
    # question (e.g. every action whose next-missing parameter is
    # CustCode asks the exact same "กรุณาแจ้งรหัสลูกค้าค่ะ"), so several
    # unrelated actions routinely tie here. The action that already has
    # MORE of its own parameters bound from history-so-far is the one
    # genuinely "in progress" — preferring it over a bare keyword score
    # against the ORIGINAL trigger message stops an already-known
    # identifier (e.g. a just-supplied OrderCode) from being silently
    # discarded by switching to an unrelated sibling action that merely
    # scores higher on a short, coincidentally-matching keyword.
    collected_counts = [(a, len(_replay_business_action_collection(a, registry, history[:-1], customer_context)))
                        for a in matches]
    max_collected = max(c for _, c in collected_counts)
    if max_collected > 0:
        matches = [a for a, c in collected_counts if c == max_collected]
        if len(matches) == 1:
            return matches[0]

    trigger_message = next((t.get("content") or "" for t in history if t.get("role") == "user"), "")
    return max(matches, key=lambda a: _keyword_score(a, trigger_message))


# Conversation Resolver (Final Conversational Correctness, 2026-08-15) —
# a SMALL, fixed set of generic Thai referring-expression patterns that
# mean "continue talking about whatever we were just discussing" rather
# than a fresh topic, used ONLY as a fallback signal when the message
# names no specific field of its own (see _resolve_conversation_reference
# — field-keyword evidence, config-driven and unbounded, always wins
# first). Deliberately narrow and idiomatic — see that function's own
# docstring for why this can only ever engage as a last resort, never
# pre-empting a real keyword/pattern match.
_REFERENCE_MARKER_RE = re.compile(
    r"ล่าสุด|ของผม|ของฉัน|ของดิฉัน|อันนี้|รายการนี้|ตอนนี้|แล้ว.*ล่ะ|ถึงหรือยัง|สถานะ",
    re.IGNORECASE)

# Declarative Service-Intent Marker (P0 Final Fix, Business Action vs
# informational boundary, 2026-08-28) — confirmed live on the REAL
# deployed LINE webhook path: "ผมต้องการสั่งซื้อสินค้าจากจีนครับ" (a general
# declarative statement of interest in a Shipify service, no question
# particle at all) got permanently stuck answering "กรุณาแจ้งรหัสลูกค้าค่ะ"
# whenever ANY earlier turn in the same conversation had left an
# incomplete Business Action pending (e.g. an unverified "เช็ก Shipment
# ของผมให้หน่อย") — both the Mid-Collection RAG Diversion guard below and
# the General Informational Question Guard further down only ever
# considered a message "informational" when it was phrased as a QUESTION
# (_QUESTION_MARKER_RE: ยังไง/อะไร/ไหม/...), so a declarative "ต้องการ/
# อยาก/สนใจ" statement with no question particle never even reached either
# guard's own classify_question/veto check. This is the declarative-
# statement counterpart to _QUESTION_MARKER_RE — used ONLY to decide
# whether to RE-EXAMINE a message via the existing classify_question/
# selection-veto machinery (never bypasses it, never itself decides
# anything), so a declarative statement that genuinely IS still about a
# pending private action is unaffected by this alone.
_DECLARATIVE_INTENT_MARKER_RE = re.compile(r"ต้องการ|อยาก|สนใจ")

# Subject-Pronoun Want-Verb Pattern (P0 Final Fix, 2026-08-28) — "ผม/ฉัน/
# ดิฉัน" used as the grammatical SUBJECT of a want/interest verb ("ผม
# ต้องการ...", "ผมอยาก...", "ผมสนใจ...") is NOT the same signal as the
# bare-pronoun POSSESSIVE usage the existing General Informational
# Question Guard's `_bare_self_reference` check exists for (e.g. "Wallet
# ผมเหลือเท่าไหร่" = "MY wallet has how much left" — a genuine, already-
# proven account-specific question that must stay ERP). Confirmed live:
# per the task's own explicit requirement, "ผม" alone must never be
# treated as equivalent to "ของผม"/"Order ของผม"/"Wallet ผม" — this narrow
# pattern is what tells the two apart: "ผม" immediately followed by a
# want-verb is a plain declarative subject ("I want..."), never a
# possessive reference to a private noun.
_SUBJECT_INTENT_RE = re.compile(r"(ผม|ฉัน|ดิฉัน)\s*(ต้องการ|อยาก|สนใจ)")

# Private-Action Verb Evidence (P0 Final Fix, 2026-08-28) — a declarative
# "ต้องการ/อยาก/สนใจ" statement that ALSO names a concrete lookup/action
# verb ("เช็ก"/"ดู"/"ยอด"/"แก้"/"เปลี่ยน"/"ยกเลิก") is real account-specific
# intent, not general service interest — e.g. "อยากเปลี่ยนที่อยู่จัดส่งครับ"
# (a genuine, already-proven address-change request) must never be swept
# into "informational" purely because it also says "อยาก". Checked by
# BOTH declarative-intent guards below so they can never disagree about
# what counts as real evidence. "สถานะ"/"รายละเอียด" are deliberately not
# repeated here — already covered by the existing _REFERENCE_MARKER_RE /
# _DETAIL_INTENT_RE this same code already checks.
_PRIVATE_ACTION_VERB_RE = re.compile(r"เช็ก|ดู|ยอด|แก้|เปลี่ยน|ยกเลิก")

# Private State/Value Query Evidence (P0 Final Presentation Hardening,
# 2026-08-28) — confirmed live: "มี Order อะไรอยู่บ้าง" and "มีคูปองเหลือ
# ไหมครับ" (both asking about a CURRENT VALUE the customer already has,
# with no self-reference pronoun and no request marker at all) were
# wrongly vetoed by the guard below purely for carrying a question
# marker ("อะไร"/"ไหม") — unlike เช็ก/ดู/แก้/เปลี่ยน/ยกเลิก above (which
# genuinely CAN form a legitimate "how does this process work" question,
# e.g. "เปลี่ยนที่อยู่จัดส่งในระบบยังไง"), a "เหลือ" (remaining)/"ยอด"
# (balance) reference or a "มี...อะไร/บ้าง" ("what do I currently have")
# shape has no such informational reading in natural Thai — asking "เหลือ
# เท่าไหร่"/"มีอะไรบ้าง" is never "explain how the remaining-balance
# concept works in general," so this evidence counts UNCONDITIONALLY
# (regardless of question-marker presence), exactly like the existing
# self-reference/reference-marker checks below already do.
_PRIVATE_STATE_QUERY_RE = re.compile(r"เหลือ|ยอดคงเหลือ|มี.{0,15}(อะไร|บ้าง)")

# A generic "I want the SPECIFIC record, not the list" signal — see the
# LIST -> DETAIL sibling preference in _resolve_conversation_reference.
# Deliberately just this one word; it is never used to invent an action,
# only to prefer a same-category sibling that ALSO already matches every
# other selection signal.
_DETAIL_INTENT_RE = re.compile(r"รายละเอียด", re.IGNORECASE)


def _resolve_conversation_reference(registry, message: str, customer_context: Dict) -> Optional[Dict]:
    """Last-resort resolution for a follow-up message that carries NO
    Business-Action-selecting signal of its own (no keyword/pattern
    match — this is only ever called from decide() after a normal fresh
    search already came up empty) but the conversation clearly isn't
    over — the profile remembers which Business Action the customer was
    last using (profiles/manager.py's own Identifier Memory persistence,
    extended to also remember `last_business_action`), and EITHER:

    (a) the message names a specific FIELD that action's own response
    mapping is configured to return (Requested Field Filtering's own
    field_metadata.keywords vocabulary, reused here as evidence of
    "still talking about the same record" — e.g. "มีคูปองไหม" /
    "เบอร์โทรอะไร" after GetDataCustomer. Fully config-driven: ANY field
    any action's response_mapping names works automatically, never a
    fixed sentence list), or

    (b) it matches a generic topic-free referring-expression marker (see
    _REFERENCE_MARKER_RE, e.g. "แล้วของถึงหรือยัง").

    LIST -> DETAIL sibling preference (Issue 3): if the remembered action
    is a LIST-shaped action (its own response_mapping returns a raw
    list/array field) and a same-category sibling action exists whose
    required parameters are a strict SUPERSET of the list action's own
    (i.e. "the same thing, plus one more specific identifier") AND that
    extra identifier is now available via Identifier Memory (Issue 2's
    response-derived capture, or a customer-supplied value) AND the
    message signals wanting the specific record (_DETAIL_INTENT_RE) —
    the more specific sibling is preferred. Never invents the extra
    identifier; if it isn't actually available yet, the list action (or
    whatever WAS matched) is still returned, and normal collection asks
    for it like any other missing parameter.

    Never invents any identifier — this only decides WHICH action to try;
    Identifier Memory fill (and, if still incomplete, a genuine follow-up
    question) happens exactly like any other selection, via
    _handle_dynamic_collection."""
    if not customer_context:
        return None
    last_action_key = customer_context.get("last_business_action")
    if not last_action_key:
        return None
    try:
        action = registry.get_by_key(last_action_key)
    except Exception:
        return None
    if not action or not action.get("enabled") or action.get("action_type") not in ("API", "WEBHOOK"):
        return None
    full = registry.get_full(action["id"], mask_secrets=False)

    message_l = (message or "").lower()
    field_match = False
    for row in (full.get("response_mapping") or []):
        keywords = (row.get("field_metadata") or {}).get("keywords") or [row.get("mapped_label")]
        if any(kw and str(kw).lower() in message_l for kw in keywords):
            field_match = True
            break
    marker_match = bool(_REFERENCE_MARKER_RE.search(message or ""))
    # Pure Identifier Guard extended to continuation: a message that IS,
    # in its entirety, a bare identifier-shaped token ("FT3182") carries
    # no field/marker signal of its own, but it must still fill the
    # ACTIVE conversation's pending slot rather than fall through to a
    # fresh, identifier-memory-boosted search where it could just as
    # easily pattern-match a widely-shared parameter (e.g. CustCode) on
    # some unrelated same-category action. Never invents which parameter
    # it binds to — _handle_dynamic_collection's own extraction decides
    # that against the REMEMBERED action's actual configured parameters.
    bare_identifier_match = bool((message or "").strip()) and _validate_generic_identifier((message or "").strip())
    if not (field_match or marker_match or bare_identifier_match):
        return None

    if _DETAIL_INTENT_RE.search(message or ""):
        detail_sibling = _find_detail_sibling(registry, full, customer_context)
        if detail_sibling:
            return detail_sibling
    return full


def _find_detail_sibling(registry, list_action: Dict, customer_context: Dict) -> Optional[Dict]:
    """See _resolve_conversation_reference's own docstring (LIST -> DETAIL
    sibling preference). Only ever returns a sibling that is ALREADY
    fully satisfiable from Identifier Memory alone — never a guess.

    A same-category action can have more than one identifier-requiring
    sibling (e.g. a shipment list's category also contains a Tracking
    lookup, which incidentally also only needs one extra remembered
    value) — among every candidate that qualifies, the one whose extra
    required parameter matches the LIST action's own PRIMARY identity
    concept (Issue 2's identity_concept tagging: the record's own code,
    e.g. ShipmentCode for a shipment list) is preferred, so "the detail
    of THIS record type" is chosen over an unrelated same-category
    action that merely happens to also be satisfiable right now."""
    category = list_action.get("category")
    if not category:
        return None
    list_required = {p["name"] for p in (list_action.get("parameters") or []) if p.get("required")}
    primary_concept = next(
        (row.get("field_metadata", {}).get("identity_concept")
         for row in (list_action.get("response_mapping") or [])
         if row.get("field_metadata", {}).get("identity_concept")), None)
    try:
        siblings = [a for a in registry.enabled_actions()
                    if a.get("category") == category and a["id"] != list_action["id"]
                    and a.get("action_type") in ("API", "WEBHOOK")]
    except Exception:
        return None
    remembered = {n: customer_context[pf] for pf, n in IDENTIFIER_MEMORY_FIELDS if customer_context.get(pf)}
    qualifying = []
    for sibling in siblings:
        try:
            sibling_params = registry.get_parameters(sibling["id"])
        except Exception:
            continue
        sibling_required = {p["name"] for p in sibling_params if p.get("required")}
        extra = sibling_required - list_required
        if not (list_required < sibling_required and extra):
            continue
        askable = {p["name"]: p for p in sibling_params
                   if p.get("input_source", "customer_message") not in _NON_ASKABLE_INPUT_SOURCES}
        if all(name in askable and name in remembered for name in extra):
            qualifying.append((sibling, extra))
    if not qualifying:
        return None
    if primary_concept:
        preferred = [s for s, extra in qualifying if primary_concept in extra]
        if preferred:
            return registry.get_full(preferred[0]["id"], mask_secrets=False)
    return registry.get_full(qualifying[0][0]["id"], mask_secrets=False)


def _capture_response_derived_identifiers(response_mapping: Optional[List[Dict]], mapped_fields: Dict) -> Dict[str, str]:
    """Issue 2 — a successful list/search execution often names the
    record's OWN identifier in its response. A response_mapping row
    tagged `field_metadata.identity_concept` (config-driven — see
    migrations/tools that set it; never a hardcoded field name here)
    identifies which mapped value represents which IDENTIFIER_MEMORY_
    FIELDS concept (CustCode/OrderCode/ShipmentCode/Tracking)."""
    captured: Dict[str, str] = {}
    if not response_mapping or not mapped_fields:
        return captured
    concept_names = {n for _, n in IDENTIFIER_MEMORY_FIELDS}
    for row in response_mapping:
        concept = (row.get("field_metadata") or {}).get("identity_concept")
        if concept not in concept_names or concept in captured:
            continue
        value = mapped_fields.get(row.get("mapped_label"))
        if value:
            captured[concept] = value
    return captured


def _opportunistic_identifier_capture(registry, message: str) -> Dict[str, str]:
    """Final Conversational Correctness (2026-08-15) — a message that
    mentions a real identifier alongside other words ("ผม FT3182", not a
    Business-Action-selecting message on its own, so it falls through to
    RAG this turn) should still be REMEMBERED for the next turn, exactly
    like a value bound via a real ERP execution would be — the customer
    already told us their code; the fact THIS turn had nothing to do
    with it doesn't mean it should be forgotten. Never invents a value:
    only a STRUCTURAL candidate (never the whole-message free-text
    fallback) that fullmatches one of the platform's own configured
    identifier patterns (IDENTIFIER_MEMORY_FIELDS' concept names, read
    from whichever enabled action happens to configure them — never a
    hardcoded shape) counts."""
    captured: Dict[str, str] = {}
    structural = _extract_structural_candidates(message)
    if not structural:
        return captured
    try:
        candidates = registry.enabled_actions()
    except Exception:
        return captured
    concept_names = {n for _, n in IDENTIFIER_MEMORY_FIELDS}
    seen_patterns: set = set()
    for action in candidates:
        try:
            params = registry.get_parameters(action["id"])
        except Exception:
            continue
        for p in params:
            name = p.get("name")
            pattern = p.get("validation_pattern")
            if name not in concept_names or name in captured or not pattern or pattern in seen_patterns:
                continue
            seen_patterns.add(pattern)
            try:
                regex = re.compile(pattern)
            except re.error:
                continue
            match = next((c for c in structural if regex.fullmatch(c)), None)
            if match:
                captured[name] = match
    return captured


# ── Alert vocabulary — deliberately small, generic, deterministic (no
# LLM call, consistent with every other conversation-intelligence module
# in this codebase). Detects a SIGNAL worth flagging; it never decides
# what to DO about it beyond attaching structured metadata + trying a
# NOTIFICATION-type Business Action if one is configured.
_COMPLAINT_RE = re.compile(r"ร้องเรียน|แย่มาก|ไม่พอใจ|บริการแย่|เลวมาก|ผิดหวังมาก", re.IGNORECASE)
_LEGAL_THREAT_RE = re.compile(r"ทนายความ|ฟ้องร้อง|แจ้งความ|ดำเนินคดี|สคบ", re.IGNORECASE)

# "HYBRID" added 2026-08-02 (Production Integration Sprint, Phase 1 Step
# C) — a genuinely new routing outcome (ERP + RAG combined via services/
# hybrid_runtime_service.py::synthesize_hybrid_answer), never previously
# representable; additive only, every existing type is unchanged.
_ROUTING_TYPES = ("RAG", "GENERAL", "API", "TOOL", "WORKFLOW", "NOTIFICATION", "HUMAN_HANDOFF", "WEBHOOK",
                  "SAFE_FALLBACK", "HYBRID")

_URL_RE = re.compile(r"https?://\S+")


def _extract_system_values(message: str, history: Optional[List[Dict]] = None) -> Dict:
    """Generic, action-agnostic values derived from the raw message that
    any Business Action's parameters may read via input_source
    'system_generated' — currently just the first URL, if any. Never
    tied to a specific action (e.g. not "if url_converter").

    system_generated resolution (services/action_executor.py::
    _resolve_param_value) looks this dict up by the parameter's own exact
    `name` — which is the real API's wire field name, not something this
    generic extractor controls. Since a URL-consuming API's field is
    equally likely to be named "url", "URL", or "Url" depending on the
    third party, the value is exposed under all three common castings
    here rather than forcing every such Business Action to rename its
    real wire parameter to match one fixed casing.

    Cross-turn carry-forward (confirmed live defect): a system_generated
    parameter is deliberately never "askable" (see
    _NON_ASKABLE_INPUT_SOURCES) and never appears in `collected`, so a URL
    given on an EARLIER turn (e.g. the customer sends a product link, is
    asked for CustCode, then replies with just the code) would otherwise
    vanish by the time this runs again — THIS turn's own `message` alone
    has no URL. Only falls back to `history` when the current message
    carries no URL of its own, so a fresher URL on this turn always wins;
    scans the most recent user turn first, exactly like every other
    identifier-memory mechanism in this module prefers the latest value."""
    match = _URL_RE.search(message or "")
    if not match and history:
        for turn in reversed(history):
            if turn.get("role") == "user":
                match = _URL_RE.search(turn.get("content") or "")
                if match:
                    break
    if not match:
        return {}
    url = match.group(0)
    return {"url": url, "URL": url, "Url": url}


def _extract_reply_attachments(chunks: List[Dict]) -> "tuple[List[str], List[Dict]]":
    """Production Integration Sprint (2026-08-02) — the ONE place that
    classifies retrieved-chunk attachments into image URLs vs. other-file
    metadata, so every channel adapter (LINE, future channels) receives
    the SAME generic reply.images/reply.files lists instead of each
    re-deriving its own classification. Respects the admin-configured
    Attachment Rules (services/policy_studio_service.py) — a toggle
    disabled there means that attachment type is never even offered to a
    channel adapter, not just hidden by convention.

    Each chunk's own `attachments` field already comes from the frozen
    RAG pipeline (services/rag_service.py) — never re-derived here, only
    read and classified."""
    try:
        from services.policy_studio_service import get_default_policy_set
        att_rules = (get_default_policy_set().get("config") or {}).get("attachment_rules", {})
    except Exception:
        att_rules = {}
    send_images = att_rules.get("send_image_if_available", True)
    send_files = att_rules.get("send_file_link_if_available", True)

    images: List[str] = []
    files: List[Dict] = []
    seen_urls: set = set()
    for chunk in chunks or []:
        for att in (chunk.get("attachments") or []):
            pub_url = att.get("public_url")
            if not pub_url or pub_url in seen_urls:
                continue
            atype = (att.get("attachment_type") or "").lower()
            mime = att.get("mime_type") or ""
            is_image = atype == "image" or (
                not atype and (mime.startswith("image/") or
                               pub_url.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp"))))
            if is_image and send_images:
                seen_urls.add(pub_url)
                images.append(pub_url)
            elif not is_image and send_files:
                seen_urls.add(pub_url)
                files.append({
                    "filename": att.get("filename") or att.get("original_filename") or "file",
                    "url": pub_url, "attachment_type": atype or None,
                })
    return images, files


def _safe_fallback_response(reason: str) -> Dict:
    return {
        "text": "ขอโทษด้วยค่ะ ตอนนี้ยังไม่พบคำตอบที่ชัดเจนสำหรับคำถามนี้ รบกวนสอบถามเจ้าหน้าที่เพิ่มเติมนะคะ",
        "message_parts": None, "buttons": [], "quick_replies": [], "images": [], "files": [],
    }, reason


_UNSAFE_CUSTOMER_TEXT_RE = re.compile(
    r"\[\{|\{'|\{\"|\['|\[\""
    r"|(?<![ก-๙A-Za-z0-9_])None(?![ก-๙A-Za-z0-9_])|(?<![ก-๙A-Za-z0-9_])null(?![ก-๙A-Za-z0-9_])"
    r"|แหล่งที่มา\s*:|\bsource\s*:|\.xlsx\b|\bpage\s*:|\bsection\s*:"
    r"|\bERP:|\bRAG:|Knowledge Base:", re.IGNORECASE)

# Latest-N Aggregation (Customer-Reported ERP Conversation Defects,
# 2026-08-17) — a generic, language-level detector for "how many of my
# last N records" / "what's the total of my last N" phrasing. Never
# tied to one Business Action or field name: it only tells the caller
# WHAT the customer is asking for (a record limit, and/or a sum);
# _aggregate_list_reply (below) is the one that decides, from the
# action's OWN response_mapping, whether a real list + a real
# currency-shaped per-item field actually exist to answer it from.
_LATEST_N_RE = re.compile(r"(\d+)\s*(?:อัน|รายการ|ใบ|ครั้ง|บิล|order|orders)?\s*(?:ล่าสุด|แรก|ที่ผ่านมา)")
_SUM_INTENT_RE = re.compile(
    r"รวม.{0,10}(?:เท่าไหร่|เท่าไร|เท่าใด)|(?:เท่าไหร่|เท่าไร).{0,10}รวม"
    r"|ยอดรวม|รวมทั้งหมด|รวมกัน(?:แล้ว)?เท่าไหร่"
    r"|ยอด.{0,15}(?:เท่าไหร่|เท่าไร|เท่าใด)|\bsum\b|\btotal\b", re.IGNORECASE)
# Count/Sum/Outstanding Filter Aggregation (Shipment Filter/Count/Sum
# fix, 2026-08-24) — two more generic, language-level signals sitting
# alongside _LATEST_N_RE/_SUM_INTENT_RE above, never tied to shipments
# or any one Business Action.
_COUNT_INTENT_RE = re.compile(
    r"กี่(?:บิล|รายการ|ใบ|ครั้ง|order|orders)|มีทั้งหมดกี่|จำนวน.{0,10}เท่าไหร่|จำนวน.{0,10}เท่าไร"
    r"|\bhow many\b|\bcount\b", re.IGNORECASE)
_OUTSTANDING_INTENT_RE = re.compile(
    r"ค้างจ่าย|ค้างชำระ|คงค้าง|ยังไม่จ่าย|ยังไม่ชำระ|\bunpaid\b|\boutstanding\b", re.IGNORECASE)
# A "which record did this number come from" question (e.g. "ยอดรวม
# บิลขนส่งล่าสุด 112.46 บาท เอามาจากบิลไหน") is a single-record
# TRACEABILITY question, never a request to compute a NEW aggregate —
# confirmed live: without this guard, "ยอดรวม" alone made the composer
# re-sum every record the customer ever had and mislabel the result as
# "112 รายการล่าสุด" (112 being the record COUNT, not part of the
# 112.46 THB figure the customer was asking about), because "no limit"
# was rendered with the same "ล่าสุด" wording as an actual N-latest
# request. Disqualifying it here lets the existing, already-correct
# single-latest-record composer (_compose_natural_reply) answer it.
_SOURCE_TRACE_RE = re.compile(
    r"(?:เอา)?มาจาก(?:บิล|ออเดอร์|รายการ)?ไหน|which\s+(?:bill|order|shipment)", re.IGNORECASE)


def _detect_aggregation_request(message: str) -> Optional[Dict]:
    message = message or ""
    if _SOURCE_TRACE_RE.search(message):
        return None
    n_match = _LATEST_N_RE.search(message)
    limit = int(n_match.group(1)) if n_match else None
    wants_sum = bool(_SUM_INTENT_RE.search(message))
    wants_count = bool(_COUNT_INTENT_RE.search(message))
    wants_outstanding = bool(_OUTSTANDING_INTENT_RE.search(message))
    if limit is None and not (wants_sum or wants_count or wants_outstanding):
        return None
    return {"limit": limit, "wants_sum": wants_sum, "wants_count": wants_count,
            "wants_outstanding": wants_outstanding}


def _sanitize_customer_text(text: Optional[str]) -> Optional[str]:
    """Customer Response Sanitizer (2026-08-16) — a final defensive net,
    NOT the primary fix. The composer/response architecture (natural
    ERP composition, Requested-Field Filtering, Hybrid's label-free
    merged_answer) is what actually keeps raw serialization and source
    metadata out of customer replies; this only catches whatever slips
    past every one of those upstream, generically, without knowing which
    business action or field produced it.

    On a hit, the ENTIRE reply is replaced with an honest, generic
    apology — never a regex-edit of the offending text in place, which
    would just leave a garbled fragment instead of a clean sentence.
    The patterns are all raw-serialization-shaped ("[{", "{'", '{"',
    "['", '["', bare None/null) or developer-metadata-shaped (citation/
    page/section markers, .xlsx, bare "ERP:"/"RAG:"/"Knowledge Base:"
    labels) — none of them collide with ordinary Thai or English
    customer-facing prose, so this never fires in normal operation.

    Customer-Reported ERP Conversation Defects (2026-08-17), Issue 4 —
    hardened to a genuinely UNCONDITIONAL final boundary: if `text`
    itself is not a plain string (a dict/list ever reached this call by
    mistake, upstream of every composer that's supposed to have already
    turned it into prose), it is NEVER stringified/leaked — the same
    honest apology is returned instead."""
    if text is not None and not isinstance(text, str):
        return "ขอโทษด้วยค่ะ ระบบพบปัญหาในการแสดงผลข้อมูล รบกวนสอบถามอีกครั้งหรือระบุคำถามให้ชัดเจนขึ้นนะคะ"
    if text and _UNSAFE_CUSTOMER_TEXT_RE.search(text):
        return "ขอโทษด้วยค่ะ ระบบพบปัญหาในการแสดงผลข้อมูล รบกวนสอบถามอีกครั้งหรือระบุคำถามให้ชัดเจนขึ้นนะคะ"
    return text


def _build_response(*, text: str, message_parts=None, buttons=None, quick_replies=None,
                     images=None, files=None) -> Dict:
    return {
        "text": _sanitize_customer_text(text), "message_parts": message_parts, "buttons": buttons or [],
        "quick_replies": quick_replies or [], "images": images or [], "files": files or [],
    }


def _detect_alert(message: str, context: Dict) -> Optional[Dict]:
    """Deterministic alert signal detection — never notifies anyone
    (Notification Adapter is explicitly future work); only attaches
    structured metadata a future adapter or human reviewer can act on."""
    customer_context = context.get("customer_context") or {}
    if _LEGAL_THREAT_RE.search(message or ""):
        return {"alert_type": "legal_threat", "priority": "high"}
    if _COMPLAINT_RE.search(message or ""):
        return {"alert_type": "complaint", "priority": "high"}
    if customer_context.get("is_vip"):
        return {"alert_type": "vip_customer", "priority": "high"}
    return None


def _semantic_score(action: Dict, message: str) -> float:
    """Interface only — future semantic routing (embedding similarity
    between the message and the action's AI Description) is not
    implemented in this phase. Always returns 0.0 today; a future phase
    can replace this function body without touching any call site."""
    return 0.0


def _embedding_score(action: Dict, message: str) -> float:
    """Interface only — future Action Embedding similarity score.
    business_action_embeddings.embedding_source_text is prepared
    (services/business_action_registry.py::prepare_embedding_source) but
    no vector is computed or compared yet. Always returns 0.0 today."""
    return 0.0


def _parameter_availability_score(params: List[Dict], collected_slots: Dict, *,
                                   param_action_counts: Optional[Dict[str, int]] = None) -> float:
    """Rewards an action whose configured parameter names are already
    (partly) satisfiable from what's been collected this turn — makes
    e.g. a customer-lookup action outrank an unrelated action once the
    caller already has CustCode/CustEmail/etc. in hand. `params` is the
    action's own real parameter rows (registry.get_parameters()) —
    passed in explicitly by the caller, which already has them on hand
    from its own pre-pass, rather than expecting them pre-attached to
    `action` under a private key (a bare `enabled_actions()` row never
    carries its parameters, so that used to always score 0).

    2026-08-15 (Final Conversational Correctness) — `collected_slots`
    now includes Identifier Memory values (see IDENTIFIER_MEMORY_FIELDS)
    alongside this turn's own bound slots, so a remembered CustCode/
    OrderCode/etc. from earlier in the conversation also counts here.
    `param_action_counts` (built once per search_candidate_actions()
    call, mirroring _identifier_pattern_score's own sharer-weighting)
    divides each matched name's contribution by how many of THIS turn's
    candidates configure that same parameter name — a name unique to one
    action (OrderCode) stays fully decisive; a name nearly every ERP
    action shares (CustCode) can no longer single-handedly out-score
    every other candidate just because Identifier Memory happens to
    remember it, which would otherwise let raw priority silently pick
    the "winner" again exactly like the bug _identifier_pattern_score's
    own weighting already fixed for structural pattern matches."""
    if not collected_slots:
        return 0.0
    names = {p.get("name") for p in params or []}
    matched = names & set(collected_slots.keys())
    if not matched:
        return 0.0
    if param_action_counts:
        return sum(1.0 / max(param_action_counts.get(n, 1), 1) for n in matched)
    return float(len(matched))


# A whole message token (never a substring match, to avoid a short pattern
# accidentally matching part of a longer, unrelated word) matching one of
# this action's OWN configured parameter validation_pattern regexes is
# strong, generic evidence the customer supplied that EXACT identifier —
# e.g. a message naming a specific, well-formed OrderCode should strongly
# favor the single-record lookup action that actually accepts that shape
# over a same-category list/summary action that doesn't. Weighted higher
# than a single keyword hit (which any nearby generic action can also
# collect) so a genuine identifier match reliably decides a close
# category-mate tie, without dominating category/keyword signals entirely.
# Entirely driven by each parameter's own admin-configured
# validation_pattern (services/business_action_registry.py) — no
# hardcoded field name, action key, or sample value anywhere here.
_IDENTIFIER_PATTERN_WEIGHT = 3.0
_TOKEN_SPLIT_RE = re.compile(r"[\s,;]+")


def _identifier_pattern_score(registry, action: Dict, message: str, *,
                               param_action_counts: Optional[Dict[str, int]] = None) -> float:
    """`param_action_counts` (built once per search_candidate_actions()
    call, see there — the SAME dict `_parameter_availability_score`
    already uses) maps each PARAMETER NAME to how many of THIS TURN'S
    candidate actions configure a parameter with that name. A name
    genuinely unique to one action (e.g. "OrderCode") is strong,
    discriminating evidence and keeps the full weight. A name shared by
    many candidates (e.g. "CustCode", configured on nearly every ERP
    action) matches ALL of them equally, so it can never actually
    discriminate between them — full weight there would let a bare
    customer code alone tip the choice via nothing more meaningful than
    each action's own priority tiebreaker (confirmed live, 2026-08-15 AI
    Playground Real User Journey UAT: a bare CustCode-shaped message with
    no other context was winning on SearchDataTracking purely because it
    happened to have priority=1 while five other equally-matching actions
    sat at priority=0 — not because tracking was ever actually implied).
    Dividing by the sharer count makes a fully-shared name contribute a
    small, non-decisive amount instead — the same "shared evidence is
    weak evidence for any ONE candidate" principle
    services/hybrid_question_classifier.py's own ambiguity-ratio check
    already applies to keyword scoring.

    Generic Business Action Routing Score Imbalance fix (2026-08-24) —
    this used to dilute by the literal validation_pattern STRING instead
    of the parameter's NAME, so two actions whose "CustCode" parameter
    happened to be spelled with a slightly different regex (one admin
    wrote `^[A-Za-z]{2}\\d{4,6}$`, another `^[A-Za-z]{2}\\d+$`) were
    treated as having completely unrelated, mutually-exclusive
    identifiers — the narrower-pattern action's match then looked
    artificially "unique" (undiluted) and could outscore every
    keyword/example-backed candidate on nothing but a bare customer code,
    for ANY action, not just one (confirmed live: "SP1008 ข้อมูลลูกค้า"
    — literally getdatacustomer's own purpose — still lost to an
    unrelated address-change action on identifier score alone). Grouping
    by name instead — reusing the exact dict `_parameter_availability_score`
    already builds and trusts for the identical purpose — means the SAME
    semantic identifier concept dilutes consistently no matter how many
    slightly-different regex spellings admins gave it across actions,
    without weakening a genuinely unique identifier's own discriminating
    power (a name only one action configures, e.g. "OrderCode", is
    unaffected)."""
    try:
        params = registry.get_parameters(action["id"])
    except Exception:
        return 0.0
    tokens = [t for t in _TOKEN_SPLIT_RE.split(message or "") if t]
    score = 0.0
    for p in params:
        pattern = p.get("validation_pattern")
        if not pattern:
            continue
        try:
            regex = re.compile(pattern)
        except re.error:
            continue
        if any(regex.match(tok) for tok in tokens):
            sharers = (param_action_counts or {}).get(p.get("name"), 1)
            score += _IDENTIFIER_PATTERN_WEIGHT / max(sharers, 1)
    return score


# ── Shared Turn-Intent Primitive (Root Change 1, Final Systemic Routing
# Fix, 2026-08-28) — the forensic routing audit's Root Cause #1: the
# "informational vs. private/action" distinction used to be decided
# AFTER Business Action scoring/selection already ran, as a bolt-on
# veto (the old "General Informational Question Guard") applied only
# to whichever candidate happened to score highest — so an obviously
# informational message (e.g. "เติม Wallet ยังไง") was routinely SCORED
# as a private ERP action first, then (hopefully) caught afterward.
# This function makes the identical decision — reusing every regex it
# already relied on, nothing new, nothing widened, precedence
# unchanged — BEFORE search_candidate_actions() ever runs, so an
# informational message can no longer be offered an identity-gated
# Business Action as a candidate in the first place. Continuation
# (already resolved earlier in decide()) and RAG-vs-General-Chat
# (decided separately inside run_playground_turn — see Root Change 2)
# are each already owned by their own existing mechanism; this
# function's only job is the private-action/informational axis for a
# FRESH, non-continuation, single-clause message.
def classify_turn_intent(message: str) -> str:
    """Returns "PRIVATE_ACTION" when the message carries real self-
    referencing/current-state/action-request evidence (must remain
    eligible to select an identity-gated Business Action),
    "SHIPIFY_INFORMATION" when it is a genuine company-subject or
    how-to question with NONE of that evidence (must never be offered
    an identity-gated Business Action as a candidate), or "AMBIGUOUS"
    when neither is decisively true (existing scoring/selection
    decides, exactly as before this fix). Message-only — never
    depends on which Business Action would otherwise have won — so it
    can run before candidate search instead of after selection."""
    text = message or ""
    has_question_marker = bool(_QUESTION_MARKER_RE.search(text))
    has_declarative_intent = bool(_DECLARATIVE_INTENT_MARKER_RE.search(text))

    # Contact-channel request (Customer-Data Relevance fix, 2026-09-01) —
    # "ขอเบอร์ติดต่อ" / "ขอเบอร์โทร" ask for the COMPANY's published contact
    # channel (a curated FAQ row exists for exactly this), NOT the
    # customer's own registered phone. These are usually phrased as a bare
    # request with no question/declarative marker, so without this they
    # hit the AMBIGUOUS early-return below, identity-gated Business Actions
    # stay eligible, and getdatacustomer wins by binding the whole message
    # as a free-text CustName — returning the user's masked number. A
    # genuine "my registered number" request always self-references
    # (ผม/ฉัน/บัญชี/ลงทะเบียน/ที่ผูก/ในระบบ) or carries a reference marker
    # (both excluded here); an identifier-bearing message still returns
    # PRIVATE_ACTION via the higher-precedence check just below.
    _contact_request = bool(re.search(
        r"เบอร์|ช่องทางติดต่อ|contact|call\s*center|ติดต่อ.{0,12}(shipify|fasttrade|เจ้าหน้าที่|แอดมิน|บริษัท)",
        text, re.IGNORECASE))
    _self_registered = bool(re.search(r"ผม|ฉัน|ดิฉัน|ลงทะเบียน|ที่ผูก|บัญชีของ|โปรไฟล์|ในระบบ", text)) \
        or bool(_REFERENCE_MARKER_RE.search(text))

    if not (has_question_marker or has_declarative_intent or _contact_request):
        return "AMBIGUOUS"

    # Identifier evidence has the HIGHEST precedence — exactly mirroring
    # the original guard's own outer gate (`not any("parameter identifier
    # pattern" in r for r in selected.get("_reasons"))`, which exempted
    # the ENTIRE veto, including the company-subject check below, once a
    # real identifier was present): a message that supplies a real
    # account/order/shipment identifier is unconditional evidence of an
    # actual account-specific request, overriding even "บริษัท" wording
    # (e.g. "บริษัทเช็คออเดอร์ SP1008 ให้หน่อยได้ไหมครับ" must still select
    # the order-lookup action).
    identifier_evidence = any(
        _validate_generic_identifier(tok) and not tok.isdigit()
        for tok in _TOKEN_SPLIT_RE.split(text) if tok)
    if identifier_evidence:
        return "PRIVATE_ACTION"

    # Company-as-subject override — unconditional (once identifier
    # evidence is ruled out above), exactly mirroring the original
    # guard's own precedence: a message naming "บริษัท" as its subject,
    # phrased as a genuine question, is never a private/self-referencing
    # request (a real private request always names ITSELF — "เช็กบิลของ
    # ผม" — never "the company"), regardless of any other signal also
    # present.
    if "บริษัท" in text and has_question_marker:
        return "SHIPIFY_INFORMATION"

    # Contact-channel request override — see the _contact_request comment
    # near the top of this function. Reached only after identifier
    # evidence (PRIVATE_ACTION, higher precedence) is ruled out.
    if _contact_request and not _self_registered:
        return "SHIPIFY_INFORMATION"

    bare_self_reference = bool(re.search(r"ผม|ฉัน|ดิฉัน", text)) and not _SUBJECT_INTENT_RE.search(text)
    declarative_private_action = not has_question_marker and bool(_PRIVATE_ACTION_VERB_RE.search(text))
    private_action_evidence = (
        bool(_REQUEST_MARKER_RE.search(text))
        or bool(_REFERENCE_MARKER_RE.search(text))
        or bare_self_reference
        or declarative_private_action
        or bool(_PRIVATE_STATE_QUERY_RE.search(text))
        or len(_split_clauses(text)) > 1
    )
    return "PRIVATE_ACTION" if private_action_evidence else "SHIPIFY_INFORMATION"


# Identity-gated action types — the SAME tuple the (now-removed)
# post-selection veto used to gate on (`selected.get("action_type") in
# ("API", "WEBHOOK")`); reused here to EXCLUDE these types from
# candidate search up front for a SHIPIFY_INFORMATION-classified
# message, instead of un-selecting one after the fact.
_IDENTITY_GATED_ACTION_TYPES = ("API", "WEBHOOK")


def search_candidate_actions(registry, *, workflow: Optional[str], message: str,
                              collected_slots: Optional[Dict] = None,
                              action_types: Optional[List[str]] = None,
                              exclude_action_types: Optional[List[str]] = None) -> List[Dict]:
    """Business Action Search — returns enabled candidates the Decision
    Engine may choose from, each annotated with a `_score`/`_reasons`.
    Considers: category (workflow match), keyword/example/ai_description
    overlap, parameter identifier-pattern evidence, parameter availability,
    priority, and (currently inert) future semantic/embedding scores.
    Never filters by a hardcoded action_key — every action, current or
    future, competes on the same generic signals. `exclude_action_types`
    (Root Change 1) is the inverse of `action_types`: used to keep
    identity-gated actions out of the candidate pool entirely for a
    message already classified SHIPIFY_INFORMATION, never to hide a
    hardcoded action."""
    try:
        candidates = registry.enabled_actions()
    except Exception:
        return []

    if action_types:
        candidates = [a for a in candidates if a.get("action_type") in action_types]
    if exclude_action_types:
        candidates = [a for a in candidates if a.get("action_type") not in exclude_action_types]

    # Pre-pass for _identifier_pattern_score's AND _parameter_availability_
    # score's shared discriminating-power weighting (see either function's
    # own docstring) — how many of THIS turn's candidates configure a
    # parameter with each NAME (never the literal validation_pattern
    # string — two actions' "CustCode" params can be spelled with
    # slightly different regexes and still mean the same identifier
    # concept; grouping by name instead of pattern text is what the
    # Generic Business Action Routing Score Imbalance fix, 2026-08-24,
    # relies on). Costs one extra get_parameters() call per candidate,
    # same "small action count" tradeoff already accepted for the
    # per-candidate calls below.
    param_action_counts: Dict[str, int] = {}
    params_by_action_id: Dict[str, List[Dict]] = {}
    for action in candidates:
        try:
            action_params = registry.get_parameters(action["id"])
        except Exception:
            action_params = []
        params_by_action_id[action["id"]] = action_params
        for name in {p.get("name") for p in action_params if p.get("name")}:
            param_action_counts[name] = param_action_counts.get(name, 0) + 1

    scored = []
    for action in candidates:
        reasons = []
        score = 0.0
        if workflow and action.get("category") == workflow:
            score += 3.0
            reasons.append(f"category matches workflow '{workflow}'")
        kw_score = _keyword_score(action, message)
        if kw_score:
            score += kw_score
            reasons.append(f"keyword/example/AI-description overlap ({kw_score})")
        # Checked for every candidate, independent of keyword score — the
        # entire point is to decide cases where the specific-record action
        # has LITTLE OR NO keyword overlap of its own (a customer naming a
        # bare identifier rarely repeats the action's own vocabulary) while
        # a same-category list/summary action's generic keyword happens to
        # match instead. The registry call this costs is cheap relative to
        # the rest of this loop and the action count here is small.
        id_score = _identifier_pattern_score(registry, action, message, param_action_counts=param_action_counts)
        if id_score:
            score += id_score
            reasons.append(f"message contains a value matching this action's own "
                            f"parameter identifier pattern ({id_score})")
        param_score = _parameter_availability_score(params_by_action_id.get(action["id"], []), collected_slots or {},
                                                      param_action_counts=param_action_counts)
        # Stale-Memory Hijack Guard (Continuation Precedence fix,
        # 2026-08-25) — parameter availability alone (i.e. this action's
        # required params happen to be satisfiable purely from
        # Identifier Memory carried over from an EARLIER turn) is never,
        # by itself, sufficient evidence that THIS message is about this
        # action. Only counted as a corroborating booster once the
        # candidate already has SOME independent evidence of its own
        # (category match, a keyword/example hit, or an identifier
        # PATTERN actually present in this turn's own message) — exactly
        # mirroring why _identifier_pattern_score's own docstring already
        # treats a widely-shared bare identifier as weak evidence on its
        # own. Confirmed live: after "SP1008 order ล่าสุด" remembers
        # CustCode+OrderCode, a completely unrelated fresh question with
        # zero topical connection to orders ("คูปองใช้ยังไง", a RAG-only
        # policy question) was still selecting searchdataorder purely
        # because OrderCode happened to still be satisfiable from memory
        # — the message itself contributed no keyword, no identifier
        # pattern, no category match at all. Genuine continuation
        # ("แล้วสถานะตอนนี้ล่ะ", "ขอรายละเอียดอันล่าสุด") is unaffected —
        # that is resolved earlier and separately by _resolve_
        # conversation_reference's own marker/field matching, never by
        # this fresh-search scoring path at all.
        if param_score and score:
            score += param_score
            reasons.append(f"{int(param_score)} collected slot(s) match this action's parameters")
        sem = _semantic_score(action, message)
        emb = _embedding_score(action, message)
        score += sem + emb
        score += (action.get("priority") or 0) * 0.01  # tiebreaker only, never dominates
        scored.append({**action, "_score": round(score, 3), "_reasons": reasons,
                        "_semantic_score": sem, "_embedding_score": emb})

    scored.sort(key=lambda a: a["_score"], reverse=True)
    return scored


def select_best_action(candidates: List[Dict], *, minimum_score: float = 0.5) -> Optional[Dict]:
    if not candidates:
        return None
    best = candidates[0]
    if best["_score"] < minimum_score:
        return None
    return best


class DecisionEngine:
    """The central orchestrator. `decide(message, history, context)` is
    the one entry point a Channel Adapter (LINE, Playground, future
    channels) calls — everything downstream (Information Collection,
    Business Action selection, execution) happens here, reading only
    from existing, frozen components."""

    def __init__(self, sb=None):
        self._sb = sb
        self.registry = get_registry(sb) if sb is not None else get_registry()
        self.executor: ActionExecutor = get_action_executor(sb)

    def decide(self, message: str, history: Optional[List[Dict]] = None, context: Optional[Dict] = None) -> Dict:
        history = history or []
        context = dict(context or {})
        customer_context = context.get("customer_context") or {}
        start = time.time()
        developer_trace: Dict = {}

        try:
            # 1-2. read message + history are inputs; 3. conversation state
            # is recomputed from `history` (no separate persistence layer
            # in this codebase — same convention every other
            # conversation-intelligence module here already follows).

            # 4. Intent resolution — determines CUSTOMER INTENT only. It
            # narrows candidate Business Actions (via `workflow` below,
            # still used as a category-matching score signal); it no
            # longer defines required slots/parameters for anything that
            # has a configured Business Action — see the Single Source of
            # Truth refactor note at the top of this module.
            actionable_intent = classify_actionable_intent(message)
            workflow_hint = resolve_active_erp_intent(history, message)
            developer_trace["intent"] = actionable_intent
            developer_trace["workflow"] = workflow_hint

            # Confirmed Pending Action — Direct Execution (Confirmation
            # Continuation Correctness fix, 2026-08-23). A caller
            # (line_bot/webhook.py, admin/routes.py Auto mode) that has
            # ALREADY resolved which pending confirmation this reply
            # answers (via services/pending_confirmation_service.py,
            # generic and channel-agnostic) passes the pending action's
            # id and its already-collected, already-validated parameter
            # dict directly here — never re-derived from a synthetic
            # history replay. Root cause this fixes: _resolve_
            # continuation_action/_replay_business_action_collection
            # reconstruct "collected" purely by replaying whatever
            # `history` this call received; a caller that (correctly)
            # replays only the pending row's own 2-turn snapshot loses
            # any parameter the customer gave MORE than one turn before
            # the confirmation question (e.g. an identifier given several
            # turns earlier) — confirmed live: "SP1008" (turn 1) then a
            # separate address message (turn 3) reaching confirmation,
            # then "ยืนยัน" fell through to an unrelated fresh-message
            # classification because CustCode could never be recovered
            # from replaying just the address turn alone. This bypasses
            # Selection/Continuation-matching ENTIRELY for this one turn
            # — every other turn (fresh messages, parameter follow-ups,
            # cancellations, an action that no longer exists/is disabled)
            # is completely unaffected, and _resolve_continuation_action
            # itself is untouched, still used exactly as before by every
            # other caller/scenario.
            confirmed_action_id = context.get("confirmed_action_id")
            if context.get("confirmed") and confirmed_action_id:
                full_action = self.registry.get_full(confirmed_action_id, mask_secrets=False)
                if full_action and full_action.get("enabled"):
                    return self._execute_selected_action(
                        full_action, [full_action], message, history, context, developer_trace, start,
                        workflow=workflow_hint, intent=(actionable_intent or {}).get("actionable_intent"),
                        collected_slots=dict(context.get("confirmed_parameters") or {}))
                # The pending action vanished/was disabled since the
                # confirmation was issued — fall through to normal
                # routing rather than silently no-op.

            if _HUMAN_REQUEST_RE.search(message or ""):
                return self._route_human_handoff(
                    message, history, context, developer_trace, start,
                    reason="user_requested_human", workflow=workflow_hint)

            # Active Handoff Follow-up (Golden Application Defect Fixes,
            # 2026-08-16 -- root cause of the ORIGINAL GOLDEN-038's
            # failure) -- a message referring to an already-outstanding
            # human-contact request ("ยังไม่มีเจ้าหน้าที่ติดต่อมาเลย") never
            # matches _HUMAN_REQUEST_RE above (it isn't a fresh request)
            # and previously fell straight through to ordinary routing
            # (usually RAG, since it carries no ERP Business Action
            # keyword), silently abandoning an active handoff instead of
            # staying inside it. Requires BOTH signals, deliberately: the
            # conversation's OWN persisted state (context["handoff_status"]
            # -- set by whichever channel adapter called decide(); "NONE"
            # when absent, e.g. a caller that never wired it in, so this
            # never fires on an ordinary complaint with no real handoff
            # behind it) AND the message's own follow-up phrasing
            # (_HANDOFF_FOLLOWUP_RE). Neither alone is sufficient -- see
            # that regex's own docstring. Duplicate-notification
            # protection itself is NOT reimplemented here: reusing
            # _route_human_handoff means the SAME PENDING/NOTIFIED dedup
            # state machine every other HUMAN_HANDOFF path already goes
            # through downstream (services/session_service.py, verified
            # working by GOLDEN-038B) suppresses the actual duplicate
            # send -- this trigger only fixes which ROUTE the turn takes.
            if context.get("handoff_status") in ("PENDING", "NOTIFIED") \
                    and _HANDOFF_FOLLOWUP_RE.search(message or ""):
                return self._route_human_handoff(
                    message, history, context, developer_trace, start,
                    reason="handoff_follow_up", workflow=workflow_hint,
                    message_override="รับทราบค่ะ ตอนนี้มีคำขอติดต่อเจ้าหน้าที่อยู่แล้ว "
                                      "เจ้าหน้าที่จะติดต่อกลับโดยเร็วที่สุดค่ะ จึงจะไม่ส่งคำขอซ้ำนะคะ")

            # Customer Intelligence Handoff Recommendation (Human Handoff
            # V1, 2026-08-15) — Trigger C. A deterministic, per-message
            # signal (services/customer_tier_service.py, the SAME function
            # Customer Intelligence V1 already uses to persist
            # handoff_recommended onto the profile) computed HERE, on this
            # turn's own message, so a genuinely decisive signal (e.g. "ขอ
            # ให้เซลส์ติดต่อกลับ") can escalate THIS turn — not just get
            # recorded for next time.
            #
            # Deliberately gated to stage=="hot" ONLY, even though
            # compute_handoff_recommendation() also returns True for a
            # NEGATIVE + complaint message with no explicit human-request
            # phrase. That negative case must NOT short-circuit here: it
            # needs to reach the RAG pipeline first so AI Policies' own
            # escalation verdict (Trigger B, below) gets a real chance to
            # answer or produce its own, more specific escalation message
            # — short-circuiting on a bare complaint keyword would deny
            # every complaint-shaped RAG question an actual answer. A
            # negative message WITH an explicit human-request phrase is
            # already caught by the _HUMAN_REQUEST_RE check just above
            # (Trigger A), so nothing is lost. The hot+callback-request
            # case has no such pipeline to defer to (there is no RAG
            # answer for "please have sales call me"), so it escalates
            # immediately, same as an explicit request would.
            from services.customer_tier_service import classify_message_stage, compute_handoff_recommendation
            ci_stage = classify_message_stage(message)
            if ci_stage["stage"] == "hot":
                ci_handoff = compute_handoff_recommendation(ci_stage["stage"], message)
                if ci_handoff["recommended"]:
                    return self._route_human_handoff(
                        message, history, context, developer_trace, start,
                        reason="customer_intelligence_recommended", workflow=workflow_hint)

            # New execution order: Search Candidate Business Actions ->
            # Select Best Business Action -> Read Business Action
            # Parameters -> Information Collection -> Execute. Selection
            # happens BEFORE information collection, so the Registry's
            # own parameter definitions — not a separate intent-keyed
            # schema — drive what gets asked.
            continuation_action = _resolve_continuation_action(
                self.registry, history, workflow_hint, context.get("customer_context"))

            # Reliable Pending-Confirmation Continuation (Address Change
            # Full UAT fix, 2026-08-24) — _resolve_continuation_action
            # only recognizes continuation by re-generating the exact
            # question/confirmation text from a PURE history-replay and
            # string-comparing it to the last assistant turn; that replay
            # can never recover a parameter that was originally filled
            # from Identifier Memory / customer_context rather than a
            # literal "you asked, I answered" history pair (confirmed
            # live: a field correction — "จังหวัดผิดครับ เปลี่ยนเป็นชลบุรี"
            # — sent while a confirmation was genuinely pending fell
            # through to RAG entirely, because CustCode had been supplied
            # several turns earlier via Identifier Memory and the replay
            # could never reconstruct the SAME confirmation text to match
            # against). A caller that already knows — via services/
            # pending_confirmation_service.py, the single source of truth
            # for "is a confirmation genuinely pending", never re-derived
            # here — passes that action's id directly; the Generic
            # Continuation Intent Guard immediately below still applies
            # in full, so a genuine topical diversion overrides this
            # exactly as it would any other continuation.
            if not continuation_action:
                pending_action_id = context.get("pending_action_id")
                if pending_action_id:
                    pending_full_action = self.registry.get_full(pending_action_id, mask_secrets=False)
                    if pending_full_action and pending_full_action.get("enabled"):
                        continuation_action = pending_full_action

            # Generic Continuation Intent Guard (Confirmation/Collection
            # Continuation Correctness fix, 2026-08-24) — confirmed live:
            # once an action asks a follow-up question, _resolve_
            # continuation_action keeps it "active" for every subsequent
            # turn purely because the LAST assistant turn matches that
            # question — regardless of what the customer's new message
            # actually says. A message that decisively matches a
            # DIFFERENT Business Action's own keywords (the SAME generic
            # scoring search_candidate_actions/select_best_action use for
            # fresh routing below, never a new mechanism, never a
            # hardcoded action/phrase) means the customer has moved on to
            # a new request — let fresh classification handle this turn
            # on its own merits instead of silently forcing it to keep
            # answering the pending action's question (which used to
            # count as a failed retry attempt purely because the message
            # didn't supply the pending value, even when it was itself a
            # perfectly valid, different, or same-action request). A
            # message with no decisive match for anything (genuinely
            # off-topic/confused replies) leaves continuation untouched —
            # existing behavior for THAT case is unchanged. See
            # _count_genuine_retries below for the companion fix that
            # keeps retry counting itself from over-counting a turn like
            # this one after the fact.
            if continuation_action:
                diversion_candidates = search_candidate_actions(
                    self.registry, workflow=workflow_hint, message=message, collected_slots={})
                diversion_selected = select_best_action(
                    diversion_candidates, minimum_score=1.0 if not workflow_hint else 0.5)
                # Address Change Full UAT fix (2026-08-24) — this guard's
                # own stated intent (see the comment above) is a message
                # that "decisively matches a DIFFERENT Business Action's
                # own KEYWORDS", but the check used the candidate's FULL
                # score, which also includes identifier-pattern evidence.
                # A bare value the pending action itself just asked for
                # (e.g. a ShipmentCode reply to "กรุณาแจ้งเลขที่บิล/
                # Shipmentค่ะ") can coincidentally ALSO match a different
                # action's identically-shaped parameter (confirmed live:
                # a ShipmentCode reply mid-address-change diverted to
                # SearchDataShipment purely because it also configures a
                # ShipmentCode parameter) — that is structural overlap on
                # the SAME datum the continuation is waiting for, never
                # real evidence the customer changed topic. Only genuine
                # keyword/example/description overlap — actual topical
                # signal — may override continuation; identifier-pattern-
                # only "evidence" never does.
                diversion_has_topical_evidence = diversion_selected and any(
                    "keyword/example/AI-description overlap" in r for r in (diversion_selected.get("_reasons") or []))
                if diversion_has_topical_evidence and diversion_selected["id"] != continuation_action["id"]:
                    continuation_action = None
                elif continuation_action:
                    # Mid-Collection RAG Diversion fix (Task 03, 2026-08-25)
                    # — the check above only recognizes a diversion to a
                    # DIFFERENT Business Action; a genuine general/RAG
                    # question (no Business Action evidence at all, e.g.
                    # "CBM คืออะไร" while ReceiverName is pending) had no
                    # way to override continuation, so _handle_dynamic_
                    # collection tried to bind it to the pending slot,
                    # failed (ReceiverName's own address-component
                    # structural-only binding correctly rejects it — Task
                    # 02B/02C's protections are untouched here), and
                    # silently re-asked the same question instead of
                    # answering it. Reuses classify_question fresh
                    # (memory-free, same spirit as diversion_candidates
                    # above) — only a confidently RAG_ONLY result counts;
                    # CLARIFICATION_REQUIRED (the Wrong-Intent Prevention
                    # fix just above) or anything else leaves continuation
                    # untouched, since those are not decisive evidence the
                    # customer abandoned the pending slot. ALSO requires a
                    # genuine question marker (_QUESTION_MARKER_RE) — RAG_ONLY
                    # is classify_question's own default fallback for ANY
                    # non-Business-Action text, so without this a bare,
                    # correct answer to the pending slot itself (e.g.
                    # "ผู้รับชื่อสมชาย", which also matches no Business
                    # Action) would be misread as a diversion and never
                    # reach _handle_dynamic_collection at all. ALSO
                    # excludes two further, already-established signals
                    # that a question-shaped reply is still genuinely
                    # ABOUT the pending flow, not a real topic change:
                    # _COLLECTION_STATUS_QUERY_RE ("มีข้อมูลอะไรบ้าง" — its
                    # own dedicated handling inside _handle_dynamic_
                    # collection must get first refusal, never pre-empted
                    # here) and _REFERENCE_MARKER_RE (e.g. "บิลนี้สถานะ
                    # อะไร" continuing to ask about the SAME just-mentioned
                    # record — "สถานะ" is already one of that pattern's own
                    # referring-expression markers). Confirmed live: without
                    # these exclusions, an ambiguous but still-on-topic
                    # follow-up like "บิลนี้สถานะอะไร" (right after being
                    # asked for CustCode) was wrongly swept into this same
                    # diversion, losing the in-progress order lookup
                    # entirely instead of continuing to wait for CustCode.
                    # Declarative Service-Intent Diversion (P0 Final Fix,
                    # 2026-08-28) — widened alongside the existing question-
                    # marker trigger below: a declarative "ต้องการ/อยาก/
                    # สนใจ" statement with no question particle at all
                    # (_DECLARATIVE_INTENT_MARKER_RE) is EQUALLY valid
                    # evidence the customer has moved to a fresh,
                    # unrelated topic — confirmed live: "ผมต้องการสั่งซื้อ
                    # สินค้าจากจีนครับ" sent while an unrelated Shipment
                    # check was still pending (unverified, waiting on
                    # CustCode) never reached this check at all before,
                    # since it carries no _QUESTION_MARKER_RE particle.
                    # Still fully gated by the SAME classify_question ==
                    # RAG_ONLY confirmation as the question-marker path —
                    # this only widens WHEN to ask, never what counts as a
                    # genuine answer.
                    if ((_QUESTION_MARKER_RE.search(message or "")
                         or _DECLARATIVE_INTENT_MARKER_RE.search(message or ""))
                            and not _COLLECTION_STATUS_QUERY_RE.search(message or "")
                            and not _REFERENCE_MARKER_RE.search(message or "")):
                        fresh_classification = classify_question(message, self.registry)
                        if fresh_classification["classification"] == "RAG_ONLY":
                            continuation_action = None

                    # Independent-Request Escape (Final Two Blockers,
                    # 2026-08-28) — confirmed live: "ช่วยคิดข้อความขายสินค้า
                    # นี้ให้หน่อย" (a General Chat creative-writing request)
                    # sent right after a Shipment lookup asked for CustCode
                    # was silently swallowed as a failed CustCode answer —
                    # the guards above only ever fire on a question/
                    # declarative-intent marker, and this message has
                    # neither. Gated on _REQUEST_MARKER_RE ("ช่วย/ขอ/
                    # รบกวน...หน่อย/ด้วย") specifically — a POSITIVE signal
                    # the message is itself a concrete, polite ask for
                    # something, not merely the ABSENCE of a slot match.
                    # Confirmed live this distinction is load-bearing: a
                    # genuine non-answer/confused reply ("เอิ่มมม", "ไม่ทราบ
                    # ครับ", "abc") ALSO fails to match the pending slot's
                    # shape and carries no ERP/company-topic evidence
                    # either, but carries no request marker — those must
                    # keep counting as retry attempts against the SAME
                    # pending action (existing retry/escalation logic),
                    # never escape here. Scoped to STRUCTURED (pattern-
                    # validated) pending slots only — CustCode/OrderCode/
                    # ShipmentCode/Tracking-style parameters, never a free-
                    # text slot (e.g. ReceiverName/Address). Escapes ONLY
                    # when ALL of: a request marker is present, the message
                    # doesn't match the pending slot's own validation_
                    # pattern shape, carries no ERP/action evidence of its
                    # own (diversion_selected, already computed above —
                    # same signal, not a new one), and carries no Shipify
                    # company-topic evidence either (reusing services/
                    # playground_orchestrator.py's existing topic gate,
                    # imported lazily to avoid a module-level import cycle
                    # — same pattern _run_rag_pipeline already uses).
                    if continuation_action and _REQUEST_MARKER_RE.search(message or ""):
                        replayed_for_escape = _replay_business_action_collection(
                            continuation_action, self.registry, history, customer_context)
                        next_param_for_escape = _next_expected_parameter(
                            continuation_action, self.registry, replayed_for_escape)
                        pattern_for_escape = (next_param_for_escape or {}).get("validation_pattern")
                        if next_param_for_escape and pattern_for_escape:
                            tokens_for_escape = [t for t in _TOKEN_SPLIT_RE.split(message or "") if t]
                            try:
                                matches_slot_shape = any(
                                    re.compile(pattern_for_escape).match(tok) for tok in tokens_for_escape)
                            except re.error:
                                matches_slot_shape = any(
                                    _validate_generic_identifier(tok) for tok in tokens_for_escape)
                            has_erp_evidence = bool(diversion_selected)
                            from services.playground_orchestrator import (
                                _COMPANY_OPERATIONAL_TOPIC_RE as _escape_company_re,
                                _CHINA_SOURCED_ACTION_RE as _escape_china_re,
                            )
                            has_company_topic_evidence = bool(
                                _escape_company_re.search(message or "") or _escape_china_re.search(message or ""))
                            if not matches_slot_shape and not has_erp_evidence and not has_company_topic_evidence:
                                continuation_action = None

            detail_sibling_action = None
            if not continuation_action and customer_context.get("last_business_action") \
                    and _DETAIL_INTENT_RE.search(message or ""):
                # LIST -> DETAIL sibling preference (Issue 3) — deliberately
                # runs BEFORE generic fresh-search scoring below: that
                # scoring has no way to prefer "the detail sibling of the
                # action just used" over some OTHER same-category action
                # that also happens to be satisfiable from Identifier
                # Memory right now (e.g. a shipment list's category also
                # contains a Tracking lookup — both can look equally
                # "available," but only one is actually the shipment's
                # OWN detail record). See _find_detail_sibling's own
                # docstring for the tie-break logic.
                try:
                    last_action = self.registry.get_by_key(customer_context["last_business_action"])
                    if last_action and last_action.get("enabled"):
                        last_full = self.registry.get_full(last_action["id"], mask_secrets=False)
                        detail_sibling_action = _find_detail_sibling(self.registry, last_full, customer_context)
                except Exception:
                    detail_sibling_action = None

            if continuation_action:
                selected = continuation_action
                candidates = [selected]
                developer_trace["selection_source"] = "conversation_continuation"
            elif detail_sibling_action:
                selected = detail_sibling_action
                candidates = [selected]
                developer_trace["selection_source"] = "conversation_reference_detail"
            else:
                # Root Change 1 (Final Systemic Routing Fix, 2026-08-28) —
                # classify the message's informational-vs-private-action
                # intent ONCE, here, BEFORE classify_question()'s own
                # private-Business-Action tie/ambiguity resolution ever
                # runs (that machinery — services/hybrid_question_
                # classifier.py — scores every enabled identity-gated
                # API/WEBHOOK action; the forensic audit reproduced live
                # that "ติดต่อ Shipify ยังไง" ties two such actions there
                # BEFORE any informational classification gets a chance to
                # win). `exclude_private` is threaded into every candidate
                # search below (classify_question's own tie/ambiguity
                # scoring, the entity-continuation diversion check, and
                # the main fresh search) so a SHIPIFY_INFORMATION message
                # can never be offered — or tied between — identity-gated
                # actions at any of those points; every OTHER existing
                # behavior (vague-interest clarification, HYBRID
                # detection, entity-continuation field matching, RAG-type
                # action selection) is completely unchanged, since none of
                # it depends on identity-gated actions being present.
                turn_intent = classify_turn_intent(message)

                # Immediate-Context-Over-Stale-ERP guard (2026-09-01) — a
                # short contextual follow-up ("แล้วจีนล่ะ" after a Thai-
                # warehouse RAG answer, "แล้วทางเรือล่ะ" after a road-rate
                # answer) names no identifier and no self-referencing
                # request of its own, so classify_turn_intent sees only
                # "AMBIGUOUS" and leaves identity-gated API/WEBHOOK actions
                # eligible. A stale customer_context identity from an
                # EARLIER, since-abandoned ERP exchange can then resurrect a
                # private profile/wallet lookup via _resolve_conversation_
                # reference — a customer-data relevance/privacy regression.
                # Reuses two existing deterministic signals, ANDed, never a
                # new classifier: (1) resolve_conversation already marks
                # this turn as a genuine follow-up (`followup_type` set) and
                # rewrites it to its real subject; (2) _last_assistant_turn_
                # requests_input already tells us the immediately preceding
                # assistant turn was NOT an active ERP parameter/
                # confirmation request. When both hold AND the rewritten
                # subject carries no identifier token of its own (so a
                # genuine "แล้ว SP1002 ล่ะ" order follow-up is untouched),
                # the immediate informational context wins — exactly as an
                # explicit SHIPIFY_INFORMATION turn would. The cheap
                # contrastive-particle precheck ("แล้ว…ล่ะ/ละ" — the same
                # shape rag/query_resolution._FOLLOWUP_RE keys on) keeps
                # resolve_conversation off the hot path for every message
                # that could not possibly be this kind of follow-up.
                if (turn_intent == "AMBIGUOUS" and history
                        and "แล้ว" in (message or "")
                        and ("ล่ะ" in (message or "") or "ละ" in (message or ""))):
                    from rag.query_resolution import resolve_conversation as _resolve_conv
                    _conv = _resolve_conv(message, history)
                    _recent_asst = [t.get("content") for t in history if t.get("role") == "assistant"][-2:]
                    _resolved_q = _conv.get("resolved_question") or ""
                    _resolved_has_identifier = any(
                        _validate_generic_identifier(tok) and not tok.isdigit()
                        for tok in _TOKEN_SPLIT_RE.split(_resolved_q) if tok)
                    if (_conv.get("followup_type") and not _resolved_has_identifier
                            and not any(_last_assistant_turn_requests_input(c) for c in _recent_asst)):
                        turn_intent = "SHIPIFY_INFORMATION"
                        developer_trace["turn_intent_coerced"] = "rag_continuity_followup_over_stale_erp"

                developer_trace["turn_intent"] = turn_intent
                exclude_private = _IDENTITY_GATED_ACTION_TYPES if turn_intent == "SHIPIFY_INFORMATION" else None

                # Hybrid Question Classifier (2026-08-02 Production
                # Integration Sprint, Phase 1 Step C) — only consulted on
                # a FRESH turn (a continuation in progress always takes
                # precedence, unchanged). Its role here is narrow: detect
                # a genuinely Hybrid or ambiguous question. For every
                # other classification (RAG_ONLY/ERP_ONLY/UNKNOWN), this
                # result is NOT used to select an action — the existing,
                # unchanged search below still does that; no duplicate
                # routing decision is made.
                classification = classify_question(message, self.registry, exclude_action_types=exclude_private)
                developer_trace["classification"] = classification
                if classification["classification"] == "HYBRID":
                    return self._handle_hybrid_turn(classification, message, history, context,
                                                      developer_trace, start, workflow=workflow_hint)
                if classification["classification"] == "CLARIFICATION_REQUIRED":
                    return self._route_clarification(classification, message, context,
                                                        developer_trace, start, workflow=workflow_hint)

                # Entity Continuation (Final Conversation State Engine,
                # 2026-08-15) — checked BEFORE identifier-memory-boosted
                # fresh-search, not merely as its fallback. Reason: once
                # several identifiers have accumulated in memory (e.g. a
                # tracking search incidentally also teaches the record's
                # own ShipmentCode — see _capture_response_derived_
                # identifiers), a DIFFERENT same-domain action whose own
                # required parameters ALL happen to be satisfiable from
                # that memory can otherwise score high enough to steal
                # the turn from an ACTIVE conversation purely by
                # coincidence — not because the customer's message ever
                # asked for it. A message that reads as a follow-up
                # reference to the action just used (_resolve_
                # conversation_reference: field/marker evidence) is
                # preferred UNLESS the message ALSO carries its own
                # decisive topical evidence for a genuinely different
                # action (checked memory-FREE, so remembered identifiers
                # can never manufacture that evidence on their own).
                referenced = _resolve_conversation_reference(self.registry, message, customer_context)
                fresh_topic_beats_reference = False
                # Pure Identifier Guard applied to continuation too: a
                # message that IS, in its entirety, a bare identifier-
                # shaped token (e.g. "FT3182") carries no real topical
                # signal — it just happens to pattern-match whichever
                # parameter (often a widely-shared one like CustCode)
                # every candidate action requires. Letting THAT alone
                # win against an active conversation reintroduces the
                # exact "entity steal" failure this reordering exists to
                # prevent, just via the message's own pattern match
                # instead of remembered identifiers. A bare code always
                # fills the ACTIVE action's pending slot; only a message
                # with real topical content (a keyword, a domain word)
                # may switch domains.
                is_bare_identifier_message = bool((message or "").strip()) \
                    and _validate_generic_identifier((message or "").strip())
                if referenced and not is_bare_identifier_message:
                    topic_only_candidates = search_candidate_actions(
                        self.registry, workflow=workflow_hint, message=message, collected_slots={},
                        exclude_action_types=exclude_private)
                    if topic_only_candidates and topic_only_candidates[0]["id"] != referenced["id"] \
                            and topic_only_candidates[0]["_score"] >= (1.0 if not workflow_hint else 0.5):
                        fresh_topic_beats_reference = True

                if referenced and not fresh_topic_beats_reference:
                    selected = referenced
                    candidates = [selected]
                    developer_trace["selection_source"] = "conversation_reference"
                else:
                    # Identifier Memory (2026-08-15) — a remembered
                    # CustCode/OrderCode/ShipmentCode/Tracking from
                    # earlier THIS conversation also counts as evidence
                    # an identifier-requiring action is relevant, exactly
                    # like a slot this turn's own message bound
                    # (_parameter_availability_score doesn't care which
                    # turn supplied the value).
                    identifier_memory_slots = {
                        param_name: customer_context[profile_field]
                        for profile_field, param_name in IDENTIFIER_MEMORY_FIELDS
                        if customer_context.get(profile_field)
                    } if customer_context else {}
                    candidates = search_candidate_actions(self.registry, workflow=workflow_hint, message=message,
                                                            collected_slots=identifier_memory_slots,
                                                            exclude_action_types=exclude_private)
                    selected = select_best_action(candidates, minimum_score=1.0 if not workflow_hint else 0.5)
                    developer_trace["selection_source"] = "fresh_search"

                    # General Informational Question Guard (Customer
                    # Journey UAT, 2026-08-27; broadened 2026-08-27 same
                    # day — Mixed Routing / Stuck Workflow production fix)
                    # — confirmed live twice: (1) "บริษัทมีประกัน All Risk
                    # ให้ทุกออเดอร์ไหมครับ" (a general policy question)
                    # matched searchdataorderlist purely via its generic
                    # "ออเดอร์" keyword; (2) "เปลี่ยนที่อยู่จัดส่งในระบบยังไง"
                    # (a HOW-TO informational question about the address-
                    # change PROCESS) matched requestshippingaddresschange
                    # purely via its "เปลี่ยนที่อยู่" keyword, identical to
                    # how a real "please change my address" request would
                    # match. Neither keyword can be removed from its
                    # action (both legitimately appear in real account-
                    # specific requests too), so this is, in both cases, a
                    # selection-time veto, not a keyword change. Vetoes a
                    # fresh selection when EITHER of two independent
                    # informational shapes is proven, AND (always required)
                    # the winning candidate's own evidence contains no
                    # identifier-pattern contribution (a message that ALSO
                    # supplies a real CustCode/OrderCode is never vetoed —
                    # that's real evidence of an actual account-specific
                    # request):
                    #   (a) the message names the company as subject
                    #       ("บริษัท") AND is phrased as a genuine question
                    #       (_QUESTION_MARKER_RE) — never present in any of
                    #       the legitimate preserve-list examples (self-
                    #       referencing requests like "เช็กบิลของผม"/"เช็ก
                    #       Shipment นี้"/"ขอดู Wallet ของผม" all name
                    #       THEMSELVES, never "the company");
                    #   (b) the message is phrased as a genuine question
                    #       (_QUESTION_MARKER_RE, e.g. "ยังไง"/"อะไร"/"ไหม")
                    #       AND carries NO polite-request marker of its own
                    #       (_REQUEST_MARKER_RE, e.g. "ช่วย...หน่อย"/"ขอ...
                    #       ด้วย" — already used elsewhere in this same
                    #       function for the identical informational-vs-
                    #       request distinction). This is what separates
                    #       "เปลี่ยนที่อยู่จัดส่งในระบบยังไง" (informational,
                    #       vetoed) from "ช่วยเปลี่ยนที่อยู่จัดส่งให้หน่อย" /
                    #       "อยากเปลี่ยนที่อยู่จัดส่งครับ" (real requests,
                    #       neither carries a question marker at all, so
                    #       condition (b) never even applies to them).
                    #
                    # Branch (b) additionally requires ALL of (confirmed
                    # live via full regression -- each guards one real,
                    # distinct false-positive, not a hypothetical):
                    #   - the winning candidate is an identity-gated
                    #     action (API/WEBHOOK). A RAG-type "Business
                    #     Action" (a knowledge-base entry configured with
                    #     its own keywords, e.g. "คลังสินค้าอยู่ที่ไหน" ->
                    #     a warehouse-location KB entry) is never
                    #     identity-gated in the first place -- it IS the
                    #     informational answer, so it must never be
                    #     vetoed away from itself.
                    #   - the message is NOT itself multi-clause
                    #     (_split_clauses returns >1 part) -- a compound
                    #     ask like "อยากเปลี่ยนที่อยู่จัดส่งบิลนี้ และ CBM
                    #     คำนวณยังไง" carries a real action-request FIRST
                    #     clause plus a genuine question SECOND clause;
                    #     _QUESTION_MARKER_RE matches the whole string
                    #     regardless of which clause it came from, so
                    #     without this guard a real request gets vetoed
                    #     purely because it happens to be joined to an
                    #     unrelated question. Multi-clause messages are
                    #     the existing Hybrid/multi-intent pipeline's own
                    #     job (classify_question/_split_clauses above),
                    #     never this guard's.
                    #   - the message carries no _REFERENCE_MARKER_RE
                    #     self-reference ("ของผม"/"ของฉัน"/"อันนี้"/...) --
                    #     "ข้อมูลลูกค้าของผมมีอะไรบ้าง" ("what's in MY OWN
                    #     customer data") is phrased as a question but is
                    #     unmistakably a self-referencing account request.
                    #   - the message contains no bare identifier-shaped
                    #     token of its own (_validate_generic_identifier,
                    #     checked message-wide, independent of whether
                    #     the WINNING action's own parameter happens to
                    #     have a validation_pattern configured) --
                    #     "คูปองของลูกค้า SP1014 มีอะไรบ้าง" names a
                    #     specific record (SP1014); the identifier-
                    #     pattern-evidence check above can miss this when
                    #     the selected action's own parameter has no
                    #     validation_pattern set, so this is a second,
                    #     independent identifier check, not a duplicate.
                    # Falls through to ordinary RAG/clarification handling
                    # below, exactly like any other unmatched message.
                    # Widened alongside the Mid-Collection RAG Diversion
                    # guard above (P0 Final Fix, 2026-08-28) — a declarative
                    # "ต้องการ/อยาก/สนใจ" statement (_DECLARATIVE_INTENT_
                    # MARKER_RE) with no question particle at all must also
                    # reach this veto's own evaluation, not only a message
                    # phrased as a literal question.
                    general_policy_question_vetoed = False
                    if selected and (_QUESTION_MARKER_RE.search(message or "")
                                      or _DECLARATIVE_INTENT_MARKER_RE.search(message or "")) \
                            and not any("parameter identifier pattern" in r for r in (selected.get("_reasons") or [])):
                        is_company_policy_question = "บริษัท" in (message or "")
                        # Strict Shipify RAG Grounding (2026-08-27) —
                        # confirmed live: "Wallet ผมเหลือเท่าไหร่" (a genuine
                        # account-specific balance question) was wrongly
                        # vetoed here too — _REFERENCE_MARKER_RE only
                        # recognizes the possessive "ของผม" ("of mine"),
                        # never the bare speaker pronoun "ผม" used without
                        # "ของ" (equally a clear self-reference in Thai,
                        # e.g. "...ผมเหลือเท่าไหร่" = "...I have left"). A
                        # narrow, local addition (not broadening the SHARED
                        # _REFERENCE_MARKER_RE, which other call sites also
                        # depend on) — bare "ผม/ฉัน/ดิฉัน" only counts here
                        # when this veto's OTHER conditions (a Business
                        # Action already matched, question-marker present,
                        # no request marker) are already true, so it can
                        # only ever narrow this one veto further, never
                        # widen anything else.
                        # P0 Final Fix (2026-08-28) — excludes the plain
                        # declarative-subject usage ("ผมต้องการ...", "ผม
                        # อยาก...", never a possessive reference to a
                        # private noun) via _SUBJECT_INTENT_RE, so "ผม
                        # ต้องการสั่งซื้อสินค้าจากจีนครับ" is no longer
                        # disqualified from this veto purely for containing
                        # the bare word "ผม" — while "Wallet ผมเหลือเท่าไหร่"
                        # (ผม NOT immediately followed by a want-verb) is
                        # completely unaffected and still correctly counts
                        # as self-reference.
                        _bare_self_reference = (bool(re.search(r"ผม|ฉัน|ดิฉัน", message or ""))
                                                 and not _SUBJECT_INTENT_RE.search(message or ""))
                        # P0 Final Fix (2026-08-28) — a private-action verb
                        # (เปลี่ยน/แก้/...) only counts as "real evidence,
                        # never veto" when the message has NO genuine
                        # question marker of its own — a pure declarative
                        # statement ("อยากเปลี่ยนที่อยู่จัดส่งครับ", no "ยังไง")
                        # is a real request, but "เปลี่ยนที่อยู่จัดส่งในระบบ
                        # ยังไง" (a genuine "how do I..." informational
                        # question that HAPPENS to also contain "เปลี่ยน")
                        # must still be vetoed exactly as before — the verb
                        # alone is not decisive once the message is already
                        # phrased as a how-to question about the process.
                        _has_question_marker = bool(_QUESTION_MARKER_RE.search(message or ""))
                        _declarative_private_action = (
                            not _has_question_marker and _PRIVATE_ACTION_VERB_RE.search(message or ""))
                        # P0 Final Fix (2026-08-28) — a bare, PURELY NUMERIC
                        # token ("1688") must never count as identifier
                        # evidence on its own: every real Shipify identifier
                        # (CustCode/OrderCode "PO.../ShipmentCode "FT.../
                        # Tracking) is letter-prefixed, so a pure-digit
                        # string is far more likely a website/platform name
                        # ("รองรับเว็บ 1688 ไหม" — does Shipify support the
                        # 1688.com platform) or an ordinary number mentioned
                        # in conversation than an actual account identifier.
                        # _validate_generic_identifier itself is left
                        # completely untouched (shared by slot-filling
                        # elsewhere, where a pure-digit tracking number IS
                        # legitimately valid) — this refines only THIS
                        # veto's own reading of its result.
                        _identifier_evidence = any(
                            _validate_generic_identifier(tok) and not tok.isdigit()
                            for tok in _TOKEN_SPLIT_RE.split(message or "") if tok)
                        is_unrequested_howto_question = (
                            not _REQUEST_MARKER_RE.search(message or "")
                            and selected.get("action_type") in ("API", "WEBHOOK")
                            and len(_split_clauses(message or "")) <= 1
                            and not _REFERENCE_MARKER_RE.search(message or "")
                            and not _bare_self_reference
                            and not _declarative_private_action
                            and not _identifier_evidence
                            and not _PRIVATE_STATE_QUERY_RE.search(message or "")
                        )
                        if is_company_policy_question or is_unrequested_howto_question:
                            selected = None
                            candidates = []
                            general_policy_question_vetoed = True
                            developer_trace["selection_source"] = "fresh_search_vetoed_general_policy_question"

                    # A vetoed general policy question must not fall back
                    # to some earlier, unrelated remembered topic either —
                    # it is a fresh question in its own right, not a
                    # continuation of anything.
                    if not selected and referenced and not general_policy_question_vetoed:
                        # The memory-boosted search still found nothing
                        # (fresh_topic_beats_reference was True only
                        # because of a WEAKER, sub-threshold memory-free
                        # score that never actually got selected here) —
                        # fall back to the reference after all, exactly
                        # as before this reordering.
                        selected = referenced
                        candidates = [selected]
                        developer_trace["selection_source"] = "conversation_reference"

            if not selected:
                if workflow_hint:
                    # Safe Migration: no Business Action is configured for
                    # this ERP workflow at all -> legacy INTENT_SCHEMAS
                    # fallback, unchanged behavior from before this task.
                    return self._handle_legacy_workflow(
                        workflow_hint, message, history, context, developer_trace, start,
                        reason="no_business_action_for_workflow")
                # RAG Guard (Final Conversational Correctness, 2026-08-15)
                # — a message that IS, in its entirety, a bare
                # identifier-shaped token (never a substring match — the
                # WHOLE stripped message) with no pending slot, no
                # remembered action to resume, and no Business Action
                # match of its own is customer-service-shaped, not a
                # knowledge-base question. Asking what to check it
                # against is honest; running semantic search against
                # documentation for a bare code and reporting "not found"
                # is not — the customer never asked a knowledge question
                # in the first place.
                bare_value = (message or "").strip()
                if bare_value and _validate_generic_identifier(bare_value):
                    # Identifier Continuity (2026-08-16) — this bare code
                    # must still be REMEMBERED for the next turn, exactly
                    # like the "identifier + other words" guard below
                    # already does, otherwise a follow-up naming only an
                    # intent ("คำสั่งซื้อ") has nothing to bind it to and
                    # either re-asks for the identifier or, worse, selects
                    # an action with a still-missing required parameter.
                    captured = _opportunistic_identifier_capture(self.registry, bare_value)
                    if captured:
                        developer_trace.setdefault("information_collection_status", {})["collected_parameters"] = captured
                    reply = _build_response(
                        text=f"ได้ค่ะ ต้องการตรวจสอบข้อมูลอะไรของ {bare_value} คะ เช่น ข้อมูลลูกค้า คำสั่งซื้อ หรือพัสดุ")
                    return self._finalize(reply=reply, routing_type="WORKFLOW", workflow=workflow_hint,
                                           developer_trace=developer_trace, context=context, start=start,
                                           alert=_detect_alert(message, context))
                # Pure Identifier Guard extended to "identifier + other
                # words" (2026-08-16) — a message like "ผม FT3182" isn't a
                # bare identifier as a WHOLE string (it carries "ผม" too),
                # so the guard above doesn't catch it, but it still names
                # a real identifier and RAG has no realistic chance of
                # having information about an arbitrary customer/order/
                # shipment code. Previously this fell through to RAG with
                # only a "remember it for next turn" consolation; now it
                # asks the SAME natural clarifying question the bare-
                # identifier guard uses, referencing the captured value —
                # RAG is never queried for a message that just introduces
                # an identifier. _opportunistic_identifier_capture only
                # ever returns a STRUCTURAL match against one of the
                # platform's own configured identifier patterns, never a
                # free-text guess, so this stays config-driven.
                captured = _opportunistic_identifier_capture(self.registry, message)
                if captured:
                    developer_trace.setdefault("information_collection_status", {})["collected_parameters"] = captured
                    identifier_value = next(iter(captured.values()))
                    reply = _build_response(
                        text=f"ได้ค่ะ ต้องการตรวจสอบข้อมูลอะไรของ {identifier_value} คะ เช่น ข้อมูลลูกค้า คำสั่งซื้อ หรือพัสดุ")
                    return self._finalize(reply=reply, routing_type="WORKFLOW", workflow=workflow_hint,
                                           developer_trace=developer_trace, context=context, start=start,
                                           alert=_detect_alert(message, context))
                return self._route_safe_fallback(message, history, context, developer_trace, start,
                                                  reason="no_matching_business_action")

            # search_candidate_actions()/enabled_actions() returns bare
            # `business_actions` rows (no joined parameters table) — must
            # check the real parameter rows, not the cheap list, to know
            # whether this action has Registry-driven parameter metadata.
            has_parameter_metadata = False
            if selected.get("action_type") in ("API", "WEBHOOK"):
                if selected.get("parameters") is not None:
                    has_parameter_metadata = bool(selected.get("parameters") or selected.get("parameter_groups"))
                else:
                    has_parameter_metadata = bool(self.registry.get_parameters(selected["id"])
                                                   or selected.get("parameter_groups"))
            if selected.get("action_type") in ("API", "WEBHOOK") and not has_parameter_metadata and workflow_hint:
                # Safe Migration: this specific Business Action has no
                # parameter metadata configured yet -> legacy fallback,
                # tagged so Developer Mode shows a warning.
                return self._handle_legacy_workflow(
                    workflow_hint, message, history, context, developer_trace, start,
                    reason="business_action_has_no_parameter_metadata", selected_action_key=selected.get("action_key"))

            if selected.get("action_type") in ("API", "WEBHOOK") and has_parameter_metadata:
                return self._handle_dynamic_collection(
                    selected, workflow_hint, message, history, context, developer_trace, start, candidates)

            # RAG / TOOL / NOTIFICATION / HUMAN_HANDOFF / WORKFLOW, or an
            # API/WEBHOOK action that genuinely needs nothing collected —
            # execute directly, no Information Collection step needed.
            return self._execute_selected_action(
                selected, candidates, message, history, context, developer_trace, start,
                workflow=workflow_hint, intent=(actionable_intent or {}).get("actionable_intent"), collected_slots={})

        except Exception as e:
            # Registry/unexpected failure -> safe fallback, never an
            # internal exception surfaced to the customer.
            return self._finalize(
                reply=_build_response(text="ขอโทษด้วยค่ะ ระบบขัดข้องชั่วคราว รบกวนลองใหม่อีกครั้งนะคะ"),
                routing_type="SAFE_FALLBACK", workflow=None,
                developer_trace={**developer_trace, "error": sanitize_for_preview(str(e))},
                context=context, start=start, error="internal_error",
            )

    # ── Dynamic, Business-Action-driven Information Collection (NEW — Single Source of Truth) ──

    def _handle_dynamic_collection(self, action: Dict, workflow_hint: Optional[str], message: str,
                                    history: List[Dict], context: Dict, developer_trace: Dict, start: float,
                                    candidates: List[Dict]) -> Dict:
        action_id = action["id"]
        full_action = action if action.get("parameters") is not None else self.registry.get_full(action_id, mask_secrets=False)

        customer_context = context.get("customer_context") or {}
        collected = _replay_business_action_collection(full_action, self.registry, history, customer_context)
        # Multi-Intent Preservation fix (Task 03, 2026-08-25; corrected
        # 2026-08-26 — Production UAT finding) — a proxy for "is this
        # message the actual trigger" (a genuine continuation reply is
        # virtually never a compound, multi-clause sentence with its own
        # second conjunction-joined clause). Originally gated on `not
        # collected`, but `collected` is seeded from Identifier Memory
        # BEFORE history is even walked (see _replay_business_action_
        # collection's own customer_context seed) — meaning a customer
        # with ANY remembered identifier (e.g. a stored CustCode, true for
        # virtually every returning customer) already has a non-empty
        # `collected` on their very FIRST message, permanently disabling
        # this detection for the realistic case. `history` itself — not
        # `collected` — is the reliable signal: a genuinely fresh trigger
        # always has empty `history` regardless of what Identifier Memory
        # prefilled, while any real continuation reply always has at
        # least one prior turn in it.
        is_first_turn_for_this_action = not history and not context.get("pending_action_id")
        # Reliable Pending-Confirmation Seed (Address Change Full UAT
        # fix, 2026-08-24; corrected 2026-08-25, Correction-Persistence
        # P1 audit finding) — `pending_parameters` (the caller's own
        # persisted, already-validated snapshot as of the last turn) now
        # OVERRIDES whatever replay re-derives for the same field, not
        # the other way around. Replay always reprocesses turn 0 of
        # `history` fresh (see _replay_business_action_collection's own
        # "the very first user turn is always processed fresh" rule), so
        # for any field the ORIGINAL message already mentioned, replay
        # keeps re-deriving that ORIGINAL value on every single turn —
        # it is never "at least as fresh" once the customer has since
        # corrected that field. Confirmed live: a Province correction
        # applied at the confirmation stage was silently discarded on
        # the very next turn because replay's stale re-derivation of the
        # original address block still "found" a Province value, so the
        # old setdefault() below always kept that stale value instead of
        # the customer's own correction. Only trusted for THIS exact
        # action id, never applied to a different one.
        if context.get("pending_action_id") == action_id:
            for name, value in (context.get("pending_parameters") or {}).items():
                collected[name] = value

        # Generic Collection Status Query (Address Change Full UAT —
        # Status Query fix, 2026-08-24) — checked BEFORE this message is
        # ever bound to any parameter, so a question like "มีข้อมูลอะไร
        # บ้าง"/"ขาดอะไรอีก" can never be mistaken for a free-text answer
        # to whatever happens to be pending (confirmed live: an address-
        # component-tagged field is already protected from this by its
        # own structural-candidate-only binding, but this guards EVERY
        # Business Action generically, including a plain non_empty field
        # that would otherwise swallow it). Never binds, never touches
        # retry/escalation, never executes — this turn's reply text is
        # never `_generate_parameter_question`'s own bare text, so a
        # later _count_genuine_retries pass naturally never counts it as
        # a repeated, unanswered question.
        if _COLLECTION_STATUS_QUERY_RE.search(message or ""):
            status_reply = _compose_collection_status_reply(full_action, self.registry, collected)
            if status_reply:
                developer_trace["information_collection_status"] = {
                    "source": "business_action_registry",
                    "selected_business_action": full_action.get("action_key"),
                    "collected_parameters": dict(collected),
                    "status_query": True,
                }
                return self._finalize(reply=_build_response(text=status_reply), routing_type="WORKFLOW",
                                       workflow=workflow_hint, developer_trace=developer_trace,
                                       context=context, start=start, alert=_detect_alert(message, context))

        result = _bind_all_from_message(full_action, self.registry, collected, message)
        collected = result["collected"]
        ambiguous_candidates = result["ambiguous_candidates"]

        # Identifier Memory (Customer Intelligence V1; generalized for
        # Final Conversational Correctness, 2026-08-15) — a customer
        # identifier already established earlier THIS conversation
        # (persisted onto the profile by profiles/manager.py::
        # update_profile_from_turn, using the SAME IDENTIFIER_MEMORY_FIELDS
        # mapping) auto-fills a still-missing parameter of the same
        # concept name, so the customer is never asked to repeat an
        # identifier they already gave two turns ago. Never overrides a
        # value THIS turn's own message (or history replay) already
        # bound — an explicit, fresher value always wins. (`customer_context`
        # already seeded replay's OWN reconstruction above; re-applying it
        # here too covers a value THIS turn's live binding still left
        # missing, and is a no-op for anything replay already carried
        # through.)
        collected = _apply_identifier_memory(full_action, collected, customer_context)

        validation = self.registry.validate_can_execute(action_id, collected)
        next_after = None if validation["ok"] else _next_expected_parameter(full_action, self.registry, collected)
        is_complete = _is_execution_ready(validation, next_after, ambiguous_candidates)

        collection_status = {
            "source": "business_action_registry",
            "selected_business_action": full_action.get("action_key"),
            # Interrupted Workflow Auto-Resume fix (Task 02C, 2026-08-25) —
            # the caller (line_bot/webhook.py, admin/routes.py's Auto Mode)
            # needs this action's real id to persist a mid-collection
            # pending row (reusing the SAME pending_confirmations
            # mechanism already used at the confirmation-gate stage, just
            # with confirmation_required=False) so a temporary diversion
            # doesn't strand an otherwise-valid, incomplete collection
            # with no structural way back. Mirrors confirmation_gate's
            # own "action_id" field name below for the same concept.
            "selected_action_id": full_action.get("id"),
            "required_parameters": [p["name"] for p in full_action.get("parameters") or [] if p.get("required")],
            "parameter_groups": full_action.get("parameter_groups") or [],
            "collected_parameters": dict(collected),
            "missing_parameters": (validation.get("missing_required") or []) +
                                   [g["name"] for g in (validation.get("failed_groups") or [])],
            "ambiguous_candidates": ambiguous_candidates,
            "is_complete": is_complete,
            "completion_reason": "all_required_parameters_and_groups_satisfied" if is_complete else None,
            "missing_reason": None if is_complete else (
                "ambiguous_candidate" if ambiguous_candidates else "awaiting_customer_input"),
        }

        max_retry = (full_action.get("retry_rules") or {}).get("max_retry", 2)
        retry_count = 0
        escalation_required = False
        escalation_reason = None
        if not is_complete and next_after and not ambiguous_candidates:
            expected_question = _generate_parameter_question(next_after)
            retry_count = _count_genuine_retries(self.registry, history, expected_question)
            if _REFUSAL_RE.search(message or ""):
                escalation_required, escalation_reason = True, "user_refused_to_provide_information"
            elif retry_count >= max_retry:
                escalation_required, escalation_reason = True, "max_retry_exceeded"
        collection_status["retry_count"] = retry_count
        collection_status["max_retry"] = max_retry
        developer_trace["information_collection_status"] = collection_status

        if escalation_required:
            escalation_message = (full_action.get("escalation_rules") or {}).get("message") or (
                "ขออภัยค่ะ ทางเราไม่สามารถขอข้อมูลที่จำเป็นได้ครบถ้วน เดี๋ยวให้เจ้าหน้าที่ติดต่อกลับเพื่อช่วยตรวจสอบให้นะคะ")
            return self._route_human_handoff(
                message, history, context, developer_trace, start, reason=escalation_reason,
                workflow=workflow_hint, message_override=escalation_message)

        if not is_complete:
            if ambiguous_candidates:
                question = f"พบข้อมูล {len(ambiguous_candidates)} รายการค่ะ รบกวนระบุว่าต้องการใช้ค่าใด"
            elif next_after:
                question = _generate_parameter_question(next_after)
            else:
                question = "ขอข้อมูลเพิ่มเติมด้วยค่ะ"
            # Multi-Intent Preservation fix (Task 03, 2026-08-25) —
            # confirmed live: "อยากเปลี่ยนที่อยู่จัดส่งบิลนี้ และช่วย
            # ประเมินค่าขนส่งถึงบ้านให้หน่อย" silently dropped its second
            # clause entirely (classify_question's HYBRID segmentation
            # only ever triggers once a parameter VALUE is bound — a bare
            # trigger message with no identifier yet never reaches that
            # check at all, so the second clause was never even
            # evaluated). Reuses _split_clauses (the exact same fixed-
            # conjunction segmentation HYBRID mode already uses) purely to
            # DETECT a second, independent, question-shaped clause on the
            # actual trigger turn — never to answer it here (that would
            # mean invoking RAG/execution logic this fix does not touch).
            # A short, generic acknowledgment is appended so the second
            # intent is visibly NOT lost; developer_trace records it too,
            # satisfying "recognized as multiple, no silent loss"
            # structurally as well as conversationally. Gated to the
            # actual trigger turn only (is_first_turn_for_this_action) —
            # an ordinary continuation reply is never itself a compound,
            # conjunction-joined sentence with its own separate question,
            # so later turns are unaffected in practice.
            if is_first_turn_for_this_action:
                clauses = _split_clauses(message or "")
                # A second clause proves it's a genuine, independent ask via
                # EITHER a literal question word (_QUESTION_MARKER_RE, e.g.
                # "CBM คืออะไร") OR a polite-request marker (_REQUEST_MARKER_RE,
                # e.g. "ช่วยประเมินค่าขนส่งถึงบ้านให้หน่อย" -- phrased as a
                # request, not a question, but just as clearly a second,
                # separate thing the customer wants). Either signal alone is
                # sufficient; requiring BOTH would miss real compound
                # messages that use only one phrasing style.
                other_clauses = [
                    c for c in clauses
                    if c != message and (_QUESTION_MARKER_RE.search(c) or _REQUEST_MARKER_RE.search(c))
                ]
                if other_clauses:
                    developer_trace["secondary_intent_detected"] = other_clauses[0]
                    question += "\n\nรับทราบอีกเรื่องที่สอบถามมาด้วยนะคะ เดี๋ยวช่วยตอบให้หลังจากเรื่องนี้เสร็จค่ะ"
            reply = _build_response(text=question)
            return self._finalize(reply=reply, routing_type="WORKFLOW", workflow=workflow_hint,
                                   developer_trace=developer_trace, context=context, start=start,
                                   alert=_detect_alert(message, context))

        return self._execute_selected_action(
            full_action, candidates, message, history, context, developer_trace, start,
            workflow=workflow_hint, intent=workflow_hint, collected_slots=collected)

    # ── Legacy path (INTENT_SCHEMAS) — kept ONLY for backward compatibility ──
    #
    # Used exclusively when EITHER no Business Action exists for a
    # detected ERP workflow, OR one exists but has no parameter metadata
    # configured yet (Safe Migration). Its own required-slot definitions
    # are deprecated — treat them as legacy, not authoritative — but the
    # behavior is preserved unchanged for any customer who hasn't yet
    # configured Business Action parameters for a given workflow.

    def _handle_legacy_workflow(self, workflow: str, message: str, history: List[Dict], context: Dict,
                                 developer_trace: Dict, start: float, *, reason: str,
                                 selected_action_key: Optional[str] = None) -> Dict:
        developer_trace["legacy_fallback_used"] = True
        developer_trace["legacy_fallback_reason"] = reason
        developer_trace["legacy_fallback_warning"] = (
            f"No Business Action parameter metadata found for workflow '{workflow}'"
            + (f" (action '{selected_action_key}')" if selected_action_key else "")
            + " — falling back to legacy INTENT_SCHEMAS. Configure this Business Action's "
              "Parameters in the Business Action Center to make it the source of truth."
        )

        slot_state = build_collection_state(workflow, history, message)
        developer_trace["information_collection_status"] = slot_state

        if slot_state["escalation_required"]:
            return self._route_human_handoff(
                message, history, context, developer_trace, start,
                reason=slot_state.get("escalation_reason") or "information_collection_escalation",
                workflow=workflow, message_override=slot_state.get("escalation_message"))

        if not slot_state["is_complete"]:
            reply = _build_response(text=slot_state["follow_up_question"] or "ขอข้อมูลเพิ่มเติมด้วยค่ะ")
            return self._finalize(reply=reply, routing_type="WORKFLOW", workflow=workflow,
                                   developer_trace=developer_trace, context=context, start=start,
                                   alert=_detect_alert(message, context))

        candidates = search_candidate_actions(
            self.registry, workflow=workflow, message=message,
            collected_slots=slot_state["collected_slots"], action_types=["API", "WEBHOOK"])
        selected = select_best_action(candidates)
        if not selected:
            return self._route_safe_fallback(message, history, context, developer_trace, start,
                                              reason=f"no_business_action_for_workflow_{workflow}")

        return self._execute_selected_action(
            selected, candidates, message, history, context, developer_trace, start,
            workflow=workflow, intent=workflow, collected_slots=slot_state["collected_slots"])

    # ── Execute the selected Business Action via the Generic Action Executor ──

    def _attempt_self_verification(self, collected_slots: Dict, message: str, history: List[Dict],
                                    context: Dict, developer_trace: Dict) -> Dict:
        """Runs ONLY when Action Executor denied execution specifically
        because no verified customer_channel_bindings row exists yet
        (services/authorization_service.py::NO_VERIFIED_BINDING_REASON —
        checked by the caller before this is even invoked; a denial for
        a DIFFERENT reason, e.g. an already-verified customer typing a
        DIFFERENT CustCode, never reaches here and stays denied exactly
        as before this fix). Lets a customer establish their OWN
        verified binding immediately by proving they know a SECOND piece
        of information already on file for the CustCode they gave —
        services/self_verification_service.py does the actual lookup/
        compare, this method only drives the deterministic conversational
        sequence and persists the binding via the SAME services/
        customer_binding_service.py::link_verified staff-assisted path
        already uses (just a different `method` value — the module's own
        docstring already reserved this: "only `method` gains a new
        value and a new caller").

        PHONE ONLY is auto-verified, deliberately — every phone/email
        field this provider returns is masked server-side before this
        module (or anything else) ever sees it (services/
        business_action_registry.py::sanitize_response_body, applied
        unconditionally, not just for display — there is no unmasked
        value to compare against anywhere in this codebase, by design).
        That masking keeps a handful of TRAILING characters visible
        regardless of field type — confirmed live: for a phone number
        that is 3-4 trailing DIGITS, a reasonably distinguishing signal;
        for an email address it is virtually always just the domain
        suffix (e.g. "****.com"), which almost every real email shares —
        comparing against it would "verify" nearly any customer's email
        as a match, which is not verification at all. Email is still
        ASKED (matching the requested phone-then-email sequence, and
        genuinely useful context for staff) but only ever used as a
        supporting detail in the escalation handoff below — never as a
        second auto-verification factor.

        Deliberately scoped to ONLY the case actually observed and
        confirmed live: the customer's given identifier is CustCode
        (collected_slots["CustCode"], the AT_LEAST_ONE identifier group's
        own first/default member — in practice virtually always what
        gets asked/given first). If collected_slots has no CustCode at
        all (the customer identified some other way — email/phone/name),
        this returns {"resolved": False} with no reply_text and no
        escalate flag, so the caller falls through to the ORIGINAL,
        unchanged denial message rather than guessing against an
        untested identifier shape.

        Returns exactly one of:
          {"resolved": True} — a verified binding now exists; caller
              should re-attempt the same action execution.
          {"resolved": False, "reply_text": <question>, "escalate": False}
              — ask the next verification question.
          {"resolved": False, "reply_text": None, "escalate": True,
              "internal_reason": <str>} — phone did not match (email is
              never auto-checked); caller should escalate, carrying
              `internal_reason` into the handoff for CS visibility
              (never shown verbatim to the customer — the customer-
              facing wording stays the existing neutral, anti-
              enumeration AUTHORIZATION_DENIED_MESSAGE).
          {"resolved": False} — nothing applicable; fall through to the
              original unchanged denial behavior."""
        cust_code = collected_slots.get("CustCode")
        if not cust_code:
            return {"resolved": False}

        last_assistant = next(
            (t.get("content") or "" for t in reversed(history or []) if t.get("role") == "assistant"), "")
        from services.self_verification_service import verify_customer_claim
        from services.customer_binding_service import get_customer_binding_service

        if last_assistant.strip() == _SELF_VERIFY_ASK_EMAIL_TEXT:
            # Email is collected as a supporting detail for the human
            # handoff below, never auto-verified (see this method's own
            # docstring) — the reply itself is the "email", whatever it
            # is; no lookup/comparison call is made here at all.
            return {"resolved": False, "reply_text": None, "escalate": True,
                    "internal_reason": f"CustCode {cust_code}: phone did not match record on file; "
                                        f"customer-claimed email for manual follow-up: {message!r}"}

        if last_assistant.strip() == _SELF_VERIFY_ASK_PHONE_TEXT:
            result = verify_customer_claim(cust_code, claimed_phone=message, sb=self.registry._sb)
            developer_trace["self_verification"] = result
            if result["verified"]:
                get_customer_binding_service(self.registry._sb).link_verified(
                    tenant_id=context.get("tenant_id"), channel=context.get("channel"),
                    external_user_id=context.get("external_user_id"), cust_code=cust_code,
                    created_by="self_verification_service", method="self_service_phone")
                return {"resolved": True}
            return {"resolved": False, "reply_text": _SELF_VERIFY_ASK_EMAIL_TEXT, "escalate": False}

        # First time hitting this denial for this CustCode this exchange.
        return {"resolved": False, "reply_text": _SELF_VERIFY_ASK_PHONE_TEXT, "escalate": False}

    def _execute_selected_action(self, selected: Dict, candidates: List[Dict], message: str,
                                  history: List[Dict], context: Dict, developer_trace: Dict, start: float,
                                  *, workflow: Optional[str], intent: Optional[str], collected_slots: Dict) -> Dict:
        alert = _detect_alert(message, context)
        routing_type = selected.get("action_type") or "SAFE_FALLBACK"

        if routing_type == "RAG":
            # Production Integration Sprint (2026-08-02), Phase 1 Step C —
            # the ONE production RAG execution path (services/
            # decision_engine.py::_run_rag_pipeline, wrapping services/
            # playground_orchestrator.py::run_playground_turn) — never
            # the Action Executor's thinner _execute_rag. One shared
            # pipeline for AI Playground, LINE OA, and future channels.
            exec_result, exec_latency = self._run_rag_pipeline(message, history, context)
        elif _requires_confirmation(selected) and not context.get("confirmed"):
            # Confirmation gate (2026-08-09, SendLineNotiCS enablement) —
            # a COMMAND-type action (real external side effect) never
            # reaches the Action Executor on the strength of parameter
            # collection alone. All required parameters may already be
            # collected here; execution still waits for the caller
            # (webhook/admin) to re-invoke decide() with
            # context={"confirmed": True} once the customer/admin
            # explicitly confirms. SecretCode is never resolved on this
            # path — Credential Store lookup only happens inside the
            # Action Executor, which this branch never reaches.
            developer_trace["confirmation_gate"] = {
                "required": True, "confirmed": False,
                "action_id": selected.get("id"), "action_key": selected.get("action_key"),
                "reason": "This action performs a real external side effect and requires "
                          "explicit confirmation before execution.",
            }
            reply = _build_response(text=_generate_confirmation_question(selected, collected_slots))
            return self._finalize(reply=reply, routing_type="WORKFLOW", workflow=workflow,
                                   developer_trace=developer_trace, context=context, start=start, alert=alert)
        else:
            system_values = _extract_system_values(message, history=history)
            system_values.update(_compose_notification_message_system_value(selected, collected_slots))
            exec_context = {
                "question": message, "collected_slots": collected_slots, "workflow": workflow, "intent": intent,
                "conversation_context": context.get("conversation_context") or {},
                "customer_context": context.get("customer_context") or {},
                "current_user": context.get("current_user"), "developer_mode": bool(context.get("developer_mode")),
                # Generic system-derived values ANY tool/action may read
                # (e.g. a URL-handling tool, or a NOTIFICATION action's
                # composed Message — see _compose_notification_message_system_value).
                "system_values": system_values,
                # Propagated so services/authorization_service.py can tell
                # an admin/Playground caller (channel == "playground") apart
                # from a real customer-facing channel (Task 06) — dropping
                # this would make the Authorization Gate fail closed for
                # Playground/admin tooling too, which is not the intent.
                "channel": context.get("channel"),
                # Task 06B — the verified-binding lookup key. Both are
                # server-derived (webhook.py sets external_user_id from
                # the LINE webhook's own HMAC-verified event), never from
                # message text, so it's safe for authorization_service.py
                # to trust them directly.
                "tenant_id": context.get("tenant_id"),
                "external_user_id": context.get("external_user_id"),
            }
            exec_start = time.time()
            try:
                exec_result = self.executor.execute(selected["id"], exec_context)
            except Exception as e:
                exec_result = {"status": "error", "result": {}, "metadata": {}, "latency_ms": 0.0,
                                "error": sanitize_for_preview(str(e)), "logs": []}
            exec_latency = round((time.time() - exec_start) * 1000, 2)
        developer_trace["executor_type"] = selected.get("action_type")
        developer_trace["execution_result"] = exec_result
        developer_trace["candidate_business_actions"] = [
            {"action_key": c.get("action_key"), "score": c.get("_score"), "reasons": c.get("_reasons")}
            for c in candidates[:5]
        ]
        developer_trace["selected_business_action"] = selected.get("action_key")
        developer_trace["selection_reason"] = selected.get("_reasons")

        status = exec_result.get("status")

        if status == "handoff_prepared":
            reply = _build_response(text="กำลังโอนสายให้เจ้าหน้าที่ดูแลต่อค่ะ")
            return self._finalize(reply=reply, routing_type="HUMAN_HANDOFF", workflow=workflow,
                                   developer_trace=developer_trace, context=context, start=start,
                                   alert=alert, handoff_payload=exec_result.get("result", {}).get("handoff_payload"))

        if status == "not_implemented":
            reply = _build_response(text="รับทราบค่ะ ทีมงานจะติดต่อกลับโดยเร็วที่สุด")
            return self._finalize(reply=reply, routing_type=routing_type, workflow=workflow,
                                   developer_trace=developer_trace, context=context, start=start, alert=alert)

        if status == "error":
            reply = _build_response(text="ขอโทษด้วยค่ะ ไม่สามารถดำเนินการได้ในขณะนี้ รบกวนลองใหม่อีกครั้งนะคะ")
            return self._finalize(reply=reply, routing_type=routing_type, workflow=workflow,
                                   developer_trace=developer_trace, context=context, start=start,
                                   alert=alert, error=exec_result.get("error"))

        if status == "denied":
            # Authorization Gate (Task 06, 2026-08-26) — without this
            # branch, "denied" falls through to the "status == success"
            # path below and _compose_natural_reply mangles the neutral
            # denial dict ({"message": ...}) into a raw "message: ..."
            # line instead of surfacing it cleanly. Never treat a denial
            # as a successful business-action result.
            #
            # Self-Service Identity Verification (2026-08-29) — checked
            # ONLY for the specific "no binding exists yet" denial reason
            # (never an identity-switch rejection, a different, more
            # suspicious case that must stay denied outright). Gives the
            # customer a legitimate way to establish their own binding
            # right now (prove a second on-file detail) instead of always
            # needing a staff member to do it manually — see
            # _attempt_self_verification's own docstring. The Authorization
            # Gate itself (services/authorization_service.py,
            # services/action_executor.py) is completely untouched: this
            # only ever creates a REAL verified binding through the SAME
            # existing services/customer_binding_service.py::link_verified
            # path, then re-attempts the SAME execution, which now passes
            # the SAME unmodified check on its own.
            from services.authorization_service import NO_VERIFIED_BINDING_REASON
            auth_reason = (exec_result.get("metadata") or {}).get("authorization_denied_reason") or ""
            if auth_reason == NO_VERIFIED_BINDING_REASON:
                sv = self._attempt_self_verification(collected_slots, message, history, context, developer_trace)
                if sv.get("resolved"):
                    return self._execute_selected_action(
                        selected, candidates, message, history, context, developer_trace, start,
                        workflow=workflow, intent=intent, collected_slots=collected_slots)
                if sv.get("reply_text"):
                    reply = _build_response(text=sv["reply_text"])
                    return self._finalize(reply=reply, routing_type=routing_type, workflow=workflow,
                                           developer_trace=developer_trace, context=context, start=start, alert=alert)
                if sv.get("escalate"):
                    developer_trace["self_verification_escalation_reason"] = sv.get("internal_reason")
                    return self._route_human_handoff(
                        message, history, context, developer_trace, start,
                        reason=f"self_verification_failed: {sv.get('internal_reason')}", workflow=workflow,
                        message_override=(exec_result.get("result") or {}).get("message"))

            denial_text = (exec_result.get("result") or {}).get("message") or \
                "ขออภัยค่ะ ไม่สามารถยืนยันสิทธิ์ในการเข้าถึงข้อมูลรายการนี้ได้ในขณะนี้ รบกวนติดต่อเจ้าหน้าที่เพื่อยืนยันตัวตนก่อนนะคะ"
            reply = _build_response(text=denial_text)
            return self._finalize(reply=reply, routing_type=routing_type, workflow=workflow,
                                   developer_trace=developer_trace, context=context, start=start, alert=alert)

        # status == "success"
        result_payload = exec_result.get("result") or {}
        # Surface whatever confidence the executor itself already computed
        # (e.g. the RAG Executor's retrieval-score confidence) into the
        # trace instead of silently discarding it — generic across any
        # executor type that returns one, never RAG-specific.
        if isinstance(result_payload, dict) and result_payload.get("confidence") is not None:
            developer_trace["confidence"] = result_payload["confidence"]
        if routing_type == "RAG":
            # AI Policies' own escalation verdict (already computed inside
            # the shared pipeline, surfaced by _run_rag_pipeline above) —
            # the SAME signal the AI Playground already reflects. Checked
            # here, once, so no channel adapter has to re-derive its own,
            # second escalation decision — "no duplicate routing logic."
            if result_payload.get("policy_escalate"):
                reply = _build_response(text=result_payload.get("policy_escalation_message")
                                         or "กำลังโอนสายให้เจ้าหน้าที่ดูแลต่อค่ะ")
                return self._finalize(reply=reply, routing_type="HUMAN_HANDOFF", workflow=workflow,
                                       developer_trace=developer_trace, context=context, start=start, alert=alert,
                                       handoff_payload={"reason": "ai_policy_escalation",
                                                         "escalation_message": result_payload.get("policy_escalation_message")})

            answer = result_payload.get("answer") or ""
            if not answer.strip():
                # The shared RAG pipeline already ran (above) and produced
                # nothing — re-running it via _route_safe_fallback would
                # just repeat the identical, already-failed call. Build
                # the canned fallback directly instead.
                reply, _ = _safe_fallback_response("rag_no_grounded_answer")
                developer_trace["fallback_reason"] = "rag_no_grounded_answer"
                return self._finalize(reply=reply, routing_type="SAFE_FALLBACK", workflow=workflow,
                                       developer_trace=developer_trace, context=context, start=start, alert=alert)
            # Attachments — generic, channel-agnostic classification (image
            # vs. other file) read from each cited chunk's own already-
            # computed `attachments` field (services/rag_service.py, RAG
            # internals, untouched); respects the SAME admin-configured
            # Attachment Rules (services/policy_studio_service.py) every
            # channel is expected to honor, computed once here rather than
            # re-implemented per channel adapter.
            images, files = _extract_reply_attachments(result_payload.get("chunks") or [])
            reply = _build_response(text=answer, images=images, files=files)
            # Root Change 2 (Final Systemic Routing Fix, 2026-08-28) — a
            # turn the shared RAG pipeline itself answered via General
            # Chat Fallback (no company-KB grounding at all) is reported
            # as its own distinct route, never silently folded into "RAG".
            if result_payload.get("general_chat_used"):
                routing_type = "GENERAL"
        else:
            full_mapped = result_payload.get("mapped_fields")
            # Response-Derived Identifier Memory (2026-08-15, Issue 2) —
            # a successful list/search execution often names the record's
            # OWN identifier in its response (response_mapping rows tagged
            # field_metadata.identity_concept, config-driven, never a
            # hardcoded field name here). Captured into the SAME
            # collected_parameters dict input-bound slots already flow
            # through, so profiles/manager.py's existing persistence
            # remembers it exactly like a customer-supplied value would —
            # the platform LEARNS an identifier from what the ERP just
            # told it, not only from what the customer typed.
            if full_mapped:
                captured = _capture_response_derived_identifiers(selected.get("response_mapping"), full_mapped)
                if captured:
                    info_status = developer_trace.setdefault("information_collection_status", {})
                    info_status["collected_parameters"] = {
                        **(info_status.get("collected_parameters") or {}), **captured}
            mapped = full_mapped
            if mapped:
                mapped = select_requested_mapped_fields(message, mapped, selected.get("response_mapping"),
                                                          input_param_names=collected_slots.keys())
            agg_request = _detect_aggregation_request(message)
            agg_text = (self._aggregate_list_reply(full_mapped, selected.get("response_mapping"), agg_request, message)
                        if agg_request and full_mapped else None)
            text = agg_text or self._compose_natural_reply(
                mapped if mapped else result_payload, selected.get("response_mapping"), fallback_payload=full_mapped)
            reply = _build_response(text=text)

        return self._finalize(reply=reply, routing_type=routing_type, workflow=workflow,
                               developer_trace=developer_trace, context=context, start=start, alert=alert)

    @staticmethod
    def _summarize_action_result(payload) -> str:
        if isinstance(payload, dict) and payload:
            parts = []
            for k, v in list(payload.items())[:6]:
                if isinstance(v, dict):
                    continue
                if isinstance(v, list):
                    if v and isinstance(v[0], dict):
                        v = f"{len(v)} รายการ"
                    else:
                        v = ", ".join(str(item) for item in v) if v else None
                if v in (None, ""):
                    continue
                parts.append(f"{k}: {v}")
            if parts:
                return " / ".join(parts)
        return "ดำเนินการเรียบร้อยค่ะ"

    @staticmethod
    def _compose_natural_reply(payload, response_mapping: Optional[List[Dict]], *,
                                fallback_payload: Optional[Dict] = None) -> str:
        """ERP Response Composer (2026-08-15, Issue 5) — a customer-facing
        Thai sentence per fact instead of a raw Python/JSON dump or a
        single " / "-joined line. Config-driven (never a hardcoded field
        name): a response_mapping row's own field_metadata.keywords are
        checked for a currency-shaped concept (ยอดเงิน/ราคา/total/ค่าขนส่ง)
        to decide whether a numeric value gets "... บาทค่ะ" formatting;
        every other field falls back to "{label}: {value}". A raw list/
        dict value is skipped here — its OWN "latest record" scalar
        sibling rows (e.g. "เลขที่คำสั่งซื้อล่าสุด") already surface the
        same information in flattened, natural form; showing the raw
        array on top of them would just reintroduce the JSON dump this
        composer exists to remove. Falls back to the plain summarizer
        only if nothing at all was usable."""
        if not isinstance(payload, dict) or not payload:
            return "ดำเนินการเรียบร้อยค่ะ"
        rows = response_mapping or []
        metadata_by_label = {r.get("mapped_label"): (r.get("field_metadata") or {}) for r in rows}
        json_path_by_label = {r.get("mapped_label"): (r.get("json_path") or "") for r in rows}
        currency_keywords = ("ยอดเงิน", "ราคา", "ค่าขนส่ง", "total", "บาท")
        # Empty Requested Field fix (P1) — `payload` differs from
        # `fallback_payload` (a DIFFERENT dict object, not just "happens
        # to have the same keys") only when Requested-Field Filtering
        # (select_requested_mapped_fields) actually narrowed the reply
        # down to the specific field(s) the customer's own question named
        # — see that function's own "safe by construction" default, which
        # returns the SAME `mapped_fields` object unchanged whenever
        # nothing in the question matched anything. That object-identity
        # check is the generic signal (no field name, no config) for
        # "the customer specifically asked about this field" vs. "this is
        # an unfiltered, general profile dump" — reused here so an
        # honestly-empty field the customer specifically asked about is
        # answered honestly (below) instead of being silently dropped and
        # falling through to the fully-unfiltered fallback three empty
        # cases below (which then substituted unrelated fields the
        # customer never asked about). A general/broad question keeps
        # today's behavior unchanged: an empty field just isn't worth
        # mentioning among everything else that DOES have data.
        is_narrowed = fallback_payload is not None and payload is not fallback_payload
        lines = []
        unflattened_list_lines = []
        for label, value in payload.items():
            if value in (None, ""):
                if is_narrowed:
                    lines.append(f"ไม่พบข้อมูล{label}ค่ะ")
                continue
            if isinstance(value, dict):
                continue
            if isinstance(value, list):
                if value and isinstance(value[0], dict):
                    # A list of RECORDS (dicts) MAY be the raw nested
                    # structure a sibling "latest record" row (e.g.
                    # "เลขที่คำสั่งซื้อล่าสุด") already flattens elsewhere
                    # in this SAME payload — in which case showing the
                    # raw array too would just reintroduce the JSON dump
                    # this composer exists to remove, so it's skipped.
                    # But a list with no such sibling (e.g. GetDataCustomer's
                    # "คูปอง" — a real array with nothing else covering it)
                    # would otherwise vanish silently; that's reported as
                    # a labeled count using the field's own name instead.
                    this_path = json_path_by_label.get(label) or ""
                    has_sibling = this_path and any(
                        other_label != label and (other_path or "").startswith(this_path + ".")
                        for other_label, other_path in json_path_by_label.items())
                    if not has_sibling:
                        unflattened_list_lines.append(f"พบ{label} {len(value)} รายการค่ะ")
                    continue
                value = ", ".join(str(v) for v in value) if value else None
                if not value:
                    # An EMPTY list ("คูปอง": []) falls through to here —
                    # it is neither a non-empty list of records (handled
                    # above) nor a non-empty scalar list; without this,
                    # it silently vanished with NEITHER a labeled count
                    # NOR an honest "no data" line, which is exactly what
                    # let a narrowed, single-field reply fall through to
                    # the fully-unfiltered fallback below.
                    if is_narrowed:
                        lines.append(f"ไม่พบข้อมูล{label}ค่ะ")
                    continue
            keywords = [str(k).lower() for k in (metadata_by_label.get(label, {}).get("keywords") or [])]
            is_currency = isinstance(value, (int, float)) and any(
                ck in " ".join(keywords) or ck in str(label).lower() for ck in currency_keywords)
            if is_currency:
                lines.append(f"{label} {value:,.2f} บาทค่ะ")
            else:
                lines.append(f"{label}: {value}")
        if not lines:
            if unflattened_list_lines:
                return "\n".join(unflattened_list_lines)
            if fallback_payload and fallback_payload is not payload:
                # Requested-Field Filtering narrowed the reply down to a
                # field that turned out to be a redundant raw list (its
                # own flattening siblings exist but weren't part of this
                # narrowed set) — retry against the FULL, unfiltered
                # mapped_fields so the customer still gets the real,
                # composed answer instead of a bare list dump.
                return DecisionEngine._compose_natural_reply(fallback_payload, response_mapping)
            return DecisionEngine._summarize_action_result(payload)
        lines.extend(unflattened_list_lines)
        text = "\n".join(lines)
        if not text.rstrip().endswith(("ค่ะ", "คะ", "ครับ")):
            # A trailing ASCII value (URL, code, number) must never have
            # the politeness particle glued directly onto it -- e.g.
            # ".../682345678901ค่ะ" corrupts the URL and breaks LINE's
            # own link auto-detection. Thai text gets no separating space
            # (idiomatic: "เรียบร้อยค่ะ" not "เรียบร้อย ค่ะ").
            sep = " " if text and text[-1].isascii() and not text[-1].isspace() else ""
            text += sep + "ค่ะ"
        return text

    @staticmethod
    def _aggregate_list_reply(payload: Dict, response_mapping: Optional[List[Dict]],
                               agg_request: Dict, message: str = "") -> Optional[str]:
        """Latest-N Aggregation composer (Customer-Reported ERP
        Conversation Defects, 2026-08-17) — answers "5 อันล่าสุด...รวม
        เท่าไหร่"-style questions from the REAL records an ERP call
        already returned, never a second/fabricated lookup. Only ever
        returns a real answer when BOTH a genuine list-of-records field
        (payload's own raw array, e.g. SearchDataOrderList's "$.data")
        AND a currency-shaped sibling field marking which per-item key
        to sum are actually present — both discovered generically from
        the action's OWN response_mapping (field_metadata.keywords /
        identity_concept), never a hardcoded field/action name. Returns
        None (never a fabricated number) when the action's response
        shape doesn't support the question — the caller falls through to
        the normal _compose_natural_reply unaffected."""
        if not isinstance(payload, dict):
            return None
        rows = response_mapping or []
        json_path_by_label = {r.get("mapped_label"): (r.get("json_path") or "") for r in rows}
        metadata_by_label = {r.get("mapped_label"): (r.get("field_metadata") or {}) for r in rows}
        currency_keywords = ("ยอดเงิน", "ราคา", "ค่าขนส่ง", "total", "บาท", "ยอดรวม")
        outstanding_keywords = ("ค้างจ่าย", "ค้างชำระ", "คงค้าง", "unpaid", "outstanding")

        for label, value in payload.items():
            if not (isinstance(value, list) and value and isinstance(value[0], dict)):
                continue
            list_path = json_path_by_label.get(label) or ""
            if not list_path:
                continue

            def _sibling_key(predicate) -> Optional[str]:
                for other_label, other_path in json_path_by_label.items():
                    if other_label == label or not (other_path or "").startswith(list_path + "."):
                        continue
                    if predicate(other_label):
                        return other_path.rsplit(".", 1)[-1]
                return None

            def _detect_filter_value() -> "Tuple[Optional[str], Optional[str]]":
                """Generic status/attribute filter (2026-08-24) — the
                customer's own phrase is only ever matched against
                filter_values maps the admin curated on a sibling
                field's field_metadata (the SAME config surface as
                identity_concept/currency keywords); the ERP value
                returned is always the map's value, never the
                customer's own phrase, so "รับเข้าไทย" never becomes a
                made-up filter — it resolves to the real ERP status
                string an admin explicitly configured (e.g. "รับเข้าที่
                ไทย"), and a China-side status string is never treated
                as if it meant the same thing."""
                for other_label, other_path in json_path_by_label.items():
                    if other_label == label or not (other_path or "").startswith(list_path + "."):
                        continue
                    filter_values = (metadata_by_label.get(other_label, {}) or {}).get("filter_values") or {}
                    if not isinstance(filter_values, dict):
                        continue
                    for phrase, erp_value in filter_values.items():
                        if phrase and str(phrase).lower() in message.lower():
                            return other_path.rsplit(".", 1)[-1], erp_value
                return None, None

            amount_key = _sibling_key(lambda lbl: any(
                ck in " ".join(str(k).lower() for k in (metadata_by_label.get(lbl, {}).get("keywords") or []))
                or ck in str(lbl).lower() for ck in currency_keywords))
            if not amount_key:
                continue

            # Generic status/attribute filter (Shipment Filter/Count/Sum
            # Aggregation fix, 2026-08-24) — confirmed live: "SP1008 มีบิล
            # ที่รับเข้าไทยกี่บิล..." always answered from the FULL,
            # unfiltered record set (or just the single latest record),
            # because nothing here could narrow `records` by anything
            # other than a positional "first N" limit. filter_values is
            # an admin-curated {customer phrase: real ERP value} map on
            # any sibling field (e.g. the Status row) — the SAME
            # established config surface as identity_concept/currency
            # keywords above; code never hardcodes a status string, and
            # the filter VALUE is always the ERP's own real value, never
            # invented.
            filter_key, filter_value = _detect_filter_value()
            base_records = [r for r in value if r.get(filter_key) == filter_value] if filter_key and filter_value else value
            filter_desc = f"ที่อยู่ในสถานะ{filter_value}" if filter_value else ""

            # Outstanding/unpaid (2026-08-24) — NEVER inferred from an
            # unrelated field; only ever answered from a real ERP field
            # explicitly tagged with an outstanding/unpaid-shaped
            # keyword, exactly like amount_key above. When the customer
            # asked about it and no such field is configured (confirmed
            # live: searchdatashipmentlist's real ERP response has no
            # such field at all), an honest limitation is stated — the
            # shipping total itself, if askable, is still given for
            # context, never silently withheld alongside the disclaimer.
            outstanding_note = None
            if agg_request.get("wants_outstanding"):
                outstanding_key = _sibling_key(lambda lbl: any(
                    ck in " ".join(str(k).lower() for k in (metadata_by_label.get(lbl, {}).get("keywords") or []))
                    or ck in str(lbl).lower() for ck in outstanding_keywords))
                if not outstanding_key:
                    outstanding_note = ("ระบบยังไม่มีข้อมูลว่ายอดใดค้างชำระอยู่ค่ะ "
                                         "แต่สามารถตรวจสอบยอดค่าขนส่งรวมที่มีอยู่ในระบบให้ได้ค่ะ")

            limit = agg_request.get("limit")
            records = base_records[:limit] if limit else base_records
            actual_count = len(records)
            if actual_count == 0:
                if filter_value:
                    text = f"ไม่พบรายการ{filter_desc}ค่ะ"
                    return f"{text} {outstanding_note}" if outstanding_note else text
                continue
            # A count-only question ("มีกี่บิล") never needs a real
            # per-item amount to answer, so it is not gated on
            # `amounts` the way a sum is — this is what let messages 2
            # and 3 (no requested sum) fall through un-answered before.
            if agg_request.get("wants_sum"):
                amounts = [r.get(amount_key) for r in records if isinstance(r.get(amount_key), (int, float))]
                if not amounts:
                    continue
                total = sum(amounts)
            else:
                total = None

            if agg_request.get("wants_sum") or agg_request.get("wants_count"):
                if agg_request.get("wants_sum"):
                    if limit and actual_count < limit:
                        text = (f"พบข้อมูลจริงเพียง {actual_count} รายการ{filter_desc} (จากที่ขอ {limit} รายการล่าสุด) "
                                f"มียอดรวม {total:,.2f} บาทค่ะ")
                    elif filter_desc:
                        text = f"พบทั้งหมด {actual_count} รายการ{filter_desc}ค่ะ ยอดรวม {total:,.2f} บาทค่ะ"
                    else:
                        text = f"{actual_count} รายการล่าสุด มียอดรวมทั้งหมด {total:,.2f} บาทค่ะ"
                else:
                    text = f"พบทั้งหมด {actual_count} รายการ{filter_desc}ค่ะ"
                return f"{text} {outstanding_note}" if outstanding_note else text

            # limit-only (no explicit sum/count word) — a short, real,
            # per-record summary, never the raw list dump.
            code_key = _sibling_key(lambda lbl: (metadata_by_label.get(lbl, {}) or {}).get("identity_concept"))
            lines = [f"{actual_count} รายการล่าสุด{filter_desc}ค่ะ"]
            for r in records:
                code = r.get(code_key) if code_key else None
                amt = r.get(amount_key)
                piece = str(code) if code else ""
                if isinstance(amt, (int, float)):
                    piece = f"{piece} ({amt:,.2f} บาท)" if piece else f"{amt:,.2f} บาท"
                if piece:
                    lines.append(f"- {piece}")
            text = "\n".join(lines)
            return f"{text}\n{outstanding_note}" if outstanding_note else text
        return None

    # ── Human Handoff ──────────────────────────────────────────────────────

    def _route_human_handoff(self, message: str, history: List[Dict], context: Dict, developer_trace: Dict,
                              start: float, *, reason: str, workflow: Optional[str],
                              message_override: Optional[str] = None) -> Dict:
        candidates = search_candidate_actions(self.registry, workflow=workflow, message=message,
                                               action_types=["HUMAN_HANDOFF"])
        selected = select_best_action(candidates, minimum_score=0.0)
        alert = _detect_alert(message, context)
        developer_trace["candidate_business_actions"] = [
            {"action_key": c.get("action_key"), "score": c.get("_score")} for c in candidates[:5]
        ]

        if selected:
            exec_context = {
                "question": message, "intent": reason, "workflow": workflow,
                "collected_slots": {}, "conversation_context": context.get("conversation_context") or {},
                "customer_context": context.get("customer_context") or {}, "current_user": context.get("current_user"),
                "developer_mode": bool(context.get("developer_mode")),
                "channel": context.get("channel"),
                "tenant_id": context.get("tenant_id"), "external_user_id": context.get("external_user_id"),
            }
            exec_result = self.executor.execute(selected["id"], exec_context)
            developer_trace["execution_result"] = exec_result
            developer_trace["selected_business_action"] = selected.get("action_key")
            handoff_payload = (exec_result.get("result") or {}).get("handoff_payload")
        else:
            handoff_payload = {"reason": reason, "workflow": workflow, "collected_slots": {}}

        # Natural, non-technical wording for an explicit customer request
        # (Human Handoff sprint, 2026-08-13, Phase 4) — never an awkward
        # "ยืนยันการเรียก SendLineNotiCS หรือไม่" confirmation prompt. Any
        # other reason (AI Policy escalation, refusal, max-retry) keeps
        # its own existing message_override / generic fallback text,
        # unchanged from before this sprint.
        if message_override:
            reply_text = message_override
        elif reason == "user_requested_human":
            reply_text = "ได้เลยค่ะ เดี๋ยวแจ้งเจ้าหน้าที่ให้ติดต่อกลับนะคะ"
        else:
            reply_text = "กำลังโอนสายให้เจ้าหน้าที่ดูแลต่อค่ะ"
        reply = _build_response(text=reply_text)
        return self._finalize(reply=reply, routing_type="HUMAN_HANDOFF", workflow=workflow,
                               developer_trace=developer_trace, context=context, start=start,
                               alert=alert, handoff_payload=handoff_payload)

    # ── Safe Fallback ──────────────────────────────────────────────────────

    def _route_safe_fallback(self, message: str, history: List[Dict], context: Dict, developer_trace: Dict,
                              start: float, *, reason: str) -> Dict:
        # Production Integration Sprint (2026-08-02), Phase 1 Step C — the
        # true "Safe Fallback" tries the ONE shared production RAG
        # pipeline directly (no more searching for a specific RAG-type
        # Business Action row, no more separate raw rag_service call —
        # run_playground_turn() needs neither) before giving up with the
        # canned fallback message.
        alert = _detect_alert(message, context)
        exec_result, _ = self._run_rag_pipeline(message, history, context)
        developer_trace["execution_result"] = exec_result
        result_payload = exec_result.get("result") or {}
        answer_text = result_payload.get("answer") if exec_result.get("status") == "success" else None
        # Same confidence-surfacing fix as _execute_selected_action's RAG
        # branch — the pipeline already computed it, this path used to
        # silently discard it here too.
        if result_payload.get("confidence") is not None:
            developer_trace["confidence"] = result_payload["confidence"]

        if answer_text and answer_text.strip():
            reply = _build_response(text=answer_text)
            # Root Change 2 (Final Systemic Routing Fix, 2026-08-28) —
            # same distinction as _execute_selected_action's RAG branch:
            # a General-Chat-Fallback answer (no company-KB grounding) is
            # reported as its own route, never silently folded into "RAG".
            safe_fallback_routing_type = "GENERAL" if result_payload.get("general_chat_used") else "RAG"
            return self._finalize(reply=reply, routing_type=safe_fallback_routing_type, workflow=None,
                                   developer_trace=developer_trace, context=context, start=start, alert=alert)

        reply, _ = _safe_fallback_response(reason)
        developer_trace["fallback_reason"] = reason
        return self._finalize(reply=reply, routing_type="SAFE_FALLBACK", workflow=None,
                               developer_trace=developer_trace, context=context, start=start, alert=alert)

    # ── Production RAG Pipeline (Production Integration Sprint, 2026-08-02) ──

    def _run_rag_pipeline(self, message: str, history: List[Dict], context: Dict) -> "tuple[Dict, float]":
        """The ONE production RAG execution path. Wraps services/
        playground_orchestrator.py::run_playground_turn() — the exact
        same full pipeline (Prompt Builder -> AI Policies -> LLM ->
        Grounding -> Citations) the AI Playground already uses — never a
        second implementation. services/action_executor.py's own
        `_execute_rag` (thinner: retrieval + raw context concatenation,
        no LLM) remains defined for any OTHER caller, but the Decision
        Engine itself no longer uses it for its own RAG execution.

        Returns an (exec_result, latency_ms) pair shaped exactly like
        ActionExecutor.execute()'s own contract
        (status/result/metadata/latency_ms/error/logs), so every existing
        caller in this module keeps working against the same shape
        regardless of which execution path produced it.

        `context.get("channel")`, if present, resolves that channel's own
        assigned Prompt Studio template (services/prompt_builder.py::
        get_active_prompt_for_channel) — generic and configuration-driven,
        never a hardcoded channel name; falls back to the global default
        template (identical to the AI Playground's own behavior) when no
        channel is given."""
        from services.playground_orchestrator import run_playground_turn
        from services.prompt_builder import get_active_prompt

        # Phase 3.4 (2026-08-05) — Customer Tier Prompt takes priority over
        # the channel's own assignment (services/prompt_builder.py::
        # get_active_prompt's own chain); customer_context.conversation_tier
        # is read-only here, set by services/customer_tier_service.py after
        # each turn — the customer/channel adapter never picks a prompt
        # directly (no manual per-conversation override, per spec).
        template_id = None
        channel = context.get("channel")
        tier = (context.get("customer_context") or {}).get("conversation_tier")
        if channel or tier:
            try:
                template_id = get_active_prompt(channel=channel, tier=tier).id
            except Exception:
                template_id = None

        pg_history = [{"role": t.get("role"), "content": t.get("content")} for t in (history or [])]
        t0 = time.time()
        try:
            result = run_playground_turn(message, template_id=template_id, history=pg_history)
            latency_ms = round((time.time() - t0) * 1000, 2)
            cited = [c.get("citation") for c in (result.chunks or []) if c.get("cited") and c.get("citation")]
            exec_result = {
                "status": "success",
                "result": {
                    "answer": result.answer, "citations": cited, "confidence": result.confidence,
                    "chunk_count": len(result.chunks or []),
                    # Phase 3 Conversation History (2026-08-05) — already
                    # computed by run_playground_turn(), just not
                    # previously surfaced here; needed so a persisted
                    # Conversation Turn can record real token counts
                    # instead of leaving them blank.
                    "input_tokens": result.input_tokens, "output_tokens": result.output_tokens,
                    # Raw chunks (already computed by the frozen RAG
                    # pipeline, including each chunk's own `attachments`)
                    # — surfaced so a channel adapter (e.g. line_bot/
                    # webhook.py) can render images/file-links without
                    # Decision Engine re-deriving retrieval data itself.
                    "chunks": result.chunks,
                    # AI Policies' own escalation verdict (services/
                    # policy_engine.py::evaluate, already computed inside
                    # run_playground_turn — never re-evaluated here) —
                    # exposed so callers escalate on the SAME signal the
                    # AI Playground already reflects, instead of a second,
                    # independently-derived escalation check.
                    "policy_escalate": result.policy.escalate,
                    "policy_escalation_message": result.policy.escalation_message,
                    # Prompt Studio / AI Policies identity — already
                    # resolved inside run_playground_turn (services/
                    # prompt_builder.py::get_template /
                    # services/policy_studio_service.py), surfaced here
                    # (not re-derived) so Developer Mode can show exactly
                    # which prompt version/template and which policy set
                    # produced this answer, in production the same way
                    # the AI Playground already displays them.
                    "prompt_template_id": result.prompt.template.id,
                    "prompt_template_name": result.prompt.template.name,
                    "prompt_template_version": result.prompt.template.version,
                    "policy_set_name": result.policy_set_name,
                    # Root Change 2 (Final Systemic Routing Fix,
                    # 2026-08-28) — surfaced (not re-derived) so callers
                    # can report routing_type="GENERAL" instead of "RAG"
                    # for a turn the shared pipeline itself answered with
                    # no company-KB grounding at all.
                    "general_chat_used": result.general_chat_used,
                },
                "metadata": {"model": result.model, "confidence_label": result.confidence_label},
                "latency_ms": latency_ms, "error": None, "logs": [],
            }
            return exec_result, latency_ms
        except Exception as e:
            latency_ms = round((time.time() - t0) * 1000, 2)
            return {"status": "error", "result": {}, "metadata": {}, "latency_ms": latency_ms,
                    "error": sanitize_for_preview(str(e)), "logs": []}, latency_ms

    # ── Hybrid Routing (Production Integration Sprint, 2026-08-02, Phase 1 Step C) ──

    def _handle_hybrid_turn(self, classification: Dict, message: str, history: List[Dict], context: Dict,
                             developer_trace: Dict, start: float, *, workflow: Optional[str]) -> Dict:
        """Runs the ERP sub-question once and the RAG sub-question once
        (services/hybrid_question_classifier.py already segmented them —
        never re-segmented here), then combines them via services/
        hybrid_runtime_service.py::synthesize_hybrid_answer() — the SAME
        classifier and synthesis the AI Playground's Hybrid mode already
        uses. ERP execution reuses the existing, unchanged parameter-
        binding machinery (`_bind_all_from_message`) and the Generic
        Action Executor; RAG execution reuses `_run_rag_pipeline` above.
        Never a duplicate implementation of either."""
        alert = _detect_alert(message, context)
        action_id = classification.get("selected_action_id")
        erp_sub_question = classification.get("erp_sub_question") or message
        rag_sub_question = classification.get("rag_sub_question") or message

        full_action = self.registry.get_full(action_id, mask_secrets=False) if action_id else None
        erp_answer = None
        erp_error = None
        if full_action:
            bind_result = _bind_all_from_message(full_action, self.registry, {}, erp_sub_question)
            collected = bind_result["collected"]
            ambiguous = bind_result["ambiguous_candidates"]
            validation = self.registry.validate_can_execute(action_id, collected)
            next_after = None if validation["ok"] else _next_expected_parameter(full_action, self.registry, collected)
            if _is_execution_ready(validation, next_after, ambiguous) and _requires_confirmation(full_action) \
                    and not context.get("confirmed"):
                # Same confirmation gate as _execute_selected_action, applied
                # here too so a COMMAND-type action can never be reached via
                # the Hybrid sub-question path either.
                erp_error = "awaiting_confirmation — this action requires explicit confirmation before execution"
            elif _is_execution_ready(validation, next_after, ambiguous):
                exec_context = {
                    "question": erp_sub_question, "collected_slots": collected, "workflow": workflow,
                    "intent": classification["classification"],
                    "conversation_context": context.get("conversation_context") or {},
                    "customer_context": context.get("customer_context") or {},
                    "current_user": context.get("current_user"), "developer_mode": bool(context.get("developer_mode")),
                    "system_values": _extract_system_values(erp_sub_question, history=history),
                    "channel": context.get("channel"),
                    "tenant_id": context.get("tenant_id"), "external_user_id": context.get("external_user_id"),
                }
                try:
                    erp_exec_result = self.executor.execute(action_id, exec_context)
                except Exception as e:
                    erp_exec_result = {"status": "error", "error": sanitize_for_preview(str(e))}
                developer_trace["erp_execution_result"] = erp_exec_result
                if erp_exec_result.get("status") == "success":
                    full_mapped = (erp_exec_result.get("result") or {}).get("mapped_fields")
                    mapped = full_mapped
                    if mapped:
                        mapped = select_requested_mapped_fields(erp_sub_question, mapped, full_action.get("response_mapping"),
                                                                  input_param_names=collected.keys())
                    agg_request = _detect_aggregation_request(erp_sub_question)
                    agg_answer = (self._aggregate_list_reply(full_mapped, full_action.get("response_mapping"), agg_request, erp_sub_question)
                                  if agg_request and full_mapped else None)
                    erp_answer = agg_answer or self._compose_natural_reply(
                        mapped if mapped else erp_exec_result.get("result"), full_action.get("response_mapping"),
                        fallback_payload=full_mapped)
                elif erp_exec_result.get("status") == "denied":
                    # Authorization Gate (Task 06) — never fall through to
                    # the generic "unknown ERP error" wording for a denial;
                    # surface the neutral denial message as the ERP section
                    # directly, never a fabricated/partial ERP answer.
                    erp_answer = (erp_exec_result.get("result") or {}).get("message") or \
                        "ขออภัยค่ะ ไม่สามารถยืนยันสิทธิ์ในการเข้าถึงข้อมูลรายการนี้ได้ในขณะนี้ รบกวนติดต่อเจ้าหน้าที่เพื่อยืนยันตัวตนก่อนนะคะ"
                else:
                    erp_error = erp_exec_result.get("error") or "unknown ERP error"
            elif ambiguous:
                erp_error = "ambiguous identifier value — could not determine which one to use"
            else:
                # Rare — classify_question() already confirmed a bindable
                # parameter value exists before classifying HYBRID at all;
                # the Registry's own group-rule evaluation is the final
                # authority and could still disagree at the margins.
                # Honest unavailability, never a fabricated ERP answer.
                erp_error = "required parameter(s) missing"
        else:
            erp_error = "selected Business Action not found"

        rag_exec_result, _ = self._run_rag_pipeline(rag_sub_question, history, context)
        developer_trace["rag_execution_result"] = rag_exec_result
        rag_answer = rag_error = None
        rag_citations: List[str] = []
        if rag_exec_result.get("status") == "success":
            rag_answer = (rag_exec_result.get("result") or {}).get("answer")
            rag_citations = (rag_exec_result.get("result") or {}).get("citations") or []
        else:
            rag_error = rag_exec_result.get("error") or "unknown RAG error"

        synthesis = synthesize_hybrid_answer(erp_answer=erp_answer, erp_error=erp_error,
                                              rag_answer=rag_answer, rag_error=rag_error,
                                              rag_citations=rag_citations)
        developer_trace["merge_strategy"] = synthesis["strategy"]
        developer_trace["selected_business_action"] = full_action.get("action_key") if full_action else None
        # Internal architecture labels ("ERP"/"Knowledge Base") and RAG
        # source citations are developer-only context — never shown to
        # the customer (see synthesize_hybrid_answer's own docstring).
        developer_trace["hybrid_labeled_answer"] = synthesis["labeled_answer"]
        developer_trace["hybrid_citations"] = synthesis["citations"]

        reply = _build_response(text=synthesis["merged_answer"])
        return self._finalize(reply=reply, routing_type="HYBRID", workflow=workflow,
                               developer_trace=developer_trace, context=context, start=start, alert=alert)

    def _route_clarification(self, classification: Dict, message: str, context: Dict,
                              developer_trace: Dict, start: float, *, workflow: Optional[str]) -> Dict:
        """Two distinct CLARIFICATION_REQUIRED causes share this
        classification value (services/hybrid_question_classifier.py) —
        distinguished by whether any Business Action was even a candidate
        (`candidate_action_ids`), never by re-deriving the reason here:
        (1) 2+ Business Actions matched with comparably strong evidence —
        genuinely ambiguous which one the customer means.
        (2) Wrong-Intent Prevention fix (Task 03, 2026-08-25) — the
        message expresses interest/intent but asks no concrete question
        and matched no Business Action at all (candidate_action_ids is
        empty); answering via RAG here risks a confident but unrelated
        answer (confirmed live: "สนใจนำเข้าสินค้าครับ" retrieved the
        closest-embedding KB chunk, "prohibited goods", despite asking
        nothing about restrictions). No execution/RAG call either way —
        asks the customer for the minimum detail needed, same as the AI
        Playground's Auto mode does for this classification."""
        alert = _detect_alert(message, context)
        if classification.get("candidate_action_ids"):
            text = "พบบริการที่ตรงกับคำถามมากกว่าหนึ่งรายการค่ะ รบกวนระบุให้ชัดเจนขึ้นอีกนิดนะคะ"
        else:
            text = "รบกวนขอรายละเอียดเพิ่มเติมสักนิดนะคะ ว่าสนใจเรื่องอะไรเป็นพิเศษ จะได้ช่วยตอบได้ตรงจุดค่ะ"
        reply = _build_response(text=text)
        return self._finalize(reply=reply, routing_type="WORKFLOW", workflow=workflow,
                               developer_trace=developer_trace, context=context, start=start, alert=alert)

    # ── Response Builder ──────────────────────────────────────────────────

    def _finalize(self, *, reply: Dict, routing_type: str, workflow: Optional[str], developer_trace: Dict,
                  context: Dict, start: float, candidates=None, selected_action=None, alert: Optional[Dict] = None,
                  handoff_payload: Optional[Dict] = None, error: Optional[str] = None) -> Dict:
        response = {
            "reply": reply,
            "routing": {"type": routing_type if routing_type in _ROUTING_TYPES else "SAFE_FALLBACK"},
            "workflow": workflow,
            "alert": alert,
            "handoff_payload": handoff_payload,
            "error": error,
        }
        if bool(context.get("developer_mode")):
            developer_trace.setdefault("confidence", None)
            response["developer"] = {**developer_trace, "latency_ms": round((time.time() - start) * 1000, 2)}
        return response


def get_decision_engine(sb=None) -> DecisionEngine:
    return DecisionEngine(sb)
