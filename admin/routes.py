import os
import sys
import json
import hmac
import hashlib
import shutil
import tempfile
import datetime
import threading
import time
from pathlib import Path
import uuid as _uuid
from typing import Dict, List, Optional
from fastapi import FastAPI, Request, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse, StreamingResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from supabase import create_client

# Windows consoles default to a legacy codepage (e.g. cp874/cp1252) that
# can't encode Thai filenames/text. Any print() with such text crashed the
# whole background sync thread and silently left it stuck at 0% forever.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from config import ADMIN_USERNAME, ADMIN_PASSWORD, SESSION_SECRET, SUPABASE_URL, SUPABASE_KEY, STORAGE_DELETE_MODE
from ingestion.ingest import read_file_pages, chunk_pages, analyze_and_chunk
from ingestion.attachment_handler import process_excel_attachments
from ingestion.embedder import (
    upsert_chunks, delete_source, deactivate_old_chunks,
    register_file, get_file_version, mark_file_synced, soft_delete_file,
    store_excel_workbook, delete_excel_data,
)

KNOWLEDGE_DIR = Path("knowledge")
SUPPORTED_EXT = {".pdf", ".docx", ".doc", ".md", ".txt", ".xlsx", ".xls", ".csv"}
SUPPORTED_MIME = {
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "text/csv",
    "text/plain",
    "text/markdown",
    "application/octet-stream",  # generic binary — extension is the authority
}
SCOPES = [
    "General",
    "Customer Service",
    "Order",
    "Shipping",
    "Return & Refund",
    "Payment & Finance",
    "Product",
    "Promotion",
    "Marketplace",
    "Policy & SOP",
    "IT & System",
    "HR",
    "Legal",
    "Marketing",
    "Sales",
    "Internal",
    "FAQ",
    "Other",
]
SOURCES = [
    "Website",
    "LINE OA",
    "Shopee",
    "Lazada",
    "TikTok Shop",
    "Facebook",
    "Instagram",
]
DEPARTMENTS = [
    "Customer Service",
    "Operations",
    "Marketplace",
    "Warehouse",
    "Finance",
    "Marketing",
    "Sales",
    "IT",
    "HR",
    "Management",
    "Other",
]
LANGUAGES = ["Thai", "English", "Chinese", "Mixed"]
templates     = Jinja2Templates(directory="admin/templates")
app           = FastAPI()
app.mount("/static", StaticFiles(directory="admin/static"), name="static")

_sb = None

# ── Background sync state ─────────────────────────────────────
_sync = {
    "running": False,
    "pct": 0,
    "subtitle": "",
    "current_file": "",
    "current_file_id": None,
    "file_index": 0,
    "total_files": 0,
    "log": [],          # [{type, file, msg}]
    "errors": [],
    "done": False,
    "cancelled": False,
    "started_at": None,
    "file_times": [],   # วินาทีต่อไฟล์ สำหรับ estimate
    "file_steps": {},   # {filename: {step, pct, error}}
    "sync_files": [],   # ordered list of filenames in this sync session
    "job_id": None,
    "last_update": None,  # time.time() of last progress touch — used for stale-job detection
    "skip_file_ids": [],  # file_ids deleted mid-job — worker aborts these instead of processing them
    "run_token": 0,  # incremented every time a NEW run claims _sync — see _run_sync_list
}
_sync_lock = threading.Lock()
STALE_JOB_TIMEOUT_SEC = 180  # 3 min without progress => treat job as stale/failed

# ── Vision/OCR analysis profile resolution ─────────────────────
# Deliberately a SEPARATE concept from the "profile" argument the AI
# Knowledge Analyzer takes (services/knowledge_analyzer.py's classification/
# summarization cost tier) — the two used to share the generic name
# "profile" across analyze_and_chunk(), which is exactly how a real Admin
# sync silently ran Vision/OCR under config.VISION_OCR_ANALYSIS_PROFILE's
# "basic" default even though the route hardcoded profile="advanced" (that
# value only ever reached the Knowledge Analyzer, never Vision/OCR).
#
# Resolution order (highest priority first): explicit per-request/per-job
# override -> config.VISION_OCR_ANALYSIS_PROFILE IF an operator actually set
# the env var -> "advanced" (this project's current demo default, so a
# real Admin-triggered sync doesn't silently fall back to "basic" with no
# one having decided that).
_DEMO_DEFAULT_VISION_OCR_PROFILE = "advanced"


def _resolve_vision_ocr_profile(override: Optional[str] = None) -> str:
    from services.pdf_page_pipeline import PROFILES
    if override:
        if override in PROFILES:
            return override
        print(f"[VisionProfile] ignoring invalid override {override!r} (must be one of {PROFILES})")
    if os.environ.get("VISION_OCR_ANALYSIS_PROFILE"):
        from config import VISION_OCR_ANALYSIS_PROFILE
        return VISION_OCR_ANALYSIS_PROFILE
    return _DEMO_DEFAULT_VISION_OCR_PROFILE

# knowledge_sync_job_files.status values that mean "not yet finished" — used
# everywhere we need to distinguish live/in-flight rows from terminal ones
# (synced/failed/cancelled) when cancelling or cleaning up a job.
_ACTIVE_FILE_STATUSES = ["pending", "extracting", "chunking", "embedding", "saving", "running"]

# Rolling average latency for the AI Playground status bar — last 20 turns.
_playground_latencies: list = []
_playground_lock = threading.Lock()


def _record_playground_latency(ms: float):
    with _playground_lock:
        _playground_latencies.append(ms)
        del _playground_latencies[:-20]


def _get_playground_avg_latency() -> Optional[float]:
    with _playground_lock:
        if not _playground_latencies:
            return None
        return round(sum(_playground_latencies) / len(_playground_latencies), 1)

def _set_step(fname: str, step: str, pct: int = 0, error: str = ""):
    with _sync_lock:
        _sync["file_steps"][fname] = {"step": step, "pct": pct, "error": error}
        _sync["last_update"] = time.time()


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _job_create(total: int) -> str:
    job_id = str(_uuid.uuid4())
    try:
        get_sb().table("knowledge_sync_jobs").insert({
            "id": job_id, "status": "pending",
            "total_files": total, "completed_files": 0,
            "failed_files": 0, "progress_percent": 0,
            "started_at": _now_iso(),
        }).execute()
    except Exception as e:
        print(f"[job_create] {e}")
    with _sync_lock:
        _sync["job_id"] = job_id
    return job_id


def _job_update(job_id: Optional[str], **kw):
    if not job_id:
        return
    try:
        get_sb().table("knowledge_sync_jobs").update(kw).eq("id", job_id).execute()
    except Exception as e:
        print(f"[job_update] {e}")


def _jf_create(job_id: str, file_id: Optional[str], file_name: str) -> Optional[str]:
    jf_id = str(_uuid.uuid4())
    try:
        get_sb().table("knowledge_sync_job_files").insert({
            "id": jf_id, "job_id": job_id, "file_id": file_id,
            "file_name": file_name, "status": "pending", "progress_percent": 0,
        }).execute()
        return jf_id
    except Exception as e:
        print(f"[jf_create] {e}")
        return None


def _jf_update(jf_id: Optional[str], **kw):
    if not jf_id:
        return
    try:
        get_sb().table("knowledge_sync_job_files").update(kw).eq("id", jf_id).execute()
    except Exception as e:
        print(f"[jf_update] {e}")


def _jf_bulk_create(job_id: str, file_rows: list) -> dict:
    """Insert one knowledge_sync_job_files row per selected file IMMEDIATELY when
    the job is created — before any file is actually processed. This guarantees
    the Sync Activity page always has a complete file list for a job, even if
    the worker thread crashes, hangs, or is cancelled before reaching later
    files (previously rows were created lazily, one per file, only as that
    file's processing began — so a crash/cancel left "waiting" files with no
    row at all, and the Files column read as empty or incomplete).

    file_rows: list of {"id": file_id, "filename": name} — same shape as rows
    returned by the knowledge_files query used to build the sync file list.
    Returns {filename: jf_id} for the caller to update as processing proceeds.
    """
    jf_ids = {}
    rows = [{
        "id": str(_uuid.uuid4()), "job_id": job_id,
        "file_id": r.get("id"), "file_name": r["filename"],
        "status": "pending", "progress_percent": 0,
    } for r in file_rows]
    if not rows:
        return jf_ids
    try:
        get_sb().table("knowledge_sync_job_files").insert(rows).execute()
        for r in rows:
            jf_ids[r["file_name"]] = r["id"]
        print(f"[SyncJobItems] created count: {len(jf_ids)}")
    except Exception as e:
        print(f"[jf_bulk_create] {e}")
    if len(jf_ids) != len(file_rows):
        print(f"[SyncValidation] job {job_id}: expected {len(file_rows)} job_files rows, created {len(jf_ids)}")
    return jf_ids

def get_sb():
    global _sb
    if _sb is None:
        _sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _sb


def _cancel_active_job(job_id: Optional[str] = None, reason: str = "Cancelled by user"):
    """The ONE place that cancels a sync job — both /admin/sync-cancel and
    /api/knowledge/sync-jobs/{id}/cancel call this, so there is exactly one
    cancel behavior instead of two divergent ones.

    Cancelling never rolls back files that already finished syncing in this
    job — only files that have not yet reached a terminal state are stopped
    and marked cancelled. (Previously /admin/sync-cancel soft-deleted every
    already-"done" file in the job while the other cancel endpoint did not
    — a real behavioral inconsistency, not just a copy mismatch.)

    Returns (ok: bool, msg: str).
    """
    with _sync_lock:
        mem_job_id = _sync.get("job_id")
        is_current = bool(_sync.get("running")) and (job_id is None or mem_job_id == job_id)
        target_job_id = mem_job_id if is_current else job_id
        if is_current:
            _sync.update({
                "cancelled": True, "running": False, "done": True,
                "subtitle": "Sync cancelled.", "job_id": None,
            })

    if not target_job_id:
        return False, "No active job to cancel"

    try:
        _job_update(target_job_id, status="cancelled", cancelled_at=_now_iso(), error_message=reason)
        get_sb().table("knowledge_sync_job_files").update({"status": "cancelled"}) \
            .eq("job_id", target_job_id).in_("status", _ACTIVE_FILE_STATUSES).execute()
    except Exception as e:
        print(f"[SyncCancel] db update failed for job {target_job_id}: {e}")
        return False, str(e)

    print(f"[SyncCancel] job {target_job_id} cancelled ({reason})")
    return True, "ok"


def _handle_sync_job_files_on_delete(file_id: str):
    """Called whenever a knowledge_files row is being deleted. Ensures no
    orphan/mismatched knowledge_sync_job_files rows remain, and — if this
    file belongs to the currently active sync job — cancels its row instead
    of hard-deleting it, and tells the worker to skip it instead of
    finishing and resurrecting a knowledge_files row that was just deleted.

    Rows belonging to any OTHER (non-active) job are simply hard-deleted —
    no live worker is touching them, so this is safe (same behavior as
    before this fix).
    """
    with _sync_lock:
        active_job_id = _sync.get("job_id") if _sync.get("running") else None

    if not active_job_id:
        try:
            get_sb().table("knowledge_sync_job_files").delete().eq("file_id", file_id).execute()
        except Exception as e:
            print(f"[Delete] sync_job_files cleanup failed for file_id={file_id}: {e}")
        return

    try:
        rows = get_sb().table("knowledge_sync_job_files").select("id,status,job_id") \
            .eq("file_id", file_id).execute().data or []
    except Exception as e:
        print(f"[Delete] sync_job_files lookup failed for file_id={file_id}: {e}")
        rows = []

    active_rows = [r for r in rows if r["job_id"] == active_job_id and r["status"] in _ACTIVE_FILE_STATUSES]
    other_row_ids = [r["id"] for r in rows if r not in active_rows]

    for rid in other_row_ids:
        try:
            get_sb().table("knowledge_sync_job_files").delete().eq("id", rid).execute()
        except Exception:
            pass

    if not active_rows:
        return

    try:
        # file_id is cleared (not just status) — the job_files row stays for
        # history (file_name/error_message still show what happened), but
        # must not keep pointing at a file_id that no longer exists, so a
        # `COUNT(*) FROM knowledge_sync_job_files WHERE file_id = :id` check
        # after delete is always 0, same as every other child table.
        get_sb().table("knowledge_sync_job_files").update(
            {"status": "cancelled", "error_message": "File deleted before/during sync", "file_id": None}
        ).eq("file_id", file_id).eq("job_id", active_job_id).in_("status", _ACTIVE_FILE_STATUSES).execute()
    except Exception as e:
        print(f"[Delete] could not cancel active job_file for file_id={file_id}: {e}")

    with _sync_lock:
        skip = set(_sync.get("skip_file_ids") or [])
        skip.add(file_id)
        _sync["skip_file_ids"] = list(skip)

    # No orphan "running" job may be left with zero live files — if that was
    # the last non-terminal file in this job, cancel the whole job now.
    try:
        remaining = get_sb().table("knowledge_sync_job_files").select("id", count="exact") \
            .eq("job_id", active_job_id).in_("status", _ACTIVE_FILE_STATUSES).execute()
        if (remaining.count or 0) == 0:
            _cancel_active_job(active_job_id, reason="All queued files were deleted")
    except Exception as e:
        print(f"[Delete] remaining-files check failed for job {active_job_id}: {e}")


@app.on_event("startup")
async def _validate_embedding_configuration_at_startup():
    """2026-08-01 configuration-hardening pass — logs the active embedding
    model/dimension and the database's expected dimension once, at boot,
    and refuses to finish starting if they're CONFIRMED to differ (see
    services/embedding_service.py::report_and_validate_embedding_configuration).
    Runs before the sync-job reconciliation below so a misconfigured
    embedding setup is caught before anything else touches the DB.
    An unreachable DB / unconfigured provider is logged, not fatal — only
    a genuine, confirmed dimension mismatch stops the app from serving,
    so ingestion or search can never silently run against the wrong
    vector space."""
    from services.embedding_service import report_and_validate_embedding_configuration
    report_and_validate_embedding_configuration()


@app.on_event("startup")
async def _reconcile_orphaned_sync_jobs():
    """Never resume a job on restart. Any job left 'pending'/'running' from
    a previous process (crash, redeploy, manual kill — no worker thread
    from that process still exists) is marked cancelled with a clear reason,
    so Sync Activity never shows a job that will run forever."""
    try:
        sb = get_sb()
        res = sb.table("knowledge_sync_jobs").select("id").in_("status", ["pending", "running"]).execute()
        stale_ids = [r["id"] for r in (res.data or [])]
        for jid in stale_ids:
            sb.table("knowledge_sync_jobs").update({
                "status": "cancelled", "completed_at": _now_iso(), "error_message": "Server Restart",
            }).eq("id", jid).execute()
            sb.table("knowledge_sync_job_files").update({"status": "cancelled"}) \
                .eq("job_id", jid).in_("status", _ACTIVE_FILE_STATUSES).execute()
        if stale_ids:
            print(f"[SyncStartup] cancelled {len(stale_ids)} orphaned job(s) from previous process: {stale_ids}")
    except Exception as e:
        print(f"[SyncStartup] reconciliation failed: {e}")


# ── Auth helpers ──────────────────────────────────────────────

def _sign(username: str) -> str:
    sig = hmac.new(SESSION_SECRET.encode(), username.encode(), hashlib.sha256).hexdigest()
    return f"{username}:{sig}"

def _verify(token: str) -> bool:
    try:
        username, sig = token.rsplit(":", 1)
        expected = hmac.new(SESSION_SECRET.encode(), username.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, expected)
    except Exception:
        return False

def auth(request: Request):
    token = request.cookies.get("session_token", "")
    if not _verify(token):
        return RedirectResponse(url="/admin/login", status_code=302)
    return None


def require_developer_feature(request: Request, feature_key: str):
    """Server-side route guard (Developer Mode milestone, Access Control
    requirement) — Category A pages (AI Evaluation / Production
    Validation / Business Action Center) must deny direct access when
    their feature isn't VISIBLE per services/developer_mode.py, never
    relying only on the sidebar link being hidden. Redirects to Settings
    with an explanatory query param rather than a bare 404, so an admin
    who bookmarked the URL understands why and how to restore it.
    Call AFTER auth(request) — this never substitutes for login."""
    from services.developer_mode import is_feature_route_accessible
    if not is_feature_route_accessible(feature_key):
        return RedirectResponse(url="/admin/settings?dev_feature_blocked=1", status_code=302)
    return None

def render(template: str, context: dict):
    """TemplateResponse + ห้าม browser เก็บ cache ทุกรูปแบบ

    Also injects `sidebar` (computed from sidebar_config.SIDEBAR_CONFIG for
    whatever `active` this call passed) into every template's context —
    the single place the sidebar's data reaches Jinja, so no individual
    route handler needs to know the sidebar exists. `developer_mode` is
    injected the same way (Developer Mode milestone) — the single place
    the "Developer Mode" badge's on/off state reaches every template.
    """
    from admin.sidebar_config import get_sidebar
    from services.developer_mode import is_developer_mode_enabled
    context.setdefault("sidebar", get_sidebar())
    context.setdefault("developer_mode", is_developer_mode_enabled())
    r = templates.TemplateResponse(template, context)
    r.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
    r.headers["Pragma"] = "no-cache"
    r.headers["Expires"] = "0"
    return r


# ── Supabase helpers ──────────────────────────────────────────

def get_synced_sources() -> dict:
    try:
        res = get_sb().table("knowledge_files").select("id,filename").is_("deleted_at", "null").not_.is_("synced_at", "null").execute()
        return {row["filename"]: row["id"] for row in res.data}
    except Exception:
        return {}


# ── Login / Logout ────────────────────────────────────────────

@app.get("/")
async def root(request: Request):
    """The Admin app has no content of its own at `/` — only `/admin/*`
    routes exist. Without this, a bare domain hit (e.g. the public site
    root) 404s with a raw {"detail":"Not Found"}. Redirects a session
    that's already authenticated straight to the dashboard (the same
    landing page login() itself redirects to); anyone else goes to the
    login page, same as auth()'s own redirect target for every other
    protected route."""
    if _verify(request.cookies.get("session_token", "")):
        return RedirectResponse(url="/admin/dashboard", status_code=302)
    return RedirectResponse(url="/admin/login", status_code=302)


@app.get("/admin/check-auth")
async def check_auth_api(request: Request):
    token = request.cookies.get("session_token", "")
    if _verify(token):
        return JSONResponse({"ok": True})
    return JSONResponse({"ok": False}, status_code=401)


