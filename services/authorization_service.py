"""Authorization Service (Task 06, 2026-08-26; verified binding wired in
Task 06B, 2026-08-26) — the ONE place that answers "is this Business
Action execution allowed to proceed?", enforced inside services/
action_executor.py::ActionExecutor.execute() so it can never be
bypassed by any caller (LINE webhook, Admin Playground, an admin route,
or a hypothetical direct service call) — matching this project's
existing "central, reusable check" convention (e.g. services/
credential_store.py for secrets) rather than scattering per-action
checks.

CONFIRMED ROOT CAUSE (Task 06 investigation): a customer-typed
`CustCode`/`OrderCode`/`ShipmentCode`/`Tracking` (or one silently
inherited from services/action_selection_primitives.py::
IDENTIFIER_MEMORY_FIELDS, a convenience cache with no ownership
verification — see profiles/manager.py's own fix) was previously
treated as sufficient proof of ownership for GetDataCustomer, order/
shipment lookup, requestshippingaddresschange, etc. IDENTIFIER !=
AUTHORIZATION.

Task 06 fixed this by failing closed unconditionally (no verified
binding existed anywhere). Task 06B replaces `_has_verified_binding`
with a real lookup against services/customer_binding_service.py's
`customer_channel_bindings` table — the dedicated, persistent source of
truth, never `user_profiles`. A verified binding existing is not, by
itself, sufficient either: `check_authorization` also enforces RESOURCE
OWNERSHIP — the CustCode this action is actually about to execute with
must match the verified binding's own CustCode, so a verified customer
typing a DIFFERENT customer's CustCode is still denied (never treated
as an identity switch). Admin/Playground contexts (channel in
{"playground", "admin"}) are staff tooling, session-cookie-
authenticated separately by admin/routes.py, and remain exempt — this
module only gates the customer-facing channel.

Every check re-queries the database on every call via the `sb` passed
in from ActionExecutor.execute() (self.registry._sb) — never trusts a
caller-supplied "verified" flag or a pre-resolved context dict, which
would reintroduce exactly the spoofing risk this module exists to
close (Phase 32/33 of the Task 06B spec).
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

# Named so callers that need to react specifically to "no binding exists
# yet" (as opposed to any other denial reason, e.g. an identity-switch
# rejection) can compare against this exact constant instead of
# duplicating the literal string (services/decision_engine.py's
# Self-Service Identity Verification sub-flow, 2026-08-29, is the first
# such caller).
NO_VERIFIED_BINDING_REASON = ("no verified customer binding exists for this channel — failing closed "
                               "(see services/authorization_service.py module docstring)")


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


def _find_custcode_param_name(action: Dict) -> Optional[str]:
    """Returns the actual parameter NAME this action uses for CustCode
    (original case, e.g. "CustCode"), or None if it has none. Every
    real, currently-enabled sensitive action (GetDataCustomer, order/
    shipment lookup, GetUrlProductDetail, RequestShippingAddressChange)
    has one — confirmed live against the production registry during
    Task 06B. A hypothetical future action with an identifier-shaped
    parameter but NO CustCode parameter at all has no locally-checkable
    resource owner and is handled by the caller (see check_authorization
    below: fails closed even with a verified binding)."""
    for p in action.get("parameters") or []:
        if (p.get("input_source") or "customer_message") != "customer_message":
            continue
        if (p.get("name") or "").strip().lower() == "custcode":
            return p.get("name")
    return None


def _resolve_verified_binding(context: Dict, sb) -> Optional[Dict]:
    """Re-queries services/customer_binding_service.py directly — never
    trusts a pre-resolved "verified" claim handed in by the caller.
    Requires `sb` (passed from ActionExecutor.execute()'s own registry)
    plus tenant_id/channel/external_user_id in `context` — all three are
    server-derived (webhook.py sets external_user_id from the LINE
    webhook's own HMAC-verified event, never from message text), so this
    is safe to trust. Returns None (never authorized) if any of these is
    missing, e.g. an old test/caller that predates Task 06B."""
    if sb is None:
        return None
    tenant_id = context.get("tenant_id")
    channel = context.get("channel")
    external_user_id = context.get("external_user_id")
    if not (tenant_id and channel and external_user_id):
        return None
    from services.customer_binding_service import get_customer_binding_service
    return get_customer_binding_service(sb).get_verified_binding(
        tenant_id=tenant_id, channel=channel, external_user_id=external_user_id)


def check_authorization(action: Dict, context: Optional[Dict] = None, sb=None) -> Dict:
    """Returns {"authorized": bool, "reason": str}. Called once, from
    inside ActionExecutor.execute() — the single choke point every
    caller (LINE, Playground, admin routes, any future direct service
    call) must go through to actually run a Business Action — so this
    can never be bypassed by routing around the conversational layer.

    `sb` is optional so every pre-Task-06B caller/test keeps working
    unchanged (no sb -> no binding can ever be resolved -> same
    universal fail-closed behavior Task 06 already shipped); ActionExecutor
    always passes its own registry's sb."""
    context = context or {}
    if _is_admin_context(context):
        return {"authorized": True, "reason": "admin/playground context — staff tooling, separately authenticated"}
    if not requires_verified_identity(action):
        return {"authorized": True, "reason": "action does not require a customer/order/shipment identifier"}

    binding = _resolve_verified_binding(context, sb)
    if not binding:
        return {
            "authorized": False,
            "reason": NO_VERIFIED_BINDING_REASON,
        }

    custcode_param = _find_custcode_param_name(action)
    if not custcode_param:
        return {
            "authorized": False,
            "reason": "action has no CustCode-scoped parameter — resource ownership cannot be "
                      "locally verified, failing closed even with a verified binding",
        }

    requested_custcode = (context.get("collected_slots") or {}).get(custcode_param)
    if requested_custcode and requested_custcode != binding.get("cust_code"):
        # Phase 15/17/18: a verified customer typing a DIFFERENT
        # customer's CustCode is never treated as an identity switch —
        # denied exactly like an unverified request would be.
        return {
            "authorized": False,
            "reason": "requested CustCode does not match the verified binding — identity switch rejected",
        }

    return {"authorized": True, "reason": "verified customer binding present and resource CustCode matches"}


# Neutral, existing-product-tone denial — never confirms or denies
# whether the specific identifier/account exists (Phase 14/26: avoid
# facilitating enumeration), never sounds like an infrastructure error
# (Phase 13/27), and is reused verbatim by every caller so the wording
# stays centralized in exactly one place.
AUTHORIZATION_DENIED_MESSAGE = "ขออภัยค่ะ ไม่สามารถยืนยันสิทธิ์ในการเข้าถึงข้อมูลรายการนี้ได้ในขณะนี้ รบกวนติดต่อเจ้าหน้าที่เพื่อยืนยันตัวตนก่อนนะคะ"
