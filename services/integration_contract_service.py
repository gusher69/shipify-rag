"""Integration Contract Layer — Platform Service.

A read-only, product-agnostic CONTRACT view over an already-configured
Business Action (services/business_action_registry.py, untouched) plus
its optional Integration Schema (services/integration_schema_service.py,
untouched, the ONE three-layer merge — this module never re-implements
merging, it only reshapes the merge's own output into a stable,
consumer-facing contract shape).

This module never implements the Decision Engine or the Business
Action Matcher, never adds ERP-vs-RAG routing, and never touches the
RAG Playground / Provider architecture / Action Executor / Credential
Store / AI Analysis Pipeline. It is a pure, deterministic transform:
    business_action_registry.get_full()
    + integration_schema_service.get_published_schema()
    -> resolve_effective_integration_schema()
    -> describe_integration() [THIS module normalizes the shape]

`resolve_effective_contract()` wraps `describe_integration()` with
validation (Part 12) and provenance — the richer function; there is
exactly ONE place (below) that builds the contract's field-by-field
content.
"""
from datetime import datetime, timezone
from typing import Dict, List, Optional

CONTRACT_VERSION = "1.1"
RESOLVER_VERSION = "1.0"


# ── Requirement 6 — shared keyed-cache abstraction ──────────────────────
# A single, reusable in-process memoization primitive. Both the pre-
# existing contract cache AND the new Conversation Form cache (Part 17,
# services/conversation_form_generator.py) use exactly THIS class rather
# than each rolling its own bare module-level dict.
class VersionedCache:
    """A tiny keyed memoization cache. The caller is entirely responsible
    for building a key that changes whenever the underlying data does
    (e.g. including action.updated_at / schema_version / resolver_version
    / contract_version) — this class does no invalidation heuristics of
    its own beyond exact key equality."""

    def __init__(self):
        self._store: Dict[tuple, object] = {}

    def get(self, key: tuple, compute_fn):
        if key in self._store:
            return self._store[key]
        value = compute_fn()
        if value is not None:
            self._store[key] = value
        return value

    def invalidate(self, predicate) -> None:
        """`predicate(key) -> bool`; drops every entry for which it's True."""
        self._store = {k: v for k, v in self._store.items() if not predicate(k)}

    def invalidate_all(self) -> None:
        self._store = {}

# Part 9 — the recognized operation-type vocabulary (static, product-
# agnostic; mirrors services/erp_test_harness.py::OPERATION_TYPES,
# reused rather than re-invented).
def list_operation_types() -> List[str]:
    from services import erp_test_harness as h
    return list(h.OPERATION_TYPES)


# Recognized output formatter types (Part 5) — purely a vocabulary
# list for validation, never a per-field hardcode.
FORMATTER_TYPES = ("date", "time", "currency", "percentage", "boolean", "identifier",
                    "url", "phone", "address", "image", "markdown", "status", "text", "number", "list", "link")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Part 4 — Input field normalization ──────────────────────────────────

# Requirement 1 — generic display-metadata keys passed through verbatim
# from the resolved effective schema field onto the contract field, with
# no per-key business logic. Absent in the source schema -> absent here
# (meaning "generate a default" is left to the consumer, e.g.
# services/conversation_form_generator.py).
_DISPLAY_METADATA_KEYS = (
    "display_type", "placeholder", "help_text", "display_order", "group",
    "icon", "color", "examples", "empty_message", "channel_overrides", "options",
    # Conversation Strategy Engine sprint — Part 3's `conversation`
    # priority/detection metadata, passed through with the EXACT same
    # generic mechanism as the display-metadata keys above (no new
    # pass-through pattern).
    "conversation_priority", "ask_first", "skip_if_detected", "auto_fill",
    "suggest_if_missing", "confidence_threshold",
)


def _pass_through_display_metadata(field: Dict) -> Dict:
    return {k: field[k] for k in _DISPLAY_METADATA_KEYS if field.get(k) not in (None, [], {})}


