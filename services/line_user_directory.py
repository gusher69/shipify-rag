"""P3.1 / P3.2 — read-only directory of LINE users for the Admin viewer.

Joins two EXISTING tables, never a new one:
    user_profiles.line_user_id  ==  customer_channel_bindings.external_user_id
for channel = 'line'. A customer identity is VERIFIED only when a
customer_channel_bindings row has status = 'verified' — user_profiles.cust_code
(a legacy customer-typed convenience cache) is never treated as verified here.

P3.2 (Conversation History Viewer) adds two more read-only functions that
reuse the SAME ai_sessions / ai_session_messages tables the AI Playground
and line_bot/webhook.py already persist every turn to (a LINE conversation
IS an ai_sessions row with channel='line', line_user_id set; each turn IS
an ai_session_messages row). No new transcript storage, no migration. The
internal per-turn trace (ai_session_traces: chunks / prompt / policy / erp
payloads) and pipeline events (ai_session_events) are simply never queried
here, so no system/RAG/debug data can reach the Admin transcript view.

REAL LINE USERS ONLY (P3.1 data-integrity, 2026-09-01). user_profiles also
holds synthetic rows — AI Playground / Real User Journey UAT ("playground:*"
labels) and developer probe scripts that posted fabricated "Uprobe…" /
"Uhardgate…" ids straight at the webhook. A profile is surfaced here only
when BOTH hold:
  1. provenance — it has a real LINE channel footprint: an ai_sessions row
     with channel='line', or a customer_channel_bindings row with
     channel='line'. (This is the primary signal; the webhook is the only
     thing that writes channel='line' sessions for a HMAC-verified userId.)
  2. shape — line_user_id matches ^U[0-9a-f]{32}$, the LINE userId format.
     Secondary only: it removes the probe scripts that also created
     channel='line' sessions. Never the sole rule.

No writes. Query shape:
  * list_line_users        — 3 reads  (line bindings + profiles + line-session ids)
  * get_line_user_detail   — 4 reads  (line-session ids + profile + bindings + newest session)
  * list_user_sessions     — 1 read   (this user's line sessions)
  * get_user_session_messages — 2 reads (ownership check + bounded transcript)
never one binding/message query per row.
"""
import re
from typing import Dict, List, Optional, Iterable

_PROFILE_HARD_CAP = 5000        # viewer, not an export — bounded but above
                               # current deployment volume so summary counts
                               # stay accurate; revisit if a tenant exceeds it
_LIST_DEFAULT_LIMIT = 50
_SESSION_LIST_CAP = 100         # sessions shown per user (most recent first)
_TRANSCRIPT_MSG_CAP = 500       # bounded transcript read — never unbounded

# Only genuinely customer-visible roles are ever surfaced. ai_session_messages.role
# is 'user' | 'assistant' by schema (migrations/016), but the whitelist is explicit
# so any future internal role ('system', 'tool', 'developer', …) can never leak.
_VISIBLE_ROLES = ("user", "assistant")

# NOTE: user_profiles has NO `updated_at` column — its "last activity" field
# is `last_active` (written every turn by profiles/manager.py), with
# first_seen / created_at as fallbacks. Selecting a non-existent column makes
# PostgREST 42703 the whole request.
_PROFILE_COLS = ("line_user_id,display_name,first_seen,message_count,"
                 "conversation_count,last_active,created_at,"
                 "lead_stage,lead_score,lead_reasons,lead_stage_updated_at,"
                 "sentiment_status,sentiment_reasons,sentiment_updated_at,"
                 "negative_last_detected_at,negative_last_alert_at")
_BINDING_COLS = "external_user_id,cust_code,status,verification_method,verified_at,updated_at"
_SESSION_COLS = ("id,name,channel,line_user_id,message_count,"
                 "created_at,updated_at,last_message_at,deleted_at")
_MSG_COLS = "role,content,turn_index,created_at"

# A real LINE userId is 'U' + 32 lowercase hex. Shape is a SECONDARY filter
# (it strips developer probe ids like "Uprobe…"/"Uhardgate…" that also have
# channel='line' sessions) — provenance below is the primary rule.
_REAL_LINE_ID_RE = re.compile(r"^U[0-9a-f]{32}$")


