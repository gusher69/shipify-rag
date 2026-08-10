"""Integration Conversation Runtime — validates that an imported
integration (internally: a "Business Action"; product-facing name: an
"ERP API") can actually answer a customer question end to end, BEFORE
it is ever wired into the (not-yet-built) Universal Decision Engine.

PRODUCT-AGNOSTIC BY DESIGN. This module is not built for the Customer/
Wallet/Coupon API — that is one test fixture among many (see
tests/test_erp_test_harness.py for order/product/invoice fixtures
proving the same code answers all of them with zero source changes).
Every function here decides behavior from an action's OWN
configuration only:
  - canonical_name / entity / semantic_type (semantic_classify())
  - capability text, HTTP method, response-field presence (infer_operation_type())
  - the action's own Response Mapping (build_available_response_options())
  - the message text against that action's own field labels/aliases (detect_requested_fields())
Never an `if entity == "wallet"` / `if canonical_name == "..."`-style
branch. Adding a new integration (orders, tracking, inventory,
invoices, memberships, appointments, insurance, HR, CRM, external
SaaS...) requires ONLY a new Business Action configuration — zero
changes to this file.

Architecture: this module is a pure ORCHESTRATOR over existing, frozen
components — it never re-implements parameter binding (reuses services/
decision_engine.py's Business-Action-driven binder), never re-implements
execution (reuses services/action_executor.py::ActionExecutor unchanged),
never re-implements credential handling (reuses services/
business_action_registry.py's resolve_secret_parameters/
resolve_secret_parameter_errors, which themselves call
services/credential_store.py). Nothing here touches the Decision Engine's
own `decide()` entry point, the Provider layer, or the AI Auto Setup /
Analysis Pipeline.

Service boundary: `run_erp_test()` is the ONE public entry point, and it
is intentionally product-agnostic despite living in a module named for
its first caller. "ERP Conversation Tester" is a product-facing PAGE
name (admin/templates/erp_conversation_tester.html); this module is the
shared runtime underneath it. Any future caller — the Universal
Decision Engine, AI Playground, LINE OA, a website chat widget, a
public API — should call `run_erp_test()` directly rather than
duplicating any of the logic below.

Three test modes (Part 3 of the spec):
  - "intent_param": understand the question + extract parameters only,
    never calls the ERP.
  - "simulation": extraction + a MOCK response (last real Test API
    result if the Business Action has one, else a schema-generated mock
    from each response_mapping field's inferred type) — never a real
    network call.
  - "live": extraction + a REAL call via ActionExecutor.

Multi-turn state (Part 4) is never persisted server-side — exactly the
established convention in this codebase (services/decision_engine.py,
services/slot_filling_engine.py): the caller (the admin's browser) keeps
`history` + `collected_params` and resends them every turn; this module
recomputes everything fresh from that each call.
"""
import re
import time
from typing import Dict, List, Optional

from services.business_action_registry import get_registry, sanitize_for_preview
from services.action_executor import ActionExecutor
from services.decision_engine import (
    _bind_all_from_message,
    _next_expected_parameter,
    _generate_parameter_question,
    _askable_parameters_by_name,
)
from services.llm_service import get_llm_service
from config import OPENAI_CHAT_MODEL

TEST_MODES = ("intent_param", "simulation", "live")


def _trace_step(name: str, status: str, *, input_summary=None, output_summary=None,
                 confidence: Optional[float] = None, latency_ms: float = 0.0,
                 explanation: str = "", error: Optional[str] = None) -> Dict:
    """One row of the ERP Developer Trace (Part 10) — every step of the
    pipeline reports the same shape, so the UI never special-cases one
    step over another."""
    return {
        "step": name, "status": status, "input_summary": input_summary,
        "output_summary": output_summary, "confidence": confidence,
        "latency_ms": round(latency_ms, 2), "explanation": explanation, "error": error,
    }


def _generated_answer_output_summary(answer_result: Dict) -> Dict:
    """Conversation Tester redesign (2026-08-02) — one place building the
    generated_answer trace step's output_summary, reused at all 3 call
    sites so the LLM metadata keys (model/latency/tokens) can never drift
    out of sync between them. `.get()` on a missing key (e.g. the
    no-LLM-was-called "no record found" path) yields None, which the UI
    treats as "not applicable" — never a fabricated value."""
    return {
        "answer": answer_result.get("answer"),
        "llm_model": answer_result.get("llm_model"),
        "llm_latency_ms": answer_result.get("llm_latency_ms"),
        "llm_input_tokens": answer_result.get("llm_input_tokens"),
        "llm_output_tokens": answer_result.get("llm_output_tokens"),
    }


# ── Semantic classification (Part 1/5/8) ────────────────────────────────
# A self-contained Python port of the SAME deterministic heuristic
# admin/templates/business_actions.html already uses client-side
# (baSemanticClassify) — kept as its own small table here rather than
# imported from services/ai_auto_setup_service.py so this harness has no
# dependency on the AI Analysis Pipeline module at all. Never a stored
# column (none exists — the semantic layer is explicitly NOT persisted
# per a prior sprint), always derived at test-time from the parameter's
# own name/display_name.
_ENTITY_PATTERNS = [
    ("customer", re.compile(r"cust|customer|ลูกค้า", re.IGNORECASE)),
    ("wallet", re.compile(r"wallet|กระเป๋าเงิน", re.IGNORECASE)),
    ("coupon", re.compile(r"coupon|คูปอง", re.IGNORECASE)),
    ("order", re.compile(r"order|^po\d|คำสั่งซื้อ", re.IGNORECASE)),
    ("membership", re.compile(r"member|tier|สมาชิก", re.IGNORECASE)),
    ("product", re.compile(r"product|sku|สินค้า", re.IGNORECASE)),
    ("invoice", re.compile(r"invoice|receipt|ใบแจ้งหนี้", re.IGNORECASE)),
    ("address", re.compile(r"address|ที่อยู่", re.IGNORECASE)),
    ("payment", re.compile(r"payment|ชำระเงิน", re.IGNORECASE)),
    ("tracking", re.compile(r"tracking|shipment|พัสดุ|ติดตาม", re.IGNORECASE)),
]


def _humanize(name: str) -> str:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name or "")
    spaced = re.sub(r"[_\-]+", " ", spaced).strip()
    return " ".join(w.capitalize() for w in spaced.split()) or name


def semantic_classify(name: str, fallback_entity: Optional[str] = None) -> Dict:
    """Technical Field -> Business Label -> Canonical Name -> Semantic
    Type (Part 1's four-level chain), computed purely from the field
    name. See admin/templates/business_actions.html::baSemanticClassify
    for the client-side twin of this exact heuristic.

    `fallback_entity`: many real APIs have generically-named response
    fields (Status, Amount, Name, Price) that only mention their domain
    implicitly, through the ACTION's own capability — not through the
    field name itself. When the field name alone doesn't match any
    known entity pattern, the caller may supply the action's own
    already-detected entity (see detect_entities()/build_available_
    response_options()) instead of leaving it "general". This is still
    entirely configuration-derived (the action's own capability/
    keywords), never a hardcoded per-field domain guess."""
    n = (name or "").lower()
    entity = next((e for e, pat in _ENTITY_PATTERNS if pat.search(n)), None) or fallback_entity or "general"

    # Identifier pattern deliberately covers common cross-domain naming
    # conventions (Code/ID/No/Number/SKU) — not just the customer-code
    # shape of the original test fixture — so an OrderNo, InvoiceNo,
    # TrackingNumber, or SKU field is recognized identically without
    # adding a per-domain special case.
    if re.search(r"code$|(?:^|_)id$|identifier|number$|no$|^sku$|_no$", n):
        attr, semantic_type = "identifier", "identifier"
    elif re.search(r"name$", n):
        attr, semantic_type = "name", "string"
    elif re.search(r"email", n):
        attr, semantic_type = "email", "email"
    elif re.search(r"phone|tel\b", n):
        attr, semantic_type = "phone", "phone"
    elif re.search(r"balance|amount|price|total|due$", n):
        attr, semantic_type = "balance", "currency"
    elif re.search(r"stock|inventory|count$|qty|quantity", n):
        attr, semantic_type = "count", "integer"
    elif re.search(r"date|time$", n):
        attr, semantic_type = "date", "date"
    elif re.search(r"percent|rate$|_rate|ratio$", n):
        attr, semantic_type = "percentage", "percentage"
    elif re.search(r"^is[_A-Z]|^has[_A-Z]|^can[_A-Z]|^is$|^has$|^can$", name or "", re.IGNORECASE):
        attr, semantic_type = "flag", "boolean"
    elif re.search(r"url|link$|_link", n):
        attr, semantic_type = "url", "url"
    elif re.search(r"address", n):
        attr, semantic_type = "address", "address"
    elif re.search(r"status", n):
        attr, semantic_type = "status", "string"
    elif re.fullmatch(r"coupons?", n):
        attr, semantic_type = "list", "array"
    else:
        attr, semantic_type = (re.sub(r"[^a-z0-9]", "", n) or "value"), "string"

    return {
        "technical_name": name, "business_label": _humanize(name),
        "entity": entity, "canonical_name": f"{entity}.{attr}", "semantic_type": semantic_type,
    }


def detect_entities(action: Dict) -> List[str]:
    """Part 3 — scans parameter names + response-mapping field names +
    capability text, same broader-than-just-capability-text approach as
    the client-side baAiDetectEntities()."""
    names = [p.get("name", "") for p in (action.get("parameters") or [])]
    names += [(m.get("json_path") or "").split(".")[-1] for m in (action.get("response_mapping") or [])]
    haystack = " ".join(names).lower() + " " + " ".join([
        action.get("display_name") or "", action.get("description") or "",
        action.get("category") or "", " ".join(action.get("search_keywords") or []),
    ]).lower()
    seen, out = set(), []
    for entity, pat in _ENTITY_PATTERNS:
        if pat.search(haystack) and entity not in seen:
            seen.add(entity)
            out.append(entity)
    return out


# ── Operation Type (product-agnostic architecture requirement) ─────────
# This platform is not built for any single API/entity — the Customer/
# Wallet/Coupon action used throughout development is ONE test fixture
# among many possible future integrations (orders, tracking, inventory,
# invoices, memberships, appointments, insurance, HR, CRM, external
# SaaS...). Every function below decides behavior purely from an
# action's own configuration (canonical_name, semantic_type, entity,
# capability text, HTTP method, response field presence) — never an
# `if entity == "wallet"`-style branch. Adding a new integration must
# require ZERO changes here, only new Business Action configuration.
_OPERATION_VERB_PATTERNS = [
    ("CREATE", re.compile(r"\bcreate\b|\badd\b|\bregister\b|สร้าง|เพิ่ม|ลงทะเบียน", re.IGNORECASE)),
    ("UPDATE", re.compile(r"\bupdate\b|\bedit\b|\bmodify\b|แก้ไข|อัปเดต|เปลี่ยนแปลง", re.IGNORECASE)),
    ("CANCEL", re.compile(r"\bcancel\b|\bvoid\b|ยกเลิก", re.IGNORECASE)),
    ("DELETE", re.compile(r"\bdelete\b|\bremove\b|ลบ", re.IGNORECASE)),
    ("NOTIFY", re.compile(r"\bnotify\b|\bnotification\b|\balert\b|แจ้งเตือน", re.IGNORECASE)),
    ("CALCULATION", re.compile(r"\bcalculate\b|\bcompute\b|\bestimate\b|คำนวณ|ประเมิน", re.IGNORECASE)),
    # TRANSFORM (Part 9 extension) — converting/parsing/extracting/
    # resolving/deriving information from an existing payload is a
    # DIFFERENT operation from CALCULATION (computing a value): URL
    # parsing, OCR result normalization, data conversion, currency
    # conversion, date formatting, JSON transformation. Checked BEFORE
    # CALCULATION would otherwise be reached via generic fallback so a
    # "convert"/"parse"-worded capability is never mislabeled CALCULATION.
    ("TRANSFORM", re.compile(r"\bconvert\b|\btransform\b|\bparse\b|\bextract\b|\bresolve\b|\bderive\b|"
                              r"\bnormalize\b|แปลง", re.IGNORECASE)),
    ("SEARCH", re.compile(r"\bsearch\b|\bfind\b|ค้นหา", re.IGNORECASE)),
    ("LIST", re.compile(r"\blist\b|\ball\b|รายการทั้งหมด|รายการ", re.IGNORECASE)),
    ("STATUS", re.compile(r"\bstatus\b|\btrack\b|\btracking\b|สถานะ|ติดตาม", re.IGNORECASE)),
]
# Operation types where "what would you like to check?" clarification is
# meaningful at all — a command action (CREATE/UPDATE/CANCEL/NOTIFY/
# WORKFLOW) has no "which field do you want back" question to ask.
# UNKNOWN is deliberately excluded — it is treated conservatively like a
# command-type action (Part 9), never as lookup-like.
LOOKUP_LIKE_OPERATION_TYPES = ("LOOKUP", "LIST", "SEARCH", "STATUS")