def _normalize_input_field(field: Dict, param: Dict) -> Dict:
    validation = {
        "pattern": field.get("validation_pattern") or param.get("validation_pattern"),
        "type": field.get("semantic_type"),
        "min": param.get("min_length"), "max": param.get("max_length"),
    }
    hidden = param.get("input_source") in ("credential_store", "secret_configuration")
    out = {
        "technical_name": field.get("technical_name") or field.get("name"),
        "business_label": field.get("business_label"),
        "canonical_name": field.get("canonical_name"),
        "semantic_type": field.get("semantic_type"),
        "required": bool(field.get("required")),
        "aliases": list(field.get("aliases") or []),
        "display_labels": dict(field.get("display_labels") or {}),
        "description": param.get("description"),
        "example_value": field.get("example_value"),
        "validation": validation,
        "visibility": "hidden" if hidden else (field.get("visibility") or "customer"),
        "sensitive": bool(hidden),
        "source": param.get("input_source") or "customer_message",
        # Additive (not in the mandated key list, but never dropped per
        # Part 18 Scenario B — a field's own configured follow-up
        # prompts must survive into the contract somewhere).
        "follow_up_prompts": dict(field.get("follow_up_prompts") or {}),
    }
    out.update(_pass_through_display_metadata(field))
    return out


# ── Part 5 — Output field normalization ─────────────────────────────────

_DEFAULT_FORMATTER_BY_SEMANTIC_TYPE = {
    "currency": "currency", "date": "date", "boolean": "boolean", "percentage": "percentage",
    "integer": "number", "array": "list", "url": "url", "address": "address",
    "phone": "phone", "identifier": "identifier", "status": "status",
}


def _normalize_output_field(field: Dict) -> Dict:
    formatting = field.get("formatting") or {}
    formatter_type = formatting.get("type") or _DEFAULT_FORMATTER_BY_SEMANTIC_TYPE.get(field.get("semantic_type"), "text")
    is_collection = field.get("semantic_type") == "array" or bool(field.get("is_collection"))
    out = {
        "technical_path": field.get("technical_path") or field.get("json_path"),
        "business_label": field.get("business_label"),
        "canonical_name": field.get("canonical_name"),
        "semantic_type": field.get("semantic_type"),
        "aliases": list(field.get("aliases") or []),
        "display_labels": dict(field.get("display_labels") or {}),
        "visible": bool(field.get("visible", True)) and field.get("visibility") not in ("hidden", "developer_only"),
        "answerable": bool(field.get("answerable", True)),
        "sensitive": bool(field.get("sensitive")),
        "masking": bool(field.get("mask")),
        "formatter": {"type": formatter_type, "options": formatting.get("options") or {}},
        "example_value": (field.get("example_values") or [None])[0] if field.get("example_values") else None,
        "collection": is_collection,
    }
    out.update(_pass_through_display_metadata(field))
    return out


# ── Part 6 — Conversation normalization ─────────────────────────────────

def _normalize_conversation(conversation: Dict) -> Dict:
    return {
        "collect_parameters": conversation.get("collect_parameters", conversation.get("ask_requested_information", True)),
        "ask_requested_information": bool(conversation.get("ask_requested_information", True)),
        "allow_multiple_fields": bool(conversation.get("allow_multiple_fields", True)),
        "allow_all_fields": bool(conversation.get("allow_all_fields", True)),
        "require_confirmation_before_execute": bool(conversation.get("require_confirmation_before_execute", False)),
        "reuse_previous_result": bool(conversation.get("reuse_last_result", conversation.get("reuse_previous_result", True))),
        "result_ttl_seconds": conversation.get("result_ttl_seconds"),
        "conversation_timeout_seconds": conversation.get("conversation_timeout_seconds"),
        "follow_up_prompts": conversation.get("follow_up_prompts") or {},
        "clarification_prompts": conversation.get("clarification_prompts") or {},
        "not_found_messages": conversation.get("not_found_messages") or {},
        "error_messages": conversation.get("error_messages") or {},
    }


