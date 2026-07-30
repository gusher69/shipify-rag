"""Integration Schema Studio — Registry Service.

A completely SEPARATE, versioned configuration layer on top of a
Business Action (see services/business_action_registry.py, which this
module never modifies). Where the Business Action tables hold the
technical wiring (endpoint, parameters, response mapping), this module
holds the BUSINESS/CONVERSATION layer an admin edits without touching
code: business labels, aliases, display labels per language, follow-up
prompts, AI synonyms, formatting rules, visibility/security, and
conversation/operation-type behavior overrides — plus full draft /
published / archived versioning with rollback and diff.

Architecture (Part 17): Integration Action -> Integration Schema ->
Generic Integration Runtime is a ONE-DIRECTIONAL read path.
services/erp_test_harness.py reads the schema (via
`resolve_effective_schema`) — this module never reads back into the
runtime's internals, never calls the Decision Engine, the Business
Action Matcher, or the Action Executor. It also never implements either
of those (out of scope for this sprint).

Storage: migrations/033_integration_action_schemas.sql —
`integration_action_schemas` (id, action_id FK, version_number, status
draft|published|archived, parent_version_id, schema JSONB, created_by,
updated_by, created_at, updated_at, published_at). Purely additive —
no changes to any business_actions* table.

Backward compatibility (Part 15): `derive_default_schema(action)`
produces a full schema shape purely from an action's OWN existing data
(parameters, response_mapping, setup_metadata, and the same heuristics
services/erp_test_harness.py already uses: semantic_classify,
_resolve_display_label's fallback chain, get_conversation_behavior's
operation-type defaults). This is the fallback layer used whenever no
schema has been published yet, AND the pre-fill seed for a brand-new
draft in the Studio UI — an admin editing schema for the first time
never sees a blank form.
"""
from datetime import datetime, timezone
from typing import Dict, List, Optional

STATUSES = ("draft", "published", "archived")