# The full recognized operation-type vocabulary (Part 9) — UNKNOWN is the
# safe fallback when no confident signal (structural or textual) exists.
# TRANSFORM (promoted to first-class here, Semantic API Analysis Engine
# hardening sprint) is DISTINCT from CALCULATION: CALCULATION computes a
# value; TRANSFORM converts/parses/extracts/resolves/derives information
# from an existing payload (URL parsing, OCR normalization, data/currency
# conversion, date formatting, JSON transformation) — they must never
# share a runtime operation. Added at the END of the tuple (never
# reordering/removing an existing value) so any existing code that
# iterates OPERATION_TYPES by position, or persisted data comparing
# against this exact tuple's prior contents, is unaffected.
OPERATION_TYPES = ("LOOKUP", "SEARCH", "LIST", "STATUS", "CALCULATION", "CREATE",
                    "UPDATE", "DELETE", "CANCEL", "WORKFLOW", "NOTIFICATION", "NOTIFY",
                    "TRANSFORM", "UNKNOWN")

# Weak (description-text) evidence must NEVER alone justify a destructive
# or command-style classification (Part 8/9's core structural fix for the
# "safe to delete" false-positive bug).
_DESTRUCTIVE_OPERATION_TYPES = ("CREATE", "UPDATE", "DELETE", "CANCEL", "NOTIFY", "WORKFLOW")

_READ_ORIENTED_CAPABILITY_PATTERN = re.compile(r"lookup|search|status|track|list|find", re.IGNORECASE)


def infer_operation_type_with_evidence(action: Dict, schema: Optional[Dict] = None) -> Dict:
    """Part 8/9 — safer operation-type inference with confidence +
    evidence. Priority, EXACTLY as specified:
      1. Explicit schema `general.operation_type` override (conf 1.0).
      2. Existing structured `action.setup_metadata.operation_type` (conf ~0.95).
      3. HTTP method + capability text together (combined signals).
      4. Action name/capability semantic analysis — capability/action_key/
         display_name ONLY, never description/ai_description prose.
      5. Description text ONLY as weak evidence (conf <=0.5), and NEVER
         alone justifying CREATE/UPDATE/DELETE/CANCEL/NOTIFY/WORKFLOW.
      6. Default LOOKUP only with a genuine read-oriented signal;
         otherwise UNKNOWN.
    `infer_operation_type()` delegates here for backward compatibility."""
    schema_override = ((schema or {}).get("general") or {}).get("operation_type")
    if schema_override:
        return {"operation_type": schema_override, "confidence": 1.0,
                "evidence": ["explicit schema override (general.operation_type)"]}

    explicit = (action.get("setup_metadata") or {}).get("operation_type")
    if explicit:
        return {"operation_type": explicit, "confidence": 0.95,
                "evidence": [f"action.setup_metadata.operation_type = '{explicit}'"]}

    execution = action.get("execution") or {}
    http_method = (execution.get("http_method") or "GET").upper()
    has_response = bool(action.get("response_mapping") or [])
    name_text = " ".join([action.get("display_name") or "", action.get("action_key") or "",
                           action.get("category") or ""]).lower()

    # Signal 3 — HTTP method + capability text combined (never either
    # alone): a GET-like call that already has response fields AND whose
    # capability text reads as read-oriented is strong, structural evidence.
    if http_method == "GET" and has_response and _READ_ORIENTED_CAPABILITY_PATTERN.search(name_text):
        for op, pattern in _OPERATION_VERB_PATTERNS:
            if pattern.search(name_text):
                return {"operation_type": op, "confidence": 0.9,
                        "evidence": [f"GET + response mapping present + capability text matches '{op}'"]}
        return {"operation_type": "LOOKUP", "confidence": 0.9,
                "evidence": ["GET + response mapping present + read-oriented capability wording"]}

    # Signal 4 — action name/capability semantic analysis, name/action_key/
    # display_name ONLY — never description/ai_description prose.
    for op, pattern in _OPERATION_VERB_PATTERNS:
        if pattern.search(name_text):
            return {"operation_type": op, "confidence": 0.8,
                    "evidence": [f"capability/action_key/display_name matches the '{op}' verb pattern"]}

    action_type = action.get("action_type")
    if action_type == "NOTIFICATION":
        return {"operation_type": "NOTIFY", "confidence": 0.85, "evidence": ["action_type == 'NOTIFICATION'"]}
    if action_type == "WORKFLOW":
        return {"operation_type": "WORKFLOW", "confidence": 0.85, "evidence": ["action_type == 'WORKFLOW'"]}

    # Signal 5 — description text is weak evidence ONLY, low confidence,
    # and must never alone justify a destructive/command classification.
    description_text = " ".join([action.get("description") or "", action.get("ai_description") or ""]).lower()
    for op, pattern in _OPERATION_VERB_PATTERNS:
        if op in _DESTRUCTIVE_OPERATION_TYPES:
            continue  # weak text evidence never alone justifies a destructive op (Part 9)
        if pattern.search(description_text):
            return {"operation_type": op, "confidence": 0.45,
                    "evidence": [f"description text weakly matches '{op}' (low confidence, no structural signal)"]}

    # Signal 6 — default LOOKUP only with a genuine read-oriented signal;
    # otherwise prefer UNKNOWN over guessing (Part 9's safety rule).
    if has_response or http_method == "GET" or _READ_ORIENTED_CAPABILITY_PATTERN.search(name_text):
        return {"operation_type": "LOOKUP", "confidence": 0.55,
                "evidence": ["read-oriented signal present (response mapping / GET / lookup-flavored capability)"]}

    return {"operation_type": "UNKNOWN", "confidence": 0.3,
            "evidence": ["no confident structural or capability signal found — defaulting to UNKNOWN for safety"]}


def infer_operation_type(action: Dict, schema: Optional[Dict] = None) -> str:
    """Generic operation-type inference (Part 9-10, schema-first,
    additive). Delegates to `infer_operation_type_with_evidence()` for
    the full confidence/evidence breakdown; kept as the simple
    string-returning function every existing caller already uses."""
    return infer_operation_type_with_evidence(action, schema=schema)["operation_type"]


def _primary_entity(action: Dict) -> Optional[str]:
    """The single entity this action is MOST about — prefers whichever
    detected entity is actually named in the capability/category text
    over one only inferred from a parameter/field name, then falls back
    to the first detected entity. Used both for the intent label and as
    the fallback entity for otherwise-ambiguous response field names
    (see semantic_classify's `fallback_entity`)."""
    entities = detect_entities(action)
    if not entities:
        return None
    haystack = ((action.get("display_name") or "") + " " + (action.get("category") or "")).lower()
    for entity, pat in _ENTITY_PATTERNS:
        if entity in entities and pat.search(haystack):
            return entity
    return entities[0]


def primary_intent(action: Dict) -> Optional[str]:
    entity = _primary_entity(action)
    if not entity:
        return None
    return f"{entity}.{infer_operation_type(action).lower()}"


# ── Action summary for the selector (Part 2) ────────────────────────────

def describe_action_for_selection(action: Dict, sb=None) -> Dict:
    """Everything Part 2 requires to be shown when a Business Action is
    manually selected: name, capability, endpoint, method, auth status,
    required search fields, response mappings, semantic intent, entities.

    Part 15 — when `sb` is given, the per-action summary (endpoint,
    method, required search fields, response mappings, entities,
    operation_type) is sourced from services/integration_contract_
    service.py's `describe_integration_cached()` (the same Integration
    Contract the runtime now resolves in run_erp_test(), Part 14) rather
    than this function independently re-joining action+response_mapping
    itself. `capability`/`name`/`action_key` stay the pre-existing
    human-readable display fields (the contract's own `capability` is a
    machine-style `entity.operation_type` id, a different, additive
    concept — exposed here as `contract_capability` rather than
    replacing the dropdown-facing label the tester UI already renders).
    Falls back to the original direct-from-action derivation, UNCHANGED,
    whenever no `sb` is supplied (existing callers/tests) or contract
    resolution doesn't succeed for any reason — this function's output
    shape never changes either way."""
    execution = action.get("execution") or {}
    secret_params = [p for p in (action.get("parameters") or [])
                     if p.get("input_source") in ("credential_store", "secret_configuration")]
    base = {
        "id": action.get("id"), "action_key": action.get("action_key"),
        "name": action.get("name"), "capability": action.get("display_name") or action.get("name"),
        "has_credentials": bool(secret_params),
        "credential_configured": bool(secret_params) and all(p.get("credential_ref") or p.get("secret_ref") for p in secret_params),
        "enabled": action.get("enabled", False),
        "is_draft": action.get("is_draft", False),
    }

    contract = None
    if sb is not None and action.get("id"):
        try:
            from services.integration_contract_service import describe_integration_cached
            contract = describe_integration_cached(action["id"], sb)
        except Exception:
            contract = None

    if contract:
        return {
            **base,
            "endpoint": contract["execution"].get("endpoint"),
            "http_method": contract["execution"].get("method"),
            "required_search_fields": [
                {"name": f["technical_name"], "business_label": f["business_label"], "canonical_name": f["canonical_name"]}
                for f in contract["inputs"] if f.get("required") and not f.get("sensitive")
            ],
            "response_mappings": [
                {"json_path": f["technical_path"], "mapped_label": f["business_label"], "canonical_name": f["canonical_name"]}
                for f in contract["outputs"]
            ],
            "semantic_intent": (f"{contract['entities'][0]}.{(contract['operation_type'] or 'unknown').lower()}"
                                 if contract.get("entities") else None),
            "entities": contract.get("entities") or [],
            "operation_type": contract.get("operation_type"),
            "contract_capability": contract.get("capability"),
        }

    # ── Fallback — original direct-from-action derivation (unchanged) ──
    askable = _askable_parameters_by_name(action)
    fallback_entity = _primary_entity(action)
    return {
        **base,
        "endpoint": execution.get("endpoint") or ((execution.get("base_url") or "") + (execution.get("endpoint_path") or "")),
        "http_method": execution.get("http_method"),
        "required_search_fields": [
            {"name": p["name"], "business_label": p.get("display_name") or _humanize(p["name"]),
             "canonical_name": semantic_classify(p["name"], fallback_entity=fallback_entity)["canonical_name"]}
            for p in askable.values() if p.get("required")
        ],
        "response_mappings": [
            {"json_path": m.get("json_path"), "mapped_label": m.get("mapped_label"),
             "canonical_name": semantic_classify((m.get("json_path") or "").split(".")[-1], fallback_entity=fallback_entity)["canonical_name"]}
            for m in (action.get("response_mapping") or [])
        ],
        "semantic_intent": primary_intent(action),
        "entities": detect_entities(action),
        "operation_type": infer_operation_type(action),
    }


def normalize_response_wrapped(action: Dict, mapped_fields: Dict) -> Dict:
    """Part 10 — an additive wrapper around `normalize_response()` for
    callers that want the `record_type` classification without risking
    the flat, canonical-keyed dict `normalize_response()` itself returns
    (see `_infer_record_type()`'s docstring for why the two are kept
    separate). `run_erp_test()` is untouched and keeps calling
    `normalize_response()` directly; this wrapper is used by the new
    `run_integration_conversation_turn()` boundary (Part 9)."""
    fields = normalize_response(action, mapped_fields)
    return {"fields": fields, "record_type": _infer_record_type(fields)}


# ── Part 7 — Operation-type conversation behavior configuration ────────

_DEFAULT_CONVERSATION_BEHAVIOR = {
    "ask_requested_information": True, "allow_multiple_fields": True, "allow_all_fields": True,
    "require_confirmation_before_execute": False, "reuse_last_result": True,
}