# ── Part 7 — Execution normalization (never a secret VALUE) ────────────

def _normalize_execution(action: Dict, response_fields: List[Dict]) -> Dict:
    execution = action.get("execution") or {}
    secret_params = [p for p in (action.get("parameters") or [])
                      if p.get("input_source") in ("credential_store", "secret_configuration")]
    credential_id = None
    if secret_params:
        credential_id = secret_params[0].get("credential_ref") or secret_params[0].get("secret_ref")
    header_params = {p["name"]: True for p in (action.get("parameters") or []) if p.get("send_as") == "header"}
    query_params = {p["name"]: True for p in (action.get("parameters") or []) if p.get("send_as") == "query"}
    path_params = {p["name"]: True for p in (action.get("parameters") or []) if p.get("send_as") == "path"}
    body_params = {p["name"]: True for p in (action.get("parameters") or [])
                    if p.get("send_as") not in ("header", "query", "path") and p.get("input_source") not in ("credential_store", "secret_configuration")}
    return {
        "provider_type": action.get("action_type") or "API",
        "method": execution.get("http_method"),
        "endpoint": execution.get("endpoint") or ((execution.get("base_url") or "") + (execution.get("endpoint_path") or "")),
        "content_type": execution.get("content_type"),
        "headers": list(header_params.keys()),
        "query_parameters": list(query_params.keys()),
        "body_parameters": list(body_params.keys()),
        "path_parameters": list(path_params.keys()),
        # A REFERENCE only — never a secret value (Part 7's core rule).
        "credential_reference": {
            "store": "credential_store" if any(p.get("input_source") == "credential_store" for p in secret_params) else
                     ("secret_configuration" if secret_params else None),
            "credential_id": credential_id,
            "required": bool(secret_params),
        },
        "timeout_seconds": execution.get("timeout_seconds"),
        "retry": {"enabled": bool(execution.get("retry_policy")), "max_attempts": (execution.get("retry_policy") or {}).get("max_attempts")},
        "success_validation": (action.get("setup_metadata") or {}).get("success_validation") or {},
        "response_mapping": [{"json_path": f["technical_path"], "canonical_name": f["canonical_name"]} for f in response_fields],
    }


# ── Part 8 — Security normalization (computed, never re-decided) ───────

def _normalize_security(execution: Dict, input_fields: List[Dict], output_fields: List[Dict], action: Dict) -> Dict:
    customer_visible = [f["canonical_name"] for f in output_fields if f["visible"] and not f["sensitive"]]
    developer_only_fields = [f["canonical_name"] for f in output_fields
                              if not f["visible"]]  # includes developer_only-marked
    hidden_fields = [f["canonical_name"] for f in output_fields if not f["visible"]]
    masked_fields = [f["canonical_name"] for f in output_fields if f["masking"]]
    sensitive_inputs = [f["canonical_name"] for f in input_fields if f["sensitive"]]
    sensitive_outputs = [f["canonical_name"] for f in output_fields if f["sensitive"]]
    return {
        "customer_visible_fields": customer_visible,
        "developer_only_fields": developer_only_fields,
        "hidden_fields": hidden_fields,
        "masked_fields": masked_fields,
        "sensitive_inputs": sensitive_inputs,
        "sensitive_outputs": sensitive_outputs,
        "allow_live_execution": bool(action.get("enabled")),
        "required_permissions": (action.get("setup_metadata") or {}).get("required_permissions") or [],
    }


# ── Part 1-3 — The contract builder (the ONE place) ─────────────────────

