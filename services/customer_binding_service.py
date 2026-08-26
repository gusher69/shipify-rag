"""Customer Channel Binding Service (Task 06B, 2026-08-26) — the ONLY
persistent source of truth for "which real customer account (CustCode)
does this channel identity (e.g. a LINE user id) actually belong to,
VERIFIED". Resolves Task 06's own confirmed gap ("VERIFIED CUSTOMER
BINDING DOES NOT EXIST").

Table: migrations/045_customer_channel_bindings.sql. Deliberately never
reuses user_profiles.cust_code (Task 06's own root cause — a customer-
typed convenience cache with no ownership proof, and Task 06 already
stopped writing it). services/authorization_service.py is the only
consumer that matters for security — it re-queries THIS table on every
sensitive Business Action call, never trusts a caller-supplied
"verified" claim.

VERIFICATION METHOD (Task 06B investigation): no trusted self-service
channel exists in this codebase today — no OTP provider, no customer
login/portal, no LINE Login (see the Task 06B before-code report).
Every binding created today is verification_method="staff_assisted":
an authenticated admin (admin/routes.py's existing auth(request)
session-cookie gate — the SAME trust boundary already protecting the
Credential Store and Business Action config) records a binding after
verifying the customer's identity out-of-band (phone call, existing
records). Real self-service verification (OTP/email/SMS/LINE Login) is
future work — this schema does not need to change for that, only
`method` gains a new value and a new caller.

MULTI-LINK POLICY (confirmed business decision, 2026-08-26): strictly
1:1 — one LINE user holds at most one VERIFIED CustCode at a time, and
one CustCode is VERIFIED-bound to at most one LINE user at a time.
`link_verified()` enforces this by revoking any conflicting existing
verified row before inserting the new one (Phase 23: explicit
relinking, never a silent overwrite); the two partial unique indexes in
the migration are the actual atomicity guarantee against a race
(Phase 30), not merely this pre-revoke step.
"""
from datetime import datetime, timezone
from typing import Dict, List, Optional

_TABLE = "customer_channel_bindings"


class CustomerBindingService:
    def __init__(self, sb):
        self._sb = sb

    def get_verified_binding(self, *, tenant_id: str, channel: str, external_user_id: str) -> Optional[Dict]:
        """The ONE lookup services/authorization_service.py actually
        relies on for a real authorization decision — scoped to tenant +
        channel + external_user_id, exactly like every other isolation-
        sensitive table in this codebase (pending_confirmations,
        webhook_processed_events)."""
        rows = (self._sb.table(_TABLE).select("*")
                .eq("tenant_id", tenant_id).eq("channel", channel)
                .eq("external_user_id", external_user_id).eq("status", "verified")
                .execute().data or [])
        return rows[0] if rows else None

    def get_verified_binding_for_custcode(self, *, tenant_id: str, channel: str, cust_code: str) -> Optional[Dict]:
        rows = (self._sb.table(_TABLE).select("*")
                .eq("tenant_id", tenant_id).eq("channel", channel)
                .eq("cust_code", cust_code).eq("status", "verified")
                .execute().data or [])
        return rows[0] if rows else None

    def _revoke_row(self, row: Dict, *, reason: str, revoked_by: Optional[str]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._sb.table(_TABLE).update({
            "status": "revoked", "revoked_at": now, "revoked_reason": reason, "updated_at": now,
        }).eq("id", row["id"]).execute()

    def link_verified(self, *, tenant_id: str, channel: str, external_user_id: str, cust_code: str,
                       created_by: str, method: str = "staff_assisted") -> Dict:
        """Creates a fresh VERIFIED binding, atomically superseding
        (never silently overwriting — Phase 23) any conflicting existing
        verified row on EITHER side of the relationship: a prior
        verified binding for this SAME external_user_id (this LINE user
        was linked to a different CustCode before) is revoked first; a
        prior verified binding for this SAME cust_code under a
        DIFFERENT external_user_id is also revoked — an explicit,
        staff-authenticated relink always wins over a stale one."""
        existing_for_user = self.get_verified_binding(
            tenant_id=tenant_id, channel=channel, external_user_id=external_user_id)
        if existing_for_user:
            self._revoke_row(existing_for_user, reason="superseded_by_relink", revoked_by=created_by)

        existing_for_custcode = self.get_verified_binding_for_custcode(
            tenant_id=tenant_id, channel=channel, cust_code=cust_code)
        if existing_for_custcode:
            self._revoke_row(existing_for_custcode, reason="superseded_by_relink_different_user",
                              revoked_by=created_by)

        now = datetime.now(timezone.utc).isoformat()
        row = {
            "tenant_id": tenant_id, "channel": channel, "external_user_id": external_user_id,
            "cust_code": cust_code, "status": "verified", "verification_method": method,
            "verified_at": now, "created_at": now, "updated_at": now, "created_by": created_by,
        }
        return self._sb.table(_TABLE).insert(row).execute().data[0]

    def revoke(self, binding_id: str, *, reason: str, revoked_by: Optional[str] = None) -> Dict:
        """Immediate revocation (Phase 24 unlink, Phase 25 admin
        revocation, Phase 31 mid-workflow revocation) — the very next
        authorization check for this LINE user finds no verified row at
        all, since get_verified_binding() only ever matches
        status='verified'."""
        now = datetime.now(timezone.utc).isoformat()
        return self._sb.table(_TABLE).update({
            "status": "revoked", "revoked_at": now, "revoked_reason": reason, "updated_at": now,
        }).eq("id", binding_id).execute().data[0]

    def list_bindings(self, *, tenant_id: str, external_user_id: Optional[str] = None,
                       cust_code: Optional[str] = None) -> List[Dict]:
        """Staff-facing lookup only (admin API) — never used for an
        authorization decision (that's get_verified_binding() alone)."""
        q = self._sb.table(_TABLE).select("*").eq("tenant_id", tenant_id)
        if external_user_id:
            q = q.eq("external_user_id", external_user_id)
        if cust_code:
            q = q.eq("cust_code", cust_code)
        return q.order("created_at", desc=True).execute().data or []


_instance: Optional[CustomerBindingService] = None


def get_customer_binding_service(sb=None) -> CustomerBindingService:
    """Named factory, same pattern as services/pending_confirmation_service.py
    ::get_pending_confirmation_service. A module-level singleton is
    cached only when no sb is given."""
    global _instance
    if sb is not None:
        return CustomerBindingService(sb)
    if _instance is None:
        from admin.routes import get_sb
        _instance = CustomerBindingService(get_sb())
    return _instance