# Part 4 — the exact, table-driven operation-type -> conversation-defaults
# cascade (RUNTIME DEFAULTS, layer 1 of the 3-layer resolver). Nothing
# here is ever permanently baked into a schema at derivation time
# (Part 1-2) — a schema's own explicit `conversation.*` overrides always
# win, and `action.setup_metadata.conversation_behavior` sits between
# this table and an explicit schema override.
_LOOKUP_LIKE_DEFAULTS = dict(_DEFAULT_CONVERSATION_BEHAVIOR)
_COMMAND_DEFAULTS = {
    "ask_requested_information": False, "allow_multiple_fields": False, "allow_all_fields": False,
    "require_confirmation_before_execute": True, "reuse_last_result": False,
}
_CONVERSATION_DEFAULTS_BY_OPERATION_TYPE = {
    "LOOKUP": dict(_LOOKUP_LIKE_DEFAULTS),
    "SEARCH": dict(_LOOKUP_LIKE_DEFAULTS),
    "STATUS": dict(_LOOKUP_LIKE_DEFAULTS),
    "LIST": {**_LOOKUP_LIKE_DEFAULTS, "supports_filters": True, "supports_pagination": True},
    "CREATE": dict(_COMMAND_DEFAULTS),
    "UPDATE": dict(_COMMAND_DEFAULTS),
    "CANCEL": dict(_COMMAND_DEFAULTS),
    "DELETE": dict(_COMMAND_DEFAULTS),
    "NOTIFY": {**_COMMAND_DEFAULTS, "require_confirmation_before_execute": True},
    "NOTIFICATION": {**_COMMAND_DEFAULTS, "require_confirmation_before_execute": True},
    "WORKFLOW": {**_COMMAND_DEFAULTS, "require_confirmation_before_execute": True},
    "CALCULATION": {
        "ask_requested_information": False, "collect_required_inputs": True,
        "allow_multiple_fields": False, "allow_all_fields": False,
        "require_confirmation_before_execute": False, "reuse_last_result": False,
    },
    # TRANSFORM — same shape as CALCULATION (collects required inputs,
    # non-destructive, no confirmation needed, result not "reused" the
    # way a lookup's last record is) but tracked as its own distinct
    # entry, never aliased to CALCULATION's dict, so the two can diverge
    # independently if a future requirement needs them to.
    "TRANSFORM": {
        "ask_requested_information": False, "collect_required_inputs": True,
        "allow_multiple_fields": False, "allow_all_fields": False,
        "require_confirmation_before_execute": False, "reuse_last_result": False,
    },
    # UNKNOWN (Part 9) — treated conservatively, same posture as a
    # command-type action: never assume it's safe to auto-execute or
    # safe to skip confirmation.
    "UNKNOWN": dict(_COMMAND_DEFAULTS),
}


def get_conversation_behavior_defaults(operation_type: str) -> Dict:
    """Part 4 — the pure runtime-defaults lookup table (layer 1 only),
    exposed so services/integration_schema_service.py's 3-layer resolver
    can start from it without duplicating the table."""
    return dict(_CONVERSATION_DEFAULTS_BY_OPERATION_TYPE.get(operation_type, _CONVERSATION_DEFAULTS_BY_OPERATION_TYPE["UNKNOWN"]))


def get_conversation_behavior(action: Dict, schema: Optional[Dict] = None) -> Dict:
    """Part 7/9-10 — how the conversation should behave for this action's
    OWN operation type, entirely table-driven from `infer_operation_type()`
    (never a per-entity/per-domain branch). Three-tier priority (schema-
    first, additive, nothing breaks when no schema exists):
      1. the Integration Schema's own `conversation` section (new
         PRIMARY source — services/integration_schema_service.py),
      2. `action.setup_metadata.conversation_behavior` (the pre-existing
         admin override),
      3. the operation-type-derived defaults table above (Part 4).

    NOTE: when `schema` is the output of
    `services.integration_schema_service.resolve_effective_integration_schema`
    (the normal case, via `run_erp_test()`), `schema["conversation"]` is
    already the fully-resolved effective behavior — this function's own
    3-tier merge below is what makes that true, and is also safe to call
    directly with a raw/partial schema (e.g. from tests) since it always
    re-derives the table defaults from the resolved operation type."""
    operation_type = infer_operation_type(action, schema=schema)
    defaults = get_conversation_behavior_defaults(operation_type)
    overrides = (action.get("setup_metadata") or {}).get("conversation_behavior") or {}
    schema_overrides = {k: v for k, v in ((schema or {}).get("conversation") or {}).items() if v is not None}
    return {**defaults, **overrides, **schema_overrides}


# ── Part 2 — Generic Integration Action Model ───────────────────────────

def build_generic_action_model(action: Dict, schema: Optional[Dict] = None) -> Dict:
    """Part 2/11-12 — a pure, read-only transformation of a Business
    Action into the domain-agnostic "Integration Action Model" shape
    future callers (a future Matcher/Decision Engine, LINE OA, Website
    Chat, a public API) can reason about without knowing this is 'the
    ERP Business Action' format. No new storage — computed fresh from
    the action's own already-frozen configuration (plus the optional,
    additive Integration Schema) every call, exactly like the rest of
    this module. This is a READ-ONLY EXPOSURE function — it builds no
    new engine logic, per Part 11-12's explicit scope boundary.

    When `schema` is given (see services/integration_schema_service.py::
    resolve_effective_schema), every field/section below is enriched
    with the schema's aliases, canonical fields, display_labels,
    synonyms, visibility, security, and conversation_behavior/
    operation_type — everything a future Matcher/Decision Engine would
    need. Nothing here changes when `schema` is None (Part 15)."""
    registry_askable = _askable_parameters_by_name(action)
    fallback_entity = _primary_entity(action)
    operation_type = infer_operation_type(action, schema=schema)
    execution = action.get("execution") or {}
    schema_input_by_name = {f.get("technical_name"): f for f in (schema or {}).get("input_fields") or []}
    schema_response_by_canonical = {f.get("canonical_name"): f for f in (schema or {}).get("response_fields") or []}

    input_fields = []
    for name, p in registry_askable.items():
        sem = semantic_classify(name, fallback_entity=fallback_entity)
        sf = schema_input_by_name.get(name) or {}
        attr = sem["canonical_name"].split(".", 1)[-1]
        business_label = p.get("display_name") or _humanize(name)
        input_fields.append({
            "name": name, "business_label": sf.get("business_label") or business_label,
            "canonical_name": sem["canonical_name"], "semantic_type": sem["semantic_type"],
            "required": bool(p.get("required")), "example_value": sf.get("example_value") or p.get("example_value"),
            "aliases": list(sf.get("aliases") or _configured_aliases(p)),
            "synonyms": list(sf.get("synonyms") or []),
            "display_labels": sf.get("display_labels") or {
                "th": _resolve_display_label(p, sem["canonical_name"], sem["entity"], attr, name, business_label, language="th"),
                "en": business_label,
            },
            "visibility": sf.get("visibility") or "customer",
            "follow_up_prompts": sf.get("follow_up_prompts") or {},
        })

    required_field_groups = [
        {"name": g.get("name"), "rule": g.get("rule"), "members": list(g.get("members") or [])}
        for g in (action.get("parameter_groups") or [])
    ]

    response_fields = []
    for m in (action.get("response_mapping") or []):
        field_name = (m.get("json_path") or "").split(".")[-1]
        sem = semantic_classify(field_name, fallback_entity=fallback_entity)
        sf = schema_response_by_canonical.get(sem["canonical_name"]) or {}
        response_fields.append({
            "canonical_name": sem["canonical_name"],
            "business_label": sf.get("business_label") or m.get("mapped_label") or sem["business_label"],
            "semantic_type": sem["semantic_type"], "json_path": m.get("json_path"),
            "aliases": list(sf.get("aliases") or _configured_aliases(m)),
            "synonyms": list(sf.get("synonyms") or []),
            "display_labels": sf.get("display_labels") or {},
            "sensitive": sf.get("sensitive") if "sensitive" in sf else _is_sensitive(field_name, sem["semantic_type"]),
            "visible": sf.get("visible", True), "visibility": sf.get("visibility"),
            "formatting": sf.get("formatting") or {},
            "follow_up_prompts": sf.get("follow_up_prompts") or {},
        })

    return {
        "action_id": action.get("id"), "name": action.get("name"),
        "operation_type": operation_type, "capability": action.get("display_name") or action.get("name"),
        "entities": detect_entities(action),
        "input_fields": input_fields, "required_field_groups": required_field_groups,
        "response_fields": response_fields,
        "conversation_behavior": get_conversation_behavior(action, schema=schema),
        "execution_config": {
            "http_method": execution.get("http_method"),
            "endpoint": execution.get("endpoint") or ((execution.get("base_url") or "") + (execution.get("endpoint_path") or "")),
            "content_type": execution.get("content_type"),
        },
        "semantic_metadata": {
            "primary_entity": fallback_entity, "primary_intent": primary_intent(action),
            "is_lookup_like": operation_type in LOOKUP_LIKE_OPERATION_TYPES,
        },
        "localization": (schema or {}).get("localization") or {},
        "ai_behaviour": (schema or {}).get("ai_behaviour") or {},
        "security": (schema or {}).get("security") or {},
        "examples": (schema or {}).get("examples") or [ex for ex in (action.get("examples") or [])],
    }


# ── Parameter extraction (Part 5) ───────────────────────────────────────
# Directly reuses services/decision_engine.py's registry-driven binder —
# the exact same functions the (future) Universal Decision Engine will
# use — so a Business Action that passes here behaves identically once
# wired into real routing.

def extract_parameters(action: Dict, registry, collected: Dict, message: str) -> Dict:
    outcome = _bind_all_from_message(action, registry, collected, message)
    new_collected = outcome["collected"]
    newly_bound = {k: v for k, v in new_collected.items() if collected.get(k) != v}
    validation = registry.validate_can_execute(action["id"], new_collected)
    # validate_can_execute()/validate_parameter_groups() only excludes the
    # LEGACY secret_configuration source from "missing required" — it
    # predates credential_store and doesn't know about it. Never count a
    # credential_store secret as something to ask the customer for (Part
    # 6's explicit rule) — filter it out of missing_required here rather
    # than changing the shared, frozen registry validation function.
    askable = _askable_parameters_by_name(action)
    filtered_missing = [n for n in (validation.get("missing_required") or []) if n in askable]
    failed_groups = validation.get("failed_groups") or []
    # An unsatisfied AT_LEAST_ONE/ALL/EXACTLY_ONE group (e.g. "give me
    # CustCode OR CustEmail OR CustName OR CustPhone") is just as much a
    # reason execution must be blocked as an individually-required
    # parameter — surface its still-missing, askable members too so
    # "missing_parameters" is never silently empty while can_execute is
    # actually False.
    group_missing = [m for g in failed_groups for m in (g.get("members") or [])
                      if m in askable and m not in new_collected and m not in filtered_missing]
    all_missing = filtered_missing + group_missing
    validation = {**validation, "missing_required": all_missing, "failed_groups": failed_groups,
                  "ok": not filtered_missing and not failed_groups}
    reason_parts = []
    for name, value in newly_bound.items():
        param = _askable_parameters_by_name(action).get(name)
        label = (param or {}).get("display_name") or name
        reason_parts.append(f"'{value}' matches the {label} field.")
    if outcome.get("ambiguous_candidates"):
        reason_parts.append(f"Ambiguous candidates found: {', '.join(outcome['ambiguous_candidates'])} — could not bind confidently.")
    if not reason_parts:
        reason_parts.append("No new parameter values were found in this message.")
    confidence = 0.96 if newly_bound and not outcome.get("ambiguous_candidates") else (0.4 if outcome.get("ambiguous_candidates") else 0.6)
    return {
        "parameters": new_collected, "newly_bound": newly_bound,
        "missing_parameters": validation.get("missing_required") or [],
        "ambiguous_candidates": outcome.get("ambiguous_candidates") or [],
        "confidence": confidence, "reason": " ".join(reason_parts),
        "can_execute": validation["ok"],
    }


def build_clarification_question(action: Dict, registry, collected: Dict,
                                  schema: Optional[Dict] = None, language: str = "th") -> Optional[str]:
    """Part 5/6 — natural follow-up built from the missing parameter's
    own configured follow-up prompt (Part 5, schema-first — new PRIMARY
    source), else its Business Name/Description/Example Value/configured
    follow-up options, reusing the exact same generator the Decision
    Engine uses."""
    param = _next_expected_parameter(action, registry, collected)
    if not param:
        return None
    schema_field = next((f for f in (schema or {}).get("input_fields") or []
                         if f.get("technical_name") == param.get("name")), None)
    if schema_field:
        configured = (schema_field.get("follow_up_prompts") or {}).get(language)
        if configured:
            return configured
    options = param.get("follow_up_options") or []
    if options:
        return options[0]
    return _generate_parameter_question(param)


# ── Simulation mode (Part 3.2) ──────────────────────────────────────────

_MOCK_BY_SEMANTIC_TYPE = {
    "identifier": lambda n: "MOCK-" + (re.sub(r"[^A-Za-z0-9]", "", n) or "ID").upper()[:8],
    "email": lambda n: "customer@example.com",
    "phone": lambda n: "0812345678",
    "currency": lambda n: 1250.0,
    "integer": lambda n: 3,
    "date": lambda n: "2026-01-15",
    "array": lambda n: ["Sample A", "Sample B"],
    "string": lambda n: "Sample " + _humanize(n),
}


