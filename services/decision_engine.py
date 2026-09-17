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
    _ACCOUNT_SCOPED_MEMORY_FIELDS,
    select_requested_mapped_fields,
    resolve_subsumed_keyword_tie,
)
# Hybrid Question Classifier (2026-08-02 Production Integration Sprint,
# Phase 1 Step C) — the SAME classifier the AI Playground's Auto mode
# already uses (services/hybrid_question_classifier.py), never a second
# implementation. Safe to import here (no cycle) because that module now
# depends only on services/action_selection_primitives.py, not on this
# module.
from services.hybrid_question_classifier import (
    classify_question, _QUESTION_MARKER_RE, _REQUEST_MARKER_RE, _split_clauses, _GREETING_RE,
)
# SEM-GEN-1 — conversational semantic backbone (frame derivation is
# deterministic; a single gated LLM call resolves open-vocabulary
# follow-up meaning only — see services/conversation_semantics.py).
from services.conversation_semantics import (
    derive_active_frame as _derive_active_frame,
    is_frame_followup as _is_frame_followup,
    resolve_followup as _resolve_frame_followup,
    resolve_frame_correction as _resolve_frame_correction,
    frame_ack_reply as _frame_ack_reply,
    interpret as _interpret_message,
    PUBLIC_INFO_FAMILIES as _PUBLIC_INFO_FAMILIES,
    _compose as _compose_intent_family,
    _HIGH_RISK_PRODUCT_RE as _HIGH_RISK_PRODUCT_RE,
    _PRICE_Q_RE as _PRICE_Q_RE,
    is_new_journey_opener as _is_new_journey_opener,
)
# PHASE-6B — pre-RAG conversational / service-intent layer (help /
# service-discovery / money-transfer / website-link / public-contact /
# warehouse-inbound journey), plus the repeated-answer loop guard. KB
# retrieval no longer owns whether one of these journeys can continue.
from services.service_intent_flow import (
    SERVICE_INTENT_FAMILIES as _SERVICE_INTENT_FAMILIES,
    reply_for_family as _service_intent_reply,
    is_repeat_of_last_assistant as _is_repeat_of_last_assistant,
    is_help_affirmation as _is_help_affirmation,
    CLARIFY_REPLY as _SERVICE_CLARIFY_REPLY,
    HELP_REPLY as _SERVICE_HELP_REPLY,
)
# CUSTOMER-RAG-2.1 — charter-truck (เหมารถ / TC19) multi-turn slot
# collection (deterministic, history-derived — no LLM, no pending table).
from services.charter_truck_flow import (
    derive_charter_state as _derive_charter_state,
    extract_charter_fields as _extract_charter_fields,
    charter_missing_prompt as _charter_missing_prompt,
    charter_handoff_summary as _charter_handoff_summary,
)
# CUSTOMER-ACTION-1 — operational change / verify requests that have NO
# executable Business Action: acknowledge -> collect the one identifier
# -> Human CS. Same deterministic history-derived pattern.
from services.operational_change_flow import (
    derive_operational_state as _derive_operational_state,
    extract_operational_fields as _extract_operational_fields,
    operational_ask_prompt as _operational_ask_prompt,
    operational_handoff_summary as _operational_handoff_summary,
    operational_handoff_reply as _operational_handoff_reply,
    OPERATIONAL_HANDOFF_REPLY as _OPERATIONAL_HANDOFF_REPLY,
)
# CUSTOMER-CALC-1 — shipping-cost estimate multi-turn slot collection
# (deterministic, history-derived; reuses the dimension/weight parser).
from services.shipping_estimate_flow import (
    derive_estimate_state as _derive_estimate_state,
    extract_estimate_fields as _extract_estimate_fields,
    estimate_missing_prompt as _estimate_missing_prompt,
    estimate_reply as _estimate_reply,
    asks_rate_basis as _asks_rate_basis,
    estimate_turn_answers as _estimate_turn_answers,
    EstimateState as _EstimateState,
)
# Hybrid Runtime Service (2026-08-02 Production Integration Sprint, Phase
# 1 Step B/C) — the SAME synthesis function the AI Playground's Hybrid
# mode already uses (services/hybrid_runtime_service.py); this module
# never imports services/hybrid_playground_router.py (Playground-only).
from services.hybrid_runtime_service import synthesize_hybrid_answer

# CUSTOMER-LINK-1 — Product Link Conversion (1688/Taobao/Tmall). Owns
# ONLY deterministic URL/domain validation and conversion-result truth
# typing; intent recognition stays in conversation_semantics.interpret().
from services.link_conversion_flow import (
    classify_link_request as _classify_link_request,
    classify_conversion_result as _classify_conversion_result,
    reply_for_state as _link_reply_for_state,
    reply_for_success as _link_reply_for_success,
    is_link_conversion_signal as _is_link_conversion_signal,
    normalize_supported_url as _link_normalize_url,
    is_1688_short_url as _link_is_1688_short_url,
    resolve_1688_short_url as _link_resolve_1688_short_url,
    classify_platform as _link_classify_platform,
    GUEST_CUSTCODE as _LINK_GUEST_CUSTCODE,
)

# CUSTOMER-RED-HOTFIX-2 — Purchase Withdrawal / Shipping Withdrawal.
# Deterministic brand resolution + a direct (never vector-similarity)
# Knowledge Base lookup by stable `intent` tag.
from services.withdrawal_flow import (
    resolve_shipping_withdrawal_brand as _resolve_shipping_withdrawal_brand,
    purchase_withdrawal_reply as _purchase_withdrawal_reply,
    shipping_withdrawal_reply as _shipping_withdrawal_reply,
    shipping_withdrawal_pending_brand_reply as _shipping_withdrawal_pending_brand_reply,
)

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


# CUSTOMER-ERP-READ-1 — the private ERP READ status-inquiry actions. The
# error-taxonomy handling below (MALFORMED_IDENTIFIER / VALID_NOT_FOUND)
# is scoped to these keys ONLY — never a WRITE flow
# (requestshippingaddresschange) and never a synthetic test action.
_ERP_READ_STATUS_ACTIONS = {"searchdatashipment", "searchdataorder", "searchdatatracking"}
# A shipment-bill code that is bill-SHAPED (2 letters + digits) but too
# short to be a complete Shipify code — real codes are a 2-letter prefix
# + 15 digits (17 chars); the customer's own documented incomplete
# example "FT31822026072" is 2 + 11. Deterministic, format-only.
_SHORT_SHIPMENT_CODE_RE = re.compile(r"\b[A-Za-z]{2}\d{6,12}\b")


def _incomplete_shipment_code(message: str, expecting_param: Optional[Dict], action_key: Optional[str]) -> Optional[str]:
    """The truncated shipment-code token in `message` when the
    searchdatashipment flow is asking for its ShipmentCode and the
    customer's value is bill-shaped but too short to be complete, else
    None. Scoped to searchdatashipment / ShipmentCode so it can never
    touch CustCode, a WRITE flow, or a synthetic test action."""
    if action_key != "searchdatashipment":
        return None
    if (expecting_param or {}).get("name", "").strip().lower() != "shipmentcode":
        return None
    m = _SHORT_SHIPMENT_CODE_RE.search(message or "")
    return m.group(0) if m else None


def _incomplete_identifier_prompt(param: Dict, token: str) -> str:
    display = (param or {}).get("display_name") or (param or {}).get("name") or "เลขที่บิลขนส่ง"
    return (f"เลขที่แจ้งมา ({token}) ดูเหมือนจะไม่ครบถ้วนค่ะ "
            f"รบกวนตรวจสอบและส่ง{display}แบบเต็มอีกครั้งนะคะ")


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
    # CUSTOMER-LINK-1 — a still-missing REQUIRED system_generated
    # parameter (e.g. geturlproductdetail's URL) has no traditional
    # slot-collection prompt (_NON_ASKABLE_INPUT_SOURCES deliberately
    # excludes system_generated, same as a secret), but unlike a secret
    # it genuinely CAN be supplied by the customer directly (pasted, or
    # auto-extracted from the message on a later turn). Without this,
    # _is_execution_ready() sees "nothing askable left" and the Executor
    # is reached with the value simply absent. Generic to any action
    # shaped this way (verified: geturlproductdetail's URL is currently
    # the only required system_generated parameter in the registry —
    # zero behavior change for every other existing action).
    for name in validation.get("missing_required") or []:
        for p in action.get("parameters") or []:
            if p["name"] == name and p.get("input_source") == "system_generated":
                return p
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
    # REAL LINE 2026-09-16 (6-source reopen) — the ANCHOR private-state
    # domain this episode actually opened on: the user turn immediately
    # BEFORE this SAME expected_question's first occurrence in the
    # window. A reply that is itself a decisive private-state inquiry of
    # a DIFFERENT domain is a genuine explicit-intent switch (never
    # counted); one of the SAME domain (or when this episode was never a
    # private-state inquiry at all) is still the customer restating the
    # SAME unanswered ask, and must still count exactly as before.
    _anchor_psi_domain = None
    for j, _t in enumerate(recent_history):
        if _t.get("role") == "assistant" and _reply_matches_question(
                (_t.get("content") or "").strip(), expected_question):
            if j > 0 and recent_history[j - 1].get("role") == "user":
                _anchor = _classify_private_state_inquiry(recent_history[j - 1].get("content") or "")
                _anchor_psi_domain = _anchor.get("domain") if _anchor else None
            break
    for i, t in enumerate(recent_history):
        if t.get("role") != "assistant" or not _reply_matches_question(
                (t.get("content") or "").strip(), expected_question):
            continue
        if i + 1 < len(recent_history) and recent_history[i + 1].get("role") == "user":
            reply = recent_history[i + 1].get("content") or ""
            candidates = search_candidate_actions(registry, workflow=None, message=reply, collected_slots={})
            if select_best_action(candidates, minimum_score=1.0):
                continue
            # CUSTOMER-RED-REAL-FAIL-1 (RED-2) — the Business-Action-only
            # exclusion above never recognized a reply that was itself a
            # decisive, DIFFERENT request answered by RAG/FAQ rather than
            # any registered Business Action (e.g. "คูปองใช้ยังไงครับ" —
            # Coupon Usage has no Business Action to match, so it scored
            # nothing above and was wrongly counted as a failed identifier
            # attempt). REAL LINE: an already-closed searchdatashipment
            # ShipmentCode ask, correctly diverted to Coupon Usage for one
            # turn, still pushed the NEXT, unrelated ShipmentCode ask over
            # max_retry, escalating "สินค้าจะเข้าไทยตอนไหนคะ" to a false
            # Human-CS "insufficient info" reply instead of simply asking
            # for the bill. Reuses the SAME central deterministic
            # interpreter tier (services/conversation_semantics.py
            # `_compose`, no LLM call — this runs once per historical
            # pair and must stay cheap) already used everywhere else for
            # "does this message carry its own decisive intent" checks;
            # never a new parallel classifier.
            fam, conf, _ent = _compose_intent_family(reply)
            if fam in _ACTIONABLE_INTENT_FAMILIES and conf >= 0.55:
                continue
            # REAL LINE 2026-09-16 (6-source reopen) — a private-status
            # inquiry (SHIPMENT_STATUS / order / tracking / customer_data:
            # "ร้านส่งหรือยังคะ", "ขอแทรคไทยค่ะ", "วันนี้มีของเข้าไทยไหม",
            # "คูปองผมมีไหม") resolves through the SEPARATE deterministic
            # _classify_private_state_inquiry() classifier, never through
            # _compose_intent_family's family table above — so a customer
            # switching between DIFFERENT private-record questions (each
            # its own genuinely fresh identifier ask) had every switch
            # counted as a failed retry of the FIRST one, saturating
            # max_retry after two turns and escalating the customer's very
            # first attempt at a brand-new, unrelated private request
            # straight to Human CS. Reuses the SAME central classifier
            # already used everywhere else for this concept — never a new
            # parallel one. An explicit different private intent does NOT
            # bypass auth (the identifier is still asked once); it only
            # resets the retry count for THAT new intent's own turn.
            _reply_psi = _classify_private_state_inquiry(reply)
            if _reply_psi is not None and _reply_psi.get("domain") != _anchor_psi_domain:
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
        if is_address_component and action.get("action_key") == "requestshippingaddresschange":
            # CUSTOMER-CSW9-REAL-3 — a free-text address-component slot
            # (ReceiverName / Address / …, validation_type "non_empty")
            # must NEVER accept a token that structurally IS this action's
            # own RECORD identifier (ShipmentCode / OrderCode / Tracking
            # — a configured validation_pattern). Real LINE: the bill
            # number the customer typed in reply to "which bill?" got
            # pre-consumed into ShipmentCode by history replay, then the
            # SAME lone token fell through here and was bound to
            # ReceiverName. If ShipmentCode is still missing the token
            # belongs there (the pattern-first sort already tried it);
            # if it is filled, this token is a duplicate, never a name.
            # PHASE-1-STABILIZATION-ISOLATE-CSW9-1 — action-key-scoped:
            # generic free-text binding for every other action is
            # untouched (no other enabled action has address-component
            # params today, so this is behavior-preserving).
            _rec_id_patterns = [
                p.get("validation_pattern") for p in (action.get("parameters") or [])
                if (p.get("name") or "").strip().lower() in ("shipmentcode", "ordercode", "tracking")
                and p.get("validation_pattern")]
            if _rec_id_patterns:
                param_candidates = [
                    c for c in param_candidates
                    if not any(re.fullmatch(pat, c) for pat in _rec_id_patterns)]
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


def _apply_identifier_memory(action: Dict, collected: Dict, customer_context: Optional[Dict],
                             *, is_continuation: bool = True,
                             history: Optional[List[Dict]] = None) -> Dict[str, str]:
    """Fills any still-missing ASKABLE parameter from Identifier Memory
    (a value the customer already established earlier — this
    conversation or a prior one — persisted onto their profile by
    profiles/manager.py::update_profile_from_turn, keyed by
    IDENTIFIER_MEMORY_FIELDS). Never overrides a value already present in
    `collected` — an explicit/fresher value always wins. Returns a NEW
    dict; never mutates the one passed in.

    New-Action vs Continuation (P0-01 real-LINE fix, 2026-09-03) — a
    remembered ACCOUNT-SCOPED RECORD identifier (last_order_code /
    last_shipment_code / last_tracking — never CustCode, which is the
    customer's own identity and is reused across the whole conversation)
    only auto-fills when THIS turn is genuinely CONTINUING an already-
    active collection (the bot asked for it and the customer is
    answering, or a live pending workflow is being resumed —
    `is_continuation`). On a FRESH explicit request that starts the
    action anew ("ขอเช็กพัสดุเดียวครับ" with no bill number), a stale
    record id left in session memory from an earlier lookup must NOT
    silently make the action executable and skip the required question —
    the customer meant "check A shipment", not "re-run the last one".
    Identifier Memory is NOT globally disabled: CustCode still fills, and
    every genuine continuation still reuses the record id it collected."""
    working = dict(collected)
    askable = _askable_parameters_by_name(action)
    # CUSTOMER-CSW9-REAL-2 — requestshippingaddresschange submits an
    # address change for a SPECIFIC shipment to CS. Its RECORD-identifier
    # target (ShipmentCode) must be given explicitly FOR THIS operation,
    # never inherited from generic Identifier Memory ("the last shipment
    # the customer asked about"). Real LINE: last_shipment_code kept
    # pre-filling ShipmentCode, so (a) the customer's own bill answer to
    # "which bill?" then had nowhere to bind and spilled into the
    # loosely-validated ReceiverName slot, and (b) a fresh "อีกบิล"
    # request silently re-targeted the previous shipment. CustCode (the
    # customer's own identity, never account-SCOPED here) still fills.
    # PHASE-1-STABILIZATION-ISOLATE-CSW9-1 — action-key-scoped (was
    # `_requires_confirmation(action)`, a whole class): every other
    # Business Action keeps its exact pre-CSW9 Identifier-Memory
    # behavior.
    _csw9 = (action.get("action_key") == "requestshippingaddresschange")
    for profile_field, param_name in IDENTIFIER_MEMORY_FIELDS:
        if param_name in working or param_name not in askable:
            continue
        if profile_field in _ACCOUNT_SCOPED_MEMORY_FIELDS and (not is_continuation or _csw9):
            continue
        remembered = (customer_context or {}).get(profile_field)
        if remembered:
            working[param_name] = remembered
            continue
        # CUSTOMER-TRACK-TH-1.1 — account-scoped record identifiers
        # (last_shipment_code / last_order_code / last_tracking) are
        # deliberately NEVER persisted onto the profile row (Task 06,
        # 2026-08-26 security fix: a user-typed identifier is never
        # proof of account ownership and must not be remembered across
        # SESSIONS). Recovering one from `history` instead is safe
        # because it never leaves the current live conversation payload
        # already passed into this exact call, is scoped to a value
        # that was CONFIRMED successfully used (never a failed/
        # malformed lookup), and only runs when `is_continuation` is
        # already True — i.e. the caller already established (via
        # _resolve_conversation_reference's own field-name / referring-
        # expression-marker gate) that this turn is a decisive,
        # genuine follow-up, never a bare unrelated re-trigger.
        if is_continuation and history and profile_field in _ACCOUNT_SCOPED_MEMORY_FIELDS:
            recovered = _recover_history_identifier(action, param_name, history)
            if recovered:
                working[param_name] = recovered
    return working


# CUSTOMER-TRACK-TH-1.1 — the two reply shapes a genuinely FAILED
# per-record lookup produces on this platform (both owned entirely by
# this module, never customer-authored text): the VALID_NOT_FOUND
# template (CUSTOMER-ERP-READ-1) and the incomplete/malformed-identifier
# retry prompt (_incomplete_shipment_code). A history turn immediately
# followed by either must never be recovered as a successful referent.
_FAILED_LOOKUP_REPLY_RE = re.compile(r"ไม่พบข้อมูลรายการสำหรับเลขที่|ดูเหมือนจะไม่ครบถ้วนค่ะ")


def _recover_history_identifier(action: Dict, param_name: str, history: List[Dict]) -> Optional[str]:
    """A record identifier (e.g. ShipmentCode) the customer successfully
    used EARLIER in THIS SAME live conversation, recovered purely from
    `history` text — never written to the customer's profile row (see
    the Task 06 removal this is deliberately NOT re-introducing).
    Walks `history` in reverse for the most recent user turn whose
    content structurally matches `param_name`'s OWN configured
    validator (the exact same validator `_bind_message_to_action` uses
    to bind a fresh reply — never a separate, looser shape check), where
    the immediately-following assistant turn is a real reply (not a
    repeat of the same question) and is NOT one of this platform's own
    failed-lookup templates. Callers gate this on `is_continuation`
    already being True — this function itself does not re-decide
    whether the current turn is a genuine follow-up."""
    param = next((p for p in (action.get("parameters") or []) if p.get("name") == param_name), None)
    if not param:
        return None
    validator = _resolve_parameter_validator(param)
    for i in range(len(history) - 1, -1, -1):
        turn = history[i]
        if turn.get("role") != "user":
            continue
        candidate = (turn.get("content") or "").strip()
        if not candidate or not validator(candidate):
            continue
        reply = ""
        if i + 1 < len(history) and history[i + 1].get("role") == "assistant":
            reply = history[i + 1].get("content") or ""
        if not reply or _FAILED_LOOKUP_REPLY_RE.search(reply):
            continue
        return candidate
    return None


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


def _assistant_text_is_generated_action_prompt(text: str, registry) -> bool:
    """True only when `text` is VERBATIM one of the prompts this engine
    emits while a Business Action is collecting its input: a generated
    parameter question (`_generate_parameter_question`) for any parameter
    of any enabled API/WEBHOOK action, that action's confirmation-gate
    question, or a self-service identity-verification prompt.

    This is the STRUCTURAL "the previous turn was an ERP collection
    prompt" signal — matched via `_reply_matches_question` (the generated
    text verbatim, optionally with a known preamble/suffix). Unlike the
    text-shape heuristic `_last_assistant_turn_requests_input` (which
    fires on any polite "กรุณา…แจ้ง" phrasing), a free-form RAG answer
    that merely asks the customer for more task detail in natural
    language never matches this — so Public Clarification Continuity can
    use it to tell "the customer is answering a real parameter question"
    apart from "the customer is adding detail to a public answer that
    happened to end with a polite request"."""
    t = (text or "").strip()
    if not t:
        return False
    if t in (_SELF_VERIFY_ASK_PHONE_TEXT, _SELF_VERIFY_ASK_EMAIL_TEXT):
        return True
    try:
        actions = [a for a in registry.enabled_actions()
                   if a.get("action_type") in _IDENTITY_GATED_ACTION_TYPES]
    except Exception:
        return False
    for a in actions:
        try:
            full = registry.get_full(a["id"], mask_secrets=True)
        except Exception:
            continue
        if not full:
            continue
        for p in (full.get("parameters") or []):
            if _reply_matches_question(t, _generate_parameter_question(p)):
                return True
        if _requires_confirmation(full) and _reply_matches_question(
                t, _generate_confirmation_question(full, {})):
            return True
    return False


# CUSTOMER-CSW9-REAL-1 — stable literal fragments of
# _generate_confirmation_question's output (its `base`), used to tell an
# OPEN confirmation gate ("…awaiting ยืนยัน") from a CLOSED one (the
# customer already answered it and the cycle executed) without needing
# the exact per-turn summary text.
_CONFIRM_Q_MARKER = "ยืนยันการดำเนินการ '"
_CONFIRM_Q_TAIL = "กรุณาตอบ 'ยืนยัน' เพื่อดำเนินการต่อ"


def _confirmation_gate_open(history: List[Dict]) -> bool:
    """True only when the MOST RECENT assistant turn in `history` is an
    as-yet-unanswered confirmation question. While it is open, a
    confirmation-gated action's fully-collected slot set is a
    legitimately-pending state and must survive replay untouched. Once
    that turn is anything else (the execution / handoff reply, an
    unrelated answer, a later question), the gated cycle has CLOSED and
    its collected operation fields must NOT leak into a fresh request —
    the same 'completed-cycle boundary' rule non-confirmation actions
    already get (P0-01), which was previously skipped outright for every
    confirmation-gated action (real LINE: a finished CSW9 address change
    made every later 'ขอเปลี่ยนที่อยู่จัดส่งอีกบิล' jump straight to a
    confirmation screen pre-filled with the PREVIOUS episode's bill,
    receiver, phone and address)."""
    for turn in reversed(history or []):
        if turn.get("role") != "assistant":
            continue
        c = turn.get("content") or ""
        return _CONFIRM_Q_MARKER in c and _CONFIRM_Q_TAIL in c
    return False


def _is_fresh_address_change_opener(message: str, semantic) -> bool:
    """CUSTOMER-CSW9-REAL-4 — a genuine NEW address-change request-opener
    ("บิลขนส่ง FTxxx ต้องการเปลี่ยนที่อยู่จัดส่ง", "ขอเปลี่ยนที่อยู่จัดส่ง
    อีกบิลค่ะ", "เปลี่ยนที่อยู่หน่อย"): ADDRESS_CHANGE semantic family AND
    the message carries NO operation DATA of its own — parse_thai_address
    finds nothing, and there is no shipment/order/tracking-shaped token.
    A DATA-answer turn ("ผู้รับ มานะ 0899999999 ที่อยู่ 99/1 ต.บางรัก ..."
    a bare bill number) can still read as the ADDRESS_CHANGE family
    (especially via the gated LLM, on a message dominated by "ที่อยู่ …"),
    so the family alone is NOT enough — it always also carries parseable
    address data or an identifier. Only a real opener has neither, and
    only then may the REAL-3 fresh-episode cleanup discard replayed
    fields (real LINE: it fired on the composite address turn and wiped
    the ShipmentCode collected one turn earlier)."""
    if getattr(semantic, "intent_family", None) != "ADDRESS_CHANGE":
        return False
    from services.thai_address_parser import parse_thai_address
    if parse_thai_address(message or ""):
        return False
    if any(_validate_generic_identifier(tok)
           for tok in _TOKEN_SPLIT_RE.split(message or "") if tok):
        return False
    return True


def _replay_business_action_collection(action: Dict, registry, history: List[Dict],
                                        customer_context: Optional[Dict] = None,
                                        *, is_continuation: bool = True) -> Dict[str, str]:
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
    identity_seed: Dict[str, str] = _apply_identifier_memory(
        action, {}, customer_context, is_continuation=is_continuation, history=history)
    collected: Dict[str, str] = dict(identity_seed)
    # PHASE-1-STABILIZATION-ISOLATE-CSW9-1 — the confirmation-gate-aware
    # completed-cycle resets below (CUSTOMER-CSW9-REAL-1) are scoped to
    # requestshippingaddresschange ONLY; every other confirmation-gated
    # Business Action keeps its exact pre-CSW9 replay behavior (the
    # unchanged `not _requires_confirmation(action)` guard).
    _csw9 = (action.get("action_key") == "requestshippingaddresschange")
    for i, turn in enumerate(history):
        if turn.get("role") != "user":
            continue

        # Completed-Cycle Boundary (P0-01 real-LINE fix, 2026-09-03) —
        # session_service.get_recent_history returns a 20-exchange
        # window that routinely spans SEVERAL completed request/response
        # cycles of the SAME action. Once `collected` already satisfies
        # every required parameter of a non-confirmation action, that
        # earlier cycle has executed and closed; a later user turn is a
        # BRAND-NEW request, never more input for the finished cycle.
        # Reset to the identity-only seed so a record identifier
        # answered several turns and one execution ago (e.g. a
        # ShipmentCode collected for a prior "ขอเช็กพัสดุเดียวครับ") never
        # carries forward into a fresh same-action request that carries
        # no identifier of its own — the exact P0-01 failure. Each such
        # turn is then re-validated through the SAME trigger check
        # `i == 0` uses (below), so a genuine still-open mid-collection
        # continuation (collected incomplete, or a confirmation-gated
        # action awaiting "ยืนยัน") is never touched.
        # CUSTOMER-CSW9-REAL-1 — a confirmation-gated action's completed
        # cycle is also a boundary, as long as its gate is not currently
        # OPEN (the pending "awaiting ยืนยัน" state, which legitimately
        # has a full `collected` and no next param). `history[:i + 1]`
        # ends at this user turn, so _confirmation_gate_open() reads the
        # assistant turn right before it.
        _cycle_closed = ((not _requires_confirmation(action)
                          or (_csw9 and not _confirmation_gate_open(history[:i + 1])))
                         and not _next_expected_parameter(action, registry, collected)
                         and bool(collected) and collected != identity_seed)
        if i == 0 or _cycle_closed:
            if _cycle_closed:
                collected = dict(identity_seed)
            # Cross-Topic Contamination fix (Task 02B, 2026-08-25) — a
            # user turn is only trusted as THIS action's own trigger
            # message if the very next assistant turn is consistent with
            # THIS action having generated it (one of its own parameter
            # questions given what tentatively binding this message
            # would collect, or its confirmation question) — never
            # unconditionally. `history` is the full recent session
            # window, not a per-action transcript, so a turn at this
            # position may equally well be an EARLIER, unrelated topic's
            # message. Confirmed live: an earlier "SP1008 order ล่าสุด"
            # turn got bound to a LATER, unrelated address-change
            # action's own ReceiverName purely because it occupied
            # history[0] — validation answers "is this value
            # structurally allowed", never "does this turn belong to
            # this action's slot", and only the surrounding
            # question/answer chain can answer that.
            tentative = _bind_all_from_message(action, registry, collected, turn.get("content") or "")
            tentative_collected = tentative["collected"]
            next_turn = history[i + 1] if len(history) > i + 1 else None
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

    # Completed-Cycle Boundary at the WINDOW TAIL (P0-01 round 3,
    # 2026-09-03) — the mid-walk reset above only fires when another user
    # turn follows the finished cycle. When the most recent thing in the
    # window IS a completed (executed) cycle of this action — e.g.
    # "ขอเช็กพัสดุเดียวครับ → กรุณาแจ้งเลขที่บิลขนส่งค่ะ → FT... → <DETAIL
    # result>" as the last four turns — the walk ends with that cycle's
    # `collected` fully satisfied and nothing resets it, so the record
    # identifier leaks into the CURRENT fresh request (real LINE turn
    # 614). If this turn is not continuing that collection
    # (`is_continuation` is False — no live pending row, not a
    # conversation-continuation / detail-sibling selection) and the
    # action is not confirmation-gated, the reconstructed collection
    # belongs to the finished cycle: return the identity-only seed so the
    # fresh cycle starts with the record identifier missing. CustCode
    # (customer identity) is preserved by the seed; a genuine
    # continuation and a confirmation-await turn are `is_continuation` /
    # `_requires_confirmation` and untouched.
    # CUSTOMER-CSW9-REAL-1 — same widening as the mid-walk reset above:
    # a confirmation-gated action whose gate is not currently OPEN has a
    # CLOSED (executed) cycle at the window tail, so its operation fields
    # must not carry into the fresh request either.
    if (not is_continuation
            and (not _requires_confirmation(action)
                 or (_csw9 and not _confirmation_gate_open(history)))
            and collected != identity_seed
            and not _next_expected_parameter(action, registry, collected)):
        return dict(identity_seed)
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

