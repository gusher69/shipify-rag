"""Conversation Form Generator — Platform Service (this sprint's core
deliverable).

Turns a resolved Integration Contract (services/integration_contract_
service.py::resolve_effective_contract()/describe_integration()) plus the
CURRENT conversation state (what's already been collected) into a
channel-agnostic "Conversation Form": a small, serializable description
of the next question(s) to ask, expressed generically as steps of
components (text/number/buttons/dropdown/etc.), never as hardcoded
per-domain text.

This module never implements the Business Action Matcher, the Decision
Engine, or ERP-vs-RAG routing. It never re-implements parameter-group
validation (reuses services/business_action_registry.py::
validate_parameter_groups) and never re-implements schema merging
(consumes the contract that already went through services/
integration_schema_service.py's ONE three-layer merge).

Everything here is table/metadata-driven: no `if entity == "..."`, no
`if canonical_name == "..."`, no hardcoded field-name branches. A new
domain (a brand-new action + schema) requires zero changes to this file.
"""
from dataclasses import dataclass, field as dc_field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional
import hashlib
import json

from services.business_action_registry import validate_parameter_groups
from services.integration_contract_service import VersionedCache

GENERATOR_VERSION = "1.0"

# ── Requirement 2 — ConversationState ───────────────────────────────────

_STATES = ("WaitingInput", "WaitingConfirmation", "ReadyToExecute", "Executing",
           "Completed", "Cancelled", "Timeout")

# The exact allowed edges (Requirement 2) — anything else is rejected.
_ALLOWED_TRANSITIONS = {
    "WaitingInput": {"ReadyToExecute", "Cancelled", "Timeout"},
    "ReadyToExecute": {"WaitingConfirmation", "Executing"},
    "WaitingConfirmation": {"Executing", "Cancelled"},
    "Executing": {"Completed"},
}


class InvalidStateTransition(Exception):
    pass


@dataclass
class ConversationState:
    current_state: str = "WaitingInput"
    collected_values: Dict = dc_field(default_factory=dict)
    missing_fields: List[str] = dc_field(default_factory=list)
    selected_required_group_fields: Dict[str, str] = dc_field(default_factory=dict)
    skipped_fields: List[str] = dc_field(default_factory=list)
    current_step: object = 0
    confirmation_status: Optional[bool] = None
    created_at: str = dc_field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = dc_field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    expires_at: Optional[str] = None

    def __post_init__(self):
        if self.current_state not in _STATES:
            raise ValueError(f"unknown state '{self.current_state}'")
        if self.expires_at is None:
            self.expires_at = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()

    def transition_to(self, new_state: str) -> bool:
        """Only allows the exact edges in _ALLOWED_TRANSITIONS. Returns
        False (never raises, never mutates) on a disallowed transition —
        callers that prefer an exception can check the return value and
        raise InvalidStateTransition themselves."""
        if new_state not in _STATES:
            return False
        allowed = _ALLOWED_TRANSITIONS.get(self.current_state, set())
        if new_state not in allowed:
            return False
        self.current_state = new_state
        self.updated_at = datetime.now(timezone.utc).isoformat()
        return True

    def to_dict(self) -> Dict:
        return {
            "current_state": self.current_state,
            "collected_values": dict(self.collected_values),
            "missing_fields": list(self.missing_fields),
            "selected_required_group_fields": dict(self.selected_required_group_fields),
            "skipped_fields": list(self.skipped_fields),
            "current_step": self.current_step,
            "confirmation_status": self.confirmation_status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, d: Optional[Dict]) -> "ConversationState":
        d = d or {}
        return cls(
            current_state=d.get("current_state", "WaitingInput"),
            collected_values=dict(d.get("collected_values") or {}),
            missing_fields=list(d.get("missing_fields") or []),
            selected_required_group_fields=dict(d.get("selected_required_group_fields") or {}),
            skipped_fields=list(d.get("skipped_fields") or []),
            current_step=d.get("current_step", 0),
            confirmation_status=d.get("confirmation_status"),
            created_at=d.get("created_at") or datetime.now(timezone.utc).isoformat(),
            updated_at=d.get("updated_at") or datetime.now(timezone.utc).isoformat(),
            expires_at=d.get("expires_at"),
        )


