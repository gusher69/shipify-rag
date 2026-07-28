"""Secure multi-tenant Credential Store — eliminates the need for one
environment variable per customer API secret.

Encryption: Fernet (`cryptography.fernet`) — authenticated symmetric
encryption (AES-128-CBC + HMAC-SHA256 under the hood), a well-reviewed
standard primitive from the `cryptography` package (an existing,
approved dependency of this project — no custom encryption is invented
here). The single master key (`config.CREDENTIAL_ENCRYPTION_KEY`) is
loaded once at import time from the environment; it is NEVER stored in
the database, never returned by any API, and never logged.

Tenant isolation: every row is scoped by `tenant_id`; every read/write
in this module takes `tenant_id` as an explicit, required argument —
there is no "list everything" method that could leak across tenants.

This module never returns a decrypted value except from `resolve()`,
which is meant to be called only by the Generic Action Executor
immediately before building a request — the caller must not persist or
log the return value.
"""
import re
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from cryptography.fernet import Fernet, InvalidToken

CREDENTIAL_TYPES = (
    "api_key", "bearer_token", "basic_auth", "secret_code",
    "username_password", "client_credentials", "custom_header", "custom_body_secret",
)
STATUSES = ("enabled", "disabled", "revoked")

_METADATA_LIST_FIELDS = ("display_name", "credential_key", "credential_type", "masked_preview",
                          "status", "last_used_at", "id", "tenant_id", "integration_id",
                          "created_at", "updated_at", "rotated_at")


class MasterKeyMissingError(Exception):
    """Raised whenever CREDENTIAL_ENCRYPTION_KEY isn't configured —
    encrypted credential storage is simply unavailable until an admin
    sets it; legacy secret_configuration parameters are unaffected."""
    pass


class MasterKeyInvalidError(Exception):
    """Raised when decryption fails under the currently configured key
    (e.g. the key was rotated/changed without re-encrypting existing
    rows) — never leaks ciphertext or key material in the message."""
    pass


def mask_value(value: str) -> str:
    """"********8151" style preview — keeps only the last 4 characters,
    everything else replaced with a fixed run of asterisks so the length
    itself never leaks how long the real secret is."""
    if not value:
        return ""
    tail = value[-4:] if len(value) > 4 else value
    return "*" * 8 + tail


def _get_fernet() -> Fernet:
    from config import CREDENTIAL_ENCRYPTION_KEY
    if not CREDENTIAL_ENCRYPTION_KEY:
        raise MasterKeyMissingError("CREDENTIAL_ENCRYPTION_KEY is not configured")
    try:
        return Fernet(CREDENTIAL_ENCRYPTION_KEY.encode() if isinstance(CREDENTIAL_ENCRYPTION_KEY, str) else CREDENTIAL_ENCRYPTION_KEY)
    except Exception as e:
        raise MasterKeyInvalidError("CREDENTIAL_ENCRYPTION_KEY is not a valid Fernet key") from e


def encrypt_value(plaintext: str) -> str:
    fernet = _get_fernet()
    return fernet.encrypt(plaintext.encode()).decode()


def decrypt_value(ciphertext: str) -> str:
    fernet = _get_fernet()
    try:
        return fernet.decrypt(ciphertext.encode()).decode()
    except InvalidToken as e:
        raise MasterKeyInvalidError("Cannot decrypt credential under the current master key") from e


def _to_metadata(row: Dict) -> Dict:
    """Strips encrypted_value (and anything else not on the safe list)
    before a row is ever returned to an Admin API caller."""
    return {k: row.get(k) for k in _METADATA_LIST_FIELDS}


def _audit(sb, tenant_id: str, credential_key: str, event: str, actor: Optional[str] = None, detail: Optional[Dict] = None):
    try:
        sb.table("credential_audit_log").insert({
            "tenant_id": tenant_id, "credential_key": credential_key, "event": event,
            "actor": actor, "detail": detail or {},
        }).execute()
    except Exception:
        pass  # audit logging must never break the primary operation


