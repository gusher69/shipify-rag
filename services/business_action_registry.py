"""Business Action Center — Registry Service. Platform-first, config-
driven registry of "Business Actions" (RAG / API / Tool / Workflow /
Notification / Human Handoff / Webhook). This is a REGISTRY only — no
execution routing, no AI Middleware / Decision Engine integration, and
no customer-specific business logic lives here. A future Decision
Engine must talk to this registry EXCLUSIVELY (registry.get/list/
search/enabled_actions/find_by_category) and must never know a
specific API endpoint, ERP vendor, or hardcoded workflow.

Never touches: knowledge_chunks, retrieval, rag/*, services/
prompt_builder.py, services/policy_engine.py, services/
benchmark_service.py, services/slot_filling_engine.py. This module is
purely additive, same architectural posture as services/
benchmark_service.py and services/validation_service.py.

Database (migrations/026_business_action_center.sql):
    business_actions, business_action_examples,
    business_action_parameters, business_action_execution,
    business_action_response_mapping, business_action_validation,
    business_action_tags, business_action_embeddings
"""
import os
import re
from typing import Dict, List, Optional
from urllib.parse import urlparse

ACTION_TYPES = ("RAG", "API", "TOOL", "WORKFLOW", "NOTIFICATION", "HUMAN_HANDOFF", "WEBHOOK")
HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
AUTH_TYPES = ("none", "bearer", "api_key", "custom_header", "oauth")
VALIDATION_TYPES = ("regex", "length", "checksum", "external")
CONTENT_TYPES = ("application/json", "application/x-www-form-urlencoded")

# Input Source — where a parameter's VALUE comes from at execution
# time. "secret_configuration" is resolved server-side from an
# environment variable (see resolve_secret_ref) and is NEVER requested
# from the customer, never logged, never shown in Developer Mode.
INPUT_SOURCES = ("customer_message", "customer_profile", "conversation_context",
                  "fixed_configuration", "secret_configuration", "system_generated", "credential_store")

# Parameter Group rules — reusable by ANY future action (login API
# email-OR-phone, order API order-OR-tracking-number, claim API
# claim-OR-policy-number, ...), never hardcoded to one specific action.
GROUP_RULES = ("ALL", "AT_LEAST_ONE", "EXACTLY_ONE", "OPTIONAL")

# Fields whose VALUES must never be echoed back verbatim by the API or
# shown unmasked in the UI — Security requirement. Applied wherever a
# `business_action_execution` row (or its embedded `auth_config`/
# `headers`) is returned to a caller.
_SECRET_KEY_PATTERN = re.compile(r"(token|secret|key|password|authorization|api_key|bearer)", re.IGNORECASE)


def mask_secret(value: Optional[str]) -> str:
    """Same masking shape used everywhere secrets are displayed — keeps
    only the last 4 characters visible, e.g. 'sk-live-...abcd'."""
    if not value:
        return ""
    value = str(value)
    if len(value) <= 4:
        return "****"
    return f"****{value[-4:]}"


def mask_execution_secrets(execution: Optional[Dict]) -> Optional[Dict]:
    """Returns a COPY of `execution` with every secret-shaped value in
    `headers` and `auth_config` masked — never mutates the input, never
    used for anything execution actually needs (only for API/UI
    responses). A field name is treated as secret if it matches
    _SECRET_KEY_PATTERN (token/secret/key/password/authorization/etc)."""
    if not execution:
        return execution
    masked = dict(execution)
    for dict_field in ("headers", "auth_config"):
        original = masked.get(dict_field) or {}
        masked[dict_field] = {
            k: (mask_secret(v) if _SECRET_KEY_PATTERN.search(k) else v)
            for k, v in original.items()
        }
    return masked


_PII_KEY_PATTERN = re.compile(r"(email|phone|mobile|tel)", re.IGNORECASE)
_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_PHONE_RE = re.compile(r"\b0\d{8,9}\b")


def generate_action_key(name: str) -> str:
    """Simple Mode never asks an ordinary administrator to invent an
    Action ID — it is generated from Action Name (lowercase, spaces/
    punctuation -> underscore). Advanced Mode may still edit it
    afterward. Reused by both the Admin UI (client-side, for instant
    preview) and here (server-side, authoritative)."""
    slug = re.sub(r"[^a-z0-9]+", "_", (name or "").strip().lower()).strip("_")
    return slug or "action"


def resolve_secret_ref(secret_ref: Optional[str]) -> Optional[str]:
    """The ONLY place a secret's real VALUE ever enters this system —
    reads an environment variable by NAME (secret_ref). The database
    never stores a real secret value, only this reference name (see
    business_action_parameters.secret_ref / migration 027). Returns None
    if the env var isn't set — callers must treat that as "Secret
    Configuration missing," never fall back to a hardcoded default."""
    if not secret_ref:
        return None
    return os.environ.get(secret_ref)


def sanitize_for_preview(value) -> str:
    """Generic PII-safe display value for request/response previews —
    masks anything that looks like an email or a Thai mobile number,
    regardless of which field name it came from (defense in depth on
    top of the explicit visible_to_customer/visible_in_developer_mode/
    loggable parameter flags)."""
    if value is None:
        return value
    text = str(value)
    text = _EMAIL_RE.sub(lambda m: mask_secret(m.group(0)), text)
    text = _PHONE_RE.sub(lambda m: mask_secret(m.group(0)), text)
    return text