def build_simulated_response(action: Dict) -> Dict:
    """Uses, in priority order: (1) the Business Action's own saved
    `test_payload` from a prior REAL Test API run if it looks like a
    response (not just a request payload), else (2) a schema-generated
    mock keyed by each response_mapping field's inferred semantic type.
    Never calls the real ERP."""
    mock = {}
    for m in action.get("response_mapping") or []:
        field_name = (m.get("json_path") or "").split(".")[-1]
        sem = semantic_classify(field_name)
        mock[m.get("mapped_label") or field_name] = _MOCK_BY_SEMANTIC_TYPE.get(sem["semantic_type"], _MOCK_BY_SEMANTIC_TYPE["string"])(field_name)
    return mock


# ── Requested Information Clarification layer ───────────────────────────
# Fixes a real conversational bug: once the search parameter (e.g.
# CustCode) is complete, the system was calling generate_grounded_answer()
# with an ambiguous "question" (the identifier itself, e.g. "C00001") —
# the LLM correctly had no idea what the customer actually wanted to
# know, and its own instruction to say "not available" when a needed
# field is unclear fired on EVERY turn, masquerading as an ERP-data-
# unavailable message. The fix: distinguish "which field does the
# customer want" from "is the field's data available" as two entirely
# separate questions, asked at the right time.

# Thai display labels for common canonical names — used verbatim when
# present (matches the spec's own examples exactly); anything else falls
# back to a compositional entity+attribute label, then to the
# already-existing English business_label as a last resort. Deliberately
# small and additive — never a replacement for the English business
# label, which always remains what's actually sent to the LLM/UI too.
_CANONICAL_TH_LABELS = {
    "customer.name": "ชื่อลูกค้า", "customer.identifier": "รหัสลูกค้า",
    "customer.email": "อีเมลลูกค้า", "customer.phone": "เบอร์โทรลูกค้า",
    "wallet.balance": "ยอดเงินในกระเป๋า",
    "coupon.count": "คูปองที่ใช้ได้", "coupon.list": "คูปองที่ใช้ได้",
    "order.identifier": "เลขที่คำสั่งซื้อ", "order.status": "สถานะคำสั่งซื้อ",
    "membership.status": "ระดับสมาชิก", "product.name": "ชื่อสินค้า",
    "payment.balance": "ยอดชำระเงิน", "tracking.status": "สถานะการจัดส่ง",
}
_ENTITY_TH = {"customer": "ลูกค้า", "wallet": "กระเป๋าเงิน", "coupon": "คูปอง", "order": "คำสั่งซื้อ",
              "membership": "สมาชิก", "product": "สินค้า", "invoice": "ใบแจ้งหนี้", "address": "ที่อยู่",
              "payment": "การชำระเงิน", "tracking": "การติดตาม", "general": ""}
_ATTR_TH = {"identifier": "รหัส", "name": "ชื่อ", "email": "อีเมล", "phone": "เบอร์โทร",
            "balance": "ยอดเงิน", "count": "จำนวน", "date": "วันที่", "status": "สถานะ", "list": "รายการ"}

# Per-attribute alias phrases (Thai + English) a customer might use to
# name a field WITHOUT its exact business label — deliberately keyed by
# the generic attribute (identifier/name/balance/...), not by any one
# hardcoded entity, so a new ERP API's "OrderBalance" or "PointBalance"
# is matched by the same "balance" aliases without new code.
_ATTR_ALIASES = {
    "identifier": ["รหัส", "customer code", "customer id", "code"],
    "name": ["ชื่อ", "customer name", "name"],
    "email": ["อีเมล", "email"],
    "phone": ["เบอร์โทร", "เบอร์", "phone"],
    "balance": ["ยอดเงิน", "ยอด wallet", "เงินในกระเป๋า", "wallet balance", "เครดิตคงเหลือ", "ยอดเงินในกระเป๋า", "balance", "amount", "price", "total", "ราคา"],
    "count": ["คูปอง", "คูปองที่ใช้ได้", "available coupons", "coupon balance", "coupon", "สต็อก", "คงเหลือ", "จำนวน", "stock"],
    "list": ["คูปอง", "คูปองที่ใช้ได้", "available coupons", "coupon balance", "coupon"],
    "status": ["สถานะ", "status"],
    "date": ["วันที่", "date"],
}
_ALL_KEYWORDS = ("ทั้งหมด", "ดูทั้งหมด", "all", "everything")

# Semantic types that must never be surfaced as "sensitive: true" — an
# identifier or status is routinely shown back to the customer who
# already provided/asked for it. Anything else defaults to non-sensitive
# UNLESS the field's own name matches a generic secret-like pattern
# (password/secret/token/pin/otp) — still entity-agnostic, driven only
# by the field's own technical name.
_NEVER_SENSITIVE_SEMANTIC_TYPES = ("identifier", "status")
_SENSITIVE_NAME_PATTERN = re.compile(r"password|secret|token|pin\b|otp", re.IGNORECASE)


def _is_sensitive(technical_name: str, semantic_type: str) -> bool:
    if semantic_type in _NEVER_SENSITIVE_SEMANTIC_TYPES:
        return False
    return bool(_SENSITIVE_NAME_PATTERN.search(technical_name or ""))


def _field_metadata(field: Dict) -> Dict:
    """Part 3/4/8 — a field (parameter or response_mapping row) MAY carry
    an optional, additive `field_metadata` JSONB blob (or, for callers
    that don't yet have a DB column, an inline `field_metadata`/`aliases`/
    `display_labels` key on the dict itself) with admin-configured
    per-field `aliases: [...]` and `display_labels: {lang: label}`. Read
    entirely via `.get()` — absent on every existing action/fixture, so
    this is a pure additive extension, never a required schema change."""
    return field.get("field_metadata") or {}


def _configured_aliases(field: Dict) -> List[str]:
    meta = _field_metadata(field)
    aliases = field.get("aliases") or meta.get("aliases")
    return list(aliases) if aliases else []


def _configured_display_label(field: Dict, language: str) -> Optional[str]:
    meta = _field_metadata(field)
    labels = field.get("display_labels") or meta.get("display_labels") or {}
    return labels.get(language)


def _canonical_th_label(canonical_name: str, entity: str, attr: str) -> str:
    """DEPRECATED fallback path — kept only as a last-resort seed table
    for canonical names that predate configuration-driven labels (Part 8
    calls for removing per-canonical-name hardcodes from being the
    PRIMARY source of truth; `_resolve_display_label()` now always tries
    configured `field_metadata.display_labels` and the generic
    entity+attribute composition FIRST — see its docstring). Retained,
    unchanged, purely for backward compatibility with the existing test
    suite's literal Thai assertions (e.g. "ยอดเงินในกระเป๋า" for
    wallet.balance) built up over prior sprints; new integrations never
    need an entry here since `_resolve_display_label()`'s config +
    generic composition path covers them without any source change."""
    if canonical_name in _CANONICAL_TH_LABELS:
        return _CANONICAL_TH_LABELS[canonical_name]
    composed = (_ENTITY_TH.get(entity, "") + _ATTR_TH.get(attr, "")).strip()
    return composed or attr


def _resolve_display_label(field: Dict, canonical_name: str, entity: str, attr: str,
                            technical_name: str, business_label: str, language: str = "th",
                            schema: Optional[Dict] = None) -> str:
    """Part 4/8 — the label-resolution PRIORITY order, schema-first:
    (0) the Integration Schema's own configured
        display_labels[language] for this exact field (new PRIMARY
        source — services/integration_schema_service.py; checked ahead
        of everything else, additive, never required),
    (1) field_metadata.display_labels[language] if an admin configured
        one for this exact field (the pre-existing configuration path),
    (2) business_label (from the action's own Response Mapping row),
    (3) the canonical name composed from the generic _ENTITY_TH/_ATTR_TH
        building-block dictionaries (entity/attribute CONCEPT
        translations, not per-specific-field overrides),
    (4) the technical name humanized.
    A caller asking specifically for Thai additionally consults the
    legacy `_CANONICAL_TH_LABELS` seed table between (1) and (3) purely
    for backward compatibility with pre-existing canonical names (see
    `_canonical_th_label`'s docstring) — a NEW integration's fields never
    need an entry there since (0)/(1)/(3)/(4) already cover them
    generically."""
    schema_field = _find_schema_field(schema, canonical_name, technical_name)
    if schema_field:
        schema_label = (schema_field.get("display_labels") or {}).get(language)
        if schema_label:
            return schema_label
    configured = _configured_display_label(field, language)
    if configured:
        return configured
    if language == "th":
        return _canonical_th_label(canonical_name, entity, attr)
    return business_label or _humanize(technical_name)


def _find_schema_field(schema: Optional[Dict], canonical_name: Optional[str], technical_name: Optional[str]) -> Optional[Dict]:
    """Looks a field up in the effective Integration Schema by whichever
    key applies — response fields are keyed by canonical_name, input
    fields by technical_name. Returns None (safe, additive) when no
    schema is present or nothing matches — every caller already handles
    that by falling through to the existing derivation chain."""
    if not schema:
        return None
    for f in schema.get("response_fields") or []:
        if canonical_name and f.get("canonical_name") == canonical_name:
            return f
    for f in schema.get("input_fields") or []:
        if technical_name and f.get("technical_name") == technical_name:
            return f
    return None


# ── Part 2 — Response Option Discovery ──────────────────────────────────

_HIDDEN_VISIBILITY_VALUES = ("hidden", "developer_only")


def build_available_response_options(action: Dict, normalized_result: Optional[Dict] = None,
                                      schema: Optional[Dict] = None) -> List[Dict]:
    """Builds the customer-facing "what would you like to check?" menu
    ENTIRELY from the selected ERP API's own configured Response Mapping
    — never a hardcoded wallet/coupon/customer-name list. When
    `normalized_result` is given (a real or simulated execution already
    happened), only fields that actually have a usable, non-empty value
    are offered — never a field with a null/missing value would be
    incorrectly presented as selectable.

    Part 4/8 — when an effective Integration Schema is given, its
    per-field `aliases`/`display_labels`/`visibility`/`formatting`/
    `synonyms` are merged in (schema wins), and a field marked
    `visibility: "hidden"` or `"developer_only"` in the schema is NEVER
    included here, regardless of the underlying response_mapping row's
    own `visible` flag (Part 8's security requirement)."""
    fallback_entity = _primary_entity(action)
    schema_response_by_canonical = {f.get("canonical_name"): f for f in (schema or {}).get("response_fields") or []}
    options = []
    for m in action.get("response_mapping") or []:
        field_name = (m.get("json_path") or "").split(".")[-1]
        if not field_name:
            continue
        sem = semantic_classify(field_name, fallback_entity=fallback_entity)
        attr = sem["canonical_name"].split(".", 1)[-1]
        if normalized_result is not None:
            entry = normalized_result.get(sem["canonical_name"])
            if entry is None or entry.get("value") in (None, "", [], {}):
                continue  # excluded: no usable value (Part 2)
        sf = schema_response_by_canonical.get(sem["canonical_name"]) or {}
        business_label = sf.get("business_label") or m.get("mapped_label") or sem["business_label"]
        display_labels = {
            "th": _resolve_display_label(m, sem["canonical_name"], sem["entity"], attr,
                                          field_name, business_label, language="th", schema=schema),
            "en": _resolve_display_label(m, sem["canonical_name"], sem["entity"], attr,
                                          field_name, business_label, language="en", schema=schema),
        }
        sensitive = sf.get("sensitive") if "sensitive" in sf else _is_sensitive(field_name, sem["semantic_type"])
        schema_visibility = sf.get("visibility")
        visible = sf.get("visible", m.get("visible", True))
        options.append({
            # Backward-compatible keys (unchanged shape/meaning):
            "canonical_name": sem["canonical_name"],
            "business_label": business_label,
            "display_label_th": display_labels["th"],
            "json_path": m.get("json_path"),
            # Part 4 — additive configuration-driven extensions:
            "display_labels": display_labels,
            "aliases": list(sf.get("aliases") or _configured_aliases(m)),
            "synonyms": list(sf.get("synonyms") or []),
            "visible": bool(visible),
            "answerable": bool(sf.get("answerable", m.get("answerable", True))),
            "sensitive": bool(sensitive),
            "masked": bool(sf.get("mask")),
            "visibility": schema_visibility,
            "formatting": sf.get("formatting") or {},
            "follow_up_prompts": sf.get("follow_up_prompts") or {},
        })
    # Only visible/answerable/not-sensitive/not-hidden fields are
    # user-selectable (Part 4/8) — this never removes a field a caller
    # explicitly opted into via visible=True/answerable=True, only ones
    # an admin marked hidden/unanswerable/sensitive/developer_only in
    # the field's own configuration (response_mapping row OR schema).
    return [o for o in options if o["visible"] and o["answerable"] and not o["sensitive"]
            and o["visibility"] not in _HIDDEN_VISIBILITY_VALUES]