@app.get("/admin/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


@app.post("/admin/login")
async def login(username: str = Form(...), password: str = Form(...)):
    if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
        token = _sign(username)
        response = RedirectResponse(url="/admin/dashboard", status_code=302)
        response.set_cookie("session_token", token, httponly=True, samesite="lax",
                            max_age=60 * 60 * 24 * 7, path="/")
        return response
    return RedirectResponse(url="/admin/login?error=1", status_code=302)


@app.get("/admin/logout")
async def logout():
    response = RedirectResponse(url="/admin/login", status_code=302)
    response.set_cookie("session_token", "", httponly=True, samesite="lax",
                        max_age=0, expires=0, path="/")
    return response


# ── Settings ──────────────────────────────────────────────────

ENV_PATH = Path(__file__).parent.parent / ".env"

def _read_env() -> dict:
    env = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env

def _write_env(updates: dict):
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    result = []
    updated = set()
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k = stripped.split("=", 1)[0].strip()
            if k in updates:
                result.append(f"{k}={updates[k]}")
                updated.add(k)
                continue
        result.append(line)
    for k, v in updates.items():
        if k not in updated:
            result.append(f"{k}={v}")
    ENV_PATH.write_text("\n".join(result) + "\n", encoding="utf-8")
    # Also update the live process environment — services/developer_mode.py
    # re-reads os.getenv("DEVELOPER_MODE", ...) on every call (never
    # cached at import time, unlike config.py's module-level constants),
    # so this is what makes the Developer Mode toggle take effect
    # immediately, no restart required, while every other existing
    # setting's file-only-until-restart behavior is unchanged.
    import os as _os
    for k, v in updates.items():
        _os.environ[k] = v

SECTION_KEYS = {
    "ai":        ["OPENAI_API_KEY", "OPENAI_CHAT_MODEL", "OPENAI_EMBEDDING_MODEL"],
    "supabase":  ["SUPABASE_URL", "SUPABASE_ANON_KEY", "SUPABASE_SERVICE_KEY"],
    # 2026-08-01: was ["LINE_CHANNEL_SECRET", "LINE_CHANNEL_ACCESS_TOKEN"] — the second
    # key never matched what config.py/line_bot/webhook.py actually read
    # (LINE_CHANNEL_TOKEN), so saving it from this UI had no effect. Confirmed no
    # existing .env had a value under the wrong name before this rename (nothing to
    # migrate) — see the 2026-08-01 Settings-key-consistency audit.
    "line":      ["LINE_CHANNEL_SECRET", "LINE_CHANNEL_TOKEN"],
    "admin":     ["ADMIN_USERNAME", "ADMIN_PASSWORD"],
    # Developer Mode milestone — same persistence mechanism (the .env
    # file, read/written via _read_env()/_write_env() above), no new
    # storage layer, no localStorage.
    "developer": ["DEVELOPER_MODE"],
}

# ── Settings field metadata (2026-08-01 configuration-hardening pass) ──────
# Single source of truth for the Settings page's reload/sensitivity badges
# and grouping — the template renders from this dict, never a second
# hardcoded copy, so a field's displayed classification can't drift out of
# sync with its actual reload behavior.
#
#   group="live"    — re-read from the environment (or DB) on every call;
#                     takes effect immediately, no restart.
#   group="restart" — written to .env immediately, but the running
#                     process already cached the OLD value in a config.py
#                     module constant and/or a singleton built from it at
#                     import/first-use time — takes effect only after the
#                     process restarts.
#   group="secret"  — sensitive credential; classified by sensitivity
#                     first regardless of reload timing (most secrets here
#                     also require a restart — see each note).
#
# Verified against the actual consumer for each key (not assumed):
# services/embedding_service.py (embedding model), services/llm_service.py
# (chat model / API key, cached in a provider singleton), admin/routes.py
# ::get_sb() (Supabase, cached client singleton), config.py (admin
# username/password, read once), services/developer_mode.py (re-reads
# os.getenv on every call — the one genuinely live file-backed setting).
SETTINGS_FIELD_METADATA = {
    "OPENAI_API_KEY": {
        "label": "OpenAI API Key", "group": "secret", "restart_too": True,
        "note": "Cached in services/llm_service.py's provider singleton at first use.",
    },
    "OPENAI_CHAT_MODEL": {
        "label": "Chat Model", "group": "restart",
        "note": "Read once into config.OPENAI_CHAT_MODEL at process start.",
    },
    "OPENAI_EMBEDDING_MODEL": {
        "label": "Embedding Model", "group": "restart",
        "note": "Read once into config.OPENAI_EMBEDDING_MODEL and cached in the embedding "
                "provider singleton. Changing this also requires re-embedding existing content "
                "— never edit casually (see hint below).",
    },
    "SUPABASE_URL": {
        "label": "Supabase URL", "group": "restart",
        "note": "Cached in the Supabase client singleton (admin/routes.py::get_sb()).",
    },
    "SUPABASE_ANON_KEY": {
        "label": "Supabase Anon Key", "group": "secret", "restart_too": True,
        "note": "Not currently read by any consumer in this codebase (config.py has no "
                "SUPABASE_ANON_KEY constant) — saving this field has no effect today. Flagged "
                "during the 2026-08-01 audit, not changed.",
    },
    "SUPABASE_SERVICE_KEY": {
        "label": "Supabase Service Key", "group": "secret", "restart_too": True,
        "note": "Cached in the Supabase client singleton (admin/routes.py::get_sb()).",
    },
    "LINE_CHANNEL_SECRET": {
        "label": "LINE Channel Secret", "group": "secret", "restart_too": True,
        "note": "Consumed by line_bot/webhook.py (a separate process) — restart that process too.",
    },
    "LINE_CHANNEL_TOKEN": {
        "label": "LINE Channel Access Token", "group": "secret", "restart_too": True,
        "note": "Consumed by line_bot/webhook.py (a separate process) — restart that process "
                "too. Renamed from the non-functional LINE_CHANNEL_ACCESS_TOKEN key during the "
                "2026-08-01 Settings-key-consistency fix (confirmed no existing .env had a "
                "value under the old name, so nothing needed migrating).",
    },
    "ADMIN_USERNAME": {
        "label": "Admin Username", "group": "restart",
        "note": "Read once into config.ADMIN_USERNAME at process start.",
    },
    "ADMIN_PASSWORD": {
        "label": "Admin Password", "group": "secret", "restart_too": True,
        "note": "Read once into config.ADMIN_PASSWORD at process start.",
    },
    "DEVELOPER_MODE": {
        "label": "Developer Mode", "group": "live",
        "note": "Re-read from the environment on every request (services/developer_mode.py) "
                "— takes effect immediately, no restart.",
    },
}

SETTINGS_GROUP_LABELS = {
    "live":    "Runtime Settings (Live Reload)",
    "restart": "Restart Required",
    "secret":  "Secrets / Credentials",
}
SETTINGS_GROUP_ORDER = ["live", "restart", "secret"]


def _build_settings_groups(env: dict) -> list:
    """Groups SETTINGS_FIELD_METADATA into the 3 categories for the
    Configuration Overview card — never the raw secret value, only
    whether something is currently set, so this adds zero new exposure
    beyond what the existing per-field forms already show."""
    groups = {g: [] for g in SETTINGS_GROUP_ORDER}
    for key, meta in SETTINGS_FIELD_METADATA.items():
        groups[meta["group"]].append({
            "key": key, "label": meta["label"], "note": meta["note"],
            "restart_too": meta.get("restart_too", False),
            "configured": bool(env.get(key)),
        })
    return [{"group": g, "title": SETTINGS_GROUP_LABELS[g], "fields": groups[g]} for g in SETTINGS_GROUP_ORDER]


@app.get("/admin/settings", response_class=HTMLResponse)
async def settings_page(request: Request, saved: str = ""):
    if (r := auth(request)): return r
    env = _read_env()
    msg = "Saved successfully." if saved == "1" else ""
    # 2026-07-29 embedding-dimension incident: warn (never silently
    # switch back) if the legacy OPENAI_EMBED_MODEL alias is still set to
    # something different from the canonical OPENAI_EMBEDDING_MODEL —
    # config.py's precedence means the canonical value always wins when
    # set, but a stale legacy value sitting in .env is a trap for the
    # next person who edits it expecting it to matter.
    legacy_embed_warning = None
    canonical_embed = env.get("OPENAI_EMBEDDING_MODEL")
    legacy_embed = env.get("OPENAI_EMBED_MODEL")
    if legacy_embed and canonical_embed and legacy_embed != canonical_embed:
        legacy_embed_warning = (
            f"Legacy OPENAI_EMBED_MODEL is set to '{legacy_embed}', which differs from the "
            f"active OPENAI_EMBEDDING_MODEL ('{canonical_embed}'). The canonical value above is "
            f"what's actually used — this legacy variable is ignored, but should be updated to "
            f"match or removed to avoid confusion."
        )
    return render("settings.html", {"request": request, "active": "settings",
                                     "env": env, "msg": msg, "msg_type": "success",
                                     "legacy_embed_warning": legacy_embed_warning,
                                     "field_meta": SETTINGS_FIELD_METADATA,
                                     "settings_groups": _build_settings_groups(env)})

@app.post("/admin/settings")
async def settings_save(request: Request):
    if (r := auth(request)): return r
    form = await request.form()
    section = form.get("section", "")
    keys = SECTION_KEYS.get(section, [])
    updates = {k: form.get(k, "") for k in keys}
    _write_env(updates)
    return RedirectResponse(url="/admin/settings?saved=1", status_code=302)


# ── Protected routes ──────────────────────────────────────────

def _run_consistency_guard():
    """Self-healing check, run before every Import Queue / File Library read.

    A file's "synced" state must be derived ONLY from synced_at + chunk_count
    — never trusted from the status text column alone, since status is
    written by several different code paths and a bug in any one of them
    (or a half-applied write, a race, a future regression) can leave it
    saying 'completed' while synced_at/chunk_count say otherwise. Any row
    caught in that contradiction is forced back to 'uploaded' (pending) here
    and logged, so it can never silently vanish from both the Import Queue
    (which used to gate on status) and File Library (which requires
    synced_at + chunk_count) at the same time — exactly the bug reported.
    """
    try:
        res = get_sb().table("knowledge_files").select("id,filename,status,synced_at,chunk_count") \
            .is_("deleted_at", "null").eq("status", "completed").execute()
        bad = [r for r in (res.data or []) if not r.get("synced_at") or not (r.get("chunk_count") or 0) > 0]
    except Exception as e:
        print(f"[ConsistencyGuard] check failed: {e}")
        return
    for row in bad:
        print(f"[ConsistencyGuard] file_id={row['id']} filename={row['filename']!r} "
              f"claimed status=completed but synced_at={row.get('synced_at')!r} "
              f"chunk_count={row.get('chunk_count')!r} — forcing back to 'uploaded'")
        try:
            get_sb().table("knowledge_files").update({"status": "uploaded"}).eq("id", row["id"]).execute()
        except Exception as e:
            print(f"[ConsistencyGuard] failed to demote file_id={row['id']}: {e}")


@app.get("/admin/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Platform Overview Dashboard — the first page of the admin system.
    Deliberately reuses only data that already exists (knowledge_files,
    knowledge_sync_jobs) — no new tables, no AI Job Queue, no background
    aggregation. Any query that fails degrades to 0 / empty rather than
    breaking the page, same pattern used everywhere else in this app."""
    if (r := auth(request)): return r
    sb = get_sb()

    def _ts(s):
        if not s:
            return None
        try:
            return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None

    now = datetime.datetime.now(datetime.timezone.utc)
    today = now.date()

    try:
        try:
            files_res = sb.table("knowledge_files").select(
                "id,filename,status,uploaded_at,synced_at,deleted_at,chunk_count,size,last_error,knowledge_type"
            ).order("uploaded_at", desc=True).execute()
        except Exception:
            # migration 014 (knowledge_type) not run yet — degrade the
            # column, not the whole page.
            files_res = sb.table("knowledge_files").select(
                "id,filename,status,uploaded_at,synced_at,deleted_at,chunk_count,size,last_error"
            ).order("uploaded_at", desc=True).execute()
        all_files = files_res.data or []
    except Exception as e:
        print(f"[Dashboard] knowledge_files query failed: {e}")
        all_files = []

    active_files = [f for f in all_files if not f.get("deleted_at")]
    deleted_files = [f for f in all_files if f.get("deleted_at")]

    total_files = len(active_files)
    synced_files = sum(1 for f in active_files if f.get("synced_at") and (f.get("chunk_count") or 0) > 0)
    processing_files = sum(1 for f in active_files if f.get("status") == "syncing")
    pending_sync = sum(1 for f in active_files if f.get("status") in ("uploaded", "syncing"))
    pending_only = sum(1 for f in active_files if f.get("status") == "uploaded")
    failed_imports = sum(1 for f in active_files if f.get("status") == "failed")
    storage_used_bytes = sum((f.get("size") or 0) for f in active_files)

    try:
        jobs_res = sb.table("knowledge_sync_jobs").select(
            "id,status,started_at,completed_at,completed_files,failed_files,total_files"
        ).order("started_at", desc=True).limit(100).execute()
        jobs = jobs_res.data or []
    except Exception as e:
        print(f"[Dashboard] knowledge_sync_jobs query failed: {e}")
        jobs = []

    running_jobs = sum(1 for j in jobs if j.get("status") in ("pending", "running"))
    completed_today = sum(1 for j in jobs if j.get("status") == "completed"
                           and (d := _ts(j.get("completed_at"))) and d.date() == today)
    failed_today = sum(1 for j in jobs if j.get("status") in ("failed", "partially_failed")
                        and (d := _ts(j.get("completed_at"))) and d.date() == today)

    stats = {
        "total_files": total_files,
        "synced_files": synced_files,
        "pending_sync": pending_sync,
        "failed_imports": failed_imports,
        "running_jobs": running_jobs,
        "completed_today": completed_today,
        "failed_today": failed_today,
        "storage_used_bytes": storage_used_bytes,
    }

    knowledge_status = {
        "synced": synced_files, "pending": pending_only,
        "failed": failed_imports, "processing": processing_files,
    }

    # ── Daily series (last 14 days, oldest -> newest) — the only source
    # for KPI sparklines/trends and the Import Activity chart. Built purely
    # from timestamps already on knowledge_files/knowledge_sync_jobs; no
    # new tables, no background aggregation job.
    day_list = [(today - datetime.timedelta(days=i)) for i in range(13, -1, -1)]
    uploaded_by_day = {d: 0 for d in day_list}
    synced_by_day = {d: 0 for d in day_list}
    analyzed_by_day = {d: 0 for d in day_list}
    for f in active_files:
        ud = _ts(f.get("uploaded_at"))
        if ud and ud.date() in uploaded_by_day:
            uploaded_by_day[ud.date()] += 1
        sd = _ts(f.get("synced_at"))
        if sd and sd.date() in synced_by_day and (f.get("chunk_count") or 0) > 0:
            synced_by_day[sd.date()] += 1
            if f.get("knowledge_type"):
                analyzed_by_day[sd.date()] += 1

    completed_job_by_day = {d: 0 for d in day_list}
    failed_job_by_day = {d: 0 for d in day_list}
    for j in jobs:
        cd = _ts(j.get("completed_at"))
        if not cd or cd.date() not in completed_job_by_day:
            continue
        if j.get("status") == "completed":
            completed_job_by_day[cd.date()] += 1
        elif j.get("status") in ("failed", "partially_failed"):
            failed_job_by_day[cd.date()] += 1

    daily_series = [{
        "date": d.isoformat(), "label": d.strftime("%b %d"),
        "uploaded": uploaded_by_day[d], "synced": synced_by_day[d],
        "analyzed": analyzed_by_day[d],
        "jobs_completed": completed_job_by_day[d], "jobs_failed": failed_job_by_day[d],
    } for d in day_list]

    def _trend(values):
        """% change between the two halves of a 14-point series. None
        (rendered as "Not available") when there's nothing to compare."""
        if not values or len(values) < 14:
            return None
        prev_half, last_half = sum(values[:7]), sum(values[7:])
        if prev_half == 0 and last_half == 0:
            return {"pct": 0, "direction": "flat"}
        if prev_half == 0:
            return {"pct": 100, "direction": "up"}
        pct = round(((last_half - prev_half) / prev_half) * 100)
        return {"pct": abs(pct), "direction": "up" if pct > 0 else ("down" if pct < 0 else "flat")}

    uploaded_series = [d["uploaded"] for d in daily_series]
    synced_series = [d["synced"] for d in daily_series]
    jobs_completed_series = [d["jobs_completed"] for d in daily_series]
    jobs_failed_series = [d["jobs_failed"] for d in daily_series]

    kpi_cards = [
        {"key": "total_files", "label": "Total Knowledge Files", "value": total_files,
         "color": "#93c5fd", "icon": "📚", "sparkline": uploaded_series, "trend": _trend(uploaded_series)},
        {"key": "synced_files", "label": "Synced Files", "value": synced_files,
         "color": "#34d399", "icon": "✅", "sparkline": synced_series, "trend": _trend(synced_series)},
        {"key": "pending_sync", "label": "Pending Sync", "value": pending_sync,
         "color": "#fcd34d", "icon": "⏳", "sparkline": uploaded_series, "trend": None},
        {"key": "failed_imports", "label": "Failed Imports", "value": failed_imports,
         "color": "#fca5a5", "icon": "❌", "sparkline": jobs_failed_series, "trend": _trend(jobs_failed_series)},
        {"key": "running_jobs", "label": "Running Jobs", "value": running_jobs,
         "color": "#F5A623", "icon": "⚙️", "sparkline": [], "trend": None},
        {"key": "completed_today", "label": "Completed Today", "value": completed_today,
         "color": "#34d399", "icon": "🏁", "sparkline": jobs_completed_series, "trend": _trend(jobs_completed_series)},
        {"key": "failed_today", "label": "Failed Today", "value": failed_today,
         "color": "#fca5a5", "icon": "⚠️", "sparkline": jobs_failed_series, "trend": _trend(jobs_failed_series)},
        {"key": "storage_used_bytes", "label": "Storage Used", "value": storage_used_bytes,
         "color": "#a78bfa", "icon": "💾", "sparkline": [], "trend": None, "is_storage": True},
    ]

    # ── Recent Activity ── built purely from existing knowledge_files rows
    # (upload/sync/failure/deletion timestamps already on that table) —
    # no separate activity-log table.
    activity = []
    for f in active_files[:50]:
        if f.get("status") == "failed" and f.get("last_error"):
            activity.append({"type": "import_failed", "kind": "error", "label": "Import failed",
                              "detail": f["filename"], "ts": f.get("synced_at") or f.get("uploaded_at")})
        elif f.get("synced_at") and (f.get("chunk_count") or 0) > 0:
            activity.append({"type": "embedding_completed", "kind": "ai", "label": "Embedding completed",
                              "detail": f["filename"], "ts": f.get("synced_at")})
            activity.append({"type": "file_synced", "kind": "sync", "label": "File synced",
                              "detail": f["filename"], "ts": f.get("synced_at")})
        if f.get("uploaded_at"):
            activity.append({"type": "file_uploaded", "kind": "upload", "label": "File uploaded",
                              "detail": f["filename"], "ts": f.get("uploaded_at")})
    for f in deleted_files[:20]:
        activity.append({"type": "file_deleted", "kind": "sync", "label": "File deleted",
                          "detail": f["filename"], "ts": f.get("deleted_at")})

    activity = [a for a in activity if a.get("ts")]
    activity.sort(key=lambda a: a["ts"], reverse=True)
    activity = activity[:15]

    # Everything the dashboard's charts need, as one JSON blob — simpler
    # and less error-prone than hand-writing Jinja for nested chart data.
    # `default=str` covers any stray datetime; the `</` escape keeps a
    # filename like "a</script>b.pdf" from breaking out of the embedding
    # <script> tag.
    dash_json = json.dumps({
        "kpi_cards": kpi_cards, "knowledge_status": knowledge_status,
        "daily_series": daily_series, "activity": activity,
    }, default=str).replace("</", "<\\/")

    return render("dashboard.html", {
        "request": request, "stats": stats, "activity": activity,
        "kpi_cards": kpi_cards, "knowledge_status": knowledge_status,
        "daily_series": daily_series, "dash_json": dash_json,
    })


@app.get("/admin/documents", response_class=HTMLResponse)
async def documents(request: Request):
    if (r := auth(request)): return r
    _run_consistency_guard()

    # Import Queue: a file belongs here iff it is not (yet) truly synced.
    # "Truly synced" is defined ONLY by synced_at + chunk_count — never by
    # the status text column, which previously gated this query
    # (`status IN ('uploaded','syncing','failed')`). That meant a row that
    # somehow ended up with status='completed' but chunk_count=0/synced_at
    # NULL was excluded from BOTH this query and File Library's — invisible
    # everywhere. Keying purely on synced_at/chunk_count means a corrupted
    # row is always visible somewhere, and the guard above self-heals it.
    try:
        res = get_sb().table("knowledge_files").select("*")\
            .is_("deleted_at", "null")\
            .or_("synced_at.is.null,chunk_count.is.null,chunk_count.eq.0")\
            .order("uploaded_at", desc=True)\
            .execute()
        db_files = res.data or []
    except Exception as e:
        print(f"[ImportQueue] query failed: {e}")
        db_files = []

    # Count of synced files for stats card linking to File Library —
    # same synced_at + chunk_count definition, not status='completed'.
    try:
        sc_res = get_sb().table("knowledge_files").select("id,chunk_count")\
            .is_("deleted_at", "null").not_.is_("synced_at", "null").execute()
        synced_rows = [r for r in (sc_res.data or []) if (r.get("chunk_count") or 0) > 0]
        synced_count = len(synced_rows)
        total_chunks = sum((r.get("chunk_count") or 0) for r in synced_rows)
    except Exception as e:
        print(f"[FileLibrary] stats query failed: {e}")
        synced_count = 0
        total_chunks = 0

    files = []
    total_storage = 0
    for row in db_files:
        row["on_disk"] = (KNOWLEDGE_DIR / row["filename"]).exists()
        row.setdefault("status", "uploaded")
        row.setdefault("title", "")
        row.setdefault("scope", "")
        row.setdefault("platform", "")
        row.setdefault("department", "")
        row.setdefault("tags", "")
        row.setdefault("description", "")
        row.setdefault("language", "Thai")
        row.setdefault("last_error", "")
        row.setdefault("version", 1)
        files.append(row)
        total_storage += row.get("size", 0) or 0

    def _ts(s):
        if not s:
            return None
        try:
            return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None

    _PRIORITY = {"syncing": 0, "failed": 1, "uploaded": 2}

    def _panel_key(f):
        st = f.get("status", "uploaded")
        pri = _PRIORITY.get(st, 2)
        ts = _ts(f.get("uploaded_at"))
        neg_ts = -(ts.timestamp() if ts else 0)
        return (pri, neg_ts)

    panel_files = sorted(files, key=_panel_key)

    stats = {
        "total":        len(files),
        "pending":      sum(1 for f in files if f.get("status") in ("uploaded",)),
        "failed":       sum(1 for f in files if f.get("status") == "failed"),
        "synced_count": synced_count,
        "total_chunks": total_chunks,
        "storage":      total_storage,
    }

    return render("documents.html", {
        "request": request, "files": files, "panel_files": panel_files, "stats": stats,
        "scopes": SCOPES, "sources": SOURCES, "departments": DEPARTMENTS,
        "languages": LANGUAGES,
    })


@app.post("/admin/upload")
async def upload(request: Request, file: List[UploadFile] = File(...),
                  title: str = Form(""), scope: str = Form("General"),
                  source: str = Form("Website"), department: str = Form(""),
                  tags: str = Form(""), description: str = Form(""),
                  language: str = Form("Thai")):
    if (r := auth(request)): return r
    if source not in SOURCES:
        source = "Website"
    from storage import get_storage_service, StorageError
    storage = get_storage_service()

    count = 0
    errors = []
    for f in (file if isinstance(file, list) else [file]):
        ext = Path(f.filename).suffix.lower()
        if ext not in SUPPORTED_EXT:
            errors.append(f"{f.filename}: unsupported file type '{ext}'")
            continue

        # Stream the upload to a temp file first, then hand it to
        # StorageService — the same two-step pattern already used for
        # attachments. For the default "local" provider this ends up at
        # the exact same knowledge/<filename> path as before (zero
        # behavior change); for any other provider the file genuinely
        # goes wherever StorageService places it.
        tmp_fd, tmp_name = tempfile.mkstemp(suffix=ext)
        try:
            with os.fdopen(tmp_fd, "wb") as out:
                shutil.copyfileobj(f.file, out)
            tmp_path = Path(tmp_name)
            try:
                stored = storage.upload_file(tmp_path, dest_name=f.filename)
            except StorageError as e:
                print(f"[Upload] storage upload failed for {f.filename}: {e.detail}")
                errors.append(f"{f.filename}: upload failed — {e}")
                continue
        finally:
            try:
                Path(tmp_name).unlink(missing_ok=True)
            except Exception:
                pass

        try:
            file_id = register_file(f.filename, stored.file_size)
            get_sb().table("knowledge_files").update({
                "title":      title or Path(f.filename).stem,
                "scope":      scope,
                "platform":   source,
                "department": department,
                "tags":       tags,
                "description": description,
                "language":   language,
                "status":     "uploaded",
                "synced_at":  None,
                "chunk_count": 0,
                "last_error": None,
                "storage_provider":  stored.provider,
                "storage_bucket":    stored.storage_bucket,
                "storage_path":      stored.storage_path,
                "storage_object_id": stored.storage_object_id,
                "storage_url":       stored.public_url,
                "original_filename": f.filename,
                "checksum":          stored.checksum,
            }).eq("id", file_id).execute()
        except Exception:
            pass
        count += 1
    if errors and count == 0:
        raise HTTPException(status_code=400, detail="; ".join(errors))

    # Upload is store-only: it must NEVER trigger parsing, chunking,
    # embedding, graph generation, or a sync job — those only start when
    # the user explicitly clicks Sync (or "Sync All"). This block used to
    # auto-launch `_run_sync` here ("Full Auto Import"), which had a real
    # bug: `_run_sync` queries the DB for pending files at THREAD START
    # time, not from this request's file list. When the frontend uploads
    # N files as N sequential POSTs (see documents.html's "one fetch per
    # file" submitUpload()), only the FIRST request found
    # `_sync["running"] == False` and launched the thread — which then
    # only picked up whichever file row(s) existed in the DB at that
    # instant (often just the first file). The remaining files' rows were
    # created afterward, while `_sync["running"]` was already True, so no
    # new thread ever picked them up — they sat at status=uploaded
    # indefinitely with no sync ever run. Uploaded files now always stay
    # at status="uploaded" (Ready) until an explicit Sync action.
    return RedirectResponse(url=f"/admin/documents?uploaded={count}", status_code=302)


@app.get("/admin/view/{filename}")
async def view_file(request: Request, filename: str):
    if (r := auth(request)): return r
    path = KNOWLEDGE_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path, filename=filename)


@app.get("/admin/attachments/{filename:path}")
async def serve_attachment(filename: str):
    """Serve locally stored attachment files (images/PDFs). Public — no auth
    required for LINE OA. `:path` (not a plain string) so year/month
    subfolders (attachments/<yyyy>/<mm>/<file>) resolve correctly instead
    of only matching a single flat filename segment."""
    att_dir = Path("knowledge/attachments")
    path = att_dir / filename
    # Path traversal guard — must still hold with nested subpaths allowed.
    try:
        path.resolve().relative_to(att_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid path")
    if not path.exists():
        raise HTTPException(status_code=404, detail="Attachment not found")
    return FileResponse(str(path))


@app.get("/admin/files/{file_id}/excel-preview")
async def excel_preview(request: Request, file_id: str):
    """Structured Excel preview for the admin UI: workbook/sheet metadata,
    detected column types, first 50 rows, and basic stats (sum/avg/min/max)
    per numeric column — computed from excel_rows, never from embeddings."""
    if (r := auth(request)): return r
    sb = get_sb()
    try:
        wb_res = sb.table("excel_workbooks").select("id,filename,sheet_count,sheet_names") \
            .eq("file_id", file_id).execute()
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    if not wb_res.data:
        return JSONResponse({"ok": False, "error": "No structured Excel data for this file — not an Excel/CSV upload, or sync hasn't run yet."}, status_code=404)

    workbook = wb_res.data[0]
    try:
        sheets_res = sb.table("excel_sheets").select(
            "id,sheet_name,sheet_index,row_count,column_count,headers,"
            "numeric_columns,date_columns,currency_columns,percentage_columns,"
            "boolean_columns,is_hidden"
        ).eq("workbook_id", workbook["id"]).order("sheet_index").execute()
        sheets = sheets_res.data or []
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    for sheet in sheets:
        try:
            rows_res = sb.table("excel_rows").select("row_index,row_data") \
                .eq("sheet_id", sheet["id"]).order("row_index").limit(50).execute()
            preview_rows = rows_res.data or []
        except Exception as e:
            preview_rows = []
            print(f"[excel_preview] row fetch failed for sheet {sheet['id']}: {e}")
        sheet["preview_rows"] = preview_rows

        # Stats per numeric column — computed here (not cached), always
        # reflects the real stored data.
        stats = {}
        try:
            all_rows_res = sb.table("excel_rows").select("row_data") \
                .eq("sheet_id", sheet["id"]).execute()
            all_rows = all_rows_res.data or []
            for col in (sheet.get("numeric_columns") or []):
                values = []
                for r in all_rows:
                    v = (r.get("row_data") or {}).get(col)
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        values.append(v)
                if values:
                    stats[col] = {
                        "sum": sum(values), "avg": sum(values) / len(values),
                        "min": min(values), "max": max(values), "count": len(values),
                    }
        except Exception as e:
            print(f"[excel_preview] stats computation failed for sheet {sheet['id']}: {e}")
        sheet["stats"] = stats

    return JSONResponse({"ok": True, "workbook": workbook, "sheets": sheets})


@app.get("/admin/files/{file_id}/knowledge-items")
async def list_knowledge_items(request: Request, file_id: str):
    """Q&A rows (knowledge_items) for a file, each with its own attachments
    — used by the File Library detail view to show which items have
    attachments and let admins preview/delete per item."""
    if (r := auth(request)): return r
    sb = get_sb()
    try:
        items_res = sb.table("knowledge_items").select(
            "id,question,answer,sheet_name,row_index,created_at"
        ).eq("knowledge_file_id", file_id).is_("deleted_at", "null").order("row_index").execute()
        items = items_res.data or []
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    if items:
        try:
            item_ids = [i["id"] for i in items]
            ids_str = "(" + ",".join(item_ids) + ")"
            att_res = sb.table("knowledge_attachments").select(
                "id,knowledge_item_id,filename,original_filename,mime_type,public_url,status"
            ).filter("knowledge_item_id", "in", ids_str).is_("deleted_at", "null").neq("status", "missing").execute()
            by_item: Dict[str, list] = {}
            for a in (att_res.data or []):
                by_item.setdefault(a["knowledge_item_id"], []).append(a)
            for item in items:
                item["attachments"] = by_item.get(item["id"], [])
        except Exception as e:
            print(f"[list_knowledge_items] attachment fetch failed: {e}")
            for item in items:
                item["attachments"] = []

    return JSONResponse({"ok": True, "items": items})


@app.delete("/admin/knowledge-items/{item_id}")
async def delete_knowledge_item(request: Request, item_id: str):
    """Soft-delete a knowledge item and cascade-soft-delete its attachments
    (removing their binaries from storage) — never leave orphan attachments."""
    if (r := auth(request)): return r
    sb = get_sb()
    try:
        att_res = sb.table("knowledge_attachments").select("id,storage_path,storage_provider,storage_bucket,metadata") \
            .eq("knowledge_item_id", item_id).is_("deleted_at", "null").execute()
        for att in (att_res.data or []):
            _delete_attachment_binaries(att)
        sb.table("knowledge_attachments").update({"deleted_at": _now_iso()}) \
            .eq("knowledge_item_id", item_id).is_("deleted_at", "null").execute()
        sb.table("knowledge_items").update({"deleted_at": _now_iso()}).eq("id", item_id).execute()
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    return JSONResponse({"ok": True})


@app.get("/admin/files/{file_id}/attachments")
async def list_file_attachments(request: Request, file_id: str):
    if (r := auth(request)): return r
    try:
        res = get_sb().table("knowledge_attachments").select(
            "id,filename,original_filename,mime_type,file_size,attachment_type,"
            "storage_provider,storage_path,public_url,status,error_message,"
            "row_index,sheet_name,created_at"
        ).eq("knowledge_file_id", file_id).is_("deleted_at", "null").order("created_at").execute()
        return JSONResponse({"ok": True, "attachments": res.data or []})
    except Exception as e:
        print(f"[list_file_attachments] file_id={file_id}: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.delete("/admin/attachments/{attachment_id}")
async def delete_attachment(request: Request, attachment_id: str):
    """Soft-delete one attachment record and remove its binary from storage."""
    if (r := auth(request)): return r
    sb = get_sb()
    try:
        res = sb.table("knowledge_attachments").select("storage_path,storage_provider,storage_bucket,metadata").eq("id", attachment_id).execute()
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    if not res.data:
        return JSONResponse({"ok": False, "error": "Attachment not found"}, status_code=404)

    _delete_attachment_binaries(res.data[0])

    try:
        sb.table("knowledge_attachments").update({"deleted_at": _now_iso()}).eq("id", attachment_id).execute()
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    return JSONResponse({"ok": True})


def _delete_attachment_binaries(att: dict):
    """Delete an attachment's main file AND its generated preview/
    thumbnail (see ingestion.attachment_handler._generate_preview) from
    storage. The ONE place every delete path (full file delete, single
    knowledge-item delete, single attachment delete) removes these two
    binaries from, so they can never drift out of sync with each other
    again — that exact divergence (two delete implementations doing
    different things) was a real bug fixed earlier in this codebase."""
    from storage import get_storage_service, FileRef
    provider = att.get("storage_provider") or "local"
    bucket = att.get("storage_bucket")
    for path in (att.get("storage_path"), (att.get("metadata") or {}).get("preview_storage_path")):
        if not path:
            continue
        try:
            storage = get_storage_service(provider)
            storage.delete_file(FileRef(storage_path=path, storage_provider=provider, storage_bucket=bucket))
        except Exception as e:
            print(f"[Delete] could not remove attachment binary from storage: {path}: {e}")


# table -> (column referencing knowledge_files.id, step name in
# _delete_file_id's result, whether it's soft-deleted via deleted_at
# rather than hard-deleted). One shared table so _verify_no_orphans and
# the delete-summary rendering both agree on the mapping.
_DELETE_ORPHAN_TABLES = {
    "knowledge_chunks":          ("file_id", "chunks", False),
    "knowledge_chunk_embeddings": ("file_id", "embeddings", False),
    "knowledge_items":           ("knowledge_file_id", "knowledge_items", True),
    "knowledge_attachments":     ("knowledge_file_id", "attachments", True),
    "excel_workbooks":           ("file_id", "excel_workbook", False),
    "excel_sheets":              ("file_id", "excel_sheets", False),
    "excel_rows":                ("file_id", "excel_rows", False),
    "knowledge_sync_job_files":  ("file_id", "sync_jobs", False),
    "knowledge_graph_nodes":     ("knowledge_file_id", "graph_nodes", True),
    "knowledge_graph_edges":     ("knowledge_file_id", "graph_edges", True),
}


def _verify_no_orphans(file_id: str, steps: Optional[dict] = None) -> dict:
    """Post-delete check: every table that can reference a file_id must show
    zero (active) rows for it. Returns {table: {"count": int, "reason": str}}
    — any entry means the delete cascade above missed something.

    `steps`, if given (see _delete_file_id), lets a genuine failure surface
    its EXACT underlying reason (the real exception message from that
    step) instead of the generic "Data remains" — falling back to a
    specific-but-honest message only when the step itself reported success
    yet the row is still there (a real bug: RLS blocking the write, a
    race with a concurrent insert, etc.)."""
    sb = get_sb()
    orphans = {}
    for table, (column, step_name, is_soft_deleted) in _DELETE_ORPHAN_TABLES.items():
        try:
            q = sb.table(table).select("id", count="exact").eq(column, file_id)
            if is_soft_deleted:
                # Soft-deleted rows keep deleted_at set, not removed — a raw
                # row count would always be nonzero even when correctly
                # cleaned up. Only rows still ACTIVE count as a real orphan.
                q = q.is_("deleted_at", "null")
            count = q.execute().count or 0
        except Exception as e:
            print(f"[DeleteVerify] {table} count query failed for file_id={file_id}: {e}")
            continue
        if not count:
            continue
        step = (steps or {}).get(step_name, {})
        if step.get("status") == "error" and step.get("reason"):
            reason = step["reason"]
        else:
            reason = (f"Cleanup step '{step_name}' reported success but {count} row(s) "
                      f"still active in {table} — a missed condition (RLS policy, a "
                      f"concurrent write, or a schema mismatch) needs investigation.")
        orphans[table] = {"count": count, "reason": reason}
    if orphans:
        print(f"[DeleteVerify] ORPHAN DATA remains for file_id={file_id}: {orphans}")
    return orphans


def _delete_file_id(file_id: str, name: str, storage_meta: Optional[dict] = None) -> dict:
    """The SINGLE shared per-file deletion routine — every delete entry
    point (delete_file, delete_bulk, the SSE delete-stream generator) MUST
    call this instead of reimplementing cleanup steps inline. Two earlier
    bugs in this codebase were exactly that divergence: a second
    hand-rolled delete implementation that forgot knowledge_items/
    attachments/excel data, and (the bug this refactor fixes) forgot
    knowledge_graph_nodes/edges entirely, leaving them orphaned forever
    since knowledge_files is only ever soft-deleted (so their ON DELETE
    CASCADE never actually fires).

    Returns {"steps": {step_name: {"status": "done"|"error", "count": int|None,
    "reason": str|None}}} — one entry per required delete-summary line
    (chunks, embeddings, knowledge_items, attachments, excel_rows,
    excel_sheets, excel_workbook, graph_nodes, graph_edges, sync_jobs,
    disk, file_record). `reason` is only set on "error" and is always the
    EXACT underlying exception message, never a generic placeholder.
    """
    sb = get_sb()
    steps: dict = {}

    def _step(name_, status, count=None, reason=None):
        steps[name_] = {"status": status, "count": count, "reason": reason}

    # Embeddings live inside knowledge_chunks.embedding (pgvector column,
    # deleted along with the chunk row) AND, for any chunk that's ever
    # been embedded under a second provider/version, in
    # knowledge_chunk_embeddings (ON DELETE CASCADE from knowledge_chunks,
    # but counted explicitly here so the delete summary can report it
    # rather than silently assuming the cascade fired).
    embed_count = 0
    try:
        chunk_ids_res = sb.table("knowledge_chunks").select("id").eq("file_id", file_id).execute()
        chunk_ids = [r["id"] for r in (chunk_ids_res.data or [])]
        if chunk_ids:
            ids_str = "(" + ",".join(chunk_ids) + ")"
            embed_count = sb.table("knowledge_chunk_embeddings").select(
                "id", count="exact"
            ).filter("chunk_id", "in", ids_str).execute().count or 0
    except Exception as e:
        print(f"[Delete] knowledge_chunk_embeddings count query failed for file_id={file_id}: {e}")

    # Hard-delete all chunks — this is also what cascades away any matching
    # knowledge_chunk_embeddings rows (FK ON DELETE CASCADE on chunk_id).
    try:
        count_res = sb.table("knowledge_chunks").select("id", count="exact").eq("file_id", file_id).execute()
        chunk_count = count_res.count or 0
        sb.table("knowledge_chunks").delete().eq("file_id", file_id).execute()
        _step("chunks", "done", chunk_count)
        _step("embeddings", "done", embed_count)
    except Exception as e:
        _step("chunks", "error", None, str(e))
        _step("embeddings", "error", None, str(e))
        try:
            delete_source(name)
        except Exception:
            pass

    # Clean up sync job file records — routes through the active-job-aware
    # handler so a file that's queued/in-progress in the CURRENT sync job
    # gets cancelled (and the worker told to skip it) instead of having
    # its row hard-deleted out from under a live worker thread.
    try:
        _handle_sync_job_files_on_delete(file_id)
        _step("sync_jobs", "done")
    except Exception as e:
        _step("sync_jobs", "error", None, str(e))

    # Remove structured Excel data (rows -> sheets -> workbook, explicit
    # order, not dependent on ON DELETE CASCADE actually being present).
    try:
        excel_counts = delete_excel_data(file_id)
        _step("excel_rows", "done", excel_counts.get("excel_rows", 0))
        _step("excel_sheets", "done", excel_counts.get("excel_sheets", 0))
        _step("excel_workbook", "done", excel_counts.get("excel_workbooks", 0))
    except Exception as e:
        _step("excel_rows", "error", None, str(e))
        _step("excel_sheets", "error", None, str(e))
        _step("excel_workbook", "error", None, str(e))

    # Knowledge Graph nodes/edges — reference-counted (see
    # KnowledgeGraphService.delete_graph_for_file): edges are always
    # removed outright; a node is only actually deleted once every file
    # that ever referenced it has released it (ref_count reaches 0).
    try:
        from services.knowledge_graph_service import get_knowledge_graph_service
        graph_result = get_knowledge_graph_service().delete_graph_for_file(sb, file_id)
        if graph_result.get("error"):
            _step("graph_edges", "error", graph_result.get("edges_deleted"), graph_result["error"])
            _step("graph_nodes", "error", graph_result.get("nodes_deleted"), graph_result["error"])
        else:
            _step("graph_edges", "done", graph_result.get("edges_deleted", 0))
            _step("graph_nodes", "done", graph_result.get("nodes_deleted", 0))
    except Exception as e:
        _step("graph_edges", "error", None, str(e))
        _step("graph_nodes", "error", None, str(e))

    # Soft-delete knowledge_items (Q&A rows) belonging to this file —
    # before attachment cleanup below, since attachments are looked up
    # per-file regardless of which knowledge_item they belong to.
    try:
        item_count_res = sb.table("knowledge_items").select("id", count="exact") \
            .eq("knowledge_file_id", file_id).is_("deleted_at", "null").execute()
        item_count = item_count_res.count or 0
        sb.table("knowledge_items").update({"deleted_at": _now_iso()}) \
            .eq("knowledge_file_id", file_id).is_("deleted_at", "null").execute()
        _step("knowledge_items", "done", item_count)
    except Exception as e:
        _step("knowledge_items", "error", None, str(e))

    # Remove attachments (image/file columns from Excel Q&A rows) — both
    # the stored binary (via StorageService, whatever provider is active)
    # and the knowledge_attachments row. Soft-delete the row (set
    # deleted_at), consistent with how knowledge_files itself is deleted —
    # but the binary is genuinely removed from storage.
    try:
        att_res = sb.table("knowledge_attachments").select(
            "id,filename,storage_provider,storage_path,storage_bucket,metadata"
        ).eq("knowledge_file_id", file_id).is_("deleted_at", "null").execute()
        for att in (att_res.data or []):
            _delete_attachment_binaries(att)
        sb.table("knowledge_attachments").update({"deleted_at": _now_iso()}) \
            .eq("knowledge_file_id", file_id).is_("deleted_at", "null").execute()
        _step("attachments", "done", len(att_res.data or []))
    except Exception as e:
        _step("attachments", "error", None, str(e))

    # Remove (or archive) the file's own binary from storage, using THIS
    # row's storage metadata — not a blanket filename-based unlink.
    try:
        meta = storage_meta or {}
        if meta.get("storage_path"):
            from storage import get_storage_service, FileRef, StorageError
            storage = get_storage_service(meta.get("storage_provider"))
            ref = FileRef(
                storage_path=meta["storage_path"],
                storage_provider=meta.get("storage_provider") or "local",
                storage_bucket=meta.get("storage_bucket"),
                storage_object_id=meta.get("storage_object_id"),
            )
            if STORAGE_DELETE_MODE == "archive":
                archive_path = f"deleted/{datetime.datetime.now().strftime('%Y/%m')}/{Path(meta['storage_path']).name}"
                storage.move_file(ref, archive_path)
            else:
                storage.delete_file(ref)
            _step("disk", "done", 1)
        else:
            _step("disk", "done", 0)
    except Exception as e:
        _step("disk", "error", None, str(e))

    # Soft-delete the file record itself (preserves audit history) — last,
    # so a crash partway through cleanup never leaves the file looking
    # "deleted" while its children are still active.
    try:
        soft_delete_file(file_id)
        _step("file_record", "done", 1)
    except Exception as e:
        _step("file_record", "error", None, str(e))

    return {"steps": steps}


def _hard_delete_file(name: str) -> dict:
    """Hard-delete a file (by filename) and ALL its resources, driving the
    SAME shared per-file routine (_delete_file_id) the SSE delete-stream
    generator uses. Returns step counts plus a `summary`/`orphans` payload
    the non-streaming delete endpoints (delete_file, delete_bulk) surface
    to the caller."""
    sb = get_sb()
    result = {"chunks": 0, "sync_jobs_cleaned": 0, "file_records": 0, "disk": False, "summary": {}}

    # Find ALL file records for this filename (including soft-deleted).
    # Each row is deleted from storage using ITS OWN storage_path/provider
    # — duplicate-filename uploads each get a distinct physical location
    # under StorageService (no more silent overwrite-on-disk), so deleting
    # once at the end by filename alone would miss every row but one.
    try:
        res = sb.table("knowledge_files").select(
            "id,storage_provider,storage_path,storage_bucket,storage_object_id"
        ).eq("filename", name).execute()
        file_rows = res.data or []
    except Exception as e:
        # storage_* columns not present yet (migration 011 not run) —
        # fall back to the bare id so deletion of the DB row/chunks still
        # works; the file just won't be cleaned up from storage this time.
        print(f"[Delete] storage-aware query failed ({e}); falling back to filename-only lookup for {name!r}")
        try:
            res = sb.table("knowledge_files").select("id").eq("filename", name).execute()
            file_rows = res.data or []
        except Exception:
            file_rows = []
    file_ids = [r["id"] for r in file_rows]
    storage_meta_by_id = {r["id"]: r for r in file_rows}

    for file_id in file_ids:
        delete_result = _delete_file_id(file_id, name, storage_meta_by_id.get(file_id))
        steps = delete_result["steps"]
        result["summary"][file_id] = steps

        result["chunks"] += (steps.get("chunks", {}).get("count") or 0)
        if steps.get("sync_jobs", {}).get("status") == "done":
            result["sync_jobs_cleaned"] += 1
        result["excel_rows_deleted"] = result.get("excel_rows_deleted", 0) + (steps.get("excel_rows", {}).get("count") or 0)
        result["excel_sheets_deleted"] = result.get("excel_sheets_deleted", 0) + (steps.get("excel_sheets", {}).get("count") or 0)
        result["excel_workbooks_deleted"] = result.get("excel_workbooks_deleted", 0) + (steps.get("excel_workbook", {}).get("count") or 0)
        result["attachments_cleaned"] = result.get("attachments_cleaned", 0) + (steps.get("attachments", {}).get("count") or 0)
        result["graph_nodes_deleted"] = result.get("graph_nodes_deleted", 0) + (steps.get("graph_nodes", {}).get("count") or 0)
        result["graph_edges_deleted"] = result.get("graph_edges_deleted", 0) + (steps.get("graph_edges", {}).get("count") or 0)
        if steps.get("disk", {}).get("count"):
            result["disk"] = True
        if steps.get("file_record", {}).get("status") == "done":
            result["file_records"] += 1
        else:
            reason = steps.get("file_record", {}).get("reason", "unknown error")
            print(f"[Delete] soft_delete_file FAILED for file_id={file_id} name={name}: {reason}")
            result.setdefault("errors", []).append(f"{name}: soft-delete failed — {reason}")

        # Belt-and-suspenders: confirm every child table is actually clean
        # for this file_id, with an exact reason attached to any orphan
        # from the step that actually failed — never a generic "Data
        # remains" placeholder.
        orphans = _verify_no_orphans(file_id, steps)
        if orphans:
            result.setdefault("orphans", {})[file_id] = orphans

    # Delete by source name for any orphaned chunks without file_id
    if not file_ids:
        try:
            delete_source(name)
        except Exception:
            pass

    return result


def _delete_one(name: str):
    """Backward-compat wrapper for _hard_delete_file."""
    _hard_delete_file(name)


# ── Delete job store for SSE progress ────────────────────────────

import json as _json

_del_jobs: dict = {}
_del_lock_jobs = threading.Lock()


def _sse(data: dict) -> str:
    return f"data: {_json.dumps(data)}\n\n"


def _delete_stream_gen(job_id: str):
    with _del_lock_jobs:
        job = _del_jobs.get(job_id)
        if not job or job.get("claimed"):
            yield _sse({"type": "error", "msg": "Invalid or expired job"})
            return
        job["claimed"] = True
        filenames = list(job["filenames"])

    # Every step below is driven by the SAME shared per-file routine
    # (_delete_file_id) the non-streaming delete endpoints use — this
    # generator's only job is to report each of its steps over SSE.
    # Previously this was an independent, hand-rolled reimplementation of
    # delete that first only did 4 steps (chunks/sync_jobs/file_record/
    # disk), was later patched to add knowledge_items/attachments/excel,
    # and STILL never touched knowledge_graph_nodes/edges at all — every
    # one of those was the same root cause: a second delete
    # implementation that could silently drift out of sync with the
    # first. Routing through _delete_file_id makes that class of bug
    # structurally impossible going forward.
    STEP_NAMES = ["chunks", "embeddings", "knowledge_items", "attachments",
                  "excel_rows", "excel_sheets", "excel_workbook",
                  "graph_nodes", "graph_edges", "sync_jobs", "disk", "file_record"]
    steps_per_file = len(STEP_NAMES)
    total = len(filenames)
    total_steps = total * steps_per_file
    steps_done = 0
    grand_chunks = 0
    files_deleted = 0
    errors = []
    # Aggregated across every file in this batch — surfaced on the final
    # "complete" event so the UI can render the full required checklist
    # (chunks/embeddings/knowledge items/attachments/graph nodes/graph
    # edges/storage/sync jobs), not just a chunk count.
    totals = {"embeddings": 0, "knowledge_items": 0, "attachments": 0,
              "graph_nodes": 0, "graph_edges": 0, "sync_jobs": 0, "disk": 0}

    def _step(step, file, status, **extra):
        return _sse({"type": "step", "step": step, "file": file, "status": status,
                     "pct": int(steps_done / total_steps * 100) if total_steps else 100, **extra})

    yield _sse({"type": "start", "total": total, "total_steps": total_steps})

    for name in filenames:
        yield _sse({"type": "file_start", "file": name})
        sb = get_sb()

        try:
            try:
                res = sb.table("knowledge_files").select(
                    "id,storage_provider,storage_path,storage_bucket,storage_object_id"
                ).eq("filename", name).execute()
                file_rows_meta = res.data or []
                file_ids = [r["id"] for r in file_rows_meta]
            except Exception:
                file_rows_meta = []
                file_ids = []

            if not file_ids:
                # No DB record — delete orphaned chunks by source name and
                # skip straight to the end; every other step is a no-op.
                yield _step("chunks", name, "running", count=0)
                try:
                    delete_source(name)
                except Exception:
                    pass
                steps_done += steps_per_file
                yield _step("chunks", name, "done", count=0)
                files_deleted += 1
                yield _sse({"type": "file_done", "file": name})
                continue

            storage_meta_by_id = {r["id"]: r for r in file_rows_meta}
            file_orphans = {}

            for fid in file_ids:
                delete_result = _delete_file_id(fid, name, storage_meta_by_id.get(fid))
                steps = delete_result["steps"]

                for step_name in STEP_NAMES:
                    s = steps.get(step_name, {})
                    status = "done" if s.get("status") == "done" else "error"
                    yield _step(step_name, name, status, count=s.get("count"))
                    steps_done += 1
                    if status == "error":
                        errors.append({"file": name, "step": step_name, "msg": s.get("reason") or "unknown error"})

                grand_chunks += (steps.get("chunks", {}).get("count") or 0)
                for key in totals:
                    if key == "sync_jobs":
                        # sync_jobs is a done/error step, not a count — it
                        # cleans up this file's job-file references as a
                        # single unit, so count it as 1 per file cleaned.
                        totals[key] += 1 if steps.get("sync_jobs", {}).get("status") == "done" else 0
                    else:
                        totals[key] += (steps.get(key, {}).get("count") or 0)

                orphans = _verify_no_orphans(fid, steps)
                if orphans:
                    file_orphans[fid] = orphans

            if file_orphans:
                yield _sse({"type": "orphans_detected", "file": name, "orphans": file_orphans})

            files_deleted += 1
            yield _sse({"type": "file_done", "file": name})

        except Exception as e:
            errors.append({"file": name, "step": "unknown", "msg": str(e)})
            yield _sse({"type": "file_error", "file": name, "msg": str(e)})
            steps_done += steps_per_file

    with _del_lock_jobs:
        _del_jobs.pop(job_id, None)

    yield _sse({"type": "complete",
                "files_deleted": files_deleted,
                "chunks_deleted": grand_chunks,
                "embeddings_deleted": totals["embeddings"],
                "knowledge_items_deleted": totals["knowledge_items"],
                "attachments_deleted": totals["attachments"],
                "graph_nodes_deleted": totals["graph_nodes"],
                "graph_edges_deleted": totals["graph_edges"],
                "sync_jobs_cleaned": totals["sync_jobs"],
                "storage_deleted": totals["disk"],
                "errors": errors})


@app.post("/admin/delete-start")
async def delete_start(request: Request):
    if (r := auth(request)): return r
    form = await request.form()
    filenames = list(form.getlist("filenames"))
    if not filenames:
        return JSONResponse({"ok": False, "msg": "No files specified"}, status_code=400)
    job_id = str(_uuid.uuid4())
    with _del_lock_jobs:
        _del_jobs[job_id] = {"filenames": filenames, "claimed": False}
    return JSONResponse({"ok": True, "job_id": job_id})


@app.get("/admin/delete-stream/{job_id}")
async def delete_stream_endpoint(request: Request, job_id: str):
    if (r := auth(request)): return r
    return StreamingResponse(
        _delete_stream_gen(job_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/admin/delete/{filename}")
async def delete_file(request: Request, filename: str):
    if (r := auth(request)): return r
    try:
        result = _hard_delete_file(filename)
        if result.get("errors"):
            # The file record itself may not have been marked deleted —
            # do NOT report success, or a re-upload of this filename could
            # find the "deleted" row still active and inherit its old state.
            return JSONResponse({"ok": False, "msg": "; ".join(result["errors"]),
                                  "deleted": 0, "chunks": result["chunks"]}, status_code=207)
        return JSONResponse({"ok": True, "deleted": 1, "chunks": result["chunks"]})
    except Exception as e:
        return JSONResponse({"ok": False, "msg": str(e)}, status_code=500)


@app.post("/admin/delete-bulk")
async def delete_bulk(request: Request):
    if (r := auth(request)): return r
    form = await request.form()
    filenames = form.getlist("filenames")
    errors = []
    total_chunks = 0
    for name in filenames:
        try:
            result = _hard_delete_file(name)
            total_chunks += result["chunks"]
            if result.get("errors"):
                errors.extend(result["errors"])
        except Exception as e:
            errors.append(f"{name}: {e}")
    if errors:
        return JSONResponse({"ok": False, "deleted": len(filenames) - len(errors),
                             "errors": errors}, status_code=207)
    return JSONResponse({"ok": True, "deleted": len(filenames), "chunks": total_chunks})


def _run_sync_safe(target, *args, **kwargs):
    """Run a sync worker function, guaranteeing _sync['running'] never gets
    stuck True if the worker crashes with an unhandled exception."""
    try:
        target(*args, **kwargs)
    except Exception as e:
        print(f"[SyncWorker] crashed: {e}")
        with _sync_lock:
            job_id = _sync.get("job_id") or kwargs.get("job_id")
            _sync.update({"running": False, "done": True,
                           "subtitle": f"Sync failed unexpectedly: {e}"[:200]})
        if job_id:
            _job_update(job_id, status="failed", completed_at=_now_iso())


def _validate_synced_files(job_id: str) -> list:
    """For every knowledge_sync_job_files row this job marked 'synced', verify
    in the DB, right now, that:
      1. knowledge_files.status == 'completed'
      2. knowledge_files.synced_at IS NOT NULL
      3. knowledge_chunks has at least one active row for that file_id
      4. knowledge_sync_job_files.status == 'synced' (already true by query)

    Any file failing this is demoted to 'failed' in knowledge_sync_job_files
    (with an error_message explaining why) so the job's reported outcome
    can never claim success for a file the database doesn't actually agree
    is synced. Returns the list of demoted file names.
    """
    try:
        jf_res = get_sb().table("knowledge_sync_job_files").select("id,file_id,file_name") \
            .eq("job_id", job_id).eq("status", "synced").execute()
        synced_rows = jf_res.data or []
    except Exception as e:
        print(f"[SyncValidation] job {job_id}: could not load job_files for final validation: {e}")
        return []

    bad = []
    for row in synced_rows:
        fid, fname, jf_row_id = row.get("file_id"), row.get("file_name"), row.get("id")
        reason = None
        if not fid:
            reason = "job_files row has no file_id"
        else:
            try:
                kf = get_sb().table("knowledge_files").select("status,synced_at").eq("id", fid).execute()
                kf_row = (kf.data or [None])[0]
            except Exception as e:
                kf_row, reason = None, f"knowledge_files lookup failed: {e}"
            if kf_row is None and reason is None:
                reason = "knowledge_files row not found"
            elif kf_row is not None:
                if kf_row.get("status") != "completed":
                    reason = f"knowledge_files.status={kf_row.get('status')!r} (expected 'completed')"
                elif not kf_row.get("synced_at"):
                    reason = "knowledge_files.synced_at is NULL"
            if reason is None:
                try:
                    cc = get_sb().table("knowledge_chunks").select("id", count="exact") \
                        .eq("file_id", fid).eq("is_active", True).execute()
                    if not (cc.count or 0):
                        reason = "knowledge_chunks has 0 active rows for this file"
                except Exception as e:
                    reason = f"knowledge_chunks count query failed: {e}"

        if reason:
            print(f"[SyncValidation] job {job_id}: {fname} failed final check — {reason}")
            bad.append(fname)
            try:
                get_sb().table("knowledge_sync_job_files").update(
                    {"status": "failed", "error_message": f"Post-sync consistency check failed: {reason}"}
                ).eq("id", jf_row_id).execute()
                if fid:
                    get_sb().table("knowledge_files").update(
                        {"status": "failed", "last_error": f"Post-sync consistency check failed: {reason}"}
                    ).eq("id", fid).execute()
            except Exception as e:
                print(f"[SyncValidation] job {job_id}: failed to demote {fname}: {e}")
    return bad


def _run_sync_list(files: list, job_id: Optional[str] = None, jf_ids: Optional[dict] = None,
                    file_ids: Optional[dict] = None, vision_ocr_profile: Optional[str] = None):
    """Core sync loop. Writes progress to in-memory _sync dict AND knowledge_sync_jobs DB.

    file_ids: {filename: file_id} resolved by the caller BEFORE this thread
    started (from the same knowledge_files query that decided which files are
    pending). The loop below must use this — never re-resolve file_id from
    filename mid-sync. Sync state belongs to file_id, not filename.

    vision_ocr_profile: already-resolved (see _resolve_vision_ocr_profile())
    by the caller — this is the value actually sent to read_pdf_pages() for
    every PDF in this batch, independent of the Knowledge Analyzer's own
    "advanced" profile hardcoded below.
    """
    jf_ids = jf_ids or {}
    file_ids = file_ids or {}
    vision_ocr_profile = vision_ocr_profile or _resolve_vision_ocr_profile()
    knowledge_analysis_profile = "advanced"
    if job_id:
        _job_update(job_id, metadata={
            "knowledge_analysis_profile": knowledge_analysis_profile,
            "vision_ocr_profile": vision_ocr_profile,
        })
    with _sync_lock:
        total = _sync["total_files"]
        # Captured once, up front. Python threads cannot be force-killed —
        # cancellation is cooperative, checked only between files — so a
        # cancelled/superseded thread can still be alive (mid-file) when a
        # NEW run claims _sync. Every new run bumps run_token; if this
        # thread ever sees a mismatch it means it's a stale zombie from an
        # old run and must stop touching _sync (and its own job's DB row)
        # immediately, instead of corrupting the NEW run's shared state.
        my_run_token = _sync.get("run_token")

    print(f"[SyncJobItems] processing {total} file(s), job_id={job_id}")
    print(f"[SyncJobItems] files: {[f.name for f in files]}")
    if job_id and len(jf_ids) != len(files):
        print(f"[SyncValidation] job {job_id}: total_files={len(files)} but job_files rows={len(jf_ids)}")

    if job_id:
        _job_update(job_id, status="running")

    # Fix 3 (2026-07-29 embedding-dimension incident) — fail fast, before
    # ANY file's chunks are embedded, if the active embedding provider's
    # dimension doesn't match knowledge_chunks' actual column width. This
    # is checked ONCE per run (not per-file/per-chunk) so a misconfigured
    # run never gets partway through and leaves a job looking "stuck" at
    # partial progress — every file in this batch is marked failed
    # immediately with the same clear, actionable reason.
    from services.embedding_service import validate_embedding_configuration, EmbeddingConfigurationError
    try:
        validate_embedding_configuration(get_sb())
    except EmbeddingConfigurationError as e:
        print(f"[SyncJobItems] job {job_id}: {e}")
        with _sync_lock:
            _sync.update({"running": False, "done": True, "errors": [str(e)],
                          "subtitle": "Embedding configuration mismatch — sync aborted."})
        if job_id:
            _job_update(job_id, status="failed", error_message=str(e), progress_percent=0,
                        completed_files=0, failed_files=len(files))
        for fname, fid in file_ids.items():
            jf_id = jf_ids.get(fname)
            if jf_id:
                _jf_update(jf_id, status="failed", progress_percent=0,
                           current_step="Aborted — embedding configuration mismatch",
                           error_message=str(e))
            if fid:
                try:
                    get_sb().table("knowledge_files").update(
                        {"status": "failed", "last_error": str(e), "synced_at": None, "chunk_count": 0}
                    ).eq("id", fid).execute()
                except Exception:
                    pass
        return

    completed = 0
    failed = 0

    for idx, file_path in enumerate(files):
        fname = file_path.name
        # Resolved once, up front, from the caller's own knowledge_files
        # query — every _sync["log"] entry below carries it so the frontend
        # can match a log entry to a DOM row by file_id instead of filename.
        # Matching by filename let a stale log entry from an old (deleted)
        # file "bleed" onto a brand-new row that happens to share the same
        # filename — the exact cause of a freshly re-uploaded file showing
        # as "Completed" before it was ever synced.
        loop_file_id = file_ids.get(fname)

        with _sync_lock:
            if _sync.get("run_token") != my_run_token:
                # A newer run has already claimed _sync (and, by
                # construction, this job's DB row was already finalized by
                # whatever caused that — a cancel, a crash handler, or a
                # stale-job sweep). Stop touching shared state now instead
                # of corrupting the NEW run's live progress.
                return

        with _sync_lock:
            if _sync.get("cancelled"):
                _sync.update({"running": False, "done": True, "subtitle": "Sync cancelled."})
                if job_id:
                    pct = int(idx / total * 100) if total else 0
                    _job_update(job_id, status="cancelled", cancelled_at=_now_iso(),
                                progress_percent=pct, completed_files=completed, failed_files=failed)
                    try:
                        get_sb().table("knowledge_sync_job_files").update({"status": "cancelled"}) \
                            .eq("job_id", job_id).eq("status", "pending").execute()
                    except Exception:
                        pass
                return

        with _sync_lock:
            skip_ids = set(_sync.get("skip_file_ids") or [])
        if loop_file_id and loop_file_id in skip_ids:
            # File was deleted (or removed from the queue) while this job
            # was running — don't process it, don't resurrect its
            # knowledge_files row, and don't count it as failed.
            skip_jf_id = jf_ids.get(fname)
            if skip_jf_id:
                _jf_update(skip_jf_id, status="cancelled", current_step="File deleted — skipped")
            with _sync_lock:
                _sync["log"].append({"type": "skip", "file": fname, "file_id": loop_file_id,
                                      "msg": "File deleted — skipped"})
            continue

        pct_start = int(idx / total * 100) if total else 0
        pct_end   = int((idx + 1) / total * 100) if total else 100
        t0 = time.time()

        with _sync_lock:
            _sync["pct"]          = pct_start
            _sync["file_index"]   = idx + 1
            _sync["current_file"]    = fname
            _sync["current_file_id"] = loop_file_id
            _sync["subtitle"]     = f"Processing file {idx+1} of {total}..."
            _sync["log"].append({"type": "start", "file": fname, "file_id": loop_file_id, "msg": ""})

        if job_id:
            _job_update(job_id, current_file_name=fname, current_step="Extracting text…",
                        progress_percent=pct_start)

        _set_step(fname, "reading", 10)
        file_id = None
        chunks_inserted = False
        chunk_count = 0
        jf_id = None

        try:
            fstat   = file_path.stat()
            file_id = file_ids.get(fname)
            if not file_id:
                # Should never happen — every pending file was resolved to a
                # file_id by the caller's own knowledge_files query before
                # this thread started. Fail loudly rather than silently
                # falling back to a filename lookup that could resolve to
                # the wrong row.
                raise RuntimeError(f"No file_id resolved for {fname} — refusing filename-based fallback")
            sb_files = get_sb().table("knowledge_files")
            sb_files.update({"size": fstat.st_size}).eq("id", file_id).execute()

            if job_id:
                # The row was already created up front (in _jf_bulk_create) so
                # every file — including ones never reached due to a crash or
                # cancel — shows up in Sync Activity. Fall back to a lazy
                # create only if the bulk-create step somehow missed it.
                jf_id = jf_ids.get(fname) or _jf_create(job_id, file_id, fname)
                _jf_update(jf_id, status="extracting", progress_percent=10,
                           current_step="Extracting text…")

            try:
                fmeta = (get_sb().table("knowledge_files").select(
                    "category,tags,description,scope,platform,version"
                ).eq("id", file_id).execute().data or [{}])[0]
            except Exception:
                fmeta = {}

            version = int(fmeta.get("version") or 1)
            tags_raw = fmeta.get("tags") or ""
            tags_list = [t.strip() for t in tags_raw.split(",") if t.strip()] if isinstance(tags_raw, str) else (tags_raw or [])

            try:
                deactivate_old_chunks(file_id)
            except Exception:
                try:
                    get_sb().table("knowledge_chunks").delete().eq("file_id", file_id).execute()
                except Exception:
                    delete_source(fname)

            # Full Auto Import: every file always gets the Advanced AI
            # Analysis Profile (classification + summaries + Knowledge
            # Graph) — there is no manual profile selection anymore.
            # KnowledgeAnalyzerService/knowledge_graph_service already
            # degrade gracefully (fallback classifier, empty graph) if the
            # LLM is unavailable, so this never blocks the import itself.
            # vision_ocr_profile is the SEPARATE, explicitly-resolved Vision/
            # OCR mode for this job (see _resolve_vision_ocr_profile()) —
            # threaded all the way to read_pdf_pages(), never left to
            # silently fall back to config.VISION_OCR_ANALYSIS_PROFILE's
            # "basic" default.
            analysis, chunks, pages = analyze_and_chunk(
                file_path, source=fname,
                knowledge_analysis_profile=knowledge_analysis_profile,
                vision_ocr_profile=vision_ocr_profile,
                file_id=file_id, file_name=fname,
                file_type=file_path.suffix.lstrip(".").lower(),
                file_size=fstat.st_size,
                storage_path=f"knowledge/{fname}",
                document_title=fmeta.get("description") or file_path.stem,
                category=fmeta.get("category") or "",
                scope=fmeta.get("scope") or "General Knowledge",
                platform=fmeta.get("platform") or "Internal System",
                tags=tags_list, version=version,
            )
            if not pages:
                # A file with 0 chunks can never be "synced" (chunk_count > 0
                # is required). Previously this branch left knowledge_files
                # untouched entirely — the file silently sat in the Import
                # Queue forever with no error shown. Mark it failed with a
                # clear reason instead, consistent with "no real chunks =
                # not synced" everywhere else in this pipeline.
                try:
                    get_sb().table("knowledge_files").update(
                        {"status": "failed", "last_error": "No text found in file",
                         "synced_at": None, "chunk_count": 0}
                    ).eq("id", file_id).execute()
                except Exception:
                    pass
                with _sync_lock:
                    _sync["log"][-1] = {"type": "skip", "file": fname, "file_id": file_id, "msg": "No text found"}
                    _sync["pct"] = pct_end
                _set_step(fname, "failed", 0, "No text found")
                if jf_id:
                    _jf_update(jf_id, status="failed", progress_percent=0,
                               current_step="Skipped — no text found",
                               error_message="No text found in file", chunk_count=0)
                failed += 1
                if job_id:
                    _job_update(job_id, failed_files=failed, progress_percent=pct_end)
                continue

            _set_step(fname, "chunking", 30)
            if jf_id:
                _jf_update(jf_id, status="chunking", progress_percent=30,
                           current_step="Splitting into chunks…")
            if job_id:
                _job_update(job_id, current_step=f"Chunking {fname}…")

            if analysis:
                try:
                    get_sb().table("knowledge_files").update({
                        "knowledge_type": analysis.knowledge_type,
                        "knowledge_type_confidence": analysis.confidence,
                        "chunk_strategy": analysis.chunk_strategy,
                        "summary_short": analysis.summary_short,
                        "summary_long": analysis.summary_long,
                        "topics": analysis.topics,
                        "suggested_questions": analysis.suggested_questions,
                        "document_structure": analysis.document_structure,
                        "quality_issues": analysis.quality_issues,
                        "ai_suggestions": analysis.ai_suggestions,
                        "ai_audience": analysis.audience,
                        "ai_department": analysis.department,
                        "ai_difficulty": analysis.difficulty,
                        "ai_visibility": analysis.visibility_recommendation,
                        "ai_priority": analysis.priority,
                        "analysis_version": analysis.analysis_version,
                    }).eq("id", file_id).execute()
                except Exception as e:
                    # Migration 014 not run yet, or another transient issue —
                    # never let AI-analysis metadata persistence block the
                    # actual chunking/embedding that follows.
                    print(f"[KnowledgeAnalyzer] could not persist analysis for file_id={file_id}: {e}")

            with _sync_lock:
                _sync["log"][-1] = {"type": "embed", "file": fname, "file_id": file_id,
                                    "msg": f"Embedding {len(chunks)} chunks..."}

            _set_step(fname, "embedding", 55)
            if jf_id:
                _jf_update(jf_id, status="embedding", progress_percent=55,
                           current_step=f"Generating embeddings ({len(chunks)} chunks)…")
            if job_id:
                _job_update(job_id, current_step=f"Embedding {len(chunks)} chunks for {fname}…")

            chunk_count = upsert_chunks(chunks)
            chunks_inserted = chunk_count > 0
            if chunks and chunk_count == 0:
                # Every chunk failed to embed — do not mark this file synced
                # with a chunk_count that doesn't match reality.
                raise RuntimeError(f"Embedding produced 0/{len(chunks)} chunks — all embed calls failed")

            # Save the Knowledge Graph extracted alongside this analysis
            # (see services/knowledge_analyzer.py + knowledge_graph_service.py).
            # Extraction happened BEFORE chunking (per the AI Knowledge
            # Analyzer's pipeline ordering) so it couldn't know real
            # chunk_ids yet — resolve them now via a best-effort match of
            # each edge's evidence_text against the just-inserted chunks'
            # content, now that they exist.
            if analysis and (analysis.knowledge_graph.get("nodes") or analysis.knowledge_graph.get("edges")):
                try:
                    from services.knowledge_graph_service import get_knowledge_graph_service
                    chunk_rows = get_sb().table("knowledge_chunks").select("id,content") \
                        .eq("file_id", file_id).eq("is_active", True).execute().data or []
                    chunk_id_by_evidence = {r["content"]: r["id"] for r in chunk_rows}
                    graph_result = get_knowledge_graph_service().save_graph(
                        get_sb(), file_id, analysis.knowledge_graph["nodes"],
                        analysis.knowledge_graph["edges"], chunk_id_by_evidence=chunk_id_by_evidence,
                    )
                    print(f"[KnowledgeGraph] saved {graph_result['nodes_saved']} nodes, "
                          f"{graph_result['edges_saved']} edges for file_id={file_id}")
                except Exception as e:
                    print(f"[KnowledgeGraph] save failed for file_id={file_id}: {e}")

            # Store structured rows for Excel/CSV analytical queries
            _workbook_data = None
            if file_path.suffix.lower() in (".xlsx", ".xls", ".csv"):
                _workbook_data = next(
                    (p.get("_workbook_data") for p in pages if p.get("_workbook_data")),
                    None,
                )
                if _workbook_data:
                    try:
                        store_excel_workbook(file_id, fname, _workbook_data)
                    except Exception as _xe:
                        print(f"[sync] store_excel_workbook failed: {_xe}")

                    # Process image attachments in attachment columns
                    try:
                        att_report = process_excel_attachments(
                            _workbook_data,
                            file_id,
                            get_sb(),
                        )
                        if att_report["attachment_records"] or att_report.get("knowledge_items_created"):
                            total_att = len(att_report["attachment_records"])
                            print(
                                f"[sync] rows_imported={att_report.get('rows_imported', 0)} "
                                f"urls_detected={att_report.get('urls_detected', 0)} | "
                                f"attachments: {total_att} total | "
                                f"linked={att_report['attachments_linked']} "
                                f"downloaded={att_report['attachments_downloaded']} "
                                f"missing={att_report['attachments_missing']} "
                                f"failed={att_report['attachments_failed']}"
                            )
                            for f in att_report.get("failed", []):
                                print(f"[sync]   failed URL sheet={f['sheet']} row={f['row']}: {f['url']} — {f['error']}")
                            with _sync_lock:
                                _sync["log"].append({
                                    "type": "attachment",
                                    "file": fname,
                                    "file_id": file_id,
                                    "msg": (
                                        f"Rows imported: {att_report.get('rows_imported', 0)} | "
                                        f"URLs detected: {att_report.get('urls_detected', 0)} | "
                                        f"Attachments: {total_att} | "
                                        f"linked={att_report['attachments_linked']} "
                                        f"downloaded={att_report['attachments_downloaded']} "
                                        f"missing={att_report['attachments_missing']} "
                                        f"failed={att_report['attachments_failed']}"
                                    ),
                                    "att_report": {
                                        k: v for k, v in att_report.items()
                                        if k != "attachment_records"
                                    },
                                })
                    except Exception as _ae:
                        print(f"[sync] attachment processing failed: {_ae}")

            _set_step(fname, "saving", 85)
            if jf_id:
                _jf_update(jf_id, status="saving", progress_percent=85,
                           current_step="Saving to Supabase…")
            if job_id:
                _job_update(job_id, current_step=f"Saving {fname} to Supabase…")

            # Single atomic write — status/synced_at/chunk_count all move together
            # or none do. Deliberately NOT wrapped in try/except: a failure here
            # must propagate to the outer except block below, which rolls back
            # the just-inserted chunks and marks this file (and job) as failed
            # instead of leaving a row that's synced_at-set but status-stale
            # (invisible to both the Import Queue and File Library queries).
            mark_file_synced(file_id, chunk_count)

            # Full Auto Import failsafe warnings: AI analysis / Knowledge
            # Graph degrading to a fallback must never fail the import
            # (see analyze_and_chunk / KnowledgeGraphService.extract_graph),
            # but the admin should still be able to see it happened. Stored
            # in the existing `last_error` column (non-fatal — status stays
            # "completed") so File Library can show "Completed with
            # Warnings" instead of silently hiding the degradation.
            warnings = []
            if analysis is None or analysis.ai_analysis_degraded:
                warnings.append("AI Analysis failed. Import continued using standard RAG ingestion.")
            if analysis and analysis.knowledge_graph.get("error"):
                warnings.append("Knowledge Graph generation failed, but the file was imported successfully.")
            if warnings:
                try:
                    get_sb().table("knowledge_files").update(
                        {"last_error": " ".join(warnings)}
                    ).eq("id", file_id).execute()
                except Exception as e:
                    print(f"[FullAutoImport] could not persist warnings for file_id={file_id}: {e}")

            # Consistency check: the chunks we just embedded must actually be
            # queryable under this file_id before we call the file "synced".
            chunk_check = get_sb().table("knowledge_chunks").select("id", count="exact") \
                .eq("file_id", file_id).eq("is_active", True).execute()
            actual_chunk_rows = chunk_check.count or 0
            if actual_chunk_rows == 0:
                raise RuntimeError(
                    f"Consistency check failed: knowledge_files.chunk_count={chunk_count} "
                    f"but knowledge_chunks has {actual_chunk_rows} active row(s) for file_id={file_id}"
                )

            elapsed = time.time() - t0
            with _sync_lock:
                _sync["file_times"].append(elapsed)
                _sync["log"][-1] = {"type": "done", "file": fname, "file_id": file_id,
                                    "msg": f"{chunk_count} chunks synced",
                                    "knowledge_analysis_profile": knowledge_analysis_profile,
                                    "vision_ocr_profile": vision_ocr_profile}
                _sync["pct"] = pct_end
            _set_step(fname, "done", 100)

            if jf_id:
                # metadata column added by migrations/022_sync_job_vision_profile.sql
                # — _jf_update() already swallows a failed .execute() (e.g. the
                # migration not yet applied) without blocking the sync itself.
                _jf_update(jf_id, status="synced", progress_percent=100,
                           current_step="Done", chunk_count=chunk_count,
                           metadata={"knowledge_analysis_profile": knowledge_analysis_profile,
                                     "vision_ocr_profile": vision_ocr_profile})
            completed += 1
            if job_id:
                _job_update(job_id, completed_files=completed, progress_percent=pct_end)

        except Exception as e:
            if file_id and chunks_inserted:
                try:
                    get_sb().table("knowledge_chunks").delete().eq("file_id", file_id).execute()
                except Exception:
                    pass
            if file_id:
                try:
                    # deactivate_old_chunks() already ran earlier in the try
                    # block, before chunking/embedding — so regardless of
                    # whether THIS attempt's chunks got inserted (and then
                    # rolled back above), this file_id now has zero active
                    # knowledge_chunks rows. Clear synced_at/chunk_count too,
                    # not just status — otherwise a file that was
                    # successfully synced before, then re-synced and failed,
                    # would keep looking "synced" (synced_at set, stale
                    # chunk_count > 0) to any query that doesn't also trust
                    # status, while actually having 0 real chunks.
                    get_sb().table("knowledge_files").update(
                        {"status": "failed", "last_error": str(e)[:500],
                         "synced_at": None, "chunk_count": 0}
                    ).eq("id", file_id).execute()
                except Exception:
                    pass
            with _sync_lock:
                _sync["errors"].append(fname)
                _sync["log"][-1] = {"type": "error", "file": fname, "file_id": file_id, "msg": str(e)[:200]}
                _sync["pct"] = pct_end
            _set_step(fname, "failed", 0, str(e)[:200])

            if jf_id:
                _jf_update(jf_id, status="failed", progress_percent=0,
                           error_message=str(e)[:500])
            failed += 1
            if job_id:
                _job_update(job_id, failed_files=failed, progress_percent=pct_end)

    # Final consistency gate — belt-and-suspenders on top of the inline check
    # done per-file above. Re-verifies every file this run just marked
    # "synced" actually satisfies all four conditions in the DB right now,
    # and demotes it (and the job's reported success) if not.
    if job_id:
        bad_files = _validate_synced_files(job_id)
        if bad_files:
            print(f"[SyncValidation] job {job_id}: demoting {len(bad_files)} inconsistent file(s) to failed: {bad_files}")
            completed -= len(bad_files)
            failed += len(bad_files)

    final_status = "completed" if failed == 0 else ("failed" if completed == 0 else "partially_failed")
    with _sync_lock:
        _sync.update({"running": False, "done": True, "pct": 100, "subtitle": "Sync complete!"})
    if job_id:
        _job_update(job_id, status=final_status, progress_percent=100,
                    completed_at=_now_iso(), completed_files=completed, failed_files=failed,
                    current_file_name=None, current_step=None)


def _ensure_local_file(row: dict) -> Optional[Path]:
    """Return a local Path to this file's content — downloading it first
    via StorageService if it isn't already on local disk.

    This is the ONE seam where the storage abstraction meets the existing
    parsing/chunking/embedding pipeline (ingestion/*), which is written
    entirely against local Path objects and must not change. For the
    default "local" provider this is exactly the old `KNOWLEDGE_DIR /
    filename` existence check — zero behavior change. For any other
    provider, the file is fetched into a temp path first.
    """
    provider = row.get("storage_provider") or "local"
    filename = row["filename"]
    if provider == "local":
        fpath = KNOWLEDGE_DIR / filename
        return fpath if fpath.exists() else None

    from storage import get_storage_service, FileRef, StorageError
    try:
        storage = get_storage_service(provider)
        ref = FileRef(
            storage_path=row.get("storage_path") or filename, storage_provider=provider,
            storage_bucket=row.get("storage_bucket"), storage_object_id=row.get("storage_object_id"),
        )
        data = storage.download_file(ref)
    except StorageError as e:
        print(f"[Sync] could not download {filename!r} from {provider}: {e.detail}")
        return None

    tmp_dir = Path(tempfile.gettempdir()) / "shipify_sync_downloads"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = tmp_dir / filename
    with open(tmp_path, "wb") as f:
        f.write(data)
    return tmp_path


def _run_sync():
    # Query DB for the exact pending files — do NOT scan disk
    try:
        res = get_sb().table("knowledge_files").select(
            "id,filename,storage_provider,storage_path,storage_bucket,storage_object_id"
        ) \
            .is_("deleted_at", "null") \
            .is_("synced_at", "null") \
            .in_("status", ["uploaded", "syncing", "failed"]) \
            .order("uploaded_at", desc=False) \
            .execute()
        pending = res.data or []
    except Exception as e:
        # storage_* columns not present yet (migration 011 not run) — fall
        # back to the pre-storage-abstraction query so Sync All Pending
        # keeps working (against local disk only) until it's run.
        print(f"[SyncAll] storage-aware query failed ({e}); falling back")
        try:
            res = get_sb().table("knowledge_files").select("id,filename") \
                .is_("deleted_at", "null") \
                .is_("synced_at", "null") \
                .in_("status", ["uploaded", "syncing", "failed"]) \
                .order("uploaded_at", desc=False) \
                .execute()
            pending = res.data or []
        except Exception as e2:
            print(f"[SyncAll] fallback query also failed: {e2}")
            pending = []

    files = []
    file_rows = []
    for row in pending:
        fpath = _ensure_local_file(row)
        if fpath:
            files.append(fpath)
            file_rows.append(row)
        else:
            print(f"[SyncAll] file missing/unreachable, skipping: {row['filename']}")

    total = len(files)
    print(f"[SyncAll] pending files selected: {total}")
    print(f"[SyncAll] file_ids: {[r['id'] for r in file_rows]}")

    job_id = _job_create(total) if total > 0 else None
    jf_ids = {}
    file_ids = {r["filename"]: r["id"] for r in file_rows}
    if job_id:
        print(f"[SyncJob] created job_id: {job_id}")
        jf_ids = _jf_bulk_create(job_id, file_rows)

    with _sync_lock:
        _sync.update({"running": True, "pct": 0, "done": False, "cancelled": False, "last_update": time.time(),
                       "errors": [], "log": [], "total_files": total, "file_index": 0,
                       "file_times": [], "started_at": time.time(), "file_steps": {}, "skip_file_ids": [],
                       "sync_files": [f.name for f in files],
                       "subtitle": "Preparing..." if total else "No files found",
                       "job_id": job_id, "run_token": (_sync.get("run_token") or 0) + 1})

    if total == 0:
        with _sync_lock:
            _sync.update({"running": False, "done": True, "pct": 100,
                           "subtitle": "All files already synced!"})
        return

    _run_sync_list(files, job_id=job_id, jf_ids=jf_ids, file_ids=file_ids,
                    vision_ocr_profile=_resolve_vision_ocr_profile())


def _run_sync_failed():
    try:
        res = get_sb().table("knowledge_files").select(
            "id,filename,storage_provider,storage_path,storage_bucket,storage_object_id"
        ) \
            .eq("status", "failed") \
            .is_("deleted_at", "null") \
            .is_("synced_at", "null") \
            .execute()
        pending = res.data or []
    except Exception as e:
        print(f"[SyncFailed] storage-aware query failed ({e}); falling back")
        try:
            res = get_sb().table("knowledge_files").select("id,filename") \
                .eq("status", "failed").is_("deleted_at", "null").is_("synced_at", "null").execute()
            pending = res.data or []
        except Exception as e2:
            print(f"[SyncFailed] fallback query also failed: {e2}")
            pending = []

    files = []
    file_rows = []
    for row in pending:
        fpath = _ensure_local_file(row)
        if fpath:
            files.append(fpath)
            file_rows.append(row)
        else:
            print(f"[SyncFailed] file missing on disk, skipping: {row['filename']}")

    total = len(files)
    print(f"[SyncFailed] files selected: {total}")

    job_id = _job_create(total) if total > 0 else None
    jf_ids = {}
    file_ids = {r["filename"]: r["id"] for r in file_rows}
    if job_id:
        print(f"[SyncJob] created job_id: {job_id}")
        jf_ids = _jf_bulk_create(job_id, file_rows)

    with _sync_lock:
        _sync.update({"running": True, "pct": 0, "done": False, "cancelled": False, "last_update": time.time(),
                       "errors": [], "log": [], "total_files": total, "file_index": 0,
                       "file_times": [], "started_at": time.time(), "file_steps": {}, "skip_file_ids": [],
                       "sync_files": [f.name for f in files],
                       "subtitle": "Retrying failed files..." if total else "No failed files",
                       "job_id": job_id, "run_token": (_sync.get("run_token") or 0) + 1})

    if total == 0:
        with _sync_lock:
            _sync.update({"running": False, "done": True, "pct": 100,
                           "subtitle": "No failed files to retry."})
        return

    _run_sync_list(files, job_id=job_id, jf_ids=jf_ids, file_ids=file_ids,
                    vision_ocr_profile=_resolve_vision_ocr_profile())


@app.post("/admin/sync-start")
async def sync_start(request: Request):
    if (r := auth(request)): return r
    with _sync_lock:
        if _sync["running"]:
            return JSONResponse({"ok": False, "msg": "Already running"})
        _sync.update({"running": True, "pct": 0, "done": False, "cancelled": False, "last_update": time.time(),
                       "errors": [], "log": [], "total_files": 0, "file_index": 0,
                       "file_times": [], "started_at": time.time(), "file_steps": {}, "skip_file_ids": [],
                       "sync_files": [], "subtitle": "Querying pending files...", "job_id": None,
                       "current_file": None, "current_file_id": None})
    threading.Thread(target=lambda: _run_sync_safe(_run_sync), daemon=True).start()
    return JSONResponse({"ok": True})


@app.get("/admin/sync-status")
async def sync_status(request: Request):
    if (r := auth(request)): return r
    stale_job_id = None
    with _sync_lock:
        if _sync["running"]:
            last = _sync.get("last_update") or _sync.get("started_at")
            if last and (time.time() - last) > STALE_JOB_TIMEOUT_SEC:
                stale_job_id = _sync.get("job_id")
                print(f"[SyncRestore] stale job detected job_id={stale_job_id}")
                _sync.update({
                    "running": False, "done": True, "job_id": None,
                    "subtitle": "Sync stalled — no progress for over 3 minutes. Marked as failed.",
                })
        state = dict(_sync)

    if stale_job_id:
        try:
            _job_update(stale_job_id, status="failed", completed_at=_now_iso(),
                        error_message="Stale job — worker stopped responding")
            get_sb().table("knowledge_sync_job_files").update(
                {"status": "failed", "error_message": "Stale job — worker stopped responding"}
            ).eq("job_id", stale_job_id).in_("status", _ACTIVE_FILE_STATUSES).execute()
        except Exception as e:
            print(f"[SyncRestore] failed to persist stale job: {e}")
        print("[SyncRestore] stale job marked failed")

    # คำนวณ estimate time remaining
    eta_str = ""
    if state["running"]:
        elapsed = int(time.time() - state["started_at"]) if state["started_at"] else 0
        if state["file_times"] and state["total_files"]:
            avg = sum(state["file_times"]) / len(state["file_times"])
            remaining = state["total_files"] - state["file_index"]
            secs = int(avg * remaining)
            if secs >= 60:
                eta_str = f"~{secs//60}m {secs%60}s remaining  •  elapsed {elapsed}s"
            else:
                eta_str = f"~{secs}s remaining  •  elapsed {elapsed}s"
        else:
            eta_str = f"Calculating estimate...  •  elapsed {elapsed}s"

    state["eta"] = eta_str
    state.pop("file_times", None)
    return JSONResponse(state)


@app.post("/admin/sync-cancel")
async def sync_cancel(request: Request):
    if (r := auth(request)): return r
    with _sync_lock:
        running = _sync["running"]
    if not running:
        print("[SyncCancel] no active job — ignored")
        return JSONResponse({"ok": False, "msg": "Not running"})
    ok, msg = _cancel_active_job(reason="Cancelled by user")
    return JSONResponse({"ok": ok, "msg": msg})


@app.patch("/admin/files/{file_id}/metadata")
async def update_file_metadata(request: Request, file_id: str):
    if (r := auth(request)): return r
    data = await request.json()
    allowed = {"title", "scope", "platform", "department", "tags", "description", "language"}
    update = {k: v for k, v in data.items() if k in allowed}
    if "platform" in update and update["platform"] not in SOURCES:
        del update["platform"]
    if update:
        get_sb().table("knowledge_files").update(update).eq("id", file_id).execute()
    return JSONResponse({"ok": True})


@app.post("/admin/files/{file_id}/deactivate")
async def deactivate_file(request: Request, file_id: str):
    if (r := auth(request)): return r
    get_sb().table("knowledge_files").update({"status": "inactive"}).eq("id", file_id).execute()
    return JSONResponse({"ok": True})


def start_full_auto_import_for_file(file_id: str, vision_ocr_profile: Optional[str] = None) -> dict:
    """The single entry point for Full Auto Import of one already-uploaded
    file — always runs the complete advanced pipeline (Extract -> AI
    Knowledge Analyzer -> Advanced Analysis -> Knowledge Graph -> Chunking
    -> Embedding -> Save), no preview/confirmation step. Exposed to the
    outside world via services.import_service.ImportService so callers
    don't need to know this lives in admin/routes.py. Returns a plain dict
    (not an HTTP response) so it's reusable from both the route below and
    ImportService.

    vision_ocr_profile: optional per-request override ("disabled"/"basic"/
    "advanced") for the Vision/OCR pipeline specifically — see
    _resolve_vision_ocr_profile(). None means "use the configured/demo
    default", resolved once here (not re-resolved per file mid-batch)."""
    resolved_vision_ocr_profile = _resolve_vision_ocr_profile(vision_ocr_profile)
    if _sync["running"]:
        return {"ok": False, "msg": "Sync already running"}
    try:
        res = get_sb().table("knowledge_files").select(
            "id,filename,storage_provider,storage_path,storage_bucket,storage_object_id"
        ).eq("id", file_id).execute()
    except Exception as e:
        print(f"[SyncOne] storage-aware query failed ({e}); falling back to filename-only")
        try:
            res = get_sb().table("knowledge_files").select("id,filename").eq("id", file_id).execute()
        except Exception as e2:
            return {"ok": False, "msg": f"Could not read file record: {e2}"}
    if not res.data:
        return {"ok": False, "msg": "File not found"}
    fname = res.data[0]["filename"]
    fpath = _ensure_local_file(res.data[0])
    if not fpath:
        return {"ok": False, "msg": "File not found in storage"}
    with _sync_lock:
        if _sync["running"]:
            return {"ok": False, "msg": "Sync already running"}
        _sync["running"] = True  # claim the slot now, before any DB work below
    job_id = _job_create(1)
    print(f"[SyncOne] file_id={file_id} filename={fname}")
    print(f"[SyncJob] created job_id: {job_id}")
    jf_ids = _jf_bulk_create(job_id, [res.data[0]])
    with _sync_lock:
        _sync.update({"running": True, "pct": 0, "done": False, "cancelled": False, "last_update": time.time(),
                       "errors": [], "log": [], "total_files": 1, "file_index": 0,
                       "file_times": [], "started_at": time.time(), "file_steps": {}, "skip_file_ids": [],
                       "sync_files": [fname],
                       "subtitle": f"Syncing {fname}...", "job_id": job_id,
                       "run_token": (_sync.get("run_token") or 0) + 1})
    threading.Thread(target=lambda: _run_sync_safe(_run_sync_list, [fpath], job_id=job_id, jf_ids=jf_ids,
                                                    file_ids={fname: file_id},
                                                    vision_ocr_profile=resolved_vision_ocr_profile),
                      daemon=True).start()
    return {"ok": True, "job_id": job_id, "vision_ocr_profile": resolved_vision_ocr_profile}


@app.post("/admin/files/{file_id}/sync")
async def sync_one_file(request: Request, file_id: str):
    if (r := auth(request)): return r
    # Optional JSON body: {"vision_ocr_profile": "advanced"|"basic"|"disabled"}
    # — absent/empty body (the normal "Sync Now" button click) resolves to
    # the configured/demo default via _resolve_vision_ocr_profile().
    override = None
    try:
        body = await request.json()
        override = (body or {}).get("vision_ocr_profile")
    except Exception:
        pass
    return JSONResponse(start_full_auto_import_for_file(file_id, vision_ocr_profile=override))


@app.get("/admin/files/{file_id}/chunks")
async def get_file_chunks(request: Request, file_id: str):
    if (r := auth(request)): return r
    try:
        res = get_sb().table("knowledge_chunks").select(
            "id,content,source,intent,is_active,metadata,created_at"
        ).eq("file_id", file_id).eq("is_active", True).order("created_at").execute()
        return JSONResponse({"chunks": res.data or []})
    except Exception as e:
        return JSONResponse({"chunks": [], "error": str(e)})


@app.post("/admin/sync-retry-failed")
async def sync_retry_failed(request: Request):
    if (r := auth(request)): return r
    with _sync_lock:
        if _sync["running"]:
            return JSONResponse({"ok": False, "msg": "Already running"})
        _sync.update({"running": True, "pct": 0, "done": False, "cancelled": False, "last_update": time.time(),
                       "errors": [], "log": [], "total_files": 0, "file_index": 0,
                       "file_times": [], "started_at": time.time(), "file_steps": {}, "skip_file_ids": [],
                       "sync_files": [], "subtitle": "Querying failed files...", "job_id": None,
                       "current_file": None, "current_file_id": None})
    threading.Thread(target=lambda: _run_sync_safe(_run_sync_failed), daemon=True).start()
    return JSONResponse({"ok": True})


@app.get("/admin/sync/active")
async def sync_active(request: Request):
    """Canonical "is anything running right now" check for the frontend.
    Returns {"job": null} — never a fabricated job — when nothing is
    active. Backed by in-memory state first (for live progress fields),
    falling back to the DB only to catch a job that's pending/running
    there but this process has no live memory of."""
    if (r := auth(request)): return r
    with _sync_lock:
        mem = dict(_sync)
    if mem.get("running"):
        return JSONResponse({"job": {
            "id": mem.get("job_id"), "status": "running",
            "total_files": mem.get("total_files", 0),
            "completed_files": mem.get("file_index", 0),
            "current_file_name": mem.get("current_file"),
            "current_step": mem.get("subtitle"),
            "progress_percent": mem.get("pct", 0),
        }})
    try:
        res = get_sb().table("knowledge_sync_jobs").select("*") \
            .in_("status", ["pending", "running"]) \
            .order("started_at", desc=True).limit(1).execute()
        job = res.data[0] if res.data else None
    except Exception:
        job = None
    return JSONResponse({"job": job})


@app.get("/api/knowledge/sync-jobs/active")
async def api_active_job(request: Request):
    if (r := auth(request)): return r
    # First check in-memory state for real-time accuracy
    with _sync_lock:
        mem = dict(_sync)
    if mem.get("running"):
        # Only a genuinely RUNNING in-memory job counts as "active" — a
        # finished job (done=True) must never be reported as active just
        # because nothing has reset _sync yet, or the widget/modal would
        # keep showing a completed job as if it were still live.
        job_id = mem.get("job_id")
        job_data = {
            "id": job_id, "status": "running",
            "total_files": mem.get("total_files", 0),
            "completed_files": mem.get("file_index", 0),
            "failed_files": len(mem.get("errors", [])),
            "progress_percent": mem.get("pct", 0),
            "current_file_name": mem.get("current_file"),
            "current_step": mem.get("subtitle"),
            "file_steps": mem.get("file_steps", {}),
        }
        if job_id:
            try:
                res = get_sb().table("knowledge_sync_job_files").select("*").eq("job_id", job_id).order("created_at").execute()
                job_data["files"] = res.data or []
            except Exception:
                job_data["files"] = []
        return JSONResponse({"job": job_data, "source": "memory"})
    # Fall back to DB
    try:
        res = get_sb().table("knowledge_sync_jobs").select("*") \
            .in_("status", ["pending", "running"]) \
            .order("started_at", desc=True).limit(1).execute()
        job = res.data[0] if res.data else None
        if job:
            fr = get_sb().table("knowledge_sync_job_files").select("*") \
                .eq("job_id", job["id"]).order("created_at").execute()
            job["files"] = fr.data or []
        return JSONResponse({"job": job, "source": "db"})
    except Exception as e:
        return JSONResponse({"job": None, "error": str(e)})


@app.get("/api/knowledge/sync-jobs/{job_id}")
async def api_job_detail(request: Request, job_id: str):
    if (r := auth(request)): return r
    try:
        res = get_sb().table("knowledge_sync_jobs").select("*").eq("id", job_id).execute()
        job = res.data[0] if res.data else None
        if not job:
            return JSONResponse({"job": None}, status_code=404)
        fr = get_sb().table("knowledge_sync_job_files").select("*") \
            .eq("job_id", job_id).order("created_at").execute()
        job["files"] = fr.data or []
        expected = job.get("total_files") or 0
        if expected and len(job["files"]) != expected:
            print(f"[SyncValidation] job {job_id}: total_files={expected} but job_files rows={len(job['files'])}")
        return JSONResponse({"job": job})
    except Exception as e:
        return JSONResponse({"job": None, "error": str(e)})


@app.post("/api/knowledge/sync-jobs/{job_id}/cancel")
async def api_cancel_job(request: Request, job_id: str):
    if (r := auth(request)): return r
    ok, msg = _cancel_active_job(job_id, reason="Cancelled by user")
    return JSONResponse({"ok": ok, "msg": msg})


@app.post("/api/knowledge/sync-jobs/{job_id}/retry-failed")
async def api_retry_failed(request: Request, job_id: str):
    if (r := auth(request)): return r
    with _sync_lock:
        if _sync["running"]:
            return JSONResponse({"ok": False, "msg": "A sync is already running"}, status_code=409)
    try:
        res = get_sb().table("knowledge_sync_job_files").select("file_id,file_name") \
            .eq("job_id", job_id).eq("status", "failed").execute()
        failed_rows = res.data or []
    except Exception:
        failed_rows = []
    # Pull storage metadata for these files so _ensure_local_file works
    # regardless of which provider each one was originally uploaded under.
    storage_meta_by_id = {}
    file_ids = [r["file_id"] for r in failed_rows if r.get("file_id")]
    if file_ids:
        try:
            kf_res = get_sb().table("knowledge_files").select(
                "id,storage_provider,storage_path,storage_bucket,storage_object_id"
            ).filter("id", "in", "(" + ",".join(file_ids) + ")").execute()
            storage_meta_by_id = {r["id"]: r for r in (kf_res.data or [])}
        except Exception as e:
            print(f"[api_retry_failed] storage metadata lookup failed: {e}")

    files, file_rows = [], []
    for row in failed_rows:
        meta = storage_meta_by_id.get(row.get("file_id"), {})
        fpath = _ensure_local_file({**meta, "filename": row["file_name"]})
        if fpath:
            files.append(fpath)
            file_rows.append({"id": row.get("file_id"), "filename": row["file_name"]})
    if not files:
        return JSONResponse({"ok": False, "msg": "No failed files to retry"})
    with _sync_lock:
        if _sync["running"]:
            return JSONResponse({"ok": False, "msg": "A sync is already running"}, status_code=409)
        _sync["running"] = True  # claim the slot before the DB work below
    new_job_id = _job_create(len(files))
    jf_ids = _jf_bulk_create(new_job_id, file_rows)
    file_ids = {r["filename"]: r["id"] for r in file_rows}
    with _sync_lock:
        _sync.update({"running": True, "pct": 0, "done": False, "cancelled": False, "last_update": time.time(),
                       "errors": [], "log": [], "total_files": len(files), "file_index": 0,
                       "file_times": [], "started_at": time.time(), "file_steps": {}, "skip_file_ids": [],
                       "sync_files": [f.name for f in files],
                       "subtitle": "Retrying failed files…", "job_id": new_job_id,
                       "run_token": (_sync.get("run_token") or 0) + 1})
    threading.Thread(target=lambda: _run_sync_safe(_run_sync_list, files, job_id=new_job_id, jf_ids=jf_ids,
                                                    file_ids=file_ids), daemon=True).start()
    return JSONResponse({"ok": True, "job_id": new_job_id})


@app.get("/api/knowledge/sync-activity")
async def api_sync_activity(request: Request, status: str = "", limit: int = 50, offset: int = 0):
    if (r := auth(request)): return r
    try:
        q = get_sb().table("knowledge_sync_jobs").select("*").order("started_at", desc=True)
        if status:
            q = q.eq("status", status)
        q = q.range(offset, offset + limit - 1)
        res = q.execute()
        return JSONResponse({"jobs": res.data or [], "total": len(res.data or [])})
    except Exception as e:
        return JSONResponse({"jobs": [], "error": str(e)})


@app.post("/admin/files/{file_id}/restore")
async def restore_file(request: Request, file_id: str):
    if (r := auth(request)): return r
    try:
        get_sb().table("knowledge_files").update(
            {"deleted_at": None, "status": "uploaded"}
        ).eq("id", file_id).execute()
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"ok": False, "msg": str(e)})


@app.get("/admin/history", response_class=HTMLResponse)
async def history_page(request: Request):
    if (r := auth(request)): return r
    return RedirectResponse(url="/admin/file-library", status_code=302)


@app.post("/admin/files/{file_id}/delete-pending")
async def delete_pending_file(request: Request, file_id: str):
    """Delete a file that has NOT yet been synced (no chunks to clean up)."""
    if (r := auth(request)): return r
    sb = get_sb()
    try:
        res = sb.table("knowledge_files").select(
            "id,filename,status,synced_at,storage_provider,storage_path,storage_bucket,storage_object_id"
        ).eq("id", file_id).execute()
    except Exception as e:
        print(f"[delete_pending_file] storage-aware query failed ({e}); falling back")
        try:
            res = sb.table("knowledge_files").select("id,filename,status,synced_at").eq("id", file_id).execute()
        except Exception as e2:
            return JSONResponse({"ok": False, "error": str(e2)}, status_code=500)
    if not res.data:
        return JSONResponse({"ok": False, "error": "File not found"}, status_code=404)
    f = res.data[0]
    if f.get("status") == "completed" or f.get("synced_at"):
        return JSONResponse({"ok": False, "error": "Use full delete for synced files"}, status_code=400)
    filename = f["filename"]
    if f.get("storage_path"):
        try:
            from storage import get_storage_service, FileRef
            get_storage_service(f.get("storage_provider")).delete_file(FileRef(
                storage_path=f["storage_path"],
                storage_provider=f.get("storage_provider") or "local",
                storage_bucket=f.get("storage_bucket"),
                storage_object_id=f.get("storage_object_id"),
            ))
        except Exception as e:
            print(f"[delete_pending_file] storage delete failed for file_id={file_id}: {e}")
    # Defensive cleanup even though this path is for never-fully-synced
    # files: a sync attempt can fail partway through after already writing
    # chunks/excel data/attachments for this file_id, leaving it 'failed'
    # (not 'completed', no synced_at) — exactly the state this endpoint
    # targets. Same cleanup set as the full-delete path, just via direct
    # hard-delete of knowledge_files at the end instead of soft-delete,
    # since this file was never a real published record.
    try:
        sb.table("knowledge_chunks").delete().eq("file_id", file_id).execute()
    except Exception as e:
        print(f"[delete_pending_file] chunks cleanup failed for file_id={file_id}: {e}")
    try:
        delete_excel_data(file_id)
    except Exception as e:
        print(f"[delete_pending_file] excel cleanup failed for file_id={file_id}: {e}")
    try:
        sb.table("knowledge_items").update({"deleted_at": _now_iso()}) \
            .eq("knowledge_file_id", file_id).is_("deleted_at", "null").execute()
        sb.table("knowledge_attachments").update({"deleted_at": _now_iso()}) \
            .eq("knowledge_file_id", file_id).is_("deleted_at", "null").execute()
    except Exception as e:
        print(f"[delete_pending_file] knowledge_items/attachments cleanup failed for file_id={file_id}: {e}")
    try:
        _handle_sync_job_files_on_delete(file_id)
    except Exception as e:
        print(f"[delete_pending_file] sync_job_files cleanup failed for file_id={file_id}: {e}")
    try:
        sb.table("knowledge_files").delete().eq("id", file_id).execute()
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    orphans = _verify_no_orphans(file_id)
    return JSONResponse({"ok": True, "filename": filename, "orphans": orphans or None})


@app.get("/admin/knowledge-collections", response_class=HTMLResponse)
async def knowledge_collections_page(request: Request):
    if (r := auth(request)): return r
    return render("knowledge_collections.html", {"request": request, "active": "knowledge-collections"})


@app.get("/admin/api/knowledge-collections")
async def api_list_knowledge_collections(request: Request):
    if (r := auth(request)): return r
    from services.knowledge_collection_service import get_knowledge_collection_service
    return JSONResponse({"ok": True, "collections": get_knowledge_collection_service().list_collections()})


@app.post("/admin/api/knowledge-collections")
async def api_create_knowledge_collection(request: Request):
    if (r := auth(request)): return r
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        return JSONResponse({"ok": False, "error": "name is required"}, status_code=400)
    from services.knowledge_collection_service import get_knowledge_collection_service
    collection = get_knowledge_collection_service().create_collection(name, body.get("description"))
    if not collection:
        return JSONResponse({"ok": False, "error": "Could not create collection"}, status_code=500)
    return JSONResponse({"ok": True, "collection": collection})


@app.put("/admin/api/knowledge-collections/{collection_id}")
async def api_update_knowledge_collection(request: Request, collection_id: str):
    if (r := auth(request)): return r
    body = await request.json()
    from services.knowledge_collection_service import get_knowledge_collection_service
    collection = get_knowledge_collection_service().update_collection(collection_id, body)
    if not collection:
        return JSONResponse({"ok": False, "error": "Could not update collection"}, status_code=500)
    return JSONResponse({"ok": True, "collection": collection})


@app.delete("/admin/api/knowledge-collections/{collection_id}")
async def api_delete_knowledge_collection(request: Request, collection_id: str):
    if (r := auth(request)): return r
    from services.knowledge_collection_service import get_knowledge_collection_service
    result = get_knowledge_collection_service().delete_collection(collection_id)
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@app.post("/admin/api/knowledge-collections/{collection_id}/set-default")
async def api_set_default_knowledge_collection(request: Request, collection_id: str):
    if (r := auth(request)): return r
    from services.knowledge_collection_service import get_knowledge_collection_service
    ok = get_knowledge_collection_service().set_default(collection_id)
    return JSONResponse({"ok": ok})


@app.get("/admin/api/knowledge-collections/files")
async def api_list_files_with_collection(request: Request):
    if (r := auth(request)): return r
    from services.knowledge_collection_service import get_knowledge_collection_service
    return JSONResponse({"ok": True, "files": get_knowledge_collection_service().list_files_with_collection()})


@app.post("/admin/api/knowledge-collections/assign-file")
async def api_assign_file_to_collection(request: Request):
    if (r := auth(request)): return r
    body = await request.json()
    file_id, collection_id = body.get("file_id"), body.get("collection_id")
    if not file_id or not collection_id:
        return JSONResponse({"ok": False, "error": "file_id and collection_id are required"}, status_code=400)
    from services.knowledge_collection_service import get_knowledge_collection_service
    ok = get_knowledge_collection_service().assign_file(file_id, collection_id)
    return JSONResponse({"ok": ok})


@app.get("/admin/file-library", response_class=HTMLResponse)
async def file_library(request: Request):
    if (r := auth(request)): return r
    _run_consistency_guard()
    try:
        # File Library visibility is defined ONLY by synced_at + chunk_count
        # — deliberately NOT status='completed'. status is an informational
        # label written by several code paths; trusting it as a visibility
        # gate is what let a corrupted row (status='completed' but never
        # actually synced) disappear from both this query and the Import
        # Queue at once.
        res = get_sb().table("knowledge_files").select("*")\
            .is_("deleted_at", "null")\
            .not_.is_("synced_at", "null")\
            .order("synced_at", desc=True)\
            .execute()
        # Exclude files that were synced but produced 0 chunks (empty/unreadable files)
        all_files = [f for f in (res.data or []) if (f.get("chunk_count") or 0) > 0]
    except Exception:
        all_files = []
    for item in all_files:
        item["on_disk"] = (KNOWLEDGE_DIR / item.get("filename", "")).exists()
        item.setdefault("status", "completed")
        item.setdefault("title", "")
        item.setdefault("scope", "")
        item.setdefault("platform", "")
        item.setdefault("department", "")
        item.setdefault("tags", "")
        item.setdefault("description", "")
        item.setdefault("language", "Thai")
        item.setdefault("visibility", "Internal Only")
        item.setdefault("last_error", "")
        item.setdefault("version", 1)
    return render("file_library.html", {
        "request": request, "all_files": all_files,
        "scopes": SCOPES, "sources": SOURCES, "departments": DEPARTMENTS,
        "languages": LANGUAGES,
    })


@app.get("/admin/sync-activity", response_class=HTMLResponse)
async def sync_activity(request: Request):
    if (r := auth(request)): return r
    try:
        res = get_sb().table("knowledge_sync_jobs").select("*").order("started_at", desc=True).limit(100).execute()
        jobs = res.data or []
    except Exception:
        jobs = []
    if jobs:
        try:
            job_ids = [j["id"] for j in jobs]
            ids_str = "(" + ",".join(job_ids) + ")"
            fr = get_sb().table("knowledge_sync_job_files") \
                .select("job_id,file_id,file_name,status,chunk_count,progress_percent,error_message,created_at,updated_at") \
                .filter("job_id", "in", ids_str).order("created_at").execute()
            files_by_job: dict = {}
            for f in (fr.data or []):
                files_by_job.setdefault(f["job_id"], []).append(f)
            for j in jobs:
                j["files"] = files_by_job.get(j["id"], [])
                expected = j.get("total_files") or 0
                actual = len(j["files"])
                print(f"[SyncActivity] rendering job_id {j['id']} items count {actual}")
                if expected and actual != expected:
                    print(f"[SyncValidation] job {j['id']}: total_files={expected} but job_files rows={actual}")
        except Exception as _e:
            print(f"[sync_activity] files fetch error: {_e}")
            for j in jobs:
                j.setdefault("files", [])
    return render("sync_activity.html", {"request": request, "jobs": jobs})


@app.get("/admin/preview", response_class=HTMLResponse)
async def preview(request: Request):
    """AI Playground — see /admin/playground/ask and /admin/playground/status
    for the actual pipeline; this route only renders the shell. The Prompt
    Template selector loads its options itself via GET /admin/api/ai/prompts
    (real Prompt Studio data) — this route no longer needs to pre-fetch
    templates/line_oa_prompt/global_prompt just to seed a server-rendered
    <select>, now that the selector is a client-side custom listbox."""
    if (r := auth(request)): return r
    return render("preview.html", {"request": request})


# ── Prompt Studio ────────────────────────────────────────────────
# Page + API. Read helpers live in services/prompt_builder.py (what
# actually gets sent to the LLM); write/versioning/assignment logic
# lives in services/prompt_studio_service.py. See migrations/
# 017_prompt_studio.sql for the schema these routes talk to.

@app.get("/admin/ai/prompts", response_class=HTMLResponse)
async def prompt_studio_page(request: Request):
    if (r := auth(request)): return r
    from services.prompt_studio_service import CHANNELS
    from services.prompt_builder import BASE_CONVERSATION_RULES
    # Read-only, platform-standard text — services/prompt_builder.py is
    # the single source of truth (it's what actually gets injected into
    # every built prompt); the template only ever displays it, never
    # edits or re-derives it.
    return render("prompt_studio.html", {"request": request, "active": "prompt-studio", "channels": CHANNELS,
                                          "base_conversation_rules": BASE_CONVERSATION_RULES})


@app.get("/admin/api/ai/prompts")
async def api_list_prompts(request: Request):
    if (r := auth(request)): return r
    q = request.query_params
    from services.prompt_studio_service import get_prompt_studio_service
    is_active = {"true": True, "false": False}.get(q.get("is_active"))
    is_default = {"true": True, "false": False}.get(q.get("is_default"))
    prompts = get_prompt_studio_service().list_prompts(
        search=q.get("search") or None, channel=q.get("channel") or None,
        is_active=is_active, is_default=is_default,
    )
    return JSONResponse({"ok": True, "prompts": prompts})


@app.get("/admin/api/ai/prompts/{prompt_id}")
async def api_get_prompt(request: Request, prompt_id: str):
    if (r := auth(request)): return r
    from services.prompt_studio_service import get_prompt_studio_service
    svc = get_prompt_studio_service()
    prompt = svc.get_prompt(prompt_id)
    if not prompt:
        return JSONResponse({"ok": False, "error": "Prompt not found"}, status_code=404)
    return JSONResponse({"ok": True, "prompt": prompt, "versions": svc.list_versions(prompt_id)})


@app.post("/admin/api/ai/prompts")
async def api_create_prompt(request: Request):
    if (r := auth(request)): return r
    body = await request.json()
    from services.prompt_studio_service import get_prompt_studio_service
    prompt = get_prompt_studio_service().create_prompt(body)
    if not prompt:
        return JSONResponse({"ok": False, "error": "Could not create prompt (migration 017 run?)"}, status_code=500)
    return JSONResponse({"ok": True, "prompt": prompt})


@app.put("/admin/api/ai/prompts/{prompt_id}")
async def api_update_prompt(request: Request, prompt_id: str):
    if (r := auth(request)): return r
    body = await request.json()
    from services.prompt_studio_service import get_prompt_studio_service
    prompt = get_prompt_studio_service().update_prompt(prompt_id, body)
    if not prompt:
        return JSONResponse({"ok": False, "error": "Could not update prompt"}, status_code=500)
    return JSONResponse({"ok": True, "prompt": prompt})


@app.delete("/admin/api/ai/prompts/{prompt_id}")
async def api_delete_prompt(request: Request, prompt_id: str):
    if (r := auth(request)): return r
    force = request.query_params.get("force") == "true"
    from services.prompt_studio_service import get_prompt_studio_service
    result = get_prompt_studio_service().delete_prompt(prompt_id, force=force)
    return JSONResponse(result, status_code=200 if result.get("ok") else (409 if result.get("warning") else 400))


@app.post("/admin/api/ai/prompts/{prompt_id}/duplicate")
async def api_duplicate_prompt(request: Request, prompt_id: str):
    if (r := auth(request)): return r
    from services.prompt_studio_service import get_prompt_studio_service
    prompt = get_prompt_studio_service().duplicate_prompt(prompt_id)
    if not prompt:
        return JSONResponse({"ok": False, "error": "Could not duplicate prompt"}, status_code=500)
    return JSONResponse({"ok": True, "prompt": prompt})


@app.post("/admin/api/ai/prompts/{prompt_id}/activate")
async def api_activate_prompt(request: Request, prompt_id: str):
    if (r := auth(request)): return r
    from services.prompt_studio_service import get_prompt_studio_service
    svc = get_prompt_studio_service()
    ok = svc.activate_prompt(prompt_id)
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    if ok and body.get("set_default"):
        svc.set_default(prompt_id)
    return JSONResponse({"ok": ok})


@app.post("/admin/api/ai/prompts/{prompt_id}/set-default")
async def api_set_default_prompt(request: Request, prompt_id: str):
    if (r := auth(request)): return r
    from services.prompt_studio_service import get_prompt_studio_service
    ok = get_prompt_studio_service().set_default(prompt_id)
    return JSONResponse({"ok": ok})


@app.post("/admin/api/ai/prompts/{prompt_id}/new-version")
async def api_new_version_prompt(request: Request, prompt_id: str):
    if (r := auth(request)): return r
    body = await request.json()
    from services.prompt_studio_service import get_prompt_studio_service
    prompt = get_prompt_studio_service().save_as_new_version(prompt_id, body)
    if not prompt:
        return JSONResponse({"ok": False, "error": "Could not create new version"}, status_code=500)
    return JSONResponse({"ok": True, "prompt": prompt})


@app.post("/admin/api/ai/prompts/{prompt_id}/rollback")
async def api_rollback_prompt(request: Request, prompt_id: str):
    if (r := auth(request)): return r
    body = await request.json()
    target_version_id = body.get("target_version_id")
    if not target_version_id:
        return JSONResponse({"ok": False, "error": "target_version_id is required"}, status_code=400)
    from services.prompt_studio_service import get_prompt_studio_service
    prompt = get_prompt_studio_service().rollback(prompt_id, target_version_id)
    if not prompt:
        return JSONResponse({"ok": False, "error": "Could not roll back"}, status_code=500)
    return JSONResponse({"ok": True, "prompt": prompt})


@app.get("/admin/api/ai/prompt-assignments")
async def api_list_assignments(request: Request):
    if (r := auth(request)): return r
    from services.prompt_studio_service import get_prompt_studio_service, CHANNELS
    svc = get_prompt_studio_service()
    assignments = svc.list_assignments()
    by_channel = {a["channel"]: a for a in assignments}
    # Every channel always appears, even if unassigned — the UI needs to
    # show "Not assigned (falls back to Global Default)" explicitly.
    rows = [by_channel.get(ch, {"channel": ch, "prompt_template_id": None, "ai_prompt_templates": None})
            for ch in CHANNELS]
    return JSONResponse({"ok": True, "assignments": rows})


@app.post("/admin/api/ai/prompt-assignments")
async def api_create_assignment(request: Request):
    if (r := auth(request)): return r
    body = await request.json()
    channel, prompt_template_id = body.get("channel"), body.get("prompt_template_id")
    if not channel or not prompt_template_id:
        return JSONResponse({"ok": False, "error": "channel and prompt_template_id are required"}, status_code=400)
    from services.prompt_studio_service import get_prompt_studio_service
    result = get_prompt_studio_service().assign_channel(channel, prompt_template_id)
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@app.put("/admin/api/ai/prompt-assignments/{assignment_id}")
async def api_update_assignment(request: Request, assignment_id: str):
    if (r := auth(request)): return r
    body = await request.json()
    prompt_template_id = body.get("prompt_template_id")
    if not prompt_template_id:
        return JSONResponse({"ok": False, "error": "prompt_template_id is required"}, status_code=400)
    from services.prompt_studio_service import get_prompt_studio_service
    result = get_prompt_studio_service().update_assignment(assignment_id, prompt_template_id)
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@app.get("/admin/api/ai/prompt-tier-assignments")
async def api_list_tier_assignments(request: Request):
    if (r := auth(request)): return r
    from services.prompt_studio_service import get_prompt_studio_service, TIERS
    svc = get_prompt_studio_service()
    assignments = svc.list_tier_assignments()
    by_tier = {a["tier"]: a for a in assignments}
    # Every tier always appears, even if unassigned — mirrors
    # api_list_assignments' "show every channel" behavior above. Unassigned
    # is a normal, common state here (tier prompts are opt-in).
    rows = [by_tier.get(t, {"tier": t, "prompt_template_id": None, "ai_prompt_templates": None}) for t in TIERS]
    return JSONResponse({"ok": True, "assignments": rows})


@app.post("/admin/api/ai/prompt-tier-assignments")
async def api_create_tier_assignment(request: Request):
    if (r := auth(request)): return r
    body = await request.json()
    tier, prompt_template_id = body.get("tier"), body.get("prompt_template_id")
    if not tier or not prompt_template_id:
        return JSONResponse({"ok": False, "error": "tier and prompt_template_id are required"}, status_code=400)
    from services.prompt_studio_service import get_prompt_studio_service
    result = get_prompt_studio_service().assign_tier(tier, prompt_template_id)
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@app.delete("/admin/api/ai/prompt-tier-assignments/{tier}")
async def api_delete_tier_assignment(request: Request, tier: str):
    if (r := auth(request)): return r
    from services.prompt_studio_service import get_prompt_studio_service
    result = get_prompt_studio_service().unassign_tier(tier)
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@app.post("/admin/api/ai/prompts/test")
async def api_test_prompt(request: Request):
    if (r := auth(request)): return r
    body = await request.json()
    prompt_id, question = body.get("prompt_id"), (body.get("question") or "").strip()
    if not prompt_id or not question:
        return JSONResponse({"ok": False, "error": "prompt_id and question are required"}, status_code=400)
    from services.prompt_studio_service import get_prompt_studio_service
    from dataclasses import asdict
    result = get_prompt_studio_service().test_prompt(prompt_id, question)
    if not result:
        return JSONResponse({"ok": False, "error": "Test failed"}, status_code=500)
    return JSONResponse({
        "ok": True, "answer": result.answer,
        "prompt": {"template_name": result.prompt.template.name, "template_version": result.prompt.template.version,
                   "final_prompt": result.prompt.final_prompt_text},
        "metadata": {"model": result.model, "confidence": round(result.confidence, 4),
                     "confidence_label": result.confidence_label, "latency_ms": round(result.latency_ms, 1)},
    })


@app.get("/admin/ai/policies", response_class=HTMLResponse)
async def ai_policies_page(request: Request):
    if (r := auth(request)): return r
    return render("ai_policies.html", {"request": request, "active": "ai-policies"})


@app.get("/admin/api/ai/policies")
async def api_list_policies(request: Request):
    if (r := auth(request)): return r
    from services.policy_studio_service import get_policy_studio_service
    policies = get_policy_studio_service().list_policy_sets(search=request.query_params.get("search") or None)
    return JSONResponse({"ok": True, "policies": policies})


@app.get("/admin/api/ai/policies/{policy_id}")
async def api_get_policy(request: Request, policy_id: str):
    if (r := auth(request)): return r
    from services.policy_studio_service import get_policy_studio_service
    policy = get_policy_studio_service().get_policy_set(policy_id)
    if not policy:
        return JSONResponse({"ok": False, "error": "Policy set not found"}, status_code=404)
    return JSONResponse({"ok": True, "policy": policy})


@app.post("/admin/api/ai/policies")
async def api_create_policy(request: Request):
    if (r := auth(request)): return r
    body = await request.json()
    from services.policy_studio_service import get_policy_studio_service
    policy = get_policy_studio_service().create_policy_set(body)
    if not policy:
        return JSONResponse({"ok": False, "error": "Could not create policy set (migration 024 run?)"}, status_code=500)
    return JSONResponse({"ok": True, "policy": policy})


@app.put("/admin/api/ai/policies/{policy_id}")
async def api_update_policy(request: Request, policy_id: str):
    if (r := auth(request)): return r
    body = await request.json()
    from services.policy_studio_service import get_policy_studio_service
    policy = get_policy_studio_service().update_policy_set(policy_id, body)
    if not policy:
        return JSONResponse({"ok": False, "error": "Could not save"}, status_code=500)
    return JSONResponse({"ok": True, "policy": policy})


@app.delete("/admin/api/ai/policies/{policy_id}")
async def api_delete_policy(request: Request, policy_id: str):
    if (r := auth(request)): return r
    force = request.query_params.get("force") == "true"
    from services.policy_studio_service import get_policy_studio_service
    result = get_policy_studio_service().delete_policy_set(policy_id, force=force)
    return JSONResponse(result, status_code=200 if result.get("ok") else (409 if result.get("warning") else 400))


@app.post("/admin/api/ai/policies/{policy_id}/duplicate")
async def api_duplicate_policy(request: Request, policy_id: str):
    if (r := auth(request)): return r
    from services.policy_studio_service import get_policy_studio_service
    policy = get_policy_studio_service().duplicate_policy_set(policy_id)
    if not policy:
        return JSONResponse({"ok": False, "error": "Could not duplicate"}, status_code=500)
    return JSONResponse({"ok": True, "policy": policy})


@app.post("/admin/api/ai/policies/{policy_id}/set-default")
async def api_set_default_policy(request: Request, policy_id: str):
    if (r := auth(request)): return r
    from services.policy_studio_service import get_policy_studio_service
    ok = get_policy_studio_service().set_default(policy_id)
    return JSONResponse({"ok": ok})


@app.post("/admin/api/hybrid-playground/ask")
async def hybrid_playground_ask(request: Request):
    """Hybrid Playground (2026-07-29 integration/wiring sprint; extended
    2026-08-02 Hybrid Question Segmentation sprint) — wires the EXISTING
    RAG orchestrator (services/playground_orchestrator.py::
    run_playground_turn, untouched) and the EXISTING ERP test harness
    (services/erp_test_harness.py::run_erp_test, untouched) onto one
    route with a mode selector. This route does NOT reimplement
    retrieval, prompting, or ERP execution — it only dispatches to the
    two pre-existing entry points.

    Auto mode now uses services/hybrid_question_classifier.py — a
    deterministic, Registry-driven classifier (RAG_ONLY / ERP_ONLY /
    HYBRID / CLARIFICATION_REQUIRED / UNKNOWN) that can select Hybrid,
    replacing the old binary-only naive_auto_route for Auto mode
    specifically. For a HYBRID classification (Auto) or explicit Hybrid
    mode, the question is SEGMENTED into an ERP sub-question and a RAG
    sub-question before execution (never the full compound question sent
    to both paths) and combined via
    services/hybrid_playground_router.py::synthesize_hybrid_answer — an
    explicit, structured, non-LLM combination, never intelligent
    cross-domain reasoning. Still Playground-only: not the production
    Decision Engine, not wired to the live LINE webhook.

    Body: {question, mode: auto|rag|erp|hybrid, action_id (ERP/Hybrid,
    optional for Auto), template_id, session_id, and the same ERP
    conversation-state passthrough fields api_erp_test_run() accepts
    (history, collected_params, awaiting_information_selection,
    available_response_options, last_normalized_result,
    conversation_state, confirmed, enforce_confirmation_gate)."""
    if (r := auth(request)): return r
    body = await request.json()
    question = (body.get("question") or "").strip()
    mode = (body.get("mode") or "auto").strip().lower()
    if mode not in ("auto", "rag", "erp", "hybrid"):
        mode = "auto"
    if not question:
        return JSONResponse({"ok": False, "error": "Question is required"}, status_code=400)

    from services.playground_orchestrator import run_playground_turn
    from services.erp_test_harness import run_erp_test
    from services.business_action_registry import get_registry as get_action_registry
    from services.hybrid_playground_router import synthesize_hybrid_answer
    from services.hybrid_question_classifier import classify_question
    from dataclasses import asdict
    import traceback

    action_id = body.get("action_id")
    errors: List[str] = []

    def _run_rag(override_message: Optional[str] = None):
        try:
            result = run_playground_turn(
                override_message or question, template_id=body.get("template_id"),
                top_k=int(body.get("top_k") or 3),
                temperature=float(body.get("temperature") if body.get("temperature") is not None else 0.3),
                max_tokens=int(body.get("max_tokens") or 500),
                history=body.get("history") or [],
            )
            return result, None
        except Exception as e:
            print(f"[HybridPlayground] RAG path failed: {e}")
            return None, str(e)

    def _run_erp(resolved_action_id: Optional[str], override_message: Optional[str] = None):
        if not resolved_action_id:
            return None, "no ERP action selected"
        try:
            result = run_erp_test(
                sb=get_sb(), action_id=resolved_action_id, message=override_message or question,
                mode=body.get("erp_mode") or "simulation",
                history=body.get("history") or [], collected_params=body.get("collected_params") or {},
                language=body.get("language") or "th",
                awaiting_information_selection=bool(body.get("awaiting_information_selection")),
                available_response_options=body.get("available_response_options") or None,
                last_normalized_result=body.get("last_normalized_result") or None,
                conversation_form_state=body.get("conversation_state") or None,
                confirmed=bool(body.get("confirmed")),
                enforce_confirmation_gate=bool(body.get("enforce_confirmation_gate")),
            )
            return result, (result.get("error") if not result.get("ok") else None)
        except Exception as e:
            print(f"[HybridPlayground] ERP path failed: {e}\n{traceback.format_exc()}")
            return {"ok": False, "error": str(e), "trace": [], "summary": {}}, str(e)

    route_decision = {"route": mode, "reason": f"explicit mode={mode}"}
    rag_result = erp_result = None
    classification_out: Optional[Dict] = None
    clarification_out: Optional[Dict] = None
    is_hybrid_turn = False
    rag_err = erp_err = None

    if mode == "auto":
        reg = get_action_registry(get_sb())
        classification = classify_question(question, reg)
        classification_out = classification
        cls = classification["classification"]
        route_decision = {
            "route": cls.lower(), "reason": "; ".join(classification["evidence"]),
            "confidence": classification["confidence"], "classification": cls,
        }
        if cls == "CLARIFICATION_REQUIRED":
            clarification_out = {
                "message": "พบ Business Action ที่ตรงกับคำถามมากกว่าหนึ่งรายการ กรุณาระบุให้ชัดเจนขึ้น หรือเลือก Business Action เอง",
                "candidate_action_ids": classification["candidate_action_ids"],
            }
        elif cls == "HYBRID":
            is_hybrid_turn = True
            erp_result, erp_err = _run_erp(classification["selected_action_id"], classification["erp_sub_question"])
            rag_result, rag_err = _run_rag(classification["rag_sub_question"])
            if erp_err: errors.append(erp_err)
            if rag_err: errors.append(rag_err)
        elif cls == "ERP_ONLY":
            erp_result, erp_err = _run_erp(classification["selected_action_id"], classification.get("erp_sub_question"))
            if erp_err: errors.append(erp_err)
        else:  # RAG_ONLY / UNKNOWN — safe fallback, never leaves the customer with nothing
            rag_result, rag_err = _run_rag(classification.get("rag_sub_question"))
            if rag_err: errors.append(rag_err)
    elif mode == "rag":
        rag_result, err = _run_rag()
        if err: errors.append(err)
    elif mode == "erp":
        erp_result, err = _run_erp(action_id)
        if err: errors.append(err)
    elif mode == "hybrid":
        is_hybrid_turn = True
        reg = get_action_registry(get_sb())
        classification = classify_question(question, reg, forced_action_id=action_id) if action_id else classify_question(question, reg)
        classification_out = classification
        erp_action_id = action_id or classification.get("selected_action_id")
        erp_sub_q = classification.get("erp_sub_question") or question
        rag_sub_q = classification.get("rag_sub_question") or question
        rag_result, rag_err = _run_rag(rag_sub_q)
        erp_result, erp_err = _run_erp(erp_action_id, erp_sub_q)
        if rag_err: errors.append(rag_err)
        if erp_err: errors.append(erp_err)
        route_decision = {"route": "hybrid", "reason": "explicit mode=hybrid — both RAG and ERP executed",
                           "classification": classification.get("classification")}

    rag_out = None
    if rag_result is not None:
        rag_out = {
            "answer": rag_result.answer,
            "chunks": [{
                "text": c.get("text"), "source": c.get("source"), "file_name": c.get("file_name"),
                "score": c.get("score"), "hybrid_score": c.get("hybrid_score"),
                "rerank_score": c.get("rerank_score"), "citation": c.get("citation"),
                "embedding_provider": c.get("embedding_provider"), "embedding_model": c.get("embedding_model"),
                "embedding_dimensions": c.get("embedding_dimensions"),
                "raw_vector_similarity": c.get("raw_vector_similarity"), "cited": c.get("cited", False),
            } for c in rag_result.chunks],
            "prompt": {
                # Prompt Studio integration (2026-08-02 Phase 3) — bring
                # this route's prompt detail to parity with
                # /admin/playground/ask, which already exposed all of
                # this from the SAME BuiltPrompt object
                # (services/prompt_builder.py) — no new field is computed
                # here, only copied through.
                "template_id": rag_result.prompt.template.id,
                "template_name": rag_result.prompt.template.name,
                "template_version": rag_result.prompt.template.version,
                "system_prompt": rag_result.prompt.template.system_prompt,
                "user_prompt": rag_result.prompt.messages[1]["content"] if len(rag_result.prompt.messages) > 1 else None,
                "final_prompt": rag_result.prompt.final_prompt_text,
            },
            "temperature": rag_result.temperature,
            # AI Policies (services/policy_engine.py) — likewise already
            # computed on rag_result.policy/policy_set_name but never
            # exposed by this route; same PolicyResult shape
            # /admin/playground/ask already returns.
            "policy": {
                "escalate": rag_result.policy.escalate,
                "active_count": rag_result.policy.active_count,
                "verdicts": [asdict(v) for v in rag_result.policy.verdicts],
                "notes": rag_result.policy.notes,
                "policy_set_name": rag_result.policy_set_name,
            },
            "confidence": rag_result.confidence, "confidence_label": rag_result.confidence_label,
            "answerability": rag_result.answerability,
            "latency_ms": rag_result.latency_ms,
            "embedding_model": rag_result.embedding_model, "embedding_provider": rag_result.embedding_provider,
            "embedding_dimensions": rag_result.embedding_dimensions,
            "input_tokens": rag_result.input_tokens, "output_tokens": rag_result.output_tokens,
            "pipeline": [asdict(s) for s in rag_result.stages],
            # AI Playground redesign (2026-08-02 Phase 2) — these were
            # already computed on `rag_result` (PlaygroundResult, see
            # services/playground_orchestrator.py) but never copied into
            # this route's response; the Unified Pipeline view's
            # Intent/Entities/LLM stages need them for Auto/ERP/Hybrid
            # mode the same way /admin/playground/ask already exposes
            # them for plain RAG mode. Purely additive keys, no behavior
            # change for any existing consumer of this response.
            "model": rag_result.model,
            "broad_intent": rag_result.broad_intent, "actionable_intent": rag_result.actionable_intent,
            "intent_confidence": rag_result.intent_confidence, "intent_entities": rag_result.intent_entities,
        }

    erp_out = None
    if erp_result is not None:
        erp_out = {
            "ok": erp_result.get("ok"), "summary": erp_result.get("summary"),
            "trace": erp_result.get("trace"), "answer": erp_result.get("answer"),
            "collected_params": erp_result.get("collected_params"),
            "missing_parameters": erp_result.get("missing_parameters"),
            "clarification_question": erp_result.get("clarification_question"),
            "normalized_result": erp_result.get("normalized_result"),
            "conversation_strategy": erp_result.get("conversation_strategy"),
            "conversation_state": erp_result.get("conversation_state"),
            "conversation_form": erp_result.get("conversation_form"),
            "awaiting_information_selection": erp_result.get("awaiting_information_selection"),
            "available_response_options": erp_result.get("available_response_options"),
            "error": erp_result.get("error"), "warning": erp_result.get("warning"),
        }

    hybrid_out = None
    if is_hybrid_turn:
        # Hybrid Question Segmentation sprint (2026-08-02) — explicit,
        # structured synthesis (services/hybrid_playground_router.py::
        # synthesize_hybrid_answer): each section reflects ONLY its own
        # sub-question's result; citations are attached to the RAG
        # section only; a path that errored gets an honest unavailability
        # note instead of a silently-missing section.
        rag_citations = [c["citation"] for c in (rag_out.get("chunks") or []) if c.get("cited") and c.get("citation")] if rag_out else []
        erp_answer_for_synthesis = (erp_out.get("answer") or erp_out.get("clarification_question")) if erp_out else None
        synthesis = synthesize_hybrid_answer(
            erp_answer=erp_answer_for_synthesis, erp_error=erp_err,
            rag_answer=rag_out.get("answer") if rag_out else None, rag_error=rag_err,
            rag_citations=rag_citations,
        )
        hybrid_out = {
            "route": ["rag", "erp"],
            "erp_contribution": synthesis["erp_section"],
            "rag_contribution": synthesis["rag_section"],
            "merged_answer": synthesis["merged_answer"],
            "merge_strategy": synthesis["strategy"],
        }

    return JSONResponse({
        "ok": True, "mode": mode, "route_decision": route_decision,
        "rag": rag_out, "erp": erp_out, "hybrid": hybrid_out,
        # Hybrid Question Segmentation sprint (2026-08-02) — the
        # classifier's own output (classification/confidence/evidence/
        # selected action/sub-questions), so Developer Mode can show
        # exactly what decided Auto/Hybrid routing this turn, and the
        # ambiguity-clarification payload when 2+ Business Actions matched.
        "classification": classification_out, "clarification": clarification_out,
        "errors": errors,
    })


@app.post("/admin/playground/ask")
async def playground_ask(request: Request):
    if (r := auth(request)): return r
    body = await request.json()
    question = (body.get("question") or "").strip()
    if not question:
        return JSONResponse({"ok": False, "error": "Question is required"}, status_code=400)

    from services.playground_orchestrator import run_playground_turn
    from services.session_service import get_session_service
    from dataclasses import asdict

    session_id = body.get("session_id")
    session_service = get_session_service()

    # Multi-turn: prior messages of THIS session become chat history for
    # the new turn (ChatGPT-style) — see prompt_builder.build_prompt's
    # `history` param.
    history = []
    if session_id:
        existing = session_service.get_session(session_id)
        if existing:
            history = [{"role": m["role"], "content": m["content"]} for m in existing["messages"]]

    try:
        result = run_playground_turn(
            question,
            template_id=body.get("template_id"),
            top_k=int(body.get("top_k") or 3),
            temperature=float(body.get("temperature") if body.get("temperature") is not None else 0.3),
            max_tokens=int(body.get("max_tokens") or 500),
            history=history,
        )
    except Exception as e:
        print(f"[Playground] turn failed: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    _record_playground_latency(result.latency_ms)

    session = session_service.record_turn(session_id, question, result)

    from services.knowledge_graph_service import get_knowledge_graph_service
    chunks_out = []
    for c in result.chunks:
        # Graph Context — best-effort; a chunk with no linked graph edges
        # (most chunks, since extraction is selective per spec) just gets
        # an empty list, never an error shown to the user.
        graph_context = []
        if c.get("chunk_id"):
            try:
                g = get_knowledge_graph_service().get_graph_for_chunk(get_sb(), c["chunk_id"])
                node_by_id = {n["id"]: n["name"] for n in g["nodes"]}
                graph_context = [{
                    "source": node_by_id.get(e["source_node_id"], "?"),
                    "relation": e.get("relation_label") or e["relation_type"],
                    "target": node_by_id.get(e["target_node_id"], "?"),
                    "evidence_text": e.get("evidence_text"),
                } for e in g["edges"]]
            except Exception as ex:
                print(f"[Playground] graph context lookup failed for chunk {c.get('chunk_id')}: {ex}")

        chunks_out.append({
            "text": c.get("text"), "source": c.get("source"), "file_name": c.get("file_name"),
            "score": c.get("score"), "citation": c.get("citation"), "page_number": c.get("page_number"),
            "section_title": c.get("section_title"), "chunk_index": c.get("chunk_index"),
            "category": c.get("category"), "language": c.get("language"),
            "is_structured": c.get("is_structured"), "is_calculated": c.get("is_calculated"),
            "attachments": c.get("attachments") or [],
            # AI Knowledge Analyzer output (services/knowledge_analyzer.py)
            "knowledge_type": c.get("knowledge_type"), "chunk_strategy": c.get("chunk_strategy"),
            "heading_path": c.get("heading_path"), "step_index": c.get("step_index"),
            "suggested_questions": c.get("suggested_questions"),
            # Knowledge Graph context (services/knowledge_graph_service.py)
            "graph_context": graph_context,
            # Hybrid retrieval score breakdown (rag/hybrid_scoring.py) —
            # None for structured/calculated results, which skip rescoring.
            "keyword_score": c.get("keyword_score"), "heading_score": c.get("heading_score"),
            "graph_score": c.get("graph_score"), "hybrid_score": c.get("hybrid_score"),
            "rerank_score": c.get("rerank_score"),
            "raw_vector_similarity": c.get("raw_vector_similarity"),
            "normalized_vector_score": c.get("normalized_vector_score"),
            # Retrieval debug fields: raw candidate rank BEFORE hybrid
            # rerank, the evidence-tier classification (legacy) and the
            # new granular evidence_label, plus which query variant/
            # heading actually matched this chunk (query expansion +
            # heading synonym matching — see rag/hybrid_scoring.py).
            "raw_vector_rank": c.get("raw_vector_rank"), "classification": c.get("classification"),
            "evidence_label": c.get("evidence_label"),
            "matched_query": c.get("matched_query"), "matched_heading": c.get("matched_heading"),
            "from_lexical_search": c.get("from_lexical_search", False),
            # Per-chunk embedding provenance — never infer the model from a
            # global header; each chunk carries what actually created it.
            "embedding_provider": c.get("embedding_provider"), "embedding_model": c.get("embedding_model"),
            "embedding_version": c.get("embedding_version"), "embedding_dimensions": c.get("embedding_dimensions"),
            # rag/evidence_classifier.py: DIRECT_EVIDENCE/PARTIAL_EVIDENCE/
            # RELATED_CONTEXT/IRRELEVANT — a different axis from the
            # hybrid-scoring `classification` above (retrieval-time
            # relevance vs. does-this-chunk-support-the-exact-claim).
            "evidence_classification": c.get("evidence_classification"),
            "cited": c.get("cited", False),
            # Vision+OCR ingestion provenance — shown in Developer Mode so
            # an answer sourced from an OCR'd/Vision-analyzed page is
            # clearly labeled (Source File/Page/Extraction Method/OCR
            # Confidence/Vision Used per the AI Playground spec).
            "extraction_method": c.get("extraction_method"), "page_type": c.get("page_type"),
            "text_quality_score": c.get("text_quality_score"), "ocr_confidence": c.get("ocr_confidence"),
            "vision_used": c.get("vision_used"), "extraction_warnings": c.get("extraction_warnings"),
            # Phase 2 (AI Playground Intelligence Pipeline): document_purpose
            # (services/document_purpose.py) + metadata_match (rag/
            # metadata_retrieval.py) for the "Metadata Match" /
            # "Document Purpose" Explainability fields; merged_citations
            # (rag/context_builder.py) lists every citation folded into
            # this chunk if it absorbed a duplicate/adjacent-overlap chunk
            # — the ORIGINAL `citation` field above is never removed.
            "document_purpose": c.get("document_purpose"),
            "metadata_match": c.get("metadata_match"),
            "merged_citations": c.get("merged_citations") or [],
        })

    # Excluded candidates (never sent to the LLM) — surfaced ONLY for
    # admin debugging, per Part 20; each carries its exclusion_reason so an
    # operator can see why a chunk was dropped instead of just its absence.
    excluded_out = [{
        "file_name": e.get("file_name"), "section_title": e.get("section_title"),
        "score": e.get("score"), "raw_vector_similarity": e.get("raw_vector_similarity"),
        "normalized_vector_score": e.get("normalized_vector_score"),
        "keyword_score": e.get("keyword_score"), "heading_score": e.get("heading_score"),
        "hybrid_score": e.get("hybrid_score"), "classification": e.get("classification"),
        "evidence_label": e.get("evidence_label"), "matched_query": e.get("matched_query"),
        "exclusion_reason": e.get("exclusion_reason"),
    } for e in (result.excluded_candidates or [])]

    return JSONResponse({
        "ok": True,
        "session_id": session["id"] if session else session_id,
        "session": session,
        "answer": result.answer,
        # Human-like Multi-Message Replies (services/message_segmenter.py)
        # — `answer` above stays the full canonical string for citations/
        # logs/analytics/Copy Answer/backward compatibility; these are
        # additive fields for the Playground's chat-bubble preview and
        # Explainability tab. LINE-ready shape (see SegmentedReply).
        "message_parts": result.message_parts,
        "message_delay_ms": result.message_delay_ms,
        "attachment_order": result.attachment_order,
        "reply_mode_used": result.reply_mode_used,
        "segmentation_applied": result.segmentation_applied,
        "message_count": result.message_count,
        "segment_reasons": result.segment_reasons,
        "boundary_types": result.boundary_types,
        # Message Segmentation data contract (Part 6, P0 2026-07-21) —
        # `messages` is the SOURCE OF TRUTH for any channel/UI supporting
        # multiple messages; `reply_text` is `answer` under an explicit
        # name, kept only for backward compatibility/exports/logs/single-
        # message channels. Built from the exact same message_parts above.
        "messages": result.messages,
        "reply_text": result.reply_text,
        # Knowledge Gap Handling (Part 10) — Developer-Mode-only; the
        # customer-facing `answer`/`messages` above are never altered by
        # this. None unless actionable_intent is company_overview/
        # company_summary AND evidence is a synthesis, not an explicit
        # company-profile FAQ/heading.
        "company_profile_warning": result.company_profile_warning,
        # Conversation Intelligence — Unified Intent Classification /
        # Answer Planner / Attachment Planner. Additive; every field
        # above is unchanged.
        "broad_intent": result.broad_intent,
        "actionable_intent": result.actionable_intent,
        "intent_confidence": result.intent_confidence,
        "requested_attributes": result.requested_attributes,
        "intent_entities": result.intent_entities,
        "answer_plan": result.answer_plan,
        "attachment_plan": result.attachment_plan,
        "selected_attachments": result.selected_attachments,
        "chunks": chunks_out,
        "context": result.context,
        # Query expansion debug (Part 5/11) — original_query/
        # expanded_queries/detected_language for the Explainability tab.
        # Phase 1 additionally includes normalized_query/detected_intent/
        # rewritten_query (rag/query_understanding.py).
        "query_expansion": result.query_expansion,
        # Conversation Intelligence Phase 1 (rag/conversation_state.py) —
        # topic bucket/subtopic/entities/transition/confidence, displayed
        # in its own Explainability panel BEFORE Canonical Query Rewrite.
        "conversation_state": result.conversation_state,
        # Information Collection Engine / Slot Filling Engine (services/
        # slot_filling_engine.py) — Developer Mode: intent, required/
        # collected/missing slots, next expected slot, escalation
        # decision. None when no ERP-backed intent was detected.
        "slot_filling_state": result.slot_filling_state,
        # Phase 2 (AI Playground Intelligence Pipeline) — Context Builder
        # summary (rag/context_builder.py) and Retrieval Confidence
        # (rag/retrieval_confidence.py), for the Explainability tab.
        "context_builder_summary": result.context_builder_summary,
        "retrieval_confidence": result.retrieval_confidence,
        "retrieval_confidence_components": result.retrieval_confidence_components,
        "prompt": {
            "template_id": result.prompt.template.id,
            "template_name": result.prompt.template.name,
            "template_version": result.prompt.template.version,
            "system_prompt": result.prompt.template.system_prompt,
            # Prompt Studio integration (2026-08-02 Phase 3) — the user-half
            # of BuiltPrompt.messages (services/prompt_builder.py) was
            # already computed, just never copied into this response.
            "user_prompt": result.prompt.messages[1]["content"] if len(result.prompt.messages) > 1 else None,
            "final_prompt": result.prompt.final_prompt_text,
        },
        "policy": {
            "escalate": result.policy.escalate,
            "active_count": result.policy.active_count,
            "verdicts": [asdict(v) for v in result.policy.verdicts],
            "notes": result.policy.notes,
            # AI Policies (services/policy_studio_service.py) — which
            # policy set was actually applied, for the Explainability tab.
            "policy_set_name": result.policy_set_name,
        },
        "pipeline": [asdict(s) for s in result.stages],
        "services_used": result.services_used,
        "excluded_candidates": excluded_out,
        "metadata": {
            "provider": "openai",
            "model": result.model,
            "embedding_model": result.embedding_model,
            "temperature": result.temperature,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "latency_ms": round(result.latency_ms, 1),
            "estimated_cost_usd": round(result.estimated_cost_usd, 6),
            "confidence": round(result.confidence, 4),
            "confidence_label": result.confidence_label,
            "retrieved_chunks": len(result.chunks),
            # Phase 2 Part 19/20: confidence, answerability, and raw vector
            # score are DISTINCT numbers — never collapse them into one.
            "answerability": result.answerability,
            "raw_vector_similarity": result.raw_vector_similarity,
            "hybrid_retrieval_score": result.hybrid_retrieval_score,
            "embedding_provider": result.embedding_provider,
            "embedding_dimensions": result.embedding_dimensions,
            "embedding_version": result.embedding_version,
        },
    })


# ── RAG Benchmark & Evaluation ──────────────────────────────────────
# Evaluation infrastructure only — never touches embedding/retrieval/
# chunking/prompt behavior itself, only CALLS the existing pipeline
# (services.rag_service / services.prompt_builder / services.llm_service)
# the exact same way AI Playground does. See migrations/020_rag_benchmark.sql
# and services/benchmark_service.py.

@app.get("/admin/ai/benchmark", response_class=HTMLResponse)
async def benchmark_page(request: Request):
    if (r := auth(request)): return r
    if (r := require_developer_feature(request, "rag_benchmark")): return r
    return render("benchmark.html", {"request": request, "active": "rag_benchmark"})


@app.get("/admin/api/benchmark/datasets")
async def api_list_benchmark_datasets(request: Request):
    if (r := auth(request)): return r
    try:
        rows = get_sb().table("rag_benchmark_datasets").select("*").order("created_at", desc=True).execute().data or []
        for d in rows:
            cnt = get_sb().table("rag_benchmark_cases").select("id", count="exact").eq("dataset_id", d["id"]).execute()
            d["case_count"] = cnt.count or 0
        return JSONResponse({"ok": True, "datasets": rows})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/admin/api/benchmark/datasets")
async def api_create_benchmark_dataset(request: Request):
    if (r := auth(request)): return r
    body = await request.json()
    try:
        row = get_sb().table("rag_benchmark_datasets").insert({
            "name": body.get("name") or "Untitled Dataset", "description": body.get("description") or "",
        }).execute().data[0]
        return JSONResponse({"ok": True, "dataset": row})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/admin/api/benchmark/datasets/{dataset_id}/cases")
async def api_list_benchmark_cases(request: Request, dataset_id: str):
    if (r := auth(request)): return r
    try:
        rows = get_sb().table("rag_benchmark_cases").select("*").eq("dataset_id", dataset_id) \
            .order("created_at").execute().data or []
        return JSONResponse({"ok": True, "cases": rows})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


_BENCHMARK_CASE_FIELDS = (
    "question", "language", "expected_answer", "expected_file", "expected_section",
    "expected_answerability", "must_include", "must_not_include", "prohibited_files",
    "tags", "notes", "is_active",
    # Phase 2 (AI Evaluation) — Query Understanding / Conversation
    # Scenario / Critical Facts, additive columns from migrations/
    # 025_rag_benchmark_phase2.sql.
    "expected_topic", "expected_subtopic", "expected_intent", "expected_entities",
    "expected_excluded_entities", "expected_canonical_query", "expected_resolved_query",
    "expected_transition", "expected_conversation_state", "critical_facts",
    "scenario_key", "turn_index", "previous_messages",
)


@app.post("/admin/api/benchmark/datasets/{dataset_id}/cases")
async def api_create_benchmark_case(request: Request, dataset_id: str):
    if (r := auth(request)): return r
    body = await request.json()
    if not (body.get("question") or "").strip():
        return JSONResponse({"ok": False, "error": "question is required"}, status_code=400)
    payload = {k: body[k] for k in _BENCHMARK_CASE_FIELDS if k in body}
    payload["dataset_id"] = dataset_id
    try:
        row = get_sb().table("rag_benchmark_cases").insert(payload).execute().data[0]
        return JSONResponse({"ok": True, "case": row})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.patch("/admin/api/benchmark/cases/{case_id}")
async def api_update_benchmark_case(request: Request, case_id: str):
    if (r := auth(request)): return r
    body = await request.json()
    payload = {k: body[k] for k in _BENCHMARK_CASE_FIELDS if k in body}
    try:
        row = get_sb().table("rag_benchmark_cases").update(payload).eq("id", case_id).execute().data
        return JSONResponse({"ok": True, "case": row[0] if row else None})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.delete("/admin/api/benchmark/cases/{case_id}")
async def api_delete_benchmark_case(request: Request, case_id: str):
    if (r := auth(request)): return r
    try:
        # Historical benchmark_results reference case_id with ON DELETE
        # SET NULL (migration 020) — deleting a case never deletes past
        # run results, it just detaches them (their snapshot columns
        # still carry the original question/expected values).
        get_sb().table("rag_benchmark_cases").delete().eq("id", case_id).execute()
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/admin/api/benchmark/cases/{case_id}/duplicate")
async def api_duplicate_benchmark_case(request: Request, case_id: str):
    if (r := auth(request)): return r
    try:
        src = get_sb().table("rag_benchmark_cases").select("*").eq("id", case_id).execute().data
        if not src:
            return JSONResponse({"ok": False, "error": "Case not found"}, status_code=404)
        payload = {k: src[0].get(k) for k in _BENCHMARK_CASE_FIELDS}
        payload["dataset_id"] = src[0]["dataset_id"]
        payload["question"] = "(Copy) " + (payload.get("question") or "")
        row = get_sb().table("rag_benchmark_cases").insert(payload).execute().data[0]
        return JSONResponse({"ok": True, "case": row})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/admin/api/benchmark/datasets/{dataset_id}/import")
async def api_import_benchmark_cases(request: Request, dataset_id: str):
    """Bulk JSON import — body: {"cases": [{...}, ...]}."""
    if (r := auth(request)): return r
    body = await request.json()
    cases = body.get("cases") or []
    inserted = 0
    errors = []
    for c in cases:
        if not (c.get("question") or "").strip():
            errors.append("skipped a case with no question")
            continue
        payload = {k: c[k] for k in _BENCHMARK_CASE_FIELDS if k in c}
        payload["dataset_id"] = dataset_id
        try:
            get_sb().table("rag_benchmark_cases").insert(payload).execute()
            inserted += 1
        except Exception as e:
            errors.append(str(e))
    return JSONResponse({"ok": True, "inserted": inserted, "errors": errors})


@app.get("/admin/api/benchmark/datasets/{dataset_id}/export")
async def api_export_benchmark_cases(request: Request, dataset_id: str):
    if (r := auth(request)): return r
    rows = get_sb().table("rag_benchmark_cases").select("*").eq("dataset_id", dataset_id).execute().data or []
    return JSONResponse({"ok": True, "cases": rows})


@app.post("/admin/api/benchmark/estimate-cost")
async def api_estimate_benchmark_cost(request: Request):
    if (r := auth(request)): return r
    body = await request.json()
    from services.benchmark_service import estimate_full_rag_cost
    estimate = estimate_full_rag_cost(int(body.get("case_count") or 0),
                                       grader_enabled=bool(body.get("grader_enabled")))
    return JSONResponse({"ok": True, "estimate": estimate})


# Guards against duplicate-click double-runs — a dataset can only have
# one run "in flight" (queued/running) at a time.
_active_benchmark_runs_by_dataset: dict = {}


@app.post("/admin/api/benchmark/runs")
async def api_start_benchmark_run(request: Request):
    if (r := auth(request)): return r
    body = await request.json()
    dataset_id = body.get("dataset_id")
    mode = body.get("mode") or "RETRIEVAL_ONLY"
    from services.benchmark_service import RUN_MODES
    if mode not in RUN_MODES:
        return JSONResponse({"ok": False, "error": f"mode must be one of {', '.join(RUN_MODES)}"}, status_code=400)

    existing_run_id = _active_benchmark_runs_by_dataset.get(dataset_id)
    if existing_run_id:
        from services.benchmark_service import get_run_progress
        prog = get_run_progress(existing_run_id)
        if prog and prog.get("status") == "running":
            return JSONResponse({"ok": False, "error": "A run is already in progress for this dataset",
                                  "run_id": existing_run_id}, status_code=409)

    try:
        from services.benchmark_service import start_run
        run_id = start_run(get_sb(), dataset_id=dataset_id, run_name=body.get("run_name") or f"Run {_now_iso()}",
                            mode=mode, top_k=int(body.get("top_k") or 3),
                            resume_run_id=body.get("resume_run_id"))
        _active_benchmark_runs_by_dataset[dataset_id] = run_id
        return JSONResponse({"ok": True, "run_id": run_id})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.get("/admin/api/benchmark/runs")
async def api_list_benchmark_runs(request: Request):
    if (r := auth(request)): return r
    dataset_id = request.query_params.get("dataset_id")
    q = get_sb().table("rag_benchmark_runs").select("*")
    if dataset_id:
        q = q.eq("dataset_id", dataset_id)
    rows = q.order("created_at", desc=True).execute().data or []
    return JSONResponse({"ok": True, "runs": rows})


@app.get("/admin/api/benchmark/runs/{run_id}")
async def api_get_benchmark_run(request: Request, run_id: str):
    if (r := auth(request)): return r
    from services.benchmark_service import get_run_progress
    run_rows = get_sb().table("rag_benchmark_runs").select("*").eq("id", run_id).execute().data
    if not run_rows:
        return JSONResponse({"ok": False, "error": "Run not found"}, status_code=404)
    progress = get_run_progress(run_id)
    return JSONResponse({"ok": True, "run": run_rows[0], "progress": progress})


@app.post("/admin/api/benchmark/runs/{run_id}/cancel")
async def api_cancel_benchmark_run(request: Request, run_id: str):
    if (r := auth(request)): return r
    from services.benchmark_service import cancel_run
    ok = cancel_run(run_id)
    return JSONResponse({"ok": ok})


@app.delete("/admin/api/benchmark/runs/{run_id}")
async def api_delete_benchmark_run(request: Request, run_id: str):
    if (r := auth(request)): return r
    try:
        get_sb().table("rag_benchmark_runs").delete().eq("id", run_id).execute()
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/admin/api/benchmark/runs/{run_id}/results")
async def api_list_benchmark_results(request: Request, run_id: str):
    if (r := auth(request)): return r
    status_filter = request.query_params.get("status")
    failure_type = request.query_params.get("failure_type")
    q = get_sb().table("rag_benchmark_results").select("*").eq("run_id", run_id)
    if status_filter:
        q = q.eq("status", status_filter)
    if failure_type:
        q = q.eq("failure_type", failure_type)
    rows = q.order("created_at").execute().data or []
    return JSONResponse({"ok": True, "results": rows})


@app.get("/admin/api/benchmark/results/{result_id}")
async def api_get_benchmark_result(request: Request, result_id: str):
    if (r := auth(request)): return r
    rows = get_sb().table("rag_benchmark_results").select("*").eq("id", result_id).execute().data
    if not rows:
        return JSONResponse({"ok": False, "error": "Result not found"}, status_code=404)
    return JSONResponse({"ok": True, "result": rows[0]})


@app.post("/admin/api/benchmark/runs/{run_id}/rerun-failed")
async def api_rerun_failed_cases(request: Request, run_id: str):
    """Reruns only the cases that failed/errored in this run, as a NEW
    run (never mutates the original historical run)."""
    if (r := auth(request)): return r
    run_rows = get_sb().table("rag_benchmark_runs").select("*").eq("id", run_id).execute().data
    if not run_rows:
        return JSONResponse({"ok": False, "error": "Run not found"}, status_code=404)
    run = run_rows[0]
    failed_results = get_sb().table("rag_benchmark_results").select("case_id").eq("run_id", run_id) \
        .neq("status", "pass").execute().data or []
    failed_case_ids = [r["case_id"] for r in failed_results if r.get("case_id")]
    if not failed_case_ids:
        return JSONResponse({"ok": True, "message": "No failed cases to rerun"})

    from services.benchmark_service import execute_run
    cases = []
    for cid in failed_case_ids:
        cr = get_sb().table("rag_benchmark_cases").select("*").eq("id", cid).execute().data
        if cr:
            cases.append(cr[0])
    new_run = get_sb().table("rag_benchmark_runs").insert({
        "dataset_id": run["dataset_id"], "run_name": f"{run['run_name']} (rerun failed)",
        "mode": run["mode"], "status": "queued", "final_top_k": run.get("final_top_k") or 3,
        "config_snapshot": {"rerun_of": run_id},
    }).execute().data[0]
    threading.Thread(target=lambda: execute_run(get_sb(), new_run["id"], cases, run["mode"],
                                                 top_k=run.get("final_top_k") or 3), daemon=True).start()
    return JSONResponse({"ok": True, "run_id": new_run["id"]})


@app.get("/admin/api/benchmark/compare")
async def api_compare_benchmark_runs(request: Request):
    if (r := auth(request)): return r
    run_a = request.query_params.get("run_a")
    run_b = request.query_params.get("run_b")
    if not run_a or not run_b:
        return JSONResponse({"ok": False, "error": "run_a and run_b are required"}, status_code=400)
    try:
        from services.benchmark_service import compare_runs
        diff = compare_runs(get_sb(), run_a, run_b)
        return JSONResponse({"ok": True, "diff": diff})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ── AI Production Validation Center ─────────────────────────────────
# Pure ORCHESTRATION over the existing AI Evaluation framework
# (services/benchmark_service.py) — sequences the 4 existing benchmark
# modes + compare_runs() against the frozen Production Baseline v1.0.
# Never a second benchmark implementation; see services/validation_service.py.

@app.get("/admin/ai/validation", response_class=HTMLResponse)
async def validation_page(request: Request):
    if (r := auth(request)): return r
    if (r := require_developer_feature(request, "ai_validation")): return r
    return render("validation.html", {"request": request, "active": "ai_validation"})


@app.post("/admin/api/validation/run")
async def api_start_validation(request: Request):
    """Part 10's automation endpoint — executes the full 7-step
    Production Validation. Body: {"dataset_id": optional, "top_k": optional}.
    Falls back to the Production Baseline's own dataset when dataset_id
    is omitted, so a CI/CD caller needs zero prior knowledge of dataset IDs."""
    if (r := auth(request)): return r
    body = await request.json() if await request.body() else {}
    from services.validation_service import start_validation, load_baseline
    dataset_id = body.get("dataset_id")
    if not dataset_id:
        baseline = load_baseline()
        dataset_id = (baseline or {}).get("dataset", {}).get("id")
    if not dataset_id:
        return JSONResponse({"ok": False, "error": "No dataset_id given and no Production Baseline dataset found"},
                             status_code=400)
    try:
        validation_id = start_validation(get_sb(), dataset_id=dataset_id, top_k=int(body.get("top_k") or 3))
        return JSONResponse({"ok": True, "validation_id": validation_id})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.get("/admin/api/validation/status/{validation_id}")
async def api_validation_status(request: Request, validation_id: str):
    if (r := auth(request)): return r
    from services.validation_service import get_validation_progress
    progress = get_validation_progress(validation_id)
    if not progress:
        return JSONResponse({"ok": False, "error": "Validation not found"}, status_code=404)
    # Report bodies can be large — omit them from the polling response;
    # the dedicated /report endpoint below serves them on demand.
    slim = {k: v for k, v in progress.items() if k not in ("report_markdown", "report_json")}
    return JSONResponse({"ok": True, "progress": slim})


@app.post("/admin/api/validation/cancel/{validation_id}")
async def api_cancel_validation(request: Request, validation_id: str):
    if (r := auth(request)): return r
    from services.validation_service import cancel_validation
    return JSONResponse({"ok": cancel_validation(validation_id)})


@app.get("/admin/api/validation/report/{validation_id}")
async def api_validation_report(request: Request, validation_id: str):
    """Part 9's export — ?format=markdown (default) | json | html."""
    if (r := auth(request)): return r
    from services.validation_service import get_validation_progress
    progress = get_validation_progress(validation_id)
    if not progress or progress.get("status") != "completed":
        return JSONResponse({"ok": False, "error": "Validation not completed yet"}, status_code=404)
    fmt = request.query_params.get("format", "markdown")
    if fmt == "json":
        return JSONResponse({"ok": True, "report": progress["report_json"]})
    if fmt == "html":
        import html as _html
        body = "<pre style='white-space:pre-wrap;font-family:monospace'>" + _html.escape(progress["report_markdown"]) + "</pre>"
        return HTMLResponse("<html><head><title>AI Production Validation Report</title></head><body>" + body + "</body></html>")
    return PlainTextResponse(progress["report_markdown"], media_type="text/markdown")


# ── Business Action Center ──────────────────────────────────────────
# Platform-first registry of configurable Business Actions (RAG / API /
# Tool / Workflow / Notification / Human Handoff / Webhook). Pure CRUD +
# lookup surface over services/business_action_registry.py — NO
# execution routing here (a future Decision Engine reads this registry;
# this task only builds the registry itself). See migrations/
# 026_business_action_center.sql.

@app.get("/admin/ai/business-actions", response_class=HTMLResponse)
async def business_actions_page(request: Request):
    if (r := auth(request)): return r
    if (r := require_developer_feature(request, "business_action_center")): return r
    return render("business_actions.html", {"request": request, "active": "business_action_center"})


# ── ERP/AI Product Architecture Separation ───────────────────────────────
# ERP Conversation Tester, ERP Test History, and the API Explorer all
# belong to "Integrations" (ERP testing/verification), never to "AI"
# (AI Playground stays a pure AI/RAG testing environment — see the
# removed erp_test tab in preview.html). None of these three pages add
# new execution behavior — they reuse services/erp_test_harness.py and
# the existing Business Action Registry/Executor exactly as before,
# just relocated + reorganized with business-facing terminology.

@app.get("/admin/erp/conversation-tester", response_class=HTMLResponse)
async def erp_conversation_tester_page(request: Request):
    if (r := auth(request)): return r
    if (r := require_developer_feature(request, "erp_conversation_tester")): return r
    return render("erp_conversation_tester.html", {"request": request, "active": "erp_conversation_tester"})


@app.get("/admin/erp/test-history", response_class=HTMLResponse)
async def erp_test_history_page(request: Request):
    """Not a separate sidebar item (per the sprint's exact required
    sidebar shape) — reachable from ERP Conversation Tester / ERP
    Integration. Guarded by the same feature key as the Conversation
    Tester since it shows the same kind of data."""
    if (r := auth(request)): return r
    if (r := require_developer_feature(request, "erp_conversation_tester")): return r
    return render("erp_test_history.html", {"request": request, "active": "erp_conversation_tester"})


@app.get("/admin/qa/uat", response_class=HTMLResponse)
async def uat_dashboard_page(request: Request):
    """UAT / Regression Test Suite Developer Page (2026-07-29 reliability
    sprint). Reuses services/uat_runner.py's run_full_uat_suite() — this
    page does not add any new test/execution logic itself, only a
    "Run Full UAT" button, live progress, and the finished report.
    Not a sidebar item (this is a developer/QA tool, not a customer-
    facing feature) — reached directly via URL, same convention as the
    API Explorer/Integration Schema Studio pages above."""
    if (r := auth(request)): return r
    return render("uat_dashboard.html", {"request": request, "active": "uat_dashboard"})


@app.post("/admin/api/uat/run")
async def uat_run(request: Request):
    """Runs the full UAT suite SYNCHRONOUSLY (real LLM calls, so this can
    take a while — the page shows a loading state instead of polling a
    background job, an intentionally low-risk choice per the sprint's
    own scope-discipline note). Returns the full report dict produced by
    services/uat_runner.py::run_full_uat_suite() — nothing here
    re-derives pass/fail, it's a direct pass-through of the runner's own
    honest results."""
    if (r := auth(request)): return r
    from services.uat_runner import run_full_uat_suite
    try:
        report = run_full_uat_suite(sb=get_sb())
        return JSONResponse({"ok": True, "report": report})
    except Exception as e:
        import traceback
        return JSONResponse({"ok": False, "error": str(e), "traceback": traceback.format_exc()}, status_code=500)


@app.get("/admin/api/uat/report/{run_id}")
async def uat_report(request: Request, run_id: str):
    """Fetches a previously-saved run's report from uat_runs/<run_id>.json
    (local disk — no DB table, per the sprint's standing constraint)."""
    if (r := auth(request)): return r
    import os
    from services.uat_runner import RUNS_DIR
    safe_id = "".join(c for c in run_id if c.isalnum() or c in "_-")
    path = os.path.join(RUNS_DIR, f"run_{safe_id}.json")
    if not os.path.isfile(path):
        return JSONResponse({"ok": False, "error": "run not found"}, status_code=404)
    import json
    with open(path, "r", encoding="utf-8") as f:
        return JSONResponse({"ok": True, "report": json.load(f)})


@app.get("/admin/api/uat/runs")
async def uat_runs_list(request: Request):
    """Lists saved run ids (newest first) so the Developer Page can offer
    a history dropdown without needing its own DB table."""
    if (r := auth(request)): return r
    import os
    from services.uat_runner import RUNS_DIR
    if not os.path.isdir(RUNS_DIR):
        return JSONResponse({"ok": True, "runs": []})
    files = sorted([f for f in os.listdir(RUNS_DIR) if f.startswith("run_") and f.endswith(".json")], reverse=True)
    run_ids = [f[len("run_"):-len(".json")] for f in files]
    return JSONResponse({"ok": True, "runs": run_ids})


@app.get("/admin/erp/api-explorer", response_class=HTMLResponse)
async def erp_api_explorer_page(request: Request):
    """Not a separate sidebar item — reachable from ERP Integration.
    Pure API verification (endpoint/method/headers/params/body/execute/
    raw response), no AI involved — the direct successor to the manual
    'Business Actions' tester tab removed from AI Playground."""
    if (r := auth(request)): return r
    if (r := require_developer_feature(request, "business_action_center")): return r
    return render("erp_api_explorer.html", {"request": request, "active": "business_action_center"})


# ── Integration Schema Studio ──────────────────────────────────────────
# Business/conversation-layer configuration (labels, aliases, display
# labels per language, follow-up prompts, AI synonyms, formatting,
# visibility/security, conversation & operation-type behavior) that sits
# ON TOP OF a Business Action, in a completely separate, versioned store
# (services/integration_schema_service.py, migrations/
# 033_integration_action_schemas.sql). Never touches business_actions*
# tables. Not a new sidebar item — reachable from ERP Integration /
# Business Action Center, same convention as the API Explorer above.

@app.get("/admin/ai/business-actions/schema/{action_id}", response_class=HTMLResponse)
async def integration_schema_studio_page(request: Request, action_id: str):
    if (r := auth(request)): return r
    if (r := require_developer_feature(request, "business_action_center")): return r
    return render("integration_schema_studio.html", {"request": request, "active": "business_action_center",
                                                       "action_id": action_id})


@app.get("/admin/api/integration-schema/{action_id}/versions")
async def api_integration_schema_list_versions(request: Request, action_id: str):
    if (r := auth(request)): return r
    from services.integration_schema_service import get_registry
    return get_registry(get_sb()).list_versions(action_id)


@app.get("/admin/api/integration-schema/{action_id}/draft")
async def api_integration_schema_get_draft(request: Request, action_id: str):
    if (r := auth(request)): return r
    from services.integration_schema_service import get_registry
    from services.business_action_registry import get_registry as get_action_registry
    action = get_action_registry(get_sb()).get_full(action_id, mask_secrets=True)
    if not action:
        return JSONResponse({"error": "action_not_found"}, status_code=404)
    return get_registry(get_sb()).get_draft(action_id, action=action)


@app.get("/admin/api/integration-schema/{action_id}/published")
async def api_integration_schema_get_published(request: Request, action_id: str):
    if (r := auth(request)): return r
    from services.integration_schema_service import get_registry, resolve_effective_schema
    from services.business_action_registry import get_registry as get_action_registry
    action = get_action_registry(get_sb()).get_full(action_id, mask_secrets=True)
    if not action:
        return JSONResponse({"error": "action_not_found"}, status_code=404)
    return {"published": get_registry(get_sb()).get_published(action_id),
            "effective": resolve_effective_schema(action, get_sb())}


@app.get("/admin/api/integration-schema/{action_id}/effective")
async def api_integration_schema_get_effective(request: Request, action_id: str):
    """Part 5-6 — the DRAFT's effective schema + provenance + warnings,
    so the Studio UI can show a live "Runtime Default / Derived / Explicit
    Override" badge next to general.operation_type and every
    conversation.* field while the admin is still editing (not yet
    published). Reads the current draft (never the published version) so
    unsaved edits are reflected immediately."""
    if (r := auth(request)): return r
    from services.integration_schema_service import get_registry, resolve_effective_integration_schema
    from services.business_action_registry import get_registry as get_action_registry
    action = get_action_registry(get_sb()).get_full(action_id, mask_secrets=True)
    if not action:
        return JSONResponse({"error": "action_not_found"}, status_code=404)
    draft = get_registry(get_sb()).get_draft(action_id, action=action)
    return resolve_effective_integration_schema(action, draft.get("schema") or {})


@app.put("/admin/api/integration-schema/{action_id}/draft")
async def api_integration_schema_save_draft(request: Request, action_id: str):
    if (r := auth(request)): return r
    body = await request.json()
    from services.integration_schema_service import get_registry
    admin_user = request.session.get("admin_user") if hasattr(request, "session") else None
    row = get_registry(get_sb()).save_draft(action_id, body.get("schema") or {}, updated_by=admin_user)
    return row


@app.post("/admin/api/integration-schema/{action_id}/publish")
async def api_integration_schema_publish(request: Request, action_id: str):
    if (r := auth(request)): return r
    from services.integration_schema_service import get_registry
    admin_user = request.session.get("admin_user") if hasattr(request, "session") else None
    row = get_registry(get_sb()).publish(action_id, updated_by=admin_user)
    if not row:
        return JSONResponse({"error": "no_draft_to_publish"}, status_code=400)
    return row


@app.post("/admin/api/integration-schema/{action_id}/rollback/{version_number}")
async def api_integration_schema_rollback(request: Request, action_id: str, version_number: int):
    if (r := auth(request)): return r
    from services.integration_schema_service import get_registry
    admin_user = request.session.get("admin_user") if hasattr(request, "session") else None
    row = get_registry(get_sb()).rollback(action_id, version_number, updated_by=admin_user)
    if not row:
        return JSONResponse({"error": "version_not_found"}, status_code=404)
    return row


@app.get("/admin/api/integration-schema/{action_id}/diff")
async def api_integration_schema_diff(request: Request, action_id: str):
    if (r := auth(request)): return r
    q = request.query_params
    from services.integration_schema_service import get_registry
    try:
        v1, v2 = int(q.get("v1")), int(q.get("v2"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "v1_and_v2_required"}, status_code=400)
    return get_registry(get_sb()).diff_versions(action_id, v1, v2)


@app.get("/admin/api/business-actions")
async def api_list_business_actions(request: Request):
    if (r := auth(request)): return r
    from services.business_action_registry import get_registry
    q = request.query_params.get("q")
    category = request.query_params.get("category")
    action_type = request.query_params.get("action_type")
    reg = get_registry(get_sb())
    try:
        actions = reg.search(q) if q else reg.list_with_summary()
        if q:
            summary_by_id = {a["id"]: a for a in reg.list_with_summary()}
            for a in actions:
                a.update({k: v for k, v in summary_by_id.get(a["id"], {}).items() if k in ("required_parameter_summary", "execution_host")})
        if category:
            actions = [a for a in actions if a.get("category") == category]
        if action_type:
            actions = [a for a in actions if a.get("action_type") == action_type]
        return JSONResponse({"ok": True, "actions": actions})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


_DUPLICATE_KEY_MARKERS = ("duplicate key value violates unique constraint", "already exists", "23505")


def _is_duplicate_key_error(exc: Exception) -> bool:
    return any(marker in str(exc) for marker in _DUPLICATE_KEY_MARKERS)


def _classify_duplicate_state(existing_row: Optional[Dict]) -> str:
    """The SAME collision can mean three very different things — a live
    Published action in production use, an in-progress Draft nobody's
    finished configuring yet, or a stale Soft-Deleted row whose key is
    still reserved at the DB level (see the 2026-07-29 getdatacustomer
    incident). Each deserves its own resolution options, never one
    generic dialog (Task 3)."""
    if not existing_row:
        return "none"
    if existing_row.get("deleted_at"):
        return "soft_deleted"
    if existing_row.get("is_draft"):
        return "draft"
    return "published"


# Per-state duplicate-resolution option sets (Task 3) — the client uses
# these to render the right buttons; never the old generic 4-option set
# for every situation.
_DUPLICATE_OPTIONS_BY_STATE = {
    "published": ["update_existing", "save_as_new", "rename_action"],
    "draft": ["continue_editing_draft", "discard_draft", "rename_action"],
    "soft_deleted": ["restore", "delete_permanently", "create_new"],
    "none": ["update_existing", "save_as_new", "rename_action", "cancel"],
}

_DUPLICATE_MESSAGE_BY_STATE = {
    "published": ("A published Business Action with the key '{key}' already exists and may be in "
                  "active use. Update it, save this as a new action under a different key, or rename this one."),
    "draft": ("A DRAFT Business Action with the key '{key}' already exists and hasn't been published yet. "
              "Continue editing that draft, discard it, or use a different key."),
    "soft_deleted": ("A Business Action with the key '{key}' already exists but was previously deleted — "
                     "the key is still reserved at the database level. Restore it, delete it permanently, "
                     "or create a new action under this key."),
    "none": "Business Action already exists.",
}


def _duplicate_action_response(action_key: str, existing_row: Optional[Dict] = None):
    """Never expose the raw PostgreSQL unique-constraint error to the
    admin — surface a friendly, actionable choice instead. The offered
    options and message differ by the EXISTING row's actual state
    (Task 3) — Published / Draft / Soft-Deleted are different situations
    calling for different resolutions, not one generic dialog."""
    state = _classify_duplicate_state(existing_row)
    existing_id = existing_row.get("id") if existing_row else None
    message = _DUPLICATE_MESSAGE_BY_STATE[state].format(key=action_key)
    return JSONResponse({
        "ok": False, "success": False, "error_type": "duplicate_action",
        "duplicate_state": state,
        "error": message, "message": message,
        "action_key": action_key, "existing_action_id": existing_id,
        "options": _DUPLICATE_OPTIONS_BY_STATE[state],
    }, status_code=409)


@app.post("/admin/api/business-actions")
async def api_create_business_action(request: Request):
    if (r := auth(request)): return r
    body = await request.json()
    action_key = (body.get("action_key") or "").strip()
    if not action_key or not (body.get("name") or "").strip():
        return JSONResponse({"ok": False, "error": "action_key and name are required"}, status_code=400)
    from services.business_action_registry import get_registry
    reg = get_registry(get_sb())
    # Look PAST soft-deleted rows too (Task 4 / the getdatacustomer
    # incident) — get_by_key() alone misses a key that belongs to a
    # soft-deleted action, letting the call fall through to reg.create()
    # and hit the raw Postgres unique-constraint error instead of this
    # friendly, state-aware response.
    existing = reg.get_by_key_including_deleted(action_key)
    if existing:
        return _duplicate_action_response(action_key, existing)
    try:
        action = reg.create(body, created_by=_current_admin_user(request))
        return JSONResponse({"ok": True, "action": action})
    except Exception as e:
        if _is_duplicate_key_error(e):
            return _duplicate_action_response(action_key, reg.get_by_key_including_deleted(action_key))
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.get("/admin/api/business-actions/list-for-playground")
async def api_list_business_actions_for_playground(request: Request):
    """Business Action Framework + ERP Sync milestone, Phase 6 — a
    lightweight listing (id/key/name/category/action_type/enabled/
    parameters) for the Playground's manual Business Action tester.
    Read-only, never routed through the Decision Engine (paused per this
    milestone) — the admin picks and runs an action directly.

    Registered BEFORE the bare "/{action_id}" route below — FastAPI/
    Starlette matches path routes in registration order, and a bare
    `{action_id}: str` path parameter would otherwise greedily match the
    literal segment "list-for-playground" as if it were an action id."""
    if (r := auth(request)): return r
    from services.business_action_registry import get_registry
    reg = get_registry(get_sb())
    actions = reg.list()
    out = []
    for a in actions:
        full = reg.get_full(a["id"], mask_secrets=True)
        full_params = (full.get("parameters") or []) if full else []
        out.append({
            "id": a["id"], "action_key": a.get("action_key"), "name": a.get("name"),
            "display_name": a.get("display_name"), "category": a.get("category"),
            "action_type": a.get("action_type"), "enabled": a.get("enabled"),
            "parameters": [{"name": p.get("name"), "display_name": p.get("display_name"),
                            "required": p.get("required"), "example_value": p.get("example_value"),
                            "input_source": p.get("input_source")}
                           for p in full_params],
        })
    return JSONResponse({"ok": True, "actions": out})


@app.get("/admin/api/business-actions/{action_id}")
async def api_get_business_action(request: Request, action_id: str):
    if (r := auth(request)): return r
    from services.business_action_registry import get_registry
    action = get_registry(get_sb()).get_full(action_id)
    if not action:
        return JSONResponse({"ok": False, "error": "Action not found"}, status_code=404)
    return JSONResponse({"ok": True, "action": action})


# ── ERP Action Test Harness ─────────────────────────────────────────────
# Validates that an imported Business Action can execute and answer a
# customer question correctly, BEFORE the (not-yet-built) Universal
# Decision Engine ever routes real traffic to it. Manual Business Action
# selection only — no automatic RAG-vs-ERP routing here. Entirely
# additive: reuses services/erp_test_harness.py, which itself only
# orchestrates the existing, unmodified Action Executor / Business
# Action Registry / Credential Store / Decision Engine parameter binder.
# Registered BEFORE any bare "/{action_id}"-style route below for the
# same reason as list-for-playground above — not applicable here since
# this uses its own "/admin/api/erp-test/..." prefix, but kept alongside
# its closest relative for readability.

@app.get("/admin/api/erp-test/actions")
async def api_erp_test_list_actions(request: Request):
    """Lists every Business Action with the full selection-time metadata
    Part 2 of the ERP Action Test Harness spec requires (capability,
    endpoint, method, auth status, required search fields, response
    mappings, semantic intent, entities). Includes disabled/draft
    actions too (shown, not hidden) so an admin can still test an
    action before enabling it — the harness itself blocks execution of
    a truly broken one via its own trace, not by hiding it here."""
    if (r := auth(request)): return r
    from services.business_action_registry import get_registry
    from services.erp_test_harness import describe_action_for_selection
    reg = get_registry(get_sb())
    out = []
    for a in reg.list():
        if a.get("action_type") not in ("API", "WEBHOOK", "TOOL"):
            continue  # ERP-style actions only — RAG/notification/human-handoff/workflow actions aren't executable via this harness
        full = reg.get_full(a["id"], mask_secrets=True)
        if not full:
            continue
        # Part 15 — sourced via the Integration Contract when possible
        # (services/integration_contract_service.py::describe_integration_
        # cached), never a second manual join of action+response_mapping.
        out.append(describe_action_for_selection(full, sb=get_sb()))
    return JSONResponse({"ok": True, "actions": out})


@app.post("/admin/api/erp-test/run")
async def api_erp_test_run(request: Request):
    """Runs one turn of the ERP Action Test flow (Parts 3-12): parameter
    extraction, missing-parameter clarification, and — depending on
    `mode` — either nothing further (intent_param), a schema-generated
    mock (simulation), or a REAL call via the existing Action Executor
    (live). The caller (browser) owns conversation state across turns —
    `history`/`collected_params` are resent every call, never persisted
    server-side, matching this codebase's existing multi-turn convention."""
    if (r := auth(request)): return r
    body = await request.json()
    action_id = (body.get("action_id") or "").strip()
    message = (body.get("message") or "").strip()
    mode = body.get("mode") or "intent_param"
    history = body.get("history") or []
    collected_params = body.get("collected_params") or {}
    language = body.get("language") or "th"
    if not message and mode != "live":
        return JSONResponse({"ok": False, "error": "A message is required."}, status_code=400)
    # Requested Information Clarification sprint (Part 3) — these three
    # fields round-trip through the browser exactly like history/
    # collected_params already did; never persisted server-side.
    awaiting_information_selection = bool(body.get("awaiting_information_selection"))
    available_response_options = body.get("available_response_options") or None
    last_normalized_result = body.get("last_normalized_result") or None
    # Conversation Form Generator sprint (Part 7/10/19) — additive,
    # caller-round-tripped exactly like history/collected_params above;
    # never persisted server-side, never required.
    conversation_form_state = body.get("conversation_state") or None
    confirmed = bool(body.get("confirmed"))
    enforce_confirmation_gate = bool(body.get("enforce_confirmation_gate"))
    from services.erp_test_harness import run_erp_test
    result = run_erp_test(sb=get_sb(), action_id=action_id, message=message, mode=mode,
                           history=history, collected_params=collected_params, language=language,
                           awaiting_information_selection=awaiting_information_selection,
                           available_response_options=available_response_options,
                           last_normalized_result=last_normalized_result,
                           conversation_form_state=conversation_form_state,
                           confirmed=confirmed, enforce_confirmation_gate=enforce_confirmation_gate)
    return JSONResponse(result)


@app.post("/admin/api/erp-test/save-case")
async def api_erp_test_save_case(request: Request):
    """Part 13 — persists a saved ERP Test Case (erp_test_cases table,
    migrations/031_erp_test_cases.sql). Deliberately NOT the full
    Benchmark dashboard — a lightweight, per-action regression record
    only, so a future re-run can compare against what actually happened
    last time."""
    if (r := auth(request)): return r
    body = await request.json()
    action_id = body.get("action_id")
    if not action_id:
        return JSONResponse({"ok": False, "error": "action_id is required."}, status_code=400)
    row = {
        "action_id": action_id,
        "question": body.get("question") or "",
        "conversation_history": body.get("conversation_history") or [],
        "input_parameters": body.get("input_parameters") or {},
        "expected_response_fields": body.get("expected_response_fields") or {},
        "test_mode": body.get("test_mode") or "simulation",
        "actual_erp_status": body.get("actual_erp_status"),
        "actual_normalized_result": body.get("actual_normalized_result") or {},
        "actual_answer": body.get("actual_answer"),
        "pass_fail_status": body.get("pass_fail_status") or "warning",
        "total_latency_ms": body.get("total_latency_ms"),
        "notes": body.get("notes"),
        "created_by": _current_admin_user(request),
    }
    try:
        created = get_sb().table("erp_test_cases").insert(row).execute().data[0]
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"Failed to save test case: {str(e)}"}, status_code=400)
    return JSONResponse({"ok": True, "test_case": created})


@app.get("/admin/api/erp-test/cases")
async def api_erp_test_list_cases(request: Request):
    """Lists saved ERP Test Cases, optionally filtered to one Business
    Action (?action_id=...)."""
    if (r := auth(request)): return r
    action_id = request.query_params.get("action_id")
    q = get_sb().table("erp_test_cases").select("*").order("created_at", desc=True)
    if action_id:
        q = q.eq("action_id", action_id)
    rows = q.execute().data or []
    return JSONResponse({"ok": True, "test_cases": rows})


@app.get("/admin/api/integration-contracts/capabilities")
async def api_integration_contracts_capabilities(request: Request):
    if (r := auth(request)): return r
    from services.integration_contract_service import list_capabilities
    return JSONResponse({"ok": True, "capabilities": list_capabilities(sb=get_sb())})


@app.get("/admin/api/integration-contracts/entities")
async def api_integration_contracts_entities(request: Request):
    if (r := auth(request)): return r
    from services.integration_contract_service import list_entities
    return JSONResponse({"ok": True, "entities": list_entities(sb=get_sb())})


@app.get("/admin/api/integration-contracts/operations")
async def api_integration_contracts_operations(request: Request):
    if (r := auth(request)): return r
    from services.integration_contract_service import list_operation_types
    return JSONResponse({"ok": True, "operation_types": list_operation_types()})


@app.get("/admin/api/integration-contracts/providers")
async def api_integration_contracts_providers(request: Request):
    if (r := auth(request)): return r
    from services.integration_contract_service import list_providers
    return JSONResponse({"ok": True, "providers": list_providers(sb=get_sb())})


@app.get("/admin/api/integration-contracts")
async def api_integration_contracts_list(request: Request):
    """Part 11 — lists every Integration Contract, supporting the same
    ?action_type=/?enabled=/?entity=/?operation_type= filters
    services/integration_contract_service.py::list_integrations()
    understands. Registered BEFORE the bare "/{action_id}" route below
    so it is never shadowed by it."""
    if (r := auth(request)): return r
    from services.integration_contract_service import list_integrations
    filters = {}
    if request.query_params.get("action_type"):
        filters["action_type"] = request.query_params["action_type"]
    if request.query_params.get("enabled") is not None:
        filters["enabled"] = request.query_params["enabled"].lower() == "true"
    if request.query_params.get("entity"):
        filters["entity"] = request.query_params["entity"]
    if request.query_params.get("operation_type"):
        filters["operation_type"] = request.query_params["operation_type"]
    return JSONResponse({"ok": True, "contracts": list_integrations(filters, sb=get_sb())})


@app.get("/admin/api/integration-contracts/{action_id}")
async def api_integration_contract_get(request: Request, action_id: str):
    if (r := auth(request)): return r
    from services.integration_contract_service import resolve_effective_contract
    result = resolve_effective_contract(action_id, sb=get_sb())
    if result["contract"] is None:
        return JSONResponse({"ok": False, "error": "action_not_found"}, status_code=404)
    return JSONResponse({"ok": True, **result})


@app.get("/admin/api/integration-contracts/{action_id}/validate")
async def api_integration_contract_validate(request: Request, action_id: str):
    if (r := auth(request)): return r
    from services.integration_contract_service import validate_integration_contract
    return JSONResponse({"ok": True, "validation": validate_integration_contract(action_id, sb=get_sb())})


@app.get("/admin/api/integration-contracts/{action_id}/conversation-form")
async def api_integration_contract_conversation_form(request: Request, action_id: str):
    """Conversation Form Generator sprint (Parts 9/15) — preview-only.
    Builds the Conversation Form for the action's CURRENT DRAFT schema
    (never the published one — an admin previews changes before
    publishing), calling the SAME generator the runtime
    (services/erp_test_harness.py::run_erp_test) uses; this route never
    re-implements question/option logic itself. Optional query params
    `?state=<json>` (a previously-returned conversation_state, to
    preview step 2/3/... of the flow) and `?language=th|en`. Never
    executes anything — read-only."""
    if (r := auth(request)): return r
    from services.business_action_registry import get_registry as get_action_registry
    from services.integration_schema_service import get_registry as get_schema_registry, \
        resolve_effective_integration_schema
    from services.integration_contract_service import describe_integration
    from services.conversation_form_generator import ConversationState, generate_conversation_form
    import json as _json

    action = get_action_registry(get_sb()).get_full(action_id, mask_secrets=True)
    if not action:
        return JSONResponse({"ok": False, "error": "action_not_found"}, status_code=404)
    draft = get_schema_registry(get_sb()).get_draft(action_id, action=action)
    draft_schema = draft.get("schema") or {}

    contract = describe_integration(action_id, sb=get_sb(), schema_override=draft_schema)
    resolution = resolve_effective_integration_schema(action, draft_schema)

    language = request.query_params.get("language") or "th"
    raw_state = request.query_params.get("state")
    try:
        state_dict = _json.loads(raw_state) if raw_state else None
    except (TypeError, ValueError):
        state_dict = None
    state = ConversationState.from_dict(state_dict)

    form = generate_conversation_form(contract, state, language=language) if contract else None
    return JSONResponse({"ok": True, "contract": contract, "conversation_form": form,
                          "provenance": resolution.get("provenance") or {}, "warnings": resolution.get("warnings") or []})


@app.get("/admin/api/integration-contracts/{action_id}/conversation-strategy")
async def api_integration_contract_conversation_strategy(request: Request, action_id: str):
    """Part 12 — Conversation Strategy Preview (Schema Studio). Same
    CURRENT-DRAFT-schema pattern as the /conversation-form route above
    (schema_override, never the published schema) plus the Conversation
    Strategy Engine. Preview-only, read-only — never executes anything.
    Query params: `?message=...` (required to get a non-trivial result),
    `?language=th|en`, optional `?state=<json>` (a previously-returned
    conversation_state, same convention as /conversation-form)."""
    if (r := auth(request)): return r
    from services.business_action_registry import get_registry as get_action_registry
    from services.integration_schema_service import get_registry as get_schema_registry
    from services.integration_contract_service import describe_integration
    from services.conversation_form_generator import ConversationState
    from services.conversation_strategy_engine import analyze_conversation_strategy
    import json as _json

    action = get_action_registry(get_sb()).get_full(action_id, mask_secrets=True)
    if not action:
        return JSONResponse({"ok": False, "error": "action_not_found"}, status_code=404)
    draft = get_schema_registry(get_sb()).get_draft(action_id, action=action)
    draft_schema = draft.get("schema") or {}
    contract = describe_integration(action_id, sb=get_sb(), schema_override=draft_schema)
    if not contract:
        return JSONResponse({"ok": False, "error": "contract_unavailable"}, status_code=404)

    message = request.query_params.get("message") or ""
    language = request.query_params.get("language") or "th"
    raw_state = request.query_params.get("state")
    try:
        state_dict = _json.loads(raw_state) if raw_state else None
    except (TypeError, ValueError):
        state_dict = None
    state = ConversationState.from_dict(state_dict)

    strategy = analyze_conversation_strategy(contract, message, state, language=language)
    return JSONResponse({"ok": True, "message": message, "strategy": strategy})


@app.get("/admin/analytics/conversation-strategy")
async def conversation_analytics_dashboard_page(request: Request):
    """Part 13 — Conversation Analytics Dashboard page (read-only,
    in-memory data only, via get_analytics_store())."""
    if (r := auth(request)): return r
    return render("conversation_analytics_dashboard.html", {"request": request, "active": "business_action_center"})


@app.get("/admin/api/conversation-analytics/metrics")
async def api_conversation_analytics_metrics(request: Request):
    """Part 13 — data route backing the dashboard. Reads ONLY through
    get_analytics_store() — never a bare in-memory dict from route code."""
    if (r := auth(request)): return r
    from services.conversation_analytics_store import get_analytics_store
    action_id = request.query_params.get("action_id") or None
    store = get_analytics_store()
    metrics = store.compute_metrics(action_id=action_id)
    recent = store.list_conversations(action_id=action_id, limit=20)
    return JSONResponse({"ok": True, "metrics": metrics, "recent_conversations": recent})


@app.patch("/admin/api/business-actions/{action_id}")
async def api_update_business_action(request: Request, action_id: str):
    if (r := auth(request)): return r
    body = await request.json()
    from services.business_action_registry import get_registry
    try:
        action = get_registry(get_sb()).update(action_id, body, updated_by=_current_admin_user(request))
        return JSONResponse({"ok": True, "action": action})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.get("/admin/api/business-actions/{action_id}/delete-info")
async def api_business_action_delete_info(request: Request, action_id: str):
    """Lets the confirmation dialog pick the right tier of warning BEFORE
    the admin commits to a permanent delete (2026-07-29 hard-delete
    rework) — a never-executed draft gets a single lightweight
    confirmation, a published/executed action gets a stronger one, and a
    protected system fixture is flagged as non-deletable up front."""
    if (r := auth(request)): return r
    from services.business_action_registry import get_registry
    reg = get_registry(get_sb())
    action = reg.get(action_id)
    if not action:
        return JSONResponse({"ok": False, "error": "action_not_found"}, status_code=404)
    dependent_counts = reg.dependent_record_counts(action_id)
    has_execution_history = dependent_counts.get("erp_test_cases", 0) > 0
    return JSONResponse({
        "ok": True,
        "action_id": action_id, "action_key": action.get("action_key"),
        "is_draft": bool(action.get("is_draft")),
        "is_published": (not action.get("is_draft")) and bool(action.get("enabled")),
        "has_execution_history": has_execution_history,
        "is_protected": reg.is_protected_fixture(action),
        "dependent_counts": dependent_counts,
    })


@app.delete("/admin/api/business-actions/{action_id}")
async def api_delete_business_action(request: Request, action_id: str):
    """Permanent delete only (2026-07-29 rework) — soft delete used to
    leave a dead row still occupying its action_key forever at the DB's
    unique-constraint level (the getdatacustomer incident); there is no
    recycle bin / restore flow for this action anymore. See
    services/business_action_registry.py::hard_delete_action().

    force=True is intentionally NEVER accepted from this (or any other)
    public HTTP route (2026-07-29 production-readiness audit, Part 6) —
    every route in this app shares one admin auth() gate with no
    privilege tiers, so a `?force=true` query param would have let ANY
    authenticated session bypass protected-fixture protection with
    nothing more than curl/devtools. force=True is reachable ONLY via a
    direct Python call to the registry (e.g. an internal maintenance
    script run by someone with filesystem/DB access), never through the
    API surface a browser session can reach."""
    if (r := auth(request)): return r
    from services.business_action_registry import get_registry
    reg = get_registry(get_sb())
    try:
        result = reg.hard_delete_action(action_id, actor=_current_admin_user(request))
        # Defense-in-depth (2026-07-29 audit, Part 2): a deleted action's
        # row is already unresolvable (get()/get_full() filter on it
        # existing at all), so no cache lookup can ever reach a stale
        # entry for it — but explicitly dropping the entry here also
        # avoids it sitting as unreachable dead weight in the in-process
        # dict for the life of the process.
        from services.integration_contract_service import invalidate_contract_cache
        from services.conversation_form_generator import invalidate_form_cache
        invalidate_contract_cache(action_id)
        invalidate_form_cache(action_id)
        return JSONResponse({"ok": True, **result})
    except ValueError as e:
        reason = str(e)
        if reason == "action_not_found":
            return JSONResponse({"ok": False, "error": "action_not_found",
                                  "message": "This Business Action no longer exists."}, status_code=404)
        if reason == "protected_fixture":
            return JSONResponse({"ok": False, "error": "protected_fixture",
                                  "message": "This is a protected system fixture and cannot be deleted."},
                                 status_code=403)
        return JSONResponse({"ok": False, "error": reason}, status_code=400)
    except Exception as e:
        # Includes the orphan-records RuntimeError — never expose raw
        # DB internals, but never silently claim success either.
        return JSONResponse({"ok": False, "error": "delete_failed",
                              "message": f"Delete failed and may need investigation: {e}"}, status_code=500)


@app.post("/admin/api/business-actions/{action_id}/enabled")
async def api_set_business_action_enabled(request: Request, action_id: str):
    if (r := auth(request)): return r
    body = await request.json()
    from services.business_action_registry import get_registry
    action = get_registry(get_sb()).set_enabled(action_id, bool(body.get("enabled")))
    return JSONResponse({"ok": True, "action": action})


@app.post("/admin/api/business-actions/{action_id}/duplicate")
async def api_duplicate_business_action(request: Request, action_id: str):
    if (r := auth(request)): return r
    from services.business_action_registry import get_registry
    action = get_registry(get_sb()).duplicate(action_id, created_by=_current_admin_user(request))
    if not action:
        return JSONResponse({"ok": False, "error": "Action not found"}, status_code=404)
    return JSONResponse({"ok": True, "action": action})


@app.put("/admin/api/business-actions/{action_id}/examples")
async def api_replace_business_action_examples(request: Request, action_id: str):
    if (r := auth(request)): return r
    body = await request.json()
    from services.business_action_registry import get_registry
    rows = get_registry(get_sb()).replace_examples(action_id, body.get("examples") or [])
    return JSONResponse({"ok": True, "examples": rows})


@app.put("/admin/api/business-actions/{action_id}/parameters")
async def api_replace_business_action_parameters(request: Request, action_id: str):
    if (r := auth(request)): return r
    body = await request.json()
    from services.business_action_registry import get_registry
    try:
        rows = get_registry(get_sb()).replace_parameters(action_id, body.get("parameters") or [])
        return JSONResponse({"ok": True, "parameters": rows})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.put("/admin/api/business-actions/{action_id}/execution")
async def api_upsert_business_action_execution(request: Request, action_id: str):
    if (r := auth(request)): return r
    body = await request.json()
    from services.business_action_registry import get_registry, mask_execution_secrets
    try:
        execution = get_registry(get_sb()).upsert_execution(action_id, body)
        return JSONResponse({"ok": True, "execution": mask_execution_secrets(execution)})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.put("/admin/api/business-actions/{action_id}/response-mapping")
async def api_replace_business_action_response_mapping(request: Request, action_id: str):
    if (r := auth(request)): return r
    body = await request.json()
    from services.business_action_registry import get_registry
    rows = get_registry(get_sb()).replace_response_mapping(action_id, body.get("mapping") or [])
    return JSONResponse({"ok": True, "response_mapping": rows})


@app.put("/admin/api/business-actions/{action_id}/validation")
async def api_replace_business_action_validation(request: Request, action_id: str):
    if (r := auth(request)): return r
    body = await request.json()
    from services.business_action_registry import get_registry
    try:
        rows = get_registry(get_sb()).replace_validation_rules(action_id, body.get("rules") or [])
        return JSONResponse({"ok": True, "validation_rules": rows})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.put("/admin/api/business-actions/{action_id}/tags")
async def api_replace_business_action_tags(request: Request, action_id: str):
    if (r := auth(request)): return r
    body = await request.json()
    from services.business_action_registry import get_registry
    tags = get_registry(get_sb()).replace_tags(action_id, body.get("tags") or [])
    return JSONResponse({"ok": True, "tags": tags})


@app.put("/admin/api/business-actions/{action_id}/parameter-groups")
async def api_set_business_action_parameter_groups(request: Request, action_id: str):
    """Parameter Groups (Part: PARAMETER GROUP SUPPORT) — reusable rule
    groups (ALL/AT_LEAST_ONE/EXACTLY_ONE/OPTIONAL) over a set of
    parameter names. Never specific to one action."""
    if (r := auth(request)): return r
    body = await request.json()
    from services.business_action_registry import get_registry
    try:
        groups = get_registry(get_sb()).set_parameter_groups(action_id, body.get("groups") or [])
        return JSONResponse({"ok": True, "groups": groups})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.post("/admin/api/business-actions/{action_id}/embedding")
async def api_prepare_business_action_embedding(request: Request, action_id: str):
    """Embedding Preparation only — never computes a vector, never used
    for routing yet (Decision Engine is a future task)."""
    if (r := auth(request)): return r
    from services.business_action_registry import get_registry
    try:
        row = get_registry(get_sb()).prepare_embedding_source(action_id)
        return JSONResponse({"ok": True, "embedding": row})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.get("/admin/api/business-actions/{action_id}/export")
async def api_export_business_action(request: Request, action_id: str):
    if (r := auth(request)): return r
    from services.business_action_registry import get_registry
    exported = get_registry(get_sb()).export_action(action_id)
    if not exported:
        return JSONResponse({"ok": False, "error": "Action not found"}, status_code=404)
    return JSONResponse({"ok": True, "exported": exported})


@app.post("/admin/api/business-actions/import")
async def api_import_business_action(request: Request):
    if (r := auth(request)): return r
    body = await request.json()
    from services.business_action_registry import get_registry
    try:
        action = get_registry(get_sb()).import_action(body.get("exported") or {}, created_by=_current_admin_user(request))
        return JSONResponse({"ok": True, "action": action})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.post("/admin/api/business-actions/ai-auto-setup/analyze")
async def api_ai_auto_setup_analyze(request: Request):
    """Smart Capability Setup's single analysis entry point (Stage 1->2):
    accepts a link/cURL/document/plain-language description (`api_input`,
    kept for backward compatibility — the unified UI calls it
    `user_input`) + optional business purpose + example questions, and
    returns a structured, schema-validated proposal that ALSO includes
    the AI's own detected_action_type/detection_confidence — the admin
    is never asked to pick a type before analysis. NOTHING is saved
    here — the admin reviews/edits in Stage 2 before Stage 3 explicitly
    saves."""
    if (r := auth(request)): return r
    body = await request.json()
    user_input = (body.get("user_input") or body.get("api_input") or "").strip()
    business_purpose = (body.get("business_purpose") or "").strip() or user_input
    search_info = (body.get("search_info") or "").strip()
    example_questions = [q for q in (body.get("example_questions") or []) if q and q.strip()][:10]
    if not user_input:
        return JSONResponse({"ok": False, "error": "กรุณาวางลิงก์ API, cURL, เอกสาร หรืออธิบายสิ่งที่ต้องการ"}, status_code=400)
    from services.ai_auto_setup_service import analyze_capability
    from services.business_action_registry import get_registry
    try:
        existing_actions = get_registry(get_sb()).list()
    except Exception:
        existing_actions = []  # Similarity Engine degrades to create_new if the list can't be fetched.
    try:
        result = analyze_capability(user_input, business_purpose, example_questions,
                                     search_info=search_info, existing_actions=existing_actions)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"AI analysis failed: {str(e)}"}, status_code=502)
    return JSONResponse({"ok": True, **result})


