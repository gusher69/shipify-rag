# -*- coding: utf-8 -*-
"""Thin, typed wrappers over the platform's already-validated primitives.

NOTHING in this file re-implements a business decision. Each function is
a call into the module that already owns that decision, plus the minimum
shaping needed to put the result into AgentState. If you find yourself
wanting to add a Thai pattern, a routing rule, a policy verdict or an
auth check HERE, it belongs in the owning service instead — that is the
whole point of the boundary.

Reused primitives, and who owns each decision:

  semantics / entities   services/conversation_semantics.py  (the ONE
                         central interpreter; product / quantity+unit /
                         method / question-clause separation)
  canonical resolution   services/conversation_resolution.py (P1: act,
                         intent, known/missing slots, requested slot,
                         explicit precedence order)
  private authority      services/decision_engine.py
                         _classify_private_state_inquiry (deterministic)
                         _private_ownership_evidence (positive evidence)
  cancellation kind      services/operational_change_flow.py
  tool execution         services/decision_engine.py::DecisionEngine.decide
                         — routing, Business Action selection, RAG, ERP,
                         calculator, link conversion, Human CS. The graph
                         PLANS; this engine still EXECUTES.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

# ── canonical semantics / resolution ─────────────────────────────────
from services.conversation_resolution import resolve_conversation as _resolve
from services.conversation_semantics import (
    derive_active_frame as _derive_active_frame,
    PUBLIC_INFO_FAMILIES as _PUBLIC_INFO_FAMILIES,
)
from services.decision_engine import (
    DecisionEngine,
    _classify_private_state_inquiry as _classify_private,
    _private_ownership_evidence as _ownership_evidence,
)
from services.operational_change_flow import classify_cancellation as _cancel_kind

PUBLIC_INFO_FAMILIES = _PUBLIC_INFO_FAMILIES


# ── TOOL CATALOGUE (task §11/§12) ────────────────────────────────────
# A declarative contract per tool. `engine_hint` records which existing
# capability the Decision Engine will actually exercise — the graph does
# not re-route, it declares intent and the engine executes.
TOOL_CONTRACTS: Dict[str, Dict[str, Any]] = {
    "PUBLIC_RAG": {
        "description": "Answer a public policy / FAQ / how-to question from the "
                       "knowledge base.",
        "tool_class": "PUBLIC_READ", "requires_auth": False, "side_effect": False,
        "grounding_source": "knowledge_base",
        "failure_behavior": "truthful no-information, never invention",
        "engine_hint": "rag",
    },
    "PRODUCT_POLICY": {
        "description": "Answer whether a kind of goods may be imported/shipped.",
        "tool_class": "PUBLIC_READ", "requires_auth": False, "side_effect": False,
        "grounding_source": "trusted_policy_evidence",
        "failure_behavior": "no eligibility verdict without trusted evidence",
        "engine_hint": "product_policy",
    },
    "CONTACT_INFO": {
        "description": "Return the company's public contact / website details.",
        "tool_class": "PUBLIC_READ", "requires_auth": False, "side_effect": False,
        "grounding_source": "committed_contact_block",
        "failure_behavior": "committed fallback block",
        "engine_hint": "service_intent",
    },
    "SHIPPING_CALCULATOR": {
        "description": "Estimate shipping cost from weight / dimensions / method.",
        "tool_class": "CALCULATOR", "requires_auth": False, "side_effect": False,
        "grounding_source": "committed_rate_table",
        "failure_behavior": "collect the missing rate basis, never guess a price",
        "engine_hint": "shipping_estimate",
    },
    "CBM_CALCULATOR": {
        "description": "Convert dimensions to volumetric CBM.",
        "tool_class": "CALCULATOR", "requires_auth": False, "side_effect": False,
        "grounding_source": "committed_formula",
        "failure_behavior": "ask for the missing dimension",
        "engine_hint": "shipping_estimate",
    },
    "LINK_CONVERSION": {
        "description": "Convert a Taobao/1688/Tmall product link.",
        "tool_class": "PUBLIC_READ", "requires_auth": False, "side_effect": False,
        "grounding_source": "erp_link_service",
        "failure_behavior": "state unsupported domain truthfully",
        "engine_hint": "link_conversion",
    },
    "PRIVATE_ERP": {
        "description": "Read the customer's OWN record (shipment / order / "
                       "wallet / invoice) from the ERP.",
        "tool_class": "PRIVATE_READ", "requires_auth": True, "side_effect": False,
        "grounding_source": "erp_live_read",
        "failure_behavior": "collect the required identifier; never invent a status",
        "engine_hint": "business_action",
    },
    "WORKFLOW": {
        "description": "Prepare an operational request (cancellation, address "
                       "change, claim) for staff — collect the identifier only.",
        "tool_class": "WORKFLOW_PREP", "requires_auth": True, "side_effect": False,
        "grounding_source": "operational_change_flow",
        "failure_behavior": "never claim the operation completed",
        "engine_hint": "operational_change",
    },
    "HUMAN_CS": {
        "description": "Escalate to a human agent with the conversation context.",
        "tool_class": "HUMAN_HANDOFF", "requires_auth": False, "side_effect": True,
        "grounding_source": "handoff_payload",
        "failure_behavior": "always safe; the fallback of last resort",
        "engine_hint": "handoff",
    },
}

# families that can only ever be answered from public company knowledge.
_PUBLIC_TOOL_BY_FAMILY = {
    "PRODUCT_POLICY": "PRODUCT_POLICY",
    "CONTACT_INFO": "CONTACT_INFO",
    "WEBSITE_LINK_REQUEST": "CONTACT_INFO",
    "SHIPPING_ESTIMATE": "SHIPPING_CALCULATOR",
    "LINK_CONVERSION": "LINK_CONVERSION",
    "CANCELLATION_POLICY": "PUBLIC_RAG",
}
_PRIVATE_FAMILIES = frozenset({"SHIPMENT_STATUS", "MY_COUPONS", "INVOICE"})
_WORKFLOW_FAMILIES = frozenset({"ADDRESS_CHANGE", "CANCELLATION_OPERATION"})


# ── one canonical parse of the current turn ──────────────────────────
def resolve_turn(message: str, history: Optional[List[Dict]] = None,
                 context: Optional[Dict] = None):
    """THE single semantic read of this turn (task §4/§6).

    Delegates wholly to services/conversation_resolution.py, which itself
    delegates to the one central interpreter. The graph never parses Thai.
    """
    return _resolve(message or "", history or [], context or {})


def active_frame(history: Optional[List[Dict]]):
    """The legacy text-derived frame — still the platform's sole READ
    authority for conversation state (no cutover was performed)."""
    return _derive_active_frame(history or [])


def is_journey_opener(message: str) -> bool:
    """REAL LINE 2026-09-16 — is THIS turn an explicit fresh import opener
    ("อยากสั่งของจากจีน 20 คู่")? The ONE detector derive_active_frame uses
    as its journey boundary, exposed so the graph's state merge does not
    carry a previous journey's measurements into a turn that starts over."""
    from services.conversation_semantics import _is_import_interest
    return bool(_is_import_interest(message or ""))