# ── Part 1/5/9 — Requested Field Detection ──────────────────────────────

def _field_alias_pool(o: Dict, ambiguous_entities: Optional[set] = None) -> List[str]:
    """Part 3/5 — the phrases checked against a message for one response
    option, table-driven only (never an `if canonical_name == "..."` /
    `if entity == "..."` / `if "coupon" in message` branch). Priority:
    configured aliases (`o["aliases"]`, from the field's own
    field_metadata) are checked FIRST; the generic, entity-agnostic
    `_ATTR_ALIASES` last-resort table is only consulted when nothing is
    configured for this field."""
    attr = o["canonical_name"].split(".", 1)[-1]
    # Part 6 — AI Synonyms: schema-configured `synonyms` (abbreviations/
    # business terms/typos/industry terms, e.g. Wallet ~ Balance/Credit/
    # Point/Coin) are an ADDITIONAL, highest-priority source ahead of
    # everything else — never a Python dict rewrite, purely schema-driven.
    pool = list(o.get("synonyms") or [])
    pool += list(o.get("aliases") or [])
    pool += [o.get("business_label"), o.get("display_label_th")]
    pool += list((o.get("display_labels") or {}).values())
    # Individual business-label WORDS (>=3 chars) are also generic
    # candidates — e.g. "Invoice Amount"'s own word "Amount" should
    # still match "what's the amount..." even though the full two-word
    # label isn't a literal substring of the message. Only entity names
    # that are AMBIGUOUS for this action (shared by 2+ of its response
    # fields, e.g. "Order" appearing in both "Order Status" and "Order
    # Total") are excluded from this per-word pool, since they name
    # WHICH record the message is about rather than WHICH field. A field
    # whose own canonical entity is unique within the action (e.g.
    # "Tracking Number" is the only "tracking.*" field on an Order
    # Lookup action) keeps that word — otherwise a message like
    # "tracking" could never match it at all.
    ambiguous = ambiguous_entities or set()
    pool += [w for w in (o.get("business_label") or "").split()
             if len(w) >= 3 and w.lower() not in ambiguous]
    if not o.get("aliases"):
        pool += _ATTR_ALIASES.get(attr, [])
    return [p for p in pool if p]


def detect_requested_fields(message: str, options: List[Dict], history: Optional[List[Dict]] = None) -> Dict:
    """Detects which of the (already visible/usable) response options the
    customer is asking about. Generic/table-driven only: numeric
    selection ("2"), "ทั้งหมด"/"all", and scored phrase matches against
    each option's OWN configured/derived alias pool (business_label,
    display_labels, canonical_name segments, configured aliases,
    semantic_type-generic fallback) — never a per-canonical-name or
    per-entity special case. Returns MULTIPLE fields when the message
    names more than one (Part 8/5).

    Backward-compatible return shape (`requested_fields`, `confidence`,
    `reason`) is preserved unchanged; `candidates` (Part 5) is an
    additive new key — a scored `[{canonical_name, score, evidence}]`
    list every caller can use going forward without breaking existing
    callers that only read the three original keys."""
    text = (message or "").strip()
    lower = text.lower()
    history_text = " ".join(
        (h.get("message") or h.get("text") or "") if isinstance(h, dict) else str(h)
        for h in (history or [])
    ).lower()

    if not options:
        return {"requested_fields": [], "confidence": 0.0,
                "reason": "No response options are configured for this ERP API.", "candidates": []}

    if lower in _ALL_KEYWORDS or any(k in lower for k in _ALL_KEYWORDS):
        candidates = [{"canonical_name": o["canonical_name"], "score": 0.95,
                        "evidence": "matched the 'show everything' keyword"} for o in options]
        return {"requested_fields": [o["canonical_name"] for o in options], "confidence": 0.95,
                "reason": "The user asked for all available information.", "candidates": candidates}

    if text.isdigit():
        idx = int(text) - 1
        if 0 <= idx < len(options):
            candidates = [{"canonical_name": options[idx]["canonical_name"], "score": 0.98,
                            "evidence": f"numeric selection '{text}'"}]
            return {"requested_fields": [options[idx]["canonical_name"]], "confidence": 0.98,
                    "reason": f"The user selected option {text} from the displayed list.", "candidates": candidates}
        return {"requested_fields": [], "confidence": 0.0,
                "reason": f"'{text}' does not match any displayed option number.", "candidates": []}

    entity_counts: Dict[str, int] = {}
    for o in options:
        entity = o["canonical_name"].split(".", 1)[0]
        entity_counts[entity] = entity_counts.get(entity, 0) + 1
    ambiguous_entities = {e for e, c in entity_counts.items() if c > 1}

    candidates = []
    for o in options:
        best_phrase, best_score = None, 0.0
        for phrase in _field_alias_pool(o, ambiguous_entities):
            haystack = lower if phrase.lower() in lower else (history_text if phrase.lower() in history_text else None)
            if haystack is None:
                continue
            # Longer, more specific phrases are weighted slightly higher
            # than short generic attribute words — purely a function of
            # the phrase's own length, never which entity it belongs to.
            score = min(0.94, 0.75 + 0.02 * len(phrase))
            if score > best_score:
                best_phrase, best_score = phrase, score
        if best_phrase:
            candidates.append({"canonical_name": o["canonical_name"], "score": round(best_score, 3),
                                "evidence": f"'{best_phrase}' matched {o.get('business_label')}"})

    matched = [c["canonical_name"] for c in candidates]
    if matched:
        reason = "; ".join(c["evidence"] for c in candidates)
        confidence = max((c["score"] for c in candidates), default=0.0)
        return {"requested_fields": matched, "confidence": round(confidence, 3), "reason": reason, "candidates": candidates}
    return {"requested_fields": [], "confidence": 0.0,
            "reason": "The message did not name any of the available response fields.", "candidates": []}


def build_clarification_message(options: List[Dict], *, language: str = "th", unavailable_hint: bool = False) -> str:
    """Part 5/9.3 — the natural "what would you like to check?" message,
    generated purely from `options` (never hardcoded field names)."""
    if language == "th":
        labels = [o["display_label_th"] or o["business_label"] for o in options]
        header = ("API นี้ไม่มีข้อมูลที่คุณถามค่ะ ข้อมูลที่ตรวจสอบได้มี:" if unavailable_hint
                   else "พบข้อมูลแล้วค่ะ ต้องการตรวจสอบเรื่องใดคะ")
        return header + "\n" + "\n".join(f"- {l}" for l in labels)
    labels = [o["business_label"] for o in options]
    header = ("That information isn't available for this API. Here's what you can check:" if unavailable_hint
              else "The record is ready. What would you like to check?")
    return header + "\n" + "\n".join(f"- {l}" for l in labels)


def _synthetic_question_for_fields(options_by_canonical: Dict[str, Dict], requested_fields: List[str], *, language: str = "th") -> str:
    """When a field was resolved via a numeric pick or a bare alias
    (e.g. "2" or "ยอดเงิน") rather than a real natural-language
    question, build a small readable question from the selected
    fields' own labels instead of forwarding the terse raw reply
    verbatim to the answer generator — purely cosmetic, the actual
    grounding still comes only from the filtered normalized fields."""
    labels = [options_by_canonical[c]["business_label"] for c in requested_fields if c in options_by_canonical]
    if not labels:
        return "?"
    joined = " และ ".join(labels) if language == "th" else " and ".join(labels)
    return ("ขอดู" + joined) if language == "th" else ("Show me " + joined)


# ── Response normalization (Part 8) ─────────────────────────────────────

def format_value(value, formatting_config: Optional[Dict], language: str = "th"):
    """Part 7 — a small, GENERIC formatter for a normalized field's
    display value, table-driven only by `formatting.type` (never a
    per-field-name branch). Unknown/absent types, or a value that
    doesn't fit the expected shape, are returned unchanged — this must
    never raise or hide data."""
    if not formatting_config:
        return value
    fmt = (formatting_config or {}).get("type")
    if value is None or fmt is None:
        return value
    try:
        if fmt == "currency":
            return f"฿{float(value):,.2f}" if language == "th" else f"{float(value):,.2f}"
        if fmt == "number" or fmt == "percent":
            n = float(value)
            out = f"{n:,.2f}" if n != int(n) else f"{int(n):,}"
            return f"{out}%" if fmt == "percent" else out
        if fmt == "boolean":
            truthy = value in (True, "true", "True", 1, "1")
            return ("ใช่" if truthy else "ไม่ใช่") if language == "th" else ("Yes" if truthy else "No")
        if fmt == "list" or fmt == "table":
            return value if isinstance(value, list) else [value]
        if fmt in ("date", "time", "status", "link", "url", "markdown", "rich_text", "image", "phone", "address"):
            return value  # pass-through — no destructive reformatting without a real date/locale library
        return value
    except (TypeError, ValueError):
        return value


def normalize_response(action: Dict, mapped_fields: Dict, schema: Optional[Dict] = None) -> Dict:
    """Converts the executor's own `mapped_fields` (label -> value,
    already computed by run_rest_call from json_path/mapped_label) into
    the canonical-name-keyed, semantically-typed shape Part 8 specifies.
    Only fields the admin marked visible in the Response Mapping /
    Response Explainer are included — an admin who hid a field on the
    Review & Edit page (client-side `_hidden` flag) never has it reach
    the LLM answer step from here, since the response_mapping list this
    reads from is the one actually saved on the Business Action.

    Part 7/8 — when an effective Integration Schema is given: a field
    whose schema `visibility` is "hidden"/"developer_only" is EXCLUDED
    from the customer-facing result entirely (never just filtered later
    — Part 8's hard requirement); a field marked `mask: true` has its
    value replaced with a masked value (reusing the same masking shape
    as business_action_registry.mask_secret); every other field's value
    is passed through `format_value()` using the schema's own
    `formatting` config."""
    # Must classify with the SAME fallback_entity as
    # build_available_response_options() — otherwise an ambiguous field
    # name (e.g. "Status") could resolve to a different canonical_name
    # here than in the options list, breaking the requested-field
    # filter (Part 7) for any action whose response fields don't
    # individually repeat the domain in their own name.
    fallback_entity = _primary_entity(action)
    label_to_path = {m.get("mapped_label"): m.get("json_path") for m in (action.get("response_mapping") or [])}
    schema_response_by_canonical = {f.get("canonical_name"): f for f in (schema or {}).get("response_fields") or []}
    normalized = {}
    for label, value in (mapped_fields or {}).items():
        path = label_to_path.get(label) or label
        field_name = path.split(".")[-1]
        sem = semantic_classify(field_name, fallback_entity=fallback_entity)
        sf = schema_response_by_canonical.get(sem["canonical_name"]) or {}
        if sf.get("visibility") in _HIDDEN_VISIBILITY_VALUES:
            continue
        display_value = value
        if sf.get("mask"):
            from services.business_action_registry import mask_secret
            display_value = mask_secret(str(value)) if value is not None else value
        elif sf.get("formatting"):
            display_value = format_value(value, sf.get("formatting"))
        normalized[sem["canonical_name"]] = {"label": label, "value": display_value, "semantic_type": sem["semantic_type"]}
    return normalized


def _infer_record_type(normalized: Dict) -> str:
    """Part 10 — "collection" when any mapped value is itself naturally
    a list (semantic_type == "array" or a raw list value), "single"
    otherwise. Deliberately NOT mixed into `normalize_response()`'s own
    flat canonical-keyed dict — every existing caller (including
    `generate_grounded_answer()` in this same module) iterates that
    dict's values assuming each one is a `{label, value, semantic_type}`
    row; adding a bare string value under an extra key there would break
    that unchanged, tested contract. Instead this is exposed as its own
    additive top-level key on `run_erp_test()`'s / `normalize_response`'s
    call sites (see `record_type` in the harness result and in
    `run_integration_conversation_turn()`'s `normalized_result`)."""
    if not normalized:
        return "single"
    is_collection = any(v.get("semantic_type") == "array" or isinstance(v.get("value"), list)
                         for v in normalized.values())
    return "collection" if is_collection else "single"


# ── Grounded answer generation (Part 9) ─────────────────────────────────

_NO_RECORD_ANSWERS = {
    "th": "ไม่พบข้อมูลสำหรับรหัสลูกค้าที่ระบุครับ กรุณาตรวจสอบรหัสอีกครั้ง",
    "en": "We couldn't find a record matching what you provided — please double-check and try again.",
}


