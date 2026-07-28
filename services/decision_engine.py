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


def _extract_candidates_for_binding(message: str) -> List[str]:
    candidates = list(extract_candidates(message))
    for m in _EMAIL_CANDIDATE_RE.finditer(message or ""):
        token = m.group(0)
        if token not in candidates:
            candidates.append(token)
    return candidates


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


def _resolve_parameter_validator(param: Dict):
    """Builds the validator for THIS parameter. A configured
    `validation_pattern` (AI Auto Setup / admin-authored regex, e.g.
    `^C\\d{5}$` for CustCode vs `^PO\\d{6,}$` for OrderCode) always wins —
    this is exactly what lets the Decision Engine distinguish two
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


_NON_ASKABLE_INPUT_SOURCES = ("secret_configuration", "credential_store")


def _askable_parameters_by_name(action: Dict) -> Dict[str, Dict]:
    """Every parameter the customer could actually be ASKED for —
    excludes secret-configuration and credential_store parameters
    (never requested from the customer, per the Business Action
    Center's own security rule)."""
    return {
        p["name"]: p for p in (action.get("parameters") or [])
        if p.get("input_source", "customer_message") not in _NON_ASKABLE_INPUT_SOURCES
    }


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
    candidates = [c for c in _extract_candidates_for_binding(message) if c not in (exclude_values or set())]

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
        binding = _bind_candidate_to_parameter(candidates, param)
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
            binding = _bind_candidate_to_parameter(candidates, askable[name])
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
    used_values = set()
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
        if turn.get("role") != "user" or i == 0:
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
        if next_param and _generate_parameter_question(next_param) == last_text:
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
    trigger_message = next((t.get("content") or "" for t in history if t.get("role") == "user"), "")
    return max(matches, key=lambda a: _keyword_score(a, trigger_message))

# ── Alert vocabulary — deliberately small, generic, deterministic (no
# LLM call, consistent with every other conversation-intelligence module
# in this codebase). Detects a SIGNAL worth flagging; it never decides
# what to DO about it beyond attaching structured metadata + trying a
# NOTIFICATION-type Business Action if one is configured.
_COMPLAINT_RE = re.compile(r"ร้องเรียน|แย่มาก|ไม่พอใจ|บริการแย่|เลวมาก|ผิดหวังมาก", re.IGNORECASE)
_LEGAL_THREAT_RE = re.compile(r"ทนายความ|ฟ้องร้อง|แจ้งความ|ดำเนินคดี|สคบ", re.IGNORECASE)

_ROUTING_TYPES = ("RAG", "API", "TOOL", "WORKFLOW", "NOTIFICATION", "HUMAN_HANDOFF", "WEBHOOK", "SAFE_FALLBACK")

_URL_RE = re.compile(r"https?://\S+")


def _extract_system_values(message: str) -> Dict:
    """Generic, action-agnostic values derived from the raw message that
    any Business Action's parameters may read via input_source
    'system_generated' — currently just the first URL, if any. Never
    tied to a specific action (e.g. not "if url_converter")."""
    match = _URL_RE.search(message or "")
    return {"url": match.group(0)} if match else {}


def _safe_fallback_response(reason: str) -> Dict:
    return {
        "text": "ขอโทษด้วยค่ะ ตอนนี้ยังไม่พบคำตอบที่ชัดเจนสำหรับคำถามนี้ รบกวนสอบถามเจ้าหน้าที่เพิ่มเติมนะคะ",
        "message_parts": None, "buttons": [], "quick_replies": [], "images": [], "files": [],
    }, reason


def _build_response(*, text: str, message_parts=None, buttons=None, quick_replies=None,
                     images=None, files=None) -> Dict:
    return {
        "text": text, "message_parts": message_parts, "buttons": buttons or [],
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


def _keyword_score(action: Dict, message: str) -> float:
    message_l = (message or "").lower()
    score = 0.0
    for kw in action.get("search_keywords") or []:
        if kw and str(kw).lower() in message_l:
            score += 1.0
    for ex in (action.get("_examples_text") or []):
        if ex and str(ex).lower() in message_l:
            score += 0.5
    ai_desc = (action.get("ai_description") or "").lower()
    if ai_desc and any(word in ai_desc for word in message_l.split() if len(word) > 2):
        score += 0.25
    return score


def _parameter_availability_score(action: Dict, collected_slots: Dict) -> float:
    """Rewards an action whose configured parameter names are already
    (partly) satisfiable from what's been collected this turn — makes
    e.g. a customer-lookup action outrank an unrelated action once the
    caller already has CustCode/CustEmail/etc. in hand."""
    if not collected_slots:
        return 0.0
    names = {p.get("name") for p in action.get("_parameters") or []}
    return float(len(names & set(collected_slots.keys())))


def search_candidate_actions(registry, *, workflow: Optional[str], message: str,
                              collected_slots: Optional[Dict] = None,
                              action_types: Optional[List[str]] = None) -> List[Dict]:
    """Business Action Search — returns enabled candidates the Decision
    Engine may choose from, each annotated with a `_score`/`_reasons`.
    Considers: category (workflow match), keyword/example/ai_description
    overlap, parameter availability, priority, and (currently inert)
    future semantic/embedding scores. Never filters by a hardcoded
    action_key — every action, current or future, competes on the same
    generic signals."""
    try:
        candidates = registry.enabled_actions()
    except Exception:
        return []

    if action_types:
        candidates = [a for a in candidates if a.get("action_type") in action_types]

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
        param_score = _parameter_availability_score(action, collected_slots or {})
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

            if _HUMAN_REQUEST_RE.search(message or ""):
                return self._route_human_handoff(
                    message, history, context, developer_trace, start,
                    reason="user_requested_human", workflow=workflow_hint)

            # New execution order: Search Candidate Business Actions ->
            # Select Best Business Action -> Read Business Action
            # Parameters -> Information Collection -> Execute. Selection
            # happens BEFORE information collection, so the Registry's
            # own parameter definitions — not a separate intent-keyed
            # schema — drive what gets asked.
            continuation_action = _resolve_continuation_action(self.registry, history, workflow_hint)
            if continuation_action:
                selected = continuation_action
                candidates = [selected]
                developer_trace["selection_source"] = "conversation_continuation"
            else:
                candidates = search_candidate_actions(self.registry, workflow=workflow_hint, message=message,
                                                        collected_slots={})
                selected = select_best_action(candidates, minimum_score=1.0 if not workflow_hint else 0.5)
                developer_trace["selection_source"] = "fresh_search"

            if not selected:
                if workflow_hint:
                    # Safe Migration: no Business Action is configured for
                    # this ERP workflow at all -> legacy INTENT_SCHEMAS
                    # fallback, unchanged behavior from before this task.
                    return self._handle_legacy_workflow(
                        workflow_hint, message, history, context, developer_trace, start,
                        reason="no_business_action_for_workflow")
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
        result = _bind_all_from_message(full_action, self.registry, collected, message)
        collected = result["collected"]
        ambiguous_candidates = result["ambiguous_candidates"]

        validation = self.registry.validate_can_execute(action_id, collected)
        is_complete = validation["ok"]
        next_after = None if is_complete else _next_expected_parameter(full_action, self.registry, collected)

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
            retry_count = sum(1 for t in history if t.get("role") == "assistant"
                               and (t.get("content") or "").strip() == expected_question)
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
        exec_context = {
            "question": message, "collected_slots": collected_slots, "workflow": workflow, "intent": intent,
            "conversation_context": context.get("conversation_context") or {},
            "customer_context": context.get("customer_context") or {},
            "current_user": context.get("current_user"), "developer_mode": bool(context.get("developer_mode")),
            # Generic system-derived values ANY tool/action may read (e.g.
            # a URL-handling tool) — never a per-action special case.
            "system_values": _extract_system_values(message),
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

        routing_type = selected.get("action_type") or "SAFE_FALLBACK"
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
        if routing_type == "RAG":
            answer = result_payload.get("answer") or ""
            if not answer.strip():
                return self._route_safe_fallback(message, history, context, developer_trace, start,
                                                   reason="rag_no_grounded_answer")
            reply = _build_response(text=answer)
        else:
            mapped = result_payload.get("mapped_fields")
            text = self._summarize_action_result(mapped if mapped else result_payload)
            reply = _build_response(text=text)

        return self._finalize(reply=reply, routing_type=routing_type, workflow=workflow,
                               developer_trace=developer_trace, context=context, start=start, alert=alert)

    @staticmethod
    def _summarize_action_result(payload) -> str:
        if isinstance(payload, dict) and payload:
            parts = [f"{k}: {v}" for k, v in list(payload.items())[:6]]
            return " / ".join(parts)
        return "ดำเนินการเรียบร้อยค่ะ"

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

        reply = _build_response(text=message_override or "กำลังโอนสายให้เจ้าหน้าที่ดูแลต่อค่ะ")
        return self._finalize(reply=reply, routing_type="HUMAN_HANDOFF", workflow=workflow,
                               developer_trace=developer_trace, context=context, start=start,
                               alert=alert, handoff_payload=handoff_payload)

    # ── Safe Fallback ──────────────────────────────────────────────────────

    def _route_safe_fallback(self, message: str, history: List[Dict], context: Dict, developer_trace: Dict,
                              start: float, *, reason: str) -> Dict:
        alert = _detect_alert(message, context)
        rag_candidates = search_candidate_actions(self.registry, workflow=None, message=message,
                                                    action_types=["RAG"])
        rag_action = select_best_action(rag_candidates, minimum_score=0.0)
        answer_text = None
        if rag_action:
            exec_context = {"question": message, "developer_mode": bool(context.get("developer_mode"))}
            exec_result = self.executor.execute(rag_action["id"], exec_context)
            developer_trace["execution_result"] = exec_result
            if exec_result.get("status") == "success":
                answer_text = (exec_result.get("result") or {}).get("answer")
        else:
            # No RAG-type Business Action is registered yet — the true
            # "Safe Fallback" per spec still tries the existing RAG
            # service directly (read-only reuse, never modified here)
            # before giving up with the canned fallback message.
            try:
                from services.rag_service import get_rag_service
                rag = get_rag_service()
                chunks = rag.retrieve(message, top_k=3)
                answer_text = rag.build_context(chunks) if chunks else ""
                developer_trace["execution_result"] = {"status": "success" if answer_text else "no_chunks",
                                                        "result": {"chunk_count": len(chunks)}}
            except Exception as e:
                developer_trace["execution_result"] = {"status": "error", "error": sanitize_for_preview(str(e))}

        if answer_text and answer_text.strip():
            reply = _build_response(text=answer_text)
            return self._finalize(reply=reply, routing_type="RAG", workflow=None,
                                   developer_trace=developer_trace, context=context, start=start, alert=alert)

        reply, _ = _safe_fallback_response(reason)
        developer_trace["fallback_reason"] = reason
        return self._finalize(reply=reply, routing_type="SAFE_FALLBACK", workflow=None,
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