def _sb(sb=None):
    if sb is not None:
        return sb
    from services.supabase_client import get_supabase
    return get_supabase()


def _is_real_line_shape(uid: Optional[str]) -> bool:
    return bool(_REAL_LINE_ID_RE.match(uid or ""))


def _line_provenance_ids(client, extra: Iterable[str] = ()) -> set:
    """The set of line_user_ids with a real LINE channel footprint:
    every ai_sessions row with channel='line', plus `extra` (the
    channel='line' binding ids the caller already has). ONE read."""
    rows = (client.table("ai_sessions").select("line_user_id")
            .eq("channel", "line").execute().data or [])
    ids = {r.get("line_user_id") for r in rows if r.get("line_user_id")}
    ids.update(x for x in extra if x)
    return ids


def _is_real_line_user(uid: Optional[str], provenance_ids: set) -> bool:
    return _is_real_line_shape(uid) and (uid in provenance_ids)


def _mask_line_user_id(uid: Optional[str]) -> str:
    uid = uid or ""
    return f"{uid[:5]}...{uid[-4:]}" if len(uid) > 12 else uid


def _last_seen(profile: Dict) -> Optional[str]:
    return (profile.get("last_active") or profile.get("first_seen")
            or profile.get("created_at"))


def list_line_users(search: Optional[str] = None, status: Optional[str] = None,
                     limit: int = _LIST_DEFAULT_LIMIT, sb=None,
                     lead_stage: Optional[str] = None, sentiment: Optional[str] = None) -> Dict:
    """{"users": [...], "summary": {"total", "verified", "unverified"}}.
    `status` in (None|"all"|"verified"|"unverified"). `search` matches a
    display_name substring (case-insensitive) OR a verified CustCode
    substring. `lead_stage` in (None|"all"|"COLD"|"WARM"|"HOT") and
    `sentiment` in (None|"all"|"NORMAL"|"NEGATIVE") filter the TABLE only —
    `summary` always reflects the global real-LINE totals. All filters
    combine (AND). Read-only."""
    client = _sb(sb)

    # Query 1 — every LINE binding (small table; one shot). Verified ones
    # drive the CustCode/status columns; all of them count as provenance.
    line_binds = (client.table("customer_channel_bindings").select(_BINDING_COLS)
                  .eq("channel", "line").execute().data or [])
    bmap = {b["external_user_id"]: b for b in line_binds
            if b.get("external_user_id") and b.get("status") == "verified"}
    bind_ids = {b["external_user_id"] for b in line_binds if b.get("external_user_id")}

    # Query 2 — LINE user profiles, most-recently-active first, bounded.
    profiles = (client.table("user_profiles").select(_PROFILE_COLS)
                .order("last_active", desc=True)
                .execute().data or [])[:_PROFILE_HARD_CAP]

    # Query 3 — provenance: keep only rows that actually came from LINE.
    provenance_ids = _line_provenance_ids(client, extra=bind_ids)
    profiles = [p for p in profiles
                if _is_real_line_user(p.get("line_user_id"), provenance_ids)]

    total = len(profiles)
    verified_count = sum(1 for p in profiles if p.get("line_user_id") in bmap)

    s = (search or "").strip().lower()
    st = (status or "all").lower()
    lead_f = (lead_stage or "all").upper()
    sent_f = (sentiment or "all").upper()
    rows: List[Dict] = []
    for p in profiles:
        uid = p.get("line_user_id") or ""
        binding = bmap.get(uid)
        is_verified = binding is not None
        if st == "verified" and not is_verified:
            continue
        if st == "unverified" and is_verified:
            continue
        if lead_f in ("COLD", "WARM", "HOT") and (p.get("lead_stage") or "COLD").upper() != lead_f:
            continue
        if sent_f in ("NORMAL", "NEGATIVE") \
                and (p.get("sentiment_status") or "NORMAL").upper() != sent_f:
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
            "lead_stage": (p.get("lead_stage") or "COLD"),
            "lead_score": int(p.get("lead_score") or 0),
            "sentiment": (p.get("sentiment_status") or "NORMAL"),
        })

    rows.sort(key=lambda r: (r["last_seen"] or ""), reverse=True)
    return {
        "users": rows[: max(1, int(limit or _LIST_DEFAULT_LIMIT))],
        "summary": {"total": total, "verified": verified_count,
                    "unverified": max(0, total - verified_count)},
    }