# ── Display-type derivation (Part 16 fallback) ──────────────────────────

def _fallback_display_type(f: Dict) -> str:
    """Never crashes, always returns something reasonable. Explicit
    `display_type` always wins (checked by the caller before reaching
    here) — this is purely the generic fallback derived from
    semantic_type / presence of enum `options`."""
    if f.get("options"):
        return "buttons" if len(f["options"]) <= 4 else "dropdown"
    sem = (f.get("semantic_type") or "").lower()
    return {
        "date": "date", "datetime": "datetime", "time": "time",
        "currency": "currency", "phone": "phone", "email": "email",
        "integer": "number", "number": "number", "boolean": "checkbox",
        "array": "multi_select", "identifier": "text", "status": "buttons",
    }.get(sem, "text")


def _component_for_field(f: Dict, language: str = "th") -> Dict:
    display_type = f.get("display_type") or _fallback_display_type(f)
    labels = f.get("display_labels") or {}
    label = labels.get(language) or f.get("business_label") or f.get("technical_name")
    options = f.get("options") or []
    component = {
        "field": f.get("technical_name") or f.get("canonical_name"),
        "canonical_name": f.get("canonical_name"),
        "component_type": display_type,
        "label": label,
        "placeholder": f.get("placeholder"),
        "help_text": f.get("help_text"),
        "required": bool(f.get("required")),
        "validation": f.get("validation") or {},
        "options": [
            {
                "value": o.get("value"), "label": o.get("label") or o.get("value"),
                "description": o.get("description"), "icon": o.get("icon"), "color": o.get("color"),
            }
            for o in options
        ],
        "option_source": "explicit" if f.get("options") else "auto_derived",
        "display_order": f.get("display_order"),
        "group": f.get("group"),
        "icon": f.get("icon"),
        "color": f.get("color"),
        "channel_variants": _channel_variants(display_type, options, f.get("channel_overrides")),
    }
    return component


# ── Requirement 5 — channel adaptation ──────────────────────────────────

def _channel_variants(display_type: str, options: List[Dict], channel_overrides: Optional[Dict] = None) -> Dict:
    """Generic per-channel rendering hints for one component. An admin's
    explicitly-configured `channel_overrides` (schema field key, same one
    tracked in `_FIELD_PROVENANCE_ATTRS`) is shallow-merged on top of the
    generically-derived variant for that channel — table-driven, no
    per-domain/per-field special-casing."""
    web = {"render_as": display_type}
    if display_type in ("buttons", "radio", "dropdown", "quick_reply") and options:
        web["render_as"] = "buttons" if len(options) <= 4 else "dropdown"
    line = {"render_as": "quick_reply" if options else ("text" if display_type not in ("date", "datetime") else display_type),
            "labels": [o.get("label") or o.get("value") for o in options][:13],
            "note": "LINE quick replies support at most 13 options" if len(options) > 13 else None}
    api = {"render_as": "structured", "schema": {"type": display_type, "options": options}}
    cli = {"render_as": "numbered_choice" if options else "text_prompt",
           "choices": [{"index": i + 1, "label": o.get("label") or o.get("value"), "value": o.get("value")}
                       for i, o in enumerate(options)]}
    variants = {"web": web, "line": line, "api": api, "cli": cli}
    if channel_overrides:
        for channel, override in channel_overrides.items():
            if channel in variants and isinstance(override, dict):
                variants[channel] = {**variants[channel], **override}
    return variants


# ── Requirement 4 — required-group-first flow ───────────────────────────

def _group_member_fields(group: Dict, input_fields: List[Dict]) -> List[Dict]:
    by_name = {f.get("technical_name"): f for f in input_fields}
    return [by_name[m] for m in (group.get("members") or []) if m in by_name]