@app.post("/admin/api/business-actions/ai-auto-setup/detect-endpoints")
async def api_ai_auto_setup_detect_endpoints(request: Request):
    """Postman Collection / OpenAPI-Swagger multi-endpoint detection —
    pure local parsing (no LLM call). Lets Stage 1 show a selectable
    endpoint checklist BEFORE the admin commits to analyzing (and
    potentially creating) one Business Action per endpoint. Returns
    an empty list for any input that isn't a multi-endpoint document —
    the normal single-endpoint cURL/plain-text flow is unaffected."""
    if (r := auth(request)): return r
    body = await request.json()
    document_text = body.get("document_text") or body.get("user_input") or ""
    from services.ai_auto_setup_service import detect_endpoints_from_document, cluster_endpoints
    try:
        endpoints = detect_endpoints_from_document(document_text)
        clusters = cluster_endpoints(endpoints)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"Endpoint detection failed: {str(e)}"}, status_code=502)
    return JSONResponse({"ok": True, "endpoints": endpoints, "is_multi": len(endpoints) > 1, "clusters": clusters})


@app.post("/admin/api/business-actions/ai-auto-setup/expand-questions")
async def api_ai_auto_setup_expand_questions(request: Request):
    """Question Enrichment: expands ONE example customer question into
    several natural Thai phrasings for the admin to pick from as chips."""
    if (r := auth(request)): return r
    body = await request.json()
    seed = (body.get("question") or "").strip()
    purpose = (body.get("business_purpose") or "").strip()
    if not seed:
        return JSONResponse({"ok": False, "error": "กรุณาระบุคำถามตัวอย่างอย่างน้อยหนึ่งข้อ"}, status_code=400)
    from services.ai_auto_setup_service import expand_example_questions
    try:
        suggestions = expand_example_questions(seed, purpose)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"ขยายตัวอย่างไม่สำเร็จ: {str(e)}"}, status_code=502)
    return JSONResponse({"ok": True, "suggestions": suggestions})


