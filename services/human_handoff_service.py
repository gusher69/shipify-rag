"""Human Handoff notification delivery (2026-08-13).

Generic, Platform-First: resolves whichever ENABLED Business Action's
operation_type is NOTIFY/NOTIFICATION (services/erp_test_harness.py::
infer_operation_type) — never hardcoded to the action_key "sendlinenotics"
— and executes it via the existing, unmodified ActionExecutor (services/
action_executor.py), the exact same execution path every other live ERP
call in this codebase already goes through. A different customer whose
"send a CS notification" action has a different name/key gets identical
behavior with zero code changes here, per this platform's "Configuration
over Code" principle.

Never touches a credential value directly — SecretCode (or any other
credential_store/secret_configuration-sourced parameter) is resolved
server-side by ActionExecutor/BusinessActionRegistry exactly as for every
other Business Action; this module only ever builds the customer-facing
`Message` text.

Deliberately bypasses the standalone command-confirmation gate
(services/decision_engine.py::_requires_confirmation, applied only inside
DecisionEngine._execute_selected_action when a customer explicitly invokes
an action by name) — a Human Handoff notification is never something the
customer is casually triggering; the real safety gates are (a) the
Decision Engine's own, pre-existing escalation trigger (explicit request /
AI Policy escalation / refusal / max-retry) deciding routing_type ==
"HUMAN_HANDOFF" in the first place, and (b) the caller's own duplicate-
notification check (see services/session_service.py's handoff-state
helpers) — never a "ยืนยันหรือไม่" prompt to the customer.
"""
from typing import Dict, Optional

from services.business_action_registry import get_registry
from services.action_executor import get_action_executor
from services.erp_test_harness import infer_operation_type

_NOTIFICATION_OPERATION_TYPES = ("NOTIFY", "NOTIFICATION")

_REASON_LABELS = {
    "user_requested_human": "ลูกค้าขอคุยกับเจ้าหน้าที่โดยตรง",
    "ai_policy_escalation": "AI Policy ตัดสินใจส่งต่อให้เจ้าหน้าที่ (ความมั่นใจต่ำ/ตรวจพบความไม่พอใจ)",
    "user_refused_to_provide_information": "ลูกค้าปฏิเสธที่จะให้ข้อมูลที่จำเป็น",
    "max_retry_exceeded": "ถามข้อมูลที่จำเป็นซ้ำครบจำนวนครั้งที่กำหนดแล้ว",
}


def find_notification_action(registry=None) -> Optional[Dict]:
    """The single enabled Business Action whose operation_type resolves to
    NOTIFY/NOTIFICATION (e.g. this customer's SendLineNotiCS). Returns the
    full action dict (business_action_registry.py::get_full) or None if no
    such action is configured/enabled."""
    reg = registry or get_registry()
    for action in reg.enabled_actions():
        full = reg.get_full(action["id"], mask_secrets=False)
        if not full:
            continue
        if infer_operation_type(full) in _NOTIFICATION_OPERATION_TYPES:
            return full
    return None


def build_handoff_message(*, reason: str, customer_name: Optional[str], cust_code: Optional[str],
                           line_user_id: Optional[str], customer_message: str,
                           conversation_summary: Optional[str] = None) -> str:
    """Builds the CS-facing notification text. Deterministic string
    formatting only — never includes SecretCode, tokens, or any other
    credential (this function is never given one in the first place; only
    customer-facing/conversational context reaches it)."""
    reason_label = _REASON_LABELS.get(reason, reason or "ต้องการความช่วยเหลือจากเจ้าหน้าที่")
    lines = [
        "[AI HANDOFF]",
        "ลูกค้าต้องการติดต่อเจ้าหน้าที่",
        "",
        f"Customer: {customer_name or 'Unknown'}",
        f"CustCode: {cust_code or 'Unknown'}",
        f"LINE User: {line_user_id or 'Unknown'}",
        "",
        f"คำถามล่าสุด: {customer_message or '-'}",
        "",
        f"เหตุผล: {reason_label}",
    ]
    if conversation_summary:
        lines += ["", f"Conversation: {conversation_summary}"]
    return "\n".join(lines)


def send_handoff_notification(*, reason: str, customer_name: Optional[str] = None,
                               cust_code: Optional[str] = None, line_user_id: Optional[str] = None,
                               customer_message: str = "", conversation_summary: Optional[str] = None,
                               registry=None, executor=None) -> Dict:
    """Sends a real Human Handoff notification through whichever
    NOTIFY/NOTIFICATION Business Action is configured (e.g. SendLineNotiCS)
    via the existing ActionExecutor. Returns {"sent": bool, "action_key":
    str|None, "error": str|None} — never raises, never returns or logs a
    credential value."""
    reg = registry or get_registry()
    action = find_notification_action(reg)
    if not action:
        return {"sent": False, "action_key": None, "error": "no_notification_action_configured"}

    message = build_handoff_message(
        reason=reason, customer_name=customer_name, cust_code=cust_code,
        line_user_id=line_user_id, customer_message=customer_message,
        conversation_summary=conversation_summary,
    )
    exec_ = executor or get_action_executor()
    result = exec_.execute(action["id"], context={"collected_slots": {"Message": message}})
    if result.get("status") == "success":
        return {"sent": True, "action_key": action.get("action_key"), "error": None}
    return {"sent": False, "action_key": action.get("action_key"), "error": result.get("error") or "execution_failed"}