# Self-referencing / on-file-account wording — a message using any of
# these is talking about ITS OWN account rather than a generic company
# subject. Extracted so classify_turn_intent (its `_self_registered`
# gate) and the Public Clarification Continuity coercion in decide()
# share ONE definition instead of two that could drift apart.
_SELF_REGISTERED_RE = re.compile(r"ผม|ฉัน|ดิฉัน|ลงทะเบียน|ที่ผูก|บัญชีของ|โปรไฟล์|ในระบบ")

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


# ── SEM-1: Private-record status-inquiry concept (2026-09-03) ──────────
# ONE generic, compositional, non-LLM recognizer for the semantic frame
# "the customer is asking about the STATE of their OWN business record /
# account" — the Customer UAT baseline's single largest failure family
# (14 SEMANTIC_INTENT primaries + 12 downstream live-RAG "ไม่มีข้อมูลยืนยัน"
# dead-ends). The routing path has NO LLM classifier; matching is purely
# lexical keyword overlap against each Business Action's configured
# search_keywords, and natural Thai phrasings ("สินค้าถึงโกดังหรือยัง",
# "ร้านส่งของให้ผมหรือยัง", "ของผมเข้าไทยยัง") share zero tokens with any
# action, so the turn falls through to RAG and dead-ends.
#
# This is NOT a phrase dictionary — it is a small set of GENERIC building
# blocks combined by rule:
#   state-probe   : a "what is the state of X" marker that has no
#                   "how does X work in general" reading in natural Thai
#   record-domain : a generic logistics/order/payment record noun
#   ownership     : first-person / on-file-account wording (optional when
#                   a record-domain noun is present, since in a 1:1 CS DM
#                   an unqualified record question is about the sender)
# and gated OFF for company-subject questions, how-to questions, and
# operational WRITE requests (those belong to RAG / the Human Handoff
# phase, never here — see the SEM-1 spec point 6).
# The completive "ยัง" ("…yet?") — NOT "ยังไง" ("how"), "ยังไม่" ("not
# yet, [statement]"). Only the sentence-final / "หรือยัง" reading is an
# arrival state probe.
_YET = r"(?:(?:หรือ|รึ)ยัง|ยัง(?=คะ|ครับ|คับ|ค่ะ|มั้ย|ไหม|เปล่า|\s|$|\?|,))"
_PSI_STATE_PROBE_RE = re.compile(
    r"ถึงไหน|ถึงไหนแล้ว|ถึง.{0,6}(โกดัง|ไทย|จีน).{0,4}" + _YET
    + r"|เข้า.{0,4}ไทย.{0,4}(?:" + _YET + r"|ไหม|มั้ย|ตอนไหน|วันไหน|เมื่อไหร่|เมื่อไร)"
    + r"|ส่ง.{0,8}(?:" + _YET + r")|ส่งของ.{0,8}(?:" + _YET + r")"
    + r"|มา.{0,4}(?:" + _YET + r")|เข้า.{0,6}(?:" + _YET + r")"
    + r"|ได้รับ.{0,8}(?:" + _YET + r")"
    + r"|มี.{0,12}(ของ|สินค้า|พัสดุ|บิล).{0,8}(เข้า|ถึง|มา)ไทย"
    + r"|สถานะ|คืบหน้า|ค้างอยู่|(?<!ที่)อยู่(ที่)?ไหน|ตรงไหน|ถึงไทยรึยัง"
    + r"|เป็นไง|เป็นยังไงบ้าง|เป็นอย่างไรบ้าง|เป็นไงบ้าง|เป็นยังไงบ้าง"
    + r"|เหลือ.{0,12}(ไหม|มั้ย|บ้าง|กี่|เท่า)|เหลือเท่า(ไหร่|ไร)|เหลือกี่|มีเท่า(ไหร่|ไร)|กี่บาท"
    + r"|เป็นของ.{0,10}(บิล|ออเดอร์|คำสั่งซื้อ).{0,4}ไหน|ของบิลสั่งซื้อ.{0,3}ไหน"
    + r"|(ที่อยู่|address).{0,10}ถูก(ต้อง)?.{0,4}(ไหม|มั้ย|รึเปล่า)|ใส่.{0,12}ถูก(ต้อง)?.{0,4}ไหม")
# "มี…อะไร/บ้าง" is a CATALOG probe ("what services are there",
# "which goods are prohibited") — public FAQ, NOT a private state probe.
# It only signals a private inquiry when the customer is asking what is
# in THEIR OWN account (owner wording + a customer-data domain).
# REAL LINE 2026-09-16 — a POSSESSION probe ("คูปองผมมีไหม", "ยอดผมเหลือ
# มั้ย") is the same customer-scoped catalog question as "…มีอะไรบ้าง":
# does MY record hold any? It is only private when owner wording is
# present (the same `private_catalog = probe and owner` rule below), so a
# public "มีขั้นต่ำในการสั่งไหม" / "มีบริการอะไรบ้าง" is untouched.
_PSI_CATALOG_PROBE_RE = re.compile(
    r"มี.{0,15}(อะไร|บ้าง)|มี.{0,12}(?:ไหม|มั้ย|มัย|ป่ะ|ปะ|หรอ|เหรอ)|เหลือ.{0,8}(?:ไหม|มั้ย|มัย|เท่าไหร่|กี่)")
# "ขอ<data-noun>" — a bare request for the customer's own record data
# (ขอแทรคไทย / ขอสถานะ / ขอรายละเอียดออเดอร์) is a status inquiry, not a
# how-to. "ขอที่อยู่โกดัง" / "ขอเบอร์ติดต่อ" carry no record-domain noun
# and are filtered out by the domain gate.
_PSI_DATA_REQUEST_RE = re.compile(
    r"(?:ขอ|อยากได้|อยากทราบ)\S{0,3}(เลข)?(แทรค|แทร็ก|แทรก|ติดตาม|tracking|สถานะ|ราย(การ|ละเอียด)|ข้อมูล|ยอด)",
    re.IGNORECASE)
# generic record-domain nouns -> coarse capability domain
_PSI_DOMAIN_RES = (
    # CUSTOMER-TRACK-TH-1: "ติดตาม" (a plain-Thai synonym for "track") added
    # alongside the existing transliterations/loanword so "ขอเลขติดตาม
    # ฝั่งไทยหน่อยครับ" collapses to the same "tracking" domain as
    # "ขอแทรคไทยค่ะ" — one word, not a phrase dictionary.
    ("tracking", re.compile(r"แทรค|แทร็ก|แทรก|tracking|ติดตาม|เลขพัสดุ|เลขจีน", re.IGNORECASE)),
    ("order", re.compile(r"ออเดอร์|คำสั่งซื้อ|บิลสั่งซื้อ|ร้าน.{0,6}ส่ง|ที่สั่งไป|ที่สั่งไว้|order", re.IGNORECASE)),
    ("customer_data", re.compile(
        r"วอลเล็ท|wallet|ยอดเงิน|เงินในระบบ|เงินที่เติม|เติมเงิน|เครดิต|คูปอง|coupon|ยอดของ"
        r"|ที่อยู่โกดังจีน|ที่อยู่จีน"
        r"|เบอร์.{0,10}(ที่ผม|ที่ลงทะเบียน|ในระบบ|ที่ผูก|ของผม|ของฉัน)|เบอร์ที่ลงทะเบียน"
        r"|อีเมล.{0,10}(ที่ผม|ที่ลงทะเบียน|ในระบบ|ที่ผูก|ของผม|ของฉัน)"
        r"|ข้อมูลลูกค้าของ(ผม|ฉัน|เรา|หนู)|ข้อมูลของผมในระบบ|โปรไฟล์ของผม", re.IGNORECASE)),
    ("shipment", re.compile(r"พัสดุ|สินค้า|กล่อง|ของที่ส่ง|ของผม|ของฉัน|ของหนู|บิลขนส่ง|ของเข้าไทย|ของ(เรา|ผม|ฉัน)", re.IGNORECASE)),
)
_PSI_OWNERSHIP_RE = re.compile(r"ผม|ฉัน|ดิฉัน|หนู|เรา|ของผม|ของฉัน|บัญชีของ|บัญชีผม|ในระบบ|ที่ผูก|ที่ลงทะเบียน|ที่สั่งไป")

# SEMANTIC-FIRST-2 — a small STRUCTURAL cue (not a sentence dictionary):
# a shipment/order STATUS question framed around the SELLER's dispatch
# ("ร้านส่ง…", "ต้นทางส่ง…", "ฝั่งร้านปล่อยของ…") is an ORDER-domain
# inquiry (the CS-approved reply asks for the order/purchase bill), vs a
# parcel-in-transit question -> shipment domain.
_SELLER_DISPATCH_RE = re.compile(r"ร้าน|ต้นทาง|ฝั่งร้าน|ร้านค้า|ร้านจีน|shop|เจ้าของร้าน", re.IGNORECASE)
# PHASE-5 D10 / D13 — an INVOICE-issuance or a CANCELLATION request that
# only carries an INCIDENTAL order identifier ("ขอใบกำกับของ PO12345",
# "ขอยกเลิก PO12345") is NOT a "check my order status" request: there is
# no invoice-fetch action and no executable CancelOrder, so the real
# answer is the how-to / policy (RAG). These two markers gate the
# diversion: fire it only when the message carries a cancellation ask (or
# is INVOICE-family) AND no genuine order-status verb of its own.
_CANCELLATION_REQUEST_RE = re.compile(r"ยกเลิก|ขอคืนเงิน|ยกเลิกบิล|ยกเลิกคำสั่งซื้อ|ยกเลิกออเดอร์")
_ORDER_STATUS_VERB_RE = re.compile(
    r"ติดตาม|เช็ก?สถานะ|เช็คสถานะ|ตรวจสอบสถานะ|ดูสถานะ|สถานะ(?:ล่าสุด|ตอนนี้|ปัจจุบัน)?"
    r"|ร้านส่ง|ร้านจัดส่ง|ร้านยังไม่ส่ง|จัดส่งหรือยัง|ส่งของหรือยัง|ถึงไหน(?:แล้ว)?")
# NOTE (PHASE 6 FINAL SAFETY CLOSURE): the public-exception blocklists
# that used to live here (_TRANSIT_TIME_Q_RE, _DELIVERY_CAPABILITY_Q_RE)
# are GONE. They guarded the LLM-family private synthesis by listing
# public phrasings to exclude, which could never be complete. That gate
# now requires POSITIVE ownership evidence instead — see
# _private_ownership_evidence() below.
# SEMANTIC-FIRST-2 / FIX-2 BOUNDARY — a bare elliptical possessive /
# demonstrative follow-up with no referent the engine could resolve
# ("ของผมล่ะ", "อันนี้ล่ะคะ", "แล้วของผมไหม") is UNCLEAR LANGUAGE, not
# missing company knowledge — it must be met with a clarification, never
# routed to Human CS as an unsupported company fact.
_ELLIPTICAL_NO_REFERENT_RE = re.compile(
    r"(?:แล้ว\s*)?(?:ของ)?(?:ผม|ฉัน|หนู|เรา|ดิฉัน|อันนี้|อันนั้น|ตัวนี้|อันของผม|ของผม|ของฉัน)"
    r"\s*(?:ล่ะ|หละ|ล้ะ|ไหม|มั้ย|ยัง)?\s*(?:คะ|ครับ|ค่ะ|น่ะ|อ่ะ|อะ)?\s*$", re.IGNORECASE)
# SYSTEM-STATE-EMERGENCY-1 — a recent assistant turn that itself put a
# SPECIFIC private-data topic on the table (offered / asked about a
# coupon, a wallet balance, a shipment / order status, tracking, a named
# bill). Only then does a following bare "ของผมล่ะ" HAVE a referent and
# may resolve to that topic. A charter slot prompt, a warehouse
# clarification, an invoice-policy answer, a rate reply — none establish
# one, so a bare possessive there must be met with a clarification, never
# a customer-profile / wallet ERP read.
_PRIVATE_REFERENT_OFFER_RE = re.compile(
    r"(?:ต้องการ|อยาก|จะ|ให้|จะให้)\s*(?:ช่วย)?\s*(?:ตรวจสอบ|เช็ก|เช็ค|ดู|แจ้ง)\s*\S{0,18}"
    r"(?:คูปอง|coupon|wallet|วอลเล็ท|กระเป๋าเงิน|ยอดเงิน|พัสดุ|ออเดอร์|คำสั่งซื้อ|บิล|แทร็ก|tracking|สถานะ)"
    r"|(?:คูปอง|coupon|ยอด\s*wallet|ยอดเงิน\s*wallet|สถานะพัสดุ|สถานะสินค้า|สถานะออเดอร์)\s*\S{0,10}"
    r"(?:ไหมคะ|ไหมครับ|มั้ยคะ|มั้ยครับ|หรือเปล่า|รึเปล่า|ใช่ไหม)",
    re.IGNORECASE)
# PPC (2026-09-03) — a facility LOCATION / hours question ("โกดังรับสินค้า
# อยู่ที่ไหน", "สาขานนทบุรีเปิดกี่โมง") is always PUBLIC FAQ, never a
# private shipment-status inquiry — even though it contains "สินค้า" +
# "อยู่ที่ไหน". Deliberately narrow: only a location/hours question about a
# named facility, NOT "ใส่ที่อยู่โกดังจีนถูกไหม" (a private address
# validation — no location/hours interrogative).
_PSI_FACILITY_LOC_RE = re.compile(
    r"(โกดัง|คลังสินค้า|คลังไทย|สาขา|จุดรับสินค้า|จุดส่ง|จุดรับของ|ออฟฟิศ|สำนักงาน|บริษัท)"
    r"[^\n]{0,16}(อยู่(ที่)?ไหน|อยู่ตรงไหน|ที่ไหน|พิกัด|แผนที่|เปิดกี่โมง|เปิดทำการ|เวลาทำการ|เปิดวันไหน|ปิดวันไหน|เปิดทุกวัน)")
# a PROHIBITED/POLICY-category question ("สินค้าที่ห้ามนำเข้ามีอะไรบ้าง")
# names a company RULE, never a personal record, however generic the
# goods noun in it looks (see the catalog-probe relaxation below).
_PSI_POLICY_TOPIC_RE = re.compile(r"ห้ามนำเข้า|สินค้าต้องห้าม|ต้องห้ามนำเข้า|นำเข้าไม่ได้|ที่ห้าม")
# PPC — a direct "คืออะไร / เป็นอะไร" value question about the customer's
# OWN on-file datum ("เบอร์ที่ผมลงทะเบียนไว้คืออะไร"). Counts as a probe
# only together with owner wording (guarded in the evidence rule), so a
# public "CBM คืออะไร" (no owner, and no record-domain noun anyway) never
# reaches it.
_PSI_VALUE_Q_RE = re.compile(r"คืออะไร|เป็นอะไร|อะไรค่ะ$|อะไรคะ$|อะไรครับ$")
# SEM-1.1 record-scope markers — an inquiry that explicitly asks for the
# LATEST record ("พัสดุล่าสุด") or the WHOLE LIST ("มีกี่รายการ", "วันนี้
# มีอะไรเข้าบ้าง") may safely use a customer-scoped list/latest action.
# Without either, a single-record status inquiry has UNSPECIFIED scope and
# MUST ask for the record identifier rather than silently return a latest
# or historical record.
_PSI_LATEST_RE = re.compile(r"ล่าสุด|อันล่าสุด|ตัวล่าสุด|ครั้งล่าสุด|เที่ยวล่าสุด|recent|latest", re.IGNORECASE)
_PSI_LIST_ALL_RE = re.compile(
    r"ทั้งหมด|ทุกรายการ|ทุกอัน|ทุกบิล|มีกี่|กี่รายการ|กี่บิล|รายการไหนบ้าง|บิลไหนบ้าง"
    r"|วันนี้|พรุ่งนี้|วันไหน|สัปดาห์นี้|เดือนนี้|list\b", re.IGNORECASE)
_PSI_THIRD_PARTY_RE = re.compile(r"บริษัท(?!.{0,4}ผม)|เพื่อน|ลูกค้าท่านอื่น|คนอื่น")
_PSI_HOWTO_RE = re.compile(r"ยังไง|ยังงัย|อย่างไร|วิธี(การ)?|ขั้นตอน|how\s*to", re.IGNORECASE)
_PSI_WRITE_VERB_RE = re.compile(r"เปลี่ยน|แก้ไข|แก้จำนวน|ยกเลิก|ถอนเงิน|ถอน|เพิ่ม.{0,4}(vat|VAT|จำนวน)|ลบ|รวมบิล|รีแพ็ค|รีเเพ็ค|ตีลัง|สั่งผลิต|สกรีน")
_PSI_CHECK_VERB_RE = re.compile(r"เช็ก|เช็ค|เชค|ตรวจสอบ|ดูให้|ขอดู|ขอเช็ก")


# PHASE 6 FINAL SAFETY CLOSURE (A) — PRIVATE-INQUIRY AUTHORITY INVERSION.
#
# The former rule was "an LLM family name makes this private UNLESS the
# wording matches one of an enumerated list of public exceptions" — an
# open-ended blocklist, so every public phrasing nobody had thought of yet
# became a demand for the customer's identity (exactly how
# "จัดส่งสินค้าถึงหน้าบ้านเลยไหม" reached production). The rule is now
# inverted and closed-form:
#
#     A PRIVATE INQUIRY REQUIRES POSITIVE PRIVATE-OWNERSHIP EVIDENCE.
#     An LLM family name is EVIDENCE, never authorization.
#
# Accepted positive evidence (any one of):
#   1. first-person possession OF A RECORD/ASSET — "ของฉัน", "บิลของผม",
#      "ออเดอร์ฉัน", "ยอดของฉัน". Deliberately NOT a bare pronoun:
#      "ผมอยากทราบว่าส่งถึงบ้านไหม" is a public question containing "ผม".
#   2. a concrete record identifier supplied in this turn;
#   3. an authenticated / active private workflow already running;
#   4. a compatible slot ANSWER inside an already-established private
#      journey. Both halves are required — without the "is actually an
#      answer" half, a fresh public question typed after a stale private
#      prompt would be swallowed by the old journey (stale takeover).
_FIRST_PERSON_MARK_RE = r"(?:ผม|ฉัน|ดิฉัน|หนู|เรา|กระผม|ข้าพเจ้า)"
_OWNED_RECORD_NOUN_RE = (r"(?:บิล|ออเดอร์|คำสั่งซื้อ|พัสดุ|ของ|สินค้า|ล็อต|ยอด|เงิน|"
                         r"กระเป๋า|วอลเล็ท|วอลเลท|คูปอง|แทรค|แทร็ก|บัญชี|เครดิต|"
                         r"ใบกำกับ|ใบเสร็จ|รายการ|สถานะ|เลขบิล|แพ็กเกจ)")
_PRIVATE_POSSESSION_RE = re.compile(
    _OWNED_RECORD_NOUN_RE + r"\s*(?:ของ\s*)?" + _FIRST_PERSON_MARK_RE)
_ASSISTANT_ASKED_PRIVATE_ID_RE = re.compile(
    r"(?:เลขบิล|บิลขนส่ง|บิลสั่งซื้อ|เลขแทรค|แทรคจีน|เลขออเดอร์|เลขพัสดุ|"
    r"รหัสลูกค้า|เลขที่คำสั่งซื้อ)")
# the shape of an ANSWER (a bare value), not of a fresh question.
_BARE_VALUE_ANSWER_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9\-_/]{2,23}\s*(?:ค่ะ|ครับ|คับ|นะ|จ้า)?$")
_FRESH_QUESTION_TAIL_RE = re.compile(
    r"(?:ไหม|มั้ย|มัย|หรือเปล่า|รึเปล่า|หรือไม่|ยังไง|อย่างไร|เท่าไหร่|กี่\S|\?)")


# 5. a SELLER-DISPATCH STATUS question ("ต้นทางส่งมาหรือยังครับ", "ฝั่งร้าน
#    ปล่อยของหรือยัง"): the subject is the supplier/origin and the predicate
#    asks whether they have dispatched YET. That speech act presupposes an
#    order the asker themselves placed — you cannot ask it about a stranger's
#    order — so it is positive ownership evidence, not a public FAQ. Requires
#    BOTH the origin subject and the dispatch-status predicate: a bare
#    "ร้านอยู่ที่ไหน" (where is the shop) is a public location question and
#    matches only the subject half. Required by the SEMANTIC_FIRST_2 source
#    ("meaning understood + input missing -> ask for the id, never a
#    no-information dead end").
_SELLER_DISPATCH_STATUS_RE = re.compile(
    r"(?:ร้าน|ร้านค้า|ร้านจีน|ต้นทาง|ฝั่งร้าน|โรงงาน|เจ้าของร้าน|shop)"
    r".{0,18}?(?:ส่ง|จัดส่ง|ปล่อย|ออกของ|ส่งออก)"
    r".{0,12}?(?:ยัง|แล้ว|หรือเปล่า|รึเปล่า|มั้ย|ไหม)",
    re.IGNORECASE)


def _private_ownership_evidence(message, history=None, customer_context=None):
    """POSITIVE evidence that this turn concerns the customer's OWN
    record, or None. Returns the evidence kind so the developer trace can
    show WHY a turn was treated as private."""
    t = message or ""
    if _PRIVATE_POSSESSION_RE.search(t):
        return "first_person_possession"
    if any(_validate_generic_identifier(tok) and not tok.isdigit()
           for tok in _TOKEN_SPLIT_RE.split(t) if tok):
        return "record_identifier_supplied"
    ctx = customer_context or {}
    if ctx.get("last_business_action") or ctx.get("pending_action_id"):
        return "active_private_workflow"
    if _SELLER_DISPATCH_STATUS_RE.search(t):
        return "seller_dispatch_status_question"
    if _BARE_VALUE_ANSWER_RE.match(t.strip()) and not _FRESH_QUESTION_TAIL_RE.search(t):
        for turn in reversed(list(history or [])[-4:]):
            if turn.get("role") != "assistant":
                continue
            if _ASSISTANT_ASKED_PRIVATE_ID_RE.search(turn.get("content") or ""):
                return "private_journey_slot_answer"
            break
    return None


def _classify_private_state_inquiry(message: str):
    """Pure, non-LLM. Returns a dict describing the private-record
    status-inquiry concept when the message expresses it, else None:

        {"domain": "shipment"|"order"|"tracking"|"customer_data",
         "intent": "status_inquiry", "ownership_scope": "USER_PRIVATE",
         "confidence": float}

    Message-only (no history, no candidate scores) so it can run in the
    same place classify_turn_intent() does. Never fires for a company-
    subject question, a how-to question, or an operational WRITE request.
    """
    text = message or ""
    if not text.strip():
        return None
    # Hard negative gates first — keep the already-good 97.8% public/
    # private behaviour intact and stay out of the Human Handoff phase's
    # operational-WRITE territory (SEM-1 spec point 6).
    if _PSI_THIRD_PARTY_RE.search(text):
        return None
    if _PSI_WRITE_VERB_RE.search(text):
        return None
    if _PSI_FACILITY_LOC_RE.search(text):
        # a warehouse/branch LOCATION or hours question — public FAQ
        return None
    if _PSI_POLICY_TOPIC_RE.search(text):
        # CUSTOMER SCREENSHOT 2026-09-17 — a PROHIBITED/POLICY-category
        # question ("สินค้าที่ห้ามนำเข้ามีอะไรบ้าง") names a company RULE,
        # never a personal record, however generic the shipment/goods noun
        # in it looks. Needed once the tracking/shipment/order catalog-
        # probe relaxation below dropped the owner-wording requirement for
        # those domains — this keeps that widened check from also
        # catching a policy question that happens to use "สินค้า"/"ของ".
        return None
    has_probe = bool(_PSI_STATE_PROBE_RE.search(text))
    has_howto = bool(_PSI_HOWTO_RE.search(text))
    has_check_verb = bool(_PSI_CHECK_VERB_RE.search(text) or _PSI_DATA_REQUEST_RE.search(text))
    has_owner = bool(_PSI_OWNERSHIP_RE.search(text))
    has_catalog_probe = bool(_PSI_CATALOG_PROBE_RE.search(text))
    # A how-to question ("ถอนเงินยังไง", "ใช้คูปองยังไง") is RAG's job even
    # when it names a record noun — unless it ALSO carries a concrete
    # state probe ("ยอดผมเหลือเท่าไหร่ ดูยังไง" is still a balance query).
    if has_howto and not has_probe:
        return None
    domain = None
    for name, rx in _PSI_DOMAIN_RES:
        if rx.search(text):
            domain = name
            break
    if domain is None:
        return None
    # A "มี…อะไร/บ้าง" catalog probe is a PRIVATE inquiry only when the
    # customer explicitly scopes it to themselves ("พัสดุของผมมีอะไรบ้าง",
    # "ในบัญชีผมมีคูปองอะไร") — the owner wording is what separates it from
    # a public catalog question ("สินค้าที่ห้ามนำเข้ามีอะไรบ้าง"). It is a
    # LIST_ALL-scope inquiry (handled in the record_scope block below).
    # CUSTOMER SCREENSHOT 2026-09-17 — the owner-wording requirement is
    # dropped for "order"/"tracking" specifically: their domain-defining
    # words (ออเดอร์/คำสั่งซื้อ, แทรค/tracking/ติดตาม) have NO meaningful
    # public/company-wide reading — "มีเลขแทรคไทยมั้ย" can only ever mean
    # THIS customer's own tracking number. "shipment" is deliberately
    # EXCLUDED here: its domain regex also matches bare "สินค้า"/"ของ",
    # which genuinely public catalog/policy questions use too
    # ("สินค้าที่ห้ามนำเข้ามีอะไรบ้าง", "มีสินค้าอะไรบ้างที่ลดราคา") — an
    # ID-shaped shipment noun ("เลขพัสดุ") is covered by widening the
    # "tracking" domain itself instead (below), not by relaxing this
    # gate for the whole "shipment" domain. customer_data (coupon/wallet)
    # also keeps the owner-wording requirement for the same reason.
    private_catalog = has_catalog_probe and (has_owner or domain in ("order", "tracking"))
    # PPC — a direct "…คืออะไร" value question about the customer's OWN
    # on-file datum ("เบอร์ที่ผมลงทะเบียนไว้คืออะไร"). Only counts with
    # owner wording, so a public "CBM คืออะไร" (no owner, no record-domain
    # noun) never reaches this branch.
    private_value_q = has_owner and bool(_PSI_VALUE_Q_RE.search(text))
    # Evidence rule: (an explicit state probe) OR (a private catalog probe)
    # OR (a private value question) OR (a check/lookup/data-request verb
    # aimed at the customer's own record). Any one, with a record-domain
    # noun and no negative gate, is a private status inquiry.
    if not (has_probe or private_catalog or private_value_q
            or (has_check_verb and (has_owner or domain in ("shipment", "order", "tracking")))):
        return None
    confidence = 0.9 if ((has_probe or private_catalog or private_value_q) and (has_owner or domain != "shipment")) else 0.75
    # SEM-1.1 — semantic record scope. An identifier token in the message
    # is EXPLICIT_RECORD; "ล่าสุด" is LATEST; a whole-list / date-range
    # ask is LIST_ALL; a "…อะไรบ้าง" catalog probe outside customer_data
    # is LIST_ALL; otherwise a single-record inquiry with no identifier is
    # UNSPECIFIED and the caller must collect the record identifier.
    if any(_validate_generic_identifier(tok) and not tok.isdigit()
           for tok in _TOKEN_SPLIT_RE.split(text) if tok):
        record_scope = "EXPLICIT_RECORD"
    elif _PSI_LATEST_RE.search(text):
        record_scope = "LATEST"
    elif _PSI_LIST_ALL_RE.search(text) or (has_catalog_probe and domain != "customer_data"):
        record_scope = "LIST_ALL"
    else:
        record_scope = "UNSPECIFIED"
    return {"domain": domain, "intent": "status_inquiry",
            "ownership_scope": "USER_PRIVATE", "record_scope": record_scope,
            "confidence": confidence}