def _collect_avoid_phrases_for_question_generation(sb, exclude_action_id: Optional[str] = None) -> List[str]:
    """2026-07-29 — example questions already "spoken for" by OTHER
    Business Actions, plus RAG FAQ-style content already in the
    knowledge base, so a brand-new AI-suggested question doesn't collide
    with wording that already routes elsewhere. Best-effort: any failure
    here just means fewer avoid-phrases, never blocks question
    generation itself."""
    phrases: List[str] = []
    try:
        q = sb.table("business_action_examples").select("example_text,action_id") \
            .eq("example_type", "question").limit(1000).execute()
        for row in (q.data or []):
            if exclude_action_id and row.get("action_id") == exclude_action_id:
                continue
            if row.get("example_text"):
                phrases.append(row["example_text"])
    except Exception as e:
        print(f"[ai_auto_setup] could not collect other-action example questions: {e}")
    try:
        c = sb.table("knowledge_chunks").select("metadata").eq("is_active", True).limit(500).execute()
        for row in (c.data or []):
            for qtext in ((row.get("metadata") or {}).get("suggested_questions") or []):
                if qtext:
                    phrases.append(qtext)
    except Exception as e:
        print(f"[ai_auto_setup] could not collect RAG suggested_questions: {e}")
    return phrases


