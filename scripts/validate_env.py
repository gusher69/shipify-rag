"""Environment health validator (2026-08-01 configuration audit / cleanup).

A complete, standalone check of this machine's local configuration —
required variables, placeholder detection, and live connectivity to every
external system the platform depends on (OpenAI, Supabase REST API,
PostgreSQL, pgvector, Storage, LINE). Returns a clear PASS/FAIL report,
category by category, and never prints a secret's real value.

Deliberately NOT wired into admin.routes:app's or line_bot.webhook:app's
startup — this is a standalone, opt-in check a developer runs by hand (or
scripts/preflight.py runs on their behalf). A missing/placeholder/
unreachable value is surfaced as a clear report line, never something that
silently changes what happens when the real app boots. Reuses config.py's
already-resolved constants (the one place this project calls os.getenv)
rather than re-reading the environment a second time, and reuses
services/embedding_service.py's existing, already-tested dimension check
rather than re-implementing it.

Usage:
    python scripts/validate_env.py
"""
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config

STATUS_ORDER = {"FAIL": 0, "WARN": 1, "SKIP": 2, "PASS": 3}


@dataclass
class CheckResult:
    category: str
    name: str
    status: str  # "PASS" | "FAIL" | "WARN" | "SKIP"
    message: str


# Known .env.example / config.py-hardcoded placeholder values — a variable
# still equal to one of these was never actually configured, just copied
# verbatim from the template or left at its insecure built-in default.
_KNOWN_PLACEHOLDERS = {
    "OPENAI_API_KEY": {"sk-xxx"},
    "ADMIN_PASSWORD": {"changeme"},
    "SESSION_SECRET": {"shipify-secret-change-me"},
}
_URL_PLACEHOLDER_MARKER = "xxxxxxxxxxxx"


def _looks_like_placeholder(name: str, value: Optional[str]) -> bool:
    if not value:
        return False
    if value in _KNOWN_PLACEHOLDERS.get(name, ()):
        return True
    if _URL_PLACEHOLDER_MARKER in value:
        return True
    return False


def _check_required_string(category: str, name: str, value: Optional[str], *, min_len: int = 1,
                            shape_ok: Optional[Callable[[str], bool]] = None,
                            shape_hint: str = "") -> List[CheckResult]:
    if not value:
        return [CheckResult(category, name, "FAIL", f"{name} is empty/missing — required.")]
    if _looks_like_placeholder(name, value):
        return [CheckResult(category, name, "FAIL",
                             f"{name} still looks like the .env.example placeholder — replace it with a real value.")]
    if len(value) < min_len:
        return [CheckResult(category, name, "WARN", f"{name} looks unusually short ({len(value)} chars).")]
    if shape_ok and not shape_ok(value):
        return [CheckResult(category, name, "WARN", f"{name} doesn't look like {shape_hint} — double-check it.")]
    return [CheckResult(category, name, "PASS", f"{name} is set and looks valid.")]


# ── 1. Required variables + placeholder detection ──────────────────────────