# ── deterministic private/public authority ───────────────────────────
def private_authority(message: str, history=None,
                      customer_context=None) -> Tuple[Optional[Dict], Optional[str]]:
    """(deterministic private-state classification, positive ownership
    evidence). Both come from services/decision_engine.py unchanged —
    this is the platform's auth boundary and the graph does not get its
    own opinion about it."""
    try:
        det = _classify_private(message or "")
    except Exception:
        det = None
    try:
        evi = _ownership_evidence(message or "", history, customer_context or {})
    except Exception:
        evi = None
    return det, evi


def cancellation_kind(message: str) -> Optional[str]:
    try:
        return _cancel_kind(message or "")
    except Exception:
        return None


def is_public_family(family: str) -> bool:
    return family in PUBLIC_INFO_FAMILIES


def tool_for_family(family: str, *, is_private: bool, handoff: bool) -> str:
    """Map a resolved family to ONE declared tool. This is a mapping, not
    a routing decision: the Decision Engine still performs the actual
    Business-Action selection when the tool is executed."""
    if handoff:
        return "HUMAN_CS"
    if family in _WORKFLOW_FAMILIES:
        return "WORKFLOW"
    if is_private or family in _PRIVATE_FAMILIES:
        return "PRIVATE_ERP"
    return _PUBLIC_TOOL_BY_FAMILY.get(family, "PUBLIC_RAG")


# ── execution ────────────────────────────────────────────────────────
_ENGINE: Optional[DecisionEngine] = None


def get_engine() -> DecisionEngine:
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = DecisionEngine()
    return _ENGINE


def execute(message: str, history: Optional[List[Dict]],
            context: Optional[Dict]) -> Dict[str, Any]:
    """Execute the turn through the EXISTING validated orchestrator.

    The graph owns understanding, memory, planning, validation, response
    shaping and safety. Execution — Business Action selection, ERP calls,
    RAG retrieval, calculator, link conversion, Human CS escalation —
    stays here, in the engine that the whole regression corpus already
    covers. That is what keeps this package from becoming a second
    decision engine.
    """
    return get_engine().decide(message or "", history=history or [],
                               context=dict(context or {}))


def reset_engine_for_tests() -> None:
    global _ENGINE
    _ENGINE = None


# ── SAFETY VOCABULARY (task §23 hard requirements) ───────────────────
# The four never-zero checks are expressed over CUSTOMER-FACING TEXT, so
# they need the same Thai vocabulary the platform's own live-tier
# evaluator and owner-contract tests already use. It lives HERE, at the
# adapter boundary, rather than inside a node: nodes stay language-free
# so "the graph does not re-implement the interpreter" is mechanically
# checkable (tests/test_agent_graph.py asserts exactly that).
import re as _re

# a PAST-TENSE claim that an action already happened. Future intent
# ("แอดมินจะดำเนินการให้นะคะ") is fine and matches the CS team's scripts.
FALSE_COMPLETION_RE = _re.compile(
    r"เรียบร้อยแล้ว|ดำเนินการให้แล้ว|ดำเนินการเรียบร้อย"
    r"|(?:ยกเลิก|แก้ไข|เปลี่ยน|ลบ|รวม|ถอน|คืนเงิน|อัปเดต|ออกใบกำกับ)\S{0,10}(?:ให้)?(?:เรียบร้อย)?แล้ว"
    r"|(?:ติดต่อ|แจ้ง|สอบถาม)(?:ร้าน|ทางร้าน|โกดัง)\S{0,6}(?:ให้)?แล้ว")
# a concrete record identifier or date in a reply backed by no read.
PRIVATE_LEAK_RE = _re.compile(
    r"(?:PO|POS|PA|PE|FT|FE|SA|SP)\d{3,}|\d{9,}|\d{1,2}/\d{1,2}/\d{2,4}")
IDENTITY_ASK_RE = _re.compile(r"รหัสลูกค้า|ยืนยันตัวตน|เลขสมาชิก")