def _criterion_selection_step(group: Dict, input_fields: List[Dict], language: str) -> Dict:
    members = _group_member_fields(group, input_fields)
    options = [
        {"value": f.get("technical_name"),
         "label": (f.get("display_labels") or {}).get(language) or f.get("business_label") or f.get("technical_name")}
        for f in members
    ]
    min_select = 1 if group.get("rule") in ("AT_LEAST_ONE",) else 1
    question = "ต้องการค้นหาด้วยเงื่อนไขใดครับ" if language == "th" else "Which criterion would you like to search by?"
    hint = "เลือกได้อย่างน้อย 1 ข้อ" if language == "th" else "Select at least one."
    return {
        "step_id": f"criterion_selection.{group.get('name')}",
        "kind": "criterion_selection",
        "group_name": group.get("name"),
        "group_rule": group.get("rule"),
        "question": question,
        "help_text": hint,
        "components": [{
            "field": f"__criterion__.{group.get('name')}",
            "component_type": "buttons" if len(options) <= 4 else "dropdown",
            "label": question,
            "options": options,
            "option_source": "auto_derived",
            "channel_variants": _channel_variants("buttons", options),
        }],
    }


def _value_step_for_field(f: Dict, language: str) -> Dict:
    component = _component_for_field(f, language)
    labels = f.get("display_labels") or {}
    label = labels.get(language) or f.get("business_label") or f.get("technical_name")
    question = (f.get("follow_up_prompts") or {}).get(language) or (
        f"กรุณาระบุ {label}" if language == "th" else f"Please provide {label}")
    return {
        "step_id": f"value.{f.get('technical_name')}",
        "kind": "value_input",
        "field": f.get("technical_name"),
        "question": question,
        "components": [component],
    }


def _groups_status(contract: Dict, collected: Dict, selected_group_fields: Dict) -> List[Dict]:
    """For each required_field_group, whether it's satisfied given the
    currently collected values (reuses validate_parameter_groups —
    Requirement/Part 5, never reinvents group semantics)."""
    groups = contract.get("required_field_groups") or []
    parameters = [{"name": f["technical_name"], "required": f.get("required"),
                   "input_source": f.get("source")} for f in contract.get("inputs") or []]
    fake_action = {"parameter_groups": groups}
    provided = {k: v for k, v in collected.items() if v not in (None, "")}
    result = validate_parameter_groups(fake_action, parameters, provided)
    failed_by_name = {g["name"]: g for g in result["failed_groups"]}
    out = []
    for g in groups:
        out.append({
            "group": g,
            "satisfied": g.get("name") not in failed_by_name,
            "selected_field": selected_group_fields.get(g.get("name")),
        })
    return out


# ── Static per-field metadata cache (Part 17 caching design) ────────────
# Per Part 17's documented design choice: only the STATIC per-field
# metadata/options derivation (which never depends on conversation
# state) is cached via the shared VersionedCache abstraction (Requirement
# 6, same class as the contract cache). The state-dependent step
# selection (which step comes next given collected_values so far) is
# always recomputed fresh — caching "the" form for an action would be
# wrong since step 2's content differs from step 1's for the exact same
# action/contract.
_form_field_cache = VersionedCache()


def _cache_key(contract: Dict) -> tuple:
    return (contract.get("action_id"), contract.get("contract_version"),
            contract.get("resolver_version"), contract.get("schema_version"), GENERATOR_VERSION)


def _static_field_components(contract: Dict, language: str) -> List[Dict]:
    key = _cache_key(contract) + ("fields", language)
    return _form_field_cache.get(key, lambda: [
        _component_for_field(f, language) for f in (contract.get("inputs") or [])
    ])


def invalidate_form_cache(action_id: Optional[str] = None) -> None:
    if action_id is None:
        _form_field_cache.invalidate_all()
    else:
        _form_field_cache.invalidate(lambda k: k[0] == action_id)


# ── Requirement 2/3 — the generator itself ──────────────────────────────

