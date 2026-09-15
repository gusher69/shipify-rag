# -*- coding: utf-8 -*-
"""The ONE structured state that travels through the agent graph.

Why a single typed state matters here (task §3/§4): before this package,
several modules on the live path each re-read the customer's raw text and
could reach different conclusions about the same turn — that is the
"split-brain" class of defect the Phase 6 work kept root-causing. The
invariant this state exists to enforce is:

    CURRENT TURN SEMANTICS ARE RESOLVED ONCE.

`resolve_current_turn` fills the semantic fields from the canonical
resolver, and every downstream node consumes those STRUCTURED fields.
A node may look at `raw_message` only for explicit ambiguity recovery,
and must say so in `notes`.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

try:                                   # LangGraph is a hard dependency of
    from typing_extensions import TypedDict, NotRequired  # this package, but
except Exception:                      # the state must stay importable for
    from typing import TypedDict       # tooling that lacks typing_extensions.
    NotRequired = Optional             # type: ignore[assignment]


# ── planned next actions the planner may choose ──────────────────────
# Deliberately a small, closed vocabulary: the planner decides WHAT
# should happen next, and the response layer renders it. The response
# layer never decides for itself which slot is missing.
NEXT_ACTIONS = (
    "ANSWER_PUBLIC_INFO", "ANSWER_POLICY", "ASK_PRODUCT", "ASK_QUANTITY",
    "ASK_SHIPPING_METHOD", "ASK_WEIGHT", "ASK_IDENTIFIER",
    "CHECK_PRIVATE_STATUS", "CALCULATE", "CONVERT_LINK", "PREPARE_WORKFLOW",
    "CLARIFY", "HANDOFF",
)

# ── tool classes (task §12) ──────────────────────────────────────────
TOOL_CLASSES = ("PUBLIC_READ", "PRIVATE_READ", "CALCULATOR",
                "WORKFLOW_PREP", "WRITE_ACTION", "HUMAN_HANDOFF")

AUTH_STATES = ("NOT_REQUIRED", "REQUIRED_MISSING", "REQUIRED_SATISFIED")


class AgentState(TypedDict, total=False):
    """LangGraph channel state. `total=False` because nodes fill it in
    progressively; `new_state()` below seeds every key that later nodes
    read unconditionally."""

    # ── identity / transport ──
    session_id: Optional[str]
    channel: str
    message_id: Optional[str]
    tenant_id: str
    external_user_id: Optional[str]
    sample_source: str

    # ── the turn ──
    raw_message: str
    normalized_message: str
    history: List[Dict[str, Any]]
    customer_context: Dict[str, Any]

    # ── human-language normalisation (services/language) ──
    # `raw_message` is never overwritten; every semantic layer reads
    # `normalized_message` / `normalized_history`, responses and audit
    # may still reference the raw text.
    normalized_history: List[Dict[str, Any]]
    normalization_candidates: List[Dict[str, Any]]
    normalization_confidence: float
    normalization_applied: bool
    normalization_method: List[str]
    normalization_trace: Dict[str, Any]

    # ── canonical semantics (filled ONCE by resolve_current_turn) ──
    conversation_act: str
    primary_intent: str
    secondary_intents: List[str]
    semantic_family: str
    active_journey: Optional[str]
    entities: Dict[str, Any]
    known_slots: Dict[str, Any]
    missing_slots: List[str]
    requested_slot: Optional[str]
    topic_switch: Optional[str]
    correction: bool
    rejection: bool
    precedence_winner: str
    confidence: float

    # ── authority ──
    private_ownership_evidence: Optional[str]
    auth_required: bool
    auth_state: str
    grounding_requirement: str

    # ── plan / tool ──
    planned_action: Optional[str]
    selected_tool: Optional[str]
    tool_class: Optional[str]
    tool_input: Dict[str, Any]
    tool_result: Dict[str, Any]
    rag_context: Optional[str]
    handoff_required: bool

    # ── response ──
    response_plan: Dict[str, Any]
    final_response: str
    routing_type: Optional[str]

    # ── bookkeeping ──
    node_path: List[str]
    notes: List[str]
    errors: List[str]

    # ── internal carriers ──
    # These MUST be declared: LangGraph only propagates channels that the
    # state schema declares, so an undeclared key is silently dropped
    # between nodes. `_decide_context` was being lost exactly that way,
    # which made execute_tool call the engine with an empty context (and
    # therefore re-execute instead of reusing a precomputed result).
    _decide_context: Dict[str, Any]
    _resolution: Any
    _engine_result: Dict[str, Any]


@dataclass
class AgentDecision:
    """The comparable OUTPUT of one graph run.

    This is what shadow mode diffs against the current engine, and what
    owner_test / production mode would answer with. Deliberately small:
    the fields a reviewer would use to say "did the two systems
    understand the same turn and decide the same thing?"."""

    primary_intent: str = "UNKNOWN"
    conversation_act: str = "UNKNOWN"
    active_journey: Optional[str] = None
    entities: Dict[str, Any] = field(default_factory=dict)
    known_slots: Dict[str, Any] = field(default_factory=dict)
    requested_slot: Optional[str] = None
    planned_action: Optional[str] = None
    selected_tool: Optional[str] = None
    tool_class: Optional[str] = None
    routing_type: Optional[str] = None
    auth_required: bool = False
    auth_state: str = "NOT_REQUIRED"
    handoff_required: bool = False
    final_response: str = ""
    confidence: float = 0.0
    node_path: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    # the FULL engine result the graph's execute step produced (routing,
    # developer block, workflow, confirmation gate...). The webhook needs
    # it when the graph is the primary responder; it is not part of the
    # comparable view and is excluded from as_dict().
    engine_result: Optional[Dict[str, Any]] = field(default=None, repr=False, compare=False)
    # what the graph actually read, for audit — raw text stays in state.
    normalized_message: str = ""
    normalization_applied: bool = False
    normalization_method: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d.pop("engine_result", None)
        return d

    @classmethod
    def from_state(cls, state: Dict[str, Any]) -> "AgentDecision":
        return cls(
            primary_intent=state.get("primary_intent") or "UNKNOWN",
            conversation_act=state.get("conversation_act") or "UNKNOWN",
            active_journey=state.get("active_journey"),
            entities=dict(state.get("entities") or {}),
            known_slots=dict(state.get("known_slots") or {}),
            requested_slot=state.get("requested_slot"),
            planned_action=state.get("planned_action"),
            selected_tool=state.get("selected_tool"),
            tool_class=state.get("tool_class"),
            routing_type=state.get("routing_type"),
            auth_required=bool(state.get("auth_required")),
            auth_state=state.get("auth_state") or "NOT_REQUIRED",
            handoff_required=bool(state.get("handoff_required")),
            final_response=state.get("final_response") or "",
            confidence=float(state.get("confidence") or 0.0),
            node_path=list(state.get("node_path") or []),
            errors=list(state.get("errors") or []),
            notes=list(state.get("notes") or []),
            normalized_message=state.get("normalized_message") or "",
            normalization_applied=bool(state.get("normalization_applied")),
            normalization_method=list(state.get("normalization_method") or []),
        )