def sanitize_response_body(body):
    """Recursively masks values under PII-shaped keys (email/phone/
    mobile/tel) in a parsed JSON response — used for Developer Mode /
    Test Action display and for anything written to logs. Never mutates
    the input; never persists the result anywhere (Test Action does not
    write to the database)."""
    if isinstance(body, dict):
        out = {}
        for k, v in body.items():
            if _PII_KEY_PATTERN.search(str(k)) and isinstance(v, str):
                out[k] = mask_secret(v)
            else:
                out[k] = sanitize_response_body(v)
        return out
    if isinstance(body, list):
        return [sanitize_response_body(v) for v in body]
    if isinstance(body, str):
        return sanitize_for_preview(body)
    return body


def validate_parameter_groups(action: Dict, parameters: List[Dict], provided: Dict[str, str]) -> Dict:
    """Contextual validation BEFORE execution — never a redesign of the
    Information Collection Engine's own Contextual Slot Binding (that
    module is untouched); this only checks whether enough of THIS
    action's parameters are present to make the call at all.

    `provided` should contain only customer-visible/context-sourced
    parameter values — secret_configuration parameters are resolved
    separately via resolve_secret_ref() and must never appear here.

    Returns {"ok": bool, "missing_required": [...], "failed_groups": [{"name":..., "rule":..., "members":...}]}.
    """
    groups = action.get("parameter_groups") or []
    grouped_names = {m for g in groups for m in (g.get("members") or [])}

    missing_required = [
        p["name"] for p in parameters
        if p.get("required") and p.get("input_source") not in ("secret_configuration", "system_generated")
        and p["name"] not in grouped_names and not provided.get(p["name"])
    ]

    failed_groups = []
    for g in groups:
        members = g.get("members") or []
        present = [m for m in members if provided.get(m)]
        rule = g.get("rule", "AT_LEAST_ONE")
        if rule == "ALL":
            ok = len(present) == len(members)
        elif rule == "AT_LEAST_ONE":
            ok = len(present) >= 1
        elif rule == "EXACTLY_ONE":
            ok = len(present) == 1
        else:  # OPTIONAL
            ok = True
        if not ok:
            failed_groups.append({"name": g.get("name"), "rule": rule, "members": members})

    return {"ok": not missing_required and not failed_groups,
            "missing_required": missing_required, "failed_groups": failed_groups}


def build_embedding_source_text(action: Dict, examples: Optional[List[Dict]] = None,
                                 tags: Optional[List[str]] = None) -> str:
    """Action Embedding Preparation — concatenates every field the task
    spec names as an embedding source (Action Name, Description, AI
    Description, Example Questions, Trigger Examples, Keywords) into one
    text blob. Does NOT call an embedding provider or compute a vector —
    "prepare the architecture only," per the task's own instruction; a
    future Decision Engine task computes embedding_source_text ->
    business_action_embeddings.embedding."""
    parts = [action.get("name") or "", action.get("description") or "", action.get("ai_description") or "",
             action.get("business_description") or ""]
    for ex in (examples or []):
        if ex.get("example_text"):
            parts.append(ex["example_text"])
    for tag in (tags or []):
        parts.append(tag)
    keywords = action.get("search_keywords") or []
    parts.extend(keywords)
    return "\n".join(p for p in parts if p)


class _EmptyResult:
    """Stand-in for a PostgREST APIResponse when a resilient Registry read
    fails after its one retry — shaped just enough (``.data``) for the
    lookup callers, which then behave as "not found"."""
    data: List = []