@app.post("/admin/api/business-actions/ai-auto-setup/suggest-questions")
async def api_ai_auto_setup_suggest_questions(request: Request):
    """AI Suggested Questions (bring-back feature) — generates 10-20
    realistic customer questions for a capability before the Business
    Action is saved. Never saves anything itself.

    2026-07-29 — also avoids reusing question phrasing already spoken
    for by another Business Action's own examples or by RAG FAQ content,
    so future ERP-vs-RAG / ERP-vs-ERP intent routing doesn't collide on
    near-identical example questions."""
    if (r := auth(request)): return r
    body = await request.json()
    display_name = (body.get("display_name") or "").strip()
    if not display_name:
        return JSONResponse({"ok": False, "error": "กรุณาระบุชื่อความสามารถ"}, status_code=400)
    description = (body.get("description") or "").strip()
    category = (body.get("category") or "").strip()
    existing_questions = [q for q in (body.get("existing_questions") or []) if q and q.strip()]
    exclude_action_id = body.get("exclude_action_id")
    from services.ai_auto_setup_service import generate_suggested_questions
    avoid_phrases = _collect_avoid_phrases_for_question_generation(get_sb(), exclude_action_id)
    try:
        questions = generate_suggested_questions(display_name, description, category, existing_questions,
                                                  avoid_phrases=avoid_phrases)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"สร้างตัวอย่างคำถามไม่สำเร็จ: {str(e)}"}, status_code=502)
    return JSONResponse({"ok": True, "questions": questions})


