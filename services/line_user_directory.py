"""P3.1 — read-only directory of LINE users for the Admin viewer.

Joins two EXISTING tables, never a new one:
    user_profiles.line_user_id  ==  customer_channel_bindings.external_user_id
for channel = 'line'. A customer identity is VERIFIED only when a
customer_channel_bindings row has status = 'verified' — user_profiles.cust_code
(a legacy customer-typed convenience cache) is never treated as verified here.

No writes. Query shape is fixed at 2 reads for the list (profiles + all
verified line bindings) and 3 reads for the detail (profile + this user's
bindings + this user's newest LINE session) — never one binding query per
row.
"""
from typing import Dict, List, Optional

_PROFILE_HARD_CAP = 1000        # viewer, not an export — bounded fetch
_LIST_DEFAULT_LIMIT = 50

_PROFILE_COLS = ("line_user_id,display_name,first_seen,message_count,"
                 "conversation_count,updated_at,created_at")
_BINDING_COLS = "external_user_id,cust_code,status,verification_method,verified_at,updated_at"


def _sb(sb=None):
    if sb is not None:
        return sb
    from services.supabase_client import get_supabase
    return get_supabase()


def _mask_line_user_id(uid: Optional[str]) -> str:
    uid = uid or ""
    return f"{uid[:5]}...{uid[-4:]}" if len(uid) > 12 else uid


def _last_seen(profile: Dict) -> Optional[str]:
    return profile.get("updated_at") or profile.get("first_seen") or profile.get("created_at")


def list_line_users(search: Optional[str] = None, status: Optional[str] = None,
                     limit: int = _LIST_DEFAULT_LIMIT, sb=None) -> Dict:
    """{"users": [...], "summary": {"total", "verified", "unverified"}}.
    `status` in (None|"all"|"verified"|"unverified"). `search` matches a
    display_name substring (case-insensitive) OR a verified CustCode
    substring. Read-only."""
    client = _sb(sb)

    # Query 1 — every VERIFIED LINE binding (small table; one shot).
    verified_binds = (client.table("customer_channel_bindings").select(_BINDING_COLS)
                      .eq("channel", "line").eq("status", "verified")
                      .execute().data or [])
    bmap = {b["external_user_id"]: b for b in verified_binds if b.get("external_user_id")}

    # Query 2 — LINE user profiles, most-recently-active first, bounded.
    profiles = (client.table("user_profiles").select(_PROFILE_COLS)
                .order("updated_at", desc=True)
                .execute().data or [])[:_PROFILE_HARD_CAP]

    total = len(profiles)
    verified_count = sum(1 for p in profiles if p.get("line_user_id") in bmap)

    s = (search or "").strip().lower()
    st = (status or "all").lower()
    rows: List[Dict] = []
    for p in profiles:
        uid = p.get("line_user_id") or ""
        binding = bmap.get(uid)
        is_verified = binding is not None
        if st == "verified" and not is_verified:
            continue
        if st == "unverified" and is_verified:
            continue
        cust_code = binding["cust_code"] if is_verified else None
        if s and s not in (p.get("display_name") or "").lower() \
                and s not in (cust_code or "").lower():
            continue
        rows.append({
            "line_user_id": uid,
            "line_user_id_masked": _mask_line_user_id(uid),
            "display_name": p.get("display_name") or "",
            "cust_code": cust_code,
            "verified": is_verified,
            "status_label": "ยืนยันแล้ว" if is_verified else "ยังไม่ยืนยัน",
            "last_seen": _last_seen(p),
            "message_count": p.get("message_count") or 0,
        })

    rows.sort(key=lambda r: (r["last_seen"] or ""), reverse=True)
    return {
        "users": rows[: max(1, int(limit or _LIST_DEFAULT_LIMIT))],
        "summary": {"total": total, "verified": verified_count,
                    "unverified": max(0, total - verified_count)},
    }


def get_line_user_detail(line_user_id: str, sb=None) -> Optional[Dict]:
    """Full durable profile + verified-binding status + newest LINE
    session summary for one user. Read-only. None if no profile row."""
    if not line_user_id:
        return None
    client = _sb(sb)

    prof_rows = (client.table("user_profiles").select("*")
                 .eq("line_user_id", line_user_id).execute().data or [])
    if not prof_rows:
        return None
    p = prof_rows[0]

    binds = (client.table("customer_channel_bindings").select(_BINDING_COLS + ",created_at")
             .eq("channel", "line").eq("external_user_id", line_user_id)
             .order("verified_at", desc=True).execute().data or [])
    verified = next((b for b in binds if b.get("status") == "verified"), None)

    session_summary = {"has_active": False, "last_interaction": None}
    try:
        sess = (client.table("ai_sessions").select("id,updated_at,last_message_at,status")
                .eq("line_user_id", line_user_id).eq("channel", "line")
                .is_("deleted_at", "null").order("updated_at", desc=True)
                .execute().data or [])
        if sess:
            session_summary["last_interaction"] = sess[0].get("last_message_at") or sess[0].get("updated_at")
            session_summary["has_active"] = (sess[0].get("status") == "running")
    except Exception:
        pass  # optional — never block the detail view

    return {
        "profile": {
            "line_user_id": p.get("line_user_id"),
            "display_name": p.get("display_name") or "",
            "first_seen": p.get("first_seen") or p.get("created_at"),
            "last_seen": _last_seen(p),
            "message_count": p.get("message_count") or 0,
            "conversation_count": p.get("conversation_count") or 0,
        },
        "binding": {
            "verified": verified is not None,
            "cust_code": verified["cust_code"] if verified else None,
            "status_label": "ยืนยันแล้ว" if verified else "ยังไม่ยืนยัน",
            "verification_method": verified.get("verification_method") if verified else None,
            "verified_at": verified.get("verified_at") if verified else None,
        },
        "session": session_summary,
    }