class BusinessActionRegistry:
    """The ONLY interface a future Decision Engine may use. Every method
    is read-only with respect to execution — it returns configuration,
    never calls an ERP/API/webhook itself (no execution routing in this
    task, per spec)."""

    def __init__(self, sb):
        self._sb = sb

    def _resilient_read(self, build):
        """Run ``build(sb).execute()`` for a Registry lookup that sits on
        the live LINE message path (get / get_by_key / list — reached via
        DecisionEngine._resolve_continuation_action on nearly every turn).

        On a Supabase transport/timeout error — a stale/half-open
        keep-alive connection, the exact production hang of 2026-08-31 —
        drop the shared client, rebuild it, and retry ONCE. If it still
        fails, return an empty result (``.data == []``): every caller
        here already treats that as "not found", and the DecisionEngine
        then degrades to normal routing instead of the worker blocking
        for minutes or a raw DB error reaching a LINE customer.

        Bounded: 1 original attempt + 1 recovery retry. No retry storm.
        Read-only — write methods keep their existing semantics."""
        import httpx
        try:
            return build(self._sb).execute()
        except (httpx.TransportError, httpx.TimeoutException) as first:
            try:
                from services.supabase_client import get_supabase, reset_supabase
                reset_supabase()
                self._sb = get_supabase()
                return build(self._sb).execute()
            except (httpx.TransportError, httpx.TimeoutException) as second:
                print(f"[business_action_registry] Supabase read unavailable "
                      f"after 1 retry ({second!r}) — degrading to empty result")
                return _EmptyResult()

    # ── Registry lookups (Decision Engine surface) ──────────────────
    def get(self, action_id: str) -> Optional[Dict]:
        rows = self._resilient_read(
            lambda sb: sb.table("business_actions").select("*").eq("id", action_id).is_("deleted_at", "null")
        ).data
        return rows[0] if rows else None

    def get_by_key(self, action_key: str) -> Optional[Dict]:
        rows = self._resilient_read(
            lambda sb: sb.table("business_actions").select("*").eq("action_key", action_key).is_("deleted_at", "null")
        ).data
        return rows[0] if rows else None

    def get_by_key_including_deleted(self, action_key: str) -> Optional[Dict]:
        """Same lookup as get_by_key(), but WITHOUT the deleted_at filter —
        the action_key column's DB unique constraint applies regardless
        of soft-delete state, so a caller that needs to know "will
        INSERTing this key hit the unique constraint?" (duplicate
        detection) must check this, not get_by_key() alone. Using
        get_by_key() for that purpose is exactly the bug that let a raw
        Postgres duplicate-key error leak through the AI Auto Setup save
        endpoint: a soft-deleted row's key is invisible to get_by_key()
        but still occupies the unique index."""
        rows = self._sb.table("business_actions").select("*").eq("action_key", action_key).execute().data
        return rows[0] if rows else None

    def restore(self, action_id: str, *, updated_by: Optional[str] = None) -> Optional[Dict]:
        """Clears deleted_at on a soft-deleted row — used when an admin
        chooses "Update Existing" against a key that collides with a
        previously soft-deleted action, so update() can resurrect and
        overwrite it in one step instead of leaving a dangling deleted
        row indefinitely blocking that key."""
        rows = self._sb.table("business_actions").update(
            {"deleted_at": None, "updated_by": updated_by}).eq("id", action_id).execute().data
        return rows[0] if rows else None

    def list(self) -> List[Dict]:
        return self._resilient_read(
            lambda sb: sb.table("business_actions").select("*").is_("deleted_at", "null")
            .order("priority", desc=True).order("created_at")
        ).data or []

    def search(self, query: str) -> List[Dict]:
        """Deterministic substring search over name/display_name/
        description/ai_description/tags — NOT semantic search (Action
        Embeddings are prepared, not yet used for routing, per spec)."""
        if not query:
            return self.list()
        q = query.strip().lower()
        results = []
        for action in self.list():
            haystack = " ".join(str(action.get(f) or "") for f in
                                 ("name", "display_name", "description", "ai_description", "business_description")).lower()
            if q in haystack:
                results.append(action)
        return results

    def list_with_summary(self) -> List[Dict]:
        """list() plus a small, generic display summary for Admin UI rows —
        required-parameter names/groups and the execution endpoint host
        (never the full URL/secrets). Reusable by any action, not specific
        to one customer integration."""
        actions = self.list()
        for action in actions:
            action_id = action["id"]
            params = self.get_parameters(action_id)
            groups = action.get("parameter_groups") or []
            required_names = [p["name"] for p in params if p.get("required")
                              and p.get("input_source", "customer_message") not in ("secret_configuration", "credential_store")]
            group_summaries = [
                (("อย่างน้อย 1 จาก: " if g.get("rule") == "AT_LEAST_ONE" else "ทั้งหมด: ") + " / ".join(g.get("members") or []))
                for g in groups
            ]
            action["required_parameter_summary"] = "; ".join(group_summaries) if group_summaries else (", ".join(required_names) or None)
            execution = self.get_execution(action_id, mask=True)
            host = None
            if execution and execution.get("endpoint"):
                try:
                    host = urlparse(execution["endpoint"]).netloc or None
                except Exception:
                    host = None
            action["execution_host"] = host
        return actions

    def enabled_actions(self) -> List[Dict]:
        return [a for a in self.list() if a.get("enabled")]

    def find_by_category(self, category: str) -> List[Dict]:
        return [a for a in self.list() if a.get("category") == category]

    def find_by_type(self, action_type: str) -> List[Dict]:
        return [a for a in self.list() if a.get("action_type") == action_type]

    # ── Related records (used by the Admin UI editor + future Decision Engine) ──
    def get_examples(self, action_id: str) -> List[Dict]:
        return self._sb.table("business_action_examples").select("*").eq("action_id", action_id) \
            .order("sort_order").execute().data or []

    def get_parameters(self, action_id: str) -> List[Dict]:
        return self._sb.table("business_action_parameters").select("*").eq("action_id", action_id) \
            .order("sort_order").execute().data or []

    def get_execution(self, action_id: str, *, mask: bool = True) -> Optional[Dict]:
        rows = self._sb.table("business_action_execution").select("*").eq("action_id", action_id).execute().data
        execution = rows[0] if rows else None
        return mask_execution_secrets(execution) if mask else execution

    def get_response_mapping(self, action_id: str) -> List[Dict]:
        return self._sb.table("business_action_response_mapping").select("*").eq("action_id", action_id) \
            .order("sort_order").execute().data or []

    def get_validation_rules(self, action_id: str) -> List[Dict]:
        return self._sb.table("business_action_validation").select("*").eq("action_id", action_id).execute().data or []

    def get_tags(self, action_id: str) -> List[str]:
        rows = self._sb.table("business_action_tags").select("tag").eq("action_id", action_id).execute().data or []
        return [r["tag"] for r in rows]

    def get_full(self, action_id: str, *, mask_secrets: bool = True) -> Optional[Dict]:
        """Everything the Action Editor / Test Action page needs in one
        call — action + examples + parameters + execution (masked by
        default) + response mapping + validation + tags."""
        action = self.get(action_id)
        if not action:
            return None
        return {
            **action,
            "examples": self.get_examples(action_id),
            "parameters": self.get_parameters(action_id),
            "execution": self.get_execution(action_id, mask=mask_secrets),
            "response_mapping": self.get_response_mapping(action_id),
            "validation_rules": self.get_validation_rules(action_id),
            "tags": self.get_tags(action_id),
        }

    # ── CRUD (Admin UI surface) ──────────────────────────────────────
    def create(self, payload: Dict, *, created_by: Optional[str] = None) -> Dict:
        row = {k: v for k, v in payload.items() if k in _ACTION_FIELDS}
        row.setdefault("action_type", "TOOL")
        row.setdefault("enabled", True)
        if row["action_type"] not in ACTION_TYPES:
            raise ValueError(f"action_type must be one of {ACTION_TYPES}")
        row["created_by"] = created_by
        row["updated_by"] = created_by
        return self._sb.table("business_actions").insert(row).execute().data[0]

    def update(self, action_id: str, payload: Dict, *, updated_by: Optional[str] = None) -> Optional[Dict]:
        row = {k: v for k, v in payload.items() if k in _ACTION_FIELDS}
        if row.get("action_type") and row["action_type"] not in ACTION_TYPES:
            raise ValueError(f"action_type must be one of {ACTION_TYPES}")
        row["updated_by"] = updated_by
        row["version"] = payload.get("version")  # caller passes current version + 1 if bumping; see routes.py
        row = {k: v for k, v in row.items() if v is not None}
        rows = self._sb.table("business_actions").update(row).eq("id", action_id).execute().data
        return rows[0] if rows else None

    def delete(self, action_id: str, *, hard: bool = False) -> bool:
        """DEPRECATED soft-delete path — kept only for any caller that
        still explicitly wants the old deleted_at-timestamp behavior.
        The normal admin "Delete" action now goes through
        hard_delete_action() below (2026-07-29 rework: soft delete left
        a dead row still occupying its action_key forever at the DB's
        unique-constraint level — see the getdatacustomer incident).
        hard=True here is the same one-statement cascade delete
        hard_delete_action() uses, minus its fixture guard/verification."""
        if hard:
            self._sb.table("business_actions").delete().eq("id", action_id).execute()
        else:
            from datetime import datetime, timezone
            self._sb.table("business_actions").update({"deleted_at": datetime.now(timezone.utc).isoformat()}) \
                .eq("id", action_id).execute()
        return True

    # Dependent tables an action's own row cascades to (migrations 026/
    # 031/033 — all declared `ON DELETE CASCADE REFERENCES
    # business_actions(id)`). A single DELETE on business_actions is
    # therefore ALREADY one atomic Postgres statement covering every one
    # of these — true rollback-on-failure is provided by the database
    # engine itself, not a manual client-side multi-step transaction
    # (which the Supabase REST layer doesn't expose anyway). This list
    # exists so hard_delete_action() can (a) report what it removed and
    # (b) defensively verify nothing was left behind, rather than to
    # implement the atomicity itself.
    _DEPENDENT_TABLES = (
        "business_action_parameters", "business_action_execution", "business_action_examples",
        "business_action_response_mapping", "business_action_tags", "business_action_validation",
        "business_action_embeddings", "integration_action_schemas", "erp_test_cases",
    )

    # Categories that must never be hard-deleted without an explicit
    # force=True — system/demo fixtures other features' tests or live
    # verification depend on (see scripts/create_generic_runtime_fixtures.py).
    _PROTECTED_CATEGORIES = ("Internal Test Fixtures",)

    def is_protected_fixture(self, action: Dict) -> bool:
        return (action.get("category") or "") in self._PROTECTED_CATEGORIES

    def dependent_record_counts(self, action_id: str) -> Dict[str, int]:
        counts = {}
        for table in self._DEPENDENT_TABLES:
            try:
                res = self._sb.table(table).select("id", count="exact").eq("action_id", action_id).execute()
                counts[table] = res.count or 0
            except Exception:
                counts[table] = 0
        return counts

    def hard_delete_action(self, action_id: str, *, force: bool = False, actor: Optional[str] = None) -> Dict:
        """Permanently removes a Business Action AND every dependent row
        (see _DEPENDENT_TABLES) — the action_key becomes immediately
        reusable, no recycle bin, no deleted_at row left behind.

        Raises ValueError with a short, stable reason code (never a raw
        DB exception) on:
          - "action_not_found"
          - "protected_fixture" (unless force=True)
        Raises RuntimeError("orphan_records_after_delete: {...}") if,
        after the delete, any dependent table still has rows for this
        action_id — this should be structurally impossible given the
        ON DELETE CASCADE FKs, but is verified explicitly rather than
        assumed, per the "never leave partial orphan records" rule.

        Writes an immutable business_action_audit_log row (event
        "business_action.deleted", migrations/034) AFTER the delete
        succeeds — audit logging is best-effort and NEVER reverses an
        already-completed deletion if it fails, but a failure is
        surfaced via `audit_log_warning` in the return value rather than
        silently swallowed (stronger than the credential_store.py
        precedent this otherwise mirrors), so the caller/admin can see
        it happened. `actor` should be the identity of whoever triggered
        the delete (e.g. the admin username) — never required, but
        omitted from the audit event as None when not supplied.

        Returns {"ok": True, "action_id", "action_key", "removed_dependent_counts",
        "audit_log_warning": Optional[str]}."""
        action = self.get(action_id)
        if not action:
            raise ValueError("action_not_found")
        if not force and self.is_protected_fixture(action):
            raise ValueError("protected_fixture")

        removed_counts = self.dependent_record_counts(action_id)
        previous_status = "draft" if action.get("is_draft") else ("published" if action.get("enabled") else "disabled")
        had_execution_history = removed_counts.get("erp_test_cases", 0) > 0

        rows = self._sb.table("business_actions").delete().eq("id", action_id).execute().data
        if not rows:
            raise ValueError("action_not_found")

        orphans = {t: c for t, c in self.dependent_record_counts(action_id).items() if c > 0}
        if orphans:
            raise RuntimeError(f"orphan_records_after_delete: {orphans}")

        audit_log_warning = None
        try:
            self._sb.table("business_action_audit_log").insert({
                "action_id": action_id, "action_key": action.get("action_key"),
                "event": "business_action.deleted", "actor": actor,
                "detail": {
                    "display_name": action.get("display_name"),
                    "previous_status": previous_status,
                    "had_execution_history": had_execution_history,
                    "dependent_record_counts": removed_counts,
                    "deletion_result": "ok",
                    "forced_past_protection": bool(force),
                },
            }).execute()
        except Exception as e:
            audit_log_warning = f"Deletion succeeded but the audit-log write failed: {e}"

        return {"ok": True, "action_id": action_id, "action_key": action.get("action_key"),
                "removed_dependent_counts": removed_counts, "audit_log_warning": audit_log_warning}

    def set_enabled(self, action_id: str, enabled: bool) -> Optional[Dict]:
        rows = self._sb.table("business_actions").update({"enabled": enabled}).eq("id", action_id).execute().data
        return rows[0] if rows else None

    def duplicate(self, action_id: str, *, created_by: Optional[str] = None) -> Optional[Dict]:
        src = self.get_full(action_id, mask_secrets=False)
        if not src:
            return None
        new_key = f"{src['action_key']}_copy"
        suffix = 2
        while self.get_by_key(new_key):
            new_key = f"{src['action_key']}_copy{suffix}"
            suffix += 1
        new_action = self.create({**{k: src[k] for k in _ACTION_FIELDS if k in src},
                                   "action_key": new_key, "name": f"{src['name']} (Copy)", "enabled": False},
                                  created_by=created_by)
        new_id = new_action["id"]
        for ex in src["examples"]:
            self._sb.table("business_action_examples").insert(
                {"action_id": new_id, "example_type": ex["example_type"], "example_text": ex["example_text"],
                 "sort_order": ex["sort_order"]}).execute()
        for p in src["parameters"]:
            self._sb.table("business_action_parameters").insert(
                {"action_id": new_id, "name": p["name"], "display_name": p.get("display_name"),
                 "required": p["required"], "validation_type": p.get("validation_type"),
                 "example_value": p.get("example_value"), "description": p.get("description"),
                 "slot_type": p.get("slot_type"), "sort_order": p["sort_order"]}).execute()
        if src.get("execution"):
            ex = src["execution"]
            self._sb.table("business_action_execution").insert(
                {"action_id": new_id, "execution_target": ex.get("execution_target"), "endpoint": ex.get("endpoint"),
                 "http_method": ex.get("http_method"), "headers": ex.get("headers"), "auth_type": ex.get("auth_type"),
                 "auth_config": ex.get("auth_config"), "timeout_seconds": ex.get("timeout_seconds"),
                 "retry_policy": ex.get("retry_policy")}).execute()
        for m in src["response_mapping"]:
            self._sb.table("business_action_response_mapping").insert(
                {"action_id": new_id, "json_path": m["json_path"], "mapped_label": m["mapped_label"],
                 "sort_order": m["sort_order"]}).execute()
        for tag in src["tags"]:
            self._sb.table("business_action_tags").insert({"action_id": new_id, "tag": tag}).execute()
        return self.get(new_id)

    def upsert_execution(self, action_id: str, execution: Dict) -> Dict:
        payload = {k: v for k, v in execution.items() if k in _EXECUTION_FIELDS}
        payload["action_id"] = action_id
        if payload.get("http_method") and payload["http_method"] not in HTTP_METHODS:
            raise ValueError(f"http_method must be one of {HTTP_METHODS}")
        if payload.get("auth_type") and payload["auth_type"] not in AUTH_TYPES:
            raise ValueError(f"auth_type must be one of {AUTH_TYPES}")
        if payload.get("content_type") and payload["content_type"] not in CONTENT_TYPES:
            raise ValueError(f"content_type must be one of {CONTENT_TYPES}")
        # Simple Mode edits Base URL + Endpoint Path separately; `endpoint`
        # (the full resolved URL, migration 026) is recomputed here so
        # every existing execution call site that reads `endpoint`
        # continues to work unchanged.
        if payload.get("base_url") is not None or payload.get("endpoint_path") is not None:
            existing_rows = self._sb.table("business_action_execution").select("*").eq("action_id", action_id).execute().data
            existing_row = existing_rows[0] if existing_rows else {}
            base_url = payload.get("base_url", existing_row.get("base_url")) or ""
            endpoint_path = payload.get("endpoint_path", existing_row.get("endpoint_path")) or ""
            if base_url:
                payload["endpoint"] = base_url.rstrip("/") + "/" + endpoint_path.lstrip("/")
        existing = self._sb.table("business_action_execution").select("id").eq("action_id", action_id).execute().data
        if existing:
            return self._sb.table("business_action_execution").update(payload).eq("action_id", action_id).execute().data[0]
        return self._sb.table("business_action_execution").insert(payload).execute().data[0]

    def validate_can_execute(self, action_id: str, provided: Dict[str, str]) -> Dict:
        """Contextual pre-execution check (Part: PARAMETER GROUP
        SUPPORT) — `provided` must contain ONLY customer/context-sourced
        values; secret_configuration parameters are resolved separately
        via resolve_secret_ref() and are never part of `provided`."""
        action = self.get(action_id)
        if not action:
            return {"ok": False, "missing_required": [], "failed_groups": [], "error": "action_not_found"}
        parameters = self.get_parameters(action_id)
        return validate_parameter_groups(action, parameters, provided)

    def validate_can_enable(self, action_id: str, *, tenant_id: Optional[str] = None) -> Dict:
        """Draft vs. Enabled gate (AI Auto Setup) — an incomplete AI
        suggestion must never be enabled by accident. Checks structural
        completeness only (never calls the network); a passing Test
        Action is checked separately by the caller via `test_payload`/
        `debug_notes` bookkeeping, not by this function."""
        from config import DEFAULT_TENANT_ID
        tenant_id = tenant_id or DEFAULT_TENANT_ID
        full = self.get_full(action_id, mask_secrets=True)
        reasons = []
        if not full:
            return {"ok": False, "reasons": ["action_not_found"]}
        execution = full.get("execution") or {}
        if full.get("action_type") in ("API", "WEBHOOK"):
            if not execution.get("endpoint") and not (execution.get("base_url") and execution.get("endpoint_path")):
                reasons.append("missing_endpoint")
            if not execution.get("http_method"):
                reasons.append("missing_http_method")
        parameters = full.get("parameters") or []
        seen_in_group = {m for g in (full.get("parameter_groups") or []) for m in (g.get("members") or [])}
        ungrouped_required = [p for p in parameters if p.get("required") and p["name"] not in seen_in_group]
        credential_store = None
        for p in parameters:
            if not p.get("name"):
                reasons.append("required_parameter_missing_name")
            if p.get("input_source") == "secret_configuration" and not p.get("secret_ref"):
                reasons.append(f"secret_parameter_missing_secret_ref:{p.get('name') or '?'}")
            if p.get("input_source") == "credential_store":
                if not p.get("credential_ref"):
                    reasons.append(f"credential_parameter_missing_credential_ref:{p.get('name') or '?'}")
                else:
                    if credential_store is None:
                        from services.credential_store import get_credential_store
                        credential_store = get_credential_store(self._sb)
                    meta = credential_store.get_metadata(tenant_id, p["credential_ref"])
                    if not meta:
                        reasons.append(f"credential_not_found:{p['credential_ref']}")
                    elif meta.get("status") == "disabled":
                        reasons.append(f"credential_disabled:{p['credential_ref']}")
                    elif meta.get("status") == "revoked":
                        reasons.append(f"credential_revoked:{p['credential_ref']}")
        askable_required = [p for p in ungrouped_required
                             if p.get("input_source") not in ("secret_configuration", "credential_store")]
        for p in askable_required:
            if not (p.get("follow_up_options") or p.get("description")):
                reasons.append(f"missing_follow_up_question:{p['name']}")
        low_confidence_names = [p["name"] for p in askable_required if p.get("validation_confidence") == "low"]
        if len(askable_required) > 1 and low_confidence_names:
            reasons.append("ambiguous_validation_for_multiple_required_fields")

        # Smart Capability Setup: per-type Draft/Enable rules for the
        # non-API/WEBHOOK types, which don't have parameters/execution
        # at all — their completeness lives in `setup_metadata`.
        action_type = full.get("action_type")
        metadata = full.get("setup_metadata") or {}
        if action_type == "RAG" and not metadata.get("knowledge_scope"):
            reasons.append("missing_knowledge_scope")
        elif action_type == "TOOL" and not metadata.get("tool_name"):
            reasons.append("missing_tool_name")
        elif action_type == "HUMAN_HANDOFF" and not (metadata.get("triggers") or []):
            reasons.append("missing_trigger")
        elif action_type == "NOTIFICATION" and not (metadata.get("triggers") or []):
            reasons.append("missing_trigger")
        elif action_type == "WORKFLOW" and not (metadata.get("workflow_steps") or []):
            reasons.append("missing_workflow_step")
        return {"ok": not reasons, "reasons": reasons}

    def resolve_secret_parameters(self, action_id: str, *, tenant_id: Optional[str] = None) -> Dict[str, Optional[str]]:
        """Resolves every secret-sourced parameter to its real value —
        the ONLY method in this module that ever touches a real secret/
        credential value. Resolution priority per parameter:
          1. input_source == "credential_store"  -> CredentialStore.resolve()
          2. input_source == "secret_configuration" (legacy)  -> env var
          3. neither resolves -> None (caller reports a structured
             missing-credential error; see services/action_executor.py)
        Returns {parameter_name: value_or_None}."""
        from config import DEFAULT_TENANT_ID
        tenant_id = tenant_id or DEFAULT_TENANT_ID
        parameters = self.get_parameters(action_id)
        result: Dict[str, Optional[str]] = {}
        credential_store = None
        for p in parameters:
            source = p.get("input_source")
            if source not in ("credential_store", "secret_configuration"):
                continue
            value = None
            if source == "credential_store" and p.get("credential_ref"):
                if credential_store is None:
                    from services.credential_store import get_credential_store
                    credential_store = get_credential_store(self._sb)
                outcome = credential_store.resolve(tenant_id, p["credential_ref"])
                value = outcome["value"] if outcome["ok"] else None
            # Fallback (priority #2): a parameter can carry BOTH a
            # credential_ref and a legacy secret_ref during migration —
            # if credential_store resolution didn't succeed, fall back
            # to the environment-variable mode before giving up.
            if value is None and p.get("secret_ref"):
                value = resolve_secret_ref(p["secret_ref"])
            result[p["name"]] = value
        return result

    def resolve_secret_parameter_errors(self, action_id: str, *, tenant_id: Optional[str] = None) -> Dict[str, str]:
        """Structured error per secret-sourced parameter that failed to
        resolve — e.g. {"SecretCode": "credential_disabled"}. Used by
        the Generic Action Executor to report WHY, not just THAT, a
        secret/credential didn't resolve."""
        from config import DEFAULT_TENANT_ID
        tenant_id = tenant_id or DEFAULT_TENANT_ID
        parameters = self.get_parameters(action_id)
        errors: Dict[str, str] = {}
        credential_store = None
        for p in parameters:
            source = p.get("input_source")
            if source not in ("credential_store", "secret_configuration"):
                continue

            if source == "secret_configuration":
                if resolve_secret_ref(p.get("secret_ref")) is None:
                    errors[p["name"]] = "environment_variable_not_set"
                continue

            # source == "credential_store"
            if not p.get("credential_ref"):
                credential_error = "credential_ref_not_configured"
            else:
                if credential_store is None:
                    from services.credential_store import get_credential_store
                    credential_store = get_credential_store(self._sb)
                outcome = credential_store.resolve(tenant_id, p["credential_ref"])
                credential_error = None if outcome["ok"] else (outcome["error"] or "credential_unresolved")
            if credential_error is None:
                continue  # resolved fine via credential_store
            # Fallback (priority #2): only a real error if the legacy
            # secret_ref path ALSO fails.
            if p.get("secret_ref") and resolve_secret_ref(p["secret_ref"]) is not None:
                continue
            errors[p["name"]] = credential_error
        return errors

    def replace_examples(self, action_id: str, examples: List[Dict]) -> List[Dict]:
        self._sb.table("business_action_examples").delete().eq("action_id", action_id).execute()
        out = []
        for i, ex in enumerate(examples):
            row = self._sb.table("business_action_examples").insert(
                {"action_id": action_id, "example_type": ex.get("example_type", "question"),
                 "example_text": ex["example_text"], "sort_order": ex.get("sort_order", i)}).execute().data[0]
            out.append(row)
        return out

    def replace_parameters(self, action_id: str, parameters: List[Dict]) -> List[Dict]:
        self._sb.table("business_action_parameters").delete().eq("action_id", action_id).execute()
        out = []
        for i, p in enumerate(parameters):
            if p.get("input_source") and p["input_source"] not in INPUT_SOURCES:
                raise ValueError(f"input_source must be one of {INPUT_SOURCES}")
            row = self._sb.table("business_action_parameters").insert(
                {"action_id": action_id, "name": p["name"], "display_name": p.get("display_name"),
                 "required": p.get("required", True), "validation_type": p.get("validation_type"),
                 "example_value": p.get("example_value"), "description": p.get("description"),
                 "slot_type": p.get("slot_type"), "sort_order": p.get("sort_order", i),
                 "input_source": p.get("input_source", "customer_message"),
                 "secret_ref": p.get("secret_ref"), "credential_ref": p.get("credential_ref"),
                 "send_as": p.get("send_as", "form"),
                 "visible_to_customer": p.get("visible_to_customer", p.get("input_source") not in ("secret_configuration", "credential_store")),
                 "visible_in_developer_mode": p.get("visible_in_developer_mode", p.get("input_source") not in ("secret_configuration", "credential_store")),
                 "loggable": p.get("loggable", p.get("input_source") not in ("secret_configuration", "credential_store")),
                 "validation_pattern": p.get("validation_pattern"),
                 "min_length": p.get("min_length"), "max_length": p.get("max_length"),
                 "follow_up_options": p.get("follow_up_options") or [],
                 # NOT a plain .get(..., "high") default — the AI legitimately
                 # returns validation_confidence=None for secret/credential_store
                 # parameters (they have no customer-facing validation rule),
                 # and .get()'s default only applies when the key is MISSING,
                 # not when it's present with value None. Without this `or`,
                 # that None reaches the DB and violates the column's NOT NULL
                 # constraint, silently failing every save of a detected secret.
                 "validation_confidence": p.get("validation_confidence") or "high",
                 "field_metadata": p.get("field_metadata") or {},
                 }).execute().data[0]
            out.append(row)
        return out

    def set_parameter_groups(self, action_id: str, groups: List[Dict]) -> List[Dict]:
        """Persists `business_actions.parameter_groups` (migration 027) —
        reusable rule groups (ALL/AT_LEAST_ONE/EXACTLY_ONE/OPTIONAL) over
        a set of parameter names. Never specific to any one action;
        e.g. a future Order API's [order_number, tracking_number]
        AT_LEAST_ONE group uses the exact same mechanism."""
        for g in groups:
            if g.get("rule") not in GROUP_RULES:
                raise ValueError(f"group rule must be one of {GROUP_RULES}")
        self._sb.table("business_actions").update({"parameter_groups": groups}).eq("id", action_id).execute()
        return groups

    def replace_response_mapping(self, action_id: str, mapping: List[Dict]) -> List[Dict]:
        self._sb.table("business_action_response_mapping").delete().eq("action_id", action_id).execute()
        out = []
        for i, m in enumerate(mapping):
            row = self._sb.table("business_action_response_mapping").insert(
                {"action_id": action_id, "json_path": m["json_path"], "mapped_label": m["mapped_label"],
                 "sort_order": m.get("sort_order", i), "field_metadata": m.get("field_metadata") or {}}).execute().data[0]
            out.append(row)
        return out

    def replace_validation_rules(self, action_id: str, rules: List[Dict]) -> List[Dict]:
        self._sb.table("business_action_validation").delete().eq("action_id", action_id).execute()
        out = []
        for r in rules:
            if r.get("validation_type") not in VALIDATION_TYPES:
                raise ValueError(f"validation_type must be one of {VALIDATION_TYPES}")
            row = self._sb.table("business_action_validation").insert(
                {"action_id": action_id, "parameter_name": r.get("parameter_name"),
                 "validation_type": r["validation_type"], "rule": r.get("rule") or {}}).execute().data[0]
            out.append(row)
        return out

    def replace_tags(self, action_id: str, tags: List[str]) -> List[str]:
        self._sb.table("business_action_tags").delete().eq("action_id", action_id).execute()
        for tag in tags:
            self._sb.table("business_action_tags").insert({"action_id": action_id, "tag": tag}).execute()
        return tags

    # ── Embedding preparation (architecture only — no vector computed) ──
    def prepare_embedding_source(self, action_id: str) -> Dict:
        action = self.get(action_id)
        if not action:
            raise ValueError("action not found")
        examples = self.get_examples(action_id)
        tags = self.get_tags(action_id)
        source_text = build_embedding_source_text(action, examples, tags)
        existing = self._sb.table("business_action_embeddings").select("id").eq("action_id", action_id).execute().data
        payload = {"action_id": action_id, "embedding_source_text": source_text, "status": "pending"}
        if existing:
            return self._sb.table("business_action_embeddings").update(payload).eq("action_id", action_id).execute().data[0]
        return self._sb.table("business_action_embeddings").insert(payload).execute().data[0]

    # ── Import/Export interfaces (prepared only — no marketplace) ────
    def export_action(self, action_id: str) -> Optional[Dict]:
        return self.get_full(action_id, mask_secrets=True)

    def import_action(self, exported: Dict, *, created_by: Optional[str] = None) -> Dict:
        """Interface prepared per spec ('do not implement marketplace,
        only prepare interfaces') — imports a previously-exported action
        as a new, disabled action so an admin must review/re-enter any
        masked secrets before enabling it."""
        base = {k: exported[k] for k in _ACTION_FIELDS if k in exported}
        base["enabled"] = False
        new_action = self.create(base, created_by=created_by)
        new_id = new_action["id"]
        if exported.get("examples"):
            self.replace_examples(new_id, exported["examples"])
        if exported.get("parameters"):
            self.replace_parameters(new_id, exported["parameters"])
        if exported.get("response_mapping"):
            self.replace_response_mapping(new_id, exported["response_mapping"])
        if exported.get("tags"):
            self.replace_tags(new_id, exported["tags"])
        return self.get(new_id)


_ACTION_FIELDS = (
    "action_key", "name", "display_name", "description", "enabled", "version",
    "action_type", "category", "priority",
    "ai_description", "business_description", "search_keywords",
    "retry_rules", "escalation_rules",
    "success_prompt", "failure_prompt", "follow_up_prompt",
    "test_payload", "debug_notes", "parameter_groups",
    "setup_source", "is_draft", "setup_metadata",
)
_EXECUTION_FIELDS = (
    "execution_target", "endpoint", "http_method", "headers", "auth_type",
    "auth_config", "timeout_seconds", "retry_policy",
    "base_url", "endpoint_path", "content_type",
)

_instance: Optional[BusinessActionRegistry] = None


def get_registry(sb=None) -> BusinessActionRegistry:
    """Named factory, same pattern as services/rag_service.py::
    get_rag_service(). Pass `sb` explicitly (as admin/routes.py does via
    get_sb()) — a module-level singleton is cached only when no sb is
    given, mirroring how other services in this codebase avoid a hidden
    global Supabase client."""
    global _instance
    if sb is not None:
        return BusinessActionRegistry(sb)
    if _instance is None:
        from admin.routes import get_sb
        _instance = BusinessActionRegistry(get_sb())
    return _instance
