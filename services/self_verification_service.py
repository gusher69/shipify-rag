"""Customer Self-Verification Service (2026-08-29) — a lightweight,
self-service alternative to staff-assisted binding
(services/customer_binding_service.py::link_verified). Lets a customer
establish their OWN verified LINE<->CustCode binding immediately, by
proving they know a SECOND piece of information already on file for
that CustCode (phone, then email as a fallback) — never trusting the
CustCode alone as proof of ownership (that's exactly the gap
services/authorization_service.py exists to close; see its module
docstring — this module never touches that gate, it only gives a
customer a legitimate way to pass it).

Looks up the on-file phone/email via the SAME "GetDataCustomer" Business
Action every other customer-data lookup already uses, executed through
an internal, server-side ADMIN-context call — services/action_executor.py
already exempts channel in {"playground", "admin"} from the
Authorization Gate for staff tooling; this reuses that SAME existing
exemption purely to read a comparison value server-side. The raw lookup
result is used ONLY for comparison inside this module and is never
returned to the customer — callers only ever see a match/no-match
boolean plus which factor matched.

The provider's own response already comes back with phone/email masked
(services/business_action_registry.py::sanitize_response_body masks any
PII-shaped field before it's ever written to a trace) — matching only
the still-visible characters, at their original (right-aligned)
position, is deliberate: this module never requires, and never sees,
the customer's full unmasked phone/email either."""
from typing import Dict, Optional

_CUSTOMER_LOOKUP_ACTION_KEY = "getdatacustomer"


def _normalize(value: Optional[str]) -> str:
    if not value:
        return ""
    return "".join(ch for ch in str(value) if ch.isalnum()).lower()


def _matches_masked(claimed: Optional[str], on_file_masked: Optional[str]) -> bool:
    """True when every VISIBLE (non-'*') character of `on_file_masked`
    matches the character at the same position in `claimed`, compared
    RIGHT-ALIGNED (this codebase's masking always hides a PREFIX and
    keeps a trailing run visible — e.g. "****5348", "08***348"; see
    business_action_registry.py::mask_secret/sanitize_response_body).
    Deliberately never requires an exact full-string match, since the
    exact masking width is not a stable contract this module should
    depend on. An on-file value with no visible characters at all
    (fully masked, empty, or missing) can never match anything —
    refuses to "verify" against an empty signal, and an empty claimed
    value never matches either."""
    claimed_n = _normalize(claimed)
    on_file_n = _normalize(on_file_masked)
    if not claimed_n or not on_file_n:
        return False
    visible = [(i, ch) for i, ch in enumerate(on_file_n) if ch != "*"]
    if not visible:
        return False
    offset = len(claimed_n) - len(on_file_n)
    for i, ch in visible:
        j = i + offset
        if j < 0 or j >= len(claimed_n) or claimed_n[j] != ch:
            return False
    return True


def _fetch_customer_record(cust_code: str, *, sb) -> Optional[Dict]:
    """Internal, server-side-only lookup — executes the SAME
    GetDataCustomer action every customer-facing lookup already uses,
    under an admin-channel context (server-constructed here, never
    customer-supplied) so it passes the Authorization Gate via its
    existing staff-tooling exemption without that gate being touched at
    all. Returns the first customer record dict from the provider's own
    `response.data[0]` shape, or None if the action isn't configured,
    the call fails, or no record is returned for this CustCode."""
    from services.business_action_registry import get_registry
    from services.action_executor import ActionExecutor
    registry = get_registry(sb)
    action = registry.get_by_key(_CUSTOMER_LOOKUP_ACTION_KEY)
    if not action:
        return None
    executor = ActionExecutor(sb)
    exec_result = executor.execute(action["id"], {
        "channel": "admin", "collected_slots": {"CustCode": cust_code},
    })
    if exec_result.get("status") != "success":
        return None
    response = ((exec_result.get("result") or {}).get("response") or {})
    data = response.get("data")
    if isinstance(data, list) and data:
        return data[0]
    if isinstance(data, dict):
        return data
    return None


def verify_customer_claim(cust_code: str, *, claimed_phone: Optional[str] = None, sb) -> Dict:
    """Attempts to confirm `cust_code` genuinely belongs to whoever
    supplied `claimed_phone`, by comparing against the (masked) phone on
    file for that CustCode. Returns {"verified": bool,
    "matched_factor": "phone"|None, "reason": str} — `reason` is an
    internal-only diagnostic (safe for a CS handoff note), never shown
    verbatim to the customer (that stays the existing neutral, anti-
    enumeration wording in services/authorization_service.py::
    AUTHORIZATION_DENIED_MESSAGE).

    PHONE ONLY, deliberately — email is intentionally NOT supported here.
    Every field this provider returns is masked server-side before
    anything sees it (sanitize_response_body, applied unconditionally —
    there is no unmasked value available anywhere to compare against,
    by design), keeping only a few TRAILING characters visible. For a
    phone number that's 3-4 trailing DIGITS — a reasonably distinguishing
    signal. For an email address the trailing characters are virtually
    always just the domain suffix (e.g. "****.com"), which nearly every
    real email shares — comparing a claimed email against that would
    "verify" almost anyone, which is not verification at all. Do not add
    an email path back onto this masked field without a genuinely
    different (unmasked, or otherwise higher-entropy) signal to check
    it against."""
    record = _fetch_customer_record(cust_code, sb=sb)
    if not record:
        return {"verified": False, "matched_factor": None,
                "reason": f"no customer record found for CustCode {cust_code!r}"}

    if claimed_phone and _matches_masked(claimed_phone, record.get("CustPhone")):
        return {"verified": True, "matched_factor": "phone", "reason": "claimed phone matched record on file"}

    reason = "claimed phone did not match record on file" if claimed_phone else "no phone was provided to check"
    return {"verified": False, "matched_factor": None, "reason": reason}