# Domain -> generic Business Action category / key hints used to resolve
# which EXISTING enabled action serves a private status inquiry.
# Config-driven: matched against each action's own `category`/`action_key`
# /`name`, never a hardcoded id. `track_hint` names the per-record
# tracking action for the tracking domain.
# CUSTOMER-TRACK-TH-1 — an explicit ask for THAI tracking OUTPUT ("ขอ
# แทรคไทย", "tracking ฝั่งไทย", "ขอเลขติดตามฝั่งไทย") is semantically the
# OPPOSITE of the "tracking" domain's default `track_hint` preference (an
# action whose OWN required input IS a Chinese tracking number, e.g.
# searchdatatracking — "ค้นหาบิลขนส่งด้วยเลข Tracking จีน", confirmed via
# a live registry audit). The customer's own source (Ai.xlsx sheet
# '2.tongchecknairabop' row 17 / CSW17, api_input_hint: "shipment_bill_no")
# never supplies a China tracking number for this ask — REAL LINE
# confirmed the bug: "กรุณาแจ้งเลข Tracking จีนค่ะ" was wrong. "จีน"
# (China) explicitly named, or an actual identifier-shaped token already
# present in the message (the customer is quoting a real tracking value,
# not merely asking for one), means the request genuinely is about a
# China tracking number and the existing track_hint behavior stays
# untouched.
_PSI_THAI_TRACKING_OUTPUT_RE = re.compile(
    r"(?:แทรค|แทร็ก|แทรก|tracking|ติดตาม).{0,15}ไทย|ไทย.{0,15}(?:แทรค|แทร็ก|แทรก|tracking|ติดตาม)",
    re.IGNORECASE)

# SHIPMENT-DIRECT-ANSWER-1 — shipment follow-up RESPONSE COMPOSITION.
# The generic ERP field-dump composer (_compose_natural_reply) answers
# "what fields does this action return" rather than "what did the
# customer actually ask" — a customer asking "สินค้าจะเข้าไทยตอนไหนคะ"
# got back ShipmentCode/Status/TrackingCH/TotalSum instead of an ETA
# answer. These 3 patterns recognize the question SHAPE (ETA / warehouse
# arrival / current stage); Thai-tracking-availability reuses the
# already-existing _PSI_THAI_TRACKING_OUTPUT_RE above. Scoped strictly
# to composing a reply from searchdatashipment's OWN already-fetched
# fields — no new ERP call, no routing/authorization change.
_ETA_QUESTION_RE = re.compile(
    r"เข้าไทย.{0,10}(?:ตอนไหน|วันไหน|เมื่อไหร่|เมื่อไร)|(?:ตอนไหน|วันไหน|เมื่อไหร่|เมื่อไร).{0,10}เข้าไทย")
_WAREHOUSE_QUESTION_RE = re.compile(r"ถึงโกดัง.{0,6}(?:หรือยัง|รึยัง|หรือเปล่า)|โกดัง.{0,6}(?:หรือยัง|รึยัง)")
_CURRENT_STATUS_QUESTION_RE = re.compile(
    r"ตอนนี้.{0,6}ถึงไหน|ถึงไหนแล้ว|สถานะตอนนี้|สถานะล่าสุด|แล้วสถานะล่ะ|แล้วตอนนี้ยังไง")
# statuses that indicate the shipment has AT LEAST reached the China
# warehouse — the actual ERP status text is always the source of truth;
# this never invents "arrived" for a status it doesn't recognize.
_WAREHOUSE_ARRIVED_STATUS_RE = re.compile(
    r"รับเข้าที่จีน|รับเข้าโกดังจีน|ถึงโกดังจีน|เข้าโกดังจีน|ส่งออกจากจีน|อยู่ระหว่างนำส่ง|ถึงไทยแล้ว")
# PHASE-5 D5 — statuses that EXPLICITLY state the shipment has not yet
# reached the China warehouse. Only these justify "ยังไม่ถึงโกดังจีน" as a
# fact; a status outside BOTH sets is genuinely undefined for this
# question and must be reported verbatim, never auto-concluded.
_WAREHOUSE_NOT_ARRIVED_STATUS_RE = re.compile(
    r"ยังไม่.{0,6}(?:ถึง|เข้า|รับเข้า).{0,6}โกดัง|รอ.{0,6}(?:เข้า|รับเข้า)โกดัง|ก่อนเข้าโกดัง|ยังไม่ได้เข้าโกดัง")
_STATUS_METHOD_ROAD_RE = re.compile(r"ทางรถ|โดยรถ|(?<![ก-๙])รถ(?![ก-๙])")
_STATUS_METHOD_SEA_RE = re.compile(r"ทางเรือ|โดยเรือ|(?<![ก-๙])เรือ(?![ก-๙])")
_ROAD_ETA_LINE = "หากจัดส่งทางรถปกติใช้เวลาประมาณ 7-10 วันนับจากเข้าโกดังจีนค่ะ"
_SEA_ETA_LINE = "หากจัดส่งทางเรือปกติใช้เวลาประมาณ 14-20 วันนับจากเข้าโกดังจีนค่ะ"
_BOTH_ETA_LINE = "หากจัดส่งทางรถปกติใช้เวลาประมาณ 7-10 วัน และทางเรือประมาณ 14-20 วันนับจากเข้าโกดังจีนค่ะ"


def _shipment_mapped_value(full_mapped, response_mapping, suffix):
    """The mapped_label + value of the searchdatashipment field whose
    json_path ends with `.suffix` (e.g. "code" / "status" / "trackingth")
    — config-driven, never a hardcoded display label string."""
    label = next((r.get("mapped_label") for r in (response_mapping or [])
                  if (r.get("json_path") or "").lower().endswith("." + suffix)), None)
    if not label:
        return None
    return (full_mapped or {}).get(label)


def _compose_shipment_followup_reply(message: str, full_mapped, response_mapping,
                                     recent_user_turns=None) -> Optional[str]:
    """A direct, natural answer to the customer's ACTUAL shipment
    follow-up question (ETA / warehouse arrival / current stage), or
    None when the message doesn't match one of those shapes — the
    caller then falls through to the existing generic composer
    unaffected. Never fabricates a confirmed arrival date or a shipping
    method the ERP data doesn't actually state."""
    bill = _shipment_mapped_value(full_mapped, response_mapping, "code")
    if not bill:
        return None
    status = (_shipment_mapped_value(full_mapped, response_mapping, "status") or "").strip()
    t = message or ""

    # CUSTOMER-G12-WAREHOUSE-ARRIVAL-RECEIVED-1 — the fresh-query shape
    # ("สินค้าถึงโกดังหรือยัง" -> ask for the bill -> bare "FT..." reply)
    # splits the question from the turn that actually executes the ERP
    # read: the bare-bill turn carries no follow-up-question shape, so
    # recover it from the most recent user turn that DID (LATEST wins).
    # ONLY the warehouse / ETA / current-stage shapes are recovered here
    # (a Thai-tracking-output ask has its OWN dedicated recent-turn
    # handler at the call site, which also keeps the status line). Uses
    # ONLY the existing canonical status semantics — no new taxonomy.
    _shape_res = (_ETA_QUESTION_RE, _WAREHOUSE_QUESTION_RE, _CURRENT_STATUS_QUESTION_RE)
    _cur_has_shape = (any(rx.search(t) for rx in _shape_res)
                      or (_PSI_THAI_TRACKING_OUTPUT_RE.search(t) and "จีน" not in t))
    if not _cur_has_shape:
        for _prev in (recent_user_turns or []):
            if any(rx.search(_prev) for rx in _shape_res):
                t = _prev
                break

    if _ETA_QUESTION_RE.search(t):
        if status:
            status_line = f"อยู่สถานะ{status}แล้วค่ะ"
        else:
            status_line = "ยังไม่มีข้อมูลสถานะที่ยืนยันในระบบค่ะ"
        if _STATUS_METHOD_SEA_RE.search(status) and not _STATUS_METHOD_ROAD_RE.search(status):
            eta_line = _SEA_ETA_LINE
        elif _STATUS_METHOD_ROAD_RE.search(status) and not _STATUS_METHOD_SEA_RE.search(status):
            eta_line = _ROAD_ETA_LINE
        else:
            eta_line = _BOTH_ETA_LINE
        return f"ตอนนี้บิล {bill} {status_line} ยังไม่มีวันเข้าไทยที่ยืนยันในระบบ {eta_line}"

    if _WAREHOUSE_QUESTION_RE.search(t):
        if not status:
            return f"ยังไม่มีข้อมูลสถานะที่ยืนยันสำหรับบิล {bill} ในระบบค่ะ"
        # NOT-arrived is checked first: "ยังไม่เข้าโกดังจีน" also matches the
        # arrived pattern's "เข้าโกดังจีน" substring, and the explicit
        # negation must win.
        if _WAREHOUSE_NOT_ARRIVED_STATUS_RE.search(status):
            return f"ยังไม่ถึงโกดังจีนค่ะ ตอนนี้บิล {bill} อยู่สถานะ{status}ค่ะ"
        if _WAREHOUSE_ARRIVED_STATUS_RE.search(status):
            return f"ถึงโกดังจีนแล้วค่ะ ตอนนี้บิล {bill} อยู่สถานะ{status}ค่ะ"
        # PHASE-5 D5 — a status in neither trusted set: report it, do NOT
        # conclude "ยังไม่ถึงโกดังจีน" (over-commit on an undefined status).
        return (f"ตอนนี้บิล {bill} อยู่สถานะ{status}ค่ะ "
                f"ยังไม่สามารถยืนยันการรับเข้าโกดังจีนจากสถานะนี้ได้ค่ะ")

    if _PSI_THAI_TRACKING_OUTPUT_RE.search(t) and "จีน" not in t:
        tracking_th = (_shipment_mapped_value(full_mapped, response_mapping, "trackingth") or "").strip()
        if tracking_th:
            return f"บิล {bill} มีเลข Tracking ไทยแล้วค่ะ: {tracking_th}"
        return f"ตอนนี้บิล {bill} ยังไม่มีเลข Tracking ไทยในระบบค่ะ"

    if _CURRENT_STATUS_QUESTION_RE.search(t):
        if not status:
            return f"ยังไม่มีข้อมูลสถานะที่ยืนยันสำหรับบิล {bill} ในระบบค่ะ"
        return f"ตอนนี้บิล {bill} อยู่สถานะ{status}ค่ะ"

    return None

_PSI_DOMAIN_ACTION_HINTS = {
    "shipment": {"cat_key": ("shipment", "parcel", "ขนส่ง", "พัสดุ"), "track_hint": ()},
    "order": {"cat_key": ("order", "คำสั่งซื้อ", "สั่งซื้อ", "ออเดอร์"), "track_hint": ()},
    "tracking": {"cat_key": ("tracking", "แทรค", "แทร็ก", "shipment"), "track_hint": ("track",)},
    "customer_data": {"cat_key": ("customer data", "customer_data", "wallet", "ลูกค้า", "บัญชี", "finance"),
                      "track_hint": ()},
}


# SEM-1.2 — a message that is ONLY a social greeting / acknowledgement,
# carrying no task content. Such a turn must never be consumed as a
# pending-workflow parameter value or treated as a continuation. Broader
# than hybrid_question_classifier._GREETING_RE (kept as an OR fallback):
# also covers "ดีครับ", a trailing "ผม/นะ/จ้า", and a bare "ขอบคุณ".
_SOCIAL_ONLY_RE = re.compile(
    r"^\s*(สวัสดี|หวัดดี|ดีครับ|ดีค่ะ|ดีจ้า|ดีคับ|hello+|hi+|hey+|halo|โย่|ทัก)"
    r"[\s\.!,~ครับคับค่ะคะจ้าาๆนะฮะฮ่ะผมโว้ยว้อย]*$", re.IGNORECASE)


def _is_social_only(message: str) -> bool:
    t = (message or "").strip()
    if not t:
        return False
    return bool(_SOCIAL_ONLY_RE.match(t) or _GREETING_RE.match(t))


# SEM-1.2 — an explicit cancellation of the in-progress collection. Same
# vocabulary rag/slot_filling_flow._CANCELLATION_RE uses for the public
# calculator flow, plus the "ไม่เช็ก/ไม่ต้อง...แล้ว" shapes seen on LINE.
_CONVERSATION_CANCEL_RE = re.compile(
    r"ไม่คำนวณแล้ว|ไม่เช็ก?แล้ว|ไม่เช็คแล้ว|ไม่ต้องแล้ว|ไม่เอาแล้ว|ยกเลิก|พอแล้ว|เลิกเช็ก|หยุดก่อน|เปลี่ยนเรื่อง")


def _psi_is_list_shaped(action: Dict) -> bool:
    """A customer-scoped 'list/latest' Business Action (as opposed to a
    per-record 'detail' action). Same test _resolve_private_state_action
    uses internally, exposed for the SEM-1.1 selection-time redirect."""
    return "list" in (action.get("action_key") or "").lower() \
        or "list" in (action.get("category") or "").lower()


def _resolve_private_state_action(registry, domain: str, record_scope: str = "UNSPECIFIED",
                                   message: str = ""):
    """Pick the EXISTING enabled Business Action that serves a private
    status inquiry for `domain` at the given semantic `record_scope`
    (SEM-1.1) — never an invented action, never a hardcoded key.

    - LATEST / LIST_ALL           -> a customer-scoped "list" action is
                                     fine (returns the latest / the set).
    - UNSPECIFIED / EXPLICIT_RECORD -> prefer the per-record "detail"
                                     action so its collection flow asks
                                     for the record identifier instead of
                                     silently returning a latest record.
    - customer_data                -> the account action regardless of
                                     scope (it is scoped by the customer
                                     identifier itself, not a per-record id).
    - tracking + a Thai-tracking-OUTPUT ask (CUSTOMER-TRACK-TH-1, see
      _PSI_THAI_TRACKING_OUTPUT_RE) -> the per-shipment DETAIL action
      (its response already returns TrackingTH), never the China-
      tracking-number-search action `track_hint` would otherwise prefer.

    Returns a bare `business_actions` row or None when nothing matches
    (caller then keeps current behaviour)."""
    hints = _PSI_DOMAIN_ACTION_HINTS.get(domain)
    if not hints:
        return None
    try:
        rows = [a for a in registry.list()
                if a.get("enabled") and not a.get("deleted_at")
                and (a.get("action_type") in ("API", "WEBHOOK", "TOOL"))]
    except Exception:
        return None
    matched = [a for a in rows
               if any(h.lower() in f"{a.get('category') or ''} {a.get('action_key') or ''} "
                                   f"{a.get('name') or ''}".lower()
                      for h in hints["cat_key"])]
    if not matched:
        return None

    def _is_list(a):
        return "list" in (a.get("action_key") or "").lower() or "list" in (a.get("category") or "").lower()

    if domain == "tracking":
        _wants_thai_output = bool(_PSI_THAI_TRACKING_OUTPUT_RE.search(message or "")) \
            and "จีน" not in (message or "") \
            and not any(_validate_generic_identifier(tok) and not tok.isdigit()
                        for tok in _TOKEN_SPLIT_RE.split(message or "") if tok)
        if _wants_thai_output:
            shipment_detail = [a for a in matched
                               if "shipment" in (a.get("action_key") or "").lower() and not _is_list(a)]
            if shipment_detail:
                return shipment_detail[0]
        else:
            track = [a for a in matched if any(h in (a.get("action_key") or "").lower() for h in hints["track_hint"])]
            if track:
                return track[0]

    if domain == "customer_data":
        non_list = [a for a in matched if not _is_list(a)]
        return (non_list or matched)[0]

    if record_scope in ("LATEST", "LIST_ALL"):
        lst = [a for a in matched if _is_list(a)]
        if lst:
            return lst[0]
        return matched[0]

    # UNSPECIFIED / EXPLICIT_RECORD -> detail action (asks for the id)
    detail = [a for a in matched if not _is_list(a)]
    if detail:
        return detail[0]
    # No detail action configured for this domain -> nothing safe to
    # force; let the caller fall through (RAG / clarification) rather
    # than auto-return a latest record the customer never asked for.
    return None