def _fmt_export_ts(ts: Optional[str]) -> str:
    """ISO timestamp -> 'YYYY-MM-DD HH:MM' as a plain string (kept a string
    so Excel never reinterprets it as a serial date in the wrong timezone).
    Falls back to the raw value it was given."""
    if not ts:
        return ""
    try:
        import datetime as _dt
        return _dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(ts)


# Columns are exactly the ones the /admin/line-users table already shows
# (plus Message Count, which is already on every list row via
# user_profiles.message_count) — no new metric is computed here. Header
# labels mix Thai/English to match the on-screen table.
_EXPORT_COLUMNS = (
    ("LINE Display Name", lambda u: u.get("display_name") or ""),
    ("LINE User ID", lambda u: u.get("line_user_id") or ""),
    ("Customer Code", lambda u: u.get("cust_code") or ""),
    ("สถานะการยืนยัน", lambda u: u.get("status_label") or ""),
    ("Lead Stage", lambda u: u.get("lead_stage") or ""),
    ("Lead Score", lambda u: int(u.get("lead_score") or 0)),
    ("Sentiment", lambda u: u.get("sentiment") or ""),
    ("จำนวนข้อความ", lambda u: int(u.get("message_count") or 0)),
    ("ใช้งานล่าสุด", lambda u: _fmt_export_ts(u.get("last_seen"))),
)
_EXPORT_COL_WIDTHS = (24, 36, 15, 16, 12, 11, 12, 12, 20)


def export_line_users_xlsx(search: Optional[str] = None, status: Optional[str] = None,
                            lead_stage: Optional[str] = None, sentiment: Optional[str] = None,
                            sb=None) -> bytes:
    """Build an .xlsx (bytes) of the LINE-user directory, honouring the SAME
    search/status/lead/sentiment filters as the on-screen table. Reuses
    list_line_users() verbatim — same query, same real-LINE provenance
    rule, same verified-CustCode source of truth (customer_channel_bindings,
    never user_profiles.cust_code). Read-only. No SecretCode / password /
    token / credential / raw private payload is ever in a row here."""
    from openpyxl import Workbook
    from openpyxl.utils import get_column_letter
    import io

    data = list_line_users(search=search, status=status, limit=_PROFILE_HARD_CAP,
                           sb=sb, lead_stage=lead_stage, sentiment=sentiment)
    users = data.get("users", [])

    wb = Workbook()
    ws = wb.active
    ws.title = "LINE Users"
    ws.append([label for label, _ in _EXPORT_COLUMNS])
    for u in users:
        ws.append([getter(u) for _, getter in _EXPORT_COLUMNS])
    for idx, width in enumerate(_EXPORT_COL_WIDTHS, start=1):
        ws.column_dimensions[get_column_letter(idx)].width = width
    ws.freeze_panes = "A2"

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def get_line_user_detail(line_user_id: str, sb=None) -> Optional[Dict]:
    """Full durable profile + verified-binding status + newest LINE
    session summary for one user. Read-only. None if this is not a real
    LINE user (no profile row, wrong id shape, or no LINE provenance) —
    the endpoint cannot be used to inspect a synthetic profile."""
    if not line_user_id or not _is_real_line_shape(line_user_id):
        return None
    client = _sb(sb)

    binds = (client.table("customer_channel_bindings").select(_BINDING_COLS + ",created_at")
             .eq("channel", "line").eq("external_user_id", line_user_id)
             .order("verified_at", desc=True).execute().data or [])

    # Provenance: a channel='line' session OR any channel='line' binding.
    if not binds and line_user_id not in _line_provenance_ids(client):
        return None

    prof_rows = (client.table("user_profiles").select("*")
                 .eq("line_user_id", line_user_id).execute().data or [])
    if not prof_rows:
        return None
    p = prof_rows[0]

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
        "lead": _lead_block(p),
        "sentiment": _sentiment_block(p),
    }


def _json_list(v) -> list:
    if isinstance(v, str):
        import json
        try:
            return json.loads(v)
        except Exception:
            return []
    return list(v or [])