@app.get("/admin/api/business-actions/ai-auto-setup/semantic-endpoint-intents")
async def api_ai_auto_setup_semantic_endpoint_intents(request: Request):
    """Canonical Step-1 endpoint-intent vocabulary
    (semantic_api_analysis_engine.ENDPOINT_INTENTS) — the Review & Edit
    UI's Endpoint Intent override dropdown loads from here rather than
    hardcoding the list a second time (mirrors the existing
    /integration-contracts/operations route for the runtime vocabulary)."""
    if (r := auth(request)): return r
    from services.semantic_api_analysis_engine import ENDPOINT_INTENTS
    return JSONResponse({"ok": True, "endpoint_intents": list(ENDPOINT_INTENTS)})


@app.post("/admin/api/business-actions/ai-auto-setup/semantic-overrides/apply")
async def api_ai_auto_setup_apply_semantic_overrides(request: Request):
    """Semantic Analysis Review & Edit UI — the ONE place that computes
    Detected/Override/Effective values, reusing
    semantic_api_analysis_engine.apply_overrides() (Step 11) directly so
    the merge logic lives in exactly one place, never duplicated in
    JavaScript. Pure/deterministic, never touches the DB or an action;
    the caller (Review & Edit page) calls this reactively whenever the
    admin edits an override, then re-renders from the response."""
    if (r := auth(request)): return r
    body = await request.json()
    semantic_analysis = body.get("semantic_analysis")
    if not isinstance(semantic_analysis, dict):
        return JSONResponse({"ok": False, "error": "missing semantic_analysis"}, status_code=400)
    overrides = body.get("overrides") or {}
    from services.semantic_api_analysis_engine import apply_overrides
    effective = apply_overrides(semantic_analysis, overrides)
    return JSONResponse({"ok": True, "effective": effective, "provenance": effective.get("provenance", {})})