def _resolve_conversation_reference(registry, message: str, customer_context: Dict,
                                     *, psi_domain_match: bool = False) -> Optional[Dict]:
    """Last-resort resolution for a follow-up message that carries NO
    Business-Action-selecting signal of its own (no keyword/pattern
    match — this is only ever called from decide() after a normal fresh
    search already came up empty) but the conversation clearly isn't
    over — the profile remembers which Business Action the customer was
    last using (profiles/manager.py's own Identifier Memory persistence,
    extended to also remember `last_business_action`), and ANY of:

    (a) the message names a specific FIELD that action's own response
    mapping is configured to return (Requested Field Filtering's own
    field_metadata.keywords vocabulary, reused here as evidence of
    "still talking about the same record" — e.g. "มีคูปองไหม" /
    "เบอร์โทรอะไร" after GetDataCustomer. Fully config-driven: ANY field
    any action's response_mapping names works automatically, never a
    fixed sentence list), or

    (b) it matches a generic topic-free referring-expression marker (see
    _REFERENCE_MARKER_RE, e.g. "แล้วของถึงหรือยัง"), or

    (c) `psi_domain_match=True` — the caller's own deterministic
    private-state classifier (services/decision_engine.py
    `_classify_private_state_inquiry`) ALREADY decisively named this
    message a same-domain, UNSPECIFIED-scope status inquiry against an
    established single-record referent (CUSTOMER-RED-HOTFIX-2: "สินค้า
    จะเข้าไทยตอนไหนคะ" / "สินค้าถึงโกดังหรือยังคะ" right after a
    successful searchdatashipment lookup — neither phrasing matches
    _REFERENCE_MARKER_RE, but a decisive same-domain private-record
    classification is at least as strong evidence of "still talking
    about the same record" as that marker regex is). The caller is
    responsible for only ever passing True when it has ALSO confirmed
    the remembered action is a per-record DETAIL action, never a LIST
    action (see _psi_is_list_shaped) — this function never re-derives
    that itself.

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
    marker_match = bool(_REFERENCE_MARKER_RE.search(message or "")) or psi_domain_match
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

# CUSTOMER-CSW9-REAL-2 / PHASE-6D — real-LINE operational-verb
# misspellings are now handled by the shared safe Thai chat-typo
# normalizer (services/thai_text_normalizer.py::_CHAT_FORMS), invoked
# once at the top of decide(). This literal ("เปเลี่ยน" -> "เปลี่ยน") was
# folded into that map. Kept here only as the historical anchor for the
# CSW9 root cause; not read anywhere.


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


# OWNER-REAL-LINE-FIX-05 — active-frame conversation-act replies. No
# business truth, no policy, no staff-referral: a correction / cancel /
# clarification inside the import-discovery journey.
_FRAME_CANCEL_REPLY = (
    "ได้ค่ะ ยกเลิกรายการนี้ให้นะคะ 😊 ถ้าต้องการให้ช่วยเรื่องอื่น แจ้งได้เลยค่ะ"
)
_FRAME_CORRECTION_CLARIFY_REPLY = (
    "ได้ค่ะ ต้องการเปลี่ยนสินค้า จำนวน หรือวิธีจัดส่งดีคะ?"
)


def _safe_fallback_response(reason: str) -> Dict:
    # PHASE-6E — natural no-information wording. No staff-referral clause:
    # this path is NOT a handoff (routing_type stays SAFE_FALLBACK), and
    # the conversation is kept open so the customer can continue.
    return {
        "text": "เรื่องนี้ตอนนี้ยังไม่มีข้อมูลที่ยืนยันได้ค่ะ ถ้ามีรายละเอียดเพิ่มเติม "
                "หรืออยากให้ช่วยเรื่องอื่น บอกได้เลยนะคะ",
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


# ── PPC-1: referent-less underspecified question -> CLARIFY ─────────────
# An INTERROGATIVE message that, once every GENERIC light verb / pronoun
# / degree word / politeness particle / bare demonstrative / bare
# attribute head is removed, names NO concrete product, service,
# quantity, attribute, topic or identifier of its own:
# "สั่งเยอะได้ไหม", "ราคาเท่าไหร่", "มีไหม", "อันนี้ได้ไหม".
# A fresh RAG search on such a bare string just retrieves
# whatever FAQ chunk is its closest lexical neighbour (confirmed live:
# "สั่งเยอะได้ไหม" pulled a battery-prohibition FAQ chunk and the answer
# wove it in). When the immediate conversation supplies no concrete
# referent either, the honest move is one natural clarifying question —
# not a guess. NOT a phrase list: the test is structural (nothing
# topical survives the generic-token strip), so "สั่งรองเท้าเยอะได้ไหม",
# "ค่าส่งเท่าไหร่", "คูปองใช้ยังไง" all keep a real remnant and are
# untouched.
_PPC_GENERIC_TOKEN_RE = re.compile(
    r"(ผม|ฉัน|ดิฉัน|หนู|เรา|คุณ|"
    r"ครับ|คับ|ค่ะ|คะ|นะ|น่ะ|จ๊ะ|จ้า|ฮะ|ๆ|"
    r"ที|หน่อย|ด้วย|ได้|สัก|นิด|บ้าง|อีก|เลย|แล้ว|ยัง|"
    r"สั่ง|มี|เอา|ทำ|เช็ก|เช็ค|เชค|ดู|ขอ|ให้|อยาก|ต้องการ|สนใจ|"
    r"เยอะ|มาก|น้อย|หมด|เท่าไหร่|เท่าไร|กี่|"
    r"ไหม|มั้ย|มัั้ย|ยังไง|ยังงัย|อย่างไร|หรือเปล่า|รึเปล่า|หรอ|เหรอ|"
    r"อันนี้|อันนั้น|อันไหน|แบบนี้|แบบนั้น|แบบไหน|ตรงนี้|ตรงนั้น|นี้|นั้น|นี่|"
    r"ราคา|รายละเอียด|ข้อมูล|"
    r"\s|\?|!|\.|,|~|ๆ)+",
    re.IGNORECASE)
# Deliberately INTERROGATIVE-only — a bare request verb ("ขอข้อมูล",
# "เช็กให้หน่อย") is not enough on its own to force a clarification here;
# the message must actually be phrased as a question.
_PPC_INTERROGATIVE_RE = re.compile(
    r"ไหม|มั้ย|เท่าไหร่|เท่าไร|กี่|ยังไง|ยังงัย|อย่างไร|หรือเปล่า|รึเปล่า|เหรอ|หรอ|อันไหน",
    re.IGNORECASE)
# Transport-mode / logistics / time / greeting vocabulary that, on its
# own in the PRECEDING turn, is NOT a product/service referent for an
# ordering question ("ทางเรือกี่วัน" before "สั่งเยอะได้ไหม" does not tell
# us WHAT the customer wants to order).
_PPC_NON_REFERENT_TOPIC_RE = re.compile(
    r"ทางเรือ|ทางรถ|ทางอากาศ|ทางบก|เรือ|รถ|อากาศ|ขนส่ง|โลจิสติกส์|"
    r"โกดัง|คลัง|สาขา|วัน|กี่วัน|ระยะเวลา|นาน|เวลา|คิว|รอบ|"
    r"สวัสดี|หวัดดี|ขอบคุณ|เบอร์|ติดต่อ|แอดมิน|เจ้าหน้าที่", re.IGNORECASE)


def _ppc_topical_remnant(text: str) -> str:
    """What survives stripping every generic (non-topical) token — a
    non-empty result means the message names something concrete."""
    return _PPC_GENERIC_TOKEN_RE.sub("", text or "").strip()


def _is_referentless_underspecified(message: str) -> bool:
    t = (message or "").strip()
    if not t or len(t) > 24:
        return False
    if not _PPC_INTERROGATIVE_RE.search(t):
        return False
    remnant = _ppc_topical_remnant(t)
    # nothing topical left, and no digit / identifier hiding in it
    if remnant:
        return False
    return True


# An explicit "I want / I'm interested in / import / order" marker in the
# PRECEDING turn is what turns a topical noun there into a referent an
# underspecified follow-up may inherit. A prior bare status question
# ("ของผมเข้าไทยหรือยัง") or a logistics fact ("ทางเรือกี่วัน") carries no
# such marker and does NOT establish a product referent.
_PPC_REFERENT_INTENT_RE = re.compile(
    r"สนใจ|อยาก|ต้องการ|อยากได้|นำเข้า|สั่งซื้อ|สั่งของ|ฝากสั่ง|ฝากนำเข้า|จะสั่ง|จะนำเข้า|ขนของ|ชิปปิ้ง")


def _recent_product_referent(history: Optional[List[Dict]]) -> bool:
    """True when the immediately preceding user turn established a
    concrete product/service referent an underspecified follow-up can
    legitimately inherit ("สนใจนำเข้ารองเท้า" -> "สั่งเยอะได้ไหม"). Needs
    BOTH an interest/import/order marker AND a residual topical noun, so
    a prior status question or logistics fact never counts."""
    if not history:
        return False
    prev_users = [t.get("content") or "" for t in history if t.get("role") == "user"][-2:]
    for msg in reversed(prev_users):
        if not _PPC_REFERENT_INTENT_RE.search(msg):
            continue
        remnant = _PPC_NON_REFERENT_TOPIC_RE.sub("", _ppc_topical_remnant(msg)).strip()
        remnant = _PPC_REFERENT_INTENT_RE.sub("", remnant).strip()
        if len(remnant) >= 3:
            return True
    return False


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
    _self_registered = bool(_SELF_REGISTERED_RE.search(text)) \
        or bool(_REFERENCE_MARKER_RE.search(text))

    # P1.2A informational transport comparison / multi-facet question
    # (2026-09-01 hotfix) — "รถกับเรืออันไหนถูกกว่า" hits no
    # _QUESTION_MARKER_RE ("อันไหน…กว่า") so it fell to the AMBIGUOUS
    # early-return below; "ทางรถกับทางเรือราคาเท่าไหร่ ใช้กี่วัน" has a
    # question marker but two clauses, so the multi-clause branch of
    # private_action_evidence tagged it PRIVATE_ACTION. Both are company
    # shipping rate/duration questions about transport MODES carrying no
    # self-reference / identifier / reference marker, yet neither reached
    # SHIPIFY_INFORMATION — so exclude_private was never set and a verified
    # customer's stale last_business_action could resurrect an identity-
    # gated ERP lookup (real LINE regression). Reuse the P1.2A RequestSpec
    # (never a new parser): >=2 transport modes AND (a cheaper/faster
    # comparison OR a rate/duration/minimum facet) AND no private evidence
    # of any kind -> a confirmed informational/RAG turn.
    if not _self_registered and not _PRIVATE_STATE_QUERY_RE.search(text):
        try:
            from rag.query_resolution import decompose_request as _p12a_decompose
            _p12a = _p12a_decompose(text, raw_question=text)
            _p12a_info = (
                len(_p12a.transport_modes) >= 2
                and (_p12a.comparison in ("cheaper", "more_expensive", "faster", "slower")
                     or any(f in ("rate", "duration", "minimum") for f in _p12a.facets))
                and not any(_validate_generic_identifier(tok) and not tok.isdigit()
                            for tok in _TOKEN_SPLIT_RE.split(text) if tok))
        except Exception:
            _p12a_info = False
        if _p12a_info:
            return "SHIPIFY_INFORMATION"

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

# SYSTEM-STATE-EMERGENCY-1 — CENTRAL current-turn authority.
#
# `_ACTIONABLE_INTENT_FAMILIES` = every central-interpreter family that
# names a concrete, actionable customer request. A DECISIVE one of these
# in the CURRENT message (follow_up_op == "NONE", confidence >= 0.55)
# that is NOT the family a pending flow serves, and is NOT a value that
# flow's own slot extractor accepts, is an explicit NEW intent that must
# beat stale unrelated pending state (a sticky charter / calculator /
# operational / Business-Action collection). GENERAL / UNKNOWN /
# IMPORT_INTEREST stay ambiguous and never break a flow on their own; a
# follow-up op (SET_VALUE / CORRECTION / COMPARISON / CONTINUE) always
# CONTINUES the active flow.
_ACTIONABLE_INTENT_FAMILIES = frozenset({
    "SHIPPING_ESTIMATE", "CHARTER_TRUCK", "PICKUP_LOCATION", "SELF_PICKUP",
    "COUPON_USAGE", "MY_COUPONS", "PRODUCT_POLICY", "INVOICE",
    "SHIPMENT_STATUS", "ADDRESS_CHANGE", "LINK_CONVERSION",
    "PURCHASE_WITHDRAWAL", "SHIPPING_WITHDRAWAL",
    # PHASE 6 POST-DEPLOY (defect class B) — both cancellation families
    # are decisive reads. Before this pass these same turns were named
    # PURCHASE_WITHDRAWAL by the gated LLM and were therefore ALREADY in
    # this set; keeping them here preserves the existing break semantics
    # while fixing which family they are.
    "CANCELLATION_POLICY", "CANCELLATION_OPERATION",
    # REAL LINE 2026-09-17 (P0) — omitting a newly-added family from this
    # set is silently invisible everywhere it is consulted: a payment
    # question asked while a DIFFERENT pending collection (e.g. an
    # unanswered private-status CustCode ask) was already active got
    # swallowed as an attempted answer to THAT collection instead of
    # being read as its own decisive intent -- "จ่ายบิลสั่งซื้อยังไง"
    # answered "กรุณาแจ้งรหัสลูกค้าค่ะ" instead of the payment steps, then
    # escalated once the SAME stale CustCode retry count saturated. No
    # Business Action of its own (same reasoning as PURCHASE_WITHDRAWAL
    # above), so it is also in _FLOW_ONLY_INTENT_FAMILIES below.
    "PURCHASE_BILL_PAYMENT",
})
# The subset that is NEVER itself a Business-Action collection — used
# where the pending flow's own family is unknown (a generic
# `_resolve_continuation_action` continuation), so a same-domain
# SHIPMENT_STATUS / INVOICE / MY_COUPONS / ADDRESS_CHANGE elaboration is
# NOT misread as a topic change (the existing question-marker / RAG_ONLY
# diversion guards still own those). LINK_CONVERSION (CUSTOMER-LINK-1) is
# excluded for the SAME reason as SHIPMENT_STATUS/INVOICE: it IS itself
# an ordinary Business-Action collection (geturlproductdetail's own
# pending url-ask), so a same-family elaboration (e.g. a second pasted
# URL) must not misread as breaking its own continuation. A genuinely
# DIFFERENT pending flow (charter/calculator/shipment/...) is still
# correctly broken by a decisive LINK_CONVERSION read via the topical-
# evidence diversion check just below (geturlproductdetail's own
# keyword/description overlap) and via `_ACTIONABLE_INTENT_FAMILIES` at
# the charter/calculator branches' own bespoke break-checks.
_FLOW_ONLY_INTENT_FAMILIES = frozenset({
    "SHIPPING_ESTIMATE", "CHARTER_TRUCK", "PICKUP_LOCATION",
    "SELF_PICKUP", "COUPON_USAGE", "PRODUCT_POLICY",
    # CUSTOMER-RED-REAL-FAIL-1 (RED-5/RED-6) — neither withdrawal family
    # has its own Business-Action collection today (no executable
    # ERP write action exists for either), so — like COUPON_USAGE /
    # PRODUCT_POLICY — a decisive read of one always counts as breaking
    # some OTHER stale pending flow (charter/operational/calculator/a
    # Business-Action collection); there is no "own continuation" of
    # theirs to protect.
    "PURCHASE_WITHDRAWAL", "SHIPPING_WITHDRAWAL",
    # a cancellation POLICY question has no collection of its own (it is
    # answered from the approved policy statement in one turn), so it is
    # flow-only. CANCELLATION_OPERATION is deliberately EXCLUDED: it IS
    # its own operational collection.
    "CANCELLATION_POLICY",
    # REAL LINE 2026-09-17 (P0) — PURCHASE_BILL_PAYMENT is a one-turn
    # deterministic reply with no Business-Action collection of its own,
    # same reasoning as PURCHASE_WITHDRAWAL above.
    "PURCHASE_BILL_PAYMENT",
})


# SYSTEM-STATE-EMERGENCY-1 — a clear, non-charter QUESTION shape. Used as
# a last-resort break signal for the (history-sticky) charter collection
# when the LLM family read is degraded/GENERAL but the turn is plainly
# not a charter slot answer.
_CHARTER_NONCONTINUATION_Q_RE = re.compile(
    r"ไหม|มั้ย|กี่วัน|กี่ชั่วโมง|กี่บาท|เท่าไหร่|เท่าไร|ยังไง|ยังงัย|อย่างไร|"
    r"ตรงไหน|ที่ไหน|อยู่ไหน|แถวไหน|หรือยัง|รึยัง|ได้ไหม|ออกได้|ใช้ยังไง|ขอ.*หน่อย")


def _current_intent_breaks_pending_flow(semantic, message, *, flow_family=None,
                                         flow_extract=None, families=None) -> bool:
    """The ONE central arbitration rule, applied BEFORE every flow-local
    pending-state continuation (charter / operational / calculator /
    Business-Action collection). Returns True when the CURRENT message
    carries a decisive NEW actionable intent the pending flow must NOT
    consume.

    A turn CONTINUES the flow (returns False) when: it is that flow's own
    family; it carries a follow-up op; the interpreter could only reach
    an ambiguous GENERAL / UNKNOWN / low-confidence read; or the flow's
    own slot extractor accepts the message as a value.
    """
    fam = getattr(semantic, "intent_family", "UNKNOWN")
    op = getattr(semantic, "follow_up_op", "NONE")
    conf = float(getattr(semantic, "confidence", 0.0) or 0.0)
    # An EXPLICIT topic switch ("ไม่เอาแล้ว ขอถามเรื่อง…", "เปลี่ยนไปถาม…",
    # "กลับมาถาม…" — the deterministic _TOPIC_RE) is decisive and must be
    # checked BEFORE the same-family early return: after a completed
    # link-conversion / import turn the interpreter's family label often
    # stays STUCK to that flow's family from the history frame, so a plain
    # `fam == flow_family` check would wrongly keep the stale flow. It
    # still CONTINUES the flow only when the switch phrase is also a valid
    # slot value for it (flow_extract).
    if op == "TOPIC_CHANGE":
        if flow_extract is not None:
            try:
                if flow_extract(message):
                    return False
            except Exception:
                pass
        return True
    if fam == flow_family:
        return False
    # If the message is itself a valid slot value for THIS flow, it
    # continues it — checked first so a value that merely happens to be
    # TOPIC_CHANGE-shaped ("งั้นเอาทางเรือดีกว่า") is not misread as a
    # switch.
    if flow_extract is not None:
        try:
            if flow_extract(message):
                return False
        except Exception:
            pass
    # CORE-CONVERSATION — an EXPLICIT topic switch (_TOPIC_RE) is handled
    # at the top of this function now, ahead of the same-family early
    # return, so a switch after a completed link/import turn breaks the
    # stale flow even when the family label is still stuck to it.
    # CORRECTION / COMPARISON / CONTINUE / SET_VALUE / CONFIRMATION still
    # continue — those refine the SAME flow.
    # PHASE-6B — an explicit REJECT of the previous answer, or a decisive
    # PUBLIC company-information turn (contact / website / warehouse-inbound
    # / policy / how-to), is never a slot value for a pending collection —
    # it breaks the flow so the turn is re-evaluated fresh (fixes the
    # reported "asks for CustCode again -> loop").
    if getattr(semantic, "conversation_act", "NONE") == "REJECT" and op != "CORRECTION":
        return True
    # a decisive NEW PHASE-6B public-info turn (public contact / website
    # link / warehouse-inbound journey) is never a slot value and never a
    # same-domain elaboration of any pending collection — it breaks it so
    # the turn is answered fresh (fixes the "asks CustCode again" loop).
    if fam in ("CONTACT_INFO", "WEBSITE_LINK_REQUEST", "WAREHOUSE_INBOUND_JOURNEY") and conf >= 0.6:
        return True
    if op != "NONE":
        return False
    if fam not in (families or _ACTIONABLE_INTENT_FAMILIES) or conf < 0.55:
        return False
    return True


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

    # Subsumed-Keyword Discriminator (P8.3.1, config-driven) — the SAME
    # shared rule the Hybrid Question Classifier applies to its own
    # `close` set (services/action_selection_primitives.
    # resolve_subsumed_keyword_tie). Without this, a top-`_score` tie that
    # exists ONLY because one action's generic one-word keyword is a
    # substring of another action's longer configured phrase (which the
    # customer actually typed) was silently resolved by returning
    # candidates[0] — so `classify_question` could correctly pick the
    # specific-record action while this independent path re-selected the
    # summary action from the same false substring tie. Now both layers
    # break that tie identically. Only touches a genuine top-`_score`
    # tie; a candidate with real independent evidence (category /
    # identifier pattern / collected-slot params) already outscores the
    # tie and is never reordered. No literal phrase, action key, or
    # action type here — purely each action's own configured keywords.
    if len(scored) > 1:
        _top_score = scored[0]["_score"]
        _tied = [a for a in scored if a["_score"] == _top_score]
        if len(_tied) > 1:
            _win_id = resolve_subsumed_keyword_tie(_tied, message)
            if _win_id is not None and scored[0]["id"] != _win_id:
                scored = ([a for a in scored if a["id"] == _win_id]
                          + [a for a in scored if a["id"] != _win_id])
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
            # PHASE-6D — safe Thai chat-typo normalization applied ONCE
            # here so every downstream consumer (semantic interpreter,
            # keyword action search, operational classifier, pre-RAG
            # flows, RAG query seed) sees the typo-tolerant form. The RAW
            # message stays authoritative for identifiers / URLs / numbers
            # / amounts / dimensions — the normalizer freezes every such
            # structured token verbatim and reverts the whole result on
            # any Semantic Invariant Guard violation. Supersedes the
            # former 1-entry _OPERATIONAL_TYPO_FIXES map (folded into
            # services/thai_text_normalizer.py::_CHAT_FORMS).
            try:
                from services.thai_text_normalizer import normalize_message as _normalize_message
                _norm = _normalize_message(message)
                # RAW stays authoritative for audit / ERP / identifiers —
                # kept verbatim in context for any downstream consumer.
                context.setdefault("raw_user_message", _norm.raw)
                if _norm.changed:
                    developer_trace["typo_normalized"] = {
                        "from": _norm.raw, "to": _norm.normalized,
                        "corrections": _norm.corrections,
                        "protected_tokens": _norm.protected_tokens,
                        "confidence": _norm.confidence,
                    }
                    message = _norm.normalized
                elif _norm.rejected:
                    developer_trace["typo_normalization_rejected"] = _norm.rejection_reason
            except Exception as _e:
                developer_trace["typo_normalization_error"] = str(_e)

            # 1-2. read message + history are inputs; 3. conversation state
            # is recomputed from `history` (no separate persistence layer
            # in this codebase — same convention every other
            # conversation-intelligence module here already follows).

            # SEMANTIC-FIRST-1 — ONE central semantic interpretation, at
            # the very start of conversational decision processing. Every
            # downstream flow consumes this normalised
            # {intent_family, entities, is_private, follow_up_op} contract
            # (services/conversation_semantics.py::interpret) instead of
            # re-classifying the raw Thai message. Deterministic
            # compositional meaning model + one gated LLM disambiguation
            # that degrades to the deterministic result. Structural inputs
            # (bare ids / URLs / numeric-only / postbacks) skip it.
            semantic = _interpret_message(message, history, context)
            developer_trace["semantic_interpretation"] = semantic.as_dict()

            # INVOICE-PRODUCT-REGRESSION-2 (Problem B) — the central layer
            # recognised a bare product NAME given in reply to the
            # assistant's own "what product?" question. Normalise it to the
            # canonical import-eligibility form so the existing, well-tested
            # prohibited-goods / product-policy retrieval path decides the
            # verdict — the trusted policy, never the semantic layer, and
            # never a company-fact no-info / Human CS just because the noun
            # is novel. Structural rewrite only; the entity is preserved in
            # `semantic` for the answerability-gate safe-ack path.
            if (semantic.intent_family == "PRODUCT_POLICY"
                    and semantic.follow_up_op == "SET_VALUE"
                    and (semantic.entities or {}).get("product")):
                _prod = semantic.entities["product"]
                message = f"{_prod}นำเข้าได้ไหม"
                developer_trace["semantic_rewrite"] = message

            # stash for _run_rag_pipeline -> run_playground_turn so the RAG
            # answer-planner intent is the SAME central result (context is
            # a per-call copy — no leak to the caller).
            context["_semantic_interpretation"] = semantic

            # 4. Intent resolution — determines CUSTOMER INTENT only. It
            # narrows candidate Business Actions (via `workflow` below,
            # still used as a category-matching score signal); it no
            # longer defines required slots/parameters for anything that
            # has a configured Business Action — see the Single Source of
            # Truth refactor note at the top of this module. The regex
            # classifier is now the FALLBACK — `semantic.intent_family`
            # drives it whenever it is not UNKNOWN.
            actionable_intent = classify_actionable_intent(message, interpretation=semantic)
            workflow_hint = resolve_active_erp_intent(history, message)
            developer_trace["intent"] = actionable_intent
            developer_trace["workflow"] = workflow_hint

            # SYSTEM-WIDE CONVERSATION INTELLIGENCE — P1. Build the ONE
            # canonical ConversationResolution here, once, by composing
            # the trusted capabilities already computed above. SHADOW-
            # FIRST: it is logged structurally and only low-risk
            # conversational branches (currently: the FIX-05 frame
            # correction / reject / clarify block) read it — auth / ERP
            # truth / business policy / money / confirmed execution are
            # untouched, no DB / schema change (P2, not yet approved).
            try:
                from services.conversation_resolution import resolve_conversation as _resolve_conversation
                resolution = _resolve_conversation(
                    message, history, context, semantic=semantic,
                    normalized_text=message,
                    has_pending_workflow=bool(context.get("pending_action_id")))
                developer_trace["conversation_resolution"] = resolution.as_dict()
            except Exception as _e:            # never let the shadow layer break a turn
                resolution = None
                developer_trace["conversation_resolution_error"] = str(_e)

            # SYSTEM-WIDE CONVERSATION INTELLIGENCE — P2 SHADOW-WRITE.
            # Build the structured conversation frame from the P1
            # resolution + the previously-persisted frame (passed in by
            # the channel adapter as context["conversation_frame"]). It
            # is logged with its parity vs the legacy text-derived frame
            # and returned in the result for the adapter to persist —
            # NOTHING routes off it (legacy derive_active_frame stays
            # authoritative until shadow parity >= 99%).
            structured_frame = None
            try:
                if resolution is not None:
                    from services.conversation_frame_store import build_frame as _build_frame, frame_parity as _frame_parity
                    from services.conversation_semantics import derive_active_frame as _legacy_frame
                    _prev_frame = context.get("conversation_frame")
                    structured_frame = _build_frame(_prev_frame, resolution)
                    _parity = _frame_parity(_legacy_frame(history), structured_frame, resolution)
                    # P2.1 SHADOW OBSERVABILITY — a purely diagnostic flag
                    # (never read by routing): the requested slot did not
                    # advance between the previous persisted frame and
                    # this one, and this turn was not an answer/correction
                    # to it. Computed here only because both frames are
                    # already in scope; changes no behaviour.
                    _known_slot_reask = bool(
                        _prev_frame and _prev_frame.get("requested_slot")
                        and _prev_frame.get("requested_slot") == structured_frame.get("requested_slot")
                        and resolution.conversation_act not in ("ANSWER", "CORRECTION"))
                    developer_trace["conversation_frame"] = {
                        "version": structured_frame.get("version"),
                        "journey": structured_frame.get("journey"),
                        "status": structured_frame.get("status"),
                        "requested_slot": structured_frame.get("requested_slot"),
                        "slots": structured_frame.get("slots"),
                        "turn_seq": structured_frame.get("turn_seq"),
                        "source": "conversation_resolution",
                        "parity_with_legacy": _parity,
                        "known_slot_reask": _known_slot_reask,
                    }
            except Exception as _e:
                developer_trace["conversation_frame_error"] = str(_e)
            # surfaced for the channel adapter's post-decide persistence.
            context["_structured_conversation_frame"] = structured_frame
            # P2.1 SHADOW OBSERVABILITY — which channel/caller produced
            # this turn, set explicitly by the caller (line_bot/webhook.py
            # -> "REAL_LINE", Admin Hybrid-Playground auto mode ->
            # "ADMIN_AUTO"); every other caller (manual playground modes,
            # every test/harness) leaves it unset -> "OTHER". Never
            # inferred from message content. Used only by
            # services/conversation_intelligence_telemetry.py to keep
            # test/synthetic traffic out of the live cutover denominator.
            developer_trace["sample_source"] = context.get("sample_source") or "OTHER"

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

            # SEM-1.2 (2026-09-03) — a pending workflow is CONTEXT, not
            # ownership of every later message. Compute the private-state
            # frame and the social-greeting flag up front so the
            # continuation / pending-resume / conversation-reference
            # resolution below can refuse to consume this turn as stale-
            # workflow input when the message is self-contained:
            #   • a social/greeting-only message ("สวัสดีครับ") is never a
            #     pending-parameter value — REAL LINE: it was consumed by
            #     an active ShipmentCode collection and re-asked.
            #   • a self-contained private-record status inquiry with
            #     UNSPECIFIED record scope ("ของผมเข้าไทยหรือยัง" — no
            #     identifier, no "ล่าสุด"/list scope) is a fresh request,
            #     not a continuation and not a reference to a remembered
            #     list action — REAL LINE: conversation_reference
            #     resurrected searchdatashipmentlist and returned latest.
            # A bare identifier / weight / value reply never fires
            # _classify_private_state_inquiry, so a genuine continuation
            # ("FT318…", "12 กก.") is unaffected.
            private_state_inquiry = _classify_private_state_inquiry(message)

            # SYSTEM-STATE-EMERGENCY-1 (2nd blocker) — a bare possessive /
            # demonstrative follow-up with NO resolvable referent
            # ("ของผมล่ะ", "แล้วอันนี้ล่ะ", "อันนั้นของผมล่ะ") must NEVER
            # default to a customer-profile / wallet / account-summary
            # ERP read. A private ERP read requires BOTH an authorized
            # identity AND an explicit or recent-context-resolved private
            # intent — a possessive pronoun alone is neither. REAL LINE:
            # "ของผมล่ะ" (right after a charter slot prompt) returned the
            # full profile + Purchase Wallet + email + phone. Resolve it
            # against the last few assistant turns: only when one
            # genuinely offered a specific private-data topic does the
            # normal private-state path continue; otherwise ASK which
            # data they mean. Runs BEFORE _resolve_conversation_reference
            # / the private-state consumption / getdatacustomer.
            if _ELLIPTICAL_NO_REFERENT_RE.match((message or "").strip()):
                _recent_asst_txt = " ".join(
                    (t.get("content") or "") for t in (history or [])[-4:]
                    if t.get("role") == "assistant")
                if not _PRIVATE_REFERENT_OFFER_RE.search(_recent_asst_txt):
                    developer_trace["referentless_private_clarify"] = True
                    return self._finalize(
                        reply=_build_response(text=(
                            "ขออภัยค่ะ ไม่แน่ใจว่าหมายถึงข้อมูลส่วนไหน "
                            "รบกวนระบุเพิ่มเติม เช่น สถานะสินค้า คูปอง หรือยอด Wallet ค่ะ")),
                        routing_type="WORKFLOW", workflow=None,
                        developer_trace=developer_trace, context=context, start=start,
                        alert=_detect_alert(message, context))

            # SEMANTIC-FIRST-2 — a novel / short paraphrase of a
            # shipment/order STATUS question ("ร้านส่งของออกมายัง",
            # "ต้นทางส่งมาหรือยัง", "ฝั่งร้านปล่อยของหรือยัง", "ส่งยังครับ")
            # that the deterministic private-state recognizer's
            # record-noun regex did not catch, but the ONE central
            # semantic interpreter did (SHIPMENT_STATUS). Treat it as the
            # SAME private status inquiry so it enters the matching
            # Status-Inquiry Business Action's collection flow — ask for
            # the bill / tracking / order id — instead of falling through
            # to RAG -> Answerability Gate -> Fix-2 -> Human CS. The
            # semantic layer only NAMES the intent; authorization,
            # shipment ownership and the ERP status stay deterministic
            # downstream.
            #
            # PHASE 6 FINAL SAFETY CLOSURE: the LLM family name is EVIDENCE,
            # never authorization. This block used to fire on that name alone
            # unless the wording matched an enumerated public-exception list;
            # it now requires POSITIVE private-ownership evidence, and the
            # exception lists are deleted. A remembered last_business_action is
            # still left to the conversation-reference resolver below (a genuine
            # continuation, not a fresh synthesized inquiry).
            _sf2_evidence = _private_ownership_evidence(
                message, history, customer_context)
            if (private_state_inquiry is None
                    and semantic.intent_family == "SHIPMENT_STATUS"
                    and not customer_context.get("last_business_action")
                    and _sf2_evidence is not None):
                _sf2_has_id = any(
                    _validate_generic_identifier(tok) and not tok.isdigit()
                    for tok in _TOKEN_SPLIT_RE.split(message or "") if tok)
                _sf2_domain = "order" if _SELLER_DISPATCH_RE.search(message or "") else "shipment"
                private_state_inquiry = {
                    "domain": _sf2_domain, "intent": "status_inquiry",
                    "ownership_scope": "USER_PRIVATE",
                    "record_scope": "EXPLICIT_RECORD" if _sf2_has_id else "UNSPECIFIED",
                    "confidence": round(max(getattr(semantic, "confidence", 0.0) or 0.0, 0.7), 2),
                    "source": "semantic_first_2",
                    "ownership_evidence": _sf2_evidence,
                }
                developer_trace["private_state_inquiry_semantic_synth"] = private_state_inquiry

            # Only the per-record domains have a list-vs-detail record-scope
            # hazard; customer_data (wallet/coupon) is account-scoped by the
            # customer id itself, so a remembered customer lookup may still
            # be resumed for it.
            #
            # CUSTOMER-RED-HOTFIX-2 (SHIPMENT REFERENT RULE) — this
            # suppression's own original purpose (see the comment two
            # blocks up) was to stop a NAKED "ของผมเข้าไทยหรือยัง"-style
            # question, with NO established single-shipment context, from
            # wrongly resurrecting a remembered LIST action's "latest"
            # record via _resolve_conversation_reference. It was never
            # meant to also block reusing an ALREADY-ESTABLISHED single
            # shipment (a real successful searchdatashipment lookup just
            # now) for an immediate same-domain follow-up that happens to
            # use the same UNSPECIFIED-scope wording ("สินค้าจะเข้าไทย
            # ตอนไหนคะ", "สินค้าถึงโกดังหรือยังคะ" — REAL LINE: both
            # re-asked for the bill instead of reusing it). Exempt exactly
            # the case the original fix targeted: only suppress when the
            # remembered last_business_action is itself LIST-shaped (or
            # nothing is remembered at all) — an established DETAIL
            # referent (searchdatashipment, not searchdatashipmentlist) is
            # real, decisive evidence, not a coincidental list resurface.
            # CUSTOMER-CHARTER-COMBINE-1.1 hotfix — the check above only
            # verified the remembered action was NOT list-shaped, never
            # that it was even the SAME DOMAIN as this private_state_
            # inquiry. REAL regression: a remembered "getdatacustomer"
            # (customer-data domain, not list-shaped) got treated as an
            # "established detail referent" for a completely unrelated
            # tracking/shipment inquiry, letting _resolve_conversation_
            # reference's marker bypass resurrect getdatacustomer itself
            # for "ขอแทรคไทยค่ะ" instead of asking for the shipment bill.
            # Precise fix: the referent only counts when it IS the exact
            # action this domain would itself resolve to (the SAME
            # config-driven lookup _resolve_private_state_action uses
            # everywhere else here) — never merely "some non-list action,
            # any domain".
            _psi_established_detail_referent = False
            _psi_last_action_key = customer_context.get("last_business_action")
            if _psi_last_action_key and private_state_inquiry:
                try:
                    _psi_referent_target = _resolve_private_state_action(
                        self.registry, private_state_inquiry["domain"],
                        record_scope=private_state_inquiry.get("record_scope", "UNSPECIFIED"),
                        message=message)
                except Exception:
                    _psi_referent_target = None
                if (_psi_referent_target is not None
                        and _psi_referent_target.get("action_key") == _psi_last_action_key
                        and not _psi_is_list_shaped(_psi_referent_target)):
                    _psi_established_detail_referent = True
            _psi_self_contained = (
                bool(private_state_inquiry)
                and private_state_inquiry.get("record_scope") == "UNSPECIFIED"
                and private_state_inquiry.get("domain") in ("shipment", "order", "tracking")
                and not _psi_established_detail_referent)
            _social_only = _is_social_only(message)
            # REAL LINE 2026-09-16 (P0) — EXPLICIT CURRENT NEW-JOURNEY INTENT
            # outranks every pending workflow / calculator / collection
            # (services/conversation_semantics.py::is_new_journey_opener,
            # the ONE journey boundary). A pending Business-Action
            # collection, charter or operational thread must never
            # continue over "อยากสั่งของจากจีน 20 คู่".
            _new_journey_turn = _is_new_journey_opener(message or "")
            _suppress_stale_workflow = _psi_self_contained or _social_only or _new_journey_turn
            if _suppress_stale_workflow:
                developer_trace["stale_workflow_suppressed"] = (
                    "private_state_self_contained" if _psi_self_contained
                    else "social_greeting" if _social_only else "new_journey_opener")

            # New execution order: Search Candidate Business Actions ->
            # Select Best Business Action -> Read Business Action
            # Parameters -> Information Collection -> Execute. Selection
            # happens BEFORE information collection, so the Registry's
            # own parameter definitions — not a separate intent-keyed
            # schema — drive what gets asked.
            continuation_action = None if _suppress_stale_workflow else _resolve_continuation_action(
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
            if not continuation_action and not _suppress_stale_workflow:
                pending_action_id = context.get("pending_action_id")
                if pending_action_id:
                    pending_full_action = self.registry.get_full(pending_action_id, mask_secrets=False)
                    if pending_full_action and pending_full_action.get("enabled"):
                        continuation_action = pending_full_action

            # CUSTOMER-CSW9-REAL-3 — a message that itself classifies as a
            # fresh ADDRESS_CHANGE request-opener (a change VERB on an
            # address OBJECT — "ต้องการเปลี่ยนที่อยู่จัดส่ง" / "ขอเปลี่ยน
            # ที่อยู่จัดส่งอีกบิล" / "เปลี่ยนที่อยู่หน่อย") is the customer
            # RE-STARTING the request, never a bare answer to whatever
            # param the abandoned prior collection last asked for. Left
            # as a "continuation", the history replay hydrates stale
            # fields from the earlier episode still in the window (real
            # LINE: a fresh CSW9 opener came back with ShipmentCode
            # already filled from a previous abandoned attempt's bill
            # answer, so it skipped straight to asking for the receiver).
            # A DATA answer ("ผู้รับ มานะ ...", "เปลี่ยนเป็นชลบุรี", a bare
            # value) never carries the ADDRESS_CHANGE family, so a genuine
            # mid-collection turn is untouched. Overrides even a caller-
            # supplied pending row — a fresh opener abandons it by design.
            if (continuation_action
                    and continuation_action.get("action_key") == "requestshippingaddresschange"
                    and _is_fresh_address_change_opener(message, semantic)):
                developer_trace["pending_flow_broken_by_current_intent"] = \
                    "continuation->fresh_ADDRESS_CHANGE_request"
                continuation_action = None

            # PHASE-1-STATE-PRECEDENCE-ROOT-FIX-2 — a message that itself
            # decisively OPENS an operational-change kind ("เพิ่ม VAT ให้
            # หน่อย" -> add_vat, "แก้จำนวนสินค้าได้ไหม" -> modify_bill_qty,
            # "เปลี่ยนจากเรือเป็นรถได้ไหม" -> change_shipping_method,
            # "เคลมของไม่ครบ" -> missing_item_claim, "สั่งสกรีนโลโก้" ->
            # custom_production) beats a stale, incomplete Business-Action
            # continuation (real LINE: an incomplete
            # requestshippingaddresschange collection swallowed a fresh
            # "เพิ่ม VAT ให้หน่อย" and escalated to Human CS via
            # max_retry_exceeded). CURRENT-message-only, deterministic:
            # classify_operational_request already requires a decisive
            # verb+object (or a self-describing object), so a bare CSW9
            # slot answer ("FT318220260726001", "มานะ", "0899999999", an
            # address block, "ยืนยัน") never matches and a genuine
            # mid-collection turn is untouched. Overrides even a caller-
            # supplied pending row. Address-change openers are handled by
            # the CSW9-REAL-3 guard above (classify_operational_request
            # returns None for anything matching _ADDRESS_CHANGE_RE).
            if (continuation_action
                    and _derive_operational_state(None, message, interpretation=semantic) is not None):
                developer_trace["pending_flow_broken_by_current_intent"] = \
                    "continuation->fresh_operational_request"
                continuation_action = None

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
            if continuation_action and _current_intent_breaks_pending_flow(
                    semantic, message, flow_family=None,
                    families=_FLOW_ONLY_INTENT_FAMILIES):
                # SYSTEM-STATE-EMERGENCY-1 / CALCULATOR-REGRESSION-2 —
                # a decisive NEW deterministic-flow / public-RAG intent
                # (calculator, charter, warehouse, coupon-usage, product
                # policy) in THIS message overrides a stale, unrelated
                # pending Business-Action collection. REAL LINE: after
                # "ร้านส่งหรือยังคะ" left a searchdataorder collection
                # pending, "ช่วยคำนวณค่าส่ง น้ำหนัก 2 โล ขนาด 54x12x43"
                # (a clear SHIPPING_ESTIMATE request, no "…หน่อย" marker)
                # was swallowed as a failed OrderCode answer. Restricted
                # to _FLOW_ONLY_INTENT_FAMILIES so a same-domain
                # SHIPMENT_STATUS / INVOICE elaboration is NOT misread as
                # a topic change (the question-marker diversion guard
                # below still owns those).
                developer_trace["pending_flow_broken_by_current_intent"] = \
                    f"continuation->{getattr(semantic, 'intent_family', None)}"
                continuation_action = None

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

            # SEM-1.2 semantic conversation-operation guard — a pending
            # workflow is only resumed when the CURRENT message behaves
            # like a CONTINUE. Two more operations that must break it
            # (the greeting/social + self-contained-private-state cases
            # were already handled up front):
            #   • CANCEL  — "ไม่เอาแล้ว" / "ยกเลิก" / "ไม่เช็กแล้ว"
            #   • CHANGE_TOPIC to a self-contained request — a concrete
            #     polite ask ("ขอเบอร์ติดต่อ") that classifies RAG_ONLY and
            #     does NOT match the pending STRUCTURED (pattern-validated)
            #     slot's shape. Free-text slots (ReceiverName/Address) keep
            #     accepting a "ขอใส่ชื่อ…"-style answer — this only fires
            #     for pattern-validated identifier slots.
            if continuation_action and _CONVERSATION_CANCEL_RE.search(message or "") \
                    and not _requires_confirmation(continuation_action):
                # Plain (non-confirmation) collection has no cancel path of
                # its own — without this it silently re-asks the pending
                # question. A confirmation-gated action keeps its existing
                # cancel handling (this guard does not touch it).
                developer_trace["conversation_operation"] = "CANCEL"
                reply = _build_response(text="รับทราบค่ะ ยกเลิกรายการนี้ให้แล้วนะคะ หากต้องการเริ่มใหม่แจ้งได้เลยค่ะ")
                return self._finalize(reply=reply, routing_type="WORKFLOW", workflow=workflow_hint,
                                       developer_trace=developer_trace, context=context, start=start)
            if continuation_action and _REQUEST_MARKER_RE.search(message or "") \
                    and not _QUESTION_MARKER_RE.search(message or "") \
                    and not _REFERENCE_MARKER_RE.search(message or "") \
                    and not _COLLECTION_STATUS_QUERY_RE.search(message or ""):
                _c_replay = _replay_business_action_collection(
                    continuation_action, self.registry, history, customer_context)
                _c_next = _next_expected_parameter(continuation_action, self.registry, _c_replay)
                _c_pat = (_c_next or {}).get("validation_pattern")
                if _c_next and _c_pat:
                    _c_toks = [t for t in _TOKEN_SPLIT_RE.split(message or "") if t]
                    try:
                        _c_shape = any(re.compile(_c_pat).match(t) for t in _c_toks)
                    except re.error:
                        _c_shape = any(_validate_generic_identifier(t) for t in _c_toks)
                    if not _c_shape and classify_question(
                            message, self.registry)["classification"] == "RAG_ONLY":
                        developer_trace["conversation_operation"] = "CHANGE_TOPIC"
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

            # Stale-identity-gated-action guard (P1 hotfix, 2026-09-01) —
            # detail_sibling_action is also selected purely from the
            # remembered customer_context["last_business_action"] +
            # Identifier Memory (via _find_detail_sibling, which only ever
            # returns API/WEBHOOK actions), before turn_intent is computed
            # in the else-branch below. Same bypass class as the
            # conversation_reference guard: on an informational turn a
            # remembered identity-gated action must not be auto-selected.
            # classify_turn_intent is a pure, regex-only function (no
            # I/O) so the extra call here is negligible; the "แล้ว…ล่ะ"
            # continuity coercion is not repeated because _DETAIL_INTENT_RE
            # requires the literal word "รายละเอียด", which never co-occurs
            # with that follow-up shape.
            if detail_sibling_action \
                    and detail_sibling_action.get("action_type") in _IDENTITY_GATED_ACTION_TYPES \
                    and classify_turn_intent(message) == "SHIPIFY_INFORMATION":
                developer_trace["stale_identity_gated_action_suppressed"] = \
                    "detail_sibling/informational_turn"
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
                # CUSTOMER-LINK-1 — Product Link Conversion (1688/Taobao/
                # Tmall). Checked FIRST among the bespoke flows (ahead of
                # charter/operational/calculator) so a decisive
                # LINK_CONVERSION read always wins over any of their
                # stale pending state. `interpret()` already requires a
                # genuine signal (explicit verb, a real URL, or a
                # link+platform composite — see conversation_semantics.
                # _compose()'s LINK_CONVERSION branch / the structural
                # bare-URL branch) before ever assigning this family, so
                # reaching it here needs no further gating. Intercepting
                # this early (rather than relying only on the generic
                # candidate search) also sidesteps a real, separate defect
                # confirmed live: classify_turn_intent's SHIPIFY_
                # INFORMATION read excludes every API/WEBHOOK-type action
                # from the candidate pool by action_type alone
                # (_IDENTITY_GATED_ACTION_TYPES), which hid
                # geturlproductdetail for some phrasings even though it no
                # longer requires verified identity (services/
                # authorization_service.py::requires_verified_identity() —
                # confirmed False for this action after the CUSTOMER-
                # LINK-1 registry fix). A pending "please send the link"
                # ask also continues here (never re-asks CustCode, never
                # loses the pending state to an ambiguous reply) unless
                # the SAME central arbitration rule (plus the SAME
                # degraded-LLM question-shape fallback the charter branch
                # below already uses) says the current turn is a
                # decisive, different actionable intent.
                _link_pending_ask = _link_reply_for_state("MISSING_URL")
                _last_asst_turn = next((h.get("content") or "" for h in reversed(history or [])
                                        if h.get("role") == "assistant"), "")
                # PHASE-6B — a pending "send me the link" ask does NOT own
                # a turn that is itself a WEBSITE-link request / another
                # pre-RAG service intent, or an explicit REJECT of that
                # very prompt ("ไม่ค่ะ ขอลิงก์ที่จะเข้าไปดูของ", "คุณไม่
                # เข้าใจลูกค้า"). Without this the clarification was fed
                # back into the URL classifier as MISSING_URL and the
                # same prompt was resent — the reported 3× loop.
                _link_was_pending = (
                    _last_asst_turn.strip() == (_link_pending_ask or "").strip()
                    and semantic.intent_family not in _SERVICE_INTENT_FAMILIES
                    and getattr(semantic, "conversation_act", "NONE") != "REJECT")
                # A LINK_CONVERSION family label after a COMPLETED link turn
                # stays STUCK from the history frame. An explicit topic
                # switch whose message carries NO link-conversion signal of
                # its own ("ไม่เอาแล้ว ขอถามค่าตีลังไม้") must NOT re-fire
                # the link action — only a switch that actually names a
                # link / platform ("…ขอแปลงลิงก์แทน") still does.
                _link_fam_now = (
                    semantic.intent_family == "LINK_CONVERSION"
                    and (getattr(semantic, "follow_up_op", "NONE") != "TOPIC_CHANGE"
                         or _is_link_conversion_signal(message)))
                if _link_fam_now or (
                        _link_was_pending
                        and private_state_inquiry is None
                        and not _current_intent_breaks_pending_flow(
                            semantic, message, flow_family="LINK_CONVERSION",
                            families=_ACTIONABLE_INTENT_FAMILIES)
                        and not _CHARTER_NONCONTINUATION_Q_RE.search(message or "")):
                    _link_action = self.registry.get_by_key("geturlproductdetail")
                    if _link_action is not None and _link_action.get("enabled"):
                        return self._execute_selected_action(
                            _link_action, [_link_action], message, history, context,
                            developer_trace, start, workflow=workflow_hint, intent=None,
                            collected_slots={})

                # CUSTOMER-RAG-2.1 — an active charter-truck (เหมารถ /
                # TC19) collection. TC19's FAQ answer opened it; collect
                # bill / destination / recipient name / phone over one or
                # more turns (any order), ask ONLY the missing fields,
                # then route to the EXISTING Human CS handoff. Runs BEFORE
                # the identifier-driven Business-Action search so a bill
                # id supplied here does NOT resurrect `searchdatashipment`.
                # Deterministic, history-derived — no LLM.
                _charter = None if _new_journey_turn else _derive_charter_state(history)
                if _charter is not None:
                    # SYSTEM-STATE-EMERGENCY-1 — the charter collection
                    # opened by a TC19 FAQ answer used to consume EVERY
                    # later turn until its 4 slots were full (REAL LINE:
                    # "ช่วยคำนวณค่าส่ง…", "ใบกำกับ…", "ผมมีคูปอง…",
                    # "ทางเรือกี่วัน", "อยากเปลี่ยนที่อยู่จัดส่ง",
                    # "ร้านส่งหรือยัง" all got the charter slot prompt).
                    # A turn only CONTINUES charter when it is a plausible
                    # charter slot value, a CHARTER_TRUCK re-trigger, or a
                    # follow-up op. Anything else — a decisive new
                    # actionable family, a confirmed private-state
                    # inquiry, or a clear non-charter question — breaks it
                    # (the central `_current_intent_breaks_pending_flow`
                    # plus the deterministic SEM-1 recognizer, so an LLM
                    # hiccup that reads "ร้านส่งหรือยัง" as GENERAL is
                    # still handled).
                    _charter_slot_value = bool(_extract_charter_fields(message).as_dict())
                    _charter_is_followup = getattr(semantic, "follow_up_op", "NONE") != "NONE"
                    _charter_is_same = getattr(semantic, "intent_family", None) == "CHARTER_TRUCK"
                    # CUSTOMER-G19-SC1-FRESH-CHARTER-TRUCK-1 — a decisive
                    # FRESH operational-change request on THIS message alone
                    # (repack / duplicate_bill / verify_warehouse_address /
                    # add_vat / …, AND combine_bills_charter — which reads
                    # CHARTER_TRUCK to the semantic layer yet is a DIFFERENT
                    # operational kind) always breaks a stale Fresh-Charter
                    # collection, exactly as it breaks a stale operational
                    # collection or a stale Business-Action continuation
                    # (PHASE-1-STATE-PRECEDENCE-ROOT-FIX-1/2). It is NOT a
                    # charter slot value, so this never touches a genuine
                    # slot-answer turn, and a bare charter re-trigger
                    # ("ขอเหมารถอีกที") does not classify as an operational
                    # kind so it still CONTINUES the collection.
                    _fresh_op_breaks_charter = (
                        not _charter_slot_value
                        and _derive_operational_state(
                            None, message, interpretation=semantic) is not None)
                    if _fresh_op_breaks_charter or (
                            not _charter_slot_value and not _charter_is_followup and not _charter_is_same
                            and (_current_intent_breaks_pending_flow(
                                    semantic, message, flow_family="CHARTER_TRUCK")
                                 or private_state_inquiry is not None
                                 or bool(_CHARTER_NONCONTINUATION_Q_RE.search(message or "")))):
                        developer_trace["pending_flow_broken_by_current_intent"] = \
                            f"charter->{getattr(semantic, 'intent_family', None)}"
                        _charter = None
                if _charter is not None:
                    _extract_charter_fields(message, _charter)
                    developer_trace["selection_source"] = "charter_truck_collection"
                    developer_trace["charter_truck_state"] = _charter.as_dict()
                    if not _charter.complete():
                        return self._finalize(
                            reply=_build_response(text=_charter_missing_prompt(_charter)),
                            routing_type="WORKFLOW", workflow=workflow_hint,
                            developer_trace=developer_trace, context=context, start=start,
                            alert=_detect_alert(message, context))
                    # all 4 collected -> hand to Human CS. The neutral
                    # reply carries NO "staff will continue" promise; the
                    # webhook adds it only if the notification actually
                    # goes out (or this episode is already NOTIFIED).
                    developer_trace.setdefault("information_collection_status", {})["collected_parameters"] = {
                        "ShipmentCode": _charter.bill,
                        "CharterDestination": _charter.destination,
                        "CharterRecipientName": _charter.recipient_name,
                        "CharterRecipientPhone": _charter.recipient_phone,
                    }
                    return self._finalize(
                        reply=_build_response(text="รับข้อมูลการขอใช้บริการเหมารถครบแล้วค่ะ"),
                        routing_type="HUMAN_HANDOFF", workflow=workflow_hint,
                        developer_trace=developer_trace, context=context, start=start,
                        alert=_detect_alert(message, context),
                        handoff_payload={"reason": "charter_truck_request",
                                         "details": _charter.as_dict(),
                                         "summary": _charter_handoff_summary(_charter)})

                # CUSTOMER-RED-HOTFIX-2 — Purchase Withdrawal (CUS-S05) /
                # Shipping Withdrawal (CUS-S12). Both are plain,
                # non-personalized HOW-TO instructions, so the answer
                # itself lives in the Knowledge Base (services/
                # withdrawal_flow.py fetches it by a STABLE `intent` tag —
                # a direct deterministic lookup, never vector similarity,
                # per the customer source's own explicit instruction not
                # to leave this to "which chunk scores highest"). Checked
                # before the operational-change flow (add_vat etc.) so a
                # withdrawal request is never mistaken for one of those.
                #
                # PHASE-1-STATE-PRECEDENCE-ROOT-FIX-1 — but a decisive
                # CURRENT operational request ("สั่งสกรีนโลโก้" ->
                # custom_production, "เคลมของไม่ครบ" -> missing_item_claim,
                # …) must beat a withdrawal family carried by the gated
                # LLM / history from an earlier withdrawal turn (real
                # LINE: "สั่งสกรีนโลโก้" sent after two withdrawal turns
                # got the purchase-withdrawal KB text back). CURRENT-
                # message-only classification, no history: a genuine
                # withdrawal follow-up ("ถอนยังไง", "กดตรงไหน", "ใช้เวลา
                # กี่วัน", "ถอนเงินขนส่ง") never classifies as an
                # operational kind, so it stays on the withdrawal path.
                _wd_yield_to_op = _derive_operational_state(
                    None, message, interpretation=semantic) is not None
                # CUSTOMER-CSW12-SHIPPING-WITHDRAWAL-SP-1 — a bare "SP" /
                # "FT" answer to the immediately-preceding SP-or-FT
                # question carries no SHIPPING_WITHDRAWAL family of its
                # own, so complete step 2 of the source flow here. A
                # decisive current operational intent still wins
                # (_wd_yield_to_op); a topic switch does not match the
                # brand-token shape and falls through untouched.
                _wd_pending_brand = _shipping_withdrawal_pending_brand_reply(
                    self.registry._sb, history, message)
                if _wd_pending_brand is not None and not _wd_yield_to_op:
                    developer_trace["selection_source"] = "shipping_withdrawal_kb"
                    return self._finalize(
                        reply=_build_response(text=_wd_pending_brand),
                        routing_type="WORKFLOW", workflow=workflow_hint,
                        developer_trace=developer_trace, context=context, start=start,
                        alert=_detect_alert(message, context))
                if semantic.intent_family == "PURCHASE_WITHDRAWAL" and not _wd_yield_to_op:
                    developer_trace["selection_source"] = "purchase_withdrawal_kb"
                    return self._finalize(
                        reply=_build_response(text=_purchase_withdrawal_reply(self.registry._sb)),
                        routing_type="WORKFLOW", workflow=workflow_hint,
                        developer_trace=developer_trace, context=context, start=start,
                        alert=_detect_alert(message, context))
                if semantic.intent_family == "SHIPPING_WITHDRAWAL" and not _wd_yield_to_op:
                    developer_trace["selection_source"] = "shipping_withdrawal_kb"
                    developer_trace["shipping_withdrawal_brand"] = _resolve_shipping_withdrawal_brand(
                        customer_context)
                    return self._finalize(
                        reply=_build_response(text=_shipping_withdrawal_reply(
                            self.registry._sb, customer_context)),
                        routing_type="WORKFLOW", workflow=workflow_hint,
                        developer_trace=developer_trace, context=context, start=start,
                        alert=_detect_alert(message, context))

                # OWNER-REAL-LINE-FIX-05 — a short CORRECTION / REJECTION /
                # AMBIGUOUS turn inside an active IMPORT_INTEREST journey is
                # resolved against the FRAME here, BEFORE the operational-
                # change flow / Business-Action search / RAG. "เปลี่ยนเป็น
                # รองเท้าได้ไหม" while product=ชั้นวางของ is a PRODUCT-slot
                # correction, not a merchandise return/exchange question.
                # The gated LLM resolve_followup() is tried first (open
                # vocabulary); a deterministic, typo-tolerant resolver
                # carries the offline / degraded path. An explicit family
                # (CONTACT_INFO / ADDRESS_CHANGE / LINK_CONVERSION / a
                # withdrawal / a private-state inquiry) still wins — those
                # are excluded here, so §7 "current explicit topic switch
                # wins" holds. Never invents business truth.
                _f5_frame = _derive_active_frame(history)
                _F5_EXPLICIT = {
                    "CONTACT_INFO", "ADDRESS_CHANGE", "LINK_CONVERSION", "SHIPMENT_STATUS",
                    "PURCHASE_WITHDRAWAL", "SHIPPING_WITHDRAWAL", "MY_COUPONS", "INVOICE",
                    "PICKUP_LOCATION", "SELF_PICKUP", "COUPON_USAGE", "CHARTER_TRUCK",
                    "SERVICE_DISCOVERY", "HELP_INTENT", "MONEY_TRANSFER_INTEREST",
                    "WEBSITE_LINK_REQUEST", "WAREHOUSE_INBOUND_JOURNEY", "SHIPPING_ESTIMATE",
                    # REAL LINE 2026-09-17 (P0) — a payment question asked
                    # INSIDE an active import-interest frame must win too,
                    # same reasoning as every other explicit family here.
                    "PURCHASE_BILL_PAYMENT",
                }
                if (_f5_frame and _f5_frame.product
                        and getattr(semantic, "intent_family", "UNKNOWN") not in _F5_EXPLICIT
                        and _classify_private_state_inquiry(message) is None
                        and _wd_pending_brand is None):
                    # P1 — read the correction from the canonical resolution
                    # (same _resolve_frame_correction call, computed once);
                    # fall back to a direct call if the shadow layer errored.
                    _f5 = ((resolution.frame_correction if resolution is not None else None)
                           or _resolve_frame_correction(message, _f5_frame))
                    if _f5["op"] == "REJECT":
                        developer_trace["selection_source"] = "frame_reject_fix05"
                        developer_trace["semantic_op"] = "REJECT"
                        return self._finalize(
                            reply=_build_response(text=_FRAME_CANCEL_REPLY),
                            routing_type="GENERAL", workflow=workflow_hint,
                            developer_trace=developer_trace, context=context, start=start,
                            alert=_detect_alert(message, context))
                    if _f5["op"] == "AMBIGUOUS":
                        developer_trace["selection_source"] = "frame_correction_ambiguous_fix05"
                        developer_trace["semantic_op"] = "AMBIGUOUS"
                        return self._finalize(
                            reply=_build_response(text=_FRAME_CORRECTION_CLARIFY_REPLY),
                            routing_type="GENERAL", workflow=workflow_hint,
                            developer_trace=developer_trace, context=context, start=start,
                            alert=_detect_alert(message, context))
                    if _f5["op"] in ("CHANGE_TARGET", "CORRECT_QUANTITY", "CHANGE_METHOD",
                                     "SET_WEIGHT"):
                        # the deterministic read of an explicit "เปลี่ยนเป็น
                        # X" / "เอ้ย <n>" correction is high-confidence and
                        # decides directly; the gated LLM resolve_followup()
                        # still owns the open-vocabulary follow-ups
                        # ("เสื้อยืดล่ะ") in the SEM-GEN-1 block below.
                        _use5 = _f5
                        developer_trace["semantic_frame"] = _f5_frame.as_dict()
                        developer_trace["semantic_op"] = _use5["op"]
                        developer_trace["selection_source"] = "frame_correction_fix05"
                        if _use5["op"] == "CHANGE_TARGET" and _use5.get("product"):
                            _newp = _use5["product"]
                            if _HIGH_RISK_PRODUCT_RE.search(_newp):
                                # a trusted high-risk category -> FIX-04
                                # canonical form so the trusted policy, not
                                # this layer, returns the verdict.
                                message = f"{_newp}นำเข้าได้ไหม"
                                developer_trace["semantic_rewrite"] = message
                            else:
                                _f5_frame.product = _newp
                                return self._finalize(
                                    reply=_build_response(
                                        text=_frame_ack_reply(_f5_frame, changed="product")),
                                    routing_type="GENERAL", workflow=workflow_hint,
                                    developer_trace=developer_trace, context=context, start=start,
                                    alert=_detect_alert(message, context))
                        elif _use5["op"] in ("CORRECT_QUANTITY", "SET_QUANTITY") and _use5.get("quantity"):
                            _f5_frame.quantity = _use5["quantity"]
                            # PHASE 6 POST-DEPLOY (defect class C) — a new
                            # quantity carries its own unit; keep the frame's
                            # previous unit when the correction stated none.
                            _f5_frame.unit = _use5.get("unit") or _f5_frame.unit
                            return self._finalize(
                                reply=_build_response(
                                    text=_frame_ack_reply(_f5_frame, changed="quantity")),
                                routing_type="GENERAL", workflow=workflow_hint,
                                developer_trace=developer_trace, context=context, start=start,
                                alert=_detect_alert(message, context))
                        elif _use5["op"] == "CHANGE_METHOD" and _use5.get("method"):
                            _f5_frame.method = _use5["method"]
                            return self._finalize(
                                reply=_build_response(
                                    text=_frame_ack_reply(
                                        _f5_frame,
                                        changed="none" if _use5.get("method_was_unset")
                                        else "method")),
                                routing_type="GENERAL", workflow=workflow_hint,
                                developer_trace=developer_trace, context=context, start=start,
                                alert=_detect_alert(message, context))
                        elif _use5["op"] == "SET_WEIGHT" and _use5.get("weight") is not None:
                            # WEIGHT-HOTFIX — a typed measurement, SEPARATE
                            # from order quantity (task: "10 กิโลกรัม" must
                            # never be read as, or overwrite, quantity).
                            # frame.product/quantity/method are untouched;
                            # frame_ack_reply's own "weight" wording is
                            # already neutral ("รับทราบค่ะ…") for both a
                            # first answer and a correction, so no
                            # was-unset branching is needed here.
                            _f5_frame.weight = _use5["weight"]
                            _f5_frame.weight_unit = _use5.get("weight_unit") or "กก."
                            return self._finalize(
                                reply=_build_response(
                                    text=_frame_ack_reply(_f5_frame, changed="weight")),
                                routing_type="GENERAL", workflow=workflow_hint,
                                developer_trace=developer_trace, context=context, start=start,
                                alert=_detect_alert(message, context))
                    # CHANGE_BRAND / UNKNOWN -> fall through to ordinary routing.

                # CUSTOMER 6-SOURCE CLOSURE — a PRICE question asked once
                # the import-interest frame already knows the weight maps
                # to the SAME authoritative shipping-cost rule as
                # AI-API-S1-4.0 / TRAIN-03 (the charge is on whichever of
                # weight-kg or CBM-volume is higher — RATES /
                # EstimateState.missing() in services/
                # shipping_estimate_flow.py, the ONE calculator this repo
                # has). Before this, frame_ack_reply's own ladder had
                # nothing left to ask once weight was known, so a price
                # question here ended on a bare acknowledgement with no
                # next step. This asks ONLY the genuinely missing input
                # (dimensions, or method) via the calculator's OWN
                # wording — never a hand-written copy of it, never an
                # invented number — and never re-asks weight, which the
                # seeded EstimateState already carries as known.
                if (_f5_frame and _f5_frame.product and getattr(_f5_frame, "weight", None)
                        and _PRICE_Q_RE.search(message or "")):
                    _price_est = _EstimateState(weight=_f5_frame.weight, method=_f5_frame.method)
                    # REAL LINE 2026-09-16 (final) — the dimensions the
                    # customer already supplied are a known INPUT of this
                    # journey (Frame.dimensions); seed them through the
                    # calculator's OWN parser so a repeated price question
                    # never re-asks a satisfied input. Complete inputs ->
                    # the authoritative calculator recomputes a FRESH
                    # estimate; an old price string is never reused.
                    if getattr(_f5_frame, "dimensions", None):
                        _extract_estimate_fields(
                            f"ขนาด {_f5_frame.dimensions} {getattr(_f5_frame, 'dim_unit', None) or 'cm'}",
                            _price_est)
                    developer_trace["shipping_estimate_state"] = _price_est.as_dict()
                    if _price_est.complete():
                        developer_trace["selection_source"] = "frame_inputs_known_price_recompute"
                        return self._finalize(
                            reply=_build_response(text=_estimate_reply(_price_est)),
                            routing_type="GENERAL", workflow=workflow_hint,
                            developer_trace=developer_trace, context=context, start=start,
                            alert=_detect_alert(message, context))
                    if _price_est.missing():
                        developer_trace["selection_source"] = "frame_weight_known_price_ask"
                        return self._finalize(
                            reply=_build_response(text=_estimate_missing_prompt(_price_est)),
                            routing_type="GENERAL", workflow=workflow_hint,
                            developer_trace=developer_trace, context=context, start=start,
                            alert=_detect_alert(message, context))

                # CUSTOMER-ACTION-1 — an operational CHANGE / VERIFY request
                # on the customer's own record that has NO executable
                # Business Action (แก้จำนวนในบิล / เปลี่ยนวิธีจัดส่ง /
                # เปลี่ยนเป็นรับเอง / บิลซ้ำ / ตรวจสอบที่อยู่โกดังจีน /
                # ยอดเงินเติมแล้วไม่เข้า). Customer-approved behaviour:
                # acknowledge + ask for the ONE required identifier, then
                # hand to Human CS — never a "no information" dead-end,
                # never a fake success, never an unrelated ERP read. A
                # delivery-address change (CUS-S09) is excluded — it keeps
                # its own requestshippingaddresschange flow. Deterministic,
                # history-derived; Semantic-First supplies the intent.
                _opreq = _derive_operational_state(None if _new_journey_turn else history, message, interpretation=semantic)
                _fresh_opreq = _derive_operational_state(None, message, interpretation=semantic)
                if _opreq is not None and _fresh_opreq is not None and _fresh_opreq.kind != _opreq.kind:
                    # CUSTOMER-RED-REAL-FAIL-1 (RED-4/RED-8) — the CURRENT
                    # message, on its own, decisively and independently
                    # classifies as a DIFFERENT operational kind than the
                    # one recovered from a history-sticky ack (e.g. a
                    # custom_production ack still open in history, but
                    # THIS message unambiguously matches combine_bills_
                    # charter's own object marker). The old guard below
                    # only ever asked "is None?" — since a fresh state
                    # existed at all (regardless of KIND), it left the
                    # STALE kind in place, silently re-asking for the
                    # wrong thing. A message that freshly, decisively
                    # matches its OWN kind always wins over a stale one.
                    developer_trace["pending_flow_broken_by_current_intent"] = \
                        f"operational->{_fresh_opreq.kind}"
                    _opreq = _fresh_opreq
                elif _opreq is not None and history and _fresh_opreq is None and (
                        _current_intent_breaks_pending_flow(
                            # PHASE-1-STATE-PRECEDENCE-ROOT-FIX-1 — the
                            # PENDING flow here is a generic operational
                            # kind (missing_item_claim / modify_bill_qty /
                            # add_vat / custom_production / …), whose
                            # family is none of _ACTIONABLE_INTENT_FAMILIES.
                            # Passing flow_family="ADDRESS_CHANGE" made an
                            # incoming ADDRESS_CHANGE opener look like a
                            # same-family continuation of the claim, so it
                            # could NEVER break it (real LINE: an abandoned
                            # missing_item_claim from ~62 min earlier
                            # answered "บิลขนส่ง FTxxx ต้องการเปลี่ยนที่อยู่
                            # จัดส่ง"). flow_family=None lets a decisive
                            # ADDRESS_CHANGE read break the stale
                            # collection; flow_extract still protects a
                            # genuine bare bill / phone slot answer, and
                            # the conf/family gate still protects an
                            # ambiguous GENERAL/UNKNOWN reply.
                            semantic, message, flow_family=None,
                            flow_extract=lambda m: any(
                                _validate_generic_identifier(tok) and not tok.isdigit()
                                for tok in _TOKEN_SPLIT_RE.split(m or "") if tok)
                            or bool(re.search(r"(?<!\d)0\d{8,9}(?!\d)", m or "")))
                        or private_state_inquiry is not None):
                    # SYSTEM-STATE-EMERGENCY-1 — same central guard, but
                    # ONLY when the operational collection is HISTORY-
                    # sticky (a recent ack in `history`) and the CURRENT
                    # message alone would NOT open one: a decisive new
                    # actionable intent then breaks it. A FRESH operational
                    # request ("บิลขนส่ง FT ต้องการเปลี่ยนเป็นรับเอง" —
                    # classify_operational_request already used the
                    # interpretation) is never touched.
                    #
                    # CUSTOMER-CSW3-SHIPPING-METHOD-CHANGE-1 — the semantic-
                    # family check alone missed short private-record
                    # follow-ups the CENTRAL deterministic PSI classifier
                    # (private_state_inquiry, already computed above) DOES
                    # recognize decisively ("ขอแทรคไทยค่ะ" reads UNKNOWN to
                    # the semantic-family layer but is a confirmed tracking-
                    # domain status inquiry) — REAL LINE: it got swallowed
                    # by a still-PENDING (not yet handed off)
                    # change_shipping_method collection instead of asking
                    # for the shipment bill. private_state_inquiry's own
                    # domains (shipment/order/tracking/customer_data) are
                    # never any operational kind's own subject, so its mere
                    # presence is always decisive, unrelated evidence here.
                    developer_trace["pending_flow_broken_by_current_intent"] = \
                        f"operational->{getattr(semantic, 'intent_family', None)}"
                    _opreq = None
                if _opreq is not None:
                    _extract_operational_fields(message, _opreq)
                    developer_trace["selection_source"] = "operational_change_collection"
                    developer_trace["operational_change_state"] = _opreq.as_dict()
                    if not _opreq.has_input():
                        return self._finalize(
                            reply=_build_response(text=_operational_ask_prompt(_opreq)),
                            routing_type="WORKFLOW", workflow=workflow_hint,
                            developer_trace=developer_trace, context=context, start=start,
                            alert=_detect_alert(message, context))
                    developer_trace.setdefault("information_collection_status", {})["collected_parameters"] = {
                        k: v for k, v in {"ShipmentCode": _opreq.bill,
                                          "ShipmentCodeList": ", ".join(_opreq.bills) if _opreq.bills else None,
                                          "CustPhone": _opreq.phone}.items() if v}
                    return self._finalize(
                        reply=_build_response(text=_operational_handoff_reply(_opreq)),
                        routing_type="HUMAN_HANDOFF", workflow=workflow_hint,
                        developer_trace=developer_trace, context=context, start=start,
                        alert=_detect_alert(message, context),
                        handoff_payload={"reason": f"operational_change_request: {_opreq.kind}",
                                         "details": _opreq.as_dict(),
                                         "summary": _operational_handoff_summary(_opreq)})

                # CUSTOMER-CALC-1 — a shipping-cost ESTIMATE request. A
                # recognized calculator intent with a missing input is
                # WORKFLOW / ASK MISSING, never Fix-2 no-info / Human CS.
                # Deterministic multi-turn slot collection (weight, 3
                # dims, ROAD/SEA), remembers supplied values, calculates
                # the moment there is enough data. Runs BEFORE the RAG
                # pipeline so a missing slot can never reach the
                # Answerability Gate. A bare "เรทเท่าไหร่" / "ค่านำเข้า
                # เท่าไหร่" (no value, no calc verb) is NOT opened -> the
                # rate FAQ answers it, unchanged.
                _est = _derive_estimate_state(history, message, interpretation=semantic)
                if _est is not None:
                    _extract_estimate_fields(message, _est)
                    developer_trace["selection_source"] = "shipping_estimate_flow"
                    developer_trace["shipping_estimate_state"] = _est.as_dict()
                    if _est.complete():
                        return self._finalize(
                            reply=_build_response(text=_estimate_reply(_est)),
                            routing_type="GENERAL", workflow=workflow_hint,
                            developer_trace=developer_trace, context=context, start=start,
                            alert=_detect_alert(message, context))
                    return self._finalize(
                        reply=_build_response(text=_estimate_missing_prompt(
                            _est, basis_question=_asks_rate_basis(message))),
                        routing_type=("GENERAL" if _estimate_turn_answers(
                            _est, basis_question=_asks_rate_basis(message))
                            else "WORKFLOW"), workflow=workflow_hint,
                        developer_trace=developer_trace, context=context, start=start,
                        alert=_detect_alert(message, context))

                # SEM-GEN-1 — a SHORT follow-up inside an active
                # IMPORT_INTEREST frame ("ถ้าเป็นกางเกงล่ะ", "200 ตัว",
                # "ไม่ใช่ 200 เอา 300", "ถ้าทางเรือล่ะ", "งั้นถามเรื่องคูปอง
                # ดีกว่า"). Deterministic gate first (an active frame with
                # a product + a follow-up shape + NOT a private-state
                # inquiry + NOT a PPC-1 no-referent turn); a single gated
                # LLM call then resolves the open-vocabulary meaning.
                # CHANGE_TARGET / CHANGE_TOPIC are rewritten to a self-
                # contained message and handed back to the normal pipeline
                # (so FIX-2.3 / prohibited-goods eligibility still decide
                # the business answer — policy is NOT baked in here);
                # quantity / method / continue produce a deterministic
                # frame acknowledgement. Any resolver error -> UNKNOWN ->
                # ordinary routing, unchanged.
                _sem_frame = _derive_active_frame(history)
                if (_sem_frame and _sem_frame.product
                        and _is_frame_followup(message)
                        and _classify_private_state_inquiry(message) is None
                        and not _is_referentless_underspecified(message)):
                    _sem = _resolve_frame_followup(message, _sem_frame)
                    if _sem["op"] == "UNKNOWN":
                        # OWNER-REAL-LINE-FIX-05 — deterministic fallback
                        # when the gated LLM resolver is offline / degraded
                        # / undecided (also the PHASE-6D typo path): a bare
                        # quantity answer, or a "เปลี่ยนเป็น X" correction.
                        _det = _resolve_frame_correction(message, _sem_frame)
                        if _det["op"] in ("CHANGE_TARGET", "CORRECT_QUANTITY",
                                          "SET_QUANTITY", "CHANGE_METHOD", "SET_WEIGHT"):
                            _sem = {**_sem, **_det}
                    developer_trace["semantic_frame"] = _sem_frame.as_dict()
                    developer_trace["semantic_op"] = _sem["op"]
                    _op = _sem["op"]
                    if _op == "CHANGE_TARGET" and _sem["product"]:
                        message = f"สนใจนำเข้า{_sem['product']}"
                        developer_trace["semantic_rewrite"] = message
                    elif _op == "CHANGE_TOPIC":
                        message = _sem.get("topic") or message
                        developer_trace["semantic_rewrite"] = message
                    elif _op in ("SET_QUANTITY", "CORRECT_QUANTITY") and _sem["quantity"]:
                        _sem_frame.quantity = _sem["quantity"]
                        _sem_frame.unit = _sem.get("unit") or _sem_frame.unit
                        return self._finalize(
                            reply=_build_response(text=_frame_ack_reply(_sem_frame, changed="quantity")),
                            routing_type="GENERAL", workflow=workflow_hint,
                            developer_trace=developer_trace, context=context, start=start,
                            alert=_detect_alert(message, context))
                    elif _op == "CHANGE_METHOD" and _sem["method"]:
                        _sem_frame.method = _sem["method"]
                        return self._finalize(
                            reply=_build_response(text=_frame_ack_reply(
                                _sem_frame,
                                changed="none" if _sem.get("method_was_unset") else "method")),
                            routing_type="GENERAL", workflow=workflow_hint,
                            developer_trace=developer_trace, context=context, start=start,
                            alert=_detect_alert(message, context))
                    elif _op == "SET_WEIGHT" and _sem.get("weight") is not None:
                        # WEIGHT-HOTFIX — same typed measurement as the
                        # Fix05 block above; reached only when Fix05 did
                        # not already handle the turn (e.g. no active frame
                        # there yet).
                        _sem_frame.weight = _sem["weight"]
                        _sem_frame.weight_unit = _sem.get("weight_unit") or "กก."
                        return self._finalize(
                            reply=_build_response(text=_frame_ack_reply(_sem_frame, changed="weight")),
                            routing_type="GENERAL", workflow=workflow_hint,
                            developer_trace=developer_trace, context=context, start=start,
                            alert=_detect_alert(message, context))
                    elif _op == "CONTINUE":
                        return self._finalize(
                            reply=_build_response(text=_frame_ack_reply(_sem_frame, changed="none")),
                            routing_type="GENERAL", workflow=workflow_hint,
                            developer_trace=developer_trace, context=context, start=start,
                            alert=_detect_alert(message, context))
                    # UNKNOWN -> fall through to ordinary routing, unchanged.

                # PHASE-6B — pre-RAG conversational / service-intent layer.
                # Runs AFTER every pending-workflow / continuation /
                # estimate / withdrawal / operational resolution has had
                # its turn (each returns above when it owns the message),
                # and BEFORE Business-Action candidate search + RAG. Fixes
                # the customer-reported root cause that a help / service-
                # discovery / money-transfer / website-link / public-
                # contact / warehouse-inbound turn fell straight to
                # KB_NOT_FOUND. Recognition is the ONE central
                # interpreter's (semantic.intent_family /
                # .conversation_act) — never re-classified here.
                _svc_fam = getattr(semantic, "intent_family", "UNKNOWN")
                _svc_act = getattr(semantic, "conversation_act", "NONE")
                _svc_conf = float(getattr(semantic, "confidence", 0.0) or 0.0)
                _svc_ent = dict(getattr(semantic, "entities", {}) or {})
                # LANGGRAPH UPGRADE (cross-turn continuity) — COMPATIBLE
                # KNOWN INFORMATION SURVIVES FOLLOW-UP TURNS. `semantic
                # .entities` is what THIS turn said; the active frame is
                # what the customer already told us. Without this merge a
                # bare product answer ("รองเท้าครับ") arrives carrying only
                # the product, and the reply ladder asks for the quantity
                # the customer supplied one turn earlier — the exact
                # "เปลี่ยนบริบท AI ตอบไม่ได้" complaint. The current turn
                # always wins; only genuinely MISSING slots are filled in.
                # The PRODUCT is deliberately NOT inherited: a fresh
                # explicit opener ("อยากสั่งของจากจีน") must re-establish the
                # journey rather than resume whatever product a previous
                # owner-test round left in history — that is exactly what
                # tests/test_owner_real_line_fix_06.py exists to prevent,
                # and inheriting it here made the bot answer "รับทราบว่า
                # ต้องการนำเข้าเก้าอี้" to a customer who had just started
                # over. Only the measurement slots are carried, and only
                # when this turn did not supply them itself.
                try:
                    # REAL LINE 2026-09-16 — a turn that is ITSELF a fresh
                    # explicit opener (the same detector derive_active_frame
                    # uses as the journey boundary) starts a NEW journey:
                    # nothing measured in the previous one (method / weight)
                    # may be carried into its first acknowledgement either.
                    _svc_frame = None if _is_new_journey_opener(message or "") else _derive_active_frame(history)
                    if _svc_frame is not None and _svc_ent.get("product"):
                        for _fk, _fv in (("quantity", _svc_frame.quantity),
                                         ("quantity_unit", getattr(_svc_frame, "unit", None)),
                                         ("method", _svc_frame.method),
                                         # WEIGHT-HOTFIX — a typed measurement
                                         # carried the same way quantity/
                                         # method already are: never inferred
                                         # from, and never overwrites, them.
                                         ("weight", getattr(_svc_frame, "weight", None)),
                                         ("weight_unit", getattr(_svc_frame, "weight_unit", None))):
                            if _fv and not _svc_ent.get(_fk):
                                _svc_ent[_fk] = _fv
                except Exception:
                    pass
                _msg_has_identifier = any(
                    _validate_generic_identifier(tok) and not tok.isdigit()
                    for tok in _TOKEN_SPLIT_RE.split(message or "") if tok)

                # (a) the customer REJECTED / clarified the previous answer
                # ("ไม่ใช่อันนี้", "ไม่ได้ถามแบบนั้น", "คุณไม่เข้าใจ",
                # "หมายถึง…"). Re-evaluate THIS message; NEVER resend the
                # same reply (the reported 3× repeated-answer loop). A
                # calculator value correction ("ไม่ใช่ 10 กิโล เอา 5") is
                # NOT this — the estimate flow already owns it above, and
                # follow_up_op == CORRECTION excludes it here.
                if (_svc_act == "REJECT" and history
                        and getattr(semantic, "follow_up_op", "NONE") != "CORRECTION"
                        and not private_state_inquiry and not _msg_has_identifier):
                    _rj_reply = _service_intent_reply(
                        _svc_fam, sb=self.registry._sb, entities=_svc_ent) \
                        if _svc_fam in _SERVICE_INTENT_FAMILIES else None
                    # A REJECT that would only re-emit the SAME family reply
                    # the customer just rejected ("ขอลิงก์เว็บ" -> 3 links ->
                    # "ไม่ใช่ค่ะ หมายถึง…" -> "คุณยังไม่เข้าใจ") must NOT
                    # resend it — switch to the neutral clarification ask so
                    # the loop is broken with a genuinely different turn.
                    if _rj_reply is None or _is_repeat_of_last_assistant(history, _rj_reply):
                        _rj_reply = _SERVICE_CLARIFY_REPLY
                    if not _is_repeat_of_last_assistant(history, _rj_reply):
                        developer_trace["selection_source"] = "phase6b_reject_reevaluate"
                        developer_trace["conversation_act"] = "REJECT"
                        return self._finalize(
                            reply=_build_response(text=_rj_reply),
                            routing_type="GENERAL", workflow=workflow_hint,
                            developer_trace=developer_trace, context=context, start=start,
                            alert=_detect_alert(message, context))
                    # would repeat -> fall through to a fresh route.

                # (a2) T01 — a bare affirmation right after the assistant
                # OFFERED help ("มีอะไรให้ช่วยไหมคะ" -> "ใช่ครับ") is the
                # customer accepting the offer: open the conversation,
                # never a KB lookup.
                if (_svc_fam not in _SERVICE_INTENT_FAMILIES
                        and not private_state_inquiry and not _msg_has_identifier
                        and _is_help_affirmation(message, history)):
                    developer_trace["selection_source"] = "phase6b_help_affirmation"
                    return self._finalize(
                        reply=_build_response(text=_SERVICE_HELP_REPLY),
                        routing_type="GENERAL", workflow=workflow_hint,
                        developer_trace=developer_trace, context=context, start=start,
                        alert=_detect_alert(message, context))

                # (b) a decisive pre-RAG service-intent family -> its
                # deterministic reply (help / discovery / money-transfer /
                # website link / public contact / warehouse-inbound
                # honest fallback). WAREHOUSE_INBOUND_JOURNEY additionally
                # escalates to Human CS (no KB chunk exists for it today).
                # A FRESH IMPORT_INTEREST that names a SPECIFIC product
                # ("ต้องการนำเข้าเครื่องจักร") is handled here so it reaches
                # discovery instead of a RAG KB_NOT_FOUND dead-end
                # (T03/T04/T07). A GENERIC "สนใจนำเข้าสินค้า" (no product
                # noun) is left to RAG — the ฝากนำเข้า process content
                # answers it well — and an in-frame follow-up keeps its
                # SEM-GEN-1 path above.
                # A broad "want to order/import goods FROM CHINA (or from
                # 1688/Taobao/Tmall)" with no product noun is a proxy-buy
                # interest that must get the two-path discovery ack (send a
                # link, or say what you want) — NOT a premature link-only
                # RAG answer (OWNER-REAL-LINE-FIX-01). A bare
                # "สนใจนำเข้าสินค้าครับ" (no จีน / no platform) still goes to
                # RAG, whose ฝากนำเข้า process content answers it well.
                _no_rag_question = not re.search(
                    r"ไหม|มั้ย|หรือเปล่า|รึเปล่า|อะไรบ้าง|อะไรบาง|ยังไง|อย่างไร|เท่าไหร่|กี่\S|\?", message or "")
                _broad_china_buy = bool(re.search(
                    # THAI-HUMAN-LANGUAGE — "ส่ง/ขน ของจากจีน" (ship / haul goods
                    # from China) is the same proxy-import interest as สั่ง/ซื้อ.
                    r"(?:อยาก|สนใจ|จะ|ต้องการ|กำลัง)?\s*(?:สั่ง|ซื้อ|หาซื้อ|ช้อป|นำเข้า|ส่ง|ขน)\s*"
                    r"(?:ของ|สินค้า|สิ่งของ|อะไร)?\s*(?:จาก)?\s*"
                    r"(?:(?:ประเทศ)?จีน|1688|เถาเป่า|taobao|tmall|ทีมอลล์|อาลีบาบา)",
                    message or "", re.IGNORECASE)) and _no_rag_question \
                    and "http" not in (message or "").lower() \
                    and getattr(semantic, "follow_up_op", "NONE") == "NONE" \
                    and (_derive_active_frame(history) is None
                         or not _is_frame_followup(message))
                # broad proxy-buy interest: serve the two-path discovery
                # ack even when the interpreter could only reach UNKNOWN /
                # GENERAL / low confidence (a degraded LLM read of
                # "อยากซื้อของจาก 1688 แต่ยังไม่มีลิงก์").
                if (_broad_china_buy and _svc_act != "REJECT"
                        and not private_state_inquiry and not _msg_has_identifier):
                    from services.service_intent_flow import import_interest_reply as _imp_reply
                    developer_trace["selection_source"] = "phase6b_service_intent"
                    developer_trace["service_intent_family"] = "IMPORT_INTEREST"
                    return self._finalize(
                        reply=_build_response(text=_imp_reply(
                            _svc_ent.get("product"), quantity=_svc_ent.get("quantity"),
                            method=_svc_ent.get("method"),
                            unit=_svc_ent.get("quantity_unit"),
                            weight=_svc_ent.get("weight"),
                            weight_unit=_svc_ent.get("weight_unit"))),
                        routing_type="GENERAL", workflow=workflow_hint,
                        developer_trace=developer_trace, context=context, start=start,
                        alert=_detect_alert(message, context))

                # THAI-HUMAN-LANGUAGE — a fresh opener that states a QUANTITY
                # or METHOD but no product ("สนใจนำเข้า 5 ใบ") is real known
                # state too (PHASE-6-SLOT-CONSUMPTION): it gets the same
                # acknowledge-and-ask-product reply as the China-marker
                # shape above, instead of a RAG detour whose generic ack
                # discarded the quantity.
                _svc_pre_rag = _svc_fam in _SERVICE_INTENT_FAMILIES or (
                    _svc_fam == "IMPORT_INTEREST"
                    and getattr(semantic, "follow_up_op", "NONE") == "NONE"
                    and (_svc_ent.get("product") or _svc_ent.get("quantity")
                         or _svc_ent.get("method"))
                    and _no_rag_question
                    # OWNER-REAL-LINE-FIX-06 — a stale frame from earlier
                    # history must not suppress a FRESH IMPORT_INTEREST
                    # opener / product-slot turn; a genuine in-frame
                    # follow-up was already handled by SEM-GEN-1 above.
                    and (_derive_active_frame(history) is None
                         or not _is_frame_followup(message)))
                if (_svc_pre_rag and _svc_conf >= 0.6 and _svc_act != "REJECT"
                        and not private_state_inquiry and not _msg_has_identifier):
                    _svc_reply = _service_intent_reply(
                        _svc_fam, sb=self.registry._sb, entities=_svc_ent,
                        product=_svc_ent.get("product"))
                    if _svc_reply:
                        developer_trace["selection_source"] = "phase6b_service_intent"
                        developer_trace["service_intent_family"] = _svc_fam
                        if _svc_fam == "WAREHOUSE_INBOUND_JOURNEY":
                            return self._finalize(
                                reply=_build_response(text=_svc_reply),
                                routing_type="HUMAN_HANDOFF", workflow=workflow_hint,
                                developer_trace=developer_trace, context=context, start=start,
                                alert=_detect_alert(message, context),
                                handoff_payload={
                                    "reason": "warehouse_inbound_journey_kb_gap",
                                    "summary": "ลูกค้าถามเรื่องการส่งของเข้าคลังจีน ("
                                               + str(_svc_ent.get("wh_kind") or "general")
                                               + ") ซึ่งยังไม่มีข้อมูลใน KB",
                                    "details": {"question": message}})
                        return self._finalize(
                            reply=_build_response(text=_svc_reply),
                            routing_type="GENERAL", workflow=workflow_hint,
                            developer_trace=developer_trace, context=context, start=start,
                            alert=_detect_alert(message, context))

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

                # SEMANTIC-FIRST-2.1 — the ONE central interpretation is
                # the authority on whether this turn is a PUBLIC company-
                # INFORMATION question. When it named a public-information
                # family (warehouse / pickup location, self-pickup
                # permission, coupon USAGE how-to, prohibited-goods,
                # charter service info, invoice / document policy), the
                # turn must never be offered an identity-gated ERP /
                # customer-data Business Action — no matter what the
                # lexical `classify_turn_intent` returned, and regardless
                # of an incidental self-pronoun ("ผมใช้คูปองยังไง" is a
                # how-to, not an account query). Guards, mirroring the
                # public_clarification_continuity coercion below:
                #   • never when the turn already reads PRIVATE_ACTION or
                #     a confirmed private-state inquiry owns it;
                #   • never when the message carries a real account /
                #     order / record identifier (decisive private
                #     evidence — "ขอชื่อคูปองของลูกค้า SP1014");
                #   • never when the message ITSELF decisively resolves to
                #     a configured Business Action (an explicit private
                #     coupon/customer lookup action keyworded on the same
                #     noun still wins its own turn) — checked memory-free.
                if (turn_intent != "PRIVATE_ACTION" and not private_state_inquiry
                        and getattr(semantic, "intent_family", None) in _PUBLIC_INFO_FAMILIES
                        and not any(_validate_generic_identifier(tok) and not tok.isdigit()
                                    for tok in _TOKEN_SPLIT_RE.split(message or "") if tok)):
                    # A DECISIVE public-info reading (the compositional
                    # meaning model scored a full OBJECT+ACTION shape,
                    # confidence >= 0.75) always wins — an identity-gated
                    # ERP action that merely scored on a shared parameter
                    # or a mapped-field keyword is the exact over-reach.
                    # A weaker reading ("คูปอง" alone — could be MY_COUPONS
                    # or a how-to) only coerces when NO Business Action
                    # decisively claims the turn as its own.
                    _sf21_conf = float(getattr(semantic, "confidence", 0.0) or 0.0)
                    _sf21_own = None
                    if _sf21_conf < 0.75:
                        _sf21_own = select_best_action(
                            search_candidate_actions(self.registry, workflow=workflow_hint,
                                                      message=message, collected_slots={}),
                            minimum_score=1.0 if not workflow_hint else 0.5)
                    if _sf21_conf >= 0.75 or _sf21_own is None:
                        turn_intent = "SHIPIFY_INFORMATION"
                        developer_trace["turn_intent_coerced"] = "semantic_public_information_family"

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

                # Public Clarification Continuity (Customer UAT Fix 1 /
                # 1.1 / 1.2, 2026-09-02) — an under-specified reply
                # carrying no question particle, no declarative-intent
                # verb, no self-reference, no current-state query and no
                # reference marker (the shape classify_turn_intent
                # returns "AMBIGUOUS" for) that lands after an assistant
                # turn is, when nothing else owns it, the customer
                # supplying task-specific detail for the PUBLIC question
                # just answered — a measurement, a weight/quantity value,
                # a code the previous answer asked them to provide. None
                # of that is account identity. classify_turn_intent sees
                # the message in isolation and leaves identity-gated
                # (API/WEBHOOK) actions eligible, so without this the
                # bare value lets _resolve_conversation_reference
                # resurrect a remembered private last_business_action and
                # same-customer Identifier Memory then makes it
                # executable — the reported customer-UAT bug (public
                # clarification data mistaken for a private request).
                # Coerce to SHIPIFY_INFORMATION so the SAME exclude_
                # private mechanism keeps the turn on the public RAG /
                # clarification path.
                #
                # Precedence this relies on (all resolved ABOVE, so this
                # `else` runs only when none of them owns the turn):
                #   • a genuine parameter-collection continuation ->
                #     continuation_action (via _resolve_continuation_
                #     action's history replay OR the explicit
                #     context["pending_action_id"] pending_confirmations
                #     row) -> the `if continuation_action` branch;
                #   • a self-service identity-verification continuation
                #     (customer answering _SELF_VERIFY_ASK_PHONE/EMAIL_
                #     TEXT) -> also continuation_action, same branch
                #     (_resolve_continuation_action handles it explicitly);
                #   • a LIST->DETAIL sibling follow-up -> detail_sibling_
                #     action -> the `elif` branch.
                # Fix 1.2 — the only remaining reason to hold back is that
                # the previous turn really was a Business Action's own
                # collection prompt and this reply is answering it in a
                # degraded (no pending_confirmations row) path. That is
                # tested STRUCTURALLY — _assistant_text_is_generated_
                # action_prompt: the previous turn is VERBATIM a
                # generated parameter / confirmation / self-verification
                # question of some enabled action — NOT with the text-
                # shape heuristic _last_assistant_turn_requests_input
                # (any polite "กรุณา…แจ้ง" phrasing), which a PUBLIC RAG
                # answer that asks for more task data in natural language
                # also trips. That false positive is exactly what blocked
                # continuity in production and let the private action
                # resurrect. _last_assistant_turn_requests_input stays
                # unchanged for its other callers (_resolve_continuation_
                # action's replay short-circuit, the self-verify
                # recognizer).
                #
                # Fix 1.1 — "AMBIGUOUS after an assistant turn" alone is
                # still not enough: classify_turn_intent also returns
                # AMBIGUOUS for a fresh, explicitly-phrased Business
                # Action request that merely lacks a Thai question
                # particle (e.g. "ขอเช็ก<record>ครับ"), and that message
                # OWNS its turn. So before coercing, ask the SAME config-
                # driven selector the fresh-routing path below uses —
                # search_candidate_actions + select_best_action at the
                # standard decisive threshold, with the P8.3.1 subsumed-
                # keyword disambiguation it already applies, and an EMPTY
                # collected_slots set so remembered Identifier Memory can
                # never manufacture ownership (identical guard to the
                # diversion / topic-only checks elsewhere in decide()).
                # If the current message alone decisively resolves to a
                # Business Action, leave it for normal fresh routing.
                # Any message carrying its own self-reference / on-file-
                # account / current-state / reference-marker evidence is
                # excluded here and still classifies (and routes) exactly
                # as before.
                if turn_intent == "AMBIGUOUS" and history:
                    _recent_asst = [t.get("content") for t in history
                                    if t.get("role") == "assistant"][-2:]
                    _msg = message or ""
                    # SHIPMENT-DIRECT-ANSWER-1 — a shipment follow-up
                    # question ("แทรคไทยมีหรือยัง") carries none of the
                    # markers above (no "ตอนนี้"/"ถึงหรือยัง"/etc.) and,
                    # memory-free, decisively matches NO Business Action
                    # at the action-keyword level (its only real evidence
                    # is a FIELD-level keyword _resolve_conversation_
                    # reference checks separately) — REAL LINE: it got
                    # coerced to SHIPIFY_INFORMATION and its otherwise-
                    # correct conversation_reference match was then
                    # suppressed by the stale-identity-gated-action guard
                    # right below. These 4 question shapes are exactly as
                    # decisive private evidence as the markers above.
                    _carries_private_evidence = bool(
                        _SELF_REGISTERED_RE.search(_msg)
                        or _PRIVATE_STATE_QUERY_RE.search(_msg)
                        or _REFERENCE_MARKER_RE.search(_msg)
                        or _ETA_QUESTION_RE.search(_msg)
                        or _WAREHOUSE_QUESTION_RE.search(_msg)
                        or _CURRENT_STATUS_QUESTION_RE.search(_msg)
                        or (_PSI_THAI_TRACKING_OUTPUT_RE.search(_msg) and "จีน" not in _msg))
                    _prev_is_action_prompt = any(
                        _assistant_text_is_generated_action_prompt(c, self.registry)
                        for c in _recent_asst)
                    if (_recent_asst and not _carries_private_evidence
                            and not _prev_is_action_prompt):
                        _own_candidates = search_candidate_actions(
                            self.registry, workflow=workflow_hint, message=message,
                            collected_slots={})
                        _own_action = select_best_action(
                            _own_candidates, minimum_score=1.0 if not workflow_hint else 0.5)
                        if _own_action is None:
                            turn_intent = "SHIPIFY_INFORMATION"
                            developer_trace["turn_intent_coerced"] = "public_clarification_continuity"

                # SEM-1 Private-record status inquiry (2026-09-03) — the
                # customer is asking about the STATE of their OWN record /
                # account ("สินค้าถึงโกดังหรือยัง", "ร้านส่งของให้ผมหรือยัง",
                # "ยอดของผมเหลือเท่าไหร่"). The lexical keyword classifier
                # shares zero tokens with any Business Action for these
                # phrasings, so without this the turn falls through to RAG
                # and dead-ends on "ไม่มีข้อมูลยืนยัน". Treat a confirmed
                # private-state inquiry as PRIVATE_ACTION so identity-gated
                # actions stay eligible (fixes the classify_turn_intent
                # mislabel that also excluded a keyword-matched action),
                # and remember the domain so the "no action selected"
                # dead-end path below can force the matching EXISTING
                # Status-Inquiry action's own collection flow instead of
                # RAG. Never fires for a company-subject question, a how-to
                # question, or an operational WRITE request.
                # private_state_inquiry was already computed up front (SEM-1.2)
                if private_state_inquiry:
                    developer_trace["private_state_inquiry"] = private_state_inquiry
                    if turn_intent != "PRIVATE_ACTION" and developer_trace.get("turn_intent_coerced") not in (
                            "public_clarification_continuity", "rag_continuity_followup_over_stale_erp"):
                        turn_intent = "PRIVATE_ACTION"
                        developer_trace["turn_intent_coerced"] = "private_state_inquiry"

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

                # PPC-1 (2026-09-03) — a referent-less underspecified
                # question ("สั่งเยอะได้ไหม", "ราคาเท่าไหร่", "มีไหม") with
                # NO concrete referent in the immediate conversation goes
                # to CLARIFY, not a fresh RAG search whose closest lexical
                # neighbour can be a stale/adjacent FAQ chunk (confirmed
                # live: it wove in an unrelated battery-prohibition FAQ).
                # Current explicit intent > stale history: an immediate
                # product referent ("สนใจนำเข้ารองเท้า" the turn before)
                # suppresses this and lets RAG use that referent.
                if classification["classification"] in ("RAG_ONLY", "UNKNOWN") \
                        and _is_referentless_underspecified(message) \
                        and not _recent_product_referent(history):
                    developer_trace["selection_source"] = "clarification_referentless_underspecified"
                    return self._route_clarification(
                        {"classification": "CLARIFICATION_REQUIRED", "candidate_action_ids": []},
                        message, context, developer_trace, start, workflow=workflow_hint)

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
                # SEM-1.2 — a self-contained UNSPECIFIED private-state
                # inquiry must not resurrect a remembered list/latest
                # action via a bare reference marker ("ของผม" alone).
                _psi_domain_match = bool(
                    private_state_inquiry and not _psi_self_contained
                    and private_state_inquiry.get("record_scope") == "UNSPECIFIED"
                    and private_state_inquiry.get("domain") in ("shipment", "order", "tracking")
                    and _psi_established_detail_referent)
                referenced = None if _psi_self_contained else _resolve_conversation_reference(
                    self.registry, message, customer_context, psi_domain_match=_psi_domain_match)

                # Stale-identity-gated-action guard (P1 hotfix, 2026-09-01)
                # — _resolve_conversation_reference resurrects the
                # remembered customer_context["last_business_action"] from
                # (field-keyword | referring-marker) evidence alone, with
                # no consultation of the current turn's informational
                # intent. Proven live: a hot ERP customer
                # (last_business_action="getdatacustomer", verified
                # cust_code) asking "ขอเบอร์ติดต่อ" / "ใช้คูปองยังไง" /
                # "แล้วจีนล่ะ" — each already classified SHIPIFY_INFORMATION
                # here (turn_intent above) and RAG_ONLY by
                # classify_question — still had getdatacustomer selected
                # via this branch and executed, dumping the customer's own
                # ERP record. Reuse the SAME exclude_private /
                # _IDENTITY_GATED_ACTION_TYPES mechanism already threaded
                # into every fresh candidate search: on an informational
                # turn, a remembered identity-gated (API/WEBHOOK) action is
                # never auto-resurrected — the turn falls through to the
                # normal RAG path below. Genuine pending continuation
                # (continuation_action, handled earlier) is untouched; an
                # explicit private request classifies PRIVATE_ACTION (not
                # SHIPIFY_INFORMATION), so exclude_private is None and this
                # guard is inert for it; a bare identifier reply
                # classifies AMBIGUOUS, likewise inert.
                if referenced and exclude_private \
                        and referenced.get("action_type") in exclude_private:
                    developer_trace["stale_identity_gated_action_suppressed"] = \
                        "conversation_reference/informational_turn"
                    referenced = None

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

                # Bound on every path (the fresh-search branch may re-set
                # it) so the SEM-1 block below can read it unconditionally
                # even when a conversation_reference won without entering
                # the fresh-search branch.
                general_policy_question_vetoed = False
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
                            # SEM-1: a confirmed private-record status
                            # inquiry ("วันนี้มีของเข้าไทยไหมคะ") already
                            # excludes how-to wording — never veto it here.
                            and not private_state_inquiry
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

                # SEM-1 / SEM-1.1 (2026-09-03) — a confirmed private-record
                # status inquiry. Resolve the EXISTING Status-Inquiry
                # action for the detected domain at the detected semantic
                # record scope (generic category/key hints, never a
                # hardcoded id). Gated by the same condition the
                # general-policy veto uses so a genuine how-to / company
                # question is never revived.
                if private_state_inquiry and not general_policy_question_vetoed:
                    _psi_scope = private_state_inquiry.get("record_scope", "UNSPECIFIED")
                    _psi_target = _resolve_private_state_action(
                        self.registry, private_state_inquiry["domain"], record_scope=_psi_scope,
                        message=message)
                    _psi_msg_has_id = any(
                        _validate_generic_identifier(tok) and not tok.isdigit()
                        for tok in _TOKEN_SPLIT_RE.split(message or "") if tok)
                    if _psi_target is not None and not selected:
                        # (a) natural wording matched no Business Action
                        # keyword — force the resolved target so its
                        # collection flow asks for the identifier instead
                        # of falling through to RAG ("ไม่มีข้อมูลยืนยัน").
                        selected = _psi_target
                        candidates = [selected]
                        # CUSTOMER-RED-HOTFIX-2 — same reasoning as the
                        # SEM-1.1 redirect branch below: when the target
                        # this private-state inquiry resolves to IS the
                        # already-established single-record referent
                        # (a real, recent successful lookup for the SAME
                        # detail action), tag it "conversation_reference"
                        # so _handle_dynamic_collection's existing
                        # Identifier Memory recovery reuses the shipment
                        # instead of asking for the bill again (REAL LINE:
                        # "สินค้าถึงโกดังหรือยังคะ" right after a
                        # successful lookup for the same shipment).
                        if (_psi_established_detail_referent
                                and _psi_target.get("action_key") == customer_context.get("last_business_action")):
                            developer_trace["selection_source"] = "conversation_reference"
                        else:
                            developer_trace["selection_source"] = "private_state_inquiry"
                    elif (_psi_target is not None and selected is not None
                          and developer_trace.get("selection_source") == "fresh_search"
                          and _psi_scope == "UNSPECIFIED" and not _psi_msg_has_id
                          and selected.get("id") != _psi_target.get("id")
                          and selected.get("action_type") in ("API", "WEBHOOK")
                          and _psi_is_list_shaped(selected)
                          and not _psi_is_list_shaped(_psi_target)):
                        # (b) SEM-1.1 — keyword search picked a
                        # customer-scoped LIST/latest action for an
                        # UNSPECIFIED single-record inquiry with no
                        # identifier. For a verified customer that action
                        # needs nothing more and would silently return the
                        # latest record (REAL LINE regression:
                        # "ของผมเข้าไทยหรือยัง" -> latest FT318…). Redirect
                        # to the per-record detail action so it asks for
                        # the bill/tracking id. Explicit "ล่าสุด" / whole-
                        # list scope keeps the list action (scope !=
                        # UNSPECIFIED); a genuine continuation was already
                        # resolved above as conversation_reference.
                        developer_trace["private_state_inquiry_redirect"] = {
                            "from": selected.get("action_key"), "to": _psi_target.get("action_key"),
                            "reason": "unspecified_record_scope_would_auto_return_latest"}
                        selected = _psi_target
                        candidates = [selected]
                        # CUSTOMER-RED-HOTFIX-2 — this redirect picks the
                        # right ACTION but, tagged "private_state_inquiry",
                        # is treated downstream as a brand-new collection
                        # (_handle_dynamic_collection's own continuation
                        # gate does not trust this source), so it asks for
                        # the identifier fresh even when one was already
                        # established moments ago (REAL LINE: "สินค้าจะเข้า
                        # ไทยตอนไหนคะ" re-asked for the bill right after a
                        # successful searchdatashipment lookup for the
                        # SAME shipment). When the redirect target IS the
                        # already-established referent, tag it with the
                        # SAME trusted continuation source used everywhere
                        # else (_resolve_conversation_reference's own
                        # "conversation_reference") so Identifier Memory
                        # recovery (_apply_identifier_memory /
                        # _recover_history_identifier) runs normally —
                        # never a new mechanism, just the correct existing
                        # label for genuinely continued evidence.
                        if (_psi_established_detail_referent
                                and _psi_target.get("action_key") == customer_context.get("last_business_action")):
                            developer_trace["selection_source"] = "conversation_reference"
                        else:
                            developer_trace["selection_source"] = "private_state_inquiry"

            if not selected:
                if workflow_hint:
                    # Safe Migration: no Business Action is configured for
                    # this ERP workflow at all -> legacy INTENT_SCHEMAS
                    # fallback, unchanged behavior from before this task.
                    return self._handle_legacy_workflow(
                        workflow_hint, message, history, context, developer_trace, start,
                        reason="no_business_action_for_workflow")
                # Public Task Clarification Continuity (Customer UAT
                # Fix 1.3, 2026-09-02) — this turn was already coerced to
                # SHIPIFY_INFORMATION by Public Clarification Continuity
                # above: it is a bare reply to a PUBLIC informational
                # answer, NOT a customer-service "check this code"
                # message. A dimensions / weight / quantity / product /
                # destination / URL value the customer typed in answer to
                # a public request must not be intercepted here and asked
                # "what entity is this?". Skip BOTH bare-identifier guards
                # below and let it reach the shared RAG pipeline
                # (_route_safe_fallback -> _run_rag_pipeline), where the
                # existing, deterministic Active Slot-Filling Flow
                # (rag/slot_filling_flow.py, already wired into
                # playground_orchestrator) recognises the structural
                # value against the previous dimensions/weight request
                # and asks only for the genuinely still-missing task
                # field — no LLM call, no new slot system, no per-field
                # branch here.
                _public_clarification_turn = (
                    developer_trace.get("turn_intent_coerced") == "public_clarification_continuity")

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
                if bare_value and _validate_generic_identifier(bare_value) \
                        and not _public_clarification_turn:
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
                captured = _opportunistic_identifier_capture(self.registry, message) \
                    if not _public_clarification_turn else None
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

            # PHASE-5 D10 / D13 — a generic ERP order-status READ was
            # selected purely because an INCIDENTAL order identifier is in
            # the message ("ขอใบกำกับของ PO12345", "ขอยกเลิก PO12345").
            # There is no invoice-fetch action and no executable
            # CancelOrder, so this is NOT a status inquiry — divert to the
            # how-to / policy answer (RAG). Scoped hard: only a
            # fresh-search selection of an _ERP_READ_STATUS_ACTIONS action,
            # only when the message is INVOICE-family or a cancellation
            # ask, and only when it carries NO order-status verb of its
            # own (so "ติดตามสถานะ PO12345" still reads the order).
            if (selected.get("action_key") in _ERP_READ_STATUS_ACTIONS
                    and developer_trace.get("selection_source") in ("fresh_search", None)
                    and not _ORDER_STATUS_VERB_RE.search(message or "")
                    and (getattr(semantic, "intent_family", None) == "INVOICE"
                         or _CANCELLATION_REQUEST_RE.search(message or ""))):
                developer_trace["phase5_incidental_identifier_diversion"] = {
                    "from": selected.get("action_key"),
                    "reason": "invoice_or_cancellation_request_with_incidental_order_id"}
                return self._route_safe_fallback(
                    message, history, context, developer_trace, start,
                    reason="phase5_incidental_identifier_diversion_to_rag")

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
        # New-Action vs Continuation (P0-01 real-LINE fix, 2026-09-03) —
        # this turn genuinely CONTINUES an active flow only when the
        # engine selected the action as one of:
        #   • "conversation_continuation" — `_resolve_continuation_action`'s
        #     history replay matched the bot's own last generated
        #     question, or a live pending workflow row is being resumed;
        #   • "conversation_reference_detail" — a LIST→DETAIL sibling
        #     drill-down ("ขอรายละเอียดอันล่าสุด" right after a list), where
        #     the record id the customer is drilling into legitimately
        #     comes from the list they just saw.
        # A fresh explicit request that merely re-selects a remembered
        # action ("ขอเช็กพัสดุเดียวครับ" with no bill number, via
        # fresh_search or a bare last_business_action resurrection) is NOT
        # a continuation — a stale account-scoped RECORD identifier
        # (last_shipment_code / last_order_code / last_tracking) left in
        # session memory must not auto-fill and silently skip the
        # required question. CustCode (the customer's own identity) still
        # fills either way; see _apply_identifier_memory.
        #
        # CUSTOMER-TRACK-TH-1.1 — "conversation_reference" ADDED to this
        # trusted set (REAL LINE regression: "ตอนนี้ถึงไหนแล้วคะ" right
        # after a successful shipment lookup re-asked for the same bill
        # number instead of reusing it). This selection_source is NEVER
        # a "bare resurrection" — both its only two call sites assign
        # `selected = referenced`, and `referenced` is ALWAYS the result
        # of `_resolve_conversation_reference()`, which itself requires
        # either (a) the message names a specific FIELD the remembered
        # action's own response returns, or (b) a topic-free referring-
        # expression marker (`_REFERENCE_MARKER_RE` — "ตอนนี้", "แล้ว...
        # ล่ะ", "ถึงหรือยัง", ...). Both are decisive evidence of a
        # genuine continuation, exactly as strong as the two sources
        # already trusted here. A referentless possessive ("ของผมล่ะ"
        # with no established topic) is unaffected — it never reaches
        # this function at all, short-circuited earlier by the dedicated
        # SYSTEM-STATE-EMERGENCY-1 referent-less-private guard.
        _is_continuation = developer_trace.get("selection_source") in (
            "conversation_continuation", "conversation_reference_detail", "conversation_reference")
        # CUSTOMER-CSW9-REAL-3 — companion to the fresh-ADDRESS_CHANGE
        # continuation break above: when THIS turn is itself a fresh
        # address-change request-opener (semantic family ADDRESS_CHANGE)
        # and not a genuine continuation, the collection MUST start clean.
        # The history replay walk would otherwise still hydrate
        # ShipmentCode / receiver fields from an earlier, abandoned CSW9
        # episode's own question/answer pairs still inside the recent
        # window (the completed-cycle boundary resets only fire once that
        # earlier collection was actually COMPLETE — an abandoned one
        # never is). Replay still runs so CustCode is recovered (it may
        # come from a bare-CustCode history turn, not only memory); every
        # operation-scoped field it hydrated is then dropped just below.
        # This turn's own message and any real pending_parameters are
        # still applied by the normal path afterwards.
        _fresh_ac_opener = (not _is_continuation
                            and full_action.get("action_key") == "requestshippingaddresschange"
                            and _is_fresh_address_change_opener(
                                message, context.get("_semantic_interpretation")))
        collected = _replay_business_action_collection(
            full_action, self.registry, history, customer_context, is_continuation=_is_continuation)
        if _fresh_ac_opener:
            # Keep only the customer's own identity (CustCode — from a
            # verified binding, Identifier Memory, or a bare-CustCode
            # history turn); drop every operation-scoped field replay
            # may have hydrated from an earlier, abandoned CSW9 episode
            # still in the window (ShipmentCode + all address parts).
            collected = {k: v for k, v in collected.items() if k.lower() == "custcode"}
            developer_trace["fresh_address_change_episode"] = True
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
        if context.get("pending_action_id") == action_id and not _fresh_ac_opener:
            # CUSTOMER-CSW9-REAL-3 — a fresh ADDRESS_CHANGE opener
            # abandons the caller's persisted mid-collection snapshot too;
            # only the identity seed carries into the new episode.
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
        collected = _apply_identifier_memory(full_action, collected, customer_context,
                                              is_continuation=_is_continuation, history=history)

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
        # CUSTOMER-CSW9-REAL-5 — a fresh address-change opener starts a
        # NEW episode with a fresh retry budget. `_count_genuine_retries`
        # recomputes the count from `history`, and this long production
        # session's window holds many "กรุณาแจ้งเลขที่บิล/Shipmentค่ะ" asks
        # from earlier, abandoned CSW9 attempts (plus the trailing one
        # with no answer yet) — enough to hit max_retry and escalate the
        # brand-new opener straight to Human CS before it ever asks for
        # the bill. Those asks belong to closed episodes; skip retry
        # accounting entirely on the turn that opens a new one.
        if (not is_complete and next_after and not ambiguous_candidates
                and not _fresh_ac_opener):
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
            _incomplete_tok = _incomplete_shipment_code(
                message, next_after, full_action.get("action_key"))
            if ambiguous_candidates:
                question = f"พบข้อมูล {len(ambiguous_candidates)} รายการค่ะ รบกวนระบุว่าต้องการใช้ค่าใด"
            elif _incomplete_tok:
                # CUSTOMER-ERP-READ-1 state C — a bill-shaped but truncated
                # ShipmentCode: ask for the COMPLETE number (never a
                # generic re-ask, never an ERP call, never Human CS).
                question = _incomplete_identifier_prompt(next_after, _incomplete_tok)
                developer_trace["erp_read_result"] = "malformed_identifier"
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

        # CUSTOMER-ERP-READ-1 state C — a bill-shaped but truncated
        # ShipmentCode that the loose validation_pattern happened to
        # accept must not be sent to the ERP (it would only come back
        # not-found). Ask for the full number. Scoped to the
        # searchdatashipment READ flow / ShipmentCode slot only.
        if full_action.get("action_key") == "searchdatashipment":
            _sc = str(collected.get("ShipmentCode") or "")
            if _sc and re.fullmatch(r"[A-Za-z]{2}\d{6,12}", _sc):
                developer_trace["erp_read_result"] = "malformed_identifier"
                _scp = next((p for p in full_action.get("parameters") or []
                             if (p.get("name") or "").lower() == "shipmentcode"), {})
                return self._finalize(
                    reply=_build_response(text=_incomplete_identifier_prompt(_scp, _sc)),
                    routing_type="WORKFLOW", workflow=workflow_hint,
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
            # IDENTITY-0 (2026-09-03) — the deterministic phone→email
            # verification sequence spans several turns; by the time the
            # customer answers the EMAIL question, the collection replay
            # for this turn has bound `CustEmail` and no longer carries
            # the `CustCode` from the turn that started the sequence.
            # Without recovering it here, this method returns
            # {"resolved": False} and the failed-verification ESCALATION
            # (a real Human CS handoff) is silently dropped — the customer
            # is still denied (the Authorization Gate is untouched), but
            # no staff notification ever fires. Recover the CustCode the
            # sequence was started for from the conversation, using the
            # SAME config-driven structural capture used elsewhere — never
            # a new identifier shape, never a free-text guess.
            for _t in reversed(history or []):
                if _t.get("role") != "user":
                    continue
                _cap = _opportunistic_identifier_capture(self.registry, _t.get("content") or "")
                if _cap.get("CustCode"):
                    cust_code = _cap["CustCode"]
                    break
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

        # D17 / SC2 — the phone→email self-verification sequence escalates
        # to a human on its FIRST failure. If that already ran to
        # completion earlier in THIS conversation (the email question was
        # asked AND the customer answered it), a later private question
        # must NOT restart the whole sequence — the customer is already
        # with CS. Fall through to the unchanged neutral denial instead of
        # re-prompting. The two branches above still run first, so a reply
        # that is genuinely mid-sequence (last_assistant is the phone or
        # email question) is unaffected.
        _hist = history or []
        for _i, _t in enumerate(_hist):
            if (_t.get("role") == "assistant"
                    and (_t.get("content") or "").strip() == _SELF_VERIFY_ASK_EMAIL_TEXT
                    and any(x.get("role") == "user" for x in _hist[_i + 1:])):
                developer_trace["self_verification_already_escalated"] = True
                return {"resolved": False}

        # First time hitting this denial for this CustCode this exchange.
        return {"resolved": False, "reply_text": _SELF_VERIFY_ASK_PHONE_TEXT, "escalate": False}

    def _execute_selected_action(self, selected: Dict, candidates: List[Dict], message: str,
                                  history: List[Dict], context: Dict, developer_trace: Dict, start: float,
                                  *, workflow: Optional[str], intent: Optional[str], collected_slots: Dict) -> Dict:
        alert = _detect_alert(message, context)
        routing_type = selected.get("action_type") or "SAFE_FALLBACK"

        # CUSTOMER-LINK-1 — deterministic pre-execution gate, scoped ONLY
        # to geturlproductdetail (same established pattern as
        # _ERP_READ_STATUS_ACTIONS's own scoped truth-typing below): a
        # missing / malformed / multiple-URLs / unsupported-domain
        # message must get its own distinct, honest reply and NEVER reach
        # the Action Executor at all (no fake success, no CustCode ask,
        # no Human CS for a simple structural link problem). URL parsing
        # is deterministic per the explicit spec constraint ("LLM must
        # NOT decide whether the URL itself is valid") — this never
        # touches an LLM.
        if selected.get("action_key") == "geturlproductdetail":
            _link_req = _classify_link_request(message, history=history)
            _last_asst = next((h.get("content") or "" for h in reversed(history or [])
                               if h.get("role") == "assistant"), "")
            _link_pending = _last_asst.strip() == _link_reply_for_state("MISSING_URL").strip()
            if _link_req["state"] == "MISSING_URL" and not _is_link_conversion_signal(message) \
                    and not _link_pending:
                # Case 6 — a message that only INCIDENTALLY names a
                # platform ("ผมซื้อของใน 1688") reached this branch purely
                # via search_candidate_actions' generic keyword overlap
                # (search_candidate_actions itself deliberately never
                # filters by action_key — see its own docstring); the
                # central semantic interpreter agrees this carries no
                # genuine link-conversion signal (no verb, no URL, no
                # pending url-ask), so this is NOT auto-treated as a
                # conversion request. Never calls the converter, never
                # invents a link-conversion-specific reply for an
                # unrelated turn.
                developer_trace["link_conversion_state"] = "NO_GENUINE_SIGNAL"
                reply, _reason = _safe_fallback_response("link_conversion_no_genuine_signal")
                return self._finalize(reply=reply, routing_type="SAFE_FALLBACK", workflow=workflow,
                                       developer_trace=developer_trace, context=context, start=start, alert=alert)
            if _link_req["state"] != "VALID":
                # TEST QUALITY / observability — populated even on this
                # early-return (not only after a real Executor call) so a
                # test/eval harness can verify the right capability
                # genuinely engaged, not only that some reply text looked
                # plausible. Never set for the NO_GENUINE_SIGNAL branch
                # above — there, this action correctly did NOT engage.
                developer_trace["selected_business_action"] = "geturlproductdetail"
                developer_trace["link_conversion_state"] = _link_req["state"]
                reply = _build_response(text=_link_reply_for_state(
                    _link_req["state"], _link_req.get("platform")))
                return self._finalize(reply=reply, routing_type="WORKFLOW", workflow=workflow,
                                       developer_trace=developer_trace, context=context, start=start, alert=alert)
            developer_trace["link_conversion_state"] = "VALID"

            # PHASE-LINK-1688 — scoped ONLY to geturlproductdetail, still
            # deterministic (no LLM), still PUBLIC (no CustCode ask, no
            # verification). Two production gaps the mocked tests never
            # exercised:
            #  (a) the FastTrade endpoint REQUIRES a CustCode even for a
            #      public/guest link — a request with none is HTTP 400
            #      ("ไม่สามารถดำเนินการได้"). Use the verified/profile
            #      CustCode when the customer has one, else the Shipify
            #      guest account code.
            #  (b) FastTrade does NOT follow 1688 QR/short redirects — it
            #      400s them. Resolve the redirect here (SSRF-guarded,
            #      *.1688.com only), then normalize, then hand the real
            #      product URL to the SAME conversion path. If it does not
            #      resolve to a supported 1688 product URL, give a useful
            #      reply instead of executing.
            _lc_cc = context.get("customer_context") or {}
            _link_conv_custcode = (_lc_cc.get("cust_code") or _lc_cc.get("CustCode")
                                    or _LINK_GUEST_CUSTCODE)
            _link_conv_url_override = None
            _lc_url = _link_req["url"]
            if _link_is_1688_short_url(_lc_url):
                _resolved = _link_resolve_1688_short_url(_lc_url)
                _resolved = _link_normalize_url(_resolved) if _resolved else None
                if (_resolved and _resolved != _lc_url
                        and not _link_is_1688_short_url(_resolved)
                        and _link_classify_platform(_resolved) == "1688"):
                    developer_trace["link_conversion_short_url_resolved"] = {"from": _lc_url, "to": _resolved}
                    _link_conv_url_override = _resolved
                else:
                    developer_trace["link_conversion_state"] = "SHORT_URL_UNRESOLVED"
                    reply = _build_response(text=_link_reply_for_state("SHORT_URL_UNRESOLVED"))
                    return self._finalize(reply=reply, routing_type="WORKFLOW", workflow=workflow,
                                           developer_trace=developer_trace, context=context, start=start, alert=alert)
            else:
                _norm = _link_normalize_url(_lc_url)
                if _norm != _lc_url:
                    developer_trace["link_conversion_url_normalized"] = {"from": _lc_url, "to": _norm}
                    _link_conv_url_override = _norm
        else:
            _link_conv_custcode = None
            _link_conv_url_override = None

        if routing_type == "RAG":
            # Production Integration Sprint (2026-08-02), Phase 1 Step C —
            # the ONE production RAG execution path (services/
            # decision_engine.py::_run_rag_pipeline, wrapping services/
            # playground_orchestrator.py::run_playground_turn) — never
            # the Action Executor's thinner _execute_rag. One shared
            # pipeline for AI Playground, LINE OA, and future channels.
            exec_result, exec_latency = self._run_rag_pipeline(message, history, context)
        elif _requires_confirmation(selected) and not context.get("confirmed"):
            # CUSTOMER-CSW9-REAL-2 — cross-field sanity BEFORE the gate
            # opens: a free-text address-component slot (ReceiverName,
            # Address, …) must never hold this action's OWN record
            # identifier value. If it does, the collection is corrupt
            # (real LINE: a bare ShipmentCode answer spilled into
            # ReceiverName). Drop just that slot and re-ask it — never
            # show a confirmation screen built from an obviously wrong
            # value, never execute one. Root cause is fixed above
            # (_apply_identifier_memory no longer pre-seeds a write
            # action's record id); this is the belt-and-braces guard.
            # PHASE-1-STABILIZATION-ISOLATE-CSW9-1 — action-key-scoped;
            # the confirmation engine is not broadened for any other
            # confirmation-gated action.
            _id_vals = {v for k, v in (collected_slots or {}).items()
                        if k.lower() in ("shipmentcode", "ordercode", "tracking") and v} \
                if selected.get("action_key") == "requestshippingaddresschange" else set()
            _corrupt = [p["name"] for p in (selected.get("parameters") or [])
                        if (p.get("field_metadata") or {}).get("address_component")
                        and collected_slots.get(p["name"]) in _id_vals] if _id_vals else []
            if _corrupt:
                for _c in _corrupt:
                    collected_slots.pop(_c, None)
                developer_trace["cross_field_safety"] = {"dropped": _corrupt, "reason": "value_equals_record_identifier"}
                _reask = _next_expected_parameter(selected, self.registry, collected_slots)
                _reask_text = _generate_parameter_question(_reask) if _reask else \
                    "รบกวนแจ้งข้อมูลผู้รับใหม่อีกครั้งนะคะ"
                return self._finalize(reply=_build_response(text=_reask_text), routing_type="WORKFLOW",
                                       workflow=workflow, developer_trace=developer_trace,
                                       context=context, start=start, alert=alert)
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
            # PHASE-LINK-1688 — hand the resolved/normalized product URL
            # (QR redirect followed, m.1688 mobile canonicalized) to the
            # converter, and ensure a CustCode is present for this public
            # action (guest fallback). Scoped to geturlproductdetail only.
            _link_conv_customer_context = None
            if selected.get("action_key") == "geturlproductdetail":
                if _link_conv_url_override:
                    for _k in ("url", "URL", "Url"):
                        system_values[_k] = _link_conv_url_override
                _link_conv_customer_context = {
                    **(context.get("customer_context") or {}), "cust_code": _link_conv_custcode}
            exec_context = {
                "question": message, "collected_slots": collected_slots, "workflow": workflow, "intent": intent,
                "conversation_context": context.get("conversation_context") or {},
                "customer_context": _link_conv_customer_context
                if _link_conv_customer_context is not None else (context.get("customer_context") or {}),
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
            # OWNER-REAL-LINE-FIX-02 — for a structurally-valid link whose
            # conversion API failed, give the specific "link looks fine,
            # couldn't convert right now" wording, not the generic outage
            # line (and never a fake staff hand-off).
            if selected.get("action_key") == "geturlproductdetail":
                _err_text = _link_reply_for_state("UPSTREAM_FAILURE")
            else:
                _err_text = "ขอโทษด้วยค่ะ ไม่สามารถดำเนินการได้ในขณะนี้ รบกวนลองใหม่อีกครั้งนะคะ"
            reply = _build_response(text=_err_text)
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

            # Customer UAT Fix 2 (2026-09-02) — a genuine COMPANY/Shipify
            # fact the RAG pipeline could not support from trusted
            # evidence (its deterministic Answerability-Gate / P7.1 no-
            # info branch fired). Treat it as a structured NEED-HUMAN-
            # FOLLOW-UP: route through the SAME Human CS handoff the AI-
            # policy escalation path uses (routing_type="HUMAN_HANDOFF" +
            # handoff_payload{reason}), so line_bot/webhook.py's existing
            # send_handoff_notification + handoff_status dedup actually
            # notify staff. The customer reply carried here is the honest
            # no-info wording WITHOUT any "staff will check" promise —
            # the channel adapter appends the follow-up clause ONLY when
            # the notification really succeeds (never a fake promise).
            # A missing-customer-input turn never reaches here: it takes
            # the Answer Plan's clarification branch in the RAG pipeline,
            # which returns a clarification question (no unsupported_
            # company_fact flag), and a supported/grounded answer or a
            # General-Chat-Fallback reply never sets the flag either.
            if result_payload.get("unsupported_company_fact"):
                # SEMANTIC-FIRST-2 / FIX-2 BOUNDARY — an understood change
                # request that just needs input, or a referent-less
                # elliptical follow-up, is not "missing company knowledge".
                _sf2_supp = self._fix2_boundary_suppression(
                    message, context, developer_trace, start, alert, workflow=workflow)
                if _sf2_supp is not None:
                    return _sf2_supp
                _noinfo_text = (result_payload.get("answer") or "").strip() \
                    or "ขออภัยค่ะ ตอนนี้ยังไม่พบข้อมูลยืนยันในส่วนนี้ค่ะ"
                reply = _build_response(text=_noinfo_text)
                developer_trace["unsupported_company_fact_handoff"] = True
                return self._finalize(reply=reply, routing_type="HUMAN_HANDOFF", workflow=workflow,
                                       developer_trace=developer_trace, context=context, start=start, alert=alert,
                                       handoff_payload={"reason": "unsupported_company_information"})

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

            # CUSTOMER-ERP-READ-1 state D — VALID_NOT_FOUND. A private ERP
            # READ status inquiry (searchdatashipment / searchdataorder /
            # searchdatatracking) that returned HTTP-OK but with NO
            # matching record: tell the customer it was not found and to
            # re-check the number — never the generic "ดำเนินการเรียบร้อย
            # ค่ะ" (implies success) and never "ระบบขัดข้อง" (only a real
            # ERP_FAILURE, handled by the status=="error" branch above).
            # LIST actions keep their own empty-result wording
            # (_aggregate_list_reply). Scoped to the three READ status
            # keys so a WRITE/notification flow is never affected.
            if (selected.get("action_key") in _ERP_READ_STATUS_ACTIONS
                    and not _detect_aggregation_request(message)):
                _rec_id = next(
                    (collected_slots.get(k) for k in ("ShipmentCode", "OrderCode", "Tracking")
                     if collected_slots.get(k)), None)
                _no_record = (not full_mapped) or (
                    isinstance(full_mapped, dict)
                    and all(v in (None, "", [], {}) for v in full_mapped.values()))
                if _rec_id and _no_record:
                    developer_trace["erp_read_result"] = "valid_not_found"
                    reply = _build_response(text=(
                        f"ไม่พบข้อมูลรายการสำหรับเลขที่ {_rec_id} ในระบบค่ะ "
                        "รบกวนตรวจสอบเลขที่บิลขนส่งหรือเลขแทร็กอีกครั้งแล้วแจ้งมาใหม่นะคะ"))
                    return self._finalize(reply=reply, routing_type=routing_type, workflow=workflow,
                                           developer_trace=developer_trace, context=context, start=start, alert=alert)

            # PHASE-5 D2 — a customer-scoped LIST ERP READ (e.g.
            # searchdatashipmentlist for "วันนี้มีของเข้าไทยไหมคะ") that
            # returned HTTP-OK with NO records must say so explicitly, not
            # fall through to the generic write-ack "ดำเนินการเรียบร้อยค่ะ"
            # (which implies an action succeeded) — and never "ระบบขัดข้อง"
            # (a real failure took the status=="error" branch). Scoped to a
            # list-shaped API/WEBHOOK read; a WRITE / notification ack is
            # never list-shaped so it keeps the generic reply. There is no
            # reliable per-record arrival DATE, so the wording stays a
            # plain "no shipments for your account" — it never claims
            # anything about "today".
            if (selected.get("action_type") in ("API", "WEBHOOK")
                    and _psi_is_list_shaped(selected)
                    and selected.get("action_key") not in _ERP_READ_STATUS_ACTIONS):
                _lm = result_payload.get("mapped_fields")
                _list_empty = (not _lm) or (
                    isinstance(_lm, dict) and all(v in (None, "", [], {}) for v in _lm.values()))
                if _list_empty and not _detect_aggregation_request(message):
                    developer_trace["erp_read_result"] = "list_valid_empty"
                    reply = _build_response(text=(
                        "ตอนนี้ยังไม่พบรายการขนส่งในระบบสำหรับบัญชีของคุณลูกค้าค่ะ "
                        "หากเพิ่งทำรายการ อาจต้องรอระบบอัปเดตสักครู่นะคะ"))
                    return self._finalize(reply=reply, routing_type=routing_type, workflow=workflow,
                                           developer_trace=developer_trace, context=context, start=start, alert=alert)

            # CUSTOMER-CSW9-ADDRESS-CHANGE-1 — requestshippingaddresschange
            # is a NOTIFICATION-shaped write (setup_metadata.operation_type
            # == "NOTIFICATION": the customer's request is forwarded to CS,
            # who make the ERP edit themselves — it is NOT an automatic
            # data write). It has no response_mapping, so on HTTP-OK
            # `full_mapped` is empty and the generic composer below would
            # fall through to `result_payload` and render the raw REST
            # executor envelope ("status_code: 200 / execution_time_ms /
            # attempts ...") straight to the customer. Same per-action-key
            # truth-typing guard pattern as geturlproductdetail /
            # searchdatashipment below: a successful call here means "the
            # request reached CS", never "the address was changed", so the
            # reply says exactly that — the source's own stage-2 closing
            # ("ตรวจสอบและชำระบิลขนส่งได้เลย") without its "เรียบร้อยค่ะ"
            # completion claim. A real failure already took the
            # status=="error" branch above; a denial took status=="denied".
            if selected.get("action_key") == "requestshippingaddresschange":
                _acbill = collected_slots.get("ShipmentCode")
                developer_trace["address_change_result"] = "request_submitted_to_cs"
                reply = _build_response(text=(
                    (f"รับเรื่องขอเปลี่ยนที่อยู่จัดส่งของบิล {_acbill} แล้วค่ะ "
                     if _acbill else "รับเรื่องขอเปลี่ยนที่อยู่จัดส่งแล้วค่ะ ")
                    + "เดี๋ยวแอดมินตรวจสอบและแก้ไขที่อยู่ในระบบให้นะคะ "
                      "จากนั้นคุณลูกค้าตรวจสอบและชำระบิลขนส่งได้เลยค่ะ"))
                return self._finalize(reply=reply, routing_type=routing_type, workflow=workflow,
                                       developer_trace=developer_trace, context=context, start=start, alert=alert)

            # SHIPMENT-DIRECT-ANSWER-1 — a shipment FOUND (full_mapped
            # non-empty; the not-found case above already returned) and
            # the customer's own message matches one of the 4 known
            # follow-up question shapes (ETA / warehouse arrival /
            # Thai-tracking availability / current stage): answer THAT
            # question directly using the SAME already-fetched fields,
            # instead of the generic ERP field-dump composer below.
            # Scoped to searchdatashipment only; every other action's
            # reply composition is unaffected. Falls through (None) for
            # any other searchdatashipment question (e.g. a request for
            # a specific unrelated field), which keeps today's generic
            # composer behaviour exactly as before.
            if (selected.get("action_key") == "searchdatashipment"
                    and isinstance(full_mapped, dict) and full_mapped
                    and any(v not in (None, "", [], {}) for v in full_mapped.values())):
                _direct = _compose_shipment_followup_reply(
                    message, full_mapped, selected.get("response_mapping"),
                    recent_user_turns=[
                        h.get("content") or "" for h in reversed(history or [])
                        if h.get("role") == "user"][:4])
                if _direct:
                    developer_trace["shipment_followup_direct_answer"] = True
                    reply = _build_response(text=_direct)
                    return self._finalize(reply=reply, routing_type=routing_type, workflow=workflow,
                                           developer_trace=developer_trace, context=context, start=start, alert=alert)

            # CUSTOMER-TRACK-TH-1 — a Thai-tracking-output ask (see
            # _PSI_THAI_TRACKING_OUTPUT_RE) whose shipment record WAS
            # found (full_mapped non-empty) but whose Thai Tracking field
            # came back empty must say so explicitly — never substitute
            # China Tracking, never fall back to a generic no-info reply
            # (the shipment itself was found; that would misreport a
            # genuine "not yet assigned" state as "nothing found at
            # all"). Scoped to searchdatashipment only (the action
            # CUSTOMER-TRACK-TH-1 resolves this ask to); config-driven —
            # finds the response_mapping row for TrackingTH by its own
            # json_path, never a hardcoded label string. The ORIGINAL ask
            # ("ขอเลขแทรคไทยค่ะ") is usually a turn EARLIER than the bare
            # shipment-bill reply actually being executed here, so this
            # checks the current message OR the last few user turns —
            # bounded to avoid a much-older, unrelated mention elsewhere
            # in a long session.
            _recent_user_turns = [message or ""] + [
                h.get("content") or "" for h in reversed(history or []) if h.get("role") == "user"][:4]
            _asked_thai_tracking = any(
                _PSI_THAI_TRACKING_OUTPUT_RE.search(t) and "จีน" not in t for t in _recent_user_turns)
            if (selected.get("action_key") == "searchdatashipment"
                    and _asked_thai_tracking
                    and isinstance(full_mapped, dict) and full_mapped
                    and any(v not in (None, "", [], {}) for v in full_mapped.values())):
                _th_label = next((r.get("mapped_label") for r in (selected.get("response_mapping") or [])
                                  if "trackingth" in (r.get("json_path") or "").lower()), None)
                if _th_label and not (full_mapped.get(_th_label) or "").strip():
                    developer_trace["thai_tracking_result"] = "shipment_found_no_thai_tracking"
                    _base = self._compose_natural_reply(full_mapped, selected.get("response_mapping"),
                                                          fallback_payload=full_mapped)
                    reply = _build_response(text=(
                        f"{_base}\nพบข้อมูลบิลแล้วค่ะ แต่ตอนนี้ยังไม่มีเลข Tracking ไทยในระบบ"))
                    return self._finalize(reply=reply, routing_type=routing_type, workflow=workflow,
                                           developer_trace=developer_trace, context=context, start=start, alert=alert)

            # CUSTOMER-LINK-1 — conversion-RESULT truth typing, scoped to
            # geturlproductdetail (same established per-action-key scoping
            # as _ERP_READ_STATUS_ACTIONS just above). Never falls through
            # to _compose_natural_reply's generic "ดำเนินการเรียบร้อยค่ะ"
            # default (a success-looking phrase) on anything but a REAL
            # converted link.
            if selected.get("action_key") == "geturlproductdetail":
                _lc_result = _classify_conversion_result(
                    executor_error=bool(exec_result.get("error")), mapped_fields=full_mapped)
                developer_trace["link_conversion_result"] = _lc_result["state"]
                if _lc_result["state"] == "CONVERSION_SUCCESS":
                    reply = _build_response(text=_link_reply_for_success(_lc_result["link"]))
                else:
                    reply = _build_response(text=_link_reply_for_state(_lc_result["state"]))
                return self._finalize(reply=reply, routing_type=routing_type, workflow=workflow,
                                       developer_trace=developer_trace, context=context, start=start, alert=alert)

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

    # ── Fix-2 boundary (SEMANTIC-FIRST-2) ──────────────────────────────────

    def _fix2_boundary_suppression(self, message: str, context: Dict, developer_trace: Dict,
                                    start: float, alert, *, workflow: Optional[str]) -> Optional[Dict]:
        """SEMANTIC-FIRST-2 / FIX-2 BOUNDARY. Fix-2 (unsupported COMPANY
        fact -> Human CS) must fire ONLY when a genuinely understood
        Shipify question has no trusted evidence. It must NOT fire when
        the turn is really (a) an understood operational CHANGE request
        that just needs the record id + new value, or (b) a bare
        elliptical follow-up with no resolvable referent (unclear
        language). Both are handled here with a clarify / collect reply
        instead. Called from every Fix-2 site so the boundary is applied
        once, consistently. Returns a finalized response dict to short-
        circuit, or None to let the caller proceed to the Fix-2 handoff.

        The ONE central interpretation (stashed on context by decide())
        is the authority on whether the *meaning* was understood."""
        _sem = context.get("_semantic_interpretation")
        _family = getattr(_sem, "intent_family", None)
        if _family == "ADDRESS_CHANGE":
            developer_trace["fix2_suppressed"] = "understood_change_request_missing_input"
            return self._finalize(
                reply=_build_response(text=(
                    "ได้ค่ะ รบกวนแจ้งเลขบิลขนส่ง (FT/FE/SA/SP) ที่ต้องการเปลี่ยนที่อยู่ "
                    "พร้อมที่อยู่ปลายทางใหม่ ชื่อผู้รับ และเบอร์โทรผู้รับด้วยนะคะ")),
                routing_type="WORKFLOW", workflow=workflow,
                developer_trace=developer_trace, context=context, start=start, alert=alert)
        if _ELLIPTICAL_NO_REFERENT_RE.match((message or "").strip()):
            developer_trace["fix2_suppressed"] = "elliptical_no_referent_clarify"
            return self._finalize(
                reply=_build_response(text=(
                    "ขออภัยค่ะ ไม่แน่ใจว่าหมายถึงรายการไหน รบกวนแจ้งเลขบิล เลขคำสั่งซื้อ "
                    "หรือรายละเอียดที่ต้องการให้ช่วยตรวจสอบเพิ่มเติมนะคะ")),
                routing_type="WORKFLOW", workflow=workflow,
                developer_trace=developer_trace, context=context, start=start, alert=alert)
        return None

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
            # Customer UAT Fix 2 — same structured NEED-HUMAN-FOLLOW-UP
            # routing as _execute_selected_action's RAG branch, in case a
            # genuine unsupported company fact reaches the RAG pipeline
            # via this fallback path instead.
            if result_payload.get("unsupported_company_fact"):
                _sf2_supp = self._fix2_boundary_suppression(
                    message, context, developer_trace, start, alert, workflow=None)
                if _sf2_supp is not None:
                    return _sf2_supp
                reply = _build_response(text=answer_text.strip())
                developer_trace["unsupported_company_fact_handoff"] = True
                return self._finalize(reply=reply, routing_type="HUMAN_HANDOFF", workflow=None,
                                       developer_trace=developer_trace, context=context, start=start, alert=alert,
                                       handoff_payload={"reason": "unsupported_company_information"})
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
        _cc = context.get("customer_context") or {}
        tier = _cc.get("conversation_tier")
        if channel or tier:
            try:
                template_id = get_active_prompt(channel=channel, tier=tier).id
            except Exception:
                template_id = None

        # P5 — stage-aware follow-up DEPTH only. Read-only preference
        # signals from the profile the caller already loaded this turn
        # (no extra Supabase read here); they never change the factual
        # answer, retrieval, ERP behavior or prompt selection.
        pg_lead_stage = _cc.get("lead_stage")
        pg_sentiment = _cc.get("sentiment_status")

        pg_history = [{"role": t.get("role"), "content": t.get("content")} for t in (history or [])]
        t0 = time.time()
        try:
            result = run_playground_turn(message, template_id=template_id, history=pg_history,
                                          lead_stage=pg_lead_stage, sentiment_status=pg_sentiment,
                                          interpretation=context.get("_semantic_interpretation"))
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
                    # Customer UAT Fix 2 (2026-09-02) — the RAG pipeline's
                    # structured "genuine unsupported company fact, needs
                    # human follow-up" signal (set only by its
                    # deterministic Answerability-Gate / P7.1 no-info
                    # branches). Surfaced, not re-derived, so the caller
                    # routes it through the existing Human CS handoff.
                    # `is True` (not a bare truthiness check) so a test
                    # mock that never sets this attribute — a bare
                    # MagicMock auto-creates a truthy child, the SAME trap
                    # documented for policy_escalate / general_chat_used —
                    # can never accidentally trigger a handoff.
                    "unsupported_company_fact": getattr(result, "unsupported_company_fact", False) is True,
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
        # P2 SHADOW-WRITE — the structured conversation frame for this
        # turn, for the channel adapter to persist to ai_sessions
        # .conversation_frame. Advisory only; never a routing input.
        _sf = context.get("_structured_conversation_frame")
        if _sf is not None:
            response["conversation_frame"] = _sf
        if bool(context.get("developer_mode")):
            developer_trace.setdefault("confidence", None)
            response["developer"] = {**developer_trace, "latency_ms": round((time.time() - start) * 1000, 2)}
        return response


def get_decision_engine(sb=None) -> DecisionEngine:
    return DecisionEngine(sb)