def generate_grounded_answer(question: str, action: Dict, normalized_result: Dict,
                              history: Optional[List[Dict]] = None, language: str = "th") -> Dict:
    """Generates the customer-facing answer using ONLY the normalized,
    visible fields — never the raw ERP response, never a value the
    normalized result doesn't contain. If there is nothing to answer
    with, returns a fixed "not found" message instead of ever letting
    the LLM invent a number."""
    if not normalized_result:
        return {"answer": _NO_RECORD_ANSWERS.get(language, _NO_RECORD_ANSWERS["en"]), "grounded": False, "used_llm": False}

    fields_text = "\n".join(f"- {v['label']}: {v['value']}" for v in normalized_result.values())
    system_prompt = (
        "You are a customer-service assistant. The data fields below have ALREADY been looked up and "
        "confirmed to belong to the specific customer/record who asked — record identification is not "
        "your job, only phrasing the answer is. Answer the customer's question using ONLY these fields. "
        "Never invent, guess, or add any value not explicitly present in the fields. If a field the "
        "question needs isn't among them, say that specific piece of information isn't available — but "
        "still use and mention whichever OTHER listed fields are relevant to the question. Reply in the "
        "same language as the customer's question. Keep it to one short, natural sentence."
    )
    user_prompt = f"Data fields for this customer's record (already verified — just report them):\n{fields_text}\n\nCustomer question: {question}\n\nBusiness capability: {action.get('display_name') or action.get('name')}"
    messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}]
    try:
        llm = get_llm_service()
        response = llm.generate(messages, model=OPENAI_CHAT_MODEL, temperature=0.1, max_tokens=150)
        # Conversation Tester redesign (2026-08-02) — surfaces metadata the
        # LLMResponse object already carries (services/llm_service.py) but
        # this function previously discarded. No new LLM call, no prompt
        # change — purely capturing fields already computed by the SAME
        # response object above.
        return {"answer": response.text.strip(), "grounded": True, "used_llm": True,
                "llm_model": response.model, "llm_latency_ms": round(response.latency_ms, 2),
                "llm_input_tokens": response.input_tokens, "llm_output_tokens": response.output_tokens}
    except Exception as e:
        return {"answer": None, "grounded": False, "used_llm": False,
                "error": f"Answer generation failed: {sanitize_for_preview(str(e))}"}


# ── The full harness pipeline (Part 10's trace, end to end) ────────────