def describe_integration(action_id: str, sb=None, schema_override: Optional[Dict] = None) -> Optional[Dict]:
    """Builds the canonical Integration Contract for one action. Loads
    the action via business_action_registry.get_full() (mask_secrets=
    True — no raw secret ever enters this function), the published
    schema via integration_schema_service, resolves the effective
    schema via the ONE three-layer merge (resolve_effective_integration_
    schema), then normalizes it into the stable contract shape. Returns
    None if the action does not exist.

    `schema_override` (additive, optional): when supplied, this exact
    schema dict is used as the "published_schema" input to the merge
    INSTEAD OF the actually-published row — e.g. a Studio Conversation
    Preview that wants the contract for the current, unpublished DRAFT
    schema. `schema_version`/`published_at`/`derived_from` in the
    returned contract remain sourced from the real published row (if
    any) so this never masquerades as a published contract."""
    from services.business_action_registry import get_registry as get_action_registry
    from services.integration_schema_service import get_registry as get_schema_registry, \
        resolve_effective_integration_schema
    from services import erp_test_harness as h

    action = get_action_registry(sb).get_full(action_id, mask_secrets=True)
    if not action:
        return None

    schema_registry = get_schema_registry(sb)
    published_row = schema_registry.get_published(action_id)
    published_schema = schema_override if schema_override is not None else (
        published_row.get("schema") if published_row else None)

    resolution = resolve_effective_integration_schema(action, published_schema)
    effective = resolution["effective_schema"]
    warnings = list(resolution["warnings"])

    operation_type = (effective.get("general") or {}).get("operation_type")
    primary_entity = h._primary_entity(action)
    entities = h.detect_entities(action)
    capability = f"{primary_entity}.{(operation_type or 'unknown').lower()}" if primary_entity else (
        action.get("display_name") or action.get("name"))

    input_fields = [_normalize_input_field(f, next((p for p in (action.get("parameters") or [])
                                                       if p.get("name") == f.get("technical_name")), {}))
                    for f in effective.get("input_fields") or []]
    output_fields = [_normalize_output_field(f) for f in effective.get("response_fields") or []]

    required_field_groups = [
        {"name": g.get("name"), "rule": g.get("rule"), "members": list(g.get("members") or [])}
        for g in (action.get("parameter_groups") or [])
    ]

    conversation = _normalize_conversation(effective.get("conversation") or {})
    execution = _normalize_execution(action, output_fields)
    security = _normalize_security(execution, input_fields, output_fields, action)

    status = "active" if action.get("enabled") else ("draft" if action.get("is_draft") else "disabled")

    contract = {
        "contract_version": CONTRACT_VERSION,
        "action_id": action.get("id"), "action_name": action.get("name"),
        "status": status,
        "operation_type": operation_type, "capability": capability,
        "provider": {
            "id": action.get("id"), "name": action.get("display_name") or action.get("name"),
            "type": action.get("action_type") or "API",
        },
        "entities": entities, "inputs": input_fields,
        "required_field_groups": required_field_groups, "outputs": output_fields,
        "conversation": conversation, "execution": execution, "security": security,
        "localization": effective.get("localization") or {},
        "examples": effective.get("examples") or [],
        "schema_version": published_row.get("version_number") if published_row else None,
        "resolver_version": RESOLVER_VERSION,
        "published_at": published_row.get("published_at") if published_row else None,
        "warnings": warnings,
        # Part 17 — versioning surface
        "action_updated_at": action.get("updated_at"),
        "schema_published_at": published_row.get("published_at") if published_row else None,
        "derived_from": published_row.get("version_number") if published_row else None,
        "backward_compatibility_version": CONTRACT_VERSION,
    }
    return contract


# ── Part 13 — In-process contract cache (no new DB table) ──────────────
# Uses the shared VersionedCache abstraction (Requirement 6) — the
# conversation-form cache in services/conversation_form_generator.py
# uses this SAME class, never a second bare module-level dict.

_contract_cache = VersionedCache()


def _cache_key(action: Dict, schema_version: Optional[int]) -> tuple:
    return (action.get("id"), action.get("updated_at"), schema_version,
            RESOLVER_VERSION, CONTRACT_VERSION, bool(action.get("enabled")))


