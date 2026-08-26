"""Authorization Service (Task 06, 2026-08-26) — the ONE place that
answers "is this Business Action execution allowed to proceed?", enforced
inside services/action_executor.py::ActionExecutor.execute() so it can
never be bypassed by any caller (LINE webhook, Admin Playground, an admin
route, or a hypothetical direct service call) — matching this project's
existing "central, reusable check" convention (e.g. services/
credential_store.py for secrets) rather than scattering per-action
checks.

CONFIRMED ROOT CAUSE (Task 06 investigation): there is currently no
verified LINE-user-to-customer-account binding anywhere in this
codebase — no account-linking table, no OTP/login flow, nothing. The
only "identity" a chat message carries is `CustCode`/`OrderCode`/
`ShipmentCode`/`Tracking` typed by the customer (or silently inherited
from services/action_selection_primitives.py::IDENTIFIER_MEMORY_FIELDS,
which was itself a convenience cache with no ownership verification —
see profiles/manager.py's own fix). Every Business Action that reads or
mutates customer-specific data (GetDataCustomer, order/shipment lookup,
requestshippingaddresschange, ...) treated a correctly-FORMATTED
identifier as sufficient proof of ownership, which it never was
(IDENTIFIER != AUTHORIZATION).

Given no verified binding exists, and per this task's own explicit
instruction not to invent a homemade OTP/verification flow, the correct
behavior for a customer-facing (LINE) request to any action requiring a
customer/order/shipment identifier is to FAIL CLOSED — deny with a safe,
neutral message — rather than silently execute on an unverified claim.
Admin/Playground contexts (channel == "playground") are staff tooling,
session-cookie-authenticated separately by admin/routes.py, and are
exempt: this module only gates the customer-facing channel.

This is deliberately the SMALLEST viable fix, and deliberately an
explicit extension point: once real account-linking infrastructure
exists (a separate, dedicated project), `_has_verified_binding` below is
the ONE function to update — everything else in this module (the
sensitivity classification, the channel check, the deny response) stays
unchanged.
"""
from typing import Dict, Optional

# Generic identifier CONCEPTS (never a specific customer's value) that,
# when REQUIRED on a Business Action, mean "this action needs to know
# WHICH customer/order/shipment account it's acting on" — i.e., the
# action is customer-specific by construction, regardless of what it's
# actually named or what category an admin filed it under. Matching by
# parameter NAME (not by action_key/category) is deliberately generic —
# any current or future Business Action configured with one of these as
# a required, customer-supplied parameter is customer-specific, not just
# the six actions confirmed during this investigation.
_IDENTIFIER_PARAM_NAMES = {
    "custcode", "ordercode", "shipmentcode", "tracking",
    "custemail", "custphone", "custname",
}

# The known, explicitly-authenticated staff channels — every one of
# these call sites sits behind admin/routes.py's own session-cookie
# `auth(request)` gate (ADMIN_USERNAME/ADMIN_PASSWORD) before it ever
# reaches the Action Executor: "playground" (AI Playground / Prompt
# Studio), and "admin" (the generic manual Business Action executor at
# POST /admin/api/business-actions/{id}/execute, and the ERP Test
# Harness's "live" mode, services/erp_test_harness.py::run_erp_test —
# both confirmed reachable only via an already-`auth(request)`-gated
# route). Every OTHER channel value (including "line" and an absent/
# unknown channel) is treated as customer-facing and subject to the
# fail-closed check below. Fail-closed by DEFAULT (an unrecognized or
# missing channel is never treated as trusted) is deliberate — see the
# module docstring.
_ADMIN_CHANNELS = {"playground", "admin"}


def requires_verified_identity(action: Dict) -> bool:
    """True when `action` has at least one customer_message-sourced
    parameter whose name is a known identifier concept — i.e., it looks
    up or mutates data scoped to a specific customer/order/shipment.
    Deliberately triggers on an OPTIONAL identifier parameter too, not
    only a required one: confirmed live against the real production
    GetDataCustomer action, whose CustCode/CustEmail/CustName/CustPhone
    are all individually OPTIONAL (any ONE of them is enough to look up
    a customer and return wallet/coupon/email/phone/name) — checking
    `required` only would have left the single most sensitive action in
    the entire registry completely unprotected. A Business Action with
    no such parameter at all (e.g. a plain "notify customer support"
    action with only a free-text message) is unaffected."""
    for p in action.get("parameters") or []:
        if (p.get("input_source") or "customer_message") != "customer_message":
            continue
        if (p.get("name") or "").strip().lower() in _IDENTIFIER_PARAM_NAMES:
            return True
    return False


def _is_admin_context(context: Dict) -> bool:
    return (context or {}).get("channel") in _ADMIN_CHANNELS


def _has_verified_binding(context: Dict) -> bool:
    """Extension point: returns True only when the requesting principal
    has been through a REAL, verified account-linking/authentication
    flow for the customer account the request names — always False
    today, since no such flow exists anywhere in this codebase (confirmed
    via Task 06's investigation: no account-linking table, no OTP/login
    mechanism). Update THIS function alone once real account-linking
    infrastructure is built; nothing else in this module should need to
    change."""
    return False


def check_authorization(action: Dict, context: Optional[Dict] = None) -> Dict:
    """Returns {"authorized": bool, "reason": str}. Called once, from
    inside ActionExecutor.execute() — the single choke point every
    caller (LINE, Playground, admin routes, any future direct service
    call) must go through to actually run a Business Action — so this
    can never be bypassed by routing around the conversational layer."""
    context = context or {}
    if _is_admin_context(context):
        return {"authorized": True, "reason": "admin/playground context — staff tooling, separately authenticated"}
    if not requires_verified_identity(action):
        return {"authorized": True, "reason": "action does not require a customer/order/shipment identifier"}
    if _has_verified_binding(context):
        return {"authorized": True, "reason": "verified customer binding present"}
    return {
        "authorized": False,
        "reason": "no verified customer binding exists for this channel — failing closed "
                   "(see services/authorization_service.py module docstring)",
    }


# Neutral, existing-product-tone denial — never confirms or denies
# whether the specific identifier/account exists (Phase 14/26: avoid
# facilitating enumeration), never sounds like an infrastructure error
# (Phase 13/27), and is reused verbatim by every caller so the wording
# stays centralized in exactly one place.
AUTHORIZATION_DENIED_MESSAGE = "ขออภัยค่ะ ไม่สามารถยืนยันสิทธิ์ในการเข้าถึงข้อมูลรายการนี้ได้ในขณะนี้ รบกวนติดต่อเจ้าหน้าที่เพื่อยืนยันตัวตนก่อนนะคะ"