class CredentialStore:
    def __init__(self, sb):
        self._sb = sb

    # ── Metadata-only reads (never return encrypted_value) ──────────

    def list_credentials(self, tenant_id: str) -> List[Dict]:
        rows = self._sb.table("integration_credentials").select("*").eq("tenant_id", tenant_id).execute().data or []
        return [_to_metadata(r) for r in rows]

    def get_metadata(self, tenant_id: str, credential_key: str) -> Optional[Dict]:
        rows = self._sb.table("integration_credentials").select("*") \
            .eq("tenant_id", tenant_id).eq("credential_key", credential_key).execute().data
        return _to_metadata(rows[0]) if rows else None

    def _get_row(self, tenant_id: str, credential_key: str) -> Optional[Dict]:
        rows = self._sb.table("integration_credentials").select("*") \
            .eq("tenant_id", tenant_id).eq("credential_key", credential_key).execute().data
        return rows[0] if rows else None

    # ── Lifecycle ─────────────────────────────────────────────────

    def create(self, tenant_id: str, credential_key: str, display_name: str, credential_type: str,
               plaintext_value: str, *, integration_id: Optional[str] = None, metadata: Optional[Dict] = None,
               created_by: Optional[str] = None) -> Dict:
        if credential_type not in CREDENTIAL_TYPES:
            raise ValueError(f"credential_type must be one of {CREDENTIAL_TYPES}")
        if self._get_row(tenant_id, credential_key):
            raise ValueError(f"credential_key '{credential_key}' already exists for this tenant")
        encrypted = encrypt_value(plaintext_value)
        row = {
            "tenant_id": tenant_id, "credential_key": credential_key, "display_name": display_name,
            "credential_type": credential_type, "encrypted_value": encrypted,
            "masked_preview": mask_value(plaintext_value), "metadata": metadata or {},
            "status": "enabled", "integration_id": integration_id, "created_by": created_by, "updated_by": created_by,
        }
        created = self._sb.table("integration_credentials").insert(row).execute().data[0]
        _audit(self._sb, tenant_id, credential_key, "credential_created", created_by)
        return _to_metadata(created)

    def get_or_create(self, tenant_id: str, credential_key: str, display_name: str, credential_type: str,
                       plaintext_value: str, *, integration_id: Optional[str] = None,
                       created_by: Optional[str] = None) -> Tuple[Dict, bool]:
        """Idempotent create — used by Smart Setup so repeated analysis
        of the same input never creates a duplicate credential row.
        Returns (metadata, created_bool)."""
        existing = self.get_metadata(tenant_id, credential_key)
        if existing:
            return existing, False
        return self.create(tenant_id, credential_key, display_name, credential_type, plaintext_value,
                            integration_id=integration_id, created_by=created_by), True

    def update_value(self, tenant_id: str, credential_key: str, plaintext_value: str, *,
                      updated_by: Optional[str] = None) -> Dict:
        return self._rotate_impl(tenant_id, credential_key, plaintext_value, updated_by, event="credential_rotated")

    def rotate(self, tenant_id: str, credential_key: str, new_plaintext_value: str, *,
               updated_by: Optional[str] = None) -> Dict:
        """Replaces the encrypted value while keeping the same
        credential_key — Business Actions referencing this credential
        need NO changes after rotation. Old plaintext is never retained."""
        return self._rotate_impl(tenant_id, credential_key, new_plaintext_value, updated_by, event="credential_rotated")

    def _rotate_impl(self, tenant_id, credential_key, plaintext_value, updated_by, event):
        row = self._get_row(tenant_id, credential_key)
        if not row:
            raise ValueError("credential not found")
        encrypted = encrypt_value(plaintext_value)
        now = datetime.now(timezone.utc).isoformat()
        updated = self._sb.table("integration_credentials").update({
            "encrypted_value": encrypted, "masked_preview": mask_value(plaintext_value),
            "rotated_at": now, "updated_by": updated_by,
        }).eq("id", row["id"]).execute().data[0]
        _audit(self._sb, tenant_id, credential_key, event, updated_by)
        return _to_metadata(updated)

    def set_status(self, tenant_id: str, credential_key: str, status: str, *, updated_by: Optional[str] = None) -> Dict:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        row = self._get_row(tenant_id, credential_key)
        if not row:
            raise ValueError("credential not found")
        updated = self._sb.table("integration_credentials").update({
            "status": status, "updated_by": updated_by,
        }).eq("id", row["id"]).execute().data[0]
        _audit(self._sb, tenant_id, credential_key, f"credential_{status}" if status != "enabled" else "credential_enabled", updated_by)
        return _to_metadata(updated)

    def delete_if_unused(self, tenant_id: str, credential_key: str, *, in_use_checker) -> bool:
        """Hard-deletes only when `in_use_checker(credential_key)`
        returns False (the caller checks the Business Action Registry
        for any parameter still referencing this credential_ref)."""
        if in_use_checker(credential_key):
            raise ValueError("credential is still referenced by one or more Business Actions")
        row = self._get_row(tenant_id, credential_key)
        if not row:
            return False
        self._sb.table("integration_credentials").delete().eq("id", row["id"]).execute()
        return True

    # ── Runtime resolution (Generic Action Executor only) ────────────

    def resolve(self, tenant_id: str, credential_key: str) -> Dict:
        """Decrypts and returns {"ok": bool, "value": str|None,
        "error": str|None} — the ONLY place a real credential value is
        read outside of create/rotate. Callers must keep the value in
        memory only, never log/persist it, and use it immediately to
        build the outgoing request. Updates last_used_at on success."""
        row = self._get_row(tenant_id, credential_key)
        if not row:
            return {"ok": False, "value": None, "error": "credential_not_found"}
        if row.get("status") == "disabled":
            return {"ok": False, "value": None, "error": "credential_disabled"}
        if row.get("status") == "revoked":
            return {"ok": False, "value": None, "error": "credential_revoked"}
        try:
            value = decrypt_value(row["encrypted_value"])
        except MasterKeyMissingError:
            return {"ok": False, "value": None, "error": "encryption_key_unavailable"}
        except MasterKeyInvalidError:
            return {"ok": False, "value": None, "error": "encryption_key_invalid"}
        try:
            self._sb.table("integration_credentials").update({
                "last_used_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", row["id"]).execute()
        except Exception:
            pass
        _audit(self._sb, tenant_id, credential_key, "credential_used")
        return {"ok": True, "value": value, "error": None}

    def test_reference(self, tenant_id: str, credential_key: str) -> Dict:
        """Confirms a credential resolves WITHOUT ever returning the
        value — only ok/error/masked_preview, safe for an Admin API."""
        outcome = self.resolve(tenant_id, credential_key)
        metadata = self.get_metadata(tenant_id, credential_key)
        _audit(self._sb, tenant_id, credential_key, "credential_tested")
        return {"ok": outcome["ok"], "error": outcome["error"],
                "masked_preview": metadata["masked_preview"] if metadata else None}


_instance: Optional[CredentialStore] = None


def get_credential_store(sb) -> CredentialStore:
    global _instance
    if _instance is None or _instance._sb is not sb:
        _instance = CredentialStore(sb)
    return _instance


# ── Suggested credential_key / display_name helpers (deterministic, no LLM) ──

def suggest_credential_key(param_name: str, integration_hint: str = "") -> str:
    base = re.sub(r"[^A-Za-z0-9]+", "_", f"{integration_hint}_{param_name}".strip("_")).strip("_").lower()
    return base or "credential"


def suggest_display_name(param_name: str, integration_hint: str = "") -> str:
    parts = [p for p in (integration_hint, param_name) if p]
    return " ".join(parts) or param_name