def describe_integration_cached(action_id: str, sb=None) -> Optional[Dict]:
    """Same as describe_integration() but memoized in-process, keyed on
    (action_id, action.updated_at, schema_version, resolver_version,
    contract_version, enabled) — Part 13. Invalidates automatically
    whenever any of those change (a schema publish/rollback bumps
    version_number; enable/disable and any action edit that bumps
    updated_at are covered; a resolver/contract_version constant bump
    invalidates every entry immediately since it's part of the key)."""
    from services.business_action_registry import get_registry as get_action_registry
    from services.integration_schema_service import get_registry as get_schema_registry

    action = get_action_registry(sb).get(action_id)
    if not action:
        return None
    published_row = get_schema_registry(sb).get_published(action_id)
    key = _cache_key(action, published_row.get("version_number") if published_row else None)
    return _contract_cache.get(key, lambda: describe_integration(action_id, sb))


def invalidate_contract_cache(action_id: Optional[str] = None) -> None:
    """Explicit invalidation hook — clears every cached entry for one
    action, or the whole cache when action_id is None. Never required
    for correctness (the cache key already changes whenever anything
    relevant changes) but useful for tests / an admin "force refresh"."""
    if action_id is None:
        _contract_cache.invalidate_all()
        return
    _contract_cache.invalidate(lambda k: k[0] == action_id)


# ── Part 12 — Contract validation ───────────────────────────────────────

def _json_path_is_valid(path: Optional[str]) -> bool:
    if not path:
        return False
    normalized = path[2:] if path.startswith("$.") else (path[1:] if path.startswith("$") else path)
    return bool(__import__("re").fullmatch(r"[A-Za-z0-9_]+(\.[A-Za-z0-9_\[\]]+)*", normalized))


def validate_integration_contract(action_id: str, sb=None) -> Dict:
    """Part 12 — {"valid": bool, "errors": [...], "warnings": [...]}.
    Errors block a contract from being considered valid; warnings
    surface but never block."""
    contract = describe_integration(action_id, sb)
    errors: List[str] = []
    warnings: List[str] = []
    if contract is None:
        return {"valid": False, "errors": ["action_not_found"], "warnings": []}

    if not contract.get("capability"):
        errors.append("missing capability")
    if contract.get("operation_type") == "UNKNOWN":
        errors.append("operation type could not be determined (UNKNOWN)")

    seen_input_names = set()
    for f in contract["inputs"]:
        if not f.get("canonical_name") or not f.get("semantic_type"):
            errors.append(f"input field '{f.get('technical_name')}' is missing canonical_name/semantic_type")
        cn = f.get("canonical_name")
        if cn:
            if cn in seen_input_names:
                errors.append(f"duplicate canonical input name '{cn}'")
            seen_input_names.add(cn)
        if f.get("visible") if "visible" in f else False:
            pass
        if not f.get("display_labels"):
            warnings.append(f"input field '{f.get('technical_name')}' has no display labels")

    seen_output_names = set()
    for f in contract["outputs"]:
        cn = f.get("canonical_name")
        if cn:
            if cn in seen_output_names:
                errors.append(f"duplicate canonical output name '{cn}'")
            seen_output_names.add(cn)
        if not _json_path_is_valid(f.get("technical_path")):
            errors.append(f"broken response path '{f.get('technical_path')}' for '{cn}'")
        if f.get("visible") and f.get("sensitive"):
            warnings.append(f"output field '{cn}' is visible but marked sensitive")
        if not f.get("visible") and f.get("answerable"):
            warnings.append(f"output field '{cn}' is answerable but hidden")
        if not f.get("display_labels"):
            warnings.append(f"output field '{cn}' has no display labels")
        if f.get("formatter", {}).get("type") not in FORMATTER_TYPES:
            warnings.append(f"output field '{cn}' has an unrecognized formatter type '{f.get('formatter', {}).get('type')}'")

    grouped_names = {m for g in contract["required_field_groups"] for m in g.get("members") or []}
    known_names = {f["technical_name"] for f in contract["inputs"]}
    for g in contract["required_field_groups"]:
        bad = [m for m in (g.get("members") or []) if m not in known_names]
        if bad:
            errors.append(f"required-field group '{g.get('name')}' references unknown input(s): {bad}")

    execution = contract["execution"]
    needs_credential = execution["credential_reference"]["required"]
    if contract["security"]["allow_live_execution"] and needs_credential and not execution["credential_reference"]["credential_id"]:
        errors.append("live execution is allowed but no credential reference is configured")

    if contract.get("status") != "active":
        warnings.append(f"action status is '{contract.get('status')}' (not active)")
    if contract.get("schema_version") is None and contract.get("operation_type") not in ("LOOKUP", None):
        warnings.append("no published schema exists for a non-trivial operation type")

    return {"valid": not errors, "errors": errors, "warnings": warnings}