# Generic (never per-field-name) default formatting type per semantic
# type — purely a lookup table, Part 7's "table-driven only" rule.
_DEFAULT_FORMATTING_BY_SEMANTIC_TYPE = {
    "currency": "currency", "date": "date", "boolean": "boolean",
    "percentage": "number", "integer": "number", "array": "list",
    "url": "link", "address": "address", "phone": "phone",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Part 15 — Default schema derivation (backward-compat fallback) ─────

def derive_default_schema(action: Dict) -> Dict:
    """Produces a full Integration Schema shape purely from an action's
    already-existing DB data (business_actions + its related tables, as
    returned by BusinessActionRegistry.get_full()). Never touches the
    network/DB itself — a pure, deterministic transformation, exactly
    like every function in services/erp_test_harness.py this reuses.

    This is BOTH the ultimate runtime fallback (when no schema has ever
    been published for this action) AND the pre-fill seed for a brand
    new draft in the Studio UI."""
    from services import erp_test_harness as h

    fallback_entity = h._primary_entity(action)

    input_fields = []
    for p in (action.get("parameters") or []):
        name = p.get("name")
        if not name:
            continue
        sem = h.semantic_classify(name, fallback_entity=fallback_entity)
        attr = sem["canonical_name"].split(".", 1)[-1]
        business_label = p.get("display_name") or h._humanize(name)
        hidden = p.get("input_source") in ("credential_store", "secret_configuration")
        input_fields.append({
            "technical_name": name,
            "business_label": business_label,
            "canonical_name": sem["canonical_name"],
            "semantic_type": sem["semantic_type"],
            "required": bool(p.get("required")),
            "validation_pattern": p.get("validation_pattern"),
            "example_value": p.get("example_value"),
            "description": p.get("description"),
            "aliases": list(p.get("aliases") or []),
            "display_labels": {
                "th": h._resolve_display_label(p, sem["canonical_name"], sem["entity"], attr, name, business_label, language="th"),
                "en": h._resolve_display_label(p, sem["canonical_name"], sem["entity"], attr, name, business_label, language="en"),
            },
            "visibility": "hidden" if hidden else "customer",
            "follow_up_prompts": {},
            "synonyms": [],
        })

    response_fields = []
    for m in (action.get("response_mapping") or []):
        field_name = (m.get("json_path") or "").split(".")[-1]
        if not field_name:
            continue
        sem = h.semantic_classify(field_name, fallback_entity=fallback_entity)
        attr = sem["canonical_name"].split(".", 1)[-1]
        business_label = m.get("mapped_label") or sem["business_label"]
        sensitive = h._is_sensitive(field_name, sem["semantic_type"])
        response_fields.append({
            "technical_path": m.get("json_path"),
            "business_label": business_label,
            "canonical_name": sem["canonical_name"],
            "semantic_type": sem["semantic_type"],
            "display_labels": {
                "th": h._resolve_display_label(m, sem["canonical_name"], sem["entity"], attr, field_name, business_label, language="th"),
                "en": h._resolve_display_label(m, sem["canonical_name"], sem["entity"], attr, field_name, business_label, language="en"),
            },
            "answerable": bool(m.get("answerable", True)),
            "visible": bool(m.get("visible", True)),
            "sensitive": sensitive,
            "mask": sensitive,
            "formatting": {"type": _DEFAULT_FORMATTING_BY_SEMANTIC_TYPE.get(sem["semantic_type"], "text")},
            "aliases": list(m.get("aliases") or []),
            "example_values": [],
            "follow_up_prompts": {},
            "synonyms": [],
        })

    return {
        # Part 1-2 — a brand-new draft's `general.operation_type` and
        # `conversation.*` are left absent/None ("inherit") rather than
        # baking a concrete, point-in-time-derived value into the
        # schema. Once a schema like this becomes a saved draft/published
        # version, a later change to the action's own configuration (or
        # to `general.operation_type` alone) can still cascade correctly
        # at RESOLVE time (see `resolve_effective_integration_schema()`)
        # instead of being masked by stale baked-in values that look
        # identical to a real admin override. FIELD-level structural data
        # (technical_name, canonical_name, business_label, semantic_type,
        # json_path, etc., in input_fields/response_fields below) is
        # positional/structural — 1:1 derived from the action's own
        # parameters/response_mapping and can't meaningfully be
        # "inherited" any other way — so it stays populated as before.
        "general": {
            "action_id": action.get("id"), "operation_type": None,
            "primary_entity": fallback_entity,
        },
        "input_fields": input_fields,
        "response_fields": response_fields,
        "localization": {"default_language": "th", "supported_languages": ["th", "en"]},
        "conversation": {},
        "ai_behaviour": {"synonyms": []},
        "visibility": {},
        "security": {},
        "prompt": {"system_prompt_override": None},
        "examples": [{"example_text": e.get("example_text")} for e in (action.get("examples") or []) if e.get("example_text")],
        # Part 10 — marks this schema as produced by the provenance-aware
        # derivation logic, so the resolver can tell it apart from an
        # older, pre-existing published schema that has no such marker
        # (and must instead be treated per the Part 10 backward-
        # compatibility strategy — see `resolve_effective_integration_schema`).
        "_schema_meta": {"provenance_version": 2},
    }


# ── Merge helpers (schema overrides win, derived fills the rest) ───────

def _merge_field_list(derived: List[Dict], overrides: List[Dict], key: str) -> List[Dict]:
    override_by_key = {o.get(key): o for o in (overrides or []) if o.get(key)}
    merged = []
    seen = set()
    for base in derived:
        k = base.get(key)
        seen.add(k)
        override = override_by_key.get(k)
        if override:
            row = dict(base)
            for field, value in override.items():
                if field in ("display_labels", "follow_up_prompts", "formatting") and isinstance(value, dict):
                    row[field] = {**(row.get(field) or {}), **value}
                elif value not in (None, [], ""):
                    row[field] = value
            merged.append(row)
        else:
            merged.append(base)
    # Admin-added fields not derived from the action's own current
    # configuration (rare — e.g. an action was reconfigured after the
    # schema was authored) are still surfaced rather than silently
    # dropped.
    for o in (overrides or []):
        if o.get(key) and o.get(key) not in seen:
            merged.append(o)
    return merged


# ── Part 3 — The Effective Schema Resolver (the core deliverable) ──────
# Field-level attributes whose provenance is tracked individually — Part
# 3's "reasonably for field-level visibility/aliases/display_labels/
# follow_up_prompts/formatting" requirement. Extended (Conversation Form
# Generator hardening pass, Requirement 1) with the configurable display-
# metadata keys introduced for the generator — same generic, key-agnostic
# mechanism, no second provenance resolver.
_FIELD_PROVENANCE_ATTRS = ("visibility", "visible", "aliases", "display_labels", "follow_up_prompts", "formatting",
                           "display_type", "placeholder", "help_text", "display_order", "group", "icon", "color",
                           "examples", "empty_message", "channel_overrides", "options",
                           # Conversation Strategy Engine sprint (Part 3) — same generic,
                           # key-agnostic provenance mechanism, no second resolver.
                           "conversation_priority", "ask_first", "skip_if_detected", "auto_fill",
                           "suggest_if_missing", "confidence_threshold")


def _has_provenance_marker(schema: Dict) -> bool:
    return bool((schema.get("_schema_meta") or {}).get("provenance_version"))


def _merge_field_list_with_provenance(derived: List[Dict], overrides: List[Dict], key: str, section_name: str):
    """Same merge as `_merge_field_list()`, plus a parallel provenance map
    (`"{section}.{field_key}.{attr}": "explicit"|"derived"`) for the
    field-level attributes Part 3 asks to track."""
    merged = _merge_field_list(derived, overrides, key)
    override_by_key = {o.get(key): o for o in (overrides or []) if o.get(key)}
    provenance: Dict[str, str] = {}
    for row in merged:
        k = row.get(key)
        if not k:
            continue
        override = override_by_key.get(k) or {}
        for attr in _FIELD_PROVENANCE_ATTRS:
            path = f"{section_name}.{k}.{attr}"
            value = override.get(attr)
            provenance[path] = "explicit" if value not in (None, [], "", {}) else "derived"
    return merged, provenance


def resolve_effective_integration_schema(action: Dict, published_schema: Optional[Dict]) -> Dict:
    """Part 3 — the ONE place that implements the actual 3-layer merge
    algorithm. Every other caller (the backward-compatible
    `resolve_effective_schema(action, sb)` wrapper below, and every
    services/erp_test_harness.py caller of `get_conversation_behavior()`/
    `infer_operation_type()`) goes through this function's output.

    Layer priority for any given value:
      explicit schema override -> derived action default -> runtime default.

    Implemented as genuinely layered resolution:
      1. Start from RUNTIME DEFAULTS for the resolved `operation_type`
         (services.erp_test_harness.get_conversation_behavior_defaults) —
         this requires resolving `operation_type` itself through the
         SAME three layers first (Part 8/9's confidence-scored inference,
         since conversation defaults cascade FROM the resolved
         operation_type).
      2. Override with anything derivable from the action's own
         structure (`action.setup_metadata.conversation_behavior` — the
         pre-existing, structural admin-configured override; never a
         schema value).
      3. Override again with anything the admin explicitly set in the
         schema's own `conversation`/`general.operation_type`.

    Returns {"effective_schema": {...}, "provenance": {...}, "warnings": [...]}.
    `provenance` mirrors effective_schema's key paths with
    "explicit"/"derived"/"runtime_default" for at least
    `general.operation_type`, every `conversation.*` key, and field-level
    visibility/aliases/display_labels/follow_up_prompts/formatting.
    `warnings` surfaces low-confidence/UNKNOWN operation-type inference
    (Part 9) and pre-provenance-tracking schemas (Part 10)."""
    from services import erp_test_harness as h

    published_schema = published_schema or {}
    warnings: List[str] = []
    provenance: Dict[str, str] = {}

    has_provenance = _has_provenance_marker(published_schema)
    pre_existing_general = (published_schema.get("general") or {}).get("operation_type")
    pre_existing_conversation = published_schema.get("conversation") or {}
    predates_provenance = bool(published_schema) and not has_provenance and (
        pre_existing_general is not None or any(v is not None for v in pre_existing_conversation.values())
    )
    if predates_provenance:
        # Part 10 — this schema has no provenance metadata at all. Per
        # "when uncertain, preserve behavior and add a provenance
        # warning": treat ALL pre-existing populated conversation.*/
        # general.operation_type values as explicit (never silently
        # discard/reinterpret real published configuration), and flag it
        # for administrator review.
        warnings.append("this schema predates provenance tracking - treating pre-existing values as "
                         "explicit overrides - review recommended.")

    # ── operation_type: resolved through the same 3 layers ──────────────
    explicit_operation_type = pre_existing_general if (has_provenance or predates_provenance) else None
    if explicit_operation_type:
        operation_type = explicit_operation_type
        provenance["general.operation_type"] = "explicit"
    else:
        evidence = h.infer_operation_type_with_evidence(action, schema=None)
        operation_type = evidence["operation_type"]
        provenance["general.operation_type"] = "derived" if evidence["confidence"] >= 0.6 else "runtime_default"
        if operation_type == "UNKNOWN":
            warnings.append("operation_type could not be confidently inferred - defaulting to UNKNOWN "
                             "and treating conservatively (no ask/auto-execute assumptions).")
        elif evidence["confidence"] < 0.6:
            warnings.append(f"operation_type '{operation_type}' was inferred with low confidence "
                             f"({evidence['confidence']}); consider setting general.operation_type explicitly.")

    # ── conversation.*: layer 1 (runtime defaults for operation_type) ───
    conversation = h.get_conversation_behavior_defaults(operation_type)
    for k in conversation:
        provenance[f"conversation.{k}"] = "runtime_default"

    # layer 2 — derivable from the action's own structure
    action_derived_conversation = (action.get("setup_metadata") or {}).get("conversation_behavior") or {}
    for k, v in action_derived_conversation.items():
        conversation[k] = v
        provenance[f"conversation.{k}"] = "derived"

    # layer 3 — explicit schema override (or, for a pre-provenance
    # schema, Part 10's "treat existing values as explicit" compat path)
    if has_provenance or predates_provenance:
        for k, v in pre_existing_conversation.items():
            if v is None:
                continue
            conversation[k] = v
            provenance[f"conversation.{k}"] = "explicit"

    general = {"action_id": action.get("id"), "operation_type": operation_type,
               "primary_entity": h._primary_entity(action)}

    derived_full = derive_default_schema(action)
    merged = dict(derived_full)
    merged["general"] = general
    merged["conversation"] = conversation
    for section in ("localization", "ai_behaviour", "visibility", "security", "prompt"):
        if isinstance(published_schema.get(section), dict):
            merged[section] = {**derived_full.get(section, {}), **published_schema[section]}
    merged["input_fields"], input_provenance = _merge_field_list_with_provenance(
        derived_full["input_fields"], published_schema.get("input_fields") or [], "technical_name", "input_fields")
    merged["response_fields"], response_provenance = _merge_field_list_with_provenance(
        derived_full["response_fields"], published_schema.get("response_fields") or [], "canonical_name", "response_fields")
    provenance.update(input_provenance)
    provenance.update(response_provenance)
    if published_schema.get("examples"):
        merged["examples"] = published_schema["examples"]
    merged.pop("_schema_meta", None)

    return {"effective_schema": merged, "provenance": provenance, "warnings": warnings}


def resolve_effective_schema(action: Dict, sb) -> Dict:
    """Thin backward-compatible wrapper around
    `resolve_effective_integration_schema()` for callers that only need
    the flat effective schema dict (not provenance/warnings) — fetches
    the currently PUBLISHED schema version fresh each call (no caching
    here, per Part 17's design note) and returns just `effective_schema`."""
    action_id = action.get("id")
    published_schema = None
    if action_id and sb is not None:
        published_schema = get_registry(sb).get_published_schema(action_id)
    return resolve_effective_integration_schema(action, published_schema)["effective_schema"]


# ── Structural diff (Part 13) ───────────────────────────────────────────

def _diff_value(a, b):
    if a == b:
        return None
    return {"before": a, "after": b}


def diff_schema(schema_a: Dict, schema_b: Dict) -> Dict:
    """A plain, structural before/after diff — no fancy diff library,
    the UI can render the {before, after} pairs however it likes.

    Part 7 — additionally notes when a change in `general.operation_type`
    alone caused a downstream EFFECTIVE change in conversation behavior
    (the operation-type cascade), so a version's diff doesn't just show
    "operation_type: CANCEL -> LOOKUP" but also that `conversation.*`
    values changed as a CASCADED consequence rather than an explicit
    admin edit — purely informational, additive to the plain diff above."""
    keys = set(schema_a.keys()) | set(schema_b.keys())
    out = {}
    for k in keys:
        d = _diff_value(schema_a.get(k), schema_b.get(k))
        if d is not None:
            out[k] = d

    op_diff = out.get("general", {})
    op_before = (op_diff.get("before") or {}).get("operation_type") if isinstance(op_diff, dict) else None
    op_after = (op_diff.get("after") or {}).get("operation_type") if isinstance(op_diff, dict) else None
    if op_before != op_after and (op_before or op_after):
        from services import erp_test_harness as h
        defaults_before = h.get_conversation_behavior_defaults(op_before) if op_before else {}
        defaults_after = h.get_conversation_behavior_defaults(op_after) if op_after else {}
        explicit_conv_a = schema_a.get("conversation") or {}
        explicit_conv_b = schema_b.get("conversation") or {}
        cascaded_notes = []
        for key in set(defaults_before) | set(defaults_after):
            before_val = defaults_before.get(key)
            after_val = defaults_after.get(key)
            # Only note it as "cascaded, not explicitly edited" when
            # neither version actually has an explicit conversation.*
            # override for this key — i.e. the change is purely a
            # consequence of the operation_type change.
            if before_val != after_val and key not in explicit_conv_a and key not in explicit_conv_b:
                cascaded_notes.append(
                    f"effective conversation.{key}: {before_val} -> {after_val} (cascaded from "
                    f"operation_type change, not explicitly edited)")
        if cascaded_notes:
            out["_cascaded_conversation_effects"] = cascaded_notes
    return out


class IntegrationSchemaRegistry:
    """Analogous, purely-additive twin of BusinessActionRegistry — CRUD +
    versioning ONLY over `integration_action_schemas`. Never touches any
    business_actions* table (read-only borrower of an action's own data
    via the caller-supplied `action` dict, e.g. from
    BusinessActionRegistry.get_full())."""

    def __init__(self, sb):
        self._sb = sb

    def list_versions(self, action_id: str) -> List[Dict]:
        return self._sb.table("integration_action_schemas").select("*").eq("action_id", action_id) \
            .order("version_number").execute().data or []

    def get_published(self, action_id: str) -> Optional[Dict]:
        rows = [r for r in self.list_versions(action_id) if r.get("status") == "published"]
        return rows[-1] if rows else None

    def get_published_schema(self, action_id: str) -> Optional[Dict]:
        row = self.get_published(action_id)
        return row.get("schema") if row else None

    def get_draft_row(self, action_id: str) -> Optional[Dict]:
        rows = [r for r in self.list_versions(action_id) if r.get("status") == "draft"]
        return rows[-1] if rows else None

    def get_draft(self, action_id: str, action: Optional[Dict] = None) -> Dict:
        """Returns the current draft row, creating one (pre-filled via
        `derive_default_schema`, or from the published version if one
        exists) if none exists yet."""
        existing = self.get_draft_row(action_id)
        if existing:
            return existing
        published = self.get_published(action_id)
        if published:
            seed = published["schema"]
        elif action is not None:
            seed = derive_default_schema(action)
        else:
            seed = {}
        next_version = (max((r.get("version_number") or 0) for r in self.list_versions(action_id)) + 1) \
            if self.list_versions(action_id) else 1
        row = {
            "action_id": action_id, "version_number": next_version, "status": "draft",
            "parent_version_id": published["id"] if published else None,
            "schema": seed, "created_at": _now_iso(), "updated_at": _now_iso(),
        }
        return self._sb.table("integration_action_schemas").insert(row).execute().data[0]

    def save_draft(self, action_id: str, schema: Dict, *, updated_by: Optional[str] = None) -> Dict:
        draft = self.get_draft_row(action_id)
        if not draft:
            draft = self.get_draft(action_id)
        payload = {"schema": schema, "updated_by": updated_by, "updated_at": _now_iso()}
        rows = self._sb.table("integration_action_schemas").update(payload).eq("id", draft["id"]).execute().data
        return rows[0] if rows else draft

    def publish(self, action_id: str, *, updated_by: Optional[str] = None) -> Optional[Dict]:
        """Archives the currently published version (if any) and promotes
        the current draft to published — a new draft can then be started
        fresh from the newly published content on next edit."""
        draft = self.get_draft_row(action_id)
        if not draft:
            return None
        previous = self.get_published(action_id)
        if previous:
            self._sb.table("integration_action_schemas").update(
                {"status": "archived", "updated_at": _now_iso()}).eq("id", previous["id"]).execute()
        rows = self._sb.table("integration_action_schemas").update(
            {"status": "published", "updated_by": updated_by, "updated_at": _now_iso(),
             "published_at": _now_iso()}).eq("id", draft["id"]).execute().data
        return rows[0] if rows else None

    def rollback(self, action_id: str, version_number: int, *, updated_by: Optional[str] = None) -> Optional[Dict]:
        """Never destroys history — creates a NEW draft version copying
        the target version's content (Part 13), which an admin can then
        review and publish like any other draft."""
        versions = self.list_versions(action_id)
        target = next((v for v in versions if v.get("version_number") == version_number), None)
        if not target:
            return None
        existing_draft = self.get_draft_row(action_id)
        if existing_draft:
            self._sb.table("integration_action_schemas").update(
                {"status": "archived", "updated_at": _now_iso()}).eq("id", existing_draft["id"]).execute()
        next_version = max((v.get("version_number") or 0) for v in versions) + 1
        row = {
            "action_id": action_id, "version_number": next_version, "status": "draft",
            "parent_version_id": target["id"], "schema": target["schema"],
            "created_by": updated_by, "updated_by": updated_by,
            "created_at": _now_iso(), "updated_at": _now_iso(),
        }
        return self._sb.table("integration_action_schemas").insert(row).execute().data[0]

    def diff_versions(self, action_id: str, v1: int, v2: int) -> Dict:
        versions = self.list_versions(action_id)
        a = next((v for v in versions if v.get("version_number") == v1), None)
        b = next((v for v in versions if v.get("version_number") == v2), None)
        if not a or not b:
            return {"error": "version_not_found"}
        return diff_schema(a["schema"], b["schema"])


_instance: Optional[IntegrationSchemaRegistry] = None


def get_registry(sb=None) -> IntegrationSchemaRegistry:
    """Same factory pattern as business_action_registry.get_registry()."""
    global _instance
    if sb is not None:
        return IntegrationSchemaRegistry(sb)
    if _instance is None:
        from admin.routes import get_sb
        _instance = IntegrationSchemaRegistry(get_sb())
    return _instance