def _sentiment_block(p: Dict) -> Dict:
    """P4.1 read-only sentiment block for the detail view."""
    from services.sentiment_service import reason_labels
    reasons = _json_list(p.get("sentiment_reasons"))
    return {
        "status": p.get("sentiment_status") or "NORMAL",
        "reasons": reason_labels(reasons),
        "reason_keys": reasons,
        "updated_at": p.get("sentiment_updated_at"),
        "last_detected_at": p.get("negative_last_detected_at"),
        "last_alert_at": p.get("negative_last_alert_at"),
    }


def _lead_block(p: Dict) -> Dict:
    """P4 read-only lead-analysis block for the detail view."""
    from services.lead_stage_service import stage_reason_labels
    reasons = p.get("lead_reasons") or []
    if isinstance(reasons, str):          # tolerate a JSON string from some clients
        import json
        try:
            reasons = json.loads(reasons)
        except Exception:
            reasons = []
    return {
        "stage": p.get("lead_stage") or "COLD",
        "score": int(p.get("lead_score") or 0),
        "reasons": stage_reason_labels(reasons),
        "reason_keys": list(reasons),
        "updated_at": p.get("lead_stage_updated_at"),
    }


# ── P3.2 — Conversation History Viewer (read-only) ────────────────────

def _session_row_to_summary(s: Dict) -> Dict:
    return {
        "session_id": s.get("id"),
        "name": s.get("name") or "",
        "started_at": s.get("created_at"),
        "last_active_at": s.get("last_message_at") or s.get("updated_at"),
        "message_count": s.get("message_count") or 0,
    }


def list_user_sessions(line_user_id: str, sb=None) -> List[Dict]:
    """Every non-deleted LINE conversation session for one user, most
    recent first. ONE read of ai_sessions, scoped server-side by
    line_user_id + channel='line' (never fetch-all-then-filter, never a
    per-session query). Read-only. `[]` for an unknown or non-LINE-shape
    user."""
    if not line_user_id or not _is_real_line_shape(line_user_id):
        return []
    client = _sb(sb)
    rows = (client.table("ai_sessions").select(_SESSION_COLS)
            .eq("line_user_id", line_user_id).eq("channel", "line")
            .is_("deleted_at", "null")
            .order("updated_at", desc=True)
            .execute().data or [])[:_SESSION_LIST_CAP]
    return [_session_row_to_summary(s) for s in rows]


def _owned_line_session(client, line_user_id: str, session_id: str) -> Optional[Dict]:
    """The ai_sessions row for session_id ONLY when it is a non-deleted
    LINE session belonging to this exact line_user_id. Server-side
    ownership check — a session_id supplied by the frontend is never
    trusted on its own. Cross-user (and therefore cross-tenant, since a
    different tenant's customer is simply a different line_user_id here)
    requests return None."""
    rows = (client.table("ai_sessions").select(_SESSION_COLS)
            .eq("id", session_id).eq("channel", "line")
            .is_("deleted_at", "null")
            .execute().data or [])
    s = rows[0] if rows else None
    if not s or s.get("line_user_id") != line_user_id:
        return None
    return s


def get_user_session_messages(line_user_id: str, session_id: str, sb=None) -> Optional[Dict]:
    """{"session": {...}, "messages": [{"role", "content", "at"}, ...]} for
    ONE session, or None when that session is not a LINE session owned by
    this line_user_id. Only customer-visible user/assistant turns, in
    chronological order — ai_session_traces / ai_session_events (RAG
    chunks, prompts, policy, ERP payloads, pipeline timings, tokens) are
    never read here. TWO reads: the ownership check, then one bounded
    transcript read. Read-only."""
    if not line_user_id or not session_id or not _is_real_line_shape(line_user_id):
        return None
    client = _sb(sb)
    s = _owned_line_session(client, line_user_id, session_id)
    if s is None:
        return None
    raw = (client.table("ai_session_messages").select(_MSG_COLS)
           .eq("session_id", session_id)
           .order("turn_index")
           .execute().data or [])[:_TRANSCRIPT_MSG_CAP]
    messages = [
        {"role": m.get("role"), "content": m.get("content") or "", "at": m.get("created_at")}
        for m in raw
        if m.get("role") in _VISIBLE_ROLES
    ]
    return {"session": _session_row_to_summary(s), "messages": messages}