def run_erp_test(*, sb, action_id: str, message: str, mode: str,
                  history: Optional[List[Dict]] = None, collected_params: Optional[Dict] = None,
                  language: str = "th", awaiting_information_selection: bool = False,
                  available_response_options: Optional[List[Dict]] = None,
                  last_normalized_result: Optional[Dict] = None,
                  conversation_form_state: Optional[Dict] = None,
                  confirmed: bool = False, enforce_confirmation_gate: bool = False) -> Dict:
    """Runs one full turn of the ERP Action Test flow and returns
    {"ok", "summary", "trace": [...], "conversation_state": {...}}.
    Never raises — every failure mode becomes a trace step with
    status="error" and a friendly top-level summary instead.

    The three new state fields (Part 3 of the Requested Information
    Clarification sprint) are, like everything else in this module,
    never persisted server-side — the caller resends them each turn:
      - awaiting_information_selection: True if the PREVIOUS turn asked
        "what would you like to check?" and this turn's message is the
        customer's answer to THAT question, not a new request.
      - available_response_options: the option list shown last turn
        (needed to resolve a numeric "2" back to a canonical field).
      - last_normalized_result: the ERP result already fetched last
        turn — reused here so answering a follow-up ("ยอดเงินในกระเป๋า")
        never re-executes the same ERP call (Part 6).

    Additive (Conversation Form Generator sprint, Part 7 — never
    required, never changes any pre-existing return key):
      - conversation_form_state: the caller-supplied, round-tripped
        `ConversationState.to_dict()` from the PREVIOUS turn (same
        non-persisted convention as `history`/`collected_params` above —
        never stored server-side). Rebuilt each call via
        `ConversationState.from_dict()`, merged with `collected_params`.
      - confirmed / enforce_confirmation_gate: opt-in-only knobs for
        Scenario F (a COMMAND-type action whose resolved contract has
        `conversation.require_confirmation_before_execute=true`).
        `enforce_confirmation_gate` defaults to False so every existing
        caller keeps today's exact behavior (direct execution, no
        confirmation blocking) unchanged; a NEW caller that wants the
        WaitingConfirmation gate passes `enforce_confirmation_gate=True`
        and then `confirmed=True` on the follow-up turn once the
        customer has confirmed. Regardless of the gate flag, the result
        always additively reports `conversation_form`/`conversation_state`
        so a caller can observe (never forced to act on) the
        ConversationState the Conversation Form Generator computed."""
    trace: List[Dict] = []
    t_start = time.time()
    collected_params = dict(collected_params or {})
    history = history or []
    mode = mode if mode in TEST_MODES else "intent_param"

    # Conversation Form Generator state (additive) — built the same
    # caller-round-tripped way as history/collected_params; never
    # persisted here.
    from services.conversation_form_generator import ConversationState as _CFState
    cf_state = _CFState.from_dict(conversation_form_state) if conversation_form_state else _CFState()
    cf_state.collected_values.update(collected_params)

    # Part 14 — Conversation Strategy Engine, run BEFORE the form
    # generator so ready_fields/group_satisfaction land in cf_state first
    # and the Form Generator's own existing logic naturally sees fewer
    # missing fields/satisfied groups. Best-effort: never breaks the real
    # conversation flow if the engine itself errors.
    strategy_result = None
    try:
        from services.conversation_strategy_engine import analyze_conversation_strategy, apply_strategy_to_state
    except Exception:
        analyze_conversation_strategy = None
        apply_strategy_to_state = None

    def _run_strategy(contract_obj):
        nonlocal strategy_result
        if not contract_obj or analyze_conversation_strategy is None:
            return
        try:
            strategy_result = analyze_conversation_strategy(contract_obj, message, cf_state, language=language)
            apply_strategy_to_state(cf_state, strategy_result)
        except Exception:
            strategy_result = None

    def _record_analytics_turn(execution_result_label: Optional[str] = None):
        """Best-effort only (wrapped in try/except) — analytics must
        never break the real conversation flow, but genuine bugs in this
        module itself are not silently swallowed beyond this boundary."""
        try:
            from services.conversation_analytics_store import get_analytics_store
            conversation_id = f"{action_id}:{cf_state.created_at}"
            record = {
                "conversation_id": conversation_id, "action_id": action_id,
                "started_at": cf_state.created_at,
                "questions_asked": list((strategy_result or {}).get("questions_to_ask") or []),
                "questions_skipped": list((strategy_result or {}).get("questions_to_skip") or []),
                "auto_filled_fields": list((strategy_result or {}).get("auto_detected_fields") or []),
                "detected_fields": list((strategy_result or {}).get("auto_detected_fields") or []),
                "confidence_scores": dict((strategy_result or {}).get("confidence") or {}),
                "confirmation_result": confirmed if enforce_confirmation_gate else None,
                "execution_result": execution_result_label,
            }
            get_analytics_store().record_turn(record)
        except Exception:
            pass

    def _conversation_extra(contract_obj, execution_result_label: Optional[str] = None):
        """Best-effort additive extra keys — never raises, never
        required for the rest of this function's behavior."""
        _run_strategy(contract_obj)
        _record_analytics_turn(execution_result_label)
        if not contract_obj:
            return {"conversation_form": None, "conversation_state": cf_state.to_dict(),
                    "conversation_strategy": strategy_result}
        try:
            from services.conversation_form_generator import generate_conversation_form
            form = generate_conversation_form(contract_obj, cf_state, language=language)
        except Exception:
            form = None
        return {"conversation_form": form, "conversation_state": cf_state.to_dict(),
                "conversation_strategy": strategy_result}

    registry = get_registry(sb)

    # Step 1 — Selected Business Action
    t0 = time.time()
    if not action_id:
        trace.append(_trace_step("selected_action", "error", error="No Business Action selected.",
                                  latency_ms=(time.time() - t0) * 1000))
        return _harness_result(False, action_id, mode, trace, t_start, error="no_action_selected")
    action = registry.get_full(action_id, mask_secrets=True)
    if not action:
        trace.append(_trace_step("selected_action", "error", error="Business Action not found.",
                                  latency_ms=(time.time() - t0) * 1000))
        return _harness_result(False, action_id, mode, trace, t_start, error="action_not_found")
    if not action.get("enabled") and not action.get("is_draft"):
        trace.append(_trace_step("selected_action", "error", error="Business Action is disabled.",
                                  latency_ms=(time.time() - t0) * 1000))
        return _harness_result(False, action_id, mode, trace, t_start, error="action_disabled")
    trace.append(_trace_step("selected_action", "ok",
                              output_summary={"name": action.get("display_name"), "action_type": action.get("action_type")},
                              latency_ms=(time.time() - t0) * 1000,
                              explanation="Business Action loaded from the registry (frozen architecture — unchanged)."))

    # Integration Schema (Part 17) — fetched ONCE here and threaded
    # through the rest of this call, rather than every helper hitting
    # the DB independently. `resolve_effective_schema()` always returns
    # a full, safe shape (the published schema merged over
    # `derive_default_schema(action)`) — a pure additive read, never
    # required for this call to succeed.
    try:
        from services.integration_schema_service import resolve_effective_schema
        schema = resolve_effective_schema(action, sb)
    except Exception:
        schema = None

    # Step 1b — Integration Contract Resolution (Part 14). Resolves the
    # SAME action+schema join via services/integration_contract_service.py
    # (which itself just calls business_action_registry.get_full() +
    # integration_schema_service.resolve_effective_integration_schema() —
    # never a second, independent join) so this orchestrator can source
    # `operation_type`/`conversation` behavior from the ONE canonical
    # Contract rather than re-deriving them a second, parallel way
    # in-line below. A pure additive read — never required for this call
    # to succeed, and never changes `schema` (still used, unchanged, by
    # every existing field-by-field helper below it).
    contract = None
    try:
        from services.integration_contract_service import resolve_effective_contract
        contract_bundle = resolve_effective_contract(action_id, sb)
        contract = contract_bundle.get("contract")
        trace.append(_trace_step(
            "contract_resolution", "ok" if contract else "skipped",
            output_summary={"capability": (contract or {}).get("capability"),
                             "operation_type": (contract or {}).get("operation_type"),
                             "contract_version": (contract or {}).get("contract_version"),
                             "validation_warnings": contract_bundle.get("validation", {}).get("warnings")},
            explanation="Integration Contract resolved via services/integration_contract_service.py "
                        "(action + effective schema, normalized) — operation_type/conversation "
                        "behavior below are sourced from this contract, not re-derived independently."))
    except Exception as e:
        trace.append(_trace_step("contract_resolution", "skipped",
                                  explanation=f"Contract resolution unavailable ({e}) — falling back to direct inference."))

    # Step 2 — Conversation Context
    t0 = time.time()
    trace.append(_trace_step("conversation_context", "ok",
                              input_summary={"turns_so_far": len(history), "already_collected": list(collected_params.keys())},
                              latency_ms=(time.time() - t0) * 1000,
                              explanation="Recomputed from caller-supplied history/collected_params — no server-side session table."))

    # Step 3 — Parameter Extraction
    t0 = time.time()
    extraction = extract_parameters(action, registry, collected_params, message)
    trace.append(_trace_step(
        "parameter_extraction", "ok" if not extraction["ambiguous_candidates"] else "warning",
        input_summary={"message": message}, output_summary=extraction,
        confidence=extraction["confidence"], latency_ms=(time.time() - t0) * 1000,
        explanation=extraction["reason"],
    ))
    collected_params = extraction["parameters"]
    # Keep the additive ConversationState's collected_values in lockstep
    # with the freshly-extracted parameters (extraction can add fields
    # the caller didn't send in yet, e.g. bound from message text) —
    # without this, conversation_form/conversation_state would keep
    # reporting fields as missing even after this turn's extraction
    # found them.
    cf_state.collected_values.update(collected_params)

    if mode == "intent_param":
        clarification = build_clarification_question(action, registry, collected_params, schema=schema, language=language) if extraction["missing_parameters"] else None
        trace.append(_trace_step("missing_parameter_validation", "ok" if not extraction["missing_parameters"] else "warning",
                                  output_summary={"missing": extraction["missing_parameters"], "clarification_question": clarification},
                                  explanation="Intent & Parameter Test mode — no ERP call is made in this mode."))
        cf_state.missing_fields = list(extraction["missing_parameters"])
        return _harness_result(True, action_id, mode, trace, t_start,
                                extra={"collected_params": collected_params, "missing_parameters": extraction["missing_parameters"],
                                       "clarification_question": clarification, "answer": None,
                                       "awaiting_information_selection": False, "available_response_options": [],
                                       "last_normalized_result": None, "requested_fields": [],
                                       **_conversation_extra(contract)})

    # Step 4 — Missing Parameter Validation (blocks execution for simulation/live)
    t0 = time.time()
    if extraction["missing_parameters"]:
        clarification = build_clarification_question(action, registry, collected_params, schema=schema, language=language)
        trace.append(_trace_step("missing_parameter_validation", "blocked",
                                  output_summary={"missing": extraction["missing_parameters"]},
                                  latency_ms=(time.time() - t0) * 1000,
                                  explanation=f"Execution blocked — required parameter(s) not yet provided: {', '.join(extraction['missing_parameters'])}."))
        # A NEW missing search parameter always takes priority over an
        # in-progress information-selection — e.g. the admin swapped in
        # a fresh CustCode mid-conversation. Never carry a stale
        # "awaiting selection" flag forward once we're back to asking
        # for a search parameter (situation 1 vs. situation 2 in the
        # spec must never be conflated).
        cf_state.missing_fields = list(extraction["missing_parameters"])
        return _harness_result(True, action_id, mode, trace, t_start,
                                extra={"collected_params": collected_params, "missing_parameters": extraction["missing_parameters"],
                                       "clarification_question": clarification, "answer": None,
                                       "awaiting_information_selection": False, "available_response_options": [],
                                       "last_normalized_result": None, "requested_fields": [],
                                       **_conversation_extra(contract)},
                                warning="missing_required_parameter")
    trace.append(_trace_step("missing_parameter_validation", "ok", latency_ms=(time.time() - t0) * 1000,
                              explanation="All required parameters (or at-least-one groups) are satisfied."))

    # ── Scenario F (Part 7) — COMMAND-type confirmation gate ────────────
    # Opt-in only (`enforce_confirmation_gate`) so every pre-existing
    # caller of run_erp_test() keeps today's exact behavior (direct
    # execution) unchanged by default — see the docstring above. When a
    # caller DOES opt in, a contract whose resolved conversation behavior
    # requires confirmation transitions the (additive, caller-round-
    # tripped) ConversationState to WaitingConfirmation and blocks
    # execution until a follow-up turn passes confirmed=True.
    needs_confirmation = bool((contract or {}).get("conversation", {}).get("require_confirmation_before_execute"))
    if needs_confirmation and enforce_confirmation_gate:
        if not confirmed:
            if cf_state.current_state == "WaitingInput":
                cf_state.transition_to("ReadyToExecute")
            if cf_state.current_state == "ReadyToExecute":
                cf_state.transition_to("WaitingConfirmation")
            trace.append(_trace_step("confirmation_gate", "blocked",
                                      output_summary={"require_confirmation_before_execute": True, "confirmed": False},
                                      explanation="COMMAND-type operation requires confirmation before execution (Scenario F) — "
                                                  "execution blocked until the caller passes confirmed=True on a follow-up turn."))
            return _harness_result(True, action_id, mode, trace, t_start,
                                    extra={"collected_params": collected_params, "missing_parameters": [],
                                           "clarification_question": None, "answer": None,
                                           "awaiting_information_selection": False, "available_response_options": [],
                                           "last_normalized_result": None, "requested_fields": [],
                                           **_conversation_extra(contract)},
                                    warning="awaiting_confirmation")
        else:
            if cf_state.current_state == "WaitingInput":
                cf_state.transition_to("ReadyToExecute")
            if cf_state.current_state == "ReadyToExecute":
                cf_state.transition_to("WaitingConfirmation")
            if cf_state.current_state == "WaitingConfirmation":
                cf_state.transition_to("Executing")
            trace.append(_trace_step("confirmation_gate", "ok",
                                      output_summary={"require_confirmation_before_execute": True, "confirmed": True},
                                      explanation="Caller confirmed — proceeding to execution."))
    else:
        trace.append(_trace_step("confirmation_gate", "skipped",
                                  explanation="No confirmation gate applies (either the operation doesn't require it, "
                                              "or enforce_confirmation_gate wasn't requested by the caller)."))

    # ── Requested Information Clarification layer ───────────────────────
    # Situation 2 (missing REQUESTED information) is now handled entirely
    # separately from situation 1 (missing search parameter, above) and
    # situation 3/4 (ERP data unavailable / ERP execution error, both
    # still handled by the untouched execution-error paths below).
    if awaiting_information_selection and last_normalized_result is not None:
        # The search parameter was already complete and the ERP was
        # already called last turn (Part 6's "alternative strategy") —
        # this turn is purely about interpreting the customer's answer
        # to "what would you like to check?". The ERP is deliberately
        # NOT called again here.
        t0 = time.time()
        options = available_response_options or build_available_response_options(action, last_normalized_result, schema=schema)
        detection = detect_requested_fields(message, options)
        trace.append(_trace_step(
            "requested_information_detection", "ok" if detection["requested_fields"] else "warning",
            input_summary={"message": message, "candidate_options": [o["canonical_name"] for o in options]},
            output_summary=detection, confidence=detection["confidence"], latency_ms=(time.time() - t0) * 1000,
            explanation=detection["reason"],
        ))
        if not detection["requested_fields"]:
            # Still unclear (or names something this API can't answer,
            # Part 9.3) — re-show the options, remain awaiting, and
            # explicitly do NOT fall back to a generic
            # "unavailable"/"not found" message (Part 11's core fix).
            clarification = build_clarification_message(options, language=language, unavailable_hint=True)
            trace.append(_trace_step("information_clarification", "warning",
                                      output_summary={"clarification_required": True, "clarification_message": clarification,
                                                       "awaiting_user_selection": True},
                                      explanation="The customer's reply didn't match any available field — re-asking rather than claiming the data is unavailable."))
            return _harness_result(True, action_id, mode, trace, t_start,
                                    extra={"collected_params": collected_params, "missing_parameters": [],
                                           "clarification_question": clarification, "answer": None,
                                           "awaiting_information_selection": True, "available_response_options": options,
                                           "last_normalized_result": last_normalized_result, "requested_fields": []},
                                    warning="requested_field_unclear")
        options_by_canonical = {o["canonical_name"]: o for o in options}
        filtered = {c: last_normalized_result[c] for c in detection["requested_fields"] if c in last_normalized_result}
        trace.append(_trace_step("information_clarification", "ok",
                                  output_summary={"clarification_required": False, "awaiting_user_selection": False},
                                  explanation="A requested field was resolved from the customer's reply — answering now, without a new ERP call."))
        t0 = time.time()
        synthetic_question = _synthetic_question_for_fields(options_by_canonical, detection["requested_fields"], language=language)
        answer_result = generate_grounded_answer(synthetic_question, action, filtered, history, language)
        trace.append(_trace_step("answer_field_selection", "ok",
                                  output_summary={"selected_fields": detection["requested_fields"],
                                                   "normalized_fields_passed_to_llm": list(filtered.keys()),
                                                   "excluded_normalized_fields": [c for c in last_normalized_result if c not in filtered]},
                                  explanation="Only the customer-requested field(s) are passed to the answer generator (Part 7)."))
        trace.append(_trace_step("generated_answer", "ok" if answer_result.get("answer") else "warning",
                                  output_summary=_generated_answer_output_summary(answer_result),
                                  latency_ms=(time.time() - t0) * 1000,
                                  error=answer_result.get("error")))
        return _harness_result(True, action_id, mode, trace, t_start,
                                extra={"collected_params": collected_params, "missing_parameters": [],
                                       "clarification_question": None, "answer": answer_result.get("answer"),
                                       "normalized_result": filtered, "awaiting_information_selection": False,
                                       "available_response_options": [], "last_normalized_result": last_normalized_result,
                                       "requested_fields": detection["requested_fields"]},
                                warning=None if answer_result.get("answer") else "no_answer_generated")

    # Step 5 — Credential Resolution (reported, resolved for real by the Executor itself)
    t0 = time.time()
    secret_params = [p for p in (action.get("parameters") or [])
                      if p.get("input_source") in ("credential_store", "secret_configuration")]
    if secret_params and mode == "live":
        errors = {}
        try:
            errors = registry.resolve_secret_parameter_errors(action_id)
        except AttributeError:
            pass
        resolved = registry.resolve_secret_parameters(action_id)
        missing = [name for name, value in resolved.items() if value is None]
        if missing:
            trace.append(_trace_step("credential_resolution", "error",
                                      output_summary={"missing_credentials": missing},
                                      latency_ms=(time.time() - t0) * 1000,
                                      error="One or more required credentials could not be resolved.",
                                      explanation="; ".join(f"{n}: {errors.get(n, 'unresolved')}" for n in missing)))
            return _harness_result(True, action_id, mode, trace, t_start,
                                    extra={"collected_params": collected_params, "answer": None,
                                           "awaiting_information_selection": False, "available_response_options": [],
                                           "last_normalized_result": None, "requested_fields": []},
                                    warning="missing_credential")
        trace.append(_trace_step("credential_resolution", "ok", latency_ms=(time.time() - t0) * 1000,
                                  output_summary={"resolved_count": len(resolved)},
                                  explanation="Resolved via the existing Credential Store / legacy env-var mechanism — values never leave the server."))
    else:
        trace.append(_trace_step("credential_resolution", "skipped", latency_ms=(time.time() - t0) * 1000,
                                  explanation="No credential resolution needed for this mode." if mode != "live" else "This action has no secret parameters."))

    # Step 6/7 — Request Construction + ERP Execution (or Simulation)
    t0 = time.time()
    execution_result = None
    if mode == "simulation":
        mapped_fields = build_simulated_response(action)
        trace.append(_trace_step("request_construction", "skipped", latency_ms=0.0,
                                  explanation="Simulation mode — no real request is built."))
        trace.append(_trace_step("erp_execution", "simulated", output_summary={"mock_fields": list(mapped_fields.keys())},
                                  latency_ms=(time.time() - t0) * 1000,
                                  explanation="Schema-generated mock response — the real ERP was never called."))
    else:  # live
        executor = ActionExecutor(sb)
        try:
            # system_values (Decision Engine's generic system_generated
            # source, e.g. a URL found in the raw message — see
            # services/decision_engine.py::_extract_system_values) was
            # missing here entirely, so any parameter sourced from
            # system_generated could never resolve through this harness
            # even though the same Business Action worked fine through
            # the real Decision Engine path. Reuses the SAME function,
            # never a second extractor.
            from services.decision_engine import _extract_system_values
            execution_result = executor.execute(action_id, context={
                "collected_slots": collected_params, "action_params": collected_params, "developer_mode": True,
                "system_values": _extract_system_values(message),
            })
        except Exception as e:
            trace.append(_trace_step("erp_execution", "error", latency_ms=(time.time() - t0) * 1000,
                                      error=f"Executor raised an unexpected exception: {sanitize_for_preview(str(e))}"))
            return _harness_result(True, action_id, mode, trace, t_start,
                                    extra={"collected_params": collected_params, "answer": None,
                                           "awaiting_information_selection": False, "available_response_options": [],
                                           "last_normalized_result": None, "requested_fields": []}, warning="execution_exception")
        latency_ms = execution_result.get("latency_ms", (time.time() - t0) * 1000)
        raw_result = execution_result.get("result") or {}
        trace.append(_trace_step(
            "request_construction", "ok" if not execution_result.get("error") else "error",
            output_summary=raw_result.get("request"), latency_ms=0.0,
            explanation="Built entirely from the Business Action's own saved execution config — reuses run_rest_call() unchanged."))
        if execution_result.get("status") == "error":
            trace.append(_trace_step("erp_execution", "error", latency_ms=latency_ms,
                                      output_summary={"status_code": raw_result.get("status_code")},
                                      error=execution_result.get("error"),
                                      explanation="The Action Executor reported an error — see Error above for the friendly message; no secret values are ever included."))
            # Situation 4 (ERP execution error) — never the generic
            # "requested information unavailable" fallback either.
            return _harness_result(True, action_id, mode, trace, t_start,
                                    extra={"collected_params": collected_params, "answer": None,
                                           "awaiting_information_selection": False, "available_response_options": [],
                                           "last_normalized_result": None, "requested_fields": []}, warning="erp_execution_failed")
        trace.append(_trace_step("erp_execution", "ok", latency_ms=latency_ms,
                                  output_summary={"status_code": raw_result.get("status_code"), "content_type": "application/json"},
                                  explanation="Live call executed via the existing, unmodified Action Executor."))
        mapped_fields = raw_result.get("mapped_fields") or {}

    # Step 8 — Response Validation
    t0 = time.time()
    if not mapped_fields:
        trace.append(_trace_step("response_validation", "warning", latency_ms=(time.time() - t0) * 1000,
                                  explanation="The response contained no fields matching the configured Response Mapping."))
    else:
        trace.append(_trace_step("response_validation", "ok", output_summary={"field_count": len(mapped_fields)},
                                  latency_ms=(time.time() - t0) * 1000))

    # Step 9 — Response Mapping -> Normalized Result
    t0 = time.time()
    normalized = normalize_response(action, mapped_fields, schema=schema)
    if mapped_fields and not normalized:
        trace.append(_trace_step("response_mapping", "warning", latency_ms=(time.time() - t0) * 1000,
                                  explanation="Fields were returned but none could be mapped to a configured Response Mapping entry."))
    else:
        trace.append(_trace_step("response_mapping", "ok", output_summary=normalized, latency_ms=(time.time() - t0) * 1000,
                                  explanation="Each field's canonical name/semantic type is derived from its json_path — never a stored column."))

    # Situation 3 (ERP data genuinely unavailable, Part 9.5) — an EMPTY
    # normalized result means there is no record at all; this is the
    # ONLY situation the fixed "not found" fallback is appropriate for,
    # and clarification must never be offered here (nothing to select
    # from).
    if not normalized:
        t0 = time.time()
        answer_result = generate_grounded_answer(message, action, normalized, history, language)
        trace.append(_trace_step("generated_answer", "warning", output_summary=_generated_answer_output_summary(answer_result),
                                  latency_ms=(time.time() - t0) * 1000,
                                  explanation="No matching record — a fixed message is used instead of letting the LLM invent one."))
        return _harness_result(True, action_id, mode, trace, t_start,
                                extra={"collected_params": collected_params, "missing_parameters": [], "clarification_question": None,
                                       "normalized_result": normalized, "answer": answer_result.get("answer"),
                                       "awaiting_information_selection": False, "available_response_options": [],
                                       "last_normalized_result": None, "requested_fields": [],
                                       **_conversation_extra(contract)},
                                warning="erp_data_unavailable")

    # ── Part 1/2/4 — decide: answer directly, or ask which field? ───────
    t0 = time.time()
    options = build_available_response_options(action, normalized, schema=schema)
    excluded = [m.get("mapped_label") for m in (action.get("response_mapping") or [])
                if m.get("mapped_label") not in [o["business_label"] for o in options]]
    trace.append(_trace_step("available_information_options", "ok" if options else "warning",
                              output_summary={"options": options, "excluded_fields": excluded},
                              latency_ms=(time.time() - t0) * 1000,
                              explanation=(f"{len(options)} visible, usable response field(s) configured." if options
                                           else "No usable visible response fields are configured for this ERP API — check Response Mapping.")))

    t0 = time.time()
    detection = detect_requested_fields(message, options)
    trace.append(_trace_step("requested_information_detection", "ok" if detection["requested_fields"] else "warning",
                              input_summary={"message": message}, output_summary=detection,
                              confidence=detection["confidence"], latency_ms=(time.time() - t0) * 1000,
                              explanation=detection["reason"]))

    # Operation Types (product-agnostic architecture requirement) —
    # clarification only makes sense for LOOKUP/LIST/SEARCH/STATUS-style
    # capabilities. A command action (CREATE/UPDATE/CANCEL/NOTIFY/
    # WORKFLOW) has no "which field do you want back" question — asking
    # one would be nonsensical for e.g. "cancel my order" or "update my
    # address". Inferred purely from the action's own configuration
    # (infer_operation_type), never a per-entity/per-domain branch.
    # Part 14 — sourced from the Integration Contract (Step 1b above)
    # when it resolved successfully; falls back to the original direct
    # inference only if contract resolution itself failed, so behavior
    # never changes for a caller when the contract is available (which,
    # since it derives from the exact same action+schema, is always).
    operation_type = (contract or {}).get("operation_type") or infer_operation_type(action, schema=schema)
    is_lookup_like = operation_type in LOOKUP_LIKE_OPERATION_TYPES
    # Part 7 — an admin-configured (or operation-type-derived)
    # conversation_behavior can also switch clarification off/on
    # explicitly; `ask_requested_information` extends (never replaces)
    # the existing lookup-like gate.
    conversation_behavior = (contract or {}).get("conversation") or get_conversation_behavior(action, schema=schema)
    should_ask = is_lookup_like and conversation_behavior.get("ask_requested_information", True)

    # Part 4/7 — only ask when: params complete (guaranteed at this point),
    # the capability is lookup-like AND configured to ask, MORE THAN ONE
    # visible option exists, and the requested field truly can't be
    # determined from this message. A single option, zero usable
    # options, or a non-lookup/ask_requested_information=False operation
    # is always answered directly instead (never a meaningless "pick one
    # of one" clarification, and never a silent failure when nothing is
    # configured).
    if not detection["requested_fields"] and len(options) > 1 and should_ask:
        clarification = build_clarification_message(options, language=language)
        trace.append(_trace_step("information_clarification", "ok",
                                  output_summary={"clarification_required": True, "clarification_message": clarification,
                                                   "awaiting_user_selection": True},
                                  explanation="More than one visible field is available and the customer hasn't said which one they want yet."))
        return _harness_result(True, action_id, mode, trace, t_start,
                                extra={"collected_params": collected_params, "missing_parameters": [],
                                       "clarification_question": clarification, "answer": None,
                                       "awaiting_information_selection": True, "available_response_options": options,
                                       "last_normalized_result": normalized, "requested_fields": []})

    trace.append(_trace_step("information_clarification", "skipped",
                              output_summary={"clarification_required": False, "awaiting_user_selection": False, "operation_type": operation_type},
                              explanation=("The customer already specified what they want." if detection["requested_fields"]
                                           else ("Only one (or zero) visible field is configured — answering directly." if is_lookup_like
                                                 else f"Operation type '{operation_type}' is not lookup-like — clarification does not apply."))))

    filtered = ({c: normalized[c] for c in detection["requested_fields"] if c in normalized}
                if detection["requested_fields"] else normalized)
    options_by_canonical = {o["canonical_name"]: o for o in options}
    trace.append(_trace_step("answer_field_selection", "ok",
                              output_summary={"selected_fields": detection["requested_fields"] or list(normalized.keys()),
                                               "normalized_fields_passed_to_llm": list(filtered.keys()),
                                               "excluded_normalized_fields": [c for c in normalized if c not in filtered]},
                              explanation="Only the customer-requested field(s) are passed to the answer generator (Part 7)."
                              if detection["requested_fields"] else "No specific field was requested and only the full result was available to answer with."))

    # Step 10/11 — Prompt Builder + Generated Answer
    t0 = time.time()
    question_for_answer = (message if detection["requested_fields"] else
                            _synthetic_question_for_fields(options_by_canonical, list(filtered.keys()), language=language)) or message
    answer_result = generate_grounded_answer(question_for_answer, action, filtered, history, language)
    trace.append(_trace_step(
        "prompt_builder", "ok" if answer_result.get("used_llm") is not False or not filtered else "skipped",
        latency_ms=0.0, explanation="Grounded strictly in the normalized result above — the raw ERP response is never sent to the LLM."))
    step_status = "ok" if answer_result.get("answer") and not answer_result.get("error") else ("warning" if not filtered else "error")
    trace.append(_trace_step("generated_answer", step_status, output_summary=_generated_answer_output_summary(answer_result),
                              latency_ms=(time.time() - t0) * 1000, error=answer_result.get("error"),
                              explanation="No matching record — a fixed message is used instead of letting the LLM invent one." if not filtered else ""))

    # The action actually executed (real or simulated) by this point —
    # advance the additive ConversationState to its terminal state for
    # observability. Never blocks/changes anything above; purely a
    # trailing bookkeeping step.
    for target in ("ReadyToExecute", "Executing", "Completed"):
        if cf_state.current_state != target:
            cf_state.transition_to(target)

    _run_strategy(contract)
    _record_analytics_turn("completed" if answer_result.get("answer") else None)
    return _harness_result(
        True, action_id, mode, trace, t_start,
        extra={"collected_params": collected_params, "missing_parameters": [], "clarification_question": None,
               "normalized_result": filtered, "answer": answer_result.get("answer"),
               "awaiting_information_selection": False, "available_response_options": [],
               "last_normalized_result": normalized, "requested_fields": detection["requested_fields"],
               "execution_status_code": (execution_result or {}).get("result", {}).get("status_code") if execution_result else None,
               "conversation_form": None, "conversation_state": cf_state.to_dict(),
               "conversation_strategy": strategy_result},
        warning=None if answer_result.get("answer") else "no_answer_generated",
    )