def check_required_variables() -> List[CheckResult]:
    cat = "Required Variables"
    results = []
    results += _check_required_string(cat, "OPENAI_API_KEY", config.OPENAI_API_KEY, min_len=20,
                                       shape_ok=lambda v: v.startswith("sk-"), shape_hint="an OpenAI API key (sk-...)")
    results += _check_required_string(cat, "SUPABASE_URL", config.SUPABASE_URL, min_len=20,
                                       shape_ok=lambda v: v.startswith("https://"), shape_hint="a Supabase project URL")
    results += _check_required_string(cat, "SUPABASE_SERVICE_KEY", config.SUPABASE_KEY, min_len=40)
    results += _check_required_string(cat, "SUPABASE_DB_URL", config.SUPABASE_DB_URL, min_len=30,
                                       shape_ok=lambda v: v.startswith("postgresql://"),
                                       shape_hint="a postgresql:// connection string")
    results += _check_required_string(cat, "ADMIN_PASSWORD", config.ADMIN_PASSWORD)
    results += _check_required_string(cat, "SESSION_SECRET", config.SESSION_SECRET, min_len=32)

    if not config.CREDENTIAL_ENCRYPTION_KEY:
        results.append(CheckResult(cat, "CREDENTIAL_ENCRYPTION_KEY", "FAIL",
                                    "Empty — Credential Store will refuse to create/rotate/resolve any "
                                    "encrypted credential until this is set."))
    else:
        try:
            from cryptography.fernet import Fernet
            Fernet(config.CREDENTIAL_ENCRYPTION_KEY.encode())
            results.append(CheckResult(cat, "CREDENTIAL_ENCRYPTION_KEY", "PASS", "Valid Fernet key."))
        except Exception:
            results.append(CheckResult(cat, "CREDENTIAL_ENCRYPTION_KEY", "FAIL",
                                        "Not a valid Fernet key — generate one with python -c \"from "
                                        "cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"."))
    return results


# ── 2. OpenAI connectivity ──────────────────────────────────────────────────

def check_openai_connectivity() -> List[CheckResult]:
    cat = "OpenAI"
    if not config.OPENAI_API_KEY or _looks_like_placeholder("OPENAI_API_KEY", config.OPENAI_API_KEY):
        return [CheckResult(cat, "API connectivity", "SKIP", "OPENAI_API_KEY not configured.")]
    try:
        from openai import OpenAI
        client = OpenAI(api_key=config.OPENAI_API_KEY, timeout=10.0)
        models = {m.id for m in client.models.list().data}
        chat_ok = config.OPENAI_CHAT_MODEL in models
        return [CheckResult(cat, "API connectivity", "PASS",
                             f"Authenticated and listed {len(models)} model(s)."
                             + ("" if chat_ok else f" (note: '{config.OPENAI_CHAT_MODEL}' not found in the list — "
                                                    f"verify OPENAI_CHAT_MODEL)"))]
    except Exception as e:
        return [CheckResult(cat, "API connectivity", "FAIL",
                             f"Could not reach OpenAI or key was rejected ({type(e).__name__}).")]


# ── 3. Supabase REST API ────────────────────────────────────────────────────

def _get_supabase_client():
    from supabase import create_client
    return create_client(config.SUPABASE_URL, config.SUPABASE_KEY)


def check_supabase_api() -> List[CheckResult]:
    cat = "Supabase API"
    if (not config.SUPABASE_URL or _looks_like_placeholder("SUPABASE_URL", config.SUPABASE_URL)
            or not config.SUPABASE_KEY):
        return [CheckResult(cat, "REST connectivity", "SKIP", "SUPABASE_URL/SUPABASE_SERVICE_KEY not configured.")]
    try:
        sb = _get_supabase_client()
        sb.table("knowledge_files").select("id").limit(1).execute()
        return [CheckResult(cat, "REST connectivity", "PASS", "Reached Supabase and queried knowledge_files.")]
    except Exception as e:
        msg = str(e)
        if "does not exist" in msg or "42P01" in msg or "PGRST" in msg:
            return [CheckResult(cat, "REST connectivity", "WARN",
                                 "Reached Supabase, but knowledge_files was not queryable as expected — "
                                 "has migrations/ been applied in order?")]
        return [CheckResult(cat, "REST connectivity", "FAIL",
                             f"Could not reach Supabase or the key was rejected ({type(e).__name__}).")]


# ── 4. PostgreSQL direct connection + 5. pgvector ───────────────────────────

def check_postgres_and_pgvector() -> List[CheckResult]:
    pg_cat, vec_cat = "PostgreSQL", "pgvector"
    if not config.SUPABASE_DB_URL or _looks_like_placeholder("SUPABASE_DB_URL", config.SUPABASE_DB_URL):
        return [
            CheckResult(pg_cat, "Direct connection", "SKIP", "SUPABASE_DB_URL not configured."),
            CheckResult(vec_cat, "Extension + column width", "SKIP", "SUPABASE_DB_URL not configured."),
        ]
    try:
        import psycopg2
    except ImportError:
        return [
            CheckResult(pg_cat, "Direct connection", "FAIL",
                        "psycopg2 is not installed — see requirements.txt (psycopg2-binary)."),
            CheckResult(vec_cat, "Extension + column width", "SKIP", "Cannot check without psycopg2."),
        ]
    try:
        conn = psycopg2.connect(config.SUPABASE_DB_URL, connect_timeout=8)
    except Exception as e:
        return [
            CheckResult(pg_cat, "Direct connection", "FAIL", f"Could not connect ({type(e).__name__})."),
            CheckResult(vec_cat, "Extension + column width", "SKIP", "Cannot check — PostgreSQL unreachable."),
        ]
    results = [CheckResult(pg_cat, "Direct connection", "PASS", "Connected and ran SELECT 1 successfully.")]
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
        has_ext = cur.fetchone() is not None
        if not has_ext:
            results.append(CheckResult(vec_cat, "Extension", "FAIL",
                                        "pgvector extension not installed — run: "
                                        "create extension if not exists vector;"))
        else:
            results.append(CheckResult(vec_cat, "Extension", "PASS", "pgvector extension is installed."))
            cur.execute(
                "SELECT atttypmod FROM pg_attribute WHERE attrelid = 'knowledge_chunks'::regclass "
                "AND attname = 'embedding' AND NOT attisdropped"
            )
            row = cur.fetchone()
            if row:
                results.append(CheckResult(vec_cat, "knowledge_chunks.embedding width", "PASS",
                                            f"Column width is VECTOR({row[0]})."))
            else:
                results.append(CheckResult(vec_cat, "knowledge_chunks.embedding width", "WARN",
                                            "knowledge_chunks table/column not found — has "
                                            "migrations/019_openai_embedding_dimension.sql been applied?"))
    except Exception as e:
        results.append(CheckResult(vec_cat, "Extension + column width", "FAIL",
                                    f"Could not check ({type(e).__name__})."))
    finally:
        conn.close()
    return results


# ── 6. Storage bucket ────────────────────────────────────────────────────────

def check_storage_bucket() -> List[CheckResult]:
    cat = "Storage"
    provider = config.STORAGE_PROVIDER
    if provider == "supabase":
        if (not config.SUPABASE_URL or _looks_like_placeholder("SUPABASE_URL", config.SUPABASE_URL)
                or not config.SUPABASE_KEY):
            return [CheckResult(cat, f"Bucket ({provider})", "SKIP", "Supabase not configured.")]
        try:
            sb = _get_supabase_client()
            buckets = sb.storage.list_buckets()
            names = [getattr(b, "name", None) or (b.get("name") if isinstance(b, dict) else None) for b in buckets]
            if config.SUPABASE_STORAGE_BUCKET in names:
                return [CheckResult(cat, f"Bucket ({provider})", "PASS",
                                     f"Bucket '{config.SUPABASE_STORAGE_BUCKET}' exists.")]
            return [CheckResult(cat, f"Bucket ({provider})", "WARN",
                                 f"Bucket '{config.SUPABASE_STORAGE_BUCKET}' not found among "
                                 f"{len(names)} bucket(s) — create it in the Supabase dashboard.")]
        except Exception as e:
            return [CheckResult(cat, f"Bucket ({provider})", "FAIL", f"Could not list buckets ({type(e).__name__}).")]
    elif provider == "local":
        root = Path(config.LOCAL_STORAGE_ROOT)
        if root.exists() and root.is_dir():
            return [CheckResult(cat, "Local directory", "PASS", f"'{root}' exists.")]
        return [CheckResult(cat, "Local directory", "WARN", f"'{root}' does not exist yet — created on first use.")]
    else:
        return [CheckResult(cat, f"Bucket ({provider})", "SKIP",
                             f"STORAGE_PROVIDER={provider} — no live check implemented for this provider.")]


# ── 7/8/9. Embedding model / runtime dimension / database dimension ────────

def check_embedding_configuration() -> List[CheckResult]:
    cat = "Embedding"
    try:
        from services.embedding_service import report_and_validate_embedding_configuration
        result = report_and_validate_embedding_configuration()
    except Exception as e:
        return [CheckResult(cat, "Model / dimension", "FAIL", str(e))]
    if result is None:
        return [CheckResult(cat, "Model", "FAIL",
                             "Could not construct the embedding provider (e.g. missing OPENAI_API_KEY).")]
    results = [CheckResult(cat, "Model + runtime dimension", "PASS",
                            f"model={result['model']}, runtime_dimension={result['runtime_dimension']}")]
    if result["expected_db_dimension"] is None:
        results.append(CheckResult(cat, "Database dimension", "WARN",
                                    "Could not reach the database to confirm the expected dimension."))
    else:
        results.append(CheckResult(cat, "Database dimension", "PASS",
                                    f"database expects {result['expected_db_dimension']} — matches runtime."))
    return results


# ── 10. Credential Store ─────────────────────────────────────────────────────

def check_credential_store() -> List[CheckResult]:
    cat = "Credential Store"
    results = []
    if not config.CREDENTIAL_ENCRYPTION_KEY:
        results.append(CheckResult(cat, "Master key", "WARN",
                                    "CREDENTIAL_ENCRYPTION_KEY is empty — disabled; legacy env-var-based "
                                    "Business Action secrets still work without it."))
    else:
        try:
            from cryptography.fernet import Fernet
            Fernet(config.CREDENTIAL_ENCRYPTION_KEY.encode())
            results.append(CheckResult(cat, "Master key", "PASS", "Valid Fernet key."))
        except Exception:
            results.append(CheckResult(cat, "Master key", "FAIL", "Not a valid Fernet key."))
    if (config.SUPABASE_URL and config.SUPABASE_KEY
            and not _looks_like_placeholder("SUPABASE_URL", config.SUPABASE_URL)):
        try:
            sb = _get_supabase_client()
            sb.table("integration_credentials").select("id").limit(1).execute()
            results.append(CheckResult(cat, "Database table", "PASS", "integration_credentials table reachable."))
        except Exception as e:
            results.append(CheckResult(cat, "Database table", "WARN",
                                        f"Could not query integration_credentials ({type(e).__name__})."))
    else:
        results.append(CheckResult(cat, "Database table", "SKIP", "Supabase not configured."))
    return results


# ── 11. LINE configuration ───────────────────────────────────────────────────

def check_line_configuration() -> List[CheckResult]:
    cat = "LINE"
    if not config.LINE_CHANNEL_SECRET and not config.LINE_CHANNEL_TOKEN:
        return [CheckResult(cat, "Configuration", "SKIP",
                             "Not set — only required for line_bot.webhook:app, not the Admin app.")]
    results = []
    if not config.LINE_CHANNEL_SECRET:
        results.append(CheckResult(cat, "LINE_CHANNEL_SECRET", "FAIL", "Missing (LINE_CHANNEL_TOKEN is set)."))
    if not config.LINE_CHANNEL_TOKEN:
        results.append(CheckResult(cat, "LINE_CHANNEL_TOKEN", "FAIL", "Missing (LINE_CHANNEL_SECRET is set)."))
    if config.LINE_CHANNEL_TOKEN:
        try:
            import requests
            resp = requests.get("https://api.line.me/v2/bot/info",
                                 headers={"Authorization": f"Bearer {config.LINE_CHANNEL_TOKEN}"}, timeout=8)
            if resp.status_code == 200:
                results.append(CheckResult(cat, "Channel Token validity", "PASS",
                                            "LINE Messaging API accepted the channel access token."))
            elif resp.status_code == 401:
                results.append(CheckResult(cat, "Channel Token validity", "FAIL",
                                            "LINE Messaging API rejected the channel access token (401)."))
            else:
                results.append(CheckResult(cat, "Channel Token validity", "WARN",
                                            f"LINE API returned HTTP {resp.status_code}."))
        except Exception as e:
            results.append(CheckResult(cat, "Channel Token validity", "WARN",
                                        f"Could not reach the LINE API ({type(e).__name__})."))
    return results


# ── Orchestration ────────────────────────────────────────────────────────────

def run_all_checks() -> List[CheckResult]:
    """Runs every category and returns the combined results. Never raises —
    any individual check's own exception is caught and reported as FAIL for
    that check, so one broken check never hides the rest of the report."""
    checks = [
        check_required_variables,
        check_openai_connectivity,
        check_supabase_api,
        check_postgres_and_pgvector,
        check_storage_bucket,
        check_embedding_configuration,
        check_credential_store,
        check_line_configuration,
    ]
    results: List[CheckResult] = []
    for check in checks:
        try:
            results += check()
        except Exception as e:
            results.append(CheckResult(check.__name__, "(check itself)", "FAIL",
                                        f"Check crashed unexpectedly: {type(e).__name__}: {e}"))
    return results


def format_report(results: List[CheckResult]) -> str:
    lines = []
    by_category: dict = {}
    for r in results:
        by_category.setdefault(r.category, []).append(r)

    counts = {"PASS": 0, "FAIL": 0, "WARN": 0, "SKIP": 0}
    for r in results:
        counts[r.status] += 1

    verdict = "FAIL" if counts["FAIL"] else "PASS"
    lines.append("=" * 70)
    lines.append(f"ENVIRONMENT HEALTH CHECK — {verdict}")
    lines.append(f"  {counts['PASS']} passed, {counts['FAIL']} failed, "
                 f"{counts['WARN']} warnings, {counts['SKIP']} skipped")
    lines.append("=" * 70)
    for category, items in by_category.items():
        lines.append(f"\n{category}")
        for r in sorted(items, key=lambda x: STATUS_ORDER[x.status]):
            lines.append(f"  [{r.status:4}] {r.name}: {r.message}")
    return "\n".join(lines)


if __name__ == "__main__":
    all_results = run_all_checks()
    print(format_report(all_results))
    sys.exit(1 if any(r.status == "FAIL" for r in all_results) else 0)