def effective_message(state: Dict[str, Any]) -> str:
    """What every semantic layer reads: the normalised text, falling
    back to the raw text only if normalisation produced nothing."""
    return state.get("normalized_message") or state.get("raw_message") or ""


def effective_history(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The history with every USER turn normalised the same way as the
    current message, so remembered slots are read from the same wording
    the current turn is."""
    return list(state.get("normalized_history") or state.get("history") or [])


def new_state(message: str, *, history=None, context=None) -> AgentState:
    """Seed a state for one turn from the SAME context dict the LINE
    webhook already builds for DecisionEngine.decide(), so the graph can
    be invoked from the existing call site with no new plumbing."""
    ctx = dict(context or {})
    return {
        "session_id": ctx.get("session_id"),
        "channel": ctx.get("channel") or "line",
        "message_id": ctx.get("message_id"),
        "tenant_id": ctx.get("tenant_id") or "default",
        "external_user_id": ctx.get("external_user_id"),
        "sample_source": ctx.get("sample_source") or "OTHER",
        "raw_message": message or "",
        "normalized_message": "",
        "history": list(history or []),
        "customer_context": dict(ctx.get("customer_context") or {}),
        "normalized_history": list(history or []),
        "normalization_candidates": [],
        "normalization_confidence": 1.0,
        "normalization_applied": False,
        "normalization_method": [],
        "normalization_trace": {},
        "conversation_act": "UNKNOWN",
        "primary_intent": "UNKNOWN",
        "secondary_intents": [],
        "semantic_family": "UNKNOWN",
        "active_journey": None,
        "entities": {},
        "known_slots": {},
        "missing_slots": [],
        "requested_slot": None,
        "topic_switch": None,
        "correction": False,
        "rejection": False,
        "precedence_winner": "NONE",
        "confidence": 0.0,
        "private_ownership_evidence": None,
        "auth_required": False,
        "auth_state": "NOT_REQUIRED",
        "grounding_requirement": "GENERAL",
        "planned_action": None,
        "selected_tool": None,
        "tool_class": None,
        "tool_input": {},
        "tool_result": {},
        "rag_context": None,
        "handoff_required": False,
        "response_plan": {},
        "final_response": "",
        "routing_type": None,
        "node_path": [],
        "notes": [],
        "errors": [],
        # the untouched context, for the adapters that need to call the
        # existing engine exactly as the webhook would.
        "_decide_context": ctx,
    }