def generate_conversation_form(contract: Dict, state: Optional[ConversationState] = None,
                                language: str = "th") -> Dict:
    """The ONE place that decides what to ask next, given a contract and
    the conversation-so-far. Frontend JS (Studio Conversation Preview/
    Inspector, ERP Tester Conversation Simulation) must only RENDER this
    output — never independently decide field->component mapping.

    Returns {"title","steps","buttons","validation","required",
    "state", "ready_to_execute"}.
    """
    state = state or ConversationState()
    collected = dict(state.collected_values)
    input_fields = contract.get("inputs") or []
    required_names = [f["technical_name"] for f in input_fields if f.get("required")
                      and f.get("visibility") != "hidden" and not f.get("sensitive")
                      and f.get("source") not in ("credential_store", "secret_configuration")]
    _static_field_components(contract, language)  # warms/uses the static cache

    groups_status = _groups_status(contract, collected, state.selected_required_group_fields)
    unsatisfied_groups = [gs for gs in groups_status if not gs["satisfied"]]

    steps: List[Dict] = []
    missing_fields: List[str] = []

    # 1) Ungrouped required fields still missing.
    grouped_names = {m for gs in groups_status for m in (gs["group"].get("members") or [])}
    for f in input_fields:
        name = f.get("technical_name")
        # Never ask the customer for a hidden/sensitive/credential-
        # sourced field (e.g. SecretCode) — those are resolved via the
        # Credential Store, not the conversation, and no secret value
        # may ever appear in generated form state (standing constraint).
        if f.get("visibility") == "hidden" or f.get("sensitive") or f.get("source") in (
                "credential_store", "secret_configuration"):
            continue
        if f.get("required") and name not in grouped_names and not collected.get(name):
            steps.append(_value_step_for_field(f, language))
            missing_fields.append(name)

    # 2) Required-group-first flow (Requirement 4) — one criterion-
    # selection step per unsatisfied group, UNLESS a member of that group
    # has already been selected (in which case ask for that member's
    # VALUE next, not the criterion list again).
    for gs in unsatisfied_groups:
        group = gs["group"]
        selected_field_name = gs["selected_field"]
        if selected_field_name:
            member = next((f for f in _group_member_fields(group, input_fields)
                            if f.get("technical_name") == selected_field_name), None)
            if member and not collected.get(selected_field_name):
                steps.append(_value_step_for_field(member, language))
                missing_fields.append(selected_field_name)
                continue
            # value already collected for the selected member -> group
            # will show satisfied on next _groups_status() call; nothing
            # more to ask for this group this turn.
        else:
            steps.append(_criterion_selection_step(group, input_fields, language))
            missing_fields.append(f"__group__.{group.get('name')}")

    ready = not missing_fields
    conversation = contract.get("conversation") or {}
    needs_confirmation = ready and bool(conversation.get("require_confirmation_before_execute"))

    buttons = []
    if ready and not needs_confirmation:
        buttons.append({"action": "execute", "label": "ยืนยัน" if language == "th" else "Submit"})
    elif needs_confirmation:
        buttons.append({"action": "confirm", "label": "ยืนยัน" if language == "th" else "Confirm"})
        buttons.append({"action": "cancel", "label": "ยกเลิก" if language == "th" else "Cancel"})

    validation = [{"field": f["technical_name"], "rule": f.get("validation")}
                  for f in input_fields if f.get("validation")]

    return {
        "title": contract.get("action_name") or contract.get("capability"),
        "steps": steps,
        "buttons": buttons,
        "validation": validation,
        "required": required_names,
        "missing_fields": missing_fields,
        "groups_status": [{"name": gs["group"].get("name"), "rule": gs["group"].get("rule"),
                            "satisfied": gs["satisfied"], "selected_field": gs["selected_field"]}
                           for gs in groups_status],
        "ready_to_execute": ready,
        "needs_confirmation": needs_confirmation,
        "state": state.to_dict(),
        "generator_version": GENERATOR_VERSION,
    }


def apply_user_selection(contract: Dict, state: ConversationState, *, group_name: Optional[str] = None,
                          field: Optional[str] = None, value=None) -> ConversationState:
    """Helper for callers/tests to advance state: either records a
    criterion-selection (`group_name` + `field`, the chosen member of an
    AT_LEAST_ONE/EXACTLY_ONE group) or a plain field value. Pure
    convenience — the generator itself is stateless and re-derives
    everything from whatever state is passed in."""
    if group_name is not None and field is not None:
        state.selected_required_group_fields[group_name] = field
    if field is not None and value is not None:
        state.collected_values[field] = value
    state.updated_at = datetime.now(timezone.utc).isoformat()
    return state


def collected_state_signature(state: ConversationState) -> str:
    """A short hash of the state-dependent parts, for callers that want
    to build their OWN cache key incorporating conversation progress
    (this module itself deliberately does not cache state-dependent
    output — see the Part 17 design note above)."""
    payload = json.dumps({
        "collected_values": state.collected_values,
        "selected_required_group_fields": state.selected_required_group_fields,
    }, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
