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
from services.hybrid_question_classifier import classify_question
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
    exactly as before."""
    count = 0
    for i, t in enumerate(history):
        if t.get("role") != "assistant" or (t.get("content") or "").strip() != expected_question:
            continue
        if i + 1 < len(history) and history[i + 1].get("role") == "user":
            reply = history[i + 1].get("content") or ""
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


def _replay_business_action_collection(action: Dict, registry, history: List[Dict]) -> Dict[str, str]:
    """Reconstructs 'Collected Parameters' purely from `history` — no
    separate persistence table, same convention every other
    conversation-intelligence module in this codebase already follows.
    Walks turns in order; whenever an assistant turn's text matches
    EXACTLY the question this engine would have generated for whatever
    parameter was next-expected at that point, the following user turn
    is bound as that parameter's answer. This generalizes the legacy
    engine's own "does the prior assistant turn match my follow-up
    question" continuation trick to an arbitrary number of dynamically-
    ordered parameters instead of one fixed schema question."""
    collected: Dict[str, str] = {}
    for i, turn in enumerate(history):
        if turn.get("role") != "user":
            continue
        if i == 0:
            # The very first user turn in the conversation is always
            # processed fresh — nothing precedes it to match a question
            # against — mirroring exactly how _handle_dynamic_collection
            # binds a brand-new conversation's first message. Without
            # this, a first message that fully satisfies every parameter
            # in one shot (e.g. SendLineNotiCS's free-text Message) would
            # replay as if NOTHING had been collected, breaking
            # continuation detection for whatever question comes next.
            result = _bind_all_from_message(action, registry, collected, turn.get("content") or "")
            collected = result["collected"]
            continue
        preceding = history[i - 1]
        if preceding.get("role") != "assistant":
            continue
        next_param = _next_expected_parameter(action, registry, collected)
        if not next_param:
            continue
        if (preceding.get("content") or "").strip() != _generate_parameter_question(next_param):
            continue
        result = _bind_all_from_message(action, registry, collected, turn.get("content") or "")
        collected = result["collected"]
    return collected


def _resolve_continuation_action(registry, history: List[Dict], workflow_hint: Optional[str] = None) -> Optional[Dict]:
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
    last_assistant = next((t for t in reversed(history) if t.get("role") == "assistant"), None)
    if not last_assistant:
        return None
    last_text = (last_assistant.get("content") or "").strip()
    if not last_text:
        return None
    try:
        candidates = registry.enabled_actions()
    except Exception:
        return None
    matches = []
    for action in candidates:
        if action.get("action_type") not in ("API", "WEBHOOK"):
            continue
        # `candidates` are bare rows from enabled_actions() (no joined
        # parameters table) — must fetch the full record before knowing
        # whether this action actually has parameter metadata.
        full_action = registry.get_full(action["id"], mask_secrets=False)
        if not (full_action.get("parameters") or full_action.get("parameter_groups")):
            continue
        collected_so_far = _replay_business_action_collection(full_action, registry, history[:-1])
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
            if last_text == expected_q or last_text.endswith("\n\n" + expected_q):
                matches.append(full_action)
        elif (next_param is None and _requires_confirmation(full_action)
              and _generate_confirmation_question(full_action, collected_so_far) == last_text):
            # The last assistant turn was THIS action's confirmation-gate
            # question (2026-08-09, SendLineNotiCS enablement) — the
            # customer's reply this turn (e.g. "ยืนยัน"/"ยกเลิก") is an
            # answer to it, not a fresh, independently-routable message.
            matches.append(full_action)

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
    collected_counts = [(a, len(_replay_business_action_collection(a, registry, history[:-1]))) for a in matches]
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
_ROUTING_TYPES = ("RAG", "API", "TOOL", "WORKFLOW", "NOTIFICATION", "HUMAN_HANDOFF", "WEBHOOK", "SAFE_FALLBACK", "HYBRID")

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


def search_candidate_actions(registry, *, workflow: Optional[str], message: str,
                              collected_slots: Optional[Dict] = None,
                              action_types: Optional[List[str]] = None) -> List[Dict]:
    """Business Action Search — returns enabled candidates the Decision
    Engine may choose from, each annotated with a `_score`/`_reasons`.
    Considers: category (workflow match), keyword/example/ai_description
    overlap, parameter identifier-pattern evidence, parameter availability,
    priority, and (currently inert) future semantic/embedding scores.
    Never filters by a hardcoded action_key — every action, current or
    future, competes on the same generic signals."""
    try:
        candidates = registry.enabled_actions()
    except Exception:
        return []

    if action_types:
        candidates = [a for a in candidates if a.get("action_type") in action_types]

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
        if param_score:
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
            continuation_action = _resolve_continuation_action(self.registry, history, workflow_hint)

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
                # Hybrid Question Classifier (2026-08-02 Production
                # Integration Sprint, Phase 1 Step C) — only consulted on
                # a FRESH turn (a continuation in progress always takes
                # precedence, unchanged). Its role here is narrow: detect
                # a genuinely Hybrid or ambiguous question. For every
                # other classification (RAG_ONLY/ERP_ONLY/UNKNOWN), this
                # result is NOT used to select an action — the existing,
                # unchanged search below still does that; no duplicate
                # routing decision is made.
                classification = classify_question(message, self.registry)
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
                        self.registry, workflow=workflow_hint, message=message, collected_slots={})
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
                                                            collected_slots=identifier_memory_slots)
                    selected = select_best_action(candidates, minimum_score=1.0 if not workflow_hint else 0.5)
                    developer_trace["selection_source"] = "fresh_search"

                    if not selected and referenced:
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

        collected = _replay_business_action_collection(full_action, self.registry, history)
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
        # bound — an explicit, fresher value always wins.
        customer_context = context.get("customer_context") or {}
        if customer_context:
            askable = _askable_parameters_by_name(full_action)
            for profile_field, param_name in IDENTIFIER_MEMORY_FIELDS:
                if param_name in collected or param_name not in askable:
                    continue
                remembered = customer_context.get(profile_field)
                if remembered:
                    collected[param_name] = remembered

        validation = self.registry.validate_can_execute(action_id, collected)
        next_after = None if validation["ok"] else _next_expected_parameter(full_action, self.registry, collected)
        is_complete = _is_execution_ready(validation, next_after, ambiguous_candidates)

        collection_status = {
            "source": "business_action_registry",
            "selected_business_action": full_action.get("action_key"),
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
        lines = []
        unflattened_list_lines = []
        for label, value in payload.items():
            if value in (None, ""):
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
            return self._finalize(reply=reply, routing_type="RAG", workflow=None,
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
        """2+ Business Actions matched the message with comparably strong
        evidence (services/hybrid_question_classifier.py) — genuinely
        ambiguous which one the customer means. No execution of either
        candidate; asks the customer to be more specific, same as the AI
        Playground's Auto mode does for this classification."""
        alert = _detect_alert(message, context)
        reply = _build_response(text="พบบริการที่ตรงกับคำถามมากกว่าหนึ่งรายการค่ะ รบกวนระบุให้ชัดเจนขึ้นอีกนิดนะคะ")
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