def resolve_effective_contract(action_id: str, sb=None) -> Dict:
    """Part 3's richer wrapper — contract + validation + provenance in
    one call. `describe_integration()` remains the simpler "just give
    me the contract" convenience function; this is the one place a
    caller that also wants validation/provenance should use."""
    from services.business_action_registry import get_registry as get_action_registry
    from services.integration_schema_service import get_registry as get_schema_registry, \
        resolve_effective_integration_schema

    action = get_action_registry(sb).get_full(action_id, mask_secrets=True)
    if not action:
        return {"contract": None, "validation": {"valid": False, "errors": ["action_not_found"], "warnings": []},
                "provenance": {}}
    published_row = get_schema_registry(sb).get_published(action_id)
    published_schema = published_row.get("schema") if published_row else None
    resolution = resolve_effective_integration_schema(action, published_schema)
    contract = describe_integration(action_id, sb)
    validation = validate_integration_contract(action_id, sb)
    return {"contract": contract, "validation": validation, "provenance": resolution["provenance"]}


def get_contract_version(action_id: str, sb=None) -> Dict:
    contract = describe_integration(action_id, sb)
    if not contract:
        return {"contract_version": None, "schema_version": None, "resolver_version": RESOLVER_VERSION}
    return {
        "contract_version": contract["contract_version"], "schema_version": contract["schema_version"],
        "resolver_version": contract["resolver_version"],
        "backward_compatibility_version": contract["backward_compatibility_version"],
    }


# ── Part 10 — Platform service list/lookup APIs ─────────────────────────

def list_integrations(filters: Optional[Dict] = None, sb=None) -> List[Dict]:
    from services.business_action_registry import get_registry as get_action_registry
    filters = filters or {}
    reg = get_action_registry(sb)
    actions = reg.list()
    out = []
    for a in actions:
        if filters.get("action_type") and a.get("action_type") != filters["action_type"]:
            continue
        if filters.get("enabled") is not None and bool(a.get("enabled")) != bool(filters["enabled"]):
            continue
        contract = describe_integration_cached(a["id"], sb)
        if contract is None:
            continue
        if filters.get("entity") and filters["entity"] not in contract.get("entities", []):
            continue
        if filters.get("operation_type") and contract.get("operation_type") != filters["operation_type"]:
            continue
        out.append(contract)
    return out


def list_capabilities(sb=None) -> List[str]:
    return sorted({c["capability"] for c in list_integrations(sb=sb) if c.get("capability")})


def list_entities(sb=None) -> List[str]:
    ents = set()
    for c in list_integrations(sb=sb):
        ents.update(c.get("entities") or [])
    return sorted(ents)


def list_providers(sb=None) -> List[Dict]:
    seen = {}
    for c in list_integrations(sb=sb):
        p = c.get("provider") or {}
        if p.get("id"):
            seen[p["id"]] = p
    return list(seen.values())


def list_required_inputs(action_id: str, sb=None) -> List[Dict]:
    contract = describe_integration(action_id, sb)
    if not contract:
        return []
    return [f for f in contract["inputs"] if f.get("required")]


def list_response_fields(action_id: str, sb=None) -> List[Dict]:
    contract = describe_integration(action_id, sb)
    return contract["outputs"] if contract else []