@app.post("/admin/api/business-actions/ai-auto-setup/save")
async def api_ai_auto_setup_save(request: Request):
    """Stage 3 of AI Auto Setup: saves the ADMIN-REVIEWED/EDITED proposal
    to the Business Action Registry — the single runtime source of truth.
    If `action_key` matches an existing permanent action (e.g.
    search_data_order), that action is UPDATED, never duplicated. New
    actions are created as a draft (enabled=False) by default; the
    caller must explicitly request enabled=True, which is still gated by
    registry.validate_can_enable()."""
    if (r := auth(request)): return r
    body = await request.json()
    proposal = body.get("proposal") or {}
    from services.ai_auto_setup_service import validate_proposal_schema
    from services.business_action_registry import get_registry
    ok, errors = validate_proposal_schema(proposal)
    if not ok:
        return JSONResponse({"ok": False, "error": "Invalid proposal", "errors": errors}, status_code=400)

    reg = get_registry(get_sb())
    action_key = proposal.get("action_id")

    # Semantic API Analysis Engine — reuse the analysis the analyze
    # endpoint already attached to `proposal.setup_metadata` (Steps
    # 1-9/11/12); recompute deterministically here only for callers
    # that saved without going through analyze_capability first (a
    # direct/manual save, or a pre-existing test fixture) — the
    # analysis is pure/deterministic so recomputing it from the SAME
    # saved parameters yields the identical result either way (Step 10).
    from services.semantic_api_analysis_engine import analyze_endpoint, apply_overrides
    existing_semantic_metadata = (proposal.get("setup_metadata") or {}).get("semantic_analysis")
    if existing_semantic_metadata:
        semantic_analysis = existing_semantic_metadata
    else:
        semantic_analysis = analyze_endpoint(
            endpoint_url=proposal.get("endpoint_path") or "",
            http_method=proposal.get("http_method") or "GET",
            description=proposal.get("description") or "",
            headers=proposal.get("headers") or {},
            body_fields=proposal.get("parameters") or [],
            example_response=proposal.get("example_response"),
        )
    existing = reg.get_by_key(action_key) if action_key else None

    # Semantic Analysis Review & Edit UI (Step 11) — admin-confirmed
    # overrides survive a re-analysis: prefer whatever the CURRENT save
    # call carries (the Review & Edit page always resends the full,
    # possibly-just-edited override set), falling back to whatever was
    # already persisted on the existing row so a legacy/stale caller
    # that doesn't know about overrides never silently erases them.
    semantic_overrides = (proposal.get("setup_metadata") or {}).get("semantic_overrides")
    if not semantic_overrides and existing:
        semantic_overrides = (existing.get("setup_metadata") or {}).get("semantic_overrides")
    semantic_overrides = semantic_overrides or {}
    effective_semantic_analysis = apply_overrides(semantic_analysis, semantic_overrides)
    requested_enabled = bool(body.get("enabled", False))

    # Pre-save existence check (Part 5C, level 1) — MUST look past
    # soft-deleted rows too. get_by_key() alone missed a key that
    # belonged to a soft-deleted action, so the code fell through to
    # reg.create() and hit the real Postgres unique constraint, leaking
    # a raw DB error (the actual reported bug). resolve_duplicate lets
    # the client explicitly say how a previously-surfaced duplicate
    # should be resolved, bypassing this check for that one call.
    resolve = body.get("resolve_duplicate") or {}
    if not existing and action_key and not resolve.get("target_action_id"):
        any_row = reg.get_by_key_including_deleted(action_key)
        if any_row:
            return _duplicate_action_response(action_key, any_row)

    # "Update Existing" resolution — the client resends the SAME save
    # call with resolve_duplicate.target_action_id set to the row the
    # duplicate modal identified. If that row was soft-deleted, restore
    # it first so update() doesn't leave a dangling deleted row still
    # occupying the key.
    if resolve.get("target_action_id"):
        target = reg.get(resolve["target_action_id"]) or reg.get_by_key_including_deleted(action_key)
        if target and target.get("deleted_at"):
            reg.restore(target["id"], updated_by=_current_admin_user(request))
        existing = reg.get(resolve["target_action_id"]) if target else None

    # The FINAL type is whatever the admin confirmed (either the AI's
    # high-confidence detection, or the interpretation they picked at
    # medium confidence) — `proposal.action_type` is what the client
    # sends after that resolution; `detected_action_type` is kept only
    # as the original AI signal for Developer Details / audit purposes.
    # Preserve any admin edits already made in the Advanced Editor's
    # Routing tab across re-runs of this AI-save endpoint — but still
    # let a first-time save populate the full AI-generated draft
    # (Parts 5 & 6: AI Behaviour / Routing auto-completion) rather than
    # leaving those fields blank.
    existing_routing = (existing.get("setup_metadata") or {}).get("routing") if existing else None
    routing_metadata = existing_routing or {
        "source_preference": proposal.get("routing_recommendation") or "erp_only",
        "when_to_call": proposal.get("when_to_call") or proposal.get("detection_reason"),
        "when_not_to_call": proposal.get("when_not_to_call"),
        "clarification_question": proposal.get("clarification_question"),
        "rag_combination": proposal.get("rag_combination") or "no_rag",
        "confidence_threshold": proposal.get("confidence_threshold_recommendation"),
        "intent": proposal.get("category"),
        "keywords": proposal.get("keywords") or [],
    }

    action_payload = {
        "action_key": action_key, "name": proposal.get("action_name"),
        "display_name": proposal.get("display_name"), "description": proposal.get("description"),
        "action_type": proposal.get("action_type") or proposal.get("detected_action_type") or "API",
        "category": proposal.get("category"),
        "ai_description": proposal.get("description"), "search_keywords": proposal.get("keywords") or [],
        # AI Behaviour tab — previously left blank by this endpoint even
        # when the AI proposal included useful drafts for them.
        "business_description": proposal.get("business_description"),
        "success_prompt": proposal.get("success_prompt"),
        "failure_prompt": proposal.get("failure_prompt"),
        "follow_up_prompt": proposal.get("follow_up_prompt"),
        "setup_source": "ai_auto_setup",
        "setup_metadata": {
            "conditions": proposal.get("conditions") or [], "warnings": proposal.get("warnings") or [],
            "connected_system": proposal.get("connected_system"),
            "knowledge_scope": proposal.get("knowledge_scope"),
            "tool_name": proposal.get("tool_name"),
            "triggers": proposal.get("triggers") or [],
            "workflow_steps": proposal.get("workflow_steps") or [],
            "detected_action_type": proposal.get("detected_action_type"),
            "detection_confidence": proposal.get("detection_confidence"),
            "detection_reason": proposal.get("detection_reason"),
            "search_info": proposal.get("search_info"),
            "when_to_call": proposal.get("when_to_call") or proposal.get("detection_reason"),
            # RAG-vs-ERP Routing Configuration — never wired into the
            # Decision Engine yet, stored for later consumption only.
            "routing": routing_metadata,
            # Semantic API Analysis Engine (Step 9's canonical metadata +
            # Step 11's override precedence) — this is what actually
            # wires the new engine into the Generic Integration Runtime
            # AND the Integration Schema/Contract: `operation_type` here
            # is read by erp_test_harness.infer_operation_type_with_
            # evidence() (tier 2, ALREADY existing) and, through it, by
            # integration_schema_service.resolve_effective_integration_
            # schema()'s operation-type resolution — zero changes needed
            # to either of those already-tested modules. Precedence,
            # highest first: (1) a raw legacy `setup_metadata.
            # operation_type` set directly by an older caller (back-
            # compat), (2) the Review & Edit page's own
            # operation_type_override (folded into `effective_...` via
            # apply_overrides() above), (3) the freshly-derived mapping.
            # Never silently replaced by a re-analysis (Step 11).
            "operation_type": (proposal.get("setup_metadata") or {}).get("operation_type") or
                               effective_semantic_analysis["mapped_runtime_operation_type"],
            "semantic_analysis": semantic_analysis,
            # The RAW detected analysis is stored separately from the
            # admin's override set — `semantic_analysis` above is always
            # the pure, deterministic detection (Step 10); overrides are
            # layered on top at READ time via apply_overrides(), exactly
            # like integration_schema_service's derived-vs-effective
            # schema split. The Review & Edit UI re-applies overrides
            # client-side (via the /semantic-overrides/apply route) so
            # it always shows Detected/Override/Effective correctly.
            "semantic_overrides": semantic_overrides,
        },
    }
    # Auto-fill parameter_groups from the engine's HIGH-confidence
    # (>=0.8) inferred validation groups only when the admin hasn't
    # already set any explicitly — never overwrites an admin-confirmed
    # group (Step 11's "never silently overwrite" rule applied to
    # validation groups, not just field-level overrides).
    if not proposal.get("parameter_groups"):
        high_confidence_groups = [
            {"name": f"auto_{g['rule'].lower()}_{i}", "rule": g["rule"], "members": g["members"]}
            for i, g in enumerate(effective_semantic_analysis.get("validation_groups") or [])
            if g.get("confidence", 1.0) >= 0.8
        ]
        if high_confidence_groups:
            proposal["parameter_groups"] = high_confidence_groups
    if existing:
        # Updating an EXISTING permanent action (e.g. re-verifying
        # search_data_order through Auto Setup) must never silently
        # disable it — only a NEW action defaults to draft/disabled.
        # requested_enabled only takes effect when the caller explicitly
        # asked to enable (the "Enable" button), never as a side effect
        # of Save Draft / Test Connection saving an already-live action.
        action_payload["is_draft"] = existing.get("is_draft", False) and not requested_enabled
        if requested_enabled:
            action_payload["enabled"] = False  # set via validate_can_enable()-gated set_enabled() below instead
        else:
            action_payload["enabled"] = existing.get("enabled", False)
    else:
        action_payload["is_draft"] = not requested_enabled
        action_payload["enabled"] = False
    # Smart Setup Save Flow, step 1-3: save/select credentials FIRST,
    # then strip any raw value out of the parameter list before it ever
    # reaches the Registry. `credential_values` is a transient,
    # request-scoped dict {credential_ref: plaintext} the admin typed
    # into "Save as new credential" in Review — it is NEVER persisted
    # anywhere itself, only used to create/rotate the encrypted row.
    from config import DEFAULT_TENANT_ID
    from services.credential_store import get_credential_store, MasterKeyMissingError, MasterKeyInvalidError
    credential_values = body.get("credential_values") or {}
    cred_store = get_credential_store(get_sb())
    parameters = proposal.get("parameters") or []
    for p in parameters:
        p.pop("_detected_credential", None)  # UI-only hint, never persisted
        if p.get("input_source") != "credential_store":
            continue
        ref = p.get("credential_ref")
        if not ref:
            continue
        plaintext = credential_values.get(ref)
        if plaintext:
            try:
                cred_store.get_or_create(DEFAULT_TENANT_ID, ref,
                                          body.get("credential_display_names", {}).get(ref, ref),
                                          body.get("credential_types", {}).get(ref, "secret_code"),
                                          plaintext, created_by=_current_admin_user(request))
            except (MasterKeyMissingError, MasterKeyInvalidError) as e:
                return JSONResponse({"ok": False, "error": f"ไม่สามารถบันทึกข้อมูลเชื่อมต่อได้: {str(e)}"}, status_code=400)
            except ValueError:
                pass  # already exists — fine, this save is just re-linking to it

    try:
        if existing:
            action = reg.update(existing["id"], action_payload, updated_by=_current_admin_user(request))
        else:
            action = reg.create(action_payload, created_by=_current_admin_user(request))
        action_id = action["id"]

        reg.replace_parameters(action_id, parameters)
        reg.set_parameter_groups(action_id, proposal.get("parameter_groups") or [])
        if action_payload["action_type"] in ("API", "WEBHOOK"):
            reg.upsert_execution(action_id, {
                "execution_target": proposal.get("action_name"),
                "base_url": proposal.get("base_url"), "endpoint_path": proposal.get("endpoint_path"),
                "http_method": proposal.get("http_method"), "content_type": proposal.get("content_type"),
                "headers": proposal.get("headers") or {}, "auth_type": "none", "timeout_seconds": 10,
            })
        if proposal.get("example_questions"):
            reg.replace_examples(action_id, [{"example_type": "question", "example_text": q}
                                              for q in proposal["example_questions"]])
        if proposal.get("response_mapping"):
            reg.replace_response_mapping(action_id, proposal["response_mapping"])

        if requested_enabled:
            if proposal.get("detection_confidence") == "low":
                return JSONResponse({"ok": True, "action": reg.get_full(action_id), "enabled": False,
                                      "enable_blocked_reasons": ["low_confidence_detection_must_be_confirmed"]})
            check = reg.validate_can_enable(action_id)
            if not check["ok"]:
                return JSONResponse({"ok": True, "action": reg.get_full(action_id), "enabled": False,
                                      "enable_blocked_reasons": check["reasons"]})
            reg.set_enabled(action_id, True)
            action = reg.get_full(action_id)

        return JSONResponse({"ok": True, "success": True, "action": reg.get_full(action_id), "was_update": bool(existing)})
    except Exception as e:
        # Pre-save check above handles the normal case; this is
        # defense-in-depth (Part 5C, level 2) for anything that slips
        # through — a race between the check and the insert, for
        # instance. NEVER forward the raw exception (constraint names,
        # SQLSTATE, stack traces) to the browser.
        if _is_duplicate_key_error(e):
            any_row = reg.get_by_key_including_deleted(action_key) if action_key else None
            return _duplicate_action_response(action_key, any_row)
        return JSONResponse({
            "ok": False, "success": False, "error": "Unable to save the Business Action. Please try again or review the configuration.",
        }, status_code=400)