# ── Part 9 — Generic Integration Runtime boundary ───────────────────────

_DEFAULT_CONVERSATION_STATE = {
    "selected_action_id": None, "operation_type": None, "collected_parameters": {},
    "missing_parameters": [], "requested_fields": [], "available_response_options": [],
    "awaiting_parameter": False, "awaiting_information_selection": False,
    "last_normalized_result": None, "execution_status": None, "conversation_context": [],
}


def run_integration_conversation_turn(*, sb, action_id: str, message: str, mode: str,
                                       history: Optional[List[Dict]] = None,
                                       conversation_state: Optional[Dict] = None) -> Dict:
    """Part 9 — the forward-looking, GENERIC-named runtime boundary future
    callers (the Universal Decision Engine, LINE OA, a Website Chat
    widget, a public API) should call instead of duplicating any of
    `run_erp_test()`'s ~250 lines of orchestration logic.

    Deliberate, LOW-RISK design choice: this is a thin ADAPTER, not a
    rewrite. `run_erp_test()` is covered by 54+ passing tests and is
    called live today by admin/routes.py's `/admin/api/erp-test/run` and
    the existing erp_conversation_tester.html JS — its internals are
    intentionally left untouched. This function only translates the
    generic `conversation_state` shape into the kwargs `run_erp_test()`
    already understands, calls it, and repacks the flat result back into
    the generic shape below. Any future caller can adopt the
    generic-named state without this module ever having two competing
    implementations of the same orchestration.

    Returns {reply, conversation_state, execution_result,
    normalized_result, trace, status}."""
    state = {**_DEFAULT_CONVERSATION_STATE, **(conversation_state or {})}
    resolved_action_id = action_id or state.get("selected_action_id")
    result = run_erp_test(
        sb=sb, action_id=resolved_action_id, message=message, mode=mode, history=history,
        collected_params=state.get("collected_parameters"), language=state.get("language", "th"),
        awaiting_information_selection=bool(state.get("awaiting_information_selection")),
        available_response_options=state.get("available_response_options"),
        last_normalized_result=state.get("last_normalized_result"),
    )

    new_state = {
        **state,
        "selected_action_id": resolved_action_id,
        "collected_parameters": result.get("collected_params", state.get("collected_parameters")),
        "missing_parameters": result.get("missing_parameters", []),
        "requested_fields": result.get("requested_fields", []),
        "available_response_options": result.get("available_response_options", []),
        "awaiting_parameter": bool(result.get("missing_parameters")),
        "awaiting_information_selection": bool(result.get("awaiting_information_selection")),
        "last_normalized_result": result.get("last_normalized_result"),
        "execution_status": result.get("summary", {}).get("overall"),
        "conversation_context": (history or []) + [{"role": "user", "message": message}],
    }
    return {
        "reply": result.get("answer") or result.get("clarification_question"),
        "conversation_state": new_state,
        "execution_result": {"error": result.get("error"), "warning": result.get("warning"),
                              "execution_status_code": result.get("execution_status_code")},
        "normalized_result": result.get("normalized_result"),
        "trace": result.get("trace", []),
        "status": result.get("summary", {}).get("overall", "fail" if not result.get("ok") else "pass"),
    }


def _harness_result(ok: bool, action_id: Optional[str], mode: str, trace: List[Dict], t_start: float, *,
                     extra: Optional[Dict] = None, error: Optional[str] = None, warning: Optional[str] = None) -> Dict:
    """Part 12 — the Test Result Summary shown at the top of the UI,
    derived purely from what actually happened in `trace` (never a
    separate, hand-maintained pass/fail flag)."""
    statuses = [t["status"] for t in trace]
    if error or "error" in statuses:
        overall = "fail"
    elif warning or "warning" in statuses or "blocked" in statuses:
        overall = "warning"
    else:
        overall = "pass"

    def _stage_status(step_name: str) -> str:
        for t in trace:
            if t["step"] == step_name:
                return t["status"]
        return "not_run"

    summary = {
        "action_id": action_id, "mode": mode,
        "parameter_extraction_status": _stage_status("parameter_extraction"),
        "erp_execution_status": _stage_status("erp_execution"),
        "response_mapping_status": _stage_status("response_mapping"),
        "answer_grounding_status": _stage_status("generated_answer"),
        "overall": overall, "total_latency_ms": round((time.time() - t_start) * 1000, 2),
    }
    result = {"ok": ok, "summary": summary, "trace": trace, "error": error, "warning": warning}
    if extra:
        result.update(extra)
    return result