@app.get("/admin/credentials", response_class=HTMLResponse)
async def credential_store_page(request: Request):
    """Credential Store UX (2026-08-02) — self-service page so an admin
    never needs a script or Claude to create a new credential or repair
    one whose encrypted value can't be decrypted under the current
    CREDENTIAL_ENCRYPTION_KEY (e.g. after moving to a new machine). Pure
    presentation — every action on this page calls the pre-existing,
    already-tested /admin/api/credentials/* routes below; no new backend
    logic."""
    if (r := auth(request)): return r
    if (r := require_developer_feature(request, "credential_store")): return r
    return render("credential_store.html", {"request": request, "active": "credential_store"})


@app.get("/admin/api/credentials")
async def api_list_credentials(request: Request):
    """Metadata-only list — never encrypted_value/plaintext. Tenant is
    always the caller's own (single-tenant deployments use
    DEFAULT_TENANT_ID; a future multi-tenant admin layer would resolve
    this from the authenticated session instead of a query param)."""
    if (r := auth(request)): return r
    from config import DEFAULT_TENANT_ID
    from services.credential_store import get_credential_store
    tenant_id = request.query_params.get("tenant_id") or DEFAULT_TENANT_ID
    credentials = get_credential_store(get_sb()).list_credentials(tenant_id)
    return JSONResponse({"ok": True, "credentials": credentials})


@app.post("/admin/api/credentials")
async def api_create_credential(request: Request):
    if (r := auth(request)): return r
    body = await request.json()
    from config import DEFAULT_TENANT_ID
    from services.credential_store import get_credential_store, MasterKeyMissingError, MasterKeyInvalidError
    tenant_id = body.get("tenant_id") or DEFAULT_TENANT_ID
    try:
        created = get_credential_store(get_sb()).create(
            tenant_id, body["credential_key"], body["display_name"], body.get("credential_type", "secret_code"),
            body["value"], integration_id=body.get("integration_id"), created_by=_current_admin_user(request))
        return JSONResponse({"ok": True, "credential": created})
    except (MasterKeyMissingError, MasterKeyInvalidError, ValueError, KeyError) as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.put("/admin/api/credentials/{credential_key}")
async def api_update_credential_value(request: Request, credential_key: str):
    """Update (rotate) the value of an existing credential — same
    endpoint semantics as rotate; kept separate per the spec's own
    "Update credential value" vs. "Rotate credential" API list, both
    calling the identical CredentialStore.rotate() under the hood since
    there is no meaningful difference between the two operations."""
    if (r := auth(request)): return r
    body = await request.json()
    from config import DEFAULT_TENANT_ID
    from services.credential_store import get_credential_store, MasterKeyMissingError, MasterKeyInvalidError
    tenant_id = body.get("tenant_id") or DEFAULT_TENANT_ID
    try:
        updated = get_credential_store(get_sb()).update_value(
            tenant_id, credential_key, body["value"], updated_by=_current_admin_user(request))
        return JSONResponse({"ok": True, "credential": updated})
    except (MasterKeyMissingError, MasterKeyInvalidError, ValueError, KeyError) as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.post("/admin/api/credentials/{credential_key}/rotate")
async def api_rotate_credential(request: Request, credential_key: str):
    if (r := auth(request)): return r
    body = await request.json()
    from config import DEFAULT_TENANT_ID
    from services.credential_store import get_credential_store, MasterKeyMissingError, MasterKeyInvalidError
    tenant_id = body.get("tenant_id") or DEFAULT_TENANT_ID
    try:
        rotated = get_credential_store(get_sb()).rotate(
            tenant_id, credential_key, body["value"], updated_by=_current_admin_user(request))
        return JSONResponse({"ok": True, "credential": rotated})
    except (MasterKeyMissingError, MasterKeyInvalidError, ValueError, KeyError) as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.post("/admin/api/credentials/{credential_key}/disable")
async def api_disable_credential(request: Request, credential_key: str):
    if (r := auth(request)): return r
    body = await request.json() if await request.body() else {}
    from config import DEFAULT_TENANT_ID
    from services.credential_store import get_credential_store
    tenant_id = body.get("tenant_id") or DEFAULT_TENANT_ID
    try:
        updated = get_credential_store(get_sb()).set_status(tenant_id, credential_key, "disabled",
                                                              updated_by=_current_admin_user(request))
        return JSONResponse({"ok": True, "credential": updated})
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.post("/admin/api/credentials/{credential_key}/enable")
async def api_enable_credential(request: Request, credential_key: str):
    if (r := auth(request)): return r
    body = await request.json() if await request.body() else {}
    from config import DEFAULT_TENANT_ID
    from services.credential_store import get_credential_store
    tenant_id = body.get("tenant_id") or DEFAULT_TENANT_ID
    try:
        updated = get_credential_store(get_sb()).set_status(tenant_id, credential_key, "enabled",
                                                              updated_by=_current_admin_user(request))
        return JSONResponse({"ok": True, "credential": updated})
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.post("/admin/api/credentials/{credential_key}/revoke")
async def api_revoke_credential(request: Request, credential_key: str):
    if (r := auth(request)): return r
    body = await request.json() if await request.body() else {}
    from config import DEFAULT_TENANT_ID
    from services.credential_store import get_credential_store
    tenant_id = body.get("tenant_id") or DEFAULT_TENANT_ID
    try:
        updated = get_credential_store(get_sb()).set_status(tenant_id, credential_key, "revoked",
                                                              updated_by=_current_admin_user(request))
        return JSONResponse({"ok": True, "credential": updated})
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.post("/admin/api/credentials/{credential_key}/test")
async def api_test_credential(request: Request, credential_key: str):
    """Confirms the credential resolves (decrypts, not disabled/revoked)
    WITHOUT ever returning the value — masked_preview only."""
    if (r := auth(request)): return r
    body = await request.json() if await request.body() else {}
    from config import DEFAULT_TENANT_ID
    from services.credential_store import get_credential_store
    tenant_id = body.get("tenant_id") or DEFAULT_TENANT_ID
    result = get_credential_store(get_sb()).test_reference(tenant_id, credential_key)
    return JSONResponse({"ok": True, **result})


@app.delete("/admin/api/credentials/{credential_key}")
async def api_delete_credential(request: Request, credential_key: str):
    """Refuses to delete a credential still referenced by any Business
    Action parameter's credential_ref — checked live against the
    Registry, never a stale cache."""
    if (r := auth(request)): return r
    from config import DEFAULT_TENANT_ID
    from services.credential_store import get_credential_store
    from services.business_action_registry import get_registry
    tenant_id = request.query_params.get("tenant_id") or DEFAULT_TENANT_ID
    reg = get_registry(get_sb())

    def _in_use(ref: str) -> bool:
        for action in reg.list():
            for p in reg.get_parameters(action["id"]):
                if p.get("input_source") == "credential_store" and p.get("credential_ref") == ref:
                    return True
        return False

    try:
        deleted = get_credential_store(get_sb()).delete_if_unused(tenant_id, credential_key, in_use_checker=_in_use)
        return JSONResponse({"ok": True, "deleted": deleted})
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=409)


@app.get("/admin/api/business-actions/{action_id}/enable-check")
async def api_business_action_enable_check(request: Request, action_id: str):
    """Draft vs. Enabled gate — used by the Test/Review screen's Enable
    button to explain WHY an action can't be enabled yet, before the
    admin even tries."""
    if (r := auth(request)): return r
    from services.business_action_registry import get_registry
    check = get_registry(get_sb()).validate_can_enable(action_id)
    return JSONResponse({"ok": True, **check})


@app.post("/admin/api/business-actions/{action_id}/execute")
async def api_execute_business_action(request: Request, action_id: str):
    """Business Action Framework + ERP Sync milestone, Phase 6 — the
    GENERIC manual execution endpoint for the Playground's Business
    Action tester. Unlike /test above (REST/HTTP-specific — builds an
    HTTP request from `execution.endpoint`), this calls
    services/action_executor.py::ActionExecutor.execute() directly, so
    it works for EVERY action_type (API/RAG/TOOL/WEBHOOK/...), including
    the TOOL-type mock-provider actions from
    tools/seed_business_action_providers.py. Never touches the Decision
    Engine (explicitly paused this milestone) or LINE OA — this is a
    direct Registry -> Executor -> Provider call, exactly per the
    existing architecture."""
    if (r := auth(request)): return r
    body = await request.json()
    from services.action_executor import ActionExecutor
    executor = ActionExecutor(get_sb())
    params = {k: v for k, v in (body.get("parameters") or {}).items() if v not in (None, "")}
    # Sent under BOTH keys: TOOL-type actions read "action_params"
    # (_tool_business_provider_lookup), while API-type actions'
    # customer_message parameters are resolved from "collected_slots"
    # (_resolve_param_value) — action_executor.py itself is untouched;
    # this is just making sure this manual test caller's values reach
    # whichever key each action_type actually reads.
    result = executor.execute(action_id, context={"action_params": params, "collected_slots": params})
    return JSONResponse({"ok": True, "result": result})


@app.post("/admin/api/business-actions/{action_id}/test")
async def api_test_business_action(request: Request, action_id: str):
    """Test Action — runs INDEPENDENTLY of the AI (Part: TEST ACTION).
    Executes the configured HTTP call with admin-supplied parameters and
    returns request/response/mapped-fields/timing/errors. Never routed
    through the Decision Engine (which doesn't exist yet) or the AI
    pipeline — this is a direct, manual configuration-verification tool.

    Security: secret-sourced parameters (input_source=secret_configuration)
    are resolved server-side from an environment variable — the caller
    must NEVER supply one in `parameters`, and its value is masked as
    "[MASKED]" (never partial) everywhere in the response. Response
    bodies are sanitized (PII-shaped fields masked) before being
    returned — never persisted anywhere."""
    if (r := auth(request)): return r
    body = await request.json()
    from services.business_action_registry import (
        get_registry, mask_execution_secrets, sanitize_response_body, sanitize_for_preview,
    )
    import time as _time
    reg = get_registry(get_sb())
    action = reg.get_full(action_id, mask_secrets=False)
    if not action:
        return JSONResponse({"ok": False, "error": "Action not found"}, status_code=404)
    execution = action.get("execution")
    if not execution or not execution.get("endpoint"):
        return JSONResponse({"ok": True, "result": {"error": "ยังไม่ได้ตั้งค่า Endpoint สำหรับ Action นี้"}})

    provided_params = {k: v for k, v in (body.get("parameters") or {}).items() if v not in (None, "")}
    parameters = action.get("parameters") or []
    secret_param_names = {p["name"] for p in parameters
                           if p.get("input_source") in ("secret_configuration", "credential_store")}

    # A caller must never be able to supply a secret/credential-sourced
    # parameter directly — even if the client tried to, it is dropped here.
    provided_params = {k: v for k, v in provided_params.items() if k not in secret_param_names}

    validation = reg.validate_can_execute(action_id, provided_params)
    if not validation["ok"]:
        parts = []
        if validation["missing_required"]:
            parts.append("ขาดข้อมูลที่จำเป็น: " + ", ".join(validation["missing_required"]))
        for g in validation["failed_groups"]:
            parts.append(f"ต้องมีอย่างน้อยหนึ่งค่าจาก: {', '.join(g['members'])}" if g["rule"] == "AT_LEAST_ONE"
                          else f"เงื่อนไขกลุ่ม '{g['name']}' ({g['rule']}) ไม่ผ่าน")
        return JSONResponse({"ok": True, "result": {"error": " / ".join(parts) or "ข้อมูลไม่ครบตามเงื่อนไข"}})

    secret_values = reg.resolve_secret_parameters(action_id)
    missing_secrets = [name for name, value in secret_values.items() if value is None]
    if missing_secrets:
        from services.action_executor import _friendly_credential_error
        return JSONResponse({"ok": True, "result": {"error": _friendly_credential_error(reg, action_id, missing_secrets)}})

    headers = dict(execution.get("headers") or {})
    auth_type = execution.get("auth_type")
    auth_config = execution.get("auth_config") or {}
    if auth_type == "bearer" and auth_config.get("bearer_token"):
        headers["Authorization"] = f"Bearer {auth_config['bearer_token']}"
    elif auth_type == "api_key" and auth_config.get("api_key"):
        headers[auth_config.get("header_name", "X-Api-Key")] = auth_config["api_key"]
    elif auth_type == "custom_header":
        headers.update(auth_config.get("headers") or {})

    # Final request body = validated customer/context params + server-
    # resolved secret params. Only names actually configured as
    # parameters for this action are ever sent.
    request_body = dict(provided_params)
    request_body.update(secret_values)

    preview_body = {k: ("[MASKED]" if k in secret_param_names else sanitize_for_preview(v))
                     for k, v in request_body.items()}
    t0 = _time.time()
    result = {"request": {"endpoint": execution["endpoint"], "method": execution.get("http_method", "GET"),
                           "headers": mask_execution_secrets({"headers": headers})["headers"],
                           "content_type": execution.get("content_type", "application/json"),
                           "body": preview_body}}
    try:
        import requests as _requests
        method = (execution.get("http_method") or "GET").upper()
        kwargs = {"headers": headers, "timeout": execution.get("timeout_seconds") or 10}
        use_form = execution.get("content_type") == "application/x-www-form-urlencoded"
        if method in ("POST", "PUT", "PATCH"):
            kwargs["data" if use_form else "json"] = request_body
        else:
            kwargs["params"] = request_body
        resp = _requests.request(method, execution["endpoint"], **kwargs)
        elapsed_ms = round((_time.time() - t0) * 1000, 1)
        try:
            response_body = resp.json()
        except Exception:
            response_body = resp.text

        sanitized_response = sanitize_response_body(response_body)
        detected_keys = list(response_body.keys()) if isinstance(response_body, dict) else []
        mapped = {}
        if isinstance(response_body, dict):
            for m in action.get("response_mapping") or []:
                path = m["json_path"].lstrip("$.")
                value = response_body
                for part in path.split("."):
                    value = value.get(part) if isinstance(value, dict) else None
                mapped[m["mapped_label"]] = sanitize_for_preview(value) if isinstance(value, str) else value

        friendly_error = None
        if resp.status_code == 401:
            friendly_error = "API ตอบกลับด้วยสถานะ 401 (ไม่ผ่านการยืนยันตัวตน)"
        elif resp.status_code == 404:
            friendly_error = "ไม่พบข้อมูลลูกค้า"
        elif resp.status_code >= 500:
            friendly_error = f"API ตอบกลับด้วยสถานะ {resp.status_code} (ฝั่งเซิร์ฟเวอร์ปลายทางขัดข้อง)"
        elif resp.status_code >= 400:
            friendly_error = f"API ตอบกลับด้วยสถานะ {resp.status_code}"

        result.update({"status_code": resp.status_code, "response": sanitized_response,
                       "detected_top_level_keys": detected_keys, "mapped_fields": mapped,
                       "execution_time_ms": elapsed_ms, "error": friendly_error})
        return JSONResponse({"ok": True, "result": result})
    except Exception as e:
        elapsed_ms = round((_time.time() - t0) * 1000, 1)
        error_text = str(e)
        for secret_value in secret_values.values():
            if secret_value:
                error_text = error_text.replace(secret_value, "[MASKED]")
        friendly = "การเชื่อมต่อหมดเวลา (Timeout)" if "timeout" in error_text.lower() else "เชื่อมต่อ API ไม่สำเร็จ"
        print(f"[business_action_test] action={action_id} failed: {friendly}")  # sanitized — never the raw exception with secrets
        result.update({"status_code": None, "response": None, "detected_top_level_keys": [], "mapped_fields": {},
                       "execution_time_ms": elapsed_ms, "error": friendly})
        return JSONResponse({"ok": True, "result": result})


def _current_admin_user(request: Request) -> str:
    """Best-effort admin identity for Audit fields (Created By/Updated
    By) — falls back to a generic label; this project has no per-admin
    login system yet (see admin/routes.py's single ADMIN_USERNAME auth)."""
    return "admin"


# ── Retrieval Settings ───────────────────────────────────────────────
# Deliberately separate from Prompt Studio — these are Search/Retrieval
# Engine settings (thresholds, weights, top-k, reranker), never LLM
# prompt settings. Rendered as a section inside the existing Settings
# page (admin/templates/settings.html), not a new top-level menu item.
# See services/retrieval_settings.py + migrations/021_retrieval_settings.sql.

@app.get("/admin/api/retrieval-settings")
async def api_get_retrieval_settings(request: Request):
    if (r := auth(request)): return r
    from services.retrieval_settings import get_active_settings, PRESETS
    settings = get_active_settings(force_reload=True)
    return JSONResponse({"ok": True, "settings": settings.as_dict(),
                          "presets": {name: p.as_dict() for name, p in PRESETS.items()}})


@app.post("/admin/api/retrieval-settings")
async def api_save_retrieval_settings(request: Request):
    if (r := auth(request)): return r
    body = await request.json()
    from services.retrieval_settings import RetrievalSettings, save_settings
    try:
        settings = RetrievalSettings(**{k: v for k, v in body.items() if k in RetrievalSettings.__dataclass_fields__})
        errors = settings.validation_errors()
        if errors:
            return JSONResponse({"ok": False, "errors": errors}, status_code=400)
        save_settings(settings)
        return JSONResponse({"ok": True, "settings": settings.as_dict()})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/admin/api/retrieval-settings/reset")
async def api_reset_retrieval_settings(request: Request):
    if (r := auth(request)): return r
    from services.retrieval_settings import get_preset, save_settings, DEFAULT_PRESET
    settings = get_preset(DEFAULT_PRESET)
    save_settings(settings)
    return JSONResponse({"ok": True, "settings": settings.as_dict()})


# ── AI Session Management (AI Trace Console) ───────────────────────
# See services/session_service.py for the actual persistence logic —
# these routes are thin wrappers so the Playground UI has somewhere to
# call. Every route degrades to a clear ok:false rather than a 500 when
# the migration (016_ai_sessions.sql) hasn't been run yet.

@app.post("/admin/playground/sessions")
async def create_session(request: Request):
    if (r := auth(request)): return r
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    from services.session_service import get_session_service
    session = get_session_service().create_session(name=body.get("name"))
    if not session:
        return JSONResponse({"ok": False, "error": "Could not create session (migration 016 run?)"}, status_code=500)
    return JSONResponse({"ok": True, "session": session})


@app.get("/admin/playground/sessions")
async def list_sessions(request: Request):
    if (r := auth(request)): return r
    q = request.query_params
    from services.session_service import get_session_service
    sessions = get_session_service().list_sessions(
        search=q.get("search") or None, date_filter=q.get("date_filter") or None,
        model=q.get("model") or None, status=q.get("status") or None,
        confidence=q.get("confidence") or None, channel=q.get("channel") or None,
    )
    return JSONResponse({"ok": True, "sessions": sessions})


@app.get("/admin/api/conversations/stats")
async def api_conversation_stats(request: Request):
    if (r := auth(request)): return r
    from services.session_service import get_session_service
    stats = get_session_service().get_conversation_stats(channel=request.query_params.get("channel") or None)
    return JSONResponse({"ok": True, "stats": stats})


@app.get("/admin/conversations", response_class=HTMLResponse)
async def conversations_page(request: Request):
    if (r := auth(request)): return r
    return render("conversations.html", {"request": request, "active": "conversations"})


@app.get("/admin/playground/sessions/{session_id}")
async def get_session_detail(request: Request, session_id: str):
    if (r := auth(request)): return r
    from services.session_service import get_session_service
    session = get_session_service().get_session(session_id)
    if not session:
        return JSONResponse({"ok": False, "error": "Session not found"}, status_code=404)
    return JSONResponse({"ok": True, "session": session})


@app.patch("/admin/playground/sessions/{session_id}")
async def rename_session_route(request: Request, session_id: str):
    if (r := auth(request)): return r
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        return JSONResponse({"ok": False, "error": "name is required"}, status_code=400)
    from services.session_service import get_session_service
    ok = get_session_service().rename_session(session_id, name)
    return JSONResponse({"ok": ok})


@app.delete("/admin/playground/sessions/{session_id}")
async def delete_session_route(request: Request, session_id: str):
    if (r := auth(request)): return r
    from services.session_service import get_session_service
    ok = get_session_service().delete_session(session_id)
    return JSONResponse({"ok": ok})


@app.post("/admin/playground/sessions/{session_id}/duplicate")
async def duplicate_session_route(request: Request, session_id: str):
    if (r := auth(request)): return r
    from services.session_service import get_session_service
    session = get_session_service().duplicate_session(session_id)
    if not session:
        return JSONResponse({"ok": False, "error": "Could not duplicate session"}, status_code=500)
    return JSONResponse({"ok": True, "session": session})


@app.post("/admin/playground/sessions/{session_id}/clear")
async def clear_session_route(request: Request, session_id: str):
    if (r := auth(request)): return r
    from services.session_service import get_session_service
    ok = get_session_service().clear_conversation(session_id)
    return JSONResponse({"ok": ok})


@app.get("/admin/playground/sessions/compare")
async def compare_sessions_route(request: Request):
    if (r := auth(request)): return r
    a, b = request.query_params.get("a"), request.query_params.get("b")
    if not a or not b:
        return JSONResponse({"ok": False, "error": "a and b session ids are required"}, status_code=400)
    from services.session_service import get_session_service
    diff = get_session_service().compare_sessions(a, b)
    if not diff:
        return JSONResponse({"ok": False, "error": "One or both sessions not found"}, status_code=404)
    return JSONResponse({"ok": True, "diff": diff})


@app.get("/admin/playground/sessions/{session_id}/export.{fmt}")
async def export_session_route(request: Request, session_id: str, fmt: str):
    if (r := auth(request)): return r
    from services.session_service import get_session_service
    svc = get_session_service()

    if fmt == "json":
        data = svc.export_json(session_id)
        if not data:
            return JSONResponse({"ok": False, "error": "Session not found"}, status_code=404)
        return JSONResponse(data, headers={
            "Content-Disposition": f'attachment; filename="session-{session_id}.json"'})

    if fmt in ("md", "markdown"):
        text = svc.export_markdown(session_id)
        if text is None:
            return JSONResponse({"ok": False, "error": "Session not found"}, status_code=404)
        return HTMLResponse(content=text, media_type="text/markdown", headers={
            "Content-Disposition": f'attachment; filename="session-{session_id}.md"'})

    if fmt == "pdf":
        # No PDF library in this project yet — render a print-friendly
        # HTML page instead of adding a new heavy dependency; the browser's
        # native "Print -> Save as PDF" produces an equivalent file. Real
        # server-side PDF generation is a reasonable future addition once
        # a PDF library is actually needed elsewhere in the app.
        return RedirectResponse(url=f"/admin/preview/sessions/{session_id}/print")

    return JSONResponse({"ok": False, "error": f"Unsupported export format '{fmt}'"}, status_code=400)


@app.get("/admin/preview/sessions/{session_id}/print", response_class=HTMLResponse)
async def print_session_page(request: Request, session_id: str):
    """Print-friendly view for the PDF export path — open this and use
    the browser's Print -> Save as PDF."""
    if (r := auth(request)): return r
    from services.session_service import get_session_service
    session = get_session_service().get_session(session_id)
    if not session:
        return HTMLResponse("<h1>Session not found</h1>", status_code=404)
    return render("session_print.html", {"request": request, "session": session})


@app.get("/admin/playground/status")
async def playground_status(request: Request):
    """Top status bar + System Health data — every number here is a real
    live query, not a placeholder."""
    if (r := auth(request)): return r
    from config import OPENAI_CHAT_MODEL, STORAGE_PROVIDER, OPENAI_API_KEY
    from services.embedding_service import get_embedding_provider
    from services.policy_engine import evaluate as evaluate_policies
    from services.prompt_builder import get_template, DEFAULT_TEMPLATE_ID

    sb = get_sb()
    health = {}

    try:
        kf = sb.table("knowledge_files").select("id", count="exact") \
            .is_("deleted_at", "null").not_.is_("synced_at", "null").execute()
        knowledge_files_count = kf.count or 0
        health["knowledge_database"] = "healthy"
    except Exception as e:
        knowledge_files_count = 0
        health["knowledge_database"] = "error"
        print(f"[Playground] status: knowledge_files count failed: {e}")

    try:
        kc = sb.table("knowledge_chunks").select("id", count="exact").eq("is_active", True).execute()
        chunk_count = kc.count or 0
        health["vector_database"] = "healthy"
    except Exception as e:
        chunk_count = 0
        health["vector_database"] = "error"
        print(f"[Playground] status: chunk count failed: {e}")

    try:
        ka = sb.table("knowledge_attachments").select("id", count="exact").is_("deleted_at", "null").execute()
        attachment_count = ka.count or 0
        health["attachment_storage"] = "healthy"
    except Exception as e:
        attachment_count = 0
        health["attachment_storage"] = "warning"  # non-critical table, don't red the whole bar

    health["openai_api"] = "healthy" if OPENAI_API_KEY else "error"

    try:
        from storage import get_storage_service
        get_storage_service()
        health["storage"] = "healthy"
    except Exception:
        health["storage"] = "error"

    overall = "error" if "error" in health.values() else ("warning" if "warning" in health.values() else "healthy")

    policy = evaluate_policies("")  # structural check — no real question, just to report active count
    template = get_template(DEFAULT_TEMPLATE_ID)
    avg_latency = _get_playground_avg_latency()

    # Report the REAL active embedding provider/model — this used to
    # import config.EMBEDDING_MODEL directly (the local-model constant,
    # always sentence-transformers regardless of EMBEDDING_PROVIDER), so
    # the header kept showing the local model name even after the
    # OpenAI migration. Must go through the same
    # services.embedding_service.get_embedding_provider() singleton
    # everything else (ingestion + query search) uses, so this can never
    # drift from what's actually embedding chunks/queries again.
    embed_provider = get_embedding_provider()

    return JSONResponse({
        "ok": True,
        "provider": "OpenAI",
        "model": OPENAI_CHAT_MODEL,
        "embedding_provider": embed_provider.provider_name,
        "embedding_model": embed_provider.model_name(),
        "embedding_dimensions": embed_provider.dimensions(),
        "embedding_version": embed_provider.version(),
        "prompt_template": template.name,
        "prompt_version": template.version,
        "active_policies": policy.active_count,
        "knowledge_files": knowledge_files_count,
        "chunks": chunk_count,
        "attachments": attachment_count,
        "storage_provider": STORAGE_PROVIDER,
        "avg_latency_ms": avg_latency,
        "status": overall,
        "health": health,
    })
